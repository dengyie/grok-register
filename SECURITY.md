# Security Policy

## Supported versions

Only the latest `main` branch is maintained. There is no long-term support
branch.

## What is sensitive

Never commit or publish:

| Path / pattern | Contents |
|----------------|----------|
| `config.json` | proxies, remote hosts, feature flags, secrets |
| `.env` | environment secrets |
| `mail_credentials.txt` | mailbox password + Microsoft refresh_token |
| `mail_assets/` | imported Hotmail credential batches |
| `accounts_cli.txt` / `accounts_*.txt` | Grok password + SSO cookie |
| `cpa_auths/*.json` | OIDC access_token / refresh_token |
| `backups/` | full credential snapshots |
| `logs/` / `screenshots/` / `cookies/` | may embed PII or tokens |

Templates are safe to share: `config.example.json`, `mail_credentials.example.txt`, `.env.example`.

## Reporting a vulnerability

Please **do not** open a public issue for security problems that expose
credentials, remote access, or RCE-style bugs.

Prefer one of:

1. GitHub **private vulnerability report** on this repository (if enabled)
2. A private contact via the repository owner’s GitHub profile

Include:

- affected commit / tag
- reproduction steps (with redacted secrets)
- impact assessment

We aim to acknowledge reports within a reasonable time; there is no bug bounty.

## If secrets leak

1. **Revoke / rotate immediately**
   - Microsoft mailbox refresh_token / app password
   - xAI / Grok session (SSO) by password reset or session revoke if available
   - CPA / OIDC tokens (re-mint after rotation)
   - SSH passwords / keys used for remote inject
2. Remove secrets from any fork, gist, chat log, or CI artifact
3. If the secret was pushed to Git history, rotate first, then rewrite or
   abandon the tainted history; rotation matters more than history scrubbing
4. Check remote CPA auth-dir and delete leaked `xai-*.json` files

## Safe defaults for contributors

- Run offline tests only in CI (`GROK_REGISTER_LIVE` unset)
- Prefer dummy values in examples
- Never attach real `cpa_auths` or mail credentials to pull requests
- Redact emails and tokens in logs pasted into issues
- SSO normalize only strips a leading `-` when a JWT header (`eyJ`) is nearby; do not change this to blind `lstrip("-")` on arbitrary session values

## Remote inject

`cpa_remote_inject` may use `sshpass` and a credentials file. That password is
as sensitive as root on the target host. Prefer SSH keys + `ssh-agent` when
possible; keep credential files mode `600` outside the repo.
