# Project-owned egress nodes

Two layers — both live **inside this repo**, not Clash Verge UI:

| Layer | Path | What |
|-------|------|------|
| HTTP/SOCKS catalog | `nodes.json` | dialable URLs for curl_cffi |
| Protocol core | `.nodes/` + mihomo | VLESS/SS/VMess/Trojan… from your YAML → `http://127.0.0.1:17897` |

## 1) HTTP/SOCKS catalog

| File | Format |
|------|--------|
| `nodes.json` | `{ "version": 1, "nodes": [ { "url", "id", "label", "tags", "enabled" } ] }` |
| `nodes.txt` / `nodes.list` | one `http://user:pass@host:port` per line |

```bash
python -m register_core nodes list
python -m register_core nodes check
python -m register_core nodes add 'http://u:p@host:port' --label us1
```

## 2) Embedded mihomo core (YAML protocol nodes)

```bash
./scripts/bootstrap_nodes_core.sh
python scripts/import_clash_to_nodes.py          # import Clash Verge YAMLs into .nodes/
python -m register_core nodes core start
python -m register_core nodes core proxies
python -m register_core nodes core select '🇺🇸【北美洲】美国04原生丨直连【2x】'
python -m register_core nodes core url            # http://127.0.0.1:17897
```

Import packs medium profiles into `.nodes/config/runtime.yaml` (gitignored).
Mega free lists (>400 proxies/file) are skipped for core; their HTTP/SOCKS still
go into `nodes.json`.

## How registration picks egress

```text
extra.proxy_list / PROXY_LIST
  → nodes.json enabled HTTP/SOCKS
  → project mihomo mixed-port (.nodes core, auto-start)
  → fixed CHATGPT_PROXY
```

Env: `REGISTER_CORE=auto|1|0`, `REGISTER_CORE_AUTOSTART=1`, `REGISTER_NODES=0` to
skip HTTP catalog only.

## Not in scope

- Driving **external** Clash Verge UI / system TUN as a required dependency
- Shipping subscription credentials in git (`.nodes/` and `nodes.json` are gitignored)
