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
        if is_entitlement_denied_auth(d):
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


def is_entitlement_denied_auth(payload: dict[str, Any] | None) -> bool:
    """True when auth is known permanent free-Build chat denial (do not remint)."""
    d = payload or {}
    if d.get("entitlement_denied") is True:
        return True
    if d.get("fail_reason") == "entitlement_denied":
        return True
    if d.get("import_gate") == "entitlement_denied":
        return True
    if d.get("usable") is False and d.get("chat_ok") is False and (
        str(d.get("fail_reason") or "") == "entitlement_denied"
        or str(d.get("import_gate") or "") == "entitlement_denied"
    ):
        return True
    return False


def is_chat_retryable_auth(payload: dict[str, Any] | None) -> bool:
    """True when local auth exists but chat probe was transient-failed (re-probe candidate)."""
    d = payload or {}
    if is_entitlement_denied_auth(d):
        return False
    if d.get("chat_ok") is True and d.get("usable") is not False:
        return False
    if d.get("chat_retryable") is True:
        return True
    if d.get("fail_reason") == "transient":
        return True
    return False


def build_chat_stamp_from_result(result: dict[str, Any] | None) -> dict[str, Any]:
    """Build auth-file stamp fields from a mint/export result dict.

    Used by mint after probe and by export after finalize/gate so disk stamps
    always match product chat_ok / entitlement classification.
    """
    r = result or {}
    if r.get("entitlement_denied"):
        import_gate = "entitlement_denied"
    elif r.get("chat_ok") is True and r.get("usable") is not False:
        import_gate = "chat_ok"
    elif r.get("import_gate"):
        import_gate = str(r.get("import_gate"))
    elif r.get("chat_retryable"):
        import_gate = str(r.get("fail_reason") or "transient")
    elif r.get("chat_ok") is False:
        import_gate = str(r.get("fail_reason") or "chat_not_ok")
    else:
        import_gate = str(r.get("fail_reason") or ("ok" if r.get("ok") else "not_ready"))

    stamp: dict[str, Any] = {
        "chat_ok": r.get("chat_ok"),
        "usable": r.get("usable", r.get("ok")),
        "entitlement_denied": bool(r.get("entitlement_denied")),
        "chat_retryable": bool(r.get("chat_retryable")) and not bool(r.get("entitlement_denied")),
        "fail_reason": r.get("fail_reason") or "",
        "chat_error_code": r.get("chat_error_code") or "",
        "import_gate": import_gate,
    }
    if stamp["chat_ok"] is None and r.get("probe_chat"):
        ch = r.get("probe_chat") or {}
        if isinstance(ch, dict) and ch:
            stamp["chat_ok"] = bool(ch.get("ok"))
    if not stamp["fail_reason"]:
        stamp.pop("fail_reason", None)
    if not stamp["chat_error_code"]:
        stamp.pop("chat_error_code", None)
    return stamp


def stamp_auth_chat_fields(
    path: str | Path,
    result: dict[str, Any] | None = None,
    *,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge chat stamp from result/updates into auth file. Returns full payload."""
    stamp = build_chat_stamp_from_result(result) if result is not None else {}
    if updates:
        stamp.update({k: v for k, v in updates.items() if v is not None or k in ("chat_ok", "usable")})
    if not stamp:
        return {}
    return patch_cpa_xai_auth(path, stamp)


def inventory_chat_stamps(auth_dir: str | Path) -> dict[str, Any]:
    """Count chat stamp coverage in an auth dir (ops / backfill planning)."""
    root = Path(auth_dir).expanduser().resolve()
    stats = {
        "total": 0,
        "chat_ok_true": 0,
        "chat_ok_false": 0,
        "chat_ok_missing": 0,
        "entitlement_denied": 0,
        "chat_retryable": 0,
        "usable_true": 0,
        "usable_false": 0,
        "import_gate": {},
    }
    for p in root.glob("xai-*.json"):
        if "_selftest" in p.name:
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        stats["total"] += 1
        if "chat_ok" not in d:
            stats["chat_ok_missing"] += 1
        elif d.get("chat_ok") is True:
            stats["chat_ok_true"] += 1
        else:
            stats["chat_ok_false"] += 1
        if is_entitlement_denied_auth(d):
            stats["entitlement_denied"] += 1
        if is_chat_retryable_auth(d):
            stats["chat_retryable"] += 1
        if d.get("usable") is True:
            stats["usable_true"] += 1
        elif d.get("usable") is False:
            stats["usable_false"] += 1
        gate = str(d.get("import_gate") or ("missing" if "chat_ok" not in d else "unset"))
        stats["import_gate"][gate] = int(stats["import_gate"].get(gate, 0)) + 1
    return stats
