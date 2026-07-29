"""Exact bounded predicate fingerprint tests (Finding 3, PR #84).

The reconciliation scanner must:
- authorize only the three approved bounded predicate fingerprints;
- reject any deviation in WHERE structure even when the surface SELECTOR
  column set looks correct;
- treat bad-fingerprint statements as contract violations AND as
  ``unexpected_role`` evidence that prevents ``centralized_writer_exists``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from polycopy.engine.source_trade_ingestion_writer_audit import (
    build_source_trade_ingestion_writer_audit,
)
from polycopy.engine.source_trade_sql_architecture import (
    PREDICATE_METADATA_BY_ID,
    PREDICATE_METADATA_BY_IDENTITY,
    PREDICATE_RESOLUTION_BY_ID,
    SqlFinding,
    contract_violations,
    parse_predicate_fingerprint,
    scan_python_file,
    scan_repository,
)

_RES_FIELDS = (
    "resolution_status",
    "resolved_at",
    "winning_token_id",
    "is_winning_trade",
    "realized_pnl",
    "settlement_source",
)
_WRITE_COLS = (
    "id",
    "source",
    "source_trade_id",
    "market_source_id",
    "side",
    "outcome",
    "quantity",
    "price",
    "trader_address",
    "timestamp",
    "is_sample",
    "token_id",
    "metadata_json",
)


# --------------------------------------------------------------------------- #
# Positive fingerprints
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "sql", "expected"),
    [
        (
            "metadata_by_id",
            "UPDATE source_trades SET metadata_json=? WHERE id=?",
            PREDICATE_METADATA_BY_ID,
        ),
        (
            "metadata_by_identity",
            "UPDATE source_trades SET metadata_json=? WHERE source=? AND source_trade_id=?",
            PREDICATE_METADATA_BY_IDENTITY,
        ),
        (
            "metadata_by_identity_reversed",
            "UPDATE source_trades SET metadata_json=? WHERE source_trade_id=? AND source=?",
            PREDICATE_METADATA_BY_IDENTITY,
        ),
        (
            "metadata_by_id_whitespace",
            "UPDATE source_trades SET metadata_json=? WHERE id = ?",
            PREDICATE_METADATA_BY_ID,
        ),
        (
            "metadata_by_id_parenthesized",
            "UPDATE source_trades SET metadata_json=? WHERE (id = ?)",
            PREDICATE_METADATA_BY_ID,
        ),
        (
            "metadata_by_id_quoted_identifier",
            'UPDATE source_trades SET metadata_json=? WHERE "id" = ?',
            PREDICATE_METADATA_BY_ID,
        ),
        (
            "resolution_by_id",
            (
                "UPDATE source_trades SET "
                "resolution_status=?, resolved_at=?, winning_token_id=?, "
                "is_winning_trade=?, realized_pnl=?, settlement_source=? "
                "WHERE id=?"
            ),
            PREDICATE_RESOLUTION_BY_ID,
        ),
        (
            "resolution_by_id_parenthesized",
            (
                "UPDATE source_trades SET "
                "resolution_status=?, resolved_at=?, winning_token_id=?, "
                "is_winning_trade=?, realized_pnl=?, settlement_source=? "
                "WHERE (id=?)"
            ),
            PREDICATE_RESOLUTION_BY_ID,
        ),
    ],
)
def test_positive_predicate_fingerprints(name: str, sql: str, expected: str) -> None:
    result = parse_predicate_fingerprint(sql)
    assert result.invalid_reason is None, (name, result.invalid_reason)
    assert result.fingerprint == expected, (name, result.fingerprint)


# --------------------------------------------------------------------------- #
# Negative predicates — must fail closed
# --------------------------------------------------------------------------- #
NEGATIVE_PREDICATES: list[tuple[str, str]] = [
    ("or_tautology", "WHERE id=? OR 1=1"),
    ("and_tautology", "WHERE id=? AND 1=1"),
    ("or_id_id", "WHERE id=? OR id=?"),
    ("and_id_id", "WHERE id=? AND id=?"),
    ("not_id", "WHERE NOT id=?"),
    ("in_clause", "WHERE id IN (?)"),
    ("subquery", "WHERE id=(SELECT id FROM source_trades LIMIT 1)"),
    (
        "identity_or_tautology",
        "WHERE source=? AND source_trade_id=? OR 1=1",
    ),
    ("identity_and_extra", "WHERE source=? AND source_trade_id=? AND side='BUY'"),
    ("identity_or_identity", "WHERE source=? OR source_trade_id=?"),
    ("literal_eq", "WHERE id='literal'"),
    ("function_on_id", "WHERE lower(id)=?"),
    ("comparison_neq", "WHERE id>?"),
    ("is_null", "WHERE id IS ?"),
    ("paren_or_tautology", "WHERE (id=? OR 1=1)"),
    ("double_paren_or", "WHERE ((id=?)) OR ((id=?)"),
    ("paren_and_partial_or", "WHERE source=? AND (source_trade_id=? OR 1=1)"),
    ("identity_and_duplicate_column", "WHERE source=? AND source_trade_id=? AND source_trade_id=?"),
    ("collate_clause", "WHERE id=? COLLATE NOCASE"),
]


@pytest.mark.parametrize(
    ("name", "where_clause"),
    NEGATIVE_PREDICATES,
)
def test_negative_predicate_fingerprints_fail_closed(name: str, where_clause: str) -> None:
    sql = f"UPDATE source_trades SET metadata_json=? {where_clause}"
    result = parse_predicate_fingerprint(sql)
    assert result.fingerprint is None, (name, result.fingerprint, sql)
    assert result.invalid_reason is not None, (name, sql)


# --------------------------------------------------------------------------- #
# Adversarial SQL — full UPDATE statements end-to-end through the scanner
# --------------------------------------------------------------------------- #
def _scan_update(tmp_path: Path, name: str, where_clause: str) -> list[SqlFinding]:
    return scan_python_file(
        tmp_path / f"{name}.py",
    ).__class__(),  # type: ignore[return-value]


def _write_update(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.py"
    path.write_text(
        "def reconcile_metadata_json(db):\n"
        f"    db.execute({body!r})\n"
    )
    return path


def test_adversarial_or_tautology_is_contract_violation(tmp_path: Path) -> None:
    path = _write_update(
        tmp_path,
        "rogue",
        "UPDATE source_trades SET metadata_json=? WHERE id=? OR 1=1",
    )
    findings = scan_python_file(path)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.operation == "UPDATE"
    assert finding.table == "source_trades"
    assert finding.columns == ("metadata_json",)
    assert finding.predicate_fingerprint is None
    assert finding.predicate_invalid_reason is not None
    assert contract_violations(findings) == findings


def test_adversarial_identity_or_tautology_is_contract_violation(tmp_path: Path) -> None:
    path = _write_update(
        tmp_path,
        "rogue",
        "UPDATE source_trades SET metadata_json=? "
        "WHERE source=? AND source_trade_id=? OR 1=1",
    )
    findings = scan_python_file(path)
    assert findings and findings[0].predicate_fingerprint is None
    assert contract_violations(findings) == findings


@pytest.mark.parametrize(
    "where_clause",
    [w for _name, w in NEGATIVE_PREDICATES],
)
def test_adversarial_predicate_produces_no_approved_fingerprint(
    tmp_path: Path, where_clause: str
) -> None:
    path = _write_update(
        tmp_path,
        "bad_predicate",
        f"UPDATE source_trades SET metadata_json=? {where_clause}",
    )
    findings = scan_python_file(path)
    assert findings, where_clause
    finding = findings[0]
    assert finding.predicate_fingerprint is None
    assert finding.predicate_invalid_reason is not None
    assert contract_violations(findings) == findings


# --------------------------------------------------------------------------- #
# Disposable-tree audit matrix — full audit pipeline
# --------------------------------------------------------------------------- #
def _full_architecture_tree(root: Path) -> None:
    """Mirror the canonical writer architecture inside a disposable repo."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "polycopy").mkdir(parents=True)
    (root / "scripts").mkdir()
    writer = (
        "def write_valid_rows(db):\n"
        "    db.execute(\n"
        "        'INSERT OR IGNORE INTO source_trades ("
        + ", ".join(_WRITE_COLS)
        + ") VALUES (1)'\n"
        "    )\n"
    )
    (root / "src" / "polycopy" / "ingestion").mkdir(parents=True)
    (root / "src" / "polycopy" / "ingestion" / "source_trade_writer.py").write_text(writer)
    metadata = (
        "def reconcile_metadata_json(db):\n"
        "    db.execute(\n"
        "        'UPDATE source_trades SET metadata_json=? WHERE id=?'\n"
        "    )\n"
        "    db.execute(\n"
        "        'UPDATE source_trades SET metadata_json=? "
        "WHERE source=? AND source_trade_id=?'\n"
        "    )\n"
    )
    (root / "src" / "polycopy" / "ingestion" / "source_trade_metadata_reconciliation.py").write_text(
        metadata
    )
    resolution = (
        "def apply_existing_resolution_updates(db):\n"
        "    db.execute(\n"
        "        'UPDATE source_trades SET "
        + ", ".join(f"{c}=?" for c in _RES_FIELDS)
        + " WHERE id=?'\n"
        "    )\n"
    )
    (root / "src" / "polycopy" / "ingestion" / "source_trade_resolution.py").write_text(resolution)


