"""Build a media inventory across all account archives and measure dedup potential.

Stage 1 (no network): walk every archive, collect media rows, group them by
    media identity (media_key, fallback canonical URL).
Stage 2 (network, optional): HEAD every distinct media URL to get exact byte size
    without downloading the file.

Outputs (under work/inventory/):
    <account>.jsonl      one line per media row, with canonical download URL
    media_index.jsonl    one line per distinct media identity, with size
    summary.json         totals, per-account and cross-account overlap
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    INVENTORY_DIR,
    WORK,
    canonical_media_url,
    human_bytes,
    iter_rows,
    load_accounts,
)

SIZE_CACHE = WORK / "media_sizes.json"
_cache_lock = threading.Lock()


def load_size_cache() -> dict:
    if SIZE_CACHE.exists():
        try:
            return json.loads(SIZE_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_size_cache(cache: dict) -> None:
    with _cache_lock:
        tmp = SIZE_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SIZE_CACHE)


def collect(accounts: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for acct in accounts:
        aid = acct["id"]
        tweet_meta: dict[str, dict] = {}
        media_rows = []
        for row in iter_rows(aid):
            rtype = row.get("record_type")
            if rtype == "tweet":
                tid = row.get("tweet_id")
                if tid:
                    tweet_meta[tid] = {
                        "handle": row.get("author_username"),
                        "name": row.get("author_display_name"),
                        "created_at": row.get("created_at"),
                        "collections": row.get("collection_type"),
                    }
            elif rtype == "media":
                media_rows.append(row)
        print(f"  {aid}: {len(tweet_meta)} tweets, {len(media_rows)} media rows", flush=True)
        for row in media_rows:
            tid = row.get("tweet_id")
            meta = tweet_meta.get(tid, {})
            mtype = row.get("media_type")
            url = row.get("media_url")
            rows.append(
                {
                    "account": aid,
                    "label": acct.get("label") or aid,
                    "tweet_id": tid,
                    "handle": meta.get("handle"),
                    "created_at": meta.get("created_at"),
                    "collections": meta.get("collections"),
                    "media_key": row.get("media_key"),
                    "media_type": mtype,
                    "media_url": url,
                    "download_url": canonical_media_url(mtype, url),
                    "thumbnail_url": row.get("thumbnail_url"),
                    "width": row.get("width"),
                    "height": row.get("height"),
                    "duration_millis": row.get("duration_millis"),
                    "position": row.get("position"),
                    "sha256": row.get("sha256"),
                    "byte_size": row.get("byte_size"),
                    "download_state": row.get("download_state"),
                }
            )
    return rows


def identity(row: dict) -> str:
    """Stable media identity: X media_key when present, else canonical URL."""
    key = row.get("media_key")
    if key:
        return f"mk:{key}"
    return f"url:{row.get('download_url') or row.get('media_url')}"


def size_of(client, url: str, cache: dict) -> dict:
    if url in cache:
        return cache[url]
    out = {"size": None, "ctype": None, "status": None}
    try:
        r = client.head(url)
        out["status"] = r.status_code
        ctype = r.headers.get("content-type")
        out["ctype"] = ctype.split(";")[0] if ctype else None
        cl = r.headers.get("content-length")
        if r.status_code in (200, 206) and cl and cl.isdigit() and int(cl) > 0:
            out["size"] = int(cl)
        else:
            r2 = client.get(url, headers={"Range": "bytes=0-0"})
            out["status"] = r2.status_code
            cr = r2.headers.get("content-range")
            ctype = r2.headers.get("content-type") or ctype
            out["ctype"] = ctype.split(";")[0] if ctype else None
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[1]
                if total.isdigit():
                    out["size"] = int(total)
            elif r2.headers.get("content-length"):
                out["size"] = int(r2.headers["content-length"])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    cache[url] = out
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", action="store_true", help="HEAD every distinct media URL")
    ap.add_argument("--limit", type=int, default=0, help="cap number of URLs to size")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--accounts", default="", help="comma separated account ids (default all)")
    args = ap.parse_args()

    accounts = load_accounts()
    if args.accounts:
        wanted = {a.strip() for a in args.accounts.split(",") if a.strip()}
        accounts = [a for a in accounts if a["id"] in wanted]
    if not accounts:
        print("no accounts registered")
        return 2

    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    print("collecting rows...", flush=True)
    rows = collect(accounts)

    by_account: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_account[row["account"]].append(row)
    for aid, arows in by_account.items():
        with open(INVENTORY_DIR / f"{aid}.jsonl", "w", encoding="utf-8") as f:
            for row in arows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    identities: dict[str, dict] = {}
    for row in rows:
        key = identity(row)
        entry = identities.get(key)
        if entry is None:
            entry = {
                "identity": key,
                "media_key": row.get("media_key"),
                "media_type": row.get("media_type"),
                "download_url": row.get("download_url") or row.get("media_url"),
                "width": row.get("width"),
                "height": row.get("height"),
                "duration_millis": row.get("duration_millis"),
                "accounts": [],
                "rows": 0,
                "example_tweet": row.get("tweet_id"),
                "example_handle": row.get("handle"),
                "size": row.get("byte_size"),
                "ctype": None,
            }
            identities[key] = entry
        entry["rows"] += 1
        if row["account"] not in entry["accounts"]:
            entry["accounts"].append(row["account"])

    cache = load_size_cache()
    for entry in identities.values():
        info = cache.get(entry.get("download_url") or "")
        if info and info.get("size") and not entry.get("size"):
            entry["size"] = info["size"]
            entry["ctype"] = info.get("ctype")

    if args.size:
        todo = [e for e in identities.values() if e["download_url"] and not e.get("size")]
        if args.limit:
            todo = todo[: args.limit]
        print(f"sizing {len(todo)} distinct media urls...", flush=True)
        import httpx

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36",
            "Referer": "https://x.com/",
        }
        done = 0
        with httpx.Client(headers=headers, timeout=20, follow_redirects=True) as client:
            def work(entry: dict) -> None:
                nonlocal done
                info = size_of(client, entry["download_url"], cache)
                entry["size"] = info.get("size")
                entry["ctype"] = info.get("ctype")
                entry["status"] = info.get("status")
                if info.get("error"):
                    entry["error"] = info["error"]
                done += 1
                if done % 100 == 0:
                    print(f"  sized {done}/{len(todo)}", flush=True)
                    save_size_cache(cache)

            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                list(pool.map(work, todo))
        save_size_cache(cache)

    with open(INVENTORY_DIR / "media_index.jsonl", "w", encoding="utf-8") as f:
        for entry in identities.values():
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def total_size(items) -> int:
        return sum((e.get("size") or 0) for e in items)

    naive = sum((identities[identity(r)].get("size") or 0) for r in rows)
    dedup = total_size(identities.values())
    missing = sum(1 for e in identities.values() if e.get("size") is None)

    shared = [e for e in identities.values() if len(e["accounts"]) > 1]
    per_account = {}
    for aid, arows in by_account.items():
        keys = {identity(r) for r in arows}
        per_account[aid] = {
            "media_rows": len(arows),
            "distinct": len(keys),
            "bytes_naive": sum((identities[identity(r)].get("size") or 0) for r in arows),
            "bytes_dedup": total_size([e for e in identities.values() if aid in e["accounts"]]),
        }

    pair = Counter()
    for e in shared:
        accs = sorted(e["accounts"])
        for i in range(len(accs)):
            for j in range(i + 1, len(accs)):
                pair[f"{accs[i]}+{accs[j]}"] += 1

    by_type: dict[str, dict] = {}
    for e in identities.values():
        t = e.get("media_type") or "unknown"
        slot = by_type.setdefault(t, {"count": 0, "bytes": 0})
        slot["count"] += 1
        slot["bytes"] += e["size"] or 0

    summary = {
        "accounts": per_account,
        "totals": {
            "media_rows": len(rows),
            "distinct_identities": len(identities),
            "naive_bytes": naive,
            "dedup_bytes": dedup,
            "saving_bytes": naive - dedup,
            "saving_ratio": round((naive - dedup) / naive, 4) if naive else None,
            "unsized": missing,
            "naive_human": human_bytes(naive),
            "dedup_human": human_bytes(dedup),
            "saving_human": human_bytes(naive - dedup),
        },
        "shared": {
            "identities_in_2plus_accounts": len(shared),
            "rows_covered": sum(e["rows"] for e in shared),
            "bytes_saved_by_sharing": total_size(shared) * 1,
            "pairs": dict(pair.most_common(10)),
        },
        "by_type": by_type,
    }
    (INVENTORY_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    t = summary["totals"]
    print("\n=== inventory ===")
    for aid, s in per_account.items():
        print(f"  {aid}: rows={s['media_rows']} distinct={s['distinct']} bytes={human_bytes(s['bytes_naive'])}")
    print(f"  total rows={t['media_rows']} distinct={t['distinct_identities']}")
    print(f"  naive={t['naive_human']}  dedup={t['dedup_human']}  saving={t['saving_human']} ({t['saving_ratio']})")
    print(f"  shared identities={summary['shared']['identities_in_2plus_accounts']} unsized={missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
