"""tinyhost.shop temp-mail (independent mailbox per account)."""

from __future__ import annotations

import json
import re
import secrets
import string
import time
from datetime import datetime, timezone
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

from register_core.contracts import Mailbox, OtpCode
from register_core.decode.extract import (
    OAI_SUBJECT_XAI_CODE_RE,
    OTP_RE,
    XAI_BODY_CODE_RE,
    _OPENAI_OTP_PATTERNS,
    extract_otp_code,
)
from register_core.errors import FailFastError, MailMissError

# Hard-block disposable hosters that rarely deliver OpenAI/OTP mail.
BAD_DOMAIN_RE = re.compile(
    r"(infinityfree|000\.pe|work\.gd|\.io\.vn$|\.\.$|\.\s*$)",
    re.I,
)
# The OTP decoder lives in register_core.decode.extract (single authoritative
# copy). Re-exported here so existing imports (`from ...tinyhost import
# extract_otp_code`) keep working. See that module for the CSS-hex / 333333
# history and the precedence contract.

DEFAULT_BASE = "https://tinyhost.shop"
# Prefer domains that historically deliver product OTPs (OpenAI/MiMo path).
# publicvm.com often accepts allocate but never delivers OpenAI OTP → demoted last.
PREFERRED_DOMAINS = (
    "huychau.online",
    "sewink.my.id",
    "graphiclens.site",
    "kimora.space",
    "sasukiez.shop",
    "nexorabio.pro.vn",
    "publicvm.com",
)
LOCAL_FALLBACK = PREFERRED_DOMAINS


def _parse_mail_ts(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        try:
            return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            return 0.0


class TinyhostSource:
    name = "tinyhost"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE,
        proxy: str | None = None,
        domain: str | None = None,
        **_: Any,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.proxy = proxy or ""
        self.forced_domain = domain
        self.last_wait_diagnostics = None

    def _opener(self):
        if self.proxy:
            return build_opener(ProxyHandler({"http": self.proxy, "https": self.proxy}))
        return build_opener()

    def _get_json(self, url: str, timeout: float = 20) -> Any:
        req = Request(url, headers={"User-Agent": "register-core/tinyhost"})
        try:
            with self._opener().open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            raise MailMissError(f"tinyhost request failed: {exc}") from exc

    def _pick_domain(self) -> str:
        if self.forced_domain:
            d = self.forced_domain.strip().strip(".")
            if BAD_DOMAIN_RE.search(d):
                raise FailFastError(f"tinyhost forced domain rejected: {d}")
            return d
        # 1) Prefer known-good domains first (higher OpenAI/OTP deliverability).
        preferred = list(PREFERRED_DOMAINS)
        secrets.SystemRandom().shuffle(preferred)
        try:
            data = self._get_json(f"{self.base_url}/api/random-domains/?limit=16", timeout=15)
            remote = data.get("domains") if isinstance(data, dict) else None
            remote_set = {
                str(raw or "").strip().strip(".").lower()
                for raw in (remote or [])
                if str(raw or "").strip()
            }
            for d in preferred:
                if d.lower() in remote_set and not BAD_DOMAIN_RE.search(d):
                    return d
            if isinstance(remote, list):
                for raw in remote:
                    d = str(raw or "").strip().strip(".")
                    if d and not BAD_DOMAIN_RE.search(d) and "." in d:
                        # Soft-prefer preferred list order already tried; accept clean remote.
                        return d
        except MailMissError:
            pass
        return secrets.choice(LOCAL_FALLBACK)

    def allocate(self) -> Mailbox:
        local = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
        domain = self._pick_domain()
        address = f"{local}@{domain}"
        return Mailbox(
            address=address,
            token="",
            provider=self.name,
            meta={"domain": domain, "local": local},
        )

    def poll_otp(
        self,
        mailbox: Mailbox,
        *,
        timeout_s: float = 180,
        poll_interval_s: float = 3,
        used_codes: set[str] | None = None,
        newer_than_epoch: float | None = None,
        sender_hint: str | None = None,
    ) -> OtpCode:
        from register_core.contracts import OtpWaitDiagnostics

        used = used_codes or set()
        local = mailbox.meta.get("local") or mailbox.address.split("@", 1)[0]
        domain = mailbox.meta.get("domain") or mailbox.address.split("@", 1)[-1]
        url = f"{self.base_url}/api/email/{domain}/{local}/?page=1&limit=100"
        started = time.time()
        deadline = started + max(5.0, timeout_s)
        since = (newer_than_epoch if newer_than_epoch is not None else started) - 15
        hint = (sender_hint or "").lower().strip()
        diag = OtpWaitDiagnostics(
            timeout_s=float(timeout_s),
            provider=self.name,
            sender_hint=(sender_hint or ""),
        )
        self.last_wait_diagnostics = diag

        while time.time() < deadline:
            diag.poll_count += 1
            try:
                data = self._get_json(url, timeout=20)
            except MailMissError:
                diag.empty_rounds += 1
                time.sleep(poll_interval_s)
                continue
            emails = data.get("emails") if isinstance(data, dict) else None
            if not isinstance(emails, list) or not emails:
                diag.empty_rounds += 1
                time.sleep(poll_interval_s)
                continue
            if diag.first_message_seen_at is None:
                diag.first_message_seen_at = time.time()
                diag.first_seen_after_seconds = diag.first_message_seen_at - started
            for mail in emails:
                if not isinstance(mail, dict):
                    continue
                diag.message_scan_count += 1
                ts = _parse_mail_ts(mail.get("date"))
                if ts and ts < since:
                    continue
                subject = str(mail.get("subject") or "")
                blob = " ".join(
                    str(mail.get(k) or "")
                    for k in ("from", "sender", "subject", "body", "html_body", "text")
                )
                if hint and hint not in blob.lower():
                    continue
                code = extract_otp_code(blob, subject=subject)
                if not code or code in used:
                    continue
                diag.matched_at = time.time()
                diag.matched_after_seconds = diag.matched_at - started
                diag.elapsed_seconds = diag.matched_after_seconds
                self.last_wait_diagnostics = diag
                return OtpCode(
                    code=code,
                    source=self.name,
                    raw_subject=str(mail.get("subject") or "")[:200],
                )
            time.sleep(poll_interval_s)
        diag.elapsed_seconds = time.time() - started
        diag.failure_class = "no_mail" if diag.message_scan_count == 0 else "parse_fail"
        self.last_wait_diagnostics = diag
        raise MailMissError(
            f"tinyhost OTP timeout for {mailbox.address}",
            diagnostics=diag,
        )

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        return
