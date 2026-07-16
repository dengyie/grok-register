#!/usr/bin/env bash
# Preflight Clash leaf health before a serious Grok register batch.
# Probes all mihomo leaves via controller delay API, rewrites register groups
# to healthy-only, restarts mihomo, selects best healthy node.
#
# Production authority for Grok on Clash mixed-port (pxed default path).
# Not the monorepo nodes.json list|auto preflight — see ARCHITECTURE.md.
#
# Usage:
#   bash preflight-clash-nodes.sh              # probe + apply + restart
#   bash preflight-clash-nodes.sh --dry-run    # probe only
#   bash preflight-clash-nodes.sh --no-restart # apply config but skip restart
#
# Env:
#   CLASH_DIR, CLASH_API, CLASH_REPORT_DIR, GROK_NODE, SKIP_CLASH_PREFLIGHT
#
# Exit: 0 if healthy>=1, 2 if no healthy nodes, 1 on tool errors.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
CLASH_DIR="${CLASH_DIR:-/personal/clash}"
PROBE="${ROOT}/scripts/probe_clash_nodes.py"
if [[ ! -f "$PROBE" ]]; then
  PROBE="${CLASH_DIR}/probe_clash_nodes.py"
fi
if [[ ! -f "$PROBE" ]]; then
  echo "ERROR: probe_clash_nodes.py not found under $ROOT/scripts or $CLASH_DIR" >&2
  exit 1
fi

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY=python3
fi

DRY=0
RESTART=1
# Collect extra argv for the probe; never expand an empty array under set -u
# with a bare "${EXTRA[@]}" (bash error / empty unrecognized argument).
EXTRA=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1; EXTRA+=(--dry-run) ;;
    --no-restart) RESTART=0 ;;
    *) EXTRA+=("$a") ;;
  esac
done

echo "=== preflight clash nodes $(date '+%F %T') dry=$DRY restart=$RESTART ==="

# Ensure mihomo is up so delay API works
if ! (netstat -tlnp 2>/dev/null | grep -q '127.0.0.1:7897.*mihomo' || ss -tlnp 2>/dev/null | grep -q '127.0.0.1:7897'); then
  echo "mihomo not up — starting via start-clash-for-grok.sh"
  if [[ -x "${ROOT}/start-clash-for-grok.sh" ]]; then
    bash "${ROOT}/start-clash-for-grok.sh" || exit 1
  else
    echo "ERROR: start-clash-for-grok.sh missing and mixed-port not listening" >&2
    exit 1
  fi
  sleep 2
fi

if [[ $DRY -eq 1 ]]; then
  # shellcheck disable=SC2086
  "$PY" "$PROBE" --dry-run --workers 16 ${EXTRA[@]+"${EXTRA[@]}"}
  exit $?
fi

# Probe + rewrite config groups to healthy-only + select best
# shellcheck disable=SC2086
"$PY" "$PROBE" --apply-config --workers 16 ${EXTRA[@]+"${EXTRA[@]}"}
code=$?
if [[ $code -ne 0 ]]; then
  echo "preflight FAILED exit=$code (no healthy nodes?)" >&2
  exit "$code"
fi

# Pick GROK_NODE from preferred healthy list if present
HEALTHY_TXT="${ROOT}/output/clash_nodes_healthy.txt"
LATEST_JSON="${ROOT}/output/clash_node_health_latest.json"
if [[ -n "${CLASH_REPORT_DIR:-}" ]]; then
  HEALTHY_TXT="${CLASH_REPORT_DIR}/clash_nodes_healthy.txt"
  LATEST_JSON="${CLASH_REPORT_DIR}/clash_node_health_latest.json"
fi
if [[ -z "${GROK_NODE:-}" && -f "$LATEST_JSON" ]]; then
  GROK_NODE=$("$PY" -c "
import json
from pathlib import Path
p=Path(r'''${LATEST_JSON}''')
d=json.loads(p.read_text())
pref=d.get('preferred_first') or []
healthy=[x['name'] for x in d.get('healthy') or []]
print((pref[0] if pref else (healthy[0] if healthy else '')).strip())
")
  export GROK_NODE
fi
export GROK_NODE="${GROK_NODE:-GVPS-AnyTLS-googlevps}"

if [[ $RESTART -eq 1 ]]; then
  echo "=== restart mihomo with health-filtered config node=$GROK_NODE ==="
  if [[ -x "${ROOT}/start-clash-for-grok.sh" ]]; then
    bash "${ROOT}/start-clash-for-grok.sh" || exit 1
  else
    echo "WARN: start-clash-for-grok.sh missing; config rewritten but mihomo not restarted" >&2
  fi
fi

echo "=== preflight OK healthy_list=$HEALTHY_TXT node=$GROK_NODE ==="
exit 0
