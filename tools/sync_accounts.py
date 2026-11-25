"""Run tweetxvault sync per X account with isolated data dirs.

Each account gets its own XDG_DATA_HOME so the archive-owner guardrail is happy
(one archive == one X account). Media downloads are always skipped: this phase
only records what *would* need downloading.

Network flakiness (proxy drops, ``httpx.RemoteProtocolError`` / ``ConnectError``,
read/connect timeouts, HTTP 429/5xx) is retried per collection with exponential
backoff so one dropped connection cannot abort an account's whole sync. Each
retry (and the reason for it) is printed and appended to the per-account log;
``work/sync_status.json`` records how many attempts each collection needed.

Usage:
    python sync_accounts.py --limit 3                 # smoke test, 3 pages
    python sync_accounts.py                           # full likes + bookmarks
    python sync_accounts.py --collections likes --parallel 2
    python sync_accounts.py --retries 8 --backoff 10  # 10/20/40/... seconds
    python sync_accounts.py --keep-creds              # keep plaintext creds after a clean run
    python sync_accounts.py --cleanup-creds           # always delete plaintext creds, even on failure

After a run in which every account/collection exited 0, work/accounts.json and
work/cookies*.json are deleted (override with --keep-creds). A failed run keeps
them so it can be resumed, and prints a loud reminder that they are live X
credentials until deleted. Pass --cleanup-creds to delete them at the end of
the run regardless of success or failure (it conflicts with --keep-creds).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, WORK, account_dir, load_accounts  # noqa: E402

def _find_tweetxvault() -> str:
    """$TWEETXVAULT_EXE, then PATH, then a local .venv install."""
    env = os.environ.get("TWEETXVAULT_EXE")
    if env:
        return env
    found = shutil.which("tweetxvault")
    if found:
        return found
    return str(ROOT / ".venv" / "Scripts" / "tweetxvault.exe")


EXE = _find_tweetxvault()
LOGS = WORK / "logs"
STATUS = WORK / "sync_status.json"
print_lock = threading.Lock()

DEFAULT_RETRIES = 5
DEFAULT_BACKOFF = "15"  # seconds; doubles each retry -> 15/30/60/120/240

# Signs that a failed run is worth retrying: transient network/proxy problems
# and rate limiting / server-side errors. Numeric matches require HTTP context
# so tweet ids or "page 504" progress lines never trigger a retry.
_RETRYABLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("RemoteProtocolError", re.compile(r"RemoteProtocolError")),
    ("ConnectError", re.compile(r"ConnectError")),
    ("ReadTimeout", re.compile(r"ReadTimeout")),
    ("ConnectTimeout", re.compile(r"ConnectTimeout")),
    ("WriteTimeout", re.compile(r"WriteTimeout")),
    ("PoolTimeout", re.compile(r"PoolTimeout")),
    ("TimeoutException", re.compile(r"TimeoutException")),
    ("server disconnected",
     re.compile(r"Server disconnected without sending a response", re.IGNORECASE)),
    ("HTTP 429/5xx", re.compile(r"\bHTTP(?:/\d(?:\.\d)?)?\s+(?:429|5\d\d)\b", re.IGNORECASE)),
    ("status 429/5xx",
     re.compile(r"\bstatus(?:_?code)?\s*[=:]\s*(?:429|5\d\d)\b", re.IGNORECASE)),
    ("429/5xx status text",
     re.compile(r"\b(?:429|5\d\d)\s+(?:Too Many Requests|Internal Server Error|"
                r"Bad Gateway|Service Unavailable|Gateway Time-?out)\b", re.IGNORECASE)),
    ("rate limited", re.compile(r"\btoo many requests\b|\brate[ _-]?limit(?:ed)?\b",
                                re.IGNORECASE)),
    ("HTTP 5xx text", re.compile(r"\b(?:internal server error|bad gateway|"
                                 r"service unavailable|gateway time-?out)\b", re.IGNORECASE)),
    ("proxy/connection dropped",
     re.compile(r"\b(?:ProxyError|ConnectionResetError|RemoteDisconnected|IncompleteRead|"
                r"upstream connect error|connection reset by peer|temporarily unavailable)\b",
                re.IGNORECASE)),
]


def retryable_reason(output: str) -> str | None:
    """Return a short reason if the run output looks like a transient failure."""
    if not output:
        return None
    for label, pattern in _RETRYABLE_PATTERNS:
        match = pattern.search(output)
        if match:
            snippet = " ".join(match.group(0).split())[:120]
            if label.lower() in snippet.lower():
                return snippet
            return f"{label}: {snippet}"
    return None


def retry_delays(backoff: str, retries: int) -> list[float]:
    """Build the sleep schedule for `retries` retries.

    A single value is an exponential base (15 -> 15/30/60/...); a comma-separated
    list is used verbatim (and extended exponentially if it is too short).
    """
    chunks = [c.strip() for c in str(backoff).replace(";", ",").split(",") if c.strip()]
    if not chunks:
        raise ValueError("--backoff must be a number or a comma-separated list of numbers")
    try:
        values = [float(c) for c in chunks]
    except ValueError as exc:
        raise ValueError(f"--backoff is not numeric: {backoff!r}") from exc
    if any(v < 0 for v in values):
        raise ValueError("--backoff values must be >= 0")
    if retries <= 0:
        return []
    if len(values) == 1:
        return [values[0] * (2 ** i) for i in range(retries)]
    delays: list[float] = []
    for i in range(retries):
        if i < len(values):
            delays.append(values[i])
        else:
            delays.append(values[-1] * (2 ** (i - len(values) + 1)))
    return delays


def fmt_delay(seconds: float) -> str:
    return f"{seconds:g}"


def account_env(acct: dict) -> dict:
    aid = acct["id"]
    base = account_dir(aid)
    env = dict(os.environ)
    env.update(
        {
            "XDG_DATA_HOME": str(base),
            "XDG_CONFIG_HOME": str(WORK / "cfg" / aid),
            "XDG_CACHE_HOME": str(WORK / "cache" / aid),
            "TEMP": str(WORK / "tmp"),
            "TMP": str(WORK / "tmp"),
            "TWEETXVAULT_AUTH_TOKEN": acct["auth_token"],
            "TWEETXVAULT_CT0": acct["ct0"],
            "TWEETXVAULT_USER_ID": acct["user_id"],
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return env


def _append_log(log_path: Path, text: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(text)
        log.flush()


def run_one(acct: dict, collections: list[str], limit: int | None, extra: list[str],
            retries: int = DEFAULT_RETRIES,
            backoff: str = DEFAULT_BACKOFF) -> dict:
    aid = acct["id"]
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"{aid}.log"
    delays = retry_delays(backoff, retries)
    env = account_env(acct)
    results = []
    for collection in collections:
        cmd = [str(EXE), "sync", collection, "--skip-media", "--skip-unfurl",
               "--skip-threads", "--skip-articles", "--skip-enrich", *extra]
        if limit:
            cmd += ["--limit", str(limit)]
        started = time.time()
        attempts = 0
        retry_reasons: list[str] = []
        exit_code = -1
        output = ""
        while True:
            attempts += 1
            attempt_started = time.time()
            stdout = stderr = ""
            try:
                proc = subprocess.run(cmd, env=env, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace")
                exit_code = proc.returncode
                stdout, stderr = proc.stdout or "", proc.stderr or ""
            except OSError as exc:  # e.g. exe vanished mid-run; not retryable
                exit_code = 127
                stderr = f"{type(exc).__name__}: {exc}"
            elapsed = time.time() - attempt_started
            output = stdout + stderr
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            _append_log(log_path,
                        f"\n===== {stamp} {' '.join(cmd)} "
                        f"(attempt {attempts}/{retries + 1})\n{stdout}{stderr}")
            reason = retryable_reason(output) if exit_code != 0 else None
            if exit_code == 0 or reason is None or attempts > retries:
                status = "ok" if exit_code == 0 else "final"
                _append_log(log_path,
                            f"\n----- attempt {attempts}/{retries + 1} exit {exit_code} "
                            f"in {elapsed:.1f}s ({status})\n")
                if exit_code != 0 and reason is not None:
                    with print_lock:
                        print(f"[{aid}/{collection}] FAILED after {attempts} attempt(s): "
                              f"{reason}", flush=True)
                break
            delay = delays[attempts - 1]
            retry_reasons.append(reason)
            _append_log(log_path,
                        f"\n----- attempt {attempts}/{retries + 1} exit {exit_code} "
                        f"in {elapsed:.1f}s retryable ({reason}); "
                        f"sleeping {fmt_delay(delay)}s before retry {attempts}/{retries} -----\n")
            with print_lock:
                print(f"[{aid}/{collection}] attempt {attempts}/{retries + 1} "
                      f"exit={exit_code} retryable ({reason}); "
                      f"retry in {fmt_delay(delay)}s", flush=True)
            if delay > 0:
                time.sleep(delay)
        total = time.time() - started
        tail = output.strip().splitlines()[-3:]
        suffix = f" attempts={attempts}" if attempts > 1 else ""
        with print_lock:
            print(f"[{aid}/{collection}] exit={exit_code} {total:.0f}s{suffix} "
                  f"| {' / '.join(tail)[:220]}", flush=True)
        result = {
            "collection": collection,
            "exit": exit_code,
            "seconds": round(total, 1),
            "attempts": attempts,
            "tail": tail,
        }
        if retry_reasons:
            result["retry_reasons"] = retry_reasons
        results.append(result)
    return {
        "id": aid,
        "attempts": sum(r["attempts"] for r in results),
        "results": results,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _log_cleanup(work: Path, message: str) -> None:
    """Best-effort run-level cleanup log (stdout already carries the same line)."""
    try:
        logs = work / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "cleanup.log", "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError as exc:
        print(f"[cleanup] log write failed ({type(exc).__name__}); ignored", flush=True)


def _announce_cleanup(work: Path, message: str) -> None:
    print(message, flush=True)
    _log_cleanup(work, message)


def cred_targets(work: Path) -> list[Path]:
    """Every plaintext-credential file this sync flow may leave behind."""
    return [work / "accounts.json", *sorted(work.glob("cookies*.json"))]


def cleanup_credentials(work: Path) -> list[Path]:
    """Delete plaintext cookie registries (accounts.json + cookies*.json)."""
    deleted: list[Path] = []
    for path in cred_targets(work):
        try:
            if path.is_file():
                path.unlink()
                deleted.append(path)
        except OSError as exc:
            print(f"[cleanup] cannot delete {path.name} ({type(exc).__name__}); skipped",
                  flush=True)
    return deleted


def maybe_cleanup_credentials(work: Path, all_succeeded: bool,
                              keep_creds: bool,
                              cleanup_creds: bool = False) -> list[Path]:
    """Apply the run-end plaintext-credential policy.

    * ``--cleanup-creds``: delete no matter how the run ended;
    * ``--keep-creds``: never delete;
    * default: delete only when every account/collection exited 0.

    Whenever a failed run still leaves ``work/accounts.json`` / ``work/cookies*.json``
    on disk, a loud reminder is printed (and appended to ``work/logs/cleanup.log``):
    those files hold live X credentials until removed.
    """
    if cleanup_creds:
        deleted = cleanup_credentials(work)
        names = ", ".join(p.name for p in deleted) or "没有找到可清理的文件"
        _announce_cleanup(work, f"[cleanup] --cleanup-creds：本次运行"
                                f"{'成功' if all_succeeded else '失败'}，已删除明文凭据: {names}")
        if not all_succeeded:
            _announce_cleanup(work, "[cleanup] 注意：本次运行失败但明文凭据已被删除；"
                                    "重跑前需要重新导入 cookie")
        remaining = [p for p in cred_targets(work) if p.is_file()]
        if remaining:
            _announce_cleanup(work, "[cleanup] !!! 仍有明文凭据未能删除: "
                                    f"{', '.join(p.name for p in remaining)}，请手动删除")
        return deleted
    if keep_creds:
        _announce_cleanup(work, "[cleanup] --keep-creds 已指定：保留 work/accounts.json 与 "
                                "work/cookies*.json")
        deleted: list[Path] = []
    elif all_succeeded:
        deleted = cleanup_credentials(work)
        if deleted:
            names = ", ".join(p.name for p in deleted)
            _announce_cleanup(work, f"已清理明文凭据（可用 --keep-creds 保留）: {names}")
        else:
            _announce_cleanup(work, "已清理明文凭据（可用 --keep-creds 保留）: 没有找到可清理的文件")
    else:
        _announce_cleanup(work, "[cleanup] 本次有失败：保留明文凭据以便续跑")
        deleted = []
    remaining = [p for p in cred_targets(work) if p.is_file()]
    if not all_succeeded and remaining:
        _announce_cleanup(
            work,
            "[cleanup] !!! 同步失败，明文凭据仍在 work/accounts.json，"
            "请手动删除或加 --cleanup-creds 重跑 !!!")
    return deleted


def _run(args: argparse.Namespace, delays: list[float], outcome: dict) -> int:
    accounts = load_accounts()
    known_ids = [a["id"] for a in accounts]
    if args.accounts:
        wanted = {a.strip() for a in args.accounts.split(",") if a.strip()}
        accounts = [a for a in accounts if a["id"] in wanted]
    if not accounts:
        if args.accounts:
            print(f"no accounts match --accounts {args.accounts!r}; "
                  f"known ids in work/accounts.json: "
                  f"{', '.join(known_ids) or '(none - file empty or missing)'}")
        else:
            print("no accounts in work/accounts.json")
        return 2
    if not EXE.exists():
        print(f"tweetxvault.exe not found: {EXE}")
        return 2

    collections = [c for c in args.collections.split(",") if c]
    extra = args.extra.split() if args.extra else []
    (WORK / "tmp").mkdir(parents=True, exist_ok=True)

    print(f"syncing {len(accounts)} account(s) x {collections}, limit={args.limit or 'full'}",
          flush=True)
    if args.retries:
        print(f"retry policy: retries={args.retries}, backoff="
              f"{'/'.join(fmt_delay(d) for d in delays)}s", flush=True)
    else:
        print("retry policy: disabled (--retries 0)", flush=True)
    out = []
    if args.parallel <= 1:
        for acct in accounts:
            out.append(run_one(acct, collections, args.limit or None, extra,
                               retries=args.retries, backoff=args.backoff))
    else:
        queue = list(accounts)
        lock = threading.Lock()

        def worker() -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    acct = queue.pop(0)
                out.append(run_one(acct, collections, args.limit or None, extra,
                                   retries=args.retries, backoff=args.backoff))

        threads = [threading.Thread(target=worker) for _ in range(args.parallel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    STATUS.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if STATUS.exists():
        try:
            history = json.loads(STATUS.read_text(encoding="utf-8")).get("runs", [])
        except json.JSONDecodeError:
            history = []
    history.append({"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "limit": args.limit, "collections": collections,
                    "retries": args.retries, "backoff": args.backoff,
                    "results": out})
    STATUS.write_text(json.dumps({"runs": history[-20:]}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    all_ok = all(r["exit"] == 0 for o in out for r in o["results"])
    outcome["all_ok"] = all_ok
    return 0 if all_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="pages per collection (0 = full)")
    ap.add_argument("--collections", default="likes,bookmarks")
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--accounts", default="")
    ap.add_argument("--extra", default="", help="extra flags passed to sync")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                    help=f"retries per collection on retryable network errors "
                         f"(default {DEFAULT_RETRIES})")
    ap.add_argument("--backoff", default=DEFAULT_BACKOFF,
                    help="base backoff seconds, doubled each retry "
                         "(default 15 -> 15/30/60/120/240), or a comma-separated list")
    ap.add_argument("--keep-creds", action="store_true",
                    help="keep work/accounts.json and work/cookies*.json after a "
                         "fully successful run (default: delete them as soon as "
                         "every account/collection exited 0)")
    ap.add_argument("--cleanup-creds", action="store_true",
                    help="delete work/accounts.json and work/cookies*.json at the end "
                         "of the run no matter whether it succeeded or failed "
                         "(mutually exclusive with --keep-creds)")
    args = ap.parse_args()

    if args.cleanup_creds and args.keep_creds:
        ap.error("--cleanup-creds 与 --keep-creds 冲突：不能同时指定")
    if args.retries < 0:
        ap.error("--retries must be >= 0")
    try:
        delays = retry_delays(args.backoff, args.retries)
    except ValueError as exc:
        ap.error(str(exc))

    outcome = {"all_ok": False}
    try:
        return _run(args, delays, outcome)
    finally:
        # The credential policy is applied on every exit path (missing accounts,
        # missing exe, crash) so --cleanup-creds can never be skipped silently.
        maybe_cleanup_credentials(WORK, outcome["all_ok"], args.keep_creds,
                                  args.cleanup_creds)


if __name__ == "__main__":
    raise SystemExit(main())
