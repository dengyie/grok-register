"""Email source factory registry."""

from __future__ import annotations

from typing import Any, Callable

from register_core.email.base import EmailSource

_FACTORY: dict[str, Callable[..., EmailSource]] = {}
_BUILTINS_READY = False


def register_email_source(name: str, factory: Callable[..., EmailSource]) -> None:
    key = name.strip().lower()
    _FACTORY[key] = factory


def list_email_sources() -> list[str]:
    _ensure_builtins()
    return sorted(_FACTORY.keys())


def get_email_source(name: str, **kwargs: Any) -> EmailSource:
    _ensure_builtins()
    key = (name or "auto").strip().lower()
    if key == "auto":
        for candidate in ("tinyhost", "duckmail", "gmail_imap"):
            if candidate in _FACTORY:
                return _FACTORY[candidate](**kwargs)
        raise KeyError("no email source registered for auto")
    if key not in _FACTORY:
        raise KeyError(f"unknown email source: {name!r}; known={list_email_sources()}")
    return _FACTORY[key](**kwargs)


def _ensure_builtins() -> None:
    global _BUILTINS_READY, _FACTORY
    if _BUILTINS_READY:
        return
    from register_core.email.sources.duckmail import DuckmailSource
    from register_core.email.sources.gmail_imap import GmailImapSource
    from register_core.email.sources.legacy_grok import LegacyGrokEmailSource
    from register_core.email.sources.tinyhost import TinyhostSource

    built: dict[str, Callable[..., EmailSource]] = {
        "tinyhost": lambda **kw: TinyhostSource(**kw),
        "duckmail": lambda **kw: DuckmailSource(**kw),
        "gmail": lambda **kw: GmailImapSource(**kw),
        "gmail_imap": lambda **kw: GmailImapSource(**kw),
        "legacy_grok": lambda **kw: LegacyGrokEmailSource(**kw),
        "hotmail": lambda **kw: LegacyGrokEmailSource(provider="hotmail", **kw),
    }
    # Atomic publish so a mid-import failure does not leave a half registry.
    _FACTORY = {**built, **_FACTORY}
    _BUILTINS_READY = True
