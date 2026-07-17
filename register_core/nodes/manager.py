"""In-process node pool manager — rotate healthy project-owned proxies."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from register_core.nodes.catalog import default_nodes_path, load_nodes, save_nodes
from register_core.nodes.health import probe_node, probe_node_layered
from register_core.nodes.models import Node

LogFn = Callable[[str], None] | None


class NodeManager:
    """Owns the project node catalog and round-robin selection."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_nodes_path()
        self._lock = threading.RLock()
        self.nodes: list[Node] = []
        self._index = 0
        self._loaded_at = 0.0
        self._require_healthy = _env_truthy(
            os.environ.get("REGISTER_NODES_REQUIRE_HEALTHY"), default=False
        )
        self._skip_failed = _env_truthy(
            os.environ.get("REGISTER_NODES_SKIP_FAILED"), default=True
        )
        self._max_fail = max(1, _as_int(os.environ.get("REGISTER_NODES_MAX_FAIL"), 3))

    def reload(self) -> list[Node]:
        with self._lock:
            self.nodes = load_nodes(self.path)
            self._loaded_at = time.time()
            if self._index >= len(self.nodes):
                self._index = 0
            return list(self.nodes)

    def ensure_loaded(self) -> list[Node]:
        with self._lock:
            if not self.nodes and self.path.is_file():
                return self.reload()
            if not self.nodes and not self.path.is_file():
                # still try load (returns [])
                return self.reload()
            return list(self.nodes)

    def is_quarantined(self, n: Node) -> bool:
        """Hard-failed nodes are skipped from rotation until they pass probe again."""
        if not self._skip_failed:
            return False
        return n.last_ok is False and int(n.fail_count or 0) >= self._max_fail

    # back-compat alias used in older call sites / tests
    def _is_quarantined(self, n: Node) -> bool:
        return self.is_quarantined(n)

    def is_cooling(self, n: Node) -> bool:
        """Soft cooldown — temporary skip without hard quarantine.

        Lazy-clears expired ``cooldown_until`` / ``cooldown_reason`` so stale cool
        fields do not linger in memory (persist happens on next mark/cooldown).
        """
        until = n.cooldown_until
        if until is None:
            return False
        try:
            if float(until) > time.time():
                return True
        except (TypeError, ValueError):
            n.cooldown_until = None
            n.cooldown_reason = ""
            return False
        # Expired — clear so status/public dict stop advertising cooling.
        n.cooldown_until = None
        n.cooldown_reason = ""
        return False

    def cooldown(
        self,
        url: str,
        seconds: float,
        reason: str = "",
        *,
        persist: bool = True,
    ) -> Node | None:
        """Put a catalog node into soft cooldown for ``seconds``."""
        url = (url or "").strip()
        if not url or seconds <= 0:
            return None
        self.ensure_loaded()
        with self._lock:
            node = None
            for n in self.nodes:
                if n.url == url:
                    node = n
                    break
            if node is None:
                return None
            node.cooldown_until = time.time() + float(seconds)
            node.cooldown_reason = (reason or "")[:80]
            if persist:
                try:
                    save_nodes(self.nodes, self.path)
                except Exception:
                    pass
            return node

    def enabled_nodes(self, *, healthy_only: bool = False) -> list[Node]:
        """Enabled dialable nodes.

        ``healthy_only=True`` → only ``last_ok is True`` (post-probe pool for register).
        Always excludes quarantined hard-fails when ``REGISTER_NODES_SKIP_FAILED``.
        Also skips soft-cooling nodes until ``cooldown_until`` expires.
        """
        self.ensure_loaded()
        with self._lock:
            out: list[Node] = []
            for n in self.nodes:
                if not n.enabled:
                    continue
                if not n.url:
                    continue
                if self._is_quarantined(n):
                    continue
                if self.is_cooling(n):
                    continue
                if healthy_only:
                    if n.last_ok is not True:
                        continue
                elif self._require_healthy and n.last_ok is not True:
                    continue
                out.append(n)
            return out

    def urls(self, *, healthy_only: bool = False) -> list[str]:
        return [n.url for n in self.enabled_nodes(healthy_only=healthy_only)]

    def as_proxy_list_value(self, *, healthy_only: bool = False) -> str:
        """Comma-joined URLs for proxy_rotate list mode."""
        return ",".join(self.urls(healthy_only=healthy_only))

    def find_by_url(self, url: str) -> Node | None:
        url = (url or "").strip()
        if not url:
            return None
        self.ensure_loaded()
        with self._lock:
            for n in self.nodes:
                if n.url == url:
                    return n
        return None

    def pick(self, *, advance: bool = True) -> Node | None:
        """Pick next enabled node (round-robin). Optionally advance index."""
        pool = self.enabled_nodes(healthy_only=True)
        if not pool:
            pool = self.enabled_nodes(healthy_only=False)
        if not pool:
            return None
        with self._lock:
            idx = self._index % len(pool)
            node = pool[idx]
            if advance:
                self._index = (idx + 1) % len(pool)
            return node

    def check_all(
        self,
        *,
        timeout: float = 15.0,
        log: LogFn = None,
        persist: bool = True,
        enabled_only: bool = True,
        limit: int | None = None,
        smart_order: bool = False,
        skip_quarantined: bool = False,
        probe_urls: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Probe catalog nodes and persist health fields.

        ``smart_order`` (register preflight): previously healthy → unprobed → soft-fail,
        and optionally skip hard-quarantined nodes so bulk dead dumps don't burn the budget.

        ``probe_urls``: optional L2 business targets. When set, each candidate runs
        ``probe_node_layered`` (L1∧L2); result ``ok`` / ``pool_ready`` gates the register
        pool. L1-only (empty probe_urls) keeps legacy ``probe_node`` behavior.
        """
        self.ensure_loaded()
        results: list[dict[str, Any]] = []
        with self._lock:
            nodes = list(self.nodes)

        targets = [str(u).strip() for u in (probe_urls or []) if str(u).strip()]
        use_layered = bool(targets)

        candidates: list[Node] = []
        for n in nodes:
            if enabled_only and not n.enabled:
                results.append(
                    {
                        "id": n.id,
                        "label": n.label,
                        "ok": False,
                        "skipped": True,
                        "reason": "disabled",
                    }
                )
                continue
            if not n.url:
                results.append(
                    {
                        "id": n.id,
                        "label": n.label,
                        "ok": False,
                        "skipped": True,
                        "reason": "empty_url",
                    }
                )
                continue
            if skip_quarantined and self._is_quarantined(n):
                results.append(
                    {
                        "id": n.id,
                        "label": n.label,
                        "ok": False,
                        "skipped": True,
                        "reason": "quarantined",
                    }
                )
                continue
            candidates.append(n)

        if smart_order:
            # Prefer dual-pass / clean L1 → unprobed → L2-miss (last_ok True +
            # last_error l2_fail) → soft L1 fail. L2-miss must not monopolize the
            # REGISTER_NODES_PROBE_LIMIT budget on large catalogs.
            def _rank(n: Node) -> tuple[int, int]:
                err = (n.last_error or "").lower()
                l2_miss = err.startswith("l2_fail") or "l2_fail " in err
                if n.last_ok is True and not l2_miss:
                    return (0, int(n.fail_count or 0))
                if n.last_ok is None:
                    return (1, 0)
                if n.last_ok is True and l2_miss:
                    return (2, 0)
                # Hard L1 fail tier: probe least-failed (most recoverable) first.
                return (3, int(n.fail_count or 0))

            candidates.sort(key=_rank)

        probed = 0
        for n in candidates:
            if limit is not None and probed >= int(limit):
                results.append(
                    {
                        "id": n.id,
                        "label": n.label,
                        "ok": False,
                        "skipped": True,
                        "reason": "limit",
                    }
                )
                continue
            if use_layered:
                r = probe_node_layered(n, probe_urls=targets, timeout=timeout)
            else:
                r = probe_node(n, timeout=timeout)
            probed += 1
            results.append(r)
            if log:
                try:
                    if r.get("ok"):
                        l2note = ""
                        if use_layered:
                            l2note = " L1∧L2"
                        log(
                            f"[nodes] OK{l2note} {r.get('label')} "
                            f"ip={r.get('ip')} {r.get('ms')}ms"
                        )
                    else:
                        log(f"[nodes] FAIL {r.get('label')}: {r.get('error')}")
                except Exception:
                    pass
        if persist:
            try:
                save_nodes(nodes, self.path)
            except Exception:
                pass
        return results

    def preflight(
        self,
        *,
        timeout: float = 15.0,
        log: LogFn = None,
        persist: bool = True,
        limit: int | None = None,
        probe_urls: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Probe enabled nodes before register; return healthy pool summary.

        Register path should only consume ``healthy_urls`` / ``proxy_list`` from this
        result. Uses smart order + skips quarantined; default limit protects large dumps.

        When ``probe_urls`` is non-empty, pool is the L1∧L2 dual-pass subset (not merely
        catalog ``last_ok``). L2-only failures stay out of the seed list without
        hard-quarantining the node for missing a business path.
        """
        if limit is None:
            # Default budget: avoid probing hundreds of dead free-list nodes on every run.
            limit = max(1, _as_int(os.environ.get("REGISTER_NODES_PROBE_LIMIT"), 40))
            # env empty → 40; explicit 0 means unlimited
            raw = (os.environ.get("REGISTER_NODES_PROBE_LIMIT") or "").strip()
            if raw == "0":
                limit = None
        targets = [str(u).strip() for u in (probe_urls or []) if str(u).strip()]
        results = self.check_all(
            timeout=timeout,
            log=log,
            persist=persist,
            enabled_only=True,
            limit=limit,
            smart_order=True,
            skip_quarantined=True,
            probe_urls=targets or None,
        )
        ok = [r for r in results if r.get("ok")]
        fail = [
            r
            for r in results
            if not r.get("ok") and not r.get("skipped")
        ]
        skipped = [r for r in results if r.get("skipped")]

        if targets:
            # Dual-pass pool: only layered ok (L1∧L2). Do not fall back to last_ok-only.
            # Prefer probe-ok order (smart_order); skip quarantine/cooling.
            self.ensure_loaded()
            # Index by id; if ids ever collide, keep ALL urls under that id so
            # L2 seed does not silently drop last-write-wins victims.
            with self._lock:
                id_to_nodes: dict[str, list] = {}
                for n in self.nodes:
                    id_to_nodes.setdefault(str(n.id or ""), []).append(n)
            ordered: list[str] = []
            seen: set[str] = set()
            for r in ok:
                nodes = id_to_nodes.get(str(r.get("id") or "")) or []
                for n in nodes:
                    if n is None or not n.url or n.url in seen:
                        continue
                    if not n.enabled or self._is_quarantined(n) or self.is_cooling(n):
                        continue
                    ordered.append(n.url)
                    seen.add(n.url)
            healthy_urls = ordered
        else:
            healthy_urls = self.urls(healthy_only=True)

        summary = {
            "probed": len(ok) + len(fail),
            "ok": len(ok),
            "fail": len(fail),
            "skipped": len(skipped),
            "healthy": len(healthy_urls),
            "healthy_urls": healthy_urls,
            "proxy_list": ",".join(healthy_urls),
            "results": results,
            "path": str(self.path),
            "limit": limit,
            "probe_targets": list(targets),
            "l2_enabled": bool(targets),
        }
        if log:
            try:
                tgt = ",".join(targets) if targets else "L1-only"
                log(
                    f"[nodes] preflight ok={summary['ok']} fail={summary['fail']} "
                    f"healthy={summary['healthy']} targets={tgt} "
                    f"limit={limit} path={self.path}"
                )
            except Exception:
                pass
        return summary

    def mark_result(
        self,
        url: str,
        *,
        ok: bool,
        error: str = "",
        persist: bool = True,
    ) -> Node | None:
        """Record success/failure for a proxy URL used during registration.

        On failure increments ``fail_count``; at ``REGISTER_NODES_MAX_FAIL`` the
        node is quarantined (skipped by rotation) until a future probe succeeds.
        """
        url = (url or "").strip()
        if not url:
            return None
        self.ensure_loaded()
        with self._lock:
            node = None
            for n in self.nodes:
                if n.url == url:
                    node = n
                    break
            if node is None:
                return None
            node.last_checked_at = time.time()
            if ok:
                node.last_ok = True
                node.last_error = ""
                node.fail_count = 0
            else:
                node.last_ok = False
                node.last_error = (error or "register_proxy_fail")[:200]
                node.fail_count = int(node.fail_count or 0) + 1
            if persist:
                try:
                    save_nodes(self.nodes, self.path)
                except Exception:
                    pass
            return node

    def status(self) -> dict[str, Any]:
        self.ensure_loaded()
        with self._lock:
            enabled = [n for n in self.nodes if n.enabled]
            healthy = [n for n in enabled if n.last_ok is True]
            quarantined = [
                n
                for n in enabled
                if n.last_ok is False and int(n.fail_count or 0) >= self._max_fail
            ]
            cooling = [n for n in enabled if self.is_cooling(n)]
            return {
                "path": str(self.path),
                "exists": self.path.is_file(),
                "total": len(self.nodes),
                "enabled": len(enabled),
                "healthy": len(healthy),
                "quarantined": len(quarantined),
                "cooling": len(cooling),
                "max_fail": self._max_fail,
                "skip_failed": self._skip_failed,
                "index": self._index,
                "nodes": [n.to_public_dict() for n in self.nodes],
            }


_manager: NodeManager | None = None
_manager_lock = threading.Lock()


def get_manager(path: Path | str | None = None) -> NodeManager:
    global _manager
    with _manager_lock:
        if _manager is None or (path and Path(path) != _manager.path):
            _manager = NodeManager(path)
        return _manager


def invalidate_manager() -> None:
    """Drop process-global NodeManager so next get_manager() reloads from disk.

    Use after CLI rewrites the catalog path out-of-band, or when tests need a
    clean singleton. Prefer ``mgr.reload()`` when you already hold the manager
    for the same path.
    """
    global _manager
    with _manager_lock:
        _manager = None


def reset_manager_for_tests() -> None:
    """Test helper alias for :func:`invalidate_manager`."""
    invalidate_manager()


def _env_truthy(val: Any, default: bool = False) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except Exception:
        return default
