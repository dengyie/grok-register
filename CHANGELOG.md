# Changelog

All notable changes to this project are documented in this file.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
project versioning follows [Semantic Versioning](https://semver.org/).

## [1.2.0] - 2026-07-13

### Added

- One-click CPA chain: `cpa_auth_priority` end-to-end, multi remote auth-dirs (live + inventory)
- `cpa_remote_live_dir` / `cpa_remote_live_required` — **live inject is the product success gate**
- Auth proxy bridge for Chromium `user:pass` proxies; browser recycle modes; account slot retry
- `scripts/remint_expired_and_sync_authdir.py` for inventory remint without starting registration
- Offline tests for one-click dirs/priority and live-gate partial inject semantics

### Fixed

- `account_slot_retry=0` no longer coerced to 3; no outer worker × slot multiplicative alias burn
- Mint browser no longer double-applies `--proxy-server`
- Proxy bridge failure hard-fails (no silent direct)
- Inventory-only remote inject no longer counts as one-click success when live was targeted
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
