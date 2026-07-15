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
    print("PASS writer stamp + inventory")


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
    test_inject_ignores_probe_via_cpa_ok_without_chat_ok()
    print("\nALL PASS (cpa chat entitlement gate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
