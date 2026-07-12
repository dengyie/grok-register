#!/usr/bin/env python3
"""Unit checks: chat probe classification + product entitlement gate + ledger."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = mod
    if "cpa_xai" not in sys.modules:
        pkg = type(sys)("cpa_xai")
        pkg.__path__ = [str(ROOT / "cpa_xai")]  # type: ignore[attr-defined]
        sys.modules["cpa_xai"] = pkg
    spec.loader.exec_module(mod)
    return mod


def test_classify_chat_probe() -> None:
    probe = _load("cpa_xai.probe", ROOT / "cpa_xai" / "probe.py")
    cls = probe.classify_chat_probe

    ok = cls({"ok": True, "status": 200, "text": "MINT_OK"})
    assert ok["ok"] is True
    assert ok["entitlement_denied"] is False

    denied = cls(
        {
            "ok": False,
            "status": 403,
            "error": '{"error":{"code":"permission_denied","message":"no access"}}',
            "error_code": "permission_denied",
        }
    )
    assert denied["entitlement_denied"] is True
    assert denied["retryable"] is False

    # Any 403 is entitlement (not only permission_denied body)
    bare_403 = cls({"ok": False, "status": 403, "error": ""})
    assert bare_403["entitlement_denied"] is True
    other_403 = cls(
        {"ok": False, "status": 403, "error": '{"error":{"type":"api_error","message":"forbidden"}}'}
    )
    assert other_403["entitlement_denied"] is True

    upgrade = cls({"ok": False, "status": 426, "error": "upgrade required"})
    assert upgrade["entitlement_denied"] is False
    assert upgrade["retryable"] is False
    assert upgrade["reason"] == "auth_or_protocol"

    transient = cls({"ok": False, "status": 429, "error": "rate limit"})
    assert transient["retryable"] is True
    assert transient["entitlement_denied"] is False

    net = cls({"ok": False, "status": 0, "error": "timeout"})
    assert net["retryable"] is True
    print("PASS classify_chat_probe")


def test_probe_mini_response_attaches_classification() -> None:
    src = (ROOT / "cpa_xai" / "probe.py").read_text(encoding="utf-8")
    assert "def classify_chat_probe" in src
    assert "classify_chat_probe(out)" in src
    assert "any HTTP 403" in src or "status == 403" in src
    print("PASS probe_mini_response attaches classification")


def test_mint_default_probe_chat_on() -> None:
    src = (ROOT / "cpa_xai" / "mint.py").read_text(encoding="utf-8")
    assert "probe_chat: bool = True" in src
    assert "entitlement_denied" in src
    assert "do not remint" in src
    assert "max_attempts" in src
    assert "patch_cpa_xai_auth" in src
    print("PASS mint default probe_chat on + retry + stamp")


def test_export_finalize_and_defaults() -> None:
    src = (ROOT / "cpa_export.py").read_text(encoding="utf-8")
    assert 'cfg.get("cpa_probe_chat"), default=True)' in src
    assert "cpa_probe_chat_required" in src
    assert "def finalize_probe_and_gate" in src
    assert "record_entitlement_denied" in src
    assert "skip remote inject (entitlement_denied)" in src
    print("PASS export finalize + defaults")


def test_config_example_chat_keys() -> None:
    raw = (ROOT / "config.example.json").read_text(encoding="utf-8")
    assert '"cpa_probe_chat": true' in raw
    assert "cpa_probe_chat_required" in raw
    print("PASS config.example chat keys")


def test_cli_chat_stats() -> None:
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert '"chat_ok"' in src
    assert '"chat_denied"' in src
    assert "chat可用" in src
    print("PASS cli chat stats")


def test_remint_skips_denied_and_retryable() -> None:
    src = (ROOT / "scripts" / "remint_expired_and_sync_authdir.py").read_text(
        encoding="utf-8"
    )
    assert "load_entitlement_denied_emails" in src
    assert "skipped_denied" in src
    assert "chat_retryable" in src
    assert 'run_cfg["cpa_probe_chat"] = True' in src
    print("PASS remint denied skip + chat_retryable")


def test_writer_ledger_roundtrip() -> None:
    writer = _load("cpa_xai.writer", ROOT / "cpa_xai" / "writer.py")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        writer.record_entitlement_denied(root, "A@B.com", extra={"path": "x"})
        writer.record_entitlement_denied(root, "c@d.com")
        # stamped auth file
        auth = {
            "type": "xai",
            "email": "e@f.com",
            "entitlement_denied": True,
            "usable": False,
            "fail_reason": "entitlement_denied",
        }
        (root / "xai-e@f.com.json").write_text(
            json.dumps(auth) + "\n", encoding="utf-8"
        )
        got = writer.load_entitlement_denied_emails(root)
        assert "a@b.com" in got
        assert "c@d.com" in got
        assert "e@f.com" in got

        # patch usable flags
        p = root / "xai-t@e.com.json"
        p.write_text(json.dumps({"email": "t@e.com", "type": "xai"}) + "\n", encoding="utf-8")
        writer.patch_cpa_xai_auth(p, {"chat_ok": False, "chat_retryable": True, "usable": False})
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["chat_retryable"] is True
        assert writer.is_chat_retryable_auth(data) is True
        assert writer.is_chat_retryable_auth({"entitlement_denied": True}) is False
    print("PASS writer ledger roundtrip")


def test_finalize_probe_and_gate_behavior() -> None:
    exp = _load("cpa_export_finalize", ROOT / "cpa_export.py")

    # entitlement hard-fail
    r = {
        "ok": False,
        "path": "/tmp/x.json",
        "email": "t@e.com",
        "entitlement_denied": True,
        "error": "chat entitlement denied",
        "fail_reason": "entitlement_denied",
    }
    out = exp.finalize_probe_and_gate(
        r, {"cpa_probe_chat": True, "cpa_probe_chat_required": True}, email="t@e.com"
    )
    assert out["ok"] is False
    assert out["non_retryable"] is True
    assert out["skip_remote_inject"] is True

    # transient: stays failed when required, keeps retryable, no entitlement
    r2 = {
        "ok": False,
        "path": "/tmp/x.json",
        "email": "t@e.com",
        "entitlement_denied": False,
        "chat_retryable": True,
        "chat_ok": False,
        "fail_reason": "transient",
        "error": "chat probe failed: status=429",
    }
    out2 = exp.finalize_probe_and_gate(
        r2, {"cpa_probe_chat": True, "cpa_probe_chat_required": True}, email="t@e.com"
    )
    assert out2["ok"] is False
    assert out2["non_retryable"] is False
    assert out2.get("chat_retryable") is True

    # soft-pass when required=false and not entitlement
    r3 = {
        "ok": False,
        "path": "/tmp/x.json",
        "email": "t@e.com",
        "entitlement_denied": False,
        "chat_ok": False,
        "fail_reason": "transient",
        "error": "chat probe failed: status=429",
    }
    out3 = exp.finalize_probe_and_gate(
        r3, {"cpa_probe_chat": True, "cpa_probe_chat_required": False}, email="t@e.com"
    )
    assert out3["ok"] is True
    assert "probe_chat_warning" in out3

    # apply_multi_remote_inject no-ops when ok false
    r4 = {
        "ok": False,
        "path": "/tmp/x.json",
        "entitlement_denied": True,
    }
    out4 = exp.apply_multi_remote_inject(
        r4,
        {"cpa_remote_inject": True, "cpa_remote_auth_dirs": "/root/.cli-proxy-api"},
        inject_fn=lambda *a, **k: {"ok": True, "remote_path": "/x"},
    )
    assert out4.get("remote_injects") is None
    print("PASS finalize_probe_and_gate behavior")


def main() -> int:
    test_classify_chat_probe()
    test_probe_mini_response_attaches_classification()
    test_mint_default_probe_chat_on()
    test_export_finalize_and_defaults()
    test_config_example_chat_keys()
    test_cli_chat_stats()
    test_remint_skips_denied_and_retryable()
    test_writer_ledger_roundtrip()
    test_finalize_probe_and_gate_behavior()
    print("\nALL PASS (cpa chat entitlement gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
