"""Pure-HTTP SSO cookie -> OAuth PKCE tokens for Grok Build/CLI access."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

from . import grpcweb
from .oauth_device import CLIENT_ID, ISSUER, SCOPE
from .proxyutil import resolve_proxy, set_runtime_proxy

AUTHORIZATION_ENDPOINT = f"{ISSUER}/oauth2/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/oauth2/token"
ACCOUNTS_ORIGIN = "https://accounts.x.ai"
CREATE_COOKIE_SETTER_RPC = f"{ACCOUNTS_ORIGIN}/auth_mgmt.AuthManagement/CreateCookieSetterLink"
SUBMIT_OAUTH2_CONSENT_ACTION = "4005315a1d7e426de592990bb54bb37471f39dd6d2"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:56121/callback"

LogFn = Callable[[str], None]


class PKCEMintError(RuntimeError):
    """PKCE protocol path failed; caller may fall back to other mint methods.

    Structured fields (preferred over message substring matching):
      - ``code``: stable machine token (e.g. consent_action_missing, cancelled)
      - ``retryable``: False when remint/spin will not heal (empty SPA / missing
        action id / cancelled / missing dependency). Callers still may residual
        to device/browser per product flags; retryable only classifies the PKCE
        failure itself.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "pkce_error",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "pkce_error").strip() or "pkce_error"
        self.retryable = bool(retryable)


def _noop_log(_: str) -> None:
    return None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _code_verifier() -> str:
    return _b64url(secrets.token_bytes(48))


def _code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _session(proxy: str | None):
    try:
        from curl_cffi import requests as cf_requests
    except ImportError as e:
        raise PKCEMintError(
            "curl_cffi not installed; cannot run PKCE mint",
            code="dependency",
            retryable=False,
        ) from e

    kwargs: dict[str, Any] = {"impersonate": "chrome131"}
    resolved = resolve_proxy(proxy)
    if resolved:
        kwargs["proxies"] = {"http": resolved, "https": resolved}
    return cf_requests.Session(**kwargs)


def _set_sso_cookie(session: Any, sso_cookie: str) -> None:
    sso_cookie = (sso_cookie or "").strip()
    if not sso_cookie:
        raise PKCEMintError("empty sso cookie", code="empty_sso", retryable=False)
    for domain in ("accounts.x.ai", ".accounts.x.ai", ".x.ai", "auth.x.ai"):
        try:
            session.cookies.set("sso", sso_cookie, domain=domain, path="/")
        except Exception:
            pass
        try:
            session.cookies.set("sso-rw", sso_cookie, domain=domain, path="/")
        except Exception:
            pass


