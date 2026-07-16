"""Email source protocol: allocate mailbox + poll OTP.

Convention (optional, not part of Protocol signature):
  After ``poll_otp`` succeeds or raises ``MailMissError``, implementations may
  set ``self.last_wait_diagnostics`` to an ``OtpWaitDiagnostics`` instance so
  adapters can attach ``artifacts["otp_wait"]`` without changing call sites.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from register_core.contracts import Mailbox, OtpCode


@runtime_checkable
class EmailSource(Protocol):
    """One mailbox provider (duckmail, gmail_imap, tinyhost, hotmail, …)."""

    name: str

    def allocate(self) -> Mailbox:
        """Create or reserve one address for a single attempt."""
        ...

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
        """Block until a fresh OTP arrives or raise MailMissError."""
        ...

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        """Optional cleanup / return alias to pool. Default no-op OK."""
        ...
