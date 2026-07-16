#!/usr/bin/env python3
"""Offline tests for browser reliability: start lock, mint retries, chrome-error page."""

from __future__ import annotations

import ast
import os
import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent


def test_tab_pool_chromium_start_lock_source() -> None:
    src = (ROOT / "tab_pool.py").read_text(encoding="utf-8")
    assert "_chromium_start_lock" in src
    assert "def chromium_start_lock" in src
    assert "with _chromium_start_lock:" in src
    assert "browser = Chromium(options)" in src
    assert "def display_available" in src
    print("PASS  tab_pool chromium start lock + display_available")


def test_display_available_unit() -> None:
    sys.path.insert(0, str(ROOT))
    import importlib

    import tab_pool as tp

    importlib.reload(tp)
    # Platform-specific: darwin always True regardless of DISPLAY
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        with mock.patch.dict(os.environ, {"DISPLAY": ""}, clear=False):
            assert tp.display_available() is True
        print("PASS  display_available always True on macOS/Windows")
    else:
        with mock.patch.dict(os.environ, {"DISPLAY": ""}, clear=False):
            os.environ.pop("DISPLAY", None)
            assert tp.display_available() is False
        with mock.patch.dict(os.environ, {"DISPLAY": ":99"}, clear=False):
            assert tp.display_available() is True
        with mock.patch.dict(os.environ, {"DISPLAY": "   "}, clear=False):
            assert tp.display_available() is False
        print("PASS  display_available Linux DISPLAY gate")


def test_create_standalone_retries_and_lock() -> None:
    src = (ROOT / "cpa_xai" / "browser_confirm.py").read_text(encoding="utf-8")
    assert "chromium_start_lock" in src
    assert "max_attempts" in src
    # Default max_attempts must match start_browser (4) — first-start flake recovery.
    assert "max_attempts: int = 4" in src
    assert "max_attempts or 4" in src or "int(max_attempts or 4)" in src
    assert "cleanup_orphan_drission_chromes" in src
    assert "SIGKILL" in src or "signal.SIGKILL" in src
    assert "standalone chromium start failed" in src
    assert "with chromium_start_lock()" in src
    # no bare single Chromium(opts) without lock in create path — lock wraps Chromium
    assert "browser = Chromium(opts)" in src
    assert "headed mint 需要 DISPLAY" in src
    assert "no DISPLAY/xvfb" in src
    print("PASS  create_standalone retry+lock+orphan+DISPLAY source")


def test_hard_recycle_cleans_orphans() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "def _hard_recycle_browser" in src
    # mid-run orphan cleanup
    assert "cleanup_orphans" in src
    assert "hard recycle orphan cleanup" in src
    assert "browser_boot" in src
    assert "def headed_display_ready" in src
    assert "Turnstile headless 失败且无可用 DISPLAY" in src
    assert "headed_display_ready()" in src
    print("PASS  hard recycle orphan cleanup + DISPLAY upgrade gate source")


def test_start_browser_cleans_orphans_on_fail() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert "def start_browser" in src
    assert "TabPool.cleanup_orphans" in src
    assert "def is_chrome_error_page" in src
    assert "def chrome_error_summary" in src
    assert "browser_boot: chrome error page" in src
    assert "raise AccountRetryNeeded" in src
    assert "headed 需要 DISPLAY/xvfb-run" in src
    print("PASS  start_browser orphan + chrome error page + DISPLAY source")


def test_classify_email_stage_browser_boot() -> None:
    sys.path.insert(0, str(ROOT))
    # Load classify without importing full register_cli side effects.
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    needed = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in (
            "classify_email_stage_failure",
            "is_fatal_register_error",
            "is_turnstile_headless_upgradeable",
            "headed_display_ready",
        ):
            needed.append(n)
        if isinstance(n, ast.ImportFrom) and n.module and "fail_policy" in (n.module or ""):
            needed.insert(0, n)
    # Prefer importing is_fatal from module if present as local def; else stub.
    names = {n.name for n in needed if isinstance(n, ast.FunctionDef)}
    if "is_fatal_register_error" not in names:
        # inject stub then classify only
        ns: dict = {
            "is_fatal_register_error": lambda _m: False,
        }
        fn = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "classify_email_stage_failure"
        )
        mod = ast.Module(body=[fn], type_ignores=[])
        ast.fix_missing_locations(mod)
        exec(compile(mod, "<classify>", "exec"), ns)
        classify = ns["classify_email_stage_failure"]
        is_fatal = ns["is_fatal_register_error"]
    else:
        ns = {"os": os, "sys": sys}
        mod = ast.Module(body=needed, type_ignores=[])
        ast.fix_missing_locations(mod)
        exec(compile(mod, "<classify2>", "exec"), ns)
        classify = ns["classify_email_stage_failure"]
        is_fatal = ns["is_fatal_register_error"]

    assert classify("browser_boot: chrome error page url=...") == "browser_boot"
    assert classify("The browser connection fails") == "browser_boot"
    assert classify("standalone chromium start failed after 3 attempts: x") == "browser_boot"
    assert classify("浏览器启动失败，已重试4次: x") == "browser_boot"
    assert classify("未收到验证码") == "mail_miss"
    assert classify("未进入资料页") == "progress_fail"
    assert classify("未找到「使用邮箱注册」按钮或邮箱表单未出现") == "other"
    # DISPLAY-related messages must be fatal (not browser_boot retry spin)
    assert is_fatal("Turnstile headless 失败且无可用 DISPLAY/xvfb-run") is True
    assert is_fatal("headed 需要 DISPLAY/xvfb-run（当前 Linux DISPLAY 为空）") is True
    assert classify("Turnstile headless 失败且无可用 DISPLAY/xvfb-run") == "fatal"
    print("PASS  classify browser_boot + DISPLAY fatal")


