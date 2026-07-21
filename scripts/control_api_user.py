#!/usr/bin/env python3
"""Manage control-plane operator accounts (password auth).

Examples:
  uv run python scripts/control_api_user.py set admin
  uv run python scripts/control_api_user.py set admin --password '...'
  uv run python scripts/control_api_user.py list
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.control_api.users import list_usernames, upsert_user, users_path  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Control API operator accounts")
    p.add_argument(
        "--root",
        default=os.environ.get("REGISTER_PROJECT_ROOT") or str(ROOT),
        help="project root (default REGISTER_PROJECT_ROOT or repo)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    set_p = sub.add_parser("set", help="create or update a user password")
    set_p.add_argument("username")
    set_p.add_argument("--password", default=None, help="if omitted, prompt")

    sub.add_parser("list", help="list usernames")

    args = p.parse_args()
    root = Path(args.root).resolve()

    if args.cmd == "list":
        users = list_usernames(root)
        print(f"path={users_path(root)}")
        for u in users:
            print(u)
        if not users:
            print("(no users)")
        return 0

    if args.cmd == "set":
        password = args.password
        if not password:
            password = getpass.getpass("password: ")
            again = getpass.getpass("confirm: ")
            if password != again:
                print("passwords do not match", file=sys.stderr)
                return 2
        try:
            upsert_user(root, args.username, password)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"ok user={args.username} path={users_path(root)}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
