#!/usr/bin/env bash
# Start project-owned Web control plane (FastAPI + static UI).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export REGISTER_PROJECT_ROOT="${REGISTER_PROJECT_ROOT:-$ROOT}"
export CONTROL_API_HOST="${CONTROL_API_HOST:-127.0.0.1}"
export CONTROL_API_PORT="${CONTROL_API_PORT:-8787}"
if [[ -z "${CONTROL_API_TOKEN:-}" ]]; then
  echo "[control_api] WARNING: CONTROL_API_TOKEN unset — API routes are open on ${CONTROL_API_HOST}:${CONTROL_API_PORT}" >&2
fi
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -m apps.control_api
fi
exec uv run python -m apps.control_api
