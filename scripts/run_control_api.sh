#!/usr/bin/env bash
# Start project-owned Web control plane (FastAPI + static UI).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export REGISTER_PROJECT_ROOT="${REGISTER_PROJECT_ROOT:-$ROOT}"
export CONTROL_API_HOST="${CONTROL_API_HOST:-127.0.0.1}"
export CONTROL_API_PORT="${CONTROL_API_PORT:-8787}"
# Session secret for password login cookies (falls back to CONTROL_API_TOKEN).
if [[ -z "${CONTROL_API_SESSION_SECRET:-}" && -n "${CONTROL_API_TOKEN:-}" ]]; then
  export CONTROL_API_SESSION_SECRET="$CONTROL_API_TOKEN"
fi
if [[ -z "${CONTROL_API_SESSION_SECRET:-}" ]]; then
  # Ephemeral secret so password login works after first user is created.
  export CONTROL_API_ALLOW_EPHEMERAL_SESSION="${CONTROL_API_ALLOW_EPHEMERAL_SESSION:-1}"
fi
if [[ ! -f "$ROOT/.control_api_users.json" && -z "${CONTROL_API_BOOTSTRAP_USER:-}" && -z "${CONTROL_API_TOKEN:-}" ]]; then
  echo "[control_api] No operators yet. Create one:" >&2
  echo "  uv run python scripts/control_api_user.py set admin" >&2
  echo "  # or once: CONTROL_API_BOOTSTRAP_USER=admin CONTROL_API_BOOTSTRAP_PASSWORD='…'" >&2
fi
if [[ -z "${CONTROL_API_TOKEN:-}" ]]; then
  echo "[control_api] NOTE: CONTROL_API_TOKEN unset — scripts without cookie need password login or open mode (no users)." >&2
fi
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -m apps.control_api
fi
exec uv run python -m apps.control_api
