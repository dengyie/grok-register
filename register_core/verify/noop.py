from __future__ import annotations

from register_core.contracts import RegisterResult, VerifyResult


class NoopVerifier:
    name = "noop"

    def verify(self, result: RegisterResult) -> VerifyResult:
        return VerifyResult(
            ok=result.ok,
            provider=result.provider,
            capability="none",
            detail="verify skipped",
        )
