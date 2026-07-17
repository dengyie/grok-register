"""One-shot OTP poll for shell adapters.

Env REGISTER_OTP_SPEC (JSON):
  {
    "address": "u@d",
    "token": "...",
    "password": "",
    "provider": "tinyhost",
    "source": "tinyhost",          # registry name when paired
    "source_kwargs": {},
    "mailbox_type": "",            # optional split
    "decode_type": "",
    "timeout_s": 180,
    "poll_interval_s": 3,
    "sender_hint": "",
    "newer_than_epoch": null
  }

argv: [address_override] [used_code ...]
stdout: single line with 6-digit code (also full line "OTP <code>")
exit 0 on success, 2 on miss, 1 on config error.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _load_spec() -> dict[str, Any]:
    raw = (os.environ.get("REGISTER_OTP_SPEC") or "").strip()
    if not raw:
        path = (os.environ.get("REGISTER_OTP_SPEC_PATH") or "").strip()
        if path and os.path.isfile(path):
            raw = open(path, encoding="utf-8").read()
    if not raw:
        raise SystemExit("REGISTER_OTP_SPEC / REGISTER_OTP_SPEC_PATH missing")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("REGISTER_OTP_SPEC must be object")
    return data


def _build_source(spec: dict[str, Any]):
    mtype = str(spec.get("mailbox_type") or "").strip().lower()
    dtype = str(spec.get("decode_type") or "").strip().lower()
    kwargs = dict(spec.get("source_kwargs") or {})
    if mtype and dtype and mtype != dtype:
        from register_core.mailbox.registry import get_mailbox
        from register_core.decode.registry import get_decoder
        from register_core.email.composite import CompositeEmailSource

        mb = get_mailbox(mtype, **kwargs)
        dec = get_decoder(dtype, **kwargs)
        return CompositeEmailSource(mb, dec)
    source = str(spec.get("source") or mtype or dtype or "tinyhost").strip().lower()
    from register_core.email.registry import get_email_source

    return get_email_source(source, **kwargs)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        spec = _load_spec()
    except SystemExit as exc:
        print(f"otp_poll config: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"otp_poll config error: {exc}", file=sys.stderr)
        return 1

    address = (argv[0] if argv else "") or str(spec.get("address") or "")
    address = address.strip()
    used = set(a.strip() for a in argv[1:] if a and a.strip())
    if not address or "@" not in address:
        print("otp_poll: missing address", file=sys.stderr)
        return 1

    from register_core.contracts import Mailbox
    from register_core.errors import MailMissError

    mailbox = Mailbox(
        address=address,
        token=str(spec.get("token") or ""),
        password=str(spec.get("password") or ""),
        provider=str(spec.get("provider") or ""),
        meta=dict(spec.get("meta") or {}),
    )
    timeout_s = float(spec.get("timeout_s") or 180)
    poll_interval_s = float(spec.get("poll_interval_s") or 3)
    sender_hint = str(spec.get("sender_hint") or "") or None
    newer = spec.get("newer_than_epoch")
    newer_f = float(newer) if newer is not None else None

    try:
        src = _build_source(spec)
        otp = src.poll_otp(
            mailbox,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            used_codes=used or None,
            newer_than_epoch=newer_f,
            sender_hint=sender_hint,
        )
    except MailMissError as exc:
        print(f"otp_poll miss: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"otp_poll error: {exc}", file=sys.stderr)
        return 1

    code = (otp.code if hasattr(otp, "code") else str(otp)).strip()
    if not code:
        print("otp_poll: empty code", file=sys.stderr)
        return 2
    # Machine-readable: first line pure code; second optional label
    print(code)
    print(f"OTP {code}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
