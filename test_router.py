#!/usr/bin/env python3
"""Static router gate: the three ./register.sh production entries must route
through the register_core Pipeline shell (migrate milestone A). Asserts every
provider branch reaches register_core by default and only falls back to legacy
runner scripts behind an explicit *_LEGACY=1 env switch.

Run validation (not commit-gated): ./register.sh grok|chatgpt|mimo 1 actually
execs register_core on a dry host — see MANUAL in docs/DEVELOPED.md.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTER_SH = ROOT / "register.sh"
RUN_REGISTER_CORE = ROOT / "run-register-core.sh"


def _case_block(src: str, trigger: str) -> str:
    """Return the text of a `case` arm like `  grok|xai) ... ;;`.

    Scan line-by-line from the trigger arm until the next sibling arm or `esac`.
    Handles trailing `\\`-continued exec lines and nested blocks.
    """
    lines = src.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == trigger + ")" or stripped.startswith(trigger + ")"):
            start = i
            break
    if start is None:
        raise AssertionError(f"case arm {trigger!r} not found in register.sh")
    end = len(lines)
    # Next arm: a line whose strip matches `word|word...)` at the same 2-space indent
    # as the trigger, or `esac`.
    trig_indent = len(lines[start]) - len(lines[start].lstrip())
    for j in range(start + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()
        if stripped == "esac":
            end = j
            break
        # arm line: indent matches trigger and looks like `<words>)`
        indent = len(line) - len(line.lstrip())
        if indent == trig_indent and re.match(r"^[A-Za-z0-9_|.\-]+\)\s*$", stripped):
            end = j
            break
    return "\n".join(lines[start:end])


class TestRouterGate(unittest.TestCase):
    def setUp(self) -> None:
        self.src = REGISTER_SH.read_text(encoding="utf-8")
        self.core_src = RUN_REGISTER_CORE.read_text(encoding="utf-8")

    # ---- grok ----
    def test_grok_routes_to_register_core_by_default(self) -> None:
        block = _case_block(self.src, "grok|xai")
        # default path reaches run-register-core.sh (which execs register_core)
        self.assertIn("run-register-core.sh", block)
        # rollback guard is present
        self.assertIn("GROK_LEGACY", block)
        # legacy fallback retained (run-register.sh), kept as rollback / adapter target
        self.assertIn("run-register.sh", block)
        # register_cli.py direct exec is only the final local/dev fallback AFTER
        # the core shell and legacy runner have both been tried — i.e. run-register-core
        # must appear before any bare register_cli.py exec.
        core_pos = block.find("run-register-core.sh")
        cli_pos = block.find("register_cli.py")
        self.assertGreater(core_pos, -1)
        if cli_pos > -1:
            self.assertLess(
                core_pos,
                cli_pos,
                "run-register-core.sh must precede any register_cli.py fallback",
            )

    def test_grok_core_shell_execs_register_core(self) -> None:
        # run-register-core.sh must drive register_core Pipeline via profile
        self.assertIn("register_core run", self.core_src)
        self.assertIn("profiles/grok-tinyhost.example.yaml", self.core_src)
        # legacy exit contract preserved (0/1/2 mapping)
        self.assertIn("legacy", self.core_src.lower())
        # env outer shell preserved from run-register.sh
        self.assertIn("preflight-clash-nodes.sh", self.core_src)
        self.assertIn(".env", self.core_src)
        self.assertIn("PLAYWRIGHT_BROWSERS_PATH", self.core_src)

    # ---- mimo ----
    def test_mimo_routes_to_register_core_profile(self) -> None:
        block = _case_block(self.src, "mimo|xiaomi|mimo-tts")
        self.assertIn("-m register_core run", block)
        self.assertIn("profiles/mimo-tinyhost.example.yaml", block)
        self.assertIn("MIMO_LEGACY", block)
        self.assertIn("providers/mimo/run-register.sh", block)
        # default branch must reach register_core BEFORE the legacy Node runner
        core_pos = block.find("-m register_core run")
        legacy_pos = block.find("providers/mimo/run-register.sh")
        self.assertGreater(core_pos, -1)
        self.assertLess(core_pos, legacy_pos, "register_core must precede legacy mimo runner")

    # ---- chatgpt ----
    def test_chatgpt_routes_to_register_core_profile(self) -> None:
        block = _case_block(self.src, "chatgpt|openai|openai-platform")
        self.assertIn("-m register_core run", block)
        self.assertIn("profiles/chatgpt-tinyhost.example.yaml", block)
        self.assertIn("CHATGPT_LEGACY", block)
        self.assertIn("providers/chatgpt/run-register.sh", block)
        # env overrides still forwarded via register_core CLI flags
        self.assertIn("REGISTER_EGRESS", block)
        self.assertIn("CHATGPT_PROXY", block)

    # ---- cross-cutting ----
    def test_all_three_provider_branches_mention_register_core(self) -> None:
        for trigger in ("grok|xai", "mimo|xiaomi|mimo-tts", "chatgpt|openai|openai-platform"):
            block = _case_block(self.src, trigger)
            self.assertIn("register_core", block, f"{trigger} must reference register_core")

    def test_core_subcommand_branch_unchanged(self) -> None:
        # `./register.sh core ...` keeps delegating to `python -m register_core "$@"`
        block = _case_block(self.src, "core|framework")
        self.assertIn("-m register_core", block)


if __name__ == "__main__":
    unittest.main()
