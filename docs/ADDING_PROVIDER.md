# Adding a provider

Copy the template, wire the registry, keep production authority honest.

## 1. Scaffold

```bash
cp -R providers/_template providers/<name>
# edit providers/<name>/README.md, run-register.sh (or Python entry)
```

Choose a stack:

| Style | When | Email |
|-------|------|-------|
| **Black-box** | Existing Node/Python runner with own mail | `email_source=provider` only |
| **In-process** | Pure Python using `EmailSource` | any registered source |

## 2. Adapter

Implement `RegisterProvider` in `register_core/providers/<name>_adapter.py`:

```python
class FooProvider:
    name = "foo"

    def register_one(self, *, email_source=None, extra=None) -> RegisterResult:
        # MUST attribute success to THIS run (offset / RESULT_JSON / ledger delta)
        ...
```

Register in `register_core/providers/registry.py` `_ensure_builtins`.

If black-box, add the name to `Pipeline._BLACKBOX_PROVIDERS`.

## 3. Verify + sink

- Optional: `register_core/verify/<name>.py` + registry
- Pipeline can use `JsonlSink` (`O_CREAT|0600`)

## 4. Hub

```bash
# register.sh — add a case that execs providers/<name>/run-register.sh
./register.sh <name> [count]
```

## 5. Tests (required)

Minimum offline cases:

1. Historical output file alone does **not** count as success
2. Exit 0 with no this-run identity → fail
3. `to_public_dict` never leaks full secret/password
4. Black-box + external `--email-source` raises

```bash
make test
# or
uv run python -m pytest tests/unit test_register_core_layers.py -q
```

## 6. Docs

- `providers/<name>/README.md` — env, run, artifacts, CPA path if any
- One line in `providers/README.md`
- Changelog entry under Added

## Do not

- Read only the last line of a shared ledger as success
- Commit keys, mail dumps, or live CPA YAML
- Auto-write production CPA without an explicit operator command
