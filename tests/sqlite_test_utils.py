"""Test-only helpers for disposable, file-backed SQLite databases.

Each instance owns one pytest-provided directory and removes only the exact
paths it allocated.  It intentionally does not perform globbing or recursive
cleanup: SQLite's three known files are handled explicitly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Union


class OwnedSQLitePaths:
    """Allocate and safely clean exact SQLite paths below one owned directory."""

    _SIDECAR_SUFFIXES = ("-shm", "-wal", "-journal", "")

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise ValueError("owned SQLite directory must not be a symlink")
        self._paths: list[Path] = []
        self._counter = 0

    def path(self, name: Union[str, Path] = "test.db") -> Path:
        """Register one exact, relative SQLite database name in the owned dir."""
        candidate_name = Path(name)
        if (
            candidate_name.is_absolute()
            or len(candidate_name.parts) != 1
            or candidate_name.name in {"", ".", ".."}
            or ".." in candidate_name.parts
        ):
            raise ValueError("SQLite test database name must be one relative filename")
        candidate = (self.directory / candidate_name).resolve(strict=False)
        # ``strict=False`` resolves any existing symlink component.  The
        # returned path is an exact resolved child of the fixture-owned dir.
        if candidate.parent != self.directory:
            raise ValueError("SQLite test database path escapes owned directory")
        if candidate not in self._paths:
            self._paths.append(candidate)
        return candidate

    def new_path(self, stem: str = "sqlite") -> Path:
        """Register a distinct exact database path, suitable for repeated opens."""
        self._counter += 1
        return self.path(f"{stem}-{self._counter}.db")

    def cleanup(self) -> None:
        """Delete registered SQLite sidecars then DBs; never traverse or glob."""
        for db_path in reversed(self._paths):
            self._validate_registered_path(db_path)
            for suffix in self._SIDECAR_SUFFIXES:
                artifact = Path(f"{db_path}{suffix}")
                self._unlink_exact_owned(artifact)

    def _validate_registered_path(self, db_path: Path) -> None:
        if db_path.is_symlink():
            raise ValueError("refusing SQLite cleanup through symlink")
        if db_path.parent.resolve() != self.directory:
            raise ValueError("refusing SQLite cleanup outside owned directory")

    def _unlink_exact_owned(self, artifact: Path) -> None:
        if artifact.parent.resolve() != self.directory:
            raise ValueError("refusing SQLite sidecar cleanup outside owned directory")
        # Do not follow a symlink whose target might escape the owned directory.
        if artifact.is_symlink():
            raise ValueError("refusing SQLite cleanup through symlink")
        try:
            artifact.unlink()
        except FileNotFoundError:
            pass
