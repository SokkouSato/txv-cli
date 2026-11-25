"""Exact per-tier video sizes: HEAD every mp4 variant instead of extrapolating.

Linear bitrate extrapolation ignores the audio track (AAC ~128k does not shrink
with resolution), so we measure each variant URL directly.

Outputs work/variant_stats.json with exact totals per collection and the
audio-overhead estimate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import WORK, human_bytes, iter_rows, load_accounts  # noqa: E402

CACHE = WORK / "variant_sizes.json"
OUT = WORK / "variant_stats.json"
TIERS = ("best", "mid", "low")


def load_cache() -> dict:
    return json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}


def save_cache(cache: dict) -> None:
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    tmp.replace(CACHE)


def head_size(client, url: str) -> dict:
    try:
        r = client.head(url)
        cl = r.headers.get("content-length")
        if r.status_code in (200, 206) and cl and cl.isdigit():
            return {"size": int(cl), "status": r.status_code, "ctype": r.headers.get("content-type")}
        r2 = client.get(url, headers={"Range": "bytes=0-0"})
        cr = r2.headers.get("content-range")
        if cr and "/" in cr and cr.rsplit("/", 1)[1].isdigit():
            return {"size": int(cr.rsplit("/", 1)[1]), "status": r2.status_code,
                    "ctype": r2.headers.get("content-type")}
        return {"size": None, "status": r2.status_code}
    except Exception as exc:  # noqa: BLE001
        return {"size": None, "status": None, "error": str(exc)[:120]}


def pick(sizes: list[tuple[float, int]], tier: str) -> int | None:
    if not sizes:
        return None
    ordered = sorted(sizes, key=lambda x: x[0])
    if tier == "best":
        return ordered[-1][1]
    if tier == "low":
        return ordered[0][1]
    # store_v3.pick_variant picks index len//2 of the DESCENDING list; mirror that
    # exactly so the measurement matches what the downloader will actually fetch.
    return ordered[(len(ordered) - 1) - len(ordered) // 2][1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    accounts = load_accounts()
    videos: list[dict] = []
    for acct in accounts:
        tweets = {}
        for row in iter_rows(acct["id"]):
            if row.get("record_type") == "tweet":
                tweets[row.get("tweet_id")] = row
        for row in iter_rows(acct["id"]):
            if row.get("record_type") != "media":
                continue
            if row.get("media_type") not in ("video", "animated_gif"):
                continue
            try:
                variants = json.loads(row.get("variants_json") or "[]")
            except json.JSONDecodeError:
                variants = []
            mp4 = [(v.get("bitrate") or 0, v.get("url")) for v in variants
                   if v.get("content_type") == "video/mp4" and v.get("url")]
            if not mp4:
                continue
            t = tweets.get(row.get("tweet_id")) or {}
            videos.append({
                "account": acct["id"],
                "media_type": row.get("media_type"),
                "collection": t.get("collection_type") or "unknown",
                "media_key": row.get("media_key"),
                "variants": mp4,
            })
    uniq = {v["media_key"]: v for v in videos}

    cache = load_cache()
    todo = sorted({url for v in uniq.values() for _, url in v["variants"] if args.refetch or url not in cache})
    print(f"{len(videos)} video rows / {len(uniq)} unique, {len(todo)} variant urls to size")

    if todo:
        import httpx

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36",
            "Referer": "https://x.com/",
        }
        done = 0
        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
            def work(url: str) -> None:
                nonlocal done
                cache[url] = head_size(client, url)
                done += 1
                if done % 300 == 0:
                    print(f"  {done}/{len(todo)}", flush=True)
                    save_cache(cache)

            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                list(pool.map(work, todo))
        save_cache(cache)

    # per (collection) sums
    per_coll: dict[str, dict] = defaultdict(lambda: {t: 0 for t in TIERS} | {"count": 0, "audio": []})
    missing = 0
    for v in uniq.values():
        sizes: list[tuple[float, int]] = []
        for bitrate, url in v["variants"]:
            info = cache.get(url) or {}
            if info.get("size"):
                sizes.append((bitrate, info["size"]))
        if not sizes:
            missing += 1
            continue
        slot = per_coll[v["collection"]]
        slot["count"] += 1
        for tier in TIERS:
            slot[tier] += pick(sizes, tier) or 0
        if len(sizes) >= 2:
            (blo, slo), (bhi, shi) = sorted(sizes)[0], sorted(sizes)[-1]
            if bhi > blo:
                implied_no_audio = shi * blo / bhi
                slot["audio"].append(slo - implied_no_audio)

    totals = {t: sum(v[t] for v in per_coll.values()) for t in TIERS}
    audio = [a for v in per_coll.values() for a in v["audio"]]
    avg_audio = sum(audio) / len(audio) if audio else 0

    def policy(bm: str, lm: str, um: str) -> int:
        return (per_coll["bookmark"][bm] + per_coll["like"][lm] + per_coll["unknown"][um])

    result = {
        "videos": len(uniq),
        "animated_gif_unique": sum(1 for v in uniq.values() if v.get("media_type") == "animated_gif"),
        "video_unique": sum(1 for v in uniq.values() if v.get("media_type") != "animated_gif"),
        "missing": missing,
        "per_collection": {k: {"count": v["count"], **{t: v[t] for t in TIERS}} for k, v in per_coll.items()},
        "totals": totals,
        "policies": {
            "all_best": policy("best", "best", "best"),
            "bookmark_best_like_mid_unknown_mid": policy("best", "mid", "mid"),
            "bookmark_best_like_mid_unknown_low": policy("best", "mid", "low"),
            "bookmark_best_like_low_unknown_low": policy("best", "low", "low"),
            "all_mid": policy("mid", "mid", "mid"),
            "all_low": policy("low", "low", "low"),
        },
        "avg_audio_bytes": round(avg_audio),
        "audio_samples": len(audio),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n== exact video totals over {len(uniq)} unique videos (missing {missing})")
    for coll, v in per_coll.items():
        print(f"  {coll:9} n={v['count']:5} best={human_bytes(v['best']):>9} "
              f"mid={human_bytes(v['mid']):>9} low={human_bytes(v['low']):>9}")
    print(f"  平均音轨开销估计: {human_bytes(avg_audio)} / 视频（{len(audio)} 个样本）")
    print("  策略：")
    for k, v in result["policies"].items():
        print(f"    {k:42} {human_bytes(v)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
