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

        email_source is ignored (ttk still owns config email_provider).
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
        try:
            proc = run_command(cmd, cwd=str(ROOT), env=env, timeout_s=timeout_s)
        except Exception as exc:
            raise FailFastError(f"grok register spawn failed: {exc}") from exc

        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.timed_out:
            raise ProviderError(f"grok register timeout after {timeout_s}s")

        low = out.lower()
        if proc.returncode != 0:
            if any(k in low for k in ("alias", "耗尽", "exhausted", "fatal", "fail-fast", "致命")):
                raise FailFastError(f"grok fatal: exit={proc.returncode}")
            return RegisterResult(
                ok=False,
                provider=self.name,
                error=f"register_cli exit={proc.returncode}",
                error_kind="provider",
                secret_kind="none",
                artifacts={
                    "exit_code": proc.returncode,
                    "ledger": accounts_file,
                    "tail": redact_log_tail(out),
                },
            )

        email, password, sso = self._parse_this_run(
            out=out,
            ledger_delta=read_appended(accounts_file, off),
        )
        if not email:
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
                },
            )

        return RegisterResult(
            ok=True,
            provider=self.name,
            email=email,
            password=password,
            secret=sso,
            secret_kind="sso" if sso else "pending",
            artifacts={
                "exit_code": 0,
                "ledger": accounts_file,
                "note": "cpa mint may be async; see cpa_auths/",
                "tail": redact_log_tail(out, limit=800),
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
