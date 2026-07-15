from __future__ import annotations

from typing import Any

from register_core.verify.base import Verifier
from register_core.verify.noop import NoopVerifier


def get_verifier(name: str, **kwargs: Any) -> Verifier:
    key = (name or "noop").strip().lower()
    if key in ("", "noop", "none", "skip"):
        return NoopVerifier()
    if key in ("mimo", "mimo_tts", "tts"):
        from register_core.verify.mimo_tts import MimoTtsVerifier

        return MimoTtsVerifier(**kwargs)
    if key in ("grok", "grok_chat", "chat"):
        from register_core.verify.grok_chat import GrokChatVerifier

        return GrokChatVerifier(**kwargs)
    raise KeyError(f"unknown verifier: {name!r}")
