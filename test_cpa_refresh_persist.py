#!/usr/bin/env python3
"""Unit checks: OAuth refresh rotation must persist; import is access-first."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

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


def _jwt(exp: int, *, sub: str = "sub-1", iat: int | None = None) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "exp": exp,
                "iat": iat if iat is not None else exp - 21600,
                "sub": sub,
            }
        ).encode()
    ).decode().rstrip("=")
    return f"aaa.{payload}.bbb"


def test_refresh_auth_file_persists_rotated_refresh() -> None:
    refresh = _load("cpa_xai.refresh", ROOT / "cpa_xai" / "refresh.py")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "xai-a@b.com.json"
        p.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "email": "a@b.com",
                    "refresh_token": "old-refresh",
                    "access_token": "old-access",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                    "base_url": "https://cli-chat-proxy.grok.com/v1",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        fake_access = _jwt(2_000_000_000)

        def fake_refresh_xai_tokens(**kwargs):  # noqa: ANN003
            assert kwargs["refresh_token"] == "old-refresh"
            return {
                "ok": True,
                "status": 200,
                "access_token": fake_access,
                "refresh_token": "new-refresh-rotated",
                "refresh_rotated": True,
                "id_token": "",
                "expires_in": 21600,
                "token_type": "Bearer",
            }

        with patch.object(refresh, "refresh_xai_tokens", side_effect=fake_refresh_xai_tokens):
            out = refresh.refresh_auth_file(p, persist=True)
        assert out["ok"] is True, out
        assert out["persisted"] is True
        assert out["refresh_rotated"] is True
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["refresh_token"] == "new-refresh-rotated"
        assert data["access_token"] == fake_access
        assert data.get("expired")
        # pre-rotate snapshot + rotation ledger
        snap_dir = p.parent / ".rt_prerotate"
        assert snap_dir.is_dir()
        assert list(snap_dir.glob("*.json"))
        ledger = p.parent / "rt_rotation.jsonl"
        assert ledger.is_file()
        line = ledger.read_text(encoding="utf-8").strip().splitlines()[-1]
        rec = json.loads(line)
        assert rec["old_rt"] == "old-refresh"
        assert rec["new_rt"] == "new-refresh-rotated"
    print("PASS refresh_auth_file persists rotated refresh + ledger")


def test_refresh_auth_file_fail_closed_on_persist_verify() -> None:
    refresh = _load("cpa_xai.refresh", ROOT / "cpa_xai" / "refresh.py")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "xai-b@c.com.json"
        p.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "email": "b@c.com",
                    "refresh_token": "rt-old",
                    "access_token": "at-old",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                    "base_url": "https://cli-chat-proxy.grok.com/v1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        fake_access = _jwt(2_000_000_000)

        def fake_refresh_xai_tokens(**kwargs):  # noqa: ANN003
            return {
                "ok": True,
                "status": 200,
                "access_token": fake_access,
                "refresh_token": "rt-new",
                "refresh_rotated": True,
                "id_token": "",
                "expires_in": 21600,
                "token_type": "Bearer",
            }

        def broken_patch(path, updates):  # noqa: ANN001
            # Write wrong RT to simulate silent corruption / race.
            body = json.loads(Path(path).read_text(encoding="utf-8"))
            body.update(updates or {})
            body["refresh_token"] = "CORRUPTED"
            Path(path).write_text(json.dumps(body) + "\n", encoding="utf-8")
            return body

        def broken_write(auth_dir, payload, filename=None):  # noqa: ANN001
            dest = Path(auth_dir) / (filename or "xai-x.json")
            payload = dict(payload)
            payload["refresh_token"] = "CORRUPTED"
            dest.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return dest

        with (
            patch.object(refresh, "refresh_xai_tokens", side_effect=fake_refresh_xai_tokens),
            patch.object(refresh, "patch_cpa_xai_auth", side_effect=broken_patch),
            patch.object(refresh, "write_cpa_xai_auth", side_effect=broken_write),
        ):
            out = refresh.refresh_auth_file(p, persist=True)
        assert out["ok"] is False, out
        assert out.get("error_code") in {
            "rt_persist_verify_failed",
            "rt_persist_failed",
        }
        assert out.get("fatal_rt_loss_risk") is True
    print("PASS refresh_auth_file fail-closed on persist verify")


def test_refresh_persist_false_force_saves_rotated_rt() -> None:
    """persist=False + rotate must not silently discard new RT."""
    refresh = _load("cpa_xai.refresh", ROOT / "cpa_xai" / "refresh.py")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "xai-c@d.com.json"
        p.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "email": "c@d.com",
                    "refresh_token": "rt-old2",
                    "access_token": "at-old2",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                    "base_url": "https://cli-chat-proxy.grok.com/v1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        fake_access = _jwt(2_000_000_000)

        def fake_refresh_xai_tokens(**kwargs):  # noqa: ANN003
            return {
                "ok": True,
                "status": 200,
                "access_token": fake_access,
                "refresh_token": "rt-new2",
                "refresh_rotated": True,
                "id_token": "",
                "expires_in": 21600,
                "token_type": "Bearer",
            }

        with patch.object(refresh, "refresh_xai_tokens", side_effect=fake_refresh_xai_tokens):
            out = refresh.refresh_auth_file(p, persist=False)
        # Forced persist should rescue the new RT.
        assert out["ok"] is True, out
        assert out.get("persist_forced") is True
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["refresh_token"] == "rt-new2"
    print("PASS persist=False still force-saves rotated RT")


def test_ensure_auth_tokens_access_first_no_network() -> None:
    refresh = _load("cpa_xai.refresh", ROOT / "cpa_xai" / "refresh.py")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "xai-e@f.com.json"
        # access still valid for ~1 day
        at = _jwt(int(time.time()) + 86400)
        p.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "email": "e@f.com",
                    "refresh_token": "rt-keep",
                    "access_token": at,
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                    "base_url": "https://cli-chat-proxy.grok.com/v1",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        def boom(**kwargs):  # noqa: ANN003
            raise AssertionError("refresh_xai_tokens must not be called for valid access")

        with patch.object(refresh, "refresh_xai_tokens", side_effect=boom):
            out = refresh.ensure_auth_tokens(p, force_refresh=False)
        assert out["ok"] is True, out
        assert out["source"] == "access_reuse"
        assert out["refresh_token"] == "rt-keep"
        assert out["refresh_rotated"] is False
        assert out["persisted"] is False
        # disk untouched
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["refresh_token"] == "rt-keep"
        assert data["access_token"] == at
    print("PASS ensure_auth_tokens access-first skips network")


def test_ensure_auth_tokens_refreshes_when_access_expired() -> None:
    refresh = _load("cpa_xai.refresh", ROOT / "cpa_xai" / "refresh.py")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "xai-g@h.com.json"
        at = _jwt(int(time.time()) - 60)  # already expired
        p.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "email": "g@h.com",
                    "refresh_token": "rt-old3",
                    "access_token": at,
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                    "base_url": "https://cli-chat-proxy.grok.com/v1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        new_at = _jwt(int(time.time()) + 86400)

        def fake_refresh_xai_tokens(**kwargs):  # noqa: ANN003
            assert kwargs["refresh_token"] == "rt-old3"
            return {
                "ok": True,
                "status": 200,
                "access_token": new_at,
                "refresh_token": "rt-new3",
                "refresh_rotated": True,
                "id_token": "",
                "expires_in": 21600,
                "token_type": "Bearer",
            }

        with patch.object(refresh, "refresh_xai_tokens", side_effect=fake_refresh_xai_tokens):
            out = refresh.ensure_auth_tokens(p, force_refresh=False)
        assert out["ok"] is True, out
        assert out.get("source") == "refresh"
        assert out["refresh_rotated"] is True
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["refresh_token"] == "rt-new3"
        assert data["access_token"] == new_at
    print("PASS ensure_auth_tokens refreshes when access expired")


def test_import_script_access_first_markers() -> None:
    src = (ROOT / "scripts" / "import_cpa_auth_dir.py").read_text(encoding="utf-8")
    assert "ensure_auth_tokens" in src
    assert "force_refresh" in src
    assert "evaluate_remote_inject_gate" in src
    assert "apply_multi_remote_inject" in src
    # Call order inside import_one: ensure_auth_tokens before probe_models/chat.
    fn_start = src.index("def import_one")
    fn_body = src[fn_start : src.index("\ndef main")]
    assert fn_body.index("ensure_auth_tokens(") < fn_body.index("probe_models(")
    assert fn_body.index("probe_models(") < fn_body.index("probe_mini_response(")
    # Must NOT unconditionally call refresh_auth_file at start of import_one.
    # (refresh only via ensure_auth_tokens when needed)
    assert "refresh_auth_file(" not in fn_body
    print("PASS import script access-first markers")


def test_access_token_usable_skew() -> None:
    refresh = _load("cpa_xai.refresh", ROOT / "cpa_xai" / "refresh.py")
    now = time.time()
    assert refresh.access_token_usable(_jwt(int(now) + 500), now=now) is True
    assert refresh.access_token_usable(_jwt(int(now) + 30), skew_seconds=120, now=now) is False
    assert refresh.access_token_usable("", now=now) is False
    print("PASS access_token_usable skew")


def main() -> int:
    test_access_token_usable_skew()
    test_refresh_auth_file_persists_rotated_refresh()
    test_refresh_auth_file_fail_closed_on_persist_verify()
    test_refresh_persist_false_force_saves_rotated_rt()
    test_ensure_auth_tokens_access_first_no_network()
    test_ensure_auth_tokens_refreshes_when_access_expired()
    test_import_script_access_first_markers()
    print("\nALL PASS (cpa refresh persist + access-first)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
