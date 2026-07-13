"""High-level: mint CPA xai-*.json for one free registered account."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .accounts import normalize_sso_cookie
from .browser_confirm import mint_with_browser
from .probe import classify_chat_probe, probe_mini_response, probe_models
from .protocol_mint import ProtocolMintError, extract_sso_from_cookies, mint_with_sso_protocol
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth
from .writer import patch_cpa_xai_auth, write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def mint_and_export(
    *,
    email: str,
    password: str,
    auth_dir: str | Path,
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    probe: bool = True,
    probe_chat: bool = True,
    browser_timeout_sec: float = 240.0,
    force_standalone: bool = True,
    cookies: Any | None = None,
    sso: str | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
    prefer_protocol: bool = True,
    protocol_only: bool = False,
    protocol_poll_timeout_sec: float = 90.0,
    priority: int = 1000,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Full pipeline: (protocol SSO device-flow |) browser device-auth → write CPA → probe.

    Protocol path (curl_cffi + sso cookie) is tried first when prefer_protocol
    and an sso cookie is available. On failure, falls back to browser mint unless
    protocol_only=True.

    priority: CPA auth-file routing weight (CLIProxyAPI). Default 1000.

    Returns dict with keys: ok, path, email, probe_*, error?, mint_method?,
    entitlement_denied?, chat_retryable?, chat_ok?.

    Product rule: when probe_chat=True, models-only success is not enough —
    free Build chat must pass /v1/responses. 403 permission-denied is
    non-retryable entitlement failure (do not remint-spin).
    """
    log = log or _noop
    email = (email or "").strip()
    if not email or not password:
        # Protocol can work with sso alone; password only required for browser fallback
        if not email:
            return {"ok": False, "email": email, "error": "missing email"}
        if not (sso or extract_sso_from_cookies(cookies)):
            return {"ok": False, "email": email, "error": "missing email/password"}

    # Config/explicit proxy wins over shell https_proxy (common 7890 trap).
    # Thread-local pin — safe under concurrent mint workers.
    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    log(f"mint start: {email} proxy={proxy_log_label(resolved) or '(none)'}")

    # Normalize at mint core so every caller (export, backfill, GUI) is safe.
    sso_val = normalize_sso_cookie(sso) or extract_sso_from_cookies(cookies)
    tokens: dict[str, Any] | None = None
    protocol_err: str | None = None

    if prefer_protocol and sso_val:
        log("mint try protocol (SSO HTTP device flow)")
        try:
            tokens = mint_with_sso_protocol(
                sso_cookie=sso_val,
                email=email,
                proxy=resolved or None,
                poll_timeout_sec=protocol_poll_timeout_sec,
                log=log,
                cancel=cancel,
            )
            log("mint protocol SUCCESS")
        except ProtocolMintError as e:
            protocol_err = str(e)
            log(f"mint protocol failed: {e}")
            if protocol_only:
                return {
                    "ok": False,
                    "email": email,
                    "error": f"protocol_only: {e}",
                    "mint_method": "protocol",
                }
            log("mint fallback → browser")
        except Exception as e:  # noqa: BLE001
            protocol_err = str(e)
            log(f"mint protocol exception: {e}")
            if protocol_only:
                return {
                    "ok": False,
                    "email": email,
                    "error": f"protocol_only: {e}",
                    "mint_method": "protocol",
                }
            log("mint fallback → browser")
    elif prefer_protocol and not sso_val:
        log("mint protocol skipped (no sso cookie) → browser")
        if protocol_only:
            return {
                "ok": False,
                "email": email,
                "error": "protocol_only but no sso cookie",
                "mint_method": "protocol",
            }
    elif not prefer_protocol:
        log("mint protocol disabled → browser")

    if tokens is None:
        if not password:
            return {
                "ok": False,
                "email": email,
                "error": protocol_err or "protocol failed and no password for browser fallback",
                "protocol_error": protocol_err,
            }
        try:
            tokens = mint_with_browser(
                email=email,
                password=password,
                page=None if force_standalone else page,
                proxy=resolved or None,
                headless=headless,
                browser_timeout_sec=browser_timeout_sec,
                force_standalone=force_standalone,
                cookies=cookies,
                reuse_browser=reuse_browser,
                recycle_every=recycle_every,
                poll_log=log,
                cancel=cancel,
            )
            tokens["mint_method"] = "browser"
            if protocol_err:
                tokens["protocol_error"] = protocol_err
        except Exception as e:  # noqa: BLE001
            log(f"mint failed: {e}")
            err = str(e)
            if protocol_err:
                err = f"{err} (protocol: {protocol_err})"
            return {
                "ok": False,
                "email": email,
                "error": err,
                "protocol_error": protocol_err,
            }

    try:
        pri = int(priority)
    except Exception:
        pri = 1000
    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url,
        priority=pri,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path} priority={pri}")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
        "mint_method": tokens.get("mint_method") or "browser",
        "priority": pri,
    }
    if protocol_err and result["mint_method"] != "protocol":
        result["protocol_error"] = protocol_err

    # Chat gate implies models probe (need has_grok_45 before /responses).
    run_models = bool(probe or probe_chat)
    chat_attempts = 0
    if run_models:
        pr = probe_models(tokens["access_token"], base_url=base_url, proxy=resolved or None)
        result["probe_models"] = pr
        log(
            f"probe models: ok={pr.get('ok')} status={pr.get('status')} "
            f"has_grok_45={pr.get('has_grok_45')} ids={pr.get('model_ids')} "
            f"error={str(pr.get('error') or '')[:200]}"
        )
        if not pr.get("has_grok_45"):
            result["ok"] = False
            result["error"] = "token ok but grok-4.5 not listed"
            result["chat_ok"] = False
            result["usable"] = False
            if probe_chat:
                result["entitlement_denied"] = False
                result["chat_retryable"] = bool(
                    pr.get("status") in (0, 408, 429, 500, 502, 503, 504)
                )
                result["fail_reason"] = "models_missing_grok_45"

        if probe_chat and pr.get("has_grok_45"):
            # Transient (429/5xx/network): retry a few times before hard outcome.
            max_attempts = 3
            ch: dict[str, Any] = {}
            for attempt in range(1, max_attempts + 1):
                chat_attempts = attempt
                ch = probe_mini_response(
                    tokens["access_token"], base_url=base_url, proxy=resolved or None
                )
                cls = classify_chat_probe(ch)
                for k, v in cls.items():
                    ch.setdefault(k, v)
                log(
                    f"probe chat attempt={attempt}/{max_attempts}: ok={ch.get('ok')} "
                    f"status={ch.get('status')} entitlement_denied={ch.get('entitlement_denied')} "
                    f"retryable={ch.get('retryable')} code={ch.get('error_code')!r} "
                    f"model={ch.get('model')} text={ch.get('text')!r}"
                )
                if ch.get("ok") or ch.get("entitlement_denied") or not ch.get("retryable"):
                    break
                if attempt < max_attempts:
                    time.sleep(1.5 * attempt)
            result["probe_chat"] = ch
            result["chat_attempts"] = chat_attempts
            result["chat_ok"] = bool(ch.get("ok"))
            result["entitlement_denied"] = bool(ch.get("entitlement_denied"))
            result["chat_retryable"] = bool(ch.get("retryable")) and not bool(ch.get("ok"))
            result["chat_error_code"] = ch.get("error_code") or ""
            if not ch.get("ok"):
                result["ok"] = False
                if ch.get("entitlement_denied"):
                    result["error"] = (
                        "chat entitlement denied (permission-denied): "
                        "account has no free Build chat grant; do not remint"
                    )
                    result["non_retryable"] = True
                    result["chat_retryable"] = False
                    result["usable"] = False
                    result["fail_reason"] = "entitlement_denied"
                    log(
                        "FAIL-FAST: chat entitlement_denied — skip remint/retry for this account"
                    )
                else:
                    result["error"] = (
                        f"chat probe failed: status={ch.get('status')} "
                        f"code={ch.get('error_code') or ''} "
                        f"{(ch.get('error') or '')[:200]}"
                    )
                    result["non_retryable"] = not bool(ch.get("retryable"))
                    result["fail_reason"] = str(ch.get("reason") or "chat_failed")
                    result["usable"] = False
                    # Keep chat_retryable so remint can re-probe without waiting for expiry.
                    if ch.get("retryable"):
                        result["chat_retryable"] = True
            else:
                result["usable"] = True
                result["chat_retryable"] = False
                result["entitlement_denied"] = False

    # Stamp local auth so remint/ops can skip denied and re-probe transient.
    if result.get("path"):
        if result.get("entitlement_denied"):
            import_gate = "entitlement_denied"
        elif result.get("chat_ok") is True and result.get("usable") is not False:
            import_gate = "chat_ok"
        elif result.get("chat_retryable"):
            import_gate = str(result.get("fail_reason") or "transient")
        elif result.get("chat_ok") is False:
            import_gate = str(result.get("fail_reason") or "chat_not_ok")
        else:
            import_gate = str(result.get("fail_reason") or ("ok" if result.get("ok") else "not_ready"))
        result["import_gate"] = import_gate
        stamp = {
            "chat_ok": result.get("chat_ok"),
            "usable": result.get("usable", result.get("ok")),
            "entitlement_denied": bool(result.get("entitlement_denied")),
            "chat_retryable": bool(result.get("chat_retryable")),
            "fail_reason": result.get("fail_reason") or "",
            "chat_error_code": result.get("chat_error_code") or "",
            "import_gate": import_gate,
        }
        # Drop empty optional noise
        if not stamp["fail_reason"]:
            stamp.pop("fail_reason", None)
        if not stamp["chat_error_code"]:
            stamp.pop("chat_error_code", None)
        try:
            patch_cpa_xai_auth(result["path"], stamp)
        except Exception as e:  # noqa: BLE001
            log(f"stamp auth chat flags failed: {e}")
    return result
