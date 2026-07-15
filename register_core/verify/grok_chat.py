"""Grok chat verify — honest deferred / shape gate (live probe opt-in later)."""

from __future__ import annotations

from typing import Any

from register_core.contracts import RegisterResult, VerifyResult


class GrokChatVerifier:
    name = "grok_chat"

    def __init__(self, *, live: bool = False, require_sso: bool = False, **_: Any) -> None:
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
        if self.require_sso and not result.secret:
            return VerifyResult(
                ok=False,
                provider="grok",
                capability="grok_chat_build",
                detail="missing sso secret",
            )
        if self.live:
            return VerifyResult(
                ok=False,
                provider="grok",
                capability="grok_chat_build",
                detail="live grok verify not wired in register_core; use cpa_xai.probe",
            )
        # Soft pass: registration ledger row exists; chat entitlement not proven.
        return VerifyResult(
            ok=True,
            provider="grok",
            capability="grok_register_ledger",
            detail="register_ok_deferred_chat_probe",
            meta={"secret_kind": result.secret_kind},
        )
