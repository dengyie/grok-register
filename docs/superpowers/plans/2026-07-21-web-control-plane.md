# Web Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a project-owned FastAPI control API + static web UI for config, import, start/stop runs and status; delete desktop TTK GUI while keeping `grok_register_ttk` engine for CLI.

**Architecture:** `apps/control_api` is a thin adapter over existing `config.json`, import scripts, `launch_batch_supervisor.sh`, and `./register.sh`. `apps/web` is static HTML/CSS/JS served by FastAPI. No Node build. Auth via `CONTROL_API_TOKEN`; default bind `127.0.0.1:8787`.

**Tech Stack:** Python 3.13, FastAPI, Uvicorn, Pydantic v2, pytest, TestClient; vanilla JS frontend.

## Global Constraints

- Deploy model B: in-repo closed loop on the register host (no SSH agent control plane).
- Multi-product hub: Grok + MiMo + ChatGPT.
- Import types: nodes/proxies, mail credentials, account/token dumps, config packs.
- Auto-run tier: start/stop + realtime status only (no scheduler).
- Delete desktop GUI; keep engine in `grok_register_ttk.py` (register_cli imports it).
- Bind default `127.0.0.1` + bearer token; backup-before-write for config/mail.
- Disk-first: supervisor freezes mid-mint `CPA_REMOTE_INJECT=false`; no mid-bulk inject UI.
- No arbitrary shell; whitelist actions only.
- Stop only recorded supervisor/run PID — never blanket `pkill register_cli`.
- Do not stop coinbot or disrupt live `batch_dc1k_ns` during deploy tests.
- No Node/npm build on pxed.
- Spec: `docs/superpowers/specs/2026-07-21-web-control-plane-design.md`

## File map

| Path | Role |
|------|------|
| `apps/control_api/__init__.py` | Package marker |
| `apps/control_api/__main__.py` | `python -m apps.control_api` entry |
| `apps/control_api/settings.py` | Env: root, host, port, token, upload limits |
| `apps/control_api/auth.py` | Bearer / X-Control-Token dependency |
| `apps/control_api/config_io.py` | Load/redact/save config.json + bak |
| `apps/control_api/overview.py` | Product counts + current run summary |
| `apps/control_api/imports_ops.py` | Nodes/mail/auths/pack import helpers |
| `apps/control_api/process_registry.py` | Track started PIDs; stop by pid only |
| `apps/control_api/runs.py` | Start supervisor / register.sh; status; logs |
| `apps/control_api/schemas.py` | Pydantic models |
| `apps/control_api/app.py` | FastAPI factory + routes + static mount |
| `apps/web/index.html` | SPA shell: Overview/Config/Import/Runs |
| `apps/web/assets/app.css` | Minimal styles |
| `apps/web/assets/app.js` | fetch API + page wiring |
| `tests/unit/test_control_api_*.py` | Unit + TestClient tests |
| `scripts/run_control_api.sh` | Launch helper |
| Docs/mise/pyproject/README/ARCHITECTURE | Retarget GUI → web |
| `grok_register_ttk.py` | Strip `GrokRegisterGUI` + tk imports |
| Delete `tests/unit/test_gui_layout_helpers.py`, replace `apps/gui` docs |

---

### Task 1: Dependencies + package skeleton + settings/auth

**Files:**
- Modify: `pyproject.toml` (add fastapi, uvicorn[standard]; retarget description later in Task 7)
- Create: `apps/control_api/__init__.py`, `settings.py`, `auth.py`, `schemas.py` (minimal HealthOut)
- Create: `tests/unit/test_control_api_auth.py`
- Create: `apps/__init__.py` if missing (needed for `python -m apps.control_api`)

**Interfaces:**
- Produces: `Settings(project_root: Path, host: str, port: int, token: str | None, max_upload_bytes: int)`, `get_settings()`, `require_token(request/header) -> None`, `HealthOut`

- [ ] **Step 1: Add deps to pyproject.toml**

```toml
dependencies = [
    "DrissionPage>=4.1",
    "curl_cffi>=0.7",
    "PyYAML>=6.0",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
]
```

