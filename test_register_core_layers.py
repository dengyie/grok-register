#!/usr/bin/env python3
"""Unit tests for register_core (no live register)."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from register_core.contracts import Mailbox, RegisterJob, RegisterResult
from register_core.email.registry import list_email_sources
from register_core.errors import FailFastError
from register_core.pipeline import Pipeline
from register_core.providers.grok_adapter import GrokProvider
from register_core.providers.mimo_adapter import MimoProvider
from register_core.providers.registry import list_providers
from register_core.sink.jsonl_sink import JsonlSink
from register_core.util.files import file_size, read_appended
from register_core.util.process import CmdResult, redact_log_tail
from register_core.verify.mimo_tts import MimoTtsVerifier
from register_core.verify.noop import NoopVerifier


class FakeEmail:
    name = "fake"

    def __init__(self) -> None:
        self.released: list[tuple[str, bool]] = []
        self.alloc_calls = 0

    def allocate(self) -> Mailbox:
        self.alloc_calls += 1
        return Mailbox(address="a@example.com", token="t", provider=self.name)

    def poll_otp(self, mailbox: Mailbox, **kwargs: Any):
        from register_core.contracts import OtpCode

        return OtpCode(code="123456", source=self.name)

    def release(self, mailbox: Mailbox, *, success: bool) -> None:
        self.released.append((mailbox.address, success))


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        outcomes: list[RegisterResult] | None = None,
        raise_fatal_on: int | None = None,
        raise_verify_bomb: bool = False,
    ) -> None:
        self.outcomes = list(outcomes or [])
        self.calls = 0
        self.raise_fatal_on = raise_fatal_on
        self.seen_email_source = None

    def register_one(self, *, email_source=None, extra=None) -> RegisterResult:
        self.calls += 1
        self.seen_email_source = email_source
        if self.raise_fatal_on is not None and self.calls == self.raise_fatal_on:
            raise FailFastError("pool exhausted")
        if self.outcomes:
            return self.outcomes.pop(0)
        return RegisterResult(
            ok=True,
            provider=self.name,
            email="a@example.com",
            secret="sk-" + "a" * 40,
            secret_kind="api_key",
        )


class BoomVerifier:
    name = "boom"

    def verify(self, result: RegisterResult):
        raise RuntimeError("probe exploded")


class TestRegistry(unittest.TestCase):
    def test_lists_builtins(self):
        ps = list_providers()
        self.assertIn("grok", ps)
        self.assertIn("mimo", ps)
        self.assertIn("chatgpt", ps)
        es = list_email_sources()
        self.assertIn("tinyhost", es)
        self.assertIn("legacy_grok", es)
        self.assertIn("cloudflare", es)
        self.assertIn("gmail_imap", es)


class TestContracts(unittest.TestCase):
    def test_public_redacts_password_and_secret(self):
        r = RegisterResult(
            ok=True,
            provider="x",
            email="e@x.com",
            password="supersecretpw",
            secret="sk-abcdefghijklmnopqrstuvwxyz",
            secret_kind="api_key",
            artifacts={"tail": "验证码: 123456 sk-abcdefghijklmnopqrstuvwxyz"},
        )
        pub = r.to_public_dict()
        self.assertNotIn("supersecretpw", json.dumps(pub))
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", json.dumps(pub))
        self.assertNotEqual(pub["password"], "supersecretpw")
        self.assertIn("tail_redacted", pub["artifacts"])
        self.assertNotIn("123456", pub["artifacts"]["tail_redacted"])

    def test_redact_log_tail(self):
        t = redact_log_tail("验证码: 654321\nkey sk-abcdef0123456789abcdef")
        self.assertNotIn("654321", t)
        self.assertNotIn("sk-abcdef0123456789abcdef", t)


class TestPipeline(unittest.TestCase):
    # Offline runs must not probe local nodes.json (backend auto + catalog hang).
    _OFFLINE_EXTRA = {"egress": "direct", "nodes_preflight": False}

    def test_fail_fast_stops(self):
        p = FakeProvider(
            outcomes=[
                RegisterResult(ok=False, provider="fake", error="x", error_kind="provider"),
                RegisterResult(ok=True, provider="fake"),
            ]
        )
        pipe = Pipeline(p, fail_fast=True, verifier=NoopVerifier())
        stats = pipe.run(3, extra=dict(self._OFFLINE_EXTRA))
        self.assertEqual(p.calls, 1)
        self.assertEqual(stats.fail, 1)
        self.assertEqual(stats.ok, 0)
        self.assertTrue(stats.stopped_reason)

    def test_fatal_exception_stops(self):
        p = FakeProvider(raise_fatal_on=1)
        pipe = Pipeline(p, fail_fast=True)
        stats = pipe.run(5, extra=dict(self._OFFLINE_EXTRA))
        self.assertEqual(stats.fail, 1)
        self.assertIn("fail_fast", stats.stopped_reason)

    def test_verify_exception_marks_fail_even_without_fail_fast(self):
        p = FakeProvider(
            outcomes=[
                RegisterResult(
                    ok=True,
                    provider="fake",
                    email="e@x.com",
                    secret="sk-" + "b" * 40,
                    secret_kind="api_key",
                )
            ]
        )
        pipe = Pipeline(p, fail_fast=False, verifier=BoomVerifier())
        stats = pipe.run(1, extra=dict(self._OFFLINE_EXTRA))
        self.assertEqual(stats.ok, 0)
        self.assertEqual(stats.fail, 1)
        self.assertEqual(stats.results[0].error_kind, "verify")

    def test_shell_providers_accept_external_email_source(self):
        """M3/M4: mimo/grok consume EmailSource via FIXED_EMAIL inject (no black-box gate)."""
        for provider in ("mimo", "grok"):
            job = RegisterJob(provider=provider, email_source="tinyhost")
            pipe = Pipeline.from_job(job)
            self.assertIsNotNone(pipe.email_source)
            self.assertEqual(getattr(pipe.email_source, "name", ""), "tinyhost")
            self.assertIsNotNone(pipe.strategy)

    def test_sink_jsonl_mode_0600(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.jsonl"
            sink = JsonlSink(path)
            p = FakeProvider(
                outcomes=[
                    RegisterResult(
                        ok=True,
                        provider="fake",
                        email="e@x.com",
                        secret="sk-secretvalue1234567890",
                        secret_kind="api_key",
                        password="pw-secret",
                    )
                ]
            )
            pipe = Pipeline(p, sink=sink, verifier=NoopVerifier())
            stats = pipe.run(1, extra=dict(self._OFFLINE_EXTRA))
            self.assertEqual(stats.ok, 1)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            data = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(data["secret"], "sk-secretvalue1234567890")


class TestMimoVerifier(unittest.TestCase):
    def test_shape(self):
        v = MimoTtsVerifier(live=False)
        bad = v.verify(RegisterResult(ok=True, provider="mimo", secret="nope"))
        self.assertFalse(bad.ok)
        good = v.verify(RegisterResult(ok=True, provider="mimo", secret="sk-" + "a" * 40))
        self.assertTrue(good.ok)
        # Hyphenated / underscored vendor keys must pass the same shape gate as inject.
        hyphen = v.verify(
            RegisterResult(
                ok=True,
                provider="mimo",
                secret="sk-hyper-abc-def-0123456789abcdef",
            )
        )
        self.assertTrue(hyphen.ok)
        under = v.verify(
            RegisterResult(
                ok=True,
                provider="mimo",
                secret="sk-cdqo_test_underscore_0123456789",
            )
        )
        self.assertTrue(under.ok)


class TestSecretPatterns(unittest.TestCase):
    def test_hyphen_underscore_api_keys(self):
        from register_core.util.secrets import find_api_keys, is_api_key

        self.assertTrue(is_api_key("sk-" + "a" * 24))
        self.assertTrue(is_api_key("sk-hyper-abc-def-0123456789abcdef"))
        self.assertTrue(is_api_key("sk-cdqo_test_underscore_0123456789"))
        self.assertFalse(is_api_key("sk-short"))
        self.assertFalse(is_api_key("not-a-key"))
        found = find_api_keys("x sk-hyper-abc-def-0123456789abcdef y")
        self.assertEqual(len(found), 1)

    def test_redact_hyphenated_key(self):
        t = redact_log_tail("got sk-hyper-abc-def-0123456789abcdef done")
        self.assertNotIn("sk-hyper-abc-def-0123456789abcdef", t)
        self.assertIn("sk-***", t)

    def test_mimo_parse_hyphenated_result_json(self):
        key = "sk-hyper-abc-def-0123456789abcdef"
        stdout = (
            'RESULT_JSON:{"status":"SUCCESS","email":"n@x.com",'
            f'"password":"p","apiKey":"{key}"}}\n'
        )
        email, secret, password = MimoProvider._parse_this_run(
            stdout=stdout,
            keys_delta="",
            accounts_delta="",
        )
        self.assertEqual(email, "n@x.com")
        self.assertEqual(secret, key)
        self.assertEqual(password, "p")


class TestGrokVerifierHonesty(unittest.TestCase):
    def test_pending_sso_fails_default_verifier(self):
        from register_core.verify.grok_chat import GrokChatVerifier

        v = GrokChatVerifier()
        r = v.verify(
            RegisterResult(
                ok=True,
                provider="grok",
                email="u@x.com",
                secret="",
                secret_kind="pending",
            )
        )
        self.assertFalse(r.ok)
        self.assertIn("missing sso", r.detail.lower())

    def test_sso_soft_passes_without_live_chat(self):
        from register_core.verify.grok_chat import GrokChatVerifier

        v = GrokChatVerifier()
        r = v.verify(
            RegisterResult(
                ok=True,
                provider="grok",
                email="u@x.com",
                secret="eyJhbGciOiJIUzI1NiJ9.aaa.bbb",
                secret_kind="sso",
            )
        )
        self.assertTrue(r.ok)
        self.assertIn("deferred", r.detail)


class TestProcessTimeout(unittest.TestCase):
    def test_timeout_kills_and_sets_flag(self):
        import sys

        from register_core.util.process import run_command

        # Short sleep child; timeout forces process-group kill path.
        code = "import time; time.sleep(30)"
        res = run_command([sys.executable, "-c", code], timeout_s=0.3)
        self.assertTrue(res.timed_out)
        self.assertNotEqual(res.returncode, 0)


class TestJobFactory(unittest.TestCase):
    def test_from_job_builds(self):
        job = RegisterJob(provider="mimo", count=1, email_source="provider", verify=False)
        pipe = Pipeline.from_job(job)
        self.assertEqual(pipe.provider.name, "mimo")
        self.assertIsNone(pipe.email_source)


class TestFileOffset(unittest.TestCase):
    def test_read_appended(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jsonl"
            p.write_text("old\n", encoding="utf-8")
            off = file_size(p)
            p.write_text("old\nnew\n", encoding="utf-8")
            self.assertEqual(read_appended(p, off).strip(), "new")


class TestMimoParse(unittest.TestCase):
    def test_ignores_historical_without_delta(self):
        email, secret, password = MimoProvider._parse_this_run(
            stdout="",
            keys_delta="",
            accounts_delta="",
        )
        self.assertEqual(secret, "")
        self.assertEqual(email, "")

    def test_result_json_line(self):
        stdout = (
            'log...\nRESULT_JSON:{"status":"SUCCESS","email":"n@x.com",'
            '"password":"p","apiKey":"sk-abcdefghijklmnopqrstuvwxyz012345"}\n'
        )
        email, secret, password = MimoProvider._parse_this_run(
            stdout=stdout,
            keys_delta="",
            accounts_delta="",
        )
        self.assertEqual(email, "n@x.com")
        self.assertTrue(secret.startswith("sk-"))
        self.assertEqual(password, "p")

    def test_accounts_delta_only(self):
        delta = json.dumps(
            {"email": "b@x.com", "password": "pw", "apiKey": "sk-" + "c" * 40}
        ) + "\n"
        email, secret, password = MimoProvider._parse_this_run(
            stdout="",
            keys_delta="",
            accounts_delta="old-should-not-matter\n" + delta
            if False
            else delta,
        )
        self.assertEqual(email, "b@x.com")
        self.assertTrue(secret.startswith("sk-"))

    def test_adapter_uses_delta_not_full_file(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            out = runtime / "output"
            out.mkdir()
            (out / "accounts.jsonl").write_text(
                json.dumps({"email": "old@x.com", "apiKey": "sk-" + "o" * 40}) + "\n",
                encoding="utf-8",
            )
            (out / "success_keys.txt").write_text("1. sk-" + "o" * 40 + "\n", encoding="utf-8")
            off_acc = file_size(out / "accounts.jsonl")
            off_keys = file_size(out / "success_keys.txt")

            new_key = "sk-" + "n" * 40
            # simulate this-run append
            with (out / "accounts.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"email": "new@x.com", "apiKey": new_key}) + "\n")
            with (out / "success_keys.txt").open("a", encoding="utf-8") as f:
                f.write(f"2. {new_key}\n")

            email, secret, _ = MimoProvider._parse_this_run(
                stdout="",
                keys_delta=read_appended(out / "success_keys.txt", off_keys),
                accounts_delta=read_appended(out / "accounts.jsonl", off_acc),
            )
            self.assertEqual(email, "new@x.com")
            self.assertEqual(secret, new_key)


class TestGrokParse(unittest.TestCase):
    def test_exit0_without_ledger_is_fail(self):
        provider = GrokProvider(accounts_file="/tmp/does-not-need-exist-core-test")
        fake = CmdResult(returncode=0, stdout="done without success marker\n", stderr="", timed_out=False)
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=""):
                    r = provider.register_one()
        self.assertFalse(r.ok)
        self.assertIn("no this-run", r.error)

    def test_ledger_email_without_sso_is_fail(self):
        provider = GrokProvider(accounts_file="/tmp/x")
        ledger = "user@x.com----pw----\n"
        fake = CmdResult(
            returncode=0,
            stdout="+ 注册成功: user@x.com\n",
            stderr="",
            timed_out=False,
        )
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=ledger):
                    r = provider.register_one()
        self.assertFalse(r.ok)
        self.assertEqual(r.email, "user@x.com")
        self.assertEqual(r.secret_kind, "pending")
        self.assertIn("without SSO", r.error)

    def test_ledger_delta_success(self):
        provider = GrokProvider(accounts_file="/tmp/x")
        ledger = "user@x.com----pw----eyJhbGciOiJIUzI1NiJ9.aaa.bbb\n"
        fake = CmdResult(
            returncode=0,
            stdout="+ 注册成功: user@x.com\n",
            stderr="",
            timed_out=False,
        )
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=ledger):
                    r = provider.register_one()
        self.assertTrue(r.ok)
        self.assertEqual(r.email, "user@x.com")
        self.assertEqual(r.secret_kind, "sso")
        self.assertTrue(r.secret.startswith("eyJ"))


class TestMimoAdapterMock(unittest.TestCase):
    def test_historical_only_is_fail(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = Path(td)
            (runtime / "output").mkdir()
            old = "sk-" + "o" * 40
            (runtime / "output" / "accounts.jsonl").write_text(
                json.dumps({"email": "old@x.com", "apiKey": old}) + "\n",
                encoding="utf-8",
            )
            (runtime / "output" / "success_keys.txt").write_text(f"1. {old}\n", encoding="utf-8")
            runner = Path(td) / "run-register.sh"
            # provider looks under providers/mimo — patch runner path via MIMO_DIR mock
            provider = MimoProvider(runtime=str(runtime))
            fake = CmdResult(returncode=0, stdout="ok\n", stderr="", timed_out=False)
            with patch.object(MimoProvider, "register_one", wraps=provider.register_one):
                pass
            with patch("register_core.providers.mimo_adapter.MIMO_DIR", runtime):
                (runtime / "run-register.sh").write_text("#!/bin/bash\n", encoding="utf-8")
                with patch("register_core.providers.mimo_adapter.run_command", return_value=fake):
                    r = provider.register_one()
            self.assertFalse(r.ok)
            self.assertIn("no this-run secret", r.error)


if __name__ == "__main__":
    unittest.main()
