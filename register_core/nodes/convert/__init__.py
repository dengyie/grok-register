"""Lightweight proxy profile conversion (no register-path weight).

Parse → validate → split → pack:
  dialable HTTP/SOCKS  → nodes.json  (curl_cffi, egress=list)
  protocol proxies     → runtime.yaml (mihomo core, egress=core)

Register machine only *consumes* those artifacts; conversion is opt-in CLI.
"""

from __future__ import annotations

from register_core.nodes.convert.pipeline import (
    ImportResult,
    MergePlan,
    convert_paths,
    convert_text,
    merge_dialable,
    pack_result,
)
from register_core.nodes.convert.validate import ValidationIssue, validate_proxy

__all__ = [
    "ImportResult",
    "MergePlan",
    "ValidationIssue",
    "convert_paths",
    "convert_text",
    "merge_dialable",
    "pack_result",
    "validate_proxy",
]
