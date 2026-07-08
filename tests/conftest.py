"""Polycopy pytest conftest — repo-wide path bootstrap.

Ensures ``src`` is importable for all tests without per-file
``sys.path.insert`` mutations that interfere with test-ordering and
cross-module import resolution (notably the test_p18_fixes HTTP suite).
"""
from __future__ import annotations

import os
import pathlib
import sys

# Tests must not inherit a local production .env kill switch. Individual tests
# can still override this with monkeypatch.setenv when they exercise risk gates.
os.environ.setdefault("POLYCOPY_ORDER_KILL_SWITCH", "false")

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)