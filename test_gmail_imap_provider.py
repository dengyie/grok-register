#!/usr/bin/env python3
"""Unit checks for Gmail IMAP catch-all provider (no live mailbox required)."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent


def _load_reg():
    """Import engine module. Prefer real deps; no tkinter (desktop GUI removed)."""
    sys.path.insert(0, str(ROOT))
    # Only stub missing optional browser deps; never replace real proxy_rotate.
    try:
        import DrissionPage  # noqa: F401
        from DrissionPage.errors import PageDisconnectedError  # noqa: F401
    except Exception:
        dp = types.ModuleType("DrissionPage")
        dp.Chromium = object
        dp.ChromiumOptions = object
        sys.modules["DrissionPage"] = dp
        err = types.ModuleType("DrissionPage.errors")
        err.PageDisconnectedError = type("PageDisconnectedError", (Exception,), {})
        sys.modules["DrissionPage.errors"] = err
        dp.errors = err
    try:
        from curl_cffi import requests as _r  # noqa: F401
    except Exception:
        curl = types.ModuleType("curl_cffi")
        req = types.ModuleType("curl_cffi.requests")
        curl.requests = req
        sys.modules["curl_cffi"] = curl
        sys.modules["curl_cffi.requests"] = req
    import grok_register_ttk as reg  # noqa: E402

    return reg


def test_gmail_generate_domain_email() -> None:
    reg = _load_reg()
    reg.config["email_provider"] = "gmail"
    reg.config["gmail_imap_user"] = "catch@gmail.com"
    reg.config["gmail_imap_password"] = "app-pass-16chars"
    reg.config["defaultDomains"] = "example-cf.com, other.com"
    with patch.object(reg, "is_email_used", return_value=False):
        addr, token = reg.gmail_get_email_and_token()
    assert "@" in addr
    domain = addr.split("@", 1)[1]
    assert domain in {"example-cf.com", "other.com"}
    assert token.startswith("gmail:")
    assert token in reg._gmail_token_map
    print("PASS gmail generate domain email")


def test_gmail_dispatch_get_email_and_code() -> None:
    reg = _load_reg()
    reg.config["email_provider"] = "gmail"
    reg.config["gmail_imap_user"] = "catch@gmail.com"
    reg.config["gmail_imap_password"] = "app-pass"
    reg.config["defaultDomains"] = "cf-domain.test"
    with patch.object(reg, "is_email_used", return_value=False):
        with patch.object(reg, "gmail_get_email_and_token", return_value=("a@cf-domain.test", "gmail:t")) as ge:
            out = reg.get_email_and_token()
            assert out == ("a@cf-domain.test", "gmail:t")
            assert ge.called
    with patch.object(reg, "gmail_get_oai_code", return_value="ABC-DEF") as gc:
        code = reg.get_oai_code("gmail:t", "a@cf-domain.test", timeout=1)
        assert code == "ABC-DEF"
        assert gc.called
    print("PASS gmail dispatch")


def test_gmail_imap_extracts_code_with_recipient_match() -> None:
    reg = _load_reg()
    reg.config["gmail_require_recipient_match"] = True
    reg.config["gmail_recent_seconds"] = 900
    reg.config["gmail_imap_last_n"] = 5

    import email.message
    from email.utils import formatdate
    import time as _time

    msg = email.message.EmailMessage()
    msg["From"] = "noreply@x.ai"
    msg["To"] = "rand123@cf-domain.test"
    msg["Subject"] = "XYZ-QWE xAI confirmation code"
    msg["Date"] = formatdate(_time.time(), localtime=False, usegmt=True)
    msg.set_content("Your verification code is XYZ-QWE")

    fake_imap = MagicMock()
    fake_imap.noop.return_value = ("OK", [b""])
    fake_imap.select.return_value = ("OK", [b"1"])
    fake_imap.search.return_value = ("OK", [b"1"])
    fake_imap.fetch.return_value = ("OK", [(b"1 (RFC822)", msg.as_bytes())])

    code = reg._gmail_imap_get_code_on_conn(fake_imap, "rand123@cf-domain.test")
    assert code == "XYZ-QWE"
    print("PASS gmail imap extract with recipient match")


def test_gmail_requires_domains() -> None:
    reg = _load_reg()
    reg.config["gmail_imap_user"] = "catch@gmail.com"
    reg.config["gmail_imap_password"] = "app-pass"
    reg.config["defaultDomains"] = ""
    try:
        reg.gmail_get_email_and_token()
        raise AssertionError("expected Exception")
    except Exception as e:
        assert "defaultDomains" in str(e)
    print("PASS gmail requires domains")


def test_gmail_recipient_match_via_received_header() -> None:
    """CF routing often leaves original RCPT only in Received, not To/Delivered-To."""
    reg = _load_reg()
    reg.config["gmail_require_recipient_match"] = True
    reg.config["gmail_recent_seconds"] = 900
    reg.config["gmail_imap_last_n"] = 5

    import email.message
    import time as _time
    from email.utils import formatdate

    msg = email.message.EmailMessage()
    msg["From"] = "noreply@x.ai"
    msg["To"] = "catch@gmail.com"  # Gmail mailbox after CF rewrite
    msg["Delivered-To"] = "catch@gmail.com"
    msg["Received"] = (
        "from filterdrecv-abcd.mail.example.net by mx.google.com "
        "for <rand123@cf-domain.test>; Tue, 14 Jul 2026 00:00:00 +0000"
    )
    msg["Subject"] = "ABC-DEF xAI confirmation code"
    msg["Date"] = formatdate(_time.time(), localtime=False, usegmt=True)
    msg.set_content("Your verification code is ABC-DEF")

    fake_imap = MagicMock()
    fake_imap.noop.return_value = ("OK", [b""])
    fake_imap.select.return_value = ("OK", [b"1"])
    fake_imap.search.return_value = ("OK", [b"1"])
    fake_imap.fetch.return_value = ("OK", [(b"1 (RFC822)", msg.as_bytes())])

    code = reg._gmail_imap_get_code_on_conn(fake_imap, "rand123@cf-domain.test")
    assert code == "ABC-DEF", code
    print("PASS gmail recipient match via Received")


def test_gmail_auth_error_classified_fatal() -> None:
    import ast

    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns: dict = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "is_fatal_register_error",
            "classify_email_stage_failure",
        }:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, "register_cli.py", "exec"), ns)
    is_fatal = ns["is_fatal_register_error"]
    classify = ns["classify_email_stage_failure"]
    cases = [
        "Gmail 模式需要 gmail_imap_user / GMAIL_IMAP_USER",
        "Gmail catch-all 需要在 defaultDomains 中配置已路由到该 Gmail 的域名",
        "Gmail IMAP 认证失败: [AUTHENTICATIONFAILED] Invalid credentials",
        "Gmail IMAP 凭证未配置（gmail_imap_user / gmail_imap_password）",
    ]
    for msg in cases:
        assert is_fatal(msg), msg
        assert classify(msg) == "fatal", (msg, classify(msg))
    print("PASS gmail auth/config fatal")


def test_gmail_cleanup_drops_token_map() -> None:
    reg = _load_reg()
    reg.config["gmail_imap_user"] = "catch@gmail.com"
    reg.config["gmail_imap_password"] = "app-pass"
    reg.config["defaultDomains"] = "cf-domain.test"
    with patch.object(reg, "is_email_used", return_value=False):
        addr, token = reg.gmail_get_email_and_token()
    assert token in reg._gmail_token_map
    reg._gmail_cleanup_email(addr)
    assert token not in reg._gmail_token_map
    assert addr.lower() not in reg._gmail_reserved_emails
    print("PASS gmail cleanup token map")


def test_save_config_strips_gmail_password() -> None:
    reg = _load_reg()
    import json
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "config.json"
        old = reg.CONFIG_FILE
        try:
            reg.CONFIG_FILE = str(cfg_path)
            reg.config["gmail_imap_password"] = "secret-app-pass"
            reg.config["gmail_imap_user"] = "catch@gmail.com"
            reg.save_config()
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert saved.get("gmail_imap_password") == ""
            assert reg.config["gmail_imap_password"] == "secret-app-pass"
        finally:
            reg.CONFIG_FILE = old
    print("PASS save_config strips gmail password")


def test_cli_persist_stage_error_includes_gmail() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "def _should_persist_email_stage_error" in src
    assert '"gmail"' in src
    assert "_should_persist_email_stage_error()" in src
    print("PASS cli persist stage error includes gmail")


def main() -> int:
    test_gmail_generate_domain_email()
    test_gmail_dispatch_get_email_and_code()
    test_gmail_imap_extracts_code_with_recipient_match()
    test_gmail_requires_domains()
    test_gmail_recipient_match_via_received_header()
    test_gmail_auth_error_classified_fatal()
    test_gmail_cleanup_drops_token_map()
    test_save_config_strips_gmail_password()
    test_cli_persist_stage_error_includes_gmail()
    print("\nALL PASS (gmail imap provider)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
