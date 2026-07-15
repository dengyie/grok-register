# Apps (entrypoints)

Thin map of user-facing entrypoints. Implementation may still live at repo root.

| App | How to run | Notes |
|-----|------------|--------|
| **Hub** | `./register.sh help` | Multi-provider |
| **Grok CLI** | `./register.sh grok 1 1` | Production |
| **Grok GUI** | `uv run python grok_register_ttk.py` | Form + progress + logs |
| **MiMo** | `./register.sh mimo` | Node runtime |
| **Core** | `./register.sh core list` | Layered orchestration |

Subdirs `cli/` and `gui/` hold short docs only until package extract.
