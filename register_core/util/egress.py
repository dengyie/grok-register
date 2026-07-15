"""User-selectable egress backend switch.

Backends
--------
- ``core``   — project-embedded mihomo (``.nodes/``, mixed-port 17897)
- ``clash``  — external Clash Verge / mihomo (default mixed-port 7897 + optional API rotate)
- ``list``   — only HTTP/SOCKS pool (``nodes.json`` / ``PROXY_LIST``), no protocol core
- ``direct`` — no proxy
- ``auto``   — list → core → clash fixed URL → direct (legacy convenience)

Persist preference in ``.nodes/config/egress.mode`` (gitignored with ``.nodes/``)
or override per-run via env / CLI.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

VALID = ("auto", "core", "clash", "list", "direct", "off")
ALIASES = {
    "mihomo": "core",
    "project": "core",
    "embedded": "core",
    "nodes-core": "core",
    "verge": "clash",
    "external": "clash",
    "system": "clash",
    "proxy_list": "list",
    "nodes": "list",
    "pool": "list",
    "none": "direct",
    "off": "direct",
    "0": "direct",
    "false": "direct",
}

DEFAULT_CLASH_PROXY = "http://127.0.0.1:7897"

_ROOT = Path(__file__).resolve().parents[2]


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def preference_path() -> Path:
    env = _env_first("REGISTER_NODES_HOME", "NODES_HOME")
    home = Path(os.path.expanduser(env)).resolve() if env else (_ROOT / ".nodes").resolve()
    return home / "config" / "egress.mode"


def read_persisted_backend() -> str:
    p = preference_path()
    if not p.is_file():
        return ""
    try:
        return normalize_backend(p.read_text(encoding="utf-8").strip())
    except Exception:
        return ""


def write_persisted_backend(backend: str) -> Path:
    backend = normalize_backend(backend)
    if backend not in VALID and backend not in {"core", "clash", "list", "direct", "auto"}:
        raise ValueError(f"invalid egress backend: {backend}")
    p = preference_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(backend + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return p


def normalize_backend(raw: str | None) -> str:
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    s = ALIASES.get(s, s)
    if s == "off":
        s = "direct"
    return s


def resolve_backend(extra: dict[str, Any] | None = None) -> str:
    """Resolve egress backend: extra > env > persisted file > auto."""
    extra = extra if isinstance(extra, dict) else {}
    raw = (
        extra.get("egress")
        or extra.get("egress_backend")
        or extra.get("proxy_backend")
        or _env_first(
            "REGISTER_EGRESS",
            "EGRESS_BACKEND",
            "CHATGPT_EGRESS",
            "PROXY_BACKEND",
            "REGISTER_PROXY_BACKEND",
        )
        or read_persisted_backend()
        or "auto"
    )
    backend = normalize_backend(str(raw))
    if backend not in {"auto", "core", "clash", "list", "direct"}:
        backend = "auto"
    return backend


def clash_proxy_url(extra: dict[str, Any] | None = None) -> str:
    extra = extra if isinstance(extra, dict) else {}
    return str(
        extra.get("clash_proxy")
        or extra.get("proxy")
        or _env_first(
            "CLASH_PROXY",
            "CHATGPT_PROXY",
            "MIMO_PROXY",
            "https_proxy",
            "HTTPS_PROXY",
            "http_proxy",
            "HTTP_PROXY",
            default=DEFAULT_CLASH_PROXY,
        )
        or DEFAULT_CLASH_PROXY
    ).strip()


def describe(backend: str | None = None) -> dict[str, Any]:
    b = backend or resolve_backend()
    return {
        "backend": b,
        "persisted": read_persisted_backend() or None,
        "preference_path": str(preference_path()),
        "choices": ["auto", "core", "clash", "list", "direct"],
        "meaning": {
            "auto": "PROXY_LIST or healthy nodes.json → project core → clash(if set) → direct",
            "core": "project mihomo only (.nodes, :17897)",
            "clash": "external mixed-port only (default :7897; advanced)",
            "list": "HTTP/SOCKS catalog only (nodes.json / PROXY_LIST)",
            "direct": "no proxy",
        }.get(b, ""),
        "primary": ["list", "core", "direct"],
        "advanced": ["auto", "clash"],
    }
