"""Typed failures for the register pipeline.

Fatal vs retriable:
  FailFastError  — stop the whole batch (alias exhausted, config broken, captcha infra dead)
  MailMissError  — this mailbox got no OTP; pipeline may allocate another (bounded)
  CaptchaError   — captcha solve failed for this attempt
  ProviderError  — signup page / product flow failed
  VerifyError    — account created but capability probe failed
"""

from __future__ import annotations


class RegisterCoreError(Exception):
    """Base for framework errors."""


class FailFastError(RegisterCoreError):
    """Unrecoverable for this batch — do not spin or open more browsers."""


class MailMissError(RegisterCoreError):
    """OTP not received for the allocated mailbox (bounded retry OK)."""


class CaptchaError(RegisterCoreError):
    """Captcha challenge failed for this attempt."""


class ProviderError(RegisterCoreError):
    """Product registration flow failed."""


class VerifyError(RegisterCoreError):
    """Post-register verification failed."""
