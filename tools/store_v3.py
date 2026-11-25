#!/usr/bin/env python
"""store_v3.py -- tweetxvault media archive v3/v4/v5: SQLite(WAL) state DB + content-addressed blobs.

Replaces the throw-away JSONL + hardlink demo (dedup_store.py).  Layout::

    store/
      media.db                        SQLite(WAL), single source of truth
        assets(media_key, tier)       one row per (media identity, quality tier)
        refs(account, collection, tweet_id, media_key, media_index)
                                      one row per account+tweet-media+collection
        meta(key, value)              schema_version etc.
      assets/<sha[:2]>/<sha>.<ext>    immutable, read-only blobs (os.replace, atomic)
      .tmp/*.part                     in-flight downloads (all removed when the lock is taken)
      .lock                           exclusive process lock for every write path
      .lock.info                      sidecar owner hint (pid + timestamp, written while held)
      index.csv                       flat human/Excel view of refs JOIN assets
      summary.json                    aggregate counts/bytes (account/collection/type/tier)
      by-account/<acct>/<collection>/ optional projection (hardlink|copy), default off
                                      <YYYY-MM>/<author_handle>@<tweet_id>__<media_index>.<ext>

Design rules (see docs/STORE_V3.md):
  * identity is (media_key, tier); one media can coexist as bookmark/best + like/mid.
  * photo tiers: orig | large.  video/animated_gif tiers: best | mid | low,
    ordered by the bitrate of the video/mp4 entries in variants_json.
  * default policy: bookmark->best, like->mid, photo->orig, animated_gif->best.
    (``--policy low`` remains available for an explicit 256k/320p download.)
  * TIER DOMINANCE: before downloading tier T for a media_key, reuse any already
    done asset of the same media_key whose tier ranks >= T (e.g. an existing best
    satisfies a like/mid request; an existing photo orig satisfies a large
    request).  The ref records the originally requested tier in requested_tier.
    A higher tier left ``pending`` by an interrupted run / self-heal is completed
    first instead of silently serving a lower tier (P0-2).
    Only a PERMANENT HTTP failure may fall back to a lower tier (video/animated_gif
    walk their bitrate ladder best->mid->low, synthesising the lower URL from
    variants_json); a transient failure leaves the refs on the retryable asset
    (never a silent downgrade).
  * reverse upgrade: when a *higher* tier is downloaded while a lower-tier asset
    of the same media_key already exists, refs are repointed to the better asset
    and the old lower-tier row becomes state=superseded.  ``--gc`` is two-phase:
    delete unreferenced superseded rows in one transaction, then (after commit)
    rescan the surviving rows' blob references and delete only blobs with a zero
    reference count (a blob shared by a surviving mid row is kept).
  * refs identity is (account, collection, tweet_id, media_key, media_index): a
    tweet that is liked *and* bookmarked keeps two refs for the same media.  The
    archive scanner expands such dual-collection tweets into one row per
    collection, and asset_id carries a FOREIGN KEY ... ON DELETE RESTRICT.
  * every download is streamed to store/.tmp/*.part, sha256'd, fsync'd, then
    os.replace()'d into place and marked FILE_ATTRIBUTE_READONLY.  If the target
    blob path already exists: the target's sha256 is recomputed, and only an
    identical hash is reused (content addressing, 0 extra bytes); a same-size but
    different hash target is treated as corruption and atomically replaced;
    different size => clear the Windows read-only bit and os.replace() over it
    (no WinError 5).
  * state=done + blob present => skipped (idempotent, resumable across processes).
  * network/5xx/429/truncated-body errors are retried with exponential backoff
    (default 15s/45s/120s => 1 attempt + 3 retries).  HTTP 400/401/403/404/410/451
    are permanent failures and are not retried again on later runs.
  * refs carry (media_key, tweet_created_at, requested_tier) so projections and
    audits never need to re-open the LanceDB archive; the month in a projection
    path is derived from the tweet's created_at, not from scrape time.
  * the LanceDB archives are opened READ-ONLY; nothing under accounts/ is touched.
  * a consistent cold backup is produced by tools/backup_store.py (sqlite
    conn.backup(), never a raw copy of media.db under WAL).

Usage::

    python tools/store_v3.py --self-test
    python tools/store_v3.py --limit 30 --types photo --policy default --project none
    python tools/store_v3.py --accounts acct-1,acct-3 --collections bookmark --policy best
    python tools/store_v3.py --verify --quick        # read-only asset/blob consistency check
    python tools/store_v3.py --gc --dry-run          # reclaimable bytes, no writes
    python tools/store_v3.py --gc                    # delete superseded rows + orphan blobs

Every write path (download, --gc, projection) takes an exclusive store/.lock
(msvcrt.locking on Windows, fcntl.flock on POSIX); a second instance fails fast
with exit code 4 instead of racing.  After the lock is held all stale
store/.tmp/*.part files are removed.  The owner hint (pid + timestamp) lives in
the store/.lock.info sidecar, never inside the locked .lock file itself, so a
second process can always report who holds the lock.

NOTE: work/accounts.json holds live credentials.  This module only reads account
ids from it through common.load_accounts(); credentials are never printed,
logged, persisted or exported.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from common import ROOT, canonical_media_url, load_accounts, open_archive  # noqa: E402

SCHEMA_VERSION = 5
DEFAULT_STORE = ROOT / "store"
STORE_LOCK_NAME = ".lock"
# Sidecar file with the lock owner hint (pid + timestamp).  The .lock file itself
# is only the byte-range lock target: on Windows the locked byte 0 cannot be read,
# so the hint must live in a separate file (P1-2).
STORE_LOCK_INFO_NAME = ".lock.info"
# Exit code used by the CLI when another instance already holds store/.lock.
EXIT_LOCK_BUSY = 4

# Twitter/X Snowflake epoch (ms).  tweet_id >> 22 is the elapsed ms since this.
SNOWFLAKE_TWITTER_EPOCH_MS = 1288834974657

# HTTP statuses that will never succeed on a retry with the same signed URL.
PERMANENT_HTTP_STATUSES = frozenset({400, 401, 403, 404, 410, 451})
# Retry delays after attempt 1..3 for transient network errors.  Override with
# STORE_V3_RETRY_DELAYS="15,45,120" (self-test uses tiny values).
DEFAULT_RETRY_DELAYS = (15.0, 45.0, 120.0)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {"User-Agent": USER_AGENT, "Referer": "https://x.com/"}

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_NORMAL = 0x80

CTYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "application/octet-stream": ".bin",
}

TIER_TABLE = {
    "photo": ("orig", "large"),
    "video": ("best", "mid", "low"),
    "animated_gif": ("best", "mid", "low"),
}
MEDIA_TYPES = ("photo", "video", "animated_gif")
MEDIA_TYPE_ALIASES = {"gif": "animated_gif", "animated-gif": "animated_gif"}

# quality ordering inside one media_type; higher rank dominates lower rank.
TIER_RANK = {
    "photo": {"orig": 2, "large": 1},
    "video": {"best": 3, "mid": 2, "low": 1},
    "animated_gif": {"best": 3, "mid": 2, "low": 1},
}
# states an assets row can be in.
ASSET_STATES = ("pending", "done", "failed", "superseded")

# Default rules are evaluated in this order; first match wins.  (media_type,
# collection) rules are more specific than media_type rules.
DEFAULT_POLICY_RULES = [
    ("video:bookmark", "best"),
    ("photo", "orig"),
    ("animated_gif", "best"),
    ("video", "mid"),
]
POLICY_PRESETS = {
    "default": DEFAULT_POLICY_RULES,
    # kept as an explicit name for the recommended archive policy: bookmarks keep
    # the best copy, likes and unknown-collection rows use the mid bitrate
    # (832k / 480-720p) instead of 256k.  Same rules as ``default`` since v5.
    "recommended": [
        ("video:bookmark", "best"),
        ("photo", "orig"),
        ("animated_gif", "best"),
        ("video", "mid"),
    ],
    "best": [("photo", "orig"), ("animated_gif", "best"), ("video", "best")],
    "low": [("photo", "large"), ("animated_gif", "low"), ("video", "low")],
}

SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def human_bytes(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def safe_component(text: str | None, fallback: str = "unknown", limit: int = 60) -> str:
    if text is None:
        return fallback
    cleaned = SAFE_RE.sub("_", str(text)).strip("._")
    return (cleaned[:limit] or fallback)


def month_of(created_at: str | None) -> str:
    """'Sun Oct 19 23:25:09 +0000 2025' -> '2025-10'; tolerant fallbacks."""
    if not created_at:
        return "unknown"
    raw = str(created_at).strip()
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").strftime("%Y-%m")
    except ValueError:
        pass
    m = re.match(r"^(\d{4})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return "unknown"


def snowflake_month(tweet_id: str | None) -> str | None:
    """UTC year-month derived from an X Snowflake tweet id, or None.

    ms = (int(tweet_id) >> 22) + 1288834974657; the value is a Unix timestamp in ms
    at which the tweet was created.  Used only by projections when a ref has no
    tweet_created_at (legacy rows / tweet rows missing created_at).
    """
    if tweet_id is None:
        return None
    raw = str(tweet_id).strip()
    if not raw.isdigit():
        return None
    try:
        timestamp_ms = (int(raw) >> 22) + SNOWFLAKE_TWITTER_EPOCH_MS
        moment = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if not (2006 <= moment.year <= 2100):  # sanity: X launched in 2006
        return None
    return moment.strftime("%Y-%m")


def projection_month(created_at: str | None, tweet_id: str | None) -> str:
    """Month directory for a projection path.

    Prefer refs.tweet_created_at; when it is missing (or unparseable) fall back to
    the Snowflake timestamp decoded from tweet_id; the last resort is
    'unknown-date' (NOT bare 'unknown', so such files stay distinguishable).
    """
    month = month_of(created_at)
    if month != "unknown":
        return month
    return snowflake_month(tweet_id) or "unknown-date"


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    """Streaming sha256 of one file (used by --verify)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ext_for(url: str | None, content_type: str | None) -> str:
    if content_type:
        base = content_type.split(";")[0].strip().lower()
        if base in CTYPE_EXT:
            return CTYPE_EXT[base]
    if url:
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix and len(suffix) <= 6 and re.fullmatch(r"\.[a-z0-9]+", suffix):
            return suffix
    return ".bin"


def photo_variant_url(url: str | None, name: str) -> str | None:
    """pbs.twimg.com URL with name=orig|large (format param preserved)."""
    if not url:
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["name"] = name
    return urlunsplit((parts.scheme or "https", parts.netloc, parts.path, urlencode(query), ""))


