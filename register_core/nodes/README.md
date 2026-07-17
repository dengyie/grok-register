# Project-owned egress nodes

Clean path:

```text
profile (YAML / V2Ray JSON / URI)
        │
        ▼
  nodes import|validate     ← opt-in convert (not on register hot path)
        │
        ├─ HTTP/SOCKS  → nodes.json          → egress=list
        └─ protocol    → .nodes runtime.yaml → egress=core (+ mihomo)
```

Primary backends for operators: **`list` | `core` | `direct`**.  
`auto` / `clash` remain advanced compatibility options.

## 1) Import / validate

```bash
python -m register_core nodes validate profile.yaml
python -m register_core nodes import profile.yaml
python -m register_core nodes import links.txt --format uri_list --dry-run

# merge is default (by URL). Replace catalog entirely:
python -m register_core nodes import profile.yaml --replace

# optional convenience: live-probe catalog right after import (batch still re-probes)
python -m register_core nodes import profile.yaml --check

# empty catalog (does not touch protocol runtime):
python -m register_core nodes clear --yes

# advanced: scan local Clash Verge profiles (opt-in only)
python -m register_core nodes import --from-clash-verge
```

**Import writes schema only by default** — liveness is enforced on every batch register (see preflight below), not at import time.

| Input | Format |
|-------|--------|
| Clash / mihomo YAML (`proxies:`) | `clash_yaml` |
| V2Ray / Xray JSON (`outbounds`) | `v2ray_json` |
| Share URI lines | `uri_list` |

| Proxy type | Artifact | Backend |
|------------|----------|---------|
| http / socks* | `nodes.json` | `list` (no core) |
| vless / ss / vmess / trojan / … | `.nodes/config/runtime.yaml` | `core` |

Schema validation rejects missing type/server/port/uuid/… — it does **not** prove the node is live.  
New dialable rows use `id=imp-*` and tag `imported` (no `from-clash` identity).

Compat scripts: `scripts/import_nodes.py` (canonical); `import_clash_to_nodes.py` is deprecated.

## 2) HTTP/SOCKS catalog

```bash
python -m register_core nodes list          # summary (sample)
python -m register_core nodes list --all
python -m register_core nodes check         # probe all → last_ok / fail_count
python -m register_core nodes add 'http://u:p@host:port' --label us1
```

| File | Format |
|------|--------|
| `nodes.json` | `{ "version": 1, "nodes": [ { "url", "id", "label", "tags", "enabled" } ] }` |
| `nodes.txt` / `nodes.list` | one URL per line |

### Register adaptation (preflight + quarantine) — product feature

When operators import their own HTTP/SOCKS nodes, **each batch register** live-probes the catalog and only puts dual-pass (or L1-only) URLs into rotation:

```text
nodes import profile.yaml          # write full catalog (schema; optional --check)
        │
pipeline.run (egress=list|auto)
  → stash provider name on extra
  → resolve_probe_targets (extra / env / provider map)
  → preflight_nodes_for_register   # L1 ipify ∧ optional L2 business targets
  → seed proxy_list = pool_ready only
  → each attempt: inject_attempt_proxy (rotate)
  → on proxy/network fail (incl. ERR_EMPTY_RESPONSE / RST): mark fail_count, drop
  → quarantine after REGISTER_NODES_MAX_FAIL (default 3)
  → 0 dual-pass on egress=list → FailFastError (no burn)
```

| Layer | Probe | Pass | Catalog stamp |
|-------|-------|------|---------------|
| **L1** | `https://api.ipify.org?format=json` | HTTP 2xx | mutates `last_ok` |
| **L2** | provider business URL(s) | any HTTP status (transport) | filter-only; does not hard-quarantine on L2 miss |

Default L2 map: `grok`/`xai` → `accounts.x.ai`; `chatgpt`/`openai` → `auth.openai.com`; `mimo`/`xiaomi`/`mimo-tts` → `api.xiaomimimo.com`. Unknown provider → L1-only (legacy).

| Env | Default | Meaning |
|-----|---------|---------|
| `REGISTER_NODES_PREFLIGHT` | `1` | Probe catalog before register |
| `REGISTER_NODES_MAX_FAIL` | `3` | Failures before quarantine |
| `REGISTER_NODES_SKIP_FAILED` | `1` | Skip quarantined in rotation |
| `REGISTER_NODES_REQUIRED` | list-backend true | Zero healthy → fail-fast |
| `REGISTER_NODES_PROBE_TIMEOUT` | `12` | Probe timeout seconds |
| `REGISTER_NODES_PROBE_LIMIT` | `40` | Max probes per preflight (`0` = unlimited) |
| `REGISTER_NODES_PROBE_TARGETS` | provider map | Comma L2 URLs; `0`/`none` disables L2 |

**Preflight is skipped (logged)** when:

| Reason | When | Operator note |
|--------|------|---------------|
| `backend=core\|clash\|direct` | not list/auto | Catalog probe N/A |
| `REGISTER_NODES=0` | catalog disabled | — |
| `explicit_proxy_list` | `PROXY_LIST` / `CHATGPT_PROXY_LIST` set | Operator-owned pool; set `force_nodes_preflight=1` to also probe catalog |
| `preflight_disabled` | `REGISTER_NODES_PREFLIGHT=0` | Not recommended for imported catalogs |

Non-proxy failures (`mail_miss`, captcha, `registration_disallowed`) **do not** quarantine nodes. Proxy/network markers include `empty response` / `connection reset` language so runtime RST cools the proxy without burning email domains.

## 3) Protocol core (only if needed)

```bash
./scripts/bootstrap_nodes_core.sh
python -m register_core nodes core start
python -m register_core nodes core proxies
python -m register_core nodes core select 'node-name'
python -m register_core nodes core url     # http://127.0.0.1:17897
```

## 4) Egress switch

```bash
python -m register_core nodes egress show
python -m register_core nodes egress set list    # primary
python -m register_core nodes egress set core
python -m register_core nodes egress set direct
# advanced:
python -m register_core nodes egress set auto    # healthy list → core → clash(if set)
python -m register_core nodes egress set clash
```

`auto` only uses nodes.json entries with `last_ok: true` (after `nodes check`).  
Unprobed bulk dumps do **not** block project core.

Env: `REGISTER_EGRESS=list|core|direct` (or advanced auto/clash).  
Persisted: `.nodes/config/egress.mode`.

## Not in scope

- Shipping credentials or mihomo binary in git (`.nodes/`, `nodes.json` gitignored)
- Self-implemented VLESS/SS crypto stacks
- Treating external GUI VPN as a required dependency
