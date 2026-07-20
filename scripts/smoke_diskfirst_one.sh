#!/usr/bin/env bash
# Official single-shot disk-first Grok reg+mint smoke (pxed / local).
#
# Success criterion: +1 complete cpa_auths/xai-*.json with access+refresh
#   (mint_token_ok / product_batch_success when CPA_PROBE_CHAT=false).
# Does NOT require chat_ok or remote inject.
#
# Single-instance:
#   - flock /tmp/grok_smoke_diskfirst.lock (this wrapper)
#   - register_cli also flocks /tmp/grok_register_cli.lock (process-level)
#
# Usage:
#   bash scripts/smoke_diskfirst_one.sh
#   SKIP_CLASH_PREFLIGHT=1 bash scripts/smoke_diskfirst_one.sh   # no Clash rewrite
#   SMOKE_TIMEOUT=900 bash scripts/smoke_diskfirst_one.sh
#
# Env freeze (always):
#   CPA_EXPORT_ENABLED=true CPA_PROBE_CHAT=false CPA_REMOTE_INJECT=false
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

# Prefer /data symlink on pxed; fall back to checkout.
if [[ -d /data/grok-register && -f /data/grok-register/register_cli.py ]]; then
  # When invoked from monorepo checkout that is not the runtime, prefer runtime.
  if [[ "$ROOT" != /data/grok-register && "$ROOT" != /personal/grok-register ]]; then
    if [[ -d /data/grok-register ]]; then
      :
    fi
  fi
fi

LOCK=/tmp/grok_smoke_diskfirst.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another smoke_diskfirst holds $LOCK; exit 1"
  exit 1
fi
echo $$ > "${LOCK}.pid"

TS=$(date +%Y%m%d_%H%M%S)
mkdir -p logs
LOG="logs/smoke_diskfirst_${TS}.log"
export SMOKE_LOG_PATH="$LOG"

# Line-safe .env load (never source whole file — may contain bare exports / comments).
if [[ -f .env ]]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ""|\#*) continue ;; esac
    key=${line%%=*}
    val=${line#*=}
    # Only export known-safe keys; never clobber disk-first freeze below.
    case "$key" in
      EMAIL_PROVIDER|EMAIL_PROVIDERS|EMAIL_PROVIDER_STRATEGY|MAIL_TIMEOUT|PLAYWRIGHT_BROWSERS_PATH|PROXY|CPA_PROXY|GROK_NODE|DISPLAY)
        export "$key=$val"
        ;;
    esac
  done < .env
fi

if [[ -f .venv/bin/activate ]]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

# Smoke always pins Cloudflare Worker temp-mail (verified product path).
# Override only with SMOKE_EMAIL_PROVIDER=... for deliberate channel experiments.
# Multi-select EMAIL_PROVIDERS is cleared so duckmail/gmail RR cannot hijack.
export EMAIL_PROVIDER=${SMOKE_EMAIL_PROVIDER:-cloudflare}
unset EMAIL_PROVIDERS || true
export MAIL_TIMEOUT=${MAIL_TIMEOUT:-20}
export CPA_EXPORT_ENABLED=true
export CPA_PROBE_CHAT=false
export CPA_REMOTE_INJECT=false
export CPA_PREFER_PROTOCOL=${CPA_PREFER_PROTOCOL:-false}
export PROXY=${PROXY:-http://127.0.0.1:7897}
export CPA_PROXY=${CPA_PROXY:-$PROXY}
export PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH:-/personal/browsers/ms-playwright}
export SKIP_CLASH_PREFLIGHT=${SKIP_CLASH_PREFLIGHT:-0}
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy || true
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost

SMOKE_TIMEOUT=${SMOKE_TIMEOUT:-900}
PY=${ROOT}/.venv/bin/python
if [[ ! -x "$PY" ]]; then
  PY=python3
fi
EXIT_FILE=/tmp/grok_smoke_diskfirst_exit.txt
rm -f "$EXIT_FILE"

baseline_complete() {
  "$PY" - <<'PY'
import json
from pathlib import Path
need = ["access_token", "refresh_token", "email", "base_url", "token_endpoint", "headers"]
xs = list(Path("cpa_auths").glob("xai-*.json")) if Path("cpa_auths").exists() else []
complete = 0
for f in xs:
    try:
        j = json.loads(f.read_text())
    except Exception:
        continue
    if all((j.get(k) or "") for k in need):
        complete += 1
print(complete)
PY
}

