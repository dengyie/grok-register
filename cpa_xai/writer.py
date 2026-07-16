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

    Never writes ``chat_ok`` / ``usable`` as JSON null — omit those keys until a
    real probe outcome exists (keeps inventory ``chat_ok_missing`` accurate).
    """
    r = result or {}
    chat_ok = r.get("chat_ok")
    if chat_ok is None:
        ch = r.get("probe_chat") or {}
        if isinstance(ch, dict) and ch:
            if "ok" in ch:
                chat_ok = bool(ch.get("ok"))
            elif ch.get("entitlement_denied"):
                chat_ok = False

    entitlement_denied = bool(r.get("entitlement_denied"))
    if not entitlement_denied and isinstance(r.get("probe_chat"), dict):
        entitlement_denied = bool((r.get("probe_chat") or {}).get("entitlement_denied"))

    usable = r.get("usable")
    if usable is None and chat_ok is True and not entitlement_denied:
        usable = True
    elif usable is None and (chat_ok is False or entitlement_denied):
        usable = False
    elif usable is None and r.get("ok") is True and chat_ok is None and not entitlement_denied:
        # models-only success path without chat probe — do not invent usable=true
        usable = None

    chat_retryable = bool(r.get("chat_retryable")) and not entitlement_denied
    if not chat_retryable and chat_ok is not True and not entitlement_denied:
        ch = r.get("probe_chat") or {}
        if isinstance(ch, dict) and ch.get("retryable") and not ch.get("ok"):
            chat_retryable = True

    fail_reason = str(r.get("fail_reason") or "").strip()
    if not fail_reason and entitlement_denied:
        fail_reason = "entitlement_denied"
    if not fail_reason and isinstance(r.get("probe_chat"), dict):
        pr = r.get("probe_chat") or {}
        if pr.get("reason") and not pr.get("ok"):
            fail_reason = str(pr.get("reason") or "")

    chat_error_code = str(r.get("chat_error_code") or "").strip()
    if not chat_error_code and isinstance(r.get("probe_chat"), dict):
        chat_error_code = str((r.get("probe_chat") or {}).get("error_code") or "").strip()

    if entitlement_denied:
        import_gate = "entitlement_denied"
    elif chat_ok is True and usable is not False:
        import_gate = "chat_ok"
    elif r.get("import_gate"):
        import_gate = str(r.get("import_gate"))
    elif chat_retryable:
        import_gate = fail_reason or "transient"
    elif chat_ok is False:
        import_gate = fail_reason or "chat_not_ok"
    else:
        import_gate = fail_reason or ("ok" if r.get("ok") else "not_ready")

    stamp: dict[str, Any] = {
        "entitlement_denied": entitlement_denied,
        "chat_retryable": chat_retryable and not entitlement_denied,
        "import_gate": import_gate,
    }
    # Only persist booleans — never null (null pollutes inventory as "stamped").
    if chat_ok is True or chat_ok is False:
        stamp["chat_ok"] = bool(chat_ok)
    if usable is True or usable is False:
        stamp["usable"] = bool(usable)
    if fail_reason:
        stamp["fail_reason"] = fail_reason
    if chat_error_code:
        stamp["chat_error_code"] = chat_error_code

    # Mint path observability (not product gates): which grant produced tokens.
    # Dual stamp contract: mint.py writes mint_method/protocol_error at create
    # (build_cpa_xai_auth extra=) and again after probe via stamp_auth_chat_fields
    # so chat patches never drop residual-path labels (protocol_device, etc.).
    mint_method = str(r.get("mint_method") or "").strip()
    if mint_method:
        stamp["mint_method"] = mint_method
    protocol_error = str(r.get("protocol_error") or "").strip()
    if protocol_error:
        # Cap size so auth JSON stays ops-friendly when PKCE dumps long HTML errors.
        stamp["protocol_error"] = protocol_error[:500]
    return stamp


def stamp_auth_chat_fields(
    path: str | Path,
    result: dict[str, Any] | None = None,
    *,
    updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge chat stamp from result/updates into auth file. Returns full payload.

    Omits null ``chat_ok``/``usable`` from the patch so incomplete results do not
    mark historical files as stamped. Explicit ``updates`` may still set booleans
    (including False); null values in updates are ignored for those keys.
    """
    stamp = build_chat_stamp_from_result(result) if result is not None else {}
    if updates:
        for k, v in updates.items():
            if k in ("chat_ok", "usable"):
                if v is True or v is False:
                    stamp[k] = bool(v)
                # skip None — never write null chat_ok/usable
                continue
            if v is not None:
                stamp[k] = v
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
