#!/usr/bin/env bash
# Unified multi-provider hub for ai-register-machine.
#
# Usage:
#   ./register.sh grok [count] [threads]
#   ./register.sh mimo [count]
#   ./register.sh chatgpt [count]
#   ./register.sh core list|run ...
#   ./register.sh smoke mimo
#   ./register.sh help
#
# Design:
#   - Layered core: register_core (email / providers / verify / sink / pipeline)
#   - Grok stays Python (register_cli + grok_register_ttk + cpa_xai)
#   - MiMo is providers/mimo (Node/Playwright register-one.js)
#   - ChatGPT is providers/chatgpt (in-process curl_cffi + EmailSource)
#   - Shared ops: project nodes.json egress (no Clash required), Xvfb, fail-fast
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

usage() {
  cat <<'EOF'
register.sh — multi-provider hub (ai-register-machine)

  ./register.sh grok [count] [threads]   Register xAI/Grok (Python production path)
  ./register.sh mimo [count]             Register Xiaomi MiMo API key (Node)
  ./register.sh chatgpt [count]          Register OpenAI platform account (protocol)
  ./register.sh core list                Layered framework: list providers/email
  ./register.sh core run -p mimo -n 1    Layered framework: pipeline run
  ./register.sh core run -p chatgpt -n 1 --email-source tinyhost
  ./register.sh smoke mimo               MiMo tinyhost + xiaomi page smoke
  ./register.sh help

Layers (register_core):
  contracts → email → providers → verify → sink → pipeline → cli
  See register_core/README.md

Env (shared):
  GROK_NODE / GROK_CONFIG   optional clash node/config (grok browser only)
  MIMO_PROXY                optional; MiMo may still default local mixed port
  MIMO_RUNTIME              node_modules home (pxed: /personal/mimo-register)
  REGISTER_NODES_FILE       project nodes catalog (default ./nodes.json)
  CHATGPT_PROXY             optional fixed URL (empty = use nodes/list)
  CHATGPT_PROXY_LIST        explicit self-controlled pool
  CHATGPT_EMAIL_SOURCE      default gmail_imap (via runner)
  HEADLESS / HEADLESS_FLAG  browser mode
  OTP_RETRIES               MiMo temp-mail polls

Nodes (project-owned egress — no external VPN required):
  python -m register_core nodes list|check|add
  cp nodes.example.json nodes.json   # edit real HTTP proxy URLs

Deploy layout example (pxed):
  /personal/grok-register or ai-register-machine   this monorepo
  /personal/mimo-register                       optional Node runtime
  nodes.json under monorepo root (gitignored credentials)

Env:
  GROK_CODE_ROOT   optional override for monorepo root on remote
EOF
}

# Prefer explicit override, then common deploy dirs that actually exist, then $ROOT.
_resolve_code_root() {
  if [[ -n "${GROK_CODE_ROOT:-}" && -d "${GROK_CODE_ROOT}" ]]; then
    printf '%s\n' "${GROK_CODE_ROOT}"
    return 0
  fi
  local cand
  for cand in \
    /personal/ai-register-machine \
    /personal/register-machine \
    /personal/grok-register \
    "$ROOT"
  do
    if [[ -d "$cand" && -f "$cand/register_cli.py" ]]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  printf '%s\n' "$ROOT"
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
    CODE_ROOT="$(_resolve_code_root)"
    if [[ -x "$CODE_ROOT/run-register.sh" && -d "$CODE_ROOT/.venv" ]]; then
      exec bash "$CODE_ROOT/run-register.sh" "$COUNT" "$THREADS"
    fi
    if [[ -x "$ROOT/run-register.sh" && "$ROOT" != "$CODE_ROOT" ]]; then
      exec bash "$ROOT/run-register.sh" "$COUNT" "$THREADS"
    fi
    # local/dev: python CLI from monorepo root that has register_cli.py
    cd "$CODE_ROOT"
    if [[ -d "$CODE_ROOT/.venv" ]]; then
      # shellcheck disable=SC1091
      source "$CODE_ROOT/.venv/bin/activate"
    elif [[ -d "$ROOT/.venv" ]]; then
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
  chatgpt|openai|openai-platform)
    COUNT="${1:-1}"
    export COUNT
    exec bash "$ROOT/providers/chatgpt/run-register.sh" "$COUNT"
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
