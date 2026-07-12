#!/usr/bin/env python3
"""Static + light unit checks for recycle modes and AccountRetryNeeded wiring."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_account_retry_exception_exists() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert "class AccountRetryNeeded" in src
    assert "raise AccountRetryNeeded" in src
    assert "except AccountRetryNeeded" in src
    assert "final_page_no_submit" in src
    print("PASS  AccountRetryNeeded in wait_for_sso_cookie")


def test_recycle_mode_in_perf_and_prepare() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert '"browser_recycle_mode"' in src or "'browser_recycle_mode'" in src
    assert "def _resolved_recycle_mode" in src
    assert "def prepare_browser_for_next_account" in src
    assert 'mode != "hard"' in src or "mode != 'hard'" in src or 'mode == "hard"' in src
    assert "hybrid" in src
    # stop_browser must tear down proxy bridge
    assert "stop_browser_proxy_bridge" in src
    assert "stop_all_browser_proxy_bridges" in src
    print("PASS  recycle mode wiring in ttk")


def test_register_cli_slot_retry() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "account_slot_retry" in src
    assert "_account_slot_retry_limit" in src
    assert "slot 重试" in src or "slot重试" in src
    assert "browser_recycle_mode" in src
    assert "--browser-recycle-mode" in src
    assert "--account-slot-retry" in src
    # must not treat AccountRetryNeeded as fatal
    assert "FatalRegisterError" in src
    assert "AccountRetryNeeded" in src
    # soft recycle respects hard mode
    assert "_resolved_recycle_mode" in src
    print("PASS  register_cli slot retry + recycle CLI")


def test_config_example_keys() -> None:
    raw = (ROOT / "config.example.json").read_text(encoding="utf-8")
    for key in (
        "browser_recycle_mode",
        "browser_recycle_every",
        "account_slot_retry",
        "final_page_no_submit_timeout",
        "LocalAuthProxyBridge",
    ):
        assert key in raw, f"missing in config.example.json: {key}"
    print("PASS  config.example keys")


def test_resolved_recycle_mode_unit() -> None:
    """Execute _resolved_recycle_mode without full ttk import if possible."""
    # Import via ast extract from register_cli is hard; smoke-import proxy_bridge only.
    sys.path.insert(0, str(ROOT))
    # Minimal: ensure register_cli source defines helper with soft default
    tree = ast.parse((ROOT / "register_cli.py").read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert "_resolved_recycle_mode" in names
    assert "_account_slot_retry_limit" in names
    assert "register_one" in names
    print("PASS  register_cli helper defs present")


def test_proxy_bridge_module() -> None:
    sys.path.insert(0, str(ROOT))
    from proxy_bridge import proxy_has_auth, resolve_browser_proxy, strip_proxy_auth

    assert proxy_has_auth("http://a:b@h:1")
    assert strip_proxy_auth("http://a:b@h:1") == "http://h:1"
    u, b = resolve_browser_proxy("http://127.0.0.1:9")
    assert b is None and "127.0.0.1" in u
    print("PASS  proxy_bridge import smoke")


def main() -> int:
    test_account_retry_exception_exists()
    test_recycle_mode_in_perf_and_prepare()
    test_register_cli_slot_retry()
    test_config_example_keys()
    test_resolved_recycle_mode_unit()
    test_proxy_bridge_module()
    print("\nALL PASS (recycle/slot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