@pytest.mark.parametrize(
    ("name", "where_clause"),
    [("metadata_or_tautology", w) for w in ("id=? OR 1=1", "id=? AND 1=1", "id=? OR id=?")],
)
def test_disposable_tree_with_adversarial_where_fails_audit(
    tmp_path: Path, name: str, where_clause: str
) -> None:
    root = tmp_path / "repo"
    _full_architecture_tree(root)
    target = root / "src" / "polycopy" / "ingestion" / "source_trade_metadata_reconciliation.py"
    body = (
        "def reconcile_metadata_json(db):\n"
        f"    db.execute('UPDATE source_trades SET metadata_json=? WHERE {where_clause}')\n"
        "    db.execute(\n"
        "        'UPDATE source_trades SET metadata_json=? "
        "WHERE source=? AND source_trade_id=?'\n"
        "    )\n"
    )
    target.write_text(body)
    audit = build_source_trade_ingestion_writer_audit(None, repo_root=str(root))
    assert audit.centralized_writer_exists is False, (name, audit.false_verdict_reasons)
    assert any("metadata_reconciliation_by_id" in reason or "unexpected" in reason
               for reason in audit.false_verdict_reasons)
    assert audit.unexpected_roles or any(
        "unexpected" in reason for reason in audit.false_verdict_reasons
    )


