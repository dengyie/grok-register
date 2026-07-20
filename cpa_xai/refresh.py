"""Refresh CPA xAI OAuth tokens and persist rotation immediately.

xAI refresh responses may rotate ``refresh_token``. Once the server issues a new
RT, the previous RT is **revoked**. Callers that probe or import MUST:

1. Prefer a still-valid access_token (no refresh) — avoid unnecessary rotation.
2. When refresh is required, write the new RT to the real auth path in the same
   step, then **re-read and verify** the on-disk RT matches.
3. Never treat ``ok`` as success if ``refresh_rotated`` and ``persisted`` is false.

Backups taken *before* a lost rotation cannot revive a revoked RT — recovery is
only possible if the *new* RT was persisted (or never rotated).
"""

from __future__ import annotations

import json
import os
import ssl
import tempfile
import time
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
    jwt_payload,
)
from .writer import patch_cpa_xai_auth, write_cpa_xai_auth

# Refresh only when access expires within this many seconds (clock skew / race).
ACCESS_SKEW_SECONDS = 120
# Side-ledger of rotated RTs (append-only). Not a substitute for on-disk auth,
# but gives an audit trail if a write race is suspected.
RT_ROTATION_LEDGER = "rt_rotation.jsonl"


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


def access_token_seconds_left(access_token: str, *, now: float | None = None) -> float | None:
    """Return seconds until JWT exp, or None if token is not a decodable JWT."""
    try:
        pl = jwt_payload(access_token)
        exp = float(pl["exp"])
    except Exception:
        return None
    t = time.time() if now is None else float(now)
    return exp - t


def access_token_usable(
    access_token: str,
    *,
    skew_seconds: int = ACCESS_SKEW_SECONDS,
    now: float | None = None,
) -> bool:
    """True when access_token is present and not near/past JWT exp."""
    at = (access_token or "").strip()
    if not at:
        return False
    left = access_token_seconds_left(at, now=now)
    if left is None:
        # Non-JWT / opaque: treat as usable if non-empty; caller may still 401.
        return True
    return left > float(skew_seconds)


def _append_rt_rotation_ledger(
    auth_path: Path,
    *,
    email: str,
    old_rt: str,
    new_rt: str,
    rotated: bool,
) -> str | None:
    """Append one RT rotation audit line next to the auth file. Best-effort path return."""
    if not rotated or not old_rt or not new_rt or old_rt == new_rt:
        return None
    try:
        ledger = auth_path.parent / RT_ROTATION_LEDGER
        rec = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "email": (email or "").strip().lower(),
            "auth_file": auth_path.name,
            "auth_path": str(auth_path),
            "old_rt_prefix": old_rt[:12],
            "new_rt_prefix": new_rt[:12],
            "old_rt": old_rt,
            "new_rt": new_rt,
        }
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(ledger, 0o600)
        except OSError:
            pass
        return str(ledger)
    except Exception:
        return None


def _write_pre_rotate_snapshot(auth_path: Path, data: dict[str, Any]) -> str | None:
    """Atomic copy of auth JSON before refresh. Recoverable only if server did not rotate yet."""
    try:
        snap_dir = auth_path.parent / ".rt_prerotate"
        snap_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(snap_dir, 0o700)
        except OSError:
            pass
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = snap_dir / f"{auth_path.stem}.{ts}.json"
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".pre-", suffix=".tmp", dir=str(snap_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, dest)
            os.chmod(dest, 0o600)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        return str(dest)
    except Exception:
        return None


