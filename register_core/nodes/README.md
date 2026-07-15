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

# empty catalog (does not touch protocol runtime):
python -m register_core nodes clear --yes

# advanced: scan local Clash Verge profiles (opt-in only)
python -m register_core nodes import --from-clash-verge
```

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
python -m register_core nodes check
python -m register_core nodes add 'http://u:p@host:port' --label us1
```

| File | Format |
|------|--------|
| `nodes.json` | `{ "version": 1, "nodes": [ { "url", "id", "label", "tags", "enabled" } ] }` |
| `nodes.txt` / `nodes.list` | one URL per line |

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
