#!/usr/bin/env python3
"""OtpWaitDiagnostics + MailMissError.diagnostics (offline)."""

from __future__ import annotations

import unittest
from dataclasses import asdict

from register_core.contracts import OtpWaitDiagnostics
from register_core.errors import MailMissError


class TestOtpWaitDiagnostics(unittest.TestCase):
    def test_defaults_and_asdict(self) -> None:
        d = OtpWaitDiagnostics()
        self.assertEqual(d.poll_count, 0)
        self.assertEqual(d.failure_class, "")
        payload = asdict(d)
        self.assertIn("message_scan_count", payload)
        self.assertIn("matched_after_seconds", payload)

    def test_mail_miss_carries_diagnostics(self) -> None:
        diag = OtpWaitDiagnostics(
            poll_count=3,
            empty_rounds=3,
            failure_class="no_mail",
            elapsed_seconds=90.0,
            timeout_s=90.0,
            provider="gmail_imap",
        )
        exc = MailMissError("gmail empty OTP", diagnostics=diag)
        self.assertEqual(str(exc), "gmail empty OTP")
        self.assertIs(exc.diagnostics, diag)
        self.assertEqual(exc.diagnostics.failure_class, "no_mail")

    def test_mail_miss_without_diagnostics(self) -> None:
        exc = MailMissError("plain miss")
        self.assertIsNone(exc.diagnostics)
        self.assertEqual(str(exc), "plain miss")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
