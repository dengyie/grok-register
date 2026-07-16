"""ChatGPT / OpenAI platform account registration protocol flow.

High-success path inspired by open-reg-auto + zhuce6:
  authorize (PKCE) → register user → email OTP → create_account → oauth/token

In-process; OTP via register_core EmailSource. No CPA/production inject.
"""

from __future__ import annotations

import base64
import hashlib
import json
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

    def __init__(self, message: str, *, kind: str = "provider") -> None:
        super().__init__(message)
        self.kind = kind


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
            "device_id": self.device_id,
            "step_keys": sorted(self.steps.keys()),
        }


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
            headers["x-openai-sentinel-error"] = str(exc)[:200]
            self.log(f"sentinel soft-fail flow={flow}: {exc}")
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
                error or "authorize_request_failed", kind="provider"
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
            headers["x-openai-sentinel-error"] = str(exc)[:200]
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
        for url in candidates:
            request_with_retry(
                self.session,
                "get",
                str(url),
                headers=headers,
                allow_redirects=True,
            )
        has_session = bool(
            cookie_get(self.session, "oai-client-auth-session")
            or cookie_get(self.session, "auth_session")
            or cookie_get(self.session, "oai-auth-token")
        )
        self.log(f"session establish ok={has_session}")
        return {"ok": has_session}

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
            detail = str(body.get("error") or body.get("detail") or error or "")[:200]
            # 409 often means already registered — still fail this attempt cleanly
            raise ChatGPTRegisterError(
                f"register_user_http_{status}:{detail}",
                kind="provider",
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
            raise ChatGPTRegisterError(
                f"send_otp_http_{status}:{error}", kind="provider"
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
            raise ChatGPTRegisterError(
                f"validate_otp_http_{status}:{error or body}",
                kind="provider",
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
            kind = "provider"
            if code == "registration_disallowed" or "registration_disallowed" in str(
                body or error or ""
            ):
                # Risk engine block (IP / domain / device reputation) — not a protocol bug.
                kind = "registration_disallowed"
            raise ChatGPTRegisterError(
                f"create_account_http_{status}:{code or error or body}",
                kind=kind,
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
        for _ in range(12):
            resp, _err = request_with_retry(
                self.session,
                "get",
                current,
                headers=NAVIGATE_HEADERS,
                allow_redirects=False,
            )
            if resp is None:
                break
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
                resp2, _ = request_with_retry(
                    self.session,
                    "get",
                    current,
                    headers=NAVIGATE_HEADERS,
                    allow_redirects=True,
                )
                if resp2 is not None:
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
        ws_resp, _ = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/workspace/select",
            json={"workspace_id": workspace_id},
            headers=headers,
            allow_redirects=False,
        )
        if ws_resp is None:
            return None
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
        org_resp, _ = request_with_retry(
            self.session,
            "post",
            f"{AUTH_BASE}/api/accounts/organization/select",
            json=body,
            headers=org_headers,
            allow_redirects=False,
        )
        if org_resp is None:
            return None
        return _extract_oauth_code(
            str(getattr(org_resp, "headers", {}).get("Location") or "")
        )

    def exchange_tokens(self, callback_url: str) -> RegistrationResult:
        params = self._follow_consent_for_code(callback_url)
        if not params:
            return RegistrationResult(
                ok=False,
                error="missing_oauth_callback",
                error_kind="provider",
                callback_url=callback_url,
                device_id=self.device_id,
            )
        code = params["code"]
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
        if resp is None:
            return RegistrationResult(
                ok=False,
                error=f"oauth_token_failed:{error}",
                error_kind="provider",
                callback_url=callback_url,
                device_id=self.device_id,
            )
        status = int(getattr(resp, "status_code", 0) or 0)
        data = response_json(resp)
        if status != 200:
            return RegistrationResult(
                ok=False,
                error=f"oauth_token_http_{status}",
                error_kind="provider",
                callback_url=callback_url,
                device_id=self.device_id,
            )
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        id_token = str(data.get("id_token") or "").strip()
        if not access_token or not refresh_token:
            return RegistrationResult(
                ok=False,
                error="missing_tokens",
                error_kind="provider",
                callback_url=callback_url,
                device_id=self.device_id,
            )
        payload = _decode_jwt_payload(id_token) or _decode_jwt_payload(access_token)
        email = str(payload.get("email") or "").strip()
        self.log("oauth token exchange ok")
        return RegistrationResult(
            ok=True,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            callback_url=callback_url,
            device_id=self.device_id,
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
    registrar = PlatformRegistrar(proxy=proxy, log=log)
    try:
        auth_info = registrar.start_authorize(email)
        steps["authorize"] = {
            "status": auth_info.get("status"),
            "final_url": str(auth_info.get("final_url") or "")[:200],
        }
        sess = registrar.establish_session()
        steps["session"] = sess
        # Soft: continue even without session cookie (some flows still work)
        reg = registrar.register_user(email, password)
        steps["register_user"] = {"status": reg.get("status")}
        otp_send = registrar.send_otp()
        steps["send_otp"] = {"status": otp_send.get("status")}
        steps["otp_sent_at"] = time.time()
        log("waiting for email OTP…")
        code = ""
        last_otp_err = ""
        # One resend after first miss — temp-mail delivery is flaky.
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
                try:
                    resend = registrar.send_otp()
                    steps[f"send_otp_resend_{attempt}"] = {"status": resend.get("status")}
                    log(f"otp resend attempt={attempt} status={resend.get('status')}")
                except ChatGPTRegisterError as exc:
                    log(f"otp resend failed: {exc}")
                    steps[f"send_otp_resend_{attempt}"] = {"error": str(exc)[:120]}
        if not code or not code.isdigit():
            raise ChatGPTRegisterError(
                f"otp_wait:{last_otp_err or code!r}", kind="mail_miss"
            )
        steps["otp_code_len"] = len(code)
        val = registrar.validate_otp(code)
        steps["validate_otp"] = {"status": val.get("status")}
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
        token_result = registrar.exchange_tokens(callback)
        token_result.password = password
        token_result.email = token_result.email or email
        token_result.steps = steps
        token_result.device_id = registrar.device_id
        if not token_result.ok:
            token_result.error_kind = token_result.error_kind or "provider"
        return token_result
    except ChatGPTRegisterError:
        raise
    except Exception as exc:
        raise ChatGPTRegisterError(f"unexpected:{exc}", kind="other") from exc
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
