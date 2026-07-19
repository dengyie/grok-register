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
from register_core.nodes.manager import (
    NodeManager,
    get_manager,
    reset_manager_for_tests,
)
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

    def test_hpup_string_normalizes_to_socks5h(self) -> None:
        n = node_from_dict(
            "us.1024proxy.io:3000:cchv57025-region-Rand-sid-pA5-t-5:tyfnvdhr"
        )
        assert n is not None
        self.assertEqual(
            n.url,
            "socks5h://cchv57025-region-Rand-sid-pA5-t-5:tyfnvdhr@us.1024proxy.io:3000",
        )
        self.assertTrue(n.url.startswith("socks5h://"))  # remote DNS resolve

    def test_dict_url_hpup_also_normalizes(self) -> None:
        n = node_from_dict(
            {"url": "us.1024proxy.io:3000:cchv-region-Rand-sid-x-t-5:pw", "tier": 1}
        )
        assert n is not None
        self.assertEqual(n.tier, 1)
        self.assertTrue(n.url.startswith("socks5h://"))

    def test_plain_host_port_not_mangled(self) -> None:
        # bare host:port (2 segments, no auth) must NOT be rewritten.
        n = node_from_dict("1.2.3.4:8080")
        assert n is not None
        self.assertEqual(n.url, "1.2.3.4:8080")
        # plain url with scheme untouched
        n2 = node_from_dict("http://u:p@1.2.3.4:8080")
        assert n2 is not None
        self.assertEqual(n2.url, "http://u:p@1.2.3.4:8080")

    def test_tier_default_zero_and_roundtrip(self) -> None:
        n = node_from_dict({"url": "http://a:1", "tier": 1})
        assert n is not None
        self.assertEqual(n.tier, 1)
        n_default = node_from_dict({"url": "http://b:2"})
        assert n_default is not None
        self.assertEqual(n_default.tier, 0)  # backward-compat default

    def test_quality_score_derivation(self) -> None:
        n = node_from_dict(
            {"url": "http://a:1", "attempt_count": 4, "success_count": 2, "disallow_count": 1}
        )
        assert n is not None
        # 2/4 - 0.5*1 = 0.0
        self.assertAlmostEqual(n.quality_score(), 0.0, places=6)
        with patch.dict(os.environ, {"REGISTER_NODES_QUALITY_LAMBDA": "1.0"}):
            # 2/4 - 1.0*1 = -0.5
            self.assertAlmostEqual(n.quality_score(), -0.5, places=6)
        # zero attempts → safe (max(1,0) denominator)
        n0 = node_from_dict({"url": "http://z:9"})
        assert n0 is not None
        self.assertEqual(n0.quality_score(), 0.0)

    def test_counts_roundtrip_through_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            n = node_from_dict(
                {"url": "http://a:1", "tier": 1, "attempt_count": 7, "success_count": 5, "disallow_count": 2}
            )
            assert n is not None
            save_nodes([n], path)
            loaded = load_nodes(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].tier, 1)
            self.assertEqual(loaded[0].attempt_count, 7)
            self.assertEqual(loaded[0].success_count, 5)
            self.assertEqual(loaded[0].disallow_count, 2)

    def test_old_store_without_counts_loads_compat(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            # hand-write a legacy stanza lacking tier/counts → defaults apply
            path.write_text('[{"url":"http://a:1","id":"a","label":"a","enabled":true}]', encoding="utf-8")
            loaded = load_nodes(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].tier, 0)
            self.assertEqual(loaded[0].attempt_count, 0)
            self.assertEqual(loaded[0].success_count, 0)
            self.assertEqual(loaded[0].disallow_count, 0)


