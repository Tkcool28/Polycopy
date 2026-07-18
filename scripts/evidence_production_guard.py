"""Shared production-DB guard for the research evidence CLIs.

Every new write CLI must refuse BOTH recognized production paths unless the
full explicit production gate is supplied. We compare canonical (resolved)
paths, never raw strings, so a symlink pointing at the production DB or a
repo-relative path is also caught.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Recognized production DB locations (resolved at module load).
REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_DB_REPO_RELATIVE = (REPO_ROOT / "data" / "polycopy.db").resolve()
PRODUCTION_DB_ABSOLUTE = Path("/root/Polycopy/data/polycopy.db").resolve()


def resolve_db_path(db_path: str) -> Path:
    """Resolve a possibly-relative/symlinked db path to its canonical form."""
    try:
        return Path(db_path).resolve()
    except OSError:
        return Path(db_path)


def is_production_db(db_path: str) -> bool:
    try:
        return resolve_db_path(db_path) in (
            PRODUCTION_DB_REPO_RELATIVE,
            PRODUCTION_DB_ABSOLUTE,
        )
    except OSError:
        return False


def require_write(args: Any) -> bool:
    """Return True iff the write is permitted.

    Fail-closed: a write is allowed only when NOT pointing at production, OR
    when writing to production WITH both --write and --confirm-production-db.
    Dry-run (no --write) is always a non-write.
    """
    if getattr(args, "dry_run", False):
        return True
    if not getattr(args, "write", False):
        return False
    if is_production_db(args.db_path):
        if not getattr(args, "confirm_production_db", False):
            return False
    return True
