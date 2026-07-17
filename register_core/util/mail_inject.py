"""Allocate core mailbox and build env inject for shell adapters (MiMo/Grok)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from register_core.contracts import Mailbox
from register_core.email.base import EmailSource


def _source_name(email_source: EmailSource) -> str:
    return str(getattr(email_source, "name", "") or "").strip().lower()


def _source_kwargs_hint(email_source: EmailSource) -> dict[str, Any]:
    """Best-effort kwargs to rebuild source in a child process."""
    kw: dict[str, Any] = {}
    proxy = getattr(email_source, "proxy", None)
    if proxy is not None:
        kw["proxy"] = proxy
    domain = getattr(email_source, "forced_domain", None) or getattr(
        email_source, "domain", None
    )
    if domain:
        kw["domain"] = domain
    # composite: pass through mailbox attrs
    mb = getattr(email_source, "mailbox", None)
    if mb is not None:
        if getattr(mb, "proxy", None) is not None:
            kw["proxy"] = mb.proxy
        if getattr(mb, "forced_domain", None):
            kw["domain"] = mb.forced_domain
    return kw


def prepare_mail_inject(
    email_source: EmailSource | None,
    env: dict[str, str],
    *,
    timeout_s: float = 180,
    sender_hint: str = "",
    force_helper: bool = False,
    work_dir: str | Path | None = None,
) -> Mailbox | None:
    """If email_source set, allocate and write FIXED_EMAIL + OTP bridge env.

    Returns allocated Mailbox or None when email_source is None (adapter-internal).
    """
    if email_source is None:
        return None

    mailbox = email_source.allocate()
    env["FIXED_EMAIL"] = mailbox.address
    env["MIMO_FIXED_EMAIL"] = mailbox.address
    if mailbox.token:
        env["FIXED_EMAIL_TOKEN"] = mailbox.token
    # Grok ttk: provider=fixed short-circuit
    env["EMAIL_PROVIDER"] = "fixed"
    env.setdefault("email_provider", "fixed")

    name = _source_name(email_source)
    needs_helper = force_helper or ("tinyhost" not in name)
    # tinyhost FIXED_EMAIL alone is enough for MiMo Node poll; still write helper
    # when decode half differs (composite name like cloudflare+gmail).
    if "+" in name:
        needs_helper = True

    if needs_helper or force_helper:
        spec = {
            "address": mailbox.address,
            "token": mailbox.token or "",
            "password": mailbox.password or "",
            "provider": mailbox.provider or name,
            "source": name.split("+", 1)[0] if name else "tinyhost",
            "source_kwargs": _source_kwargs_hint(email_source),
            "timeout_s": float(timeout_s),
            "poll_interval_s": 3,
            "sender_hint": sender_hint or "",
            "newer_than_epoch": time.time(),
            "meta": dict(mailbox.meta or {}),
        }
        # split composite if possible
        mb_obj = getattr(email_source, "mailbox", None)
        dec_obj = getattr(email_source, "decoder", None)
        if mb_obj is not None and dec_obj is not None:
            spec["mailbox_type"] = str(getattr(mb_obj, "name", "") or "")
            spec["decode_type"] = str(getattr(dec_obj, "name", "") or "")
            if "+" in name:
                # prefer explicit types over composite string as registry name
                spec["source"] = str(getattr(mb_obj, "name", "") or spec["source"])

        wd = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="reg_otp_"))
        wd.mkdir(parents=True, exist_ok=True)
        spec_path = wd / "otp_spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        try:
            spec_path.chmod(0o600)
        except Exception:
            pass

        # Prefer module form so PYTHONPATH=repo root works without copying files.
        env["REGISTER_OTP_SPEC_PATH"] = str(spec_path)
        env["REGISTER_OTP_SPEC"] = json.dumps(spec, ensure_ascii=False)
        env["OTP_HELPER_PYTHON"] = sys.executable
        # Node/Grok spawn: python -m register_core.tools.poll_otp <email> [used…]
        # We still set OTP_HELPER to a tiny launcher script for argv compatibility.
        launcher = wd / "otp_helper.py"
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import runpy, sys\n"
            "sys.exit(runpy.run_module('register_core.tools.poll_otp', run_name='__main__') or 0)\n"
            if False
            else (
                "#!/usr/bin/env python3\n"
                "from register_core.tools.poll_otp import main\n"
                "import sys\n"
                "raise SystemExit(main(sys.argv[1:]))\n"
            ),
            encoding="utf-8",
        )
        try:
            launcher.chmod(0o700)
        except Exception:
            pass
        env["OTP_HELPER"] = str(launcher)
        env["MIMO_OTP_HELPER"] = str(launcher)
        env["OTP_HELPER_STRICT"] = env.get("OTP_HELPER_STRICT") or "1"
        # Ensure child can import register_core
        root = str(Path(__file__).resolve().parents[2])
        pp = env.get("PYTHONPATH") or os.environ.get("PYTHONPATH") or ""
        parts = [p for p in pp.split(os.pathsep) if p]
        if root not in parts:
            env["PYTHONPATH"] = os.pathsep.join([root, *parts]) if parts else root

    return mailbox