Run: `cd /Users/mango/project/claude-project/grok-register && uv sync --extra dev`

- [ ] **Step 2: Write failing auth tests**

```python
# tests/unit/test_control_api_auth.py
from pathlib import Path
import os
import pytest
from fastapi.testclient import TestClient

def test_settings_reads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_TOKEN", "secret-token")
    monkeypatch.setenv("CONTROL_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CONTROL_API_PORT", "8787")
    from apps.control_api.settings import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.project_root == tmp_path.resolve()
    assert s.token == "secret-token"
    assert s.port == 8787

def test_health_ok_without_token_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("CONTROL_API_TOKEN", raising=False)
    from apps.control_api.settings import get_settings
    get_settings.cache_clear()
    from apps.control_api.app import create_app
    client = TestClient(create_app())
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True

def test_protected_route_401_without_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_TOKEN", "secret-token")
    from apps.control_api.settings import get_settings
    get_settings.cache_clear()
    from apps.control_api.app import create_app
    client = TestClient(create_app())
    r = client.get("/api/overview")
    assert r.status_code == 401

def test_protected_route_ok_with_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("REGISTER_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTROL_API_TOKEN", "secret-token")
    (tmp_path / "cpa_auths").mkdir()
    from apps.control_api.settings import get_settings
    get_settings.cache_clear()
    from apps.control_api.app import create_app
    client = TestClient(create_app())
    r = client.get("/api/overview", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
```

- [ ] **Step 3: Implement skeleton so tests can import**

`apps/__init__.py` empty.  
`apps/control_api/__init__.py` empty.  
`apps/control_api/settings.py`:

```python
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    project_root: Path
    host: str
    port: int
    token: str | None
    max_upload_bytes: int = 20 * 1024 * 1024

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = os.environ.get("REGISTER_PROJECT_ROOT")
    project_root = Path(root).resolve() if root else Path.cwd().resolve()
    token = os.environ.get("CONTROL_API_TOKEN") or None
    if token is not None and token.strip() == "":
        token = None
    host = os.environ.get("CONTROL_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CONTROL_API_PORT", "8787"))
    return Settings(project_root=project_root, host=host, port=port, token=token)
```

`apps/control_api/auth.py`:

```python
from __future__ import annotations
from fastapi import Header, HTTPException, status
from apps.control_api.settings import get_settings

def require_token(
    authorization: str | None = Header(default=None),
    x_control_token: str | None = Header(default=None, alias="X-Control-Token"),
) -> None:
    settings = get_settings()
    if not settings.token:
        return
    presented = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_control_token:
        presented = x_control_token.strip()
    if presented != settings.token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token")
```

Minimal `app.py` with `/api/health` (no auth) and `/api/overview` stub (Depends require_token) returning `{"product_ok":0,"run":None}` until Task 2.

- [ ] **Step 4: Run tests**

`uv run pytest tests/unit/test_control_api_auth.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock apps/ tests/unit/test_control_api_auth.py
git commit -m "feat(control_api): skeleton, settings, auth"
```

---

### Task 2: Config IO + overview counts

**Files:**
- Create: `apps/control_api/config_io.py`, `apps/control_api/overview.py`
- Extend: `schemas.py`, `app.py` routes GET/PUT `/api/config`, GET `/api/overview`
- Create: `tests/unit/test_control_api_config.py`

**Interfaces:**
- Produces:
  - `load_config(root: Path) -> dict`
  - `redact_config(data: dict) -> dict` (mask secret-like keys)
  - `save_config(root: Path, data: dict) -> dict`  # returns `{backup, changed_keys}`
  - `SECRET_KEY_SUBSTR = ("password","api_key","token","jwt","secret")` — mask if key lower contains
  - `count_product_ok(root: Path) -> int`  # cpa_auths/xai-*.json with access+refresh
  - `build_overview(root: Path, run_summary: dict | None) -> dict`

Secret keys never returned in full after save; empty string on PUT means leave unchanged for secret fields.

