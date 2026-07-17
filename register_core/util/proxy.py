"""Register-scoped egress selection for layered providers.

User switch (``REGISTER_EGRESS`` / ``--egress`` / ``nodes egress set``):

- ``core``   — project-embedded mihomo (``.nodes/``)
- ``clash``  — external Clash Verge / system mixed port
- ``list``   — HTTP/SOCKS pool only (``nodes.json`` / ``PROXY_LIST``)
- ``direct`` — no proxy
- ``auto``   — list → core → clash URL → direct
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from register_core.util.egress import (
    clash_proxy_url,
    describe as describe_egress,
    normalize_backend,
    resolve_backend,
)

log = logging.getLogger("register_core.util.proxy")

LogFn = Callable[[str], None] | None

_CONFIGURED = False


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        val = os.environ.get(name)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def _load_nodes_proxy_list(*, healthy_only: bool | None = None) -> str:
    """Pull enabled URLs from project-owned node catalog (empty if none).

    ``healthy_only``:
      - False → all enabled URLs (explicit ``egress=list``)
      - True  → manager healthy filter (skips hard-failed)
      - None  → **auto purity**: only ``last_ok is True``; unprobed/dirty dumps
                return empty so auto falls through to project core
    """
    disable = _env_first("REGISTER_NODES", "USE_NODES", default="1").lower()
    if disable in {"0", "false", "off", "no"}:
        return ""
    try:
        from register_core.nodes import get_manager

        mgr = get_manager()
        if healthy_only is None:
            urls = [
                n.url
                for n in mgr.enabled_nodes(healthy_only=False)
                if n.last_ok is True and n.url
            ]
            return ",".join(urls)
        return mgr.as_proxy_list_value(healthy_only=healthy_only)
    except Exception as exc:
        log.debug("nodes catalog unavailable: %s", exc)
        return ""


def _load_core_proxy_url(*, require: bool = False, autostart: bool | None = None) -> str:
    """Ensure project mihomo core and return its mixed-port URL (or empty)."""
    try:
        from register_core.nodes import core_runtime as core

        st = core.status()
        if not st.get("bin_exists") or not st.get("config_exists"):
            if require:
                log.warning("project mihomo core missing bin/config under .nodes/")
            return ""
        if autostart is None:
            autostart = _env_first("REGISTER_CORE_AUTOSTART", "CORE_AUTOSTART", default="1").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if not st.get("running") and not autostart and not require:
            return ""
        url = core.ensure_proxy_url(start_core=True)
        return (url or "").strip()
    except Exception as exc:
        if require:
            log.warning("project core unavailable: %s", exc)
        else:
            log.debug("project core unavailable: %s", exc)
        return ""


def rotation_config_from_env_and_extra(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build proxy_rotate.configure() dict from extra + env + egress backend switch."""
    extra = extra if isinstance(extra, dict) else {}
    backend = resolve_backend(extra)

    # Legacy rotate mode can still force clash-group rotation when backend=clash.
    mode_raw = str(
        extra.get("proxy_rotate_mode")
        if extra.get("proxy_rotate_mode") is not None
        else _env_first("CHATGPT_PROXY_ROTATE_MODE", "PROXY_ROTATE_MODE")
    ).strip().lower()
    mode_explicit = bool(mode_raw)
    mode = mode_raw
    if mode in {"none", "disabled", "0", "false", ""}:
        mode = "off"
    if mode in {"proxy_list", "pool", "url", "urls", "nodes", "core"}:
        mode = "list"

    explicit_list = (
        extra.get("proxy_list")
        or extra.get("proxy_pool")
        or _env_first("CHATGPT_PROXY_LIST", "PROXY_LIST", "PROXY_POOL")
        or ""
    )
    nodes_source = ""
    core_source = ""
    clash_source = ""
    proxy_list = ""
    base_proxy = ""
    source = "none"

    if backend == "direct":
        mode = "off"
        source = "direct"
    elif backend == "list":
        proxy_list = str(explicit_list or "")
        if not proxy_list:
            # explicit list backend: use full enabled catalog (operator chose list)
            nodes_source = _load_nodes_proxy_list(healthy_only=False)
            proxy_list = nodes_source
        base_proxy = str(extra.get("proxy") or "").strip()
        if not base_proxy and proxy_list:
            base_proxy = str(proxy_list).split(",")[0].strip()
        if not mode_explicit:
            mode = "list" if proxy_list else "off"
        source = "list"
    elif backend == "core":
        # Project mini-core only — do not fall back to Clash.
        core_source = _load_core_proxy_url(require=True, autostart=True)
        proxy_list = core_source
        base_proxy = core_source
        if not mode_explicit:
            mode = "list" if core_source else "off"
        source = "core"
    elif backend == "clash":
        clash_source = clash_proxy_url(extra)
        base_proxy = clash_source
        # Optional: if user also set PROXY_LIST while on clash backend, ignore unless explicit list mode.
        if mode_explicit and mode == "list" and explicit_list:
            proxy_list = str(explicit_list)
            if not base_proxy and proxy_list:
                base_proxy = str(proxy_list).split(",")[0].strip()
        else:
            # Prefer clash controller rotation when available; else fixed mixed port.
            if not mode_explicit:
                # If clash API configured, use clash rotate; else fixed off+proxy URL.
                api = _env_first("CLASH_API", "CLASH_CONTROLLER")
                mode = "clash" if api else "off"
            proxy_list = ""
        source = "clash"
    else:
        # auto: explicit PROXY_LIST → healthy nodes.json → core → clash(if set) → direct
        # Unprobed/dirty bulk nodes.json must NOT block project core.
        proxy_list = str(explicit_list or "")
        if proxy_list:
            source = "list"
        else:
            nodes_source = _load_nodes_proxy_list(healthy_only=None)
            if nodes_source:
                proxy_list = nodes_source
                source = "list"
        if not proxy_list:
            # honor REGISTER_CORE off inside auto
            core_mode = _env_first("REGISTER_CORE", "USE_CORE", "REGISTER_MIHOMO", default="auto").lower()
            if core_mode not in {"0", "false", "off", "no"}:
                core_source = _load_core_proxy_url(
                    require=core_mode in {"1", "true", "on", "yes", "require", "force"},
                    autostart=True,
                )
                if core_source:
                    proxy_list = core_source
                    source = "core"
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
        if not base_proxy and proxy_list:
            base_proxy = str(proxy_list).split(",")[0].strip()
        if not base_proxy and not proxy_list:
            # last resort external clash port if listening intent via env default
            # only when user has CHATGPT_PROXY/MIMO_PROXY style or CLASH_PROXY set
            if _env_first("CLASH_PROXY", "USE_CLASH", default=""):
                clash_source = clash_proxy_url(extra)
                base_proxy = clash_source
                source = "clash"
        if not mode_explicit:
            if proxy_list:
                mode = "list"
            elif source == "clash":
                api = _env_first("CLASH_API", "CLASH_CONTROLLER")
                mode = "clash" if api else "off"
            else:
                mode = "off"

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
        "egress_backend": backend,
        "egress_source": source,
        "proxy_rotate_mode": mode,
        "proxy_rotate_every": every,
        "proxy_rotate_on_start": on_start,
        "proxy_rotate_required": required,
        "proxy_list": proxy_list,
        "proxy": base_proxy,
        "proxy_rotate_update_cpa": False,
        "nodes_pool": bool(nodes_source),
        "core_pool": bool(core_source) or source == "core",
        "clash_pool": source == "clash" or bool(clash_source),
    }

    if mode == "clash" or backend == "clash":
        cfg["clash_api"] = _env_first("CLASH_API", "CLASH_CONTROLLER") or None
        cfg["clash_secret"] = _env_first("CLASH_SECRET")
        cfg["clash_proxy_group"] = _env_first("CLASH_GROUP", "CLASH_PROXY_GROUP") or None
        cfg["clash_rule_domains"] = (
            extra.get("clash_rule_domains")
            or _env_first("CLASH_DOMAINS", "CLASH_RULE_DOMAINS")
            or None
        )
        if not cfg.get("proxy"):
            cfg["proxy"] = clash_proxy_url(extra)
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

    _log(
        f"[egress] backend={cfg.get('egress_backend')} source={cfg.get('egress_source')} "
        f"rotate={cfg.get('proxy_rotate_mode')} proxy={_redact(cfg.get('proxy') or '')}"
    )
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
    """Rotate (if enabled) and return (proxy_url, rotate_info) for one attempt."""
    from proxy_rotate import current_proxy_override, maybe_rotate_proxy

    cfg = configure_rotation_once(extra, log_fn=log_fn)
    info = maybe_rotate_proxy(log=log_fn, config=cfg)
    if isinstance(info, dict):
        info = {
            **info,
            "egress_backend": cfg.get("egress_backend"),
            "egress_source": cfg.get("egress_source"),
        }

    override = (current_proxy_override() or "").strip()
    if override:
        return override, info

    base = str(
        (extra or {}).get("proxy")
        or cfg.get("proxy")
        or ""
    ).strip()
    # In direct backend force empty even if env has proxy leftovers — only when
    # resolve_backend says direct and extra didn't set proxy explicitly for clash tests.
    if cfg.get("egress_backend") == "direct" and not (extra or {}).get("proxy"):
        return "", info
    if not base and cfg.get("egress_backend") != "direct":
        base = _env_first(
            "CHATGPT_PROXY",
            "MIMO_PROXY",
            "https_proxy",
            "HTTPS_PROXY",
            "http_proxy",
            "HTTP_PROXY",
        )
    return base, info


