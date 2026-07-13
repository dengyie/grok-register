#!/usr/bin/env python3
"""Unit tests for proxy_rotate (no live Clash required for most checks)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import proxy_rotate as pr  # noqa: E402


def test_parse_proxy_list() -> None:
    assert pr.parse_proxy_list(None) == []
    assert pr.parse_proxy_list("") == []
    assert pr.parse_proxy_list("http://a:1, http://b:2") == ["http://a:1", "http://b:2"]
    assert pr.parse_proxy_list("http://a:1\nhttp://b:2") == ["http://a:1", "http://b:2"]
    assert pr.parse_proxy_list('["http://a:1","http://b:2"]') == ["http://a:1", "http://b:2"]
    assert pr.parse_proxy_list(["http://a:1", " http://b:2 "]) == ["http://a:1", "http://b:2"]
    # file path with one proxy per line
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("# comment\nhttp://a:1\nhttp://b:2\n")
        path = f.name
    try:
        assert pr.parse_proxy_list(path) == ["http://a:1", "http://b:2"]
    finally:
        os.unlink(path)
    print("PASS parse_proxy_list")


def test_parse_domain_list() -> None:
    assert pr.parse_domain_list(None) == list(pr.DEFAULT_GROK_DOMAINS)
    assert pr.parse_domain_list("") == list(pr.DEFAULT_GROK_DOMAINS)
    assert pr.parse_domain_list("x.ai,grok.com") == ["x.ai", "grok.com"]
    assert pr.parse_domain_list(".x.ai") == ["x.ai"]
    assert pr.parse_domain_list(["x.ai", " grok.com "]) == ["x.ai", "grok.com"]
    print("PASS parse_domain_list")


def test_list_mode_rotates_and_applies_to_config() -> None:
    rot = pr.ProxyRotator()
    rot.configure(
        {
            "proxy_rotate_mode": "list",
            "proxy_rotate_every": 1,
            "proxy": "http://127.0.0.1:7897",
            "proxy_list": "http://a:1,http://b:2,http://c:3",
        }
    )
    cfg = {"proxy": "http://127.0.0.1:7897", "cpa_proxy": "http://127.0.0.1:7897"}

    # First rotate_on_start: index 0
    r0 = rot.maybe_rotate(log=None, config=cfg)
    assert r0["rotated"] is True
    assert r0["label"] == "http://a:1"
    assert cfg["proxy"] == "http://a:1"
    assert cfg["cpa_proxy"] == "http://a:1"

    # 2nd account: due (every=1) -> rotate to b
    r1 = rot.maybe_rotate(log=None, config=cfg)
    assert r1["label"] == "http://b:2"
    assert cfg["proxy"] == "http://b:2"

    r2 = rot.maybe_rotate(log=None, config=cfg)
    assert r2["label"] == "http://c:3"
    # wraps around
    r3 = rot.maybe_rotate(log=None, config=cfg)
    assert r3["label"] == "http://a:1"
    print("PASS list mode rotates + applies to config")


def test_list_mode_every_two() -> None:
    rot = pr.ProxyRotator()
    rot.configure(
        {
            "proxy_rotate_mode": "list",
            "proxy_rotate_every": 2,
            "proxy": "http://x:1",
            "proxy_list": "http://a:1,http://b:2",
            "proxy_rotate_on_start": False,
        }
    )
    cfg = {"proxy": "http://x:1"}
    # rotate_on_start=False: first call just counts (current=pool[0]=a)
    r0 = rot.maybe_rotate(log=None, config=cfg)
    assert r0["rotated"] is False
    # 2nd: accounts_on_current=1 < every=2 -> not due
    r1 = rot.maybe_rotate(log=None, config=cfg)
    assert r1["rotated"] is False
    # 3rd: accounts_on_current=2 >= every=2 -> rotate to next (b)
    r2 = rot.maybe_rotate(log=None, config=cfg)
    assert r2["rotated"] is True
    assert r2["label"] == "http://b:2"
    print("PASS list mode every=2")


def test_clash_mode_guards_main_group() -> None:
    rot = pr.ProxyRotator()
    # configure with main group name should fall back to GROK-REG
    rot.configure(
        {
            "proxy_rotate_mode": "clash",
            "clash_proxy_group": "宝可梦",  # same as donor -> rejected
            "clash_donor_group": "宝可梦",
        }
    )
    assert rot.clash_group == "GROK-REG", rot.clash_group
    # explicit GLOBAL rejected
    rot.configure(
        {
            "proxy_rotate_mode": "clash",
            "clash_proxy_group": "GLOBAL",
        }
    )
    assert rot.clash_group == "GROK-REG"
    print("PASS clash guards main group")


def test_clash_setup_injects_group_and_rules(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    yaml = tmp_path / "clash.yaml"
    yaml.write_text(
        "mixed-port: 7897\nmode: rule\nproxy-groups:\n- name: 宝可梦\n  type: select\n  proxies:\n  - DIRECT\nrules:\n- MATCH,宝可梦\n",
        encoding="utf-8",
    )
    rot = pr.ProxyRotator()
    rot.configure(
        {
            "proxy_rotate_mode": "clash",
            "clash_api": "unix:///tmp/nonexistent.sock",
            "clash_proxy_group": "GROK-REG",
            "clash_donor_group": "宝可梦",
            "clash_rule_domains": ["x.ai", "grok.com"],
            "clash_config_path": str(yaml),
            "clash_profiles_dir": "",  # skip verge enhancement write
            "clash_restore_on_exit": False,
        }
    )
    # fake proxies: first call (before create) has no GROK-REG; after reload it appears
    before = {
        "宝可梦": {"type": "Selector", "all": ["DIRECT", "🇭🇰 香港节点"], "now": "DIRECT"},
        "DIRECT": {"type": "Direct"},
        "🇭🇰 香港节点": {"type": "Vmess"},
    }
    after = dict(before)
    after["GROK-REG"] = {"type": "Selector", "all": ["🇭🇰 香港节点"], "now": "🇭🇰 香港节点"}
    seq = {"i": 0}

    def fake_get(api, **k):
        seq["i"] += 1
        return before if seq["i"] == 1 else after

    with patch.object(pr, "clash_get_proxies", side_effect=fake_get):
        with patch.object(pr, "clash_force_reload") as fake_reload:
            rot._ensure_clash_setup_locked(log=lambda m: print(m))
            assert fake_reload.called
    text = yaml.read_text(encoding="utf-8")
    assert 'name: "GROK-REG"' in text
    assert "DOMAIN-SUFFIX,x.ai,GROK-REG" in text
    assert "DOMAIN-SUFFIX,grok.com,GROK-REG" in text
    # donor main group untouched
    assert "- name: 宝可梦" in text
    print("PASS clash setup injects group + rules")


def test_clash_mode_rotate_and_restore() -> None:
    rot = pr.ProxyRotator()
    rot.configure(
        {
            "proxy_rotate_mode": "clash",
            "clash_api": "unix:///tmp/fake.sock",
            "clash_proxy_group": "GROK-REG",
            "clash_donor_group": "宝可梦",
            "clash_rule_domains": ["x.ai"],
            "clash_restore_on_exit": True,
        }
    )
    rot.clash_setup_done = True  # skip ensure
    rot._original_clash_node = "NODE_A"
    calls = {"switch": [], "list": 0}

    def fake_list_nodes(api, group, **k):
        calls["list"] += 1
        return ["NODE_A", "NODE_B", "NODE_C"], "NODE_A", {}

    def fake_switch(api, group, node, **k):
        calls["switch"].append((group, node))

    with patch.object(pr, "clash_list_nodes", side_effect=fake_list_nodes):
        with patch.object(pr, "clash_switch_node", side_effect=fake_switch):
            r = rot.maybe_rotate(log=None)
            assert r["rotated"] is True
            assert r["node"] == "NODE_B"
            assert calls["switch"][-1] == ("GROK-REG", "NODE_B")
            # restore
            ok = rot.restore_clash()
            assert ok is True
            assert calls["switch"][-1] == ("GROK-REG", "NODE_A")
    print("PASS clash rotate + restore")


def test_clash_mode_refuses_main_group_at_runtime() -> None:
    rot = pr.ProxyRotator()
    rot.configure(
        {
            "proxy_rotate_mode": "clash",
            "clash_api": "unix:///tmp/fake.sock",
            "clash_proxy_group": "GROK-REG",
            "clash_donor_group": "宝可梦",
        }
    )
    # sabotage: pretend group equals donor
    rot.clash_group = "宝可梦"
    rot.clash_setup_done = True
    r = rot.maybe_rotate(log=None)
    assert r["rotated"] is False
    assert "error" in r
    assert "主策略组" in r["error"]
    print("PASS clash refuses main group at runtime")


def test_off_mode() -> None:
    rot = pr.ProxyRotator()
    rot.configure({"proxy_rotate_mode": "off"})
    r = rot.maybe_rotate(log=None)
    assert r["rotated"] is False
    assert r["mode"] == "off"
    # restore is no-op
    assert rot.restore_clash() is False
    print("PASS off mode")


def main() -> int:
    test_parse_proxy_list()
    test_parse_domain_list()
    test_list_mode_rotates_and_applies_to_config()
    test_list_mode_every_two()
    test_clash_mode_guards_main_group()
    test_clash_setup_injects_group_and_rules(Path("/tmp/_grok_reg_test"))
    test_clash_mode_rotate_and_restore()
    test_clash_mode_refuses_main_group_at_runtime()
    test_off_mode()
    print("\nALL PASS (proxy_rotate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
