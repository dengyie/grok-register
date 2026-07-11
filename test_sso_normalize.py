#!/usr/bin/env python3
"""Offline checks for SSO cookie normalization and email identity keys."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_normalize_sso_cookie() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from cpa_xai.accounts import normalize_sso_cookie

    jwt = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.abc.def"
    assert normalize_sso_cookie(jwt) == jwt
    assert normalize_sso_cookie("-" + jwt) == jwt
    assert normalize_sso_cookie("  -" + jwt + "  ") == jwt
    assert normalize_sso_cookie("--" + jwt) == jwt
    assert normalize_sso_cookie("") == ""
    assert normalize_sso_cookie(None) == ""
    # do not strip arbitrary leading dash without eyJ nearby
    assert normalize_sso_cookie("-session-id-value") == "-session-id-value"
    print("PASS normalize_sso_cookie")


def test_format_account_line() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from cpa_xai.accounts import format_account_line

    jwt = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.abc.def"
    line = format_account_line("a@x.com", "pw", "-" + jwt)
    assert line == f"a@x.com----pw----{jwt}\n"
    print("PASS format_account_line")


def test_parse_accounts_strips_leading_dash() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from cpa_xai.accounts import parse_accounts_file

    jwt = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.abc.def"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "accounts_cli.txt"
        # Explicit: third field starts with '-' before JWT (5 dashes between pw and jwt)
        p.write_text(f"a@x.com----pw-----{jwt}\n", encoding="utf-8")
        rows = parse_accounts_file(p)
        assert len(rows) == 1
        assert rows[0].sso == jwt
    print("PASS parse_accounts_strips_leading_dash")


def test_extract_sso_from_cookies_normalizes() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from cpa_xai.protocol_mint import extract_sso_from_cookies

    jwt = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.abc.def"
    assert extract_sso_from_cookies([{"name": "sso", "value": "-" + jwt}]) == jwt
    assert extract_sso_from_cookies({"sso": "-" + jwt}) == jwt
    print("PASS extract_sso_from_cookies_normalizes")


def test_email_match_keys_plus_alias() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from cpa_xai.accounts import email_in_existing, email_match_keys, existing_cpa_emails

    email = "user+abc@hotmail.com"
    keys = email_match_keys(email)
    assert "user+abc@hotmail.com" in keys
    assert "user-abc@hotmail.com" in keys

    with tempfile.TemporaryDirectory() as td:
        auth = Path(td)
        # filename-only (no JSON email field) — historical/partial files
        (auth / "xai-user-abc@hotmail.com.json").write_text(
            json.dumps({"type": "xai", "access_token": "x"}),
            encoding="utf-8",
        )
        have = existing_cpa_emails(auth)
        assert email_in_existing(email, have)
        assert not email_in_existing("other+zz@hotmail.com", have)
    print("PASS email_match_keys_plus_alias")


def test_config_bool_strings() -> None:
    import sys

    sys.path.insert(0, str(ROOT))
    from cpa_export import _config_bool

    assert _config_bool("false", default=True) is False
    assert _config_bool("0", default=True) is False
    assert _config_bool("true", default=False) is True
    assert _config_bool(None, default=True) is True
    assert _config_bool(True) is True
    print("PASS config_bool_strings")


def main() -> int:
    test_normalize_sso_cookie()
    test_format_account_line()
    test_parse_accounts_strips_leading_dash()
    test_extract_sso_from_cookies_normalizes()
    test_email_match_keys_plus_alias()
    test_config_bool_strings()
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
