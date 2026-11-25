"""Hide archived media from other Windows apps (Explorer, gallery, search, some
cloud agents) by setting the Hidden + System + NotContentIndexed attributes.

What this does NOT do: it is not encryption and not an ACL. Any process running
as the same user can still read the bytes; cloud clients that ignore attributes
may still upload. It only removes the trees from normal indexing/browsing.

Usage:
    python tools/media_privacy.py --status
    python tools/media_privacy.py --on
    python tools/media_privacy.py --off
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLAG = ROOT / "work" / "privacy.json"
TARGETS = [
    ROOT / "store" / "assets",
    ROOT / "store" / "by-account",
    ROOT / "viewer" / "vault",
]

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_NOT_CONTENT_INDEXED = 0x2000
FLAGS = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_NOT_CONTENT_INDEXED
INVALID = 0xFFFFFFFF

kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None


def get_attrs(path: Path) -> int:
    if kernel32 is None:
        return 0
    return kernel32.GetFileAttributesW(str(path))


def set_attrs(path: Path, attrs: int) -> bool:
    if kernel32 is None:
        return False
    return bool(kernel32.SetFileAttributesW(str(path), attrs))


def walk(path: Path):
    for root, dirs, files in os.walk(path):
        for name in dirs:
            yield Path(root) / name
        for name in files:
            yield Path(root) / name


def is_hidden(path: Path) -> bool:
    attrs = get_attrs(path)
    return attrs != INVALID and bool(attrs & FILE_ATTRIBUTE_HIDDEN)


def write_flag(on: bool) -> None:
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    FLAG.write_text(
        '{"hidden": %s, "updated_at": "%s"}\n'
        % ("true" if on else "false", __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z")),
        encoding="utf-8",
    )


def apply(on: bool) -> int:
    if kernel32 is None:
        print("this feature is Windows-only")
        return 2
    changed = total = 0
    for target in TARGETS:
        if not target.exists():
            continue
        for item in walk(target):
            total += 1
            attrs = get_attrs(item)
            if attrs == INVALID:
                continue
            new = (attrs | FLAGS) if on else (attrs & ~FLAGS)
            if new != attrs and set_attrs(item, new):
                changed += 1
        # hide/unhide the root directory itself as well
        attrs = get_attrs(target)
        if attrs != INVALID:
            new = (attrs | FLAGS) if on else (attrs & ~FLAGS)
            if new != attrs and set_attrs(target, new):
                changed += 1
    write_flag(on)
    verb = "hidden" if on else "visible"
    print(f"{verb}: {changed} changed / {total} entries scanned")
    return 0


def status() -> int:
    if kernel32 is None:
        print("this feature is Windows-only")
        return 2
    for target in TARGETS:
        if not target.exists():
            print(f"{target.relative_to(ROOT)}: missing")
            continue
        total = hidden = 0
        size = 0
        for item in walk(target):
            total += 1
            if is_hidden(item):
                hidden += 1
            if item.is_file():
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
        mark = "HIDDEN" if total and hidden >= total else ("partly" if hidden else "VISIBLE")
        print(f"{target.relative_to(ROOT)}: {mark} ({hidden}/{total} entries, "
              f"{size / 1024 ** 3:.2f} GB)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", action="store_true", help="set Hidden+System+NotContentIndexed")
    group.add_argument("--off", action="store_true", help="clear those attributes")
    group.add_argument("--status", action="store_true", help="report current state")
    args = ap.parse_args()
    if args.status:
        return status()
    return apply(args.on)


if __name__ == "__main__":
    sys.exit(main())
