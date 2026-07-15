#!/usr/bin/env bash
# MiMo one-shot register (production path: scripts/register-one.js).
# Usage:
#   bash providers/mimo/run-register.sh
#   COUNT=1 bash providers/mimo/run-register.sh
# Env:
#   MIMO_RUNTIME  — dir with node_modules/playwright (default: this provider dir, or /personal/mimo-register)
#   MIMO_PROXY    — default http://127.0.0.1:7897
#   HEADLESS      — true|false (default true)
#   OTP_RETRIES   — tinyhost poll retries (default 35)
#   MIMO_INVITE_CODE — optional
set -euo pipefail

PROVIDER_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -n "${MIMO_RUNTIME:-}" ]]; then
  RUNTIME="$MIMO_RUNTIME"
elif [[ -d /personal/mimo-register/node_modules ]]; then
  RUNTIME=/personal/mimo-register
elif [[ -d "$PROVIDER_DIR/node_modules" ]]; then
  RUNTIME="$PROVIDER_DIR"
else
  RUNTIME="$PROVIDER_DIR"
fi

SCRIPT_SRC="$PROVIDER_DIR/scripts/register-one.js"
if [[ ! -f "$SCRIPT_SRC" ]]; then
  SCRIPT_SRC="$RUNTIME/scripts/register-one.js"
fi
if [[ ! -f "$SCRIPT_SRC" ]]; then
  echo "register-one.js not found under $PROVIDER_DIR or $RUNTIME" >&2
  exit 2
fi

# Sync versioned script into runtime so require('../dist/temp-mail') resolves on pxed
if [[ "$RUNTIME" != "$PROVIDER_DIR" ]]; then
  mkdir -p "$RUNTIME/scripts" "$RUNTIME/output" "$RUNTIME/logs"
  cp -f "$SCRIPT_SRC" "$RUNTIME/scripts/register-one.js"
  RUN_JS="$RUNTIME/scripts/register-one.js"
  cd "$RUNTIME"
else
  mkdir -p "$PROVIDER_DIR/output" "$PROVIDER_DIR/logs"
  RUN_JS="$SCRIPT_SRC"
  cd "$PROVIDER_DIR"
fi

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy no_proxy NO_PROXY || true
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"

export MIMO_PROXY="${MIMO_PROXY:-http://127.0.0.1:7897}"
export http_proxy="$MIMO_PROXY"
export https_proxy="$MIMO_PROXY"
export HTTP_PROXY="$MIMO_PROXY"
export HTTPS_PROXY="$MIMO_PROXY"
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost

export HEADLESS="${HEADLESS:-true}"
export OTP_RETRIES="${OTP_RETRIES:-35}"
export XIAOMI_REGION="${XIAOMI_REGION:-Singapore}"

ensure_clash() {
  if command -v ss >/dev/null 2>&1 && ss -lntp 2>/dev/null | grep -q ':7897'; then
    echo "[mimo] clash already up on 7897"
    return 0
  fi
  if command -v netstat >/dev/null 2>&1 && netstat -tlnp 2>/dev/null | grep -q '7897'; then
    echo "[mimo] clash already up on 7897"
    return 0
  fi
  local starter monorepo
  for monorepo in \
    "${GROK_CODE_ROOT:-}" \
    /personal/ai-register-machine \
    /personal/register-machine \
    /personal/grok-register \
    "$(cd "$PROVIDER_DIR/../.." && pwd)"
  do
    [[ -n "$monorepo" ]] || continue
    starter="$monorepo/start-clash-for-grok.sh"
    if [[ -x "$starter" ]]; then
      echo "[mimo] starting clash via $starter ..."
      bash "$starter"
      return $?
    fi
  done
  echo "[mimo] WARN: no clash starter; ensure $MIMO_PROXY is reachable" >&2
  return 0
}

ensure_xvfb() {
  export DISPLAY="${DISPLAY:-:99}"
  if pgrep -f 'Xvfb :99' >/dev/null 2>&1; then
    return 0
  fi
  if command -v Xvfb >/dev/null 2>&1; then
    Xvfb :99 -screen 0 1280x900x24 -ac -nolisten tcp >/tmp/xvfb-mimo.log 2>&1 &
    sleep 1
  fi
}

COUNT="${COUNT:-1}"
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
  COUNT="$1"
fi

ensure_clash
ensure_xvfb
export DISPLAY="${DISPLAY:-:99}"

TS=$(date +%Y%m%d_%H%M%S)
LOG_DIR="${RUNTIME}/logs"
mkdir -p "$LOG_DIR" "${RUNTIME}/output"
LOG="$LOG_DIR/mimo-register-${TS}.log"

echo "[mimo] runtime=$RUNTIME script=$RUN_JS proxy=$MIMO_PROXY count=$COUNT" | tee -a "$LOG"

ok=0
fail=0
for i in $(seq 1 "$COUNT"); do
  echo "=== mimo register $i/$COUNT ===" | tee -a "$LOG"
  set +e
  node "$RUN_JS" 2>&1 | tee -a "$LOG"
  code=${PIPESTATUS[0]}
  set -e
  if [[ $code -eq 0 ]]; then
    ok=$((ok + 1))
  else
    fail=$((fail + 1))
    echo "[mimo] fail-fast after error code=$code (no empty spin)" | tee -a "$LOG"
    break
  fi
done

echo "[mimo] done ok=$ok fail=$fail keys=${RUNTIME}/output/success_keys.txt" | tee -a "$LOG"
if [[ $ok -lt 1 ]]; then
  exit 1
fi
exit 0
