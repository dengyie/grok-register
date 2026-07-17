"""ChatGPT / OpenAI platform account registration protocol flow.

High-success path inspired by open-reg-auto + zhuce6:
  authorize (PKCE) → register user → email OTP → create_account → oauth/token

In-process; OTP via register_core EmailSource. No CPA/production inject.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import secrets
import string
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .constants import (
    AUTH_BASE,
    COMMON_HEADERS,
    NAVIGATE_HEADERS,
    PLATFORM_OAUTH_AUDIENCE,
    PLATFORM_OAUTH_CLIENT_ID,
    PLATFORM_OAUTH_REDIRECT_URI,
)
from .sentinel import build_sentinel_token
from .session import (
    cookie_get,
    create_session,
    quote_param,
    request_with_retry,
    response_json,
)

LogFn = Callable[[str], None]


class ChatGPTRegisterError(RuntimeError):
    """Single-attempt registration failure (caller maps to RegisterResult)."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "provider",
        step: str = "",
        steps: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.step = step or ""
        # Partial protocol ledger when raised mid-flow (adapter may surface as artifacts).
        self.steps: dict[str, Any] = dict(steps or {})


@dataclass
class RegistrationResult:
    ok: bool
    email: str = ""
    password: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    callback_url: str = ""
    error: str = ""
    error_kind: str = ""
    fail_step: str = ""
    steps: dict[str, Any] = field(default_factory=dict)
    device_id: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "email": self.email,
            "password": _preview(self.password),
            "access_token": _preview(self.access_token),
            "refresh_token": _preview(self.refresh_token),
            "id_token": _preview(self.id_token),
            "error": self.error,
            "error_kind": self.error_kind,
            "fail_step": self.fail_step,
            "device_id": self.device_id,
            "step_keys": sorted(self.steps.keys()),
        }


def _sentinel_soft_enabled() -> bool:
    """Default hard-fail on sentinel PoW. Opt-in soft: CHATGPT_SENTINEL_SOFT=1."""
    flag = str(os.environ.get("CHATGPT_SENTINEL_SOFT", "0") or "0").strip().lower()
    return flag in ("1", "true", "yes", "on", "soft")


def _is_transport_exception(exc: BaseException) -> bool:
    """True when a sentinel/req failure is network/transport, NOT PoW/captcha.

    build_sentinel_token POSTs sentinel/req via the session; if the proxy is
    dead / TLS reset / connect timeout the resulting exception is transport,
    which must surface as kind=network (need-quarantine path) — NOT kind=captcha
    (which would retry-captcha an account whose PoW machinery is actually fine).
    """
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    name = type(exc).__name__
    if name in {
        "ProxyError",
        "ConnectError",
        "ConnectionError",
        "ReadTimeout",
        "ConnectTimeout",
        "Timeout",
        "SSLError",
        "RequestsError",
        "RequestException",
        "CurlError",
        "CffiError",
    }:
        return True
    # requests.exceptions.RequestException subclasses live under requests/urllib3.
    mod = (type(exc).__module__ or "").lower()
    if mod.startswith("requests.") or mod.startswith("urllib3") or mod.startswith("curl_cffi"):
        return True
    msg = str(exc or "").lower()
    return any(
        s in msg
        for s in (
            "proxy",
            "connection",
            "timed out",
            "timeout",
            "connectionreset",
            "remotedisconnected",
            "tls",
            "ssl",
            "max retries",
        )
    )


def _preview(value: str) -> str:
    s = value or ""
    if not s:
        return ""
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}…{s[-4:]}(len={len(s)})"


def _noop_log(_: str) -> None:
    return None


def _std_log(msg: str) -> None:
    print(f"[chatgpt] {msg}", flush=True)


