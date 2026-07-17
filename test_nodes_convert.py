"""Unit tests: lightweight node profile convert/validate (no network, no core)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from register_core.nodes.convert.parsers import detect_format, parse_text, ParseError
from register_core.nodes.convert.pipeline import convert_text, pack_result
from register_core.nodes.convert.uri import parse_uri
from register_core.nodes.convert.validate import validate_proxy


class TestDetectFormat(unittest.TestCase):
    def test_clash_yaml(self):
        text = "proxies:\n  - name: a\n    type: http\n    server: 1.1.1.1\n    port: 80\n"
        self.assertEqual(detect_format(text, filename="x.yaml"), "clash_yaml")

    def test_uri_list(self):
        self.assertEqual(detect_format("socks5://1.2.3.4:1080\n"), "uri_list")

    def test_v2ray_json(self):
        text = json.dumps({"outbounds": [{"protocol": "freedom"}]})
        self.assertEqual(detect_format(text, filename="c.json"), "v2ray_json")


class TestValidate(unittest.TestCase):
    def test_http_ok(self):
        issues = validate_proxy(
            {"name": "n1", "type": "http", "server": "1.2.3.4", "port": 8080}
        )
        self.assertEqual([i for i in issues if i.level == "error"], [])

    def test_missing_fields(self):
        issues = validate_proxy({"name": "x", "type": "vless"})
        codes = {i.code for i in issues if i.level == "error"}
        self.assertIn("missing_server", codes)
        self.assertIn("missing_port", codes)
        self.assertIn("missing_uuid", codes)

    def test_bad_port(self):
        issues = validate_proxy(
            {"name": "n", "type": "http", "server": "1.1.1.1", "port": 99999}
        )
        self.assertTrue(any(i.code == "bad_port" for i in issues))


class TestUri(unittest.TestCase):
    def test_socks(self):
        p = parse_uri("socks5://user:pass@10.0.0.1:1080#lab")
        assert p is not None
        self.assertEqual(p["type"], "socks5")
        self.assertEqual(p["server"], "10.0.0.1")
        self.assertEqual(p["port"], 1080)
        self.assertEqual(p["username"], "user")
        self.assertEqual(p["name"], "lab")

    def test_ss(self):
        # method:password@host:port
        p = parse_uri("ss://aes-256-gcm:secret@1.2.3.4:8388#ss1")
        assert p is not None
        self.assertEqual(p["type"], "ss")
        self.assertEqual(p["cipher"], "aes-256-gcm")
        self.assertEqual(p["password"], "secret")

    def test_trojan(self):
        p = parse_uri("trojan://pwd@example.com:443?sni=example.com#tj")
        assert p is not None
        self.assertEqual(p["type"], "trojan")
        self.assertEqual(p["password"], "pwd")
        self.assertEqual(p["sni"], "example.com")


class TestPipeline(unittest.TestCase):
    def test_convert_http_only_no_core(self):
        text = """
proxies:
  - name: h1
    type: http
    server: 1.2.3.4
    port: 8080
  - name: bad
    type: vless
    server: 1.1.1.1
"""
        r = convert_text(text, source="t.yaml")
        self.assertTrue(r.ok)
        self.assertEqual(len(r.dialable), 1)
        # bad vless missing uuid → rejected
        self.assertEqual(len(r.protocol), 0)
        self.assertFalse(r.needs_core)

    def test_convert_vless_needs_core(self):
        text = """
proxies:
  - name: v1
    type: vless
    server: 1.2.3.4
    port: 443
    uuid: 12345678-1234-1234-1234-123456789abc
"""
        r = convert_text(text, source="t.yaml")
        self.assertTrue(r.ok)
        self.assertEqual(len(r.protocol), 1)
        self.assertTrue(r.needs_core)

    def test_invalid_yaml_reports(self):
        r = convert_text("proxies: [[[[", source="bad.yaml")
        self.assertFalse(r.ok)
        self.assertTrue(r.errors or r.reports)

    def test_pack_writes_artifacts(self):
        text = """
proxies:
  - name: h1
    type: socks5
    server: 9.9.9.9
    port: 1080
  - name: v1
    type: trojan
    server: 8.8.8.8
    port: 443
    password: secret
