"""Parse register machine accounts_cli.txt lines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def normalize_sso_cookie(value: str | None) -> str:
    """Normalize SSO cookie / JWT-like token from ledger or browser export.

    Some capture paths prefix a stray ``-`` before ``eyJ...``. Protocol mint
    treats that as invalid and lands on sign-in; stripping restores a usable JWT.

    Only strips leading dashes when a JWT header (``eyJ``) is nearby — never
    blindly lstrip all dashes from arbitrary session ids.
    """
    s = (value or "").strip()
    if not s:
        return ""
    # Strip accidental leading dashes only when a JWT header is present nearby.
    while s.startswith("-") and "eyJ" in s[:8]:
        s = s[1:].lstrip()
    return s.strip()


def format_account_line(email: str, password: str, sso: str | None = None) -> str:
    """Build one accounts_cli ledger line with normalized SSO."""
    email_s = (email or "").strip()
    password_s = (password or "").strip()
    sso_s = normalize_sso_cookie(sso)
    if sso_s:
        return f"{email_s}----{password_s}----{sso_s}\n"
    return f"{email_s}----{password_s}\n"


def email_match_keys(email: str) -> set[str]:
    """Identity keys for plus-alias ↔ sanitized filename matching.

    CPA filenames replace ``+`` (and other unsafe chars) with ``-``, so
    ``user+abc@x.com`` becomes stem ``user-abc@x.com``. Skip/existing checks
    must treat both forms as the same account.
    """
    e = (email or "").strip().lower()
    if not e:
        return set()
    keys = {e, e.replace("+", "-")}
    try:
        from .schema import credential_file_name

        fn = credential_file_name(e)
        if fn.startswith("xai-") and fn.endswith(".json"):
            stem = fn[len("xai-") : -len(".json")].lower()
            if stem:
                keys.add(stem)
    except Exception:
        pass
    return keys


def email_in_existing(email: str, existing: set[str]) -> bool:
    """True if email (any alias/sanitized form) is already in existing set."""
    if not existing:
        return False
    return bool(email_match_keys(email) & existing)


@dataclass
class AccountLine:
    email: str
    password: str
    sso: str
    raw: str
    line_no: int


def parse_accounts_file(path: str | Path) -> list[AccountLine]:
    path = Path(path)
    out: list[AccountLine] = []
    if not path.is_file():
        return out
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("----")
        if len(parts) < 2:
            continue
        email = parts[0].strip()
        password = parts[1].strip()
        sso = normalize_sso_cookie(parts[2] if len(parts) > 2 else "")
        if not email or not password:
            continue
        out.append(AccountLine(email=email, password=password, sso=sso, raw=s, line_no=i))
    return out


def existing_cpa_emails(auth_dir: str | Path) -> set[str]:
    """Emails already present as xai-*.json in auth_dir.

    Returns an expanded key set (raw email, plus→dash, filename stem) so
    skip-existing works for Hotmail plus-aliases even when JSON lacks email.
    """
    auth_dir = Path(auth_dir)
    found: set[str] = set()
    if not auth_dir.is_dir():
        return found
    for p in auth_dir.glob("xai-*.json"):
        name = p.name[len("xai-") : -len(".json")]
        if name:
            found |= email_match_keys(name)
        try:
            import json

            d = json.loads(p.read_text(encoding="utf-8"))
            em = str(d.get("email") or "").strip()
            if em:
                found |= email_match_keys(em)
        except Exception:
            continue
    return found
