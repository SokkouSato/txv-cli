"""txv — one CLI for the whole local X archive.

Subcommands
    sync           fetch new bookmarks/likes (metadata only, needs X cookies)
    download       download media for everything registered in the store
    export         rebuild the viewer data (hardlinks + avatars + posters)
    serve          export (unless --no-export) and serve the offline web UI
    backup         consistent cold backup of store + archives
    refresh-counts bounded head re-sync to refresh recent counts, then export
    verify         consistency check of the media store
    gc             reclaim superseded assets / orphan blobs
    hide           hide media from Explorer/gallery/search (reversible)
    stats          print archive totals (and regenerate REPORT.md)

The web server and the viewer export never touch X credentials: sync is the only
subcommand that needs them.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = Path(sys.executable)
TOOLS = ROOT / "tools"
VIEWER = ROOT / "viewer"


def run(cmd: list[str], *, quiet: bool = False) -> int:
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    proc = subprocess.run([str(c) for c in cmd])
    return proc.returncode


def py(script: str, *args: str) -> int:
    return run([PY, TOOLS / script, *args])


def cmd_sync(args: argparse.Namespace) -> int:
    extra = ["--accounts", args.accounts] if args.accounts else []
    extra += ["--collections", args.collections]
    extra += ["--parallel", str(args.parallel)]
    if args.limit:
        extra += ["--limit", str(args.limit)]
    return py("sync_accounts.py", *extra)


def privacy_enabled() -> bool:
    import json

    flag = ROOT / "work" / "privacy.json"
    try:
        return bool(json.loads(flag.read_text(encoding="utf-8")).get("hidden"))
    except (OSError, ValueError):
        return False


def rehide_if_enabled() -> None:
    if privacy_enabled():
        print("[privacy] media hiding is on; applying attributes to new files")
        py("media_privacy.py", "--on")


def cmd_download(args: argparse.Namespace) -> int:
    rc = py("store_v3.py", "--limit", "0", "--max-mb", str(args.max_mb),
            "--policy", args.policy, "--project", args.project)
    if rc == 0:
        rehide_if_enabled()
    return rc


def cmd_export(args: argparse.Namespace) -> int:
    extra = ["--out", str(VIEWER)]
    if args.no_avatars:
        extra.append("--no-avatars")
    if args.no_posters:
        extra.append("--no-posters")
    rc = py("export_viewer.py", *extra)
    if rc == 0:
        rehide_if_enabled()
    return rc


def cmd_serve(args: argparse.Namespace) -> int:
    if not args.no_export:
        rc = cmd_export(args)
        if rc != 0:
            print("export failed; serving existing snapshot anyway", file=sys.stderr)
    return run([PY, ROOT / "viewer" / "serve.py",
                "--host", args.host, "--port", str(args.port),
                "--data", VIEWER / "data", "--vault", VIEWER / "vault",
                "--state", VIEWER / "state.db"])


def cmd_backup(args: argparse.Namespace) -> int:
    dest = Path(args.dest) if args.dest else ROOT / "work" / f"backup-{time.strftime('%Y%m%d-%H%M%S')}"
    extra = ["--dest", str(dest)]
    if args.copy_assets:
        extra.append("--copy-assets")
    if args.include_archives:
        extra.append("--include-archives")
    return py("backup_store.py", *extra)


def cmd_refresh_counts(args: argparse.Namespace) -> int:
    """Bounded head re-sync: refreshes the newest N pages of likes+bookmarks.

    Counting only changes for recent tweets, so this is a manual, rate-limited
    operation; it reuses the sync page delay and retry logic.
    """
    pages = args.pages
    if args.months:
        # ~20 tweets per page; ~2 posts/day is a safe over-estimate for likes.
        pages = max(1, min(args.pages or 60, int(args.months * 30 * 2 / 20)))
    print(f"[refresh-counts] re-syncing head pages={pages} (rate-limited by page delay)")
    rc = py("sync_accounts.py", "--collections", "likes,bookmarks", "--limit", str(pages),
            "--parallel", "1")
    if rc != 0:
        print("[refresh-counts] sync failed; keeping existing data", file=sys.stderr)
        return rc
    return cmd_export(args)


def cmd_verify(args: argparse.Namespace) -> int:
    extra = ["--verify"] + (["--quick"] if args.quick else [])
    return py("store_v3.py", *extra)


def cmd_gc(args: argparse.Namespace) -> int:
    extra = ["--gc"] + (["--dry-run"] if args.dry_run else [])
    return py("store_v3.py", *extra)


def cmd_privacy(args: argparse.Namespace) -> int:
    flag = "--status" if args.status else ("--off" if args.off else "--on")
    return py("media_privacy.py", flag)


def cmd_stats(args: argparse.Namespace) -> int:
    rc = py("stats.py")
    if rc == 0:
        rc = py("report_v3.py")
    return rc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="txv", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="fetch new bookmarks/likes (needs X cookies)")
    p.add_argument("--accounts", default="", help="comma separated acct ids (default: all)")
    p.add_argument("--collections", default="likes,bookmarks")
    p.add_argument("--parallel", type=int, default=1)
    p.add_argument("--limit", type=int, default=0, help="pages per collection (0=all)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("download", help="download media for registered assets")
    p.add_argument("--max-mb", type=float, default=3000, help="0 = unlimited")
    p.add_argument("--policy", default="default", choices=["default", "recommended", "best", "low"])
    p.add_argument("--project", default="none", choices=["none", "hardlink", "copy"])
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("export", help="rebuild viewer data (avatars + posters)")
    p.add_argument("--no-avatars", action="store_true")
    p.add_argument("--no-posters", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("serve", help="serve the offline viewer")
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-export", action="store_true", help="serve the existing snapshot")
    p.add_argument("--no-avatars", action="store_true")
    p.add_argument("--no-posters", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("backup", help="cold backup of store + archives")
    p.add_argument("--dest", default="")
    p.add_argument("--copy-assets", action="store_true", help="copy blobs (full restore point)")
    p.add_argument("--include-archives", action="store_true", help="record archive paths")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("refresh-counts", help="bounded head re-sync to refresh recent counts")
    p.add_argument("--months", type=int, default=3, help="how far back to refresh (rough)")
    p.add_argument("--pages", type=int, default=0, help="override page count")
    p.add_argument("--no-avatars", action="store_true")
    p.add_argument("--no-posters", action="store_true")
    p.set_defaults(func=cmd_refresh_counts)

    p = sub.add_parser("verify", help="check every done asset (file/size/sha256)")
    p.add_argument("--quick", action="store_true")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("gc", help="reclaim superseded assets / orphan blobs")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_gc)

    p = sub.add_parser("hide", help="hide media from Explorer/gallery/search (Windows attributes)")
    p.add_argument("--off", action="store_true", help="clear the hidden attributes")
    p.add_argument("--status", action="store_true", help="report current visibility")
    p.set_defaults(func=cmd_privacy)

    p = sub.add_parser("stats", help="print totals and regenerate REPORT.md")
    p.set_defaults(func=cmd_stats)
    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
