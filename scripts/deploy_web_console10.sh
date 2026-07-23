#!/usr/bin/env bash
# scripts/deploy_web_console10.sh — MANUAL only; does not stop batch/coinbot
# Build locally, scp dist to pxed. Adjust PXED_* before first run.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${PXED_HOST:-pxed}"
REMOTE="${PXED_WEB:-/data/grok-register/apps/web}"
"$ROOT/scripts/build_web_console.sh"
tar -C "$ROOT/apps/web" -czf /tmp/console10-dist.tgz dist
scp /tmp/console10-dist.tgz "$HOST:/tmp/"
ssh "$HOST" "mkdir -p '$REMOTE' && tar -C '$REMOTE' -xzf /tmp/console10-dist.tgz && ls -la '$REMOTE/dist' | head"
echo "[deploy] dist on $HOST:$REMOTE/dist — restart control_api if needed (static only usually hot)"
