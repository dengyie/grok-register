#!/usr/bin/env python3
"""Unified layered register CLI — production entry for all providers.

The three ./register.sh production entries (grok | mimo | chatgpt) all route
through this register_core Pipeline (migrate milestone A):
  grok    → run-register-core.sh → `python -m register_core run --profile ...`
  mimo    → `python -m register_core run --profile profiles/mimo-tinyhost...`
  chatgpt → `python -m register_core run --profile profiles/chatgpt-tinyhost...`

Pipeline owns attribution, strategy burn/cool, verifiers, and JSONL sink for all
three entries. Egress ownership is backend-dependent and declared in the profile
(not implicit): Grok/MiMo profiles pin `strategy.egress.mode: clash proxy
127.0.0.1:7897` so profile_to_job sets extra["proxy"] truthy and the grok/mimo
adapter force-sets the child PROXY/CPA_PROXY from the attempt proxy (attempt
proxy wins over ambient shell env → Pipeline owns the egress); Clash leaf health
is still probed by preflight-clash-nodes.sh (Grok) / Node runner (MiMo), since
preflight_nodes_for_register skips `clash` backend (nodes.json L1/L2 catalog
preflight is the separate `list|auto` backend). ChatGPT in-process selects mailbox
from CHATGPT_EMAIL_SOURCE → matching profile (cf default clash:7897; tinyhost/gmail
variants), with CHATGPT_* env overrides forwarded as register_core CLI flags.
Grok/MiMo adapters still shell out to the legacy runners internally (register_cli.py
/ providers/mimo Node runner) as adapter targets + rollback
(GROK_LEGACY / MIMO_LEGACY / CHATGPT_LEGACY=1). In-process providers (chatgpt)
consume EmailSource directly.
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
    try:
        from register_core.mailbox.registry import list_mailbox_providers
        from register_core.decode.registry import list_otp_decoders

        print("mailbox:", ", ".join(list_mailbox_providers()))
        print("decode:", ", ".join(list_otp_decoders()))
    except Exception as exc:
        print("mailbox/decode: (unavailable)", exc)
    print(
        "layers: contracts → mailbox/decode → providers → verify → sink → pipeline → cli"
    )
    print(
        "nodes: python -m register_core nodes "
        "import|validate|list|check|add|clear|core|egress"
    )
    print(
        "egress primary: list|core|direct  "
        "(advanced: auto|clash) via REGISTER_EGRESS / --egress / nodes egress set"
    )
    print(
        "profile: python -m register_core run --profile profiles/<name>.yaml "
        "(register.v1; mailbox+decode+strategy). "
        "Legacy flags still work. Hub: ./register.sh grok|mimo|chatgpt"
    )
    print(
        "note: chatgpt/mimo/grok all accept profile mailbox+decode "
        "(CompositeEmailSource → FIXED_EMAIL / OTP_HELPER inject for shell runners). "
        "strategy.burn + fail_fast_kinds are live via StrategyEngine."
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
    headless = None if args.headless is None else bool(args.headless)
    profile_path = str(getattr(args, "profile", "") or "").strip()

    if profile_path:
        from register_core.config.loader import (
            ProfileLoadError,
            apply_cli_overrides,
            load_profile,
        )

        try:
            profile = load_profile(profile_path)
        except ProfileLoadError as exc:
            print(
                json.dumps({"ok": 0, "fail": 1, "error": str(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2
        # Optional: CLI -p must match profile provider when both set
        if getattr(args, "provider", None):
            if str(args.provider).strip().lower() != profile.provider.name.strip().lower():
                print(
                    json.dumps(
                        {
                            "ok": 0,
                            "fail": 1,
                            "error": (
                                f"--provider {args.provider!r} != profile provider "
                                f"{profile.provider.name!r}"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    file=sys.stderr,
                )
                return 2
        # --count default is None so profile.count is not clobbered by argparse=1.
        cli_count = args.count if getattr(args, "count", None) is not None else None
        job, sink_path = apply_cli_overrides(
            profile,
            count=cli_count,
            no_verify=bool(args.no_verify),
            no_fail_fast=bool(args.no_fail_fast),
            egress=str(getattr(args, "egress", "") or ""),
            proxy=str(getattr(args, "proxy", "") or ""),
            proxy_list=str(getattr(args, "proxy_list", "") or ""),
            sink=str(args.sink or ""),
            timeout=args.timeout,
            threads=args.threads,
            headless=headless,
        )
        # Explicit -n wins; otherwise keep profile / apply_cli_overrides result.
        if cli_count is not None:
            job.count = int(cli_count)
        sink = JsonlSink(sink_path) if sink_path else None
        try:
            pipe = Pipeline.from_profile(profile, sink=sink, overrides={
                "count": job.count,
                "verify": job.verify,
                "fail_fast": job.fail_fast,
                "egress": (job.extra or {}).get("egress"),
                "proxy": (job.extra or {}).get("proxy"),
                "proxy_list": (job.extra or {}).get("proxy_list"),
                "timeout_s": (job.extra or {}).get("timeout_s"),
                "threads": (job.extra or {}).get("threads"),
                "headless": (job.extra or {}).get("headless"),
            })
        except ValueError as exc:
            print(
                json.dumps({"ok": 0, "fail": 1, "error": str(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2
        stats = pipe.run(job.count, extra=job.extra)
    else:
        if not getattr(args, "provider", None):
            print(
                json.dumps(
                    {
                        "ok": 0,
                        "fail": 1,
                        "error": "require --profile or --provider / -p",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        sink = JsonlSink(args.sink) if args.sink else None
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
        if (
            getattr(args, "proxy_rotate_every", None) is not None
            and args.proxy_rotate_every >= 1
        ):
            extra["proxy_rotate_every"] = int(args.proxy_rotate_every)
        if getattr(args, "proxy_rotate_required", False):
            extra["proxy_rotate_required"] = True

        job = RegisterJob(
            provider=args.provider,
            # Non-profile path: default 1 when -n omitted (profile path keeps profile.count).
            count=int(args.count) if args.count is not None else 1,
            email_source=args.email_source,
            verify=not args.no_verify,
            fail_fast=not args.no_fail_fast,
            extra=extra,
        )

        try:
            pipe = Pipeline.from_job(job, sink=sink)
        except ValueError as exc:
            print(
                json.dumps({"ok": 0, "fail": 1, "error": str(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
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
    pr.add_argument(
        "--profile",
        default="",
        help="register.v1 YAML/JSON profile (mailbox+decode+strategy+provider)",
    )
    pr.add_argument(
        "--provider",
        "-p",
        default="",
        help="grok | mimo | chatgpt (required unless --profile)",
    )
    pr.add_argument(
        "--count",
        "-n",
        type=int,
        default=None,
        help="override profile count (omit to keep profile.count; default was clobbering)",
    )
    pr.add_argument(
        "--email-source",
        default="provider",
        help=(
            "legacy: provider=adapter-internal mail (required for grok/mimo). "
            "chatgpt: cloudflare|gmail_imap|tinyhost|duckmail|auto. "
            "Prefer --profile for mailbox+decode split."
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