- [ ] **Step 1: Failing tests for load/redact/save/backup and overview count**

```python
# tests/unit/test_control_api_config.py
import json
from pathlib import Path
from apps.control_api.config_io import load_config, redact_config, save_config
from apps.control_api.overview import count_product_ok

def test_load_strips_comment_keys(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "// note": "x",
        "email_provider": "cloudflare",
        "cloudflare_api_key": "abc12345",
    }), encoding="utf-8")
    data = load_config(tmp_path)
    assert "// note" not in data
    assert data["email_provider"] == "cloudflare"

def test_redact_masks_secrets():
    out = redact_config({"email_provider": "cloudflare", "cloudflare_api_key": "abc12345", "proxy": "http://x"})
    assert out["email_provider"] == "cloudflare"
    assert out["cloudflare_api_key"].startswith("***")
    assert out["cloudflare_api_key"].endswith("2345") or "2345" in out["cloudflare_api_key"]
    assert out["proxy"] == "http://x"

def test_save_backup_and_preserve_secret_on_empty(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "email_provider": "cloudflare",
        "cloudflare_api_key": "keepme-secret",
        "defaultDomains": "a.com",
    }), encoding="utf-8")
    result = save_config(tmp_path, {
        "email_provider": "cloudflare",
        "cloudflare_api_key": "",
        "defaultDomains": "b.com",
    })
    assert result["backup"]
    assert Path(result["backup"]).is_file()
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["cloudflare_api_key"] == "keepme-secret"
    assert data["defaultDomains"] == "b.com"

def test_count_product_ok(tmp_path):
    d = tmp_path / "cpa_auths"
    d.mkdir()
    (d / "xai-a.json").write_text(json.dumps({"access_token":"a","refresh_token":"r"}), encoding="utf-8")
    (d / "xai-b.json").write_text(json.dumps({"access_token":"a"}), encoding="utf-8")
    (d / "other.json").write_text(json.dumps({"access_token":"a","refresh_token":"r"}), encoding="utf-8")
    assert count_product_ok(tmp_path) == 1
```

- [ ] **Step 2: Implement config_io + overview; wire routes**

`load_config`: json load, drop keys starting with `//` or `#`.  
`save_config`: merge with existing for empty secret fields; shutil.copy2 backup `config.json.bak-web-<YYYYMMDD_HHMMSS>`; atomic write via temp + replace.  
`count_product_ok`: glob `cpa_auths/xai-*.json`, require both tokens non-empty strings.

PUT body: full dict of non-comment keys (client sends redacted view edited — server treats `***...` masked values as unchanged too: if value starts with `***` leave old).

- [ ] **Step 3: pytest PASS + commit**

```bash
git add apps/control_api tests/unit/test_control_api_config.py
git commit -m "feat(control_api): config io, redact, overview counts"
```

---

### Task 3: Process registry + runs start/stop/status/logs

**Files:**
- Create: `apps/control_api/process_registry.py`, `apps/control_api/runs.py`
- Extend: `schemas.py` (`StartRunRequest`), `app.py`
- Create: `tests/unit/test_control_api_runs.py`

**Interfaces:**
- `ProcessRegistry` (in-memory + optional `logs/control_api_runs.json` under project root for restart visibility)
  - `register(run_id, pid, kind, meta) -> None`
  - `current() -> dict | None`
  - `clear_if_dead() -> None`
- `supervisor_lock_held(root) -> bool` — check `/tmp/grok_batch_supervisor.lock.pid` alive OR registry current kind grok_supervisor alive
- `start_run(root, req: StartRunRequest) -> dict` — 409 if conflict
- `stop_run(root) -> dict` — SIGTERM then wait 10s then SIGKILL on **recorded pid only**
- `run_status(root) -> dict` — merge registry + parse latest `logs/*supervisor.log` / state.json if present
- `tail_log(root, n=200) -> str`

**StartRunRequest fields:**  
`kind: Literal["grok_supervisor","register_sh"]`, `product: Literal["grok","mimo","chatgpt"]="grok"`, `mode: Literal["ordinary","residential"]="ordinary"`, `target: int=100`, `threads: int=1`, `tag: str="batch_web"`, `extra_env: dict[str,str]={}`

