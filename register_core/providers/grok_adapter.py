"""Grok / xAI provider — adapts existing register_cli + grok_register_ttk."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from register_core.contracts import RegisterResult
from register_core.email.base import EmailSource
from register_core.errors import FailFastError, ProviderError
from register_core.util.files import file_size, read_appended
from register_core.util.process import redact_log_tail, run_command

ROOT = Path(__file__).resolve().parents[2]
_SUCCESS_LOG = re.compile(r"\+\s*注册成功:\s*(\S+@\S+)")


class GrokProvider:
    name = "grok"

    def __init__(
        self,
        *,
        threads: int = 1,
        headless: bool | None = None,
        account_slot_retry: int = 0,
        accounts_file: str | None = None,
        extra_cli: list[str] | None = None,
        **_: Any,
    ) -> None:
        self.threads = max(1, int(threads))
        self.headless = headless
        self.account_slot_retry = account_slot_retry
        self.accounts_file = accounts_file or str(ROOT / "accounts_cli.txt")
        self.extra_cli = list(extra_cli or [])

    def register_one(
        self,
        *,
        email_source: EmailSource | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RegisterResult:
        """Shell out to register_cli for one account.

        When email_source is set, allocate FIXED_EMAIL and set EMAIL_PROVIDER=fixed
        so ttk uses core mailbox; OTP goes through OTP_HELPER / REGISTER_OTP_SPEC.
        Success requires exit=0 **and** a this-run ledger increment (or
        success log email). secret_kind is sso only when SSO was captured.
        """
        extra = extra or {}
        py = sys.executable
        cli = ROOT / "register_cli.py"
        if not cli.is_file():
            raise FailFastError(f"register_cli.py missing at {cli}")

        accounts_file = str(extra.get("accounts_file") or self.accounts_file)
        off = file_size(accounts_file)

        cmd = [
            py,
            "-u",
            str(cli),
            "--extra",
            "1",
            "--threads",
            str(self.threads),
            "--account-slot-retry",
            str(self.account_slot_retry),
            "--accounts-file",
            accounts_file,
            "--fast",
        ]
        if self.headless is True:
            cmd.append("--headless")
        elif self.headless is False:
            cmd.append("--no-headless")
        cmd.extend(self.extra_cli)

        env = os.environ.copy()
        timeout_s = int(extra.get("timeout_s", 900) or 900)
        otp_timeout = float(extra.get("otp_timeout_s") or 180)
        mailbox = None
        mail_meta: dict[str, Any] = {}
        if email_source is not None:
            from register_core.util.mail_inject import prepare_mail_inject

            try:
                mailbox = prepare_mail_inject(
                    email_source,
                    env,
                    timeout_s=otp_timeout,
                    sender_hint="xai",
                    force_helper=True,
                    work_dir=ROOT / "logs" / "otp_bridge",
                )
            except Exception as exc:
                raise FailFastError(f"grok mail allocate failed: {exc}") from exc
            if mailbox is not None:
                mail_meta = {
                    "fixed_email": mailbox.address,
                    "email_source": getattr(email_source, "name", ""),
                    "otp_helper": bool(env.get("OTP_HELPER")),
                }

        try:
            proc = run_command(cmd, cwd=str(ROOT), env=env, timeout_s=timeout_s)
        except Exception as exc:
            if mailbox is not None and email_source is not None:
                try:
                    email_source.release(mailbox, success=False)
                except Exception:
                    pass
            raise FailFastError(f"grok register spawn failed: {exc}") from exc

        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.timed_out:
            if mailbox is not None and email_source is not None:
                try:
                    email_source.release(mailbox, success=False)
                except Exception:
                    pass
            raise ProviderError(f"grok register timeout after {timeout_s}s")

        low = out.lower()
        if proc.returncode != 0:
            if mailbox is not None and email_source is not None:
                try:
                    email_source.release(mailbox, success=False)
                except Exception:
                    pass
            if any(k in low for k in ("alias", "耗尽", "exhausted", "fatal", "fail-fast", "致命")):
                raise FailFastError(f"grok fatal: exit={proc.returncode}")
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=(mailbox.address if mailbox else ""),
                error=f"register_cli exit={proc.returncode}",
                error_kind="provider",
                secret_kind="none",
                artifacts={
                    "exit_code": proc.returncode,
                    "ledger": accounts_file,
                    "tail": redact_log_tail(out),
                    **mail_meta,
                },
            )

        email, password, sso = self._parse_this_run(
            out=out,
            ledger_delta=read_appended(accounts_file, off),
        )
        if not email and mailbox is not None:
            email = mailbox.address
        if not email:
            if mailbox is not None and email_source is not None:
                try:
                    email_source.release(mailbox, success=False)
                except Exception:
                    pass
            return RegisterResult(
                ok=False,
                provider=self.name,
                error="register_cli exit=0 but no this-run ledger/email",
                error_kind="provider",
                secret_kind="none",
                artifacts={
                    "exit_code": 0,
                    "ledger": accounts_file,
                    "tail": redact_log_tail(out),
                    **mail_meta,
                },
            )

        if not sso:
            if mailbox is not None and email_source is not None:
                try:
                    email_source.release(mailbox, success=False)
                except Exception:
                    pass
            # Email-only ledger row is incomplete for product success (no SSO / mint input).
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=email,
                password=password,
                secret="",
                secret_kind="pending",
                error="this-run email without SSO cookie (pending); not product-ready",
                error_kind="provider",
                artifacts={
                    "exit_code": 0,
                    "ledger": accounts_file,
                    "note": "require SSO in accounts ledger (email----pw----sso)",
                    "tail": redact_log_tail(out, limit=800),
                    **mail_meta,
                },
            )

        if mailbox is not None and email_source is not None:
            try:
                email_source.release(mailbox, success=True)
            except Exception:
                pass

        return RegisterResult(
            ok=True,
            provider=self.name,
            email=email,
            password=password,
            secret=sso,
            secret_kind="sso",
            artifacts={
                "exit_code": 0,
                "ledger": accounts_file,
                "note": "sso captured; chat entitlement still via cpa_xai.probe",
                "tail": redact_log_tail(out, limit=800),
                **mail_meta,
            },
        )

    @staticmethod
    def _parse_this_run(*, out: str, ledger_delta: str) -> tuple[str, str, str]:
        email, password, sso = "", "", ""
        # Prefer ledger append (authoritative)
        for line in ledger_delta.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("----")
            if len(parts) >= 1 and "@" in parts[0]:
                email = parts[0].strip()
                password = parts[1].strip() if len(parts) > 1 else ""
                sso = parts[2].strip() if len(parts) > 2 else ""
        if email:
            return email, password, sso
        # Fallback: success log line
        m = _SUCCESS_LOG.search(out)
        if m:
            email = m.group(1).strip().rstrip(",;")
        return email, password, sso
