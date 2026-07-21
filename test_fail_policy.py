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
    # disk-first (probe_chat=off): mint_token_ok is product success; chat/inject irrelevant
    assert product(
        {"reg_success": 1, "mint_token_ok": 1, "chat_ok": 0, "remote_live_ok": 0},
        {
            "cpa_export_enabled": True,
            "cpa_probe_chat": False,
            "cpa_remote_inject": False,
        },
    ) is True
    assert product(
        {"reg_success": 1, "mint_token_ok": 0, "chat_ok": 0},
        {"cpa_export_enabled": True, "cpa_probe_chat": False},
    ) is False
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


def test_desktop_gui_removed_cli_keeps_product_counters() -> None:
    """Desktop TTK GUI removed; product counters live in register_cli + control plane."""
    ttk = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert "class GrokRegisterGUI" not in ttk
    assert "import tkinter" not in ttk
    assert "Desktop GUI removed" in ttk or "apps.control_api" in ttk
    cli = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    for needle in (
        "product_batch_success",
        "mint_token_ok",
        "chat_ok",
    ):
        assert needle in cli, f"missing CLI product marker: {needle}"
    assert (ROOT / "apps" / "control_api" / "app.py").is_file()
    print("PASS desktop GUI removed; CLI/control_api own product surface")


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


