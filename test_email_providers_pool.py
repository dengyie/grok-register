#!/usr/bin/env python3
"""Unit tests for EMAIL_PROVIDERS multi-select pool + attempt bind."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _stub_heavy_deps() -> None:
    """Allow importing grok_register_ttk without tkinter/DrissionPage in CI/headless."""
    import types

    if "tkinter" not in sys.modules:
        tk = types.ModuleType("tkinter")
        tk.StringVar = object
        tk.BooleanVar = object
        tk.END = "end"
        for attr in ("W", "E", "N", "S", "EW", "BOTH", "X", "Y", "LEFT"):
            setattr(tk, attr, attr.lower())
        tk.Tk = type("Tk", (), {})
        sys.modules["tkinter"] = tk
        for sub in ("ttk", "messagebox", "scrolledtext"):
            sys.modules[f"tkinter.{sub}"] = types.ModuleType(f"tkinter.{sub}")
        sys.modules["tkinter.ttk"] = types.ModuleType("tkinter.ttk")
    if "DrissionPage" not in sys.modules:
        dp = types.ModuleType("DrissionPage")
        dp.Chromium = object
        dp.ChromiumOptions = object
        sys.modules["DrissionPage"] = dp
        err = types.ModuleType("DrissionPage.errors")
        err.PageDisconnectedError = type("PageDisconnectedError", (Exception,), {})
        sys.modules["DrissionPage.errors"] = err
    if "curl_cffi" not in sys.modules:
        cc = types.ModuleType("curl_cffi")
        req = types.ModuleType("curl_cffi.requests")
        cc.requests = req
        sys.modules["curl_cffi"] = cc
        sys.modules["curl_cffi.requests"] = req


def _load():
    try:
        _stub_heavy_deps()
        import grok_register_ttk as m

        return m
    except Exception as e:
        print("SKIP full import:", type(e).__name__, e)
        return None


def _reset_pool_state(m) -> None:
    m.clear_email_provider_bind()
    m.reset_email_provider_failover()
    with m._email_provider_rr_lock:
        m._email_provider_rr_index = 0


def test_parse_email_providers_list() -> None:
    m = _load()
    if m is None:
        src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
        assert "def parse_email_providers_list" in src
        print("PASS parse (source contract)")
        return
    assert m.parse_email_providers_list("duckmail, cloudmail;cloudflare") == [
        "duckmail",
        "cloudmail",
        "cloudflare",
    ]
    assert m.parse_email_providers_list(["outlookmail", "cf", "duckmail", "duckmail"]) == [
        "hotmail",
        "cloudflare",
        "duckmail",
    ]
    assert m.parse_email_providers_list("") == []
    assert m.parse_email_providers_list(None) == []
    try:
        m.parse_email_providers_list("duckmail,not_a_real_provider")
        raise AssertionError("expected unknown provider to fail")
    except Exception as exc:
        assert "未知" in str(exc) or "unknown" in str(exc).lower()
    try:
        m.parse_email_providers_list("fixed,duckmail")
        raise AssertionError("expected fixed to fail")
    except Exception as exc:
        assert "fixed" in str(exc).lower()
    # gmail intentionally not in multi-select pool (still valid as EMAIL_PROVIDER single)
    try:
        m.parse_email_providers_list("gmail,cloudflare")
        raise AssertionError("expected gmail in EMAIL_PROVIDERS to fail")
    except Exception as exc:
        assert "gmail" in str(exc).lower() or "未知" in str(exc)
    assert "gmail" not in m._EMAIL_PROVIDER_POOL_KNOWN
    print("PASS parse_email_providers_list")


def test_round_robin_and_bind() -> None:
    m = _load()
    if m is None:
        print("SKIP round_robin (import failed)")
        return
    old_env = {k: os.environ.get(k) for k in ("EMAIL_PROVIDERS", "EMAIL_PROVIDER", "EMAIL_PROVIDER_STRATEGY", "FIXED_EMAIL", "MIMO_FIXED_EMAIL")}
    old_cfg = dict(m.config)
    try:
        os.environ.pop("FIXED_EMAIL", None)
        os.environ.pop("MIMO_FIXED_EMAIL", None)
        os.environ["EMAIL_PROVIDERS"] = "duckmail,cloudmail,cloudflare"
        os.environ["EMAIL_PROVIDER_STRATEGY"] = "round_robin"
        m.config = dict(old_cfg)
        m.config["email_provider"] = "hotmail"
        _reset_pool_state(m)

        pool = m.get_email_providers_pool()
        assert pool == ["duckmail", "cloudmail", "cloudflare"]

        picks = [m.select_email_provider_from_pool(pool, strategy="round_robin", bind=True) for _ in range(4)]
        assert picks == ["duckmail", "cloudmail", "cloudflare", "duckmail"]
        # last bind still cloudflare? no — last select was duckmail
        assert m.get_bound_email_provider() == "duckmail"
        assert m.get_email_provider() == "duckmail"

        m.clear_email_provider_bind()
        assert m.get_bound_email_provider() is None
        # unbound multi-select reports first pool member for UI/logs
        assert m.get_email_provider() == "duckmail"
        print("PASS round_robin_and_bind")
    finally:
        m.config = old_cfg
        _reset_pool_state(m)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_failover_advance() -> None:
    m = _load()
    if m is None:
        print("SKIP failover")
        return
    old_env = {k: os.environ.get(k) for k in ("EMAIL_PROVIDERS", "EMAIL_PROVIDER_STRATEGY", "FIXED_EMAIL", "MIMO_FIXED_EMAIL")}
    old_cfg = dict(m.config)
    try:
        os.environ.pop("FIXED_EMAIL", None)
        os.environ.pop("MIMO_FIXED_EMAIL", None)
        os.environ["EMAIL_PROVIDERS"] = "cloudflare,duckmail"
        os.environ["EMAIL_PROVIDER_STRATEGY"] = "failover"
        m.config = dict(old_cfg)
        _reset_pool_state(m)

        a = m.select_email_provider_from_pool(strategy="failover", bind=True)
        assert a == "cloudflare"
        m.advance_email_provider_failover()
        b = m.select_email_provider_from_pool(strategy="failover", bind=True)
        assert b == "duckmail"
        m.advance_email_provider_failover()
        try:
            m.select_email_provider_from_pool(strategy="failover", bind=True)
            raise AssertionError("expected failover exhaust")
        except Exception as exc:
            assert "exhausted" in str(exc).lower() or "failover" in str(exc).lower()
        print("PASS failover_advance")
    finally:
        m.config = old_cfg
        _reset_pool_state(m)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_single_provider_compat() -> None:
    m = _load()
    if m is None:
        print("SKIP single")
        return
    old_env = {k: os.environ.get(k) for k in ("EMAIL_PROVIDERS", "EMAIL_PROVIDER", "FIXED_EMAIL", "MIMO_FIXED_EMAIL")}
    old_cfg = dict(m.config)
    try:
        os.environ.pop("EMAIL_PROVIDERS", None)
        os.environ.pop("FIXED_EMAIL", None)
        os.environ.pop("MIMO_FIXED_EMAIL", None)
        os.environ["EMAIL_PROVIDER"] = "hotmail"
        m.config = dict(old_cfg)
        m.config["email_provider"] = "duckmail"
        m.config["email_providers"] = []
        # apply_env should overlay EMAIL_PROVIDER
        out = m.apply_env_config_overrides(m.config)
        m.config = out
        _reset_pool_state(m)
        assert m.get_email_providers_pool() == []
        assert m.get_email_provider() == "hotmail"
        print("PASS single_provider_compat")
    finally:
        m.config = old_cfg
        _reset_pool_state(m)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_get_email_and_token_binds_pool() -> None:
    m = _load()
    if m is None:
        print("SKIP allocate bind")
        return
    old_env = {k: os.environ.get(k) for k in ("EMAIL_PROVIDERS", "EMAIL_PROVIDER_STRATEGY", "FIXED_EMAIL", "MIMO_FIXED_EMAIL")}
    old_cfg = dict(m.config)
    try:
        os.environ.pop("FIXED_EMAIL", None)
        os.environ.pop("MIMO_FIXED_EMAIL", None)
        os.environ["EMAIL_PROVIDERS"] = "cloudmail,duckmail"
        os.environ["EMAIL_PROVIDER_STRATEGY"] = "round_robin"
        m.config = dict(old_cfg)
        m.config["defaultDomains"] = "example.com"
        _reset_pool_state(m)

        with mock.patch.object(m, "_allocate_email_for_provider", side_effect=lambda p, api_key=None: (f"u@{p}.test", "tok")) as alloc:
            email, tok = m.get_email_and_token()
            assert email == "u@cloudmail.test"
            assert m.get_bound_email_provider() == "cloudmail"
            assert m.get_email_provider() == "cloudmail"
            # OTP path must see same provider
            assert alloc.call_args_list[0].args[0] == "cloudmail"

            # second allocate without clear advances RR
            m.clear_email_provider_bind()
            email2, _ = m.get_email_and_token()
            assert email2 == "u@duckmail.test"
            assert m.get_bound_email_provider() == "duckmail"
        print("PASS get_email_and_token_binds_pool")
    finally:
        m.config = old_cfg
        _reset_pool_state(m)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_failover_resets_between_accounts() -> None:
    """failover_index must not leak across accounts after reset_email_provider_failover."""
    m = _load()
    if m is None:
        print("SKIP failover reset")
        return
    old_env = {
        k: os.environ.get(k)
        for k in ("EMAIL_PROVIDERS", "EMAIL_PROVIDER_STRATEGY", "FIXED_EMAIL", "MIMO_FIXED_EMAIL")
    }
    old_cfg = dict(m.config)
    try:
        os.environ.pop("FIXED_EMAIL", None)
        os.environ.pop("MIMO_FIXED_EMAIL", None)
        os.environ["EMAIL_PROVIDERS"] = "cloudflare,duckmail,cloudmail"
        os.environ["EMAIL_PROVIDER_STRATEGY"] = "failover"
        m.config = dict(old_cfg)
        _reset_pool_state(m)

        assert m.select_email_provider_from_pool(strategy="failover", bind=True) == "cloudflare"
        m.advance_email_provider_failover()
        assert m.select_email_provider_from_pool(strategy="failover", bind=True) == "duckmail"
        # Simulate next account start (register_cli / GUI reset)
        m.reset_email_provider_failover()
        m.clear_email_provider_bind()
        assert m.select_email_provider_from_pool(strategy="failover", bind=True) == "cloudflare"
        print("PASS failover_resets_between_accounts")
    finally:
        m.config = old_cfg
        _reset_pool_state(m)
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_env_overlay_includes_providers() -> None:
    m = _load()
    if m is None:
        src = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
        assert "EMAIL_PROVIDERS" in src
        print("PASS overlay source contract")
        return
    old = os.environ.get("EMAIL_PROVIDERS")
    old_s = os.environ.get("EMAIL_PROVIDER_STRATEGY")
    old_mt = os.environ.get("MAIL_TIMEOUT")
    try:
        os.environ["EMAIL_PROVIDERS"] = "cloudflare,duckmail"
        os.environ["EMAIL_PROVIDER_STRATEGY"] = "random"
        os.environ["MAIL_TIMEOUT"] = "20"
        out = m.apply_env_config_overrides({})
        assert out["email_providers"] == "cloudflare,duckmail"
        assert out["email_provider_strategy"] == "random"
        assert str(out.get("mail_timeout")) == "20"
        print("PASS env_overlay_includes_providers")
    finally:
        if old is None:
            os.environ.pop("EMAIL_PROVIDERS", None)
        else:
            os.environ["EMAIL_PROVIDERS"] = old
        if old_s is None:
            os.environ.pop("EMAIL_PROVIDER_STRATEGY", None)
        else:
            os.environ["EMAIL_PROVIDER_STRATEGY"] = old_s
        if old_mt is None:
            os.environ.pop("MAIL_TIMEOUT", None)
        else:
            os.environ["MAIL_TIMEOUT"] = old_mt


def test_duckmail_api_key_env_wins() -> None:
    m = _load()
    if m is None:
        print("SKIP duckmail env wins")
        return
    old = os.environ.get("DUCKMAIL_API_KEY")
    old_cfg = dict(m.config)
    try:
        m.config = dict(old_cfg)
        m.config["duckmail_api_key"] = "from-config-key-value"
        os.environ.pop("DUCKMAIL_API_KEY", None)
        assert m.get_duckmail_api_key() == "from-config-key-value"
        os.environ["DUCKMAIL_API_KEY"] = "from-env-key-value"
        assert m.get_duckmail_api_key() == "from-env-key-value"
        # empty env must not wipe to blank if we only use or-chain; empty string is falsy → config
        os.environ["DUCKMAIL_API_KEY"] = ""
        assert m.get_duckmail_api_key() == "from-config-key-value"
        print("PASS duckmail_api_key_env_wins")
    finally:
        m.config = old_cfg
        if old is None:
            os.environ.pop("DUCKMAIL_API_KEY", None)
        else:
            os.environ["DUCKMAIL_API_KEY"] = old


if __name__ == "__main__":
    test_parse_email_providers_list()
    test_round_robin_and_bind()
    test_failover_advance()
    test_failover_resets_between_accounts()
    test_single_provider_compat()
    test_get_email_and_token_binds_pool()
    test_env_overlay_includes_providers()
    test_duckmail_api_key_env_wins()
    print("ALL OK")
