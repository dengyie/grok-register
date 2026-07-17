"""Probe free Grok 4.5 via cli-chat-proxy (direct) or tebi CPA mid-tier."""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Mapping

from .proxyutil import resolve_proxy
from .schema import DEFAULT_BASE_URL, DEFAULT_CLIENT_HEADERS

# Free Build chat 403: account has no L1 entitlement (not a network flake).
_ENTITLEMENT_CODE_RE = re.compile(
    r"permission[-_ ]?denied|permission_error|not[_\s-]?allowed|"
    r"access[_\s-]?denied|entitlement|insufficient[_\s-]?permission",
    re.I,
)
_TRANSIENT_STATUS = frozenset({0, 408, 425, 429, 500, 502, 503, 504})

# Product default pin header name for CPA per-credential routing (if supported).
DEFAULT_CPA_PIN_HEADER = "X-CPA-Credential"


def build_probe_transport(
    *,
    via: str,
    upstream_base_url: str = DEFAULT_BASE_URL,
    cpa_base_url: str = "",
    cpa_api_key: str = "",
    access_token: str = "",  # noqa: ARG001 — reserved for callers / future pin modes
    credential_pin: str = "",
    pin_header: str = DEFAULT_CPA_PIN_HEADER,
) -> dict[str, Any]:
    """Build a probe transport dict for direct bearer or CPA OpenAI-compatible.

    mode=direct: Authorization Bearer = xAI access_token + CLI headers.
    mode=cpa: Authorization Bearer = CPA API key; optional credential pin header.
    """
    _ = access_token  # accepted for API symmetry with callers
    mode = (via or "direct").strip().lower()
    if mode not in {"direct", "cpa"}:
        mode = "direct"
    upstream = (upstream_base_url or DEFAULT_BASE_URL).rstrip("/")
    if mode == "cpa":
        base = (cpa_base_url or "").strip().rstrip("/")
        if not base:
            # Misconfigured CPA → fail closed to direct (same host as auth write).
            return {
                "mode": "direct",
                "base_url": upstream,
                "api_key": "",
                "credential_pin": "",
                "pin_header": "",
                "upstream_base_url": upstream,
            }
        return {
            "mode": "cpa",
            "base_url": base,
            "api_key": (cpa_api_key or "").strip(),
            "credential_pin": (credential_pin or "").strip(),
            "pin_header": (pin_header or DEFAULT_CPA_PIN_HEADER).strip(),
            "upstream_base_url": upstream,
        }
    return {
        "mode": "direct",
        "base_url": upstream,
        "api_key": "",
        "credential_pin": "",
        "pin_header": "",
        "upstream_base_url": upstream,
    }


def resolve_gate_probe_policy(
    *,
    via: str,
    cpa_base_url: str = "",
    cpa_api_key: str = "",
    credential_pin: str = "",
    allow_unpinned_cpa_gate: bool = False,
) -> dict[str, Any]:
    """Decide which path stamps chat_ok (gate) vs optional CPA smoke.

    Spec: unpinned CPA pool must not stamp chat_ok. Hybrid keeps gate=direct and
    may run observational CPA smoke that never replaces chat_ok for inject.
    """
    v = (via or "direct").strip().lower()
    if v not in {"direct", "cpa", "hybrid"}:
        v = "direct"
    pin = (credential_pin or "").strip()
    has_cpa = bool((cpa_base_url or "").strip() and (cpa_api_key or "").strip())

    if v == "direct" or not has_cpa:
        return {
            "gate_via": "direct",
            "cpa_smoke": False,
            "reason": "direct" if v == "direct" else "cpa_config_missing",
        }
    if v == "hybrid":
        return {"gate_via": "direct", "cpa_smoke": True, "reason": "hybrid"}
    # via == cpa
    if pin or allow_unpinned_cpa_gate:
        return {
            "gate_via": "cpa",
            "cpa_smoke": False,
            "reason": "cpa_pinned" if pin else "cpa_unpinned_allowed",
        }
    return {"gate_via": "direct", "cpa_smoke": True, "reason": "unpinned_cpa_hybrid"}


def probe_request_headers(
    transport: Mapping[str, Any],
    *,
    access_token: str,
) -> dict[str, str]:
    """HTTP headers for models/responses based on transport mode."""
    mode = str(transport.get("mode") or "direct")
    if mode == "cpa":
        key = str(transport.get("api_key") or "").strip()
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        pin = str(transport.get("credential_pin") or "").strip()
        pin_h = str(transport.get("pin_header") or DEFAULT_CPA_PIN_HEADER).strip()
        if pin and pin_h:
            headers[pin_h] = pin
        return headers
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        **DEFAULT_CLIENT_HEADERS,
    }


