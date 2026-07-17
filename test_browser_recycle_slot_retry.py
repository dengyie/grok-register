#!/usr/bin/env python3
"""Static + unit checks for recycle modes, slot retry, and mint proxy wiring."""

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
    # no silent bridge downgrade
    assert "refuse direct" in src or "proxy bridge failed" in src
    assert "apply_config_proxy" in src
    print("PASS  AccountRetryNeeded + proxy API in ttk")


def test_recycle_mode_in_perf_and_prepare() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert '"browser_recycle_mode"' in src or "'browser_recycle_mode'" in src
    assert "def _resolved_recycle_mode" in src
    assert "def prepare_browser_for_next_account" in src
    assert 'mode != "hard"' in src or "mode != 'hard'" in src or 'mode == "hard"' in src
    assert "hybrid" in src
    assert "stop_browser_proxy_bridge" in src
    assert "stop_all_browser_proxy_bridges" in src
    # dead helper must stay gone
    assert "def _thread_proxy_bridge" not in src
    print("PASS  recycle mode wiring in ttk")


def test_register_cli_slot_retry() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "account_slot_retry" in src
    assert "_account_slot_retry_limit" in src
    assert "slot 重试" in src or "slot重试" in src
    assert "browser_recycle_mode" in src
    assert "--browser-recycle-mode" in src
    assert "--account-slot-retry" in src
    assert "FatalRegisterError" in src
    assert "AccountRetryNeeded" in src
    assert "_resolved_recycle_mode" in src
    # no local AccountRetryNeeded fallback class
    assert "class AccountRetryNeeded" not in src
    # no worker×slot multiplicative outer re-run loop
    assert "while retry < 2" not in src
    assert "slot_exhausted" in src
    assert "skip-outer-retry" in src
    # patch must forward apply_config_proxy
    assert "apply_config_proxy" in src
    print("PASS  register_cli slot retry + recycle CLI")


def test_mint_proxy_once() -> None:
    src = (ROOT / "cpa_xai" / "browser_confirm.py").read_text(encoding="utf-8")
    assert "apply_config_proxy=False" in src
    assert 'browser_proxy=""' in src or "browser_proxy=''" in src
    assert "resolve_browser_proxy" in src
    assert "_auth_proxy_bridge" in src
    print("PASS  mint path proxy set once")


def test_config_example_keys() -> None:
    raw = (ROOT / "config.example.json").read_text(encoding="utf-8")
    for key in (
        "browser_recycle_mode",
        "browser_recycle_every",
        "account_slot_retry",
        "final_page_no_submit_timeout",
        "LocalAuthProxyBridge",
        "soft 忽略",
    ):
        assert key in raw, f"missing in config.example.json: {key}"
    print("PASS  config.example keys")


def test_resolved_recycle_mode_unit() -> None:
    sys.path.insert(0, str(ROOT))
    tree = ast.parse((ROOT / "register_cli.py").read_text(encoding="utf-8"))
    names = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert "_resolved_recycle_mode" in names
    assert "_account_slot_retry_limit" in names
    assert "register_one" in names
    print("PASS  register_cli helper defs present")


def test_account_slot_retry_limit_values() -> None:
    """0 must stay 0; missing/None/empty → 3; clamp to [0,10]."""
    sys.path.insert(0, str(ROOT))
    # Import only the pure helper without side-effectful register_cli import.
    # Execute the function body from source via a minimal namespace.
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    # Locate function AST and compile just that function + deps
    tree = ast.parse(src)
    fn_node = None
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_account_slot_retry_limit":
            fn_node = n
            break
    assert fn_node is not None
    mod = ast.Module(body=[fn_node], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns: dict = {}
    exec(compile(mod, "<slot_limit>", "exec"), ns)
    limit = ns["_account_slot_retry_limit"]

    assert limit({"account_slot_retry": 0}) == 0
    assert limit({"account_slot_retry": "0"}) == 0
    assert limit({"account_slot_retry": 3}) == 3
    assert limit({"account_slot_retry": 11}) == 10
    assert limit({"account_slot_retry": -1}) == 0
    assert limit({}) == 3
    assert limit({"account_slot_retry": None}) == 3
    assert limit({"account_slot_retry": ""}) == 3
    assert limit({"account_slot_retry": "nope"}) == 3
    print("PASS  account_slot_retry_limit 0/3/11/missing")


def test_proxy_bridge_module() -> None:
    sys.path.insert(0, str(ROOT))
    from proxy_bridge import proxy_has_auth, resolve_browser_proxy, strip_proxy_auth

    assert proxy_has_auth("http://a:b@h:1")
    assert strip_proxy_auth("http://a:b@h:1") == "http://h:1"
    u, b = resolve_browser_proxy("http://127.0.0.1:9")
    assert b is None and "127.0.0.1" in u
    print("PASS  proxy_bridge import smoke")


def test_list_rotate_hard_recycles_browser() -> None:
    """list-mode proxy rotate must stop_browser so --proxy-server rebinds."""
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "def _ensure_browser" in src
    assert 'rotate_result.get("rotated")' in src or "rotate_result.get('rotated')" in src
    assert '== "list"' in src or "== 'list'" in src
    assert "list 代理已轮换" in src
    assert "reg.stop_browser()" in src
    # must sit inside _ensure_browser path (not only elsewhere)
    idx = src.find("def _ensure_browser")
    assert idx >= 0
    chunk = src[idx : idx + 2500]
    assert "list 代理已轮换" in chunk
    assert "stop_browser" in chunk
    print("PASS  list rotate hard-recycles browser in _ensure_browser")


def test_final_page_cf_fail_fast_markers() -> None:
    """final-page Turnstile must fail-fast via AccountRetryNeeded + log throttle."""
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert "final_cf_wait_since" in src
    assert "last_final_cf_token_len" in src
    assert "final_cf_retried" in src
    assert "final_cf_stuck_timeout" in src
    assert "final_cf_retry_limit" in src
    assert "最终页 Turnstile 卡住 fail-fast" in src
    assert "最终页 Turnstile retries exhausted" in src
    assert "raise AccountRetryNeeded" in src
    # fill_email must re-raise AccountRetryNeeded (SPA-stuck / browser_boot)
    assert "except AccountRetryNeeded" in src
    fill_idx = src.find("def fill_email_and_submit")
    assert fill_idx >= 0
    fill_chunk = src[fill_idx : fill_idx + 3500]
    assert "except AccountRetryNeeded" in fill_chunk
    assert "raise" in fill_chunk[fill_chunk.find("except AccountRetryNeeded") :]
    print("PASS  final-page CF fail-fast + fill_email re-raise AccountRetryNeeded")


def main() -> int:
    test_account_retry_exception_exists()
    test_recycle_mode_in_perf_and_prepare()
    test_register_cli_slot_retry()
    test_mint_proxy_once()
    test_config_example_keys()
    test_resolved_recycle_mode_unit()
    test_account_slot_retry_limit_values()
    test_proxy_bridge_module()
    test_list_rotate_hard_recycles_browser()
    test_final_page_cf_fail_fast_markers()
    print("\nALL PASS (recycle/slot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
