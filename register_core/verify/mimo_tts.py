"""Optional MiMo TTS key shape check (no live billable call by default)."""

from __future__ import annotations

import re
from typing import Any

from register_core.contracts import RegisterResult, VerifyResult
from register_core.errors import VerifyError

_KEY_RE = re.compile(r"^sk-[A-Za-z0-9]{20,}$")


class MimoTtsVerifier:
    name = "mimo_tts"

    def __init__(self, *, live: bool = False, **_: Any) -> None:
        self.live = live

    def verify(self, result: RegisterResult) -> VerifyResult:
        if not result.ok:
            return VerifyResult(ok=False, provider="mimo", capability="mimo_tts", detail="register not ok")
        secret = result.secret or ""
        if not _KEY_RE.match(secret):
            return VerifyResult(
                ok=False,
                provider="mimo",
                capability="mimo_tts",
                detail=f"key shape invalid len={len(secret)}",
            )
        if not self.live:
            return VerifyResult(
                ok=True,
                provider="mimo",
                capability="mimo_tts",
                detail="shape_ok (live probe off)",
                meta={"key_len": len(secret)},
            )
        # Live probe left as Manual-required / opt-in to avoid surprise cost.
        raise VerifyError("mimo live TTS probe not enabled in this milestone; set live=False")
