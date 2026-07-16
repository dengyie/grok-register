#!/usr/bin/env python3
"""error_kind taxonomy constants + normalize (offline)."""

from __future__ import annotations

import unittest

from register_core.contracts import ALLOWED_ERROR_KINDS, normalize_error_kind


class TestErrorKinds(unittest.TestCase):
    def test_allowed_contains_oss_set(self) -> None:
        for k in (
            "mail_miss",
            "registration_disallowed",
            "captcha",
            "proxy",
            "network",
            "provider",
            "verify",
            "fatal",
            "other",
        ):
            self.assertIn(k, ALLOWED_ERROR_KINDS)

    def test_normalize_keeps_known(self) -> None:
        self.assertEqual(normalize_error_kind("registration_disallowed"), "registration_disallowed")
        self.assertEqual(normalize_error_kind("mail_miss"), "mail_miss")
        self.assertEqual(normalize_error_kind("proxy"), "proxy")
        self.assertEqual(normalize_error_kind("network"), "network")
        self.assertEqual(normalize_error_kind("fatal"), "fatal")

    def test_normalize_unknown_to_provider(self) -> None:
        self.assertEqual(normalize_error_kind("weird_thing"), "provider")
        self.assertEqual(normalize_error_kind(""), "provider")
        self.assertEqual(normalize_error_kind(None), "provider")  # type: ignore[arg-type]


if __name__ == "__main__":
    raise SystemExit(unittest.main())
