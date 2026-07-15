"""Product registration provider protocol."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from register_core.contracts import RegisterResult
from register_core.email.base import EmailSource


@runtime_checkable
class RegisterProvider(Protocol):
    """One product signup flow (Grok, MiMo, …)."""

    name: str

    def register_one(
        self,
        *,
        email_source: EmailSource | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RegisterResult:
        """Run a single account registration end-to-end.

        Implementations may ignore email_source when they own mailbox logic
        internally (e.g. MiMo Node runner using tinyhost). Prefer accepting
        EmailSource for new pure-Python providers.
        """
        ...
