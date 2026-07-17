#!/usr/bin/env python3
"""Offline unit tests for ChatGPT provider (no live OpenAI register)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from register_core.contracts import Mailbox, OtpCode, RegisterResult
from register_core.errors import FailFastError, MailMissError
from register_core.pipeline import Pipeline
from register_core.providers.chatgpt_adapter import ChatGPTProvider
from register_core.providers.registry import get_provider, list_providers
from register_core.verify.chatgpt_token import ChatGPTTokenVerifier
from register_core.verify.noop import NoopVerifier
from register_core.verify.registry import get_verifier


class FakeEmail:
    name = "fake"

    def __init__(self, *, fail_otp: bool = False) -> None:
        self.fail_otp = fail_otp
        self.released: list[tuple[str, bool]] = []

    def allocate(self) -> Mailbox:
        return Mailbox(
            address="newuser@example.com",
            token="t",
            provider=self.name,
            meta={"local": "newuser", "domain": "example.com"},
        )

    def poll_otp(self, mailbox: Mailbox, **kwargs: Any) -> OtpCode:
        if self.fail_otp:
            raise MailMissError("otp timeout test")
        return OtpCode(code="123456", source=self.name)

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        self.released.append((mailbox.address, success))


class TestChatGPTRegistry(unittest.TestCase):
    def test_registered_and_aliases(self):
        self.assertIn("chatgpt", list_providers())
        p = get_provider("openai")
        self.assertEqual(p.name, "chatgpt")
        v = get_verifier("chatgpt")
        self.assertEqual(v.name, "chatgpt_token")


class TestChatGPTVerifier(unittest.TestCase):
    def test_shape_gate(self):
        v = ChatGPTTokenVerifier(live=False)
        bad = v.verify(RegisterResult(ok=True, provider="chatgpt", secret="short"))
        self.assertFalse(bad.ok)
        good = v.verify(
            RegisterResult(
                ok=True,
                provider="chatgpt",
                secret="rt_" + "x" * 40,
                secret_kind="refresh_token",
            )
        )
        self.assertTrue(good.ok)
        # Live OpenAI opaque refresh: rt.1.<payload> (two dots, short first segment)
        modern_rt = v.verify(
            RegisterResult(
                ok=True,
                provider="chatgpt",
                secret="rt.1." + ("A" * 160),
                secret_kind="refresh_token",
            )
        )
        self.assertTrue(modern_rt.ok, modern_rt.detail)
        # Still reject clearly broken JWT-shaped secrets (not rt.*)
        malformed_jwt = v.verify(
            RegisterResult(
                ok=True,
                provider="chatgpt",
                secret="ab.cd." + ("e" * 40),
                secret_kind="access_token",
            )
        )
        self.assertFalse(malformed_jwt.ok)
        wrong_kind = v.verify(
            RegisterResult(
                ok=True,
                provider="chatgpt",
                secret="rt_" + "y" * 40,
                secret_kind="api_key",
            )
        )
        self.assertFalse(wrong_kind.ok)


class TestChatGPTProtocolHelpers(unittest.TestCase):
    def test_pkce_and_sentinel_pow_offline(self):
        from providers.chatgpt.protocol.flow import _generate_pkce
        from providers.chatgpt.protocol.sentinel import SentinelTokenGenerator

        verifier, challenge = _generate_pkce()
        self.assertGreater(len(verifier), 20)
        self.assertGreater(len(challenge), 20)
        self.assertNotEqual(verifier, challenge)

        gen = SentinelTokenGenerator("device-test")
        req = gen.generate_requirements_token()
        self.assertTrue(req.startswith("gAAAAAC"))
        # difficulty "f" is easy — should solve quickly
        tok = gen.generate_token("seed", "f")
        self.assertTrue(tok.startswith("gAAAAAB"))

    def test_registration_result_public_redacts(self):
        from providers.chatgpt.protocol.flow import RegistrationResult

        r = RegistrationResult(
            ok=True,
            email="e@x.com",
            password="SuperSecretPass1!",
            access_token="sk-access-abcdefghijklmnopqrstuvwxyz",
            refresh_token="rt-refresh-abcdefghijklmnopqrstuvwxyz",
            id_token="id.token.value.here",
        )
        pub = r.to_public_dict()
        blob = json.dumps(pub)
        self.assertNotIn("SuperSecretPass1!", blob)
        self.assertNotIn("sk-access-abcdefghijklmnopqrstuvwxyz", blob)
        self.assertNotIn("rt-refresh-abcdefghijklmnopqrstuvwxyz", blob)

    def test_human_pause_off_is_noop(self):
        from providers.chatgpt.protocol.flow import _human_pause

        logs: list[str] = []
        with patch.dict(
            "os.environ",
            {"CHATGPT_HUMAN_PACE": "0"},
            clear=False,
        ), patch("providers.chatgpt.protocol.flow.time.sleep") as sleep:
            waited = _human_pause(logs.append, label="unit")
        self.assertEqual(waited, 0.0)
        sleep.assert_not_called()
        self.assertEqual(logs, [])

    def test_human_pause_default_jitter_range(self):
        from providers.chatgpt.protocol.flow import _human_pause

        logs: list[str] = []
        with patch.dict(
            "os.environ",
            {
                "CHATGPT_HUMAN_PACE": "1",
                "CHATGPT_STEP_DELAY_S": "10",
                "CHATGPT_STEP_JITTER_S": "1",
            },
            clear=False,
        ), patch(
            "providers.chatgpt.protocol.flow.random.uniform", return_value=10.4
        ) as uniform, patch(
            "providers.chatgpt.protocol.flow.time.sleep"
        ) as sleep:
            waited = _human_pause(logs.append, label="after_authorize")
        self.assertEqual(waited, 10.4)
        uniform.assert_called_once_with(9.0, 11.0)
        sleep.assert_called_once_with(10.4)
        self.assertTrue(any("human_pace" in x and "after_authorize" in x for x in logs))


class TestChatGPTAdapterAttribution(unittest.TestCase):
    def test_success_from_this_run_only(self):
        """Historical accounts.jsonl must not make ok=True without protocol result."""
        email = FakeEmail()
        provider = ChatGPTProvider(proxy="")

        from providers.chatgpt.protocol.flow import RegistrationResult

        fake_ok = RegistrationResult(
            ok=True,
            email="newuser@example.com",
            password="Pw1!abcdef",
            access_token="a" * 48,
            refresh_token="r" * 48,
            id_token="h.p.s",
            device_id="dev",
            steps={"authorize": {}, "register_user": {}},
        )

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            with patch(
                "register_core.providers.chatgpt_adapter.OUTPUT_DIR", out
            ), patch(
                "providers.chatgpt.protocol.flow.register_one",
                return_value=fake_ok,
            ):
                # Pre-seed historical ledger
                (out / "accounts.jsonl").write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "email": "old@example.com",
                            "refresh_token": "old-refresh-token-should-not-win",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                result = provider.register_one(email_source=email)

        self.assertTrue(result.ok)
        self.assertEqual(result.email, "newuser@example.com")
        self.assertEqual(result.secret, "r" * 48)
        self.assertEqual(result.secret_kind, "refresh_token")
        self.assertNotEqual(result.secret, "old-refresh-token-should-not-win")
        pub = result.to_public_dict()
        self.assertNotIn("r" * 48, json.dumps(pub))
        self.assertEqual(email.released, [("newuser@example.com", True)])

    def test_missing_refresh_is_failure(self):
        email = FakeEmail()
        provider = ChatGPTProvider(proxy="")
        from providers.chatgpt.protocol.flow import RegistrationResult

        fake = RegistrationResult(
            ok=True,
            email="newuser@example.com",
            access_token="a" * 48,
            refresh_token="",
        )
        with patch(
            "providers.chatgpt.protocol.flow.register_one", return_value=fake
        ):
            result = provider.register_one(email_source=email)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "provider")
        self.assertEqual(email.released[-1][1], False)

    def test_mail_miss_returns_result_with_email(self):
        email = FakeEmail(fail_otp=True)
        provider = ChatGPTProvider(proxy="")
        from providers.chatgpt.protocol.flow import ChatGPTRegisterError

        with patch(
            "providers.chatgpt.protocol.flow.register_one",
            side_effect=ChatGPTRegisterError("otp_wait:x", kind="mail_miss"),
        ):
            result = provider.register_one(email_source=email)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "mail_miss")
        self.assertEqual(result.email, "newuser@example.com")
        self.assertEqual(email.released[-1][1], False)

    def test_pipeline_in_process_accepts_email_source(self):
        """chatgpt is not black-box — Pipeline.from_job allows tinyhost."""
        from register_core.contracts import RegisterJob

        # should not raise ValueError (unlike mimo)
        job = RegisterJob(provider="chatgpt", email_source="tinyhost", verify=False)
        # from_job will try to construct real tinyhost source — ok
        pipe = Pipeline.from_job(job)
        self.assertEqual(pipe.provider.name, "chatgpt")
        self.assertIsNotNone(pipe.email_source)

    def test_fail_fast_empty_allocate(self):
        class EmptyMail:
            name = "empty"

            def allocate(self) -> Mailbox:
                return Mailbox(address="", provider=self.name)

            def poll_otp(self, mailbox, **kwargs):
                raise AssertionError("no")

            def release(self, mailbox, *, success):
                return None

        with self.assertRaises(FailFastError):
            ChatGPTProvider(proxy="").register_one(email_source=EmptyMail())

    def test_pipeline_uses_this_run_result(self):
        email = FakeEmail()
        provider = ChatGPTProvider(proxy="")
        from providers.chatgpt.protocol.flow import RegistrationResult

        fake_ok = RegistrationResult(
            ok=True,
            email="newuser@example.com",
            password="Pw1!abcdef",
            access_token="a" * 48,
            refresh_token="r" * 48,
        )

        def _noop_preflight(extra, **_kw):
            base = dict(extra or {})
            base["_nodes_preflight_done"] = True
            base["_nodes_preflight"] = {"skipped": True, "reason": "test"}
            return base

        def _noop_inject(extra, **_kw):
            return dict(extra or {})

        with tempfile.TemporaryDirectory() as td:
            with (
                patch(
                    "register_core.providers.chatgpt_adapter.OUTPUT_DIR", Path(td)
                ),
                patch(
                    "providers.chatgpt.protocol.flow.register_one",
                    return_value=fake_ok,
                ),
                patch(
                    "register_core.util.proxy.preflight_nodes_for_register",
                    side_effect=_noop_preflight,
                ),
                patch(
                    "register_core.util.proxy.inject_attempt_proxy",
                    side_effect=_noop_inject,
                ),
            ):
                pipe = Pipeline(
                    provider,
                    email_source=email,
                    verifier=ChatGPTTokenVerifier(),
                    fail_fast=True,
                )
                # direct + no preflight: offline unit must never probe nodes.json
                stats = pipe.run(
                    1, extra={"egress": "direct", "nodes_preflight": False}
                )
        self.assertEqual(stats.ok, 1)
        self.assertEqual(stats.fail, 0)
        self.assertTrue(stats.results[0].ok)


if __name__ == "__main__":
    unittest.main()
