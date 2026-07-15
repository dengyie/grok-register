#!/usr/bin/env python3
"""Deprecated name — use scripts/import_nodes.py or:
  python -m register_core nodes import <file>
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
print(
    "[deprecated] import_clash_to_nodes.py → use scripts/import_nodes.py "
    "or: python -m register_core nodes import …",
    file=sys.stderr,
)
sys.argv[0] = str(_HERE / "import_nodes.py")
runpy.run_path(str(_HERE / "import_nodes.py"), run_name="__main__")
