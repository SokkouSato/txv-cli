# patches/

`windows-support.patch` is a small compatibility patch for
[tweetxvault](https://github.com/lhl/tweetxvault) (Apache-2.0) that makes the
collector run on Windows:

- `fcntl` → `msvcrt` file locking in `sync.py`
- `resource` / `RLIMIT_NOFILE` guard in `cli.py`
- `strftime("%-d")` / `%-I` replacements in `cli.py`
- `platformdirs` `appauthor=False` in `config.py`
- Firefox `profiles.ini` default path on Windows/macOS in `auth/firefox.py`

Apply it inside a tweetxvault checkout:

```bash
git clone https://github.com/lhl/tweetxvault.git
cd tweetxvault
git apply /path/to/windows-support.patch
uv sync
```

The patch is derivative work of tweetxvault and remains under its
Apache-2.0 license. This repository's own code is MIT (see `../LICENSE`).
