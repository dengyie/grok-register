# Contributing

Thanks for considering a contribution.

## Before you start

1. Read [DISCLAIMER.md](DISCLAIMER.md) and [SECURITY.md](SECURITY.md).
2. Do **not** commit secrets or local runtime files.
3. Prefer small, focused pull requests.

## Development setup

```bash
git clone https://github.com/dengyie/ai-register-machine.git
cd ai-register-machine
# outsider path (Grok simple config)
bash scripts/setup_simple.sh
# or full template
uv sync --extra dev
cp config.example.json config.json
# optional: mail_credentials for live mail tests only
bash scripts/doctor_secrets.sh   # hygiene; never prints secret contents
```

Python **3.13** is required (`requires-python` in `pyproject.toml`). Install via [uv](https://docs.astral.sh/uv/): `uv python install 3.13`.

Repo layout (multi-provider) — see [ARCHITECTURE.md](ARCHITECTURE.md):

- `register_cli.py` / `grok_register_ttk.py` / `cpa_xai/` — Grok production path (root until migrate)
- `providers/mimo/` — MiMo production path (Node)
- `providers/_template/` — copy-me for new products ([docs/ADDING_PROVIDER.md](docs/ADDING_PROVIDER.md))
- `register_core/` — layered orchestration (not a browser rewrite)
- `./register.sh` / `Makefile` — unified hub + dev targets
- `apps/`, `docs/`, `examples/`, `tests/` — entry map, how-tos, samples, unit tests

## Tests

Offline (default, used by CI):

```bash
make test          # or: uv run python -m pytest -q
make test-unit
make syntax
bash -n scripts/setup_simple.sh scripts/doctor_secrets.sh register.sh
bash scripts/doctor_secrets.sh || test $? -eq 2
```

Live Hotmail REST (needs real `mail_credentials.txt`, **not** for CI):

```bash
GROK_REGISTER_LIVE=1 uv run python test_hotmail_rest_code.py
```

Syntax check (CI also compiles `grok_register_ttk.py` and scripts):

```bash
uv run python -m py_compile register_cli.py grok_register_ttk.py cpa_export.py account_backup.py cpa_xai/*.py
```

## Coding guidelines

- Match existing style; **avoid drive-by refactors in `grok_register_ttk.py`** (~5k lines). Prefer extract-with-tests if you must split; do not “clean up while here.”
- Keep changes scoped to the bug/feature
- Add or extend offline tests when fixing logic (prefer behavior tests over source-string contracts when practical)
- Never log raw passwords, refresh tokens, or access tokens
- Document user-facing config keys in `config.example.json` / `config.simple.example.json` comment keys
- **SSO handling:** only use `cpa_xai.accounts.normalize_sso_cookie` / `format_account_line`. Do not invent a second strip rule. Normalize must stay at mint core + ledger write (CLI/GUI).
- **CPA export path:** production register and default backfill go through `cpa_export.export_cpa_xai_for_account` so remote inject / backup hooks stay consistent. Do not reintroduce “mint only” as the default backfill path.
- **Chat gate:** free Build product success requires chat probe when enabled; `entitlement_denied` must not soft-pass or remint-spin.
- **Config booleans:** use `_config_bool` (or equivalent) so string `"false"` is false.

## Pull requests

- Describe **what** and **why**
- Note how you tested (offline / live)
- Confirm no secrets are included (`git status`, diff review)
- Link related issues when applicable

## Issue reports

Include OS, Python version, proxy yes/no, headed/headless, and redacted logs.
Do not paste SSO cookies, OIDC tokens, or mailbox refresh tokens.
