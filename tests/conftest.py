"""Polycopy pytest conftest — repo-wide path bootstrap.

Ensures ``src`` is importable for all tests without per-file
``sys.path.insert`` mutations that interfere with test-ordering and
cross-module import resolution (notably the test_p18_fixes HTTP suite).
"""
from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Generator

import pytest
from tests.sqlite_test_utils import OwnedSQLitePaths

pytest_plugins = ("pytester",)

# Tests must explicitly use the safe test default rather than inheriting a
# production `.env` kill switch. Individual risk-gate tests override this with
# monkeypatch and restore their own required value.
os.environ["POLYCOPY_ORDER_KILL_SWITCH"] = "false"

_SRC = str(pathlib.Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# The historical PR24Z reconciliation suite and its generator are now migration-only
# evidence. Clean PR #50 keeps permanent ingestion canonical-only; the corrected
# reconciliation tests are applied in the stacked canonical-migration PR.
collect_ignore = ["test_pr24z_historical_production_reconciliation.py"]


@pytest.fixture
def owned_sqlite(tmp_path: pathlib.Path) -> Generator[OwnedSQLitePaths, None, None]:
    """Per-test exact SQLite paths below pytest's owned ``tmp_path`` directory."""
    paths = OwnedSQLitePaths(tmp_path / "owned-sqlite")
    try:
        yield paths
    finally:
        paths.cleanup()
