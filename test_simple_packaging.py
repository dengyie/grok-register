#!/usr/bin/env python3
"""Contract + smoke checks for outsider-friendly simple packaging."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_simple_config_template() -> None:
    p = ROOT / "config.simple.example.json"
    assert p.is_file(), "missing config.simple.example.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    cfg = {
        k: v
        for k, v in raw.items()
        if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
    }
    # low-friction default channel
    assert cfg.get("email_provider") == "duckmail"
    assert "duckmail_api_key" in cfg
    assert cfg.get("cpa_export_enabled") is True
    assert cfg.get("cpa_probe_chat") is True
    assert cfg.get("cpa_probe_chat_required") is True
    assert cfg.get("cpa_remote_inject") is False
    assert cfg.get("cpa_auth_dir") == "./cpa_auths"
    assert "cli-chat-proxy.grok.com" in str(cfg.get("cpa_base_url") or "")
    # explicit headless/mint knobs for outsiders
    assert cfg.get("cpa_headless") is False
    assert cfg.get("cpa_mint_required") is False
    print("PASS simple config template")


def test_setup_script_syntax_and_readme() -> None:
    setup = ROOT / "scripts" / "setup_simple.sh"
    assert setup.is_file()
    mode = setup.stat().st_mode
    assert mode & stat.S_IXUSR, "setup_simple.sh must be executable"
    src = setup.read_text(encoding="utf-8")
    assert "config.simple.example.json" in src
    assert "register_cli.py" in src
    assert "doctor" in src
    assert "placeholder" in src or "duckmail_api_key" in src
    # bash -n
    subprocess.run(["bash", "-n", str(setup)], check=True)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "最短路径" in readme or "快速开始" in readme
    assert "config.simple.example.json" in readme
    assert "entitlement_denied" in readme
    assert "setup_simple.sh" in readme
    assert "duckmail" in readme.lower()
    assert "常见卡点" in readme or "Typical blockers" in readme or "卡点" in readme
    print("PASS setup script + readme")


def test_gitignore_keeps_examples() -> None:
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "config.json" in gi
    banned = {
        ln.strip().lstrip("/")
        for ln in gi.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    assert "config.simple.example.json" not in banned
    assert "scripts/setup_simple.sh" not in banned
    print("PASS gitignore does not ban simple example")


def test_setup_smoke_tmp_copy() -> None:
    """Run setup in a temp tree: creates config, does not overwrite existing."""
    setup_src = ROOT / "scripts" / "setup_simple.sh"
    simple = ROOT / "config.simple.example.json"
    mail_ex = ROOT / "mail_credentials.example.txt"
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "scripts").mkdir()
        shutil.copy2(setup_src, td_path / "scripts" / "setup_simple.sh")
        shutil.copy2(simple, td_path / "config.simple.example.json")
        shutil.copy2(mail_ex, td_path / "mail_credentials.example.txt")
        # minimal pyproject so uv may fail softly — doctor still runs
        (td_path / "pyproject.toml").write_text(
            '[project]\nname="t"\nversion="0"\nrequires-python=">=3.13"\n',
            encoding="utf-8",
        )
        env = os.environ.copy()
        # avoid network uv sync failure killing the script if uv tries hard —
        # script continues after warn if uv missing; if uv present sync may fail.
        # Patch: run with UV offline if possible
        env.setdefault("UV_NO_SYNC", "1")
        # First run
        r = subprocess.run(
            ["bash", "scripts/setup_simple.sh"],
            cwd=td_path,
            env=env,
            capture_output=True,
            text=True,
        )
        # allow non-zero if uv sync fails in empty project; files must exist
        out = (r.stdout or "") + (r.stderr or "")
        assert (td_path / "config.json").is_file(), out
        assert (td_path / "mail_credentials.txt").is_file(), out
        # mutate config and ensure second run does not overwrite
        (td_path / "config.json").write_text('{"email_provider":"keep-me"}\n', encoding="utf-8")
        r2 = subprocess.run(
            ["bash", "scripts/setup_simple.sh"],
            cwd=td_path,
            env=env,
            capture_output=True,
            text=True,
        )
        out2 = (r2.stdout or "") + (r2.stderr or "")
        assert "skip" in out2.lower() or "already exists" in out2.lower(), out2
        assert "keep-me" in (td_path / "config.json").read_text(encoding="utf-8")
        # placeholder mail should trigger warn path when provider hotmail —
        # default simple is duckmail so expect duckmail key warn in output ideally
        assert "doctor" in out.lower() or "doctor" in out2.lower()
    print("PASS setup smoke tmp copy")


def main() -> int:
    test_simple_config_template()
    test_setup_script_syntax_and_readme()
    test_gitignore_keeps_examples()
    test_setup_smoke_tmp_copy()
    print("\nALL PASS (simple packaging)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
