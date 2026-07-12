"""Atomic write of CPA xAI auth files (mode 0600)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .schema import credential_file_name

ENTITLEMENT_DENIED_LEDGER = "entitlement_denied.jsonl"


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


def patch_cpa_xai_auth(path: str | Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge updates into an existing auth JSON file (atomic). Returns full payload."""
    p = Path(path).expanduser().resolve()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(updates or {})
    write_cpa_xai_auth(
        p.parent,
        data,
        filename=p.name,
    )
    return data


def entitlement_denied_ledger_path(auth_dir: str | Path) -> Path:
    return Path(auth_dir).expanduser().resolve() / ENTITLEMENT_DENIED_LEDGER


def load_entitlement_denied_emails(auth_dir: str | Path) -> set[str]:
    """Emails permanently without free Build chat (from ledger + stamped auth files)."""
    root = Path(auth_dir).expanduser().resolve()
    out: set[str] = set()
    ledger = root / ENTITLEMENT_DENIED_LEDGER
    if ledger.is_file():
        try:
            for line in ledger.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    # plain email line
                    em = line.split("----", 1)[0].strip().lower()
                    if "@" in em:
                        out.add(em)
                    continue
                if isinstance(rec, dict):
                    em = str(rec.get("email") or "").strip().lower()
                    if em:
                        out.add(em)
        except Exception:
            pass
    for p in root.glob("xai-*.json"):
        if "_selftest" in p.name:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("entitlement_denied") is True or (
            d.get("usable") is False and d.get("fail_reason") == "entitlement_denied"
        ):
            em = str(d.get("email") or "").strip().lower()
            if em:
                out.add(em)
    return out


def record_entitlement_denied(
    auth_dir: str | Path,
    email: str,
    *,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Append one ledger line; idempotent for same email (still appends for audit)."""
    from datetime import datetime, timezone

    root = Path(auth_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / ENTITLEMENT_DENIED_LEDGER
    rec = {
        "email": (email or "").strip().lower(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "reason": "entitlement_denied",
    }
    if extra:
        for k, v in extra.items():
            if k not in rec and v is not None:
                rec[k] = v
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def is_chat_retryable_auth(payload: dict[str, Any] | None) -> bool:
    """True when local auth exists but chat probe was transient-failed (re-probe candidate)."""
    d = payload or {}
    if d.get("entitlement_denied") is True:
        return False
    if d.get("chat_ok") is True and d.get("usable") is not False:
        return False
    if d.get("chat_retryable") is True:
        return True
    if d.get("fail_reason") == "transient":
        return True
    return False
