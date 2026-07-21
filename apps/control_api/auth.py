"""Auth for control API: session cookie and/or bearer token."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from apps.control_api.sessions import COOKIE_NAME, read_session_token
from apps.control_api.settings import get_settings
from apps.control_api.users import has_any_user


def session_username(request: Request) -> str | None:
    settings = get_settings()
    if not settings.session_secret:
        return None
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    payload = read_session_token(
        raw,
        secret=settings.session_secret,
        max_age=settings.session_ttl_seconds,
    )
    if not payload:
        return None
    user = payload.get("u")
    return str(user) if user else None


def auth_is_required(project_root=None) -> bool:
    """Gate API when bearer token is set OR password login mode is on."""
    settings = get_settings()
    if settings.token:
        return True
    # Password mode: require session (even before first user — login returns 503).
    return bool(settings.password_login_enabled)


def _bearer_ok(
    authorization: str | None,
    x_control_token: str | None,
) -> bool:
    settings = get_settings()
    if not settings.token:
        return False
    presented: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_control_token:
        presented = x_control_token.strip()
    if not presented:
        return False
    return hmac.compare_digest(presented, settings.token)


def require_token(
    authorization: str | None = Header(default=None),
    x_control_token: str | None = Header(default=None, alias="X-Control-Token"),
) -> None:
    """Legacy dependency: bearer/X-Control-Token only."""
    settings = get_settings()
    if not settings.token:
        return
    if _bearer_ok(authorization, x_control_token):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing token",
    )


def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_control_token: str | None = Header(default=None, alias="X-Control-Token"),
) -> str | None:
    """Accept valid session cookie OR bearer token. Returns username or 'token'."""
    settings = get_settings()

    if settings.token and _bearer_ok(authorization, x_control_token):
        return "token"

    user = session_username(request)
    if user:
        return user

    if not auth_is_required(settings.project_root):
        return None

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="login required",
    )
