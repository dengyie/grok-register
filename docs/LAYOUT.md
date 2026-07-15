# Repository layout (quick)

See [ARCHITECTURE.md](../ARCHITECTURE.md) for the full map.

| Path | Role |
|------|------|
| `register.sh` | Multi-provider hub |
| `register_core/` | Layered orchestration library |
| `providers/<name>/` | Product package (scripts, README, env) |
| `providers/_template/` | Copy-me for new products |
| `apps/` | Human map of CLI/GUI entrypoints |
| `docs/` | How-to and layout |
| `examples/` | Minimal safe samples |
| `scripts/` | Ops (doctor, remint, setup) |
| `tests/` | Preferred location for **new** tests |
| Root `test_*.py` | Legacy offline tests (CI still runs them) |
| `cpa_xai/`, `register_cli.py`, `grok_register_ttk.py` | Grok production (root until migrate) |

Runtime secrets stay gitignored: `config.json`, `.env`, `mail_credentials.txt`, `accounts_*.txt`, `cpa_auths/`, `backups/`, `logs/`, `providers/*/output/`.
