"""Skeleton smoke: required paths and public contracts exist."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_architecture_and_docs_exist() -> None:
    for rel in (
        "ARCHITECTURE.md",
        "Makefile",
        "docs/ADDING_PROVIDER.md",
        "docs/LAYOUT.md",
        "providers/README.md",
        "providers/_template/README.md",
        "providers/_template/run-register.sh",
        "providers/grok/README.md",
        "providers/mimo/README.md",
        "apps/README.md",
        "examples/minimal_pipeline.py",
        "register_core/README.md",
        "register.sh",
    ):
        p = ROOT / rel
        assert p.is_file(), f"missing skeleton file: {rel}"


def test_template_runner_is_executable_or_shell() -> None:
    runner = ROOT / "providers/_template/run-register.sh"
    text = runner.read_text(encoding="utf-8")
    assert text.startswith("#!")
    assert "RESULT_JSON" in text or "exit 2" in text


def test_register_core_lists_builtin_providers() -> None:
    from register_core.providers.registry import list_providers

    names = set(list_providers())
    assert "grok" in names
    assert "mimo" in names


def test_public_redact_in_example_shape() -> None:
    from register_core.contracts import RegisterResult

    r = RegisterResult(
        ok=True,
        provider="demo",
        email="a@b.c",
        password="hunter2-password",
        secret="sk-aaaaaaaaaaaaaaaaaaaaaaaa",
        secret_kind="api_key",
    )
    pub = r.to_public_dict()
    assert "hunter2" not in str(pub)
    assert pub["secret_kind"] == "api_key"
