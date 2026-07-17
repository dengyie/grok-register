"""Persistent burn / soft-cool ledger for egress keys and email domains."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _now() -> float:
    return time.time()


@dataclass
class BurnRecord:
    key: str
    kind: str  # ip | domain | proxy
    reason: str = ""
    at: float = field(default_factory=_now)
    email: str = ""
    cool_until: float = 0.0  # soft cool; 0 = hard burn only

    def is_hard_burned(self) -> bool:
        return self.cool_until <= 0 and bool(self.reason)

    def is_cooling(self, now: float | None = None) -> bool:
        t = now if now is not None else _now()
        return self.cool_until > t


class BurnStore:
    """Thread-safe burn ledger; optional JSON state_path for cross-run persistence."""

    def __init__(self, state_path: str = "") -> None:
        self.state_path = (state_path or "").strip()
        self._lock = threading.RLock()
        self.ips: dict[str, BurnRecord] = {}
        self.domains: dict[str, BurnRecord] = {}
        self.proxies: dict[str, BurnRecord] = {}
        if self.state_path:
            self.load()

    def load(self) -> None:
        if not self.state_path:
            return
        p = Path(self.state_path)
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        with self._lock:
            self.ips = self._load_map(data.get("ips"), "ip")
            self.domains = self._load_map(data.get("domains"), "domain")
            self.proxies = self._load_map(data.get("proxies"), "proxy")

    @staticmethod
    def _load_map(raw: Any, kind: str) -> dict[str, BurnRecord]:
        out: dict[str, BurnRecord] = {}
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            if not k:
                continue
            if isinstance(v, dict):
                out[str(k)] = BurnRecord(
                    key=str(k),
                    kind=kind,
                    reason=str(v.get("reason") or ""),
                    at=float(v.get("at") or 0) or _now(),
                    email=str(v.get("email") or ""),
                    cool_until=float(v.get("cool_until") or 0),
                )
            else:
                out[str(k)] = BurnRecord(key=str(k), kind=kind, reason=str(v or "burned"))
        return out

    def save(self) -> None:
        if not self.state_path:
            return
        p = Path(self.state_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ips": {k: self._dump(r) for k, r in self.ips.items()},
                "domains": {k: self._dump(r) for k, r in self.domains.items()},
                "proxies": {k: self._dump(r) for k, r in self.proxies.items()},
                "updated_at": _now(),
            }
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
            try:
                p.chmod(0o600)
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _dump(r: BurnRecord) -> dict[str, Any]:
        return {
            "reason": r.reason,
            "at": r.at,
            "email": r.email,
            "cool_until": r.cool_until,
        }

    def burn_ip(self, ip: str, *, reason: str, email: str = "") -> None:
        ip = (ip or "").strip()
        if not ip:
            return
        with self._lock:
            self.ips[ip] = BurnRecord(key=ip, kind="ip", reason=reason, email=email)
            self.save()

    def burn_domain(self, domain: str, *, reason: str, email: str = "") -> None:
        domain = (domain or "").strip().lower().lstrip("@")
        if not domain or "." not in domain:
            return
        with self._lock:
            self.domains[domain] = BurnRecord(
                key=domain, kind="domain", reason=reason, email=email
            )
            self.save()

    def burn_proxy(self, proxy: str, *, reason: str, email: str = "") -> None:
        proxy = (proxy or "").strip()
        if not proxy:
            return
        with self._lock:
            self.proxies[proxy] = BurnRecord(
                key=proxy, kind="proxy", reason=reason, email=email
            )
            self.save()

    def cool_ip(self, ip: str, seconds: float, *, reason: str) -> None:
        ip = (ip or "").strip()
        if not ip or seconds <= 0:
            return
        with self._lock:
            rec = self.ips.get(ip) or BurnRecord(key=ip, kind="ip")
            rec.cool_until = _now() + float(seconds)
            rec.reason = reason or rec.reason or "cool"
            rec.at = _now()
            self.ips[ip] = rec
            self.save()

    def is_ip_burned(self, ip: str) -> bool:
        ip = (ip or "").strip()
        if not ip:
            return False
        with self._lock:
            rec = self.ips.get(ip)
            return bool(rec and rec.is_hard_burned())

    def is_domain_burned(self, domain: str) -> bool:
        domain = (domain or "").strip().lower().lstrip("@")
        if not domain:
            return False
        with self._lock:
            rec = self.domains.get(domain)
            return bool(rec and rec.is_hard_burned())

    def is_proxy_burned(self, proxy: str) -> bool:
        proxy = (proxy or "").strip()
        if not proxy:
            return False
        with self._lock:
            rec = self.proxies.get(proxy)
            return bool(rec and rec.is_hard_burned())

    def is_ip_cooling(self, ip: str) -> bool:
        ip = (ip or "").strip()
        if not ip:
            return False
        with self._lock:
            rec = self.ips.get(ip)
            return bool(rec and rec.is_cooling())

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ips": len(self.ips),
                "domains": len(self.domains),
                "proxies": len(self.proxies),
                "state_path": self.state_path or "",
            }
