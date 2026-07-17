"""Load register.v1 profiles from YAML/JSON and map to RegisterJob + composite mail."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from register_core.config.schema import (
    BurnSpec,
    DecodeSpec,
    EgressSpec,
    MailboxSpec,
    ProviderSpec,
    RegisterProfile,
    SecretsSpec,
    SinkSpec,
    StrategySpec,
    VerifySpec,
)
from register_core.contracts import RegisterJob
from register_core.decode.registry import get_otp_decoder
from register_core.email.composite import CompositeEmailSource
from register_core.mailbox.registry import get_mailbox_provider

_SECRET_KEY_RE = re.compile(
    r"(password|secret|token|api_key|apikey|authorization|auth_key|imap_pass)",
    re.I,
)
_TYPE_ALIASES = {
    "cf": "cloudflare",
    "cloudflare_worker": "cloudflare",
    "gmail": "gmail_imap",
}


class ProfileLoadError(ValueError):
    """Invalid profile path, schema, or secret policy violation."""


def _read_raw(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ProfileLoadError(
                "PyYAML required for .yaml profiles (project dep; use .venv)"
            ) from exc
        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        # try json then yaml
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                import yaml  # type: ignore

                data = yaml.safe_load(text)
            except Exception as exc:
                raise ProfileLoadError(f"cannot parse profile {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProfileLoadError(f"profile root must be object: {path}")
    return data


def _as_dict(v: Any) -> dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _as_list_str(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,;\s]+", v) if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()] if str(v).strip() else []


def _norm_type(name: str) -> str:
    key = (name or "").strip().lower()
    return _TYPE_ALIASES.get(key, key)


def _check_secrets_policy(raw: dict[str, Any], *, mode: str, path: str) -> None:
    """prod mode: reject non-empty plaintext values under secret-like keys."""
    if (mode or "prod").strip().lower() != "prod":
        return

    def walk(obj: Any, key_path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                kp = f"{key_path}.{k}" if key_path else str(k)
                # env *names* are ok: keys ending with _env or values that are env refs only
                if str(k).endswith("_env"):
                    continue
                if _SECRET_KEY_RE.search(str(k)):
                    if isinstance(v, str) and v.strip() and not str(k).endswith("_env"):
                        # allow empty / env:VAR form
                        if v.strip().startswith("env:"):
                            continue
                        # allow pure env var name placeholders only if key ends with _env (handled)
                        raise ProfileLoadError(
                            f"secrets.mode=prod forbids plaintext secret at {kp} in {path}"
                        )
                walk(v, kp)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{key_path}[{i}]")

    walk(raw, "")


def parse_profile_dict(data: dict[str, Any], *, source_path: str = "") -> RegisterProfile:
    api = str(data.get("apiVersion") or data.get("api_version") or "").strip()
    if api and api not in ("register.v1", "v1"):
        raise ProfileLoadError(f"unsupported apiVersion={api!r}; want register.v1")

    # support both CRD-ish {metadata,spec} and flat {name,provider,...}
    meta = _as_dict(data.get("metadata"))
    spec = _as_dict(data.get("spec")) if "spec" in data else data

    name = str(
        meta.get("name")
        or spec.get("name")
        or data.get("name")
        or (Path(source_path).stem if source_path else "unnamed")
    ).strip()

    prov_raw = spec.get("provider")
    if isinstance(prov_raw, str):
        provider = ProviderSpec(name=prov_raw.strip())
    elif isinstance(prov_raw, dict):
        provider = ProviderSpec(
            name=str(prov_raw.get("name") or "").strip(),
            options=_as_dict(prov_raw.get("options")),
        )
    else:
        # flat: provider: chatgpt at top of spec
        provider = ProviderSpec(name=str(spec.get("provider_name") or "").strip())
    if not provider.name:
        raise ProfileLoadError("profile.spec.provider.name is required")

    count = int(spec.get("count") or 1)
    if count < 1:
        raise ProfileLoadError("count must be >= 1")

    email_source = str(spec.get("email_source") or "").strip().lower()

    mailbox: MailboxSpec | None = None
    mb = spec.get("mailbox")
    if isinstance(mb, dict) and mb.get("type"):
        mailbox = MailboxSpec(
            type=_norm_type(str(mb.get("type"))),
            domain=str(mb.get("domain") or "").strip(),
            options=_as_dict(mb.get("options")),
        )
    elif isinstance(mb, str) and mb.strip():
        mailbox = MailboxSpec(type=_norm_type(mb))

    decode: DecodeSpec | None = None
    dc = spec.get("decode")
    if isinstance(dc, dict) and dc.get("type"):
        decode = DecodeSpec(
            type=_norm_type(str(dc.get("type"))),
            options=_as_dict(dc.get("options")),
        )
    elif isinstance(dc, str) and dc.strip():
        decode = DecodeSpec(type=_norm_type(dc))

    # paired fallback from email_source
    if email_source and email_source not in ("provider", "none", "internal", ""):
        if mailbox is None:
            mailbox = MailboxSpec(type=_norm_type(email_source))
        if decode is None:
            decode = DecodeSpec(type=_norm_type(email_source))

    st_raw = _as_dict(spec.get("strategy"))
    eg_raw = _as_dict(st_raw.get("egress"))
    burn_raw = _as_dict(st_raw.get("burn"))
    cool_raw = st_raw.get("cool")
    cool_soft = 0
    if isinstance(cool_raw, dict):
        cool_soft = int(cool_raw.get("soft_seconds") or 0)
    elif "cool_soft_seconds" in st_raw:
        cool_soft = int(st_raw.get("cool_soft_seconds") or 0)

    egress = EgressSpec(
        mode=str(eg_raw.get("mode") or "auto").strip().lower() or "auto",
        proxy=str(eg_raw.get("proxy") or "").strip(),
        proxy_list=str(eg_raw.get("proxy_list") or "").strip(),
        rotate_every=max(1, int(eg_raw.get("rotate_every") or 1)),
        rotate_required=bool(eg_raw.get("rotate_required") or False),
    )
    burn = BurnSpec(
        enabled=bool(burn_raw.get("enabled", True)),
        track=_as_list_str(burn_raw.get("track")) or ["ip", "domain"],
        on_kinds=_as_list_str(burn_raw.get("on_kinds"))
        or ["registration_disallowed", "unsupported_email"],
        state_path=str(burn_raw.get("state_path") or "").strip(),
    )
    strategy = StrategySpec(
        fail_fast=bool(st_raw.get("fail_fast", True)),
        fail_fast_kinds=_as_list_str(st_raw.get("fail_fast_kinds"))
        or [
            "registration_disallowed",
            "unsupported_email",
            "fatal",
            "verify",
        ],
        egress=egress,
        mail_proxy=str(st_raw.get("mail_proxy") or "direct").strip() or "direct",
        burn=burn,
        cool_soft_seconds=cool_soft,
    )

    vf_raw = spec.get("verify")
    if isinstance(vf_raw, bool):
        verify = VerifySpec(enabled=vf_raw)
    elif isinstance(vf_raw, dict):
        verify = VerifySpec(
            enabled=bool(vf_raw.get("enabled", True)),
            name=str(vf_raw.get("name") or "auto").strip() or "auto",
        )
    else:
        verify = VerifySpec()

    sk_raw = _as_dict(spec.get("sink"))
    sink = SinkSpec(path=str(sk_raw.get("path") or spec.get("sink_path") or "").strip())

    sec_raw = _as_dict(spec.get("secrets"))
    secrets = SecretsSpec(
        mode=str(sec_raw.get("mode") or "prod").strip().lower() or "prod",
        maps={
            str(k): str(v)
            for k, v in _as_dict(sec_raw.get("maps")).items()
            if str(k).strip()
        },
    )

    _check_secrets_policy(data, mode=secrets.mode, path=source_path or name)

    return RegisterProfile(
        name=name,
        provider=provider,
        count=count,
        mailbox=mailbox,
        decode=decode,
        strategy=strategy,
        verify=verify,
        sink=sink,
        secrets=secrets,
        email_source=email_source,
        source_path=source_path,
        raw=data,
    )


def load_profile(path: str | Path) -> RegisterProfile:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ProfileLoadError(f"profile not found: {p}")
    return parse_profile_dict(_read_raw(p), source_path=str(p))


def resolve_mail_proxy_value(raw: str) -> str:
    """Map strategy.mail_proxy to concrete URL or empty (direct)."""
    s = (raw or "direct").strip()
    if not s or s.lower() in ("direct", "none", "off", "-"):
        return ""
    if s.lower().startswith("env:"):
        env_name = s.split(":", 1)[1].strip()
        return str(os.environ.get(env_name) or "").strip()
    return s


def _source_kwargs_from_profile(profile: RegisterProfile) -> dict[str, Any]:
    """Kwargs for mailbox/decode factories (proxy + domain + options)."""
    kw: dict[str, Any] = {}
    mail_proxy = resolve_mail_proxy_value(profile.strategy.mail_proxy)
    if mail_proxy:
        kw["proxy"] = mail_proxy
    else:
        kw["proxy"] = None
    domain = ""
    if profile.mailbox and profile.mailbox.domain:
        domain = profile.mailbox.domain
    domain = domain or str((profile.mailbox.options if profile.mailbox else {}).get("domain") or "")
    if domain:
        kw["domain"] = domain
    # merge mailbox options (non-secret) then decode options for decoder path
    if profile.mailbox:
        for k, v in (profile.mailbox.options or {}).items():
            if k in ("proxy", "domain"):
                continue
            kw.setdefault(k, v)
    return kw


def build_composite_email(profile: RegisterProfile) -> CompositeEmailSource | None:
    """Build Mailbox+Decode composite, or None for provider-internal mail."""
    mtype = profile.mailbox_type()
    dtype = profile.decode_type()
    if mtype in ("", "provider", "none", "internal") and dtype in (
        "",
        "provider",
        "none",
        "internal",
    ):
        return None
    if mtype in ("", "provider", "none", "internal") or dtype in (
        "",
        "provider",
        "none",
        "internal",
    ):
        raise ProfileLoadError(
            f"mailbox/decode must both be core types or both provider-internal; "
            f"got mailbox={mtype!r} decode={dtype!r}"
        )

    base_kw = _source_kwargs_from_profile(profile)
    # Paired same-backend: one EmailSource instance shared (token continuity for CF JWT).
    if mtype == dtype:
        from register_core.email.registry import get_email_source
        from register_core.mailbox.adapters import EmailSourceMailbox
        from register_core.decode.adapters import EmailSourceDecoder

        src = get_email_source(mtype, **base_kw)
        return CompositeEmailSource(
            EmailSourceMailbox(src, name=mtype),
            EmailSourceDecoder(src, name=dtype),
            name=mtype,
        )

    # Split: separate backends (caller must ensure address contract matches).
    mb_kw = dict(base_kw)
    if profile.mailbox:
        mb_kw.update(profile.mailbox.options or {})
        if profile.mailbox.domain:
            mb_kw["domain"] = profile.mailbox.domain
    dc_kw = dict(base_kw)
    if profile.decode:
        # decode options may include timeout defaults later; strip domain pin if any
        for k, v in (profile.decode.options or {}).items():
            if k in ("timeout_s", "poll_interval_s", "sender_hint"):
                continue
            dc_kw[k] = v
    mailbox = get_mailbox_provider(mtype, **mb_kw)
    decoder = get_otp_decoder(dtype, **dc_kw)
    return CompositeEmailSource(mailbox, decoder)


def profile_to_job(
    profile: RegisterProfile,
    *,
    overrides: dict[str, Any] | None = None,
) -> RegisterJob:
    """Map profile → RegisterJob (extra holds strategy/egress for existing pipeline)."""
    ov = dict(overrides or {})
    count = int(ov.get("count") or profile.count)
    verify = profile.verify.enabled
    if "verify" in ov:
        verify = bool(ov["verify"])
    fail_fast = profile.strategy.fail_fast
    if "fail_fast" in ov:
        fail_fast = bool(ov["fail_fast"])

    email_source = profile.mailbox_type()
    if email_source in ("", "provider"):
        email_source = "provider"
    # For in-process, pipeline resolves composite separately; keep name for artifacts.
    if profile.mailbox_type() not in ("", "provider", "none", "internal"):
        email_source = profile.mailbox_type()

    extra: dict[str, Any] = {}
    # provider options
    extra.update(profile.provider.options or {})
    # strategy → existing pipeline keys
    eg = profile.strategy.egress
    mode = str(ov.get("egress") or eg.mode or "").strip()
    if mode:
        extra["egress"] = mode
    proxy = str(ov.get("proxy") or eg.proxy or "").strip()
    if proxy:
        extra["proxy"] = proxy
    proxy_list = str(ov.get("proxy_list") or eg.proxy_list or "").strip()
    if proxy_list:
        extra["proxy_list"] = proxy_list
    extra["proxy_rotate_every"] = int(eg.rotate_every or 1)
    if eg.rotate_required:
        extra["proxy_rotate_required"] = True

    mail_proxy = resolve_mail_proxy_value(profile.strategy.mail_proxy)
    # explicit direct marker for resolve_mail_proxy: leave empty
    if mail_proxy:
        extra["mail_proxy"] = mail_proxy

    if profile.mailbox and profile.mailbox.domain:
        extra["email_domain"] = profile.mailbox.domain

    # decode timeouts into extra for adapters
    if profile.decode and profile.decode.options:
        if "timeout_s" in profile.decode.options:
            extra["otp_timeout_s"] = profile.decode.options["timeout_s"]
        if "otp_timeout_s" in profile.decode.options:
            extra["otp_timeout_s"] = profile.decode.options["otp_timeout_s"]

    # strategy metadata consumed by StrategyEngine (burn/cool + fail_fast_kinds)
    extra["_strategy"] = {
        "fail_fast_kinds": list(profile.strategy.fail_fast_kinds),
        "burn": {
            "enabled": profile.strategy.burn.enabled,
            "track": list(profile.strategy.burn.track),
            "on_kinds": list(profile.strategy.burn.on_kinds),
            "state_path": profile.strategy.burn.state_path,
        },
        "cool_soft_seconds": profile.strategy.cool_soft_seconds,
    }
    extra["_profile"] = {
        "name": profile.name,
        "path": profile.source_path,
        "mailbox": profile.mailbox_type(),
        "decode": profile.decode_type(),
        "secrets_mode": profile.secrets.mode,
    }

    # CLI overrides already folded for count/verify/fail_fast/egress/proxy
    if ov.get("timeout_s") is not None:
        extra["timeout_s"] = ov["timeout_s"]
    if ov.get("threads") is not None:
        extra["threads"] = ov["threads"]
    if ov.get("headless") is not None:
        extra["headless"] = ov["headless"]

    return RegisterJob(
        provider=profile.provider.name,
        count=count,
        email_source=email_source,
        verify=verify,
        fail_fast=fail_fast,
        extra=extra,
    )


def apply_cli_overrides(
    profile: RegisterProfile,
    *,
    count: int | None = None,
    no_verify: bool = False,
    no_fail_fast: bool = False,
    egress: str = "",
    proxy: str = "",
    proxy_list: str = "",
    sink: str = "",
    timeout: int | None = None,
    threads: int | None = None,
    headless: bool | None = None,
) -> tuple[RegisterJob, str]:
    """Build job + effective sink path from profile and CLI flags."""
    ov: dict[str, Any] = {}
    if count is not None and count >= 1:
        ov["count"] = count
    if no_verify:
        ov["verify"] = False
    if no_fail_fast:
        ov["fail_fast"] = False
    if egress:
        ov["egress"] = egress
    if proxy:
        ov["proxy"] = proxy
    if proxy_list:
        ov["proxy_list"] = proxy_list
    if timeout is not None:
        ov["timeout_s"] = timeout
    if threads is not None:
        ov["threads"] = threads
    if headless is not None:
        ov["headless"] = headless
    job = profile_to_job(profile, overrides=ov)
    sink_path = (sink or profile.sink.path or "").strip()
    return job, sink_path
