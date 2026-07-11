#!/usr/bin/env python3
"""Batch mint CPA xai-*.json from register accounts_cli.txt.

Default path goes through ``cpa_export.export_cpa_xai_for_account`` so SSO
normalize, optional tebi remote inject, hotload copy, and local backup match
the live register pipeline.

Use ``--local-only`` to call mint_and_export directly (no remote inject).

Example (from grok-register project root):
  uv run python -u scripts/backfill_cpa_xai_from_accounts.py --limit 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cpa_xai import email_in_existing, existing_cpa_emails, parse_accounts_file  # noqa: E402


def _load_config(path: str | Path) -> dict:
    cfg_path = Path(path)
    if not cfg_path.is_file():
        return {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"warn: read config failed: {e}", flush=True)
        return {}
    if not isinstance(cfg, dict):
        return {}
    return {
        k: v
        for k, v in cfg.items()
        if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--accounts",
        default=str(_ROOT / "accounts_cli.txt"),
    )
    ap.add_argument(
        "--out-dir",
        default="",
        help="Override cpa_auth_dir (default: config or ./cpa_auths)",
    )
    ap.add_argument(
        "--cpa-dir",
        default="",
        help="Optional CPA hot-load auth-dir override",
    )
    ap.add_argument("--limit", type=int, default=0, help="0 = all missing")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--email", default="", help="Only this email")
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--no-skip-existing", action="store_false", dest="skip_existing")
    ap.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Headless Chromium (usually blocked by Cloudflare on accounts.x.ai)",
    )
    ap.add_argument(
        "--headed",
        action="store_true",
        default=True,
        help="Show browser (default; required for stable device consent)",
    )
    ap.add_argument("--probe", action="store_true", default=True)
    ap.add_argument("--no-probe", action="store_false", dest="probe")
    ap.add_argument("--probe-chat", action="store_true", default=False)
    ap.add_argument(
        "--proxy",
        default="",
        help="Outbound proxy. Empty → read register config.json cpa_proxy/proxy, else env",
    )
    ap.add_argument(
        "--config",
        default=str(_ROOT / "config.json"),
        help="register config.json for cpa_* defaults and remote inject",
    )
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--sleep", type=float, default=3.0, help="Sleep between accounts")
    ap.add_argument(
        "--fail-log",
        default=str(_ROOT / "cpa_auths" / "backfill_failed.jsonl"),
        help="Append failures JSONL",
    )
    ap.add_argument(
        "--force-standalone",
        action="store_true",
        default=True,
        help="Always open fresh Chromium (default)",
    )
    ap.add_argument(
        "--local-only",
        action="store_true",
        default=False,
        help="Bypass cpa_export (no remote inject / backup hooks); mint only",
    )
    ap.add_argument(
        "--no-remote",
        action="store_true",
        default=False,
        help="Keep cpa_export path but force cpa_remote_inject=false for this run",
    )
    args = ap.parse_args()

    if args.headless:
        args.headed = False
    else:
        args.headless = False

    cfg = _load_config(args.config)

    if not args.proxy:
        args.proxy = (cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
    if not args.proxy:
        args.proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()

    out_dir = (args.out_dir or cfg.get("cpa_auth_dir") or str(_ROOT / "cpa_auths")).strip()
    out_path = Path(out_dir).expanduser()
    if not out_path.is_absolute():
        out_path = (_ROOT / out_path).resolve()

    cpa_dir = (args.cpa_dir or cfg.get("cpa_hotload_dir") or "").strip()

    # Overlay CLI flags onto config for export path
    run_cfg = dict(cfg)
    run_cfg["cpa_auth_dir"] = str(out_path)
    if cpa_dir:
        run_cfg["cpa_hotload_dir"] = cpa_dir
        run_cfg["cpa_copy_to_hotload"] = True
    if args.proxy:
        run_cfg["cpa_proxy"] = args.proxy
    run_cfg["cpa_headless"] = bool(args.headless)
    run_cfg["cpa_probe_after_write"] = bool(args.probe)
    run_cfg["cpa_probe_chat"] = bool(args.probe_chat)
    run_cfg["cpa_mint_timeout_sec"] = float(args.timeout)
    run_cfg["cpa_force_standalone"] = bool(args.force_standalone)
    if args.no_remote:
        run_cfg["cpa_remote_inject"] = False

    print(
        f"proxy={args.proxy or '(none)'} out={out_path} "
        f"local_only={args.local_only} remote_inject={run_cfg.get('cpa_remote_inject')}",
        flush=True,
    )

    accounts = parse_accounts_file(args.accounts)
    if args.email:
        accounts = [a for a in accounts if a.email.lower() == args.email.lower()]
    accounts = accounts[args.offset :]

    have: set[str] = set()
    if args.skip_existing:
        have |= existing_cpa_emails(out_path)
        if cpa_dir:
            have |= existing_cpa_emails(cpa_dir)

    todo = []
    for a in accounts:
        if args.skip_existing and email_in_existing(a.email, have):
            continue
        todo.append(a)
        if args.limit and len(todo) >= args.limit:
            break

    print(
        f"accounts total={len(parse_accounts_file(args.accounts))} "
        f"todo={len(todo)} out={out_path}",
        flush=True,
    )
    out_path.mkdir(parents=True, exist_ok=True)
    if cpa_dir:
        Path(cpa_dir).expanduser().mkdir(parents=True, exist_ok=True)

    ok_n = fail_n = 0
    results = []
    for i, acc in enumerate(todo, 1):
        print(f"\n=== [{i}/{len(todo)}] {acc.email} ===", flush=True)

        def log(msg: str, _email=acc.email) -> None:
            print(f"[{time.strftime('%H:%M:%S')}] [{_email}] {msg}", flush=True)

        if args.local_only:
            from cpa_xai import mint_and_export

            r = mint_and_export(
                email=acc.email,
                password=acc.password,
                auth_dir=out_path,
                page=None,
                proxy=args.proxy or None,
                headless=args.headless,
                probe=args.probe,
                probe_chat=args.probe_chat,
                browser_timeout_sec=args.timeout,
                force_standalone=args.force_standalone,
                sso=acc.sso or None,
                prefer_protocol=True,
                log=log,
            )
            if r.get("ok") and r.get("path") and cpa_dir:
                import shutil

                src = Path(r["path"])
                dst = Path(cpa_dir).expanduser() / src.name
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
                print(f"copied -> {dst}", flush=True)
        else:
            import cpa_export

            r = cpa_export.export_cpa_xai_for_account(
                acc.email,
                acc.password,
                page=None,
                sso=acc.sso or None,
                config=run_cfg,
                log_callback=log,
            )

        results.append(r)
        if r.get("ok") and r.get("path"):
            ok_n += 1
            from cpa_xai.accounts import email_match_keys

            have |= email_match_keys(acc.email)
        else:
            fail_n += 1
            if args.fail_log:
                Path(args.fail_log).parent.mkdir(parents=True, exist_ok=True)
                with open(args.fail_log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        if args.sleep and i < len(todo):
            time.sleep(args.sleep)

    print(f"\n=== done ok={ok_n} fail={fail_n} ===", flush=True)
    summary = out_path / f"backfill_summary_{int(time.time())}.json"
    summary.write_text(
        json.dumps(results, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"summary {summary}")
    return 0 if ok_n > 0 or not todo else 1


if __name__ == "__main__":
    raise SystemExit(main())
