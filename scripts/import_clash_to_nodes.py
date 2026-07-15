#!/usr/bin/env python3
"""Import Clash/mihomo YAML proxies into project .nodes runtime.

- Copies YAML into .nodes/profiles/clash-import/
- Builds .nodes/config/runtime.yaml for project mihomo core
- Imports dialable HTTP/SOCKS into nodes.json for direct curl_cffi use
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

from register_core.nodes.catalog import save_nodes  # noqa: E402
from register_core.nodes.models import Node  # noqa: E402

DEFAULT_MIXED = 17897
DEFAULT_CTL = "127.0.0.1:19097"
DEFAULT_GROUP = "REGISTER"
DIALABLE = {"http", "https", "socks5", "socks5h", "socks4"}


def _auth_url(scheme: str, server: str, port, username: str = "", password: str = "") -> str:
    host = f"{server}:{int(port)}"
    user = (username or "").strip()
    pwd = (password or "").strip()
    if user or pwd:
        return f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}"
    return f"{scheme}://{host}"


def _node_from_http(x: dict, source: str) -> Node | None:
    t = str(x.get("type") or "").lower().strip()
    if t not in DIALABLE:
        return None
    server = str(x.get("server") or "").strip()
    port = x.get("port")
    if not server or not port:
        return None
    name = str(x.get("name") or f"{server}:{port}").strip()
    user = str(x.get("username") or x.get("user") or "")
    pwd = str(x.get("password") or "")
    scheme = "socks5" if t.startswith("socks") else "http"
    url = _auth_url(scheme, server, port, user, pwd)
    nid = hashlib.sha1(f"{scheme}|{server}|{port}|{user}".encode()).hexdigest()[:12]
    return Node(
        url=url,
        id=f"clash-{nid}",
        label=name[:80],
        tags=[t, "from-clash", source[:24]],
        enabled=True,
    )


def _sig(p: dict) -> str:
    return hashlib.sha1(
        f"{p.get('type')}|{p.get('server')}|{p.get('port')}|{p.get('uuid') or p.get('password') or ''}|{p.get('name')}".encode()
    ).hexdigest()


def collect_files(paths: list[Path], clash_home: Path | None) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        p = p.expanduser().resolve()
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.glob("*.yaml")))
            out.extend(sorted(p.glob("*.yml")))
    if clash_home and clash_home.is_dir():
        active = clash_home / "clash-verge.yaml"
        if active.is_file():
            out.append(active)
        prof = clash_home / "profiles"
        if prof.is_dir():
            out.extend(sorted(prof.glob("*.yaml")))
    # dedupe
    seen = set()
    uniq = []
    for f in out:
        if f in seen:
            continue
        seen.add(f)
        uniq.append(f)
    return uniq


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import Clash YAML into project .nodes")
    ap.add_argument("paths", nargs="*", type=Path, help="YAML files/dirs")
    ap.add_argument(
        "--clash-home",
        type=Path,
        default=Path.home()
        / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev",
        help="Clash Verge data dir (mac default)",
    )
    ap.add_argument("--no-clash-home", action="store_true")
    ap.add_argument("--max-profile-proxies", type=int, default=400)
    ap.add_argument("--mixed-port", type=int, default=DEFAULT_MIXED)
    ap.add_argument("--controller", default=DEFAULT_CTL)
    ap.add_argument("--nodes-home", type=Path, default=_ROOT / ".nodes")
    args = ap.parse_args(argv)

    home: Path = args.nodes_home.resolve()
    imp = home / "profiles" / "clash-import"
    cfg_dir = home / "config"
    imp.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)

    files = collect_files(list(args.paths or []), None if args.no_clash_home else args.clash_home)
    if not files:
        print("no yaml files found", file=sys.stderr)
        return 2

    packed: list[dict] = []
    seen_sig: set[str] = set()
    seen_name: set[str] = set()
    http_nodes: list[Node] = []
    seen_http: set[str] = set()
    skipped = Counter()
    sources = Counter()

    for f in files:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8", errors="replace")) or {}
        except Exception as exc:
            skipped[f"yaml_err:{f.name}"] += 1
            print(f"skip {f}: {exc}", file=sys.stderr)
            continue
        proxies = data.get("proxies") or []
        if not isinstance(proxies, list) or not proxies:
            continue
        # archive copy
        dest = imp / f"profile-{f.stem}.yaml"
        if f.resolve() != dest.resolve():
            shutil.copy2(f, dest)
        if f.name == "clash-verge.yaml":
            shutil.copy2(f, imp / "active-clash-verge.yaml")

        large = len(proxies) > int(args.max_profile_proxies)
        for x in proxies:
            if not isinstance(x, dict):
                continue
            t = str(x.get("type") or "?").lower()
            # always harvest dialable into nodes.json
            n = _node_from_http(x, f.stem)
            if n and n.url not in seen_http:
                seen_http.add(n.url)
                http_nodes.append(n)
            if large:
                skipped["large_profile_skip_core"] += 1
                continue
            name = str(x.get("name") or "").strip()
            if not name:
                skipped["no_name"] += 1
                continue
            s = _sig(x)
            if s in seen_sig:
                skipped["dup"] += 1
                continue
            if name in seen_name:
                name = f"{name}#{s[:6]}"
                x = dict(x)
                x["name"] = name
            seen_sig.add(s)
            seen_name.add(name)
            packed.append(x)
            sources[f.name] += 1
            skipped[f"type:{t}"] += 0  # touch

    names = [p["name"] for p in packed]
    runtime = {
        "mixed-port": int(args.mixed_port),
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "info",
        "ipv6": True,
        "external-controller": str(args.controller),
        "secret": "",
        "unified-delay": True,
        "tcp-concurrent": True,
        "find-process-mode": "off",
        "dns": {
            "enable": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["8.8.8.8", "1.1.1.1"],
            "fallback": ["8.8.4.4", "1.0.0.1"],
        },
        "proxies": packed,
        "proxy-groups": [
            {"name": DEFAULT_GROUP, "type": "select", "proxies": names + ["DIRECT"]},
        ],
        "rules": [f"MATCH,{DEFAULT_GROUP}"],
    }
    runtime_path = cfg_dir / "runtime.yaml"
    runtime_path.write_text(yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False), encoding="utf-8")
    try:
        runtime_path.chmod(0o600)
    except Exception:
        pass

    meta = {
        "group": DEFAULT_GROUP,
        "mixed_port": int(args.mixed_port),
        "controller": str(args.controller),
        "proxy_url": f"http://127.0.0.1:{int(args.mixed_port)}",
        "names": names,
        "types": dict(Counter(str(p.get("type")) for p in packed)),
        "sources": dict(sources),
        "http_socks_imported": len(http_nodes),
    }
    (cfg_dir / "proxy-names.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    nodes_path = _ROOT / "nodes.json"
    if http_nodes:
        save_nodes(http_nodes, nodes_path)

    print(
        json.dumps(
            {
                "ok": True,
                "runtime": str(runtime_path),
                "core_proxies": len(packed),
                "http_socks_nodes": len(http_nodes),
                "nodes_json": str(nodes_path) if http_nodes else None,
                "types": meta["types"],
                "mixed_port": meta["mixed_port"],
                "proxy_url": meta["proxy_url"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if packed or http_nodes else 1


if __name__ == "__main__":
    raise SystemExit(main())
