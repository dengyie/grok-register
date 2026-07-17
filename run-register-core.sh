#!/usr/bin/env bash
# Usage: bash run-register-core.sh [count] [threads]
# register_core Pipeline-backed Grok production entry (migrate milestone A).
#
# Keeps the legacy run-register.sh outer shell intact (Clash preflight +
# line-safe .env load + env assembly + xvfb) and swaps only the tail from
# `register_cli.py --extra N --threads T` (legacy: 1 process, internal thread
# pool, N accounts) to `python -m register_core run --profile
# profiles/grok-tinyhost.example.yaml -n N --threads T`. register_core Pipeline
# serially shells out N grok_adapter attempts (each `register_cli --extra 1`);
# throughput drops, but Pipeline owns attribution / feedback / node-preflight /
# proxy rotation / verifier / sink.
#
# Env: GROK_NODE, GROK_CONFIG, HEADLESS_FLAG=--headless|--no-headless
#      SKIP_CLASH_PREFLIGHT=1  skip leaf health probe (debug only)
# Exit code: mapped back to legacy contract for ops/cron callers:
#   0 = product-ok (register_core ok≥1); 1 = not product-usable (ok<1); 2 = fatal.
# Do not mask with `| tee` alone — use PIPESTATUS[0] for the inner side.
#
# Grok production egress (pxed): Clash mixed-port :7897 after preflight-clash-nodes.sh.
# Authoritative public IP: scripts/check_clash_egress.py (not bare curl -x on Bohrium/pxed).
# Monorepo nodes.json list|auto is a separate backend — see ARCHITECTURE.md.
# Rollback: edit register.sh grok|xai branch back to legacy run-register.sh.
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost

# Preflight: probe all Clash leaves, strip dead from register groups, restart mihomo.
# Skip with SKIP_CLASH_PREFLIGHT=1 (e.g. dry local debug / non-Clash host).
export GROK_NODE="${GROK_NODE:-GVPS-AnyTLS-googlevps}"
if [[ "${SKIP_CLASH_PREFLIGHT:-0}" != "1" && -x "$ROOT/preflight-clash-nodes.sh" ]]; then
  bash "$ROOT/preflight-clash-nodes.sh" || exit 1
elif [[ -x "$ROOT/start-clash-for-grok.sh" ]]; then
  bash "$ROOT/start-clash-for-grok.sh" || exit 1
fi

# load .env line-safe when present (never `source` whole file — secrets/export side effects)
if [[ -f "$ROOT/.env" ]]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    key=${line%%=*}
    val=${line#*=}
    export "$key=$val"
  done < "$ROOT/.env"
fi

# register_core inherits these via parent env; grok_adapter force-sets PROXY/CPA_PROXY
# from inject_attempt_proxy (attempt proxy wins over ambient).
export PROXY="${PROXY:-http://127.0.0.1:7897}"
export CPA_PROXY="${CPA_PROXY:-http://127.0.0.1:7897}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/personal/browsers/ms-playwright}"
export DISPLAY="${DISPLAY:-}"

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
fi
COUNT=${1:-1}
THREADS=${2:-1}
HEADLESS_FLAG="${HEADLESS_FLAG:---no-headless}"
TS=$(date +%Y%m%d_%H%M%S)
LOG="$ROOT/logs/run-core-${TS}.log"
mkdir -p "$ROOT/logs" "$ROOT/screenshots"

# register_core profile: egress/mailbox/strategy/verify/sink declared in yaml.
# -n COUNT = register_core Pipeline.run(N) attempts (serial shell-out, --extra 1 each).
# --threads / --headless forwarded via apply_cli_overrides → job.extra → grok_adapter.
# --no-fail-fast left OFF to preserve fail_fast (profile strategy.fail_fast=true);
# fatal attempts surface as Python exception → register_core exit ≥1 below.
HEADLESS_ARG=""
if [[ "$HEADLESS_FLAG" == "--headless" ]]; then
  HEADLESS_ARG="--headless 1"
else
  HEADLESS_ARG="--headless 0"
fi

echo "=== register_core grok start count=$COUNT threads=$THREADS node=$GROK_NODE headless_flag=$HEADLESS_FLAG ===" | tee -a "$LOG"

set +e
if command -v xvfb-run >/dev/null 2>&1 && [[ "$HEADLESS_FLAG" != "--headless" ]]; then
  xvfb-run -a -s "-screen 0 1280x900x24 -ac +extension GLX +render -noreset" \
    python -u -m register_core run \
      --profile "$ROOT/profiles/grok-tinyhost.example.yaml" \
      -n "$COUNT" --threads "$THREADS" $HEADLESS_ARG 2>&1 | tee -a "$LOG"
  code=${PIPESTATUS[0]}
else
  python -u -m register_core run \
    --profile "$ROOT/profiles/grok-tinyhost.example.yaml" \
    -n "$COUNT" --threads "$THREADS" $HEADLESS_ARG 2>&1 | tee -a "$LOG"
  code=${PIPESTATUS[0]}
fi
set -e

# Map register_core exit → legacy Grok contract (0=ok / 1=not product-usable / 2=fatal).
# register_core cmd_run: 0 = ok≥1, 1 = ok<1 or profile/job ValueError, Python
# exception (FailFastError etc.) → nonzero exit from ProcessExit/SystemExit.
# Treat any nonzero as not-product-or-fatal; distinguish fatal via stderr log marker.
if [[ "$code" -eq 0 ]]; then
  out=0
  label="0=ok"
else
  if grep -qiE "(FatalError|fail_fast|cairo|fatal_stop|Traceback|register_cli.py missing|fatal:)" "$LOG" 2>/dev/null; then
    out=2
    label="2=fatal"
  else
    out=1
    label="1=not product-usable"
  fi
fi
echo "=== register_core exit=$code → contract $label (product: 0=ok 1=not product-usable 2=fatal) ===" | tee -a "$LOG"
exit "$out"
