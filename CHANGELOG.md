# Changelog

All notable changes to this project are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
project versioning follows [Semantic Versioning](https://semver.org/).

## [1.3.0] - 2026-07-15

### Added

- **Multi-provider hub**: `./register.sh` (`grok` / `mimo` / `core` / `smoke`)
- **`register_core/`** layered framework: contracts, email sources, provider adapters, verify, sink, pipeline, CLI
- **`providers/mimo/`**: Xiaomi MiMo API Key registration (Node/Playwright `register-one.js`, Geetest slide, tinyhost OTP)
- MiMo → CPA OpenAI-compat helper (optional, local): `providers/mimo/inject_cpa_openai.py` (does **not** touch production by default)
- Unit tests: `test_register_core_layers.py`, `test_mimo_cpa_openai_inject.py`
- Grok Gmail IMAP: HTTP CONNECT via configured proxy (container/no-public egress)
- Positioning docs vs [ThinkerWen/ai-register](https://github.com/ThinkerWen/ai-register): higher production usability, richer pipeline, **TTK UI** (form input + live log/status)
- **Monorepo skeleton** (inspired by ai-register product dirs + LiteLLM-style ARCHITECTURE/Makefile):
  - `ARCHITECTURE.md`, `docs/LAYOUT.md`, `docs/ADDING_PROVIDER.md`
  - `providers/_template/`, `providers/grok/README.md`, `providers/README.md`
  - `apps/{cli,gui}/`, `examples/minimal_pipeline.py`, `tests/unit/`, `Makefile`

### Changed

- Project rename path: **`grok-register` → `register-machine` → `ai-register-machine`** (GitHub + package metadata)
- README highlights desktop UI (`grok_register_ttk.py`): count/threads/mail/proxy form, start/stop, status, scrolling logs, tutorial
- Fail-fast + this-run result attribution (no historical tail as success); public redact password/secret
- pytest `pythonpath` + `tests/` tree; gitignore all `providers/*/output|node_modules`

### Fixed

- Align `sk-` patterns across adapter / verifier / inject / redaction (hyphen & underscore vendor keys)
- Grok adapter no longer reports `ok=True` with empty SSO (`pending`); default Grok verifier requires SSO
- `inject_cpa_openai.py` no longer defaults to production CPA path; requires `--config`/`CPA_CONFIG` and `--i-understand-production` for prod
- `register.sh` / MiMo clash starter resolve monorepo root via `GROK_CODE_ROOT` and multi-name candidates (not hard-only `/personal/grok-register`)
- README no longer links a non-existent `v1.3.0` GitHub Release tag

### Security

- Jsonl sink `O_CREAT|0600`; black-box providers reject fake external `--email-source`
- Ignore MiMo runtime output / node_modules; never commit keys or mail ledgers
- Production CPA inject is opt-in with explicit ack (no silent default path)

## [1.2.3] - 2026-07-13

### Added

- `scripts/doctor_secrets.sh` — local secret hygiene (tracked paths, modes, cloud-sync path warn; never prints contents)
- CI: `bash -n` on setup/doctor scripts; py_compile quality scripts; run doctor_secrets in guard step
- README education banner + local secret hygiene; CONTRIBUTING ttk refactor caution + chat-gate rule

### Changed

- Package description emphasizes OIDC + chat gate (not Hotmail-only / not a quota farm)

## [1.2.2] - 2026-07-13

### Fixed

- Simple default email channel is **duckmail** (not hotmail four-segment) for lower onboarding friction
- `setup_simple.sh` doctor: Python/Chrome/proxy port, duckmail key / hotmail placeholder warnings; `uv sync` failure no longer aborts bootstrap
- README honest **最短路径** timing, 常见卡点 table, production inject key checklist
- Packaging tests: `bash -n`, setup smoke in temp dir (no overwrite)

## [1.2.1] - 2026-07-13

### Added

- Outsider-friendly packaging: `config.simple.example.json` + `scripts/setup_simple.sh`
- README quickstart (simple local path; tebi inject off by default)
- Product success table: chat probe required; `entitlement_denied` ≠ remint

## [1.2.0] - 2026-07-13

### Added

- One-click CPA chain: `cpa_auth_priority` end-to-end, multi remote auth-dirs (live + inventory)
- `cpa_remote_live_dir` / `cpa_remote_live_required` — **live inject is the product success gate**
- Free Build **chat entitlement gate**: default-on `/v1/responses` probe; 403 → `entitlement_denied`, skip live inject, remint ledger skip
- Transient chat probe retries + `chat_retryable` auth stamps; `entitlement_denied.jsonl`
- Auth proxy bridge for Chromium `user:pass` proxies; browser recycle modes; account slot retry
- `scripts/remint_expired_and_sync_authdir.py` for inventory remint without starting registration
- Offline tests for one-click dirs/priority, live-gate, and chat entitlement

### Fixed

- `account_slot_retry=0` no longer coerced to 3; no outer worker × slot multiplicative alias burn
- Mint browser no longer double-applies `--proxy-server`
- Proxy bridge failure hard-fails (no silent direct)
- Inventory-only remote inject no longer counts as one-click success when live was targeted
- Models-only 200 no longer counts as usable free Build when chat probe is on
- CI `py_compile` covers `proxy_bridge.py` and remint script

### Security

- Runtime mail/auth assets remain gitignored (`mail_credentials.txt`, `mail_assets/`)

## [1.1.3] - 2026-07-12

### Fixed

- Hard resource/config failures (Hotmail alias exhaustion, missing mail credentials, unconfigured providers) now **stop the whole batch immediately** instead of empty-loop retries
- `FatalRegisterError` + `_fatal_stop` event: workers exit without further account retries; process exit code `2`

### Added

- `classify_email_stage_failure` returns `fatal` for unrecoverable markers
- Offline tests for fatal classification and stop wiring

## [1.1.2] - 2026-07-12

### Added

- Startup cleanup of orphan Drission Chromes reparented to init/launchd (PPID=1)
- `tab_pool.cleanup_orphan_drission_chromes` / `TabPool.cleanup_orphans` with protect list
- Offline tests for Drission Chrome cmdline matching and dry-run cleanup

### Notes

- Success path still **reuses** the register browser (`clear_session`); cleanup only targets crashed leftovers, not the live worker Chrome.

## [1.1.1] - 2026-07-12

### Fixed

- SSO leading-dash normalize now applies at mint core, protocol extract/set, GUI ledger write, and shared `format_account_line`
- `existing_cpa_emails` / skip-existing match Hotmail plus-aliases against sanitized CPA filenames
- Official backfill script routes through `cpa_export` (remote inject + hooks); `--local-only` / `--no-remote` opt out
- Config booleans in `cpa_export` use `_config_bool` so string `"false"` is not truthy
- CI syntax-checks `grok_register_ttk.py`

### Added

- `format_account_line`, `email_match_keys`, `email_in_existing` helpers
- Offline tests for cookie extract normalize, plus-alias skip keys, `_config_bool`

## [1.1.0] - 2026-07-12

### Added

- Public project packaging for open-source release
- `LICENSE` (MIT), `DISCLAIMER.md`, `SECURITY.md`, `CONTRIBUTING.md`
- GitHub Actions CI (syntax + offline tests)
- Local account backup helpers (`account_backup.py`, `scripts/backup_registered_accounts.py`)
- Optional remote CPA auth inject after successful OIDC mint
- Hotmail/Outlook Office REST code fetch with IMAP fallback
- Protocol-first CPA OIDC mint (`cpa_xai/protocol_mint.py`) with browser fallback

### Changed

- README rebranded as **Grok 注册机** with full setup / ops docs
- Live Hotmail REST test gated behind `GROK_REGISTER_LIVE=1`
- Project package name aligned to `grok-register`

### Security

- Runtime secrets remain gitignored (`config.json`, accounts, CPA auths, backups, logs)

## [1.0.0] - 2026-07-11

### Added

- Chromium + DrissionPage registration core
- Hotmail four-field credential pool and plus-alias registration
- CPA export hook and `cpa_xai` OIDC tooling
- CLI (`register_cli.py`) and GUI (`grok_register_ttk.py`) entrypoints
