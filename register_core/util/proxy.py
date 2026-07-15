"""Register-scoped egress selection for layered providers.

Self-controlled nodes (preferred):
  1. Project catalog ``nodes.json`` / ``REGISTER_NODES_FILE`` via ``register_core.nodes``
  2. Explicit ``PROXY_LIST`` / ``proxy_list`` / ``CHATGPT_PROXY_LIST``
  3. Single fixed ``CHATGPT_PROXY`` / ``proxy`` URL

None of the above require Clash/mihomo UI. Clash mode remains an optional
legacy path for Grok browser only — never the ChatGPT default.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

log = logging.getLogger("register_core.util.proxy")

LogFn = Callable[[str], None] | None

_CONFIGURED = False


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def _load_nodes_proxy_list() -> str:
    """Pull enabled URLs from project-owned node catalog (empty if none)."""
    disable = _env_first("REGISTER_NODES", "USE_NODES", default="1").lower()
    if disable in {"0", "false", "off", "no"}:
        return ""
    try:
        from register_core.nodes import get_manager

        mgr = get_manager()
        return mgr.as_proxy_list_value(healthy_only=False)
    except Exception as exc:
        log.debug("nodes catalog unavailable: %s", exc)
        return ""


def _load_core_proxy_url() -> str:
    """Ensure project mihomo core and return its mixed-port URL (or empty)."""
    mode = _env_first("REGISTER_CORE", "USE_CORE", "REGISTER_MIHOMO", default="auto").lower()
    if mode in {"0", "false", "off", "no"}:
        return ""
    try:
        from register_core.nodes import core_runtime as core

        st = core.status()
        if not st.get("bin_exists") or not st.get("config_exists"):
            if mode in {"1", "true", "on", "yes", "require", "force"}:
                log.warning("project mihomo core missing bin/config under .nodes/")
            return ""
        if mode == "auto" and not st.get("running"):
            # auto: only attach if already running, unless REGISTER_CORE_AUTOSTART=1
            autostart = _env_first("REGISTER_CORE_AUTOSTART", "CORE_AUTOSTART", default="1").lower()
            if autostart not in {"1", "true", "yes", "on"}:
                return ""
        url = core.ensure_proxy_url(start_core=True)
        return (url or "").strip()
    except Exception as exc:
        log.debug("project core unavailable: %s", exc)
        return ""


def rotation_config_from_env_and_extra(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build proxy_rotate.configure() dict from extra + env + nodes catalog.

    Priority for pool: extra.proxy_list > env PROXY_LIST > nodes.json.
    If a proxy list is present and mode is unset, auto-select ``list``.
    """
    extra = extra if isinstance(extra, dict) else {}

    mode_raw = str(
        extra.get("proxy_rotate_mode")
        if extra.get("proxy_rotate_mode") is not None
        else _env_first("CHATGPT_PROXY_ROTATE_MODE", "PROXY_ROTATE_MODE")
    ).strip().lower()
    mode_explicit = bool(mode_raw)
    mode = mode_raw
    if mode in {"none", "disabled", "0", "false", ""}:
        mode = "off"

    proxy_list = (
        extra.get("proxy_list")
        or extra.get("proxy_pool")
        or _env_first("CHATGPT_PROXY_LIST", "PROXY_LIST", "PROXY_POOL")
        or ""
    )
    nodes_source = ""
    core_source = ""
    if not proxy_list:
        nodes_source = _load_nodes_proxy_list()
        if nodes_source:
            proxy_list = nodes_source
    # Embedded mihomo core local mixed port (protocol nodes from YAML).
    # Used when no explicit pool; can also be merged if REGISTER_CORE_MERGE=1.
    if not proxy_list:
        core_source = _load_core_proxy_url()
        if core_source:
            proxy_list = core_source
    elif _env_first("REGISTER_CORE_MERGE", default="").lower() in {"1", "true", "yes", "on"}:
        core_source = _load_core_proxy_url()
        if core_source and core_source not in str(proxy_list):
            proxy_list = f"{proxy_list},{core_source}" if proxy_list else core_source

    base_proxy = str(
        extra.get("proxy")
        or _env_first(
            "CHATGPT_PROXY",
            "MIMO_PROXY",
            "https_proxy",
            "HTTPS_PROXY",
            "http_proxy",
            "HTTP_PROXY",
        )
        or ""
    ).strip()
    # If pool from nodes/core and no fixed proxy, seed base with first URL for status.
    if not base_proxy and proxy_list:
        first = str(proxy_list).split(",")[0].strip()
        if first:
            base_proxy = first
    if not base_proxy and core_source:
        base_proxy = core_source

    # Self-control default: unset mode + explicit pool ⇒ list (no Clash selector).
    # Explicit proxy_rotate_mode=off stays off even if a list is present.
    if not mode_explicit and proxy_list:
        mode = "list"
    if mode in {"proxy_list", "pool", "url", "urls", "nodes"}:
        mode = "list"

    every_raw = extra.get("proxy_rotate_every")
    if every_raw is None or every_raw == "":
        every_raw = _env_first("CHATGPT_PROXY_ROTATE_EVERY", "PROXY_ROTATE_EVERY", default="1")
    try:
        every = max(1, int(every_raw))
    except Exception:
        every = 1

    required_raw = extra.get("proxy_rotate_required")
    if required_raw is None:
        required_raw = _env_first("CHATGPT_PROXY_ROTATE_REQUIRED", "PROXY_ROTATE_REQUIRED", default="")
    if isinstance(required_raw, bool):
        required = required_raw
    else:
        required = str(required_raw).strip().lower() in {"1", "true", "yes", "on"}

    on_start_raw = extra.get("proxy_rotate_on_start")
    if on_start_raw is None:
        on_start_raw = _env_first(
            "CHATGPT_PROXY_ROTATE_ON_START", "PROXY_ROTATE_ON_START", default=""
        )
    if on_start_raw is None or on_start_raw == "":
        on_start = mode in {"list", "clash"}
    elif isinstance(on_start_raw, bool):
        on_start = on_start_raw
    else:
        on_start = str(on_start_raw).strip().lower() in {"1", "true", "yes", "on"}

    cfg: dict[str, Any] = {
        "proxy_rotate_mode": mode,
        "proxy_rotate_every": every,
        "proxy_rotate_on_start": on_start,
        "proxy_rotate_required": required,
        "proxy_list": proxy_list,
        "proxy": base_proxy,
        "proxy_rotate_update_cpa": False,
        "nodes_pool": bool(nodes_source),
        "core_pool": bool(core_source),
    }

    # Optional clash knobs only if operator explicitly chose clash.
    if mode == "clash":
        cfg["clash_api"] = _env_first("CLASH_API", "CLASH_CONTROLLER") or None
        cfg["clash_secret"] = _env_first("CLASH_SECRET")
        cfg["clash_proxy_group"] = _env_first("CLASH_GROUP", "CLASH_PROXY_GROUP") or None
        cfg["clash_rule_domains"] = (
            extra.get("clash_rule_domains")
            or _env_first("CLASH_DOMAINS", "CLASH_RULE_DOMAINS")
            or None
        )
        # Drop Nones so proxy_rotate keeps its defaults.
        cfg = {k: v for k, v in cfg.items() if v is not None}

    return cfg


