#!/usr/bin/env python3
"""Offline protocol step mapping + hard gates (no live OpenAI)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from providers.chatgpt.protocol.flow import (
    ChatGPTRegisterError,
    PlatformRegistrar,
    RegistrationResult,
    _is_transport_exception,
    _sentinel_soft_enabled,
    register_one,
)


class _Resp:
    def __init__(self, status: int, body: dict[str, Any] | None = None, headers: dict | None = None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}
        self.text = str(body or "")
        self.url = "https://auth.openai.com/test"

    def json(self):
        return self._body


class TestSentinelSoftFlag(unittest.TestCase):
    def test_default_hard(self) -> None:
        with patch.dict("os.environ", {"CHATGPT_SENTINEL_SOFT": "0"}, clear=False):
            self.assertFalse(_sentinel_soft_enabled())
        with patch.dict("os.environ", {}, clear=False):
            # Unset → hard
            import os

            os.environ.pop("CHATGPT_SENTINEL_SOFT", None)
            self.assertFalse(_sentinel_soft_enabled())

    def test_soft_opt_in(self) -> None:
        with patch.dict("os.environ", {"CHATGPT_SENTINEL_SOFT": "1"}, clear=False):
            self.assertTrue(_sentinel_soft_enabled())
        with patch.dict("os.environ", {"CHATGPT_SENTINEL_SOFT": "soft"}, clear=False):
            self.assertTrue(_sentinel_soft_enabled())


class TestSessionHardGate(unittest.TestCase):
    def test_missing_session_cookie_raises_session_kind(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev-test"
        reg.state = "st"
        reg.last_authorize = {"final_url": "https://auth.openai.com/u/signup"}

        with (
            patch.object(reg, "_ensure_sentinel", return_value="tok"),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(_Resp(200, {}), None),
            ),
            patch(
                "providers.chatgpt.protocol.flow.cookie_get",
                return_value="",
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.establish_session()
        self.assertEqual(cm.exception.kind, "session")
        self.assertEqual(cm.exception.step, "session")

    def test_session_all_transport_fail_is_network(self) -> None:
        """All candidate GETs return None → network, not product session."""
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev-test"
        reg.state = "st"
        reg.last_authorize = {"final_url": "https://auth.openai.com/u/signup"}

        with (
            patch.object(reg, "_ensure_sentinel", return_value="tok"),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(None, "connection reset"),
            ),
            patch(
                "providers.chatgpt.protocol.flow.cookie_get",
                return_value="",
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.establish_session()
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "session")
        self.assertIn("transport", str(cm.exception))

    def test_sentinel_hard_fail_on_accounts_headers(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev-test"
        with (
            patch.dict("os.environ", {"CHATGPT_SENTINEL_SOFT": "0"}, clear=False),
            patch.object(
                reg,
                "_ensure_sentinel",
                side_effect=RuntimeError("pow_failed"),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg._accounts_headers("https://auth.openai.com/x", "authorize_continue")
        self.assertEqual(cm.exception.kind, "captcha")
        self.assertIn("sentinel", cm.exception.step)

    def test_is_transport_exception_classifies_network(self) -> None:
        """Transport/proxy/timeout must not be mis-tagged as captcha."""
        self.assertTrue(_is_transport_exception(TimeoutError("timed out")))
        self.assertTrue(_is_transport_exception(OSError("connection reset")))
        self.assertTrue(_is_transport_exception(ConnectionError("proxy refused")))
        self.assertTrue(_is_transport_exception(RuntimeError("HTTPSConnectionPool timed out")))
        self.assertTrue(_is_transport_exception(RuntimeError("ProxyError: 407")))
        # genuine PoW/captcha failures stay non-transport
        self.assertFalse(_is_transport_exception(RuntimeError("pow_failed")))
        self.assertFalse(_is_transport_exception(RuntimeError("sentinel challenge")))
        self.assertFalse(_is_transport_exception(ValueError("bad token")))

    def test_sentinel_transport_fail_is_network_not_captcha(self) -> None:
        """Proxy/timeout on sentinel must hard-fail as network, not captcha."""
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev-test"
        with (
            patch.dict("os.environ", {"CHATGPT_SENTINEL_SOFT": "0"}, clear=False),
            patch.object(
                reg,
                "_ensure_sentinel",
                side_effect=TimeoutError("proxy timed out"),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg._accounts_headers("https://auth.openai.com/x", "authorize_continue")
        self.assertEqual(cm.exception.kind, "network")
        self.assertIn("sentinel_transport", str(cm.exception))

    def test_sentinel_soft_continues(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev-test"
        with (
            patch.dict("os.environ", {"CHATGPT_SENTINEL_SOFT": "1"}, clear=False),
            patch.object(
                reg,
                "_ensure_sentinel",
                side_effect=RuntimeError("pow_failed"),
            ),
        ):
            headers = reg._accounts_headers(
                "https://auth.openai.com/x", "authorize_continue"
            )
        self.assertIn("x-openai-sentinel-error", headers)
        self.assertNotIn("openai-sentinel-token", headers)


class TestStepKinds(unittest.TestCase):
    def test_register_user_409_is_already_registered(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(_Resp(409, {"error": "already exists"}), None),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.register_user("a@b.com", "Pw1!abcdef")
        self.assertEqual(cm.exception.kind, "already_registered")
        self.assertEqual(cm.exception.step, "register_user")

    def test_register_user_bare_exists_not_already_registered(self) -> None:
        """Loose 'exists'/'already' alone must not classify as identity collision."""
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(_Resp(400, {"error": "rate limit exists upstream"}), None),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.register_user("a@b.com", "Pw1!abcdef")
        self.assertEqual(cm.exception.kind, "provider")
        self.assertNotEqual(cm.exception.kind, "already_registered")

    def test_register_user_transport_none_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(None, "connection reset"),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.register_user("a@b.com", "Pw1!abcdef")
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "register_user")

    def test_validate_otp_bad_is_otp_invalid(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(_Resp(400, {"error": "invalid code"}), None),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.validate_otp("000000")
        self.assertEqual(cm.exception.kind, "otp_invalid")
        self.assertEqual(cm.exception.step, "validate_otp")

    def test_validate_otp_captcha_body_is_captcha(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(
                    _Resp(403, {"error": {"code": "captcha_required", "message": "solve captcha"}}),
                    None,
                ),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.validate_otp("123456")
        self.assertEqual(cm.exception.kind, "captcha")
        self.assertEqual(cm.exception.step, "validate_otp")

    def test_validate_otp_rate_limit_is_provider(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(
                    _Resp(429, {"error": {"message": "rate limit exceeded"}}),
                    None,
                ),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.validate_otp("123456")
        self.assertEqual(cm.exception.kind, "provider")
        self.assertEqual(cm.exception.step, "validate_otp")

    def test_validate_otp_transport_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(None, "proxy timeout"),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.validate_otp("123456")
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "validate_otp")

    def test_validate_otp_5xx_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(_Resp(502, {"error": "bad gateway"}), None),
            ),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.validate_otp("123456")
        self.assertEqual(cm.exception.kind, "network")

    def test_authorize_transport_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with patch(
            "providers.chatgpt.protocol.flow.request_with_retry",
            return_value=(None, "connect timeout"),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.start_authorize("a@b.com")
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "authorize")

    def test_authorize_http_4xx_is_provider(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with patch(
            "providers.chatgpt.protocol.flow.request_with_retry",
            return_value=(_Resp(403, {"error": "forbidden"}), None),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.start_authorize("a@b.com")
        self.assertEqual(cm.exception.kind, "provider")
        self.assertEqual(cm.exception.step, "authorize")

    def test_authorize_http_5xx_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with patch(
            "providers.chatgpt.protocol.flow.request_with_retry",
            return_value=(_Resp(502, {"error": "bad gateway"}), None),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.start_authorize("a@b.com")
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "authorize")

    def test_send_otp_transport_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with patch(
            "providers.chatgpt.protocol.flow.request_with_retry",
            return_value=(None, "empty response"),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.send_otp()
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "send_otp")

    def test_create_account_transport_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        seq = [
            (_Resp(200, {}), None),  # about-you GET
            (None, "connection refused"),  # create_account POST
        ]

        def _req(*_a, **_k):
            return seq.pop(0)

        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                side_effect=_req,
            ),
            patch(
                "providers.chatgpt.protocol.flow.response_json",
                side_effect=lambda r: getattr(r, "_body", {}) if r is not None else {},
            ),
            patch(
                "providers.chatgpt.protocol.flow._human_pause",
                return_value=0.0,
            ),
            patch.dict("os.environ", {"CHATGPT_HUMAN_PACE": "0"}, clear=False),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.create_account("Test User", "1990-01-01")
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "create_account")

    def test_create_account_disallowed(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        # First call = GET about-you (200); second = POST create_account (400).
        seq = [
            (_Resp(200, {}), None),
            (_Resp(400, {"error": {"code": "registration_disallowed"}}), None),
        ]

        def _req(*_a, **_k):
            return seq.pop(0)

        with (
            patch.object(reg, "_accounts_headers", return_value={}),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                side_effect=_req,
            ),
            patch(
                "providers.chatgpt.protocol.flow.response_json",
                side_effect=lambda r: getattr(r, "_body", {}) if r is not None else {},
            ),
            patch(
                "providers.chatgpt.protocol.flow._human_pause",
                return_value=0.0,
            ),
            patch.dict("os.environ", {"CHATGPT_HUMAN_PACE": "0"}, clear=False),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg.create_account("Test User", "1990-01-01")
        self.assertEqual(cm.exception.kind, "registration_disallowed")
        self.assertEqual(cm.exception.step, "create_account")

    def test_exchange_tokens_missing_callback(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        reg.code_verifier = "v" * 43
        with patch.object(reg, "_follow_consent_for_code", return_value=None):
            result = reg.exchange_tokens("")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "oauth_callback")
        self.assertEqual(result.fail_step, "oauth_callback")
        self.assertIn("oauth_callback", result.steps)

    def test_exchange_tokens_consent_transport_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        reg.code_verifier = "v" * 43
        with patch.object(
            reg,
            "_follow_consent_for_code",
            side_effect=ChatGPTRegisterError(
                "consent_transport:connection reset",
                kind="network",
                step="oauth_callback",
            ),
        ):
            result = reg.exchange_tokens(
                "https://auth.openai.com/api/accounts/consent?continue=x"
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "network")
        self.assertEqual(result.fail_step, "oauth_callback")
        self.assertIn("oauth_callback", result.steps)

    def test_follow_consent_first_get_none_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        with patch(
            "providers.chatgpt.protocol.flow.request_with_retry",
            return_value=(None, "proxy timeout"),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                reg._follow_consent_for_code(
                    "https://auth.openai.com/api/accounts/consent?continue=x"
                )
        self.assertEqual(cm.exception.kind, "network")
        self.assertEqual(cm.exception.step, "oauth_callback")

    def test_exchange_tokens_http_error_is_token(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        reg.code_verifier = "v" * 43
        fake_session = MagicMock()
        with (
            patch.object(
                reg, "_follow_consent_for_code", return_value={"code": "abc"}
            ),
            patch(
                "providers.chatgpt.protocol.flow.create_session",
                return_value=fake_session,
            ),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(_Resp(400, {"error": "bad"}), None),
            ),
            patch(
                "providers.chatgpt.protocol.flow.response_json",
                return_value={"error": "bad"},
            ),
        ):
            result = reg.exchange_tokens("https://platform.openai.com/auth/callback?code=x")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "token")
        self.assertEqual(result.fail_step, "token")
        self.assertIn("token", result.steps)
        self.assertIn("oauth_callback", result.steps)

    def test_exchange_tokens_transport_none_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        reg.code_verifier = "v" * 43
        fake_session = MagicMock()
        with (
            patch.object(
                reg, "_follow_consent_for_code", return_value={"code": "abc"}
            ),
            patch(
                "providers.chatgpt.protocol.flow.create_session",
                return_value=fake_session,
            ),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(None, "connection aborted"),
            ),
        ):
            result = reg.exchange_tokens(
                "https://platform.openai.com/auth/callback?code=x"
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "network")
        self.assertEqual(result.fail_step, "token")

    def test_exchange_tokens_5xx_is_network(self) -> None:
        reg = PlatformRegistrar(proxy="", log=lambda _m: None)
        reg.device_id = "dev"
        reg.code_verifier = "v" * 43
        fake_session = MagicMock()
        with (
            patch.object(
                reg, "_follow_consent_for_code", return_value={"code": "abc"}
            ),
            patch(
                "providers.chatgpt.protocol.flow.create_session",
                return_value=fake_session,
            ),
            patch(
                "providers.chatgpt.protocol.flow.request_with_retry",
                return_value=(_Resp(503, {"error": "unavailable"}), None),
            ),
            patch(
                "providers.chatgpt.protocol.flow.response_json",
                return_value={"error": "unavailable"},
            ),
        ):
            result = reg.exchange_tokens(
                "https://platform.openai.com/auth/callback?code=x"
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "network")


class TestRegisterOnePartialSteps(unittest.TestCase):
    def test_mail_miss_attaches_partial_steps(self) -> None:
        """otp miss after successful early steps carries ledger on exception."""

        class _FakeReg:
            device_id = "dev"

            def __init__(self, *a, **k):
                pass

            def start_authorize(self, email):
                return {"status": 200, "final_url": "https://auth.openai.com/u/signup"}

            def establish_session(self):
                return {"ok": True}

            def register_user(self, email, password):
                return {"status": 200}

            def send_otp(self):
                return {"status": 200}

            def close(self):
                return None

        with (
            patch(
                "providers.chatgpt.protocol.flow.PlatformRegistrar",
                _FakeReg,
            ),
            patch(
                "providers.chatgpt.protocol.flow._human_pause",
                return_value=0.0,
            ),
            patch.dict("os.environ", {"CHATGPT_HUMAN_PACE": "0"}, clear=False),
        ):
            def otp_provider():
                raise TimeoutError("no mail")

            with self.assertRaises(ChatGPTRegisterError) as cm:
                register_one(
                    email="a@b.com",
                    password="Pw1!abcdef12",
                    proxy="",
                    otp_provider=otp_provider,
                    log=lambda _m: None,
                )
        self.assertEqual(cm.exception.kind, "mail_miss")
        self.assertEqual(cm.exception.step, "otp_wait")
        self.assertIn("authorize", cm.exception.steps)
        self.assertIn("send_otp", cm.exception.steps)

    def test_session_fail_short_circuits(self) -> None:
        class _FakeReg:
            device_id = "dev"

            def __init__(self, *a, **k):
                pass

            def start_authorize(self, email):
                return {"status": 200, "final_url": "https://auth.openai.com/u/signup"}

            def establish_session(self):
                raise ChatGPTRegisterError(
                    "session_cookie_missing", kind="session", step="session"
                )

            def close(self):
                return None

        with (
            patch(
                "providers.chatgpt.protocol.flow.PlatformRegistrar",
                _FakeReg,
            ),
            patch(
                "providers.chatgpt.protocol.flow._human_pause",
                return_value=0.0,
            ),
            patch.dict("os.environ", {"CHATGPT_HUMAN_PACE": "0"}, clear=False),
        ):
            with self.assertRaises(ChatGPTRegisterError) as cm:
                register_one(
                    email="a@b.com",
                    password="Pw1!abcdef12",
                    proxy="",
                    otp_provider=lambda: "123456",
                    log=lambda _m: None,
                )
        self.assertEqual(cm.exception.kind, "session")
        self.assertEqual(cm.exception.step, "session")
        self.assertIn("authorize", cm.exception.steps)
        self.assertNotIn("register_user", cm.exception.steps)


class TestRegistrationResultPublic(unittest.TestCase):
    def test_public_includes_fail_step(self) -> None:
        r = RegistrationResult(
            ok=False,
            error="missing_oauth_callback",
            error_kind="oauth_callback",
            fail_step="oauth_callback",
            steps={"oauth_callback": {"ok": False}},
        )
        pub = r.to_public_dict()
        self.assertEqual(pub["fail_step"], "oauth_callback")
        self.assertEqual(pub["error_kind"], "oauth_callback")
        self.assertIn("oauth_callback", pub["step_keys"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
