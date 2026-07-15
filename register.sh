#!/usr/bin/env bash
# Unified multi-provider hub for register-machine.
#
# Usage:
#   ./register.sh grok [count] [threads]
#   ./register.sh mimo [count]
#   ./register.sh core list|run ...
#   ./register.sh smoke mimo
#   ./register.sh help
#
# Design:
#   - Layered core: register_core (email / providers / verify / sink / pipeline)
#   - Grok stays Python (register_cli + grok_register_ttk + cpa_xai)
#   - MiMo is providers/mimo (Node/Playwright register-one.js)
#   - Shared ops: clash/mihomo 7897, Xvfb, fail-fast, no alias-email farm
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

usage() {
  cat <<'EOF'
register.sh — multi-provider hub (register-machine)

  ./register.sh grok [count] [threads]   Register xAI/Grok (Python production path)
  ./register.sh mimo [count]             Register Xiaomi MiMo API key (Node)
  ./register.sh core list                Layered framework: list providers/email
  ./register.sh core run -p mimo -n 1    Layered framework: pipeline run
  ./register.sh smoke mimo               MiMo tinyhost + xiaomi page smoke
  ./register.sh help

Layers (register_core):
  contracts → email → providers → verify → sink → pipeline → cli
  See register_core/README.md

Env (shared):
  GROK_NODE / GROK_CONFIG   clash node/config (grok path)
  MIMO_PROXY                default http://127.0.0.1:7897
  MIMO_RUNTIME              node_modules home (pxed: /personal/mimo-register)
  HEADLESS / HEADLESS_FLAG  browser mode
  OTP_RETRIES               MiMo temp-mail polls

Deploy layout example (pxed):
  /personal/grok-register or register-machine   this monorepo
  /personal/mimo-register                       optional Node runtime
  /personal/clash                               mihomo
EOF
}

cmd="${1:-help}"
shift || true

case "$cmd" in
  help|-h|--help)
    usage
    ;;
  grok|xai)
    COUNT="${1:-1}"
    THREADS="${2:-1}"
    if [[ -x /personal/grok-register/run-register.sh && -d /personal/grok-register/.venv ]]; then
      exec bash /personal/grok-register/run-register.sh "$COUNT" "$THREADS"
    fi
    if [[ -x "$ROOT/run-register.sh" ]]; then
      exec bash "$ROOT/run-register.sh" "$COUNT" "$THREADS"
    fi
    # local/dev: python CLI
    if [[ -d "$ROOT/.venv" ]]; then
      # shellcheck disable=SC1091
      source "$ROOT/.venv/bin/activate"
    fi
    HEADLESS_FLAG="${HEADLESS_FLAG:---no-headless}"
    if [[ "$HEADLESS_FLAG" == "--headless" ]]; then
      exec python -u register_cli.py --extra "$COUNT" --threads "$THREADS" --headless --fast --account-slot-retry 0
    fi
    if command -v xvfb-run >/dev/null 2>&1; then
      exec xvfb-run -a -s "-screen 0 1280x900x24 -ac +extension GLX +render -noreset" \
        python -u register_cli.py --extra "$COUNT" --threads "$THREADS" --no-headless --fast --account-slot-retry 0
    fi
    exec python -u register_cli.py --extra "$COUNT" --threads "$THREADS" --no-headless --fast --account-slot-retry 0
    ;;
  mimo|xiaomi|mimo-tts)
    COUNT="${1:-1}"
    export COUNT
    exec bash "$ROOT/providers/mimo/run-register.sh" "$COUNT"
    ;;
  core|framework)
    # Layered register_core CLI (email / provider / verify / sink)
    if [[ -d "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]]; then
      PY="$ROOT/.venv/bin/python"
    else
      PY="${PYTHON:-python3}"
    fi
    exec "$PY" -m register_core "$@"
    ;;
  smoke)
    target="${1:-mimo}"
    case "$target" in
      mimo|xiaomi)
        exec bash "$ROOT/providers/mimo/smoke.sh"
        ;;
      *)
        echo "unknown smoke target: $target" >&2
        exit 2
        ;;
    esac
    ;;
  *)
    echo "unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
