#!/usr/bin/env bash
# One-shot bootstrap for outsiders (Aaron-style: clone → config → doctor → run).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

WARN=0
warn() { echo "[warn] $*" >&2; WARN=1; }
ok() { echo "[ok] $*"; }
skip() { echo "[skip] $*"; }

# --- write templates (idempotent) ---
if [[ ! -f config.json ]]; then
  if [[ -f config.simple.example.json ]]; then
    cp config.simple.example.json config.json
    ok "wrote config.json from config.simple.example.json"
  elif [[ -f config.example.json ]]; then
    cp config.example.json config.json
    ok "wrote config.json from config.example.json"
  else
    echo "[error] no config template found" >&2
    exit 1
  fi
else
  skip "config.json already exists"
fi

if [[ ! -f mail_credentials.txt ]]; then
  if [[ -f mail_credentials.example.txt ]]; then
    cp mail_credentials.example.txt mail_credentials.txt
    ok "wrote mail_credentials.txt (only needed for hotmail/outlookmail)"
  else
    warn "mail_credentials.example.txt missing"
  fi
else
  skip "mail_credentials.txt already exists"
fi

# --- deps ---
if command -v uv >/dev/null 2>&1; then
  if uv sync; then
    ok "uv sync done"
  else
    warn "uv sync failed — fix Python 3.13 / network, then: uv sync"
  fi
else
  warn "uv not found — install: https://docs.astral.sh/uv/ then re-run: uv sync"
fi

# --- doctor: environment ---
echo
echo "=== doctor ==="

if command -v uv >/dev/null 2>&1; then
  PY_VER="$(uv run python -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
  if [[ "$PY_VER" == "3.13" ]]; then
    ok "Python $PY_VER (required)"
  elif [[ -n "$PY_VER" ]]; then
    warn "Python $PY_VER via uv — project requires 3.13 (see pyproject.toml)"
  else
    warn "could not resolve Python via uv run"
  fi
else
  if command -v python3 >/dev/null 2>&1; then
    warn "system $(python3 -V 2>&1); install uv + Python 3.13 for this project"
  fi
fi

# Chrome / Chromium presence (best-effort)
if command -v google-chrome >/dev/null 2>&1 \
  || command -v google-chrome-stable >/dev/null 2>&1 \
  || command -v chromium >/dev/null 2>&1 \
  || command -v chromium-browser >/dev/null 2>&1 \
  || [[ -d "/Applications/Google Chrome.app" ]] \
  || [[ -d "/Applications/Chromium.app" ]]; then
  ok "Chrome/Chromium detected"
else
  warn "Chrome/Chromium not found — registration needs a real browser"
fi

# proxy port from config (default 7890)
PROXY_URL="$(
  if command -v uv >/dev/null 2>&1; then
    uv run python - <<'PY' 2>/dev/null || true
import json
from pathlib import Path
p = Path("config.json")
if not p.is_file():
    raise SystemExit
raw = json.loads(p.read_text(encoding="utf-8"))
cfg = {k: v for k, v in raw.items() if not str(k).startswith(("//", "#"))}
print((cfg.get("proxy") or cfg.get("cpa_proxy") or "").strip())
PY
  fi
)"
if [[ -n "${PROXY_URL}" ]]; then
  # extract host:port for tcp check (http://host:port or http://user:pass@host:port)
  HOSTPORT="$(
    PROXY_URL="$PROXY_URL" uv run python - <<'PY' 2>/dev/null || true
import os, re
u = os.environ.get("PROXY_URL", "")
m = re.search(r"@([^/]+)$", u) or re.search(r"://([^/]+)", u)
print(m.group(1) if m else "")
PY
  )"
  if [[ -z "$HOSTPORT" ]]; then
    HOSTPORT="127.0.0.1:7890"
  fi
  # strip credentials if still present
  HOSTPORT="${HOSTPORT##*@}"
  H="${HOSTPORT%%:*}"
  P="${HOSTPORT##*:}"
  if [[ -n "$H" && -n "$P" && "$P" =~ ^[0-9]+$ ]]; then
    if (echo >/dev/tcp/"$H"/"$P") >/dev/null 2>&1; then
      ok "proxy port open $H:$P ($PROXY_URL)"
    else
      warn "proxy port closed $H:$P — start your proxy or edit config.json proxy"
    fi
  else
    ok "proxy configured: $PROXY_URL (could not parse host:port for probe)"
  fi
