"""Provider factory registry."""

from __future__ import annotations

from typing import Any, Callable

from register_core.providers.base import RegisterProvider

_FACTORY: dict[str, Callable[..., RegisterProvider]] = {}
_BUILTINS_READY = False


def register_provider(name: str, factory: Callable[..., RegisterProvider]) -> None:
    _FACTORY[name.strip().lower()] = factory


def list_providers() -> list[str]:
    _ensure_builtins()
    return sorted(_FACTORY.keys())


def get_provider(name: str, **kwargs: Any) -> RegisterProvider:
    _ensure_builtins()
    key = name.strip().lower()
    aliases = {
        "xai": "grok",
        "xiaomi": "mimo",
        "mimo-tts": "mimo",
    }
    key = aliases.get(key, key)
    if key not in _FACTORY:
        raise KeyError(f"unknown provider: {name!r}; known={list_providers()}")
    return _FACTORY[key](**kwargs)


def _ensure_builtins() -> None:
    global _BUILTINS_READY, _FACTORY
    if _BUILTINS_READY:
        return
    from register_core.providers.grok_adapter import GrokProvider
    from register_core.providers.mimo_adapter import MimoProvider

    built: dict[str, Callable[..., RegisterProvider]] = {
        "grok": lambda **kw: GrokProvider(**kw),
        "mimo": lambda **kw: MimoProvider(**kw),
    }
    _FACTORY = {**built, **_FACTORY}
    _BUILTINS_READY = True
