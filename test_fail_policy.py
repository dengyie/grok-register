#!/usr/bin/env python3
"""Static checks for email-stage failure classification + open_signup hardening."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_fail_fast_helpers():
    """Load is_fatal_register_error + classify without importing ttk."""
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {
        "is_fatal_register_error",
        "classify_email_stage_failure",
        "product_batch_success",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
    names = {n.name for n in nodes}
    if "is_fatal_register_error" not in names or "classify_email_stage_failure" not in names:
        raise RuntimeError(f"missing core helpers, got {sorted(names)}")
    # is_fatal must come before classify (classify calls it); product free
    order = {"is_fatal_register_error": 0, "product_batch_success": 1, "classify_email_stage_failure": 2}
    nodes.sort(key=lambda n: order.get(n.name, 9))
    module = ast.Module(body=nodes, type_ignores=[])
    code = compile(module, "register_cli.py", "exec")
    ns: dict = {}
    exec(code, ns)
    return (
        ns["is_fatal_register_error"],
        ns["classify_email_stage_failure"],
        ns.get("product_batch_success"),
    )


def test_classify() -> None:
    is_fatal, classify, _product = _load_fail_fast_helpers()
    cases = [
        ("验证码已填写，但未进入资料页: code=ABC", "progress_fail"),
        ("验证码已填写，但未进入资料页 IMAP", "progress_fail"),
        ("Hotmail/Outlook 在 1200s 内未收到验证码邮件: a@b.com", "mail_miss"),
        ("获取验证码失败", "mail_miss"),
        ("验证码已获取，但自动填写/提交失败: code=1", "mail_miss"),
        ("验证码 IMAP 连接失败", "mail_miss"),
        ("IMAP SSL EOF", "other"),
        ("未找到邮箱输入框或注册按钮", "other"),
        ("浏览器启动失败", "browser_boot"),
        ("net::ERR_CONNECTION_CLOSED", "browser_boot"),
        ("ERR_CONNECTION_RESET at accounts.x.ai", "browser_boot"),
        ("ERR_PROXY_CONNECTION_FAILED", "browser_boot"),
        ("ERR_TUNNEL_CONNECTION_FAILED", "browser_boot"),
        ("打开注册页失败: page is None", "other"),
        (
            "Hotmail/Outlook 可用别名已耗尽：请增加 hotmail_max_aliases_per_account、"
            "补充 mail_credentials.txt，或清理 emails_used.txt / emails_error.txt",
            "fatal",
        ),
        ("Hotmail/Outlook 账号文件不存在: /tmp/x", "fatal"),
        ("Hotmail/Outlook 账号文件无有效记录: /tmp/x", "fatal"),
        ("Cloudflare API Base 未配置", "fatal"),
        ("DuckMail 没有返回任何可用域名", "fatal"),
        ("Gmail 模式需要 gmail_imap_user / GMAIL_IMAP_USER", "fatal"),
        ("Gmail catch-all 需要在 defaultDomains 中配置已路由到该 Gmail 的域名", "fatal"),
        ("Gmail IMAP 认证失败: [AUTHENTICATIONFAILED] Invalid credentials", "fatal"),
        (
            "Hotmail plus-alias 已禁用（hotmail_allow_plus_alias=false / mode=off）：主邮箱均已消耗",
            "fatal",
        ),
        (
            "browser_boot: signup_spa_stuck 邮箱注册按钮点击后停留在「您正在登录」"
            "中间态，邮箱表单未挂载",
            "browser_boot",
        ),
        (
            "browser_boot: signup_spa_stuck 未找到邮箱表单：页面停在「您正在登录」"
            "中间态（点击邮箱注册后未挂载输入框）",
            "browser_boot",
        ),
        (
            "邮箱注册按钮点击后停留在「您正在登录」中间态，邮箱表单未挂载",
            "browser_boot",
        ),
        (
            "未找到邮箱表单：页面停在「您正在登录」中间态（点击邮箱注册后未挂载输入框）",
            "browser_boot",
        ),
        # SSO post-submit mid-state must stay other (not browser_boot recycle policy)
        ("final-page-no-submit:您正在登录 您正在登录 | 返回 返回", "other"),
    ]
    failed = 0
    for msg, expect in cases:
        got = classify(msg)
        ok = got == expect
        print(f"{'PASS' if ok else 'FAIL'}  {got!r:16} expect={expect!r:16}  {msg[:48]}")
        if not ok:
            failed += 1
        if expect == "fatal" and not is_fatal(msg):
            print(f"FAIL  is_fatal should be True for: {msg[:48]}")
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


def test_fatal_stop_wiring() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    for needle in (
        "class FatalRegisterError",
        "def request_fatal_stop",
        "def is_fatal_register_error",
        "_fatal_stop",
        "raise FatalRegisterError",
        "except FatalRegisterError",
        "exit_code = 2",
        "return exit_code",
        "致命错误，停止整批（不空转）",
        "致命错误已停止任务（不空转）",
    ):
        assert needle in src, f"missing: {needle}"
    # worker must check stop flag and must NOT retry FatalRegisterError
    assert "if _fatal_stop.is_set()" in src
    # bare `return 2` is NOT required — main uses exit_code then return exit_code
    print("PASS  fatal stop wiring present")


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



def test_product_batch_success() -> None:
    is_fatal, _classify, product = _load_fail_fast_helpers()
    assert product is not None
    assert product({"reg_success": 0}, {"cpa_export_enabled": True}) is False
    assert product({"reg_success": 1, "chat_ok": 0}, {"cpa_export_enabled": True}) is False
    assert product({"reg_success": 1, "chat_ok": 1}, {"cpa_export_enabled": True}) is True
    assert product(
        {"reg_success": 1, "chat_ok": 1, "remote_live_ok": 0},
        {"cpa_export_enabled": True, "cpa_remote_inject": True},
    ) is False
    assert product(
        {"reg_success": 1, "chat_ok": 1, "remote_live_ok": 1},
        {"cpa_export_enabled": True, "cpa_remote_inject": True},
    ) is True
    # pure register mode
    assert product({"reg_success": 2, "chat_ok": 0}, {"cpa_export_enabled": False}) is True
    # fatal markers for alias kill-switch
    assert is_fatal("Hotmail plus-alias 已禁用（mode=off）") is True
    assert is_fatal("plus-alias 已禁用") is True
    print("PASS product_batch_success + alias fatal markers")


def test_alias_kill_switch_source() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert '"hotmail_alias_mode": "off"' in src
    assert '"hotmail_allow_plus_alias": False' in src or '"hotmail_allow_plus_alias": false' in src
    assert "HOTMAIL_ALLOW_PLUS_ALIAS" in src
    assert "Hotmail plus-alias 已禁用" in src
    assert "hotmail_allow_plus_alias" in src
    cfg = (ROOT / "config.example.json").read_text(encoding="utf-8")
    assert '"hotmail_allow_plus_alias": false' in cfg
    assert '"hotmail_alias_mode": "off"' in cfg
    print("PASS alias kill-switch source + config.example")


def test_cli_product_exit_wiring() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "def product_batch_success" in src
    assert "product_batch_success(s, cfg_exit)" in src
    # disk-first: message names current product criterion (chat_ok or mint_token_ok)
    assert (
        "未达到产品可用 free Build 标准" in src
        or "本批未达到当前产品标准" in src
    )
    assert "disk-first (probe_chat=off)" in src
    assert "mint_token_ok" in src
    print("PASS cli product exit wiring")


def test_cli_mint_token_ok_honesty() -> None:
    """mint_token_ok (OIDC write) must be distinct from mint_success (product ok)."""
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert '"mint_token_ok": 0' in src or "'mint_token_ok': 0" in src
    assert '_inc("mint_token_ok")' in src
    assert "CPA token写入" in src
    assert "CPA产品OK" in src
    assert "CPA写失败" in src
    # SUMMARY_JSON must expose mint_token_ok additively while keeping mint_success
    assert '"mint_token_ok": int(s.get("mint_token_ok"' in src
    assert '"mint_success": int(s.get("mint_success"' in src
    # token write counted even when product ok is false
    assert 'if result.get("token_ok") is True:' in src
    assert 'if result.get("ok"):' in src
    print("PASS cli mint_token_ok honesty metrics")


def test_gui_product_batch_summary() -> None:
    """GUI must track free Build product counters and end-of-batch product_ok."""
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    for needle in (
        "self.cpa_token_ok_count = 0",
        "self.cpa_product_ok_count = 0",
        "self.cpa_chat_ok_count = 0",
        "self.cpa_chat_denied_count = 0",
        "self.cpa_remote_live_ok_count = 0",
        "self.cpa_remote_inject_skip_count = 0",
        "self.cpa_token_ok_count += 1",
        "self.cpa_product_ok_count += 1",
        "self.cpa_chat_ok_count += 1",
        "free Build 产品",
        "product_ok=",
        "from register_cli import product_batch_success",
        "注册成功",  # end summary must not conflate register with product
    ):
        assert needle in src, f"missing GUI product marker: {needle}"
    # batch start must reset product counters (not only __init__)
    assert src.count("self.cpa_token_ok_count = 0") >= 2
    print("PASS gui free Build product batch summary")


def test_run_register_preserves_product_exit() -> None:
    """run-register.sh must not mask register_cli exit with `| tee` alone."""
    path = ROOT / "run-register.sh"
    assert path.is_file(), "run-register.sh missing at repo root (pxed production entry)"
    src = path.read_text(encoding="utf-8")
    assert "PIPESTATUS[0]" in src
    # ban: exec ... | tee which always yields tee's 0
    assert "exec python" not in src
    assert "exec xvfb-run" not in src
    assert "exit \"$code\"" in src or "exit $code" in src
    print("PASS run-register preserves product exit (PIPESTATUS)")


def main() -> int:
    test_classify()
    test_open_signup_hardens_release()
    test_soft_hard_helpers()
    test_fatal_stop_wiring()
    test_hotpath_no_mojibake()
    test_product_batch_success()
    test_alias_kill_switch_source()
    test_cli_product_exit_wiring()
    test_cli_mint_token_ok_honesty()
    test_gui_product_batch_summary()
    test_run_register_preserves_product_exit()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
