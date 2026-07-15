#!/usr/bin/env bash
# Template product runner — replace with real signup.
# Contract for register_core black-box adapters:
#   - Prefer printing one line: RESULT_JSON:{"ok":true,"email":"...","secret":"..."}
#   - Or append-only files with caller-owned offsets
#   - Exit non-zero on failure; do not exit 0 with empty identity
set -euo pipefail

COUNT="${COUNT:-${1:-1}}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_DIR="$(cd "$(dirname "$0")" && pwd)/output"
mkdir -p "$OUT_DIR"

echo "[template] COUNT=$COUNT ROOT=$ROOT" >&2
echo "[template] Replace this runner. Failing closed." >&2
exit 2
