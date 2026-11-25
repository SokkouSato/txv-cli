# txv — a local X (Twitter) archive toolkit

[简体中文](README.md) | English

Archive your X bookmarks, likes and own tweets locally: media is stored by content
hash, and a bundled offline web UI browses it like X — no external requests.

- **Collect**: reuses [tweetxvault](https://github.com/lhl/tweetxvault) for timeline
  fetching (`sync` writes metadata only, never media)
- **Store**: SQLite state tables + content-addressed blobs (`sha256` names,
  write-once, read-only, atomic replace)
- **Browse**: bundled offline viewer (timeline / media wall / author pages / stats)
- **Maintain**: `verify`, `gc`, consistent `backup`, `hide`

## Features

- One tweetxvault archive per X account (`accounts/<id>/tweetxvault`), matching
  tweetxvault's owner guardrail
- Media identity is `(media_key, tier)`: bookmarks keep the best bitrate, likes keep
  mid — repeated references never download twice
- Content addressing dedupes bytes for free; `--gc` only reclaims superseded rows
  nothing references
- Process lock (`store/.lock`) + atomic writes + full sha256 `--verify`
- Cold backup through `sqlite3.Connection.backup()`, mutually exclusive with writers
- Chrome extension reads session cookies (the only reliable way under App-Bound
  Encryption); local bridge enforces a token + a fixed extension ID
- `hide` marks media trees Hidden+System+NotContentIndexed so galleries, search and
  some cloud agents skip them

## Layout

```
txv.py                 unified CLI
tools/                 storage, export, backup, security, stats scripts
viewer/                offline web UI (serve.py + web/)
ext/                   Chrome extension (reads auth_token / ct0 / auth_multi)
patches/               optional Windows patch for tweetxvault
```

## Install

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/) (recommended) or pip,
and Chrome logged into X.

```bash
# 1) dependencies
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt      # Windows
# .venv/bin/python -m pip install -r requirements.txt        # Linux/macOS

# 2) collector (upstream project)
pip install tweetxvault
#    on Windows, apply patches/windows-support.patch to a tweetxvault checkout instead

# 3) generate a fixed extension ID (writes ext/manifest.json key + work/extension_id.txt)
.venv/Scripts/python tools/setup_extension.py
```

## Quick start

```powershell
$py = ".\.venv\Scripts\python.exe"

# 1. cookie bridge (creates work/bridge_token.txt on first run)
& $py tools/cookie_bridge.py --port 8791

# 2. chrome://extensions -> Developer mode -> Load unpacked -> pick ext/
#    paste work/bridge_token.txt into the popup's "bridge token", log into X, switch account once

# 3. fetch metadata only
& $py txv.py sync --collections likes,bookmarks

# 4. download registered media
& $py txv.py download --max-mb 3000

# 5. export viewer data and serve
& $py txv.py serve --port 8799
```

Open <http://127.0.0.1:8799/>.

## CLI

| command | purpose |
|---|---|
| `sync` | fetch new bookmarks/likes (needs X cookies) |
| `download` | download registered media (idempotent) |
| `export` | rebuild viewer data (hardlinks + avatars + video posters) |
| `serve` | export and serve the offline UI |
| `backup` | cold backup (`--copy-assets` includes media) |
| `refresh-counts` | manual, rate-limited refresh of recent engagement counts |
| `verify` | consistency check (`--quick` = existence + size) |
| `gc` | reclaim superseded assets / orphan blobs |
| `hide` | hide/unhide media trees (`--on/--off/--status`) |
| `stats` | print totals |

```powershell
& $py txv.py --help
& $py txv.py download --max-mb 0
& $py txv.py verify --quick
& $py txv.py hide --on
& $py txv.py backup --dest D:\backup\x --copy-assets --include-archives
```

## Runtime layout

```
accounts/<acct>/tweetxvault/   LanceDB archive (read-only)
store/
  media.db                     SQLite: assets + refs + download state
  assets/<xx>/<sha>.<ext>      media originals (read-only, content-addressed)
  by-account/<acct>/<collection>/<YYYY-MM>/   readable hardlinks (rebuildable)
viewer/
  data/timeline.ndjson         viewer data
  data/avatars/                author avatars
  vault/media/<tweet_id>/      media hardlinks + video posters
  state.db                     read state / favorites / local likes
work/                          config, tokens, logs (.gitignore)
```

The cold-backup trio: `store/assets/` + `store/media.db` (snapshot from `backup`) +
`accounts/<id>/tweetxvault/`. Everything under `by-account/`, `viewer/vault/` and
`viewer/data/` is a derived, rebuildable view.

## Security and privacy

- The archive and the web UI are fully offline; only `sync` / `refresh-counts` use X
  credentials.
- `work/accounts.json`, `work/bridge_token.txt` and `work/extension_key.pem` are
  local secrets and are gitignored; the bridge tries to tighten their ACLs
  (`icacls`) to the current user.
- Local likes/favorites only write `viewer/state.db`; nothing is sent back to X.
- `txv.py hide` only sets hidden attributes — it is not encryption. Use
  BitLocker/VeraCrypt for real confidentiality.

## Known limitations

1. No link revalidation (`--revalidate` is not implemented); stored assets never
   revisit their source URL.
2. Engagement counts are a snapshot; refresh manually with `refresh-counts`
   (rate-limited).
3. Single writer: do not run two writers against the same `store/`.
4. One archive per X account; use separate `accounts/<id>` directories for multiple
   accounts.
5. The extension ID is bound to the `key` in `ext/manifest.json`; after `--force`
   rotation reload the extension and update `work/extension_id.txt`.

## Development

```bash
python tools/store_v3.py --self-test     # offline self-test (fake HTTP server)
```

## License

MIT (see `LICENSE`). The bundled `patches/` are derivative work of
tweetxvault (Apache-2.0); see `patches/README.md`.
