"""Verification layer."""

from __future__ import annotations

from register_core.verify.base import Verifier
from register_core.verify.noop import NoopVerifier
from register_core.verify.registry import get_verifier

__all__ = ["Verifier", "NoopVerifier", "get_verifier"]
