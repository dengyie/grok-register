# Apps (entrypoints)

Thin map of user-facing entrypoints. Implementation may still live at repo root.

| App | How to run | Notes |
|-----|------------|--------|
| **Hub** | `./register.sh help` | Multi-provider |
| **Grok CLI** | `./register.sh grok 1 1` | Production |
| **Web control plane** | `./scripts/run_control_api.sh` | Config / Import / Runs UI at `http://127.0.0.1:8787` |
| **MiMo** | `./register.sh mimo` | Node runtime |
| **Core** | `./register.sh core list` | Layered orchestration |

## Web control plane

```bash
export REGISTER_PROJECT_ROOT="$(pwd)"   # optional; defaults to cwd
export CONTROL_API_TOKEN="$(openssl rand -hex 32)"
export CONTROL_API_HOST=127.0.0.1
export CONTROL_API_PORT=8787
./scripts/run_control_api.sh
```

- API: `/api/health`, `/api/overview`, `/api/config`, `/api/import/*`, `/api/runs/*`
- UI: static files under `apps/web/` served by FastAPI
- Auth: `Authorization: Bearer <CONTROL_API_TOKEN>` (or header `X-Control-Token`)
- Default bind is localhost; use SSH tunnel for remote browser access

Subdir `gui/` only documents that the desktop TTK UI was removed.