def inject_attempt_proxy(extra: dict[str, Any] | None = None, *, log_fn: LogFn = None) -> dict[str, Any]:
    """Return a shallow-copied extra dict with ``proxy`` set for this attempt."""
    base = dict(extra or {})
    proxy, info = resolve_attempt_proxy(base, log_fn=log_fn)
    if proxy:
        base["proxy"] = proxy
    elif base.get("egress") in {"direct", "off"} or resolve_backend(base) == "direct":
        base.pop("proxy", None)
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
                "egress_backend",
                "egress_source",
            )
            if k in info
        }
    return base


def _env_truthy_name(*names: str, default: bool = False) -> bool:
    raw = _env_first(*names, default="")
    if raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "y"}


def _nodes_preflight_enabled(extra: dict[str, Any] | None) -> bool:
    extra = extra if isinstance(extra, dict) else {}
    if "nodes_preflight" in extra:
        v = extra.get("nodes_preflight")
        if isinstance(v, bool):
            return v
        return str(v or "").strip().lower() in {"1", "true", "yes", "on"}
    return _env_truthy_name(
        "REGISTER_NODES_PREFLIGHT",
        "NODES_PREFLIGHT",
        default=True,
    )


def _nodes_required_for_backend(backend: str, extra: dict[str, Any] | None) -> bool:
    """Whether zero healthy dialable nodes must stop the batch."""
    extra = extra if isinstance(extra, dict) else {}
    if "nodes_required" in extra:
        v = extra.get("nodes_required")
        if isinstance(v, bool):
            return v
        return str(v or "").strip().lower() in {"1", "true", "yes", "on"}
    if _env_truthy_name("REGISTER_NODES_REQUIRED", "NODES_REQUIRED", default=False):
        return True
    # explicit list backend with catalog intent: empty healthy pool is fatal
    if backend == "list":
        return True
    return False


