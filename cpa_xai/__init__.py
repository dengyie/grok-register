"""CPA xAI (Grok Build free) auth helpers for the register machine.

Produce CLIProxyAPI-compatible ``xai-<email>.json`` credentials.
"""

from .accounts import AccountLine, existing_cpa_emails, parse_accounts_file
from .mint import mint_and_export
from .probe import probe_mini_response, probe_models
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
from .writer import write_cpa_xai_auth

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
    "credential_file_name",
    "existing_cpa_emails",
    "expired_from_access_token",
    "extract_sso_from_cookies",
    "mint_and_export",
    "mint_with_sso_protocol",
    "parse_accounts_file",
    "probe_mini_response",
    "probe_models",
    "write_cpa_xai_auth",
]
