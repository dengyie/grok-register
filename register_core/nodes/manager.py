"""In-process node pool manager — rotate healthy project-owned proxies."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from register_core.nodes.catalog import default_nodes_path, load_nodes, save_nodes
from register_core.nodes.health import probe_node
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

    def enabled_nodes(self, *, healthy_only: bool = False) -> list[Node]:
        self.ensure_loaded()
        with self._lock:
            out: list[Node] = []
            for n in self.nodes:
                if not n.enabled:
                    continue
                if not n.url:
                    continue
                if healthy_only and n.last_ok is False:
                    if self._skip_failed and int(n.fail_count or 0) >= self._max_fail:
                        continue
                    if self._require_healthy:
                        continue
                out.append(n)
            return out

    def urls(self, *, healthy_only: bool = False) -> list[str]:
        return [n.url for n in self.enabled_nodes(healthy_only=healthy_only)]

    def as_proxy_list_value(self, *, healthy_only: bool = False) -> str:
        """Comma-joined URLs for proxy_rotate list mode."""
        return ",".join(self.urls(healthy_only=healthy_only))

    def pick(self, *, advance: bool = True) -> Node | None:
        """Pick next enabled node (round-robin). Optionally advance index."""
        pool = self.enabled_nodes(healthy_only=self._require_healthy)
        if not pool and self._require_healthy:
            # fall back to any enabled if none marked healthy yet
            pool = self.enabled_nodes(healthy_only=False)
        if not pool:
            return None
        with self._lock:
            # map pool back to full list indices for stable rotation
            enabled = [n for n in self.nodes if n.enabled and n.url]
            if not enabled:
                return None
            if self._skip_failed:
                candidates = [
                    n
                    for n in enabled
                    if not (
                        n.last_ok is False and int(n.fail_count or 0) >= self._max_fail
                    )
                ]
                if candidates:
                    enabled = candidates
            idx = self._index % len(enabled)
            node = enabled[idx]
            if advance:
                self._index = (idx + 1) % len(enabled)
            return node

    def check_all(
        self,
        *,
        timeout: float = 15.0,
        log: LogFn = None,
        persist: bool = True,
    ) -> list[dict[str, Any]]:
        self.ensure_loaded()
        results: list[dict[str, Any]] = []
        with self._lock:
            nodes = list(self.nodes)
        for n in nodes:
            if not n.enabled:
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
            r = probe_node(n, timeout=timeout)
            results.append(r)
            if log:
                try:
                    if r.get("ok"):
                        log(f"[nodes] OK {r.get('label')} ip={r.get('ip')} {r.get('ms')}ms")
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

    def status(self) -> dict[str, Any]:
        self.ensure_loaded()
        with self._lock:
            enabled = [n for n in self.nodes if n.enabled]
            healthy = [n for n in enabled if n.last_ok is True]
            return {
                "path": str(self.path),
                "exists": self.path.is_file(),
                "total": len(self.nodes),
                "enabled": len(enabled),
                "healthy": len(healthy),
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


def reset_manager_for_tests() -> None:
    global _manager
    with _manager_lock:
        _manager = None


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
