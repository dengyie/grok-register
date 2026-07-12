# Hotmail mail assets (local only)

This directory holds imported Hotmail credential batches (`hotmail.com_*.txt`).

- Format per line: `email----password----ClientID----Token`
- **Never commit** these files (gitignored via `mail_assets/`)
- Active pool used by the registerer: project-root `mail_credentials.txt`
  (`config.hotmail_accounts_file`)

Import / merge into the active pool:

```bash
# already done by ops; re-merge if you add new batches:
# (dedupe by email, keep first occurrence)
python3 - <<'PY'
from pathlib import Path
root = Path('.')
pool = root / 'mail_credentials.txt'
seen = set()
out = []
for src in [pool, *sorted((root/'mail_assets').glob('hotmail.com_*.txt'))]:
    if not src.is_file():
        continue
    for line in src.read_text(encoding='utf-8', errors='replace').splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        email = s.split('----', 1)[0].strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        out.append(s)
pool.write_text('\n'.join(out) + ('\n' if out else ''), encoding='utf-8')
print(f'merged unique accounts: {len(out)}')
PY
```
