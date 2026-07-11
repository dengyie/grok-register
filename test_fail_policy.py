#!/usr/bin/env python3
"""Static checks for email-stage failure classification + open_signup hardening."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_classify():
    """Load classify_email_stage_failure without importing grok_register_ttk (tkinter)."""
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "classify_email_stage_failure":
            target = node
            break
    if target is None:
        raise RuntimeError("classify_email_stage_failure not found")
    module = ast.Module(body=[target], type_ignores=[])
    code = compile(module, "register_cli.py", "exec")
    ns: dict = {}
    exec(code, ns)
    return ns["classify_email_stage_failure"]


def test_classify() -> None:
    classify = _load_classify()
    cases = [
        ("验证码已填写，但未进入资料页: code=ABC", "progress_fail"),
        ("验证码已填写，但未进入资料页 IMAP", "progress_fail"),
        ("Hotmail/Outlook 在 1200s 内未收到验证码邮件: a@b.com", "mail_miss"),
        ("获取验证码失败", "mail_miss"),
        ("验证码已获取，但自动填写/提交失败: code=1", "mail_miss"),
        ("验证码 IMAP 连接失败", "mail_miss"),
        ("IMAP SSL EOF", "other"),
        ("未找到邮箱输入框或注册按钮", "other"),
        ("浏览器启动失败", "other"),
        ("打开注册页失败: page is None", "other"),
    ]
    failed = 0
    for msg, expect in cases:
        got = classify(msg)
        ok = got == expect
        print(f"{'PASS' if ok else 'FAIL'}  {got!r:16} expect={expect!r:16}  {msg[:48]}")
        if not ok:
            failed += 1
    if failed:
        raise SystemExit(f"classify failures: {failed}")


def test_open_signup_hardens_release() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    # After release_tab, must rebuild via start_browser / restart_browser, not page.get on None
    assert "def open_signup_page" in src
    assert "TabPool.release_tab()" in src
    assert "start_browser" in src
    # The buggy pattern: release_tab then immediate _get_page().get without start
    # Ensure helper / rebuild path exists
    assert "重建浏览器" in src or "start_browser(log_callback" in src
    # page null guard
    assert "页面未就绪" in src or "page is None" in src
    print("PASS  open_signup_page rebuild guards present")


def test_soft_hard_helpers() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "def _soft_recycle_browser" in src
    assert "def _hard_recycle_browser" in src
    assert "clear_session" in src
    assert "classify_email_stage_failure" in src
    # mail_miss uses soft; progress_fail uses hard
    assert "_soft_recycle_browser(worker_id)" in src
    assert "_hard_recycle_browser(worker_id)" in src
    print("PASS  soft/hard recycle helpers present")


def test_hotpath_no_mojibake() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    # hot-path markers that must be correct Chinese
    required = [
        "获取邮箱失败",
        "已创建邮箱:",
        "成功开启 NSFW",
        "获取 DuckMail token 失败",
        "从邮件中提取到验证码",
    ]
    bad = [s for s in required if s not in src]
    if bad:
        raise SystemExit(f"missing fixed strings: {bad}")
    # residual mojibake probe (common broken sequence)
    if "鑾峰彇" in src or "宸插垱寤" in src:
        raise SystemExit("residual mojibake still present")
    print("PASS  hot-path Chinese strings fixed")


def main() -> int:
    test_classify()
    test_open_signup_hardens_release()
    test_soft_hard_helpers()
    test_hotpath_no_mojibake()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
