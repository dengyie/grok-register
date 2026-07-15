#!/usr/bin/env python3
"""Unified layered register CLI.

Production authority for full signup remains:
  ./register.sh grok | ./register.sh mimo

This CLI orchestrates adapters with honest success attribution. Black-box
providers (grok/mimo) use adapter-internal mail only (email_source=provider).
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
        "note: grok/mimo are black-box adapters; use email_source=provider "
        "(default). Production runners: ./register.sh grok|mimo"
    )
    return 0


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

    pr = sub.add_parser("run", help="Run registration pipeline")
    pr.add_argument("--provider", "-p", required=True, help="grok | mimo")
    pr.add_argument("--count", "-n", type=int, default=1)
    pr.add_argument(
        "--email-source",
        default="provider",
        help=(
            "provider=adapter-internal mail (required for grok/mimo). "
            "tinyhost|duckmail|gmail_imap|legacy_grok|auto only for in-process providers."
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
    pr.add_argument("-v", "--verbose", action="store_true")
    pr.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
