"""Pure-HTTP SSO cookie → OIDC tokens (device flow, no browser).

Uses curl_cffi (Chrome TLS fingerprint) + SSO cookie to:
  1. Validate session on accounts.x.ai
  2. Request device code (stdlib / oauth_device)
  3. GET verification_uri_complete
  4. POST /oauth2/device/verify
  5. POST /oauth2/device/approve
  6. Poll token endpoint

On any failure raise ProtocolMintError so callers can fall back to browser mint.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

from .oauth_device import (
    CLIENT_ID,
    ISSUER,
    OAuthDeviceError,
    SCOPE,
    poll_device_token,
    request_device_code,
)
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy

LogFn = Callable[[str], None]

VERIFY_URL = f"{ISSUER}/oauth2/device/verify"
APPROVE_URL = f"{ISSUER}/oauth2/device/approve"


class ProtocolMintError(RuntimeError):
    """Protocol path failed; caller may fall back to browser mint."""


def _noop_log(_: str) -> None:
    return None


def extract_sso_from_cookies(cookies: Any) -> str:
    """Pull sso / sso-rw value from a cookie list/dict."""
    if not cookies:
        return ""
    if isinstance(cookies, str):
        return cookies.strip()
    if isinstance(cookies, dict):
        for name in ("sso", "sso-rw"):
            v = cookies.get(name)
            if v:
                return str(v).strip()
        return ""
    if isinstance(cookies, (list, tuple)):
        # Prefer bare "sso" over "sso-rw"
        found_rw = ""
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or c.get("Name") or "")
            value = c.get("value") if "value" in c else c.get("Value")
            if not value:
                continue
            if name == "sso":
                return str(value).strip()
            if name == "sso-rw" and not found_rw:
                found_rw = str(value).strip()
        return found_rw
    return ""


def _session(proxy: str | None, log: LogFn):
    try:
        from curl_cffi import requests as cf_requests
    except ImportError as e:
        raise ProtocolMintError(
            "curl_cffi not installed; cannot run protocol mint"
        ) from e

    s = cf_requests.Session()
    resolved = resolve_proxy(proxy)
    if resolved:
        s.proxies = {"http": resolved, "https": resolved}
        log(f"protocol proxy={proxy_log_label(resolved)}")
    return s


def _set_sso_cookie(session: Any, sso_cookie: str) -> None:
    sso_cookie = (sso_cookie or "").strip()
    if not sso_cookie:
        raise ProtocolMintError("empty sso cookie")
    for domain in (".x.ai", "accounts.x.ai", "auth.x.ai", ".accounts.x.ai"):
        try:
            session.cookies.set("sso", sso_cookie, domain=domain)
        except Exception:
            try:
                session.cookies.set("sso", sso_cookie, domain=domain, path="/")
            except Exception:
                pass
        try:
            session.cookies.set("sso-rw", sso_cookie, domain=domain)
        except Exception:
            pass


def _url_path(url: str) -> str:
    try:
        return urlparse(url or "").path or ""
    except Exception:
        return url or ""


def mint_with_sso_protocol(
    *,
    sso_cookie: str,
    email: str = "",
    proxy: str | None = None,
    timeout: float = 30.0,
    poll_timeout_sec: float = 90.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """SSO cookie → OIDC token dict (access/refresh/id/expires_in).

    Raises ProtocolMintError on failure.
    """
    log = log or _noop_log
    sso_cookie = (sso_cookie or "").strip()
    if not sso_cookie:
        raise ProtocolMintError("missing sso cookie")

    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)

    session = _session(resolved or None, log)
    _set_sso_cookie(session, sso_cookie)

    # 1) Validate SSO
    try:
        r = session.get(
            "https://accounts.x.ai/",
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as e:  # noqa: BLE001
        raise ProtocolMintError(f"accounts.x.ai network error: {e}") from e

    final_url = getattr(r, "url", "") or ""
    if "sign-in" in final_url or "sign-up" in final_url:
        raise ProtocolMintError(f"sso invalid (landed {final_url[:120]})")
    log(f"protocol sso valid url={final_url[:120]}")

    # 2) Device code
    try:
        sess = request_device_code(proxy=resolved or None, timeout=timeout)
    except OAuthDeviceError as e:
        raise ProtocolMintError(f"device code: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise ProtocolMintError(f"device code: {e}") from e
    log(f"protocol user_code={sess.user_code}")

    if cancel and cancel():
        raise ProtocolMintError("cancelled")

    # 3) Open verification URI (sets session state / CSRF)
    try:
        r = session.get(
            sess.verification_uri_complete,
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )
        log(f"protocol verify-uri status={getattr(r, 'status_code', '?')} url={getattr(r, 'url', '')[:140]}")
    except Exception as e:  # noqa: BLE001
        raise ProtocolMintError(f"verification_uri get failed: {e}") from e

    if cancel and cancel():
        raise ProtocolMintError("cancelled")

    # 4) POST device/verify
    try:
        r = session.post(
            VERIFY_URL,
            data={"user_code": sess.user_code},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as e:  # noqa: BLE001
        raise ProtocolMintError(f"device/verify exception: {e}") from e

    verify_url = getattr(r, "url", "") or ""
    status = getattr(r, "status_code", 0)
    path = _url_path(verify_url)
    body_snip = ""
    try:
        body_snip = (r.text or "")[:200]
    except Exception:
        pass

    if "consent" not in verify_url and "consent" not in path:
        # Some flows land on consent via relative redirect body; accept soft signals
        soft_ok = (
            "consent" in (body_snip or "").lower()
            or "authorize grok build" in (body_snip or "").lower()
            or "授权 grok build" in (body_snip or "").lower()
        )
        if not soft_ok:
            raise ProtocolMintError(
                f"device/verify failed status={status} url={verify_url[:160]}"
            )
    log(f"protocol verify ok status={status} url={verify_url[:140]}")

    if cancel and cancel():
        raise ProtocolMintError("cancelled")

    # 5) POST device/approve
    try:
        r = session.post(
            APPROVE_URL,
            data={
                "user_code": sess.user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome",
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as e:  # noqa: BLE001
        raise ProtocolMintError(f"device/approve exception: {e}") from e

    approve_url = getattr(r, "url", "") or ""
    status = getattr(r, "status_code", 0)
    if "done" not in approve_url and "device/done" not in _url_path(approve_url):
        # Accept success page markers in body
        try:
            text = r.text or ""
        except Exception:
            text = ""
        if "设备已授权" not in text and "device authorized" not in text.lower() and "done" not in text.lower():
            raise ProtocolMintError(
                f"device/approve failed status={status} url={approve_url[:160]}"
            )
    log(f"protocol approve ok status={status} url={approve_url[:140]}")

    # 6) Poll tokens
    poll_expires = min(int(sess.expires_in), max(int(poll_timeout_sec), 30))
    try:
        tr = poll_device_token(
            sess.device_code,
            interval=max(int(sess.interval), 2),
            expires_in=poll_expires,
            timeout=timeout,
            log=log,
            cancel=cancel,
            proxy=resolved or None,
        )
    except OAuthDeviceError as e:
        raise ProtocolMintError(f"token poll: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise ProtocolMintError(f"token poll: {e}") from e

    log(
        f"protocol token ok expires_in={tr.expires_in}"
        + (f" email={email}" if email else "")
    )
    return {
        "access_token": tr.access_token,
        "refresh_token": tr.refresh_token,
        "id_token": tr.id_token,
        "token_type": tr.token_type,
        "expires_in": tr.expires_in,
        "user_code": sess.user_code,
        "mint_method": "protocol",
    }
