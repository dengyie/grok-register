# Project-owned egress nodes

Register machine keeps its own node catalog. **No Clash / mihomo / system VPN
is required** for ChatGPT or other in-process providers.

## Catalog

| File | Format |
|------|--------|
| `nodes.json` | `{ "version": 1, "nodes": [ { "url", "id", "label", "tags", "enabled" } ] }` |
| `nodes.txt` / `nodes.list` | one `http://user:pass@host:port` per line |

Path resolution: `REGISTER_NODES_FILE` / `NODES_FILE` → first existing default → `./nodes.json`.

Copy `nodes.example.json` → `nodes.json` and replace with real upstream proxies
(residential / ISP / static HTTP proxies you control). Files are gitignored.

## CLI

```bash
python -m register_core nodes list
python -m register_core nodes check          # probe ipify via curl_cffi
python -m register_core nodes add 'http://u:p@host:port' --label us1
python -m register_core nodes urls --redact
```

## How registration uses nodes

1. `Pipeline` / `register_core.util.proxy` loads enabled URLs from the catalog.
2. Auto-enables **list** rotation (`proxy_rotate`) — each attempt binds a concrete URL.
3. ChatGPT `curl_cffi` session uses that URL directly (not a local Clash port).

Priority:

```text
extra.proxy_list / CHATGPT_PROXY_LIST / PROXY_LIST
  → nodes.json enabled URLs
  → single CHATGPT_PROXY / proxy
  → (none — fail if required)
```

Disable catalog: `REGISTER_NODES=0`.

## Not in scope

- Embedding a full VLESS/Hysteria client binary (operators supply **HTTP/SOCKS
  proxy endpoints** the process can dial).
- Driving external Clash UI to switch GLOBAL/PROXY — that is explicitly out.