def _human_pause(log: LogFn | None = None, *, label: str = "") -> float:
    """Sleep ~10s ±1s between protocol steps to mimic human form pacing.

    Env:
      CHATGPT_HUMAN_PACE=0|off|false → disable (tests / debug)
      CHATGPT_STEP_DELAY_S=10        → base seconds (default 10)
      CHATGPT_STEP_JITTER_S=1        → ±jitter seconds (default 1)
    """
    flag = str(os.environ.get("CHATGPT_HUMAN_PACE", "1") or "1").strip().lower()
    if flag in ("0", "off", "false", "no", "disabled"):
        return 0.0
    try:
        base = float(os.environ.get("CHATGPT_STEP_DELAY_S") or "10")
    except ValueError:
        base = 10.0
    try:
        jitter = float(os.environ.get("CHATGPT_STEP_JITTER_S") or "1")
    except ValueError:
        jitter = 1.0
    lo = max(0.0, base - max(0.0, jitter))
    hi = max(lo, base + max(0.0, jitter))
    delay = random.uniform(lo, hi)
    if log is not None:
        log(f"human_pace wait={delay:.2f}s step={label or 'next'}")
    if delay > 0:
        time.sleep(delay)
    return delay


def _generate_pkce() -> tuple[str, str]:
    code_verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    )
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return code_verifier, code_challenge


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    value = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(0, length - 4)))
    )
    random.shuffle(value)
    return "".join(value)


def _random_name() -> str:
    first = random.choice(
        ["James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"]
    )
    last = random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"]
    )
    return f"{first} {last}"


def _random_birthdate() -> str:
    # Prefer 22–40yo; some risk engines flag teen-range + temp mail.
    return (
        f"{random.randint(1986, 2002):04d}-"
        f"{random.randint(1, 12):02d}-"
        f"{random.randint(1, 28):02d}"
    )


