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
    # Next arm: a line whose strip matches `<words>)` at the same 2-space indent
    # as the trigger, or `esac`. `words` allows `*` and `...` so a same-indent
    # `*)` / `*|*)` default guard also terminates the block, and `esac` must be at
    # the same indent as the trigger so a nested inner `case ... esac` (deeper
    # indent) doesn't prematurely end the outer arm.
    trig_indent = len(lines[start]) - len(lines[start].lstrip())
    for j in range(start + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()
        if stripped == "esac" and (len(line) - len(line.lstrip())) == trig_indent:
            end = j
            break
        # arm line: indent matches trigger and looks like `<words>)`
        indent = len(line) - len(line.lstrip())
        if indent == trig_indent and re.match(r"^[A-Za-z0-9_|.*\-\[\] ]+\)\s*$", stripped):
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
        self.assertIn("CHATGPT_LEGACY", block)
        self.assertIn("providers/chatgpt/run-register.sh", block)
        # env overrides still forwarded via register_core CLI flags
        self.assertIn("REGISTER_EGRESS", block)
        self.assertIn("CHATGPT_PROXY", block)
        # default register_core must precede the legacy runner — check on exec lines
        # only (strip `#` comments so prose mentions don't skew ordering).
        code = "\n".join(
            ln for ln in block.splitlines() if not ln.lstrip().startswith("#")
        )
        core_pos = code.find('exec "$_PY"')
        legacy_pos = code.find('exec bash "$ROOT/providers/chatgpt/run-register.sh"')
        self.assertGreater(core_pos, -1, "chatgpt branch must exec register_core")
        self.assertGreater(legacy_pos, -1, "chatgpt legacy fallback must be present")
        self.assertLess(core_pos, legacy_pos, "register_core must precede legacy chatgpt runner")

    def test_chatgpt_profile_selection_by_email_source(self) -> None:
        block = _case_block(self.src, "chatgpt|openai|openai-platform")
        # Profile is chosen by CHATGPT_EMAIL_SOURCE (legacy default = cloudflare → cf).
        self.assertIn("CHATGPT_EMAIL_SOURCE", block)
        for prof in (
            "profiles/chatgpt-cf.example.yaml",
            "profiles/chatgpt-tinyhost.example.yaml",
            "profiles/chatgpt-gmail.example.yaml",
        ):
            self.assertIn(prof, block, f"chatgpt branch must list profile {prof}")
        # default (cloudflare/auto/empty) maps to cf profile (matches legacy default)
        self.assertIn('cloudflare|cf|auto|"")', block)
        self.assertIn("CHATGPT_TIMEOUT", block)
        # timeout default 900 preserved (not argparse 1200)
        self.assertIn('"${CHATGPT_TIMEOUT:-900}"', block)

    def test_chatgpt_env_knobs_preserved(self) -> None:
        block = _case_block(self.src, "chatgpt|openai|openai-platform")
        # proxy rotation env forwarded (was dropped before the fix)
        self.assertIn("CHATGPT_PROXY_ROTATE_MODE", block)
        self.assertIn("CHATGPT_PROXY_ROTATE_EVERY", block)
        self.assertIn("--proxy-rotate", block)
        # sink only passed when CHATGPT_SINK explicitly set (else profile sink.path wins)
        self.assertIn("CHATGPT_SINK", block)
        self.assertIn("--sink", block)
        # email domain override honored at the env layer (profile no longer pins it)
        self.assertIn("CHATGPT_EMAIL_DOMAIN", block)

    # ---- cross-cutting ----
    def test_all_three_provider_branches_mention_register_core(self) -> None:
        for trigger in ("grok|xai", "mimo|xiaomi|mimo-tts", "chatgpt|openai|openai-platform"):
            block = _case_block(self.src, trigger)
            self.assertIn("register_core", block, f"{trigger} must reference register_core")

    def test_core_subcommand_branch_unchanged(self) -> None:
        # `./register.sh core ...` keeps delegating to `python -m register_core "$@"`
        block = _case_block(self.src, "core|framework")
        self.assertIn("-m register_core", block)

    # ---- _case_block regex hardening ----
    def test_case_block_regex_recognizes_default_guard(self) -> None:
        # A same-indent `*)` (default arm) must terminate a sibling block, so future
        # additions of a default guard don't get mis-segmented into the next sibling.
        src = (
            "case x in\n"
            "  foo|bar)\n"
            "    echo one\n"
            "    ;;\n"
            "  *)\n"
            "    echo default\n"
            "    ;;\n"
            "esac\n"
        )
        block = _case_block(src, "foo|bar")
        self.assertIn("echo one", block)
        self.assertNotIn("echo default", block, "*) default guard must end the foo|bar block")

    def test_case_block_regex_recognizes_dotdot_guard(self) -> None:
        # `...` range arm (some shells) and `*|*)` both terminate.
        src = (
            "case x in\n"
            "  grok|xai)\n"
            "    exec a\n"
            "    ;;\n"
            "  ...)\n"
            "    exec b\n"
            "esac\n"
        )
        block = _case_block(src, "grok|xai")
        self.assertIn("exec a", block)
        self.assertNotIn("exec b", block)

    # ---- profile egress honesty (fix #1 / core finding) ----
    def test_grok_profile_pins_clash_egress(self) -> None:
        # Grok egress must be pinned in the profile (not `auto`) so profile_to_job
        # sets extra["proxy"] truthy and grok_adapter force-sets child PROXY/CPA_PROXY
        # → Pipeline owns egress instead of relying on inherited shell PROXY env.
        p = (ROOT / "profiles" / "grok-tinyhost.example.yaml").read_text(encoding="utf-8")
        self.assertIn("mode: clash", p)
        self.assertIn('"http://127.0.0.1:7897"', p)
        # a concrete proxy url must accompany clash mode (the fix)
        self.assertRegex(p, r"proxy:\s*\"?http://127\.0\.0\.1:7897")

    def test_chatgpt_tinyhost_profile_does_not_pin_domain(self) -> None:
        # chatgpt tinyhost profile must NOT pin domain so CHATGPT_EMAIL_DOMAIN env
        # override is honored (extra["email_domain"] unset → adapter self.email_domain ← env).
        p = (ROOT / "profiles" / "chatgpt-tinyhost.example.yaml").read_text(encoding="utf-8")
        mailbox_lines = p.split("  decode:", 1)[0].split("  mailbox:", 1)[1]
        self.assertNotIn("publicvm.com", mailbox_lines)
        self.assertNotRegex(mailbox_lines, r"^\s*domain:\s*\S")


if __name__ == "__main__":
    unittest.main()
