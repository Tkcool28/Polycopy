"""Regression coverage for test-only exact SQLite path ownership and cleanup."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.sqlite_test_utils import OwnedSQLitePaths


def test_owned_sqlite_fixture_allocates_multiple_exact_paths(owned_sqlite):
    first = owned_sqlite.path("first.db")
    second = owned_sqlite.path("second.sqlite")
    generated = owned_sqlite.new_path("run")

    assert first.parent == second.parent == generated.parent
    assert first.parent == first.parent.resolve()
    assert [first.name, second.name, generated.name] == [
        "first.db", "second.sqlite", "run-1.db"
    ]


@pytest.mark.parametrize("name", ["/tmp/escape.db", "../escape.db", "a/escape.db", ".", ".."])
def test_owned_sqlite_rejects_absolute_and_traversal_names(tmp_path: Path, name: str):
    paths = OwnedSQLitePaths(tmp_path / "owned")

    with pytest.raises(ValueError, match="relative filename"):
        paths.path(name)


def test_cleanup_removes_only_registered_exact_sqlite_artifacts(tmp_path: Path):
    paths = OwnedSQLitePaths(tmp_path / "owned")
    registered = paths.path("registered.db")
    unregistered = paths.directory / "keep.db"
    for artifact in (
        registered,
        Path(f"{registered}-wal"),
        Path(f"{registered}-shm"),
        Path(f"{registered}-journal"),
        unregistered,
    ):
        artifact.write_text("fixture-owned")

    paths.cleanup()

    assert not registered.exists()
    assert not Path(f"{registered}-wal").exists()
    assert not Path(f"{registered}-shm").exists()
    assert not Path(f"{registered}-journal").exists()
    assert unregistered.read_text() == "fixture-owned"


def test_cleanup_rejects_symlink_escape_without_touching_target(tmp_path: Path):
    paths = OwnedSQLitePaths(tmp_path / "owned")
    database = paths.path("linked.db")
    outside = tmp_path / "outside.db"
    outside.write_text("must survive")
    database.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        paths.cleanup()

    assert outside.read_text() == "must survive"
    assert database.is_symlink()


def test_cleanup_rejects_sidecar_symlink_escape_without_touching_target(tmp_path: Path):
    paths = OwnedSQLitePaths(tmp_path / "owned")
    database = paths.path("test.db")
    database.write_text("database")
    outside = tmp_path / "outside-wal"
    outside.write_text("must survive")
    Path(f"{database}-wal").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        paths.cleanup()

    assert database.exists()
    assert outside.read_text() == "must survive"
