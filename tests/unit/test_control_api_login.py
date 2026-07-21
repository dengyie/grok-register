"""Password login + session cookie tests for control API."""

from __future__ import annotations


def _client(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_SESSION_SECRET", "test-session-secret-32bytes-min!!")
    monkeypatch.setenv("CONTROL_API_PASSWORD_LOGIN", "1")
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    from apps.control_api.settings import clear_settings_cache
    from apps.control_api.app import create_app
    from fastapi.testclient import TestClient

    clear_settings_cache()
    return TestClient(create_app())


def test_password_hash_roundtrip():
    from apps.control_api.passwords import hash_password, verify_password

    h = hash_password("correct horse battery")
    assert h.startswith("scrypt$")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong", h)


def test_login_sets_cookie_and_opens_overview(tmp_path, monkeypatch):
    from apps.control_api.users import upsert_user

    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("CONTROL_API_TOKEN", raising=False)
    upsert_user(tmp_path, "admin", "password123")
    client = _client(tmp_path, monkeypatch, CONTROL_API_TOKEN=None)

    r = client.get("/api/overview")
    assert r.status_code == 401

    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert r.status_code == 401

    r = client.post("/api/auth/login", json={"username": "admin", "password": "password123"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "control_session" in r.cookies

    r = client.get("/api/overview")
    assert r.status_code == 200
    assert "product_ok" in r.json()

    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["authenticated"] is True
    assert r.json()["username"] == "admin"

    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    r = client.get("/api/overview")
    assert r.status_code == 401


def test_bearer_still_works_with_password_users(tmp_path, monkeypatch):
    from apps.control_api.users import upsert_user

    (tmp_path / "cpa_auths").mkdir()
    upsert_user(tmp_path, "admin", "password123")
    client = _client(tmp_path, monkeypatch, CONTROL_API_TOKEN="secret-token")

    r = client.get("/api/overview")
    assert r.status_code == 401
    r = client.get("/api/overview", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200


def test_bootstrap_creates_first_user(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_SESSION_SECRET", "test-session-secret-32bytes-min!!")
    monkeypatch.setenv("CONTROL_API_BOOTSTRAP_USER", "ops")
    monkeypatch.setenv("CONTROL_API_BOOTSTRAP_PASSWORD", "bootstrap-pass-9")
    monkeypatch.delenv("CONTROL_API_TOKEN", raising=False)
    from apps.control_api.settings import clear_settings_cache
    from apps.control_api.app import create_app
    from apps.control_api.users import has_any_user
    from fastapi.testclient import TestClient

    clear_settings_cache()
    with TestClient(create_app()) as client:
        assert has_any_user(tmp_path)
        r = client.post(
            "/api/auth/login",
            json={"username": "ops", "password": "bootstrap-pass-9"},
        )
        assert r.status_code == 200
        r = client.get("/api/overview")
        assert r.status_code == 200


def test_login_rate_limit(tmp_path, monkeypatch):
    from apps.control_api.users import upsert_user
    from apps.control_api.rate_limit import login_limiter

    upsert_user(tmp_path, "admin", "password123")
    # reset limiter state between tests
    login_limiter._failures.clear()
    client = _client(tmp_path, monkeypatch, CONTROL_API_TOKEN=None)
    for _ in range(8):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
        assert r.status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 429
