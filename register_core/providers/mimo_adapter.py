"""MiMo / Xiaomi provider — runs providers/mimo Node register-one via shell runner."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from register_core.contracts import RegisterResult
from register_core.email.base import EmailSource
from register_core.errors import FailFastError, ProviderError
from register_core.util.files import file_size, read_appended
from register_core.util.process import redact_log_tail, run_command
from register_core.util.secrets import API_KEY_SEARCH_RE, is_api_key

ROOT = Path(__file__).resolve().parents[2]
MIMO_DIR = ROOT / "providers" / "mimo"
_RESULT_LINE = re.compile(r"^RESULT_JSON:(.+)$", re.M)


class MimoProvider:
    name = "mimo"

    def __init__(
        self,
        *,
        runtime: str | None = None,
        proxy: str | None = None,
        headless: bool = True,
        **_: Any,
    ) -> None:
        self.runtime = runtime or os.environ.get("MIMO_RUNTIME") or ""
        self.proxy = proxy or os.environ.get("MIMO_PROXY") or "http://127.0.0.1:7897"
        self.headless = headless

    def register_one(
        self,
        *,
        email_source: EmailSource | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RegisterResult:
        """Invoke run-register.sh COUNT=1.

        When email_source is set (profile mailbox/decode), allocate once and
        inject FIXED_EMAIL (+ OTP_HELPER for non-tinyhost) into the Node runner.
        Results are attributed via RESULT_JSON stdout and/or file *increments*
        only — never the historical tail of shared output files alone.
        """
        extra = extra or {}
        runner = MIMO_DIR / "run-register.sh"
        if not runner.is_file():
            raise FailFastError(f"mimo runner missing: {runner}")

        env = os.environ.copy()
        env["COUNT"] = "1"
        # Prefer attempt proxy from pipeline rotate when present.
        proxy = str(extra.get("proxy") or self.proxy or "").strip() or self.proxy
        env["MIMO_PROXY"] = proxy
        env["HEADLESS"] = "true" if self.headless else "false"
        if self.runtime:
            env["MIMO_RUNTIME"] = self.runtime
        elif Path("/personal/mimo-register/node_modules").is_dir():
            env["MIMO_RUNTIME"] = "/personal/mimo-register"

        runtime = env.get("MIMO_RUNTIME") or str(MIMO_DIR)
        keys_path = Path(runtime) / "output" / "success_keys.txt"
        accounts_path = Path(runtime) / "output" / "accounts.jsonl"
        off_keys = file_size(keys_path)
        off_acc = file_size(accounts_path)

        timeout_s = int(extra.get("timeout_s", 1200) or 1200)
        otp_timeout = float(extra.get("otp_timeout_s") or 180)
        mailbox = None
        mail_meta: dict[str, Any] = {}
        released = False
        success = False

        def _release() -> None:
            nonlocal released
            if released or mailbox is None or email_source is None:
                return
            released = True
            try:
                email_source.release(mailbox, success=success)
            except Exception:
                pass

        if email_source is not None:
            from register_core.util.mail_inject import prepare_mail_inject

            try:
                mailbox = prepare_mail_inject(
                    email_source,
                    env,
                    timeout_s=otp_timeout,
                    sender_hint="xiaomi",
                    work_dir=Path(runtime) / "output" / "otp_bridge",
                )
            except Exception as exc:
                raise FailFastError(f"mimo mail allocate failed: {exc}") from exc
            if mailbox is not None:
                mail_meta = {
                    "fixed_email": mailbox.address,
                    "email_source": getattr(email_source, "name", ""),
                    "otp_helper": bool(env.get("OTP_HELPER")),
                }

        try:
            try:
                proc = run_command(
                    ["bash", str(runner), "1"],
                    cwd=str(ROOT),
                    env=env,
                    timeout_s=timeout_s,
                )
            except Exception as exc:
                raise FailFastError(f"mimo spawn failed: {exc}") from exc

            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            if proc.timed_out:
                raise ProviderError(f"mimo register timeout after {timeout_s}s")

            email, secret, password = self._parse_this_run(
                stdout=proc.stdout or "",
                keys_delta=read_appended(keys_path, off_keys),
                accounts_delta=read_appended(accounts_path, off_acc),
            )
            if not email and mailbox is not None:
                email = mailbox.address

            arts = {
                "runtime": runtime,
                "keys_path": str(keys_path),
                "accounts_path": str(accounts_path),
                "exit_code": proc.returncode,
                "tail": redact_log_tail(out, limit=1500),
                **mail_meta,
            }

            ok = proc.returncode == 0 and bool(secret)
            if not ok:
                kind = self._classify(out)
                return RegisterResult(
                    ok=False,
                    provider=self.name,
                    email=email,
                    error=f"mimo exit={proc.returncode}"
                    + ("" if secret else " (no this-run secret)"),
                    error_kind=kind,
                    artifacts=arts,
                )

            success = True
            return RegisterResult(
                ok=True,
                provider=self.name,
                email=email,
                password=password,
                secret=secret,
                secret_kind="api_key",
                artifacts=arts,
            )
        finally:
            _release()

    @staticmethod
    def _classify(out: str) -> str:
        low = out.lower()
        if "fail-fast" in low or "fatal" in low:
            return "fatal"
        if "geetest" in low or "captcha" in low:
            return "captcha"
        if "otp" in low and "timeout" in low:
            return "mail_miss"
        return "provider"

    @classmethod
    def _parse_this_run(
        cls,
        *,
        stdout: str,
        keys_delta: str,
        accounts_delta: str,
    ) -> tuple[str, str, str]:
        email, secret, password = "", "", ""

        # 1) Structured RESULT_JSON from register-one (preferred)
        for m in _RESULT_LINE.finditer(stdout or ""):
            try:
                data = json.loads(m.group(1))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if str(data.get("status") or "").upper() == "SUCCESS":
                cand = str(
                    data.get("apiKey") or data.get("api_key") or data.get("secret") or ""
                ).strip()
                if is_api_key(cand):
                    secret = cand
                email = str(data.get("email") or "") or email
                password = str(data.get("password") or "") or password

        # 2) Increment of accounts.jsonl only
        if not secret and accounts_delta.strip():
            for line in accounts_delta.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except Exception:
                    continue
                if not isinstance(last, dict):
                    continue
                cand = str(last.get("apiKey") or last.get("api_key") or last.get("key") or "").strip()
                if is_api_key(cand):
                    secret = cand
                    email = str(last.get("email") or last.get("address") or email)
                    password = str(last.get("password") or password)

        # 3) Increment of success_keys.txt only
        if not secret and keys_delta.strip():
            for line in keys_delta.splitlines():
                m = API_KEY_SEARCH_RE.search(line)
                if m:
                    secret = m.group(0)

        # 4) SUCCESS json log line without RESULT_JSON prefix (prefix only)
        if not secret:
            for line in (stdout or "").splitlines():
                if '"status": "SUCCESS"' in line or '"status":"SUCCESS"' in line:
                    try:
                        data = json.loads(line.strip())
                        # never accept prefix-only as secret
                        email = email or str(data.get("email") or "")
                    except Exception:
                        pass

        return email, secret, password
