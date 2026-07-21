"""Login / logout / session me endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from apps.control_api.auth import auth_is_required, require_auth, session_username
from apps.control_api.rate_limit import login_limiter
from apps.control_api.schemas import LoginIn, LoginOut, MeOut
from apps.control_api.sessions import (
    COOKIE_NAME,
    mint_session_token,
    session_cookie_kwargs,
)
from apps.control_api.settings import get_settings
from apps.control_api.users import authenticate, has_any_user, list_usernames

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}|{(username or '').strip().lower()}"


@router.get("/me", response_model=MeOut)
def me(request: Request) -> MeOut:
    s = get_settings()
    user = session_username(request)
    return MeOut(
        authenticated=bool(user),
        username=user,
        auth_required=auth_is_required(s.project_root),
        password_login_enabled=s.password_login_enabled,
        token_auth_enabled=bool(s.token),
        users_configured=has_any_user(s.project_root),
    )


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, request: Request, response: Response) -> LoginOut:
    s = get_settings()
    if not s.password_login_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="password login disabled")
    if not s.session_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session secret not configured",
        )
    if not has_any_user(s.project_root):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no operators configured; set CONTROL_API_BOOTSTRAP_USER/PASSWORD once",
        )

    key = _client_key(request, body.username)
    if login_limiter.is_blocked(key):
        wait = login_limiter.remaining_seconds(key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"too many failed logins; retry in {wait}s",
            headers={"Retry-After": str(max(1, wait))},
        )

    ok = authenticate(s.project_root, body.username, body.password)
    if not ok:
        login_limiter.record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid username or password")

    login_limiter.clear(key)
    username = body.username.strip()
    token = mint_session_token(
        secret=s.session_secret,
        username=username,
        ttl_seconds=s.session_ttl_seconds,
    )
    kw = session_cookie_kwargs(secure=s.cookie_secure, max_age=s.session_ttl_seconds)
    response.set_cookie(value=token, **kw)
    return LoginOut(ok=True, username=username)


@router.post("/logout", response_model=LoginOut)
def logout(response: Response) -> LoginOut:
    s = get_settings()
    response.delete_cookie(COOKIE_NAME, path="/")
    # also clear with secure flag variants
    response.delete_cookie(COOKIE_NAME, path="/", secure=s.cookie_secure)
    return LoginOut(ok=True, username=None)


@router.get("/users", response_model=dict, dependencies=[Depends(require_auth)])
def users_list() -> dict:
    """List operator usernames (no hashes). Auth required."""
    s = get_settings()
    return {"users": list_usernames(s.project_root)}
