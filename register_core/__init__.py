"""Layered multi-provider account register framework.

Layers (dependency direction top → bottom):

  hub / CLI
    → pipeline (orchestrator)
      → providers/*   (product signup: grok, mimo, …)
      → email/*       (mailbox allocate + OTP poll)
      → verify/*      (post-signup capability probe)
      → sink/*        (persist accounts/keys)
      → contracts + errors (shared types)

Providers keep their own browser stack (Python/Drission vs Node/Playwright).
This package only standardizes interfaces and orchestration.
"""

from .contracts import (
    Mailbox,
    OtpCode,
    RegisterJob,
    RegisterResult,
    VerifyResult,
)
from .errors import (
    CaptchaError,
    FailFastError,
    MailMissError,
    ProviderError,
    VerifyError,
)

__all__ = [
    "Mailbox",
    "OtpCode",
    "RegisterJob",
    "RegisterResult",
    "VerifyResult",
    "CaptchaError",
    "FailFastError",
    "MailMissError",
    "ProviderError",
    "VerifyError",
]
