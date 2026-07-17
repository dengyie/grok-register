# Target-Aware Node Preflight (L1 ∧ L2) Design

**Date:** 2026-07-17  
**Status:** Approved (user: make small milestone + development docs; strategy-group domain preflight before inject)  
**Goal:** Before seeding the register rotation pool from `nodes.json`, live-probe not only generic egress (ipify) but the **business domains required by the current provider**, and inject only the dual-pass subset.

## Context

### Evidence (2026-07-17)

- Imported auth proxies: **299 unique → pxed probe ok=258** (L1 = `api.ipify.org`).
- Grok smoke with one random L1-healthy list node: browser **ERR_EMPTY_RESPONSE / RST** on `accounts.x.ai`.
- Sample of 40 L1-healthy catalog nodes → **0** reachable for `accounts.x.ai` (almost all Connection reset).
- Clash mixed-port `127.0.0.1:7897` → `accounts.x.ai` **200**.

Root cause: product “healthy” was **shallow** — “can egress to ipify” ≠ “can reach registration target”.

### Dual backends (unchanged)

| Backend | Entry | Probe | Scope of this milestone |
|---------|-------|-------|-------------------------|
| Clash mihomo (pxed Grok prod) | `preflight-clash-nodes.sh` | controller delay API + strategy groups | **Out of scope** (already target-group aware) |
| `nodes.json` list\|auto | `preflight_nodes_for_register` | HTTP via proxy | **In scope** — add L2 business targets |

## Architecture

```text
nodes import → catalog (schema; optional L1 --check)
        │
pipeline.run(egress=list|auto, provider=P)
        │
preflight_nodes_for_register
  resolve_probe_targets(P | extra | env)
        │
  for each candidate node (smart order, limit):
    L1: GET https://api.ipify.org?format=json  → require 2xx + optional IP
    L2: GET each business URL through same proxy
        → require *transport success* (any HTTP status OK)
        → fail on RST / tunnel timeout / empty / connect error
        │
  pool_ready = L1 ∧ L2
        │
  seed proxy_list = pool_ready only
  0 pool_ready on list|required → FailFastError (no account burn)
        │
  register attempts on pool_ready only
        │
  runtime proxy/network fail (incl. ERR_EMPTY_RESPONSE)
    → mark/cool/quarantine proxy; never burn email domain
```

### Health layers

| Layer | Probe | Pass rule | Mutates `last_ok` |
|-------|-------|-----------|-------------------|
| **L1** egress | `DEFAULT_PROBE_URL` (ipify) | HTTP 2xx | **Yes** (catalog stamp) |
| **L2** target | provider business URL(s) | TCP/TLS/HTTP response received (status ignored) | **No** — filters pool only |
| **L3** runtime | register outcome | existing `report_attempt_proxy_result` | fail_count / cool / quarantine |

**Why L2 ignores HTTP status:** registration targets often return 302/403/426 without cookies/CLI headers. That still proves the proxy can open the path. RST / `ERR_EMPTY_RESPONSE` is the failure mode we observed.

### Default target map (strategy-group analogue)

| Provider aliases | Default L2 URL(s) |
|------------------|-------------------|
| `grok`, `xai` | `https://accounts.x.ai/` |
| `chatgpt`, `openai` | `https://auth.openai.com/` |
| `mimo`, `xiaomi`, `mimo-tts` | `https://api.xiaomimimo.com/` |
| unknown / empty | L1 only (backward compatible) |

### Override order (highest first)

1. `extra["probe_targets"]` / `extra["nodes_probe_targets"]` — list or comma string  
2. Env `REGISTER_NODES_PROBE_TARGETS` / `NODES_PROBE_TARGETS` — comma URLs  
3. Provider map from `extra["provider"]` / `extra["_provider"]` (pipeline injects `self.provider.name`)  
4. Empty → L1-only (legacy)

`REGISTER_NODES_PROBE_TARGETS=0` or `none` disables L2 explicitly.

## Module plan

| File | Change |
|------|--------|
| `register_core/nodes/targets.py` | **New** — map + `resolve_probe_targets` |
| `register_core/nodes/health.py` | `probe_reachable`; `probe_node_layered` (L1∧L2) |
| `register_core/nodes/manager.py` | `check_all` / `preflight` accept `probe_urls`; pool from result when L2 on |
| `register_core/util/proxy.py` | resolve targets; pass into manager; empty_response markers |
| `register_core/pipeline.py` | stash `provider` name on extra before preflight |
| `register_core/nodes/README.md` + `ARCHITECTURE.md` | product contract L1/L2 |
| `test_register_core_nodes.py` | L2 filter + empty pool fail-fast + resolve map |

## Non-goals (P2/P3 backlog)

- Clash strategy-group rewrite inside monorepo preflight  
- Persist `last_ok_by_target` on Node (runtime filter is enough for M1)  
- AND across many L2 URLs beyond first resolved set (defaults are 1 URL)  
- Soft-inject CPA on chat 403  
- Changing import to require L2  

## Manual-required

- Grok production on pxed: keep **Clash 7897** until an L2-ready catalog pool is non-empty for `accounts.x.ai`.  
- Free Build chat 403 remains account entitlement, unrelated to this milestone.

## Acceptance

1. Unit: L1-only path unchanged when no provider/targets.  
2. Unit: L1 pass + L2 fail → URL **not** in seeded `proxy_list`.  
3. Unit: all L2 fail on `egress=list` → `FailFastError`.  
4. Unit: pipeline/extra provider `grok` resolves `accounts.x.ai`.  
5. Docs: ARCHITECTURE + nodes README describe L1∧L2 gate.  
6. Runtime markers include empty response / connection reset language.

## Git

Single commit preferred:  
`feat(nodes): target-aware L1+L2 preflight before pool inject`
