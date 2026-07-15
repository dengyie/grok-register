"""ChatGPT / OpenAI platform provider — in-process EmailSource consumer."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from register_core.contracts import RegisterResult
from register_core.email.base import EmailSource
from register_core.email.registry import get_email_source
from register_core.errors import FailFastError, MailMissError, ProviderError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROTOCOL_DIR = ROOT / "providers" / "chatgpt"
OUTPUT_DIR = PROTOCOL_DIR / "output"


class ChatGPTProvider:
    """In-process OpenAI platform OAuth register.

    Consumes EmailSource (default tinyhost via auto). Produces refresh_token
    as primary secret for pool use. Never injects production CPA.
    """

    name = "chatgpt"

    def __init__(
        self,
        *,
        proxy: str | None = None,
        email_source_name: str | None = None,
        otp_timeout_s: float = 180,
        email_domain: str | None = None,
        **_: Any,
    ) -> None:
        self.proxy = (
            proxy
            or os.environ.get("CHATGPT_PROXY")
            or os.environ.get("MIMO_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or ""
        )
        # Project-owned nodes.json is the default egress when no fixed proxy set.
        # Do NOT hardcode Clash/mihomo 7897 — external VPN software is optional only.
        self.email_source_name = (
            email_source_name
            or os.environ.get("CHATGPT_EMAIL_SOURCE")
            or "gmail_imap"
        )
        self.otp_timeout_s = float(
            os.environ.get("CHATGPT_OTP_TIMEOUT") or otp_timeout_s
        )
        self.email_domain = (
            email_domain or os.environ.get("CHATGPT_EMAIL_DOMAIN") or "publicvm.com"
        ).strip() or None

    def register_one(
        self,
        *,
        email_source: EmailSource | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RegisterResult:
        from providers.chatgpt.protocol.flow import (
            ChatGPTRegisterError,
            generate_password,
            register_one,
            save_result,
        )

        extra = dict(extra or {})
        # Prefer pipeline-injected proxy (self-controlled list rotation); fall back
        # to constructor/env. Never require Clash UI node selection.
        if not str(extra.get("proxy") or "").strip():
            try:
                from register_core.util.proxy import resolve_attempt_proxy

                resolved, rot_info = resolve_attempt_proxy(extra)
                if resolved:
                    extra["proxy"] = resolved
                if rot_info:
                    extra.setdefault("_proxy_rotate", rot_info)
            except Exception:
                pass
        proxy = str(extra.get("proxy") or self.proxy or "").strip()
        otp_timeout = float(extra.get("otp_timeout_s") or self.otp_timeout_s)
        domain = str(extra.get("email_domain") or self.email_domain or "").strip() or None

        source = email_source
        if source is None:
            try:
                kw: dict[str, Any] = {"proxy": proxy or None}
                if domain and self.email_source_name in ("tinyhost", "auto"):
                    kw["domain"] = domain
                source = get_email_source(self.email_source_name, **kw)
            except Exception as exc:
                raise FailFastError(f"chatgpt email source unavailable: {exc}") from exc
        elif domain and getattr(source, "name", "") == "tinyhost":
            # Pipeline may construct TinyhostSource without domain; pin preferred host.
            try:
                source.forced_domain = domain  # type: ignore[attr-defined]
            except Exception:
                pass

        mailbox = source.allocate()
        email = (mailbox.address or "").strip()
        if not email or "@" not in email:
            raise FailFastError("chatgpt allocate returned empty mailbox")

        password = generate_password()
        t0 = time.time()
        logs: list[str] = []

        def log(msg: str) -> None:
            line = str(msg)
            logs.append(line)
            print(f"[chatgpt] {line}", flush=True)

        def otp_provider() -> str:
            # Split budget across first poll + one resend poll inside protocol flow.
            per_poll = max(45.0, otp_timeout / 2.0)
            otp = source.poll_otp(
                mailbox,
                timeout_s=per_poll,
                poll_interval_s=2.5,
                newer_than_epoch=t0 - 10,
                sender_hint=None,
            )
            return otp.code

        rot_meta = extra.get("_proxy_rotate") if isinstance(extra.get("_proxy_rotate"), dict) else {}
        arts: dict[str, Any] = {
            "runtime": str(PROTOCOL_DIR),
            "email_source": getattr(source, "name", self.email_source_name),
            "mailbox_provider": mailbox.provider,
            "proxy": proxy or "(none)",
            "proxy_mode": str(rot_meta.get("mode") or "fixed"),
            "proxy_label": str(rot_meta.get("label") or proxy or "(none)"),
            "note": "in-process openai platform oauth; no cpa inject; egress self-controlled",
        }

        try:
            result = register_one(
                email=email,
                password=password,
                proxy=proxy,
                otp_provider=otp_provider,
                log=log,
            )
        except ChatGPTRegisterError as exc:
            kind = getattr(exc, "kind", "provider") or "provider"
            try:
                source.release(mailbox, success=False)
            except Exception:
                pass
            if kind == "fatal":
                raise FailFastError(str(exc)) from exc
            # Keep email/password/artifacts on mail_miss (do not raise bare MailMissError
            # which would strip identity in pipeline exception path).
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=email,
                password=password,
                error=str(exc),
                error_kind=kind if kind in ("mail_miss", "provider", "captcha", "other") else "provider",
                secret_kind="none",
                artifacts={**arts, "tail": "\n".join(logs)[-1500:]},
            )
        except MailMissError as exc:
            try:
                source.release(mailbox, success=False)
            except Exception:
                pass
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=email,
                password=password,
                error=str(exc),
                error_kind="mail_miss",
                secret_kind="none",
                artifacts={**arts, "tail": "\n".join(logs)[-1500:]},
            )
        except Exception as exc:
            try:
                source.release(mailbox, success=False)
            except Exception:
                pass
            raise ProviderError(f"chatgpt unexpected: {exc}") from exc

        arts["tail"] = "\n".join(logs)[-1500:]
        arts["steps"] = list((result.steps or {}).keys())
        arts["device_id"] = result.device_id

        if not result.ok:
            try:
                source.release(mailbox, success=False)
            except Exception:
                pass
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=result.email or email,
                password=password,
                error=result.error or "register_failed",
                error_kind=result.error_kind or "provider",
                secret_kind="none",
                artifacts=arts,
            )

        refresh = (result.refresh_token or "").strip()
        access = (result.access_token or "").strip()
        if not refresh:
            try:
                source.release(mailbox, success=False)
            except Exception:
                pass
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=result.email or email,
                password=password,
                error="missing_refresh_token",
                error_kind="provider",
                secret_kind="none",
                artifacts=arts,
            )

        # Persist this-run auth file (private 0600) — operator-local only
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        safe_email = (result.email or email).replace("@", "_at_").replace("/", "_")
        out_path = OUTPUT_DIR / f"chatgpt-{safe_email}-{int(time.time())}.json"
        try:
            save_result(result, out_path)
            arts["auth_path"] = str(out_path)
        except Exception as exc:
            arts["auth_write_error"] = str(exc)[:200]

        # Also append RESULT_JSON-friendly line to accounts.jsonl (offset-safe)
        accounts_path = OUTPUT_DIR / "accounts.jsonl"
        record = {
            "ok": True,
            "email": result.email or email,
            "password": password,
            "refresh_token": refresh,
            "access_token": access,
            "id_token": result.id_token,
            "provider": self.name,
            "ts": int(time.time()),
        }
        try:
            with open(accounts_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            try:
                accounts_path.chmod(0o600)
            except Exception:
                pass
            arts["accounts_path"] = str(accounts_path)
        except Exception as exc:
            arts["accounts_write_error"] = str(exc)[:200]

        try:
            source.release(mailbox, success=True)
        except Exception:
            pass

        return RegisterResult(
            ok=True,
            provider=self.name,
            email=result.email or email,
            password=password,
            secret=refresh,
            secret_kind="refresh_token",
            artifacts={
                **arts,
                "access_token_preview": f"{access[:4]}…{access[-4:]}" if len(access) > 8 else "***",
                "has_id_token": bool(result.id_token),
            },
        )
