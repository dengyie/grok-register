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
  CHATGPT_EMAIL_SOURCE      default cloudflare Worker (via runner)
  HEADLESS / HEADLESS_FLAG  browser mode
  OTP_RETRIES               MiMo temp-mail polls

Egress (primary list|core|direct; advanced auto|clash):
  python -m register_core nodes egress set list   # or core|direct
  REGISTER_EGRESS=core ./register.sh chatgpt 1
  python -m register_core nodes list|check|core start|add|clear

Node import (light convert; not on register hot path):
  python -m register_core nodes validate profile.yaml
  python -m register_core nodes import profile.yaml          # merge by URL
  python -m register_core nodes import profile.yaml --replace
  python -m register_core nodes import links.txt --format uri_list

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
    # migrate milestone A: production entry → register_core Pipeline (grok_adapter
    # shell-out, serial). Prefers run-register-core.sh when present (Clash preflight
    # + .env + xvfb外壳 preserved, only the inner register_cli call swapped).
    # GROK_LEGACY=1 forces the legacy run-register.sh path (rollback / batch并发).
    if [[ "${GROK_LEGACY:-0}" != "1" ]]; then
      for _core in "$CODE_ROOT/run-register-core.sh" "$ROOT/run-register-core.sh"; do
        if [[ -x "$_core" ]]; then
          exec bash "$_core" "$COUNT" "$THREADS"
        fi
      done
    fi
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
    # migrate milestone A: production entry → register_core Pipeline (mimo_adapter
    # shell-out to providers/mimo Node runner, which self-handles clash/xvfb/dp).
    # MIMO_LEGACY=1 rolls back to the env-only Node runner loop (no Pipeline shell).
    if [[ "${MIMO_LEGACY:-0}" != "1" ]]; then
      if [[ -d "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]]; then
        _PY="$ROOT/.venv/bin/python"
      else
        _PY="${PYTHON:-python3}"
      fi
      exec "$_PY" -m register_core run \
        --profile "$ROOT/profiles/mimo-tinyhost.example.yaml" \
        -n "$COUNT"
    fi
    exec bash "$ROOT/providers/mimo/run-register.sh" "$COUNT"
    ;;
  chatgpt|openai|openai-platform)
    COUNT="${1:-1}"
    export COUNT
    # migrate milestone A: production entry → register_core profile path.
    # Profile is selected by CHATGPT_EMAIL_SOURCE to preserve the legacy operator
    # knob (legacy runner defaulted to cloudflare). Provider options mailbox.type
    # is the source of truth; selecting the matching profile keeps env override alive:
    #   CHATGPT_EMAIL_SOURCE=cloudflare (default) → chatgpt-cf.example.yaml
    #   CHATGPT_EMAIL_SOURCE=tinyhost              → chatgpt-tinyhost.example.yaml
    #   CHATGPT_EMAIL_SOURCE=gmail_imap            → chatgpt-gmail.example.yaml
    # Other CHATGPT_* env overrides are forwarded as register_core CLI flags so the
    # profile defaults do not silently shadow them (timeout 900, proxy rotation, sink).
    # CHATGPT_LEGACY=1 rolls back to providers/chatgpt/run-register.sh (env-driven).
    if [[ "${CHATGPT_LEGACY:-0}" != "1" ]]; then
      if [[ -d "$ROOT/.venv" && -x "$ROOT/.venv/bin/python" ]]; then
        _PY="$ROOT/.venv/bin/python"
      else
        _PY="${PYTHON:-python3}"
      fi
      # Resolve mailbox profile from CHATGPT_EMAIL_SOURCE (legacy default = cloudflare).
      _ES="${CHATGPT_EMAIL_SOURCE:-cloudflare}"
      case "$_ES" in
        tinyhost)        _CHAT_PROFILE="$ROOT/profiles/chatgpt-tinyhost.example.yaml" ;;
        gmail_imap|gmail) _CHAT_PROFILE="$ROOT/profiles/chatgpt-gmail.example.yaml" ;;
        cloudflare|cf|auto|"") _CHAT_PROFILE="$ROOT/profiles/chatgpt-cf.example.yaml" ;;
        *)
          echo "register.sh: unsupported CHATGPT_EMAIL_SOURCE=$_ES (use cloudflare|tinyhost|gmail_imap)" >&2
          exit 2
          ;;
      esac
      _ARGS=(
        -m register_core run
        --profile "$_CHAT_PROFILE"
        -n "$COUNT"
        --timeout "${CHATGPT_TIMEOUT:-900}"
      )
      # --sink only when operator pinning it; otherwise let the profile sink.path win.
      [[ -n "${CHATGPT_SINK:-}" ]] && _ARGS+=(--sink "$CHATGPT_SINK")
      [[ -n "${REGISTER_EGRESS:-}" ]] && _ARGS+=(--egress "$REGISTER_EGRESS")
      [[ -n "${CHATGPT_PROXY:-}" ]] && _ARGS+=(--proxy "$CHATGPT_PROXY")
      [[ -n "${CHATGPT_PROXY_LIST:-}" ]] && _ARGS+=(--proxy-list "$CHATGPT_PROXY_LIST")
      [[ -n "${CHATGPT_PROXY_ROTATE_MODE:-}" ]] && _ARGS+=(--proxy-rotate "$CHATGPT_PROXY_ROTATE_MODE")
      if [[ -n "${CHATGPT_PROXY_ROTATE_EVERY:-}" ]]; then
        _ARGS+=(--proxy-rotate-every "$CHATGPT_PROXY_ROTATE_EVERY")
      fi
      # CHATGPT_EMAIL_DOMAIN override: profile mailbox.domain wins at the loader
      # (extra["email_domain"] set from profile), so to honor an operator pin we set
      # the env the composite/tinyhost reads. Only override when explicitly set.
      [[ -n "${CHATGPT_EMAIL_DOMAIN:-}" ]] && export CHATGPT_EMAIL_DOMAIN
      exec "$_PY" "${_ARGS[@]}"
    fi
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