def configure_rotation_once(
    extra: dict[str, Any] | None = None,
    *,
    log_fn: LogFn = None,
    force: bool = False,
) -> dict[str, Any]:
    """Configure process-wide ProxyRotator once (idempotent unless force)."""
    global _CONFIGURED
    from proxy_rotate import configure_proxy_rotation

    cfg = rotation_config_from_env_and_extra(extra)
    if _CONFIGURED and not force:
        return cfg

    def _log(msg: str) -> None:
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass
        else:
            log.info("%s", msg)

    configure_proxy_rotation(cfg, log=_log)
    _CONFIGURED = True
    return cfg


def reset_rotation_for_tests() -> None:
    """Test helper: allow re-configure in the same process."""
    global _CONFIGURED
    _CONFIGURED = False


def resolve_attempt_proxy(
    extra: dict[str, Any] | None = None,
    *,
    log_fn: LogFn = None,
) -> tuple[str, dict[str, Any]]:
    """Rotate (if enabled) and return (proxy_url, rotate_info) for one attempt.

    List mode: returns the concrete pool URL — self-controlled node.
    Clash mode: returns base_proxy (usually local mixed port); node switch is
    side-effect on Clash dedicated group only.
    Off mode: returns explicit/extra/env proxy unchanged.
    """
    from proxy_rotate import current_proxy_override, maybe_rotate_proxy

    cfg = configure_rotation_once(extra, log_fn=log_fn)
    info = maybe_rotate_proxy(log=log_fn, config=cfg)

    override = (current_proxy_override() or "").strip()
    if override:
        return override, info

    # clash / off: use configured base proxy from extra/env
    base = str(
        (extra or {}).get("proxy")
        or cfg.get("proxy")
        or _env_first(
            "CHATGPT_PROXY",
            "MIMO_PROXY",
            "https_proxy",
            "HTTPS_PROXY",
            "http_proxy",
            "HTTP_PROXY",
        )
        or ""
    ).strip()
    return base, info


def inject_attempt_proxy(extra: dict[str, Any] | None = None, *, log_fn: LogFn = None) -> dict[str, Any]:
    """Return a shallow-copied extra dict with ``proxy`` set for this attempt."""
    base = dict(extra or {})
    proxy, info = resolve_attempt_proxy(base, log_fn=log_fn)
    if proxy:
        base["proxy"] = proxy
    if info:
        base["_proxy_rotate"] = {
            k: info.get(k)
            for k in (
                "rotated",
                "mode",
                "label",
                "index",
                "pool_size",
                "group",
                "error",
                "scope",
            )
            if k in info
        }
    return base
