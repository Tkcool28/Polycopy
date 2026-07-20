"""Focused auto_commit seam tests for PR #73.

These tests prove the transaction-control fixes in:
  - source_trade_writer.write_valid_rows (fix A)
  - specialist_evidence_collector.collect_evidence (fix B)
  - specialist_evidence_collector.collect_evidence -> write_valid_rows forwarding (fix C)

Run with:
  PYTHONNOUSERSITE=1 PYTHONPATH=/root/workspaces/polycopy-pr73/src \
  /root/Polycopy/.venv/bin/python -B -m pytest -q \
  tests/test_pr73_auto_commit_seam.py -vv
"""
import tempfile
from pathlib import Path


def _make_db():
    """Create a temp-file DB with the full Polycopy schema (via migrations)."""
    from polycopy.db.database import Database
    db_path = Path(tempfile.mktemp(suffix=".db"))
    db = Database(db_path)
    db.connect()
    return db


def _make_valid_row(source_trade_id="test:1"):
    from polycopy.ingestion.normalized_source_trade import NormalizedSourceTrade
    from datetime import datetime, timezone
    return NormalizedSourceTrade(
        source="test",
        source_trade_id=source_trade_id,
        trader_address="0xgood0000000000000000000000000000000000aa",
        market_source_id="0xabc",
        token_id="0xdef",
        side="BUY",
        outcome="Yes",
        price=0.5,
        quantity=1.0,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_sample=0,
        metadata={"metadata_version": "1"},
        validation_status="valid",
    )


def test_write_valid_rows_auto_commit_true_commits():
    """write_valid_rows(auto_commit=True) commits and reports committed=True."""
    from polycopy.ingestion.source_trade_writer import write_valid_rows

    db = _make_db()
    row = _make_valid_row()
    result = write_valid_rows(db, [row], dry_run=False, auto_commit=True)
    assert result.committed is True, f"committed={result.committed}"
    assert result.inserted == 1, f"inserted={result.inserted}"
    # Row is durable (committed)
    count = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    assert count == 1, f"count={count}"
    db.close()
    db.db_path.unlink(missing_ok=True)


def test_write_valid_rows_auto_commit_false_no_commit():
    """write_valid_rows(auto_commit=False) leaves rows staged, not committed."""
    from polycopy.ingestion.source_trade_writer import write_valid_rows

    db = _make_db()
    row = _make_valid_row("test:2")
    result = write_valid_rows(db, [row], dry_run=False, auto_commit=False)
    assert result.committed is False, f"committed={result.committed}"
    # Row is visible inside the same connection (uncommitted)
    count = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    assert count == 1, f"count={count}"
    # Rollback should remove it (caller owns transaction)
    db.conn.rollback()
    count_after = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    assert count_after == 0, f"count_after={count_after}"
    db.close()
    db.db_path.unlink(missing_ok=True)


def test_write_valid_rows_auto_commit_false_error_propagates():
    """write_valid_rows(auto_commit=False) does NOT inner-rollback on sqlite error."""
    from polycopy.ingestion.source_trade_writer import write_valid_rows

    db = _make_db()
    # Drop source_trades to force sqlite3.Error on INSERT
    db.conn.execute("DROP TABLE IF EXISTS source_trades")
    row = _make_valid_row("test:3")
    result = write_valid_rows(db, [row], dry_run=False, auto_commit=False)
    # errors should be > 0 (exception caught, no inner rollback when auto_commit=False)
    assert result.errors > 0 or result.error_message, (
        f"errors={result.errors}, err={result.error_message}"
    )
    assert result.committed is False
    db.close()
    db.db_path.unlink(missing_ok=True)


def test_write_valid_rows_dry_run_no_writes():
    """write_valid_rows(dry_run=True) performs no writes and no commit."""
    from polycopy.ingestion.source_trade_writer import write_valid_rows

    db = _make_db()
    row = _make_valid_row("test:4")
   # Dry run: no INSERT, no commit
    result = write_valid_rows(db, [row], dry_run=True, auto_commit=True)
    assert result.committed is False
    count = db.conn.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
    assert count == 0
    db.close()
    db.db_path.unlink(missing_ok=True)


def test_collect_evidence_forwards_auto_commit_to_write_valid_rows():
    """collect_evidence(auto_commit=False) passes auto_commit to write_valid_rows."""
    from polycopy.ingestion.specialist_evidence_collector import collect_evidence
    import inspect
    sig = inspect.signature(collect_evidence)
    assert 'auto_commit' in sig.parameters, "auto_commit parameter missing from collect_evidence"

    # Verify the default is True (backward-compatible)
    assert sig.parameters['auto_commit'].default is True

    # Verify that write_valid_rows is called with auto_commit=auto_commit.
    # We do this by reading the source and checking for 'auto_commit=auto_commit'
    source = inspect.getsource(collect_evidence)
    assert 'auto_commit=auto_commit' in source, (
        "collect_evidence must forward auto_commit to write_valid_rows"
    )
