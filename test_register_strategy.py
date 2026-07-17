#!/usr/bin/env python3
"""Unit tests for StrategyEngine + BurnStore (M2)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from register_core.contracts import RegisterResult
from register_core.pipeline import Pipeline
from register_core.strategy.burn import BurnStore
from register_core.strategy.engine import (
    StrategyEngine,
    domain_from_email,
    extract_egress_ip,
    proxy_host_key,
)


class FakeProvider:
    name = "fake"

    def __init__(self, outcomes: list[RegisterResult] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls = 0

    def register_one(self, *, email_source=None, extra=None) -> RegisterResult:
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return RegisterResult(
            ok=True,
            provider=self.name,
            email="a@example.com",
            secret="sk-" + "a" * 40,
            secret_kind="api_key",
        )


class TestBurnStore(unittest.TestCase):
    def test_hard_burn_ip_domain_proxy(self) -> None:
        store = BurnStore("")
        store.burn_ip("1.2.3.4", reason="registration_disallowed", email="u@x.com")
        store.burn_domain("bad.example", reason="unsupported_email")
        store.burn_proxy("127.0.0.1:7897", reason="registration_disallowed")
        self.assertTrue(store.is_ip_burned("1.2.3.4"))
        self.assertTrue(store.is_domain_burned("BAD.example"))
        self.assertTrue(store.is_proxy_burned("127.0.0.1:7897"))
        self.assertFalse(store.is_ip_cooling("1.2.3.4"))

    def test_soft_cool_ip(self) -> None:
        store = BurnStore("")
        store.cool_ip("9.9.9.9", 30.0, reason="soft")
        self.assertFalse(store.is_ip_burned("9.9.9.9"))
        self.assertTrue(store.is_ip_cooling("9.9.9.9"))

    def test_persist_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "burn.json")
            a = BurnStore(path)
            a.burn_domain("burned.tld", reason="unsupported_email", email="x@burned.tld")
            a.burn_ip("8.8.8.8", reason="registration_disallowed")
            b = BurnStore(path)
            self.assertTrue(b.is_domain_burned("burned.tld"))
            self.assertTrue(b.is_ip_burned("8.8.8.8"))
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertIn("domains", data)
            self.assertIn("burned.tld", data["domains"])


class TestStrategyHelpers(unittest.TestCase):
    def test_domain_from_email(self) -> None:
        self.assertEqual(domain_from_email("U@Example.COM"), "example.com")
        self.assertEqual(domain_from_email("nope"), "")

    def test_proxy_host_key(self) -> None:
        self.assertEqual(
            proxy_host_key("http://user:pass@127.0.0.1:7897"),
            "127.0.0.1:7897",
        )
        self.assertEqual(proxy_host_key("socks5://10.0.0.2:1080"), "10.0.0.2:1080")

    def test_extract_egress_ip(self) -> None:
        self.assertEqual(
            extract_egress_ip({"_egress_ip": "1.1.1.1"}),
            "1.1.1.1",
        )
        r = RegisterResult(
            ok=False,
            provider="x",
            error="x",
            artifacts={"egress_ip": "2.2.2.2"},
        )
        self.assertEqual(extract_egress_ip({}, r), "2.2.2.2")


class TestStrategyEngine(unittest.TestCase):
    def test_from_extra_defaults(self) -> None:
        eng = StrategyEngine.from_extra({})
        self.assertTrue(eng.fail_fast)
        self.assertIn("registration_disallowed", eng.fail_fast_kinds)
        self.assertIn("domain", eng.burn_track)

    def test_from_extra_profile_block(self) -> None:
        eng = StrategyEngine.from_extra(
            {
                "_strategy": {
                    "fail_fast": True,
                    "fail_fast_kinds": ["fatal", "verify"],
                    "burn": {
                        "enabled": True,
                        "track": ["ip", "domain", "proxy"],
                        "on_kinds": ["registration_disallowed"],
                        "state_path": "",
                    },
                    "cool_soft_seconds": 12,
                }
            }
        )
        self.assertEqual(eng.fail_fast_kinds, {"fatal", "verify"})
        self.assertEqual(eng.burn_track, {"ip", "domain", "proxy"})
        self.assertEqual(eng.cool_soft_seconds, 12.0)

    def test_should_stop_on_fail_fast_kind(self) -> None:
        eng = StrategyEngine(fail_fast=True, fail_fast_kinds=["registration_disallowed"])
        stop, reason = eng.should_stop_on_result(
            RegisterResult(
                ok=False,
                provider="chatgpt",
                error="blocked",
                error_kind="registration_disallowed",
            )
        )
        self.assertTrue(stop)
        self.assertIn("registration_disallowed", reason)

    def test_on_result_burns_domain_and_ip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "s.json")
            eng = StrategyEngine(
                burn_enabled=True,
                burn_track=["ip", "domain"],
                burn_on_kinds=["registration_disallowed"],
                burn_state_path=path,
            )
            r = RegisterResult(
                ok=False,
                provider="chatgpt",
                email="u@blocked.example",
                error="nope",
                error_kind="registration_disallowed",
            )
            fb = eng.on_result(r, {"_egress_ip": "3.3.3.3"})
            self.assertEqual(fb.action, "burned")
            self.assertEqual(fb.burned_domain, "blocked.example")
            self.assertEqual(fb.burned_ip, "3.3.3.3")
            self.assertTrue(eng.store.is_domain_burned("blocked.example"))
            self.assertTrue(eng.store.is_ip_burned("3.3.3.3"))
            self.assertIn("strategy_burn", r.artifacts or {})

    def test_soft_cool_when_ip_not_in_track(self) -> None:
        eng = StrategyEngine(
            burn_enabled=True,
            burn_track=["domain"],
            burn_on_kinds=["registration_disallowed"],
            cool_soft_seconds=60,
        )
        r = RegisterResult(
            ok=False,
            provider="chatgpt",
            email="u@x.com",
            error_kind="registration_disallowed",
        )
        fb = eng.on_result(r, {"_egress_ip": "4.4.4.4"})
        self.assertEqual(fb.cooled_ip, "4.4.4.4")
        self.assertTrue(eng.store.is_ip_cooling("4.4.4.4"))
        self.assertFalse(eng.store.is_ip_burned("4.4.4.4"))

    def test_precheck_egress_stops_on_burned_ip(self) -> None:
        eng = StrategyEngine(burn_state_path="")
        eng.store.burn_ip("5.5.5.5", reason="registration_disallowed")
        fb = eng.precheck_egress({"_egress_ip": "5.5.5.5"})
        self.assertTrue(fb.should_stop)
        self.assertIn("ip burned", fb.stop_reason)

    def test_precheck_proxy_burned(self) -> None:
        eng = StrategyEngine(burn_track=["proxy"])
        eng.store.burn_proxy("10.0.0.1:8080", reason="registration_disallowed")
        fb = eng.precheck_egress(
            {"proxy": "http://user:x@10.0.0.1:8080"},
            proxy="http://user:x@10.0.0.1:8080",
        )
        self.assertTrue(fb.should_stop)
        self.assertIn("proxy burned", fb.stop_reason)

    def test_no_burn_on_unrelated_kind(self) -> None:
        eng = StrategyEngine(
            burn_on_kinds=["registration_disallowed"],
            burn_track=["domain"],
        )
        r = RegisterResult(
            ok=False,
            provider="chatgpt",
            email="u@x.com",
            error_kind="mail_miss",
        )
        fb = eng.on_result(r, {})
        self.assertEqual(fb.action, "no_burn_kind")
        self.assertFalse(eng.store.is_domain_burned("x.com"))


class TestPipelineStrategyIntegration(unittest.TestCase):
    def test_pipeline_stops_on_strategy_fail_fast_kind(self) -> None:
        p = FakeProvider(
            outcomes=[
                RegisterResult(
                    ok=False,
                    provider="fake",
                    email="u@blocked.tld",
                    error="disallowed",
                    error_kind="registration_disallowed",
                ),
                RegisterResult(
                    ok=True,
                    provider="fake",
                    email="ok@x.com",
                    secret="sk-" + "b" * 40,
                    secret_kind="api_key",
                ),
            ]
        )
        eng = StrategyEngine(
            fail_fast=True,
            fail_fast_kinds=["registration_disallowed"],
            burn_enabled=True,
            burn_track=["domain"],
            burn_on_kinds=["registration_disallowed"],
        )
        pipe = Pipeline(p, fail_fast=False, strategy=eng, verifier=None)
        with (
            patch(
                "register_core.util.proxy.preflight_nodes_for_register",
                side_effect=lambda extra, **kw: {
                    **(extra or {}),
                    "_nodes_preflight_done": True,
                    "_nodes_preflight": {"skipped": True},
                },
            ),
            patch(
                "register_core.util.proxy.inject_attempt_proxy",
                side_effect=lambda extra, **kw: {
                    **(extra or {}),
                    "_egress_ip": "7.7.7.7",
                },
            ),
            patch(
                "register_core.util.proxy.report_attempt_proxy_result",
                return_value=None,
            ),
        ):
            stats = pipe.run(2, extra={"egress": "direct", "nodes_preflight": False})
        self.assertEqual(stats.ok, 0)
        self.assertEqual(stats.fail, 1)
        self.assertEqual(p.calls, 1)
        self.assertIn("registration_disallowed", stats.stopped_reason or "")
        self.assertTrue(eng.store.is_domain_burned("blocked.tld"))

    def test_pipeline_precheck_stops_before_register(self) -> None:
        p = FakeProvider()
        eng = StrategyEngine()
        eng.store.burn_ip("6.6.6.6", reason="registration_disallowed")
        pipe = Pipeline(p, fail_fast=True, strategy=eng, verifier=None)
        with (
            patch(
                "register_core.util.proxy.preflight_nodes_for_register",
                side_effect=lambda extra, **kw: {
                    **(extra or {}),
                    "_nodes_preflight_done": True,
                    "_nodes_preflight": {"skipped": True},
                },
            ),
            patch(
                "register_core.util.proxy.inject_attempt_proxy",
                side_effect=lambda extra, **kw: {
                    **(extra or {}),
                    "_egress_ip": "6.6.6.6",
                },
            ),
        ):
            stats = pipe.run(1, extra={"egress": "direct", "nodes_preflight": False})
        self.assertEqual(p.calls, 0)
        self.assertEqual(stats.fail, 1)
        self.assertIn("ip burned", stats.stopped_reason or "")


class TestMailInject(unittest.TestCase):
    def test_prepare_mail_inject_sets_fixed_and_helper(self) -> None:
        from register_core.contracts import Mailbox
        from register_core.util.mail_inject import prepare_mail_inject

        class Src:
            name = "tinyhost"

            def allocate(self):
                return Mailbox(
                    address="fixed@publicvm.com",
                    token="tok",
                    provider="tinyhost",
                )

        env: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as td:
            mb = prepare_mail_inject(
                Src(),  # type: ignore[arg-type]
                env,
                timeout_s=30,
                sender_hint="xai",
                force_helper=True,
                work_dir=td,
            )
            self.assertIsNotNone(mb)
            assert mb is not None
            self.assertEqual(env["FIXED_EMAIL"], "fixed@publicvm.com")
            self.assertEqual(env["EMAIL_PROVIDER"], "fixed")
            self.assertTrue(env.get("OTP_HELPER"))
            self.assertTrue(Path(env["OTP_HELPER"]).is_file())
            self.assertTrue(env.get("REGISTER_OTP_SPEC_PATH"))
            self.assertTrue(Path(env["REGISTER_OTP_SPEC_PATH"]).is_file())


if __name__ == "__main__":
    unittest.main()