def parse_variants(variants_json: str | None, media_url: str | None) -> list[tuple[float, str]]:
    """Return [(bitrate, url)] for video/mp4 variants, highest bitrate first."""
    variants: list[dict] = []
    if variants_json:
        try:
            loaded = json.loads(variants_json)
            if isinstance(loaded, list):
                variants = [v for v in loaded if isinstance(v, dict)]
        except (TypeError, ValueError):
            variants = []
    out: list[tuple[float, str]] = []
    seen: set[str] = set()
    for variant in variants:
        ctype = str(variant.get("content_type") or "").split(";")[0].strip().lower()
        url = variant.get("url")
        if ctype != "video/mp4" or not url or url in seen:
            continue
        try:
            bitrate = float(variant.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0.0
        seen.add(url)
        out.append((bitrate, url))
    out.sort(key=lambda item: -item[0])
    if not out and media_url and urlsplit(media_url).path.lower().endswith((".mp4", ".mov")):
        out = [(0.0, media_url)]
    return out


def pick_variant(variants: list[tuple[float, str]], tier: str) -> str | None:
    if not variants:
        return None
    if tier == "best":
        return variants[0][1]
    if tier == "low":
        return variants[-1][1]
    if tier == "mid":
        return variants[len(variants) // 2][1]
    return None


# --------------------------------------------------------------------------- #
# Windows read-only attribute
# --------------------------------------------------------------------------- #
def set_readonly(path: Path) -> bool:
    """Set FILE_ATTRIBUTE_READONLY (ctypes on Windows, chmod elsewhere)."""
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            attrs = kernel32.GetFileAttributesW(str(path))
            if attrs == -1:
                return False
            return bool(kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_READONLY))
        os.chmod(path, 0o444)
        return True
    except (OSError, AttributeError):
        return False


def clear_readonly(path: Path) -> None:
    """Best-effort clear of FILE_ATTRIBUTE_READONLY *and* the chmod write bits."""
    try:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            attrs = kernel32.GetFileAttributesW(str(path))
            if attrs != -1 and attrs & FILE_ATTRIBUTE_READONLY:
                kernel32.SetFileAttributesW(str(path), (attrs & ~FILE_ATTRIBUTE_READONLY) or FILE_ATTRIBUTE_NORMAL)
        if path.exists():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except (OSError, AttributeError):
        pass


def is_readonly(path: Path) -> bool:
    if os.name == "nt":
        attrs = getattr(path.stat(), "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_READONLY)
    return not bool(path.stat().st_mode & 0o222)


def force_replace(src: Path, dst: Path) -> bool:
    """os.replace(src, dst), clearing a read-only dst first (Windows WinError 5).

    Returns True when dst existed beforehand (i.e. this was an overwrite).
    """
    existed = dst.exists()
    if existed:
        clear_readonly(dst)
    try:
        os.replace(src, dst)
        return existed
    except PermissionError:
        # A stale read-only flag or a scanner holding the file: clear and retry once.
        if dst.exists():
            clear_readonly(dst)
        time.sleep(0.05)
        os.replace(src, dst)
        return existed


def unlink_force(path: Path) -> bool:
    """Unlink a (possibly read-only) file; True when a file was actually removed."""
    try:
        clear_readonly(path)
    except OSError:
        pass
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except PermissionError:
        try:
            clear_readonly(path)
            path.unlink()
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
def _normalize_media_type(media_type: str | None) -> str:
    mt = (media_type or "").strip().lower()
    return MEDIA_TYPE_ALIASES.get(mt, mt)


def tier_rank(media_type: str | None, tier: str | None) -> int:
    """Quality rank of ``tier`` inside ``media_type``; 0 = unknown/invalid."""
    table = TIER_RANK.get(_normalize_media_type(media_type) or "", {})
    return int(table.get((tier or "").strip().lower(), 0))


def tier_dominates(media_type: str | None, candidate_tier: str | None, requested_tier: str | None) -> bool:
    """True when a done asset at candidate_tier can serve a requested_tier request."""
    candidate_rank = tier_rank(media_type, candidate_tier)
    requested_rank = tier_rank(media_type, requested_tier)
    return bool(candidate_rank and requested_rank and candidate_rank >= requested_rank)


def _scope_matcher(scope: str):
    scope = scope.strip().lower()
    if scope in ("*", "all", ""):
        return lambda mt, col: True
    if ":" in scope:
        mt_part, col_part = scope.split(":", 1)
        mt = _normalize_media_type(mt_part)
        col = col_part.strip().lower()
        return lambda media_type, collection: media_type == mt and (
            col == "*" or (collection or "").lower() == col
        )
    mt = _normalize_media_type(scope)
    if mt in MEDIA_TYPES:
        return lambda media_type, collection: media_type == mt
    # anything else is treated as a collection name (e.g. "bookmark=best")
    return lambda media_type, collection: (collection or "").lower() == scope


class Policy:
    """Ordered (matcher, tier) rules; the first matching rule wins."""

    def __init__(self, rules: list[tuple[str, str]]):
        self.rules = [(_scope_matcher(scope), tier) for scope, tier in rules]
        self.spec = ", ".join(f"{scope}={tier}" for scope, tier in rules)

    def pick(self, media_type: str, collection: str | None) -> str | None:
        for matcher, tier in self.rules:
            if matcher(media_type, collection):
                return tier
        return None


def make_policy(spec: str | None) -> Policy:
    spec = (spec or "default").strip()
    preset = POLICY_PRESETS.get(spec.lower())
    if preset is not None:
        return Policy(list(preset))
    user_rules: list[tuple[str, str]] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise ValueError(f"invalid --policy token {token!r}; expected scope=tier")
        scope, tier = token.split("=", 1)
        user_rules.append((scope.strip(), tier.strip().lower()))
    if not user_rules:
        raise ValueError("empty --policy")
    return Policy(user_rules + list(DEFAULT_POLICY_RULES))


# --------------------------------------------------------------------------- #
# data records
# --------------------------------------------------------------------------- #
@dataclass
class MediaRow:
    """One media row of one account's LanceDB archive, joined with its tweet."""

    account: str
    collection: str
    tweet_id: str
    media_key: str
    media_type: str
    position: int
    author_handle: str | None
    tweet_created_at: str | None
    media_url: str | None
    variants_json: str | None


@dataclass
class AssetPlan:
    media_key: str
    tier: str
    media_type: str
    url: str
    rows: list[MediaRow] = field(default_factory=list)

    def account_label(self) -> str:
        return self.rows[0].account if self.rows else "?"


@dataclass
class RunStats:
    selected_assets: int = 0
    selected_refs: int = 0
    new_refs: int = 0
    unresolved: int = 0
    would_download: int = 0
    already_done: int = 0
    skipped_permanent: int = 0
    downloaded: int = 0
    bytes_downloaded: int = 0
    content_reused: int = 0
    readonly_overwrites: int = 0
    dominance_reused: int = 0
    dominance_merged: int = 0
    upgraded: int = 0
    refs_repointed: int = 0
    refs_unresolved: int = 0
    refs_downgraded: int = 0
    failed_permanent: int = 0
    failed_transient: int = 0
    retry_events: int = 0
    projected: int = 0
    projection_fallbacks: int = 0
    projection_errors: int = 0
    budget_stopped: bool = False
    dry_run: bool = False
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# --------------------------------------------------------------------------- #
# exceptions
# --------------------------------------------------------------------------- #
class DownloadError(Exception):
    def __init__(self, message: str, *, status: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


class StoreLockError(RuntimeError):
    """Another process/instance already holds ``store/.lock``."""


class StoreLock:
    """Cross-process exclusive lock on ``store/.lock`` (Windows + POSIX).

    Windows: ``msvcrt.locking(fd, LK_NBLCK, 1)`` on byte 0 (the file always has at
    least one byte).  POSIX: ``fcntl.flock(fd, LOCK_EX | LOCK_NB)``.  Both are
    non-blocking: a busy lock raises :class:`StoreLockError` immediately instead of
    waiting.  The OS releases the lock when the owning process dies.

    The owner hint (pid + timestamp) is written atomically to the sidecar
    ``store/.lock.info`` while the lock is held and removed on release.  It is NOT
    written into ``.lock`` itself: on Windows a process that did not acquire the
    byte lock cannot read the locked byte range, so the hint would be invisible
    exactly when it is needed (P1-2).
    """

    def __init__(self, path: Path, info_path: Path | None = None):
        self.path = Path(path)
        self.info_path = (
            Path(info_path)
            if info_path is not None
            else self.path.with_name(STORE_LOCK_INFO_NAME)
        )
        self.handle = None

    @property
    def acquired(self) -> bool:
        return self.handle is not None

    def _owner_hint(self) -> str:
        """Best-effort ``held by: pid=...`` suffix from the sidecar info file."""
        try:
            with open(self.info_path, "r", encoding="utf-8", errors="replace") as handle:
                raw = handle.read(200).strip()
        except OSError:
            return ""
        return f" (held by: {raw})" if raw else ""

    def _write_owner_info(self) -> None:
        """Atomically record pid + timestamp in the sidecar file (never fatal)."""
        payload = f"pid={os.getpid()} {now_iso()}\n".encode("utf-8", "replace")
        tmp_path = self.info_path.with_name(self.info_path.name + f".{os.getpid()}.tmp")
        try:
            self.info_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.info_path)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def acquire(self) -> "StoreLock":
        if self.handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise StoreLockError(
                        f"store lock busy: another store_v3 instance is already running "
                        f"({self.path}){self._owner_hint()}"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise StoreLockError(
                        f"store lock busy: another store_v3 instance is already running "
                        f"({self.path}){self._owner_hint()}"
                    ) from exc
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        self._write_owner_info()
        return self

    def release(self) -> None:
        handle = self.handle
        if handle is None:
            return
        # Remove the sidecar hint BEFORE releasing the byte lock: once the lock is
        # free another instance may write its own hint, and this process must never
        # delete that one.
        try:
            self.info_path.unlink()
        except OSError:
            pass
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                handle.close()
            finally:
                self.handle = None

    def __enter__(self) -> "StoreLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


# --------------------------------------------------------------------------- #
# discovery (read-only LanceDB)
# --------------------------------------------------------------------------- #
def resolve_accounts(spec: str | None) -> list[str]:
    """--accounts acct-1,acct-2 or default: ids from accounts.json that have an archive."""
    ids: list[str] = []
    if spec:
        ids = [part.strip() for part in spec.split(",") if part.strip()]
    else:
        try:
            # Only the id field is used; credentials from accounts.json are never
            # read, printed or persisted.
            ids = [str(acct.get("id")) for acct in load_accounts() if acct.get("id")]
        except (OSError, ValueError, TypeError):
            ids = []
        if not ids:
            accounts_dir = ROOT / "accounts"
            if accounts_dir.is_dir():
                ids = sorted(
                    path.name
                    for path in accounts_dir.iterdir()
                    if path.is_dir() and (path / "tweetxvault" / "archive.lancedb").exists()
                )
    return [aid for aid in ids if (ROOT / "accounts" / aid / "tweetxvault" / "archive.lancedb").exists()]


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [part.strip() for part in value.split(",") if part.strip()]
    return items or None


def rows_from_records(
    account: str,
    media_records: list[dict],
    tweet_records: list[dict],
    object_records: list[dict] = (),
    collections: list[str] | None = None,
    media_types: list[str] | None = None,
) -> list[MediaRow]:
    """Turn raw archive rows into MediaRow candidates (pure, no LanceDB).

    One media row can belong to several tweet records with different
    ``collection_type`` values (a tweet that was liked *and* bookmarked appears
    twice in the archive).  Every (tweet, media, collection) combination becomes
    its own MediaRow, otherwise the refs table could never keep both the like and
    the bookmark reference for the same media (P0-2).  Tweets without a record fall
    back to collection ``unknown``.
    """
    allowed_collections = {c.lower() for c in collections} if collections else None
    allowed_types = {_normalize_media_type(t) for t in media_types} if media_types else None

    # tweet_id -> {collections, author, created}
    tweet_info: dict[str, dict] = {}
    for tweet in tweet_records:
        tid = str(tweet.get("tweet_id") or "")
        if not tid:
            continue
        info = tweet_info.setdefault(tid, {"collections": set(), "author": None, "created": None})
        collection = str(tweet.get("collection_type") or "").strip().lower()
        if collection:
            info["collections"].add(collection)
        info["author"] = info["author"] or tweet.get("author_username")
        info["created"] = info["created"] or tweet.get("created_at")
    objects: dict[str, tuple[str | None, str | None]] = {}
    for obj in object_records:
        tid = str(obj.get("tweet_id") or "")
        if tid:
            objects[tid] = (obj.get("author_username"), obj.get("created_at"))

    rows: list[MediaRow] = []
    seen: set[tuple] = set()
    for media in media_records:
        media_type = _normalize_media_type(media.get("media_type"))
        if media_type not in MEDIA_TYPES:
            continue
        if allowed_types and media_type not in allowed_types:
            continue
        tweet_id = str(media.get("tweet_id") or "")
        media_key = str(media.get("media_key") or "")
        if not tweet_id or not media_key:
            continue
        info = tweet_info.get(tweet_id)
        collections_for_row = sorted(info["collections"]) if info and info["collections"] else ["unknown"]
        author = (info or {}).get("author")
        created = (info or {}).get("created")
        if tweet_id in objects:
            author = author or objects[tweet_id][0]
            created = created or objects[tweet_id][1]
        position = media.get("position")
        try:
            media_index = int(position)
        except (TypeError, ValueError):
            media_index = 0
        for collection in collections_for_row:
            collection = collection or "unknown"
            if allowed_collections and collection not in allowed_collections:
                continue
            identity = (account, collection, tweet_id, media_key, media_index)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(
                MediaRow(
                    account=account,
                    collection=collection,
                    tweet_id=tweet_id,
                    media_key=media_key,
                    media_type=media_type,
                    position=media_index,
                    author_handle=author,
                    tweet_created_at=created,
                    media_url=media.get("media_url"),
                    variants_json=media.get("variants_json"),
                )
            )
    return rows


def discover_rows(
    account_ids: list[str],
    collections: list[str] | None = None,
    media_types: list[str] | None = None,
    *,
    log=print,
) -> list[MediaRow]:
    """Load media rows (joined with tweet info) from every account archive, read-only."""
    rows: list[MediaRow] = []
    for account in account_ids:
        table = open_archive(account)
        if table is None:
            log(f"[warn] {account}: no LanceDB archive, skipped")
            continue
        media_rows = table.search().where("record_type = 'media'").limit(500_000).to_list()
        tweet_rows = table.search().where("record_type = 'tweet'").limit(500_000).to_list()
        object_rows = table.search().where("record_type = 'tweet_object'").limit(500_000).to_list()
        account_rows = rows_from_records(
            account, media_rows, tweet_rows, object_rows, collections, media_types
        )
        collections_seen = sorted({row.collection for row in account_rows})
        log(
            f"[scan] {account}: {len(media_rows)} media rows -> {len(account_rows)} candidates "
            f"(collections: {','.join(collections_seen) or '-'})"
        )
        rows.extend(account_rows)
    return rows


# --------------------------------------------------------------------------- #
# tier resolution
# --------------------------------------------------------------------------- #
def resolve_tier(row: MediaRow, policy: Policy) -> tuple[str, str]:
    """Map one media row to (tier, download_url).  Raises ValueError when impossible."""
    media_type = row.media_type
    valid_tiers = TIER_TABLE.get(media_type)
    if not valid_tiers:
        raise ValueError(f"unsupported media_type {media_type!r}")
    tier = policy.pick(media_type, row.collection)
    if tier is None:
        raise ValueError(f"policy has no rule for {media_type}/{row.collection}")
    if tier not in valid_tiers:
        raise ValueError(f"policy tier {tier!r} invalid for {media_type} (allowed: {valid_tiers})")
    if media_type == "photo":
        url = photo_variant_url(row.media_url, "orig" if tier == "orig" else "large")
        if not url:
            raise ValueError("photo without media_url")
        return tier, url
    variants = parse_variants(row.variants_json, row.media_url)
    url = pick_variant(variants, tier)
    if not url:
        raise ValueError(f"{media_type} without usable mp4 variant")
    return tier, canonical_media_url(media_type, url) or url


def build_plans(
    rows: list[MediaRow], policy: Policy
) -> tuple[list[AssetPlan], list[tuple[MediaRow, str]]]:
    """Group rows by (media_key, tier) into download plans, in deterministic order."""
    plans: dict[tuple[str, str], AssetPlan] = {}
    unresolved: list[tuple[MediaRow, str]] = []
    for row in rows:
        try:
            tier, url = resolve_tier(row, policy)
        except ValueError as exc:
            unresolved.append((row, str(exc)))
            continue
        key = (row.media_key, tier)
        plan = plans.get(key)
        if plan is None:
            plans[key] = AssetPlan(
                media_key=row.media_key, tier=tier, media_type=row.media_type, url=url, rows=[row]
            )
        else:
            plan.rows.append(row)
    ordered = sorted(plans.values(), key=lambda plan: (plan.media_key, plan.tier))
    return ordered, unresolved


# --------------------------------------------------------------------------- #
# SQLite schema
# --------------------------------------------------------------------------- #
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    media_key    TEXT NOT NULL,
    media_type   TEXT,
    tier         TEXT NOT NULL,
    url          TEXT,
    sha256       TEXT,
    size         INTEGER,
    content_type TEXT,
    blob_path    TEXT,
    state        TEXT NOT NULL DEFAULT 'pending',
    http_status  INTEGER,
    retry_count  INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(media_key, tier)
);

CREATE TABLE IF NOT EXISTS refs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id         INTEGER NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
    account          TEXT NOT NULL,
    collection       TEXT NOT NULL,
    tweet_id         TEXT NOT NULL,
    media_key        TEXT NOT NULL,
    media_index      INTEGER NOT NULL,
    media_type       TEXT,
    author_handle    TEXT,
    tweet_created_at TEXT,
    requested_tier   TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(account, collection, tweet_id, media_key, media_index)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Indexes are created after the column migration of pre-v4 databases (refs did not
# always have media_key/tweet_created_at/requested_tier), so keep them separate.
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_refs_asset ON refs(asset_id);
CREATE INDEX IF NOT EXISTS idx_refs_media_key ON refs(media_key);
CREATE INDEX IF NOT EXISTS idx_refs_account_created ON refs(account, tweet_created_at);
CREATE INDEX IF NOT EXISTS idx_assets_state ON assets(state);
"""

INDEX_COLUMNS = [
    "account",
    "collection",
    "media_type",
    "tier",
    "requested_tier",
    "tweet_id",
    "media_index",
    "author_handle",
    "media_key",
    "tweet_created_at",
    "state",
    "sha256",
    "size",
    "content_type",
    "blob_path",
    "projected_path",
    "url",
]


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class Store:
    """The v3 store rooted at ``root`` (default <repo>/store)."""

    def __init__(
        self,
        root: Path | str = DEFAULT_STORE,
        *,
        retry_delays: tuple[float, ...] | list[float] = DEFAULT_RETRY_DELAYS,
        max_attempts: int = 4,
        client_factory=None,
        link_fn=os.link,
        log=print,
    ):
        self.root = Path(root)
        self.db_path = self.root / "media.db"
        self.assets_dir = self.root / "assets"
        self.tmp_dir = self.root / ".tmp"
        self.projection_dir = self.root / "by-account"
        self.lock_path = self.root / STORE_LOCK_NAME
        self.lock_info_path = self.root / STORE_LOCK_INFO_NAME
        self.retry_delays = tuple(float(d) for d in retry_delays) or (0.0,)
        self.max_attempts = max(1, int(max_attempts))
        self.client_factory = client_factory or self._default_client
        self.link_fn = link_fn
        self.log = log
        self.conn: sqlite3.Connection | None = None
        self._lock = StoreLock(self.lock_path, self.lock_info_path)
        self.last_stats = RunStats()
        self.last_policy: Policy | None = None

    # -- lifecycle --------------------------------------------------------- #
    @staticmethod
    def _default_client():
        import httpx

        return httpx.Client(
            headers=HTTP_HEADERS,
            timeout=httpx.Timeout(90.0, connect=30.0),
            follow_redirects=True,
        )

    @property
    def lock_held(self) -> bool:
        return self._lock.acquired

    def open(self) -> "Store":
        """Open the store for writing: acquire store/.lock, clean .part, migrate.

        Raises :class:`StoreLockError` immediately when another process/instance
        already holds the lock (the CLI turns that into exit code 4).  A Store is
        a single-writer object: every download / --gc / projection path goes
        through an opened Store, so the lock covers all of them.
        """
        if self.conn is not None:
            return self
        for path in (self.root, self.assets_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)
        self._lock.acquire()
        try:
            cleaned = self._cleanup_parts()
            self.log(
                f"[lock] held {self.lock_path} (pid={os.getpid()}); "
                f"cleaned {cleaned} stale .part file(s) under store/.tmp"
            )
            self.conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA_SQL)
            self._migrate_schema()
            self.conn.executescript(INDEX_SQL)
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()
        except BaseException:
            if self.conn is not None:
                try:
                    self.conn.close()
                finally:
                    self.conn = None
            self._lock.release()
            raise
        return self

    def _table_columns(self, table: str) -> set[str]:
        assert self.conn is not None
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _refs_schema_needs_rebuild(self) -> bool:
        """True when refs still has the pre-v5 shape (collection nullable / old UNIQUE)."""
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='refs'"
        ).fetchone()
        if row is None or not row[0]:
            return False
        sql = re.sub(r"\s+", " ", row[0]).lower()
        has_new_unique = "unique(account, collection, tweet_id, media_key, media_index)" in sql
        has_fk = "references assets" in sql
        has_not_null_collection = bool(re.search(r"collection\s+text\s+not\s+null", sql))
        return not (has_new_unique and has_fk and has_not_null_collection)

    def _rebuild_refs_table(self) -> None:
        """SQLite cannot ALTER a constraint: rebuild refs inside one transaction.

        v4 -> v5 changes:
          * collection TEXT NOT NULL (old NULLs become 'unknown');
          * UNIQUE(account, collection, tweet_id, media_key, media_index) so one
            tweet/media can keep a like *and* a bookmark ref;
          * asset_id REFERENCES assets(id) ON DELETE RESTRICT.
        The migration is idempotent: :meth:`_refs_schema_needs_rebuild` skips it on
        an already-v5 database.  Orphan refs (asset_id with no assets row) abort
        the migration loudly instead of being silently dropped.
        """
        assert self.conn is not None
        orphans = self.conn.execute(
            "SELECT COUNT(*) FROM refs r LEFT JOIN assets a ON a.id = r.asset_id "
            "WHERE a.id IS NULL"
        ).fetchone()[0]
        if orphans:
            raise RuntimeError(
                f"refs has {orphans} orphan row(s) (asset_id without an assets row); "
                "restore the pre-migration backup and repair before migrating"
            )
        self.conn.execute(
            """
            CREATE TABLE refs_new (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id         INTEGER NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
                account          TEXT NOT NULL,
                collection       TEXT NOT NULL,
                tweet_id         TEXT NOT NULL,
                media_key        TEXT NOT NULL,
                media_index      INTEGER NOT NULL,
                media_type       TEXT,
                author_handle    TEXT,
                tweet_created_at TEXT,
                requested_tier   TEXT,
                created_at       TEXT NOT NULL,
                UNIQUE(account, collection, tweet_id, media_key, media_index)
            )
            """
        )
        self.conn.execute(
            """
            INSERT INTO refs_new(
                id, asset_id, account, collection, tweet_id, media_key, media_index,
                media_type, author_handle, tweet_created_at, requested_tier, created_at
            )
            SELECT r.id, r.asset_id, r.account, COALESCE(r.collection, 'unknown'),
                   r.tweet_id, r.media_key, r.media_index, r.media_type,
                   r.author_handle, r.tweet_created_at,
                   COALESCE(r.requested_tier, (SELECT tier FROM assets WHERE assets.id = r.asset_id)),
                   r.created_at
              FROM refs r
            """
        )
        self.conn.execute("DROP TABLE refs")
        self.conn.execute("ALTER TABLE refs_new RENAME TO refs")

    def _migrate_schema(self) -> None:
        """Idempotent in-place migration of pre-v4/v5 stores (safe for the live media.db).

        v3 -> v4:
          * refs.requested_tier TEXT added; backfilled with the asset tier the ref
            currently points at (that *was* the originally requested tier).
          * refs.media_key / refs.tweet_created_at TEXT added for very old stores;
            media_key is backfilled from assets.  tweet_created_at cannot be
            reconstructed from the store, so it is backfilled lazily the next time
            the owning tweet is scanned (register()).
        v4 -> v5:
          * refs table rebuilt (collection NOT NULL, UNIQUE + collection, FK to
            assets) because SQLite cannot alter a UNIQUE constraint in place.
        The whole migration runs in one explicit transaction; indexes are created
        by open() after it.
        """
        assert self.conn is not None
        ref_cols = self._table_columns("refs")
        added: list[str] = []
        rebuilt = False
        self.conn.commit()  # close any implicit transaction opened by connects/PRAGMAs
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if "media_key" not in ref_cols:
                self.conn.execute("ALTER TABLE refs ADD COLUMN media_key TEXT")
                self.conn.execute(
                    "UPDATE refs SET media_key=(SELECT media_key FROM assets WHERE assets.id=refs.asset_id) "
                    "WHERE media_key IS NULL"
                )
                added.append("refs.media_key")
            if "tweet_created_at" not in ref_cols:
                self.conn.execute("ALTER TABLE refs ADD COLUMN tweet_created_at TEXT")
                added.append("refs.tweet_created_at")
            if "requested_tier" not in ref_cols:
                self.conn.execute("ALTER TABLE refs ADD COLUMN requested_tier TEXT")
                added.append("refs.requested_tier")
            # Backfill helpers are cheap and keep older DBs self-describing.
            self.conn.execute(
                "UPDATE refs SET media_key=(SELECT media_key FROM assets WHERE assets.id=refs.asset_id) "
                "WHERE media_key IS NULL"
            )
            self.conn.execute(
                "UPDATE refs SET requested_tier=(SELECT tier FROM assets WHERE assets.id=refs.asset_id) "
                "WHERE requested_tier IS NULL"
            )
            if self._refs_schema_needs_rebuild():
                self._rebuild_refs_table()
                rebuilt = True
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        if added:
            self.log(f"[migrate] schema v{SCHEMA_VERSION}: added {', '.join(added)}")
        if rebuilt:
            self.log(
                "[migrate] schema v5: rebuilt refs "
                "(collection NOT NULL, UNIQUE(account, collection, tweet_id, media_key, media_index), "
                "FOREIGN KEY asset_id ON DELETE RESTRICT)"
            )

    def close(self) -> None:
        try:
            if self.conn is not None:
                try:
                    self.conn.commit()
                finally:
                    self.conn.close()
                    self.conn = None
        finally:
            self._lock.release()

    def __enter__(self) -> "Store":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def _cleanup_parts(self) -> int:
        """Delete *all* .part files under store/.tmp; returns how many were removed.

        Only called after the exclusive store lock is held, so every .part file
        belongs to a crashed previous run and is safe to remove (no 6h heuristic).
        """
        removed = 0
        try:
            for part in self.tmp_dir.glob("*.part"):
                try:
                    part.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        except OSError:
            pass
        return removed

    # -- paths ------------------------------------------------------------- #
    def blob_abs(self, blob_path: str | None) -> Path | None:
        if not blob_path:
            return None
        candidate = Path(blob_path)
        if candidate.is_absolute():
            return candidate
        return self.root / candidate

    def projection_rel(self, ref: sqlite3.Row | dict, ext: str) -> Path:
        """Readable projection path; includes the ref's collection (P0-3).

        V5 keeps a like *and* a bookmark ref for the same (account, tweet, media,
        index).  Without the collection dimension both refs would map to one path
        and ``_project_one`` would silently overwrite one with the other, so the
        path is ``by-account/<acct>/<collection>/<YYYY-MM>/<name>``.  Collections
        outside {like, bookmark} collapse to ``unknown`` (same fallback as refs).
        """
        month = projection_month(ref["tweet_created_at"], ref["tweet_id"])
        collection = str(ref["collection"] or "unknown").strip().lower()
        if collection not in ("like", "bookmark"):
            collection = "unknown"
        name = (
            f"{safe_component(ref['author_handle'], 'unknown')}"
            f"@{safe_component(ref['tweet_id'], 'na')}"
            f"__{int(ref['media_index'] or 0)}{ext}"
        )
        return (
            Path("by-account")
            / safe_component(ref["account"], "unknown")
            / collection
            / month
            / name
        )

    # -- asset lookup / dominance ----------------------------------------- #
    def _asset_row(self, media_key: str, tier: str) -> sqlite3.Row | None:
        assert self.conn is not None
        return self.conn.execute(
            "SELECT id, media_key, media_type, tier, url, sha256, size, blob_path, state, "
            "       http_status, retry_count FROM assets WHERE media_key=? AND tier=?",
            (media_key, tier),
        ).fetchone()

    def _healthy_done(self, asset: sqlite3.Row) -> bool:
        if asset["state"] != "done":
            return False
        blob = self.blob_abs(asset["blob_path"])
        if not blob or not blob.is_file():
            return False
        if asset["size"] is not None and blob.stat().st_size != int(asset["size"]):
            return False
        return True

    def _reset_to_pending(self, asset_id: int) -> None:
        assert self.conn is not None
        with self.conn:
            self.conn.execute(
                "UPDATE assets SET state='pending', updated_at=? WHERE id=?",
                (now_iso(), asset_id),
            )

    def _done_assets(self, media_key: str) -> list[sqlite3.Row]:
        """All done assets of a media_key whose blob is healthy; unhealthy ones reset."""
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT id, media_key, media_type, tier, url, sha256, size, blob_path, state, "
            "       http_status, retry_count FROM assets WHERE media_key=? AND state='done'",
            (media_key,),
        ).fetchall()
        healthy: list[sqlite3.Row] = []
        for row in rows:
            if self._healthy_done(row):
                healthy.append(row)
            else:  # vanished blob / stale size: self-heal on the next download pass
                self._reset_to_pending(row["id"])
        return healthy

    def _pick_dominant_done(
        self, media_key: str, media_type: str | None, requested_tier: str
    ) -> sqlite3.Row | None:
        """Highest-quality healthy done asset of media_key that dominates requested_tier."""
        best: sqlite3.Row | None = None
        best_rank = 0
        for asset in self._done_assets(media_key):
            rank = tier_rank(asset["media_type"] or media_type, asset["tier"])
            if rank <= 0:
                continue
            if not tier_dominates(asset["media_type"] or media_type, asset["tier"], requested_tier):
                continue
            if rank > best_rank:
                best, best_rank = asset, rank
        return best

    def _group_plans(self, plans: list[AssetPlan]) -> dict[str, list[AssetPlan]]:
        """Group plans by media_key; inside a group higher tiers come first."""
        groups: dict[str, list[AssetPlan]] = {}
        for plan in plans:
            groups.setdefault(plan.media_key, []).append(plan)
        for group in groups.values():
            group.sort(key=lambda p: (-tier_rank(p.media_type, p.tier), p.tier))
        return groups

    def _ensure_asset_row(self, plan: AssetPlan, ts: str) -> sqlite3.Row:
        assert self.conn is not None
        with self.conn:
            self.conn.execute(
                "INSERT INTO assets(media_key, media_type, tier, url, state, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT(media_key, tier) DO UPDATE SET "
                "  media_type=COALESCE(excluded.media_type, assets.media_type), "
                "  url=excluded.url, updated_at=excluded.updated_at",
                (plan.media_key, plan.media_type, plan.tier, plan.url, ts, ts),
            )
        row = self._asset_row(plan.media_key, plan.tier)
        assert row is not None
        return row

    def _insert_plan_refs(self, plan: AssetPlan, asset_id: int, ts: str, stats: RunStats) -> None:
        """Insert the refs of one plan, pointing at the serving asset.

        The conflict target is the v5 identity (account, collection, tweet_id,
        media_key, media_index): one tweet/media can legitimately have BOTH a like
        and a bookmark ref.  Existing rows are kept (DO NOTHING) but their
        requested_tier / tweet_created_at are lazily backfilled when they were
        NULL after migration.
        """
        assert self.conn is not None
        for row in plan.rows:
            collection = row.collection or "unknown"
            cursor = self.conn.execute(
                "INSERT INTO refs("
                " asset_id, account, collection, tweet_id, media_key, media_index,"
                " media_type, author_handle, tweet_created_at, requested_tier, created_at"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(account, collection, tweet_id, media_key, media_index) DO NOTHING",
                (
                    asset_id,
                    row.account,
                    collection,
                    row.tweet_id,
                    row.media_key,
                    row.position,
                    row.media_type,
                    row.author_handle,
                    row.tweet_created_at,
                    plan.tier,
                    ts,
                ),
            )
            stats.new_refs += cursor.rowcount if cursor.rowcount > 0 else 0
            self.conn.execute(
                "UPDATE refs SET tweet_created_at=COALESCE(tweet_created_at, ?), "
                "                requested_tier=COALESCE(requested_tier, ?) "
                " WHERE account=? AND collection=? AND tweet_id=? AND media_key=? AND media_index=?",
                (
                    row.tweet_created_at,
                    plan.tier,
                    row.account,
                    collection,
                    row.tweet_id,
                    row.media_key,
                    row.position,
                ),
            )

    def _point_refs(self, plan: AssetPlan, asset_id: int, stats: RunStats) -> None:
        """Repoint one plan's refs at the serving asset (idempotent).

        The WHERE clause includes collection: repointing the bookmark ref of a
        tweet must never move the like ref of the same tweet/media_key.
        """
        assert self.conn is not None
        for row in plan.rows:
            cursor = self.conn.execute(
                "UPDATE refs SET asset_id=? WHERE account=? AND collection=? AND tweet_id=? "
                "AND media_key=? AND media_index=? AND asset_id<>?",
                (
                    asset_id,
                    row.account,
                    row.collection or "unknown",
                    row.tweet_id,
                    row.media_key,
                    row.position,
                    asset_id,
                ),
            )
            stats.refs_repointed += max(0, cursor.rowcount)

    # -- registration ------------------------------------------------------ #
    def register(self, plans: list[AssetPlan], stats: RunStats) -> None:
        """Create/lookup the serving asset row per media_key and write every ref.

        Tier dominance is applied here: if a healthy done asset of the same
        media_key already ranks >= the plan's tier, no new asset row is created
        and the refs point at the dominating asset.  Otherwise only the highest
        requested tier of the group is registered as pending; lower requested
        tiers wait for the download pass (they may be served by it or by a
        fallback tier).  This keeps budgets/resume semantics unchanged: every
        ref exists with an asset row even if the download is deferred.
        """
        assert self.conn is not None
        ts = now_iso()
        with self.conn:
            for media_key, group in self._group_plans(plans).items():
                top = group[0]
                serving = self._pick_dominant_done(media_key, top.media_type, top.tier)
                if serving is None:
                    serving = self._ensure_asset_row(top, ts)
                for plan in group:
                    self._insert_plan_refs(plan, serving["id"], ts, stats)

    # -- download ---------------------------------------------------------- #
    def _download_once(self, client, url: str, part: Path) -> tuple[str, int, str | None]:
        digest = hashlib.sha256()
        size = 0
        content_type = None
        with client.stream("GET", url) as response:
            status = response.status_code
            if status != 200:
                raise DownloadError(
                    f"HTTP {status}",
                    status=status,
                    permanent=status in PERMANENT_HTTP_STATUSES,
                )
            content_type = response.headers.get("content-type")
            expected: int | None = None
            raw_length = response.headers.get("content-length")
            if raw_length and raw_length.isdigit():
                expected = int(raw_length)
            with open(part, "wb") as handle:
                for chunk in response.iter_bytes(1 << 16):
                    digest.update(chunk)
                    handle.write(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if expected is not None and size != expected:
            raise DownloadError(f"truncated body: {size}/{expected} bytes", status=200)
        if size == 0:
            raise DownloadError("empty body", status=200)
        return digest.hexdigest(), size, content_type

    def _mark_failure(
        self,
        asset_id: int,
        *,
        status: int | None,
        error: str,
        retries: int,
        permanent: bool,
    ) -> None:
        assert self.conn is not None
        with self.conn:
            self.conn.execute(
                "UPDATE assets SET state='failed', http_status=?, retry_count=retry_count+?, "
                "last_error=?, updated_at=? WHERE id=?",
                (status, retries, error[:500], now_iso(), asset_id),
            )

    def _mark_done(
        self,
        asset_id: int,
        *,
        sha256: str,
        size: int,
        content_type: str | None,
        blob_path: str,
        status: int,
        retries: int,
    ) -> None:
        assert self.conn is not None
        with self.conn:
            self.conn.execute(
                "UPDATE assets SET state='done', sha256=?, size=?, content_type=?, blob_path=?, "
                "http_status=?, retry_count=retry_count+?, last_error=NULL, updated_at=? WHERE id=?",
                (sha256, size, content_type, blob_path, status, retries, now_iso(), asset_id),
            )

    def _store_blob(self, part: Path, sha256: str, ext: str, stats: RunStats) -> tuple[Path, str, bool, bool]:
        """Place .part at assets/<sha[:2]>/<sha><ext> atomically.

        Returns (abs_path, rel_path, reused, overwritten).

        Existing target blob:
          * same size AND the target's recomputed sha256 equals the fresh download's
            sha256 -> byte-identical (content-addressed): drop the .part and reuse
            (0 extra bytes);
          * otherwise (different size, or same size but a different sha256 - i.e. an
            equal-length corruption left behind by an older run) -> clear the
            read-only attribute (both via chmod and FILE_ATTRIBUTE_READONLY) and
            os.replace() over it.  On Windows os.replace() over a +R file fails with
            WinError 5.  The equal-size corruption case MUST be replaced, otherwise
            the corrupted bytes would block self-healing forever (P1-1).
        """
        blob_dir = self.assets_dir / sha256[:2]
        blob_dir.mkdir(parents=True, exist_ok=True)
        target = blob_dir / f"{sha256}{ext}"
        reused = False
        overwritten = False
        if target.exists():
            try:
                same_size = target.stat().st_size == part.stat().st_size
            except OSError:  # pragma: no cover - race with an external cleaner
                same_size = False
            same_content = False
            if same_size:
                try:
                    same_content = file_sha256(target) == sha256
                except OSError:  # unreadable/locked target: treat as corrupt
                    same_content = False
            if same_content:
                part.unlink(missing_ok=True)
                reused = True
            else:
                previous = target.stat().st_size if target.exists() else -1
                new_size = part.stat().st_size
                force_replace(part, target)
                overwritten = True
                stats.readonly_overwrites += 1
                reason = "same size but different sha256 (corrupt)" if same_size else "size mismatch"
                self.log(
                    f"    [blob] replaced stale target {target.name} "
                    f"({human_bytes(previous)} -> {human_bytes(new_size)}, {reason})"
                )
        else:
            os.replace(part, target)
        set_readonly(target)
        return target, target.relative_to(self.root).as_posix(), reused, overwritten

    def _fetch_asset(self, client, plan: AssetPlan, asset_id: int, stats: RunStats) -> str:
        """Download one asset with retries.  Returns final state ('done'|'failed')."""
        attempts = 0
        while attempts < self.max_attempts:
            attempts += 1
            part = self.tmp_dir / f"{os.getpid()}-{time.time_ns()}.part"
            try:
                sha256, size, content_type = self._download_once(client, plan.url, part)
            except DownloadError as exc:
                part.unlink(missing_ok=True)
                if exc.permanent:
                    stats.failed_permanent += 1
                    self._mark_failure(
                        asset_id,
                        status=exc.status,
                        error=str(exc),
                        retries=attempts - 1,
                        permanent=True,
                    )
                    self.log(f"    FAIL(permanent) {plan.media_key} {plan.tier}: {exc}")
                    return "failed"
                last_error, last_status = str(exc), exc.status
            except Exception as exc:  # noqa: BLE001 - httpx/OS errors are network-level
                part.unlink(missing_ok=True)
                last_error, last_status = f"{type(exc).__name__}: {exc}", getattr(exc, "status_code", None)
            else:
                ext = ext_for(plan.url, content_type)
                target, blob_rel, reused, _overwritten = self._store_blob(part, sha256, ext, stats)
                if reused:
                    stats.content_reused += 1
                stats.downloaded += 1
                stats.bytes_downloaded += size
                self._mark_done(
                    asset_id,
                    sha256=sha256,
                    size=size,
                    content_type=content_type,
                    blob_path=blob_rel,
                    status=200,
                    retries=attempts - 1,
                )
                self.log(
                    f"    OK {plan.account_label()} {plan.media_key} {plan.tier:<4} "
                    f"{human_bytes(size):>9} sha={sha256[:12]}"
                )
                return "done"
            if attempts < self.max_attempts:
                delay = self.retry_delays[min(attempts - 1, len(self.retry_delays) - 1)]
                stats.retry_events += 1
                self.log(
                    f"    retry {attempts}/{self.max_attempts - 1} in {delay:g}s: {last_error[:140]}"
                )
                if delay > 0:
                    time.sleep(delay)
        stats.failed_transient += 1
        self._mark_failure(
            asset_id, status=last_status, error=last_error, retries=attempts - 1, permanent=False
        )
        self.log(f"    FAIL(transient, will resume) {plan.media_key} {plan.tier}: {last_error[:160]}")
        return "failed"

    def _synth_tier_plan(
        self, group: list[AssetPlan], tier: str, media_type: str
    ) -> AssetPlan | None:
        """Synthesise an AssetPlan for one tier from the group's archive metadata.

        P0-2 / P1-3: used when the DB has no assets row for that tier, or when a
        permanent failure requires walking the bitrate ladder.  The refs rows carry
        ``media_url``/``variants_json`` (photos a pbs URL), so a tier can be
        resolved without re-scanning LanceDB.  Returns None when the tier cannot be
        built (unknown tier/media_type, no usable variant metadata).
        """
        if tier not in TIER_TABLE.get(media_type, ()):
            return None
        for plan in group:
            for row in plan.rows:
                if row.media_type != media_type:
                    continue
                if media_type == "photo":
                    url = photo_variant_url(
                        row.media_url, "orig" if tier == "orig" else "large"
                    )
                else:
                    variants = parse_variants(row.variants_json, row.media_url)
                    url = pick_variant(variants, tier)
                    if url:
                        url = canonical_media_url(media_type, url) or url
                if url:
                    return AssetPlan(
                        media_key=group[0].media_key,
                        tier=tier,
                        media_type=media_type,
                        url=url,
                        rows=[row],
                    )
        return None

    def _plan_for_tier(self, group: list[AssetPlan], tier: str, media_type: str) -> AssetPlan | None:
        """Find the group's plan for ``tier`` or synthesise one.

        Order: plan of this run -> stored assets row URL (self-heal, P0-2) ->
        variants/media_url carried by the refs rows (tier downgrade, P1-3).
        """
        for plan in group:
            if plan.tier == tier:
                return plan
        asset = self._asset_row(group[0].media_key, tier)
        if asset is not None and asset["url"]:
            return AssetPlan(
                media_key=group[0].media_key,
                tier=tier,
                media_type=asset["media_type"] or media_type,
                url=asset["url"],
                rows=[group[0].rows[0]],
            )
        return self._synth_tier_plan(group, tier, media_type)

    def _degrade_ladder_after(self, media_type: str, tier: str) -> list[str]:
        """Lower tiers of the bitrate ladder to try after ``tier`` failed (P1-3).

        Only video/animated_gif walk their ladder (best->mid->low): photo
        orig/large are ``name=`` variants of the same pbs image where a dead orig
        almost always means a dead large, so probing it would only pollute stats.
        """
        if media_type not in ("video", "animated_gif"):
            return []
        ladder = list(TIER_TABLE.get(media_type, ()))
        if tier not in ladder:
            return []
        return ladder[ladder.index(tier) + 1:]

    def _append_degrade_tiers(
        self,
        media_key: str,
        media_type: str,
        failed_tier: str,
        ordered: list[str],
        group: list[AssetPlan],
    ) -> None:
        """Queue synthesised lower tiers in ``ordered`` after a permanent failure."""
        added: list[str] = []
        for lower in self._degrade_ladder_after(media_type, failed_tier):
            if lower in ordered:
                continue
            if self._plan_for_tier(group, lower, media_type) is None:
                continue
            lower_rank = tier_rank(media_type, lower)
            insert_at = len(ordered)
            for position in range(len(ordered)):
                if tier_rank(media_type, ordered[position]) < lower_rank:
                    insert_at = position
                    break
            ordered.insert(insert_at, lower)
            added.append(lower)
        if added:
            self.log(
                f"    [degrade] {media_key}: {failed_tier} permanently dead; "
                f"trying lower tier(s) {','.join(added)}"
            )

    def _point_group_refs(self, group: list[AssetPlan], asset_id: int, stats: RunStats) -> None:
        assert self.conn is not None
        with self.conn:
            for plan in group:
                self._point_refs(plan, asset_id, stats)

    def _reconcile_after_upgrade(self, media_key: str, serving: sqlite3.Row, stats: RunStats) -> None:
        """A better tier just finished downloading: make it serve *all* refs.

        Every ref of the media_key currently pointing at a lower-tier asset is
        repointed to the better asset; the lower-tier asset rows become
        state='superseded' (they keep their blob until ``--gc`` runs, so an
        interrupted run never loses data).
        """
        assert self.conn is not None
        serving_rank = tier_rank(serving["media_type"], serving["tier"])
        if serving_rank <= 0:
            return
        others = self.conn.execute(
            "SELECT id, media_type, tier FROM assets WHERE media_key=? AND id<>?",
            (media_key, serving["id"]),
        ).fetchall()
        lower = [
            row
            for row in others
            if tier_rank(row["media_type"] or serving["media_type"], row["tier"]) < serving_rank
            and (row["media_type"] or serving["media_type"]) == serving["media_type"]
        ]
        if not lower:
            return
        ids = [int(row["id"]) for row in lower]
        placeholders = ",".join("?" for _ in ids)
        with self.conn:
            cursor = self.conn.execute(
                f"UPDATE refs SET asset_id=? WHERE asset_id IN ({placeholders})",
                (serving["id"], *ids),
            )
            stats.refs_repointed += max(0, cursor.rowcount)
            self.conn.execute(
                f"UPDATE assets SET state='superseded', updated_at=? WHERE id IN ({placeholders})",
                (now_iso(), *ids),
            )
        stats.upgraded += len(lower)
        self.log(
            f"    [dominance] {media_key}: {serving['tier']} supersedes "
            f"{','.join(str(row['tier']) for row in lower)}"
        )

    def _download_group(
        self, client, media_key: str, group: list[AssetPlan], stats: RunStats, max_bytes: int | None
    ) -> None:
        """Resolve one media_key group: reuse / download highest usable tier available.

        Candidate tiers, highest rank first:
          * every tier requested by this run's plans;
          * every tier already ``done`` in the DB;
          * every *higher* tier left ``pending`` by an interrupted run or by the
            self-heal reset of a vanished blob (P0-2): it is completed first instead
            of silently serving a lower tier that this run only asks for.

        Failure semantics (P0-3 + P1-3):
          * a *permanent* HTTP failure (400/401/403/404/410/451) queues the lower
            tiers of the bitrate ladder (best->mid->low), synthesised from
            variants_json, and keeps trying; only when every tier is permanently
            dead does the group become ``refs_unresolved``;
          * a transient failure (network/5xx/429/truncated) stops the group: refs
            keep pointing at the retryable failed/pending asset and are counted in
            ``refs_unresolved`` — the store never silently repoints them to a lower
            tier that happens to be done;
          * only assets with ``state='done'`` are reuse candidates; a merely
            failed/superseded row never counts as a served tier.
        """
        assert self.conn is not None
        media_type = group[0].media_type
        requested_tiers = {plan.tier for plan in group}
        max_requested_rank = max(
            (tier_rank(media_type, tier) for tier in requested_tiers), default=0
        )
        candidate_tiers = set(requested_tiers)
        for row in self.conn.execute(
            "SELECT tier, state, url FROM assets WHERE media_key=?", (media_key,)
        ):
            rank = tier_rank(media_type, row["tier"])
            if rank <= 0:
                continue
            if row["state"] == "done":
                candidate_tiers.add(row["tier"])
            elif (
                row["state"] == "pending"
                and row["url"]
                and rank >= max_requested_rank
            ):
                # P0-2: a higher tier is pending (blob lost -> self-heal reset, or an
                # earlier run was interrupted).  Finish it rather than downgrade.
                candidate_tiers.add(row["tier"])
        ordered = sorted(candidate_tiers, key=lambda tier: (-tier_rank(media_type, tier), tier))
        serving: sqlite3.Row | None = None
        downloaded_now = False
        index = 0
        while index < len(ordered):
            tier = ordered[index]
            index += 1
            asset = self._asset_row(media_key, tier)
            if asset is not None and asset["state"] == "done":
                if self._healthy_done(asset):
                    blob = self.blob_abs(asset["blob_path"])
                    if blob is not None and not is_readonly(blob):  # self-heal lost attribute
                        set_readonly(blob)
                    serving = asset
                    break
                self._reset_to_pending(asset["id"])  # vanished/stale blob: self-heal
                asset = self._asset_row(media_key, tier)
            if (
                asset is not None
                and asset["state"] == "failed"
                and asset["http_status"] in PERMANENT_HTTP_STATUSES
            ):
                # Permanently dead URL: skip it and make sure the lower ladder is queued.
                self._append_degrade_tiers(media_key, media_type, tier, ordered, group)
                continue
            if max_bytes is not None and stats.bytes_downloaded >= max_bytes:
                stats.budget_stopped = True
                self.log(f"[budget] --max-mb reached ({human_bytes(max_bytes)}); stopping downloads")
                return
            plan = self._plan_for_tier(group, tier, media_type)
            if plan is None:
                continue
            if asset is None:
                asset = self._ensure_asset_row(plan, now_iso())
            self.log(
                f"    [{media_key} {tier} {plan.media_type} "
                f"{plan.rows[0].account}]"
            )
            if self._fetch_asset(client, plan, asset["id"], stats) == "done":
                serving = self._asset_row(media_key, tier)
                downloaded_now = True
                break
            refreshed = self._asset_row(media_key, tier)
            if not (
                refreshed is not None
                and refreshed["state"] == "failed"
                and refreshed["http_status"] in PERMANENT_HTTP_STATUSES
            ):
                # Transient error: keep the retryable asset, do NOT degrade this group.
                stats.refs_unresolved += sum(len(p.rows) for p in group)
                self.log(
                    f"    [retry-later] {media_key} {tier}: transient failure; "
                    f"refs stay on the retryable asset (no downgrade)"
                )
                return
            # P1-3: permanent failure -> queue synthesised lower tiers (best->mid->low).
            self._append_degrade_tiers(media_key, media_type, tier, ordered, group)
        if serving is None:
            stats.refs_unresolved += sum(len(plan.rows) for plan in group)
            self.log(f"    [unresolved] {media_key}: no usable tier this run")
            return
        serving_rank = tier_rank(serving["media_type"] or media_type, serving["tier"])
        downgraded = sum(
            len(plan.rows)
            for plan in group
            if tier_rank(plan.media_type, plan.tier) > serving_rank
        )
        if downgraded:
            # Only reachable after the requested higher tier(s) failed permanently.
            stats.refs_downgraded += downgraded
            self.log(
                f"    [degrade] {media_key}: serving lower tier {serving['tier']} "
                f"after permanent failure(s); {downgraded} ref(s) downgraded"
            )
        self._point_group_refs(group, serving["id"], stats)
        if downloaded_now:
            self._reconcile_after_upgrade(media_key, serving, stats)
        for plan in group:
            if plan.tier == serving["tier"]:
                continue
            requested_rank = tier_rank(plan.media_type, plan.tier)
            if 0 < requested_rank < serving_rank:
                if downloaded_now:
                    stats.dominance_merged += 1
                else:
                    stats.dominance_reused += 1

    def download_phase(self, client, plans: list[AssetPlan], stats: RunStats, max_bytes: int | None) -> None:
        assert self.conn is not None
        groups = self._group_plans(plans)
        # User-facing counters per requested (media_key, tier) identity.
        for plan in plans:
            own = self._asset_row(plan.media_key, plan.tier)
            if own is None:
                continue
            if own["state"] == "done" and self._healthy_done(own):
                stats.already_done += 1
                blob = self.blob_abs(own["blob_path"])
                if blob is not None and not is_readonly(blob):
                    set_readonly(blob)
            elif own["state"] == "failed" and own["http_status"] in PERMANENT_HTTP_STATUSES:
                stats.skipped_permanent += 1
        total = len(groups)
        for index, (media_key, group) in enumerate(groups.items(), start=1):
            if max_bytes is not None and stats.bytes_downloaded >= max_bytes:
                stats.budget_stopped = True
                self.log(f"[budget] --max-mb reached ({human_bytes(max_bytes)}); stopping downloads")
                break
            tiers = ",".join(plan.tier for plan in group)
            self.log(
                f"[{index}/{total}] {media_key} requested={tiers} "
                f"{group[0].media_type} {group[0].rows[0].account}"
            )
            self._download_group(client, media_key, group, stats, max_bytes)

    # -- projections ------------------------------------------------------- #
    def project_phase(self, _plans: list[AssetPlan], mode: str, stats: RunStats) -> None:
        """Project every done ref; paths come only from refs (no LanceDB lookup)."""
        if mode == "none":
            return
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT r.account, r.collection, r.tweet_id, r.media_index, r.author_handle,"
            "       r.tweet_created_at, a.blob_path"
            "  FROM refs r JOIN assets a ON a.id = r.asset_id"
            " WHERE a.state='done' AND a.blob_path IS NOT NULL"
            " ORDER BY r.account, r.collection, r.tweet_id, r.media_index"
        ).fetchall()
        for ref in rows:
            source = self.blob_abs(ref["blob_path"])
            if not source or not source.is_file():
                continue
            ext = Path(ref["blob_path"]).suffix or ".bin"
            rel = self.projection_rel(ref, ext)
            dst = self.root / rel
            try:
                self._project_one(source, dst, mode, stats)
            except OSError as exc:
                stats.projection_errors += 1
                self.log(f"    [project] ERROR {rel.as_posix()}: {exc}")

    def _project_one(self, source: Path, dst: Path, mode: str, stats: RunStats) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            try:
                same = os.path.samefile(source, dst)
            except OSError:
                same = False
            if same and mode == "hardlink":
                stats.projected += 1
                return
            if same and mode == "copy":
                stats.projected += 1
                return
            if mode == "copy" and not same:
                try:
                    if dst.stat().st_size == source.stat().st_size:
                        stats.projected += 1
                        return
                except OSError:
                    pass
            clear_readonly(dst)
            try:
                dst.unlink()
            except FileNotFoundError:
                pass
        if mode == "hardlink":
            try:
                self.link_fn(source, dst)
                stats.projected += 1
                return
            except OSError as exc:
                stats.projection_fallbacks += 1
                self.log(f"    [project] hardlink failed ({exc}); falling back to copy")
        shutil.copyfile(source, dst)
        stats.projected += 1

    # -- exports ----------------------------------------------------------- #
    def export(self) -> dict:
        """Write store/index.csv + store/summary.json from current DB state."""
        assert self.conn is not None
        self.root.mkdir(parents=True, exist_ok=True)
        rows = self.conn.execute(
            "SELECT r.id AS ref_id, r.account, r.collection, r.tweet_id, r.media_index,"
            "       r.author_handle, r.media_key, r.tweet_created_at, r.requested_tier,"
            "       r.media_type AS ref_media_type,"
            "       a.id AS asset_id, a.media_type AS asset_media_type, a.tier, a.state,"
            "       a.sha256, a.size, a.content_type, a.blob_path, a.url, a.http_status,"
            "       a.retry_count, a.last_error"
            "  FROM refs r JOIN assets a ON a.id = r.asset_id"
            " ORDER BY r.account, r.collection, r.tweet_id, r.media_index"
        ).fetchall()
        index_path = self.root / "index.csv"
        with open(index_path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(INDEX_COLUMNS)
            for row in rows:
                media_type = row["asset_media_type"] or row["ref_media_type"] or ""
                ext = Path(row["blob_path"]).suffix if row["blob_path"] else ".bin"
                projected = self.projection_rel(row, ext or ".bin")
                projected_rel = projected.as_posix() if (self.root / projected).exists() else ""
                writer.writerow(
                    [
                        row["account"],
                        row["collection"],
                        media_type,
                        row["tier"],
                        row["requested_tier"] or "",
                        row["tweet_id"],
                        row["media_index"],
                        row["author_handle"] or "",
                        row["media_key"],
                        row["tweet_created_at"] or "",
                        row["state"],
                        row["sha256"] or "",
                        row["size"] if row["size"] is not None else "",
                        row["content_type"] or "",
                        row["blob_path"] or "",
                        projected_rel,
                        row["url"] or "",
                    ]
                )
        summary = self.build_summary(rows)
        summary_path = self.root / "summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"[export] {index_path} ({len(rows)} rows), {summary_path}")
        return summary

    def build_summary(self, rows: list[sqlite3.Row]) -> dict:
        assert self.conn is not None

        def empty() -> dict:
            return {"refs": 0, "done": 0, "failed": 0, "pending": 0, "superseded": 0, "bytes": 0}

        def grouped(key_fn) -> dict:
            out: dict = {}
            for row in rows:
                key = key_fn(row)
                stats = out.setdefault(key, empty())
                stats["refs"] += 1
                if row["state"] in ASSET_STATES:
                    stats[row["state"]] += 1
                if row["state"] == "done" and row["size"]:
                    stats["bytes"] += int(row["size"])
            return dict(sorted(out.items(), key=lambda item: str(item[0])))

        asset_states = {
            row["state"]: row["count"]
            for row in self.conn.execute("SELECT state, COUNT(*) AS count FROM assets GROUP BY state")
        }
        unique_blob = self.conn.execute(
            "SELECT COUNT(*) AS blobs, COALESCE(SUM(size), 0) AS bytes FROM "
            "(SELECT DISTINCT sha256, size FROM assets WHERE state='done' AND sha256 IS NOT NULL)"
        ).fetchone()
        totals = {
            "assets": sum(asset_states.values()),
            "assets_done": asset_states.get("done", 0),
            "assets_failed": asset_states.get("failed", 0),
            "assets_pending": asset_states.get("pending", 0),
            "assets_superseded": asset_states.get("superseded", 0),
            "refs": len(rows),
            "refs_done": sum(1 for row in rows if row["state"] == "done"),
            "logical_bytes": sum(int(row["size"]) for row in rows if row["state"] == "done" and row["size"]),
            "unique_blobs": unique_blob["blobs"],
            "unique_blob_bytes": unique_blob["bytes"],
        }
        cross = [
            {
                "account": key[0],
                "collection": key[1],
                "media_type": key[2],
                "tier": key[3],
                **value,
            }
            for key, value in grouped(
                lambda row: (
                    row["account"],
                    row["collection"] or "",
                    row["asset_media_type"] or row["ref_media_type"] or "",
                    row["tier"],
                )
            ).items()
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now_iso(),
            "policy": self.last_policy.spec if self.last_policy else None,
            "totals": totals,
            "by_account": grouped(lambda row: row["account"]),
            "by_collection": grouped(lambda row: row["collection"] or ""),
            "by_media_type": grouped(
                lambda row: row["asset_media_type"] or row["ref_media_type"] or ""
            ),
            "by_tier": grouped(lambda row: row["tier"]),
            "by_account_collection_media_type_tier": cross,
            "last_run": self.last_stats.as_dict(),
        }

    # -- dry run ----------------------------------------------------------- #
    def _known_asset_states(self) -> dict[tuple[str, str], dict]:
        if not self.db_path.exists():
            return {}
        conn = None
        try:
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            return {
                (row["media_key"], row["tier"]): dict(row)
                for row in conn.execute(
                    "SELECT media_key, tier, media_type, state, http_status, blob_path, size FROM assets"
                )
            }
        except sqlite3.Error:
            return {}
        finally:
            if conn is not None:
                conn.close()

    def _dry_healthy(self, record: dict | None) -> bool:
        if not record or record.get("state") != "done":
            return False
        blob = self.blob_abs(record.get("blob_path"))
        if not blob or not blob.is_file():
            return False
        if record.get("size") is not None and blob.stat().st_size != int(record["size"]):
            return False
        return True

    def _dry_serving(self, media_key: str, requested_tier: str, states: dict) -> dict | None:
        best: dict | None = None
        best_rank = 0
        for (mk, tier), record in states.items():
            if mk != media_key or not self._dry_healthy(record):
                continue
            media_type = record.get("media_type")
            if not tier_dominates(media_type, tier, requested_tier):
                continue
            rank = tier_rank(media_type, tier)
            if rank > best_rank:
                best, best_rank = record, rank
        return best

    def dry_run(self, rows: list[MediaRow], policy: Policy, limit: int = 0) -> RunStats:
        stats = RunStats(dry_run=True)
        plans, unresolved = build_plans(rows, policy)
        stats.unresolved = len(unresolved)
        if limit and limit > 0:
            plans = plans[:limit]
        states = self._known_asset_states()
        stats.selected_assets = len(plans)
        stats.selected_refs = sum(len(plan.rows) for plan in plans)
        for plan in plans:
            record = states.get((plan.media_key, plan.tier))
            if self._dry_healthy(record):
                stats.already_done += 1
            elif record and record.get("state") == "failed" and record.get("http_status") in PERMANENT_HTTP_STATUSES:
                stats.skipped_permanent += 1
        groups = self._group_plans(plans)
        for media_key, group in groups.items():
            top = group[0]
            serving = self._dry_serving(media_key, top.tier, states)
            if serving is None:
                # A group whose every candidate tier is a known permanent failure
                # is skipped, not "would download" (matches download_phase).
                candidates = {plan.tier for plan in group}
                candidates.update(tier for (mk, tier) in states if mk == media_key)
                # P1-3: a permanently dead tier still falls back to synthesised lower
                # ladder tiers (best->mid->low) that are not in the DB yet; such a
                # group is not permanently dead and a real run would download them.
                for tier in self._degrade_ladder_after(top.media_type, top.tier):
                    if tier in candidates:
                        continue
                    if self._synth_tier_plan(group, tier, top.media_type) is not None:
                        candidates.add(tier)
                permanently_dead = bool(candidates) and all(
                    (states.get((media_key, tier)) or {}).get("state") == "failed"
                    and (states.get((media_key, tier)) or {}).get("http_status") in PERMANENT_HTTP_STATUSES
                    for tier in candidates
                )
                if not permanently_dead:
                    stats.would_download += 1
                continue
            serving_rank = tier_rank(serving.get("media_type") or top.media_type, serving["tier"])
            for plan in group:
                if plan.tier == serving["tier"]:
                    continue
                if 0 < tier_rank(plan.media_type, plan.tier) < serving_rank:
                    stats.dominance_reused += 1
        self.log(
            f"[dry-run] discovered {len(rows)} media rows; selected {stats.selected_assets} assets "
            f"/ {stats.selected_refs} refs; would download {stats.would_download} asset(s) "
            f"({stats.already_done} already done, {stats.skipped_permanent} permanently failed, "
            f"{stats.dominance_reused} dominated by a better tier); unresolved {stats.unresolved}"
        )
        shown = 0
        for media_key, group in groups.items():
            if shown >= 10:
                break
            shown += 1
            top = group[0]
            serving = self._dry_serving(media_key, top.tier, states)
            tiers = ",".join(plan.tier for plan in group)
            if serving is not None:
                note = f"reuse {serving['tier']} (requested {tiers})"
            else:
                note = f"download {tiers}"
            self.log(f"    {media_key} {top.media_type:<12} {note}")
        if len(groups) > shown:
            self.log(f"    ... {len(groups) - shown} more media groups")
        return stats

    # -- gc helpers -------------------------------------------------------- #
    def _blob_key(self, blob_path: str | None) -> str | None:
        """Normalised absolute key for a stored blob_path (None when unset)."""
        if not blob_path:
            return None
        path = self.blob_abs(blob_path)
        if path is None:
            return None
        return os.path.normcase(os.path.abspath(str(path)))

    def _referenced_blob_keys(self, exclude_ids: tuple[int, ...] = ()) -> set[str]:
        """Blob keys referenced by the *remaining* assets rows (optionally excluding ids)."""
        assert self.conn is not None
        sql = "SELECT blob_path FROM assets WHERE blob_path IS NOT NULL"
        params: tuple = ()
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            sql += f" AND id NOT IN ({placeholders})"
            params = tuple(exclude_ids)
        keys: set[str] = set()
        for row in self.conn.execute(sql, params):
            key = self._blob_key(row[0])
            if key is not None:
                keys.add(key)
        return keys

    def _iter_blob_files(self) -> list[Path]:
        if not self.assets_dir.is_dir():
            return []
        return [path for path in sorted(self.assets_dir.rglob("*")) if path.is_file()]

    def gc(self, *, dry_run: bool = False) -> dict:
        """Reclaim superseded asset rows and unreferenced blob files (two phases).

        Phase 1 (one transaction): delete ``state='superseded'`` asset rows that no
        ref points at any more.
        Phase 2 (after the commit): re-scan ``assets.blob_path`` reference counts of
        the *remaining* rows only and delete physical files whose count is 0 —
        including orphans left by older runs / interrupted cleanups.  A blob shared
        by a surviving row (e.g. a mid asset that supersedes a byte-identical low
        asset) is therefore kept.  Read-only blobs are made writable before
        unlinking; empty sha-prefix dirs are removed; exports are refreshed.
        ``dry_run=True`` reports exactly what both phases would do, without writes.
        """
        assert self.conn is not None
        superseded = self.conn.execute(
            "SELECT a.id, a.media_key, a.tier, a.blob_path, a.size FROM assets a "
            "WHERE a.state='superseded' AND NOT EXISTS(SELECT 1 FROM refs r WHERE r.asset_id=a.id)"
        ).fetchall()
        superseded_ids = tuple(int(row["id"]) for row in superseded)
        superseded_blob_keys = {
            key for key in (self._blob_key(row["blob_path"]) for row in superseded) if key
        }

        def _scan(remaining: set[str]) -> tuple[list[Path], int]:
            to_delete: list[Path] = []
            orphan_files = 0
            for file in self._iter_blob_files():
                key = os.path.normcase(os.path.abspath(str(file)))
                if key in remaining:
                    continue
                to_delete.append(file)
                if key not in superseded_blob_keys:  # a plain orphan, not a superseded blob
                    orphan_files += 1
            return to_delete, orphan_files

        remaining_after = self._referenced_blob_keys(exclude_ids=superseded_ids)
        delete_paths, orphan_files = _scan(remaining_after)
        reclaim_bytes = 0
        for blob in delete_paths:
            try:
                reclaim_bytes += blob.stat().st_size
            except OSError:
                pass
        summary = {
            "dry_run": dry_run,
            "superseded_rows": len(superseded),
            "reclaim_files": len(delete_paths),
            "orphan_files": orphan_files,
            "reclaim_bytes": reclaim_bytes,
        }
        if dry_run:
            self.log(
                f"[gc] dry-run: phase-1 would delete {len(superseded)} superseded row(s); "
                f"phase-2 would delete {len(delete_paths)} blob file(s) "
                f"({orphan_files} orphan), reclaimable {human_bytes(reclaim_bytes)}"
            )
            for row in superseded[:20]:
                self.log(
                    f"    row {row['media_key']} {row['tier']} ({human_bytes(row['size'] or 0)})"
                )
            for blob in delete_paths[:20]:
                self.log(f"    blob {blob.relative_to(self.root).as_posix()}")
            if len(delete_paths) > 20:
                self.log(f"    ... {len(delete_paths) - 20} more file(s)")
            return summary

        # Phase 1: remove the superseded rows (commit before touching any file).
        if superseded_ids:
            placeholders = ",".join("?" for _ in superseded_ids)
            with self.conn:
                self.conn.execute(
                    f"DELETE FROM assets WHERE id IN ({placeholders})", superseded_ids
                )

        # Phase 2: rebuild the reference map from the surviving rows, then delete
        # every file that no remaining row references (recount, never trust phase 1).
        remaining = self._referenced_blob_keys()
        delete_paths, orphan_files = _scan(remaining)
        removed_files = 0
        reclaimed = 0
        for blob in delete_paths:
            try:
                reclaimed += blob.stat().st_size
            except OSError:
                pass
            if unlink_force(blob):
                removed_files += 1
        removed_dirs = 0
        for directory in sorted(self.assets_dir.iterdir(), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                try:
                    directory.rmdir()
                    removed_dirs += 1
                except OSError:
                    pass
        self.export()
        self.log(
            f"[gc] phase-1 removed {len(superseded_ids)} superseded row(s); "
            f"phase-2 removed {removed_files}/{len(delete_paths)} blob file(s) "
            f"({orphan_files} orphan), reclaimed {human_bytes(reclaimed)} "
            f"({reclaimed} bytes), {removed_dirs} empty dir(s)"
        )
        summary["reclaimed_bytes"] = reclaimed
        summary["removed_files"] = removed_files
        summary["removed_dirs"] = removed_dirs
        return summary

    # -- verification ------------------------------------------------------ #
    def verify(self, *, quick: bool = False) -> int:
        """Read-only consistency check of the store; returns a process exit code.

        Checks every ``state='done'`` asset row: blob file exists, size matches the
        DB, the read-only attribute is set; in full mode the sha256 is recomputed and
        compared.  Also counts blob files under assets/ that no asset row references.
        Lists at most 20 problems and returns 1 when any inconsistency was found
        (0 when clean, 2 when the database cannot be opened).  Never writes: the DB
        is opened with ``mode=ro`` and no lock is taken, so it can run while another
        instance is serving reads.
        """
        if not self.db_path.is_file():
            self.log(f"[verify] database not found: {self.db_path}")
            return 2
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            self.log(f"[verify] cannot open {self.db_path} read-only: {exc}")
            return 2
        problems: list[str] = []
        checked = 0
        try:
            rows = conn.execute(
                "SELECT id, media_key, tier, sha256, size, blob_path FROM assets "
                "WHERE state='done' ORDER BY id"
            ).fetchall()
            for row in rows:
                checked += 1
                label = f"asset {row['id']} {row['media_key']}/{row['tier']}"
                blob = self.blob_abs(row["blob_path"])
                if blob is None:
                    problems.append(f"{label}: blob_path is NULL")
                    continue
                if not blob.is_file():
                    problems.append(f"{label}: blob missing ({row['blob_path']})")
                    continue
                try:
                    actual_size = blob.stat().st_size
                except OSError as exc:
                    problems.append(f"{label}: cannot stat blob ({exc})")
                    continue
                if row["size"] is not None and actual_size != int(row["size"]):
                    problems.append(
                        f"{label}: size mismatch (db={row['size']} file={actual_size})"
                    )
                    continue
                if not is_readonly(blob):
                    problems.append(f"{label}: blob is not read-only ({row['blob_path']})")
                if not quick:
                    if not row["sha256"]:
                        problems.append(f"{label}: sha256 is NULL in DB")
                        continue
                    try:
                        digest = file_sha256(blob)
                    except OSError as exc:
                        problems.append(f"{label}: cannot read blob ({exc})")
                        continue
                    if digest != row["sha256"]:
                        problems.append(
                            f"{label}: sha256 mismatch (db={row['sha256'][:12]} file={digest[:12]})"
                        )
            referenced: set[str] = set()
            for row in conn.execute(
                "SELECT blob_path FROM assets WHERE blob_path IS NOT NULL"
            ):
                key = self._blob_key(row[0])
                if key is not None:
                    referenced.add(key)
            orphan_files = [
                path
                for path in self._iter_blob_files()
                if os.path.normcase(os.path.abspath(str(path))) not in referenced
            ]
        finally:
            conn.close()
        mode = "quick" if quick else "full"
        self.log(
            f"[verify] mode={mode} checked={checked} done asset(s); "
            f"problems={len(problems)}; orphan blobs={len(orphan_files)}"
        )
        for problem in problems[:20]:
            self.log(f"    BAD {problem}")
        if len(problems) > 20:
            self.log(f"    ... {len(problems) - 20} more problem(s)")
        for orphan in orphan_files[:20]:
            self.log(f"    ORPHAN {orphan.relative_to(self.root).as_posix()}")
        if len(orphan_files) > 20:
            self.log(f"    ... {len(orphan_files) - 20} more orphan blob(s)")
        if problems:
            self.log(f"[verify] FAILED: {len(problems)} inconsistency(ies) - run --gc / re-run downloads")
            return 1
        self.log(f"[verify] OK: {checked} done asset(s) consistent, {len(orphan_files)} orphan blob(s)")
        return 0

    # -- main run ---------------------------------------------------------- #
    def run(
        self,
        rows: list[MediaRow],
        *,
        policy: Policy,
        limit: int = 0,
        max_bytes: int | None = None,
        project: str = "none",
        dry_run: bool = False,
    ) -> RunStats:
        started = time.time()
        self.last_policy = policy
        if dry_run:
            return self.dry_run(rows, policy, limit)
        if self.conn is None:
            raise RuntimeError("Store.run() requires Store.open()")
        plans, unresolved = build_plans(rows, policy)
        stats = RunStats()
        stats.unresolved = len(unresolved)
        for row, reason in unresolved[:5]:
            self.log(f"[skip] {row.account}/{row.tweet_id} {row.media_key}: {reason}")
        if len(unresolved) > 5:
            self.log(f"[skip] ... {len(unresolved) - 5} more unresolved rows")
        if limit and limit > 0:
            plans = plans[:limit]
        stats.selected_assets = len(plans)
        stats.selected_refs = sum(len(plan.rows) for plan in plans)
        self.log(
            f"[plan] rows={len(rows)} assets={stats.selected_assets} refs={stats.selected_refs} "
            f"policy={policy.spec}"
        )
        self.register(plans, stats)
        try:
            client = self.client_factory()
            try:
                self.download_phase(client, plans, stats, max_bytes)
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            if project and project != "none":
                self.project_phase(plans, project, stats)
        finally:
            stats.seconds = round(time.time() - started, 1)
            self.last_stats = stats
            self.export()
            self.print_report(stats)
        return stats

    # -- reporting --------------------------------------------------------- #
    def print_report(self, stats: RunStats) -> None:
        self.log("")
        self.log("=== store v3 run ===")
        self.log(f"  selected      : {stats.selected_assets} assets / {stats.selected_refs} refs"
                 f" (new refs {stats.new_refs}, unresolved {stats.unresolved})")
        self.log(f"  already done  : {stats.already_done}")
        self.log(f"  downloaded    : {stats.downloaded} ({human_bytes(stats.bytes_downloaded)}, "
                 f"content reuse {stats.content_reused}, read-only overwrites {stats.readonly_overwrites})")
        self.log(f"  dominance     : {stats.dominance_reused} request(s) served by an existing better tier, "
                 f"{stats.dominance_merged} merged into this run's higher tier, "
                 f"{stats.upgraded} lower asset(s) superseded")
        if stats.refs_repointed:
            self.log(f"  refs moved    : {stats.refs_repointed}")
        self.log(f"  failures      : {stats.failed_permanent} permanent / "
                 f"{stats.failed_transient} transient / {stats.skipped_permanent} skipped-permanent")
        if stats.refs_unresolved or stats.refs_downgraded:
            self.log(f"  refs quality  : {stats.refs_unresolved} unresolved (transient failure, "
                     f"will retry), {stats.refs_downgraded} downgraded (permanent failure)")
        self.log(f"  retries       : {stats.retry_events}")
        self.log(f"  projections   : {stats.projected} (fallbacks {stats.projection_fallbacks}, "
                 f"errors {stats.projection_errors})")
        if stats.budget_stopped:
            self.log("  budget        : stopped early (raise --max-mb or re-run to continue)")
        self.log(f"  elapsed       : {stats.seconds}s")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_retry_delays(value: str | None) -> tuple[float, ...]:
    if not value:
        return DEFAULT_RETRY_DELAYS
    delays = []
    for part in value.split(","):
        part = part.strip()
        if part:
            delays.append(float(part))
    return tuple(delays) or DEFAULT_RETRY_DELAYS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="store_v3.py",
        description="tweetxvault media archive v5: SQLite state + content-addressed blobs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--limit", type=int, default=100,
                        help="max (media_key,tier) assets to process this run; 0 = unlimited")
    parser.add_argument("--accounts", default="", help="comma separated account ids (default: all)")
    parser.add_argument("--collections", default="", help="like,bookmark,unknown (default: all)")
    parser.add_argument("--types", default="", help="photo,video,animated_gif (default: all)")
    parser.add_argument("--policy", default="default",
                        help="default|recommended|best|low or a spec like 'photo=large,video:bookmark=best'")
    parser.add_argument("--project", default="none", choices=["none", "hardlink", "copy"],
                        help="optional human-readable projection")
    parser.add_argument("--dry-run", action="store_true", help="plan only, no downloads, no writes")
    parser.add_argument("--gc", action="store_true",
                        help="maintenance mode: delete superseded asset rows + orphan blobs "
                             "(combine with --dry-run to only report reclaimable bytes)")
    parser.add_argument("--verify", action="store_true",
                        help="read-only consistency check of every done asset + orphan blob count")
    parser.add_argument("--quick", action="store_true",
                        help="with --verify: only check file existence + size (skip sha256)")
    parser.add_argument("--max-mb", type=float, default=500.0,
                        help="soft cap on bytes downloaded in this run (0 = unlimited)")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="store root directory")
    parser.add_argument("--self-test", action="store_true", help="run the offline self-test and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        from store_v3_selftest import main as selftest_main

        return selftest_main([])

    if args.quick and not args.verify:
        print("error: --quick requires --verify", file=sys.stderr)
        return 2
    if args.verify and args.gc:
        print("error: --verify and --gc are separate modes; run them one at a time", file=sys.stderr)
        return 2
    if args.verify:
        # Read-only: no store lock, no migration, no exports.
        return Store(args.store).verify(quick=args.quick)

    retry_delays = parse_retry_delays(os.environ.get("STORE_V3_RETRY_DELAYS"))
    store = Store(args.store, retry_delays=retry_delays)

    # --gc is a standalone maintenance mode: it never scans LanceDB and never
    # downloads; with --dry-run it only prints the reclaimable byte total.
    if args.gc:
        try:
            store.open()
        except StoreLockError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_LOCK_BUSY
        try:
            summary = store.gc(dry_run=args.dry_run)
        except KeyboardInterrupt:
            print("\ninterrupted; gc changes are transactional per DELETE batch", file=sys.stderr)
            return 130
        finally:
            store.close()
        if not args.dry_run:
            print(f"[gc] reclaimed {summary.get('reclaimed_bytes', 0)} bytes total")
        return 0

    collections = _split_csv(args.collections)
    if collections:
        collections = [c.lower() for c in collections]
    media_types = _split_csv(args.types)
    limit = max(0, args.limit)
    max_bytes = None if args.max_mb <= 0 else int(args.max_mb * 1024 * 1024)
    try:
        policy = make_policy(args.policy)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    accounts = resolve_accounts(args.accounts or None)
    if not accounts:
        print("error: no account archives found under accounts/", file=sys.stderr)
        return 2
    print(f"[store] accounts={','.join(accounts)} policy={policy.spec}")
    rows = discover_rows(accounts, collections, media_types)
    if not rows:
        print("error: no media rows matched the filters", file=sys.stderr)
        return 2

    if args.dry_run:
        stats = store.run(rows, policy=policy, limit=limit, dry_run=True)
        return 0

    try:
        store.open()
    except StoreLockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_BUSY
    try:
        stats = store.run(
            rows,
            policy=policy,
            limit=limit,
            max_bytes=max_bytes,
            project=args.project,
        )
    except KeyboardInterrupt:
        print("\ninterrupted; state is resumable, .part files cleaned up on next run", file=sys.stderr)
        return 130
    finally:
        store.close()
    if stats.failed_permanent or stats.failed_transient:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
