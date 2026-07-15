"""Post-register verification protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from register_core.contracts import RegisterResult, VerifyResult


@runtime_checkable
class Verifier(Protocol):
    name: str

    def verify(self, result: RegisterResult) -> VerifyResult:
        ...
