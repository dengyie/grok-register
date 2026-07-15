#!/usr/bin/env python3
"""Unit tests for MiMo → CPA openai-compatibility inject helper."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MOD_PATH = ROOT / "providers" / "mimo" / "inject_cpa_openai.py"


def _load():
    spec = importlib.util.spec_from_file_location("inject_cpa_openai", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


SAMPLE = """# sample
auth-dir: "/root/.cli-proxy-api"
api-keys:
  - sk-client-aaa
openai-compatibility:
  - name: deepseek
    priority: -10
    base-url: https://api.deepseek.com
    api-key-entries:
      - api-key: sk-deepseek-old
    models:
      - name: deepseek-v4-flash
        alias: ""
  - name: xiaomimimo
    priority: 100
    base-url: https://api.xiaomimimo.com/v1
    api-key-entries:
      - api-key: sk-existingoldkey111111111111111111111111
    models:
      - name: mimo-v2.5-tts
        alias: ""
      - name: mimo-v2.5-tts-voiceclone
        alias: ""
other-section:
  foo: 1
"""


class TestMimoCpaOpenaiInject(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load()

    def test_extract_keys_from_numbered_line(self) -> None:
        keys = self.mod.extract_keys_from_text("1. sk-testdummykeyaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("sk-testdummy"))

    def test_append_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.yaml"
            cfg.write_text(SAMPLE, encoding="utf-8")
            key = "sk-existingoldkey111111111111111111111111"
            r1 = self.mod.inject_local(cfg, [key])
            self.assertTrue(r1["ok"])
            self.assertFalse(r1["changed"])
            text = cfg.read_text(encoding="utf-8")
            self.assertEqual(text.count(key), 1)

    def test_append_new_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.yaml"
            cfg.write_text(SAMPLE, encoding="utf-8")
            new = "sk-newkeyaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            r = self.mod.inject_local(cfg, [new])
            self.assertTrue(r["ok"])
            self.assertTrue(r["changed"])
            text = cfg.read_text(encoding="utf-8")
            self.assertIn(new, text)
            self.assertIn("sk-existingoldkey", text)
            # deepseek untouched
            self.assertIn("sk-deepseek-old", text)
            self.assertIn("other-section:", text)
            span = self.mod._channel_block_span(text, "xiaomimimo")
            assert span
            entry = text[span[0] : span[1]]
            self.assertEqual(len(self.mod.list_keys_in_entry(entry)), 2)

    def test_create_channel_if_missing(self) -> None:
        slim = """openai-compatibility:
  - name: deepseek
    base-url: https://api.deepseek.com
    api-key-entries:
      - api-key: sk-deepseek-old
    models:
      - name: deepseek-v4-flash
        alias: ""
tail: 1
"""
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "config.yaml"
            cfg.write_text(slim, encoding="utf-8")
            new = "sk-brandnewbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            r = self.mod.inject_local(cfg, [new])
            self.assertTrue(r["changed"])
            self.assertTrue(r.get("created_channel"))
            text = cfg.read_text(encoding="utf-8")
            self.assertIn("name: xiaomimimo", text)
            self.assertIn(new, text)
            self.assertIn("api.xiaomimimo.com/v1", text)

    def test_jsonl_extract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jsonl"
            p.write_text(
                json.dumps(
                    {
                        "email": "a@b.com",
                        "apiKey": "sk-jsonlcccccccccccccccccccccccccccccccccccc",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            keys = self.mod.extract_keys_from_jsonl(p)
            self.assertEqual(keys[0][:10], "sk-jsonlcc")


if __name__ == "__main__":
    unittest.main()
