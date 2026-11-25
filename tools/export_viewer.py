"""Export archived tweets + store media into xx-timeline's data format (v2).

What v2 adds over v1:
  * interaction counts parsed from raw_json (like/retweet/reply/quote/bookmark/view)
  * author profile (bio / followers / joined / verified) from raw_json
  * avatars downloaded into <data>/avatars (small _normal variant)
  * video posters downloaded into <vault>/media/<tweet_id>/<key>-poster.jpg
    (ffmpeg frame grab as fallback when the thumbnail URL is gone)
  * per-account membership on every card, and account/storage stats in meta.json

Usage:
    python tools/export_viewer.py --out viewer            # full (avatars+posters)
    python tools/export_viewer.py --out viewer --no-avatars --no-posters  # fast
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, iter_rows, load_accounts  # noqa: E402

STORE = ROOT / "store"
STORE_DB = STORE / "media.db"
TIER_ORDER = {"best": 4, "orig": 3, "mid": 2, "low": 1, "large": 0}


def parse_created(raw: str | None) -> tuple[str, int | None]:
    if not raw:
        return "", None
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return raw, None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), int(dt.timestamp())


def tiny_avatar(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url)
    path = parts.path
    for suffix in ("_normal.", "_bigger.", "_mini.", "_400x400."):
        if suffix in path:
            path = path.replace(suffix, "_normal.")
            break
    else:
        stem, dot, ext = path.rpartition(".")
        if dot:
            path = f"{stem}_normal.{ext}"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def extract_raw(raw_json: str | None) -> dict:
    out = {"counts": {}, "author": {}}
    if not raw_json:
        return out
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError):
        return out
    legacy = raw.get("legacy") or {}
    views = raw.get("views") or {}
    view_count = None
    try:
        if str(views.get("count", "")).isdigit():
            view_count = int(views["count"])
    except (TypeError, ValueError):
        pass
    out["counts"] = {
        "reply_count": legacy.get("reply_count"),
        "retweet_count": legacy.get("retweet_count"),
        "like_count": legacy.get("favorite_count"),
        "quote_count": legacy.get("quote_count"),
        "bookmark_count": legacy.get("bookmark_count"),
        "view_count": view_count,
    }
    user = ((raw.get("core") or {}).get("user_results") or {}).get("result") or {}
    if user:
        ul = user.get("legacy") or {}
        uc = user.get("core") or {}
        avatar = (user.get("avatar") or {}).get("image_url") or ul.get("profile_image_url_https")
        out["author"] = {
            "handle": uc.get("screen_name") or "",
            "name": uc.get("name") or "",
            "bio": ul.get("description") or "",
            "followers": ul.get("followers_count"),
            "following": ul.get("friends_count"),
            "location": ul.get("location") or "",
            "joined": uc.get("created_at") or "",
            "verified": bool(user.get("is_blue_verified")),
            "avatar_url": avatar,
        }
    return out


def load_assets() -> dict[str, dict]:
    con = sqlite3.connect(f"file:{STORE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    best: dict[str, dict] = {}
    for row in con.execute(
        "SELECT media_key, tier, blob_path, size, media_type FROM assets WHERE state='done'"
    ):
        rank = TIER_ORDER.get(row["tier"], 0)
        cur = best.get(row["media_key"])
        if cur is None or rank > TIER_ORDER.get(cur["tier"], 0):
            best[row["media_key"]] = dict(row)
    con.close()
    return best


def link_or_copy(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return True
    try:
        os.link(src, dst)
        return True
    except OSError:
        try:
            shutil.copyfile(src, dst)
            return True
        except OSError:
            return False


def download(url: str, dst: Path, headers: dict) -> bool:
    if dst.exists() and dst.stat().st_size > 0:
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    try:
        import httpx

        with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 16):
                        fh.write(chunk)
        tmp.replace(dst)
        return True
    except Exception:  # noqa: BLE001
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def frame_grab(video: Path, dst: Path) -> bool:
    if dst.exists() and dst.stat().st_size > 0:
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1",
             "-i", str(video), "-frames:v", "1", "-y", str(dst)],
            capture_output=True, timeout=60,
        )
        return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        return False


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "viewer"))
    ap.add_argument("--no-avatars", action="store_true")
    ap.add_argument("--no-posters", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    out = Path(args.out)
    data_dir = out / "data"
    vault = out / "vault"
    media_root = vault / "media"
    avatars_dir = data_dir / "avatars"
    data_dir.mkdir(parents=True, exist_ok=True)
    media_root.mkdir(parents=True, exist_ok=True)
    avatars_dir.mkdir(parents=True, exist_ok=True)

    assets = load_assets()
    accounts = load_accounts()
    cards: dict[str, dict] = {}
    account_tweets: Counter = Counter()
    collections: Counter = Counter()
    media_types: Counter = Counter()
    avatars_wanted: dict[str, tuple[str, str]] = {}   # handle -> (url, filename)
    posters_wanted: list[tuple[str, Path]] = []
    linked = missing = 0

    for acct in accounts:
        tweets: dict[str, dict] = {}
        media_rows: dict[str, list[dict]] = defaultdict(list)
        rels: dict[str, list[str]] = defaultdict(list)
        for row in iter_rows(acct["id"]):
            rtype = row.get("record_type")
            if rtype == "tweet":
                tid = row.get("tweet_id")
                meta = extract_raw(row.get("raw_json"))
                seen = tweets.get(tid)
                cols = {row.get("collection_type") or "unknown"}
                if seen is None:
                    tweets[tid] = {
                        "handle": row.get("author_username") or "unknown",
                        "name": row.get("author_display_name") or "",
                        "text": row.get("note_tweet_text") or row.get("text") or "",
                        "created": row.get("created_at"),
                        "lang": row.get("lang") or "",
                        "collections": cols,
                        "accounts": {acct["id"]},
                        "saved": row.get("first_seen_at") or row.get("synced_at"),
                        "counts": meta["counts"],
                        "author": meta["author"],
                    }
                else:
                    seen["collections"] |= cols
                    seen["accounts"].add(acct["id"])
            elif rtype == "media":
                media_rows[row.get("tweet_id")].append(row)
            elif rtype == "tweet_relation":
                target = row.get("target_tweet_id")
                rel = row.get("relation_type")
                if target and rel in ("reply_to", "quote_of", "thread_parent"):
                    rels[row.get("tweet_id")].append(f"{rel}:{target}")

        for tid, tw in tweets.items():
            created, created_ts = parse_created(tw.get("created"))
            saved, saved_ts = parse_created(tw.get("saved"))
            items = []
            rows = sorted(media_rows.get(tid, []), key=lambda r: (r.get("position") or 0))
            for m in rows:
                key = m.get("media_key")
                asset = assets.get(key)
                if not asset or not asset.get("blob_path"):
                    missing += 1
                    items.append({"key": key, "t": m.get("media_type"), "src": None,
                                  "w": m.get("width"), "h": m.get("height"),
                                  "ms": m.get("duration_millis"), "size": None,
                                  "missing": True})
                    continue
                blob = STORE / asset["blob_path"]
                ext = Path(asset["blob_path"]).suffix or ".bin"
                name = f"{key}{ext}"
                dst = media_root / tid / name
                ok = link_or_copy(blob, dst)
                if ok:
                    linked += 1
                else:
                    missing += 1
                media_types[m.get("media_type") or "unknown"] += 1
                poster_src = None
                if m.get("media_type") in ("video", "animated_gif"):
                    poster_name = f"{key}-poster.jpg"
                    poster_dst = media_root / tid / poster_name
                    if poster_dst.exists() and poster_dst.stat().st_size > 0:
                        poster_src = f"/media/{tid}/{poster_name}"
                    elif m.get("thumbnail_url"):
                        posters_wanted.append((m["thumbnail_url"], poster_dst))
                        poster_src = f"/media/{tid}/{poster_name}"
                    elif ok:
                        posters_wanted.append((f"frame:{blob}", poster_dst))
                        poster_src = f"/media/{tid}/{poster_name}"
                items.append({
                    "key": key, "t": m.get("media_type"),
                    "src": f"/media/{tid}/{name}" if ok else None,
                    "poster": poster_src,
                    "w": m.get("width"), "h": m.get("height"),
                    "ms": m.get("duration_millis"), "size": asset.get("size"),
                    "missing": not ok,
                })

            existing = cards.get(tid)
            if existing is not None:
                existing["collections"] = sorted(set(existing["collections"]) | tw["collections"])
                existing["accounts"] = sorted(set(existing["accounts"]) | tw["accounts"])
                if not existing["media"] and items:
                    existing["media"] = items
                continue
            for c in tw["collections"]:
                collections[c] += 1
            for a in tw["accounts"]:
                account_tweets[a] += 1

            author = dict(tw.get("author") or {})
            handle = tw["handle"]
            avatar_name = None
            if author.get("avatar_url") and not args.no_avatars:
                avatar_name = hashlib.sha1(handle.encode("utf-8")).hexdigest()[:16] + ".jpg"
                avatars_wanted[handle] = (author["avatar_url"], avatar_name)
            cards[tid] = {
                "id": tid,
                "handle": handle,
                "name": tw["name"] or author.get("name") or handle,
                "lang": tw["lang"],
                "created": created,
                "saved": saved,
                "collections": sorted(tw["collections"]),
                "accounts": sorted(tw["accounts"]),
                "text": tw["text"],
                "urls": [],
                "rels": rels.get(tid, []),
                "media": items,
                "created_ts": created_ts,
                "saved_ts": saved_ts,
                "avatar": avatar_name,
                "author_bio": author.get("bio") or "",
                "author_followers": author.get("followers"),
                "author_following": author.get("following"),
                "author_location": author.get("location") or "",
                "author_joined": author.get("joined") or "",
                "author_verified": bool(author.get("verified")),
                **tw["counts"],
            }

    # ---- avatars / posters ----
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36",
        "Referer": "https://x.com/",
    }
    avatar_ok = avatar_miss = 0
    if avatars_wanted:
        def fetch_avatar(item):
            handle, (url, name) = item
            ok = download(tiny_avatar(url) or url, avatars_dir / name, headers)
            return ok

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(fetch_avatar, list(avatars_wanted.items())))
        avatar_ok = sum(1 for r in results if r)
        avatar_miss = len(results) - avatar_ok

    poster_ok = poster_miss = 0
    if posters_wanted and args.no_posters:
        posters_wanted = []
    if posters_wanted:
        def fetch_poster(item):
            url, dst = item
            if url.startswith("frame:"):
                return frame_grab(Path(url[6:]), dst)
            return download(url, dst, headers)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(fetch_poster, posters_wanted))
        poster_ok = sum(1 for r in results if r)
        poster_miss = len(results) - poster_ok

    ordered = sorted(cards.values(), key=lambda c: c.get("created_ts") or 0, reverse=True)
    with open(data_dir / "timeline.ndjson", "w", encoding="utf-8", newline="\n") as fh:
        for card in ordered:
            fh.write(json.dumps(card, ensure_ascii=False) + "\n")

    with_media = [c for c in ordered if c["media"]]
    counts = {
        "tweets": len(ordered),
        "with_video": sum(1 for c in with_media if any(m["t"] in ("video", "animated_gif") for m in c["media"])),
        "with_photo": sum(1 for c in with_media if any(m["t"] == "photo" for m in c["media"])),
        "text_only": sum(1 for c in ordered if not c["media"]),
        "media_only": sum(1 for c in with_media if not c["text"]),
        "media_rows": sum(len(c["media"]) for c in ordered),
        "media_in_timeline": sum(len(c["media"]) for c in ordered),
        "media_orphan_rows": 0,
        "media_files_missing": missing,
        "orphan_media_dirs": 0,
    }
    storage = {
        "db_bytes": sum((STORE / name).stat().st_size for name in ("media.db", "media.db-wal", "media.db-shm")
                        if (STORE / name).exists()),
        "media_bytes": dir_size(STORE / "assets"),
        "avatars_bytes": dir_size(avatars_dir),
        "posters_bytes": sum(p.stat().st_size for p in media_root.rglob("*-poster.jpg")),
        "tmp_bytes": dir_size(STORE / ".tmp"),
        "projection_bytes": dir_size(STORE / "by-account"),
        "viewer_media_bytes": dir_size(media_root),
    }
    created_list = [c["created"] for c in ordered if c["created"]]
    meta = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "vault": str(vault),
        "db_version": 5,
        "counts": counts,
        "owner": {
            "name": f"{len(accounts)} accounts",
            "handle": ", ".join("@" + (a.get("handle") or a["id"]) for a in accounts),
            "followers": None, "following": None, "joined": None,
            "verified": False, "protected": False,
        },
        "sync": {},
        "raw_operations": {},
        "collections": dict(collections),
        "accounts": [
            {
                "id": a["id"],
                "handle": a.get("handle") or "",
                "name": a.get("label") or a.get("handle") or a["id"],
                "tweets": account_tweets.get(a["id"], 0),
            }
            for a in accounts
        ],
        "tweet_rows": len(cards),
        "duplicate_tweet_rows": 0,
        "objects": 0,
        "related_objects": 0,
        "avatars": avatar_ok,
        "enriched_urls": 0,
        "filters": {
            "all": len(ordered),
            "video": counts["with_video"],
            "photo": counts["with_photo"],
            "text": counts["text_only"],
            "bookmark": collections.get("bookmark", 0),
        },
        "oldest": min(created_list) if created_list else None,
        "newest": max(created_list) if created_list else None,
        "media_types": dict(media_types),
        "media_types_in_timeline": dict(media_types),
        "download_states": {"done": sum(media_types.values())},
        "warnings": {"missing_files": missing, "avatar_missing": avatar_miss,
                     "poster_missing": poster_miss},
        "storage": storage,
        "counts_fields": ["reply_count", "retweet_count", "like_count", "quote_count",
                          "bookmark_count", "view_count"],
    }
    (data_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                        encoding="utf-8")

    print(f"cards={len(ordered)} media_linked={linked} missing={missing}")
    print(f"avatars ok={avatar_ok} missing={avatar_miss}; posters ok={poster_ok} missing={poster_miss}")
    print(f"data: {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
