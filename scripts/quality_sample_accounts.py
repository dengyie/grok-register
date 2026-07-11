#!/usr/bin/env python3
"""Quality sample for registered accounts + CPA auths (no secret echo).

Checks ledger shape, SSO normalize, CPA file presence/schema, optional live
API probe (models + short chat) when --live and network/proxy allow.

Example:
  uv run python -u scripts/quality_sample_accounts.py --sample 2 --live
  uv run python -u scripts/quality_sample_accounts.py --milestone 200 --live
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cpa_xai.accounts import (  # noqa: E402
    email_in_existing,
    existing_cpa_emails,
    normalize_sso_cookie,
    parse_accounts_file,
)
from cpa_xai.schema import credential_file_name  # noqa: E402


def _load_cfg(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
    }


def _proxy_from_cfg(cfg: dict) -> str:
    p = (cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
    if p:
        return p
    return (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip()


def _cpa_path_for_email(auth_dir: Path, email: str) -> Path | None:
    name = credential_file_name(email)
    p = auth_dir / name
    if p.is_file():
        return p
    # fallback scan by JSON email
    for f in auth_dir.glob("xai-*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if str(d.get("email") or "").strip().lower() == email.lower():
                return f
        except Exception:
            continue
    return None


def _check_cpa_file(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"path": str(path), "ok": False}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out["error"] = f"json: {e}"
        return out
    if not isinstance(d, dict):
        out["error"] = "not object"
        return out
    out["type"] = d.get("type")
    out["has_access"] = bool(d.get("access_token"))
    out["has_refresh"] = bool(d.get("refresh_token"))
    out["has_email"] = bool(str(d.get("email") or "").strip())
    out["base_url"] = str(d.get("base_url") or "")[:80]
    mode = path.stat().st_mode & 0o777
    out["mode"] = oct(mode)
    if d.get("type") != "xai":
        out["error"] = f"type={d.get('type')!r}"
        return out
    if not out["has_access"]:
        out["error"] = "missing access_token"
        return out
    out["ok"] = True
    out["access_token"] = d.get("access_token")  # internal only, never print
    out["email"] = d.get("email")
    out["headers"] = d.get("headers") if isinstance(d.get("headers"), dict) else {}
    return out


def _live_api(
    token: str,
    proxy: str,
    base_url: str,
    *,
    headers_extra: dict | None = None,
    try_chat: bool = False,
) -> dict[str, Any]:
    """Probe /v1/models (required). Optional chat — free Build tokens often 403 chat.

    Pass criteria (same as production cpa_xai.probe): models HTTP 200 + grok-4.5 listed.
    Never logs token.
    """
    base = (base_url or "https://cli-chat-proxy.grok.com/v1").rstrip("/")
    result: dict[str, Any] = {"ok": False, "models": None, "chat": None}
    try:
        from curl_cffi import requests as cf_requests
    except ImportError as e:
        result["error"] = f"curl_cffi missing: {e}"
        return result

    try:
        from cpa_xai.schema import DEFAULT_CLIENT_HEADERS
    except Exception:
        DEFAULT_CLIENT_HEADERS = {
            "x-grok-client-version": "0.2.93",
            "x-xai-token-auth": "xai-grok-cli",
            "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
        }

    try:
        sess = cf_requests.Session(impersonate="chrome")
    except TypeError:
        sess = cf_requests.Session()
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    headers = dict(DEFAULT_CLIENT_HEADERS)
    if isinstance(headers_extra, dict):
        headers.update({k: str(v) for k, v in headers_extra.items() if v is not None})
    headers["Authorization"] = f"Bearer {token}"
    headers["Content-Type"] = "application/json"
    try:
        r = sess.get(f"{base}/models", headers=headers, timeout=45)
        result["models_status"] = r.status_code
        if r.status_code != 200:
            result["error"] = f"models HTTP {r.status_code}"
            return result
        data = r.json()
        ids = []
        if isinstance(data, dict):
            for m in data.get("data") or []:
                if isinstance(m, dict) and m.get("id"):
                    ids.append(str(m["id"]))
        result["models"] = ids[:20]
        result["has_grok_45"] = any(i == "grok-4.5" or "grok-4.5" in i for i in ids)
        result["ok"] = bool(result["has_grok_45"])
        if not result["ok"]:
            result["error"] = "models missing grok-4.5"
    except Exception as e:  # noqa: BLE001
        result["error"] = f"models: {e}"
        return result

    if not try_chat:
        return result

    try:
        r2 = sess.post(
            f"{base}/chat/completions",
            headers=headers,
            json={
                "model": "grok-4.5",
                "messages": [{"role": "user", "content": "Reply with exactly OK"}],
                "max_tokens": 16,
                "stream": False,
            },
            timeout=90,
        )
        result["chat_status"] = r2.status_code
        if r2.status_code == 200:
            body = r2.json()
            text = ""
            try:
                text = (
                    body.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
            except Exception:
                text = ""
            result["chat"] = (text or "")[:40]
        else:
            # informational only — free tokens often deny chat on direct proxy
            result["chat_error"] = f"chat HTTP {r2.status_code}"
    except Exception as e:  # noqa: BLE001
        result["chat_error"] = f"chat: {e}"
    return result


def sample_quality(
    *,
    accounts_file: Path,
    auth_dir: Path,
    sample_n: int = 2,
    live: bool = False,
    proxy: str = "",
    seed: int | None = None,
) -> dict[str, Any]:
    rows = parse_accounts_file(accounts_file)
    have = existing_cpa_emails(auth_dir)
    report: dict[str, Any] = {
        "ts": int(time.time()),
        "accounts_total": len(rows),
        "cpa_files": len(list(auth_dir.glob("xai-*.json"))) if auth_dir.is_dir() else 0,
        "sample_n": 0,
        "ok": True,
        "items": [],
        "summary": {},
    }
    if not rows:
        report["ok"] = False
        report["error"] = "empty accounts"
        return report

    rng = random.Random(seed if seed is not None else time.time_ns())
    # Prefer recent accounts for quality of current pipeline
    pool = rows[-min(len(rows), max(50, sample_n * 20)) :]
    picks = pool if len(pool) <= sample_n else rng.sample(pool, sample_n)
    report["sample_n"] = len(picks)

    ledger_ok = sso_ok = cpa_ok = live_ok = 0
    live_ran = 0

    for acc in picks:
        item: dict[str, Any] = {
            "email": acc.email,
            "line_no": acc.line_no,
            "ledger_ok": False,
            "sso_ok": False,
            "cpa_ok": False,
            "live_ok": None,
        }
        # ledger
        if acc.email and "@" in acc.email and acc.password:
            item["ledger_ok"] = True
            ledger_ok += 1
        sso = normalize_sso_cookie(acc.sso)
        item["sso_prefix"] = (sso[:4] + "…") if sso else ""
        if sso.startswith("eyJ") and sso.count(".") >= 2:
            item["sso_ok"] = True
            sso_ok += 1
        else:
            item["sso_problem"] = "missing_or_not_jwt"

        # cpa present / schema
        matched = email_in_existing(acc.email, have)
        item["cpa_matched"] = matched
        cpath = _cpa_path_for_email(auth_dir, acc.email)
        if cpath:
            cinfo = _check_cpa_file(cpath)
            item["cpa_ok"] = bool(cinfo.get("ok"))
            item["cpa_file"] = cpath.name
            item["cpa_type"] = cinfo.get("type")
            item["cpa_has_access"] = cinfo.get("has_access")
            if cinfo.get("ok"):
                cpa_ok += 1
            else:
                item["cpa_error"] = cinfo.get("error")
            if live and cinfo.get("ok") and cinfo.get("access_token"):
                live_ran += 1
                lres = _live_api(
                    str(cinfo["access_token"]),
                    proxy,
                    str(cinfo.get("base_url") or "https://cli-chat-proxy.grok.com/v1"),
                    headers_extra=cinfo.get("headers") if isinstance(cinfo.get("headers"), dict) else None,
                    try_chat=False,
                )
                item["live_ok"] = bool(lres.get("ok"))
                item["live_models_status"] = lres.get("models_status")
                item["live_has_grok_45"] = lres.get("has_grok_45")
                if lres.get("error"):
                    item["live_error"] = lres["error"]
                if item["live_ok"]:
                    live_ok += 1
        else:
            item["cpa_error"] = "file_missing"

        if not item["ledger_ok"] or not item["sso_ok"] or not item["cpa_ok"]:
            report["ok"] = False
        if live and item.get("live_ok") is False:
            report["ok"] = False

        report["items"].append(item)

    report["summary"] = {
        "ledger_ok": f"{ledger_ok}/{len(picks)}",
        "sso_ok": f"{sso_ok}/{len(picks)}",
        "cpa_ok": f"{cpa_ok}/{len(picks)}",
        "live_ok": f"{live_ok}/{live_ran}" if live else "skipped",
        "cpa_coverage_hint": f"cpa_files={report['cpa_files']} accounts={report['accounts_total']}",
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--accounts", default=str(_ROOT / "accounts_cli.txt"))
    ap.add_argument("--auth-dir", default=str(_ROOT / "cpa_auths"))
    ap.add_argument("--config", default=str(_ROOT / "config.json"))
    ap.add_argument("--sample", type=int, default=2, help="how many accounts to sample")
    ap.add_argument("--live", action="store_true", help="hit cli-chat-proxy with access_token")
    ap.add_argument("--milestone", type=int, default=0, help="label only")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--out",
        default="",
        help="optional JSON report path (no tokens written)",
    )
    args = ap.parse_args()

    cfg = _load_cfg(Path(args.config))
    proxy = _proxy_from_cfg(cfg)
    auth_dir = Path(args.auth_dir)
    if not auth_dir.is_absolute():
        auth_dir = (_ROOT / auth_dir).resolve()

    report = sample_quality(
        accounts_file=Path(args.accounts),
        auth_dir=auth_dir,
        sample_n=max(1, args.sample),
        live=bool(args.live),
        proxy=proxy,
        seed=args.seed,
    )
    if args.milestone:
        report["milestone"] = args.milestone

    # Print safe summary only
    print(
        f"[quality] milestone={args.milestone or '-'} "
        f"accounts={report['accounts_total']} cpa={report['cpa_files']} "
        f"ok={report['ok']} summary={report['summary']}",
        flush=True,
    )
    for it in report.get("items") or []:
        email = it.get("email") or ""
        # redact local-part middle
        if "@" in email:
            local, _, domain = email.partition("@")
            safe = (local[:3] + "***@" + domain) if len(local) > 3 else "***@" + domain
        else:
            safe = "***"
        print(
            f"  - {safe} ledger={it.get('ledger_ok')} sso={it.get('sso_ok')} "
            f"cpa={it.get('cpa_ok')} live={it.get('live_ok')} "
            f"err={it.get('cpa_error') or it.get('live_error') or it.get('sso_problem') or '-'}",
            flush=True,
        )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        safe = json.loads(json.dumps(report, ensure_ascii=False, default=str))
        for it in safe.get("items") or []:
            it.pop("access_token", None)
        out_path.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[quality] wrote {out_path}", flush=True)

    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
