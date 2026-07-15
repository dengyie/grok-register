"""Shared secret patterns (API keys, redaction helpers).

MiMo / vendor OpenAI-compat keys may contain hyphens or underscores after
the sk- prefix (e.g. sk-hyper-..., sk-foo_bar-...). All parsers and verifiers
in this repo must use these patterns so inject / adapter / verify stay aligned.
"""

from __future__ import annotations

import re

# Body after "sk-": first char alnum, then alnum/_/- ; min length keeps noise out.
_API_KEY_BODY = r"sk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"

API_KEY_RE = re.compile(_API_KEY_BODY)
API_KEY_SEARCH_RE = re.compile(_API_KEY_BODY)
API_KEY_FULLMATCH_RE = re.compile(rf"^{_API_KEY_BODY}$")
# Word-boundary form for log redaction (avoid eating trailing punctuation).
API_KEY_WORD_RE = re.compile(rf"\b{_API_KEY_BODY}\b")


def is_api_key(value: str | None) -> bool:
    if not value:
        return False
    return bool(API_KEY_FULLMATCH_RE.match(value.strip()))


def find_api_keys(text: str | None) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    for m in API_KEY_SEARCH_RE.finditer(text):
        k = m.group(0)
        if k not in found:
            found.append(k)
    return found


def preview_secret(value: str | None, *, head: int = 4, tail: int = 4) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) <= head + tail:
        return "***"
    return f"{s[:head]}…{s[-tail:]}(len={len(s)})"