def remap_cpa_gateway_failure(
    raw: Mapping[str, Any],
    classified: Mapping[str, Any],
) -> dict[str, Any]:
    """CPA API-key / gateway failures must not stamp entitlement_denied.

    Direct cli-chat-proxy 403 remains entitlement. CPA transport 401/ambiguous 403
    are ops/config or mid-tier errors unless body clearly is free-Build permission.
    """
    out = dict(classified)
    if str(raw.get("transport_mode") or "") != "cpa":
        return out
    status = int(raw.get("status") or 0)
    err = str(raw.get("error") or "").lower()
    # Clear upstream free-Build denial through CPA still counts as entitlement.
    # Tolerate hyphen / underscore / whitespace forms (permission-denied,
    # permission_denied, "permission denied") and the console.x.ai marker.
    if "permission" in err and "denied" in err and (
        "console.x.ai" in err or _ENTITLEMENT_CODE_RE.search(err) is not None
    ):
        out["upstream_entitlement"] = True
        # mint hard gate reads entitlement_denied; keep it non-retryable so we
        # fail-fast (skip remint/retry) instead of falling to ambiguous-retryable.
        out["entitlement_denied"] = True
        out["retryable"] = False
        out["reason"] = "upstream_entitlement"
        out["error_code"] = str(raw.get("error_code") or "permission_denied")
        return out
    if status == 401 or "api key" in err or "unauthorized" in err or "invalid key" in err:
        out["entitlement_denied"] = False
        out["retryable"] = False
        out["reason"] = "cpa_gateway_auth"
        out["error_code"] = str(raw.get("error_code") or status or "unauthorized")
        return out
    # Ambiguous CPA 403 (no clear free-Build body): not account entitlement.
    if status == 403:
        out["entitlement_denied"] = False
        out["retryable"] = True
        out["reason"] = "cpa_gateway_error"
        out["error_code"] = str(raw.get("error_code") or "403")
    return out


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

    # Free Build quota exhausted (rolling ~24h). Not currently usable — do not inject.
    # Not entitlement (will recover after window); not remint-fixed either.
    if status == 429 and (
        "free-usage-exhausted" in blob
        or "free_usage_exhausted" in blob
        or "usage-exhausted" in blob
        or "usage_exhausted" in blob
    ):
        return {
            "ok": False,
            "entitlement_denied": False,
            "retryable": False,
            "error_code": code or "free-usage-exhausted",
            "reason": "usage_exhausted",
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
    transport: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    t = dict(
        transport
        or build_probe_transport(
            via="direct",
            upstream_base_url=base_url,
            access_token=access_token,
        )
    )
    base = str(t.get("base_url") or base_url).rstrip("/")
    url = f"{base}/models"
    headers = probe_request_headers(t, access_token=access_token)
    headers.setdefault("Accept", "application/json")
    opener = _opener(proxy)
    req = urllib.request.Request(url, headers=headers, method="GET")
    mode = str(t.get("mode") or "direct")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            ids = [x.get("id") for x in body.get("data") or [] if isinstance(x, dict)]
            return {
                "ok": True,
                "status": getattr(resp, "status", 200),
                "model_ids": ids,
                "has_grok_45": any(i == "grok-4.5" for i in ids),
                "transport_mode": mode,
            }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        out = {
            "ok": False,
            "status": e.code,
            "error": err_body,
            "error_code": _extract_error_code(err_body),
            "model_ids": [],
            "has_grok_45": False,
            "transport_mode": mode,
        }
        return out
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "error": str(e),
            "model_ids": [],
            "has_grok_45": False,
            "transport_mode": mode,
        }