**extra_env allowlist:**  
`SKIP_CLASH_PREFLIGHT`, `CPA_PROBE_CHAT`, `CPA_BATCH_END_INJECT`, `SUPERVISOR_CHUNK`, `EMAIL_PROVIDER`, `DEFAULT_DOMAINS`, `NODE_SCORE`  
Unknown key → 400.

**Start commands (cwd=project_root, env=os.environ copy + allowlist):**
- `grok_supervisor`: `bash scripts/launch_batch_supervisor.sh {mode} {target} {threads} {tag}`
- `register_sh`: `bash ./register.sh {product} {target} {threads}`  (if product needs 2 args only, pass what register.sh accepts — use `target` as count and threads as 3rd arg)

Use `subprocess.Popen` with stdout/stderr to `logs/control_api_{run_id}.log`. Do not use `shell=True` with user strings; argv list only.

- [ ] **Step 1: Tests with mocked Popen / fake pid**

```python
def test_extra_env_reject_unknown():
    from apps.control_api.runs import filter_extra_env
    import pytest
    with pytest.raises(ValueError, match="not allowed"):
        filter_extra_env({"EVIL": "1"})

def test_start_409_when_lock_pid_alive(tmp_path, monkeypatch):
    # write fake lock pid = os.getpid(); start_run raises HTTPException 409
    ...

def test_stop_only_recorded_pid(monkeypatch):
    # registry has pid; stop calls os.kill with that pid only
    ...
```

- [ ] **Step 2: Implement + wire POST `/api/runs/start`, POST `/api/runs/stop`, GET `/api/runs/current`, GET `/api/runs/current/logs?tail=200`, GET `/api/runs`**

- [ ] **Step 3: pytest PASS + commit**

```bash
git commit -m "feat(control_api): runs start/stop/status/logs"
```

---

### Task 4: Import operations

**Files:**
- Create: `apps/control_api/imports_ops.py`
- Extend: `app.py` multipart endpoints
- Create: `tests/unit/test_control_api_import.py`

**Interfaces:**
- Staging dir: `{root}/output/web_uploads/` (mkdir parents)
- Reject path traversal: resolved path must be under staging or project root
- `import_nodes(root, file_path: Path, *, dry_run: bool, replace: bool) -> dict`  
  subprocess: `[sys.executable, "scripts/import_nodes.py", str(file_path), ...]` with optional `--dry-run` `--replace`
- `import_mail(root, content: str, *, mode: Literal["append","replace"]) -> dict`  
  target `mail_credentials.txt` (or config `hotmail_accounts_file`); backup then write
- `import_auths(root, src_dir: Path, *, no_remote: bool=True) -> dict`  
  subprocess `scripts/import_cpa_auth_dir.py --src ...` + `--no-remote` default
- `import_pack(root, zip_path: Path, *, apply: bool) -> dict`  
  extract to staging; if apply: backup+copy config.json / nodes.json / mail file if present

Upload size enforced via settings.max_upload_bytes.

- [ ] **Step 1: Tests** — traversal reject; mail append backup; nodes dry-run mocks subprocess

- [ ] **Step 2: Implement endpoints**  
  `POST /api/import/nodes`, `/mail`, `/auths`, `/pack`

- [ ] **Step 3: pytest + commit**

```bash
git commit -m "feat(control_api): import nodes/mail/auths/pack"
```

---

### Task 5: Static web UI

**Files:**
- Create: `apps/web/index.html`, `apps/web/assets/app.css`, `apps/web/assets/app.js`
- Extend: `app.py` mount StaticFiles for `apps/web` at `/` (mount after API routes; use `html=True`)
- Create: `scripts/run_control_api.sh`
- Create: `apps/control_api/__main__.py` (uvicorn run with settings host/port)