def test_is_chrome_error_page_unit() -> None:
    sys.path.insert(0, str(ROOT))
    # Import helpers by executing only the pure functions from ttk would pull heavy deps.
    # Instead, re-implement the url/title quick path tests via a minimal fake page
    # after importing from a stripped exec of the two functions.
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in (
            "is_chrome_error_page",
            "chrome_error_summary",
        ):
            fns.append(n)
    assert len(fns) == 2
    mod = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(mod)
    ns: dict = {}
    exec(compile(mod, "<chrome_err>", "exec"), ns)
    is_err = ns["is_chrome_error_page"]
    summary = ns["chrome_error_summary"]

    class FakePage:
        def __init__(self, url="", title="", js_ret="ok", body=""):
            self.url = url
            self.title = title
            self._js = js_ret
            self._body = body

        def run_js(self, _code):
            # summary path requests body slice; error probe returns _js
            if "innerText" in _code and "needles" not in _code and "main-frame-error" not in _code:
                return self._body
            return self._js

    assert is_err(None) is False
    assert is_err(FakePage(url="https://accounts.x.ai/sign-up")) is False
    assert is_err(FakePage(url="chrome-error://chromewebdata/")) is True
    assert is_err(FakePage(url="https://x.com", title="This site can’t be reached")) is True
    assert is_err(FakePage(url="https://x.com", js_ret="error")) is True
    assert is_err(FakePage(url="https://x.com", js_ret="ok")) is False
    s = summary(FakePage(url="chrome-error://x", title="err", body="ERR_PROXY"))
    assert "chrome-error" in s
    print("PASS  is_chrome_error_page unit")


def test_create_standalone_retries_mocked() -> None:
    """3 connection fails → BrowserConfirmError; orphan cleanup called."""
    sys.path.insert(0, str(ROOT))
    # Stub DrissionPage before import path uses it
    fake_dp = types.ModuleType("DrissionPage")

    class BoomChromium:
        def __init__(self, _opts):
            raise RuntimeError("The browser connection fails")

    class OkOpts:
        def set_argument(self, *_a, **_k):
            return None

        def headless(self, *_a, **_k):
            return None

        def auto_port(self):
            return None

        def set_timeouts(self, **_k):
            return None

        def set_browser_path(self, *_a):
            return None

        def add_extension(self, *_a):
            return None

    fake_dp.Chromium = BoomChromium
    fake_dp.ChromiumOptions = lambda: OkOpts()
    sys.modules["DrissionPage"] = fake_dp

    # stub tab_pool cleanup + lock
    import tab_pool as tp

    clean_calls: list[int] = []

    def fake_clean(**_kwargs):
        clean_calls.append(1)
        return {"killed": 0, "matched": 0, "pids": [], "scanned": 0}

    with mock.patch.object(tp, "cleanup_orphan_drission_chromes", side_effect=fake_clean):
        # force reimport of browser_confirm helpers
        if "cpa_xai.browser_confirm" in sys.modules:
            del sys.modules["cpa_xai.browser_confirm"]
        from cpa_xai.browser_confirm import BrowserConfirmError, create_standalone_page

        logs: list[str] = []
        with mock.patch("cpa_xai.browser_confirm._sleep", return_value=None):
            try:
                create_standalone_page(
                    proxy=None,
                    headless=True,
                    log=logs.append,
                    max_attempts=3,
                )
                raise AssertionError("expected BrowserConfirmError")
            except BrowserConfirmError as e:
                assert "after 3 attempts" in str(e)
                assert "browser connection fails" in str(e).lower()
    assert len(clean_calls) == 3
    assert any("start failed" in m for m in logs)
    print("PASS  create_standalone mocked 3-retry + orphan")


