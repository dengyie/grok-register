#!/usr/bin/env python3
"""Mail path must never inherit register egress proxy (pipeline + adapter)."""

from __future__ import annotations

import os
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from register_core.contracts import OtpWaitDiagnostics, RegisterJob, RegisterResult
from register_core.email.mail_proxy import resolve_mail_proxy
from register_core.errors import MailMissError
from register_core.pipeline import Pipeline
from register_core.providers.chatgpt_adapter import ChatGPTProvider
from register_core.providers.chatgpt_adapter import resolve_mail_proxy as adapter_resolve


class TestMailProxySeparation(unittest.TestCase):
    def test_resolve_mail_proxy_never_falls_back_to_register(self) -> None:
        self.assertEqual(resolve_mail_proxy({"proxy": "http://reg:1"}), "")
        self.assertEqual(
            resolve_mail_proxy({"proxy": "http://reg:1", "mail_proxy": "http://mail:2"}),
            "http://mail:2",
        )
        # re-export from adapter stays in sync
        self.assertIs(adapter_resolve, resolve_mail_proxy)

    def test_env_mail_proxy(self) -> None:
        with patch.dict(
            os.environ,
            {"EMAIL_PROXY": "http://env-mail:3", "CHATGPT_MAIL_PROXY": "", "MAIL_PROXY": ""},
            clear=False,
        ):
            self.assertEqual(resolve_mail_proxy({}), "http://env-mail:3")

    def test_adapter_constructs_source_with_mail_proxy_only(self) -> None:
        captured: dict = {}

        def fake_get(src_name, **kw):
            captured["name"] = src_name
            captured["kw"] = kw

            class Src:
                def allocate(self):
                    raise RuntimeError("stop-before-network")

            src = Src()
            src.name = src_name  # type: ignore[attr-defined]
            return src

        prov = ChatGPTProvider(proxy="http://register-egress:8080", email_source_name="tinyhost")
        with patch(
            "register_core.providers.chatgpt_adapter.get_email_source",
            side_effect=fake_get,
        ):
            try:
                prov.register_one(extra={"proxy": "http://register-egress:8080"})
            except Exception:
                pass
        self.assertIn("kw", captured)
        # default: no register proxy on mail path
        self.assertIn(captured["kw"].get("proxy"), (None, ""))

    def test_adapter_applies_mail_proxy_to_injected_source(self) -> None:
        class Src:
            name = "tinyhost"
            proxy = ""

            def allocate(self):
                raise RuntimeError("stop-before-network")

        src = Src()
        prov = ChatGPTProvider(email_source_name="tinyhost")
        try:
            prov.register_one(
                email_source=src,  # type: ignore[arg-type]
                extra={"mail_proxy": "http://mail-only:9"},
            )
        except Exception:
            pass
        self.assertEqual(src.proxy, "http://mail-only:9")

    def test_pipeline_resolve_email_source_passes_mail_proxy(self) -> None:
        captured: dict = {}

        def fake_get(src_name, **kw):
            captured["name"] = src_name
            captured["kw"] = kw
            src = type("Src", (), {})()
            src.name = src_name
            return src

        job = RegisterJob(
            provider="chatgpt",
            email_source="tinyhost",
            extra={"proxy": "http://reg:1", "mail_proxy": "http://mail:2"},
        )
        with patch("register_core.pipeline.get_email_source", side_effect=fake_get):
            src = Pipeline._resolve_email_source(job)
        self.assertIsNotNone(src)
        self.assertEqual(captured["name"], "tinyhost")
        self.assertEqual(captured["kw"].get("proxy"), "http://mail:2")

    def test_pipeline_resolve_email_source_explicit_direct(self) -> None:
        captured: dict = {}

        def fake_get(src_name, **kw):
            captured["kw"] = kw
            src = type("Src", (), {})()
            src.name = src_name
            return src

        job = RegisterJob(
            provider="chatgpt",
            email_source="tinyhost",
            extra={"proxy": "http://reg:1"},
        )
        with patch("register_core.pipeline.get_email_source", side_effect=fake_get):
            Pipeline._resolve_email_source(job)
        self.assertIn(captured["kw"].get("proxy"), (None, ""))

    def test_pipeline_bare_mail_miss_attaches_otp_wait_and_feedback(self) -> None:
        diag = OtpWaitDiagnostics(
            timeout_s=30.0,
            provider="tinyhost",
            failure_class="no_mail",
            poll_count=3,
        )

        class RaisingProv:
            name = "chatgpt"

            def register_one(self, **_kw):
                raise MailMissError("otp timeout", diagnostics=diag)

        feedback_calls: list[RegisterResult] = []

        def _noop_preflight(extra, **_kw):
            base = dict(extra or {})
            base["_nodes_preflight_done"] = True
            base["_nodes_preflight"] = {"skipped": True, "reason": "test"}
            return base

        def _noop_inject(extra, **_kw):
            return dict(extra or {})

        pipe = Pipeline(RaisingProv(), fail_fast=True)  # type: ignore[arg-type]
        with (
            patch(
                "register_core.util.proxy.preflight_nodes_for_register",
                side_effect=_noop_preflight,
            ),
            patch(
                "register_core.util.proxy.inject_attempt_proxy",
                side_effect=_noop_inject,
            ),
            patch.object(
                pipe,
                "_feedback_proxy",
                side_effect=lambda _extra, result: feedback_calls.append(result),
            ),
        ):
            stats = pipe.run(1, extra={"proxy": "http://reg:1", "nodes_preflight": False})

        self.assertEqual(stats.fail, 1)
        self.assertEqual(len(stats.results), 1)
        r = stats.results[0]
        self.assertFalse(r.ok)
        self.assertEqual(r.error_kind, "mail_miss")
        self.assertIn("otp_wait", r.artifacts)
        self.assertEqual(r.artifacts["otp_wait"]["failure_class"], "no_mail")
        self.assertEqual(r.artifacts["otp_wait"]["poll_count"], 3)
        self.assertEqual(len(feedback_calls), 1)
        self.assertEqual(feedback_calls[0].error_kind, "mail_miss")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
