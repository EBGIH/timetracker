"""RFC 6238 TOTP, implemented on the standard library (NFR-S-04)."""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
from hashlib import sha1


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify(secret: str, code: str, window: int = 1, now: float | None = None) -> bool:
    if not code or not secret:
        return False
    code = code.strip().replace(" ", "")
    counter = int((now or time.time()) // 30)
    return any(
        hmac.compare_digest(_code_at(secret, counter + drift), code)
        for drift in range(-window, window + 1)
    )


def provisioning_uri(secret: str, account: str, issuer: str = "TimeKeeper") -> str:
    from urllib.parse import quote

    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        "&algorithm=SHA1&digits=6&period=30"
    )
