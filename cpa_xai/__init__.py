"""CPA xAI (Grok Build free) auth helpers for the register machine.

Produce CLIProxyAPI-compatible ``xai-<email>.json`` credentials.
"""

from .accounts import (
    AccountLine,
    email_in_existing,
    email_match_keys,
    existing_cpa_emails,
    format_account_line,
    normalize_sso_cookie,
    parse_accounts_file,
)
from .mint import mint_and_export
from .probe import classify_chat_probe, probe_mini_response, probe_models
from .protocol_mint import (
    ProtocolMintError,
    extract_sso_from_cookies,
    mint_with_sso_protocol,
)
from .schema import (
    CLIENT_ID,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_HEADERS,
    DEFAULT_REDIRECT_URI,
    DEFAULT_TOKEN_ENDPOINT,
    build_cpa_xai_auth,
    credential_file_name,
    expired_from_access_token,
)
from .writer import (
    is_chat_retryable_auth,
    load_entitlement_denied_emails,
    patch_cpa_xai_auth,
    record_entitlement_denied,
    write_cpa_xai_auth,
)

# CLIENT_ID lives in oauth_device; re-export from schema if present
try:
    from .oauth_device import CLIENT_ID as OAUTH_CLIENT_ID
except Exception:  # pragma: no cover
    OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"

__all__ = [
    "AccountLine",
    "CLIENT_ID",
    "DEFAULT_BASE_URL",
    "DEFAULT_CLIENT_HEADERS",
    "DEFAULT_REDIRECT_URI",
    "DEFAULT_TOKEN_ENDPOINT",
    "OAUTH_CLIENT_ID",
    "ProtocolMintError",
    "build_cpa_xai_auth",
    "classify_chat_probe",
    "credential_file_name",
    "email_in_existing",
    "email_match_keys",
    "existing_cpa_emails",
    "expired_from_access_token",
    "extract_sso_from_cookies",
    "format_account_line",
    "is_chat_retryable_auth",
    "load_entitlement_denied_emails",
    "mint_and_export",
    "mint_with_sso_protocol",
    "normalize_sso_cookie",
    "parse_accounts_file",
    "patch_cpa_xai_auth",
    "probe_mini_response",
    "probe_models",
    "record_entitlement_denied",
    "write_cpa_xai_auth",
]
