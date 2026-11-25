"""Shared helpers for the 5-account tweetxvault experiment.

Layout:
    D:\\dshwork\\txv\\
        accounts\\acct-<n>\\tweetxvault\\{archive.lancedb, media}
        work\\accounts.json      account registry (cookies live here, never committed)
        work\\inventory\\         per-account media inventories
        store\\                   dedup demo store (blobs + hardlinks)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

def _default_root() -> Path:
    """Repo root: $TXV_HOME when set, otherwise the directory above tools/."""
    env = os.environ.get("TXV_HOME")
    return Path(env).expanduser().resolve() if env else Path(__file__).resolve().parent.parent


ROOT = _default_root()
ACCOUNTS_FILE = ROOT / "work" / "accounts.json"
INVENTORY_DIR = ROOT / "work" / "inventory"
WORK = ROOT / "work"


def account_dir(account_id: str) -> Path:
    return ROOT / "accounts" / account_id


def vault_dir(account_id: str) -> Path:
    """tweetxvault data dir (XDG_DATA_HOME=<accounts>/<id> -> <id>/tweetxvault)."""
    return account_dir(account_id) / "tweetxvault"


def db_dir(account_id: str) -> Path:
    return vault_dir(account_id) / "archive.lancedb"


def load_accounts() -> list[dict]:
    if not ACCOUNTS_FILE.exists():
        return []
    with open(ACCOUNTS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("accounts", data) if isinstance(data, dict) else data


def save_accounts(accounts: list[dict]) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)


def canonical_media_url(media_type: str | None, url: str | None) -> str | None:
    """Normalise a media URL the same way tweetxvault downloads it.

    Photos: force name=orig (the largest variant) and drop the rest of the query.
    Videos/gifs: keep as-is, minus cache-busting query keys.
    """
    if not url:
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if media_type == "photo" or parts.netloc.endswith("pbs.twimg.com"):
        keep = {"format": query["format"]} if "format" in query else {}
        if parts.netloc.endswith("pbs.twimg.com"):
            keep["name"] = "orig"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(keep), ""))
    for junk in ("tag", "_nc", "v", "cn"):
        query.pop(junk, None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def open_archive(account_id: str):
    """Open an account's LanceDB archive read-only; returns the table or None."""
    import lancedb

    path = db_dir(account_id)
    if not path.exists():
        return None
    db = lancedb.connect(str(path))
    names = db.table_names()
    if not names:
        return None
    return db.open_table("archive" if "archive" in names else names[0])


def iter_rows(account_id: str, where: str | None = None):
    table = open_archive(account_id)
    if table is None:
        return
    query = table.search()
    if where:
        query = query.where(where)
    yield from query.to_list()


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
