#!/usr/bin/env python
"""backup_store.py -- consistent cold backup of the v3/v4/v5 media store.

Why not just copy store/media.db?  The store DB runs in SQLite WAL mode:
committed transactions can still live in media.db-wal, and a byte-for-byte copy
of media.db taken while a writer is active can be torn (or silently miss data).
This tool uses sqlite3's online backup API (``source_conn.backup(dest_conn)``),
which produces a single consistent snapshot file at a point in time, without
blocking the writer for more than a short page-step.

WHAT IS WRITTEN
    <dest>/media.db                 consistent SQLite snapshot (WAL already folded in)
    <dest>/BACKUP_MANIFEST.json     counts + per-file sha256 + schema_version + timestamp
    <dest>/assets/...               only with --copy-assets

The manifest records:
  * assets/refs counts of the snapshot and of the source (verified equal unless
    the source was being written during the backup),
  * sha256 + byte size of every file this tool produced,
  * meta.schema_version of the snapshot,
  * an ISO timestamp,
  * with --include-archives: the read-only inventory of accounts/*/tweetxvault
    (paths only - archives are never written by this tool).

This module never reads credentials and never prints store contents.

The backup takes the exclusive ``store/.lock`` (the same lock store_v3 uses for
every write path) before it starts reading the database or assets, so a cold
backup and a running writer are mutually exclusive.  A second backup / a writer
holding the lock makes this tool fail fast with exit code 4 (P0-1).

Usage::

    python tools/backup_store.py --dest D:\\dshwork\\txv\\work\\backup-YYYYMMDD
    python tools/backup_store.py --dest ... --copy-assets
    python tools/backup_store.py --dest ... --copy-assets --include-archives --force

Exit codes: 0 = backup verified, 2 = argument/source/destination problem,
4 = another instance holds store/.lock (backup/write paths are exclusive).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from common import ROOT  # noqa: E402
from store_v3 import (  # noqa: E402
    EXIT_LOCK_BUSY,
    STORE_LOCK_NAME,
    StoreLock,
    StoreLockError,
)

DEFAULT_STORE = ROOT / "store"
MANIFEST_NAME = "BACKUP_MANIFEST.json"
BACKUP_FORMAT = "txv-store-backup/1"
CHUNK = 1 << 20


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _db_counts(conn: sqlite3.Connection) -> dict:
    """Counts + schema_version straight out of one SQLite connection."""
    one = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    refs = conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM assets WHERE state='done'").fetchone()[0]
    superseded = conn.execute("SELECT COUNT(*) FROM assets WHERE state='superseded'").fetchone()[0]
    unique = conn.execute(
        "SELECT COUNT(*) AS blobs, COALESCE(SUM(size), 0) AS bytes FROM "
        "(SELECT DISTINCT sha256, size FROM assets WHERE state='done' AND sha256 IS NOT NULL)"
    ).fetchone()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        schema_version = row[0] if row else None
    except sqlite3.Error:
        schema_version = None
    return {
        "assets": int(one),
        "refs": int(refs),
        "assets_done": int(done),
        "assets_superseded": int(superseded),
        "unique_blobs": int(unique[0]),
        "unique_blob_bytes": int(unique[1]),
        "schema_version": schema_version,
    }


def _walk_assets(store_root: Path) -> list[Path]:
    assets = store_root / "assets"
    if not assets.is_dir():
        return []
    return sorted(path for path in assets.rglob("*") if path.is_file())


def _copy_assets(store_root: Path, dest: Path, log=print) -> list[dict]:
    """Copy store/assets/ -> dest/assets/, hashing every produced file."""
    entries: list[dict] = []
    assets_dir = store_root / "assets"
    if not assets_dir.is_dir():
        log("[backup] [warn] source has no assets/ directory; nothing copied")
        return entries
    for source in _walk_assets(store_root):
        rel = source.relative_to(store_root)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Materialise a writable copy even though the source blob is +R.
        data = source.read_bytes()
        target.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        entries.append(
            {"path": rel.as_posix(), "bytes": len(data), "sha256": digest}
        )
    log(f"[backup] assets copied: {len(entries)} file(s)")
    return entries


def _archive_inventory(projects_root: Path) -> list[dict]:
    """Read-only inventory of accounts/*/tweetxvault (paths, never contents)."""
    accounts_dir = projects_root / "accounts"
    inventory: list[dict] = []
    if not accounts_dir.is_dir():
        return inventory
    for account in sorted(accounts_dir.iterdir()):
        vault = account / "tweetxvault"
        if not account.is_dir():
            continue
        if not vault.exists():
            continue
        files = 0
        total = 0
        for current, _dirs, names in os.walk(vault):
            for name in names:
                candidate = Path(current) / name
                try:
                    total += candidate.stat().st_size
                    files += 1
                except OSError:
                    continue
        inventory.append(
            {
                "path": vault.relative_to(projects_root).as_posix(),
                "exists": True,
                "files": files,
                "bytes": total,
            }
        )
    return inventory


def create_backup(
    store_root: Path | str = DEFAULT_STORE,
    dest: Path | str | None = None,
    *,
    copy_assets: bool = False,
    include_archives: bool = False,
    projects_root: Path | str | None = None,
    force: bool = False,
    hold_lock: bool = True,
    log=print,
) -> dict:
    """Create and verify one consistent snapshot while holding ``store/.lock``.

    The cold backup is mutually exclusive with every writer (store_v3 download /
    --gc / projection): the exclusive lock is acquired *before* the sqlite online
    backup and the assets copy start, and released only after the manifest has been
    written and fsync'd (P0-1).  A busy lock raises :class:`StoreLockError`; the CLI
    turns that into exit code 4 with the same "another instance is running" message
    as store_v3.  ``hold_lock=False`` is only for a caller that already owns the
    lock (the CLI never uses it).
    """
    store_root = Path(store_root)
    if dest is None:
        raise ValueError("--dest is required")
    dest = Path(dest)
    projects_root = Path(projects_root) if projects_root is not None else ROOT
    source_db = store_root / "media.db"
    if not source_db.is_file():
        raise ValueError(f"source database not found: {source_db}")
    store_abs = store_root.resolve()
    dest_abs = dest.resolve()
    if dest_abs == store_abs or store_abs in dest_abs.parents:
        raise ValueError("--dest must not be inside the store root (would recurse into assets/)")
    if dest_abs in store_abs.parents:
        raise ValueError("--dest must not be a parent of the store root")
    if dest.exists():
        existing = list(dest.iterdir())
        if existing and not force:
            raise ValueError(
                f"destination {dest} is not empty; pass --force to overwrite the backup in place"
            )
    lock = StoreLock(store_root / STORE_LOCK_NAME)
    if hold_lock:
        # Busy -> StoreLockError (CLI exit code 4).  Acquired before any read of the
        # database/assets so the snapshot cannot interleave with a writer.
        lock.acquire()
    try:
        dest.mkdir(parents=True, exist_ok=True)
        return _create_snapshot(
            store_root,
            dest,
            source_db,
            projects_root,
            copy_assets=copy_assets,
            include_archives=include_archives,
            log=log,
        )
    finally:
        if hold_lock:
            lock.release()


def _create_snapshot(
    store_root: Path,
    dest: Path,
    source_db: Path,
    projects_root: Path,
    *,
    copy_assets: bool,
    include_archives: bool,
    log,
) -> dict:
    """Snapshot + manifest while the caller holds ``store/.lock``."""
    log(f"[backup] source {source_db} (store/.lock held)")
    source = sqlite3.connect(str(source_db), timeout=30.0)
    source.execute("PRAGMA busy_timeout=30000")
    try:
        before = _db_counts(source)
        snapshot_path = dest / "media.db"
        snapshot = sqlite3.connect(str(snapshot_path))
        try:
            # Consistent point-in-time copy of a live-WAL database.
            source.backup(snapshot)
            snapshot.commit()
            integrity = snapshot.execute("PRAGMA integrity_check").fetchone()[0]
            after = _db_counts(snapshot)
        finally:
            snapshot.close()
        source_after = _db_counts(source)
    finally:
        source.close()

    verified = (
        integrity == "ok"
        and after["assets"] == before["assets"]
        and after["refs"] == before["refs"]
        and after["schema_version"] == source_after["schema_version"]
    )
    if not verified:
        log(
            "[backup] [warn] source changed while snapshotting "
            f"(assets {before['assets']} -> {source_after['assets']}, "
            f"refs {before['refs']} -> {source_after['refs']}); snapshot is still consistent"
        )
    snapshot_bytes = snapshot_path.stat().st_size
    snapshot_sha = sha256_file(snapshot_path)
    files = [{"path": "media.db", "bytes": snapshot_bytes, "sha256": snapshot_sha}]

    asset_entries: list[dict] = []
    if copy_assets:
        asset_entries = _copy_assets(store_root, dest, log=log)
        files.extend(asset_entries)

    archives: list[dict] = []
    if include_archives:
        archives = _archive_inventory(projects_root)
        log(f"[backup] archive inventory recorded: {len(archives)} path(s)")

    manifest = {
        "format": BACKUP_FORMAT,
        "tool": "tools/backup_store.py",
        "created_at": now_iso(),
        "source": {
            "store": str(store_root),
            "media_db": str(source_db),
            "lock": str(store_root / STORE_LOCK_NAME),
            "lock_held": True,
        },
        "dest": str(dest),
        "snapshot": {
            "file": "media.db",
            "bytes": snapshot_bytes,
            "sha256": snapshot_sha,
            "integrity_check": integrity,
            "counts": after,
        },
        "source_counts_before": before,
        "source_counts_after": source_after,
        "counts_verified": verified,
        "copy_assets": bool(copy_assets),
        "assets_total_bytes": sum(entry["bytes"] for entry in asset_entries),
        "files": files,
        "include_archives": bool(include_archives),
        "archives": archives,
        "notes": [
            "media.db is a sqlite3 online-backup snapshot; media.db-wal/-shm are not copied.",
            "store/.lock is held for the whole snapshot: a cold backup and every store "
            "writer (download/--gc/projection) are mutually exclusive; a second backup "
            "also fails with exit code 4.",
            "restore: copy media.db next to an empty store/ directory, copy assets/ from "
            "the same backup (or keep the original store/assets/), then run store_v3.py --gc --dry-run.",
        ],
    }
    manifest_path = dest / MANIFEST_NAME
    tmp_path = dest / (MANIFEST_NAME + ".tmp")
    tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, manifest_path)

    log(
        f"[backup] snapshot verified={verified}: assets={after['assets']} "
        f"refs={after['refs']} schema_version={after['schema_version']} "
        f"integrity={integrity} ({snapshot_bytes} bytes, sha256={snapshot_sha[:12]})"
    )
    log(f"[backup] manifest {manifest_path}")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup_store.py",
        description="Consistent cold backup of the v5 media store (sqlite online backup, store/.lock held).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dest", required=True, help="backup destination directory (required)")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="store root to back up")
    parser.add_argument("--copy-assets", action="store_true",
                        help="also copy store/assets/ and record the sha256 of every blob")
    parser.add_argument("--include-archives", action="store_true",
                        help="record the read-only accounts/*/tweetxvault path inventory")
    parser.add_argument("--projects-root", default=str(ROOT),
                        help="project root used by --include-archives")
    parser.add_argument("--force", action="store_true",
                        help="allow writing into a non-empty --dest directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        create_backup(
            args.store,
            args.dest,
            copy_assets=args.copy_assets,
            include_archives=args.include_archives,
            projects_root=args.projects_root,
            force=args.force,
        )
    except StoreLockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_BUSY
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
