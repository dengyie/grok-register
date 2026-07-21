# Desktop GUI — removed

The TTK desktop UI (`GrokRegisterGUI`) was removed.

Use the **Web control plane** instead:

```bash
export CONTROL_API_TOKEN=$(openssl rand -hex 32)   # recommended
./scripts/run_control_api.sh
# open http://127.0.0.1:8787
```

See `apps/README.md` and `docs/superpowers/specs/2026-07-21-web-control-plane-design.md`.
