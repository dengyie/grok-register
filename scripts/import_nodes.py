#!/usr/bin/env python3
"""Import proxy profiles into project nodes (canonical script name).

Prefer:
  python -m register_core nodes import path/to/profile.yaml
  python -m register_core nodes validate path/to/profile.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from register_core.nodes.convert.cli_import import run_import  # noqa: E402
from register_core.nodes.convert.types import DEFAULT_CONTROLLER, DEFAULT_MIXED_PORT  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Import YAML / V2Ray JSON / URI into nodes.json + runtime.yaml"
    )
    ap.add_argument("paths", nargs="*", type=Path, help="profile files or dirs")
    ap.add_argument("--format", default="", help="clash_yaml|v2ray_json|uri_list")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replace", action="store_true", help="replace nodes.json (default merge)")
    ap.add_argument("--max-profile-proxies", type=int, default=400)
    ap.add_argument("--mixed-port", type=int, default=DEFAULT_MIXED_PORT)
    ap.add_argument("--controller", default=DEFAULT_CONTROLLER)
    ap.add_argument("--nodes-home", type=Path, default=_ROOT / ".nodes")
    ap.add_argument("--nodes-json", type=Path, default=_ROOT / "nodes.json")
    ap.add_argument(
        "--from-clash-verge",
        action="store_true",
        help="opt-in scan local Clash Verge profiles",
    )
    ap.add_argument("--clash-home", type=Path, default=None)
    args = ap.parse_args(argv)

    clash_home = args.clash_home
    if args.from_clash_verge and clash_home is None:
        clash_home = (
            Path.home()
            / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
        )

    return run_import(
        [str(p) for p in (args.paths or [])],
        format_hint=args.format,
        nodes_home=args.nodes_home,
        nodes_json=args.nodes_json,
        mixed_port=int(args.mixed_port),
        controller=str(args.controller),
        max_profile_proxies=int(args.max_profile_proxies),
        dry_run=bool(args.dry_run),
        replace_nodes=bool(args.replace),
        from_clash_verge=bool(args.from_clash_verge),
        clash_home=clash_home,
    )


if __name__ == "__main__":
    raise SystemExit(main())
