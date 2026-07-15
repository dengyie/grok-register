#!/usr/bin/env python3
"""Unified layered register CLI.

Production authority for full signup remains:
  ./register.sh grok | ./register.sh mimo

This CLI orchestrates adapters with honest success attribution. Black-box
providers (grok/mimo) use adapter-internal mail only (email_source=provider).
In-process providers (chatgpt) accept EmailSource (tinyhost/auto/…).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from register_core.contracts import RegisterJob  # noqa: E402
from register_core.email.registry import list_email_sources  # noqa: E402
from register_core.pipeline import Pipeline  # noqa: E402
from register_core.providers.registry import list_providers  # noqa: E402
from register_core.sink.jsonl_sink import JsonlSink  # noqa: E402


def cmd_list(_: argparse.Namespace) -> int:
    print("providers:", ", ".join(list_providers()))
    print("email_sources:", ", ".join(list_email_sources()))
    print("layers: contracts → email → providers → verify → sink → pipeline → cli")
    print(
        "nodes: python -m register_core nodes "
        "import|validate|list|check|add|clear|core|egress"
    )
    print(
        "egress primary: list|core|direct  "
        "(advanced: auto|clash) via REGISTER_EGRESS / --egress / nodes egress set"
    )
    print(
        "note: grok/mimo are black-box (email_source=provider). "
        "chatgpt is in-process (use --email-source tinyhost|auto). "
        "Hub: ./register.sh grok|mimo|chatgpt"
    )
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    """Delegate to register_core.nodes.cli (import/list/core/egress)."""
    from register_core.nodes.cli import main as nodes_main

    # Rebuild argv for nodes CLI: everything after `nodes`
    sub = list(args.nodes_argv or [])
    if sub and sub[0] == "--":
        sub = sub[1:]
    if args.file:
        sub = ["--file", args.file, *sub]
    return int(nodes_main(sub if sub else ["list"]))


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sink = JsonlSink(args.sink) if args.sink else None
    headless = None if args.headless is None else bool(args.headless)
    extra: dict = {
        "timeout_s": args.timeout,
        "threads": args.threads,
    }
    if headless is not None:
        extra["headless"] = headless
    # Egress backend switch: core (project mihomo) | clash | list | direct | auto
    if getattr(args, "egress", None):
        extra["egress"] = args.egress
    if getattr(args, "proxy", None):
        extra["proxy"] = args.proxy
    if getattr(args, "proxy_list", None):
        extra["proxy_list"] = args.proxy_list
    if getattr(args, "proxy_rotate", None):
        extra["proxy_rotate_mode"] = args.proxy_rotate
    if getattr(args, "proxy_rotate_every", None) is not None and args.proxy_rotate_every >= 1:
        extra["proxy_rotate_every"] = int(args.proxy_rotate_every)
    if getattr(args, "proxy_rotate_required", False):
        extra["proxy_rotate_required"] = True

    job = RegisterJob(
        provider=args.provider,
        count=args.count,
        email_source=args.email_source,
        verify=not args.no_verify,
        fail_fast=not args.no_fail_fast,
        extra=extra,
    )

    try:
        pipe = Pipeline.from_job(job, sink=sink)
    except ValueError as exc:
        print(json.dumps({"ok": 0, "fail": 1, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    stats = pipe.run(job.count, extra=job.extra)
    summary = {
        "ok": stats.ok,
        "fail": stats.fail,
        "stopped_reason": stats.stopped_reason,
        "results": [r.to_public_dict() for r in stats.results],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if stats.ok < 1:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="register_core",
        description="Layered multi-provider register hub (experimental orchestration)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="List providers and email sources")
    pl.set_defaults(func=cmd_list)

    pn = sub.add_parser(
        "nodes",
        help="Egress nodes: import|validate|list|check|core|egress",
    )
    pn.add_argument("--file", "-f", default="", help="nodes catalog path")
    pn.add_argument(
        "nodes_argv",
        nargs=argparse.REMAINDER,
        help="nodes subcommand: import|validate|list|check|add|clear|core|egress|…",
    )
    pn.set_defaults(func=cmd_nodes)

    pr = sub.add_parser("run", help="Run registration pipeline")
    pr.add_argument("--provider", "-p", required=True, help="grok | mimo | chatgpt")
    pr.add_argument("--count", "-n", type=int, default=1)
    pr.add_argument(
        "--email-source",
        default="provider",
        help=(
            "provider=adapter-internal mail (required for grok/mimo). "
            "chatgpt defaults via adapter to tinyhost when provider; "
            "prefer tinyhost|duckmail|gmail_imap|auto for chatgpt."
        ),
    )
    pr.add_argument("--sink", default="", help="JSONL path for private results (0600)")
    pr.add_argument("--no-verify", action="store_true")
    pr.add_argument(
        "--no-fail-fast",
        action="store_true",
        help="continue after failure (not recommended)",
    )
    pr.add_argument("--timeout", type=int, default=1200)
    pr.add_argument("--threads", type=int, default=1, help="grok register threads")
    pr.add_argument("--headless", type=int, choices=(0, 1), default=None)
    pr.add_argument(
        "--egress",
        choices=("auto", "core", "clash", "list", "direct"),
        default="",
        help=(
            "egress backend switch: core=project mihomo (.nodes); "
            "clash=external Clash :7897; list=nodes.json/PROXY_LIST; "
            "direct=no proxy; auto=list→core→clash (env REGISTER_EGRESS)"
        ),
    )
    pr.add_argument(
        "--proxy",
        default="",
        help="fixed outbound proxy URL for this run (overrides CHATGPT_PROXY)",
    )
    pr.add_argument(
        "--proxy-list",
        default="",
        help=(
            "self-controlled node pool: comma/newline URLs or .txt path. "
            "When set with --egress list/auto, rotation uses list mode."
        ),
    )
    pr.add_argument(
        "--proxy-rotate",
        choices=("off", "list", "nodes", "clash", "core"),
        default="",
        help=(
            "low-level rotation: list/nodes/core=URL pool; clash=external controller; "
            "prefer --egress for backend choice"
        ),
    )
    pr.add_argument(
        "--proxy-rotate-every",
        type=int,
        default=-1,
        help="rotate every N attempts (default 1)",
    )
    pr.add_argument(
        "--proxy-rotate-required",
        action="store_true",
        help="fail-fast if rotation fails (no silent reuse of bad egress)",
    )
    pr.add_argument("-v", "--verbose", action="store_true")
    pr.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
