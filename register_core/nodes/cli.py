"""CLI for project-owned egress nodes (no Clash)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def cmd_list(args: argparse.Namespace) -> int:
    from register_core.nodes import get_manager

    mgr = get_manager(args.file or None)
    st = mgr.status()
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        print(f"nodes file: {st['path']} exists={st['exists']}")
        print(f"total={st['total']} enabled={st['enabled']} healthy={st['healthy']}")
        for n in st["nodes"]:
            flag = "on " if n.get("enabled") else "off"
            health = (
                "ok"
                if n.get("last_ok") is True
                else ("fail" if n.get("last_ok") is False else "?")
            )
            ip = n.get("last_ip") or "-"
            print(f"  [{flag}] {health:4} {n.get('label')} ip={ip} id={n.get('id')}")
    return 0 if st["total"] else 1


def cmd_check(args: argparse.Namespace) -> int:
    from register_core.nodes import get_manager

    mgr = get_manager(args.file or None)
    if not mgr.ensure_loaded():
        print("no nodes loaded; create nodes.json (see nodes.example.json)", file=sys.stderr)
        return 2
    results = mgr.check_all(
        timeout=float(args.timeout),
        log=lambda m: print(m, flush=True),
        persist=not args.no_save,
    )
    ok_n = sum(1 for r in results if r.get("ok"))
    print(json.dumps({"ok": ok_n, "total": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0 if ok_n else 1


def cmd_add(args: argparse.Namespace) -> int:
    from register_core.nodes import get_manager, save_nodes
    from register_core.nodes.models import Node

    url = (args.url or "").strip()
    if not url:
        print("url required", file=sys.stderr)
        return 2
    mgr = get_manager(args.file or None)
    mgr.ensure_loaded()
    existing = {n.url for n in mgr.nodes}
    if url in existing:
        print(json.dumps({"ok": False, "error": "duplicate", "url_label": Node(url=url).label}))
        return 1
    node = Node(url=url, label=args.label or "", tags=list(args.tag or []))
    mgr.nodes.append(node)
    path = save_nodes(mgr.nodes, mgr.path)
    print(json.dumps({"ok": True, "path": str(path), "node": node.to_public_dict()}, ensure_ascii=False))
    return 0


def cmd_urls(args: argparse.Namespace) -> int:
    from register_core.nodes import get_manager

    mgr = get_manager(args.file or None)
    urls = mgr.urls(healthy_only=bool(args.healthy))
    if args.json:
        print(json.dumps(urls, ensure_ascii=False))
    else:
        for u in urls:
            # never print credentials raw if possible — still need real URL for operators
            from proxy_bridge import proxy_log_label

            print(proxy_log_label(u) if args.redact else u)
    return 0 if urls else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="register_core.nodes",
        description="Project-owned egress nodes (self-controlled; no Clash required)",
    )
    p.add_argument(
        "--file",
        "-f",
        default="",
        help="nodes catalog path (default: REGISTER_NODES_FILE or ./nodes.json)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="List nodes (public labels, no secrets)")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pc = sub.add_parser("check", help="Health-check all enabled nodes via curl_cffi")
    pc.add_argument("--timeout", type=float, default=15.0)
    pc.add_argument("--no-save", action="store_true", help="do not write last_* back to catalog")
    pc.set_defaults(func=cmd_check)

    pa = sub.add_parser("add", help="Append a proxy URL to the catalog")
    pa.add_argument("url", help="http://user:pass@host:port or socks5://...")
    pa.add_argument("--label", default="")
    pa.add_argument("--tag", action="append", default=[])
    pa.set_defaults(func=cmd_add)

    pu = sub.add_parser("urls", help="Print pool URLs for PROXY_LIST export")
    pu.add_argument("--healthy", action="store_true")
    pu.add_argument("--json", action="store_true")
    pu.add_argument("--redact", action="store_true")
    pu.set_defaults(func=cmd_urls)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