{
  echo "=== smoke_diskfirst_one start $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  echo "root=$ROOT"
  echo "EMAIL_PROVIDER=$EMAIL_PROVIDER EMAIL_PROVIDERS=${EMAIL_PROVIDERS:-} MAIL_TIMEOUT=${MAIL_TIMEOUT:-} CPA_EXPORT=$CPA_EXPORT_ENABLED PROBE=$CPA_PROBE_CHAT INJECT=$CPA_REMOTE_INJECT PROXY=$PROXY"
  echo "SKIP_CLASH_PREFLIGHT=$SKIP_CLASH_PREFLIGHT CPA_PREFER_PROTOCOL=$CPA_PREFER_PROTOCOL timeout=$SMOKE_TIMEOUT"
  echo "success criterion: +1 complete xai-*.json with access+refresh (not chat_ok)"

  BASE=$(baseline_complete)
  echo "baseline_complete=$BASE"
  echo "$BASE" > /tmp/grok_smoke_baseline_complete.txt

  if [[ "${SKIP_CLASH_PREFLIGHT}" != "1" && -f preflight-clash-nodes.sh ]]; then
    echo "[smoke] preflight-clash-nodes ..."
    set +e
    bash preflight-clash-nodes.sh 2>&1 | tail -40
    pc=$?
    set -e
    echo "preflight_exit=$pc"
  else
    echo "[smoke] preflight skipped SKIP_CLASH_PREFLIGHT=$SKIP_CLASH_PREFLIGHT"
  fi

  set +e
  if command -v xvfb-run >/dev/null 2>&1 && [[ -z "${DISPLAY:-}" || "${FORCE_XVFB:-0}" == "1" ]]; then
    timeout "$SMOKE_TIMEOUT" xvfb-run -a -s "-screen 0 1280x900x24 -ac +extension GLX +render -noreset -nolisten tcp" \
      "$PY" -u register_cli.py --extra 1 --threads 1 --no-headless --fast
    code=$?
  else
    timeout "$SMOKE_TIMEOUT" \
      "$PY" -u register_cli.py --extra 1 --threads 1 --no-headless --fast
    code=$?
  fi
  set -e

  echo "register_cli_exit=$code"
  echo "$code" > "$EXIT_FILE"
  date -u +%Y-%m-%dT%H:%M:%SZ

  "$PY" - <<'PY'
import json
from pathlib import Path
need = ["access_token", "refresh_token", "email", "base_url", "token_endpoint", "headers"]
try:
    base = int(Path("/tmp/grok_smoke_baseline_complete.txt").read_text().strip() or "0")
except Exception:
    base = 0
xs = sorted(Path("cpa_auths").glob("xai-*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if Path("cpa_auths").exists() else []
complete = 0
newest = None
for f in xs:
    try:
        j = json.loads(f.read_text())
    except Exception:
        continue
    ok = all((j.get(k) or "") for k in need)
    if ok:
        complete += 1
        if newest is None:
            newest = {
                "file": f.name,
                "email": j.get("email"),
                "has_refresh": bool(j.get("refresh_token")),
                "chat_ok": j.get("chat_ok"),
                "mint_method": j.get("mint_method"),
            }
print(f"complete_now={complete} baseline={base} delta={complete - base}")
print("newest", newest)
if complete > base:
    print("SMOKE_PRODUCT_OK=1")
else:
    print("SMOKE_PRODUCT_OK=0")
PY

  echo "=== smoke_diskfirst_one end exit=$code ==="
} >"$LOG" 2>&1

# Surface path + product line for operators
echo "$LOG"
if grep -q "SMOKE_PRODUCT_OK=1" "$LOG" 2>/dev/null; then
  echo "smoke product ok (complete delta>=1)"
  # Prefer product ok over raw exit when disk-first wrote tokens
  exit 0
fi
# Fall through to register_cli exit (1=not product, 2=fatal). code lives in EXIT_FILE
# because the log block is a redirected group (not a subshell variable scope leak).
code=1
if [[ -f "$EXIT_FILE" ]]; then
  code=$(cat "$EXIT_FILE" 2>/dev/null || echo 1)
fi
tail -n 30 "$LOG" || true
exit "${code:-1}"
