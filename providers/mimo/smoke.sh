#!/usr/bin/env bash
# Smoke: tinyhost API + Playwright open account.xiaomi.com via clash.
set -euo pipefail

PROVIDER_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -d /personal/mimo-register/node_modules ]]; then
  RUNTIME=/personal/mimo-register
elif [[ -d "$PROVIDER_DIR/node_modules" ]]; then
  RUNTIME="$PROVIDER_DIR"
else
  RUNTIME="$PROVIDER_DIR"
fi

SMOKE_JS="$PROVIDER_DIR/scripts/smoke-browser.js"
if [[ ! -f "$SMOKE_JS" ]]; then
  SMOKE_JS="$RUNTIME/scripts/smoke-browser.js"
fi

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy no_proxy NO_PROXY || true
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"
export http_proxy="${MIMO_PROXY:-http://127.0.0.1:7897}"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"
export NO_PROXY=127.0.0.1,localhost
export no_proxy=$NO_PROXY

if [[ -x /personal/grok-register/start-clash-for-grok.sh ]]; then
  if ! (ss -lntp 2>/dev/null | grep -q ':7897' || netstat -tlnp 2>/dev/null | grep -q '7897'); then
    bash /personal/grok-register/start-clash-for-grok.sh
  fi
fi

echo "=== tempmail tinyhost ==="
curl -sS --max-time 20 'https://tinyhost.shop/api/random-domains/?limit=3' | head -c 400
echo
echo "=== playwright smoke xiaomi ==="
cd "$RUNTIME"
if [[ "$SMOKE_JS" != "$RUNTIME/scripts/smoke-browser.js" && -f "$SMOKE_JS" ]]; then
  mkdir -p "$RUNTIME/scripts"
  cp -f "$SMOKE_JS" "$RUNTIME/scripts/smoke-browser.js"
  node scripts/smoke-browser.js
else
  node "$SMOKE_JS"
fi
echo SMOKE_OK
