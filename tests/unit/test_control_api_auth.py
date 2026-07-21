"""Auth and settings tests for control API."""

from __future__ import annotations


def test_settings_reads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_TOKEN", "secret-token")
    monkeypatch.setenv("CONTROL_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CONTROL_API_PORT", "8787")
    from apps.control_api.settings import clear_settings_cache, get_settings

    clear_settings_cache()
    s = get_settings()
    assert s.project_root == tmp_path.resolve()
    assert s.token == "secret-token"
    assert s.port == 8787


def test_health_ok_without_token_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("CONTROL_API_TOKEN", raising=False)
    monkeypatch.setenv("CONTROL_API_PASSWORD_LOGIN", "0")
    from apps.control_api.settings import clear_settings_cache
    from apps.control_api.app import create_app
    from fastapi.testclient import TestClient

    clear_settings_cache()
    client = TestClient(create_app())
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["auth_required"] is False


def test_protected_route_401_without_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_TOKEN", "secret-token")
    from apps.control_api.settings import clear_settings_cache
    from apps.control_api.app import create_app
    from fastapi.testclient import TestClient

    clear_settings_cache()
    client = TestClient(create_app())
    r = client.get("/api/overview")
    assert r.status_code == 401


def test_protected_route_ok_with_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_TOKEN", "secret-token")
    (tmp_path / "cpa_auths").mkdir()
    from apps.control_api.settings import clear_settings_cache
    from apps.control_api.app import create_app
    from fastapi.testclient import TestClient

    clear_settings_cache()
    client = TestClient(create_app())
    r = client.get("/api/overview", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["product_ok"] == 0
    assert "project_root" in body
