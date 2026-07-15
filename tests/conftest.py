"""Pytest root for tests/ tree.

Legacy root-level test_*.py remain discoverable via pyproject testpaths.
Prefer adding new unit tests under tests/unit/.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