def _make_trace_headers() -> dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(int(parent_id), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _extract_oauth_code(url: str) -> dict[str, str] | None:
    if not url:
        return None
    try:
        params = parse_qs(urlparse(url).query)
    except Exception:
        return None
    code = str((params.get("code") or [""])[0]).strip()
    if not code:
        return None
    return {
        "code": code,
        "state": str((params.get("state") or [""])[0]).strip(),
        "scope": str((params.get("scope") or [""])[0]).strip(),
    }


class PlatformRegistrar:
    def __init__(self, proxy: str = "", log: LogFn | None = None) -> None:
        self.session = create_session(proxy)
        self.device_id = str(uuid.uuid4())
        self.proxy = proxy or ""
        self.log = log or _noop_log
        self.code_verifier = ""
        self.state = ""
        self.sentinel_tokens: dict[str, str] = {}
        self.last_authorize: dict[str, Any] = {}

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def _ensure_sentinel(self, flow: str) -> str:
        cached = self.sentinel_tokens.get(flow)
        if cached:
            return cached
        token = build_sentinel_token(self.session, self.device_id, flow)
        self.sentinel_tokens[flow] = token
        return token

    def _accounts_headers(self, referer: str, flow: str) -> dict[str, str]:
        headers = dict(COMMON_HEADERS)
        headers["referer"] = referer
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        try:
            token = self._ensure_sentinel(flow)
            headers["openai-sentinel-token"] = token
            headers["OpenAI-Sentinel-Token"] = token
        except Exception as exc:
            if _sentinel_soft_enabled():
                headers["x-openai-sentinel-error"] = str(exc)[:200]
                self.log(f"sentinel soft-fail flow={flow}: {exc}")
            elif _is_transport_exception(exc):
                # sentinel/req transport death (dead proxy/TLS reset/timeout) is a
                # network failure, not a captcha/PoW failure — wrong recovery path.
                raise ChatGPTRegisterError(
                    f"sentinel_transport:{flow}:{exc}",
                    kind="network",
                    step=f"sentinel:{flow}",
                ) from exc
            else:
                # Hard gate: genuine PoW failure is captcha class, not silent continue.
                raise ChatGPTRegisterError(
                    f"sentinel_fail:{flow}:{exc}",
                    kind="captcha",
                    step=f"sentinel:{flow}",
                ) from exc
        return headers

    def start_authorize(self, email: str) -> dict[str, str]:
        try:
            self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
            self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")
        except Exception:
            pass
        code_verifier, code_challenge = _generate_pkce()
        self.code_verifier = code_verifier
        state = secrets.token_urlsafe(32)
        self.state = state
        nonce = secrets.token_urlsafe(32)
        params = {
            "issuer": AUTH_BASE,
            "client_id": PLATFORM_OAUTH_CLIENT_ID,
            "audience": PLATFORM_OAUTH_AUDIENCE,
            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            "device_id": self.device_id,
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": email,
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "auth0Client": "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9",
        }
        url = f"{AUTH_BASE}/api/accounts/authorize?" + "&".join(
            f"{k}={quote_param(str(v))}" for k, v in params.items()
        )
        resp, error = request_with_retry(
            self.session,
            "get",
            url,
            headers=NAVIGATE_HEADERS,
            allow_redirects=True,
        )
        if resp is None:
            raise ChatGPTRegisterError(
                error or "authorize_request_failed",
                kind="network",
                step="authorize",
            )
        status = int(getattr(resp, "status_code", 0) or 0)
        final_url = str(getattr(resp, "url", "") or "")
        self.last_authorize = {
            "email": email,
            "state": state,
            "code_verifier": code_verifier,
            "final_url": final_url,
            "status": status,
        }
        self.log(f"authorize status={status} final_url={final_url[:120]}")
        # OAuth authorize must land on a usable page; non-2xx is not a soft continue.
        if not (200 <= status < 300):
            kind = "network" if status >= 500 else "provider"
            raise ChatGPTRegisterError(
                f"authorize_http_{status}:{error or final_url[:120]}",
                kind=kind,
                step="authorize",
            )
        return {
            "code_verifier": code_verifier,
            "final_url": final_url,
            "status": str(status),
        }

    def establish_session(self) -> dict[str, Any]:
        state = self.state
        headers = dict(NAVIGATE_HEADERS)
        headers["oai-device-id"] = self.device_id
        headers.update(_make_trace_headers())
        try:
            headers["OpenAI-Sentinel-Token"] = self._ensure_sentinel("signup")
        except Exception as exc:
            if _sentinel_soft_enabled():
                headers["x-openai-sentinel-error"] = str(exc)[:200]
                self.log(f"sentinel soft-fail flow=signup: {exc}")
            elif _is_transport_exception(exc):
                raise ChatGPTRegisterError(
                    f"sentinel_transport:signup:{exc}",
                    kind="network",
                    step="session",
                ) from exc
            else:
                raise ChatGPTRegisterError(
                    f"sentinel_fail:signup:{exc}",
                    kind="captcha",
                    step="session",
                ) from exc
        candidates = [
            self.last_authorize.get("final_url") or f"{AUTH_BASE}/u/signup",
            f"{AUTH_BASE}/u/signup",
            f"{AUTH_BASE}/u/signup?state={state}" if state else f"{AUTH_BASE}/u/signup",
            f"{AUTH_BASE}/u/signup/identifier?state={state}"
            if state
            else f"{AUTH_BASE}/u/signup/identifier",
            f"{AUTH_BASE}/api/auth/session",
            f"{AUTH_BASE}/api/client_auth_session_dump",
        ]
        saw_any_response = False
        last_transport_err = ""
        for url in candidates:
            resp, error = request_with_retry(
                self.session,
                "get",
                str(url),
                headers=headers,
                allow_redirects=True,
            )
            if resp is not None:
                saw_any_response = True
            elif error:
                last_transport_err = str(error)[:200]
        has_session = bool(
            cookie_get(self.session, "oai-client-auth-session")
            or cookie_get(self.session, "auth_session")
            or cookie_get(self.session, "oai-auth-token")
        )
        self.log(f"session establish ok={has_session}")
        if not has_session:
            # All candidate GETs transport-failed → network (quarantine-eligible).
            # At least one HTTP response but no cookie → product session gate.
            if not saw_any_response:
                raise ChatGPTRegisterError(
                    f"session_establish_transport:{last_transport_err or 'empty'}",
                    kind="network",
                    step="session",
                )
            raise ChatGPTRegisterError(
                "session_cookie_missing",
                kind="session",
                step="session",
            )
        return {"ok": True}

    def register_user(self, email: str, password: str) -> dict[str, Any]:
        headers = self._accounts_headers(
            f"{AUTH_BASE}/create-account/password",
            "username_password_create",
        )
        resp, error = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/user/register",
            json={"username": email, "password": password},
            headers=headers,
        )
        status = int(getattr(resp, "status_code", 0) or 0) if resp is not None else 0
        body = response_json(resp)
        ok = resp is not None and 200 <= status < 300
        self.log(f"register_user status={status} ok={ok}")
        if not ok:
            if resp is None or status >= 500:
                raise ChatGPTRegisterError(
                    f"register_user_http_{status}:{error or body}",
                    kind="network",
                    step="register_user",
                )
            detail = str(body.get("error") or body.get("detail") or error or "")[:200]
            detail_l = detail.lower()
            kind = "provider"
            # Identity collision: HTTP 409 or explicit already-registered phrasing.
            # Avoid bare "exists"/"already" alone — those appear in unrelated API text.
            if status == 409 or any(
                needle in detail_l
                for needle in (
                    "already registered",
                    "already exists",
                    "user already",
                    "email already",
                    "account already",
                    "username already",
                    "already_registered",
                )
            ):
                kind = "already_registered"
            raise ChatGPTRegisterError(
                f"register_user_http_{status}:{detail}",
                kind=kind,
                step="register_user",
            )
        return {"ok": True, "status": status, "json": body}

    def send_otp(self) -> dict[str, Any]:
        headers = dict(NAVIGATE_HEADERS)
        headers["referer"] = f"{AUTH_BASE}/create-account/password"
        headers["oai-device-id"] = self.device_id
        resp, error = request_with_retry(
            self.session,
            "get",
            f"{AUTH_BASE}/api/accounts/email-otp/send",
            headers=headers,
            allow_redirects=True,
        )
        status = int(getattr(resp, "status_code", 0) or 0) if resp is not None else 0
        ok = resp is not None and status in (200, 302)
        self.log(f"send_otp status={status} ok={ok}")
        if not ok:
            kind = "network" if (resp is None or status >= 500) else "provider"
            raise ChatGPTRegisterError(
                f"send_otp_http_{status}:{error}",
                kind=kind,
                step="send_otp",
            )
        return {"ok": True, "status": status, "json": response_json(resp)}

    def validate_otp(self, code: str) -> dict[str, Any]:
        headers = self._accounts_headers(
            f"{AUTH_BASE}/create-account/email-verification",
            "authorize_continue",
        )
        resp, error = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/email-otp/validate",
            json={"code": str(code).strip()},
            headers=headers,
        )
        status = int(getattr(resp, "status_code", 0) or 0) if resp is not None else 0
        body = response_json(resp)
        ok = resp is not None and 200 <= status < 300
        self.log(f"validate_otp status={status} ok={ok}")
        if not ok:
            # Transport / upstream 5xx is not a bad OTP code.
            if resp is None or status >= 500:
                kind = "network"
            else:
                # Body-aware: wrong/expired code vs captcha/rate-limit/provider noise.
                detail = ""
                try:
                    err_obj = (body or {}).get("error") if isinstance(body, dict) else None
                    if isinstance(err_obj, dict):
                        detail = str(
                            err_obj.get("message")
                            or err_obj.get("code")
                            or err_obj.get("type")
                            or err_obj
                        )
                    elif err_obj is not None:
                        detail = str(err_obj)
                    else:
                        detail = str(
                            (body or {}).get("detail")
                            or (body or {}).get("message")
                            or body
                            or error
                            or ""
                        )
                except Exception:
                    detail = str(error or body or "")
                detail_l = detail.lower()
                if any(
                    n in detail_l
                    for n in (
                        "captcha",
                        "sentinel",
                        "challenge",
                        "turnstile",
                        "cloudflare",
                        "cf-challenge",
                    )
                ):
                    kind = "captcha"
                elif any(
                    n in detail_l
                    for n in (
                        "rate limit",
                        "rate_limit",
                        "too many",
                        "throttl",
                        "try again later",
                    )
                ):
                    kind = "provider"
                elif any(
                    n in detail_l
                    for n in (
                        "invalid",
                        "incorrect",
                        "wrong code",
                        "wrong otp",
                        "expired",
                        "bad code",
                        "mismatch",
                        "verification code",
                        "one-time",
                        "otp",
                        "does not match",
                    )
                ) or status in (400, 422):
                    kind = "otp_invalid"
                else:
                    kind = "provider"
            raise ChatGPTRegisterError(
                f"validate_otp_http_{status}:{error or body}",
                kind=kind,
                step="validate_otp",
            )
        return {"ok": True, "status": status, "json": body}

    def create_account(self, name: str, birthdate: str) -> dict[str, Any]:
        # Fresh sentinel for create_account — cached token from earlier flows
        # correlates with registration_disallowed on some risk engines.
        self.sentinel_tokens.pop("oauth_create_account", None)
        # Visit about-you page first so cookies/referer look human.
        request_with_retry(
            self.session,
            "get",
            f"{AUTH_BASE}/about-you",
            headers={
                **NAVIGATE_HEADERS,
                "oai-device-id": self.device_id,
                "referer": f"{AUTH_BASE}/create-account/email-verification",
            },
            allow_redirects=True,
        )
        # Pause after page view before POST create_account (form fill).
        _human_pause(self.log, label="about_you_form")
        headers = self._accounts_headers(
            f"{AUTH_BASE}/about-you",
            "oauth_create_account",
        )
        resp, error = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/create_account",
            json={"name": name, "birthdate": birthdate},
            headers=headers,
            allow_redirects=False,
        )
        status = int(getattr(resp, "status_code", 0) or 0) if resp is not None else 0
        body = response_json(resp)
        raw_text = ""
        if resp is not None and not body:
            try:
                raw_text = str(getattr(resp, "text", "") or "")[:400]
            except Exception:
                raw_text = ""
        location = ""
        if resp is not None:
            try:
                location = str(getattr(resp, "headers", {}).get("Location") or "")
            except Exception:
                location = ""
        ok = resp is not None and status in (200, 302)
        self.log(f"create_account status={status} ok={ok} location={location[:80]}")
        if not ok:
            code = ""
            try:
                code = str(((body or {}).get("error") or {}).get("code") or "")
            except Exception:
                code = ""
            detail_blob = body or error or raw_text or ""
            # Transport exhaustion / 5xx before risk body → network (quarantine-eligible).
            if resp is None or (status >= 500 and not code):
                kind = "network"
            elif code == "registration_disallowed" or "registration_disallowed" in str(
                detail_blob
            ):
                # Risk engine block (IP / domain / device reputation) — not a protocol bug.
                kind = "registration_disallowed"
            else:
                kind = "provider"
            # Include truncated body for risk-engine diagnosis (no secrets here).
            detail = code or error or body or raw_text or "unknown"
            if isinstance(detail, dict):
                detail = json.dumps(detail, ensure_ascii=False)[:300]
            else:
                detail = str(detail)[:300]
            raise ChatGPTRegisterError(
                f"create_account_http_{status}:{detail}",
                kind=kind,
                step="create_account",
            )
        continue_url = str(
            body.get("continue_url") or location or body.get("url") or ""
        ).strip()
        return {
            "ok": True,
            "status": status,
            "json": body,
            "location": location,
            "continue_url": continue_url,
        }

    def _follow_consent_for_code(self, consent_url: str) -> dict[str, str] | None:
        if not consent_url:
            return None
        if consent_url.startswith("/"):
            consent_url = f"{AUTH_BASE}{consent_url}"
        current = consent_url
        saw_http = False
        last_transport_err = ""
        for _ in range(12):
            resp, err = request_with_retry(
                self.session,
                "get",
                current,
                headers=NAVIGATE_HEADERS,
                allow_redirects=False,
            )
            if resp is None:
                last_transport_err = str(err or "empty")[:200]
                # Pure transport death mid-consent → network (not oauth_callback).
                if not saw_http:
                    raise ChatGPTRegisterError(
                        f"consent_transport:{last_transport_err}",
                        kind="network",
                        step="oauth_callback",
                    )
                break
            saw_http = True
            found = _extract_oauth_code(str(getattr(resp, "url", "") or ""))
            if found:
                return found
            loc = str(getattr(resp, "headers", {}).get("Location") or "").strip()
            found = _extract_oauth_code(loc)
            if found:
                return found
            status = int(getattr(resp, "status_code", 0) or 0)
            if status not in (301, 302, 303, 307, 308) or not loc:
                # try allow_redirects once
                resp2, err2 = request_with_retry(
                    self.session,
                    "get",
                    current,
                    headers=NAVIGATE_HEADERS,
                    allow_redirects=True,
                )
                if resp2 is None:
                    last_transport_err = str(err2 or "empty")[:200]
                    break
                found = _extract_oauth_code(str(getattr(resp2, "url", "") or ""))
                if found:
                    return found
                for hist in getattr(resp2, "history", []) or []:
                    found = _extract_oauth_code(
                        str(getattr(hist, "headers", {}).get("Location") or "")
                    )
                    if found:
                        return found
                break
            current = f"{AUTH_BASE}{loc}" if loc.startswith("/") else loc

        # workspace select fallback from oai-client-auth-session cookie
        raw = cookie_get(self.session, "oai-client-auth-session")
        if not raw:
            if last_transport_err and not saw_http:
                raise ChatGPTRegisterError(
                    f"consent_transport:{last_transport_err}",
                    kind="network",
                    step="oauth_callback",
                )
            return None
        try:
            first_part = raw.split(".")[0]
            padding = 4 - len(first_part) % 4
            if padding != 4:
                first_part += "=" * padding
            payload = json.loads(base64.urlsafe_b64decode(first_part))
            workspace_id = payload["workspaces"][0]["id"]
        except Exception:
            return None
        headers = self._accounts_headers(consent_url, "authorize_continue")
        ws_resp, ws_err = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/workspace/select",
            json={"workspace_id": workspace_id},
            headers=headers,
            allow_redirects=False,
        )
        if ws_resp is None:
            raise ChatGPTRegisterError(
                f"consent_workspace_transport:{ws_err or 'empty'}",
                kind="network",
                step="oauth_callback",
            )
        found = _extract_oauth_code(
            str(getattr(ws_resp, "headers", {}).get("Location") or "")
        )
        if found:
            return found
        ws_data = response_json(ws_resp)
        orgs = ((ws_data.get("data") or {}).get("orgs") or []) if ws_data else []
        if not orgs:
            return None
        org_id = str((orgs[0] or {}).get("id") or "").strip()
        project_id = str(
            ((orgs[0] or {}).get("projects") or [{}])[0].get("id") or ""
        ).strip()
        if not org_id:
            return None
        body: dict[str, str] = {"org_id": org_id}
        if project_id:
            body["project_id"] = project_id
        org_headers = self._accounts_headers(
            str(ws_data.get("continue_url") or consent_url),
            "authorize_continue",
        )
        org_resp, org_err = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/organization/select",
            json=body,
            headers=org_headers,
            allow_redirects=False,
        )
        if org_resp is None:
            raise ChatGPTRegisterError(
                f"consent_org_transport:{org_err or 'empty'}",
                kind="network",
                step="oauth_callback",
            )
        return _extract_oauth_code(
            str(getattr(org_resp, "headers", {}).get("Location") or "")
        )

    def exchange_tokens(self, callback_url: str) -> RegistrationResult:
        consent_step: dict[str, Any] = {
            "callback_url_present": bool(callback_url),
            "callback_url_preview": str(callback_url or "")[:160],
        }
        try:
            params = self._follow_consent_for_code(callback_url)
        except ChatGPTRegisterError as exc:
            # Consent transport death surfaces as network (quarantine-eligible).
            consent_step["ok"] = False
            consent_step["error"] = str(exc)[:200]
            consent_step["error_kind"] = exc.kind
            return RegistrationResult(
                ok=False,
                error=str(exc),
                error_kind=exc.kind or "network",
                fail_step=exc.step or "oauth_callback",
                callback_url=callback_url,
                device_id=self.device_id,
                steps={"oauth_callback": consent_step},
            )
        if not params:
            consent_step["ok"] = False
            consent_step["error"] = "missing_oauth_callback"
            return RegistrationResult(
                ok=False,
                error="missing_oauth_callback",
                error_kind="oauth_callback",
                fail_step="oauth_callback",
                callback_url=callback_url,
                device_id=self.device_id,
                steps={"oauth_callback": consent_step},
            )
        code = params["code"]
        consent_step["ok"] = True
        consent_step["has_code"] = True
        # Fresh session for token exchange is more reliable with some proxies
        token_session = create_session(self.proxy)
        try:
            resp, error = request_with_retry(
                token_session,
                "post",
                f"{AUTH_BASE}/oauth/token",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
                    "client_id": PLATFORM_OAUTH_CLIENT_ID,
                    "code_verifier": self.code_verifier,
                },
                timeout=60,
            )
        finally:
            try:
                token_session.close()
            except Exception:
                pass
        token_step: dict[str, Any] = {}
        if resp is None:
            token_step = {"ok": False, "error": str(error or "")[:200]}
            return RegistrationResult(
                ok=False,
                error=f"oauth_token_failed:{error}",
                error_kind="network",
                fail_step="token",
                callback_url=callback_url,
                device_id=self.device_id,
                steps={"oauth_callback": consent_step, "token": token_step},
            )
        status = int(getattr(resp, "status_code", 0) or 0)
        data = response_json(resp)
        token_step["status"] = status
        if status != 200:
            token_step["ok"] = False
            # Upstream 5xx during token POST is transport/edge, not bad grant body.
            err_kind = "network" if status >= 500 else "token"
            return RegistrationResult(
                ok=False,
                error=f"oauth_token_http_{status}",
                error_kind=err_kind,
                fail_step="token",
                callback_url=callback_url,
                device_id=self.device_id,
                steps={"oauth_callback": consent_step, "token": token_step},
            )
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        id_token = str(data.get("id_token") or "").strip()
        token_step["has_access"] = bool(access_token)
        token_step["has_refresh"] = bool(refresh_token)
        token_step["has_id"] = bool(id_token)
        if not access_token or not refresh_token:
            token_step["ok"] = False
            token_step["error"] = "missing_tokens"
            return RegistrationResult(
                ok=False,
                error="missing_tokens",
                error_kind="token",
                fail_step="token",
                callback_url=callback_url,
                device_id=self.device_id,
                steps={"oauth_callback": consent_step, "token": token_step},
            )
        payload = _decode_jwt_payload(id_token) or _decode_jwt_payload(access_token)
        email = str(payload.get("email") or "").strip()
        token_step["ok"] = True
        self.log("oauth token exchange ok")
        return RegistrationResult(
            ok=True,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            callback_url=callback_url,
            device_id=self.device_id,
            steps={"oauth_callback": consent_step, "token": token_step},
        )


