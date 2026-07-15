"""Append RegisterResult as JSONL (private secrets, mode 0o600)."""

from __future__ import annotations

import os
import json
from pathlib import Path

from register_core.contracts import RegisterResult


class JsonlSink:
    name = "jsonl"

    def __init__(self, path: str | Path, *, public_only: bool = False) -> None:
        self.path = Path(path)
        self.public_only = public_only
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, result: RegisterResult) -> None:
        payload = result.to_public_dict() if self.public_only else result.to_sink_dict()
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        path = self.path
        flags = os.O_APPEND | os.O_WRONLY | os.O_CREAT
        # Create with 0600 so umask cannot leave a world-readable secret file.
        fd = os.open(str(path), flags, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "ab") as f:
                f.write(data)
                fd = -1  # fdopen owns it
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