def preflight_nodes_for_register(
    extra: dict[str, Any] | None = None,
    *,
    log_fn: LogFn = None,
) -> dict[str, Any]:
    """Probe project nodes and seed register rotation with healthy-only pool.

    Called once before a pipeline batch (product gate for imported catalogs). Rules:
    - Only when backend is ``list`` or ``auto`` and nodes catalog is enabled
    - Skipped for ``core`` / ``clash`` / ``direct`` (no HTTP catalog rotation)
    - Explicit ``PROXY_LIST`` / ``CHATGPT_PROXY_LIST`` skips catalog probe (operator-owned pool)
      unless ``force_nodes_preflight`` is set
    - ``REGISTER_NODES_PREFLIGHT=0`` disables probe (not recommended)
    - Skip reasons are always logged for operators
    - On ``list`` (or required), zero healthy nodes → FailFastError
    - Mutates a copy of ``extra``: sets ``proxy_list`` to healthy URLs and forces reconfig
    """
    from register_core.errors import FailFastError

    base = dict(extra or {})
    backend = resolve_backend(base)

    # already preflighted this extra payload
    if base.get("_nodes_preflight_done"):
        return base

    def _log(msg: str) -> None:
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass
        else:
            log.info("%s", msg)

    if backend in {"direct", "core", "clash"}:
        base["_nodes_preflight_done"] = True
        base["_nodes_preflight"] = {
            "skipped": True,
            "reason": f"backend={backend}",
            "healthy": 0,
        }
        _log(
            f"[nodes] preflight skipped: backend={backend} "
            "(catalog live-probe only applies to egress=list|auto)"
        )
        return base

    # explicit PROXY_LIST from operator skips catalog probe (they own the pool)
    explicit_list = (
        base.get("proxy_list")
        or base.get("proxy_pool")
        or _env_first("CHATGPT_PROXY_LIST", "PROXY_LIST", "PROXY_POOL")
        or ""
    )
    nodes_disabled = _env_first("REGISTER_NODES", "USE_NODES", default="1").lower() in {
        "0",
        "false",
        "off",
        "no",
    }
    if nodes_disabled:
        base["_nodes_preflight_done"] = True
        base["_nodes_preflight"] = {"skipped": True, "reason": "REGISTER_NODES=0", "healthy": 0}
        _log("[nodes] preflight skipped: REGISTER_NODES=0 (catalog disabled)")
        return base

    if str(explicit_list).strip() and not base.get("force_nodes_preflight"):
        # operator-provided list: still optional probe if they ask; default skip network gate
        base["_nodes_preflight_done"] = True
        base["_nodes_preflight"] = {
            "skipped": True,
            "reason": "explicit_proxy_list",
            "healthy": 0,
        }
        _log(
            "[nodes] preflight skipped: operator PROXY_LIST/CHATGPT_PROXY_LIST owns the pool "
            "(catalog not probed; set force_nodes_preflight=1 or clear PROXY_LIST to use "
            "nodes.json live-probe → healthy-only rotation)"
        )
        return base

    if not _nodes_preflight_enabled(base):
        # still prefer known-healthy for auto; list without preflight uses full catalog
        if backend == "auto":
            healthy = _load_nodes_proxy_list(healthy_only=None)
            if healthy:
                base["proxy_list"] = healthy
                base["egress"] = "list"
        base["_nodes_preflight_done"] = True
        base["_nodes_preflight"] = {"skipped": True, "reason": "preflight_disabled", "healthy": 0}
        _log(
            "[nodes] preflight skipped: REGISTER_NODES_PREFLIGHT=0 "
            "(not recommended for imported catalogs)"
        )
        configure_rotation_once(base, log_fn=log_fn, force=True)
        return base

    try:
        from register_core.nodes import get_manager

        mgr = get_manager()
    except Exception as exc:
        if _nodes_required_for_backend(backend, base):
            raise FailFastError(f"nodes catalog unavailable: {exc}") from exc
        base["_nodes_preflight_done"] = True
        base["_nodes_preflight"] = {
            "skipped": True,
            "reason": f"catalog_unavailable:{exc}",
            "healthy": 0,
        }
        _log(f"[nodes] preflight skipped: catalog unavailable: {exc}")
        return base

    timeout_raw = base.get("nodes_probe_timeout") or _env_first(
        "REGISTER_NODES_PROBE_TIMEOUT", "NODES_PROBE_TIMEOUT", default="12"
    )
    try:
        timeout = max(3.0, float(timeout_raw))
    except Exception:
        timeout = 12.0
    limit_raw = base.get("nodes_probe_limit")
    if limit_raw is None or limit_raw == "":
        limit_raw = _env_first("REGISTER_NODES_PROBE_LIMIT", "NODES_PROBE_LIMIT", default="")
    limit: int | None
    # None → NodeManager.preflight default budget (40); 0 → unlimited
    try:
        if limit_raw is None or str(limit_raw).strip() == "":
            limit = None  # manager default
        else:
            parsed = int(limit_raw)
            limit = None if parsed == 0 else max(1, parsed)
    except Exception:
        limit = None

    # L2 business targets (strategy-group analogue). Empty → L1-only legacy.
    try:
        from register_core.nodes.targets import (
            provider_target_summary,
            resolve_probe_targets,
        )

        probe_urls = resolve_probe_targets(base)
    except Exception:
        probe_urls = []
        provider_target_summary = lambda _t: "L1-only"  # noqa: E731

    summary = mgr.preflight(
        timeout=timeout,
        log=_log,
        persist=True,
        limit=limit,
        probe_urls=probe_urls or None,
    )
    healthy_list = summary.get("proxy_list") or ""
    healthy_n = int(summary.get("healthy") or 0)
    targets_label = provider_target_summary(summary.get("probe_targets") or probe_urls)

    if healthy_n <= 0:
        msg = (
            f"nodes preflight found 0 healthy proxies "
            f"(probed={summary.get('probed')} fail={summary.get('fail')} "
            f"targets={targets_label} path={summary.get('path')})"
        )
        if _nodes_required_for_backend(backend, base) or backend == "list":
            raise FailFastError(msg)
        # auto: fall through to core without list pool
        _log(f"[nodes] {msg}; falling through to core/auto")
        base["_nodes_preflight_done"] = True
        base["_nodes_preflight"] = {
            **summary,
            "skipped": False,
            "healthy": 0,
            "probe_targets": list(summary.get("probe_targets") or probe_urls),
            "l2_enabled": bool(probe_urls),
        }
        # ensure empty list doesn't block core: leave proxy_list unset
        base.pop("proxy_list", None)
        configure_rotation_once(base, log_fn=log_fn, force=True)
        return base

    # Seed rotation with only dual-pass (or L1-only) URLs; force list mode for this batch.
    base["proxy_list"] = healthy_list
    if backend == "auto":
        base["egress"] = "list"
    if not base.get("proxy_rotate_mode"):
        base["proxy_rotate_mode"] = "list"
    base["_nodes_preflight_done"] = True
    base["_nodes_preflight"] = {
        "skipped": False,
        "ok": summary.get("ok"),
        "fail": summary.get("fail"),
        "healthy": healthy_n,
        "probed": summary.get("probed"),
        "path": summary.get("path"),
        "probe_targets": list(summary.get("probe_targets") or probe_urls),
        "l2_enabled": bool(summary.get("l2_enabled") or probe_urls),
    }
    _log(
        f"[nodes] preflight ready healthy={healthy_n} targets={targets_label} → list rotation"
    )
    configure_rotation_once(base, log_fn=log_fn, force=True)
    return base


