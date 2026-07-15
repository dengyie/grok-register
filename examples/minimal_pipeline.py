#!/usr/bin/env python3
"""Minimal layered pipeline example (offline-safe).

Does not open browsers or touch production CPA.
Lists registered providers and shows public redact shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from register_core.contracts import RegisterResult  # noqa: E402
from register_core.providers.registry import list_providers  # noqa: E402


def main() -> None:
    print("providers:", ", ".join(list_providers()))
    sample = RegisterResult(
        ok=True,
        provider="demo",
        email="user@example.com",
        password="super-secret-password",
        secret="sk-abcdefghijklmnopqrstuvwxyz0123456789",
        secret_kind="api_key",
    )
    print("public:", sample.to_public_dict())
    assert "super-secret" not in str(sample.to_public_dict())
    print("ok: redact works")


if __name__ == "__main__":
    main()
