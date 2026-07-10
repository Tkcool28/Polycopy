"""Neutral PR24Z migration-marker schema and fail-closed validator."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MARKER_VERSION = "pr24z_canonical_identity_v1"
REQUIRED_MARKER_FIELDS = frozenset({
    "migration_version", "timestamp_utc", "migration_commit_sha", "production_db_path",
    "backup_path", "backup_sha256", "rows_expected", "rows_updated", "already_migrated",
    "canonical_row_count", "legacy_row_count", "integrity_result", "foreign_key_result",
    "mapping_artifact_sha256",
})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

@dataclass(frozen=True)
class MarkerValidationResult:
    valid: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    data: dict[str, Any] | None = None


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_pr24z_migration_marker(marker_path: str | Path, expected_db_path: str | Path) -> MarkerValidationResult:
    """Validate the complete structured marker; never trust existence alone."""
    path = Path(marker_path)
    expected = Path(expected_db_path).resolve()
    reasons: list[str] = []
    if not path.exists():
        return MarkerValidationResult(False, ("marker_missing",))
    if not path.is_file():
        return MarkerValidationResult(False, ("marker_not_regular_file",))
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeError):
        return MarkerValidationResult(False, ("marker_unreadable",))
    except json.JSONDecodeError:
        return MarkerValidationResult(False, ("marker_invalid_json",))
    if not isinstance(data, dict):
        return MarkerValidationResult(False, ("marker_not_object",))
    missing = sorted(REQUIRED_MARKER_FIELDS - data.keys())
    if missing:
        reasons.append("missing_fields:" + ",".join(missing))
    if data.get("migration_version") != MARKER_VERSION:
        reasons.append("unsupported_migration_version")
    if not isinstance(data.get("production_db_path"), str) or Path(data.get("production_db_path", "")).resolve() != expected:
        reasons.append("wrong_production_db_path")
    if data.get("backup_path") in (None, "") or not isinstance(data.get("backup_path"), str):
        reasons.append("backup_path_missing")
    if not isinstance(data.get("timestamp_utc"), str) or not data.get("timestamp_utc"):
        reasons.append("timestamp_missing")
    else:
        try:
            datetime.fromisoformat(data["timestamp_utc"].replace("Z", "+00:00"))
        except ValueError:
            reasons.append("timestamp_invalid")
    if not isinstance(data.get("migration_commit_sha"), str) or not _COMMIT_RE.fullmatch(data.get("migration_commit_sha", "")):
        reasons.append("migration_commit_sha_invalid")
    for name in ("backup_sha256", "mapping_artifact_sha256"):
        if not isinstance(data.get(name), str) or not _SHA256_RE.fullmatch(data.get(name, "")):
            reasons.append(name + "_invalid")
    for name, value in (("rows_expected", data.get("rows_expected")), ("canonical_row_count", data.get("canonical_row_count")), ("legacy_row_count", data.get("legacy_row_count")), ("foreign_key_result", data.get("foreign_key_result"))):
        if not _int(value):
            reasons.append(name + "_type_invalid")
    if data.get("rows_expected") != 14:
        reasons.append("rows_expected_invalid")
    if data.get("canonical_row_count") != 14:
        reasons.append("canonical_count_invalid")
    if data.get("legacy_row_count") != 0:
        reasons.append("legacy_count_invalid")
    if data.get("integrity_result") != "ok":
        reasons.append("integrity_invalid")
    if data.get("foreign_key_result") != 0:
        reasons.append("foreign_key_invalid")
    if not isinstance(data.get("already_migrated"), bool):
        reasons.append("already_migrated_type_invalid")
    if not _int(data.get("rows_updated")) or data.get("rows_updated") not in (0, 14):
        reasons.append("rows_updated_invalid")
    elif data["rows_updated"] == 0 and data.get("already_migrated") is not True:
        reasons.append("idempotent_marker_flag_invalid")
    elif data["rows_updated"] == 14 and data.get("already_migrated") is not False:
        reasons.append("first_run_marker_flag_invalid")
    return MarkerValidationResult(not reasons, tuple(reasons), data)
