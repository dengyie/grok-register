"""Password hashing (stdlib scrypt) for control-plane operators."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must be non-empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
    )
    return "scrypt${}${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        f"n={_SCRYPT_N},r={_SCRYPT_R},p={_SCRYPT_P}",
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def _b64decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def verify_password(password: str, encoded: str) -> bool:
    if not password or not encoded:
        return False
    try:
        scheme, salt_b64, params, dig_b64 = encoded.split("$", 3)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        parts = dict(p.split("=", 1) for p in params.split(","))
        n = int(parts.get("n", _SCRYPT_N))
        r = int(parts.get("r", _SCRYPT_R))
        p = int(parts.get("p", _SCRYPT_P))
        salt = _b64decode(salt_b64)
        expected = _b64decode(dig_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected) or _DKLEN,
        )
    except Exception:
        return False
    return hmac.compare_digest(digest, expected)
