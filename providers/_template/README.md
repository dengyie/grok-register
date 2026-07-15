# Provider template: `{{name}}`

Copy this directory:

```bash
cp -R providers/_template providers/myproduct
```

## Checklist

- [ ] `name` / aliases in `register_core/providers/registry.py`
- [ ] Adapter `register_one` with **this-run** success attribution
- [ ] Black-box? Add to `Pipeline._BLACKBOX_PROVIDERS`
- [ ] `run-register.sh` or Python entry; hub case in `register.sh`
- [ ] `.env.example` (no secrets)
- [ ] Offline tests for attribution + redact
- [ ] Row in `providers/README.md`

## Suggested layout

```text
providers/myproduct/
  README.md
  .env.example
  run-register.sh          # COUNT=1 production runner
  scripts/                 # product-specific
  output/                  # gitignored
```

## Contract

Return `RegisterResult(ok=..., provider=..., email=..., secret=..., secret_kind=...)`.  
Never treat historical `output/*` tails as this-run success.
