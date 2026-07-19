"""Project-owned egress nodes + optional embedded mihomo core.

Two layers:

1. ``nodes.json`` / ``nodes.txt`` — dialable HTTP/SOCKS URLs for curl_cffi
2. ``.nodes/`` + mihomo core — protocol proxies from Clash YAML (vless/ss/…)
   exposed as local ``http://127.0.0.1:17897`` without Clash Verge UI
"""

from __future__ import annotations

from register_core.nodes.catalog import default_nodes_path, load_nodes, save_nodes
from register_core.nodes.manager import (
    NodeManager,
    get_manager,
    invalidate_manager,
    node_matches_pool_strategy,
    normalize_node_pool_strategy,
    reset_manager_for_tests,
)
from register_core.nodes.models import Node

__all__ = [
    "Node",
    "NodeManager",
    "default_nodes_path",
    "get_manager",
    "invalidate_manager",
    "load_nodes",
    "node_matches_pool_strategy",
    "normalize_node_pool_strategy",
    "reset_manager_for_tests",
    "save_nodes",
]
