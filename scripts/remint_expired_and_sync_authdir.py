#!/usr/bin/env python3
"""Remint expired/missing CPA xai auths from ledger SSO, then inject live CPA auth-dir.

Local write: ./cpa_auths
Remote inject (default):
  1) /root/.cli-proxy-api   — CPA config auth-dir (live pool)
  2) /personal/cpa/auths    — inventory/backup dir used by register inject

Does NOT start new registration. Fail-fast on missing curl_cffi / no SSO pool.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cpa_xai.accounts import normalize_sso_cookie, parse_accounts_file  # noqa: E402
from cpa_xai.schema import credential_file_name  # noqa: E402


def _load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"warn: config read failed: {e}", flush=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
    }


def _is_expired(payload: dict, now: datetime) -> bool:
    exp = (payload.get("expired") or "").strip()
    if not exp:
        return True
    try:
        dt = datetime.strptime(exp.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return dt < now
    except Exception:
        return True


def _collect_todo(
    accounts_path: Path,
    auth_dir: Path,
    *,
    include_missing: bool,
    include_expired: bool,
    include_chat_retryable: bool,
    only_email: str,
    limit: int,
    denied_emails: set[str] | None = None,
) -> tuple:
    from cpa_xai.writer import is_chat_retryable_auth, is_entitlement_denied_auth

    accounts = parse_accounts_file(str(accounts_path))
    by_email = {a.email.lower(): a for a in accounts if a.email and normalize_sso_cookie(a.sso)}
    now = datetime.now(timezone.utc)
    denied = {e.lower() for e in (denied_emails or set())}

    existing: dict[str, Path] = {}
    existing_payload: dict[str, dict] = {}
    for p in auth_dir.glob("xai-*.json"):
        if "_selftest" in str(p):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        em = (d.get("email") or "").strip().lower()
        if em:
            existing[em] = p
            existing_payload[em] = d if isinstance(d, dict) else {}

    todo = []
    reasons: dict[str, int] = {
        "expired": 0,
        "missing": 0,
        "chat_retryable": 0,
        "skipped_denied": 0,
        "skipped_chat_ok": 0,
    }

    def _skip_denied(em: str) -> bool:
        if em in denied:
            reasons["skipped_denied"] += 1
            return True
        payload = existing_payload.get(em) or {}
        if is_entitlement_denied_auth(payload):
            reasons["skipped_denied"] += 1
            return True
        return False

    # missing first (never minted)
    if include_missing:
        for em, acc in by_email.items():
            if only_email and em != only_email.lower():
                continue
            if _skip_denied(em):
                continue
            if em not in existing:
                todo.append((acc, "missing"))
                reasons["missing"] += 1

    # expired rewrite — never remint entitlement_denied accounts
    if include_expired:
        for em, p in existing.items():
            if only_email and em != only_email.lower():
                continue
            if em not in by_email:
                continue
            if _skip_denied(em):
                continue
            d = existing_payload.get(em) or {}
            # Already chat_ok: expired tokens can still remint, but never denied.
            if _is_expired(d, now):
                todo.append((by_email[em], "expired"))
                reasons["expired"] += 1

    # unexpired but chat probe was transient — re-probe without waiting for token expiry
    if include_chat_retryable:
        for em, p in existing.items():
            if only_email and em != only_email.lower():
                continue
            if em not in by_email:
                continue
            if _skip_denied(em):
                continue
            d = existing_payload.get(em) or {}
            # Never re-queue permanent chat_ok=False entitlement via retryable path
            if d.get("chat_ok") is True and d.get("usable") is not False and not _is_expired(d, now):
                reasons["skipped_chat_ok"] += 1
                continue
            if is_chat_retryable_auth(d) and not _is_expired(d, now):
                todo.append((by_email[em], "chat_retryable"))
                reasons["chat_retryable"] += 1

    # de-dup keep first reason
    seen = set()
    out = []
    for acc, reason in todo:
        key = acc.email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((acc, reason))
        if limit and len(out) >= limit:
            break
    return out, reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--accounts", default=str(_ROOT / "accounts_cli.txt"))
    ap.add_argument("--config", default=str(_ROOT / "config.json"))
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--limit", type=int, default=0, help="0=all candidates")
    ap.add_argument("--email", default="")
    ap.add_argument("--sleep", type=float, default=3.0)
    ap.add_argument("--no-missing", action="store_true")
    ap.add_argument("--no-expired", action="store_true")
    ap.add_argument(
        "--include-chat-retryable",
        action="store_true",
        default=True,
        help="Also remint unexpired auths stamped chat_retryable (transient chat fail)",
    )
    ap.add_argument(
        "--no-chat-retryable",
        action="store_false",
        dest="include_chat_retryable",
    )
    ap.add_argument(
        "--include-denied",
        action="store_true",
        default=False,
        help="Do NOT skip entitlement_denied ledger (debug only; remint cannot grant chat)",
    )
    ap.add_argument(
        "--no-remote",
        action="store_true",
        help="Only local remint, skip tebi inject",
    )
    ap.add_argument(
        "--auth-dirs",
        default="/root/.cli-proxy-api,/personal/cpa/auths",
        help="Comma-separated remote CPA dirs to inject into (live first)",
    )
    ap.add_argument(
        "--fail-log",
        default=str(_ROOT / "cpa_auths" / "remint_failed.jsonl"),
    )
    ap.add_argument(
        "--state",
        default=str(_ROOT / "logs" / "remint_sync_state.json"),
    )
    args = ap.parse_args()

    cfg = _load_config(Path(args.config))
    out_dir = Path(args.out_dir or cfg.get("cpa_auth_dir") or (_ROOT / "cpa_auths"))
    if not out_dir.is_absolute():
        out_dir = (_ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    from cpa_xai.writer import load_entitlement_denied_emails

    denied = set() if args.include_denied else load_entitlement_denied_emails(out_dir)
    if denied:
        print(f"entitlement_denied ledger size={len(denied)} (skipped unless --include-denied)", flush=True)

    todo, reasons = _collect_todo(
        Path(args.accounts),
        out_dir,
        include_missing=not args.no_missing,
        include_expired=not args.no_expired,
        include_chat_retryable=bool(args.include_chat_retryable),
        only_email=args.email,
        limit=args.limit,
        denied_emails=denied,
    )
    remote_dirs = [d.strip() for d in (args.auth_dirs or "").split(",") if d.strip()]
    if args.no_remote:
        remote_dirs = []

    print(
        f"candidates={len(todo)} reasons={reasons} out={out_dir} "
        f"remote_dirs={remote_dirs} sleep={args.sleep}",
        flush=True,
    )
    if not todo:
        print("nothing to do", flush=True)
        return 0

    import cpa_export

    ok_n = fail_n = inject_ok = inject_fail = 0
    chat_ok_n = chat_denied_n = chat_fail_n = 0
    t0 = time.time()
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    for i, (acc, reason) in enumerate(todo, 1):
        print(f"\n=== [{i}/{len(todo)}] {reason} {acc.email} ===", flush=True)

        def log(msg: str, _email=acc.email) -> None:
            print(f"[{time.strftime('%H:%M:%S')}] [{_email}] {msg}", flush=True)

        run_cfg = dict(cfg)
        run_cfg["cpa_auth_dir"] = str(out_dir)
        run_cfg["cpa_prefer_protocol"] = True
        run_cfg["cpa_protocol_only"] = True
        run_cfg["cpa_probe_after_write"] = True
        # Remint cannot grant free Build chat entitlement. Probe chat so we
        # fail-fast and skip tebi inject for permission-denied accounts.
        run_cfg["cpa_probe_chat"] = True
        run_cfg["cpa_probe_chat_required"] = True
        # Prefer unified multi-dir inject from cpa_export (same as register one-click).
        if remote_dirs:
            run_cfg["cpa_remote_inject"] = True
            run_cfg["cpa_remote_auth_dirs"] = remote_dirs
        else:
            run_cfg["cpa_remote_inject"] = False
        run_cfg["cpa_auth_priority"] = int(cfg.get("cpa_auth_priority", 1000) or 1000)

        r = cpa_export.export_cpa_xai_for_account(
            acc.email,
            acc.password or "x",
            page=None,
            sso=acc.sso or None,
            config=run_cfg,
            log_callback=log,
        )
        rec = {
            "email": acc.email,
            "reason": reason,
            "ok": bool(r.get("ok")),
            "chat_ok": r.get("chat_ok"),
            "entitlement_denied": bool(r.get("entitlement_denied")),
            "chat_retryable": bool(r.get("chat_retryable")),
            "import_gate": r.get("import_gate"),
            "fail_reason": r.get("fail_reason"),
            "error": r.get("error"),
            "path": r.get("path"),
            "mint_method": r.get("mint_method"),
            "priority": r.get("priority"),
            "remote": r.get("remote_injects") or [],
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        if r.get("entitlement_denied"):
            chat_denied_n += 1
        elif r.get("chat_ok") is True:
            chat_ok_n += 1
        elif r.get("chat_ok") is False or r.get("fail_reason") or r.get("error"):
            chat_fail_n += 1

        if r.get("ok") and r.get("path") and r.get("chat_ok") is True:
            ok_n += 1
            multi = r.get("remote_injects") or []
            if multi:
                for item in multi:
                    if item.get("ok"):
                        inject_ok += 1
                    elif not item.get("skipped"):
                        inject_fail += 1
            elif remote_dirs and not r.get("remote_inject_skipped"):
                # export should have multi-injected; count as fail visibility
                inject_fail += len(remote_dirs)
        else:
            fail_n += 1
            Path(args.fail_log).parent.mkdir(parents=True, exist_ok=True)
            with open(args.fail_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            # Ensure entitlement lands in ledger even if export path missed it
            if r.get("entitlement_denied"):
                try:
                    from cpa_xai.writer import record_entitlement_denied

                    record_entitlement_denied(
                        out_dir,
                        acc.email,
                        extra={
                            "path": r.get("path"),
                            "source": "remint",
                            "chat_error_code": r.get("chat_error_code"),
                        },
                    )
                except Exception as e:  # noqa: BLE001
                    log(f"entitlement ledger write failed: {e}")

        # periodic state
        if i == 1 or i % 10 == 0 or i == len(todo):
            state = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "done": i,
                "total": len(todo),
                "ok": ok_n,
                "fail": fail_n,
                "chat_ok": chat_ok_n,
                "chat_denied": chat_denied_n,
                "chat_fail": chat_fail_n,
                "skipped_denied": reasons.get("skipped_denied", 0),
                "inject_ok": inject_ok,
                "inject_fail": inject_fail,
                "elapsed_s": round(time.time() - t0, 1),
                "last_email": acc.email,
                "last_ok": bool(r.get("ok")),
                "last_chat_ok": r.get("chat_ok"),
                "last_import_gate": r.get("import_gate"),
            }
            state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        if args.sleep and i < len(todo):
            time.sleep(args.sleep)

    print(
        f"\n=== done ok={ok_n} fail={fail_n} "
        f"chat_ok={chat_ok_n} chat_denied={chat_denied_n} chat_fail={chat_fail_n} "
        f"skipped_denied={reasons.get('skipped_denied', 0)} "
        f"inject_ok={inject_ok} inject_fail={inject_fail} "
        f"elapsed={round(time.time()-t0,1)}s ===",
        flush=True,
    )
    return 0 if fail_n == 0 or ok_n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
