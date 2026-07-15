"""Gmail IMAP catch-all source via existing grok_register_ttk helpers."""

from __future__ import annotations

from typing import Any

from register_core.contracts import Mailbox, OtpCode
from register_core.errors import FailFastError, MailMissError, ProviderError


class GmailImapSource:
    name = "gmail_imap"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def _reg(self):
        try:
            import grok_register_ttk as reg  # type: ignore
        except Exception as exc:
            raise FailFastError(f"gmail_imap requires grok_register_ttk: {exc}") from exc
        return reg

    def allocate(self) -> Mailbox:
        reg = self._reg()
        prev = None
        try:
            prev = reg.config.get("email_provider")
            reg.config["email_provider"] = "gmail"
            address, token = reg.get_email_and_token()
        except Exception as exc:
            raise ProviderError(f"gmail allocate failed: {exc}") from exc
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
            reg.config["email_provider"] = "gmail"
            code = reg.get_oai_code(
                mailbox.token,
                mailbox.address,
                timeout=int(timeout_s),
                poll_interval=int(poll_interval_s),
            )
        except Exception as exc:
            raise MailMissError(f"gmail OTP failed: {exc}") from exc
        finally:
            if prev is not None:
                reg.config["email_provider"] = prev
        if not code:
            raise MailMissError(f"gmail empty OTP for {mailbox.address}")
        if used_codes and code in used_codes:
            raise MailMissError(f"gmail OTP already used: {code}")
        return OtpCode(code=str(code), source=self.name)

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        try:
            reg = self._reg()
            if hasattr(reg, "_gmail_cleanup_email"):
                reg._gmail_cleanup_email(mailbox.address)  # type: ignore[attr-defined]
        except Exception:
            return
