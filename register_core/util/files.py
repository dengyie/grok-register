"""File offset helpers for one-shot result attribution."""

from __future__ import annotations

from pathlib import Path


def file_size(path: str | Path) -> int:
    p = Path(path)
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def read_appended(path: str | Path, offset: int) -> str:
    """Read bytes written after offset (empty if truncated/missing)."""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        size = p.stat().st_size
        if size <= offset:
            return ""
        with p.open("rb") as f:
            f.seek(max(0, offset))
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""