_PROXY_FAIL_MARKERS = (
    "proxy",
    "connect",
    "connection",
    "timeout",
    "timed out",
    "tunnel",
    "socks",
    "curl:",
    "curl error",
    "network",
    "unreachable",
    "refused",
    "reset by peer",
    "connection reset",
    "connection_reset",
    "err_empty_response",
    "empty response",
    "empty_response",
    "ssl",
    "tls",
    "eof",
    "name or service not known",
    "nodename nor servname",
    "failed to connect",
    "proxyerror",
    "max retries",
    "temporarily unavailable",
)


def is_proxy_network_failure(
    *,
    ok: bool,
    error: str = "",
    error_kind: str = "",
) -> bool:
    """Heuristic: treat attempt failure as likely egress/proxy damage (quarantine candidate)."""
    if ok:
        return False
    kind = (error_kind or "").strip().lower()
    # Business / mailbox failures must never burn nodes.
    if kind in {
        "mail_miss",
        "captcha",
        "verify",
        "fatal",
        "registration_disallowed",
        "disallowed",
    }:
        return False
    if kind in {"proxy", "network", "egress", "timeout"}:
        return True
    text = (error or "").lower()
    if not text:
        return False
    # OpenAI risk rejection is not a dead proxy.
    if "registration_disallowed" in text or (
        "disallowed" in text and "registration" in text
    ):
        return False
    return any(m in text for m in _PROXY_FAIL_MARKERS)


