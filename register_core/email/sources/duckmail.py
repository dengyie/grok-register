"""DuckMail source — thin adapter; full HTTP lives in grok_register_ttk when available."""

from __future__ import annotations

from typing import Any

from register_core.contracts import Mailbox, OtpCode
from register_core.errors import FailFastError, MailMissError, ProviderError


class DuckmailSource:
    name = "duckmail"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def _reg(self):
        try:
            import grok_register_ttk as reg  # type: ignore
        except Exception as exc:
            raise FailFastError(f"duckmail requires grok_register_ttk: {exc}") from exc
        return reg

    def allocate(self) -> Mailbox:
        reg = self._reg()
        # Force duckmail path without mutating global if possible
        prev = None
        try:
            prev = reg.config.get("email_provider")
            reg.config["email_provider"] = "duckmail"
            address, token = reg.get_email_and_token()
        except Exception as exc:
            raise ProviderError(f"duckmail allocate failed: {exc}") from exc
        finally:
            if prev is not None:
                reg.config["email_provider"] = prev
        return Mailbox(address=address, token=token or "", provider=self.name)

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
            prev = reg.config.get("email_provider")
            reg.config["email_provider"] = "duckmail"
            code = reg.get_oai_code(
                mailbox.token,
                mailbox.address,
                timeout=int(timeout_s),
                poll_interval=int(poll_interval_s),
            )
        except Exception as exc:
            raise MailMissError(f"duckmail OTP failed: {exc}") from exc
        finally:
            if prev is not None:
                reg.config["email_provider"] = prev
        if not code:
            raise MailMissError(f"duckmail empty OTP for {mailbox.address}")
        if used_codes and code in used_codes:
            raise MailMissError(f"duckmail OTP already used: {code}")
        return OtpCode(code=str(code), source=self.name)

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        return
