"""High-level: mint CPA xai-*.json for one free registered account."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .accounts import normalize_sso_cookie
from .browser_confirm import mint_with_browser
from .pkce_mint import PKCEMintError, mint_with_sso_pkce
from .probe import (
    apply_chat_probe_to_result,
    build_probe_transport,
    probe_chat_with_retries,
    probe_models,
    resolve_gate_probe_policy,
)
from .protocol_mint import ProtocolMintError, extract_sso_from_cookies, mint_with_sso_protocol
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth, credential_file_name
from .writer import stamp_auth_chat_fields, write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def _is_pkce_non_retryable(err: str | Exception | None) -> bool:
    """True when this PKCE failure must not remint-spin the same PKCE path.

    Covers more than empty SPA / action-id extract:
      - structured ``retryable=False`` (any code)
      - structured codes: consent_action_missing / consent_action_rejected /
        dependency / empty_sso / cancelled
      - legacy message needles for consent HTML / Server Action extract

    Residual policy is separate: non-retryable consent shells may still fall
    through to device/browser when ``allow_device_flow_fallback`` is True.
    Cancel is non-retryable *and* must short-circuit residual (see
    ``_is_cancelled_error``) — do not spend device/browser after abort.
    Distinct from transient network (token_exchange, state_mismatch, etc.).
    """
    if isinstance(err, PKCEMintError):
        if err.retryable is False:
            return True
        code = str(getattr(err, "code", "") or "").strip().lower()
        if code in {
            "consent_action_missing",
            "consent_action_rejected",
            "dependency",
            "empty_sso",
            "cancelled",
        }:
            return True
        # Structured True/unknown → use message needles below for residual classes
        err = str(err)
    s = (str(err) if err is not None else "").lower()
    if not s:
        return False
    needles = (
        "server action not found",
        "consent html missing",
        "submitoauth2consent",
        "action id",
        "action_id",
        "stale hardcoded fallback",
        "empty spa",
    )
    return any(n in s for n in needles)


def _is_cancelled_error(err: Exception | str | None) -> bool:
    """True when the failure is an explicit cancel/abort (no residual grant)."""
    if isinstance(err, PKCEMintError):
        if str(getattr(err, "code", "") or "").strip().lower() == "cancelled":
            return True
    s = (str(err) if err is not None else "").strip().lower()
    return s == "cancelled" or s.startswith("cancelled")


def _should_stamp_protocol_error(mint_method: str) -> bool:
    """Stamp prior PKCE/device failure reason when residual path produced tokens.

    Pure pkce/protocol primary success should not carry a protocol_error.
    Residuals (protocol_device, browser after protocol fail) should.
    """
    mm = (mint_method or "").strip().lower()
    return mm not in ("pkce", "protocol")


def _cancelled_result(
    email: str,
    *,
    mint_method: str,
    protocol_err: str | None = None,
    pkce_error_code: str | None = "cancelled",
) -> dict[str, Any]:
    """Stable cancelled fail dict — never residual after abort."""
    out: dict[str, Any] = {
        "ok": False,
        "email": email,
        "error": "cancelled",
        "mint_method": mint_method,
        "pkce_retryable": False,
    }
    if pkce_error_code:
        out["pkce_error_code"] = pkce_error_code
    if protocol_err:
        out["protocol_error"] = str(protocol_err)[:500]
    return out


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
    allow_device_flow_fallback: bool = True,
    protocol_flow: str = "pkce",
    priority: int = 1000,
    probe_via: str = "hybrid",
    cpa_probe_base_url: str = "",
    cpa_probe_api_key: str = "",
    probe_credential_pin: str = "",
    probe_pin_header: str = "X-CPA-Credential",
    allow_unpinned_cpa_gate: bool = False,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Full pipeline: (protocol PKCE | device residual | browser) → write CPA → probe.

    Protocol path (curl_cffi + sso cookie) is tried first when prefer_protocol
    and an sso cookie is available. ``protocol_flow`` selects the HTTP grant:
    ``pkce`` (default, authorization-code) or ``device`` (legacy device-code —
    known to yield /models-ok-but-chat-403).
    ``allow_device_flow_fallback`` (product default True) lets failed PKCE fall
    through to device flow for local disk residual; device tokens are often
    chat-denied — inject still requires chat_ok / entitlement hard gate.
    On protocol failure, falls back to browser mint unless protocol_only=True.

    ``mint_method`` taxonomy (disk + SUMMARY observability):
      - ``pkce`` — SSO HTTP authorization-code success
      - ``protocol`` — primary device flow (``protocol_flow=device``)
      - ``protocol_device`` — PKCE failed then device residual succeeded
      - ``browser`` — browser device-auth residual

    Dual stamp path: auth is written at create with ``mint_method`` /
    ``protocol_error`` via ``extra=``; after probe, ``stamp_auth_chat_fields``
    re-merges the same observability fields so probe patches never drop them.

    priority: CPA auth-file routing weight (CLIProxyAPI). Default 1000.

    Mid-tier probe (tebi CPA):
      - ``probe_via``: direct | cpa | hybrid (product default hybrid until pin proven).
      - Unpinned ``cpa`` never stamps chat_ok; policy falls back to hybrid
        (direct gate + optional observational ``probe_via_cpa_ok`` smoke).
      - Auth file ``base_url`` remains upstream cli-chat-proxy (argument ``base_url``),
        never the public CPA OpenAI base.

    Returns dict with keys: ok, path, email, probe_*, error?, mint_method?,
    entitlement_denied?, chat_retryable?, chat_ok?, probe_gate_via?,
    probe_via_cpa_ok?, probe_policy_reason?, protocol_error?.

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
        flow = (protocol_flow or "pkce").strip().lower()
        if flow not in {"pkce", "device"}:
            return {
                "ok": False,
                "email": email,
                "error": f"unsupported cpa_protocol_flow: {protocol_flow}; expected pkce or device",
                "mint_method": "protocol",
            }
        # Track whether we already attempted PKCE for residual fail labeling.
        pkce_attempted = False

        if flow == "pkce":
            log(
                "mint try protocol (SSO HTTP PKCE authorization-code; best-effort — "
                "empty consent SPA shell → residual device/browser)"
            )
            try:
                tokens = mint_with_sso_pkce(
                    sso_cookie=sso_val,
                    email=email,
                    proxy=resolved or None,
                    log=log,
                    cancel=cancel,
                )
                log("mint protocol PKCE SUCCESS")
            except PKCEMintError as e:
                protocol_err = str(e)
                pkce_attempted = True
                non_retry = _is_pkce_non_retryable(e)
                code = str(getattr(e, "code", "") or "").strip()
                log(
                    f"mint protocol PKCE failed"
                    f"{' (non-retryable)' if non_retry else ''}"
                    f"{f' code={code}' if code else ''}"
                    f": {e}"
                )
                # Cancel/abort: never residual to device or browser.
                if _is_cancelled_error(e) or (cancel and cancel()):
                    log("mint cancelled after PKCE — skip residual device/browser")
                    return _cancelled_result(
                        email,
                        mint_method="pkce",
                        protocol_err=protocol_err,
                        pkce_error_code=code or "cancelled",
                    )
                if allow_device_flow_fallback:
                    if non_retry:
                        log(
                            "mint best-effort residual → device flow "
                            "(PKCE non-retryable extract/consent; accept device residual)"
                        )
                    else:
                        log("mint fallback → device flow")
                else:
                    if protocol_only:
                        return {
                            "ok": False,
                            "email": email,
                            "error": f"pkce protocol failed: {protocol_err}",
                            "mint_method": "pkce",
                            "pkce_error_code": code or None,
                            "pkce_retryable": bool(e.retryable),
                        }
                    log(
                        "mint device residual disabled "
                        "(allow_device_flow_fallback=False) → browser if available"
                    )
            except Exception as e:  # noqa: BLE001
                protocol_err = str(e)
                pkce_attempted = True
                log(f"mint protocol PKCE exception: {e}")
                if _is_cancelled_error(e) or (cancel and cancel()):
                    log("mint cancelled after PKCE exception — skip residual")
                    return _cancelled_result(
                        email,
                        mint_method="pkce",
                        protocol_err=protocol_err,
                    )
                if allow_device_flow_fallback:
                    log("mint fallback → device flow")
                else:
                    if protocol_only:
                        return {
                            "ok": False,
                            "email": email,
                            "error": f"pkce protocol failed: {protocol_err}",
                            "mint_method": "pkce",
                        }
                    log(
                        "mint device residual disabled "
                        "(allow_device_flow_fallback=False) → browser if available"
                    )

        if tokens is None and (flow == "device" or allow_device_flow_fallback):
            # Re-check cancel before spending a device-code grant.
            if cancel and cancel():
                log("mint cancelled before device residual — skip")
                return _cancelled_result(
                    email,
                    mint_method="protocol_device" if pkce_attempted else "protocol",
                    protocol_err=protocol_err,
                )
            if flow == "device":
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
                # Taxonomy: primary device vs PKCE residual device.
                if pkce_attempted:
                    tokens["mint_method"] = "protocol_device"
                    tokens["protocol_error"] = str(protocol_err)[:500]
                    log("mint protocol device residual SUCCESS (mint_method=protocol_device)")
                else:
                    tokens["mint_method"] = "protocol"
                    log("mint protocol device-flow SUCCESS")
            except ProtocolMintError as e:
                device_err = str(e)
                log(f"mint protocol device-flow failed: {e}")
                protocol_err = (
                    f"pkce: {protocol_err}; device: {device_err}" if protocol_err else device_err
                )
                fail_mm = "protocol_device" if pkce_attempted else "protocol"
                if _is_cancelled_error(e) or (cancel and cancel()):
                    log("mint cancelled during device residual — skip browser")
                    return _cancelled_result(
                        email,
                        mint_method=fail_mm,
                        protocol_err=protocol_err,
                    )
                if protocol_only:
                    return {
                        "ok": False,
                        "email": email,
                        "error": f"protocol_only: {protocol_err}",
                        "mint_method": fail_mm,
                        "protocol_error": str(protocol_err)[:500],
                    }
                log("mint fallback → browser")
            except Exception as e:  # noqa: BLE001
                device_err = str(e)
                log(f"mint protocol device-flow exception: {e}")
                protocol_err = (
                    f"pkce: {protocol_err}; device: {device_err}" if protocol_err else device_err
                )
                fail_mm = "protocol_device" if pkce_attempted else "protocol"
                if _is_cancelled_error(e) or (cancel and cancel()):
                    log("mint cancelled during device residual exception — skip browser")
                    return _cancelled_result(
                        email,
                        mint_method=fail_mm,
                        protocol_err=protocol_err,
                    )
                if protocol_only:
                    return {
                        "ok": False,
                        "email": email,
                        "error": f"protocol_only: {protocol_err}",
                        "mint_method": fail_mm,
                        "protocol_error": str(protocol_err)[:500],
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
        # Abort must not open a browser residual grant.
        if cancel and cancel():
            log("mint cancelled before browser residual — skip")
            return _cancelled_result(
                email,
                mint_method="browser",
                protocol_err=protocol_err,
            )
        if protocol_err and _is_cancelled_error(protocol_err):
            log("mint cancelled (protocol_err) — skip browser residual")
            return _cancelled_result(
                email,
                mint_method="pkce",
                protocol_err=protocol_err,
            )
        if not password:
            return {
                "ok": False,
                "email": email,
                "error": protocol_err or "protocol failed and no password for browser fallback",
                "protocol_error": protocol_err,
                "mint_method": "protocol_device"
                if (prefer_protocol and sso_val and (protocol_flow or "pkce").strip().lower() == "pkce" and protocol_err)
                else ("protocol" if protocol_err else "browser"),
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
            if _is_cancelled_error(e) or (cancel and cancel()):
                return _cancelled_result(
                    email,
                    mint_method="browser",
                    protocol_err=protocol_err or str(e),
                )
            err = str(e)
            if protocol_err:
                err = f"{err} (protocol: {protocol_err})"
            return {
                "ok": False,
                "email": email,
                "error": err,
                "protocol_error": protocol_err,
                "mint_method": "browser",
            }

    try:
        pri = int(priority)
    except Exception:
        pri = 1000
    mint_method = str(tokens.get("mint_method") or "browser").strip() or "browser"
    extra_auth: dict[str, Any] = {"mint_method": mint_method}
    # Stamp prior protocol failure reason on residual success paths only
    # (protocol_device / browser after protocol fail). Pure pkce/protocol keep clean.
    if protocol_err and _should_stamp_protocol_error(mint_method):
        extra_auth["protocol_error"] = str(protocol_err)[:500]
    # Device residual may already carry protocol_error on tokens dict.
    if tokens.get("protocol_error") and "protocol_error" not in extra_auth:
        extra_auth["protocol_error"] = str(tokens.get("protocol_error"))[:500]
    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url,
        priority=pri,
        extra=extra_auth,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path} priority={pri} mint_method={mint_method}")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
        "mint_method": mint_method,
        "priority": pri,
    }
    if protocol_err and _should_stamp_protocol_error(mint_method):
        result["protocol_error"] = str(protocol_err)[:500]
    elif tokens.get("protocol_error"):
        result["protocol_error"] = str(tokens.get("protocol_error"))[:500]

    # Resolve product gate vs optional CPA observational smoke.
    # Auth write always uses upstream base_url (cli-chat-proxy), never CPA public host.
    pin = (probe_credential_pin or "").strip()
    if not pin and email:
        # Default pin identity is the CPA auth filename (safe ops handle).
        pin = credential_file_name(email=email)
    policy = resolve_gate_probe_policy(
        via=probe_via,
        cpa_base_url=cpa_probe_base_url,
        cpa_api_key=cpa_probe_api_key,
        credential_pin=pin if str(probe_via or "").strip().lower() != "direct" else "",
        allow_unpinned_cpa_gate=bool(allow_unpinned_cpa_gate),
    )
    result["probe_gate_via"] = policy.get("gate_via") or "direct"
    result["probe_policy_reason"] = policy.get("reason") or ""
    result["probe_via_cpa_ok"] = None
    log(
        f"probe policy: via={probe_via!r} gate={result['probe_gate_via']} "
        f"cpa_smoke={policy.get('cpa_smoke')} reason={result['probe_policy_reason']}"
    )

    gate_transport = build_probe_transport(
        via=str(policy.get("gate_via") or "direct"),
        upstream_base_url=base_url,
        cpa_base_url=cpa_probe_base_url,
        cpa_api_key=cpa_probe_api_key,
        access_token=tokens["access_token"],
        credential_pin=pin if str(policy.get("gate_via")) == "cpa" else "",
        pin_header=probe_pin_header or "X-CPA-Credential",
    )

    # Chat gate implies models probe (need has_grok_45 before /responses).
    run_models = bool(probe or probe_chat)
    if run_models:
        pr = probe_models(
            tokens["access_token"],
            base_url=base_url,
            proxy=resolved or None,
            transport=gate_transport,
        )
        result["probe_models"] = pr
        log(
            f"probe models: ok={pr.get('ok')} status={pr.get('status')} "
            f"has_grok_45={pr.get('has_grok_45')} ids={pr.get('model_ids')} "
            f"mode={pr.get('transport_mode')!r} "
            f"error={str(pr.get('error') or '')[:200]}"
        )
        if not pr.get("has_grok_45"):
            if probe_chat:
                apply_chat_probe_to_result(
                    result,
                    None,
                    models_missing=True,
                    models_status=int(pr.get("status") or 0),
                )
            else:
                result["ok"] = False
                result["error"] = "token ok but grok-4.5 not listed"
                result["chat_ok"] = False
                result["usable"] = False

        if probe_chat and pr.get("has_grok_45"):
            ch = probe_chat_with_retries(
                tokens["access_token"],
                base_url=base_url,
                proxy=resolved or None,
                max_attempts=3,
                log=log,
                transport=gate_transport,
            )
            apply_chat_probe_to_result(result, ch)
            if result.get("entitlement_denied"):
                log(
                    "FAIL-FAST: chat entitlement_denied — skip remint/retry for this account"
                )

    # Observational CPA mid-tier smoke: never replaces chat_ok for inject.
    if (
        policy.get("cpa_smoke")
        and result.get("chat_ok") is True
        and not result.get("entitlement_denied")
        and (cpa_probe_base_url or "").strip()
        and (cpa_probe_api_key or "").strip()
    ):
        cpa_t = build_probe_transport(
            via="cpa",
            upstream_base_url=base_url,
            cpa_base_url=cpa_probe_base_url,
            cpa_api_key=cpa_probe_api_key,
            access_token=tokens["access_token"],
            credential_pin=pin,
            pin_header=probe_pin_header or "X-CPA-Credential",
        )
        try:
            smoke = probe_chat_with_retries(
                tokens["access_token"],
                base_url=base_url,
                proxy=resolved or None,
                max_attempts=1,
                log=log,
                transport=cpa_t,
            )
            result["probe_cpa_smoke"] = smoke
            result["probe_via_cpa_ok"] = bool(smoke.get("ok"))
            log(
                f"cpa smoke: ok={smoke.get('ok')} status={smoke.get('status')} "
                f"code={smoke.get('error_code')!r} mode={smoke.get('transport_mode')!r}"
            )
        except Exception as e:  # noqa: BLE001
            result["probe_via_cpa_ok"] = False
            result["probe_cpa_smoke_error"] = str(e)
            log(f"cpa smoke failed: {e}")

    # Dual stamp: create-time write already put mint_method/protocol_error via
    # extra_auth. Re-stamp after probe so chat fields never drop mint observability
    # (stamp_auth_chat_fields merges result + updates; both carry mint_method).
    if result.get("path"):
        try:
            updates = {
                "probe_gate_via": result.get("probe_gate_via"),
                "probe_policy_reason": result.get("probe_policy_reason"),
            }
            if result.get("probe_via_cpa_ok") is True or result.get("probe_via_cpa_ok") is False:
                updates["probe_via_cpa_ok"] = bool(result.get("probe_via_cpa_ok"))
            if result.get("mint_method"):
                updates["mint_method"] = str(result.get("mint_method"))
            if result.get("protocol_error"):
                updates["protocol_error"] = str(result.get("protocol_error"))[:500]
            stamped = stamp_auth_chat_fields(result["path"], result, updates=updates)
            if stamped.get("import_gate"):
                result["import_gate"] = stamped["import_gate"]
        except Exception as e:  # noqa: BLE001
            log(f"stamp auth chat flags failed: {e}")
            result["stamp_error"] = str(e)
    return result