def _grpc_headers(referer: str) -> dict[str, str]:
    return {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "accept": "*/*",
        "origin": ACCOUNTS_ORIGIN,
        "referer": referer,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _extract_urls_from_fields(fields: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for field in fields:
        if field.get("type") == "string":
            value = str(field.get("value") or "")
            if value.startswith(("http://", "https://")):
                urls.append(value)
        elif field.get("type") == "bytes" and field.get("hex"):
            try:
                urls.extend(_extract_urls_from_fields(grpcweb.decode_message(bytes.fromhex(field["hex"]))))
            except Exception:
                pass
    return urls


def _parse_grpc_error(headers: dict[str, str], body: bytes) -> tuple[int | None, str]:
    status = headers.get("grpc-status")
    message = unquote(headers.get("grpc-message") or "")
    if status is not None:
        try:
            return int(status), message
        except ValueError:
            return None, message
    try:
        parsed = grpcweb.parse_response(body)
    except Exception:
        return None, message
    if parsed.get("grpc_status") is not None:
        return int(parsed["grpc_status"]), message or str(parsed.get("trailers") or "")
    return None, message


def _build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scope: str,
) -> str:
    params = {
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
        "plan": "generic",
        "redirect_uri": redirect_uri,
        "referrer": "cli-proxy-api",
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return AUTHORIZATION_ENDPOINT + "?" + urlencode(params)


def _code_from_url(url: str, state: str) -> str:
    qs = parse_qs(urlparse(url).query)
    if (qs.get("state") or [""])[0] != state:
        raise PKCEMintError(
            "authorization failed: state mismatch",
            code="state_mismatch",
            retryable=True,
        )
    code = (qs.get("code") or [""])[0]
    if not code:
        raise PKCEMintError(
            f"authorization failed: missing code in {url[:200]}",
            code="missing_code",
            retryable=True,
        )
    return code


def _extract_action_id_from_html(page_html: str | None) -> str | None:
    """Parse Next.js server-action id for submitOAuth2Consent from consent HTML/RSC.

    Returns None for empty/SPA shells so callers do not silently POST a stale
    hardcoded action id (live symptom: HTTP 404 "Server action not found").
    """
    if not page_html or not isinstance(page_html, str):
        return None
    text = page_html
    if len(text) < 40 and "createServerReference" not in text and "submitOAuth2Consent" not in text:
        return None

    # 1) createServerReference)("id", ... "submitOAuth2Consent"
    m = re.search(
        r'createServerReference\)\("([a-f0-9]{40,44})"[^)]*submitOAuth2Consent',
        text,
    )
    if m:
        return m.group(1)

    # 2) name-before-id / flight payload near submitOAuth2Consent
    #    e.g. submitOAuth2Consent",{id:"…"}  or  {"id":"…","bound":null},"submitOAuth2Consent"
    near = re.search(
        r'submitOAuth2Consent["\']?\s*[,}][\s\S]{0,200}?"([a-f0-9]{40,44})"'
        r'|'
        r'"([a-f0-9]{40,44})"[\s\S]{0,200}?submitOAuth2Consent'
        r'|'
        r'submitOAuth2Consent["\']?\s*,\s*\{[^}]*?id["\']?\s*:\s*["\']([a-f0-9]{40,44})',
        text,
    )
    if near:
        for g in near.groups():
            if g:
                return g

    # 3) next-action header value embedded in HTML
    m = re.search(
        r'(?:next-action|nextAction)["\']?\s*[:=]\s*["\']([a-f0-9]{40,44})',
        text,
        re.I,
    )
    if m:
        return m.group(1)

    # 4) last-resort: single anonymous createServerReference id on page
    refs = re.findall(r'createServerReference\)\("([a-f0-9]{40,44})"', text)
    if len(refs) == 1:
        return refs[0]
    return None


def _create_cookie_setter_link(session: Any, success_url: str) -> str:
    msg = grpcweb.encode_string(1, success_url) + grpcweb.encode_string(2, f"{ACCOUNTS_ORIGIN}/sign-in")
    resp = session.post(
        CREATE_COOKIE_SETTER_RPC,
        headers=_grpc_headers(f"{ACCOUNTS_ORIGIN}/sign-in?redirect=oauth2-provider"),
        data=grpcweb.frame_request(msg),
        timeout=45,
    )
    hdrs = {k.lower(): v for k, v in resp.headers.items()}
    header_status, header_msg = _parse_grpc_error(hdrs, resp.content)
    try:
        parsed = grpcweb.parse_response(resp.content)
    except Exception:
        parsed = {"messages": [], "trailers": {}, "grpc_status": None}
    grpc_status = parsed.get("grpc_status")
    if grpc_status is None:
        grpc_status = header_status
    grpc_msg = header_msg or unquote(str((parsed.get("trailers") or {}).get("grpc-message") or ""))
    fields = parsed["messages"][0] if parsed.get("messages") else []
    urls = _extract_urls_from_fields(fields)
    cookie_setter = next((u for u in urls if "set-cookie" in u), None) or (urls[0] if urls else "")
    if grpc_status not in (None, 0) or not cookie_setter:
        raise PKCEMintError(
            grpc_msg or "CreateCookieSetterLink failed",
            code="cookie_setter",
            retryable=True,
        )
    return cookie_setter


def _submit_consent(
    session: Any,
    *,
    page_url: str,
    page_html: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    nonce: str,
) -> str:
    extracted = _extract_action_id_from_html(page_html)
    used_hardcoded = extracted is None
    action_id = extracted or SUBMIT_OAUTH2_CONSENT_ACTION

    router_tree = (
        '["",{"children":["(app)",{"children":["(auth)",{"children":["oauth2",'
        '{"children":["consent",{"children":["__PAGE__",{}]}]}]}]}]},'
        '"$undefined","$undefined",16]'
    )
    payload = [
        {
            "action": "allow",
            "clientId": client_id,
            "redirectUri": redirect_uri,
            "scope": scope,
            "state": state,
            "codeChallenge": code_challenge,
            "codeChallengeMethod": "S256",
            "nonce": nonce,
            "principalType": "User",
            "principalId": "",
            "referrer": "",
        }
    ]

    def _post(url: str, aid: str):
        headers = {
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "next-action": aid,
            "next-router-state-tree": quote(router_tree, safe=""),
            "origin": ACCOUNTS_ORIGIN,
            "referer": page_url,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return session.post(url, headers=headers, data=body, timeout=45)

    def _code_from_resp(resp: Any) -> str | None:
        text = resp.text or ""
        match = re.search(r'"code"\s*:\s*"([^"]+)"', text)
        if match:
            return match.group(1)
        match = re.search(r"code=([A-Za-z0-9._~\-]+)", text)
        if match and "error" not in match.group(0):
            return match.group(1)
        loc = resp.headers.get("location") or resp.headers.get("Location") or ""
        if "code=" in loc:
            return _code_from_url(urljoin(page_url, loc), state)
        return None

    post_url = page_url.split("?")[0] if "consent" in page_url else page_url
    resp = _post(post_url, action_id)
    text = resp.text or ""
    code = _code_from_resp(resp)
    if code:
        return code

    # Retry full page_url if strip-query POST failed
    if resp.status_code >= 400 or ("error" in text[:200].lower() and "code" not in text):
        resp = _post(page_url, action_id)
        text = resp.text or ""
        code = _code_from_resp(resp)
        if code:
            return code

    # 404 "Server action not found": re-fetch consent HTML, re-extract action id, retry once.
    # Root cause of smoke: empty/JS shell → stale hardcoded action id.
    not_found = resp.status_code == 404 or "server action not found" in text.lower()
    if not_found:
        try:
            refresh = session.get(page_url, allow_redirects=True, timeout=45)
            refreshed_html = refresh.text or ""
            refresh_url = str(getattr(refresh, "url", None) or page_url)
            new_aid = _extract_action_id_from_html(refreshed_html)
            if new_aid and new_aid != action_id:
                action_id = new_aid
                used_hardcoded = False
                page_url = refresh_url
                post_url = page_url.split("?")[0] if "consent" in page_url else page_url
                resp = _post(post_url, action_id)
                text = resp.text or ""
                code = _code_from_resp(resp)
                if code:
                    return code
                if resp.status_code >= 400:
                    resp = _post(page_url, action_id)
                    text = resp.text or ""
                    code = _code_from_resp(resp)
                    if code:
                        return code
        except Exception:
            pass

        if used_hardcoded:
            raise PKCEMintError(
                "submitOAuth2Consent failed HTTP 404: Server action not found "
                "(consent HTML missing submitOAuth2Consent action id; "
                "stale hardcoded fallback rejected by Next.js)",
                code="consent_action_missing",
                retryable=False,
            )
        raise PKCEMintError(
            f"submitOAuth2Consent failed HTTP {resp.status_code}: {text[:300]} "
            f"(action_id={action_id[:12]}… extracted from page)",
            code="consent_action_rejected",
            retryable=False,
        )

    raise PKCEMintError(
        f"submitOAuth2Consent failed HTTP {resp.status_code}: {text[:300]}",
        code="consent_submit",
        retryable=True,
    )


def _exchange_code_for_token(
    session: Any,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
) -> dict[str, Any]:
    resp = session.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=45,
    )
    if resp.status_code != 200:
        raise PKCEMintError(
            f"token exchange failed HTTP {resp.status_code}: {resp.text[:300]}",
            code="token_exchange",
            retryable=True,
        )
    token = resp.json()
    if "expires_in" in token and "expires_at" not in token:
        try:
            token["expires_at"] = int(time.time()) + int(token["expires_in"])
        except Exception:
            pass
    if not token.get("access_token") or not token.get("refresh_token"):
        raise PKCEMintError(
            "token exchange response missing access_token/refresh_token",
            code="token_exchange",
            retryable=True,
        )
    return token


def mint_with_sso_pkce(
    *,
    sso_cookie: str,
    email: str = "",
    proxy: str | None = None,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
) -> dict[str, Any]:
    """Mint authorization-code OAuth tokens from an existing accounts.x.ai SSO cookie."""
    log = log or _noop_log
    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    session = _session(resolved or None)
    _set_sso_cookie(session, sso_cookie)

    state = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    verifier = _code_verifier()
    challenge = _code_challenge(verifier)
    auth_url = _build_authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
        scope=scope,
    )
    consent_url = (
        f"{ACCOUNTS_ORIGIN}/oauth2/consent?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "nonce": nonce,
            }
        )
    )

    if cancel and cancel():
        raise PKCEMintError("cancelled", code="cancelled", retryable=False)
    session.get(auth_url, allow_redirects=False, timeout=timeout)
    setter = _create_cookie_setter_link(session, consent_url)
    log("pkce cookie-setter link ok")

    current = setter
    for _ in range(6):
        if cancel and cancel():
            raise PKCEMintError("cancelled", code="cancelled", retryable=False)
        if "code=" in current and (current.startswith(redirect_uri) or "127.0.0.1" in current):
            code = _code_from_url(current, state)
            break
        if "set-cookie" not in current:
            break
        resp = session.get(current, allow_redirects=False, timeout=timeout)
        loc = resp.headers.get("location") or resp.headers.get("Location") or ""
        log(f"pkce set-cookie HTTP {resp.status_code}")
        if not loc:
            break
        current = urljoin(current, loc)
    else:
        code = ""

    if "code" not in locals():
        if "consent" not in current:
            raise PKCEMintError(
                f"cookie-setter did not reach consent/code: {current[:180]}",
                code="cookie_setter_path",
                retryable=True,
            )
        page = session.get(current, allow_redirects=False, timeout=timeout)
        loc = page.headers.get("location") or page.headers.get("Location") or ""
        if loc and "code=" in loc:
            code = _code_from_url(urljoin(current, loc), state)
        else:
            page_html = page.text or ""
            page_url = current
            # Follow non-code redirect, or re-fetch with redirects when action id missing
            # (empty SPA shell after set-cookie 303 is the smoke-era 404 root cause).
            if loc and "code=" not in loc:
                page_url = urljoin(current, loc)
                page = session.get(page_url, allow_redirects=True, timeout=timeout)
                page_html = page.text or ""
                page_url = str(getattr(page, "url", None) or page_url)
                loc2 = page.headers.get("location") or page.headers.get("Location") or ""
                if loc2 and "code=" in loc2:
                    code = _code_from_url(urljoin(page_url, loc2), state)
            if "code" not in locals() or not locals().get("code"):
                if not _extract_action_id_from_html(page_html):
                    page = session.get(page_url, allow_redirects=True, timeout=timeout)
                    page_html = page.text or ""
                    page_url = str(getattr(page, "url", None) or page_url)
                    loc3 = page.headers.get("location") or page.headers.get("Location") or ""
                    if loc3 and "code=" in loc3:
                        code = _code_from_url(urljoin(page_url, loc3), state)
            if "code" not in locals() or not locals().get("code"):
                code = _submit_consent(
                    session,
                    page_url=page_url,
                    page_html=page_html,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    scope=scope,
                    state=state,
                    code_challenge=challenge,
                    nonce=nonce,
                )
    log(f"pkce authorization code ok{f' email={email}' if email else ''}")

    token = _exchange_code_for_token(
        session,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
    )
    return {
        "access_token": str(token["access_token"]).strip(),
        "refresh_token": str(token["refresh_token"]).strip(),
        "id_token": str(token.get("id_token") or "").strip() or None,
        "token_type": str(token.get("token_type") or "Bearer"),
        "expires_in": int(token.get("expires_in") or 21600),
        "mint_method": "pkce",
    }
