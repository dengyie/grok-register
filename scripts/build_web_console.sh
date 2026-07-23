#!/usr/bin/env bash
# scripts/build_web_console.sh — build console10 (Vite+Preact) into apps/web/dist
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/apps/web"
if [[ ! -d node_modules ]]; then
  npm ci || npm install
fi
npm run build
echo "[web] built → $ROOT/apps/web/dist"
