"""Grok chat verify — honest deferred / shape gate (live probe opt-in later)."""

from __future__ import annotations

from typing import Any

from register_core.contracts import RegisterResult, VerifyResult


class GrokChatVerifier:
    """Honest Grok gate for register_core.

    Default: require this-run SSO cookie shape. Does **not** claim free Build chat
    entitlement — that remains `cpa_xai.probe` / production CLI. Soft-pass without
    SSO was misleading (secret_kind=pending still looked like product success).
    """

    name = "grok_chat"

    def __init__(self, *, live: bool = False, require_sso: bool = True, **_: Any) -> None:
        self.live = live
        self.require_sso = require_sso

    def verify(self, result: RegisterResult) -> VerifyResult:
        if not result.ok:
            return VerifyResult(
                ok=False,
                provider="grok",
                capability="grok_chat_build",
                detail="register not ok",
            )
        if not result.email:
            return VerifyResult(
                ok=False,
                provider="grok",
                capability="grok_chat_build",
                detail="missing this-run email",
            )
        if self.require_sso and not (result.secret or "").strip():
            return VerifyResult(
                ok=False,
                provider="grok",
                capability="grok_register_ledger",
                detail="missing sso secret (pending); not product-ready",
                meta={"secret_kind": result.secret_kind or "pending"},
            )
        if self.live:
            return VerifyResult(
                ok=False,
                provider="grok",
                capability="grok_chat_build",
                detail="live grok verify not wired in register_core; use cpa_xai.probe",
            )
        # SSO present this-run; chat entitlement still deferred to cpa_xai.
        return VerifyResult(
            ok=True,
            provider="grok",
            capability="grok_sso_ledger",
            detail="sso_captured_deferred_chat_probe",
            meta={"secret_kind": result.secret_kind or "sso"},
        )
