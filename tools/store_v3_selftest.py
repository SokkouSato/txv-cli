#!/usr/bin/env python
"""store_v3_selftest.py -- fully offline self-test for tools/store_v3.py (v5).

No internet, no LanceDB, no credentials: a local http.server (127.0.0.1, random
port, trust_env disabled) serves synthetic media and misbehaves on purpose so we
can prove the hard parts of the store:

  * happy path          : 200 photo / video / animated_gif, sha256 + atomic blob
  * content dedupe      : two identities, same bytes -> one shared blob
  * tier dominance      : bookmark(best) + like(mid) for one media_key -> the mid
                          request is served by the best asset (0 extra downloads)
  * existing-tier reuse : a done better tier serves a later lower-tier request
                          (0 HTTP requests), photo orig also dominates large
  * upgrade + gc        : mid downloaded first, later best => old mid becomes
                          state=superseded, refs are repointed, --gc reclaims its
                          blob bytes plus orphan blobs (strict two-phase gc)
  * shared-blob gc      : low then mid over byte-identical URLs -> low is
                          superseded but mid references the same blob; --gc must
                          keep it (regression for the P0-1 "shared blob" case)
  * failure semantics   : permanent 404 on the best tier degrades to a done lower
                          tier; transient 5xx must NOT silently degrade — refs stay
                          unresolved on the retryable asset
  * tier downgrade      : single-collection video whose best tier is permanently
                          404 walks best->mid->low (synthesised from variants_json);
                          only when the whole ladder is dead is the ref unresolved
  * blob self-heal      : DB says best(done) but the blob vanished -> a later run
                          that only asks for mid re-downloads best and serves it
  * refs composite key  : one (tweet, media) keeps a like AND a bookmark ref
                          (schema v5 UNIQUE + collection, FK to assets); both refs
                          project to two distinct collection paths (copy/hardlink)
  * old-DB migration    : v3 refs automatically gain columns; v4 refs table is
                          rebuilt to v5 (collection NOT NULL, new UNIQUE, FK),
                          idempotent, NULL collection -> 'unknown'
  * process lock        : second handle/process fails fast; stale .part cleared;
                          owner pid readable from the .lock.info sidecar
  * verify              : --verify/--quick asset checks + orphan blob count
  * read-only overwrite : pre-existing +R blob with different content is replaced
                          atomically (Windows WinError 5 path); a same-size but
                          different-sha256 blob is detected as corruption and
                          replaced (never silently reused)
  * refs autonomy       : projections use the stored tweet_created_at; a missing
                          date falls back to the Snowflake month / 'unknown-date'
  * retry               : HTTP 500 twice, then 200 -> exponential retry succeeds
  * permanent failure   : HTTP 404 -> state=failed, no retry on later runs
  * interrupted body    : truncated response -> retried; always-truncated ->
                          state=failed with no blob and no .part leftovers
  * read-only blobs     : FILE_ATTRIBUTE_READONLY is set and writes are refused
  * idempotency         : second run performs zero HTTP requests / zero downloads
  * limit semantics     : "--limit N twice" downloads nothing the second time
  * projections         : copy + hardlink naming, hardlink->copy fallback
  * gc read-only delete : superseded + orphan read-only blobs are really unlinked
                          (unlink_force), byte accounting exact
  * exports             : index.csv rows == refs rows, summary.json aggregates
  * backup               : tools/backup_store.py snapshot counts/sha256 match source;
                          store/.lock is held for the whole snapshot (db-only and
                          --copy-assets), busy lock -> exit code 4 for both tools
  * secrets              : no credential-looking strings in generated artifacts

Run:
    python tools/store_v3.py --self-test
    python tools/store_v3_selftest.py [--keep]
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import store_v3 as sv  # noqa: E402
import backup_store as bs  # noqa: E402

# --------------------------------------------------------------------------- #
# synthetic media bodies (no network needed)
# --------------------------------------------------------------------------- #
JPEG_A = b"\xff\xd8\xff\xe0" + b"selftest-jpeg-A" + bytes(range(256)) * 8 + b"\xff\xd9"
JPEG_FLAKY = b"\xff\xd8\xff\xe0" + b"selftest-jpeg-flaky" + bytes(range(255, -1, -1)) * 8 + b"\xff\xd9"
JPEG_SHARED = b"\xff\xd8\xff\xe0" + b"selftest-jpeg-shared" + b"S" * 4096 + b"\xff\xd9"
JPEG_BROKEN = b"\xff\xd8\xff\xe0" + b"selftest-jpeg-broken" + b"B" * 8192 + b"\xff\xd9"
MP4_LOW = b"\x00\x00\x00\x18ftypmp42" + b"low!" * 512
MP4_MID = b"\x00\x00\x00\x18ftypmp42" + b"mid!" * 1024
MP4_HIGH = b"\x00\x00\x00\x18ftypmp42" + b"high!" * 2048
MP4_GIF = b"\x00\x00\x00\x18ftypmp42" + b"gif!" * 256
MP4_HEAL = b"\x00\x00\x00\x18ftypmp42" + b"heal!" * 1500
MP4_DOM = b"\x00\x00\x00\x18ftypmp42" + b"dom!" * 900
MP4_UP_LOW = b"\x00\x00\x00\x18ftypmp42" + b"uplow!" * 700
MP4_UP_HIGH = b"\x00\x00\x00\x18ftypmp42" + b"uphigh!" * 2100


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "store-v3-selftest"

    def log_message(self, *_args):  # keep the test output clean
        pass

    def do_GET(self):  # noqa: N802 - stdlib naming
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- helpers ---------------------------------------------------------- #
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_truncated(self, body: bytes, content_type: str) -> None:
        """Declare a longer body than we send, then close: simulates a proxy reset."""
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body) + 64))
        self.end_headers()
        self.wfile.write(body[: max(1, len(body) // 2)])
        self.wfile.flush()
        self.close_connection = True

    def _dispatch(self) -> None:
        path = urlsplit(self.path).path
        server: "SelftestServer" = self.server  # type: ignore[assignment]
        with server.lock:
            server.counts[path] = server.counts.get(path, 0) + 1
            count = server.counts[path]
        if path == "/ok.jpg":
            self._send(200, JPEG_A, "image/jpeg")
        elif path == "/flaky.jpg":
            if count <= 2:
                self._send(500, b"temporary upstream failure", "text/plain")
            else:
                self._send(200, JPEG_FLAKY, "image/jpeg")
        elif path == "/missing.jpg":
            self._send(404, b"gone", "text/plain")
        elif path == "/heal.mp4":
            if count <= 2:
                self._send_truncated(MP4_HEAL, "video/mp4")
            else:
                self._send(200, MP4_HEAL, "video/mp4")
        elif path == "/broken.jpg":
            self._send_truncated(JPEG_BROKEN, "image/jpeg")
        elif path == "/v-low.mp4":
            self._send(200, MP4_LOW, "video/mp4")
        elif path == "/v-mid.mp4":
            self._send(200, MP4_MID, "video/mp4")
        elif path == "/v-high.mp4":
            self._send(200, MP4_HIGH, "video/mp4")
        elif path == "/v-flaky.mp4":
            if count <= 2:
                self._send(503, b"temporary upstream failure", "text/plain")
            else:
                self._send(200, MP4_HIGH, "video/mp4")
        elif path == "/v-404.mp4":
            self._send(404, b"gone", "text/plain")
        elif path == "/v-404-mid.mp4":
            self._send(404, b"gone", "text/plain")
        elif path == "/v-404-low.mp4":
            self._send(404, b"gone", "text/plain")
        elif path == "/v-transient.mp4":
            self._send(503, b"still down", "text/plain")
        elif path == "/gif.mp4":
            self._send(200, MP4_GIF, "video/mp4")
        elif path == "/shared-a.jpg":
            self._send(200, JPEG_SHARED, "image/jpeg")
        elif path == "/shared-b.jpg":
            self._send(200, JPEG_SHARED, "image/jpeg")
        elif path == "/dom-high.mp4":
            self._send(200, MP4_DOM, "video/mp4")
        elif path == "/dom-low.mp4":
            self._send(200, MP4_DOM + b"-low", "video/mp4")
        elif path == "/up-low.mp4":
            self._send(200, MP4_UP_LOW, "video/mp4")
        elif path == "/up-high.mp4":
            self._send(200, MP4_UP_HIGH, "video/mp4")
        else:
            self._send(404, b"unknown", "text/plain")


class SelftestServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address):
        super().__init__(address, Handler)
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return dict(self.counts)

    def handle_error(self, *_args):  # silence expected broken pipes
        pass


# --------------------------------------------------------------------------- #
# test harness
# --------------------------------------------------------------------------- #
class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, condition: bool, message: str) -> bool:
        if condition:
            self.passed += 1
            print(f"  PASS  {message}")
            return True
        self.failed.append(message)
        print(f"  FAIL  {message}")
        return False

    def eq(self, actual, expected, message: str) -> bool:
        return self.check(actual == expected, f"{message} (got {actual!r}, want {expected!r})")

    def phase(self, title: str) -> None:
        print(f"\n--- {title} ---")


def silent_store(root: Path, log=None) -> sv.Store:
    import httpx

    def factory():
        # trust_env=False: never route 127.0.0.1 through the corporate proxy.
        return httpx.Client(trust_env=False, follow_redirects=True, timeout=15.0)

    return sv.Store(
        root,
        retry_delays=(0.02, 0.04, 0.06),
        max_attempts=4,
        client_factory=factory,
        log=log or (lambda _msg: None),
    )


def force_rmtree(path: Path) -> None:
    if not path.exists():
        return
    for current, dirs, files in os.walk(path):
        for name in files:
            candidate = Path(current) / name
            try:
                sv.clear_readonly(candidate)
                candidate.unlink()
            except OSError:
                pass
        for name in dirs:
            try:
                sv.clear_readonly(Path(current) / name)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def db_scalar(store: sv.Store, sql: str, params: tuple = ()):
    assert store.conn is not None
    return store.conn.execute(sql, params).fetchone()[0]


def db_rows(store: sv.Store, sql: str, params: tuple = ()):
    assert store.conn is not None
    return store.conn.execute(sql, params).fetchall()


def asset_row(store: sv.Store, media_key: str, tier: str):
    assert store.conn is not None
    return store.conn.execute(
        "SELECT * FROM assets WHERE media_key=? AND tier=?", (media_key, tier)
    ).fetchone()


def ref_row(store: sv.Store, tweet_id: str):
    assert store.conn is not None
    return store.conn.execute(
        "SELECT * FROM refs WHERE tweet_id=?", (tweet_id,)
    ).fetchone()


def read_index(store: sv.Store) -> list[dict]:
    with open(store.root / "index.csv", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_summary(store: sv.Store) -> dict:
    return json.loads((store.root / "summary.json").read_text(encoding="utf-8"))


def count_blobs(store: sv.Store) -> int:
    return sum(1 for path in store.assets_dir.rglob("*") if path.is_file())


def leftover_parts(store: sv.Store) -> list[Path]:
    return list(store.tmp_dir.glob("*.part")) if store.tmp_dir.exists() else []


def seed_parts(root: Path, count: int) -> list[Path]:
    tmp_dir = root / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for index in range(count):
        path = tmp_dir / f"stale-selftest-{index}.part"
        path.write_bytes(b"partial-bytes")
        parts.append(path)
    return parts


def archive_media(
    tweet_id: str,
    media_key: str,
    *,
    media_type: str = "photo",
    url: str = "https://pbs.twimg.com/media/selftest.jpg?format=jpg&name=orig",
    position: int = 0,
) -> dict:
    return {
        "tweet_id": tweet_id,
        "media_key": media_key,
        "media_type": media_type,
        "media_url": url,
        "position": position,
        "variants_json": None,
    }


def archive_tweet(
    tweet_id: str,
    collection: str | None,
    *,
    author: str = "Writer",
    created: str = "Sun Oct 19 23:25:09 +0000 2025",
) -> dict:
    return {
        "tweet_id": tweet_id,
        "collection_type": collection,
        "author_username": author,
        "created_at": created,
    }


# --------------------------------------------------------------------------- #
# synthetic rows
# --------------------------------------------------------------------------- #
def photo_row(account, collection, tweet_id, media_key, url, *, author="Writer", created="Sun Oct 19 23:25:09 +0000 2025"):
    return sv.MediaRow(
        account=account,
        collection=collection,
        tweet_id=tweet_id,
        media_key=media_key,
        media_type="photo",
        position=0,
        author_handle=author,
        tweet_created_at=created,
        media_url=url,
        variants_json=None,
    )


def video_row(
    account,
    collection,
    tweet_id,
    media_key,
    url=None,
    *,
    variants=None,
    author="Vidder",
    created="Fri Jun 05 13:00:01 +0000 2026",
    gif=False,
):
    if variants is None:
        assert url is not None
        root = url.rstrip("/")
        variants = [
            (256000, root + "/v-low.mp4"),
            (832000, root + "/v-mid.mp4"),
            (2176000, root + "/v-high.mp4"),
        ]
    payload = [{"bitrate": br, "content_type": "video/mp4", "url": u} for br, u in variants]
    if not gif:
        payload.append(
            {"bitrate": None, "content_type": "application/x-mpegURL", "url": variants[-1][1] + ".m3u8"}
        )
    media_url = url or variants[-1][1]
    return sv.MediaRow(
        account=account,
        collection=collection,
        tweet_id=tweet_id,
        media_key=media_key,
        media_type="animated_gif" if gif else "video",
        position=0,
        author_handle=author,
        tweet_created_at=created,
        media_url=media_url,
        variants_json=json.dumps(payload),
    )


def phase1_rows(base: str) -> list[sv.MediaRow]:
    rows = [
        # two identities, same bytes -> one blob after download (content dedupe)
        photo_row("t-1", "bookmark", "1001", "3_1001", base + "/ok.jpg", author="WriterOne"),
        photo_row("t-2", "like", "1002", "3_1002", base + "/ok.jpg", author="ReaderTwo"),
        # HTTP 500 x2 then 200 -> exponential retry
        photo_row("t-2", "like", "1003", "3_1003", base + "/flaky.jpg", author="FlakyThree"),
        # HTTP 404 -> permanent failure
        photo_row("t-2", "like", "1004", "3_1004", base + "/missing.jpg", author="GoneFour"),
        # same media_key, different collection -> best + low requested, but the
        # best download dominates -> only ONE asset instead of two tiers.
        video_row("t-1", "bookmark", "2001", "13_2001", base, author="VidOne"),
        video_row("t-2", "like", "2002", "13_2001", base, author="VidTwo"),
        # animated_gif -> best
        video_row("t-1", "like", "3001", "16_3001", variants=[(0, base + "/gif.mp4")], author="GifOne", gif=True),
        # different identities, identical bytes -> content dedupe again
        photo_row("t-3", "like", "1005", "3_1005", base + "/shared-a.jpg", author="SharedA"),
        photo_row("t-3", "like", "1006", "3_1006", base + "/shared-b.jpg", author="SharedB"),
    ]
    return rows


# --------------------------------------------------------------------------- #
# the actual test
# --------------------------------------------------------------------------- #
def run_selftest(keep: bool = False) -> int:
    checker = Checker()
    base_dir = sv.DEFAULT_STORE / ".selftest" / f"run-{os.getpid()}-{int(time.time())}"
    phase1_root = base_dir / "phase1"
    phase4_root = base_dir / "phase4"
    phase7_root = base_dir / "phase7"
    phase8_root = base_dir / "phase8"
    phase9_root = base_dir / "phase9"
    phase10_root = base_dir / "phase10"
    server = SelftestServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"[selftest] offline server {base}")
    print(f"[selftest] temp store  {base_dir}")

    try:
        # ------------------------------------------------------------------ #
        checker.phase("phase 0: unit helpers (no network)")
        policy = sv.make_policy("default")
        checker.eq(sv.month_of("Sun Oct 19 23:25:09 +0000 2025"), "2025-10", "month_of parses twitter date")
        checker.eq(sv.month_of(None), "unknown", "month_of falls back on missing date")
        checker.eq(sv.month_of("2026-06-05T13:00:01Z"), "2026-06", "month_of accepts ISO prefix")
        checker.eq(sv.safe_component('a/b:c*?'), "a_b_c", "safe_component sanitises handle")
        checker.eq(sv.ext_for("https://pbs.twimg.com/media/x.jpg?name=orig", "image/jpeg"), ".jpg", "ext_for uses content-type")
        checker.eq(sv.ext_for("https://video.twimg.com/x.mp4?tag=21", None), ".mp4", "ext_for falls back to URL suffix")
        photo = photo_row("a", "like", "1", "3_1", "https://pbs.twimg.com/media/X.jpg?format=jpg")
        tier, url = sv.resolve_tier(photo, policy)
        checker.eq(tier, "orig", "default policy: photo -> orig")
        checker.check("name=orig" in url, f"photo orig URL gets name=orig ({url})")
        large_policy = sv.make_policy("photo=large")
        tier, url = sv.resolve_tier(photo, large_policy)
        checker.eq(tier, "large", "custom policy: photo -> large")
        checker.check("name=large" in url, "photo large URL gets name=large")
        video = video_row("a", "like", "2", "13_2", "http://example.invalid")
        tier, url = sv.resolve_tier(video, policy)
        checker.eq(tier, "mid", "default policy: like video -> mid (P1-1)")
        checker.check(url.endswith("/v-mid.mp4"), f"mid tier picks the median bitrate ({url})")
        tier, url = sv.resolve_tier(video, sv.make_policy("low"))
        checker.check(tier == "low" and url.endswith("/v-low.mp4"), "explicit low preset still picks 256k")
        tier, url = sv.resolve_tier(video, sv.make_policy("recommended"))
        checker.eq(tier, "mid", "recommended preset agrees with default (mid)")
        tier, url = sv.resolve_tier(video, sv.make_policy("best"))
        checker.check(tier == "best" and url.endswith("/v-high.mp4"), "best preset picks highest bitrate")
        mid = sv.pick_variant(sv.parse_variants(video.variants_json, video.media_url), "mid")
        checker.check(mid.endswith("/v-mid.mp4"), "mid tier picks the median bitrate")
        try:
            sv.make_policy("nonsense")
            checker.check(False, "invalid --policy is rejected")
        except ValueError:
            checker.check(True, "invalid --policy is rejected")
        bad = photo_row("a", "like", "3", "3_3", "https://pbs.twimg.com/x.jpg")
        try:
            sv.resolve_tier(bad, sv.make_policy("photo=best"))
            checker.check(False, "tier invalid for media type is rejected")
        except ValueError:
            checker.check(True, "tier invalid for media type is rejected")
        checker.eq(sv.tier_rank("video", "best"), 3, "tier_rank: video best = 3")
        checker.eq(sv.tier_rank("video", "low"), 1, "tier_rank: video low = 1")
        checker.eq(sv.tier_rank("photo", "orig"), 2, "tier_rank: photo orig = 2")
        checker.check(sv.tier_dominates("video", "best", "low"), "best dominates low")
        checker.check(not sv.tier_dominates("video", "low", "best"), "low does NOT dominate best")
        checker.check(sv.tier_dominates("photo", "orig", "large"), "photo orig dominates large")
        checker.check(not sv.tier_dominates("video", "mid", "best"), "mid does NOT dominate best")

        # Snowflake fallback used by projections when tweet_created_at is missing.
        snowflake_id = str((1760916309000 - sv.SNOWFLAKE_TWITTER_EPOCH_MS) << 22)
        checker.eq(sv.snowflake_month(snowflake_id), "2025-10", "snowflake_month decodes the tweet UTC month")
        checker.eq(sv.snowflake_month("not-a-number"), None, "snowflake_month rejects non-numeric ids")
        checker.eq(sv.snowflake_month(None), None, "snowflake_month handles None")
        checker.eq(sv.projection_month(None, snowflake_id), "2025-10", "projection_month falls back to the Snowflake month")
        checker.eq(sv.projection_month(None, "not-a-number"), "unknown-date", "projection_month falls back to unknown-date")
        checker.eq(
            sv.projection_month("Sun Oct 19 23:25:09 +0000 2025", snowflake_id),
            "2025-10",
            "projection_month prefers tweet_created_at",
        )

        # P0-2: one media row joined with a like *and* a bookmark tweet record must
        # become two MediaRow candidates (otherwise the bookmark ref is lost before
        # the INSERT even runs).
        archive_rows = sv.rows_from_records(
            "acct-x",
            [archive_media("77", "3_77"), archive_media("78", "3_78")],
            [
                archive_tweet("77", "like"),
                archive_tweet("77", "bookmark"),
                archive_tweet("78", None),
            ],
        )
        checker.eq(len(archive_rows), 3, "rows_from_records expands a dual-collection tweet into 2 candidates")
        checker.eq(
            sorted(row.collection for row in archive_rows),
            ["bookmark", "like", "unknown"],
            "rows_from_records keeps like + bookmark + unknown collections",
        )
        bookmark_only = sv.rows_from_records(
            "acct-x",
            [archive_media("77", "3_77"), archive_media("78", "3_78")],
            [archive_tweet("77", "like"), archive_tweet("77", "bookmark"), archive_tweet("78", None)],
            collections=["bookmark"],
        )
        checker.eq(len(bookmark_only), 1, "rows_from_records honours the --collections filter")
        checker.eq(bookmark_only[0].collection, "bookmark", "rows_from_records keeps the selected collection")
        no_video = sv.rows_from_records(
            "acct-x",
            [archive_media("77", "3_77")],
            [archive_tweet("77", "like")],
            media_types=["video"],
        )
        checker.eq(no_video, [], "rows_from_records honours the --types filter")

        # ------------------------------------------------------------------ #
        checker.phase("phase 1: success / retry / 404 / dedupe / dominance / copy projection")
        rows = phase1_rows(base)
        store = silent_store(phase1_root).open()
        stats = store.run(rows, policy=policy, limit=0, project="copy")
        checker.eq(stats.selected_assets, 9, "9 requested (media_key,tier) plans")
        checker.eq(stats.selected_refs, 9, "9 refs selected")
        checker.eq(stats.downloaded, 7, "7 assets downloaded (low video dominated by best)")
        checker.eq(stats.failed_permanent, 1, "1 permanent failure (404)")
        checker.eq(stats.dominance_merged, 1, "1 low request merged into this run's best tier")
        checker.eq(stats.readonly_overwrites, 0, "no read-only overwrite needed on a clean store")
        checker.check(stats.retry_events >= 2, f"retry events recorded ({stats.retry_events})")
        checker.eq(stats.projected, 8, "8 refs projected (the 404 ref has no blob)")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets"), 8, "assets table has 8 rows (low merged)")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM refs"), 9, "refs table has 9 rows")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets WHERE state='done'"), 7, "7 assets done")
        checker.eq(count_blobs(store), 5, "5 unique blobs on disk (content dedupe)")
        checker.eq(stats.content_reused, 2, "2 downloads reused existing content-addressed blobs")
        checker.eq(len(leftover_parts(store)), 0, "no .part files left behind")
        checker.eq(db_scalar(store, "PRAGMA journal_mode"), "wal", "SQLite journal_mode is WAL")
        checker.eq(db_scalar(store, "SELECT value FROM meta WHERE key='schema_version'"), str(sv.SCHEMA_VERSION), "meta.schema_version written")
        checker.check(db_scalar(store, "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_refs_media_key'") == 1, "idx_refs_media_key exists")
        checker.check(db_scalar(store, "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_refs_account_created'") == 1, "idx_refs_account_created exists")

        flaky = asset_row(store, "3_1003", "orig")
        checker.eq(flaky["state"], "done", "flaky asset eventually done")
        checker.eq(flaky["retry_count"], 2, "flaky asset recorded 2 retries")
        missing = asset_row(store, "3_1004", "orig")
        checker.eq(missing["state"], "failed", "404 asset marked failed")
        checker.eq(missing["http_status"], 404, "404 asset keeps http_status=404")
        checker.check(bool(missing["last_error"]), "404 asset has last_error text")
        best = asset_row(store, "13_2001", "best")
        checker.check(best["state"] == "done", "media_key 13_2001 has its best asset done")
        checker.check(asset_row(store, "13_2001", "mid") is None, "no mid asset row was created (tier dominance)")
        dominant_ref = ref_row(store, "2002")
        checker.eq(dominant_ref["requested_tier"], "mid", "dominated ref keeps requested_tier=mid for audit")
        checker.eq(dominant_ref["asset_id"], best["id"], "dominated ref points at the best asset")
        checker.eq(asset_row(store, "16_3001", "best")["state"], "done", "animated_gif downloaded at best")
        checker.check(
            asset_row(store, "3_1005", "orig")["sha256"] == asset_row(store, "3_1006", "orig")["sha256"],
            "different media_keys with equal bytes share sha256",
        )
        ok_sha = asset_row(store, "3_1001", "orig")["sha256"]
        checker.check(
            asset_row(store, "3_1002", "orig")["sha256"] == ok_sha,
            "URL-identical identities share sha256",
        )

        # read-only blobs
        blob = store.blob_abs(asset_row(store, "3_1001", "orig")["blob_path"])
        checker.check(blob is not None and blob.exists(), "blob file exists at assets/<sha[:2]>/<sha>.ext")
        checker.check(blob is not None and sv.is_readonly(blob), "blob has FILE_ATTRIBUTE_READONLY")
        refused = False
        try:
            with open(blob, "r+b"):
                pass
        except OSError:
            refused = True
        checker.check(refused, "writes to a read-only blob are refused by the OS")

        # exports
        index = read_index(store)
        checker.eq(len(index), 9, "index.csv has one row per ref")
        audit_row = next(row for row in index if row["tweet_id"] == "2002")
        checker.check(
            audit_row["requested_tier"] == "mid" and audit_row["tier"] == "best",
            "index.csv shows tier=best + requested_tier=mid for the dominated ref",
        )
        checker.eq(audit_row["tweet_created_at"], "Fri Jun 05 13:00:01 +0000 2026", "index.csv carries tweet_created_at")
        summary = read_summary(store)
        checker.eq(summary["totals"]["assets"], 8, "summary.totals.assets matches DB")
        checker.eq(summary["totals"]["refs"], 9, "summary.totals.refs matches DB")
        checker.eq(summary["totals"]["refs_done"], 8, "summary counts 8 done refs")
        checker.eq(summary["totals"]["unique_blobs"], 5, "summary counts 5 unique blobs")
        checker.check(summary["totals"]["unique_blob_bytes"] > 0, "summary reports blob bytes")
        checker.check(set(summary["by_account"]) == {"t-1", "t-2", "t-3"}, "summary groups by account")
        checker.check(set(summary["by_tier"]) == {"orig", "best"}, "summary groups by tier (no low was downloaded)")
        checker.check(summary["by_collection"]["bookmark"]["refs"] == 2, "summary groups by collection")
        projected_rows = [row for row in index if row["projected_path"]]
        checker.eq(len(projected_rows), 8, "index.csv records 8 projected paths")
        writer_row = next(row for row in index if row["tweet_id"] == "1001")
        checker.eq(writer_row["projected_path"], "by-account/t-1/bookmark/2025-10/WriterOne@1001__0.jpg", "projection path naming by-account/<acct>/<collection>/<month>/<handle>@<tweet>__<index>.<ext>")
        dst = store.root / writer_row["projected_path"]
        checker.check(dst.exists(), "copy projection file exists")
        checker.check(dst.read_bytes() == blob.read_bytes(), "copy projection content equals blob")
        checker.check(not os.path.samefile(dst, blob), "copy projection is a real copy (different file)")
        checker.check("2026-06" in audit_row["projected_path"], "projection month comes from tweet_created_at (2026-06)")

        # -------------------------------------------------------------- #
        checker.phase("phase 1b: idempotent re-run (zero HTTP traffic)")
        before = server.snapshot()
        stats2 = store.run(rows, policy=policy, limit=0, project="none")
        after = server.snapshot()
        checker.eq(stats2.downloaded, 0, "second run downloads 0 assets")
        checker.eq(stats2.bytes_downloaded, 0, "second run transfers 0 bytes")
        checker.eq(stats2.already_done, 7, "second run skips 7 done plan assets")
        checker.eq(stats2.skipped_permanent, 1, "second run skips the permanent 404")
        checker.eq(stats2.dominance_reused, 1, "second run reuses best for the low request")
        checker.eq(after, before, "second run makes no HTTP requests at all")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets"), 8, "assets row count stable across runs")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM refs"), 9, "refs row count stable across runs")
        checker.eq(ref_row(store, "2002")["requested_tier"], "mid", "requested_tier audit value is stable")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 2: hardlink projection + hardlink->copy fallback")
        store = silent_store(phase1_root).open()
        stats3 = store.run(rows, policy=policy, limit=0, project="hardlink")
        checker.eq(stats3.downloaded, 0, "hardlink re-run downloads nothing")
        checker.eq(stats3.projected, 8, "hardlink run projects 8 files")
        link_dst = store.root / "by-account/t-1/bookmark/2025-10/WriterOne@1001__0.jpg"
        link_blob = store.blob_abs(asset_row(store, "3_1001", "orig")["blob_path"])
        checker.check(link_dst.exists() and os.path.samefile(link_dst, link_blob), "hardlink projection points at the blob inode")
        link_dst_b = store.root / "by-account/t-2/like/2025-10/ReaderTwo@1002__0.jpg"
        checker.check(link_dst_b.exists() and os.path.samefile(link_dst_b, link_blob), "second ref hardlinks the same blob")
        checker.check(link_blob.stat().st_nlink >= 3, f"blob inode link count reflects hardlinks ({link_blob.stat().st_nlink})")

        force_rmtree(store.root / "by-account")
        def _always_fail_link(_src, _dst):
            raise OSError("simulated cross-device link failure")
        store.link_fn = _always_fail_link
        stats4 = store.run(rows, policy=policy, limit=0, project="hardlink")
        store.link_fn = os.link
        checker.eq(stats4.projected, 8, "fallback run still projects 8 files")
        checker.check(stats4.projection_fallbacks >= 1, f"hardlink failures fall back to copy ({stats4.projection_fallbacks})")
        fallback_dst = store.root / "by-account/t-1/bookmark/2025-10/WriterOne@1001__0.jpg"
        checker.check(fallback_dst.exists(), "fallback projection file exists")
        checker.check(not os.path.samefile(fallback_dst, link_blob), "fallback projection is a copy, not a hardlink")
        checker.check(fallback_dst.read_bytes() == link_blob.read_bytes(), "fallback copy content matches blob")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 2b: P0-3 projection keeps like and bookmark apart")
        proj_root = base_dir / "phase2b"
        store = silent_store(proj_root).open()
        dual_proj_rows = [
            video_row("t-p", "bookmark", "2201", "13_2201", variants=[(1000, base + "/dom-high.mp4")], author="DualProj"),
            video_row("t-p", "like", "2201", "13_2201", variants=[(1000, base + "/dom-high.mp4")], author="DualProj"),
        ]
        proj_run = store.run(dual_proj_rows, policy=policy, limit=0, project="copy")
        checker.eq(proj_run.downloaded, 1, "P0-3 seed: one best download serves both collections")
        dual_refs = db_rows(store, "SELECT * FROM refs WHERE tweet_id='2201' ORDER BY collection")
        checker.eq([row["collection"] for row in dual_refs], ["bookmark", "like"], "P0-3: same tweet/media keeps like + bookmark refs")
        proj_blob = store.blob_abs(asset_row(store, "13_2201", "best")["blob_path"])
        rels = {row["collection"]: store.projection_rel(row, ".mp4") for row in dual_refs}
        checker.check(rels["bookmark"] != rels["like"], "P0-3: like and bookmark refs map to DIFFERENT projection paths")
        checker.eq(rels["like"].as_posix(), "by-account/t-p/like/2026-06/DualProj@2201__0.mp4", "P0-3: like path includes the collection segment")
        checker.eq(rels["bookmark"].as_posix(), "by-account/t-p/bookmark/2026-06/DualProj@2201__0.mp4", "P0-3: bookmark path includes the collection segment")
        for collection, rel in rels.items():
            dst = store.root / rel
            checker.check(dst.exists(), f"P0-3: {collection} projection exists (no silent overwrite)")
            checker.check(dst.read_bytes() == proj_blob.read_bytes(), f"P0-3: {collection} projection content matches the blob")
        index_dual = {
            row["collection"]: row["projected_path"]
            for row in read_index(store)
            if row["tweet_id"] == "2201"
        }
        checker.eq(index_dual["like"], rels["like"].as_posix(), "P0-3: index.csv projected_path for like carries the collection")
        checker.eq(index_dual["bookmark"], rels["bookmark"].as_posix(), "P0-3: index.csv projected_path for bookmark carries the collection")
        # Hardlink mode must produce two distinct paths as well.
        force_rmtree(store.root / "by-account")
        hard_run = store.run(dual_proj_rows, policy=policy, limit=0, project="hardlink")
        checker.eq(hard_run.downloaded, 0, "P0-3 hardlink pass re-uses the done best asset")
        hard_like = store.root / rels["like"]
        hard_bookmark = store.root / rels["bookmark"]
        checker.check(hard_like.exists() and hard_bookmark.exists(), "P0-3: hardlink mode writes both collection paths")
        checker.check(
            os.path.samefile(hard_like, proj_blob) and os.path.samefile(hard_bookmark, proj_blob),
            "P0-3: both hardlink paths point at the same blob inode",
        )
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 3: interrupted bodies, transient failure, resume")
        rows3 = rows + [
            video_row("t-1", "like", "4001", "13_4001", variants=[(0, base + "/heal.mp4")], author="Healer"),
            photo_row("t-1", "like", "5001", "3_5001", base + "/broken.jpg", author="BrokenFive"),
        ]
        store = silent_store(phase1_root).open()
        stats5 = store.run(rows3, policy=policy, limit=0, project="none")
        checker.eq(stats5.selected_assets, 11, "11 plans selected with heal/broken rows")
        heal = asset_row(store, "13_4001", "mid")
        checker.eq(heal["state"], "done", "truncated-then-healed download succeeds")
        checker.check(heal["retry_count"] >= 2, f"healed asset retried before success ({heal['retry_count']})")
        broken = asset_row(store, "3_5001", "orig")
        checker.eq(broken["state"], "failed", "always-truncated asset ends failed")
        checker.eq(broken["retry_count"], 3, "always-truncated asset used 1 attempt + 3 retries")
        checker.check(broken["sha256"] is None and broken["blob_path"] is None, "failed asset has no sha256/blob_path (no dirty row)")
        checker.eq(stats5.failed_transient, 1, "stats reports the transient failure")
        checker.eq(len(leftover_parts(store)), 0, "interrupted downloads leave no .part files")
        checker.eq(count_blobs(store), 6, "only the healed blob was added")
        counts_before = server.snapshot()
        stats6 = store.run(rows3, policy=policy, limit=0, project="none")
        counts_after = server.snapshot()
        checker.eq(stats6.downloaded, 0, "resume run completes no new downloads")
        checker.eq(counts_after.get("/heal.mp4"), counts_before.get("/heal.mp4"), "done asset is not refetched on resume")
        checker.check(counts_after.get("/broken.jpg", 0) > counts_before.get("/broken.jpg", 0), "transient failure is retried on the next run (resumable)")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets"), 10, "assets count stable after resume")
        dry = store.dry_run(rows3, policy, limit=0)
        checker.eq(dry.would_download, 1, "dry-run reports exactly 1 asset that still needs downloading")
        checker.eq(dry.selected_assets, 11, "dry-run sees all 11 planned assets")
        checker.eq(dry.dominance_reused, 1, "dry-run knows the low request is dominated by best")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 4: --limit selection is stable across runs")
        store = silent_store(phase4_root).open()
        first = store.run(rows, policy=policy, limit=3, project="none")
        checker.eq(first.selected_assets, 3, "limit=3 selects 3 plans")
        checker.eq(first.downloaded, 2, "limit=3 downloads 2 assets (low merged into best)")
        checker.eq(first.dominance_merged, 1, "limit=3 merges the dominated low plan")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM refs"), 3, "limit registers only the selected refs")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets"), 2, "limit creates only the serving asset rows")
        before = server.snapshot()
        second = store.run(rows, policy=policy, limit=3, project="none")
        checker.eq(second.downloaded, 0, "same limit re-run downloads 0 assets (acceptance criterion)")
        checker.eq(second.already_done, 2, "same limit re-run skips both done serving assets")
        checker.eq(second.dominance_reused, 1, "same limit re-run reuses best for low")
        checker.eq(server.snapshot(), before, "same limit re-run makes no HTTP requests")
        third = store.run(rows, policy=policy, limit=9, project="none")
        checker.eq(third.downloaded, 5, "raising the limit continues with the remaining assets")
        checker.eq(third.failed_permanent, 1, "raising the limit still reports the 404")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets"), 8, "all serving asset rows registered after widening the limit")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM refs"), 9, "all 9 refs registered after widening the limit")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 5: --max-mb soft budget stops and resumes")
        budget_root = base_dir / "phase5"
        store = silent_store(budget_root).open()
        limited = store.run(rows, policy=policy, limit=0, max_bytes=1000)
        checker.check(limited.budget_stopped, "tiny --max-mb stops the download loop")
        checker.eq(limited.downloaded, 1, "budget run downloads exactly one asset before stopping")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets WHERE state='pending'"), 7, "budget run leaves the rest pending (resumable)")
        resumed = store.run(rows, policy=policy, limit=0)
        checker.eq(resumed.downloaded, 6, "next run resumes the pending assets")
        checker.eq(resumed.failed_permanent, 1, "resume run finishes with the expected 404")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets WHERE state='done'"), 7, "resume run completes 7 downloads")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 6: tier dominance reuses an existing better tier (0 downloads)")
        store = silent_store(phase7_root).open()
        dom_rows = [
            video_row("t-d", "bookmark", "7001", "13_7001", variants=[(1000, base + "/dom-high.mp4")], author="Dom"),
        ]
        first = store.run(dom_rows, policy=policy, limit=0, project="none")
        checker.eq(first.downloaded, 1, "seed run downloads the bookmark best video")
        checker.eq(asset_row(store, "13_7001", "best")["state"], "done", "seed best asset is done")
        # A like/unknown row for the SAME media_key would want tier low: dominance must reuse best.
        like_rows = dom_rows + [video_row("t-d", "like", "7002", "13_7001", variants=[(1000, base + "/dom-high.mp4")], author="DomLike")]
        before = server.snapshot()
        second = store.run(like_rows, policy=policy, limit=0, project="none")
        after = server.snapshot()
        checker.eq(second.downloaded, 0, "existing best tier serves a like/low request: 0 downloads")
        checker.eq(second.bytes_downloaded, 0, "existing best tier serves a like/low request: 0 bytes")
        checker.eq(second.dominance_reused, 1, "dominance reuse counted")
        checker.eq(after, before, "dominance reuse makes no HTTP request")
        dominated = ref_row(store, "7002")
        checker.eq(dominated["requested_tier"], "mid", "like ref records requested_tier=mid while served by best")
        checker.eq(dominated["asset_id"], asset_row(store, "13_7001", "best")["id"], "like ref points at the done best asset")
        # Photo: existing orig (largest) dominates a later large request.
        photo_small = photo_row("t-p", "like", "7101", "3_7101", base + "/ok.jpg", author="PhotoDom")
        store.run([photo_small], policy=policy, limit=0, project="none")
        checker.eq(asset_row(store, "3_7101", "orig")["state"], "done", "seed photo orig asset is done")
        before = server.snapshot()
        large_run = store.run([photo_small], policy=sv.make_policy("photo=large"), limit=0, project="none")
        checker.eq(large_run.downloaded, 0, "existing photo orig serves a later large request: 0 downloads")
        checker.eq(large_run.dominance_reused, 1, "photo orig dominance counted")
        checker.eq(server.snapshot(), before, "photo dominance reuse makes no HTTP request")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 7: reverse upgrade -> superseded -> --gc reclaims bytes")
        store = silent_store(phase8_root).open()
        seed = [video_row("t-u", "like", "8001", "13_8001", variants=[(1000, base + "/up-low.mp4")], author="Up")]
        run1 = store.run(seed, policy=sv.make_policy("video=low"), limit=0, project="none")
        checker.eq(run1.downloaded, 1, "seed run downloads the like/low video")
        low_asset = asset_row(store, "13_8001", "low")
        checker.eq(low_asset["state"], "done", "low asset done before the upgrade")
        low_blob = store.blob_abs(low_asset["blob_path"])
        low_size = low_blob.stat().st_size
        checker.eq(low_size, len(MP4_UP_LOW), "low blob bytes match served body")
        upgrade_rows = seed + [
            video_row("t-u2", "bookmark", "8001", "13_8001", variants=[(1000, base + "/up-high.mp4")], author="UpBookmark"),
        ]
        run2 = store.run(upgrade_rows, policy=policy, limit=0, project="none")
        checker.eq(run2.downloaded, 1, "upgrade run downloads the higher (best) tier only")
        checker.eq(run2.upgraded, 1, "upgrade marks exactly one lower asset superseded")
        checker.check(run2.refs_repointed >= 1, f"upgrade repoints the old refs ({run2.refs_repointed})")
        best_asset = asset_row(store, "13_8001", "best")
        checker.eq(best_asset["state"], "done", "best asset is done after upgrade")
        checker.eq(asset_row(store, "13_8001", "low")["state"], "superseded", "old low asset is state=superseded")
        checker.eq(ref_row(store, "8001")["asset_id"], best_asset["id"], "old like ref now points at best")
        checker.eq(ref_row(store, "8001")["requested_tier"], "low", "old like ref keeps requested_tier=low (用高挡低 audit)")
        checker.check(ref_row(store, "8001")["tweet_created_at"], "like ref has tweet_created_at")
        # Orphan blob simulation: a file under assets/ no DB row references.
        orphan = store.assets_dir / "ff" / "orphan-selftest.bin"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan-bytes-1234")
        sv.set_readonly(orphan)
        checker.check(sv.is_readonly(low_blob), "P1-5: superseded blob is read-only before gc")
        checker.check(sv.is_readonly(orphan), "P1-5: orphan blob is read-only before gc")
        expected_reclaim = low_size + orphan.stat().st_size
        dry = store.gc(dry_run=True)
        checker.eq(dry["superseded_rows"], 1, "gc --dry-run sees 1 superseded row")
        checker.eq(dry["reclaim_bytes"], expected_reclaim, "gc --dry-run adds supervised blob + orphan blob bytes")
        checker.eq(dry["orphan_files"], 1, "gc --dry-run sees the orphan blob")
        checker.check(asset_row(store, "13_8001", "low") is not None, "gc --dry-run does not delete the superseded row")
        checker.check(low_blob.exists(), "gc --dry-run does not delete the superseded blob")
        result = store.gc()
        checker.eq(result["reclaimed_bytes"], expected_reclaim, "gc reports the reclaimed byte total")
        checker.eq(result.get("removed_files"), 2, "P1-5: gc really unlinked both read-only files")
        checker.check(result.get("removed_dirs", 0) >= 1, "P1-5: gc removed the emptied sha-prefix directory")
        checker.check(asset_row(store, "13_8001", "low") is None, "gc removed the superseded row")
        checker.check(not low_blob.exists(), "gc removed the superseded blob")
        checker.check(not orphan.exists(), "gc removed the orphan blob")
        checker.eq(count_blobs(store), 1, "only the surviving best blob remains")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM assets"), 1, "only the surviving best asset row remains")
        checker.eq(db_scalar(store, "SELECT COUNT(*) FROM refs WHERE asset_id=?", (best_asset["id"],)), 2, "both refs point at the surviving best asset")
        again = store.gc(dry_run=True)
        checker.eq(again["reclaim_bytes"], 0, "gc is idempotent (second dry-run reclaims nothing)")
        # P1-5: unlink_force is the single physical-delete primitive gc relies on;
        # prove it really removes a +R file and is a safe no-op on a missing one.
        probe = store.root / "probe-readonly.bin"
        probe.write_bytes(b"probe-readonly")
        sv.set_readonly(probe)
        checker.check(sv.is_readonly(probe), "P1-5: unlink_force probe file is read-only")
        checker.check(sv.unlink_force(probe), "P1-5: unlink_force really deletes a read-only file")
        checker.check(not probe.exists(), "P1-5: read-only probe file is gone")
        checker.check(not sv.unlink_force(probe), "P1-5: unlink_force is a safe no-op on a missing file")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 7b: P0-1 shared blob across a low->mid upgrade survives --gc")
        shared_root = base_dir / "phase7b"
        store = silent_store(shared_root).open()
        # mid and low resolve to the same URL (and therefore the same bytes/sha):
        # parse_variants dedupes the URL, so both tiers point at the same blob.
        shared_url = base + "/up-low.mp4"
        shared_row = video_row(
            "t-s", "like", "8101", "13_8101",
            variants=[(256000, shared_url), (832000, shared_url)], author="SharedUp",
        )
        low_run = store.run([shared_row], policy=sv.make_policy("video=low"), limit=0, project="none")
        checker.eq(low_run.downloaded, 1, "seed run downloads the low tier")
        low_asset = asset_row(store, "13_8101", "low")
        low_sha = low_asset["sha256"]
        low_blob_path = low_asset["blob_path"]
        low_blob = store.blob_abs(low_blob_path)
        checker.eq(low_asset["state"], "done", "low asset done before the upgrade")
        mid_run = store.run([shared_row], policy=sv.make_policy("video=mid"), limit=0, project="none")
        checker.eq(mid_run.downloaded, 1, "upgrade run downloads the mid tier")
        checker.eq(mid_run.upgraded, 1, "mid supersedes the byte-identical low asset")
        mid_asset = asset_row(store, "13_8101", "mid")
        checker.eq(asset_row(store, "13_8101", "low")["state"], "superseded", "old low row is superseded")
        checker.eq(mid_asset["sha256"], low_sha, "mid keeps the same content sha256")
        checker.eq(mid_asset["blob_path"], low_blob_path, "mid and low share one content-addressed blob")
        checker.check(low_blob.exists(), "shared blob exists before gc")
        dry = store.gc(dry_run=True)
        checker.eq(dry["superseded_rows"], 1, "gc dry-run sees exactly the superseded low row")
        checker.eq(dry["orphan_files"], 0, "shared blob is not reported as an orphan")
        checker.eq(dry["reclaim_bytes"], 0, "gc must not reclaim bytes of a still-referenced shared blob")
        result = store.gc()
        checker.eq(result["reclaimed_bytes"], 0, "gc keeps the blob referenced by the surviving mid row")
        checker.check(asset_row(store, "13_8101", "low") is None, "gc removed the superseded low row")
        surviving_mid = asset_row(store, "13_8101", "mid")
        checker.check(surviving_mid is not None and surviving_mid["state"] == "done", "mid row survives gc")
        checker.eq(surviving_mid["sha256"], low_sha, "sha256 unchanged after gc")
        checker.check(low_blob.exists() and sv.is_readonly(low_blob), "shared mid blob still on disk after gc")
        checker.eq(count_blobs(store), 1, "exactly one blob remains after gc")
        checker.eq(ref_row(store, "8101")["asset_id"], surviving_mid["id"], "ref still points at the mid asset")
        before = server.snapshot()
        reuse_run = store.run([shared_row], policy=sv.make_policy("video=low"), limit=0, project="none")
        checker.eq(reuse_run.downloaded, 0, "refs are still usable after gc: 0 downloads")
        checker.eq(reuse_run.dominance_reused, 1, "mid dominates the later low request after gc")
        checker.eq(server.snapshot(), before, "after-gc reuse makes no HTTP request")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 7c: P0-3 permanent vs transient tier failures")
        fail_root = base_dir / "phase7c"
        fail_log: list[str] = []
        store = silent_store(fail_root, log=fail_log.append).open()
        # (a) 503 twice then 200 on the high tier -> the high tier is served.
        flaky_row = video_row(
            "t-f", "bookmark", "6101", "13_6101",
            variants=[(256000, base + "/v-low.mp4"), (832000, base + "/v-mid.mp4"), (2176000, base + "/v-flaky.mp4")],
            author="FlakyHigh",
        )
        flaky_run = store.run([flaky_row], policy=sv.make_policy("video=best"), limit=0, project="none")
        checker.eq(flaky_run.downloaded, 1, "high tier retried through 2x 503 and is eventually served")
        checker.eq(flaky_run.failed_transient, 0, "no transient failure recorded after recovery")
        flaky_asset = asset_row(store, "13_6101", "best")
        checker.eq(flaky_asset["state"], "done", "high tier asset is done after recovery")
        checker.check(flaky_asset["retry_count"] >= 2, f"high tier recorded retries ({flaky_asset['retry_count']})")
        checker.eq(ref_row(store, "6101")["asset_id"], flaky_asset["id"], "ref is served by the recovered high tier")
        # (b) permanent 404 on the high tier -> degrade to an existing done lower tier.
        seed_row = video_row("t-g", "like", "6102", "13_6102", variants=[(1000, base + "/v-low.mp4")], author="LowSeed")
        store.run([seed_row], policy=sv.make_policy("video=low"), limit=0, project="none")
        low_asset_2 = asset_row(store, "13_6102", "low")
        checker.eq(low_asset_2["state"], "done", "low seed asset is done")
        dead_row = video_row(
            "t-g", "bookmark", "6103", "13_6102",
            variants=[(256000, base + "/v-low.mp4"), (832000, base + "/v-mid.mp4"), (2176000, base + "/v-404.mp4")],
            author="DeadHigh",
        )
        dead_run = store.run([dead_row], policy=sv.make_policy("video=best"), limit=0, project="none")
        checker.eq(dead_run.failed_permanent, 1, "permanent 404 on the high tier is recorded")
        dead_asset = asset_row(store, "13_6102", "best")
        checker.eq(dead_asset["state"], "failed", "permanently dead high tier is state=failed")
        checker.eq(dead_asset["http_status"], 404, "permanently dead high tier keeps http_status=404")
        checker.eq(dead_run.refs_downgraded, 1, "permanent failure allows exactly one ref downgrade")
        # P1-3: instead of jumping straight to the existing done low tier, the missing
        # middle tier is synthesised from variants_json and downloaded (best->mid->low).
        mid_asset_2 = asset_row(store, "13_6102", "mid")
        checker.check(
            mid_asset_2 is not None and mid_asset_2["state"] == "done",
            "P1-3: the mid gap is filled after the best tier 404s",
        )
        checker.eq(dead_run.downloaded, 1, "P1-3: the fallback mid tier was downloaded")
        checker.eq(ref_row(store, "6103")["asset_id"], mid_asset_2["id"], "P1-3: bookmark ref is served by the mid tier")
        checker.check(
            any("[degrade]" in line and "13_6102" in line for line in fail_log),
            "P1-3: each downgrade step is logged",
        )
        # (c) transient failure on the high tier with a done lower tier -> NO downgrade.
        seed_row_c = video_row("t-h", "like", "6104", "13_6103", variants=[(1000, base + "/v-low.mp4")], author="LowSeedC")
        store.run([seed_row_c], policy=sv.make_policy("video=low"), limit=0, project="none")
        low_asset_c = asset_row(store, "13_6103", "low")
        transient_row = video_row(
            "t-h", "bookmark", "6105", "13_6103",
            variants=[(256000, base + "/v-low.mp4"), (832000, base + "/v-mid.mp4"), (2176000, base + "/v-transient.mp4")],
            author="TransientHigh",
        )
        transient_run = store.run([transient_row], policy=sv.make_policy("video=best"), limit=0, project="none")
        checker.eq(transient_run.failed_transient, 1, "transient high-tier failure is recorded")
        transient_asset = asset_row(store, "13_6103", "best")
        checker.eq(transient_asset["state"], "failed", "transient high tier stays retryable (state=failed)")
        checker.check(
            transient_asset["http_status"] not in sv.PERMANENT_HTTP_STATUSES,
            "transient status is not in PERMANENT_HTTP_STATUSES",
        )
        checker.eq(
            ref_row(store, "6105")["asset_id"],
            transient_asset["id"],
            "ref must NOT be silently repointed to the done low tier on a transient error",
        )
        checker.eq(asset_row(store, "13_6103", "low")["state"], "done", "the done low tier is left untouched")
        checker.eq(transient_run.refs_unresolved, 1, "the unresolved ref is counted in stats")
        checker.eq(ref_row(store, "6104")["asset_id"], low_asset_c["id"], "the earlier like ref still points at low")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 7d: P0-2 self-heal: done best blob lost, this run requests only mid")
        heal_root = base_dir / "phase7d"
        store = silent_store(heal_root).open()
        heal_variants = [
            (256000, base + "/v-low.mp4"),
            (832000, base + "/v-mid.mp4"),
            (2176000, base + "/v-high.mp4"),
        ]
        seed_row = video_row("t-heal", "bookmark", "6301", "13_6301", variants=heal_variants, author="HealSeed")
        seed_run = store.run([seed_row], policy=policy, limit=0, project="none")
        checker.eq(seed_run.downloaded, 1, "P0-2 seed: bookmark best downloaded")
        best_asset = asset_row(store, "13_6301", "best")
        checker.eq(best_asset["state"], "done", "P0-2 seed: best asset is done with a blob on disk")
        best_blob = store.blob_abs(best_asset["blob_path"])
        checker.check(best_blob is not None and best_blob.exists(), "P0-2 seed: best blob exists")
        # Physical loss: the DB row still says done/best, the file is gone.
        sv.clear_readonly(best_blob)
        best_blob.unlink()
        checker.check(not best_blob.exists(), "P0-2: best blob deleted behind the DB's back")
        heal_rows = [
            video_row("t-heal", "like", "6302", "13_6301", variants=heal_variants, author="HealLike")
        ]
        before_counts = server.snapshot()
        heal_run = store.run(heal_rows, policy=policy, limit=0, project="none")
        after_counts = server.snapshot()
        best_after = asset_row(store, "13_6301", "best")
        fetched = {
            path: after_counts.get(path, 0) - before_counts.get(path, 0)
            for path in ("/v-high.mp4", "/v-mid.mp4", "/v-low.mp4")
        }
        checker.eq(best_after["state"], "done", "P0-2: lost best is re-downloaded and returns to done")
        checker.check(best_blob.exists() and sv.is_readonly(best_blob), "P0-2: healed best blob is back and read-only")
        checker.eq(fetched["/v-high.mp4"], 1, "P0-2: self-heal re-fetched the best URL (assets.url synthesis)")
        checker.eq(fetched["/v-mid.mp4"] + fetched["/v-low.mp4"], 0, "P0-2: no lower tier was downloaded instead")
        checker.eq(
            ref_row(store, "6302")["asset_id"],
            best_after["id"],
            "P0-2: the mid request is served by the healed best tier",
        )
        checker.eq(heal_run.downloaded, 1, "P0-2: exactly one download (the healed best)")
        checker.eq(heal_run.refs_downgraded, 0, "P0-2: healing is an upgrade, not a downgrade")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 7e: P1-3 permanent 404 walks best -> mid -> low")
        deg_root = base_dir / "phase7e"
        deg_log: list[str] = []
        store = silent_store(deg_root, log=deg_log.append).open()
        # (a) single-collection video: only best is requested, best 404, mid works.
        single = video_row(
            "t-deg", "bookmark", "6401", "13_6401",
            variants=[(256000, base + "/v-low.mp4"), (832000, base + "/v-mid.mp4"), (2176000, base + "/v-404.mp4")],
            author="DegSingle",
        )
        run_a = store.run([single], policy=policy, limit=0, project="none")
        best_a = asset_row(store, "13_6401", "best")
        mid_a = asset_row(store, "13_6401", "mid")
        checker.eq(best_a["state"], "failed", "P1-3: best stays failed after HTTP 404")
        checker.eq(best_a["http_status"], 404, "P1-3: best keeps http_status=404")
        checker.check(mid_a is not None and mid_a["state"] == "done", "P1-3: mid synthesised from variants and downloaded")
        checker.eq(run_a.downloaded, 1, "P1-3: exactly the fallback mid download happened")
        checker.eq(run_a.failed_permanent, 1, "P1-3: one permanent failure recorded")
        checker.eq(run_a.refs_downgraded, 1, "P1-3: the ref is counted as downgraded")
        checker.eq(ref_row(store, "6401")["asset_id"], mid_a["id"], "P1-3: ref is served by the mid tier")
        checker.check(
            any("[degrade]" in line and "13_6401" in line for line in deg_log),
            "P1-3: the best->mid downgrade step is logged",
        )
        # (b) best and mid 404 -> the low tier is tried and served.
        cascade = video_row(
            "t-cas", "bookmark", "6402", "13_6402",
            variants=[(256000, base + "/v-low.mp4"), (832000, base + "/v-404-mid.mp4"), (2176000, base + "/v-404.mp4")],
            author="DegCascade",
        )
        run_b = store.run([cascade], policy=policy, limit=0, project="none")
        low_b = asset_row(store, "13_6402", "low")
        checker.check(low_b is not None and low_b["state"] == "done", "P1-3: cascade reaches the low tier")
        checker.eq(run_b.failed_permanent, 2, "P1-3: best + mid permanent failures both recorded")
        checker.eq(run_b.refs_downgraded, 1, "P1-3: one ref downgraded all the way to low")
        checker.eq(ref_row(store, "6402")["asset_id"], low_b["id"], "P1-3: ref served by low after cascading")
        checker.eq(asset_row(store, "13_6402", "mid")["state"], "failed", "P1-3: mid row stays failed permanent")
        # (c) every ladder tier 404 -> unresolved, no silent lower-tier repoint.
        dead_row = video_row(
            "t-ded", "bookmark", "6403", "13_6403",
            variants=[(256000, base + "/v-404-low.mp4"), (832000, base + "/v-404-mid.mp4"), (2176000, base + "/v-404.mp4")],
            author="DegDead",
        )
        run_c = store.run([dead_row], policy=policy, limit=0, project="none")
        checker.eq(run_c.failed_permanent, 3, "P1-3: all three ladder tiers fail permanently")
        checker.eq(run_c.refs_unresolved, 1, "P1-3: group is unresolved only when every tier failed")
        checker.eq(
            ref_row(store, "6403")["asset_id"],
            asset_row(store, "13_6403", "best")["id"],
            "P1-3: an unresolved ref stays on the requested (failed) tier",
        )
        # dry-run agrees: a dead best with a synthesised lower tier is still "would download".
        store.conn.execute(
            "INSERT INTO assets(media_key, media_type, tier, url, state, http_status, retry_count,"
            " created_at, updated_at) VALUES('13_6404', 'video', 'best', ?, 'failed', 404, 0, ?, ?)",
            (base + "/v-404.mp4", sv.now_iso(), sv.now_iso()),
        )
        store.conn.commit()
        dry_row = video_row(
            "t-dry", "bookmark", "6404", "13_6404",
            variants=[(256000, base + "/v-low.mp4"), (832000, base + "/v-mid.mp4"), (2176000, base + "/v-404.mp4")],
            author="DegDry",
        )
        dry = store.dry_run([dry_row], policy)
        checker.eq(dry.would_download, 1, "P1-3: dry-run still plans the synthesised mid fallback")
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 8: Windows read-only overwrite path + P1-1 same-size hash check")
        ro_log: list[str] = []
        store = silent_store(phase9_root, log=ro_log.append)
        ok_sha = sha256_bytes(JPEG_A)
        stale_target = store.assets_dir / ok_sha[:2] / f"{ok_sha}.jpg"
        stale_target.parent.mkdir(parents=True, exist_ok=True)
        stale_target.write_bytes(b"stale-content-with-a-different-size")
        sv.set_readonly(stale_target)
        checker.check(sv.is_readonly(stale_target), "pre-seeded stale blob is read-only")
        same_size_sha = sha256_bytes(JPEG_SHARED)
        same_size_target = store.assets_dir / same_size_sha[:2] / f"{same_size_sha}.jpg"
        same_size_target.parent.mkdir(parents=True, exist_ok=True)
        same_size_target.write_bytes(b"Z" * len(JPEG_SHARED))  # same size, different bytes
        sv.set_readonly(same_size_target)
        store = store.open()
        ro_rows = [
            photo_row("t-ro", "like", "9001", "3_9001", base + "/ok.jpg", author="RO"),
            photo_row("t-ro", "like", "9002", "3_9002", base + "/shared-a.jpg", author="RO2"),
        ]
        ro_stats = store.run(ro_rows, policy=policy, limit=0, project="none")
        checker.eq(ro_stats.downloaded, 2, "both downloads succeeded over pre-existing read-only blobs")
        checker.eq(ro_stats.readonly_overwrites, 2, "P1-1: different-size AND same-size-corrupt targets were both overwritten")
        checker.eq(ro_stats.content_reused, 0, "P1-1: a same-size but different-hash target is NOT reused")
        ok_asset = asset_row(store, "3_9001", "orig")
        ok_blob = store.blob_abs(ok_asset["blob_path"])
        checker.check(ok_blob.exists() and ok_blob.read_bytes() == JPEG_A, "overwritten blob contains the fresh download bytes")
        checker.eq(ok_blob.stat().st_size, len(JPEG_A), "overwritten blob has the downloaded size")
        checker.check(sv.is_readonly(ok_blob), "overwritten blob is read-only again")
        checker.eq(len(leftover_parts(store)), 0, "no .part file left by the overwrite path")
        same_blob = store.blob_abs(asset_row(store, "3_9002", "orig")["blob_path"])
        checker.eq(
            sv.file_sha256(same_blob),
            sha256_bytes(JPEG_SHARED),
            "P1-1: equal-length corrupt blob is replaced by the fresh download (hash matches new content)",
        )
        checker.check(sv.is_readonly(same_blob), "P1-1: replaced equal-length blob is read-only again")
        checker.eq(same_blob.read_bytes(), JPEG_SHARED, "P1-1: equal-length corrupt bytes are gone")
        checker.check(
            any("same size but different sha256" in line for line in ro_log),
            "P1-1: the equal-length corruption replacement is logged",
        )
        store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 9: legacy DB migration + refs self-contained projection")
        phase10_root.mkdir(parents=True, exist_ok=True)
        legacy_db = phase10_root / "media.db"
        legacy_sha = sha256_bytes(JPEG_A)
        legacy_rel = f"assets/{legacy_sha[:2]}/{legacy_sha}.jpg"
        legacy_blob = phase10_root / legacy_rel
        legacy_blob.parent.mkdir(parents=True, exist_ok=True)
        legacy_blob.write_bytes(JPEG_A)
        sv.set_readonly(legacy_blob)
        legacy = sqlite3.connect(str(legacy_db))
        legacy.executescript(
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, media_key TEXT NOT NULL,
                media_type TEXT, tier TEXT NOT NULL, url TEXT, sha256 TEXT, size INTEGER,
                content_type TEXT, blob_path TEXT, state TEXT NOT NULL DEFAULT 'pending',
                http_status INTEGER, retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(media_key, tier)
            );
            CREATE TABLE refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id INTEGER NOT NULL,
                account TEXT NOT NULL, collection TEXT, tweet_id TEXT NOT NULL,
                media_index INTEGER NOT NULL, media_type TEXT, author_handle TEXT,
                created_at TEXT NOT NULL, UNIQUE(account, tweet_id, media_index)
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        legacy.execute(
            "INSERT INTO assets(media_key, media_type, tier, url, sha256, size, content_type, "
            "blob_path, state, http_status, retry_count, created_at, updated_at) "
            "VALUES(?, 'photo', 'orig', ?, ?, ?, 'image/jpeg', ?, 'done', 200, 0, ?, ?)",
            ("3_9001", base + "/ok.jpg", legacy_sha, len(JPEG_A), legacy_rel, "2026-01-01T00:00:00+0800", "2026-01-01T00:00:00+0800"),
        )
        legacy.execute(
            "INSERT INTO refs(asset_id, account, collection, tweet_id, media_index, media_type, "
            "author_handle, created_at) VALUES(1, 'acct-legacy', 'like', '9001', 0, 'photo', 'Legacy', ?)",
            ("2026-01-01T00:00:00+0800",),
        )
        legacy.execute(
            "INSERT INTO refs(asset_id, account, collection, tweet_id, media_index, media_type, "
            "author_handle, created_at) VALUES(1, 'acct-legacy', NULL, '9002', 0, 'photo', 'LegacyNull', ?)",
            ("2026-01-01T00:00:00+0800",),
        )
        legacy.execute("INSERT INTO meta(key, value) VALUES('schema_version', '3')")
        legacy.commit()
        legacy.close()
        legacy_log: list[str] = []
        legacy_store = sv.Store(phase10_root, log=legacy_log.append).open()
        ref_cols = {row[1] for row in legacy_store.conn.execute("PRAGMA table_info(refs)")}
        checker.check({"media_key", "tweet_created_at", "requested_tier"} <= ref_cols, "migration added media_key/tweet_created_at/requested_tier")
        legacy_ref = ref_row(legacy_store, "9001")
        checker.eq(legacy_ref["media_key"], "3_9001", "migration backfilled refs.media_key from assets")
        checker.eq(legacy_ref["requested_tier"], "orig", "migration backfilled requested_tier from the asset tier")
        checker.check(legacy_ref["tweet_created_at"] is None, "tweet_created_at is unknown until the owning tweet is scanned")
        checker.eq(db_scalar(legacy_store, "SELECT value FROM meta WHERE key='schema_version'"), str(sv.SCHEMA_VERSION), "migration bumped meta.schema_version to 5")
        idx_names = {row[1] for row in legacy_store.conn.execute("PRAGMA index_list(refs)")}
        checker.check({"idx_refs_media_key", "idx_refs_account_created"} <= idx_names, "migration created the refs indexes")
        # v4 -> v5: rebuilt refs table (collection NOT NULL, composite UNIQUE, FK).
        ref_info = {row[1]: row for row in legacy_store.conn.execute("PRAGMA table_info(refs)")}
        checker.eq(ref_info["collection"][3], 1, "migration rebuilt refs.collection as NOT NULL")
        unique_cols: list[str] = []
        for index in legacy_store.conn.execute("PRAGMA index_list(refs)"):
            if index[2]:  # unique flag
                cols = [row[2] for row in legacy_store.conn.execute(f"PRAGMA index_info({index[1]})")]
                if "collection" in cols:
                    unique_cols = cols
        checker.eq(
            unique_cols,
            ["account", "collection", "tweet_id", "media_key", "media_index"],
            "migration rebuilt UNIQUE(account, collection, tweet_id, media_key, media_index)",
        )
        fk_rows = legacy_store.conn.execute("PRAGMA foreign_key_list(refs)").fetchall()
        checker.check(
            any(row[2] == "assets" and str(row[6]).upper() == "RESTRICT" for row in fk_rows),
            "migration added asset_id REFERENCES assets(id) ON DELETE RESTRICT",
        )
        checker.eq(db_scalar(legacy_store, "PRAGMA foreign_keys"), 1, "PRAGMA foreign_keys=ON on the connection")
        checker.eq(
            legacy_store.conn.execute("PRAGMA foreign_key_check").fetchall(),
            [],
            "PRAGMA foreign_key_check is empty after migration",
        )
        null_collection_ref = ref_row(legacy_store, "9002")
        checker.eq(null_collection_ref["collection"], "unknown", "migration backfills NULL collection with 'unknown'")
        checker.check(any("rebuilt refs" in line for line in legacy_log), "migration logged the v5 refs rebuild")
        checker.eq(
            db_scalar(legacy_store, "SELECT COUNT(*) FROM sqlite_master WHERE name='refs_new'"),
            0,
            "migration leaves no temporary refs_new table behind",
        )
        legacy_store.close()
        legacy_store = sv.Store(phase10_root, log=legacy_log.append).open()
        checker.eq(db_scalar(legacy_store, "SELECT COUNT(*) FROM refs"), 2, "re-opened migrated DB is idempotent (refs stable)")
        checker.eq(db_scalar(legacy_store, "SELECT COUNT(*) FROM assets"), 1, "re-opened migrated DB is idempotent (assets stable)")
        legacy_store.close()
        legacy_store = sv.Store(phase10_root, log=legacy_log.append).open()
        # P0-2: the same (tweet, media) keeps a like AND a bookmark ref after the
        # rebuild (old UNIQUE dropped the second one).
        dual_rows = [
            photo_row("acct-legacy", "like", "9100", "3_9001", base + "/ok.jpg", author="Dual", created="Sun Oct 19 23:25:09 +0000 2025"),
            photo_row("acct-legacy", "bookmark", "9100", "3_9001", base + "/ok.jpg", author="Dual", created="Sun Oct 19 23:25:09 +0000 2025"),
        ]
        before = server.snapshot()
        dual_run = legacy_store.run(dual_rows, policy=policy, limit=0, project="none")
        checker.eq(dual_run.downloaded, 0, "dual-collection refs are served by the migrated done asset")
        dual_refs = db_rows(
            legacy_store,
            "SELECT collection, requested_tier, asset_id FROM refs WHERE account='acct-legacy' AND tweet_id='9100'"
            " ORDER BY collection",
        )
        checker.eq(len(dual_refs), 2, "the same tweet/media keeps BOTH a like and a bookmark ref")
        checker.eq([row["collection"] for row in dual_refs], ["bookmark", "like"], "ref collections are bookmark + like")
        checker.check(all(row["asset_id"] == 1 for row in dual_refs), "both collection refs point at the same done asset")
        # A second insert of the exact same identity must be ignored (idempotent).
        run_again = legacy_store.run(dual_rows, policy=policy, limit=0, project="none")
        checker.eq(run_again.new_refs, 0, "re-inserting both collection refs is idempotent")
        checker.eq(
            db_scalar(legacy_store, "SELECT COUNT(*) FROM refs WHERE tweet_id='9100'"),
            2,
            "no duplicate refs after a second run",
        )
        # FK RESTRICT: a referenced asset row cannot be deleted (gc deletes only
        # superseded rows without refs, so this never triggers in normal gc).
        fk_restricted = False
        try:
            legacy_store.conn.execute("DELETE FROM assets WHERE id=1")
            legacy_store.conn.rollback()
        except sqlite3.IntegrityError:
            fk_restricted = True
            legacy_store.conn.rollback()
        checker.check(fk_restricted, "FK asset_id ON DELETE RESTRICT blocks deleting a referenced asset")
        before = server.snapshot()
        legacy_rows = [
            photo_row(
                "acct-legacy", "like", "9001", "3_9001", base + "/ok.jpg",
                author="Legacy", created="Sun Oct 19 23:25:09 +0000 2025",
            )
        ]
        legacy_stats = legacy_store.run(legacy_rows, policy=policy, limit=0, project="copy")
        checker.eq(legacy_stats.downloaded, 0, "migrated done asset is served without a download")
        checker.eq(legacy_stats.already_done, 1, "migrated done asset counted as already done")
        checker.eq(server.snapshot(), before, "migrated store run makes no HTTP request")
        backfilled = ref_row(legacy_store, "9001")
        checker.eq(backfilled["tweet_created_at"], "Sun Oct 19 23:25:09 +0000 2025", "tweet_created_at backfilled lazily from the scanned tweet")
        projected = legacy_store.root / "by-account/acct-legacy/like/2025-10/Legacy@9001__0.jpg"
        checker.check(projected.exists(), "legacy ref projection uses tweet_created_at month (2025-10) without LanceDB")
        checker.check(projected.read_bytes() == JPEG_A, "legacy projection content matches the blob")
        # P1-2: a ref without tweet_created_at projects by Snowflake month (9002 is
        # a legacy id whose >>22 is 0 -> 2010-11); an unparseable id goes to unknown-date/.
        snowflake_projected = legacy_store.root / "by-account/acct-legacy/unknown/2010-11/LegacyNull@9002__0.jpg"
        checker.check(snowflake_projected.exists(), "missing tweet_created_at projects via Snowflake month")
        ghost = legacy_store.conn.execute(
            "INSERT INTO refs(asset_id, account, collection, tweet_id, media_key, media_index,"
            " media_type, author_handle, tweet_created_at, requested_tier, created_at)"
            " VALUES(1, 'acct-legacy', 'unknown', 'not-a-snowflake', '3_9001', 1, 'photo',"
            " 'Ghost', NULL, 'orig', '2026-01-01T00:00:00+0800')"
        )
        legacy_store.conn.commit()
        checker.eq(ghost.rowcount, 1, "unknown-date test ref inserted")
        legacy_store.project_phase([], "copy", sv.RunStats())
        unknown_date = legacy_store.root / "by-account/acct-legacy/unknown/unknown-date/Ghost@not-a-snowflake__1.jpg"
        checker.check(unknown_date.exists(), "unparseable tweet id falls back to unknown-date/")
        legacy_store.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 9b: P0-4 process lock + P1-2 .lock.info sidecar + stale .part cleanup")
        lock_root = base_dir / "phase-lock"
        stale_parts = seed_parts(lock_root, 2)
        lock_log: list[str] = []
        lock_store = silent_store(lock_root, log=lock_log.append)
        lock_store.open()
        checker.check(lock_store.lock_held, "first Store.open() holds store/.lock")
        checker.check((lock_root / sv.STORE_LOCK_NAME).is_file(), "store/.lock file exists while held")
        lock_info = lock_root / sv.STORE_LOCK_INFO_NAME
        checker.check(lock_info.is_file(), "P1-2: store/.lock.info sidecar exists while held")
        checker.check(f"pid={os.getpid()}" in lock_info.read_text(encoding="utf-8"), "P1-2: .lock.info records the owner pid")
        checker.eq(leftover_parts(lock_store), [], "holding the lock removes ALL stale .part files")
        checker.check(any("cleaned 2 stale .part" in line for line in lock_log), "lock log prints the .part cleanup count")
        checker.check(all(not path.exists() for path in stale_parts), "stale .part files were deleted")
        second = silent_store(lock_root)
        lock_failed = False
        lock_message = ""
        try:
            second.open()
        except sv.StoreLockError as exc:
            lock_failed = True
            lock_message = str(exc)
        checker.check(lock_failed, "second handle fails fast while the lock is held")
        checker.check("another store_v3 instance" in lock_message, "lock error message says another instance is running")
        checker.check(
            f"pid={os.getpid()}" in lock_message,
            "P1-2: busy-lock message reads the owner pid from .lock.info",
        )
        checker.check(second.conn is None and not second.lock_held, "failed second open leaves no DB/lock behind")
        # The failed acquirer must not have clobbered the sidecar.
        checker.check(lock_info.is_file() and f"pid={os.getpid()}" in lock_info.read_text(encoding="utf-8"), "P1-2: failed acquire leaves the owner hint intact")
        lock_store.close()
        checker.check(not lock_store.lock_held, "closing the first Store releases the lock")
        checker.check(not lock_info.exists(), "P1-2: releasing the lock removes .lock.info")
        second.open()
        checker.check(second.lock_held, "second handle can acquire the lock after release")
        checker.check(f"pid={os.getpid()}" in lock_info.read_text(encoding="utf-8"), "P1-2: new owner rewrites .lock.info")
        second.close()
        checker.check(not lock_info.exists(), "P1-2: second release also cleans .lock.info")
        # A crashed owner's stale hint must never break acquisition (file removed on open).
        lock_info.write_text("pid=999999 died", encoding="utf-8")
        recovered = silent_store(lock_root)
        recovered.open()
        checker.check(recovered.lock_held, "P1-2: a stale .lock.info does not block a new owner")
        checker.check(f"pid={os.getpid()}" in lock_info.read_text(encoding="utf-8"), "P1-2: the new owner overwrites the stale hint")
        recovered.close()

        # -------------------------------------------------------------- #
        checker.phase("phase 9c: P1-3 --verify quick/full + orphan count")
        verify_root = base_dir / "phase-verify"
        store = silent_store(verify_root).open()
        verify_row = photo_row("t-v", "like", "9201", "3_9201", base + "/ok.jpg", author="Verifier")
        store.run([verify_row], policy=policy, limit=0, project="none")
        verify_asset = asset_row(store, "3_9201", "orig")
        verify_blob = store.blob_abs(verify_asset["blob_path"])
        store.close()
        vlog: list[str] = []
        verifier = silent_store(verify_root, log=vlog.append)
        checker.eq(verifier.verify(quick=True), 0, "verify --quick passes on a clean store")
        checker.eq(verifier.verify(quick=False), 0, "verify full (sha256) passes on a clean store")
        original_bytes = verify_blob.read_bytes()
        sv.clear_readonly(verify_blob)
        verify_blob.write_bytes(original_bytes + b"corrupt-extra")
        checker.eq(verifier.verify(quick=True), 1, "verify --quick fails on a size mismatch")
        checker.eq(verifier.verify(quick=False), 1, "verify full also fails on a size mismatch")
        checker.check(any("BAD asset" in line for line in vlog), "verify lists the inconsistency")
        # Same-size corruption: only the full sha256 check can see it.
        verify_blob.write_bytes(original_bytes[:-1] + bytes([original_bytes[-1] ^ 0xFF]))
        sv.set_readonly(verify_blob)
        checker.eq(verifier.verify(quick=True), 0, "verify --quick cannot see a same-size corruption")
        checker.eq(verifier.verify(quick=False), 1, "verify full catches the same-size sha256 mismatch")
        # Missing blob.
        sv.clear_readonly(verify_blob)
        verify_blob.unlink()
        checker.eq(verifier.verify(quick=True), 1, "verify --quick fails on a missing blob")
        # Restore the good blob.
        verify_blob.write_bytes(original_bytes)
        sv.set_readonly(verify_blob)
        checker.eq(verifier.verify(quick=True), 0, "verify passes again after restoring the blob")
        # Orphan blobs are reported in the stats but do not fail verification.
        orphan = verify_root / "assets" / "ee" / "verify-orphan.bin"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"verify-orphan-bytes")
        sv.set_readonly(orphan)
        vlog.clear()
        checker.eq(verifier.verify(quick=True), 0, "orphan blobs are reported but do not fail verify")
        checker.check(any("orphan blobs=1" in line for line in vlog), "verify reports the orphan blob count")
        checker.check(any("ORPHAN assets" in line for line in vlog), "verify lists the orphan blob path")
        sv.clear_readonly(orphan)
        orphan.unlink()
        # A lost read-only attribute is an inconsistency.
        sv.clear_readonly(verify_blob)
        checker.eq(verifier.verify(quick=True), 1, "verify fails when the read-only attribute is lost")
        sv.set_readonly(verify_blob)
        checker.eq(verifier.verify(quick=True), 0, "verify passes after restoring the read-only attribute")
        # A store without a database cannot be verified.
        checker.eq(silent_store(base_dir / "phase-nodb").verify(), 2, "verify returns 2 when media.db is missing")

        # -------------------------------------------------------------- #
        checker.phase("phase 10: backup_store.py consistent snapshot + P0-1 store lock")
        project_root = base_dir / "proj"
        archive_dir = project_root / "accounts/acct-fake/tweetxvault/archive.lancedb"
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "data.lance").write_bytes(b"fake-lance-part")
        backup_dest = base_dir / "backup"
        # (a) A busy store/.lock must make the backup fail fast: the db-only mode
        #     (default) through the CLI (exit 4) and --copy-assets through the API.
        lock_probe = sv.StoreLock(phase1_root / sv.STORE_LOCK_NAME)
        lock_probe.acquire()
        locked_dest = base_dir / "backup-locked"
        db_only_code = bs.main(["--dest", str(locked_dest), "--store", str(phase1_root)])
        checker.eq(db_only_code, sv.EXIT_LOCK_BUSY, "P0-1: db-only backup exits 4 while store/.lock is held")
        checker.check(not locked_dest.exists(), "P0-1: a rejected backup does not create the destination")
        copy_locked = False
        copy_message = ""
        try:
            bs.create_backup(phase1_root, locked_dest, copy_assets=True, log=lambda _msg: None)
        except sv.StoreLockError as exc:
            copy_locked = True
            copy_message = str(exc)
        checker.check(copy_locked, "P0-1: --copy-assets backup refuses a busy store/.lock too")
        checker.check("another store_v3 instance" in copy_message, "P0-1: backup lock error matches the store_v3 wording")
        lock_probe.release()
        # (b) The backup holds the lock for the whole snapshot: while create_backup is
        #     inside the copy loop a second acquirer must be refused (deterministic).
        backup_started = threading.Event()
        release_backup = threading.Event()

        def backup_log(message: str) -> None:
            if message.startswith("[backup] source") and not backup_started.is_set():
                backup_started.set()
                release_backup.wait(10)

        backup_result: dict = {}

        def run_backup() -> None:
            try:
                backup_result["manifest"] = bs.create_backup(
                    phase1_root,
                    backup_dest,
                    copy_assets=True,
                    include_archives=True,
                    projects_root=project_root,
                    log=backup_log,
                )
            except BaseException as exc:  # noqa: BLE001 - asserted below
                backup_result["error"] = exc

        backup_thread = threading.Thread(target=run_backup)
        backup_thread.start()
        checker.check(backup_started.wait(10), "P0-1: backup acquired store/.lock before snapshotting")
        rival = sv.StoreLock(phase1_root / sv.STORE_LOCK_NAME)
        rival_failed = False
        rival_message = ""
        try:
            rival.acquire()
        except sv.StoreLockError as exc:
            rival_failed = True
            rival_message = str(exc)
        checker.check(rival_failed, "P0-1: a second writer/backup is refused while the backup runs")
        checker.check(f"pid={os.getpid()}" in rival_message, "P0-1: the refusal names the backup pid via .lock.info")
        release_backup.set()
        backup_thread.join(30)
        checker.check(not backup_thread.is_alive(), "P0-1: backup finished")
        checker.check("error" not in backup_result, f"P0-1: backup thread completed cleanly ({backup_result.get('error')})")
        checker.check(not (phase1_root / sv.STORE_LOCK_INFO_NAME).exists(), "P0-1: backup removes .lock.info when done")
        manifest = backup_result.get("manifest") or {}
        checker.check(manifest.get("source", {}).get("lock_held") is True, "P0-1: manifest records lock_held=True")
        checker.check(
            any("mutually exclusive" in note for note in manifest.get("notes", [])),
            "P0-1: manifest documents the backup/writer exclusion",
        )
        source_db = phase1_root / "media.db"
        source_conn = sqlite3.connect(str(source_db))
        source_counts = bs._db_counts(source_conn)
        source_conn.close()
        checker.eq(manifest["snapshot"]["counts"]["assets"], source_counts["assets"], "backup snapshot assets count == source assets count")
        checker.eq(manifest["snapshot"]["counts"]["refs"], source_counts["refs"], "backup snapshot refs count == source refs count")
        checker.eq(manifest["snapshot"]["counts"]["schema_version"], str(sv.SCHEMA_VERSION), "backup records schema_version")
        checker.check(manifest["counts_verified"], "backup snapshot verified against the source")
        checker.eq(manifest["snapshot"]["integrity_check"], "ok", "backup snapshot PRAGMA integrity_check=ok")
        snapshot_db = backup_dest / "media.db"
        checker.check(snapshot_db.is_file(), "snapshot media.db written")
        checker.eq(hashlib.sha256(snapshot_db.read_bytes()).hexdigest(), manifest["snapshot"]["sha256"], "manifest sha256 matches the snapshot file")
        copied_assets = sorted(path for path in (backup_dest / "assets").rglob("*") if path.is_file())
        checker.eq(len(manifest["files"]), 1 + len(copied_assets), "manifest lists media.db + every copied asset")
        checker.eq(len(copied_assets), count_blobs(silent_store(phase1_root)), "every source blob was copied")
        asset_entries = [entry for entry in manifest["files"] if entry["path"].startswith("assets/")]
        bad = [entry for entry in asset_entries if entry["sha256"] != Path(entry["path"]).stem.split(".")[0]]
        checker.eq(bad, [], "copied blob sha256 matches its content-addressed filename")
        checker.check(any(entry["path"] == "accounts/acct-fake/tweetxvault" for entry in manifest["archives"]), "--include-archives recorded accounts/*/tweetxvault paths")
        manifest_on_disk = json.loads((backup_dest / bs.MANIFEST_NAME).read_text(encoding="utf-8"))
        checker.eq(manifest_on_disk["format"], bs.BACKUP_FORMAT, "BACKUP_MANIFEST.json is on disk with the expected format")
        checker.check(manifest_on_disk["created_at"], "BACKUP_MANIFEST.json has a timestamp")
        # --dest inside the store root is refused (would recurse into assets/).
        refused_dest = False
        try:
            bs.create_backup(phase1_root, phase1_root / "bad-backup", log=lambda _msg: None)
        except ValueError:
            refused_dest = True
        checker.check(refused_dest, "backup refuses a --dest inside the store root")
        # Re-running into a non-empty dest is refused unless --force.
        refused_nonempty = False
        try:
            bs.create_backup(phase1_root, backup_dest, log=lambda _msg: None)
        except ValueError:
            refused_nonempty = True
        checker.check(refused_nonempty, "backup refuses a non-empty dest without --force")

        # -------------------------------------------------------------- #
        checker.phase("phase 11: no secrets leak into store artifacts")
        leaked = []
        needles = (b"auth_token", b"ct0", b"Bearer ", b"cookies")
        for path in base_dir.rglob("*"):
            if not path.is_file() or path.suffix not in (".csv", ".json", ".md", ".txt"):
                continue
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                leaked.append(path)
        checker.eq(leaked, [], "no credential-looking strings in csv/json artifacts")

        # ------------------------------------------------------------------ #
        print()
        if checker.failed:
            print(f"SELF-TEST FAILED: {len(checker.failed)} failure(s), {checker.passed} passed")
            for failure in checker.failed:
                print(f"  - {failure}")
            return 1
        print(f"SELF-TEST OK: {checker.passed} checks passed, 0 failed")
        return 0
    except Exception:  # noqa: BLE001 - report any crash as a test failure
        print("\nSELF-TEST CRASHED:")
        traceback.print_exc()
        return 1
    finally:
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        if not keep:
            force_rmtree(base_dir)
            selftest_dir = sv.DEFAULT_STORE / ".selftest"
            try:
                if selftest_dir.is_dir() and not any(selftest_dir.iterdir()):
                    selftest_dir.rmdir()
            except OSError:
                pass
        else:
            print(f"[selftest] kept temp store at {base_dir}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    keep = "--keep" in argv
    return run_selftest(keep=keep)


if __name__ == "__main__":
    raise SystemExit(main())
