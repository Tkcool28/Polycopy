"""Regression tests for preserving market IDs across repeated ingestion."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT))

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.market_persistence import persist_market_preserving_identity  # noqa: E402
from polycopy.domain.market import Market, MarketOutcome  # noqa: E402

run_scan = importlib.import_module("scripts.run_scan")
collect_smart_money_data = importlib.import_module("scripts.collect_smart_money_data")


def _market(
    *,
    source_id: str = "condition-1",
    source: str = "polymarket",
    question: str = "Will X happen?",
    outcomes: list[tuple[str, float, float]] | None = None,
    active: bool = True,
    closed: bool = False,
    resolved: bool = False,
    resolution_outcome: str | None = None,
    volume_24h: float = 123.0,
) -> Market:
    if outcomes is None:
        outcomes = [("Yes", 0.6, 10.0), ("No", 0.4, 5.0)]
    return Market(
        source_id=source_id,
        source=source,
        question=question,
        outcomes=[MarketOutcome(label=label, price=price, volume=volume) for label, price, volume in outcomes],
        active=active,
        closed=closed,
        resolved=resolved,
        resolution_outcome=resolution_outcome,
        volume_24h=volume_24h,
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_sample=False,
    )


@pytest.fixture
def db(tmp_path: Path):
    database = Database(db_path=tmp_path / "p38-market-upsert.db").connect()
    try:
        yield database
    finally:
        database.close()


def _market_row(db: Database, source: str = "polymarket", source_id: str = "condition-1"):
    return db.fetchone(
        "SELECT * FROM markets WHERE source = ? AND source_id = ?",
        (source, source_id),
    )


def _outcomes(db: Database, market_id: str) -> list[tuple[str, float, float]]:
    rows = db.fetchall(
        "SELECT label, price, volume FROM market_outcomes WHERE market_id = ? ORDER BY label",
        (market_id,),
    )
    return [(row["label"], float(row["price"]), float(row["volume"])) for row in rows]


def _fk_check(db: Database) -> list:
    return db.fetchall("PRAGMA foreign_key_check")


def test_new_market_insert_uses_generated_market_id_and_persists_outcomes(db: Database):
    market = _market()

    persisted_id = persist_market_preserving_identity(db, market)

    assert persisted_id == str(market.id)
    row = _market_row(db)
    assert row is not None
    assert row["id"] == str(market.id)
    assert row["question"] == "Will X happen?"
    assert _outcomes(db, persisted_id) == [("No", 0.4, 5.0), ("Yes", 0.6, 10.0)]
    assert _fk_check(db) == []


def test_repeated_market_upsert_preserves_parent_id_and_refreshes_outcomes(db: Database):
    first = _market(outcomes=[("Yes", 0.7, 11.0), ("No", 0.3, 7.0)])
    second = _market(
        question="Updated question",
        outcomes=[("Yes", 0.2, 22.0), ("Maybe", 0.8, 33.0)],
        active=False,
        closed=True,
        resolved=True,
        resolution_outcome="Maybe",
        volume_24h=456.0,
    )
    assert first.id != second.id

    first_id = persist_market_preserving_identity(db, first)
    second_id = persist_market_preserving_identity(db, second)

    assert second_id == first_id == str(first.id)
    assert second_id != str(second.id)
    assert db.fetchone("SELECT COUNT(*) AS count FROM markets")["count"] == 1

    row = _market_row(db)
    count = db.fetchone("SELECT COUNT(*) AS count FROM markets")
    assert row is not None
    assert count is not None
    assert row["id"] == first_id
    assert row["question"] == "Updated question"
    assert row["active"] == 0
    assert row["closed"] == 1
    assert row["resolved"] == 1
    assert row["resolution_outcome"] == "Maybe"
    assert float(row["volume_24h"]) == pytest.approx(456.0)

    # The old "No" outcome is gone, the refreshed outcomes point at the preserved parent ID.
    assert _outcomes(db, first_id) == [("Maybe", 0.8, 33.0), ("Yes", 0.2, 22.0)]
    outcome_count = db.fetchone("SELECT COUNT(*) AS count FROM market_outcomes")
    assert outcome_count is not None
    assert outcome_count["count"] == 2
    assert _fk_check(db) == []


def test_run_scan_persist_market_wrapper_preserves_identity(db: Database):
    first = _market(source_id="run-scan-condition", outcomes=[("Yes", 0.55, 1.0)])
    second = _market(
        source_id="run-scan-condition",
        question="run_scan refreshed",
        outcomes=[("No", 0.45, 2.0)],
    )

    run_scan._persist_market(db, first)  # noqa: SLF001 - regression target
    run_scan._persist_market(db, second)  # noqa: SLF001 - regression target

    row = _market_row(db, source_id="run-scan-condition")
    assert row is not None
    assert row["id"] == str(first.id)
    assert row["id"] != str(second.id)
    assert row["question"] == "run_scan refreshed"
    assert _outcomes(db, str(first.id)) == [("No", 0.45, 2.0)]
    assert _fk_check(db) == []


def test_collect_smart_money_persist_market_wrapper_preserves_identity(db: Database):
    collector = collect_smart_money_data.PolymarketCollector(settings=object())
    first = _market(source_id="collector-condition", outcomes=[("Yes", 0.51, 1.0)])
    second = _market(
        source_id="collector-condition",
        question="collector refreshed",
        outcomes=[("No", 0.49, 2.0)],
    )

    collector._persist_market(db, first)  # noqa: SLF001 - regression target
    collector._persist_market(db, second)  # noqa: SLF001 - regression target

    row = _market_row(db, source_id="collector-condition")
    assert row is not None
    assert row["id"] == str(first.id)
    assert row["id"] != str(second.id)
    assert row["question"] == "collector refreshed"
    assert _outcomes(db, str(first.id)) == [("No", 0.49, 2.0)]
    assert _fk_check(db) == []


def test_old_insert_or_replace_pattern_can_orphan_outcomes_when_fk_enforcement_is_off(db: Database):
    """Local simulation proves why replacing the parent UUID is unsafe."""
    first = _market(source_id="old-behavior", outcomes=[("Yes", 0.6, 1.0)])
    second = _market(source_id="old-behavior", outcomes=[("No", 0.4, 2.0)])

    db.conn.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    for market in (first, second):
        db.execute(
            """INSERT OR REPLACE INTO markets
               (id, source_id, source, question, active, closed, resolved,
                resolution_outcome, volume_24h, fetched_at, is_sample)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(market.id),
                market.source_id,
                market.source,
                market.question,
                int(market.active),
                int(market.closed),
                int(market.resolved),
                market.resolution_outcome,
                market.volume_24h,
                market.fetched_at.isoformat(),
                int(market.is_sample),
            ),
        )
        db.execute("DELETE FROM market_outcomes WHERE market_id = ?", (str(market.id),))
        for outcome in market.outcomes:
            db.execute(
                "INSERT INTO market_outcomes (market_id, label, price, volume) VALUES (?, ?, ?, ?)",
                (str(market.id), outcome.label, outcome.price, outcome.volume),
            )
    db.conn.commit()

    row = _market_row(db, source_id="old-behavior")
    assert row is not None
    assert row["id"] == str(second.id)
    assert db.fetchall("PRAGMA foreign_key_check") != []
