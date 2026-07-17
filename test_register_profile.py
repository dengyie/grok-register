#!/usr/bin/env python3
"""Unit tests for register.v1 profile loader + mailbox/decode composite."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from register_core.config.loader import (
    ProfileLoadError,
    build_composite_email,
    load_profile,
    parse_profile_dict,
    profile_to_job,
    resolve_mail_proxy_value,
)
from register_core.contracts import Mailbox, OtpCode, RegisterResult
from register_core.email.composite import CompositeEmailSource
from register_core.verify.chatgpt_token import ChatGPTTokenVerifier


class TestProfileParse(unittest.TestCase):
    def test_minimal_chatgpt_cf(self):
        raw = {
            "apiVersion": "register.v1",
            "metadata": {"name": "t1"},
            "spec": {
                "provider": {"name": "chatgpt"},
                "mailbox": {"type": "cloudflare"},
                "decode": {"type": "cf"},
                "strategy": {
                    "egress": {"mode": "clash", "proxy": "http://127.0.0.1:7897"},
                    "mail_proxy": "direct",
                },
                "secrets": {"mode": "dev"},
            },
        }
        p = parse_profile_dict(raw, source_path="mem")
        self.assertEqual(p.name, "t1")
        self.assertEqual(p.provider.name, "chatgpt")
        self.assertEqual(p.mailbox_type(), "cloudflare")
        self.assertEqual(p.decode_type(), "cloudflare")
        job = profile_to_job(p)
        self.assertEqual(job.provider, "chatgpt")
        self.assertEqual(job.extra.get("egress"), "clash")
        self.assertEqual(job.extra.get("proxy"), "http://127.0.0.1:7897")
        self.assertNotIn("mail_proxy", job.extra)  # direct → empty
        self.assertEqual(job.extra["_profile"]["mailbox"], "cloudflare")

    def test_email_source_paired_fallback(self):
        raw = {
            "apiVersion": "register.v1",
            "spec": {
                "name": "legacy",
                "provider": "chatgpt",
                "email_source": "tinyhost",
                "secrets": {"mode": "dev"},
            },
        }
        p = parse_profile_dict(raw)
        self.assertEqual(p.mailbox_type(), "tinyhost")
        self.assertEqual(p.decode_type(), "tinyhost")

    def test_prod_rejects_plaintext_secret(self):
        raw = {
            "apiVersion": "register.v1",
            "spec": {
                "provider": {"name": "chatgpt"},
                "mailbox": {"type": "gmail_imap"},
                "decode": {"type": "gmail_imap"},
                "secrets": {"mode": "prod"},
                "imap_password": "hunter2",
            },
        }
        with self.assertRaises(ProfileLoadError):
            parse_profile_dict(raw, source_path="bad.yaml")

    def test_prod_allows_env_ref(self):
        raw = {
            "apiVersion": "register.v1",
            "spec": {
                "provider": {"name": "chatgpt"},
                "mailbox": {"type": "gmail_imap"},
                "decode": {"type": "gmail_imap"},
                "secrets": {"mode": "prod"},
                "imap_password": "env:GMAIL_IMAP_PASSWORD",
            },
        }
        p = parse_profile_dict(raw)
        self.assertEqual(p.secrets.mode, "prod")

    def test_load_yaml_example(self):
        root = Path(__file__).resolve().parent
        path = root / "profiles" / "chatgpt-tinyhost.example.yaml"
        if not path.is_file():
            self.skipTest("example profile missing")
        p = load_profile(path)
        self.assertEqual(p.provider.name, "chatgpt")
        self.assertEqual(p.mailbox_type(), "tinyhost")
        self.assertTrue(p.mailbox and p.mailbox.domain)


class TestMailProxyResolve(unittest.TestCase):
    def test_direct(self):
        self.assertEqual(resolve_mail_proxy_value("direct"), "")
        self.assertEqual(resolve_mail_proxy_value(""), "")

    def test_url(self):
        self.assertEqual(
            resolve_mail_proxy_value("http://127.0.0.1:9"), "http://127.0.0.1:9"
        )


class TestComposite(unittest.TestCase):
    def test_delegate_allocate_poll_release(self):
        mb = MagicMock()
        mb.name = "cloudflare"
        box = Mailbox(address="a@b.com", token="t", provider="cloudflare")
        mb.allocate.return_value = box
        dec = MagicMock()
        dec.name = "cloudflare"
        dec.wait_otp.return_value = OtpCode(code="123456", source="cloudflare")
        dec.last_wait_diagnostics = None
        comp = CompositeEmailSource(mb, dec)
        self.assertEqual(comp.name, "cloudflare")
        self.assertEqual(comp.allocate().address, "a@b.com")
        self.assertEqual(comp.poll_otp(box).code, "123456")
        comp.release(box, success=True)
        mb.release.assert_called_once_with(box, success=True)

    def test_split_name(self):
        mb = MagicMock()
        mb.name = "local"
        dec = MagicMock()
        dec.name = "gmail_imap"
        c = CompositeEmailSource(mb, dec)
        self.assertEqual(c.name, "local+gmail_imap")

    def test_proxy_forwards_live_mailbox_and_setter(self):
        """Composite must not freeze mailbox.proxy at construction."""
        mb = MagicMock()
        mb.name = "tinyhost"
        mb.proxy = "http://mail-proxy:1"
        dec = MagicMock()
        dec.name = "tinyhost"
        c = CompositeEmailSource(mb, dec)
        self.assertEqual(c.proxy, "http://mail-proxy:1")
        # live mailbox mutation must surface
        mb.proxy = "http://mail-proxy:2"
        self.assertEqual(c.proxy, "http://mail-proxy:2")
        # setter writes through
        c.proxy = "http://mail-proxy:3"
        self.assertEqual(c.proxy, "http://mail-proxy:3")
        self.assertEqual(mb.proxy, "http://mail-proxy:3")


class TestCliCountDoesNotClobberProfile(unittest.TestCase):
    def test_apply_cli_overrides_none_keeps_profile_count(self):
        from register_core.config.loader import apply_cli_overrides, parse_profile_dict

        raw = {
            "apiVersion": "register.v1",
            "spec": {
                "provider": {"name": "chatgpt"},
                "mailbox": {"type": "tinyhost"},
                "decode": {"type": "tinyhost"},
                "count": 7,
            },
        }
        profile = parse_profile_dict(raw)
        self.assertEqual(profile.count, 7)
        job, _ = apply_cli_overrides(profile, count=None)
        self.assertEqual(job.count, 7)
        job2, _ = apply_cli_overrides(profile, count=3)
        self.assertEqual(job2.count, 3)


class TestMimoProxyDefault(unittest.TestCase):
    def test_no_hardcoded_7897_without_env(self):
        from register_core.providers.mimo_adapter import MimoProvider
        import os
        from unittest.mock import patch

        env = {k: v for k, v in os.environ.items() if k not in {
            "MIMO_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"
        }}
        with patch.dict(os.environ, env, clear=True):
            p = MimoProvider()
        self.assertNotEqual(p.proxy, "http://127.0.0.1:7897")
        self.assertEqual(p.proxy, "")


class TestPipelineFromProfile(unittest.TestCase):
    def test_from_profile_chatgpt_tinyhost_offline(self):
        from register_core.pipeline import Pipeline
        from register_core.providers.chatgpt_adapter import ChatGPTProvider
        from unittest.mock import patch

        raw = {
            "apiVersion": "register.v1",
            "spec": {
                "provider": {"name": "chatgpt"},
                "mailbox": {"type": "tinyhost", "domain": "publicvm.com"},
                "decode": {"type": "tinyhost"},
                "strategy": {
                    "fail_fast": True,
                    "egress": {"mode": "direct"},
                    "mail_proxy": "direct",
                },
                "verify": {"enabled": True},
                "secrets": {"mode": "dev"},
            },
        }
        profile = parse_profile_dict(raw)

        class FakeSrc:
            name = "tinyhost"

            def allocate(self):
                return Mailbox(
                    address="u@publicvm.com", token="", provider="tinyhost"
                )

            def poll_otp(self, mailbox, **kwargs):
                return OtpCode(code="999999", source="tinyhost")

            def release(self, mailbox, *, success):
                return None

        from providers.chatgpt.protocol.flow import RegistrationResult

        fake_ok = RegistrationResult(
            ok=True,
            email="u@publicvm.com",
            password="Pw1!abcdef",
            access_token="a" * 48,
            refresh_token="rt.1." + ("B" * 160),
            id_token="h.p.s",
        )

        with tempfile.TemporaryDirectory() as td:
            with (
                patch(
                    "register_core.email.registry.get_email_source",
                    return_value=FakeSrc(),
                ),
                patch(
                    "register_core.providers.chatgpt_adapter.OUTPUT_DIR", Path(td)
                ),
                patch(
                    "providers.chatgpt.protocol.flow.register_one",
                    return_value=fake_ok,
                ),
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
                    side_effect=lambda extra, **kw: dict(extra or {}),
                ),
            ):
                pipe = Pipeline.from_profile(profile)
                self.assertIsInstance(pipe.email_source, CompositeEmailSource)
                stats = pipe.run(
                    1, extra={"egress": "direct", "nodes_preflight": False}
                )
        self.assertEqual(stats.ok, 1, stats.results[0].error if stats.results else "")
        self.assertTrue(stats.results[0].ok)
        self.assertTrue(str(stats.results[0].secret).startswith("rt.1."))

    def test_mimo_profile_accepts_composite_email(self):
        """M3: mimo profile builds CompositeEmailSource (FIXED_EMAIL inject path)."""
        from register_core.pipeline import Pipeline

        raw = {
            "apiVersion": "register.v1",
            "spec": {
                "provider": {"name": "mimo"},
                "mailbox": {"type": "tinyhost"},
                "decode": {"type": "tinyhost"},
                "strategy": {
                    "fail_fast": True,
                    "burn": {
                        "enabled": True,
                        "track": ["ip", "domain"],
                        "on_kinds": ["registration_disallowed"],
                    },
                },
                "secrets": {"mode": "dev"},
            },
        }
        profile = parse_profile_dict(raw)
        pipe = Pipeline.from_profile(profile)
        self.assertIsInstance(pipe.email_source, CompositeEmailSource)
        self.assertIsNotNone(pipe.strategy)
        self.assertIn("domain", pipe.strategy.burn_track)

    def test_grok_profile_accepts_composite_email(self):
        """M4: grok profile builds CompositeEmailSource + strategy engine."""
        from register_core.pipeline import Pipeline

        raw = {
            "apiVersion": "register.v1",
            "spec": {
                "provider": {"name": "grok"},
                "mailbox": {"type": "tinyhost", "domain": "publicvm.com"},
                "decode": {"type": "tinyhost"},
                "strategy": {
                    "fail_fast": True,
                    "fail_fast_kinds": ["fatal", "verify"],
                    "cool_soft_seconds": 5,
                },
                "secrets": {"mode": "dev"},
            },
        }
        profile = parse_profile_dict(raw)
        pipe = Pipeline.from_profile(profile)
        self.assertIsInstance(pipe.email_source, CompositeEmailSource)
        self.assertIsNotNone(pipe.strategy)
        self.assertEqual(pipe.strategy.cool_soft_seconds, 5.0)


class TestVerifierStillOk(unittest.TestCase):
    def test_rt1(self):
        v = ChatGPTTokenVerifier(live=False)
        r = v.verify(
            RegisterResult(
                ok=True,
                provider="chatgpt",
                secret="rt.1." + ("A" * 160),
                secret_kind="refresh_token",
            )
        )
        self.assertTrue(r.ok)


if __name__ == "__main__":
    unittest.main()
