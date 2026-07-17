"""StrategyEngine — fail-fast kinds + burn/cool feedback from profile strategy."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from register_core.contracts import RegisterResult
from register_core.strategy.burn import BurnStore

log = logging.getLogger("register_core.strategy")

LogFn = Callable[[str], None] | None

_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


@dataclass
class StrategyFeedback:
    burned_ip: str = ""
    burned_domain: str = ""
    burned_proxy: str = ""
    cooled_ip: str = ""
    action: str = ""
    should_stop: bool = False
    stop_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def domain_from_email(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return ""
    return e.rsplit("@", 1)[-1].strip().lower()


def extract_egress_ip(extra: dict[str, Any] | None, result: RegisterResult | None = None) -> str:
    """Best-effort egress IP from attempt extra / artifacts (may be empty)."""
    extra = extra if isinstance(extra, dict) else {}
    for key in ("_egress_ip", "egress_ip", "exit_ip"):
        v = str(extra.get(key) or "").strip()
        if _IP_RE.fullmatch(v):
            return v
    rot = extra.get("_proxy_rotate") if isinstance(extra.get("_proxy_rotate"), dict) else {}
    for key in ("egress_ip", "ip", "exit_ip"):
        v = str(rot.get(key) or "").strip()
        if _IP_RE.fullmatch(v):
            return v
    arts = (result.artifacts if result else None) or {}
    for key in ("egress_ip", "exit_ip", "ip"):
        v = str(arts.get(key) or "").strip()
        if _IP_RE.fullmatch(v):
            return v
    # last resort: scan proxy_label / tail snippets
    for raw in (
        str(arts.get("proxy_label") or ""),
        str(extra.get("proxy_label") or ""),
        str(rot.get("label") or ""),
    ):
        m = _IP_RE.search(raw)
        if m:
            return m.group(1)
    return ""


def proxy_host_key(proxy: str) -> str:
    """Stable key for a proxy URL (host:port), redaction-safe for burn ledger."""
    s = (proxy or "").strip()
    if not s:
        return ""
    try:
        u = urlparse(s if "://" in s else f"http://{s}")
        host = u.hostname or ""
        port = u.port
        if host and port:
            return f"{host}:{port}"
        if host:
            return host
    except Exception:
        pass
    # strip credentials if present
    if "@" in s:
        return s.split("@", 1)[-1][:120]
    return s[:120]


class StrategyEngine:
    """Consume profile strategy block (via job.extra['_strategy'])."""

    def __init__(
        self,
        *,
        fail_fast: bool = True,
        fail_fast_kinds: list[str] | None = None,
        burn_enabled: bool = True,
        burn_track: list[str] | None = None,
        burn_on_kinds: list[str] | None = None,
        burn_state_path: str = "",
        cool_soft_seconds: float = 0,
        log_fn: LogFn = None,
    ) -> None:
        self.fail_fast = fail_fast
        self.fail_fast_kinds = {
            str(x).strip().lower()
            for x in (fail_fast_kinds or [])
            if str(x).strip()
        } or {
            "registration_disallowed",
            "unsupported_email",
            "fatal",
            "verify",
        }
        self.burn_enabled = burn_enabled
        self.burn_track = {
            str(x).strip().lower() for x in (burn_track or ["ip", "domain"]) if str(x).strip()
        }
        self.burn_on_kinds = {
            str(x).strip().lower()
            for x in (burn_on_kinds or ["registration_disallowed", "unsupported_email"])
            if str(x).strip()
        }
        self.cool_soft_seconds = max(0.0, float(cool_soft_seconds or 0))
        self.store = BurnStore(burn_state_path)
        self._log = log_fn or (lambda m: log.info("%s", m))

    @classmethod
    def from_extra(
        cls,
        extra: dict[str, Any] | None,
        *,
        fail_fast: bool = True,
        log_fn: LogFn = None,
    ) -> StrategyEngine:
        extra = extra if isinstance(extra, dict) else {}
        st = extra.get("_strategy") if isinstance(extra.get("_strategy"), dict) else {}
        burn = st.get("burn") if isinstance(st.get("burn"), dict) else {}
        return cls(
            fail_fast=fail_fast if st.get("fail_fast") is None else bool(st.get("fail_fast")),
            fail_fast_kinds=list(st.get("fail_fast_kinds") or []),
            burn_enabled=bool(burn.get("enabled", True)),
            burn_track=list(burn.get("track") or ["ip", "domain"]),
            burn_on_kinds=list(burn.get("on_kinds") or []),
            burn_state_path=str(burn.get("state_path") or "").strip(),
            cool_soft_seconds=float(st.get("cool_soft_seconds") or 0),
            log_fn=log_fn,
        )

    def should_stop_on_result(self, result: RegisterResult) -> tuple[bool, str]:
        if result.ok:
            return False, ""
        if not self.fail_fast:
            return False, ""
        kind = (result.error_kind or "").strip().lower()
        if kind in self.fail_fast_kinds:
            return True, f"strategy fail_fast kind={kind}"
        # also match unsupported_email in error text when kind generic
        err = (result.error or "").lower()
        if "unsupported_email" in err and "unsupported_email" in self.fail_fast_kinds:
            return True, "strategy fail_fast unsupported_email"
        return False, ""

    def precheck_domain(self, email_or_domain: str) -> StrategyFeedback:
        fb = StrategyFeedback(action="precheck_domain")
        dom = domain_from_email(email_or_domain) or (email_or_domain or "").strip().lower()
        if dom and self.store.is_domain_burned(dom):
            fb.should_stop = True
            fb.stop_reason = f"domain burned: {dom}"
            fb.burned_domain = dom
        return fb

    def precheck_egress(
        self, extra: dict[str, Any] | None, *, proxy: str = ""
    ) -> StrategyFeedback:
        fb = StrategyFeedback(action="precheck_egress")
        extra = extra if isinstance(extra, dict) else {}
        ip = extract_egress_ip(extra)
        px = proxy or str(extra.get("proxy") or "").strip()
        if ip and self.store.is_ip_burned(ip):
            fb.should_stop = True
            fb.stop_reason = f"ip burned: {ip}"
            fb.burned_ip = ip
            return fb
        if ip and self.store.is_ip_cooling(ip):
            fb.should_stop = True
            fb.stop_reason = f"ip cooling: {ip}"
            fb.cooled_ip = ip
            return fb
        key = proxy_host_key(px)
        if key and self.store.is_proxy_burned(key):
            fb.should_stop = True
            fb.stop_reason = f"proxy burned: {key}"
            fb.burned_proxy = key
        return fb

    def on_result(
        self,
        result: RegisterResult,
        extra: dict[str, Any] | None = None,
    ) -> StrategyFeedback:
        extra = extra if isinstance(extra, dict) else {}
        fb = StrategyFeedback()
        kind = (result.error_kind or "").strip().lower()
        reason = kind or (result.error or "fail")[:120]
        email = (result.email or "").strip()
        dom = domain_from_email(email)
        ip = extract_egress_ip(extra, result)
        proxy = str(extra.get("proxy") or "").strip()
        pkey = proxy_host_key(proxy)

        stop, stop_reason = self.should_stop_on_result(result)
        fb.should_stop = stop
        fb.stop_reason = stop_reason

        if result.ok or not self.burn_enabled:
            fb.action = "ok_or_burn_disabled"
            return fb

        burn_this = kind in self.burn_on_kinds or any(
            k in (result.error or "").lower() for k in self.burn_on_kinds if k
        )
        if not burn_this:
            # optional soft cool on risk-adjacent network? leave to proxy layer
            fb.action = "no_burn_kind"
            return fb

        if "domain" in self.burn_track and dom:
            self.store.burn_domain(dom, reason=reason, email=email)
            fb.burned_domain = dom
            self._log(f"[strategy] burn domain={dom} reason={reason}")
        if "ip" in self.burn_track and ip:
            self.store.burn_ip(ip, reason=reason, email=email)
            fb.burned_ip = ip
            self._log(f"[strategy] burn ip={ip} reason={reason}")
        if "proxy" in self.burn_track and pkey:
            self.store.burn_proxy(pkey, reason=reason, email=email)
            fb.burned_proxy = pkey
            self._log(f"[strategy] burn proxy={pkey} reason={reason}")
        # soft cool when configured and we have IP but hard-burn track skipped ip
        if self.cool_soft_seconds > 0 and ip and "ip" not in self.burn_track:
            self.store.cool_ip(ip, self.cool_soft_seconds, reason=reason)
            fb.cooled_ip = ip
            self._log(
                f"[strategy] cool ip={ip} s={self.cool_soft_seconds} reason={reason}"
            )

        fb.action = "burned"
        fb.meta = self.store.summary()
        # attach for operators
        try:
            arts = dict(result.artifacts or {})
            arts.setdefault("strategy_burn", {
                "ip": fb.burned_ip,
                "domain": fb.burned_domain,
                "proxy": fb.burned_proxy,
            })
            result.artifacts = arts
        except Exception:
            pass
        return fb