**UI behavior:**
- Token prompt stored in `sessionStorage.controlToken`; send as Bearer
- Nav: Overview | Config | Import | Runs
- Overview: fetch `/api/overview` every 5s when visible
- Config: load redacted JSON into form fields for key groups (email_provider, defaultDomains, proxy, proxy_rotate_mode, turnstile_stuck_timeout, cpa_probe_chat, cpa_remote_inject); Save → PUT
- Import: file inputs + dry-run checkbox for nodes; mail textarea; auths note local-only default
- Runs: start form (kind, mode, target, threads, tag); Stop button; log pre tail refresh

Keep CSS simple dark-neutral readable; Chinese+English labels OK matching project.

- [ ] **Step 1: Implement static files + __main__ + run script**

`scripts/run_control_api.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export REGISTER_PROJECT_ROOT="${REGISTER_PROJECT_ROOT:-$ROOT}"
export CONTROL_API_HOST="${CONTROL_API_HOST:-127.0.0.1}"
export CONTROL_API_PORT="${CONTROL_API_PORT:-8787}"
exec .venv/bin/python -m apps.control_api
```

- [ ] **Step 2: Manual smoke** — `CONTROL_API_TOKEN=dev uv run python -m apps.control_api` & curl health

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(web): static control plane UI + run entry"
```

---

### Task 6: Delete desktop GUI (surgical)

**Files:**
- Modify: `grok_register_ttk.py` — remove lines class `GrokRegisterGUI` through `main()`/Tk `__main__`; remove top-level tkinter imports if unused above
- Delete: `tests/unit/test_gui_layout_helpers.py`
- Replace: `apps/gui/README.md` with short “removed; use Web control plane”
- Modify: `apps/README.md`, `mise.toml` (gui task → control-api)

**Keep:** all engine code above former class line; `register_cli` import path.

- [ ] **Step 1: Delete GUI class** — replace tail with:

```python
def main() -> None:
    raise SystemExit(
        "Desktop GUI removed. Use: scripts/run_control_api.sh "
        "or: uv run python -m apps.control_api (see apps/README.md)."
    )

if __name__ == "__main__":
    main()
```

Remove `import tkinter` / `from tkinter import ...` at top if nothing else needs them (verify with rg that engine body does not use tk).

- [ ] **Step 2: Delete test_gui_layout_helpers.py; update mise/apps README**

- [ ] **Step 3: Verify**

```bash
uv run python -m py_compile grok_register_ttk.py register_cli.py
uv run pytest -q
rg -n "GrokRegisterGUI|import tkinter" --glob '*.py' || true
```

Expect: no GrokRegisterGUI; no tkinter in py sources; pytest green.

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor: remove desktop TTK GUI; keep register engine"
```

---

### Task 7: Docs + pyproject polish + full regression

**Files:**
- Modify: `README.md` (desktop UI → Web control plane section)
- Modify: `ARCHITECTURE.md` (usable row; backlog Web UI done; tree apps/control_api + apps/web)
- Modify: `pyproject.toml` description/keywords (`web` not `gui`)
- Modify: `docs/DEVELOPED.md` if it mentions desktop GUI
- Modify: `.gitignore` add `output/web_uploads/`

- [ ] **Step 1: Doc edits** matching design success criteria

- [ ] **Step 2: Full pytest**

```bash
uv run pytest -q
```

- [ ] **Step 3: Commit**

```bash
git commit -m "docs: web control plane is the operator UI"
```

---

## Spec coverage checklist

| Spec item | Task |
|-----------|------|
| FastAPI control API | 1–4 |
| Static web UI | 5 |
| Overview / Config / Import / Runs | 2–5 |
| Auth token + 127.0.0.1 | 1, 5 |
| Backup-before-write | 2, 4 |
| Whitelist start/env | 3 |
| Stop by pid only | 3 |
| Four import types | 4 |
| Delete desktop GUI keep engine | 6 |
| Docs/mise/pyproject | 6–7 |
| Tests | 1–4, 7 |

## Execution note

User requested immediate development. Prefer **inline execution** of this plan in order Task 1→7 without waiting for a second confirmation. Do not stop live pxed batch. Do not enable mid-mint CPA inject.
