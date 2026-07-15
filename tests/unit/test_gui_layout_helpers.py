"""Offline helpers for GrokRegisterGUI layout (no Tk mainloop required)."""

from __future__ import annotations

import grok_register_ttk as m


def test_normalize_provider_key_aliases():
    n = m.GrokRegisterGUI._normalize_provider_key
    assert n(None, "outlookmail") == "hotmail"
    assert n(None, "outlook") == "hotmail"
    assert n(None, "microsoft") == "hotmail"
    assert n(None, "google") == "gmail"
    assert n(None, "GOOGLE") == "gmail"
    assert n(None, "yyds") == "yyds"
    assert n(None, "duckmail") == "duckmail"
    assert n(None, "") == "duckmail"
    assert n(None, "unknown-provider") == "duckmail"


def test_gui_public_helper_surface():
    """Regression: deep polish helpers must remain on the class."""
    for name in (
        "_enqueue_ui",
        "_drain_ui_queue",
        "_set_form_enabled",
        "_validation_fail",
        "copy_log",
        "open_output_file",
        "set_phase",
    ):
        assert hasattr(m.GrokRegisterGUI, name), name