"""
        r = convert_text(text, source="mix.yaml")
        self.assertTrue(r.ok)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / ".nodes"
            nodes_json = Path(td) / "nodes.json"
            packed = pack_result(r, nodes_home=home, nodes_json=nodes_json)
            self.assertTrue(packed.ok)
            self.assertTrue(nodes_json.is_file())
            self.assertTrue((home / "config" / "runtime.yaml").is_file())
            data = nodes_json.read_text(encoding="utf-8")
            self.assertIn("9.9.9.9", data)
            self.assertIn("imp-", data)
            self.assertNotIn("from-clash", data)
            rt = (home / "config" / "runtime.yaml").read_text(encoding="utf-8")
            self.assertIn("trojan", rt)
            self.assertIn("v1", rt)
            self.assertIsNotNone(packed.merge)
            self.assertEqual(packed.merge.mode, "merge")

    def test_merge_keeps_existing(self):
        from register_core.nodes.catalog import load_nodes, save_nodes
        from register_core.nodes.models import Node

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            nodes_json = td_path / "nodes.json"
            save_nodes(
                [Node(url="http://old:1@1.1.1.1:80", id="keep-old", label="old")],
                nodes_json,
            )
            r = convert_text(
                "proxies:\n  - name: n\n    type: http\n    server: 2.2.2.2\n    port: 8080\n",
                source="t.yaml",
            )
            packed = pack_result(
                r, nodes_home=td_path / ".nodes", nodes_json=nodes_json, replace_nodes=False
            )
            loaded = load_nodes(nodes_json)
            urls = {n.url for n in loaded}
            self.assertIn("http://old:1@1.1.1.1:80", urls)
            self.assertIn("http://2.2.2.2:8080", urls)
            self.assertEqual(packed.merge.added, 1)
            self.assertEqual(packed.merge.kept, 1)

    def test_merge_preserves_cooldown(self):
        """Re-import must not drop soft-cool on existing URL."""
        from register_core.nodes.convert.pipeline import merge_dialable
        from register_core.nodes.models import Node

        old = Node(
            url="http://u:p@1.1.1.1:80",
            id="keep",
            label="old",
            cooldown_until=9_999_999_999.0,
            cooldown_reason="registration_disallowed",
            fail_count=3,
        )
        incoming = Node(url="http://u:p@1.1.1.1:80", id="new", label="fresh")
        merged, plan = merge_dialable([old], [incoming], replace=False)
        self.assertEqual(plan.updated, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].cooldown_until, 9_999_999_999.0)
        self.assertEqual(merged[0].cooldown_reason, "registration_disallowed")
        self.assertEqual(merged[0].fail_count, 3)

    def test_default_id_unique_for_same_host_different_auth(self):
        from register_core.nodes.models import Node, _default_id

        a = "http://userA:pwA@10.0.0.1:8080"
        b = "http://userB:pwB@10.0.0.1:8080"
        id_a = _default_id(a)
        id_b = _default_id(b)
        self.assertNotEqual(id_a, id_b)
        self.assertEqual(_default_id(a), id_a)  # stable
        na, nb = Node(url=a), Node(url=b)
        self.assertNotEqual(na.id, nb.id)
        # redacted label alone would collide; digest disambiguates
        self.assertIn("10.0.0.1", na.id)

    def test_replace_drops_existing(self):
        from register_core.nodes.catalog import load_nodes, save_nodes
        from register_core.nodes.models import Node

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            nodes_json = td_path / "nodes.json"
            save_nodes([Node(url="http://old:1@1.1.1.1:80", label="old")], nodes_json)
            r = convert_text(
                "proxies:\n  - name: n\n    type: http\n    server: 2.2.2.2\n    port: 8080\n",
                source="t.yaml",
            )
            packed = pack_result(
                r, nodes_home=td_path / ".nodes", nodes_json=nodes_json, replace_nodes=True
            )
            loaded = load_nodes(nodes_json)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].url, "http://2.2.2.2:8080")
            self.assertEqual(packed.merge.mode, "replace")
            self.assertEqual(packed.merge.dropped, 1)


class TestParseErrors(unittest.TestCase):
    def test_empty_raises(self):
        with self.assertRaises(ParseError):
            parse_text("   ", source="x")


if __name__ == "__main__":
    unittest.main()
