#!/usr/bin/env bash
# Start (or restart) project mihomo for Grok register mixed-port egress.
# Default layout (pxed): CLASH_DIR=/personal/clash, mixed-port 7897, API 9090.
#
# Env:
#   CLASH_DIR     default /personal/clash
#   GROK_CONFIG   preferred config path (mac-merged → mac-sync → grok-register)
#   GROK_NODE     selector pin after start (default GVPS-AnyTLS-googlevps)
#   CLASH_API     default http://127.0.0.1:9090
set -u
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy

DIR="${CLASH_DIR:-/personal/clash}"
BIN="${CLASH_BIN:-$DIR/mihomo}"
CFG="${GROK_CONFIG:-}"
if [[ -z "$CFG" ]]; then
  if [[ -f "$DIR/config.mac-merged.yaml" ]]; then
    CFG="$DIR/config.mac-merged.yaml"
  elif [[ -f "$DIR/config.mac-sync.yaml" ]]; then
    CFG="$DIR/config.mac-sync.yaml"
  elif [[ -f "$DIR/config.grok-register.yaml" ]]; then
    CFG="$DIR/config.grok-register.yaml"
  else
    CFG="$DIR/config.yaml"
  fi
fi
LOG="${CLASH_LOG:-$DIR/mihomo.log}"
API="${CLASH_API:-http://127.0.0.1:9090}"
SECRET_FILE="$DIR/.controller-secret"
if [[ -f "$SECRET_FILE" ]]; then
  SECRET=$(tr -d '\r\n' <"$SECRET_FILE")
elif [[ -f "$CFG" ]] && grep -qE '^secret:' "$CFG"; then
  SECRET=$(sed -nE 's/^secret:[[:space:]]*["'\'']?([^"'\'']+).*/\1/p' "$CFG" | head -1)
else
  echo "ERROR: controller secret missing ($SECRET_FILE or secret: in $CFG)" >&2
  exit 1
fi
NODE="${GROK_NODE:-GVPS-AnyTLS-googlevps}"

if [[ ! -x "$BIN" && ! -f "$BIN" ]]; then
  echo "ERROR: mihomo binary not found: $BIN" >&2
  exit 1
fi
if [[ ! -f "$CFG" ]]; then
  echo "ERROR: clash config not found: $CFG" >&2
  exit 1
fi

pkill -x mihomo 2>/dev/null && sleep 1
cp -f "$CFG" "$DIR/config.yaml"
echo "===== $(date '+%F %T') start-clash cfg=$(basename "$CFG") node=$NODE =====" >>"$LOG"
setsid "$BIN" -d "$DIR" -f "$DIR/config.yaml" >>"$LOG" 2>&1 </dev/null &
for i in $(seq 1 20); do
  sleep 1
  if netstat -tlnp 2>/dev/null | grep -q '127.0.0.1:7897.*mihomo' || ss -tlnp 2>/dev/null | grep -q '127.0.0.1:7897'; then
    for g in GLOBAL PROXY; do
      curl -q -sS -X PUT -H "Authorization: Bearer $SECRET" -H 'Content-Type: application/json' \
        "${API}/proxies/$g" -d "{\"name\":\"$NODE\"}" -o /dev/null || true
    done
    GPATH=$(python3 -c "import urllib.parse; print(urllib.parse.quote('🎯Grok注册'))")
    curl -q -sS -X PUT -H "Authorization: Bearer $SECRET" -H 'Content-Type: application/json' \
      "${API}/proxies/$GPATH" -d "{\"name\":\"$NODE\"}" -o /dev/null || true
    echo "mihomo up mixed-port=7897 cfg=$(basename "$CFG") node=$NODE"
    exit 0
  fi
done
echo "failed to start mihomo on 127.0.0.1:7897" >&2
tail -30 "$LOG" 2>/dev/null || true
exit 1
