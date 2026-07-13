#!/usr/bin/env python3
"""Unit checks: OAuth refresh rotation must persist to disk immediately."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
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

        # minimal JWT-like access with exp/iat/sub (base64url payload)
        import base64

        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": 2000000000, "iat": 1999990000, "sub": "sub-1"}).encode()
        ).decode().rstrip("=")
        fake_access = f"aaa.{payload}.bbb"

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
        assert out["ok"] is True
        assert out["persisted"] is True
        assert out["refresh_rotated"] is True
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["refresh_token"] == "new-refresh-rotated"
        assert data["access_token"] == fake_access
        assert data.get("expired")
    print("PASS refresh_auth_file persists rotated refresh")


def test_import_script_refresh_before_probe_markers() -> None:
    src = (ROOT / "scripts" / "import_cpa_auth_dir.py").read_text(encoding="utf-8")
    assert "refresh_auth_file" in src
    assert "persist=True" in src
    assert "evaluate_remote_inject_gate" in src
    assert "apply_multi_remote_inject" in src
    # Call order inside import_one: refresh_auth_file(...) before probe_models/chat.
    fn_start = src.index("def import_one")
    fn_body = src[fn_start : src.index("\ndef main")]
    assert fn_body.index("refresh_auth_file(") < fn_body.index("probe_models(")
    assert fn_body.index("probe_models(") < fn_body.index("probe_mini_response(")
    print("PASS import script refresh-before-probe markers")


def main() -> int:
    test_refresh_auth_file_persists_rotated_refresh()
    test_import_script_refresh_before_probe_markers()
    print("\nALL PASS (cpa refresh persist)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
