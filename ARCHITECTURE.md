# Architecture — ai-register-machine

Production-oriented multi-provider register monorepo. Inspired by:

- [ThinkerWen/ai-register](https://github.com/ThinkerWen/ai-register) — `register/<product>` + shared `util`
- LiteLLM / LangChain — explicit layers, registry, partner/provider packages, Makefile + ARCHITECTURE
- Our additions — fail-fast, this-run attribution, black-box honesty, CPA OIDC/OpenAI gates, desktop UI

## Goals

| Goal | Meaning |
|------|---------|
| **Usable** | One hub (`./register.sh`), one layered CLI (`python -m register_core`), desktop GUI |
| **Honest** | Success = this-run delta / RESULT_JSON; never historical tail alone |
| **Layered** | email / providers / verify / sink / pipeline contracts |
| **Product-local stacks** | Grok stays Python+Drission; MiMo stays Node+Playwright — no fake merge |
| **Safe defaults** | gitignore secrets, sink 0600, public redact, no mass alias farm |

## Directory map

```text
ai-register-machine/
├── register.sh                 # hub: grok | mimo | core | smoke | help
├── ARCHITECTURE.md             # this file (canonical layout)
├── Makefile                    # test / syntax / doctor / help
├── pyproject.toml              # uv + pytest
├── config.example.json         # Grok production config template
├── config.simple.example.json  # outsider quickstart
├── apps/
│   ├── README.md               # entrypoints map
│   ├── cli/                    # thin docs for CLI paths
│   └── gui/                    # thin docs for TTK GUI
├── register_core/              # shared layers (contracts → pipeline)
│   ├── contracts.py
│   ├── errors.py
│   ├── pipeline.py
│   ├── cli.py
│   ├── email/                  # EmailSource registry + sources/*
│   ├── providers/              # adapter registry (black-box OK)
│   ├── verify/
│   ├── sink/
│   └── util/
├── providers/                  # product packages (runtime authority)
│   ├── README.md
│   ├── _template/              # copy-me skeleton for a new product
│   ├── mimo/                   # Xiaomi MiMo (Node/Playwright) — production
│   └── grok/                   # Grok layout notes (runtime still root paths)
├── docs/
│   ├── ADDING_PROVIDER.md
│   └── LAYOUT.md
├── examples/
│   └── minimal_pipeline.py
├── scripts/                    # ops helpers (doctor, remint, setup)
├── tests/                      # preferred home for new tests
│   ├── conftest.py
│   └── unit/                   # layer unit tests live here when added
├── cpa_xai/                    # Grok OIDC mint / chat probe (Grok product lib)
├── register_cli.py             # Grok CLI production entry (legacy root path)
├── grok_register_ttk.py        # Desktop GUI production entry
└── turnstilePatch/             # browser extension for Grok path
```

## Layer dependency (one way)

```text
hub / GUI / apps
        │
        ▼
   register_core.pipeline
        │
        ├── providers  (signup)
        ├── email      (allocate + OTP)     [in-process only]
        ├── verify     (capability probe)
        └── sink       (JSONL 0600)
              │
              ▼
         contracts + errors
```

Black-box providers (`grok`, `mimo`) **own mail internally**. Passing `--email-source=tinyhost` is rejected so we never pretend the pipeline controls their mailbox.

## Production authority (do not invert)

| Concern | Authority path |
|---------|----------------|
| Grok signup + mint + chat gate | `./register.sh grok` → `register_cli.py` / `grok_register_ttk.py` + `cpa_xai/` |
| MiMo API key | `./register.sh mimo` → `providers/mimo/run-register.sh` |
| Layered orchestration only | `./register.sh core` → `python -m register_core` |
| CPA OpenAI inject (MiMo) | `providers/mimo/inject_cpa_openai.py` (local; never silent prod write) |

Root-level Grok modules (`register_cli.py`, `grok_register_ttk.py`, `cpa_xai/`, `proxy_*`) remain **runtime-valid** until a dedicated migrate milestone. `providers/grok/` documents the target package shape without breaking imports.

## Registry pattern

Factories live in `register_core/*/registry.py`:

- `register_provider` / `get_provider` / `list_providers`
- `register_email_source` / `get_email_source`
- `get_verifier`

New products: implement adapter → register factory → optional verifier → document in `providers/README.md`.

## Success attribution (hard rule)

1. Prefer structured `RESULT_JSON:` (or equivalent) from this process.
2. Else file **size offset before run** + append-only delta.
3. Exit 0 without this-run identity → **failure**.
4. Subprocess timeout → kill process group (`start_new_session` + `killpg`).

## Hard safety contracts

| Contract | Enforcement |
|----------|-------------|
| API key shape | `register_core.util.secrets` — single source; adapter/verify/inject/redact must agree |
| Grok product-ready | this-run **SSO** required (`ok=False` if email-only / pending) |
| MiMo product-ready | this-run **secret** via RESULT_JSON or file delta (never historical tail) |
| CPA OpenAI inject | no default prod path; `--config`/`CPA_CONFIG` + prod requires `--i-understand-production` |
| Deploy path | `GROK_CODE_ROOT` or first existing `/personal/{ai-register-machine,register-machine,grok-register}` |

## Non-goals (this skeleton)

- Mass account farm / alias email expansion
- Unifying browser stacks into one framework
- Silent production CPA config mutation
- Web UI (desktop TTK is the mature UI for Grok today)
