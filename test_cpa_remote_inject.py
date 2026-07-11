#!/usr/bin/env python3
"""Static checks for CPA remote inject helper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_cpa_export():
    spec = importlib.util.spec_from_file_location("cpa_export", ROOT / "cpa_export.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_helpers_present() -> None:
    mod = _load_cpa_export()
    assert hasattr(mod, "inject_cpa_auth_remote")
    assert hasattr(mod, "_resolve_remote_ssh_password")
    assert hasattr(mod, "_config_bool")
    assert mod._config_bool("true") is True
    assert mod._config_bool("0") is False
    print("PASS helpers present")


def test_disabled_skip() -> None:
    mod = _load_cpa_export()
    res = mod.inject_cpa_auth_remote(
        ROOT / "cpa_auths" / ".gitkeep",
        config={"cpa_remote_inject": False},
        log_callback=lambda m: None,
    )
    assert res.get("skipped") is True
    print("PASS disabled skip")


def test_export_wires_remote_flag() -> None:
    src = (ROOT / "cpa_export.py").read_text(encoding="utf-8")
    assert "inject_cpa_auth_remote" in src
    assert "cpa_remote_inject" in src
    assert "remote_inject" in src
    # success path must call inject (regression from missing wire)
    assert "result[\"remote_inject\"]" in src or "result['remote_inject']" in src or 'result["remote_inject"]' in src
    assert "ControlMaster=auto" in src
    assert "_REMOTE_DIR_READY" in src
    print("PASS export wires remote inject")


def test_hotmail_adaptive_poll_markers() -> None:
    src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert 'current_interval = 0.0' in src
    assert "empty_rounds" in src
    assert '"hotmail_poll_interval": 2' in src
    print("PASS hotmail adaptive poll markers")


def test_cli_remote_stats() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "remote_inject_ok" in src
    assert "tebi inject" in src
    assert "tebi注入成功" in src
    print("PASS cli remote stats")


def main() -> int:
    test_helpers_present()
    test_disabled_skip()
    test_export_wires_remote_flag()
    test_hotmail_adaptive_poll_markers()
    test_cli_remote_stats()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
