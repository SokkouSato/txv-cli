"""Revised statistics: split by account AND by collection, add HTTP status and
bitrate-tier projections.

Fixes the blind spots in the first report:
  * acct-3 dominates the bytes, so global averages hide the structure
  * no link-failure accounting
  * animated_gif was neither photo nor video in the policy table
  * the "bookmarks best / likes low bitrate" axis was never quantified
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    INVENTORY_DIR, WORK, canonical_media_url, human_bytes, iter_rows, load_accounts,
)

OUT = WORK / "stats_v2.json"


def load_sizes() -> dict:
    path = WORK / "media_sizes.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> int:
    accounts = load_accounts()
    sizes = load_sizes()
    rows: list[dict] = []
    per_account_dual: dict[str, int] = {}

    for acct in accounts:
        aid = acct["id"]
        tweets: dict[str, dict] = {}
        colspan: dict[str, set] = {}
        media: list[dict] = []
        for row in iter_rows(aid):
            if row.get("record_type") == "tweet":
                tid = row.get("tweet_id")
                tweets[tid] = row
                colspan.setdefault(tid, set()).add(row.get("collection_type") or "unknown")
            elif row.get("record_type") == "media":
                media.append(row)
        dual_tweets_here = sum(
            1 for cols in colspan.values() if {"like", "bookmark"} <= cols
        )
        per_account_dual.setdefault(aid, dual_tweets_here)
        for m in media:
            t = tweets.get(m.get("tweet_id")) or {}
            variants = []
            if m.get("variants_json"):
                try:
                    variants = json.loads(m["variants_json"])
                except json.JSONDecodeError:
                    variants = []
            mp4 = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("bitrate")]
            mp4.sort(key=lambda v: v["bitrate"])
            canon = canonical_media_url(m.get("media_type"), m.get("media_url")) or m.get("media_url")
            rows.append({
                "account": aid,
                "canon_url": canon,
                # A tweet can be both liked and bookmarked; attribute it to the
                # higher-intent collection and record that it was dual.
                # A tweet can be both liked and bookmarked (two tweet rows in the
                # archive): attribute it to the higher-intent collection and keep
                # a flag so the report can disclose how many rows were dual.
                "collection": (
                    "bookmark" if "bookmark" in colspan.get(m.get("tweet_id"), set())
                    else ("like" if "like" in colspan.get(m.get("tweet_id"), set()) else "unknown")
                ),
                "dual": {"like", "bookmark"} <= colspan.get(m.get("tweet_id"), set()),
                "tweet_id": m.get("tweet_id"),
                "created_at": t.get("created_at") or "",
                "media_key": m.get("media_key"),
                "media_type": m.get("media_type"),
                "url": m.get("media_url"),
                "size": (sizes.get(canon or "") or {}).get("size"),
                "status": (sizes.get(canon or "") or {}).get("status"),
                "error": (sizes.get(canon or "") or {}).get("error"),
                "duration_millis": m.get("duration_millis"),
                "bitrates": [v["bitrate"] for v in mp4],
                "low_url": mp4[0]["url"] if mp4 else None,
            })

    # ---------- per account ----------
    per_acct: dict[str, dict] = defaultdict(lambda: {
        "tweets": set(), "media_rows": 0, "unique": set(), "bytes": 0,
        "by_type": Counter(), "bytes_by_type": Counter(), "status": Counter(),
    })
    for r in rows:
        a = per_acct[r["account"]]
        a["media_rows"] += 1
        a["unique"].add(r["media_key"])
        a["by_type"][r["media_type"]] += 1
        a["status"][r["status"]] += 1
        if r["size"]:
            a["bytes"] += r["size"]
            a["bytes_by_type"][r["media_type"]] += r["size"]

    # ---------- per account x collection ----------
    per_coll: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "media_rows": 0, "unique": set(), "bytes": 0, "by_type": Counter(),
        "bytes_by_type": Counter(), "status": Counter(),
    })
    for r in rows:
        c = per_coll[(r["account"], r["collection"])]
        c["media_rows"] += 1
        c["unique"].add(r["media_key"])
        c["by_type"][r["media_type"]] += 1
        c["status"][r["status"]] += 1
        if r["size"]:
            c["bytes"] += r["size"]
            c["bytes_by_type"][r["media_type"]] += r["size"]

    # ---------- status by year ----------
    by_year: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        year = (r["created_at"] or "")[-4:] or "?"
        by_year[year][r["status"]] += 1

    # ---------- bitrate tier projection ----------
    tier = {"all_best": 0, "bookmarks_best_likes_low": 0, "all_low": 0}
    videos = 0
    for r in rows:
        if r["media_type"] not in ("video", "animated_gif") or not r["size"]:
            continue
        videos += 1
        best = r["size"]
        low = best
        if r["bitrates"] and r["bitrates"][-1] and r["bitrates"][0]:
            ratio = r["bitrates"][0] / r["bitrates"][-1]
            low = int(best * ratio)
        r["low_size"] = low
        tier["all_best"] += best
        tier["all_low"] += low
        if r["collection"] == "bookmark":
            tier["bookmarks_best_likes_low"] += best
        else:
            tier["bookmarks_best_likes_low"] += low

    photos_bytes = sum(r["size"] or 0 for r in rows if r["media_type"] == "photo")
    gif_rows = [r for r in rows if r["media_type"] == "animated_gif"]
    gif_bytes = sum(r["size"] or 0 for r in gif_rows)
    unmeasured = [r for r in rows if r["size"] is None]

    report = {
        "accounts": {
            aid: {
                "media_rows": v["media_rows"],
                "unique": len(v["unique"]),
                "bytes": v["bytes"],
                "avg_bytes": v["bytes"] / v["media_rows"] if v["media_rows"] else 0,
                "by_type": dict(v["by_type"]),
                "bytes_by_type": dict(v["bytes_by_type"]),
                "status": dict(v["status"]),
            } for aid, v in per_acct.items()
        },
        "account_collection": {
            f"{aid}/{coll}": {
                "media_rows": v["media_rows"],
                "unique": len(v["unique"]),
                "bytes": v["bytes"],
                "avg_bytes": v["bytes"] / v["media_rows"] if v["media_rows"] else 0,
                "by_type": dict(v["by_type"]),
                "bytes_by_type": dict(v["bytes_by_type"]),
                "status": dict(v["status"]),
            } for (aid, coll), v in sorted(per_coll.items())
        },
        "status_by_year": {y: dict(c) for y, c in sorted(by_year.items())},
        "dual_collection_rows": sum(1 for r in rows if r.get("dual")),
        "dual_collection_tweets": sum(per_account_dual.values()),
        "dual_collection_tweets_by_account": per_account_dual,
        "link_health": {
            "rows": len(rows),
            "measured_200": sum(1 for r in rows if r["status"] == 200),
            "not_measured": len(unmeasured),
            "gone_404": sum(1 for r in rows if r["status"] == 404),
            "forbidden_403": sum(1 for r in rows if r["status"] == 403),
            "other": sum(1 for r in rows if r["status"] not in (200, 404, 403, None)),
        },
        "tiers": {
            "videos": videos,
            "all_best_bytes": tier["all_best"],
            "bookmarks_best_likes_low_bytes": tier["bookmarks_best_likes_low"],
            "all_low_bytes": tier["all_low"],
            "all_low_saving_ratio": 1 - tier["all_low"] / tier["all_best"] if tier["all_best"] else None,
            "bookmarks_policy_saving_ratio":
                1 - tier["bookmarks_best_likes_low"] / tier["all_best"] if tier["all_best"] else None,
            "photos_bytes": photos_bytes,
            "animated_gif_rows": len(gif_rows),
            "animated_gif_bytes": gif_bytes,
        },
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(v["bytes"] for v in per_acct.values())
    print(f"== total {human_bytes(total)} over {sum(v['media_rows'] for v in per_acct.values())} rows")
    print(f"{'account':8} {'rows':>6} {'unique':>7} {'bytes':>10} {'avg':>9}  type mix")
    for aid, v in per_acct.items():
        print(f"{aid:8} {v['media_rows']:6} {len(v['unique']):7} {human_bytes(v['bytes']):>10} "
              f"{human_bytes(v['bytes'] / max(v['media_rows'],1)):>9}  {dict(v['by_type'])}")
    print("\n== account/collection")
    for k, v in sorted(per_coll.items()):
        print(f"{k[0]}/{k[1]:9} rows={v['media_rows']:5} bytes={human_bytes(v['bytes']):>9} "
              f"avg={human_bytes(v['bytes']/max(v['media_rows'],1)):>8} {dict(v['by_type'])}")
    lh = report["link_health"]
    print(f"\n== link health: 200={lh['measured_200']} 404={lh['gone_404']} "
          f"403={lh['forbidden_403']} unmeasured={lh['not_measured']}")
    t = report["tiers"]
    print(f"== videos {t['videos']}: best={human_bytes(t['all_best_bytes'])} "
          f"bookmarks-best/likes-low={human_bytes(t['bookmarks_best_likes_low_bytes'])} "
          f"({t['bookmarks_policy_saving_ratio']:.1%} saved), "
          f"all-low={human_bytes(t['all_low_bytes'])} ({t['all_low_saving_ratio']:.1%})")
    print(f"   photos={human_bytes(t['photos_bytes'])}, gifs={t['animated_gif_rows']} "
          f"({human_bytes(t['animated_gif_bytes'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