else
  warn "proxy empty in config.json — xAI access usually needs a local proxy"
fi

# --- doctor: mail credentials / keys ---
MAIL_STATUS="$(
  if command -v uv >/dev/null 2>&1; then
    uv run python - <<'PY' 2>/dev/null || true
import json, re
from pathlib import Path

def load_cfg():
    raw = json.loads(Path("config.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not str(k).startswith(("//", "#"))}

cfg = load_cfg() if Path("config.json").is_file() else {}
provider = str(cfg.get("email_provider") or "duckmail").strip().lower()
print(f"provider={provider}")

if provider in ("hotmail", "outlook", "outlookmail", "microsoft"):
    p = Path(str(cfg.get("hotmail_accounts_file") or "mail_credentials.txt"))
    if not p.is_file():
        print("mail=missing_file")
        raise SystemExit
    lines = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    if not lines:
        print("mail=empty")
        raise SystemExit
    placeholders = ("your@hotmail.com", "mailPassword", "client-id", "refresh-token", "xxxx")
    bad = 0
    good = 0
    for s in lines:
        parts = s.split("----")
        if len(parts) < 4:
            bad += 1
            continue
        blob = s.lower()
        if any(ph.lower() in blob for ph in placeholders):
            bad += 1
        elif parts[3].strip() in ("", "token", "refresh_token"):
            bad += 1
        else:
            good += 1
    if good == 0:
        print("mail=placeholder_or_invalid")
    else:
        print(f"mail=ok good={good} bad={bad}")
elif provider == "duckmail":
    key = str(cfg.get("duckmail_api_key") or "").strip()
    if not key:
        print("mail=duckmail_key_missing")
    else:
        print("mail=duckmail_key_set")
elif provider == "cloudmail":
    if not str(cfg.get("cloudmail_url") or "").strip():
        print("mail=cloudmail_url_missing")
    else:
        print("mail=cloudmail_configured")
elif provider == "cloudflare":
    if not str(cfg.get("cloudflare_api_base") or "").strip():
        print("mail=cloudflare_base_missing")
    else:
        print("mail=cloudflare_configured")
else:
    print(f"mail=provider_{provider}_check_manual")
PY
  fi
)"

if [[ -n "$MAIL_STATUS" ]]; then
  while IFS= read -r line; do
    case "$line" in
      provider=*)
        ok "email_provider=${line#provider=}"
        ;;
      mail=ok*)
        ok "mail credentials look filled ($line)"
        ;;
      mail=duckmail_key_set)
        ok "duckmail_api_key is set"
        ;;
      mail=duckmail_key_missing)
        warn "duckmail_api_key empty — set it in config.json (email_provider=duckmail)"
        ;;
      mail=placeholder_or_invalid)
        warn "mail_credentials.txt still placeholder/invalid — replace before hotmail register"
        ;;
      mail=missing_file|mail=empty)
        warn "mail_credentials.txt missing/empty (required for hotmail)"
        ;;
      mail=cloudmail_url_missing)
        warn "cloudmail_url empty in config.json"
        ;;
      mail=cloudflare_base_missing)
        warn "cloudflare_api_base empty in config.json"
        ;;
      mail=*)
        ok "$line"
        ;;
    esac
  done <<< "$MAIL_STATUS"
else
  warn "could not run mail doctor (need uv + config.json)"
fi

echo "=== end doctor ==="
echo

if [[ "$WARN" -ne 0 ]]; then
  echo "[note] setup finished with warnings — fix them before register_cli"
else
  ok "setup looks ready"
fi

cat <<'EOF'

Next (after fixing any [warn] above):
  1) config.json
       - proxy → your local proxy
       - duckmail: set duckmail_api_key
       - hotmail: email_provider=hotmail + real mail_credentials.txt lines
  2) Register one account (headed browser recommended):

     uv run python -u register_cli.py --extra 1 --threads 1 --no-headless --fast

  3) Re-run doctor only:

     bash scripts/setup_simple.sh

  4) Outputs:

     ls accounts_cli.txt cpa_auths/
     # Product success = chat probe ok (not models-only)
     # entitlement_denied → no free Build chat; do not remint

Typical blockers: proxy down · Turnstile · duckmail key · hotmail placeholder · chat 403 (no entitlement)

Docs: README.md  |  Full config: config.example.json  |  Production: cpa_remote_inject
EOF
