#!/usr/bin/env bash
# ChatGPT / OpenAI platform register runner (in-process via register_core).
# Usage:
#   ./providers/chatgpt/run-register.sh [count]
#
# Project-owned egress (preferred — no Clash / no system VPN):
#   1. Put proxies in ./nodes.json (see nodes.example.json)
#   2. Or: CHATGPT_PROXY_LIST='http://u:p@host:port,...'
#   3. Or: CHATGPT_PROXY=http://user:pass@host:port
set -euo pipefail

COUNT="${COUNT:-${1:-1}}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

# No default to Clash 7897 — nodes.json / PROXY_LIST own egress.
export CHATGPT_PROXY="${CHATGPT_PROXY:-${MIMO_PROXY:-}}"
export CHATGPT_EMAIL_SOURCE="${CHATGPT_EMAIL_SOURCE:-gmail_imap}"
export CHATGPT_PROXY_LIST="${CHATGPT_PROXY_LIST:-${PROXY_LIST:-}}"
export CHATGPT_PROXY_ROTATE_MODE="${CHATGPT_PROXY_ROTATE_MODE:-${PROXY_ROTATE_MODE:-}}"
export CHATGPT_PROXY_ROTATE_EVERY="${CHATGPT_PROXY_ROTATE_EVERY:-${PROXY_ROTATE_EVERY:-1}}"
export REGISTER_NODES_FILE="${REGISTER_NODES_FILE:-${NODES_FILE:-$ROOT/nodes.json}}"

SINK="${CHATGPT_SINK:-$ROOT/providers/chatgpt/output/pipeline.jsonl}"
mkdir -p "$(dirname "$SINK")"

echo "[chatgpt] COUNT=$COUNT proxy=${CHATGPT_PROXY:-'(nodes/list)'} proxy_list=${CHATGPT_PROXY_LIST:-'(nodes.json)'} rotate=${CHATGPT_PROXY_ROTATE_MODE:-auto} email_source=$CHATGPT_EMAIL_SOURCE nodes=$REGISTER_NODES_FILE" >&2

ARGS=(
  -m register_core run
  -p chatgpt
  -n "$COUNT"
  --email-source "${CHATGPT_EMAIL_SOURCE}"
  --sink "$SINK"
  --timeout "${CHATGPT_TIMEOUT:-900}"
)
if [[ -n "${CHATGPT_PROXY}" ]]; then
  ARGS+=(--proxy "${CHATGPT_PROXY}")
fi
if [[ -n "${CHATGPT_PROXY_LIST}" ]]; then
  ARGS+=(--proxy-list "${CHATGPT_PROXY_LIST}")
fi
if [[ -n "${CHATGPT_PROXY_ROTATE_MODE}" ]]; then
  ARGS+=(--proxy-rotate "${CHATGPT_PROXY_ROTATE_MODE}")
fi
if [[ -n "${CHATGPT_PROXY_ROTATE_EVERY}" ]]; then
  ARGS+=(--proxy-rotate-every "${CHATGPT_PROXY_ROTATE_EVERY}")
fi

exec "$PY" "${ARGS[@]}"
