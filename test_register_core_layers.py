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

    def test_extra_proxy_forwarded_to_env(self):
        """Pipeline inject_attempt_proxy must reach register_cli via PROXY env."""
        provider = GrokProvider(accounts_file="/tmp/x")
        captured: dict = {}
        want = "http://user:pw@10.0.0.9:9000"

        def _capture_run(cmd, cwd=None, env=None, timeout_s=None):  # noqa: ARG001
            captured["env"] = dict(env or {})
            return CmdResult(returncode=1, stdout="fail", stderr="", timed_out=False)

        # Isolate from ambient PROXY/CPA_PROXY left by other tests or host env.
        scrub = {
            k: v
            for k, v in os.environ.items()
            if k not in {"PROXY", "CPA_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
        }
        with patch.dict(os.environ, scrub, clear=True):
            with patch(
                "register_core.providers.grok_adapter.run_command",
                side_effect=_capture_run,
            ):
                with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                    provider.register_one(extra={"proxy": want})
        self.assertEqual(captured.get("env", {}).get("PROXY"), want)
        self.assertEqual(captured.get("env", {}).get("CPA_PROXY"), want)


class TestGrokFatalContract(unittest.TestCase):
    """register_cli exit-code contract (authoritative, machine-readable):
    exit 2 = fatal; exit 1 = retryable not-product; exit 0 = product ok.
    SUMMARY_JSON "fatal" bool is the cross-check. The old substring matcher
    (`"fatal" in lower(out)`) mis-promoted exit-1 OTP timeouts — which always
    print `"fatal":false` and the key "fatal_reason" — to FailFastError, stopping
    the whole batch on a single transient failure (pxed smoke regression).

    `_SUMMARY` mimics the real register_cli tail (=== 完成 ... ===, banner line,
    then `SUMMARY_JSON {...}` on its own line).
    """

    _OK_SUMMARY = (
        '=== 完成: 注册成功 0, 注册失败 1 ===\n'
        '[!] 本批未达到产品可用 free Build 标准\n'
        'SUMMARY_JSON {"event":"register_cli_summary","exit":1,"reg_success":0,'
        '"reg_fail":1,"fatal":false,"fatal_reason":"","product_ok":false}\n'
    )
    _FATAL_SUMMARY = (
        '=== 完成: 注册成功 0, 注册失败 1 ===\n'
        '[!] 致命错误已停止任务（不空转）: 可用别名已耗尽\n'
        'SUMMARY_JSON {"event":"register_cli_summary","exit":2,"reg_success":0,'
        '"reg_fail":1,"fatal":true,"fatal_reason":"可用别名已耗尽","product_ok":false}\n'
    )

    def test_exit1_summary_fatalfalse_is_not_failfast(self):
        # Regression: pxed grok smoke hit a transient OTP timeout → register_cli
        # exit=1 with "fatal":false; the old substring matcher raised FailFastError
        # because `"fatal" in lower(out)` matched the JSON key/value. Must be a
        # recoverable provider failure (error_kind=provider), not fatal.
        provider = GrokProvider(accounts_file="/tmp/x")
        fake = CmdResult(returncode=1, stdout=self._OK_SUMMARY, stderr="", timed_out=False)
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=""):
                    r = provider.register_one()
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "provider", "exit-1 non-fatal must be provider, not fatal")
        self.assertNotIn("fail_fast", (r.error or "").lower())

    def test_exit2_is_failfast(self):
        provider = GrokProvider(accounts_file="/tmp/x")
        fake = CmdResult(returncode=2, stdout=self._FATAL_SUMMARY, stderr="", timed_out=False)
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=""):
                    with self.assertRaises(FailFastError) as ctx:
                        provider.register_one()
        # authority contract reason surfaces (not the old bare "exit=2")
        self.assertIn("可用别名已耗尽", str(ctx.exception))

    def test_summary_fatal_true_promotes_exit1_to_failfast(self):
        # Cross-check via SUMMARY_JSON: if register_cli says fatal:true even at exit=1
        # (shouldn't happen by contract, but the JSON is authoritative) we honor it.
        provider = GrokProvider(accounts_file="/tmp/x")
        sum1 = self._FATAL_SUMMARY.replace('"exit":2', '"exit":1')
        fake = CmdResult(returncode=1, stdout=sum1, stderr="", timed_out=False)
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=""):
                    with self.assertRaises(FailFastError):
                        provider.register_one()

    def test_no_summary_no_output_is_failfast(self):
        # No output at all (orphan before main) → treat as fatal spawn.
        provider = GrokProvider(accounts_file="/tmp/x")
        fake = CmdResult(returncode=1, stdout="", stderr="", timed_out=False)
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=""):
                    with self.assertRaises(FailFastError):
                        provider.register_one()

    def test_exit1_otp_timeout_is_not_failfast(self):
        # Even when stdout contains a Traceback (OTP timeout), exit=1 + "fatal":false
        # must NOT be fail-fast. The OTP timeout is a retryable provider error.
        provider = GrokProvider(accounts_file="/tmp/x")
        stdout = (
            '=== 完成: 注册成功 0, 注册失败 1 ===\n'
            'SUMMARY_JSON {"event":"register_cli_summary","exit":1,"reg_fail":1,'
            '"fatal":false,"fatal_reason":"","product_ok":false}\n'
            'Traceback (most recent call last):\n'
            '  File "register_cli.py", line 666, in register_one\n'
            '    code = reg.fill_code_and_submit(\n'
            'Exception: fixed core OTP timeout for x@publicvm.com: timed out after 30.0 seconds\n'
        )
        fake = CmdResult(returncode=1, stdout=stdout, stderr="", timed_out=False)
        with patch("register_core.providers.grok_adapter.run_command", return_value=fake):
            with patch("register_core.providers.grok_adapter.file_size", return_value=0):
                with patch("register_core.providers.grok_adapter.read_appended", return_value=""):
                    r = provider.register_one()
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "provider")



