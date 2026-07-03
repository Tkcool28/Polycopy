"""Polycopy pytest conftest — repo-wide path bootstrap.

Ensures ``src`` is importable for all tests without per-file
``sys.path.insert`` mutations that interfere with test-ordering and
cross-module import resolution (notably the test_p18_fixes HTTP suite).
"""
from __future__ import annotations

import pathlib
import sys

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)