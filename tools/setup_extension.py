"""Generate a fixed Chrome extension ID for the cookie bridge.

Creates:
    ext/manifest.json   <- adds the "key" field (RSA-2048 public key, base64 DER)
    work/extension_id.txt
    work/extension_key.pem   (private key; keep it local, never commit)

Usage:
    python tools/setup_extension.py           # create if missing
    python tools/setup_extension.py --force   # rotate (changes the extension ID)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "ext" / "manifest.json"
WORK = ROOT / "work"
ID_FILE = WORK / "extension_id.txt"
KEY_FILE = WORK / "extension_key.pem"

ALPHABET = "abcdefghijklmnop"


def extension_id(der: bytes) -> str:
    digest = hashlib.sha256(der).digest()[:16]
    out = []
    for byte in digest:
        out.append(ALPHABET[byte >> 4])
        out.append(ALPHABET[byte & 0x0F])
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rotate the key (changes the extension id)")
    args = ap.parse_args()

    if ID_FILE.exists() and not args.force:
        print(f"extension id already set: {ID_FILE.read_text(encoding='utf-8').strip()}")
        print("use --force to rotate")
        return 0

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        print("cryptography is required: pip install cryptography", file=sys.stderr)
        return 2

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    ext_id = extension_id(der)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["key"] = base64.b64encode(der).decode("ascii")
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    WORK.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    ID_FILE.write_text(ext_id + "\n", encoding="utf-8")

    print(f"extension id: {ext_id}")
    print(f"wrote {MANIFEST}")
    print(f"wrote {ID_FILE}")
    print(f"wrote {KEY_FILE}  (keep private)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
