"""Email layer: pluggable mailbox sources."""

from __future__ import annotations

from register_core.email.base import EmailSource
from register_core.email.registry import get_email_source, list_email_sources, register_email_source

__all__ = [
    "EmailSource",
    "get_email_source",
    "list_email_sources",
    "register_email_source",
]
