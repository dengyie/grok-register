"""Node record — one self-controlled egress endpoint."""

from __future__ import annotations

import os
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
    # Isolation tier + credential-granularity quality signal.
    #   tier: 0 = datacenter / Clash leaf (default, backward-compat); 1 = residential.
    # Pool selection via REGISTER_NODES_POOL={residential|datacenter|both}
    # filters by tier (see NodeManager + docs/IP-ISOLATION-QUALITY-DESIGN.md).
    # quality counters are keyed by Node id (= url), which for region-Rand
    # residential creds accumulates across rotating exit IPs — exactly right.
    tier: int = 0
    success_count: int = 0
    attempt_count: int = 0
    disallow_count: int = 0
    # Runtime (not required on disk)
    last_ok: bool | None = None
    last_ip: str = ""
    last_ms: int | None = None
    last_error: str = ""
    last_checked_at: float | None = None
    fail_count: int = 0
    cooldown_until: float | None = None  # epoch seconds; soft cool, not quarantine
    cooldown_reason: str = ""

    def quality_score(self, lambda_disallow: float | None = None) -> float:
        """Derived credential-granularity quality.

        ``success_count / max(1, attempt_count) - lambda * disallow_count``.
        ``lambda`` defaults to env ``REGISTER_NODES_QUALITY_LAMBDA`` (0.5)
        so ops can tune without code change. Score is display/history only;
        pool membership is decided by ``tier`` + ``REGISTER_NODES_POOL``.
        """
        if lambda_disallow is None:
            try:
                lambda_disallow = float(
                    os.environ.get("REGISTER_NODES_QUALITY_LAMBDA", "0.5")
                )
            except (TypeError, ValueError):
                lambda_disallow = 0.5
        attempts = max(1, int(self.attempt_count or 0))
        return (int(self.success_count or 0) / attempts) - float(
            lambda_disallow
        ) * int(self.disallow_count or 0)

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
        if self.tier:
            d["tier"] = int(self.tier)
        # Quality signal persists so credential-granularity history survives
        # process restarts (matches report_attempt_proxy_result回写键=id=url).
        if self.attempt_count:
            d["attempt_count"] = int(self.attempt_count)
            d["success_count"] = int(self.success_count or 0)
            d["disallow_count"] = int(self.disallow_count or 0)
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
        url = _normalize_hpup(url)
        return Node(url=url)
    if not isinstance(raw, dict):
        return None
    url = str(raw.get("url") or raw.get("proxy") or raw.get("endpoint") or "").strip()
    if not url:
        return None
    url = _normalize_hpup(url)
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
    # tier: 0 default (backward-compat); 1 residential. Coerce robustly.
    tier = 0
    try:
        tier = int(raw.get("tier") or 0)
    except (TypeError, ValueError):
        tier = 0
    return Node(
        url=url,
        id=str(raw.get("id") or "").strip(),
        label=str(raw.get("label") or raw.get("name") or "").strip(),
        tags=list(tags),
        enabled=bool(enabled),
        tier=tier,
        success_count=int(raw.get("success_count") or 0),
        attempt_count=int(raw.get("attempt_count") or 0),
        disallow_count=int(raw.get("disallow_count") or 0),
        last_ok=raw.get("last_ok"),
        last_ip=str(raw.get("last_ip") or ""),
        last_ms=raw.get("last_ms"),
        last_error=str(raw.get("last_error") or ""),
        fail_count=int(raw.get("fail_count") or 0),
        cooldown_until=cool_until,
        cooldown_reason=str(raw.get("cooldown_reason") or ""),
    )


def _normalize_hpup(url: str) -> str:
    """Normalize a ``host:port:user:pass`` (hpup) credential string to a proxy URL.

    1024proxy residential creds arrive as ``us.1024proxy.io:3000:user:pass`` where
    ``user`` itself contains ``region-Rand-sid-<token>-t-5`` — a 4-segment colon
    string NOT a scheme://URL. ``node_from_dict``'s old string branch fed the raw
    string straight into ``Node.url`` and downstream proxy parsing failed. Detect
    the hpup shape (exactly 4 colon-separated segments, user segment bearing
    residential credential markers) and rewrite to
    ``socks5h://user:pass@host:port`` (socks5h = remote DNS resolve, mandatory
    for residential egress to bypass local DNS pollution). Anything that already
    has a scheme, or doesn't match the shape, is returned untouched.
    """
    s = (url or "").strip()
    if not s or "://" in s:
        return s
    # Heuristic: four colon segments; segment[2] (user) carries residential
    # markers so a plain ``host:port`` (two segments, no auth) is left alone.
    parts = s.split(":")
    if len(parts) != 4:
        return s
    host, port, user, pwd = parts
    if not host or not port or not user or not pwd:
        return s
    if "region-" not in user and "sid-" not in user:
        return s
    return f"socks5h://{user}:{pwd}@{host}:{port}"


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
