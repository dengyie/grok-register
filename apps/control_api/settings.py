"""Environment settings for the control API."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os
import secrets


@dataclass(frozen=True)
class Settings:
    project_root: Path
    host: str
    port: int
    token: str | None
    max_upload_bytes: int = 20 * 1024 * 1024
    # Password / session auth
    session_secret: str | None = None
    session_ttl_seconds: int = 12 * 3600
    cookie_secure: bool = False
    password_login_enabled: bool = True
    bootstrap_user: str | None = None
    bootstrap_password: str | None = None

    @property
    def auth_required(self) -> bool:
        """True when bearer token is set. Password users checked via auth_is_required()."""
        return bool(self.token)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = os.environ.get("REGISTER_PROJECT_ROOT")
    project_root = Path(root).resolve() if root else Path.cwd().resolve()
    token = os.environ.get("CONTROL_API_TOKEN")
    if token is not None and token.strip() == "":
        token = None
    host = os.environ.get("CONTROL_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CONTROL_API_PORT", "8787"))
    max_upload = int(os.environ.get("CONTROL_API_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))

    session_secret = os.environ.get("CONTROL_API_SESSION_SECRET")
    if session_secret is not None and session_secret.strip() == "":
        session_secret = None
    # Prefer dedicated secret; fall back to token; last resort ephemeral (dev only)
    if not session_secret:
        session_secret = token or os.environ.get("CONTROL_API_DEV_SESSION_SECRET")
    if not session_secret and os.environ.get("CONTROL_API_ALLOW_EPHEMERAL_SESSION") == "1":
        session_secret = secrets.token_hex(32)

    ttl = int(os.environ.get("CONTROL_API_SESSION_TTL", str(12 * 3600)))
    cookie_secure = os.environ.get("CONTROL_API_COOKIE_SECURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    # Password login on by default; set CONTROL_API_PASSWORD_LOGIN=0 to disable
    pw_flag = os.environ.get("CONTROL_API_PASSWORD_LOGIN", "1").strip().lower()
    password_login_enabled = pw_flag not in {"0", "false", "no", "off"}

    bootstrap_user = os.environ.get("CONTROL_API_BOOTSTRAP_USER")
    bootstrap_password = os.environ.get("CONTROL_API_BOOTSTRAP_PASSWORD")
    if bootstrap_user is not None and bootstrap_user.strip() == "":
        bootstrap_user = None
    if bootstrap_password is not None and bootstrap_password.strip() == "":
        bootstrap_password = None

    return Settings(
        project_root=project_root,
        host=host,
        port=port,
        token=token,
        max_upload_bytes=max_upload,
        session_secret=session_secret,
        session_ttl_seconds=max(300, ttl),
        cookie_secure=cookie_secure,
        password_login_enabled=password_login_enabled,
        bootstrap_user=bootstrap_user,
        bootstrap_password=bootstrap_password,
    )


def clear_settings_cache() -> None:
    get_settings.cache_clear()