def report_attempt_proxy_result(
    extra: dict[str, Any] | None,
    *,
    ok: bool,
    error: str = "",
    error_kind: str = "",
    log_fn: LogFn = None,
) -> dict[str, Any]:
    """Feed registration outcome back into node health + live rotation pool.

    - success → clear fail_count on that URL
    - proxy/network failure → mark fail; if quarantined, drop from rotator list and force next
    - non-proxy failure → no quarantine (avoid killing good IPs for OTP/captcha/risk)
    """
    base = dict(extra or {})
    proxy = str(base.get("proxy") or "").strip()
    info: dict[str, Any] = {
        "proxy": _redact(proxy),
        "ok": ok,
        "marked": False,
        "quarantined": False,
        "removed_from_pool": False,
    }
    if not proxy:
        return info

    # only mark nodes that live in the project catalog (not core/clash fixed local ports)
    try:
        from register_core.nodes import get_manager

        mgr = get_manager()
        node = mgr.find_by_url(proxy)
    except Exception as exc:
        info["error"] = str(exc)
        return info

    if node is None:
        info["reason"] = "not_in_catalog"
        return info

    def _cool_seconds(env_name: str, default: float) -> float:
        raw = _env_first(env_name, default=str(default))
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return float(default)

    kind = (error_kind or "").strip().lower()
    network_fail = is_proxy_network_failure(ok=ok, error=error, error_kind=error_kind)

    if ok:
        mgr.mark_result(proxy, ok=True, error="", persist=True)
        info["marked"] = True
        info["action"] = "success_clear"
        # Clear soft cool on success so the node re-enters rotation immediately.
        try:
            if node.cooldown_until is not None:
                node.cooldown_until = None
                node.cooldown_reason = ""
                try:
                    from register_core.nodes.catalog import save_nodes

                    save_nodes(mgr.nodes, mgr.path)
                except Exception:
                    pass
        except Exception:
            pass
        per_use = _cool_seconds("REGISTER_NODES_COOLDOWN_PER_USE", 0.0)
        if per_use > 0:
            mgr.cooldown(proxy, per_use, reason="per_use", persist=True)
            info["cooldown_s"] = per_use
            info["action"] = "success_clear_per_use_cool"
        return info

    # Business failures: never quarantine; risk gets soft cool only.
    if kind in {"mail_miss", "captcha", "verify", "fatal"}:
        info["reason"] = "non_proxy_failure"
        return info

    if kind in {"registration_disallowed", "disallowed"} or (
        "registration_disallowed" in (error or "").lower()
    ):
        risk_s = _cool_seconds("REGISTER_NODES_COOLDOWN_RISK", 600.0)
        if risk_s > 0:
            mgr.cooldown(proxy, risk_s, reason="registration_disallowed", persist=True)
            info["action"] = "risk_cooldown"
            info["cooldown_s"] = risk_s
        else:
            info["action"] = "risk_no_cooldown"
        info["quarantined"] = False
        return info

    if not network_fail:
        info["reason"] = "non_proxy_failure"
        return info

    marked = mgr.mark_result(proxy, ok=False, error=error or error_kind or "proxy_fail", persist=True)
    info["marked"] = marked is not None
    info["action"] = "fail_mark"
    net_s = _cool_seconds("REGISTER_NODES_COOLDOWN_NETWORK", 120.0)
    if net_s > 0:
        mgr.cooldown(proxy, net_s, reason="network", persist=True)
        info["cooldown_s"] = net_s
        info["action"] = "fail_mark_network_cool"
    if marked is not None:
        info["fail_count"] = int(marked.fail_count or 0)
        info["quarantined"] = mgr.is_quarantined(marked)

    # Drop quarantined (or immediately failed) URL from live list pool so next attempt skips it.
    removed = _drop_url_from_rotator(proxy, log_fn=log_fn)
    info["removed_from_pool"] = removed
    if removed and log_fn:
        try:
            log_fn(
                f"[nodes] quarantined/removed dead proxy {_redact(proxy)} "
                f"fail_count={info.get('fail_count', '?')}"
            )
        except Exception:
            pass

    # Rebuild healthy proxy_list on extra for subsequent configure if needed
    try:
        healthy = mgr.as_proxy_list_value(healthy_only=True)
        if healthy:
            base["proxy_list"] = healthy
            configure_rotation_once(base, log_fn=log_fn, force=True)
        elif _nodes_required_for_backend(resolve_backend(base), base):
            from register_core.errors import FailFastError

            raise FailFastError(
                "all catalog nodes failed/quarantined during register; refusing to continue"
            )
    except Exception as exc:
        # re-raise fail-fast; swallow soft reconfigure errors
        from register_core.errors import FailFastError

        if isinstance(exc, FailFastError):
            raise
        info["reconfigure_error"] = str(exc)[:160]

    return info


