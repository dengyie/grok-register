"""Load / save project-owned node catalog (no Clash)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from register_core.nodes.models import Node, node_from_dict

# Repo root: register_core/nodes/catalog.py → parents[2]
_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_NODES_FILENAMES = (
    "nodes.json",
    "nodes.txt",
    "nodes.list",
    "proxy_list.txt",
)


def default_nodes_path() -> Path:
    """Resolve catalog path: env → first existing default → nodes.json."""
    env = (
        os.environ.get("REGISTER_NODES_FILE")
        or os.environ.get("NODES_FILE")
        or os.environ.get("CHATGPT_NODES_FILE")
        or ""
    ).strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    for name in DEFAULT_NODES_FILENAMES:
        p = _ROOT / name
        if p.is_file():
            return p.resolve()
    return (_ROOT / "nodes.json").resolve()


def load_nodes(path: Path | str | None = None) -> list[Node]:
    """Load nodes from JSON object/array or plain URL list file."""
    p = Path(path) if path else default_nodes_path()
    if not p.is_file():
        return []
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return []

    # JSON?
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if data is not None:
            return _from_json(data)

    # Plain list: one URL per line (or comma-separated)
    nodes: list[Node] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # allow comma on one line
        parts = [x.strip() for x in line.replace(";", ",").split(",") if x.strip()]
        for part in parts:
            n = node_from_dict(part)
            if n and n.url not in seen:
                seen.add(n.url)
                nodes.append(n)
    return nodes


def save_nodes(nodes: list[Node], path: Path | str | None = None) -> Path:
    """Write catalog as JSON (0600)."""
    p = Path(path) if path else default_nodes_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "nodes": [n.to_store_dict() for n in nodes],
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return p


def _from_json(data: Any) -> list[Node]:
    raw_list: list[Any]
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        raw_list = data.get("nodes") or data.get("proxies") or data.get("pool") or []
        if not isinstance(raw_list, list):
            raw_list = []
    else:
        return []
    out: list[Node] = []
    seen: set[str] = set()
    for item in raw_list:
        n = node_from_dict(item)
        if n and n.url not in seen:
            seen.add(n.url)
            out.append(n)
    return out