def test_create_standalone_succeeds_second_attempt() -> None:
    sys.path.insert(0, str(ROOT))
    fake_dp = types.ModuleType("DrissionPage")
    state = {"n": 0}

    class FlipChromium:
        def __init__(self, _opts):
            state["n"] += 1
            if state["n"] < 2:
                raise RuntimeError("The browser connection fails")
            self.process_id = 4242
            self.latest_tab = object()

        def quit(self):
            return None

    class OkOpts:
        def set_argument(self, *_a, **_k):
            return None

        def headless(self, *_a, **_k):
            return None

        def auto_port(self):
            return None

        def set_timeouts(self, **_k):
            return None

        def set_browser_path(self, *_a):
            return None

        def add_extension(self, *_a):
            return None

    fake_dp.Chromium = FlipChromium
    fake_dp.ChromiumOptions = lambda: OkOpts()
    sys.modules["DrissionPage"] = fake_dp

    if "cpa_xai.browser_confirm" in sys.modules:
        del sys.modules["cpa_xai.browser_confirm"]
    from cpa_xai import browser_confirm as bc

    with mock.patch.object(bc, "_cleanup_orphans_best_effort"):
        with mock.patch.object(bc, "_sleep", return_value=None):
            # Avoid register options import complexity: force fallback options
            with mock.patch.object(bc, "_build_standalone_options", return_value=OkOpts()):
                with mock.patch.object(
                    bc, "_resolve_standalone_chrome_proxy", return_value=("", None)
                ):
                    browser, page = bc.create_standalone_page(
                        proxy=None, headless=True, log=lambda _m: None, max_attempts=3
                    )
    assert state["n"] == 2
    assert browser is not None and page is not None
    print("PASS  create_standalone succeeds on 2nd attempt")


def test_create_standalone_headed_no_display_fail_fast() -> None:
    """Headed mint with empty DISPLAY must raise immediately (no 3× spin)."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        # macOS always has display_available=True — skip behavior test, check source only
        src = (ROOT / "cpa_xai" / "browser_confirm.py").read_text(encoding="utf-8")
        assert "headed mint 需要 DISPLAY" in src
        print("PASS  create_standalone headed DISPLAY fail-fast (source-only on macOS)")
        return

    sys.path.insert(0, str(ROOT))
    fake_dp = types.ModuleType("DrissionPage")

    class NeverChromium:
        def __init__(self, _opts):
            raise AssertionError("Chromium must not start without DISPLAY")

    class OkOpts:
        def set_argument(self, *_a, **_k):
            return None

        def headless(self, *_a, **_k):
            return None

        def auto_port(self):
            return None

        def set_timeouts(self, **_k):
            return None

        def set_browser_path(self, *_a):
            return None

        def add_extension(self, *_a):
            return None

    fake_dp.Chromium = NeverChromium
    fake_dp.ChromiumOptions = lambda: OkOpts()
    sys.modules["DrissionPage"] = fake_dp

    if "cpa_xai.browser_confirm" in sys.modules:
        del sys.modules["cpa_xai.browser_confirm"]
    from cpa_xai.browser_confirm import BrowserConfirmError, create_standalone_page

    logs: list[str] = []
    with mock.patch.dict(os.environ, {"DISPLAY": ""}, clear=False):
        os.environ.pop("DISPLAY", None)
        with mock.patch("cpa_xai.browser_confirm._sleep", return_value=None) as sleep_m:
            try:
                create_standalone_page(
                    proxy=None,
                    headless=False,
                    log=logs.append,
                    max_attempts=3,
                )
                raise AssertionError("expected BrowserConfirmError for empty DISPLAY")
            except BrowserConfirmError as e:
                assert "DISPLAY" in str(e) or "xvfb" in str(e).lower()
        assert sleep_m.call_count == 0
    print("PASS  create_standalone headed no-DISPLAY fail-fast")


def test_headed_display_ready_unit() -> None:
    sys.path.insert(0, str(ROOT))
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "headed_display_ready"
    )
    ns: dict = {"os": os, "sys": sys}
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<hdr>", "exec"), ns)
    ready = ns["headed_display_ready"]
    with mock.patch("tab_pool.display_available", return_value=False):
        assert ready() is False
    with mock.patch("tab_pool.display_available", return_value=True):
        assert ready() is True
    print("PASS  headed_display_ready unit")


def main() -> int:
    test_tab_pool_chromium_start_lock_source()
    test_display_available_unit()
    test_create_standalone_retries_and_lock()
    test_hard_recycle_cleans_orphans()
    test_start_browser_cleans_orphans_on_fail()
    test_classify_email_stage_browser_boot()
    test_is_chrome_error_page_unit()
    test_create_standalone_retries_mocked()
    test_create_standalone_succeeds_second_attempt()
    test_create_standalone_headed_no_display_fail_fast()
    test_headed_display_ready_unit()
    print("\nALL PASS (browser reliability)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
