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
export CONTROL_API_HOST=127.0.0.1
export CONTROL_API_PORT=8787
# Recommended: stable session secret + optional script bearer
export CONTROL_API_SESSION_SECRET="$(openssl rand -hex 32)"
export CONTROL_API_TOKEN="$(openssl rand -hex 32)"   # optional; for curl/scripts
# Create operator (once):
uv run python scripts/control_api_user.py set admin
# Or bootstrap on first start:
# export CONTROL_API_BOOTSTRAP_USER=admin CONTROL_API_BOOTSTRAP_PASSWORD='…'
./scripts/run_control_api.sh
```

Open `http://127.0.0.1:8787` → **login form** (username/password). Session is HttpOnly cookie.

- API: `/api/health`, `/api/auth/*`, `/api/overview`, `/api/config`, `/api/import/*`, `/api/runs/*`
- UI: static files under `apps/web/` served by FastAPI
- Auth (either):
  - **Browser:** password login → signed cookie `control_session`
  - **Scripts:** `Authorization: Bearer <CONTROL_API_TOKEN>` or `X-Control-Token`
- Operators file: `.control_api_users.json` (gitignored, mode 0600, scrypt hashes)
- Login rate limit: 8 failures / 5 min per IP+username
- Default bind is localhost; use SSH tunnel for remote browser access
- Disable password login only for break-glass: `CONTROL_API_PASSWORD_LOGIN=0` (then rely on bearer or open)

Subdir `gui/` only documents that the desktop TTK UI was removed.
