"""CLI for project-owned egress nodes + embedded mihomo core."""

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
        if not args.all and st.get("nodes"):
            # summary JSON unless --all
            sample = st["nodes"][: int(args.sample)]
            print(
                json.dumps(
                    {
                        "path": st["path"],
                        "exists": st["exists"],
                        "total": st["total"],
                        "enabled": st["enabled"],
                        "healthy": st["healthy"],
                        "sample": sample,
                        "truncated": st["total"] > len(sample),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        print(f"nodes file: {st['path']} exists={st['exists']}")
        print(f"total={st['total']} enabled={st['enabled']} healthy={st['healthy']}")
        nodes = st["nodes"]
        if not args.all:
            limit = max(1, int(args.sample))
            shown = nodes[:limit]
            for n in shown:
                flag = "on " if n.get("enabled") else "off"
                health = (
                    "ok"
                    if n.get("last_ok") is True
                    else ("fail" if n.get("last_ok") is False else "?")
                )
                ip = n.get("last_ip") or "-"
                print(f"  [{flag}] {health:4} {n.get('label')} ip={ip} id={n.get('id')}")
            if st["total"] > len(shown):
                print(f"  … {st['total'] - len(shown)} more (use --all)")
        else:
            for n in nodes:
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
            from proxy_bridge import proxy_log_label

            print(proxy_log_label(u) if args.redact else u)
    return 0 if urls else 1


def cmd_core(args: argparse.Namespace) -> int:
    from register_core.nodes import core_runtime as core

    action = args.core_action
    if action == "status":
        st = core.status()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0 if st.get("bin_exists") else 2
    if action == "start":
        res = core.start(wait_s=float(args.wait))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if action == "stop":
        res = core.stop()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if action == "select":
        res = core.select(args.name)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if action == "proxies":
        names = core.list_proxy_names()
        if args.json:
            print(json.dumps(names, ensure_ascii=False, indent=2))
        else:
            for n in names:
                print(n)
        return 0 if names else 1
    if action == "url":
        try:
            url = core.ensure_proxy_url(start_core=not args.no_start)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
            return 1
        print(url)
        return 0
    print(f"unknown core action: {action}", file=sys.stderr)
    return 2


def cmd_import(args: argparse.Namespace) -> int:
    """Convert YAML / V2Ray JSON / share URIs into project artifacts."""
    from register_core.nodes.convert.cli_import import run_import
    from register_core.nodes.convert.types import DEFAULT_CONTROLLER, DEFAULT_MIXED_PORT

    from_verge = bool(getattr(args, "from_clash_verge", False))
    clash_home = None
    if from_verge:
        raw = (getattr(args, "clash_home", None) or "").strip()
        if raw:
            clash_home = Path(raw).expanduser()
        else:
            clash_home = (
                Path.home()
                / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
            )
    nh = (args.nodes_home or "").strip()
    nj = (args.nodes_json or "").strip()
    return run_import(
        list(args.paths or []),
        format_hint=args.format or "",
        nodes_home=Path(nh).expanduser() if nh else None,
        nodes_json=Path(nj).expanduser() if nj else None,
        mixed_port=int(args.mixed_port or DEFAULT_MIXED_PORT),
        controller=str(args.controller or DEFAULT_CONTROLLER),
        max_profile_proxies=int(args.max_profile_proxies),
        dry_run=bool(args.dry_run),
        replace_nodes=bool(getattr(args, "replace", False)),
        from_clash_verge=from_verge,
        clash_home=clash_home,
        check=bool(getattr(args, "check", False)),
        check_timeout=float(getattr(args, "check_timeout", 12.0) or 12.0),
    )


def cmd_clear(args: argparse.Namespace) -> int:
    """Clear HTTP/SOCKS catalog (nodes.json). Does not touch protocol runtime.yaml."""
    from register_core.nodes import get_manager, save_nodes

    mgr = get_manager(args.file or None)
    mgr.ensure_loaded()
    n = len(mgr.nodes)
    if not args.yes:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "refusing to clear without --yes",
                    "would_drop": n,
                    "path": str(mgr.path),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    save_nodes([], mgr.path)
    try:
        from register_core.nodes.manager import reset_manager_for_tests

        reset_manager_for_tests()
    except Exception:
        pass
    print(json.dumps({"ok": True, "dropped": n, "path": str(mgr.path)}, ensure_ascii=False))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate profiles without writing (legality, not liveness)."""
    from register_core.nodes.convert.cli_import import run_validate

    return run_validate(list(args.paths or []), format_hint=args.format or "")


def cmd_egress(args: argparse.Namespace) -> int:
    """Get/set egress backend switch (core vs clash vs list vs direct)."""
    from register_core.util.egress import (
        describe,
        normalize_backend,
        resolve_backend,
        write_persisted_backend,
    )
    from register_core.util import proxy as core_proxy

    action = args.egress_action
    if action in (None, "", "show", "get", "status"):
        info = describe()
        # also show what resolve would pick for a dry run
        core_proxy.reset_rotation_for_tests()
        cfg = core_proxy.rotation_config_from_env_and_extra({"egress": info["backend"]})
        info["resolved"] = {
            "backend": cfg.get("egress_backend"),
            "source": cfg.get("egress_source"),
            "rotate": cfg.get("proxy_rotate_mode"),
            "proxy": cfg.get("proxy") or "",
            "core_pool": cfg.get("core_pool"),
            "clash_pool": cfg.get("clash_pool"),
            "nodes_pool": cfg.get("nodes_pool"),
        }
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if action == "set":
        backend = normalize_backend(args.backend or "")
        if backend not in {"auto", "core", "clash", "list", "direct"}:
            print(
                "backend required: auto|core|clash|list|direct",
                file=sys.stderr,
            )
            return 2
        path = write_persisted_backend(backend)
        print(
            json.dumps(
                {"ok": True, "backend": backend, "path": str(path), **describe(backend)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if action == "which":
        # resolve with optional override
        extra = {}
        if args.backend:
            extra["egress"] = args.backend
        b = resolve_backend(extra)
        core_proxy.reset_rotation_for_tests()
        cfg = core_proxy.rotation_config_from_env_and_extra(extra if extra else {"egress": b})
        print(
            json.dumps(
                {
                    "backend": b,
                    "source": cfg.get("egress_source"),
                    "proxy": cfg.get("proxy") or "",
                    "rotate": cfg.get("proxy_rotate_mode"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"unknown egress action: {action}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="register_core.nodes",
        description=(
            "Project egress: import profiles → nodes.json / runtime.yaml; "
            "list|core|direct backends (optional external clash)"
        ),
    )
    p.add_argument(
        "--file",
        "-f",
        default="",
        help="nodes catalog path (default: REGISTER_NODES_FILE or ./nodes.json)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="Summarize HTTP/SOCKS catalog (use --all for full)")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--all", action="store_true", help="print every node")
    pl.add_argument("--sample", type=int, default=12, help="sample size when not --all")
    pl.set_defaults(func=cmd_list)

    pc = sub.add_parser("check", help="Health-check enabled HTTP/SOCKS nodes via curl_cffi")
    pc.add_argument("--timeout", type=float, default=15.0)
    pc.add_argument("--no-save", action="store_true")
    pc.set_defaults(func=cmd_check)

    pa = sub.add_parser("add", help="Append a proxy URL to the catalog")
    pa.add_argument("url", help="http://user:pass@host:port or socks5://...")
    pa.add_argument("--label", default="")
    pa.add_argument("--tag", action="append", default=[])
    pa.set_defaults(func=cmd_add)

    pclr = sub.add_parser("clear", help="Empty nodes.json (requires --yes)")
    pclr.add_argument("--yes", action="store_true", help="confirm destructive clear")
    pclr.set_defaults(func=cmd_clear)

    pu = sub.add_parser("urls", help="Print pool URLs for PROXY_LIST export")
    pu.add_argument("--healthy", action="store_true")
    pu.add_argument("--json", action="store_true")
    pu.add_argument("--redact", action="store_true")
    pu.set_defaults(func=cmd_urls)

    pcore = sub.add_parser("core", help="Project mihomo mini-core (protocol nodes only)")
    pcore.add_argument(
        "core_action",
        choices=("status", "start", "stop", "select", "proxies", "url"),
        help="core lifecycle / select proxy / print local mixed URL",
    )
    pcore.add_argument("name", nargs="?", default="", help="for select: proxy name")
    pcore.add_argument("--wait", type=float, default=8.0, help="start wait seconds")
    pcore.add_argument("--no-start", action="store_true", help="url: do not auto-start")
    pcore.add_argument("--json", action="store_true")
    pcore.set_defaults(func=cmd_core)

    pimp = sub.add_parser(
        "import",
        help="Import profile (YAML/JSON/URI) → merge nodes.json + pack runtime.yaml",
    )
    pimp.add_argument("paths", nargs="*", help="files/dirs; empty=stdin")
    pimp.add_argument(
        "--format",
        default="",
        help="force format: clash_yaml | v2ray_json | uri_list (default: auto-detect)",
    )
    pimp.add_argument("--dry-run", action="store_true", help="parse+validate+merge plan, no write")
    pimp.add_argument(
        "--replace",
        action="store_true",
        help="replace nodes.json with this import only (default: merge by URL)",
    )
    pimp.add_argument("--nodes-home", default="", help="default: ./.nodes")
    pimp.add_argument("--nodes-json", default="", help="default: ./nodes.json")
    pimp.add_argument("--mixed-port", type=int, default=17897)
    pimp.add_argument("--controller", default="127.0.0.1:19097")
    pimp.add_argument("--max-profile-proxies", type=int, default=400)
    pimp.add_argument(
        "--from-clash-verge",
        action="store_true",
        help="opt-in: also scan local Clash Verge profiles (advanced)",
    )
    pimp.add_argument(
        "--clash-home",
        default="",
        help="with --from-clash-verge: Verge data dir (mac default if empty)",
    )
    pimp.add_argument(
        "--check",
        action="store_true",
        help=(
            "after import, live-probe HTTP/SOCKS catalog (optional convenience). "
            "Batch register still re-probes and uses healthy-only rotation"
        ),
    )
    pimp.add_argument(
        "--check-timeout",
        type=float,
        default=12.0,
        help="per-node probe timeout seconds when using --check (default 12)",
    )
    pimp.set_defaults(func=cmd_import)

    pval = sub.add_parser(
        "validate",
        help="Validate profiles (schema/legality report, no write)",
    )
    pval.add_argument("paths", nargs="*", help="files; empty=stdin")
    pval.add_argument("--format", default="", help="force format (auto default)")
    pval.set_defaults(func=cmd_validate)

    peg = sub.add_parser(
        "egress",
        help="Backend switch: list|core|direct (auto|clash advanced)",
    )
    peg.add_argument(
        "egress_action",
        nargs="?",
        default="show",
        choices=("show", "get", "status", "set", "which"),
        help="show current switch / set backend / which resolves now",
    )
    peg.add_argument(
        "backend",
        nargs="?",
        default="",
        help="for set/which: auto|core|clash|list|direct",
    )
    peg.set_defaults(func=cmd_egress)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
