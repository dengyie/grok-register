"""Mail-path proxy resolution — never inherits register egress.

Used by Pipeline when constructing EmailSource and by provider adapters.
Register proxy (PROXY_LIST / attempt proxy) must not leak into mailbox HTTP.
"""

from __future__ import annotations

import os
from typing import Any


def resolve_mail_proxy(extra: dict[str, Any] | None = None) -> str:
    """Return dedicated mail HTTP proxy URL, or empty for direct.

    Priority:
      1. extra["mail_proxy"] / extra["email_proxy"]
      2. CHATGPT_MAIL_PROXY / EMAIL_PROXY / MAIL_PROXY env
    Never falls back to register egress (extra["proxy"], PROXY_LIST, etc.).
    """
    extra = extra if isinstance(extra, dict) else {}
    for key in ("mail_proxy", "email_proxy"):
        v = str(extra.get(key) or "").strip()
        if v:
            return v
    for env in ("CHATGPT_MAIL_PROXY", "EMAIL_PROXY", "MAIL_PROXY"):
        v = str(os.environ.get(env) or "").strip()
        if v:
            return v
    return ""
