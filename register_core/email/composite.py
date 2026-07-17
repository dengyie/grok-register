"""Composite EmailSource = MailboxProvider + OtpDecoder (compat for adapters)."""

from __future__ import annotations

from typing import Any

from register_core.contracts import Mailbox, OtpCode
from register_core.decode.base import OtpDecoder
from register_core.mailbox.base import MailboxProvider


class CompositeEmailSource:
    """Façade implementing the legacy EmailSource shape.

    Existing providers (ChatGPT) call allocate/poll_otp/release. Profile-driven
    runs inject this object so mailbox and decode can be configured separately
    while adapters stay unchanged.
    """

    def __init__(
        self,
        mailbox: MailboxProvider,
        decoder: OtpDecoder,
        *,
        name: str | None = None,
    ) -> None:
        self.mailbox = mailbox
        self.decoder = decoder
        mname = getattr(mailbox, "name", "mailbox")
        dname = getattr(decoder, "name", "decode")
        if name:
            self.name = name
        elif mname == dname:
            self.name = str(mname)
        else:
            self.name = f"{mname}+{dname}"
        self.last_wait_diagnostics = None
        # Snapshot-only attrs silently freeze mailbox.proxy at construction time.
        # Adapters that set mailbox.proxy after wrap (or set composite.proxy) must
        # forward live; use properties that prefer override, then live mailbox.
        self._proxy_override: str | None = None
        self._forced_domain_override: str | None = None

    @property
    def proxy(self) -> str:
        if self._proxy_override is not None:
            return self._proxy_override
        return str(getattr(self.mailbox, "proxy", "") or "")

    @proxy.setter
    def proxy(self, value: str) -> None:
        self._proxy_override = str(value or "")
        if hasattr(self.mailbox, "proxy"):
            try:
                self.mailbox.proxy = self._proxy_override
            except Exception:
                pass

    @property
    def forced_domain(self) -> str | None:
        if self._forced_domain_override is not None:
            return self._forced_domain_override
        return getattr(self.mailbox, "forced_domain", None)

    @forced_domain.setter
    def forced_domain(self, value: str | None) -> None:
        self._forced_domain_override = value
        if hasattr(self.mailbox, "forced_domain"):
            try:
                self.mailbox.forced_domain = value
            except Exception:
                pass

    def allocate(self) -> Mailbox:
        return self.mailbox.allocate()

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
        otp = self.decoder.wait_otp(
            mailbox,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            used_codes=used_codes,
            newer_than_epoch=newer_than_epoch,
            sender_hint=sender_hint,
        )
        self.last_wait_diagnostics = getattr(
            self.decoder, "last_wait_diagnostics", None
        )
        return otp

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        self.mailbox.release(mailbox, success=success)

    def __repr__(self) -> str:
        return f"CompositeEmailSource(name={self.name!r})"
