"""Local ingest server for the X Cookie Bridge Chrome extension.

Each capture carries the *active* X account (auth_token + ct0 + twid) plus the
auth_multi list of other accounts. Because ct0 is bound to the active session in
Chrome, a full 5-account capture needs one click per account switch; this server
merges every capture into work/accounts.json keyed by user_id.

Hardened security posture (this process holds live credentials):
  * binds 127.0.0.1 only;
  * a shared token is *mandatory*: it comes from --token, else the token file
    (--token-file, default work/bridge_token.txt). If none exists a fresh
    32-byte random token is generated, written to that file (best-effort ACL
    tightening, failures ignored) and only its path is printed - never the
    value. There is no token-less mode;
  * the token file is ACL-tightened to the current user whenever it is read or
    written via harden_acl() (icacls /inheritance:r /grant:r "<user>":(R,W));
    a failure is one log line and never aborts startup;
  * a fixed extension ID is *mandatory* in the same way: work/extension_id.txt
    (or --extension-id) must hold the 32-char Chrome id derived from the RSA
    "key" in ext/manifest.json. POST /ingest only accepts the exact Origin
    chrome-extension://<id> and an X-Bridge-Token that matches in constant time
    (hmac.compare_digest); anything else is 403 and logged without the value;
  * --allow-cli-no-origin is the only escape hatch, for local CLI tests: it
    accepts requests that carry *no* Origin header (a wrong Origin is still
    rejected) and the token stays mandatory. Off by default; startup prints a
    WARNING when it is enabled. Never use it with real cookies;
  * request body <= 256 KiB (413 otherwise); only POST /ingest, GET / and
    GET /health are routed (plus CORS preflight OPTIONS /ingest), else 404;
  * every write of a credential-bearing file under work/ — accounts.json,
    cookies_raw.json, cookies.json — is immediately followed by
    harden_acl(path): icacls /inheritance:r /grant:r "<user>":(R,W) on Windows,
    chmod 600 elsewhere. A missing file is skipped; a failure is exactly one log
    line and never aborts the request (the file then keeps whatever ACL it
    inherited). Same-user processes can still read these files, so the ACL is
    defence in depth rather than an access boundary;
  * logs never contain cookie values or the bridge token.

GET  /             -> {"accounts": [...], "missing_ct0": [...]}
GET  /health       -> {"ok": true, ...}
POST /ingest       -> merge payload, return progress

Usage:
    python cookie_bridge.py                          # 127.0.0.1:8791, token+origin required
    python cookie_bridge.py --port 8792              # test instance (own token file!)
    python cookie_bridge.py --work-dir <tmp>         # isolated work dir for tests
    python cookie_bridge.py --token <secret>         # explicit token (never logged)
    python cookie_bridge.py --token-file <tmp>/tok   # ACL-tightened token file
    python cookie_bridge.py --allow-cli-no-origin    # TEST ONLY: missing Origin ok, token still required
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import WORK as DEFAULT_WORK  # noqa: E402

PORT = 8791
MAX_BODY = 256 * 1024  # 256 KiB
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")

WORK: Path = DEFAULT_WORK
ACCOUNTS_FILE: Path = WORK / "accounts.json"
RAW: Path = WORK / "cookies_raw.json"
PARSED: Path = WORK / "cookies.json"

TOKEN: str | None = None
TOKEN_SOURCE: str = "none"
EXTENSION_ID: str | None = None
ALLOW_CLI_NO_ORIGIN = False


def set_work_dir(work: Path) -> None:
    """Point every runtime file at `work` (tests use a throw-away directory)."""
    global WORK, ACCOUNTS_FILE, RAW, PARSED
    WORK = work
    ACCOUNTS_FILE = WORK / "accounts.json"
    RAW = WORK / "cookies_raw.json"
    PARSED = WORK / "cookies.json"


def parse_auth_multi(raw: str) -> list[dict]:
    """auth_multi looks like '"uid:token|uid:token"' (sometimes base64/url-encoded)."""
    if not raw:
        return []
    import base64

    cands = [raw, urllib.parse.unquote(raw), raw.strip().strip('"')]
    for c in list(cands):
        pad = "=" * (-len(c) % 4)
        for dec in ("b64", "urlsafe"):
            try:
                b = base64.b64decode(c + pad) if dec == "b64" else base64.urlsafe_b64decode(c + pad)
                cands.append(b.decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                pass
    out, seen = [], set()
    for cand in cands:
        for part in cand.replace("|", "\n").splitlines():
            part = part.strip().strip('"')
            if ":" not in part:
                continue
            uid, _, token = part.partition(":")
            uid, token = uid.strip().strip('"'), token.strip().strip('"')
            if uid.isdigit() and len(token) >= 20 and token not in seen:
                seen.add(token)
                out.append({"user_id": uid, "auth_token": token})
    return out


def active_uid(twid: str) -> str:
    if not twid:
        return ""
    decoded = urllib.parse.unquote(twid)
    return decoded.split("=", 1)[-1].strip()


def load_accounts() -> list[dict]:
    if ACCOUNTS_FILE.exists():
        try:
            data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            return data.get("accounts", []) if isinstance(data, dict) else data
        except json.JSONDecodeError:
            return []
    return []


def validate_capture(payload: dict) -> None:
    """Reject malformed captures before anything is written to disk."""
    cookies = payload.get("cookies") or []
    if not isinstance(cookies, list):
        raise ValueError("cookies must be a list")
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError("cookie entries must be objects")
        for key in ("name", "host", "value"):
            if key in cookie and not isinstance(cookie[key], str):
                raise ValueError(f"cookie {key} must be a string")


def merge_capture(payload: dict) -> tuple[list[dict], dict]:
    cookies = payload.get("cookies") or []
    x = {c["name"]: c.get("value", "") for c in cookies if "x.com" in (c.get("host") or "")}
    ct0 = x.get("ct0", "")
    token = x.get("auth_token", "")
    uid = active_uid(x.get("twid", ""))
    multi = parse_auth_multi(x.get("auth_multi", ""))

    accounts = load_accounts()
    by_uid = {a["user_id"]: a for a in accounts}

    def upsert(user_id: str, tok: str, ct0_value: str, source: str) -> None:
        entry = by_uid.get(user_id)
        if entry is None:
            entry = {
                "id": f"acct-{len(accounts) + 1}",
                "user_id": user_id,
                "label": "",
                "auth_token": tok,
                "ct0": ct0_value,
                "source": source,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            accounts.append(entry)
            by_uid[user_id] = entry
        else:
            if tok:
                entry["auth_token"] = tok
            if ct0_value:
                entry["ct0"] = ct0_value
            entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if ct0_value:
                entry["source"] = source

    # Tokens from auth_multi: gives us the roster, but no usable ct0.
    for item in multi:
        upsert(item["user_id"], item["auth_token"], "", "auth_multi")
    # The active session has both a token and a matching ct0.
    if uid and token:
        upsert(uid, token, ct0, "active_session")

    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_FILE.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    # Plaintext tokens/ct0 just hit the disk: tighten the ACL immediately.
    harden_acl(ACCOUNTS_FILE)
    summary = {
        "accounts": len(accounts),
        "with_ct0": [a["user_id"] for a in accounts if a.get("ct0")],
        "missing_ct0": [a["user_id"] for a in accounts if not a.get("ct0")],
        "active_uid": uid,
    }
    return accounts, summary


def harden_acl(path: Path) -> None:
    """Best-effort: make a credential file accessible by this user only.

    Shared by the bridge token file and every plaintext credential file the
    ingest server writes (accounts.json, cookies_raw.json, cookies.json).
    Windows: icacls /inheritance:r /grant:r "<user>":(R,W), which drops the
    ACEs inherited from work/ and grants only the current user. Non-Windows:
    chmod 600. A file that does not exist is skipped; every failure is exactly
    one log line and is ignored, so a request/startup never fails because ACL
    tightening failed.
    """
    try:
        if not path.exists():
            return
        if os.name != "nt":
            path.chmod(0o600)
            return
        user = (os.environ.get("USERNAME") or "").strip()
        if not user:
            print(f"[bridge] icacls skipped for {path.name}: USERNAME unset; ignored",
                  flush=True)
            return
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15,
        )
        if proc.returncode != 0:
            print(f"[bridge] icacls failed for {path.name} (rc={proc.returncode}); "
                  f"permissions unchanged, ignored", flush=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[bridge] icacls failed for {path.name} ({type(exc).__name__}); "
              f"permissions unchanged, ignored", flush=True)


def extension_origin() -> str | None:
    """The one and only browser Origin allowed to POST captures."""
    return f"chrome-extension://{EXTENSION_ID}" if EXTENSION_ID else None


def origin_allowed(origin: str | None) -> bool:
    """Exact match against the fixed extension origin; no prefix guessing.

    There is no blanket bypass. A *missing* Origin is rejected unless the
    explicit --allow-cli-no-origin test flag was given; that flag never accepts
    a wrong Origin and never relaxes the token check.
    """
    expected = extension_origin()
    if not expected:
        return False
    value = (origin or "").strip()
    if not value:
        return ALLOW_CLI_NO_ORIGIN
    return value == expected


def token_matches(provided: str | None) -> bool:
    """Constant-time token comparison; never log either side.

    The token is mandatory in every mode, including --allow-cli-no-origin.
    """
    if TOKEN is None or not provided:
        return False
    try:
        return hmac.compare_digest(provided.encode("utf-8"), TOKEN.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def resolve_token(cli_token: str | None, token_file: Path) -> str:
    """Return the mandatory token, generating and persisting one if needed.

    The token value is never printed. A freshly generated token is written to
    `token_file` (work/bridge_token.txt by default) and the path is announced.
    """
    if cli_token is not None and cli_token.strip():
        print("[bridge] 令牌来源: --token（不打印令牌值）", flush=True)
        return cli_token.strip()
    if cli_token is not None:
        print("[bridge] --token 为空，回退到令牌文件", flush=True)
    try:
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                # Same hardening as on generation: a token file handed to us by
                # the operator must end up readable by this user only.
                harden_acl(token_file)
                print(f"[bridge] 令牌来源: {token_file}（不打印令牌值；ACL 已尝试收紧）",
                      flush=True)
                return token
            print(f"[bridge] {token_file} 为空，将生成新令牌", flush=True)
    except OSError as exc:
        print(f"[bridge] 读取 {token_file} 失败 ({type(exc).__name__})，将生成新令牌",
              flush=True)
    token = secrets.token_hex(32)  # 32 random bytes
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token + "\n", encoding="utf-8")
    harden_acl(token_file)
    print(f"[bridge] 令牌已写入 {token_file}（新生成 32 字节随机令牌；不打印令牌值）",
          flush=True)
    return token


def resolve_extension_id(cli_id: str | None) -> str:
    """Return the mandatory fixed extension id, read from work/extension_id.txt."""
    if cli_id is not None and cli_id.strip():
        value = cli_id.strip()
        if not EXTENSION_ID_RE.match(value):
            raise ValueError("--extension-id must be 32 chars in [a-p]")
        print("[bridge] extension id 来源: --extension-id", flush=True)
        return value
    path = WORK / "extension_id.txt"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read {path} ({type(exc).__name__}); "
                         f"run the key-generation step first") from exc
    if not value:
        raise ValueError(f"{path} is empty; fixed extension ID is required")
    if not EXTENSION_ID_RE.match(value):
        raise ValueError(f"{path} does not look like a Chrome extension id ([a-p]{{32}})")
    print(f"[bridge] extension id 来源: {path}", flush=True)
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "CookieBridge/1.3"

    def log_message(self, fmt: str, *args) -> None:
        # Never let a query string (or anything appended to the path) reach logs.
        message = re.sub(r"\?[^ \"']*", "?<redacted>", fmt % args)
        print(f"[bridge] {self.address_string()} {message}", flush=True)

    def _cors(self) -> None:
        # Responses only ever expose the fixed extension origin (the extension
        # is the only caller that passes Origin checks). --allow-cli-no-origin
        # callers are CLI clients and ignore CORS headers anyway.
        self.send_header("Access-Control-Allow-Origin", extension_origin() or "*")
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Bridge-Token")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _reject(self, code: int, message: str) -> None:
        self._json({"ok": False, "error": message}, code)
        self.close_connection = True

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path != "/ingest":
            self._reject(404, "not found")
            return
        # Preflight carries Origin but no token header; the real POST verifies both.
        if not origin_allowed(self.headers.get("Origin")):
            print("[bridge] preflight /ingest rejected: origin mismatch", flush=True)
            self._reject(403, "origin not allowed: fixed extension origin required")
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            # Anonymous, credential-free liveness probe.
            self._json({"ok": True, "service": "cookie-bridge",
                        "token_required": True,
                        "origin_locked": bool(EXTENSION_ID),
                        "cli_no_origin_allowed": ALLOW_CLI_NO_ORIGIN})
            return
        if self.path != "/":
            self._reject(404, "not found")
            return
        # Read-only metadata (ids only, no tokens); extension never calls this.
        accounts = load_accounts()
        self._json({
            "ok": True,
            "accounts": [{"user_id": a["user_id"], "has_ct0": bool(a.get("ct0"))}
                         for a in accounts],
            "missing_ct0": [a["user_id"] for a in accounts if not a.get("ct0")],
        })

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ingest":
            self._reject(404, "not found")
            return
        if not origin_allowed(self.headers.get("Origin")):
            origin = (self.headers.get("Origin") or "").strip()
            detail = "origin 缺失" if not origin else "origin 不匹配"
            print(f"[bridge] POST /ingest 403: {detail}（需要 {extension_origin()}；"
                  f"--allow-cli-no-origin={ALLOW_CLI_NO_ORIGIN}）", flush=True)
            self._reject(403, "origin not allowed: fixed extension origin required")
            return
        if not token_matches(self.headers.get("X-Bridge-Token")):
            print("[bridge] POST /ingest 403: X-Bridge-Token 缺失或不匹配（不记录令牌值）",
                  flush=True)
            self._reject(403, "invalid or missing X-Bridge-Token")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self._reject(411, "content length required")
            return
        if length > MAX_BODY:
            self._reject(413, f"body too large (max {MAX_BODY} bytes)")
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(400, "bad json")
            return
        if not isinstance(payload, dict):
            self._reject(400, "payload must be a JSON object")
            return
        try:
            validate_capture(payload)
        except ValueError as exc:
            self._reject(400, f"invalid payload: {exc}")
            return

        try:
            RAW.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            # The raw capture also holds plaintext tokens: harden right away.
            harden_acl(RAW)
            accounts, summary = merge_capture(payload)
            PARSED.write_text(json.dumps({"captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                          "accounts": accounts}, ensure_ascii=False, indent=2),
                              encoding="utf-8")
            harden_acl(PARSED)
        except OSError as exc:
            print(f"[bridge] write failed: {type(exc).__name__}", flush=True)
            self._reject(500, "write failed")
            return

        print(f"[bridge] capture: active={summary['active_uid']} "
              f"total={summary['accounts']} with_ct0={len(summary['with_ct0'])} "
              f"missing={summary['missing_ct0']}", flush=True)
        self._json({"ok": True, "saved": str(ACCOUNTS_FILE), **summary})

    # Anything other than the routes above is a plain 404, never a write.
    def _not_found(self) -> None:
        self._reject(404, "not found")

    do_PUT = _not_found
    do_DELETE = _not_found
    do_PATCH = _not_found

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(404)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True


def main(argv: list[str] | None = None) -> int:
    global TOKEN, TOKEN_SOURCE, EXTENSION_ID, ALLOW_CLI_NO_ORIGIN, PORT
    # Deterministic log encoding: the token-file hint is Chinese, and a parent
    # process may pipe stdout with a different legacy code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass
    ap = argparse.ArgumentParser(
        description="Localhost-only cookie ingest server for the X Cookie Bridge extension")
    ap.add_argument("--port", type=int, default=PORT,
                    help=f"loopback port to listen on (default {PORT})")
    ap.add_argument("--token", default=None,
                    help="shared token required as X-Bridge-Token (overrides the "
                         "token file); the value is never logged")
    ap.add_argument("--token-file", default=None,
                    help="token file to read/generate (default <work>/bridge_token.txt)")
    ap.add_argument("--extension-id", default=None,
                    help="fixed 32-char extension id (default <work>/extension_id.txt)")
    ap.add_argument("--work-dir", default=None,
                    help="override the work directory (default: TXV_BRIDGE_WORK env "
                         "var or <repo>/work)")
    ap.add_argument("--allow-cli-no-origin", action="store_true",
                    help="TEST ONLY: accept requests that omit the Origin header "
                         "(the token is still mandatory; a wrong Origin is still "
                         "rejected); default: reject")
    args = ap.parse_args(argv)

    work_arg = args.work_dir or os.environ.get("TXV_BRIDGE_WORK") or str(DEFAULT_WORK)
    work = Path(work_arg).expanduser().resolve()
    set_work_dir(work)
    work.mkdir(parents=True, exist_ok=True)
    token_file = (Path(args.token_file).expanduser().resolve()
                  if args.token_file else WORK / "bridge_token.txt")

    PORT = args.port
    ALLOW_CLI_NO_ORIGIN = args.allow_cli_no_origin

    # Both checks below are mandatory: there is no token-less / origin-less mode.
    try:
        EXTENSION_ID = resolve_extension_id(args.extension_id)
    except ValueError as exc:
        print(f"[bridge] 启动失败：{exc}", flush=True)
        print("[bridge] 安全要求：必须先固定扩展 ID（ext/manifest.json 的 key -> "
              "work/extension_id.txt）", flush=True)
        return 2
    TOKEN = resolve_token(args.token, token_file)
    TOKEN_SOURCE = "--token" if args.token and args.token.strip() else str(token_file)
    if not TOKEN:
        print("[bridge] 启动失败：未取得共享令牌，拒绝无令牌运行", flush=True)
        return 2
    if ALLOW_CLI_NO_ORIGIN:
        print("[bridge] WARNING: --allow-cli-no-origin 已开启：缺失 Origin 的请求将通过 "
              "Origin 校验（令牌仍然强制，错误 Origin 仍拒绝；仅限本地 CLI 测试）",
              flush=True)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        print(f"[bridge] cannot bind 127.0.0.1:{PORT}: {exc}", flush=True)
        return 2
    srv.daemon_threads = True
    flags = ["bind=127.0.0.1",
             f"origin=chrome-extension://{EXTENSION_ID} (exact)",
             f"token=required (source: {TOKEN_SOURCE}; value not logged)"]
    if ALLOW_CLI_NO_ORIGIN:
        flags.append("WARNING: missing Origin accepted (token still required)")
    print(f"[bridge] listening on http://127.0.0.1:{PORT}/ingest "
          f"({', '.join(flags)})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
