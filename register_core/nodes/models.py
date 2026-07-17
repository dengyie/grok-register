"""Node record — one self-controlled egress endpoint."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Node:
    """A single upstream proxy owned by the register machine."""

    url: str
    id: str = ""
    label: str = ""
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    # Runtime (not required on disk)
    last_ok: bool | None = None
    last_ip: str = ""
    last_ms: int | None = None
    last_error: str = ""
    last_checked_at: float | None = None
    fail_count: int = 0
    cooldown_until: float | None = None  # epoch seconds; soft cool, not quarantine
    cooldown_reason: str = ""

    def __post_init__(self) -> None:
        self.url = str(self.url or "").strip()
        if not self.id:
            self.id = _default_id(self.url)
        if not self.label:
            self.label = _safe_label(self.url)

    def to_public_dict(self) -> dict[str, Any]:
        cooling = False
        try:
            cooling = self.cooldown_until is not None and float(self.cooldown_until) > time.time()
        except (TypeError, ValueError):
            cooling = False
        return {
            "id": self.id,
            "label": self.label or _safe_label(self.url),
            "tags": list(self.tags or []),
            "enabled": bool(self.enabled),
            "last_ok": self.last_ok,
            "last_ip": self.last_ip,
            "last_ms": self.last_ms,
            "last_error": (self.last_error or "")[:160],
            "fail_count": int(self.fail_count or 0),
            "cooling": cooling,
            "cooldown_reason": self.cooldown_reason or "",
        }

    def to_store_dict(self) -> dict[str, Any]:
        """Persistable fields only (no runtime probe noise required)."""
        d: dict[str, Any] = {
            "id": self.id,
            "url": self.url,
            "label": self.label,
            "tags": list(self.tags or []),
            "enabled": bool(self.enabled),
        }
        # Keep last health if present so operators can inspect offline.
        if self.last_ok is not None:
            d["last_ok"] = self.last_ok
        if self.last_ip:
            d["last_ip"] = self.last_ip
        if self.last_ms is not None:
            d["last_ms"] = self.last_ms
        if self.last_error:
            d["last_error"] = self.last_error[:200]
        if self.fail_count:
            d["fail_count"] = int(self.fail_count)
        if self.cooldown_until is not None:
            try:
                d["cooldown_until"] = float(self.cooldown_until)
            except (TypeError, ValueError):
                pass
        if self.cooldown_reason:
            d["cooldown_reason"] = str(self.cooldown_reason)[:80]
        return d


def node_from_dict(raw: Any) -> Node | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        url = raw.strip()
        if not url or url.startswith("#"):
            return None
        return Node(url=url)
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or raw.get("proxy") or raw.get("endpoint") or "").strip()
    if not url:
        return None
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]
    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in {"0", "false", "no", "off"}
    cool_until = raw.get("cooldown_until")
    if cool_until is not None and cool_until != "":
        try:
            cool_until = float(cool_until)
        except (TypeError, ValueError):
            cool_until = None
    else:
        cool_until = None
    return Node(
        url=url,
        id=str(raw.get("id") or "").strip(),
        label=str(raw.get("label") or raw.get("name") or "").strip(),
        tags=list(tags),
        enabled=bool(enabled),
        last_ok=raw.get("last_ok"),
        last_ip=str(raw.get("last_ip") or ""),
        last_ms=raw.get("last_ms"),
        last_error=str(raw.get("last_error") or ""),
        fail_count=int(raw.get("fail_count") or 0),
        cooldown_until=cool_until,
        cooldown_reason=str(raw.get("cooldown_reason") or ""),
    )


def _default_id(url: str) -> str:
    """Stable unique id. Label is redacted (no creds) so host:port alone collides
    when two nodes share endpoint with different auth — append short URL digest.
    """
    import hashlib

    label = _safe_label(url)
    safe = "".join(c if c.isalnum() or c in "-._" else "-" for c in label)
    safe = (safe[:48] or "node").rstrip("-")
    digest = hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def _safe_label(url: str) -> str:
    from proxy_bridge import proxy_log_label

    return proxy_log_label(url) or url or "(node)"
