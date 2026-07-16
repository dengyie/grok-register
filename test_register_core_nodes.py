#!/usr/bin/env python3
"""Offline tests for project-owned node catalog (no live network required)."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from register_core.nodes.catalog import load_nodes, save_nodes
from register_core.nodes.manager import NodeManager, reset_manager_for_tests
from register_core.nodes.models import Node, node_from_dict
from register_core.util import proxy as core_proxy


class TestNodeModels(unittest.TestCase):
    def test_from_url_string(self) -> None:
        n = node_from_dict("http://u:p@1.2.3.4:8080")
        assert n is not None
        self.assertTrue(n.url.startswith("http://"))
        self.assertNotIn("p@", n.label)  # credentials redacted in label
        self.assertIn("1.2.3.4", n.label)

    def test_from_dict(self) -> None:
        n = node_from_dict(
            {"url": "http://a:1", "id": "n1", "label": "lab", "tags": "us,res", "enabled": "true"}
        )
        assert n is not None
        self.assertEqual(n.id, "n1")
        self.assertEqual(n.tags, ["us", "res"])
        self.assertTrue(n.enabled)


class TestCatalog(unittest.TestCase):
    def test_json_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            nodes = [
                Node(url="http://a:1", id="a", label="a"),
                Node(url="http://b:2", id="b", label="b", enabled=False),
            ]
            save_nodes(nodes, path)
            loaded = load_nodes(path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0].url, "http://a:1")
            self.assertFalse(loaded[1].enabled)

    def test_txt_load(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.txt"
            path.write_text(
                "# comment\nhttp://a:1\nhttp://b:2,http://c:3\n",
                encoding="utf-8",
            )
            loaded = load_nodes(path)
            self.assertEqual([n.url for n in loaded], ["http://a:1", "http://b:2", "http://c:3"])


class TestManager(unittest.TestCase):
    def setUp(self) -> None:
        reset_manager_for_tests()
        core_proxy.reset_rotation_for_tests()

    def tearDown(self) -> None:
        reset_manager_for_tests()
        core_proxy.reset_rotation_for_tests()

    def test_pick_round_robin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://n1:1", id="n1"),
                    Node(url="http://n2:2", id="n2"),
                ],
                path,
            )
            mgr = NodeManager(path)
            a = mgr.pick()
            b = mgr.pick()
            c = mgr.pick()
            assert a and b and c
            self.assertEqual(a.url, "http://n1:1")
            self.assertEqual(b.url, "http://n2:2")
            self.assertEqual(c.url, "http://n1:1")

    def test_proxy_util_loads_nodes_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://from-nodes:9", id="x"),
                    Node(url="http://from-nodes-2:9", id="y"),
                ],
                path,
            )
            # Isolate env so only nodes catalog supplies the pool.
            env_clear = {
                "CHATGPT_PROXY_LIST": "",
                "PROXY_LIST": "",
                "PROXY_POOL": "",
                "CHATGPT_PROXY": "",
                "MIMO_PROXY": "",
                "https_proxy": "",
                "HTTPS_PROXY": "",
                "http_proxy": "",
                "HTTP_PROXY": "",
                "PROXY_ROTATE_MODE": "",
                "CHATGPT_PROXY_ROTATE_MODE": "",
                "REGISTER_NODES_FILE": str(path),
                "REGISTER_NODES": "1",
                "REGISTER_EGRESS": "list",
                "REGISTER_CORE": "0",
                "REGISTER_NODES_PREFLIGHT": "0",
            }
            with patch.dict(os.environ, env_clear, clear=False):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                # explicit list backend uses full catalog (including unprobed)
                cfg = core_proxy.rotation_config_from_env_and_extra({"egress": "list"})
                self.assertEqual(cfg["proxy_rotate_mode"], "list")
                self.assertIn("http://from-nodes:9", cfg["proxy_list"])
                self.assertTrue(cfg.get("nodes_pool"))

                proxies = []
                for _ in range(2):
                    p, info = core_proxy.resolve_attempt_proxy({"egress": "list"})
                    proxies.append(p)
                    self.assertEqual(info.get("mode"), "list")
                self.assertEqual(proxies[0], "http://from-nodes:9")
                self.assertEqual(proxies[1], "http://from-nodes-2:9")

    def test_pipeline_uses_nodes_file(self) -> None:
        from register_core.contracts import RegisterResult
        from register_core.pipeline import Pipeline
        from register_core.verify.noop import NoopVerifier

        seen: list[str] = []

        class Stub:
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

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes([Node(url="http://pipe-node:1", id="p")], path)
            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "PROXY_LIST": "",
                    "CHATGPT_PROXY_LIST": "",
                    "PROXY_ROTATE_MODE": "",
                    "CHATGPT_PROXY": "",
                    "REGISTER_NODES": "1",
                    "REGISTER_EGRESS": "list",
                    "REGISTER_CORE": "0",
                    "REGISTER_NODES_PREFLIGHT": "0",
                },
                clear=False,
            ):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                pipe = Pipeline(Stub(), verifier=NoopVerifier(), fail_fast=False)
                pipe.run(1, extra={"egress": "list", "nodes_preflight": False})
        self.assertEqual(seen, ["http://pipe-node:1"])

    def test_mark_result_quarantines_after_max_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://good:1", id="g", last_ok=True),
                    Node(url="http://bad:1", id="b", last_ok=True),
                ],
                path,
            )
            with patch.dict(
                os.environ,
                {"REGISTER_NODES_MAX_FAIL": "2", "REGISTER_NODES_SKIP_FAILED": "1"},
                clear=False,
            ):
                mgr = NodeManager(path)
                mgr.mark_result("http://bad:1", ok=False, error="timeout")
                self.assertEqual(len(mgr.enabled_nodes(healthy_only=True)), 1)
                mgr.mark_result("http://bad:1", ok=False, error="timeout")
                bad = mgr.find_by_url("http://bad:1")
                assert bad is not None
                self.assertTrue(mgr._is_quarantined(bad))
                urls = mgr.urls(healthy_only=False)
                self.assertNotIn("http://bad:1", urls)
                self.assertIn("http://good:1", urls)

    def test_preflight_seeds_healthy_only_pool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://alive:1", id="a"),
                    Node(url="http://dead:1", id="d"),
                ],
                path,
            )

            def fake_probe(node, **kwargs):
                ok = node.url.endswith("alive:1")
                node.last_ok = ok
                node.fail_count = 0 if ok else 1
                node.last_error = "" if ok else "down"
                return {"id": node.id, "label": node.label, "ok": ok, "error": node.last_error}

            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES": "1",
                    "REGISTER_EGRESS": "list",
                    "REGISTER_CORE": "0",
                    "PROXY_LIST": "",
                    "CHATGPT_PROXY_LIST": "",
                    "REGISTER_NODES_PREFLIGHT": "1",
                },
                clear=False,
            ), patch("register_core.nodes.manager.probe_node", side_effect=fake_probe):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                extra = core_proxy.preflight_nodes_for_register({"egress": "list"})
                self.assertFalse(extra["_nodes_preflight"].get("skipped"))
                self.assertEqual(extra["_nodes_preflight"]["healthy"], 1)
                self.assertEqual(extra.get("proxy_list"), "http://alive:1")
                p, info = core_proxy.resolve_attempt_proxy(extra)
                self.assertEqual(p, "http://alive:1")
                self.assertEqual(info.get("mode"), "list")

    def test_preflight_zero_healthy_fail_fast_on_list(self) -> None:
        from register_core.errors import FailFastError

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes([Node(url="http://dead:1", id="d")], path)

            def fake_probe(node, **kwargs):
                node.last_ok = False
                node.fail_count = 1
                node.last_error = "down"
                return {"id": node.id, "label": node.label, "ok": False, "error": "down"}

            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES": "1",
                    "REGISTER_EGRESS": "list",
                    "REGISTER_CORE": "0",
                    "PROXY_LIST": "",
                    "REGISTER_NODES_PREFLIGHT": "1",
                },
                clear=False,
            ), patch("register_core.nodes.manager.probe_node", side_effect=fake_probe):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                with self.assertRaises(FailFastError):
                    core_proxy.preflight_nodes_for_register({"egress": "list"})

    def test_report_attempt_drops_dead_proxy_from_pool(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://n1:1", id="n1", last_ok=True),
                    Node(url="http://n2:2", id="n2", last_ok=True),
                ],
                path,
            )
            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES": "1",
                    "REGISTER_EGRESS": "list",
                    "REGISTER_CORE": "0",
                    "REGISTER_NODES_MAX_FAIL": "1",
                    "PROXY_LIST": "",
                    "REGISTER_NODES_PREFLIGHT": "0",
                },
                clear=False,
            ):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                extra = {
                    "egress": "list",
                    "proxy_list": "http://n1:1,http://n2:2",
                    "proxy_rotate_mode": "list",
                    "proxy_rotate_on_start": True,
                    "nodes_preflight": False,
                }
                p1, _ = core_proxy.resolve_attempt_proxy(extra)
                self.assertEqual(p1, "http://n1:1")
                info = core_proxy.report_attempt_proxy_result(
                    {"proxy": "http://n1:1", "egress": "list", "proxy_list": "http://n1:1,http://n2:2"},
                    ok=False,
                    error="connection timeout via proxy",
                    error_kind="other",
                )
                self.assertTrue(info.get("marked"))
                self.assertTrue(info.get("removed_from_pool") or info.get("quarantined"))
                # next resolve should not stick on dead n1 if pool rebuilt
                p2, _ = core_proxy.resolve_attempt_proxy(
                    {
                        "egress": "list",
                        "proxy_list": "http://n2:2",
                        "proxy_rotate_mode": "list",
                        "proxy_rotate_on_start": True,
                    }
                )
                self.assertEqual(p2, "http://n2:2")

    def test_pipeline_preflight_and_skip_dead_node(self) -> None:
        from register_core.contracts import RegisterResult
        from register_core.pipeline import Pipeline
        from register_core.verify.noop import NoopVerifier

        seen: list[str] = []

        class Stub:
            name = "stub"

            def register_one(self, *, email_source=None, extra=None):
                proxy = str((extra or {}).get("proxy") or "")
                seen.append(proxy)
                if "dead" in proxy:
                    return RegisterResult(
                        ok=False,
                        provider=self.name,
                        error="connection refused proxy",
                        error_kind="other",
                        secret_kind="none",
                    )
                return RegisterResult(
                    ok=False,
                    provider=self.name,
                    error="registration_disallowed",
                    error_kind="provider",
                    secret_kind="none",
                )

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://dead:1", id="dead"),
                    Node(url="http://alive:2", id="alive"),
                ],
                path,
            )

            def fake_probe(node, **kwargs):
                ok = "alive" in node.url
                node.last_ok = ok
                node.fail_count = 0 if ok else 3
                node.last_error = "" if ok else "down"
                return {"id": node.id, "label": node.label, "ok": ok, "error": node.last_error}

            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES": "1",
                    "REGISTER_EGRESS": "list",
                    "REGISTER_CORE": "0",
                    "PROXY_LIST": "",
                    "CHATGPT_PROXY_LIST": "",
                    "REGISTER_NODES_PREFLIGHT": "1",
                    "REGISTER_NODES_MAX_FAIL": "1",
                },
                clear=False,
            ), patch("register_core.nodes.manager.probe_node", side_effect=fake_probe):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                pipe = Pipeline(Stub(), verifier=NoopVerifier(), fail_fast=False)
                stats = pipe.run(2, extra={"egress": "list"})
        self.assertTrue(all("alive" in p for p in seen))
        self.assertFalse(any("dead" in p for p in seen))
        self.assertEqual(stats.nodes_preflight.get("healthy"), 1)

    def test_cooldown_skips_pick_until_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://cool:1", id="c"),
                    Node(url="http://hot:2", id="h"),
                ],
                path,
            )
            mgr = NodeManager(path)
            n = mgr.cooldown("http://cool:1", seconds=600, reason="registration_disallowed")
            self.assertIsNotNone(n)
            assert n is not None
            self.assertTrue(mgr.is_cooling(n))
            picked = {mgr.pick().url for _ in range(4)}  # type: ignore[union-attr]
            self.assertEqual(picked, {"http://hot:2"})
            # force expire
            for node in mgr.nodes:
                if node.url == "http://cool:1":
                    node.cooldown_until = time.time() - 1
            urls = {n.url for n in mgr.enabled_nodes()}
            self.assertIn("http://cool:1", urls)

    def test_mail_miss_mark_does_not_require_cooldown_api(self) -> None:
        # structural: is_cooling false by default
        n = Node(url="http://x:1")
        mgr = NodeManager.__new__(NodeManager)
        mgr._skip_failed = True
        mgr._max_fail = 3
        self.assertFalse(NodeManager.is_cooling(mgr, n))


if __name__ == "__main__":
    unittest.main()