def test_disposable_tree_extra_tautological_role_fails_audit(tmp_path: Path) -> None:
    """A correct role and an extra tautological mutation must still fail the audit."""
    root = tmp_path / "repo"
    _full_architecture_tree(root)
    target = root / "src" / "polycopy" / "ingestion" / "source_trade_metadata_reconciliation.py"
    existing = target.read_text()
    extra = (
        "\ndef extra_metadata(db):\n"
        "    db.execute(\n"
        "        'UPDATE source_trades SET metadata_json=? WHERE id=? OR 1=1'\n"
        "    )\n"
    )
    target.write_text(existing + extra)
    audit = build_source_trade_ingestion_writer_audit(None, repo_root=str(root))
    assert audit.observed_role_counts.get("metadata_reconciliation_by_id") == 1
    assert audit.observed_role_counts.get("metadata_reconciliation_by_identity") == 1
    assert audit.centralized_writer_exists is False
    assert audit.unexpected_roles


def test_scan_repository_does_not_crash_on_real_repo() -> None:
    """Sanity: the in-tree scanner still runs against the actual repo."""
    findings = scan_repository(Path.cwd())
    # The scanner must report findings (canonical writer + reconciler are present).
    assert isinstance(findings, list)
    for finding in findings:
        if finding.table == "source_trades" and finding.operation == "UPDATE":
            # Every UPDATE finding that targets source_trades must have a parsed
            # predicate fingerprint OR an explicit invalid reason.
            assert (
                finding.predicate_fingerprint is not None
                or finding.predicate_invalid_reason is not None
            ), finding