class TestMimoFatalContract(unittest.TestCase):
    """MiMo classify contract (authoritative, machine-readable):
    register-one.js emits ``RESULT_JSON:{status, email, error, at}`` and sets
    ``process.exitCode = 1`` for *every* failure (Geetest, OTP timeout, provider
    error) — there is no ``exit 2`` fatal here. run-register.sh wrapper echoes
    ``[mimo] fail-fast after error code=...`` on ANY non-zero exit, so the old
    ``_classify`` substring matcher (``"fail-fast" in lower(out)``) labeled every
    retryable Geetest/OTP timeout as ``error_kind="fatal"`` → StrategyEngine
    ``fail_fast_kings`` stopped the whole batch on a single transient failure,
    contradicting the rerun-wins strategy for probabilistic Geetest
    (see [[project-mimo-migrate-route-ok-20260718]]).

    Fix: ``_classify`` decodes RESULT_JSON ``status:"FAILED"`` + ``error`` as
    authority → captcha / mail_miss / provider (retryable). Only a genuinely
    empty spawn (no RESULT_JSON, no output) is fatal.
    """

    def _make_provider(self, tmp: str) -> MimoProvider:
        runtime = Path(tmp)
        (runtime / "output").mkdir(exist_ok=True)
        provider = MimoProvider(runtime=str(runtime))
        return provider, runtime

    def _run(self, provider: MimoProvider, runtime: Path, stdout: str, stderr: str = "", rc: int = 1) -> RegisterResult:
        fake = CmdResult(returncode=rc, stdout=stdout, stderr=stderr, timed_out=False)
        runner = runtime / "run-register.sh"
        if not runner.is_file():
            runner.write_text("#!/bin/bash\n", encoding="utf-8")
        with patch("register_core.providers.mimo_adapter.MIMO_DIR", runtime):
            with patch("register_core.providers.mimo_adapter.file_size", return_value=0):
                with patch("register_core.providers.mimo_adapter.read_appended", return_value=""):
                    with patch("register_core.providers.mimo_adapter.run_command", return_value=fake):
                        return provider.register_one()

    def test_geetest_failure_is_captcha_not_fatal(self):
        # Regression: Geetest solve failure is probabilistic + retryable. The
        # runner wrapper prints "fail-fast" on this exit-1; RESULT_JSON error
        # "geetest" must drive kind=captcha, NOT fatal.
        with tempfile.TemporaryDirectory() as td:
            provider, runtime = self._make_provider(td)
            stdout = (
                "=== mimo register 1/1 ===\n"
                "[mimo] fail-fast after error code=1 (no empty spin)\n"
                "RESULT_JSON:" + json.dumps(
                    {"status": "FAILED", "email": "r1@boom", "error": "geetest challenge failed"}
                ) + "\n"
            )
            r = self._run(provider, runtime, stdout=stdout, rc=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "captcha", "Geetest failure must be retryable captcha, not fatal")
        self.assertNotIn("fatal", (r.error_kind or ""))

    def test_otp_timeout_is_mail_miss_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            provider, runtime = self._make_provider(td)
            stdout = (
                "[mimo] fail-fast after error code=1 (no empty spin)\n"
                "RESULT_JSON:" + json.dumps(
                    {"status": "FAILED", "email": "r2@boom", "error": "otp timeout: no mail in 180s"}
                ) + "\n"
            )
            r = self._run(provider, runtime, stdout=stdout, rc=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "mail_miss", "OTP timeout must be retryable mail_miss")

    def test_empty_spawn_is_fatal(self):
        # No RESULT_JSON + empty output → fatal spawn (orphan before main).
        with tempfile.TemporaryDirectory() as td:
            provider, runtime = self._make_provider(td)
            r = self._run(provider, runtime, stdout="", stderr="", rc=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "fatal", "empty spawn with no authority must be fatal")

    def test_generic_failed_error_is_provider_not_fatal(self):
        # RESULT_JSON FAILED with a non-captcha, non-otp, non-alias error → provider
        # (retryable), never fatal.
        with tempfile.TemporaryDirectory() as td:
            provider, runtime = self._make_provider(td)
            stdout = (
                "[mimo] fail-fast after error code=1 (no empty spin)\n"
                "RESULT_JSON:" + json.dumps(
                    {"status": "FAILED", "email": "r3@boom", "error": "network reset by peer"}
                ) + "\n"
            )
            r = self._run(provider, runtime, stdout=stdout, rc=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "provider")

    def test_result_json_wins_over_failfast_substring(self):
        # The exact regression: output contains BOTH the wrapper's "fail-fast"
        # string AND a retryable RESULT_JSON. Authority (RESULT_JSON) must win;
        # kind must not be fatal even though "fail-fast" / "fatal" substring is
        # present in the merged blob.
        with tempfile.TemporaryDirectory() as td:
            provider, runtime = self._make_provider(td)
            stdout = (
                "[mimo] fail-fast after error code=1 (no empty spin)\n"
                "fatal: unhandledRejection should not happen\n"
                "RESULT_JSON:" + json.dumps(
                    {"status": "FAILED", "email": "r4@boom", "error": "geetest timeout"}
                ) + "\n"
            )
            r = self._run(provider, runtime, stdout=stdout, rc=1)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "captcha")



