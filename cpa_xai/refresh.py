"""Refresh CPA xAI OAuth tokens and persist rotation immediately.

xAI refresh responses may rotate ``refresh_token``. Callers that probe or import
MUST write the new refresh back to disk in the same step — otherwise the on-disk
credential is permanently revoked (invalid_grant).
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .proxyutil import resolve_proxy
from .schema import (
    CLIENT_ID,
    DEFAULT_CLIENT_HEADERS,
    DEFAULT_TOKEN_ENDPOINT,
    build_cpa_xai_auth,
    expired_from_access_token,
)
from .writer import patch_cpa_xai_auth, write_cpa_xai_auth


def _ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    p = resolve_proxy(proxy)
    handlers: list[Any] = []
    ctx = _ssl_context()
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if p:
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()


def refresh_xai_tokens(
    *,
    refresh_token: str,
    client_id: str = CLIENT_ID,
    token_endpoint: str = DEFAULT_TOKEN_ENDPOINT,
    proxy: str | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Exchange refresh_token for a new access_token (and possibly new refresh).

    Returns:
      ok, status, access_token, refresh_token, id_token?, expires_in?, error?
    """
    rt = (refresh_token or "").strip()
    if not rt:
        return {"ok": False, "status": 0, "error": "missing_refresh_token"}

    endpoint = (token_endpoint or DEFAULT_TOKEN_ENDPOINT).strip()
    data = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": client_id or CLIENT_ID,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        **DEFAULT_CLIENT_HEADERS,
    }
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            access = str(body.get("access_token") or "").strip()
            if not access:
                return {
                    "ok": False,
                    "status": getattr(resp, "status", 200),
                    "error": "token response missing access_token",
                }
            new_rt = str(body.get("refresh_token") or "").strip() or rt
            return {
                "ok": True,
                "status": getattr(resp, "status", 200),
                "access_token": access,
                "refresh_token": new_rt,
                "refresh_rotated": new_rt != rt,
                "id_token": str(body.get("id_token") or "").strip(),
                "expires_in": body.get("expires_in"),
                "token_type": body.get("token_type") or "Bearer",
            }
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:500]
        return {
            "ok": False,
            "status": e.code,
            "error": err,
            "error_code": "invalid_grant" if e.code == 400 and "invalid_grant" in err else str(e.code),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": str(e)[:300]}


def refresh_auth_file(
    path: str | Path,
    *,
    proxy: str | None = None,
    persist: bool = True,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Refresh one xai-*.json auth file and optionally persist rotated tokens.

    When ``persist=True`` (default), writes access/refresh/id/expired back to
    the same path immediately after a successful refresh — before any probe.
    """
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return {"ok": False, "error": f"missing auth file: {p}", "path": str(p)}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"invalid json: {e}", "path": str(p)}
    if not isinstance(data, dict):
        return {"ok": False, "error": "auth root must be object", "path": str(p)}

    email = str(data.get("email") or "").strip()
    ref = refresh_xai_tokens(
        refresh_token=str(data.get("refresh_token") or ""),
        token_endpoint=str(data.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT),
        proxy=proxy,
        timeout=timeout,
    )
    out: dict[str, Any] = {
        "path": str(p),
        "email": email,
        "ok": bool(ref.get("ok")),
        "status": ref.get("status"),
        "refresh_rotated": bool(ref.get("refresh_rotated")),
    }
    if not ref.get("ok"):
        out["error"] = ref.get("error")
        out["error_code"] = ref.get("error_code")
        return out

    access = str(ref["access_token"])
    refresh = str(ref["refresh_token"])
    id_token = str(ref.get("id_token") or data.get("id_token") or "")
    try:
        expired, expires_in, sub = expired_from_access_token(access)
    except Exception:
        expired = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        expires_in = int(ref.get("expires_in") or 0) or 21600
        sub = str(data.get("sub") or "")

    updates = {
        "access_token": access,
        "refresh_token": refresh,
        "expired": expired,
        "expires_in": expires_in or int(ref.get("expires_in") or 0) or 21600,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token_type": ref.get("token_type") or data.get("token_type") or "Bearer",
    }
    if id_token:
        updates["id_token"] = id_token
    if sub and not data.get("sub"):
        updates["sub"] = sub

    out.update(
        {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            "expired": expired,
            "expires_in": updates["expires_in"],
            "sub": sub or data.get("sub"),
        }
    )

    if persist:
        try:
            patch_cpa_xai_auth(p, updates)
            out["persisted"] = True
        except Exception as e:  # noqa: BLE001
            # Fallback full rewrite via schema builder if patch fails mid-file.
            try:
                payload = build_cpa_xai_auth(
                    email=email,
                    access_token=access,
                    refresh_token=refresh,
                    id_token=id_token,
                    sub=str(sub or data.get("sub") or ""),
                    base_url=str(data.get("base_url") or ""),
                    priority=int(data.get("priority") or 1000),
                    disabled=bool(data.get("disabled")),
                )
                # preserve non-token stamps
                for k in (
                    "chat_ok",
                    "usable",
                    "entitlement_denied",
                    "chat_retryable",
                    "import_gate",
                    "fail_reason",
                    "chat_error_code",
                    "headers",
                ):
                    if k in data:
                        payload[k] = data[k]
                write_cpa_xai_auth(p.parent, payload, filename=p.name)
                out["persisted"] = True
                out["persist_fallback"] = True
            except Exception as e2:  # noqa: BLE001
                out["ok"] = False
                out["error"] = f"refresh ok but persist failed: {e}; {e2}"
                out["persisted"] = False
                return out
    else:
        out["persisted"] = False
    return out
