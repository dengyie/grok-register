#!/usr/bin/env python3
"""Offline checks for the PKCE authorization-code mint path.

Covers the pure helper functions (no network, no curl_cffi, no real SSO):
  - grpcweb varint/string/frame encode→decode roundtrip
  - PKCE code_verifier / code_challenge S256 shape
  - authorization URL params (response_type=code, S256, cli-proxy-api referrer)
  - submitOAuth2Consent action_id extraction from a next.js HTML page
  - _code_from_url state mismatch failure + success
"""

from __future__ import annotations

import hashlib
import importlib.util
import base64
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    if "cpa_xai" not in sys.modules:
        pkg = type(sys)("cpa_xai")
        pkg.__path__ = [str(ROOT / "cpa_xai")]  # type: ignore[attr-defined]
        sys.modules["cpa_xai"] = pkg
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_grpcweb_roundtrip() -> None:
    gw = _load("cpa_xai.grpcweb", ROOT / "cpa_xai" / "grpcweb.py")

    # single string field, field number 1
    msg = gw.encode_string(1, "https://accounts.x.ai/set-cookie?token=abc")
    frame = gw.frame_request(msg)
    parsed = gw.parse_response(frame)
    assert parsed.get("grpc_status") in (None, 0), parsed
    msgs = parsed["messages"]
    assert msgs, "no messages decoded"
    # messages is list[list[dict]]; first sub-message's first field is the string
    first_msg = msgs[0]
    str_fields = [f for f in first_msg if f.get("type") == "string"]
    assert str_fields, first_msg
    text = str_fields[0].get("value")
    assert text and "set-cookie" in text, text
    print("PASS grpcweb_roundtrip")


def test_pkce_code_challenge_shape() -> None:
    pkce = _load("cpa_xai.pkce_mint", ROOT / "cpa_xai" / "pkce_mint.py")

    verifier = pkce._code_verifier()
    challenge = pkce._code_challenge(verifier)

    # verifier: 48 bytes -> 64 base64url chars, no padding
    assert len(verifier) == 64, len(verifier)
    assert "=" not in verifier
    # challenge == S256(verifier), b64url no padding
    expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    assert challenge == expected, (challenge, expected)
    assert "=" not in challenge
    # different verifiers yield different challenges
    other = pkce._code_challenge(pkce._code_verifier())
    assert other != challenge
    print("PASS pkce_code_challenge_shape")


def test_authorization_url_params() -> None:
    pkce = _load("cpa_xai.pkce_mint", ROOT / "cpa_xai" / "pkce_mint.py")

    url = pkce._build_authorization_url(
        client_id="CLIENT-X",
        redirect_uri="http://127.0.0.1:56121/callback",
        state="ST",
        nonce="NN",
        code_challenge="CH",
        scope="openid offline_access",
    )
    assert url.startswith("https://auth.x.ai/oauth2/authorize?"), url
    qs = parse_qs(urlparse(url).query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["CLIENT-X"]
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["code_challenge"] == ["CH"]
    assert qs["state"] == ["ST"]
    assert qs["nonce"] == ["NN"]
    assert qs["redirect_uri"] == ["http://127.0.0.1:56121/callback"]
    assert qs["scope"] == ["openid offline_access"]
    assert qs["referrer"] == ["cli-proxy-api"]
    print("PASS authorization_url_params")


def test_submit_consent_action_id_extraction() -> None:
    pkce = _load("cpa_xai.pkce_mint", ROOT / "cpa_xai" / "pkce_mint.py")
    assert hasattr(pkce, "_extract_action_id_from_html"), (
        "_extract_action_id_from_html must be a first-class helper "
        "(inline-only extraction silently falls back to stale hardcoded action → live 404)"
    )
    extract = pkce._extract_action_id_from_html

    # 1. explicit submitOAuth2Consent action wins (even when other actions present)
    html_named = (
        '<script>createServerReference)("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        ' "otherAction");'
        'createServerReference)("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        ' "submitOAuth2Consent");</script>'
    )
    assert extract(html_named) == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

    # 2. name before id (alternate bundle order)
    html_name_first = (
        'submitOAuth2Consent",{id:"cccccccccccccccccccccccccccccccccccccccccc"}'
        'createServerReference)("dddddddddddddddddddddddddddddddddddddddddd"'
    )
    # prefer explicit next-action / action id near submitOAuth2Consent
    aid2 = extract(html_name_first)
    assert aid2 in {
        "cccccccccccccccccccccccccccccccccccccccccc",
        "dddddddddddddddddddddddddddddddddddddddddd",
    }, aid2

    # 3. empty / SPA shell HTML → None (must NOT silently return stale hardcoded id)
    assert extract("") is None
    assert extract("<html><body>loading</body></html>") is None
    assert extract(None) is None  # type: ignore[arg-type]

    # 4. single anonymous createServerReference → that id (last-resort page scrape)
    html_one = 'createServerReference)("eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", void 0)'
    assert extract(html_one) == "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    # 5. flight / quoted next-action style
    html_flight = (
        r'{"id":"ffffffffffffffffffffffffffffffffffffffffff",'
        r'"bound":null},"submitOAuth2Consent"'
    )
    assert extract(html_flight) == "ffffffffffffffffffffffffffffffffffffffffff"

    # 6. constant still present as last-resort POST fallback only
    assert len(pkce.SUBMIT_OAUTH2_CONSENT_ACTION) >= 40
    print("PASS submit_consent_action_id_extraction")


def test_code_from_url_state_mismatch_and_success() -> None:
    pkce = _load("cpa_xai.pkce_mint", ROOT / "cpa_xai" / "pkce_mint.py")

    # success
    code = pkce._code_from_url(
        "http://127.0.0.1:56121/callback?code=AC-123&state=ST", "ST"
    )
    assert code == "AC-123", code

    # state mismatch -> PKCEMintError
    raised = False
    try:
        pkce._code_from_url(
            "http://127.0.0.1:56121/callback?code=AC-123&state=OTHER", "ST"
        )
    except pkce.PKCEMintError as e:
        raised = "state mismatch" in str(e).lower()
        assert e.code == "state_mismatch"
        assert e.retryable is True
    assert raised, "state mismatch should raise PKCEMintError"

    # missing code -> PKCEMintError
    raised = False
    try:
        pkce._code_from_url("http://127.0.0.1:56121/callback?state=ST", "ST")
    except pkce.PKCEMintError as e:
        raised = "missing code" in str(e).lower()
        assert e.code == "missing_code"
        assert e.retryable is True
    assert raised, "missing code should raise PKCEMintError"
    print("PASS code_from_url_state_mismatch_and_success")


def test_pkce_mint_error_structured_fields() -> None:
    pkce = _load("cpa_xai.pkce_mint", ROOT / "cpa_xai" / "pkce_mint.py")
    e = pkce.PKCEMintError(
        "consent HTML missing submitOAuth2Consent action id",
        code="consent_action_missing",
        retryable=False,
    )
    assert e.code == "consent_action_missing"
    assert e.retryable is False
    assert "consent" in str(e).lower()
    # defaults
    e2 = pkce.PKCEMintError("generic")
    assert e2.code == "pkce_error"
    assert e2.retryable is True
    print("PASS pkce_mint_error_structured_fields")


if __name__ == "__main__":
    test_grpcweb_roundtrip()
    test_pkce_code_challenge_shape()
    test_authorization_url_params()
    test_submit_consent_action_id_extraction()
    test_code_from_url_state_mismatch_and_success()
    test_pkce_mint_error_structured_fields()
    print("\nALL PKCE UNIT TESTS PASSED")
