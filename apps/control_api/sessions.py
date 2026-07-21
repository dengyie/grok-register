"""Signed HttpOnly session cookies for browser login."""

from __future__ import annotations

import time
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "control_session"


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="control-api-session-v1")


def mint_session_token(*, secret: str, username: str, ttl_seconds: int) -> str:
    if not secret:
        raise ValueError("session secret required")
    payload = {
        "u": username,
        "iat": int(time.time()),
        "ttl": int(ttl_seconds),
    }
    return _serializer(secret).dumps(payload)


def read_session_token(
    token: str,
    *,
    secret: str,
    max_age: int,
) -> dict[str, Any] | None:
    if not token or not secret:
        return None
    try:
        payload = _serializer(secret).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    user = payload.get("u")
    if not isinstance(user, str) or not user:
        return None
    return payload


def session_cookie_kwargs(*, secure: bool, max_age: int) -> dict[str, Any]:
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "strict",
        "secure": secure,
        "path": "/",
        "max_age": max_age,
    }
