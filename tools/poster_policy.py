"""Size the video posters and compare storage policies.

Answers: what would "photos + video posters only" cost versus downloading every
video file? Also reports the footprint of a single video variant vs the largest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import INVENTORY_DIR, WORK, human_bytes  # noqa: E402

OUT = WORK / "poster_sizes.json"


def main() -> int:
    rows = [json.loads(l) for l in (INVENTORY_DIR / "media_index.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if r.get("size")]
    cache_path = WORK / "poster_size_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    import httpx

    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://x.com/"}
    # media_index has no thumbnail_url, so read it from the per-account jsonl
    posters: dict[str, str] = {}
    for f in INVENTORY_DIR.glob("acct-*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = row.get("media_key")
            if key and row.get("thumbnail_url"):
                posters[f"mk:{key}"] = row["thumbnail_url"]

    videos = [r for r in rows if r.get("media_type") in ("video", "animated_gif")]
    photos = [r for r in rows if r.get("media_type") == "photo"]
    todo = [r for r in videos if r["identity"] in posters and posters[r["identity"]] not in cache]
    print(f"{len(videos)} videos, {len(todo)} posters to size")

    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for i, r in enumerate(todo, start=1):
            url = posters[r["identity"]]
            try:
                resp = client.head(url)
                cl = resp.headers.get("content-length")
                cache[url] = int(cl) if cl and cl.isdigit() else 0
            except Exception:  # noqa: BLE001
                cache[url] = 0
            if i % 200 == 0:
                print(f"  {i}/{len(todo)}")
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    poster_bytes = sum(cache.get(posters.get(r["identity"], ""), 0) for r in videos)
    video_bytes = sum(r["size"] for r in videos)
    photo_bytes = sum(r["size"] for r in photos)
    total = video_bytes + photo_bytes

    report = {
        "videos": len(videos),
        "video_bytes": video_bytes,
        "poster_bytes": poster_bytes,
        "photos": len(photos),
        "photo_bytes": photo_bytes,
        "total_bytes": total,
        "policy_photos_only": photo_bytes,
        "policy_photos_plus_posters": photo_bytes + poster_bytes,
        "saving_vs_full": {
            "photos_only": 1 - photo_bytes / total if total else None,
            "photos_plus_posters": 1 - (photo_bytes + poster_bytes) / total if total else None,
        },
        "avg_video_mb": round(video_bytes / len(videos) / 1048576, 2) if videos else None,
        "avg_poster_kb": round(poster_bytes / len(videos) / 1024, 1) if videos else None,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"photos={human_bytes(photo_bytes)} videos={human_bytes(video_bytes)} "
          f"posters={human_bytes(poster_bytes)}")
    print(f"photos only: {human_bytes(photo_bytes)} "
          f"({report['saving_vs_full']['photos_only']:.1%} smaller)")
    print(f"photos+posters: {human_bytes(photo_bytes + poster_bytes)} "
          f"({report['saving_vs_full']['photos_plus_posters']:.1%} smaller)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