class TestReportAttemptCounters(unittest.TestCase):
    """report_attempt_proxy_result increments tier-quality counters per branch
    (attempt always; success on ok; disallow on registration_disallowed). The
    prelude bump is exactly once per call — success path must NOT double-count.
    """

    def setUp(self) -> None:
        reset_manager_for_tests()
        core_proxy.reset_rotation_for_tests()

    def tearDown(self) -> None:
        reset_manager_for_tests()
        core_proxy.reset_rotation_for_tests()

    def _env_for(self, path: Path) -> dict:
        return {
            "REGISTER_NODES_FILE": str(path),
            "REGISTER_NODES": "1",
            "REGISTER_EGRESS": "list",
            "REGISTER_CORE": "0",
            "REGISTER_NODES_MAX_FAIL": "50",
            "PROXY_LIST": "",
            "REGISTER_NODES_PREFLIGHT": "0",
        }

    def test_success_increments_attempt_and_success_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            url = "http://n1:1"
            save_nodes([Node(url=url, id="n1", last_ok=True)], path)
            with patch.dict(os.environ, self._env_for(path), clear=False):
                reset_manager_for_tests()
                core_proxy.report_attempt_proxy_result(
                    {"proxy": url, "egress": "list"}, ok=True
                )
                from register_core.nodes import get_manager

                n = get_manager().find_by_url(url)
                assert n is not None
                self.assertEqual(n.attempt_count, 1)
                self.assertEqual(n.success_count, 1)
                self.assertEqual(n.disallow_count, 0)

    def test_registration_disallowed_increments_attempt_and_disallow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            url = "http://n1:1"
            save_nodes([Node(url=url, id="n1", last_ok=True)], path)
            with patch.dict(os.environ, self._env_for(path), clear=False):
                reset_manager_for_tests()
                core_proxy.report_attempt_proxy_result(
                    {"proxy": url, "egress": "list"},
                    ok=False,
                    error="registration_disallowed",
                    error_kind="registration_disallowed",
                )
                from register_core.nodes import get_manager

                n = get_manager().find_by_url(url)
                assert n is not None
                self.assertEqual(n.attempt_count, 1)
                self.assertEqual(n.disallow_count, 1)
                self.assertEqual(n.success_count, 0)

    def test_network_fail_increments_attempt_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            url = "http://n1:1"
            # second node keeps the pool non-empty after n1 is fenced out,
            # so the catalog-wide fail-fast path is not the thing under test.
            save_nodes(
                [Node(url=url, id="n1", last_ok=True), Node(url="http://n2:2", id="n2", last_ok=True)],
                path,
            )
            with patch.dict(os.environ, self._env_for(path), clear=False):
                reset_manager_for_tests()
                core_proxy.report_attempt_proxy_result(
                    {"proxy": url, "egress": "list"},
                    ok=False,
                    error="connection timeout via proxy",
                    error_kind="other",
                )
                from register_core.nodes import get_manager

                n = get_manager().find_by_url(url)
                assert n is not None
                self.assertEqual(n.attempt_count, 1)
                self.assertEqual(n.success_count, 0)
                self.assertEqual(n.disallow_count, 0)

    def test_non_proxy_kind_increments_attempt_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            url = "http://n1:1"
            save_nodes([Node(url=url, id="n1", last_ok=True)], path)
            with patch.dict(os.environ, self._env_for(path), clear=False):
                reset_manager_for_tests()
                core_proxy.report_attempt_proxy_result(
                    {"proxy": url, "egress": "list"},
                    ok=False,
                    error="mail never arrived",
                    error_kind="mail_miss",
                )
                from register_core.nodes import get_manager

                n = get_manager().find_by_url(url)
                assert n is not None
                self.assertEqual(n.attempt_count, 1)
                self.assertEqual(n.success_count, 0)
                self.assertEqual(n.disallow_count, 0)

    def test_node_not_in_catalog_skips_counters(self) -> None:
        # unknown proxy url → no counter writes, no crash.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes([Node(url="http://other:1", id="other")], path)
            with patch.dict(os.environ, self._env_for(path), clear=False):
                reset_manager_for_tests()
                info = core_proxy.report_attempt_proxy_result(
                    {"proxy": "http://not-in-catalog:1", "egress": "list"}, ok=True
                )
                self.assertEqual(info.get("reason"), "not_in_catalog")
                from register_core.nodes import get_manager

                self.assertIsNone(get_manager().find_by_url("http://not-in-catalog:1"))


