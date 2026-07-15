"""Bridge to grok_register_ttk config-driven email_provider (duckmail/gmail/hotmail/…)."""

from __future__ import annotations

from typing import Any

from register_core.contracts import Mailbox, OtpCode
from register_core.errors import FailFastError, MailMissError, ProviderError


class LegacyGrokEmailSource:
    """Uses whatever email_provider is in grok config (or forced name)."""

    def __init__(self, provider: str | None = None, **kwargs: Any) -> None:
        self.forced_provider = (provider or "").strip().lower() or None
        self.name = self.forced_provider or "legacy_grok"
        self.kwargs = kwargs

    def _reg(self):
        try:
            import grok_register_ttk as reg  # type: ignore
        except Exception as exc:
            raise FailFastError(f"legacy_grok email requires grok_register_ttk: {exc}") from exc
        return reg

    def allocate(self) -> Mailbox:
        reg = self._reg()
        prev = None
        try:
            if self.forced_provider:
                prev = reg.config.get("email_provider")
                reg.config["email_provider"] = self.forced_provider
            address, token = reg.get_email_and_token()
        except Exception as exc:
            msg = str(exc)
            if any(k in msg.lower() for k in ("alias", "耗尽", "exhausted", "no available")):
                raise FailFastError(f"email pool fatal: {msg}") from exc
            raise ProviderError(f"legacy allocate failed: {exc}") from exc
        finally:
            if prev is not None:
                reg.config["email_provider"] = prev
        return Mailbox(
            address=address,
            token=token or "",
            provider=self.forced_provider or str(reg.config.get("email_provider") or "legacy_grok"),
        )

    def poll_otp(
        self,
        mailbox: Mailbox,
        *,
        timeout_s: float = 180,
        poll_interval_s: float = 3,
        used_codes: set[str] | None = None,
        newer_than_epoch: float | None = None,
        sender_hint: str | None = None,
    ) -> OtpCode:
        reg = self._reg()
        prev = None
        try:
            if self.forced_provider:
                prev = reg.config.get("email_provider")
                reg.config["email_provider"] = self.forced_provider
            code = reg.get_oai_code(
                mailbox.token,
                mailbox.address,
                timeout=int(timeout_s),
                poll_interval=int(poll_interval_s),
            )
        except Exception as exc:
            raise MailMissError(f"legacy OTP failed: {exc}") from exc
        finally:
            if prev is not None:
                reg.config["email_provider"] = prev
        if not code:
            raise MailMissError(f"legacy empty OTP for {mailbox.address}")
        if used_codes and str(code) in used_codes:
            raise MailMissError(f"legacy OTP already used: {code}")
        return OtpCode(code=str(code), source=self.name)

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        try:
            reg = self._reg()
            # best-effort; ttk has hotmail/gmail release helpers
            if hasattr(reg, "_hotmail_release_alias"):
                try:
                    reg._hotmail_release_alias(mailbox.address)  # type: ignore[attr-defined]
                except Exception:
                    pass
            if hasattr(reg, "_gmail_cleanup_email"):
                try:
                    reg._gmail_cleanup_email(mailbox.address)  # type: ignore[attr-defined]
                except Exception:
                    pass
        except Exception:
            return
