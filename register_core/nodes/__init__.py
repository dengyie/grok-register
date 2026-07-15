"""Project-owned egress nodes (no external Clash/mihomo required).

Operators put proxy URLs in ``nodes.json`` / ``nodes.txt`` (gitignored).
``NodeManager`` health-checks and rotates them; ``register_core.util.proxy``
pulls the pool automatically so providers never depend on a local VPN UI.
"""

from __future__ import annotations

from register_core.nodes.catalog import default_nodes_path, load_nodes, save_nodes
from register_core.nodes.manager import NodeManager, get_manager, reset_manager_for_tests
from register_core.nodes.models import Node

__all__ = [
    "Node",
    "NodeManager",
    "default_nodes_path",
    "get_manager",
    "load_nodes",
    "reset_manager_for_tests",
    "save_nodes",
]
