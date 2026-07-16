#!/usr/bin/env python3
"""Offline tests: egress switch + self-controlled proxy list for register_core."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from register_core.util import proxy as core_proxy
from register_core.util import egress as egress_mod


_ENV_KEYS = (
    "CHATGPT_PROXY_LIST",
    "PROXY_LIST",
    "PROXY_POOL",
    "CHATGPT_PROXY_ROTATE_MODE",
    "PROXY_ROTATE_MODE",
    "CHATGPT_PROXY",
    "MIMO_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "http_proxy",
    "HTTP_PROXY",
    "CHATGPT_PROXY_ROTATE_EVERY",
    "PROXY_ROTATE_EVERY",
    "REGISTER_EGRESS",
    "EGRESS_BACKEND",
    "CHATGPT_EGRESS",
    "PROXY_BACKEND",
    "REGISTER_PROXY_BACKEND",
    "REGISTER_CORE",
    "USE_CORE",
    "REGISTER_MIHOMO",
    "REGISTER_NODES",
    "USE_NODES",
    "REGISTER_NODES_FILE",
    "NODES_FILE",
    "CLASH_PROXY",
    "CLASH_API",
    "CLASH_CONTROLLER",
    "REGISTER_CORE_AUTOSTART",
    "USE_CLASH",
)


class TestRotationConfig(unittest.TestCase):
    def setUp(self) -> None:
        core_proxy.reset_rotation_for_tests()
        self._env_backup = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
        # prevent real nodes.json / core from leaking into unit tests
        os.environ["REGISTER_NODES"] = "0"
        os.environ["REGISTER_CORE"] = "0"
        os.environ["REGISTER_EGRESS"] = "auto"

    def tearDown(self) -> None:
        core_proxy.reset_rotation_for_tests()
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_proxy_list_auto_selects_list_mode(self) -> None:
        cfg = core_proxy.rotation_config_from_env_and_extra(
            {"proxy_list": "http://a:1,http://b:2"}
        )
        self.assertEqual(cfg["proxy_rotate_mode"], "list")
        self.assertEqual(cfg["proxy_list"], "http://a:1,http://b:2")
        self.assertEqual(cfg.get("egress_source"), "list")

    def test_env_proxy_list_auto_list(self) -> None:
        os.environ["PROXY_LIST"] = "http://a:1\nhttp://b:2"
        cfg = core_proxy.rotation_config_from_env_and_extra({})
        self.assertEqual(cfg["proxy_rotate_mode"], "list")

    def test_explicit_off_keeps_off_even_with_list(self) -> None:
        cfg = core_proxy.rotation_config_from_env_and_extra(
            {
                "proxy_rotate_mode": "off",
                "proxy_list": "http://a:1,http://b:2",
                "proxy": "http://127.0.0.1:7897",
            }
        )
        self.assertEqual(cfg["proxy_rotate_mode"], "off")

    def test_resolve_attempt_rotates_list(self) -> None:
        proxies: list[str] = []
        extra = {
            "proxy_list": "http://a:1,http://b:2,http://c:3",
            "proxy_rotate_every": 1,
            "proxy_rotate_on_start": True,
            "egress": "list",
        }
        for _ in range(3):
            p, info = core_proxy.resolve_attempt_proxy(extra)
            proxies.append(p)
            self.assertEqual(info.get("mode"), "list")
            self.assertEqual(info.get("egress_backend"), "list")
        self.assertEqual(proxies[0], "http://a:1")
        self.assertEqual(proxies[1], "http://b:2")
        self.assertEqual(proxies[2], "http://c:3")

    def test_inject_attempt_proxy_sets_extra_proxy(self) -> None:
        extra = core_proxy.inject_attempt_proxy(
            {
                "proxy_list": "http://only:9",
                "proxy_rotate_on_start": True,
                "egress": "list",
            }
        )
        self.assertEqual(extra.get("proxy"), "http://only:9")
        self.assertEqual(extra.get("_proxy_rotate", {}).get("mode"), "list")

    def test_pipeline_passes_rotated_proxy_to_provider(self) -> None:
        from register_core.contracts import RegisterResult
        from register_core.pipeline import Pipeline
        from register_core.verify.noop import NoopVerifier

        seen: list[str] = []

        class StubProvider:
            name = "stub"

            def register_one(self, *, email_source=None, extra=None):
                seen.append(str((extra or {}).get("proxy") or ""))
                return RegisterResult(
                    ok=False,
                    provider=self.name,
                    error="stop",
                    error_kind="other",
                    secret_kind="none",
                )

        pipe = Pipeline(
            StubProvider(),
            email_source=None,
            verifier=NoopVerifier(),
            fail_fast=False,
        )
        extra = {
            "proxy_list": "http://n1:1,http://n2:2",
            "proxy_rotate_every": 1,
            "proxy_rotate_on_start": True,
            "egress": "list",
        }
        stats = pipe.run(2, extra=extra)
        self.assertEqual(stats.fail, 2)
        self.assertEqual(seen, ["http://n1:1", "http://n2:2"])

    def test_egress_clash_uses_7897_not_core(self) -> None:
        os.environ["REGISTER_CORE"] = "0"
        os.environ["REGISTER_NODES"] = "0"
        cfg = core_proxy.rotation_config_from_env_and_extra({"egress": "clash"})
        self.assertEqual(cfg["egress_backend"], "clash")
        self.assertEqual(cfg["egress_source"], "clash")
        self.assertIn("7897", cfg.get("proxy") or "")
        self.assertFalse(cfg.get("core_pool"))

    def test_egress_direct_clears_proxy(self) -> None:
        os.environ["CHATGPT_PROXY"] = "http://should-not-use:1"
        p, info = core_proxy.resolve_attempt_proxy({"egress": "direct"})
        self.assertEqual(p, "")
        self.assertEqual(info.get("egress_backend"), "direct")

    def test_egress_core_require_core_url(self) -> None:
        with patch.object(
            core_proxy,
            "_load_core_proxy_url",
            return_value="http://127.0.0.1:17897",
        ):
            cfg = core_proxy.rotation_config_from_env_and_extra({"egress": "core"})
        self.assertEqual(cfg["egress_backend"], "core")
        self.assertEqual(cfg["egress_source"], "core")
        self.assertEqual(cfg["proxy"], "http://127.0.0.1:17897")
        self.assertTrue(cfg.get("core_pool"))
        # must be project port 17897, not external Clash 7897
        self.assertTrue((cfg.get("proxy") or "").endswith(":17897"))
        self.assertNotEqual(cfg.get("proxy"), "http://127.0.0.1:7897")

    def test_persist_egress_preference(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            os.environ["REGISTER_NODES_HOME"] = td
            path = egress_mod.write_persisted_backend("clash")
            self.assertTrue(Path(path).is_file())
            self.assertEqual(egress_mod.read_persisted_backend(), "clash")
            # env still wins over file
            os.environ["REGISTER_EGRESS"] = "core"
            self.assertEqual(egress_mod.resolve_backend({}), "core")
            os.environ.pop("REGISTER_EGRESS", None)
            self.assertEqual(egress_mod.resolve_backend({}), "clash")

    def test_auto_skips_unprobed_nodes_json(self) -> None:
        """Dirty bulk catalog without last_ok=true must not win over core in auto."""
        os.environ.pop("REGISTER_NODES", None)
        os.environ["REGISTER_NODES"] = "1"
        os.environ["REGISTER_CORE"] = "auto"
        os.environ["REGISTER_EGRESS"] = "auto"
        with patch.object(
            core_proxy,
            "_load_nodes_proxy_list",
            side_effect=lambda healthy_only=None: (
                "" if healthy_only is None else "http://dirty:1"
            ),
        ), patch.object(
            core_proxy,
            "_load_core_proxy_url",
            return_value="http://127.0.0.1:17897",
        ):
            cfg = core_proxy.rotation_config_from_env_and_extra({"egress": "auto"})
        self.assertEqual(cfg.get("egress_source"), "core")
        self.assertTrue((cfg.get("proxy") or "").endswith(":17897"))

    def test_auto_uses_known_healthy_list(self) -> None:
        os.environ["REGISTER_NODES"] = "1"
        os.environ["REGISTER_EGRESS"] = "auto"
        with patch.object(
            core_proxy,
            "_load_nodes_proxy_list",
            side_effect=lambda healthy_only=None: (
                "http://ok:1" if healthy_only is None else "http://ok:1"
            ),
        ), patch.object(
            core_proxy,
            "_load_core_proxy_url",
            return_value="http://127.0.0.1:17897",
        ):
            cfg = core_proxy.rotation_config_from_env_and_extra({"egress": "auto"})
        self.assertEqual(cfg.get("egress_source"), "list")
        self.assertIn("http://ok:1", cfg.get("proxy_list") or "")


class TestReportAttemptSoftCool(unittest.TestCase):
    def setUp(self) -> None:
        core_proxy.reset_rotation_for_tests()

    def tearDown(self) -> None:
        core_proxy.reset_rotation_for_tests()

    def test_registration_disallowed_soft_cools_not_quarantine(self) -> None:
        from register_core.nodes.catalog import save_nodes
        from register_core.nodes.manager import NodeManager, reset_manager_for_tests
        from register_core.nodes.models import Node

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes([Node(url="http://risk:9", id="r")], path)
            reset_manager_for_tests()
            env = {
                "REGISTER_NODES_FILE": str(path),
                "REGISTER_NODES": "1",
                "REGISTER_NODES_COOLDOWN_RISK": "600",
                "REGISTER_NODES_COOLDOWN_NETWORK": "120",
                "REGISTER_NODES_COOLDOWN_PER_USE": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                reset_manager_for_tests()
                info = core_proxy.report_attempt_proxy_result(
                    {"proxy": "http://risk:9"},
                    ok=False,
                    error="create_account registration_disallowed",
                    error_kind="registration_disallowed",
                )
                self.assertEqual(info.get("action"), "risk_cooldown")
                self.assertFalse(info.get("quarantined"))
                mgr = NodeManager(path)
                n = mgr.find_by_url("http://risk:9")
                assert n is not None
                self.assertTrue(mgr.is_cooling(n))
                self.assertEqual(n.cooldown_reason, "registration_disallowed")
                # not hard-quarantined
                self.assertFalse(mgr.is_quarantined(n))
            reset_manager_for_tests()

    def test_mail_miss_no_cool_no_quarantine(self) -> None:
        from register_core.nodes.catalog import save_nodes
        from register_core.nodes.manager import NodeManager, reset_manager_for_tests
        from register_core.nodes.models import Node

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes([Node(url="http://m:1", id="m")], path)
            env = {"REGISTER_NODES_FILE": str(path), "REGISTER_NODES": "1"}
            with patch.dict(os.environ, env, clear=False):
                reset_manager_for_tests()
                info = core_proxy.report_attempt_proxy_result(
                    {"proxy": "http://m:1"},
                    ok=False,
                    error="otp_wait",
                    error_kind="mail_miss",
                )
                self.assertEqual(info.get("reason"), "non_proxy_failure")
                mgr = NodeManager(path)
                n = mgr.find_by_url("http://m:1")
                assert n is not None
                self.assertFalse(mgr.is_cooling(n))
                self.assertEqual(int(n.fail_count or 0), 0)
            reset_manager_for_tests()


if __name__ == "__main__":
    unittest.main()