def _verify_persisted_tokens(
    path: Path,
    *,
    expect_access: str,
    expect_refresh: str,
) -> dict[str, Any]:
    """Re-read auth file; require access + refresh match post-refresh values."""
    try:
        disk = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"re-read failed: {e}"}
    if not isinstance(disk, dict):
        return {"ok": False, "error": "re-read root not object"}
    got_at = str(disk.get("access_token") or "").strip()
    got_rt = str(disk.get("refresh_token") or "").strip()
    if got_rt != expect_refresh:
        return {
            "ok": False,
            "error": (
                f"persist verify failed: disk RT mismatch "
                f"(want prefix {expect_refresh[:12]!r} got {got_rt[:12]!r})"
            ),
            "disk_rt_prefix": got_rt[:12],
            "want_rt_prefix": expect_refresh[:12],
        }
    if got_at != expect_access:
        return {
            "ok": False,
            "error": (
                f"persist verify failed: disk access mismatch "
                f"(want prefix {expect_access[:16]!r} got {got_at[:16]!r})"
            ),
        }
    return {"ok": True, "disk": disk}


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
    require_persist_on_rotate: bool = True,
) -> dict[str, Any]:
    """Refresh one xai-*.json auth file and optionally persist rotated tokens.

    When ``persist=True`` (default), writes access/refresh/id/expired back to
    the same path immediately after a successful refresh — before any probe —
    then re-reads the file to verify the new RT is on disk.

    If the server rotated the RT and persist is off or fails, returns
    ``ok=False`` (fail-closed) when ``require_persist_on_rotate`` is True —
    silent success with a dead on-disk RT is never allowed.
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
    old_rt = str(data.get("refresh_token") or "").strip()
    prerotate = _write_pre_rotate_snapshot(p, data)

    ref = refresh_xai_tokens(
        refresh_token=old_rt,
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
        "prerotate_snapshot": prerotate,
        "source": "refresh",
    }
    if not ref.get("ok"):
        out["error"] = ref.get("error")
        out["error_code"] = ref.get("error_code")
        return out

    access = str(ref["access_token"])
    refresh = str(ref["refresh_token"])
    id_token = str(ref.get("id_token") or data.get("id_token") or "")
    rotated = bool(ref.get("refresh_rotated")) or (refresh != old_rt)
    out["refresh_rotated"] = rotated

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

    # Fail-closed: rotation without persist is permanent credential loss.
    if rotated and not persist and require_persist_on_rotate:
        out["ok"] = False
        out["persisted"] = False
        out["error"] = (
            "refresh rotated RT but persist=False; refusing to return ok "
            "(old RT is revoked — would leave unrecoverable credential)"
        )
        out["error_code"] = "rt_rotate_persist_required"
        out["fatal_rt_loss_risk"] = True
        # Still try to force-persist to save the new RT even when caller asked not to.
        try:
            patch_cpa_xai_auth(p, updates)
            verify = _verify_persisted_tokens(p, expect_access=access, expect_refresh=refresh)
            if verify.get("ok"):
                out["ok"] = True
                out["persisted"] = True
                out["persist_forced"] = True
                out["error"] = None
                out["fatal_rt_loss_risk"] = False
                _append_rt_rotation_ledger(
                    p, email=email, old_rt=old_rt, new_rt=refresh, rotated=True
                )
                return out
            out["error"] = f"forced persist after rotate failed verify: {verify.get('error')}"
        except Exception as e:  # noqa: BLE001
            out["error"] = f"forced persist after rotate failed: {e}"
        return out

    if persist:
        try:
            patch_cpa_xai_auth(p, updates)
            verify = _verify_persisted_tokens(p, expect_access=access, expect_refresh=refresh)
            if not verify.get("ok"):
                # One more full rewrite attempt before fail-closed.
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
                verify = _verify_persisted_tokens(p, expect_access=access, expect_refresh=refresh)
                out["persist_fallback"] = True
            if not verify.get("ok"):
                out["ok"] = False
                out["persisted"] = False
                out["error"] = (
                    f"refresh ok but persist verify failed: {verify.get('error')}"
                )
                out["error_code"] = "rt_persist_verify_failed"
                out["fatal_rt_loss_risk"] = bool(rotated)
                return out
            out["persisted"] = True
            if rotated:
                ledger = _append_rt_rotation_ledger(
                    p, email=email, old_rt=old_rt, new_rt=refresh, rotated=True
                )
                out["rt_rotation_ledger"] = ledger
        except Exception as e:  # noqa: BLE001
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
                verify = _verify_persisted_tokens(p, expect_access=access, expect_refresh=refresh)
                if not verify.get("ok"):
                    out["ok"] = False
                    out["persisted"] = False
                    out["error"] = (
                        f"refresh ok but persist failed: {e}; verify={verify.get('error')}"
                    )
                    out["error_code"] = "rt_persist_failed"
                    out["fatal_rt_loss_risk"] = bool(rotated)
                    return out
                out["persisted"] = True
                out["persist_fallback"] = True
                if rotated:
                    ledger = _append_rt_rotation_ledger(
                        p, email=email, old_rt=old_rt, new_rt=refresh, rotated=True
                    )
                    out["rt_rotation_ledger"] = ledger
            except Exception as e2:  # noqa: BLE001
                out["ok"] = False
                out["error"] = f"refresh ok but persist failed: {e}; {e2}"
                out["persisted"] = False
                out["error_code"] = "rt_persist_failed"
                out["fatal_rt_loss_risk"] = bool(rotated)
                return out
    else:
        out["persisted"] = False
    return out


def ensure_auth_tokens(
    path: str | Path,
    *,
    proxy: str | None = None,
    skew_seconds: int = ACCESS_SKEW_SECONDS,
    force_refresh: bool = False,
    timeout: float = 45.0,
) -> dict[str, Any]:
    """Return usable access+refresh for an auth file, refreshing only when needed.

    Default policy (access-first):
      - If access_token is still valid past skew → return disk tokens, **no**
        network refresh (avoids RT rotation).
      - Else → ``refresh_auth_file(..., persist=True)`` with verify.

    ``force_refresh=True`` always hits the token endpoint (import ops that
    explicitly need a brand-new access for remote CPA).
    """
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return {
            "ok": False,
            "error": f"missing auth file: {p}",
            "path": str(p),
            "source": "missing",
        }
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"invalid json: {e}",
            "path": str(p),
            "source": "invalid",
        }
    if not isinstance(data, dict):
        return {
            "ok": False,
            "error": "auth root must be object",
            "path": str(p),
            "source": "invalid",
        }

    email = str(data.get("email") or "").strip()
    access = str(data.get("access_token") or "").strip()
    refresh = str(data.get("refresh_token") or "").strip()
    id_token = str(data.get("id_token") or "").strip()
    left = access_token_seconds_left(access)

    base: dict[str, Any] = {
        "path": str(p),
        "email": email,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "sub": data.get("sub"),
        "access_seconds_left": left,
        "refresh_rotated": False,
        "persisted": False,
    }

    if not refresh:
        base.update({"ok": False, "error": "missing_refresh_token", "source": "disk"})
        return base

    if not force_refresh and access_token_usable(access, skew_seconds=skew_seconds):
        base.update(
            {
                "ok": True,
                "source": "access_reuse",
                "expired": data.get("expired"),
                "expires_in": data.get("expires_in"),
            }
        )
        return base

    if not force_refresh and access and left is None:
        # Opaque access token with no exp — reuse; import will 401 → then refresh.
        base.update({"ok": True, "source": "access_reuse_opaque"})
        return base

    ref = refresh_auth_file(p, proxy=proxy, persist=True, timeout=timeout)
    ref.setdefault("source", "refresh")
    return ref