def probe_chat_with_retries(
    access_token: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    proxy: str | None = None,
    max_attempts: int = 3,
    timeout: float = 60.0,
    sleep_fn: Any | None = None,
    log: Any | None = None,
    transport: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Probe /v1/responses with classification + transient retries.

    Shared by mint and historical backfill so retry/classification stay aligned.
    Returns the last probe dict (includes classify_chat_probe fields) plus
    ``attempts`` count.
    """
    import time as _time

    _sleep = sleep_fn or _time.sleep
    _log = log or (lambda _m: None)
    max_attempts = max(1, int(max_attempts or 1))
    ch: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        ch = probe_mini_response(
            access_token,
            base_url=base_url,
            proxy=proxy,
            timeout=timeout,
            transport=transport,
        )
        # probe_mini_response already attaches classification; re-apply for safety.
        cls = classify_chat_probe(ch)
        for k, v in cls.items():
            ch.setdefault(k, v)
        # CPA gateway remap must win over raw 403→entitlement classification.
        if str(ch.get("transport_mode") or "") == "cpa":
            remapped = remap_cpa_gateway_failure(ch, ch)
            for k, v in remapped.items():
                ch[k] = v
        _log(
            f"probe chat attempt={attempt}/{max_attempts}: ok={ch.get('ok')} "
            f"status={ch.get('status')} entitlement_denied={ch.get('entitlement_denied')} "
            f"retryable={ch.get('retryable')} code={ch.get('error_code')!r} "
            f"mode={ch.get('transport_mode')!r} model={ch.get('model')} text={ch.get('text')!r}"
        )
        if ch.get("ok") or ch.get("entitlement_denied") or not ch.get("retryable"):
            ch["attempts"] = attempt
            return ch
        if attempt < max_attempts:
            _sleep(1.5 * attempt)
    ch["attempts"] = max_attempts
    return ch


def apply_chat_probe_to_result(
    result: dict[str, Any],
    ch: dict[str, Any] | None,
    *,
    models_missing: bool = False,
    models_status: int | None = None,
) -> dict[str, Any]:
    """Mutate mint/backfill result with chat probe classification fields.

    Keeps mint + backfill outcome fields identical for stamp/remint consumers.
    """
    if models_missing:
        result["ok"] = False
        result["chat_ok"] = False
        result["usable"] = False
        result["entitlement_denied"] = False
        result["chat_retryable"] = bool(
            models_status in (0, 408, 429, 500, 502, 503, 504)
        )
        result["fail_reason"] = "models_missing_grok_45"
        if not result.get("error"):
            result["error"] = "token ok but grok-4.5 not listed"
        return result

    ch = ch or {}
    result["probe_chat"] = ch
    result["chat_attempts"] = int(ch.get("attempts") or 0)
    result["chat_ok"] = bool(ch.get("ok"))
    result["entitlement_denied"] = bool(ch.get("entitlement_denied"))
    result["chat_retryable"] = bool(ch.get("retryable")) and not bool(ch.get("ok"))
    result["chat_error_code"] = ch.get("error_code") or ""
    if ch.get("ok"):
        result["ok"] = True
        result["usable"] = True
        result["chat_retryable"] = False
        result["entitlement_denied"] = False
        result["fail_reason"] = ""
        result.pop("error", None)
        return result

    result["ok"] = False
    result["usable"] = False
    if ch.get("entitlement_denied"):
        result["error"] = (
            "chat entitlement denied (permission-denied): "
            "account has no free Build chat grant; do not remint"
        )
        result["non_retryable"] = True
        result["chat_retryable"] = False
        result["fail_reason"] = "entitlement_denied"
    else:
        result["error"] = (
            f"chat probe failed: status={ch.get('status')} "
            f"code={ch.get('error_code') or ''} "
            f"{(ch.get('error') or '')[:200]}"
        )
        result["non_retryable"] = not bool(ch.get("retryable"))
        result["fail_reason"] = str(ch.get("reason") or "chat_failed")
        if ch.get("retryable"):
            result["chat_retryable"] = True
    return result


def probe_mini_response(
    access_token: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 60.0,
    proxy: str | None = None,
    transport: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    t = dict(
        transport
        or build_probe_transport(
            via="direct",
            upstream_base_url=base_url,
            access_token=access_token,
        )
    )
    base = str(t.get("base_url") or base_url).rstrip("/")
    url = f"{base}/responses"
    payload = {
        "model": "grok-4.5",
        "stream": False,
        "input": "Reply with exactly MINT_OK",
        "reasoning": {"effort": "low"},
    }
    headers = probe_request_headers(t, access_token=access_token)
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")
    opener = _opener(proxy)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    mode = str(t.get("mode") or "direct")
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
                "transport_mode": mode,
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
            "transport_mode": mode,
        }
        cls = classify_chat_probe(out)
        out.update(remap_cpa_gateway_failure(out, cls))
        return out
    except Exception as e:  # noqa: BLE001
        out = {
            "ok": False,
            "status": 0,
            "error": str(e),
            "transport_mode": mode,
        }
        out.update(classify_chat_probe(out))
        return out
