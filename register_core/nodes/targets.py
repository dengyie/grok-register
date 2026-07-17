"""Provider → business-domain probe targets for L2 preflight.

L1 (ipify) proves generic egress. L2 proves the proxy can open the registration
target path (any HTTP status counts as transport success).
"""

from __future__ import annotations

import os
from typing import Any, Iterable

# Strategy-group analogue: one default business URL per provider family.
DEFAULT_PROVIDER_PROBE_TARGETS: dict[str, tuple[str, ...]] = {
    "grok": ("https://accounts.x.ai/",),
    "xai": ("https://accounts.x.ai/",),
    "chatgpt": ("https://auth.openai.com/",),
    "openai": ("https://auth.openai.com/",),
    "mimo": ("https://api.xiaomimimo.com/",),
    "xiaomi": ("https://api.xiaomimimo.com/",),
    "mimo-tts": ("https://api.xiaomimimo.com/",),
}

_DISABLE_TOKENS = frozenset({"", "0", "none", "off", "false", "no", "-"})


def _split_urls(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        text = str(raw).strip()
        if not text:
            return []
        # Disable tokens as sole value
        if text.lower() in _DISABLE_TOKENS:
            return []
        items = [p.strip() for p in text.replace(";", ",").split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item.lower() in _DISABLE_TOKENS:
            continue
        if not item.lower().startswith(("http://", "https://")):
            # bare host → https URL for GET probe
            item = "https://" + item.lstrip("/")
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def resolve_probe_targets(
    extra: dict[str, Any] | None = None,
    *,
    provider: str | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Resolve L2 business probe URLs.

    Override order (highest first):
      1. extra["probe_targets"] / extra["nodes_probe_targets"]
      2. env REGISTER_NODES_PROBE_TARGETS / NODES_PROBE_TARGETS
      3. provider map (extra provider / _provider / explicit provider=)
      4. empty → L1-only (legacy)

    Explicit disable: ``0`` / ``none`` / empty list via extra or env.
    """
    ex = dict(extra or {})
    environ = env if env is not None else os.environ

    for key in ("probe_targets", "nodes_probe_targets"):
        if key in ex and ex[key] is not None:
            return _split_urls(ex[key])

    for env_key in ("REGISTER_NODES_PROBE_TARGETS", "NODES_PROBE_TARGETS"):
        if env_key in environ:
            return _split_urls(environ.get(env_key))

    name = (
        provider
        or ex.get("provider")
        or ex.get("_provider")
        or ""
    )
    name = str(name).strip().lower()
    if name in DEFAULT_PROVIDER_PROBE_TARGETS:
        return list(DEFAULT_PROVIDER_PROBE_TARGETS[name])
    return []


def provider_target_summary(targets: Iterable[str]) -> str:
    items = list(targets)
    if not items:
        return "L1-only"
    return ",".join(items)
