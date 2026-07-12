"""Probe free Grok 4.5 via cli-chat-proxy with a CPA access_token."""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

from .proxyutil import resolve_proxy
from .schema import DEFAULT_BASE_URL, DEFAULT_CLIENT_HEADERS

# Free Build chat 403: account has no L1 entitlement (not a network flake).
_ENTITLEMENT_CODE_RE = re.compile(
    r"permission[-_ ]?denied|permission_error|not[_\s-]?allowed|"
    r"access[_\s-]?denied|entitlement|insufficient[_\s-]?permission",
    re.I,
)
_TRANSIENT_STATUS = frozenset({0, 408, 425, 429, 500, 502, 503, 504})


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


def _extract_error_code(error_text: str) -> str:
    """Best-effort parse of API error code/type from JSON or plain text."""
    raw = (error_text or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            for key in ("code", "type", "error", "message"):
                val = err.get(key)
                if val is not None and str(val).strip():
                    if key in ("code", "type", "error"):
                        return str(val).strip()
            msg = err.get("message")
            if msg is not None:
                return str(msg).strip()[:120]
        for key in ("code", "type", "error"):
            val = data.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    # plain-text fallback: first token-ish match
    m = _ENTITLEMENT_CODE_RE.search(raw)
    if m:
        return m.group(0).lower().replace(" ", "_")
    return ""


def classify_chat_probe(result: dict[str, Any] | None) -> dict[str, Any]:
    """Classify /v1/responses probe outcome for product gating.

    Returns keys:
      ok, entitlement_denied, retryable, error_code, reason
    """
    r = result or {}
    if r.get("ok"):
        return {
            "ok": True,
            "entitlement_denied": False,
            "retryable": False,
            "error_code": "",
            "reason": "chat_ok",
        }

    status = int(r.get("status") or 0)
    err_text = str(r.get("error") or "")
    code = str(r.get("error_code") or _extract_error_code(err_text) or "").strip()
    blob = f"{code} {err_text}".lower()

    # Free Build /v1/responses: any HTTP 403 is L1 entitlement (models may still 200).
    # Observed default is permission-denied; other 403 bodies are still non-retryable.
    if status == 403:
        return {
            "ok": False,
            "entitlement_denied": True,
            "retryable": False,
            "error_code": code or "permission_denied",
            "reason": "entitlement_denied",
        }

    # Explicit permission wording on other statuses (rare) still counts as entitlement.
    if _ENTITLEMENT_CODE_RE.search(blob) and status in (400, 401, 404):
        return {
            "ok": False,
            "entitlement_denied": True,
            "retryable": False,
            "error_code": code or "permission_denied",
            "reason": "entitlement_denied",
        }

    # Client/protocol misconfig — not fixed by remint of same account.
    if status in (401, 426):
        return {
            "ok": False,
            "entitlement_denied": False,
            "retryable": False,
            "error_code": code or str(status),
            "reason": "auth_or_protocol",
        }

    if status in _TRANSIENT_STATUS or status >= 500:
        return {
            "ok": False,
            "entitlement_denied": False,
            "retryable": True,
            "error_code": code or str(status or "network"),
            "reason": "transient",
        }

    return {
        "ok": False,
        "entitlement_denied": False,
        "retryable": False,
        "error_code": code or str(status or "unknown"),
        "reason": "chat_failed",
    }


def probe_models(
    access_token: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> dict[str, Any]:
    base = base_url.rstrip("/")
    url = f"{base}/models"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        **DEFAULT_CLIENT_HEADERS,
    }
    opener = _opener(proxy)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            ids = [x.get("id") for x in body.get("data") or [] if isinstance(x, dict)]
            return {
                "ok": True,
                "status": getattr(resp, "status", 200),
                "model_ids": ids,
                "has_grok_45": any(i == "grok-4.5" for i in ids),
            }
    except urllib.error.HTTPError as e:
        return {
            "ok": False,
            "status": e.code,
            "error": e.read().decode("utf-8", errors="replace")[:500],
            "model_ids": [],
            "has_grok_45": False,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "error": str(e),
            "model_ids": [],
            "has_grok_45": False,
        }


def probe_mini_response(
    access_token: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 60.0,
    proxy: str | None = None,
) -> dict[str, Any]:
    base = base_url.rstrip("/")
    url = f"{base}/responses"
    payload = {
        "model": "grok-4.5",
        "stream": False,
        "input": "Reply with exactly MINT_OK",
        "reasoning": {"effort": "low"},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **DEFAULT_CLIENT_HEADERS,
    }
    opener = _opener(proxy)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            texts: list[str] = []
            for item in body.get("output") or []:
                if item.get("type") == "message":
                    for c in item.get("content") or []:
                        if c.get("type") == "output_text":
                            texts.append(c.get("text") or "")
            out = {
                "ok": True,
                "status": getattr(resp, "status", 200),
                "model": body.get("model"),
                "text": "\n".join(texts),
                "usage": body.get("usage"),
            }
            out.update(classify_chat_probe(out))
            return out
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:800]
        out = {
            "ok": False,
            "status": e.code,
            "error": err_body,
            "error_code": _extract_error_code(err_body),
        }
        out.update(classify_chat_probe(out))
        return out
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "status": 0, "error": str(e)}
        out.update(classify_chat_probe(out))
        return out
