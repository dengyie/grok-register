#!/usr/bin/env python3
"""Unit checks: chat probe classification + product entitlement gate + ledger."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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

    exhausted = cls(
        {
            "ok": False,
            "status": 429,
            "error": '{"error":{"code":"subscription:free-usage-exhausted","message":"quota"}}',
            "error_code": "subscription:free-usage-exhausted",
        }
    )
    assert exhausted["retryable"] is False
    assert exhausted["entitlement_denied"] is False
    assert exhausted["reason"] == "usage_exhausted"

    net = cls({"ok": False, "status": 0, "error": "timeout"})
    assert net["retryable"] is True
    print("PASS classify_chat_probe")


def test_probe_mini_response_attaches_classification() -> None:
    src = (ROOT / "cpa_xai" / "probe.py").read_text(encoding="utf-8")
    assert "def classify_chat_probe" in src
    assert "classify_chat_probe(out)" in src
    assert "any HTTP 403" in src or "status == 403" in src
    assert "def probe_chat_with_retries" in src
    assert "def apply_chat_probe_to_result" in src
    print("PASS probe_mini_response attaches classification")


def test_probe_chat_with_retries_and_apply() -> None:
    probe = _load("cpa_xai.probe_retry", ROOT / "cpa_xai" / "probe.py")
    sleeps: list[float] = []
    calls = {"n": 0}

    def fake_mini(_token, **_kw):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            out = {"ok": False, "status": 429, "error": "rate", "error_code": "429"}
            out.update(probe.classify_chat_probe(out))
            return out
        out = {"ok": True, "status": 200, "text": "MINT_OK", "model": "grok-4.5"}
        out.update(probe.classify_chat_probe(out))
        return out

    probe.probe_mini_response = fake_mini  # type: ignore[assignment]
    ch = probe.probe_chat_with_retries(
        "tok",
        max_attempts=3,
        sleep_fn=lambda s: sleeps.append(s),
        log=lambda _m: None,
    )
    assert ch.get("ok") is True
    assert ch.get("attempts") == 3
    assert calls["n"] == 3
    assert len(sleeps) == 2

    calls["n"] = 0
    sleeps.clear()

    def fake_denied(_token, **_kw):  # noqa: ANN001
        calls["n"] += 1
        out = {
            "ok": False,
            "status": 403,
            "error": "permission",
            "error_code": "permission_denied",
        }
        out.update(probe.classify_chat_probe(out))
        return out

    probe.probe_mini_response = fake_denied  # type: ignore[assignment]
    ch2 = probe.probe_chat_with_retries(
        "tok", max_attempts=3, sleep_fn=lambda s: sleeps.append(s)
    )
    assert ch2.get("entitlement_denied") is True
    assert ch2.get("attempts") == 1
    assert calls["n"] == 1
    assert sleeps == []

    r: dict = {}
    probe.apply_chat_probe_to_result(r, ch2)
    assert r["chat_ok"] is False
    assert r["entitlement_denied"] is True
    assert r["chat_retryable"] is False
    assert r["fail_reason"] == "entitlement_denied"
    assert r["usable"] is False

    r_miss: dict = {}
    probe.apply_chat_probe_to_result(r_miss, None, models_missing=True, models_status=200)
    assert r_miss["fail_reason"] == "models_missing_grok_45"
    assert r_miss["chat_ok"] is False
    assert r_miss["chat_retryable"] is False
    print("PASS probe_chat_with_retries + apply")


def test_mint_default_probe_chat_on() -> None:
    src = (ROOT / "cpa_xai" / "mint.py").read_text(encoding="utf-8")
    assert "probe_chat: bool = True" in src
    assert "entitlement_denied" in src
    assert "do not remint" in src or "FAIL-FAST" in src
    assert "probe_chat_with_retries" in src
    assert "apply_chat_probe_to_result" in src
    assert "stamp_auth_chat_fields" in src
    print("PASS mint default probe_chat on + shared probe + stamp")


def test_export_finalize_and_defaults() -> None:
    src = (ROOT / "cpa_export.py").read_text(encoding="utf-8")
    assert 'cfg.get("cpa_probe_chat"), default=True)' in src
    assert "cpa_probe_chat_required" in src
    assert "def finalize_probe_and_gate" in src
    assert "record_entitlement_denied" in src
    assert "skip remote inject (entitlement_denied)" in src
    assert "stamp_auth_chat_fields" in src
    assert "Re-stamp full chat fields after finalize" in src
    print("PASS export finalize + defaults")


def test_config_example_chat_keys() -> None:
    raw = (ROOT / "config.example.json").read_text(encoding="utf-8")
    assert '"cpa_probe_chat": true' in raw
    assert "cpa_probe_chat_required" in raw
    assert "clash_pin_node" in raw
    assert '"cpa_allow_device_flow_fallback": true' in raw
    assert "best-effort" in raw or "Manual-required" in raw
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CPA_ALLOW_DEVICE_FLOW_FALLBACK=true" in env
    assert "GROK_NODE" in env or "CLASH_PIN_NODE" in env
    print("PASS config.example chat keys + pin/fallback docs")


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
    assert "is_entitlement_denied_auth" in src
    assert "chat_ok_n" in src and "chat_denied_n" in src
    assert "chat_ok=" in src and "chat_denied=" in src
    assert 'r.get("chat_ok") is True' in src
    print("PASS remint denied skip + chat_retryable + summary")


def test_remint_collect_todo_behavior() -> None:
    """Tempdir behavior: denied skip, missing, expired, chat_retryable, chat_ok skip."""
    remint_path = ROOT / "scripts" / "remint_expired_and_sync_authdir.py"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    remint = _load("remint_sync_authdir", remint_path)

    future = (datetime.now(timezone.utc) + timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        auth_dir = root / "auths"
        auth_dir.mkdir()
        accounts = root / "accounts.txt"
        accounts.write_text(
            "\n".join(
                [
                    "missing@ex.com----pw----sso=missing-sso-token",
                    "ok@ex.com----pw----sso=ok-sso-token",
                    "retry@ex.com----pw----sso=retry-sso-token",
                    "denied@ex.com----pw----sso=denied-sso-token",
                    "expired@ex.com----pw----sso=expired-sso-token",
                    "ledgerden@ex.com----pw----sso=ledger-sso-token",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        (auth_dir / "xai-ok@ex.com.json").write_text(
            json.dumps(
                {
                    "email": "ok@ex.com",
                    "chat_ok": True,
                    "usable": True,
                    "expired": future,
                    "access_token": "t",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (auth_dir / "xai-retry@ex.com.json").write_text(
            json.dumps(
                {
                    "email": "retry@ex.com",
                    "chat_ok": False,
                    "chat_retryable": True,
                    "fail_reason": "transient",
                    "usable": False,
                    "expired": future,
                    "access_token": "t",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (auth_dir / "xai-denied@ex.com.json").write_text(
            json.dumps(
                {
                    "email": "denied@ex.com",
                    "chat_ok": False,
                    "entitlement_denied": True,
                    "fail_reason": "entitlement_denied",
                    "import_gate": "entitlement_denied",
                    "usable": False,
                    "expired": past,
                    "access_token": "t",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (auth_dir / "xai-expired@ex.com.json").write_text(
            json.dumps(
                {
                    "email": "expired@ex.com",
                    "chat_ok": True,
                    "usable": True,
                    "expired": past,
                    "access_token": "t",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        denied_set = {"ledgerden@ex.com"}

        todo, reasons = remint._collect_todo(
            accounts,
            auth_dir,
            include_missing=True,
            include_expired=True,
            include_chat_retryable=True,
            only_email="",
            limit=0,
            denied_emails=denied_set,
        )
        emails = {acc.email.lower(): reason for acc, reason in todo}
        assert "missing@ex.com" in emails and emails["missing@ex.com"] == "missing"
        assert "retry@ex.com" in emails and emails["retry@ex.com"] == "chat_retryable"
        assert "expired@ex.com" in emails and emails["expired@ex.com"] == "expired"
        assert "ok@ex.com" not in emails
        assert "denied@ex.com" not in emails
        assert "ledgerden@ex.com" not in emails
        assert reasons["skipped_denied"] >= 2
        assert reasons["skipped_chat_ok"] >= 1
        assert reasons["missing"] >= 1
        assert reasons["chat_retryable"] >= 1
        assert reasons["expired"] >= 1

        todo2, _ = remint._collect_todo(
            accounts,
            auth_dir,
            include_missing=True,
            include_expired=True,
            include_chat_retryable=True,
            only_email="",
            limit=1,
            denied_emails=denied_set,
        )
        assert len(todo2) == 1
    print("PASS remint _collect_todo behavior")


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
        assert writer.is_entitlement_denied_auth({"import_gate": "entitlement_denied"}) is True
        assert writer.is_entitlement_denied_auth({"chat_ok": False, "fail_reason": "transient"}) is False
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

    # cpa_probe_chat_required=false no longer soft-passes free Build product ok
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
    assert out3["ok"] is False
    assert out3.get("skip_remote_inject") is True
    assert out3.get("chat_ok") is False
    assert "probe_chat_warning" not in out3

    # models-only miss never soft-passes, even when probe_chat/probe_required off
    r_models = {
        "ok": False,
        "path": "/tmp/x-models.json",
        "email": "m@e.com",
        "token_ok": True,
        "error": "token ok but grok-4.5 not listed",
        "fail_reason": "models_missing_grok_45",
        "chat_ok": False,
    }
    out_models = exp.finalize_probe_and_gate(
        r_models,
        {
            "cpa_probe_chat": False,
            "cpa_probe_required": False,
            "cpa_probe_chat_required": False,
        },
        email="m@e.com",
    )
    assert out_models["ok"] is False
    assert out_models.get("skip_remote_inject") is True
    assert out_models.get("chat_ok") is False
    assert out_models.get("fail_reason") == "models_missing_grok_45"
    assert "probe_warning" not in out_models

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


def test_writer_stamp_and_inventory() -> None:
    writer = _load("cpa_xai.writer_stamp", ROOT / "cpa_xai" / "writer.py")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p_ok = root / "xai-ok@example.com.json"
        p_ok.write_text(
            json.dumps({"type": "xai", "email": "ok@example.com", "access_token": "t"})
            + "\n",
            encoding="utf-8",
        )
        stamped = writer.stamp_auth_chat_fields(
            p_ok,
            {
                "chat_ok": True,
                "usable": True,
                "entitlement_denied": False,
                "chat_retryable": False,
            },
        )
        assert stamped.get("chat_ok") is True
        assert stamped.get("import_gate") == "chat_ok"
        assert stamped.get("entitlement_denied") is False

        p_den = root / "xai-den@example.com.json"
        p_den.write_text(
            json.dumps({"type": "xai", "email": "den@example.com"}) + "\n",
            encoding="utf-8",
        )
        writer.stamp_auth_chat_fields(
            p_den,
            {
                "chat_ok": False,
                "usable": False,
                "entitlement_denied": True,
                "fail_reason": "entitlement_denied",
                "chat_error_code": "permission_denied",
            },
        )
        den = json.loads(p_den.read_text(encoding="utf-8"))
        assert den["import_gate"] == "entitlement_denied"
        assert writer.is_entitlement_denied_auth(den) is True
        assert writer.is_chat_retryable_auth(den) is False

        p_miss = root / "xai-miss@example.com.json"
        p_miss.write_text(
            json.dumps({"type": "xai", "email": "miss@example.com"}) + "\n",
            encoding="utf-8",
        )
        inv = writer.inventory_chat_stamps(root)
        assert inv["total"] == 3
        assert inv["chat_ok_true"] == 1
        assert inv["chat_ok_false"] == 1
        assert inv["chat_ok_missing"] == 1
        assert inv["entitlement_denied"] >= 1

        stamp = writer.build_chat_stamp_from_result(
            {
                "chat_ok": False,
                "chat_retryable": True,
                "fail_reason": "transient",
                "usable": False,
            }
        )
        assert stamp["import_gate"] == "transient"
        assert stamp["chat_retryable"] is True
        assert stamp["chat_ok"] is False

        # incomplete result must NOT write chat_ok: null
        incomplete = writer.build_chat_stamp_from_result({"ok": True, "path": "/x"})
        assert "chat_ok" not in incomplete
        assert "usable" not in incomplete
        assert incomplete.get("import_gate") in ("ok", "not_ready")

        p_partial = root / "xai-partial@example.com.json"
        p_partial.write_text(
            json.dumps({"type": "xai", "email": "partial@example.com"}) + "\n",
            encoding="utf-8",
        )
        writer.stamp_auth_chat_fields(p_partial, {"ok": True})
        disk = json.loads(p_partial.read_text(encoding="utf-8"))
        assert "chat_ok" not in disk  # omit until real probe

        writer.stamp_auth_chat_fields(
            p_partial, None, updates={"chat_ok": None, "import_gate": "not_ready"}
        )
        disk2 = json.loads(p_partial.read_text(encoding="utf-8"))
        assert "chat_ok" not in disk2
        assert disk2.get("import_gate") == "not_ready"

        # mint_method / protocol_error are ops observability stamps (not product gates)
        stamp_mm = writer.build_chat_stamp_from_result(
            {
                "chat_ok": True,
                "usable": True,
                "mint_method": "browser",
                "protocol_error": "pkce: consent action id missing " + ("x" * 600),
            }
        )
        assert stamp_mm.get("mint_method") == "browser"
        assert stamp_mm.get("protocol_error")
        assert len(stamp_mm["protocol_error"]) <= 500
        p_mm = root / "xai-mm@example.com.json"
        p_mm.write_text(
            json.dumps({"type": "xai", "email": "mm@example.com", "access_token": "t"})
            + "\n",
            encoding="utf-8",
        )
        writer.stamp_auth_chat_fields(
            p_mm,
            {
                "chat_ok": False,
                "usable": False,
                "entitlement_denied": True,
                "fail_reason": "entitlement_denied",
                "mint_method": "browser",
                "protocol_error": "pkce failed: 404",
            },
        )
        disk_mm = json.loads(p_mm.read_text(encoding="utf-8"))
        assert disk_mm.get("mint_method") == "browser"
        assert "pkce failed" in str(disk_mm.get("protocol_error") or "")
        assert disk_mm.get("entitlement_denied") is True  # product gate still works
    print("PASS writer stamp + inventory")


def test_register_cli_summary_json_surface() -> None:
    """Batch end emits stable SUMMARY_JSON line; mint_method counters exist."""
    src = (ROOT / "register_cli.py").read_text(encoding="utf-8")
    assert "SUMMARY_JSON" in src
    assert '"event": "register_cli_summary"' in src or '"event":"register_cli_summary"' in src
    assert "mint_method_pkce" in src
    assert "mint_method_browser" in src
    assert "mint_method_protocol_device" in src
    assert "cpa_allow_device_flow_fallback" in src
    assert "product_ok" in src
    # mint path counters bumped on success
    assert '_inc("mint_method_pkce")' in src or "_inc('mint_method_pkce')" in src
    assert (
        '_inc("mint_method_protocol_device")' in src
        or "_inc('mint_method_protocol_device')" in src
    )
    print("PASS register_cli SUMMARY_JSON surface")


def test_mint_writes_mint_method_extra() -> None:
    """mint_and_export passes mint_method into build_cpa_xai_auth extra + stamp updates."""
    src = (ROOT / "cpa_xai" / "mint.py").read_text(encoding="utf-8")
    assert 'extra_auth: dict[str, Any] = {"mint_method": mint_method}' in src or (
        '"mint_method": mint_method' in src and "extra=extra_auth" in src
    )
    assert "extra=extra_auth" in src
    assert 'updates["mint_method"]' in src
    assert 'tokens["mint_method"] = "protocol_device"' in src
    assert "def _should_stamp_protocol_error" in src
    print("PASS mint mint_method disk path")


def test_backfill_chat_stamps_script_exists() -> None:
    src = (ROOT / "scripts" / "backfill_chat_stamps.py").read_text(encoding="utf-8")
    assert "inventory_chat_stamps" in src
    assert "stamp_auth_chat_fields" in src
    assert "record_entitlement_denied" in src
    assert "--probe" in src
    assert "--inventory-only" in src
    assert "--only-missing" in src
    assert "probe_chat_with_retries" in src
    assert "apply_chat_probe_to_result" in src
    print("PASS backfill_chat_stamps script surface")


def test_remote_inject_chat_ok_hard_gate() -> None:
    """Direct apply_multi_remote_inject / inject_cpa_auth_remote must refuse non-chat_ok."""
    exp = _load("cpa_export_inject_gate", ROOT / "cpa_export.py")

    calls: list[str] = []

    def fake_inject(path, config=None, log_callback=None):  # noqa: ANN001
        calls.append(str(path))
        return {"ok": True, "remote_path": f"/remote/{Path(path).name}"}

    cfg = {
        "cpa_remote_inject": True,
        "cpa_remote_auth_dirs": "/root/.cli-proxy-api,/personal/cpa/auths",
        "cpa_remote_inject_require_chat_ok": True,
        "cpa_probe_chat": True,
    }

    # result.ok but chat_ok missing/false → refuse, never call inject
    r_bad = {
        "ok": True,
        "path": "/tmp/xai-bad@example.com.json",
        "email": "bad@example.com",
        "chat_ok": False,
        "usable": False,
        "fail_reason": "usage_exhausted",
    }
    out_bad = exp.apply_multi_remote_inject(r_bad, cfg, inject_fn=fake_inject)
    assert out_bad.get("remote_inject_skipped") is True
    assert out_bad.get("remote_inject", {}).get("skipped") is True
    assert out_bad.get("import_gate") in ("usage_exhausted", "chat_not_ok")
    assert calls == []

    r_denied = {
        "ok": True,
        "path": "/tmp/xai-denied@example.com.json",
        "email": "denied@example.com",
        "chat_ok": False,
        "entitlement_denied": True,
        "usable": False,
    }
    out_denied = exp.apply_multi_remote_inject(r_denied, cfg, inject_fn=fake_inject)
    assert out_denied.get("remote_inject_skipped") is True
    assert out_denied.get("remote_inject_skip_reason") == "entitlement_denied"
    assert calls == []

    # chat_ok true → inject proceeds
    r_ok = {
        "ok": True,
        "path": "/tmp/xai-ok@example.com.json",
        "email": "ok@example.com",
        "chat_ok": True,
        "usable": True,
        "entitlement_denied": False,
        "import_gate": "chat_ok",
    }
    out_ok = exp.apply_multi_remote_inject(r_ok, cfg, inject_fn=fake_inject)
    assert out_ok.get("remote_inject_skipped") is not True
    assert len(calls) == 2  # live + inventory
    assert out_ok.get("import_gate") == "chat_ok"

    # File-stamp gate for direct inject_cpa_auth_remote
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "xai-stamp@example.com.json"
        p.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "email": "stamp@example.com",
                    "chat_ok": False,
                    "usable": False,
                    "import_gate": "usage_exhausted",
                    "fail_reason": "usage_exhausted",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        refused = exp.inject_cpa_auth_remote(
            p,
            config={"cpa_remote_inject": True, "cpa_remote_inject_require_chat_ok": True},
            log_callback=lambda _m: None,
        )
        assert refused.get("ok") is False
        assert refused.get("skipped") is True
        assert refused.get("reason") in ("usage_exhausted", "chat_not_ok")

        p2 = Path(td) / "xai-good@example.com.json"
        p2.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "email": "good@example.com",
                    "chat_ok": True,
                    "usable": True,
                    "import_gate": "chat_ok",
                    "entitlement_denied": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        # Gate allows; actual SSH may fail — only assert not skipped for chat reason.
        allowed_gate = exp.evaluate_remote_inject_gate({"path": str(p2)}, cfg, auth_path=p2)
        assert allowed_gate.get("allow") is True
        assert allowed_gate.get("import_gate") == "chat_ok"

        # require_chat_ok=false must refuse (chat_gate_disabled_refused), not soft-allow
        cfg_off = dict(cfg)
        cfg_off["cpa_remote_inject_require_chat_ok"] = False
        refused_gate = exp.evaluate_remote_inject_gate(
            {"path": str(p2), "chat_ok": True, "usable": True},
            cfg_off,
            auth_path=p2,
        )
        assert refused_gate.get("allow") is False
        assert refused_gate.get("reason") == "chat_gate_disabled_refused"
        assert refused_gate.get("import_gate") == "chat_gate_disabled_refused"

    print("PASS remote inject chat_ok hard gate")



def test_probe_transport_direct_uses_bearer_and_cli_headers() -> None:
    probe = _load("cpa_xai.probe_transport", ROOT / "cpa_xai" / "probe.py")
    assert hasattr(probe, "build_probe_transport")
    assert hasattr(probe, "probe_request_headers")
    t = probe.build_probe_transport(
        via="direct",
        upstream_base_url="https://cli-chat-proxy.grok.com/v1",
        cpa_base_url="https://cpa.example/v1",
        cpa_api_key="sk-cpa",
        access_token="tok-xai",
    )
    assert t["mode"] == "direct"
    assert t["base_url"].rstrip("/") == "https://cli-chat-proxy.grok.com/v1"
    h = probe.probe_request_headers(t, access_token="tok-xai")
    assert h["Authorization"] == "Bearer tok-xai"
    assert "x-grok-client-identifier" in h
    assert h.get("x-grok-client-identifier") == "grok-pager"
    print("PASS direct transport headers")


def test_probe_transport_cpa_uses_api_key_not_xai_token() -> None:
    probe = _load("cpa_xai.probe_transport2", ROOT / "cpa_xai" / "probe.py")
    t = probe.build_probe_transport(
        via="cpa",
        upstream_base_url="https://cli-chat-proxy.grok.com/v1",
        cpa_base_url="https://cpa.mangoq.ccwu.cc/v1",
        cpa_api_key="sk-cpa-key",
        access_token="tok-xai-should-not-be-auth",
        credential_pin="xai-user@example.com.json",
        pin_header="X-CPA-Credential",
    )
    assert t["mode"] == "cpa"
    assert t["base_url"].endswith("/v1") or "cpa.mangoq" in t["base_url"]
    h = probe.probe_request_headers(t, access_token="tok-xai-should-not-be-auth")
    assert h["Authorization"] == "Bearer sk-cpa-key"
    assert h.get("X-CPA-Credential") == "xai-user@example.com.json"
    assert "tok-xai" not in h["Authorization"]
    print("PASS cpa transport headers")


def test_build_probe_transport_rejects_unpinned_cpa_as_gate_mode() -> None:
    """Spec §6: unpinned cpa must not be used as entitlement gate without hybrid."""
    probe = _load("cpa_xai.probe_transport3", ROOT / "cpa_xai" / "probe.py")
    policy = probe.resolve_gate_probe_policy(
        via="cpa",
        cpa_base_url="https://cpa.example/v1",
        cpa_api_key="sk",
        credential_pin="",
        allow_unpinned_cpa_gate=False,
    )
    assert policy["gate_via"] == "direct"
    assert policy["cpa_smoke"] is True
    assert policy["reason"] in ("unpinned_cpa_hybrid", "hybrid")
    print("PASS unpinned cpa → hybrid policy")


def test_cpa_gateway_401_not_entitlement() -> None:
    probe = _load("cpa_xai.probe_cls_gw", ROOT / "cpa_xai" / "probe.py")
    out = {
        "ok": False,
        "status": 401,
        "error": "invalid api key",
        "transport_mode": "cpa",
        "error_code": "unauthorized",
    }
    cls = probe.classify_chat_probe(out)
    remapped = probe.remap_cpa_gateway_failure(out, cls)
    assert remapped["entitlement_denied"] is False
    assert remapped["reason"] in ("auth_or_protocol", "cpa_gateway_auth")
    print("PASS cpa gateway 401 not entitlement")


def test_config_example_documents_mid_tier_probe_keys() -> None:
    raw = (ROOT / "config.example.json").read_text(encoding="utf-8")
    assert "cpa_probe_via" in raw
    assert "cpa_probe_base_url" in raw
    assert "cpa_probe_api_key" in raw
    assert "cpa_probe_credential_pin_mode" in raw
    assert "cli-chat-proxy.grok.com" in raw
    print("PASS config.example mid-tier keys")


def test_mint_passes_transport_kwargs_signature() -> None:
    src = (ROOT / "cpa_xai" / "mint.py").read_text(encoding="utf-8")
    assert "probe_via" in src
    assert "build_probe_transport" in src or "resolve_gate_probe_policy" in src
    assert "probe_via_cpa_ok" in src or "cpa_smoke" in src
    assert "build_cpa_xai_auth" in src
    print("PASS mint transport kwargs signature")


def test_export_resolves_env_api_key() -> None:
    src = (ROOT / "cpa_export.py").read_text(encoding="utf-8")
    assert "cpa_probe_via" in src
    assert "CPA_PROBE_API_KEY" in src
    assert "resolve_gate_probe_policy" in src
    print("PASS export mid-tier resolve")


def test_pkce_non_retryable_residual_classifier() -> None:
    """Empty SPA / action-id extract failures are non-retryable residuals (source + unit)."""
    import ast

    from cpa_xai.pkce_mint import PKCEMintError

    src = (ROOT / "cpa_xai" / "mint.py").read_text(encoding="utf-8")
    assert "def _is_pkce_non_retryable" in src
    assert "mint best-effort residual → device flow" in src
    assert "allow_device_flow_fallback: bool = True" in src
    assert "empty consent SPA shell" in src or "best-effort" in src
    assert "protocol_device" in src
    assert "def _is_cancelled_error" in src
    assert "def _cancelled_result" in src
    assert "skip residual device/browser" in src or "skip residual" in src
    assert "skip browser residual" in src or "before browser residual" in src
    # Doc covers cancel short-circuit + broader non-retryable set.
    assert "short-circuit residual" in src or "must short-circuit residual" in src
    assert '"cancelled"' in src
    tree = ast.parse(src)
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_is_pkce_non_retryable"
    )
    # Inject PKCEMintError into exec ns so isinstance checks work when present.
    ns: dict = {"PKCEMintError": PKCEMintError}
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<mint_pkce_nr>", "exec"), ns)
    is_nr = ns["_is_pkce_non_retryable"]
    assert is_nr("consent HTML missing submitOAuth2Consent") is True
    assert is_nr("server action not found in HTML") is True
    assert is_nr("action id extract failed") is True
    assert is_nr("empty SPA residual") is True
    assert is_nr("connection reset by peer") is False
    assert is_nr("") is False
    assert is_nr(None) is False
    # Structured error preferred over message needles.
    assert is_nr(PKCEMintError("any", code="consent_action_missing", retryable=False)) is True
    assert is_nr(PKCEMintError("network blip", code="token_exchange", retryable=True)) is False
    # retryable=False short-circuits regardless of code.
    assert is_nr(PKCEMintError("x", code="token_exchange", retryable=False)) is True
    # cancelled is non-retryable (and residual short-circuits separately).
    assert is_nr(PKCEMintError("cancelled", code="cancelled", retryable=False)) is True
    print("PASS pkce non-retryable residual classifier")


def test_cancelled_helpers_and_fail_taxonomy_surface() -> None:
    """Cancel helpers + residual fail mint_method taxonomy (unit + source)."""
    from cpa_xai import mint as mint_mod
    from cpa_xai.pkce_mint import PKCEMintError

    assert mint_mod._is_cancelled_error(
        PKCEMintError("cancelled by operator", code="cancelled", retryable=False)
    )
    assert mint_mod._is_cancelled_error("cancelled")
    assert mint_mod._is_cancelled_error("cancelled by user")
    assert not mint_mod._is_cancelled_error(
        PKCEMintError("consent HTML missing", code="consent_action_missing", retryable=False)
    )
    assert not mint_mod._is_cancelled_error("connection reset")

    cr = mint_mod._cancelled_result(
        "a@ex.com",
        mint_method="pkce",
        protocol_err="cancelled",
        pkce_error_code="cancelled",
    )
    assert cr["ok"] is False
    assert cr["error"] == "cancelled"
    assert cr["mint_method"] == "pkce"
    assert cr["pkce_retryable"] is False
    assert cr["pkce_error_code"] == "cancelled"
    assert cr["protocol_error"] == "cancelled"
    assert mint_mod._should_stamp_protocol_error("protocol_device") is True
    assert mint_mod._should_stamp_protocol_error("browser") is True
    assert mint_mod._should_stamp_protocol_error("pkce") is False
    assert mint_mod._should_stamp_protocol_error("protocol") is False
    print("PASS cancelled helpers + fail taxonomy surface")


def test_mint_pkce_fail_device_residual_e2e() -> None:
    """Mocked integration: PKCE non-retryable → device residual → protocol_device dual stamp."""
    from cpa_xai import mint as mint_mod
    from cpa_xai.pkce_mint import PKCEMintError

    calls = {"pkce": 0, "device": 0, "browser": 0, "models": 0, "chat": 0}
    orig = {
        "pkce": mint_mod.mint_with_sso_pkce,
        "device": mint_mod.mint_with_sso_protocol,
        "browser": mint_mod.mint_with_browser,
        "models": mint_mod.probe_models,
        "chat": mint_mod.probe_chat_with_retries,
    }

    def fake_pkce(**_kw):  # noqa: ANN001
        calls["pkce"] += 1
        raise PKCEMintError(
            "consent HTML missing submitOAuth2Consent action id",
            code="consent_action_missing",
            retryable=False,
        )

    def fake_device(**_kw):  # noqa: ANN001
        calls["device"] += 1
        return {
            "access_token": "at-device-residual",
            "refresh_token": "rt-device-residual",
            "id_token": "id-device",
            "expires_in": 3600,
            "mint_method": "protocol",  # overridden by residual taxonomy
        }

    def fake_browser(**_kw):  # noqa: ANN001
        calls["browser"] += 1
        raise AssertionError("browser must not run when device residual succeeds")

    def fake_models(_token, **_kw):  # noqa: ANN001
        calls["models"] += 1
        return {
            "ok": True,
            "status": 200,
            "has_grok_45": True,
            "model_ids": ["grok-4.5"],
            "transport_mode": "direct",
        }

    def fake_chat(_token, **_kw):  # noqa: ANN001
        calls["chat"] += 1
        # Keep residual labeling independent of entitlement Manual-required.
        from cpa_xai.probe import classify_chat_probe

        out = {
            "ok": True,
            "status": 200,
            "text": "MINT_OK",
            "model": "grok-4.5",
            "error_code": "",
        }
        out.update(classify_chat_probe(out))
        return out

    mint_mod.mint_with_sso_pkce = fake_pkce  # type: ignore[assignment]
    mint_mod.mint_with_sso_protocol = fake_device  # type: ignore[assignment]
    mint_mod.mint_with_browser = fake_browser  # type: ignore[assignment]
    mint_mod.probe_models = fake_models  # type: ignore[assignment]
    mint_mod.probe_chat_with_retries = fake_chat  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            result = mint_mod.mint_and_export(
                email="residual@ex.com",
                password="",
                auth_dir=td,
                sso="sso-token-residual",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=True,
                protocol_only=False,
                probe=True,
                probe_chat=True,
                probe_via="direct",
                log=lambda _m: None,
            )
            assert result.get("ok") is True, result
            assert result.get("mint_method") == "protocol_device", result
            assert "consent" in str(result.get("protocol_error") or "").lower(), result
            assert calls["pkce"] == 1
            assert calls["device"] == 1
            assert calls["browser"] == 0
            assert calls["models"] >= 1
            path = Path(str(result["path"]))
            disk = json.loads(path.read_text(encoding="utf-8"))
            assert disk.get("mint_method") == "protocol_device", disk
            assert "consent" in str(disk.get("protocol_error") or "").lower(), disk
            # Dual stamp: probe fields present without dropping residual labels.
            assert "chat_ok" in disk
            assert disk.get("mint_method") == "protocol_device"
    finally:
        mint_mod.mint_with_sso_pkce = orig["pkce"]  # type: ignore[assignment]
        mint_mod.mint_with_sso_protocol = orig["device"]  # type: ignore[assignment]
        mint_mod.mint_with_browser = orig["browser"]  # type: ignore[assignment]
        mint_mod.probe_models = orig["models"]  # type: ignore[assignment]
        mint_mod.probe_chat_with_retries = orig["chat"]  # type: ignore[assignment]
    print("PASS mint pkce→device residual e2e protocol_device dual stamp")


def test_mint_cancel_short_circuits_residual() -> None:
    """Cancelled PKCE / cancel callback must not spend device or browser residual."""
    from cpa_xai import mint as mint_mod
    from cpa_xai.pkce_mint import PKCEMintError
    from cpa_xai.protocol_mint import ProtocolMintError

    calls = {"pkce": 0, "device": 0, "browser": 0}
    orig = {
        "pkce": mint_mod.mint_with_sso_pkce,
        "device": mint_mod.mint_with_sso_protocol,
        "browser": mint_mod.mint_with_browser,
    }

    def fake_pkce_cancelled(**_kw):  # noqa: ANN001
        calls["pkce"] += 1
        raise PKCEMintError("cancelled", code="cancelled", retryable=False)

    def fake_device(**_kw):  # noqa: ANN001
        calls["device"] += 1
        raise AssertionError("device residual must not run after cancel")

    def fake_browser(**_kw):  # noqa: ANN001
        calls["browser"] += 1
        raise AssertionError("browser residual must not run after cancel")

    mint_mod.mint_with_sso_pkce = fake_pkce_cancelled  # type: ignore[assignment]
    mint_mod.mint_with_sso_protocol = fake_device  # type: ignore[assignment]
    mint_mod.mint_with_browser = fake_browser  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            r1 = mint_mod.mint_and_export(
                email="cancel1@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-cancel",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=True,
                probe=False,
                probe_chat=False,
                log=lambda _m: None,
            )
            assert r1.get("ok") is False
            assert r1.get("error") == "cancelled"
            assert r1.get("mint_method") == "pkce"
            assert r1.get("pkce_retryable") is False
            assert calls["pkce"] == 1
            assert calls["device"] == 0
            assert calls["browser"] == 0

            # Cancel callback before residual even when PKCE is retryable-looking.
            def fake_pkce_retryable(**_kw):  # noqa: ANN001
                calls["pkce"] += 1
                raise PKCEMintError("token exchange timeout", code="token_exchange", retryable=True)

            mint_mod.mint_with_sso_pkce = fake_pkce_retryable  # type: ignore[assignment]
            r2 = mint_mod.mint_and_export(
                email="cancel2@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-cancel2",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=True,
                probe=False,
                probe_chat=False,
                cancel=lambda: True,
                log=lambda _m: None,
            )
            assert r2.get("ok") is False
            assert r2.get("error") == "cancelled"
            assert r2.get("mint_method") == "pkce"
            assert calls["device"] == 0
            assert calls["browser"] == 0

            # Device residual cancelled → no browser; fail mint_method protocol_device.
            def fake_pkce_consent(**_kw):  # noqa: ANN001
                calls["pkce"] += 1
                raise PKCEMintError(
                    "consent HTML missing",
                    code="consent_action_missing",
                    retryable=False,
                )

            def fake_device_cancel(**_kw):  # noqa: ANN001
                calls["device"] += 1
                raise ProtocolMintError("cancelled")

            mint_mod.mint_with_sso_pkce = fake_pkce_consent  # type: ignore[assignment]
            mint_mod.mint_with_sso_protocol = fake_device_cancel  # type: ignore[assignment]
            r3 = mint_mod.mint_and_export(
                email="cancel3@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-cancel3",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=True,
                protocol_only=False,
                probe=False,
                probe_chat=False,
                log=lambda _m: None,
            )
            assert r3.get("ok") is False
            assert r3.get("error") == "cancelled"
            assert r3.get("mint_method") == "protocol_device", r3
            assert calls["browser"] == 0
    finally:
        mint_mod.mint_with_sso_pkce = orig["pkce"]  # type: ignore[assignment]
        mint_mod.mint_with_sso_protocol = orig["device"]  # type: ignore[assignment]
        mint_mod.mint_with_browser = orig["browser"]  # type: ignore[assignment]
    print("PASS mint cancel short-circuits residual")


def test_mint_protocol_only_fail_labels_protocol_device() -> None:
    """protocol_only after PKCE+device fail uses residual fail taxonomy mint_method."""
    from cpa_xai import mint as mint_mod
    from cpa_xai.pkce_mint import PKCEMintError
    from cpa_xai.protocol_mint import ProtocolMintError

    orig = {
        "pkce": mint_mod.mint_with_sso_pkce,
        "device": mint_mod.mint_with_sso_protocol,
        "browser": mint_mod.mint_with_browser,
    }

    def fake_pkce(**_kw):  # noqa: ANN001
        raise PKCEMintError(
            "consent HTML missing",
            code="consent_action_missing",
            retryable=False,
        )

    def fake_device(**_kw):  # noqa: ANN001
        raise ProtocolMintError("device poll timeout")

    def fake_browser(**_kw):  # noqa: ANN001
        raise AssertionError("browser must not run under protocol_only")

    mint_mod.mint_with_sso_pkce = fake_pkce  # type: ignore[assignment]
    mint_mod.mint_with_sso_protocol = fake_device  # type: ignore[assignment]
    mint_mod.mint_with_browser = fake_browser  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            r = mint_mod.mint_and_export(
                email="po@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-po",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=True,
                protocol_only=True,
                probe=False,
                probe_chat=False,
                log=lambda _m: None,
            )
            assert r.get("ok") is False
            assert r.get("mint_method") == "protocol_device", r
            assert "protocol_error" in r
            assert "device" in str(r.get("error") or "").lower() or "device" in str(
                r.get("protocol_error") or ""
            ).lower()

            # Primary device path (no PKCE attempt) stays mint_method=protocol on fail.
            mint_mod.mint_with_sso_protocol = fake_device  # type: ignore[assignment]
            r2 = mint_mod.mint_and_export(
                email="po2@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-po2",
                prefer_protocol=True,
                protocol_flow="device",
                allow_device_flow_fallback=True,
                protocol_only=True,
                probe=False,
                probe_chat=False,
                log=lambda _m: None,
            )
            assert r2.get("ok") is False
            assert r2.get("mint_method") == "protocol", r2
    finally:
        mint_mod.mint_with_sso_pkce = orig["pkce"]  # type: ignore[assignment]
        mint_mod.mint_with_sso_protocol = orig["device"]  # type: ignore[assignment]
        mint_mod.mint_with_browser = orig["browser"]  # type: ignore[assignment]
    print("PASS mint protocol_only fail labels protocol_device")


def test_inject_ignores_probe_via_cpa_ok_without_chat_ok() -> None:
    """Observational CPA smoke alone must never unlock remote inject."""
    exp = _load("cpa_export_midtier_inject", ROOT / "cpa_export.py")
    cfg = {
        "cpa_remote_inject": True,
        "cpa_remote_inject_require_chat_ok": True,
    }
    denied = exp.evaluate_remote_inject_gate(
        {
            "chat_ok": False,
            "usable": False,
            "entitlement_denied": False,
            "probe_via_cpa_ok": True,
            "fail_reason": "usage_exhausted",
            "import_gate": "usage_exhausted",
        },
        cfg,
    )
    assert denied.get("allow") is False
    assert denied.get("reason") != "chat_ok"
    print("PASS inject ignores probe_via_cpa_ok without chat_ok")


def test_mint_token_ok_honesty() -> None:
    """token_ok after write; product ok only after probes (or probes off)."""
    from cpa_xai import mint as mint_mod
    from cpa_xai.probe import classify_chat_probe

    orig = {
        "pkce": mint_mod.mint_with_sso_pkce,
        "models": mint_mod.probe_models,
        "chat": mint_mod.probe_chat_with_retries,
    }

    def fake_pkce(**_kw):  # noqa: ANN001
        return {
            "access_token": "at-honesty",
            "refresh_token": "rt-honesty",
            "id_token": "id-honesty",
            "expires_in": 3600,
            "mint_method": "pkce",
        }

    def fake_models_ok(_token, **_kw):  # noqa: ANN001
        return {
            "ok": True,
            "status": 200,
            "has_grok_45": True,
            "model_ids": ["grok-4.5"],
            "transport_mode": "direct",
        }

    def fake_models_miss(_token, **_kw):  # noqa: ANN001
        return {
            "ok": True,
            "status": 200,
            "has_grok_45": False,
            "model_ids": ["grok-3"],
            "transport_mode": "direct",
        }

    def fake_chat_ok(_token, **_kw):  # noqa: ANN001
        out = {
            "ok": True,
            "status": 200,
            "text": "MINT_OK",
            "model": "grok-4.5",
            "error_code": "",
        }
        out.update(classify_chat_probe(out))
        return out

    mint_mod.mint_with_sso_pkce = fake_pkce  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            # probes off: token write alone → product ok + token_ok
            mint_mod.probe_models = fake_models_ok  # type: ignore[assignment]
            mint_mod.probe_chat_with_retries = fake_chat_ok  # type: ignore[assignment]
            r_off = mint_mod.mint_and_export(
                email="off@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-off",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=False,
                probe=False,
                probe_chat=False,
                log=lambda _m: None,
            )
            assert r_off.get("token_ok") is True, r_off
            assert r_off.get("ok") is True, r_off
            assert r_off.get("path")

            # models miss, chat off → token_ok True, product ok False
            mint_mod.probe_models = fake_models_miss  # type: ignore[assignment]
            r_miss = mint_mod.mint_and_export(
                email="miss@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-miss",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=False,
                probe=True,
                probe_chat=False,
                log=lambda _m: None,
            )
            assert r_miss.get("token_ok") is True, r_miss
            assert r_miss.get("ok") is False, r_miss
            assert "grok-4.5 not listed" in str(r_miss.get("error") or "")

            # chat ok path → token_ok + product ok
            mint_mod.probe_models = fake_models_ok  # type: ignore[assignment]
            mint_mod.probe_chat_with_retries = fake_chat_ok  # type: ignore[assignment]
            r_ok = mint_mod.mint_and_export(
                email="ok@ex.com",
                password="pw",
                auth_dir=td,
                sso="sso-ok",
                prefer_protocol=True,
                protocol_flow="pkce",
                allow_device_flow_fallback=False,
                probe=True,
                probe_chat=True,
                probe_via="direct",
                log=lambda _m: None,
            )
            assert r_ok.get("token_ok") is True, r_ok
            assert r_ok.get("ok") is True, r_ok
            assert r_ok.get("chat_ok") is True, r_ok
    finally:
        mint_mod.mint_with_sso_pkce = orig["pkce"]  # type: ignore[assignment]
        mint_mod.probe_models = orig["models"]  # type: ignore[assignment]
        mint_mod.probe_chat_with_retries = orig["chat"]  # type: ignore[assignment]
    print("PASS mint token_ok honesty")


def main() -> int:
    test_classify_chat_probe()
    test_probe_mini_response_attaches_classification()
    test_probe_chat_with_retries_and_apply()
    test_mint_default_probe_chat_on()
    test_export_finalize_and_defaults()
    test_config_example_chat_keys()
    test_cli_chat_stats()
    test_remint_skips_denied_and_retryable()
    test_remint_collect_todo_behavior()
    test_writer_ledger_roundtrip()
    test_writer_stamp_and_inventory()
    test_register_cli_summary_json_surface()
    test_mint_writes_mint_method_extra()
    test_backfill_chat_stamps_script_exists()
    test_finalize_probe_and_gate_behavior()
    test_remote_inject_chat_ok_hard_gate()
    test_probe_transport_direct_uses_bearer_and_cli_headers()
    test_probe_transport_cpa_uses_api_key_not_xai_token()
    test_build_probe_transport_rejects_unpinned_cpa_as_gate_mode()
    test_cpa_gateway_401_not_entitlement()
    test_config_example_documents_mid_tier_probe_keys()
    test_mint_passes_transport_kwargs_signature()
    test_export_resolves_env_api_key()
    test_pkce_non_retryable_residual_classifier()
    test_cancelled_helpers_and_fail_taxonomy_surface()
    test_mint_pkce_fail_device_residual_e2e()
    test_mint_cancel_short_circuits_residual()
    test_mint_protocol_only_fail_labels_protocol_device()
    test_inject_ignores_probe_via_cpa_ok_without_chat_ok()
    test_mint_token_ok_honesty()
    print("\nALL PASS (cpa chat entitlement gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
