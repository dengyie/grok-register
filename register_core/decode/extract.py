"""Single authoritative OTP-code decoder.

Moved out of ``register_core.email.sources.tinyhost`` (2026-07-18) so the
legacy ``grok_register_ttk.extract_verification_code`` could collapse onto
ONE decoder instead of maintaining a weaker duplicate. Two diverging copies
of the same decode was how the xAI ``#333333`` CSS-hex mis-decode slipped in:
the copy in the legacy path drifted from the one that actually strips
``<style>`` blocks.

Contract:
  - xAI real code is an alnum+dash ``XXX-XXX`` token (e.g. ``FN8-ECQ``) in the
    subject ("FN8-ECQ xAI confirmation code") and body.
  - OpenAI uses a 6-digit code inside "verification code" context.
  - ``<style>{color:#333333}</style>`` must be stripped BEFORE any digit
    search, else the bare ``\\b(\\d{4,8})\\b`` fallback seizes ``333333`` and
    xAI rejects the form. See pxed smoke 2026-07-18.
"""

from __future__ import annotations

import re

OAI_SUBJECT_XAI_CODE_RE = re.compile(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", re.I)
XAI_BODY_CODE_RE = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b")
# Kept for callers that want the bare 4-8 digit fallback; extract_otp_code goes
# through the contextual OpenAI patterns first to avoid CSS-hex false hits.
OTP_RE = re.compile(r"\b(\d{4,8})\b")
_OPENAI_OTP_PATTERNS = (
    re.compile(r"temporary\s+verification\s+code[^\d]{0,80}(\d{6})", re.I),
    re.compile(r"verification\s+code\s+to\s+continue[:\s]+(\d{6})", re.I),
    re.compile(r"verification\s+code[^\d]{0,40}(\d{4,8})", re.I),
    re.compile(r"your\s+(?:temporary\s+)?code[:\s]+(\d{4,8})", re.I),
    re.compile(r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})", re.I),
    re.compile(r"otp[^\d]{0,20}(\d{4,8})", re.I),
)


def extract_otp_code(blob: str, subject: str = "") -> str:
    """Extract a real OTP code from a decoded email (xAI ``XXX-XXX`` or OpenAI 6-digit)."""
    # 1. xAI subject-style "FN8-ECQ xAI confirmation code".
    if subject:
        m = OAI_SUBJECT_XAI_CODE_RE.search(str(subject))
        if m:
            return m.group(1)
    raw = str(blob or "")
    # 2. xAI body token (works on raw HTML too — alnum+dash isn't in CSS).
    m = XAI_BODY_CODE_RE.search(raw)
    if m:
        return m.group(1)
    # 3+4. OpenAI/numeric — strip style/script/comments FIRST so CSS hex
    # colors like #333333 / #888888 never win a 6-digit hit.
    if "<" in raw and ">" in raw:
        raw = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", raw)
        raw = re.sub(r"(?is)<!--.*?-->", " ", raw)
        raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    m = XAI_BODY_CODE_RE.search(raw)
    if m:
        return m.group(1)
    for pat in _OPENAI_OTP_PATTERNS:
        m = pat.search(raw)
        if m:
            return m.group(1)
    # Subject-aligned 6-digit fallback (OpenAI subject context).
    if subject and re.search(r"openai|verification code", subject, re.I):
        m = re.search(r"\b(\d{6})\b", raw)
        if m:
            return m.group(1)
    return ""
