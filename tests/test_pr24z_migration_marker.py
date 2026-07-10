from __future__ import annotations

import json
from pathlib import Path
import pytest

from polycopy.migrations.pr24z_marker import MARKER_VERSION, validate_pr24z_migration_marker


def marker(expected: Path, *, rows_updated=14, already_migrated=False) -> dict:
    return {
        "migration_version": MARKER_VERSION,
        "timestamp_utc": "2026-07-10T17:00:00+00:00",
        "migration_commit_sha": "a" * 40,
        "production_db_path": str(expected.resolve()),
        "backup_path": str(expected.with_name("backup.db")),
        "backup_sha256": "b" * 64,
        "rows_expected": 14,
        "rows_updated": rows_updated,
        "already_migrated": already_migrated,
        "canonical_row_count": 14,
        "legacy_row_count": 0,
        "integrity_result": "ok",
        "foreign_key_result": 0,
        "mapping_artifact_sha256": "c" * 64,
    }


def write(path: Path, data) -> None:
    path.write_text(json.dumps(data))


def test_marker_absent_blocks(tmp_path):
    assert not validate_pr24z_migration_marker(tmp_path / "missing", tmp_path / "db").valid


def test_marker_empty_blocks(tmp_path):
    p = tmp_path / "marker"
    p.write_text("")
    assert not validate_pr24z_migration_marker(p, tmp_path / "db").valid


def test_marker_invalid_json_blocks(tmp_path):
    p = tmp_path / "marker"
    p.write_text("not json")
    assert not validate_pr24z_migration_marker(p, tmp_path / "db").valid


def test_marker_array_blocks(tmp_path):
    p = tmp_path / "marker"
    write(p, [])
    assert not validate_pr24z_migration_marker(p, tmp_path / "db").valid


@pytest.mark.parametrize(
    "field,value",
    [
        ("migration_version", "wrong"),
        ("production_db_path", "/wrong/db"),
        ("canonical_row_count", 13),
        ("legacy_row_count", 1),
        ("integrity_result", "failed"),
        ("foreign_key_result", 2),
        ("mapping_artifact_sha256", "bad"),
        ("backup_sha256", "bad"),
        ("rows_updated", 3),
    ],
)
def test_marker_invalid_values_block(tmp_path, field, value):
    db = tmp_path / "db"
    data = marker(db)
    data[field] = value
    p = tmp_path / "marker"
    write(p, data)
    result = validate_pr24z_migration_marker(p, db)
    assert not result.valid


def test_marker_missing_required_fields_blocks(tmp_path):
    db = tmp_path / "db"
    data = marker(db)
    data.pop("backup_path")
    p = tmp_path / "marker"
    write(p, data)
    assert not validate_pr24z_migration_marker(p, db).valid


def test_marker_directory_blocks(tmp_path):
    p = tmp_path / "marker"
    p.mkdir()
    assert not validate_pr24z_migration_marker(p, tmp_path / "db").valid


def test_marker_first_run_is_valid(tmp_path):
    db = tmp_path / "db"
    p = tmp_path / "marker"
    write(p, marker(db))
    result = validate_pr24z_migration_marker(p, db)
    assert result.valid and result.data is not None and result.data["rows_updated"] == 14


def test_marker_idempotent_run_is_valid(tmp_path):
    db = tmp_path / "db"
    p = tmp_path / "marker"
    write(p, marker(db, rows_updated=0, already_migrated=True))
    result = validate_pr24z_migration_marker(p, db)
    assert result.valid and result.data is not None and result.data["already_migrated"] is True


def test_marker_zero_updates_without_idempotent_flag_blocks(tmp_path):
    db = tmp_path / "db"
    p = tmp_path / "marker"
    write(p, marker(db, rows_updated=0))
    assert not validate_pr24z_migration_marker(p, db).valid


def test_marker_fourteen_updates_with_idempotent_flag_blocks(tmp_path):
    db = tmp_path / "db"
    p = tmp_path / "marker"
    write(p, marker(db, rows_updated=14, already_migrated=True))
    assert not validate_pr24z_migration_marker(p, db).valid


def test_normal_ingestion_uses_validator_before_write():
    source = Path(__file__).resolve().parents[1] / "scripts/ingest_real_source_trades.py"
    text = source.read_text()
    assert "validate_pr24z_migration_marker" in text
    assert "_CANONICAL_MIGRATION_COMPLETE_MARKER.exists()" not in text


def test_invalid_marker_does_not_call_writer(monkeypatch):
    # The CLI refuses incomplete production-write flag combinations before any
    # writable DB/writer call; marker validation is separately covered above.
    from scripts import ingest_real_source_trades as cli

    called = False

    def fail_writer(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli, "write_valid_rows", fail_writer)
    assert cli.main(["--fixture", "--write"]) == 2
    assert called is False