def _drop_url_from_rotator(url: str, *, log_fn: LogFn = None) -> bool:
    """Remove a dead URL from the process-wide list pool immediately."""
    url = (url or "").strip()
    if not url:
        return False
    try:
        from proxy_rotate import get_rotator

        rot = get_rotator()
        with rot._lock:  # noqa: SLF001
            if rot.mode != "list":
                return False
            pool = list(rot.list_pool or [])
            if url not in pool:
                return False
            pool = [p for p in pool if p != url]
            rot.list_pool = pool
            if not pool:
                rot.current_proxy = ""
                rot.current_label = "(empty)"
                rot.list_index = 0
                return True
            # advance away from removed slot
            rot.list_index = rot.list_index % len(pool)
            # if current was removed, point at next
            if rot.current_proxy == url:
                rot.current_proxy = pool[rot.list_index]
                try:
                    from proxy_bridge import proxy_log_label

                    rot.current_label = proxy_log_label(rot.current_proxy) or rot.current_proxy
                except Exception:
                    rot.current_label = rot.current_proxy
            return True
    except Exception as exc:
        log.debug("drop_url_from_rotator failed: %s", exc)
        return False


def _redact(url: str) -> str:
    try:
        from proxy_bridge import proxy_log_label

        return proxy_log_label(url) or url or "(none)"
    except Exception:
        return url or "(none)"


# re-export helpers for CLI
__all__ = [
    "configure_rotation_once",
    "describe_egress",
    "inject_attempt_proxy",
    "is_proxy_network_failure",
    "normalize_backend",
    "preflight_nodes_for_register",
    "report_attempt_proxy_result",
    "reset_rotation_for_tests",
    "resolve_attempt_proxy",
    "resolve_backend",
    "rotation_config_from_env_and_extra",
]
