# Target-Aware Node Preflight Implementation Plan

> **For agentic workers:** single-phase milestone; implement in place, verify tests, commit once.

**Goal:** Seed register rotation only with proxies that pass L1 (ipify) **and** L2 (provider business domain).

**Architecture:** Resolve target URLs from provider/extra/env → layered probe → `proxy_list = pool_ready` → empty fail-fast on list.

**Tech Stack:** Python 3, existing `curl_cffi`/urllib probe stack, unittest.

## Global Constraints

- Do not break Clash preflight path.
- Do not burn email domains on proxy RST.
- Do not soft-inject CPA without chat_ok.
- Never commit secrets / nodes.json / ops noise.

---

### Task 1: Targets + layered probe + wire + tests + docs

**Files:**
- Create: `register_core/nodes/targets.py`
- Modify: `register_core/nodes/health.py`
- Modify: `register_core/nodes/manager.py`
- Modify: `register_core/util/proxy.py`
- Modify: `register_core/pipeline.py`
- Modify: `register_core/nodes/README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `test_register_core_nodes.py`
- Create: design + plan under `docs/superpowers/`

- [x] Implement `resolve_probe_targets` + default map
- [x] Implement `probe_reachable` (any status) + `probe_node_layered`
- [x] `NodeManager.preflight(probe_urls=...)` builds pool from layered ok
- [x] `preflight_nodes_for_register` resolves targets; pipeline sets provider
- [x] Add empty-response markers to proxy network heuristic
- [x] Unit tests for filter / fail-fast / resolve
- [x] Docs product contract
- [x] Run tests + commit