class TestDualPoolStrategy(unittest.TestCase):
    """Residential (tier>=1) vs datacenter (tier==0) dual pools + 3 strategies."""

    def setUp(self) -> None:
        reset_manager_for_tests()
        core_proxy.reset_rotation_for_tests()

    def tearDown(self) -> None:
        reset_manager_for_tests()
        core_proxy.reset_rotation_for_tests()
        for k in (
            "REGISTER_NODES_POOL",
            "REGISTER_NODE_POOL",
            "NODE_POOL",
            "NODES_POOL",
            "NODE_POOL_STRATEGY",
            "REGISTER_NODES_POOL_STRATEGY",
        ):
            os.environ.pop(k, None)

    def _seed(self, path: Path) -> None:
        save_nodes(
            [
                Node(url="http://dc0:1", id="dc0", tier=0, last_ok=True),
                Node(url="http://dc1:1", id="dc1", tier=0, last_ok=True),
                Node(url="http://res1:1", id="res1", tier=1, last_ok=True),
                Node(url="http://res2:1", id="res2", tier=1, last_ok=True),
            ],
            path,
        )

    def test_normalize_pool_strategy_aliases(self) -> None:
        from register_core.nodes.manager import normalize_node_pool_strategy

        self.assertEqual(normalize_node_pool_strategy("residential"), "residential")
        self.assertEqual(normalize_node_pool_strategy("加宽"), "residential")
        self.assertEqual(normalize_node_pool_strategy("家宽"), "residential")
        self.assertEqual(normalize_node_pool_strategy("datacenter"), "datacenter")
        self.assertEqual(normalize_node_pool_strategy("机房"), "datacenter")
        self.assertEqual(normalize_node_pool_strategy("非加宽"), "datacenter")
        self.assertEqual(normalize_node_pool_strategy("ordinary"), "datacenter")
        self.assertEqual(normalize_node_pool_strategy("both"), "both")
        self.assertEqual(normalize_node_pool_strategy("混用"), "both")
        self.assertEqual(normalize_node_pool_strategy(""), "both")
        self.assertEqual(normalize_node_pool_strategy(None), "both")

    def test_enabled_nodes_residential_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            self._seed(path)
            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES_POOL": "residential",
                },
                clear=False,
            ):
                reset_manager_for_tests()
                mgr = get_manager()
                urls = [n.url for n in mgr.enabled_nodes()]
                self.assertEqual(urls, ["http://res1:1", "http://res2:1"])
                self.assertEqual(mgr.pool_strategy(), "residential")

    def test_enabled_nodes_datacenter_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            self._seed(path)
            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES_POOL": "机房",
                },
                clear=False,
            ):
                reset_manager_for_tests()
                mgr = get_manager()
                urls = [n.url for n in mgr.enabled_nodes()]
                self.assertEqual(urls, ["http://dc0:1", "http://dc1:1"])

    def test_enabled_nodes_both(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            self._seed(path)
            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES_POOL": "both",
                },
                clear=False,
            ):
                reset_manager_for_tests()
                mgr = get_manager()
                urls = [n.url for n in mgr.enabled_nodes()]
                self.assertEqual(
                    urls,
                    [
                        "http://dc0:1",
                        "http://dc1:1",
                        "http://res1:1",
                        "http://res2:1",
                    ],
                )

    def test_preflight_respects_pool_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            self._seed(path)
            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES_POOL": "residential",
                    "REGISTER_NODES_PROBE_LIMIT": "0",
                },
                clear=False,
            ):
                reset_manager_for_tests()
                mgr = get_manager()

                def _fake_check_all(**_kw):
                    # Only return OK for candidates that would be probed; manager
                    # already filtered by strategy before check_all body — but we
                    # stub the whole method and re-run filter via enabled_nodes.
                    out = []
                    for n in mgr.enabled_nodes(healthy_only=False):
                        out.append(
                            {
                                "id": n.id,
                                "label": n.label,
                                "ok": True,
                                "ip": "1.1.1.1",
                                "ms": 10,
                            }
                        )
                        n.last_ok = True
                    return out

                with patch.object(mgr, "check_all", side_effect=_fake_check_all):
                    summary = mgr.preflight(timeout=1.0, persist=False, limit=None)
                self.assertEqual(summary["pool_strategy"], "residential")
                self.assertEqual(
                    summary["healthy_urls"], ["http://res1:1", "http://res2:1"]
                )
                self.assertEqual(summary["healthy"], 2)

    def test_both_preserves_catalog_order_in_rotation(self) -> None:
        """When pool=both, RR still follows catalog order (no silent reweight)."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://dc0:1", id="dc0", tier=0),
                    Node(url="http://res1:1", id="res1", tier=1),
                    Node(url="http://res2:1", id="res2", tier=1),
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
                    "PROXY_LIST": "",
                    "REGISTER_NODES_PREFLIGHT": "0",
                    "REGISTER_NODES_POOL": "both",
                },
                clear=False,
            ):
                reset_manager_for_tests()
                extra = {
                    "egress": "list",
                    "proxy_list": "http://dc0:1,http://res1:1,http://res2:1",
                    "proxy_rotate_mode": "list",
                    "proxy_rotate_on_start": True,
                    "nodes_preflight": False,
                }
                seq = []
                for _ in range(3):
                    p, _ = core_proxy.resolve_attempt_proxy(extra)
                    seq.append(p)
                self.assertEqual(seq, ["http://dc0:1", "http://res1:1", "http://res2:1"])



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
            cool_node = None
            for node in mgr.nodes:
                if node.url == "http://cool:1":
                    node.cooldown_until = time.time() - 1
                    node.cooldown_reason = "stale"
                    cool_node = node
            urls = {n.url for n in mgr.enabled_nodes()}
            self.assertIn("http://cool:1", urls)
            # lazy-clear expired cool fields on is_cooling / enabled scan
            assert cool_node is not None
            self.assertFalse(mgr.is_cooling(cool_node))
            self.assertIsNone(cool_node.cooldown_until)
            self.assertEqual(cool_node.cooldown_reason, "")

    def test_mail_miss_mark_does_not_require_cooldown_api(self) -> None:
        # structural: is_cooling false by default
        n = Node(url="http://x:1")
        mgr = NodeManager.__new__(NodeManager)
        mgr._skip_failed = True
        mgr._max_fail = 3
        self.assertFalse(NodeManager.is_cooling(mgr, n))

    def test_preflight_skip_logs_backend_and_proxy_list(self) -> None:
        """Skip reasons for non-list backends / operator PROXY_LIST must be explicit."""
        logs: list[str] = []

        with patch.dict(
            os.environ,
            {
                "REGISTER_NODES": "1",
                "REGISTER_EGRESS": "direct",
                "PROXY_LIST": "",
                "CHATGPT_PROXY_LIST": "",
            },
            clear=False,
        ):
            core_proxy.reset_rotation_for_tests()
            extra = core_proxy.preflight_nodes_for_register(
                {"egress": "direct"}, log_fn=logs.append
            )
            self.assertTrue(extra["_nodes_preflight"].get("skipped"))
            self.assertEqual(extra["_nodes_preflight"].get("reason"), "backend=direct")
            self.assertTrue(any("preflight skipped: backend=direct" in m for m in logs))

        logs.clear()
        with patch.dict(
            os.environ,
            {
                "REGISTER_NODES": "1",
                "REGISTER_EGRESS": "list",
                "REGISTER_CORE": "0",
                "PROXY_LIST": "http://op:1",
                "CHATGPT_PROXY_LIST": "",
                "REGISTER_NODES_PREFLIGHT": "1",
            },
            clear=False,
        ):
            core_proxy.reset_rotation_for_tests()
            extra = core_proxy.preflight_nodes_for_register(
                {"egress": "list"}, log_fn=logs.append
            )
            self.assertTrue(extra["_nodes_preflight"].get("skipped"))
            self.assertEqual(extra["_nodes_preflight"].get("reason"), "explicit_proxy_list")
            self.assertTrue(any("explicit_proxy_list" in m or "PROXY_LIST" in m for m in logs))

        logs.clear()
        with patch.dict(
            os.environ,
            {
                "REGISTER_NODES": "0",
                "REGISTER_EGRESS": "list",
                "PROXY_LIST": "",
                "CHATGPT_PROXY_LIST": "",
            },
            clear=False,
        ):
            core_proxy.reset_rotation_for_tests()
            extra = core_proxy.preflight_nodes_for_register(
                {"egress": "list"}, log_fn=logs.append
            )
            self.assertTrue(extra["_nodes_preflight"].get("skipped"))
            self.assertEqual(extra["_nodes_preflight"].get("reason"), "REGISTER_NODES=0")
            self.assertTrue(any("REGISTER_NODES=0" in m for m in logs))

    def test_import_hint_mentions_batch_preflight(self) -> None:
        """Post-import hint must advertise batch healthy-only preflight (product contract)."""
        from register_core.nodes.convert.cli_import import run_import

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            nodes_json = td_path / "nodes.json"
            src = td_path / "links.txt"
            src.write_text("http://user:pass@1.2.3.4:8080\n", encoding="utf-8")
            with patch("builtins.print") as mock_print:
                code = run_import(
                    [str(src)],
                    format_hint="uri_list",
                    nodes_home=td_path / ".nodes",
                    nodes_json=nodes_json,
                    dry_run=False,
                    replace_nodes=True,
                )
            self.assertEqual(code, 0)
            dumped = "\n".join(
                str(c.args[0]) for c in mock_print.call_args_list if c.args
            )
            self.assertIn("healthy-only", dumped)
            self.assertIn("schema only", dumped)
            self.assertIn("egress=list|auto", dumped)

    def test_import_check_probes_dialable_even_with_needs_core(self) -> None:
        """--check must probe dialable HTTP/SOCKS even when protocol needs_core is set."""
        from register_core.nodes.convert import cli_import
        from register_core.nodes.convert.pipeline import ImportResult, MergePlan
        from register_core.nodes.models import Node

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            nodes_json = td_path / "nodes.json"
            save_nodes([Node(url="http://alive:1", id="a")], nodes_json)

            packed = ImportResult(
                ok=True,
                dialable=[Node(url="http://alive:1", id="a")],
                protocol=[{"name": "vless-1", "type": "vless"}],
                needs_core=True,
                nodes_path=str(nodes_json),
                merge=MergePlan(
                    mode="replace",
                    existing=0,
                    incoming=1,
                    added=1,
                    final=1,
                ),
            )

            def fake_check(*, nodes_json, timeout=12.0):  # noqa: ARG001
                return {
                    "ok": 1,
                    "total": 1,
                    "path": str(nodes_json),
                    "results": [{"id": "a", "ok": True}],
                }

            with patch.object(cli_import, "pack_result", return_value=packed), patch.object(
                cli_import, "convert_paths"
            ) as convert_mock, patch.object(
                cli_import, "_post_import_check", side_effect=fake_check
            ) as check_mock, patch("builtins.print") as mock_print:
                convert_mock.return_value = packed
                code = cli_import.run_import(
                    [str(td_path / "dummy.yaml")],
                    format_hint="clash_yaml",
                    nodes_home=td_path / ".nodes",
                    nodes_json=nodes_json,
                    dry_run=False,
                    replace_nodes=True,
                    check=True,
                )
            self.assertEqual(code, 0)
            self.assertTrue(check_mock.called)
            dumped = "\n".join(
                str(c.args[0]) for c in mock_print.call_args_list if c.args
            )
            self.assertIn("import+check: 1/1 live", dumped)
            self.assertIn("nodes core start", dumped)
            self.assertIn('"check"', dumped)

    def test_post_import_check_reloads_manager_without_test_helper(self) -> None:
        """_post_import_check uses get_manager+reload, not reset_manager_for_tests."""
        from register_core.nodes.convert.cli_import import _post_import_check
        from register_core.nodes import manager as mgr_mod

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes([Node(url="http://alive:1", id="a")], path)

            def fake_probe(node, **kwargs):
                node.last_ok = True
                node.fail_count = 0
                return {"id": node.id, "label": node.label, "ok": True}

            # Warm singleton with empty path first, then probe rewritten file.
            reset_manager_for_tests()
            with patch.object(mgr_mod, "probe_node", side_effect=fake_probe), patch(
                "builtins.print"
            ):
                # Pre-load a manager so reload path is exercised.
                warm = mgr_mod.get_manager(path)
                warm.nodes = []  # stale in-memory
                summary = _post_import_check(nodes_json=path, timeout=1.0)
            self.assertEqual(summary["ok"], 1)
            self.assertEqual(summary["total"], 1)
            self.assertNotIn("error", summary)
            # Product path must not depend on missing catalog after reload.
            self.assertTrue(path.is_file())

    def test_force_nodes_preflight_probes_despite_proxy_list(self) -> None:
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
                return {"id": node.id, "label": node.label, "ok": ok}

            with patch.dict(
                os.environ,
                {
                    "REGISTER_NODES_FILE": str(path),
                    "REGISTER_NODES": "1",
                    "REGISTER_EGRESS": "list",
                    "REGISTER_CORE": "0",
                    "PROXY_LIST": "http://op:1",
                    "CHATGPT_PROXY_LIST": "",
                    "REGISTER_NODES_PREFLIGHT": "1",
                },
                clear=False,
            ), patch("register_core.nodes.manager.probe_node", side_effect=fake_probe):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                extra = core_proxy.preflight_nodes_for_register(
                    {"egress": "list", "force_nodes_preflight": 1}
                )
                self.assertFalse(extra["_nodes_preflight"].get("skipped"))
                self.assertEqual(extra["_nodes_preflight"]["healthy"], 1)
                self.assertEqual(extra.get("proxy_list"), "http://alive:1")

    def test_preflight_catalog_unavailable_is_logged(self) -> None:
        logs: list[str] = []
        with patch.dict(
            os.environ,
            {
                "REGISTER_NODES": "1",
                "REGISTER_EGRESS": "auto",
                "REGISTER_CORE": "1",
                "PROXY_LIST": "",
                "CHATGPT_PROXY_LIST": "",
                "REGISTER_NODES_PREFLIGHT": "1",
            },
            clear=False,
        ), patch(
            "register_core.nodes.get_manager",
            side_effect=RuntimeError("boom-catalog"),
        ):
            core_proxy.reset_rotation_for_tests()
            # auto + not required → soft skip with log
            with patch.object(
                core_proxy, "_nodes_required_for_backend", return_value=False
            ):
                extra = core_proxy.preflight_nodes_for_register(
                    {"egress": "auto"}, log_fn=logs.append
                )
        self.assertTrue(extra["_nodes_preflight"].get("skipped"))
        self.assertIn("catalog_unavailable", str(extra["_nodes_preflight"].get("reason")))
        self.assertTrue(any("catalog unavailable" in m for m in logs))

    def test_resolve_probe_targets_provider_and_override(self) -> None:
        from register_core.nodes.targets import resolve_probe_targets

        self.assertEqual(
            resolve_probe_targets({"provider": "grok"}, env={}),
            ["https://accounts.x.ai/"],
        )
        self.assertEqual(
            resolve_probe_targets({"_provider": "chatgpt"}, env={}),
            ["https://auth.openai.com/"],
        )
        self.assertEqual(
            resolve_probe_targets(
                {"provider": "grok", "probe_targets": "https://custom.example/"},
                env={},
            ),
            ["https://custom.example/"],
        )
        self.assertEqual(
            resolve_probe_targets({}, env={"REGISTER_NODES_PROBE_TARGETS": "0"}),
            [],
        )
        # Present-but-empty env falls through to provider map (not silent L1-only).
        self.assertEqual(
            resolve_probe_targets(
                {"provider": "grok"},
                env={"REGISTER_NODES_PROBE_TARGETS": ""},
            ),
            ["https://accounts.x.ai/"],
        )
        self.assertEqual(resolve_probe_targets({}, provider="unknown", env={}), [])

    def test_preflight_l2_filters_l1_only_passers(self) -> None:
        """L1 pass + L2 fail must not seed proxy_list when targets are set."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes(
                [
                    Node(url="http://dual:1", id="dual"),
                    Node(url="http://l1only:1", id="l1"),
                ],
                path,
            )

            def fake_layered(node, **kwargs):
                dual = node.url.endswith("dual:1")
                node.last_ok = True  # L1 stamp true for both
                node.fail_count = 0
                node.last_error = "" if dual else "l2_fail"
                return {
                    "id": node.id,
                    "label": node.label,
                    "ok": dual,
                    "l1_ok": True,
                    "l2_ok": dual,
                    "pool_ready": dual,
                    "error": "" if dual else "l2_fail target=https://accounts.x.ai/",
                }

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
                    "REGISTER_NODES_PROBE_TARGETS": "",
                },
                clear=False,
            ), patch(
                "register_core.nodes.manager.probe_node_layered",
                side_effect=fake_layered,
            ):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                extra = core_proxy.preflight_nodes_for_register(
                    {
                        "egress": "list",
                        "provider": "grok",
                        "probe_targets": "https://accounts.x.ai/",
                    }
                )
                self.assertFalse(extra["_nodes_preflight"].get("skipped"))
                self.assertTrue(extra["_nodes_preflight"].get("l2_enabled"))
                self.assertEqual(extra["_nodes_preflight"]["healthy"], 1)
                self.assertEqual(extra.get("proxy_list"), "http://dual:1")

    def test_preflight_all_l2_fail_fail_fast_on_list(self) -> None:
        from register_core.errors import FailFastError

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            save_nodes([Node(url="http://l1ok:1", id="n1")], path)

            def fake_layered(node, **kwargs):
                node.last_ok = True
                node.fail_count = 0
                node.last_error = "l2_fail"
                return {
                    "id": node.id,
                    "label": node.label,
                    "ok": False,
                    "l1_ok": True,
                    "l2_ok": False,
                    "pool_ready": False,
                    "error": "l2_fail target=https://accounts.x.ai/",
                }

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
            ), patch(
                "register_core.nodes.manager.probe_node_layered",
                side_effect=fake_layered,
            ):
                reset_manager_for_tests()
                core_proxy.reset_rotation_for_tests()
                with self.assertRaises(FailFastError):
                    core_proxy.preflight_nodes_for_register(
                        {
                            "egress": "list",
                            "provider": "grok",
                            "probe_targets": "https://accounts.x.ai/",
                        }
                    )

    def test_empty_response_is_proxy_network_failure(self) -> None:
        self.assertTrue(
            core_proxy.is_proxy_network_failure(
                ok=False,
                error="net::ERR_EMPTY_RESPONSE while loading accounts.x.ai",
            )
        )
        self.assertTrue(
            core_proxy.is_proxy_network_failure(
                ok=False,
                error="Connection reset by peer to target",
            )
        )
        self.assertFalse(
            core_proxy.is_proxy_network_failure(
                ok=False,
                error="registration_disallowed by risk",
                error_kind="other",
            )
        )
        # Generic provider/other no longer hard-exclude: markers can quarantine.
        self.assertTrue(
            core_proxy.is_proxy_network_failure(
                ok=False,
                error="connection timeout via proxy",
                error_kind="provider",
            )
        )
        self.assertTrue(
            core_proxy.is_proxy_network_failure(
                ok=False,
                error="proxy connection refused",
                error_kind="other",
            )
        )
        # Explicit product kinds still never quarantine.
        self.assertFalse(
            core_proxy.is_proxy_network_failure(
                ok=False,
                error="connection timeout",
                error_kind="session",
            )
        )
        self.assertFalse(
            core_proxy.is_proxy_network_failure(
                ok=False,
                error="connection timeout",
                error_kind="token",
            )
        )

    def test_probe_reachable_http_error_is_transport_ok(self) -> None:
        """Any HTTP status (incl. 4xx/5xx) must count as L2 transport success."""
        from register_core.nodes import health as health_mod

        with patch.object(
            health_mod,
            "_http_get",
            return_value=("<html>denied</html>", 403, "urllib"),
        ):
            r = health_mod.probe_reachable(
                "http://proxy:1", "https://accounts.x.ai/", timeout=2.0
            )
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], 403)
        self.assertEqual(r["error"], "")
        self.assertEqual(r["backend"], "urllib")

    def test_http_get_urllib_http_error_returns_status(self) -> None:
        """urllib path: HTTPError must return (body, code), not raise into L2."""
        import builtins
        import urllib.error
        from io import BytesIO

        from register_core.nodes import health as health_mod

        class _FakeHTTPError(urllib.error.HTTPError):
            def __init__(self) -> None:
                super().__init__(
                    "https://accounts.x.ai/",
                    403,
                    "Forbidden",
                    {},
                    BytesIO(b"nope"),
                )

        class _Opener:
            def open(self, req, timeout=None):
                raise _FakeHTTPError()

        real_import = builtins.__import__

        def _import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "curl_cffi" or name.startswith("curl_cffi."):
                raise ImportError("forced no curl_cffi")
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=_import), patch(
            "urllib.request.build_opener", return_value=_Opener()
        ):
            body, status, backend = health_mod._http_get(
                "http://proxy:1", "https://accounts.x.ai/", timeout=2.0
            )
        self.assertEqual(status, 403)
        self.assertIn("nope", body)
        self.assertEqual(backend, "urllib")

    def test_layered_l2_fail_keeps_l1_stamp_and_annotates_error(self) -> None:
        from register_core.nodes import health as health_mod

        node = Node(url="http://p:1", id="n1", label="n1")

        def fake_l1(n, **kw):
            n.last_ok = True
            n.fail_count = 0
            n.last_error = ""
            return {
                "id": n.id,
                "label": n.label,
                "ok": True,
                "ip": "1.2.3.4",
                "ms": 10,
                "status": 200,
                "error": "",
            }

        l2_calls: list[str] = []

        def fake_l2(proxy_url, target_url, **kw):
            l2_calls.append(target_url)
            return {
                "ok": False,
                "status": None,
                "ms": 5,
                "error": "ConnectionResetError: reset",
                "target": target_url,
            }

        with patch.object(health_mod, "probe_node", side_effect=fake_l1), patch.object(
            health_mod, "probe_reachable", side_effect=fake_l2
        ):
            r = health_mod.probe_node_layered(
                node,
                probe_urls=[
                    "https://accounts.x.ai/",
                    "https://other.example/",
                ],
                timeout=2.0,
            )
        self.assertTrue(node.last_ok)
        self.assertEqual(node.fail_count, 0)
        self.assertFalse(r["ok"])
        self.assertFalse(r["pool_ready"])
        self.assertTrue(str(node.last_error).startswith("l2_fail"))
        # short-circuit: second L2 target never called
        self.assertEqual(l2_calls, ["https://accounts.x.ai/"])
        self.assertEqual(len(r["l2"]), 1)

    def test_smart_order_deprioritizes_l2_miss(self) -> None:
        """L2-miss (last_ok True + last_error l2_fail) must rank after unprobed."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            a = Node(
                url="http://a:1",
                id="a",
                last_ok=True,
                last_error="l2_fail target=x",
            )
            b = Node(url="http://b:1", id="b", last_ok=None, last_error="")
            c = Node(url="http://c:1", id="c", last_ok=True, last_error="")
            save_nodes([a, b, c], path)
            order: list[str] = []

            def fake_probe(node, **kwargs):
                order.append(node.id)
                node.last_ok = True
                node.fail_count = 0
                node.last_error = ""
                return {
                    "id": node.id,
                    "label": node.label,
                    "ok": True,
                    "error": "",
                    "pool_ready": True,
                }

            with patch(
                "register_core.nodes.manager.probe_node_layered",
                side_effect=fake_probe,
            ):
                mgr = NodeManager(path)
                mgr.check_all(
                    probe_urls=["https://accounts.x.ai/"],
                    smart_order=True,
                    limit=None,
                    persist=False,
                )
            # clean L1 (c) → unprobed (b) → l2_miss (a)
            self.assertEqual(order, ["c", "b", "a"])

    def test_smart_order_fail_tier_probes_least_failed_first(self) -> None:
        """In the hard-L1-fail tier, least-failed (most recoverable) probes first."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            # Two dead nodes: last_ok=False, non-l2_fail errors, different fail_counts.
            big = Node(
                url="http://big:1",
                id="big",
                last_ok=False,
                last_error="connect_error x",
                fail_count=9,
            )
            small = Node(
                url="http://small:1",
                id="small",
                last_ok=False,
                last_error="connect_error x",
                fail_count=1,
            )
            save_nodes([big, small], path)
            order: list[str] = []

            def fake_probe(node, **kwargs):
                order.append(node.id)
                return {"id": node.id, "label": node.label, "ok": False, "error": "x"}

            with patch(
                "register_core.nodes.manager.probe_node",
                side_effect=fake_probe,
            ):
                mgr = NodeManager(path)
                mgr.check_all(smart_order=True, limit=None, persist=False)
            # least-failed (small, fail_count=1) must precede heaviest-failed (big, 9)
            self.assertEqual(order, ["small", "big"])


        from register_core.nodes import health as health_mod

        with patch.object(
            health_mod,
            "_http_get",
            return_value=('{"ip":"139.162.20.40"}', 200, "curl_cffi"),
        ):
            r = health_mod.probe_egress_ip("http://127.0.0.1:7897", timeout=2.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["ip"], "139.162.20.40")
        self.assertEqual(r["proxy"], "http://127.0.0.1:7897")
        self.assertEqual(r["error"], "")
        # backend is the transport that served the successful GET, not "importable?"
        self.assertEqual(r["backend"], "curl_cffi")

    def test_probe_egress_ip_falls_through_urls(self) -> None:
        from register_core.nodes import health as health_mod

        calls: list[str] = []

        def fake_get(proxy, url, *, timeout=15.0):
            calls.append(url)
            if "ipify" in url:
                return ("", 502, "curl_cffi")
            return ("8.8.8.8\n", 200, "urllib")

        with patch.object(health_mod, "_http_get", side_effect=fake_get):
            r = health_mod.probe_egress_ip(
                "http://p:1",
                timeout=2.0,
                urls=(
                    "https://api.ipify.org?format=json",
                    "https://ifconfig.me/ip",
                ),
            )
        self.assertTrue(r["ok"])
        self.assertEqual(r["ip"], "8.8.8.8")
        self.assertEqual(len(calls), 2)
        self.assertIn("ifconfig.me", r["url"])
        self.assertEqual(r["backend"], "urllib")

    def test_probe_egress_ip_all_fail(self) -> None:
        from register_core.nodes import health as health_mod

        with patch.object(
            health_mod,
            "_http_get",
            side_effect=RuntimeError("boom"),
        ):
            r = health_mod.probe_egress_ip("", timeout=1.0, urls=("https://x/",))
        self.assertFalse(r["ok"])
        self.assertEqual(r["ip"], "")
        self.assertIn("boom", r["error"])
        self.assertEqual(r["backend"], "")


if __name__ == "__main__":
    unittest.main()
