"""Find byte-identical media that live under *different* media identities.

Downloading every media just to hash it is expensive, so we use an exact
pre-filter: two byte-identical files must have the same byte length. We size every
distinct media with HEAD (inventory.py --size), group by length, and only hash the
groups that contain more than one identity.

Outputs work/content_dupes.json:
    groups: [[identity, identity, ...], ...] with sha256 and size
    extra_savings_bytes: bytes that a media_key-only dedup would still duplicate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import INVENTORY_DIR, WORK, human_bytes  # noqa: E402

OUT = WORK / "content_dupes.json"


def load_index() -> list[dict]:
    path = INVENTORY_DIR / "media_index.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def hash_url(client, url: str) -> tuple[str | None, int, str | None]:
    sha = hashlib.sha256()
    size = 0
    with client.stream("GET", url) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_bytes(1 << 16):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size, resp.headers.get("content-type")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-groups", type=int, default=80)
    ap.add_argument("--max-mb", type=float, default=800)
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    rows = load_index()
    candidates = [r for r in rows if r.get("size") and r.get("download_url")]
    if not candidates:
        print("inventory has no sizes; run inventory.py --size first")
        return 2

    by_size: dict[int, list[dict]] = defaultdict(list)
    for row in candidates:
        by_size[row["size"]].append(row)

    groups = {size: items for size, items in by_size.items() if len(items) > 1}
    ordered = sorted(groups.items(), key=lambda kv: -kv[0])[: args.max_groups]
    print(f"{len(candidates)} sized identities; {len(groups)} size-collision groups; "
          f"checking {len(ordered)} (largest first)")

    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36",
        "Referer": "https://x.com/",
    }
    confirmed = []
    budget = args.max_mb * 1024 * 1024
    checked = 0
    with httpx.Client(headers=headers, timeout=90, follow_redirects=True) as client:
        for size, items in ordered:
            if budget <= 0:
                break
            # hash at most a few members per size group
            subset = items[:6]
            hashes: dict[str, list[str]] = defaultdict(list)
            meta: dict[str, dict] = {}
            for item in subset:
                try:
                    digest, real_size, ctype = hash_url(client, item["download_url"])
                except Exception as exc:  # noqa: BLE001
                    continue
                checked += 1
                budget -= real_size
                hashes[digest].append(item["identity"])
                meta[digest] = {"size": real_size, "content_type": ctype}
            for digest, ids in hashes.items():
                if len(ids) > 1:
                    confirmed.append({"sha256": digest, "identities": ids,
                                      "size": meta[digest]["size"],
                                      "content_type": meta[digest]["content_type"]})
                    print(f"  DUPE {meta[digest]['size']}B x{len(ids)}: {ids[:3]}")

    extra = sum(g["size"] * (len(g["identities"]) - 1) for g in confirmed)
    report = {
        "sized_identities": len(candidates),
        "size_collision_groups": len(groups),
        "groups_checked": len(ordered),
        "files_hashed": checked,
        "confirmed_groups": confirmed,
        "extra_savings_bytes": extra,
        "note": "pre-filter is exact: identical bytes imply identical length",
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nconfirmed {len(confirmed)} content-dupe groups, "
          f"extra saving {human_bytes(extra)}; hashed {checked} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
