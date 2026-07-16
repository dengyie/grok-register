#!/usr/bin/env python3
"""ChatGPT adapter must not feed register proxy into email sources by default."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from register_core.providers.chatgpt_adapter import ChatGPTProvider, resolve_mail_proxy


class TestMailProxySeparation(unittest.TestCase):
    def test_resolve_mail_proxy_never_falls_back_to_register(self) -> None:
        self.assertEqual(resolve_mail_proxy({"proxy": "http://reg:1"}), "")
        self.assertEqual(
            resolve_mail_proxy({"proxy": "http://reg:1", "mail_proxy": "http://mail:2"}),
            "http://mail:2",
        )

    def test_env_mail_proxy(self) -> None:
        with patch.dict(
            os.environ,
            {"EMAIL_PROXY": "http://env-mail:3", "CHATGPT_MAIL_PROXY": "", "MAIL_PROXY": ""},
            clear=False,
        ):
            self.assertEqual(resolve_mail_proxy({}), "http://env-mail:3")

    def test_adapter_constructs_source_with_mail_proxy_only(self) -> None:
        captured: dict = {}

        def fake_get(name, **kw):
            captured["name"] = name
            captured["kw"] = kw

            class Src:
                name = name

                def allocate(self):
                    raise RuntimeError("stop-before-network")

            return Src()

        prov = ChatGPTProvider(proxy="http://register-egress:8080", email_source_name="tinyhost")
        with patch(
            "register_core.providers.chatgpt_adapter.get_email_source",
            side_effect=fake_get,
        ):
            try:
                prov.register_one(extra={"proxy": "http://register-egress:8080"})
            except Exception:
                pass
        self.assertIn("kw", captured)
        # default: no register proxy on mail path
        self.assertIn(captured["kw"].get("proxy"), (None, ""))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