def _load_turnstile_helpers():
    """Load Turnstile demote helpers without importing ttk side effects."""
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {
        "is_turnstile_stuck_error",
        "is_turnstile_headless_upgradeable",
        "is_fatal_register_error",
        "note_turnstile_streak",
        "_turnstile_fatal_streak_limit",
        "turnstile_slot_exhausted",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
    names = {n.name for n in nodes}
    missing = wanted - names
    if missing:
        raise RuntimeError(f"missing turnstile helpers: {sorted(missing)}")
    # Minimal globals used by helpers
    ns: dict = {
        "os": __import__("os"),
        "sys": sys,
        "threading": __import__("threading"),
        "reg": type("R", (), {"config": {}})(),
        "_turnstile_fail_streak_lock": __import__("threading").Lock(),
        "_turnstile_fail_streak": 0,
    }
    # Exec order: stuck → upgradeable → fatal → streak_limit → note_streak → slot helper
    order = {
        "is_turnstile_stuck_error": 0,
        "is_turnstile_headless_upgradeable": 1,
        "is_fatal_register_error": 2,
        "_turnstile_fatal_streak_limit": 3,
        "note_turnstile_streak": 4,
        "turnstile_slot_exhausted": 5,
    }
    nodes.sort(key=lambda n: order.get(n.name, 9))
    mod = ast.Module(body=nodes, type_ignores=[])
    exec(compile(mod, "register_cli.py", "exec"), ns)
    return ns


def test_turnstile_demote_not_unconditional_fatal() -> None:
    """Profile Turnstile stuck is slot-retry class; not bare process-fatal."""
    ns = _load_turnstile_helpers()
    is_stuck = ns["is_turnstile_stuck_error"]
    is_fatal = ns["is_fatal_register_error"]
    is_upgradeable = ns["is_turnstile_headless_upgradeable"]

    profile_msg = (
        "Turnstile 卡住 fail-fast: token_len=0 "
        "stuck_timeout=60.0s retries=3/3 snap={}"
    )
    assert is_stuck(profile_msg) is True
    # Demoted: plain Turnstile stuck must NOT be unconditional process-fatal
    assert is_fatal(profile_msg) is False
    assert is_upgradeable(profile_msg) is True

    final_msg = (
        "最终页 Turnstile 卡住 fail-fast: wait 45s token_len=0 "
        "stuck_timeout=60s retries=2/3"
    )
    assert is_stuck(final_msg) is True
    assert is_fatal(final_msg) is False

    # ARN demote prefix still classifies as stuck
    arn_msg = f"turnstile: {profile_msg}"
    assert is_stuck(arn_msg) is True
    assert is_fatal(arn_msg) is False

    # Real unrecoverable markers still fatal
    assert is_fatal("Hotmail plus-alias 已禁用（mode=off）") is True
    assert is_fatal("Turnstile headless 失败且无可用 DISPLAY/xvfb-run") is True
    assert is_fatal("Turnstile 连续失败 fail-fast streak=6/6: x") is True
    assert is_fatal("headed 需要 DISPLAY/xvfb-run") is True
    # no-DISPLAY demote path must be recognized by is_fatal (marker aligned)
    assert is_fatal(
        "Turnstile 卡住且无可用 DISPLAY/xvfb-run（无法换路重试）: token_len=0"
    ) is True

    # Source wiring: demote path must raise AccountRetryNeeded for turnstile
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "def is_turnstile_stuck_error" in src
    assert "note_turnstile_streak" in src
    assert "def turnstile_slot_exhausted" in src
    assert 'f"turnstile: {msg' in src
    assert "Turnstile 卡住，换路 slot 重试" in src
    assert "Turnstile demote: force headed" in src
    assert '"Turnstile 卡住且无可用 DISPLAY"' in src
    # Must not list bare "Turnstile 卡住 fail-fast" as unconditional fatal marker
    assert '"Turnstile 连续失败 fail-fast"' in src
    # reg_ok / streak reset after SSO (not before wait_for_sso_cookie)
    sso_i = src.find("wait_for_sso_cookie")
    reg_ok_i = src.find('note_egress_outcome(\n                    "reg_ok"')
    if reg_ok_i < 0:
        reg_ok_i = src.find('"reg_ok"')
    streak_ok_i = src.find("note_turnstile_streak(ok=True)")
    assert sso_i > 0 and reg_ok_i > sso_i, "reg_ok must be after wait_for_sso_cookie"
    assert streak_ok_i > sso_i, "streak reset must be after wait_for_sso_cookie"
    # Streak only on slot exhaust, not every mid-slot ARN
    assert "Turnstile slot 耗尽计 streak=" in src
    assert "per-account, not mid-slot" in src
    print("PASS turnstile demote policy (not unconditional fatal)")


def test_turnstile_streak_fail_fast() -> None:
    ns = _load_turnstile_helpers()
    note = ns["note_turnstile_streak"]
    slot_ex = ns["turnstile_slot_exhausted"]
    # Reset via ok=True
    assert note(ok=True) == 0
    assert note(ok=False) == 1
    assert note(ok=False) == 2
    assert note(ok=True) == 0
    assert note(ok=False) == 1
    limit = ns["_turnstile_fatal_streak_limit"]({})
    assert 1 <= limit <= 50
    # Slot-exhaust helper: mid-slot retries must NOT count as exhausted
    # After slot_retry += 1, exhausted iff slot_retry > max_slot_retry
    assert slot_ex(slot_retry=1, max_slot_retry=2) is False
    assert slot_ex(slot_retry=2, max_slot_retry=2) is False
    assert slot_ex(slot_retry=3, max_slot_retry=2) is True
    assert slot_ex(slot_retry=1, max_slot_retry=0) is True  # no budget → first ARN exhausts
    print(f"PASS turnstile streak counter (default limit={limit})")


def test_turnstile_streak_per_account_not_per_attempt() -> None:
    """Simulate ARN chain: only slot-exhausted accounts bump process streak."""
    ns = _load_turnstile_helpers()
    note = ns["note_turnstile_streak"]
    is_stuck = ns["is_turnstile_stuck_error"]
    is_fatal = ns["is_fatal_register_error"]
    slot_ex = ns["turnstile_slot_exhausted"]
    limit_fn = ns["_turnstile_fatal_streak_limit"]

    note(ok=True)
    max_slot_retry = 2
    # One account, 3 Turnstile ARNs (initial + 2 retries) → 1 streak only
    slot_retry = 0
    turnstile_arn = "turnstile: Turnstile 卡住 fail-fast: token_len=0"
    assert is_stuck(turnstile_arn) is True
    for _ in range(3):
        slot_retry += 1
        if slot_retry <= max_slot_retry:
            continue  # mid-slot: no note_turnstile_streak(ok=False)
        streak = note(ok=False)
        assert streak == 1
        assert slot_ex(slot_retry=slot_retry, max_slot_retry=max_slot_retry) is True
    # Second exhausted account → streak 2
    slot_retry = 0
    for _ in range(3):
        slot_retry += 1
        if slot_retry <= max_slot_retry:
            continue
        streak = note(ok=False)
        assert streak == 2
    # reg_ok resets
    assert note(ok=True) == 0
    # Reach fatal at limit without mid-slot inflation
    limit = limit_fn({"turnstile_fatal_streak": 3})
    assert limit == 3
    for i in range(3):
        streak = note(ok=False)
        if streak >= limit:
            fatal_msg = (
                f"Turnstile 连续失败 fail-fast streak={streak}/{limit}: "
                f"{turnstile_arn}"
            )
            assert is_fatal(fatal_msg) is True
            assert streak == 3
            break
    else:
        raise AssertionError("expected streak to hit fatal limit")
    print("PASS turnstile streak per-account (not per mid-slot ARN)")


def test_turnstile_force_headed_on_demote_wiring() -> None:
    """Still-headless demote must force headed before ARN when DISPLAY ready."""
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    # demote block: if browser_headless and headed_display_ready → force False + recycle
    assert "Turnstile demote: force headed + recycle before slot retry" in src
    assert 'reg.config["browser_headless"] = False' in src
    # no-DISPLAY demote string is both raised and in is_fatal markers
    assert "Turnstile 卡住且无可用 DISPLAY/xvfb-run（无法换路重试）" in src
    assert '"Turnstile 卡住且无可用 DISPLAY"' in src
    print("PASS turnstile force-headed demote wiring")


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
    test_desktop_gui_removed_cli_keeps_product_counters()
    test_run_register_preserves_product_exit()
    test_turnstile_demote_not_unconditional_fatal()
    test_turnstile_streak_fail_fast()
    test_turnstile_streak_per_account_not_per_attempt()
    test_turnstile_force_headed_on_demote_wiring()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