class TestExtractOtpCode(unittest.TestCase):
    """xAI OTP is alnum+dash XXX-XXX (e.g. FN8-ECQ); OpenAI is 6 digits in
    "verification code" context. CSS hex (#333333/#888888) embedded in xAI
    HTML <style> must never win. Regression for pxed 2026-07-18 form-fill
    of 333333 / SPA hang at /sign-up."""

    def _xai_html(self, code: str = "FN8-ECQ") -> str:
        # Real xAI confirmation-code email shape: visible code in body, CSS hex
        # colors in a <style> block (the thing the old \b(\d{4,8})\b seized).
        return f"""<!DOCTYPE html><html><head><style>
body {{ background:#ffffff; color:#333333; }}
a {{ color:#888888; }}
.code {{ font-family:monospace; }}
</style></head>
<body>
<p>Please use the code below to confirm your xAI account.</p>
<div class="code"><strong>{code}</strong></div>
<p>If you did not request this, ignore this email.</p>
</body></html>"""

    def test_xai_subject_code_wins(self):
        from register_core.email.sources.tinyhost import extract_otp_code

        blob = self._xai_html("FN8-ECQ")
        self.assertEqual(extract_otp_code(blob, subject="FN8-ECQ xAI confirmation code"), "FN8-ECQ")

    def test_xai_body_code_not_css_hex(self):
        from register_core.email.sources.tinyhost import extract_otp_code

        # The whole point: real code FN8-ECQ, NOT 333333/888888 from CSS.
        blob = self._xai_html("FN8-ECQ")
        code = extract_otp_code(blob, subject="xAI confirmation code")
        self.assertEqual(code, "FN8-ECQ")
        self.assertNotIn("333333", code)
        self.assertNotIn("888888", code)

    def test_openai_contextual_6_digit(self):
        from register_core.email.sources.tinyhost import extract_otp_code

        blob = "Enter this temporary verification code to continue: 042902"
        self.assertEqual(extract_otp_code(blob, subject="Your OpenAI verification code"), "042902")

    def test_openai_no_css_hex_false_positive(self):
        from register_core.email.sources.tinyhost import extract_otp_code

        # OpenAI-style body that happens to ship CSS hex but with NO real OTP
        # context must NOT fabricate 333333. (Empty stub subject vs an html
        # mailer header.) extract_otp_code should return "" here.
        blob = "<style>.x{color:#333333}</style> hi welcome <style>a{color:#888888}</style>"
        self.assertEqual(extract_otp_code(blob, subject="Welcome to xAI"), "")

    def test_empty_returns_empty(self):
        from register_core.email.sources.tinyhost import extract_otp_code

        self.assertEqual(extract_otp_code(""), "")



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
