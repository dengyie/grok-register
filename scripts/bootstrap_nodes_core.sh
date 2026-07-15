#!/usr/bin/env bash
# Bootstrap project-owned mihomo core under .nodes/bin/
# Usage: ./scripts/bootstrap_nodes_core.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN_DIR="$ROOT/.nodes/bin"
mkdir -p "$BIN_DIR" "$ROOT/.nodes/config" "$ROOT/.nodes/runtime" "$ROOT/.nodes/profiles"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
  arm64|aarch64) ARCH=arm64 ;;
  x86_64|amd64) ARCH=amd64 ;;
esac

ASSET="mihomo-${OS}-${ARCH}"
# Prefer go-default build
TAG="${MIHOMO_TAG:-v1.19.28}"
URL="https://github.com/MetaCubeX/mihomo/releases/download/${TAG}/${ASSET}-${TAG}.gz"
# fallback name without tag middle
URL2="https://github.com/MetaCubeX/mihomo/releases/download/${TAG}/${ASSET}-v${TAG#v}.gz"

echo "[bootstrap] fetch $URL"
TMP="$(mktemp)"
if curl -fsSL -L "$URL" -o "$TMP"; then
  :
elif curl -fsSL -L "$URL2" -o "$TMP"; then
  :
else
  # last resort: copy from local Clash Verge install
  VERGE="/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"
  if [[ -x "$VERGE" ]]; then
    echo "[bootstrap] github failed; copying local verge-mihomo"
    cp -f "$VERGE" "$BIN_DIR/mihomo"
    chmod +x "$BIN_DIR/mihomo"
    "$BIN_DIR/mihomo" -v | head -2
    exit 0
  fi
  echo "[bootstrap] failed to download mihomo" >&2
  exit 1
fi
gunzip -c "$TMP" > "$BIN_DIR/mihomo"
rm -f "$TMP"
chmod +x "$BIN_DIR/mihomo"
echo "[bootstrap] installed:"
"$BIN_DIR/mihomo" -v | head -3
