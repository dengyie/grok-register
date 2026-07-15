#!/usr/bin/env python3
"""Offline tests for project-owned node catalog (no live network required)."""

from __future__ import annotations

import json
import os
import tempfile
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
            }
            with patch.dict(os.environ, env_clear, clear=False):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                cfg = core_proxy.rotation_config_from_env_and_extra({})
                self.assertEqual(cfg["proxy_rotate_mode"], "list")
                self.assertIn("http://from-nodes:9", cfg["proxy_list"])
                self.assertTrue(cfg.get("nodes_pool"))

                proxies = []
                for _ in range(2):
                    p, info = core_proxy.resolve_attempt_proxy({})
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
                },
                clear=False,
            ):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                pipe = Pipeline(Stub(), verifier=NoopVerifier(), fail_fast=False)
                pipe.run(1, extra={})
        self.assertEqual(seen, ["http://pipe-node:1"])


if __name__ == "__main__":
    unittest.main()