def register_one(
    *,
    email: str,
    password: str | None = None,
    proxy: str = "",
    full_name: str | None = None,
    birthdate: str | None = None,
    otp_provider: Callable[[], str] | None = None,
    log: LogFn | None = None,
) -> RegistrationResult:
    """Run one platform OAuth signup. otp_provider must return the 6-digit code."""
    log = log or _std_log
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ChatGPTRegisterError("missing_email", kind="fatal")
    if otp_provider is None:
        raise ChatGPTRegisterError("missing_otp_provider", kind="fatal")

    password = password or _random_password()
    full_name = full_name or _random_name()
    birthdate = birthdate or _random_birthdate()
    steps: dict[str, Any] = {}
    pace_waits: list[dict[str, Any]] = []
    registrar = PlatformRegistrar(proxy=proxy, log=log)

    def pace(label: str) -> None:
        waited = _human_pause(log, label=label)
        if waited > 0:
            pace_waits.append({"step": label, "wait_s": round(waited, 3)})

    fail_step = ""
    try:
        fail_step = "authorize"
        auth_info = registrar.start_authorize(email)
        steps["authorize"] = {
            "status": auth_info.get("status"),
            "final_url": str(auth_info.get("final_url") or "")[:200],
        }
        pace("after_authorize")
        fail_step = "session"
        sess = registrar.establish_session()
        steps["session"] = sess
        pace("after_session")
        fail_step = "register_user"
        reg = registrar.register_user(email, password)
        steps["register_user"] = {"status": reg.get("status")}
        pace("after_register_user")
        fail_step = "send_otp"
        otp_send = registrar.send_otp()
        steps["send_otp"] = {"status": otp_send.get("status")}
        steps["otp_sent_at"] = time.time()
        log("waiting for email OTP…")
        code = ""
        last_otp_err = ""
        # One resend after first miss — temp-mail delivery is flaky.
        fail_step = "otp_wait"
        for attempt in range(1, 3):
            try:
                code = str(otp_provider() or "").strip()
            except Exception as exc:
                last_otp_err = str(exc)
                log(f"otp poll attempt={attempt} miss: {exc}")
                code = ""
            if code and code.isdigit():
                break
            if attempt < 2:
                pace("before_otp_resend")
                try:
                    resend = registrar.send_otp()
                    steps[f"send_otp_resend_{attempt}"] = {"status": resend.get("status")}
                    log(f"otp resend attempt={attempt} status={resend.get('status')}")
                except ChatGPTRegisterError as exc:
                    log(f"otp resend failed: {exc}")
                    steps[f"send_otp_resend_{attempt}"] = {"error": str(exc)[:120]}
        if not code or not code.isdigit():
            raise ChatGPTRegisterError(
                f"otp_wait:{last_otp_err or code!r}",
                kind="mail_miss",
                step="otp_wait",
                steps=steps,
            )
        steps["otp_code_len"] = len(code)
        pace("before_validate_otp")
        fail_step = "validate_otp"
        val = registrar.validate_otp(code)
        steps["validate_otp"] = {"status": val.get("status")}
        pace("before_create_account")
        fail_step = "create_account"
        create = registrar.create_account(full_name, birthdate)
        steps["create_account"] = {
            "status": create.get("status"),
            "continue_url": str(create.get("continue_url") or "")[:200],
        }
        callback = str(
            create.get("continue_url")
            or (val.get("json") or {}).get("continue_url")
            or auth_info.get("final_url")
            or ""
        )
        pace("before_token_exchange")
        fail_step = "token"
        token_result = registrar.exchange_tokens(callback)
        token_result.password = password
        token_result.email = token_result.email or email
        if pace_waits:
            steps["human_pace"] = pace_waits
        # Merge pre-token protocol ledger with consent/token sub-steps.
        merged = dict(steps)
        for k, v in (token_result.steps or {}).items():
            merged[k] = v
        token_result.steps = merged
        token_result.device_id = registrar.device_id
        if not token_result.ok:
            token_result.error_kind = token_result.error_kind or "provider"
            token_result.fail_step = token_result.fail_step or fail_step
        return token_result
    except ChatGPTRegisterError as exc:
        # Attach partial steps for sink attribution when raised mid-flow.
        if pace_waits and "human_pace" not in steps:
            steps["human_pace"] = pace_waits
        if not exc.steps:
            exc.steps = dict(steps)
        if not exc.step:
            exc.step = fail_step
        raise
    except Exception as exc:
        if pace_waits and "human_pace" not in steps:
            steps["human_pace"] = pace_waits
        raise ChatGPTRegisterError(
            f"unexpected:{exc}",
            kind="other",
            step=fail_step or "unknown",
            steps=steps,
        ) from exc
    finally:
        registrar.close()


def save_result(result: RegistrationResult, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception:
        pass
    return p


def generate_password() -> str:
    return _random_password()
