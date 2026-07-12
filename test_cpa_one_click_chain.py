#!/usr/bin/env python3
"""Unit/static checks for one-click CPA chain: priority + multi remote dirs."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_schema_priority_default() -> None:
    schema = _load("cpa_xai.schema", ROOT / "cpa_xai" / "schema.py")
    # fake JWT-like fields not required when expired provided
    payload = schema.build_cpa_xai_auth(
        email="a@b.com",
        access_token="x.y.z",
        refresh_token="r",
        expired="2026-01-01T00:00:00Z",
        expires_in=3600,
        sub="sub1",
    )
    assert payload.get("priority") == 1000
    payload2 = schema.build_cpa_xai_auth(
        email="a@b.com",
        access_token="x.y.z",
        refresh_token="r",
        expired="2026-01-01T00:00:00Z",
        expires_in=3600,
        sub="sub1",
        priority=50,
    )
    assert payload2.get("priority") == 50
    print("PASS schema priority")


def test_resolve_remote_auth_dirs() -> None:
    exp = _load("cpa_export", ROOT / "cpa_export.py")
    # explicit list
    assert exp.resolve_remote_auth_dirs(
        {"cpa_remote_auth_dirs": ["/a", "/b", "/a"]}
    ) == ["/a", "/b"]
    # comma string
    assert exp.resolve_remote_auth_dirs(
        {"cpa_remote_auth_dirs": "/live,/inv"}
    ) == ["/live", "/inv"]
    # legacy single only when inject off
    assert exp.resolve_remote_auth_dirs(
        {"cpa_remote_auth_dir": "/only"}
    ) == ["/only"]
    # inject on + no multi dirs → live+inventory (ignore legacy inventory-only)
    assert exp.resolve_remote_auth_dirs(
        {"cpa_remote_inject": True, "cpa_remote_auth_dir": "/personal/cpa/auths"}
    ) == ["/root/.cli-proxy-api", "/personal/cpa/auths"]
    assert exp.resolve_remote_auth_dirs(
        {"cpa_remote_inject": True}
    ) == ["/root/.cli-proxy-api", "/personal/cpa/auths"]
    # custom live dir in defaults
    assert exp.resolve_remote_auth_dirs(
        {"cpa_remote_inject": True, "cpa_remote_live_dir": "/custom/live"}
    ) == ["/custom/live", "/personal/cpa/auths"]
    # inject off + no dirs → empty
    assert exp.resolve_remote_auth_dirs({}) == []
    print("PASS resolve_remote_auth_dirs")


def test_ensure_auth_file_priority() -> None:
    exp = _load("cpa_export", ROOT / "cpa_export.py")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "xai-t@e.com.json"
        p.write_text(json.dumps({"type": "xai", "email": "t@e.com"}) + "\n", encoding="utf-8")
        got = exp.ensure_auth_file_priority(p, priority=42)
        assert got == 42
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["priority"] == 42
        # idempotent
        got2 = exp.ensure_auth_file_priority(p, priority=42)
        assert got2 == 42
    print("PASS ensure_auth_file_priority")


def test_mint_accepts_priority_kw() -> None:
    src = (ROOT / "cpa_xai" / "mint.py").read_text(encoding="utf-8")
    assert "priority: int = 1000" in src
    assert "priority=pri" in src
    print("PASS mint priority kw")


def test_export_wires_multi_and_priority() -> None:
    src = (ROOT / "cpa_export.py").read_text(encoding="utf-8")
    assert "resolve_remote_auth_dirs" in src
    assert "ensure_auth_file_priority" in src
    assert "apply_multi_remote_inject" in src
    assert "remote_injects" in src
    assert "priority=auth_priority" in src
    assert "cpa_auth_priority" in src
    assert "cpa_remote_live_required" in src
    print("PASS export multi+priority wiring")


def test_config_example_one_click_keys() -> None:
    raw = (ROOT / "config.example.json").read_text(encoding="utf-8")
    for key in (
        "cpa_auth_priority",
        "cpa_remote_auth_dirs",
        "/root/.cli-proxy-api",
        "cpa_remote_inject",
    ):
        assert key in raw, f"missing {key}"
    print("PASS config.example one-click keys")


def test_remint_script_present() -> None:
    p = ROOT / "scripts" / "remint_expired_and_sync_authdir.py"
    assert p.is_file()
    src = p.read_text(encoding="utf-8")
    assert "export_cpa_xai_for_account" in src
    assert "cpa_remote_auth_dirs" in src
    assert "Does NOT start new registration" in src
    print("PASS remint script present")


def test_cli_multi_inject_stats() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "remote_injects" in src
    assert "tebi inject x" in src or "tebi inject ok x" in src
    assert "remote_live_ok" in src
    print("PASS cli multi inject stats")


def test_live_gate_inventory_only_fails() -> None:
    """live fail + inventory ok must hard-fail export (one-click product gate)."""
    exp = _load("cpa_export_live_gate", ROOT / "cpa_export.py")
    calls: list[str] = []

    def fake_inject(path, config=None, log_callback=None):
        rdir = str((config or {}).get("cpa_remote_auth_dir") or "")
        calls.append(rdir)
        if rdir.rstrip("/") == "/root/.cli-proxy-api":
            return {"ok": False, "error": "ssh fail live"}
        return {"ok": True, "remote_path": f"{rdir}/xai-t.json"}

    with tempfile.TemporaryDirectory() as td:
        auth = Path(td) / "xai-t@e.com.json"
        auth.write_text(
            json.dumps({"type": "xai", "email": "t@e.com", "priority": 1000}) + "\n",
            encoding="utf-8",
        )
        result = {"ok": True, "path": str(auth), "email": "t@e.com"}
        cfg = {
            "cpa_remote_inject": True,
            "cpa_remote_auth_dirs": "/root/.cli-proxy-api,/personal/cpa/auths",
            "cpa_remote_live_required": True,
            "cpa_remote_inject_required": False,
        }
        out = exp.apply_multi_remote_inject(result, cfg, inject_fn=fake_inject)
        assert out["ok"] is False
        assert out.get("remote_live_ok") is False
        assert out.get("remote_inventory_ok") is True
        assert "live inject failed" in (out.get("remote_inject_error") or "")
        assert "/root/.cli-proxy-api" in calls
        assert "/personal/cpa/auths" in calls
    print("PASS live gate inventory-only fails")


def test_live_gate_live_ok_keeps_success() -> None:
    exp = _load("cpa_export_live_gate_ok", ROOT / "cpa_export.py")

    def fake_inject(path, config=None, log_callback=None):
        rdir = str((config or {}).get("cpa_remote_auth_dir") or "")
        return {"ok": True, "remote_path": f"{rdir}/xai-t.json"}

    result = {"ok": True, "path": "/tmp/xai-t.json", "email": "t@e.com"}
    cfg = {
        "cpa_remote_inject": True,
        "cpa_remote_auth_dirs": "/root/.cli-proxy-api,/personal/cpa/auths",
        "cpa_remote_live_required": True,
    }
    out = exp.apply_multi_remote_inject(result, cfg, inject_fn=fake_inject)
    assert out["ok"] is True
    assert out.get("remote_live_ok") is True
    assert out.get("remote_inventory_ok") is True
    assert str(out.get("remote_path") or "").startswith("/root/.cli-proxy-api/")
    print("PASS live gate live ok")


def test_config_example_live_keys() -> None:
    raw = (ROOT / "config.example.json").read_text(encoding="utf-8")
    for key in ("cpa_remote_live_dir", "cpa_remote_live_required"):
        assert key in raw, f"missing {key}"
    print("PASS config.example live keys")


def main() -> int:
    test_schema_priority_default()
    test_resolve_remote_auth_dirs()
    test_ensure_auth_file_priority()
    test_mint_accepts_priority_kw()
    test_export_wires_multi_and_priority()
    test_config_example_one_click_keys()
    test_config_example_live_keys()
    test_remint_script_present()
    test_cli_multi_inject_stats()
    test_live_gate_inventory_only_fails()
    test_live_gate_live_ok_keeps_success()
    print("\nALL PASS (cpa one-click)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
