"""Atomic write of CPA xAI auth files (mode 0600)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .schema import credential_file_name


def write_cpa_xai_auth(
    auth_dir: str | Path,
    payload: dict[str, Any],
    *,
    filename: str | None = None,
) -> Path:
    """Write payload to auth_dir/xai-<email>.json atomically. Returns final path."""
    auth_dir = Path(auth_dir).expanduser().resolve()
    auth_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        filename = credential_file_name(
            str(payload.get("email") or ""),
            str(payload.get("sub") or ""),
        )
    if not filename.endswith(".json"):
        filename = filename + ".json"

    dest = auth_dir / filename
    data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".xai-", suffix=".tmp", dir=str(auth_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, dest)
        os.chmod(dest, 0o600)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return dest
