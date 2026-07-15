# Providers

Product packages. Each provider owns its browser stack and (for black-box runners) its mailbox.

| Provider | Path | Stack | Hub | Status |
|----------|------|-------|-----|--------|
| **Grok / xAI** | root `register_cli.py` + `cpa_xai/` · notes in `providers/grok/` | Python + Drission | `./register.sh grok` | production |
| **MiMo** | `providers/mimo/` | Node + Playwright | `./register.sh mimo` | production |
| **_template** | `providers/_template/` | your choice | — | copy-me |

Layered orchestration (not a replacement for the above):

```bash
./register.sh core list
./register.sh core run -p mimo -n 1
./register.sh core run -p grok -n 1 --no-verify
```

See [docs/ADDING_PROVIDER.md](../docs/ADDING_PROVIDER.md) and [ARCHITECTURE.md](../ARCHITECTURE.md).
