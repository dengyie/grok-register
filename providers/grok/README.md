# Grok / xAI provider

## Runtime authority (current)

Grok production still lives at **repo root** for stable imports and pxed paths:

| Entry | Path |
|-------|------|
| Hub | `./register.sh grok [count] [threads]` |
| CLI | `register_cli.py` |
| GUI | `grok_register_ttk.py` |
| OIDC / chat | `cpa_xai/` |
| Adapter | `register_core/providers/grok_adapter.py` (black-box → CLI) |

Do **not** move these modules into this directory without a dedicated migrate milestone (breaks pxed `/personal/grok-register` scripts and import paths).

## Target package shape (future)

```text
providers/grok/
  README.md
  run-register.sh          # thin wrapper → root or package CLI
  py/                      # optional package extract of register core
  assets/turnstilePatch/
```

## Success gate

Register → SSO ledger → CPA OIDC mint → free Build **chat probe**.  
`entitlement_denied` is not remint-worthy; models-only 200 is not product success.

## Config

Use root `config.example.json` / `config.simple.example.json`. Secrets never committed.
