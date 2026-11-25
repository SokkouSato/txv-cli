"""Re-test media URLs that failed the first sizing pass (proxy resets).

The first pass left 118 URLs with no Content-Length because the proxy dropped the
connection (not 404). This script retries them with backoff and updates the size
cache, then reports the real 200/403/404 distribution.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import WORK  # noqa: E402

CACHE = WORK / "media_sizes.json"


def head(client, url: str) -> dict:
    for attempt in range(3):
        try:
            r = client.head(url)
            cl = r.headers.get("content-length")
            if r.status_code in (200, 206) and cl and cl.isdigit():
                return {"size": int(cl), "status": r.status_code,
                        "ctype": (r.headers.get("content-type") or "").split(";")[0] or None}
            return {"size": None, "status": r.status_code, "ctype": None}
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                return {"size": None, "status": None, "error": str(exc)[:120]}
            time.sleep(5 * (attempt + 1))
    return {"size": None, "status": None}


def main() -> int:
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    pending = [u for u, v in cache.items() if not v.get("size")]
    print(f"{len(pending)} urls without size; retesting")

    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36",
        "Referer": "https://x.com/",
    }
    fixed = 0
    status = Counter()
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for i, url in enumerate(pending, start=1):
            info = head(client, url)
            cache[url] = info
            status[info.get("status")] += 1
            if info.get("size"):
                fixed += 1
            if i % 40 == 0:
                print(f"  {i}/{len(pending)} (fixed {fixed})", flush=True)

    CACHE.write_text(json.dumps(cache), encoding="utf-8")
    total = len(cache)
    sized = sum(1 for v in cache.values() if v.get("size"))
    dist = Counter(v.get("status") for v in cache.values())
    print(f"resolved {fixed}/{len(pending)}; overall {sized}/{total} sized; status={dict(dist)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
