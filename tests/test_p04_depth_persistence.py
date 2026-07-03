"""Tests for bounded order-book depth-level persistence (PR 4, Phase 6).

Covers:
- first successful persistence (correct bid + ask row counts)
- retrieval equality (round-trip of normalized bounded levels)
- identical idempotent repeat (same hash → no writes, success)
- mismatched repeat (different bounded book → DEPTH_SNAPSHOT_MISMATCH)
- forced insert failure rollback (no partial book remains)
- missing parent snapshot FK failure
- old snapshot with no levels (DEPTH_NOT_CAPTURED)
- malformed existing rows (DEPTH_LEVELS_MALFORMED)
- noncontiguous indexes
- inconsistent cumulative size / notional
- one-sided stored corruption
- hash comparison
- exact row counts
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polycopy.db.database import Database
from polycopy.db.levels_persistence import (
    DEPTH_LEVELS_MALFORMED,
    DEPTH_NOT_CAPTURED,
    DEPTH_SNAPSHOT_MISMATCH,
    compute_trade_fill_from_depth,
    get_depth_levels_for_snapshot,
    get_latest_depth_levels_for_candidate,
    has_snapshot_levels,
    persist_depth_levels,
)


# ── Test helpers ───────────────────────────────────────────────────────────


def _make_db(tmp_path) -> Database:
    """Yield a fresh v10-schema DB with a candidate + snapshot row
    pre-inserted so FK constraints on the levels table are satisfied.
    """
    db_path = tmp_path / "depth_test.db"
    db = Database(db_path=db_path)
    db.connect()
    # Minimal wallets row (referenced by copy_candidates via v8 schema).
    db.execute(
        """INSERT INTO wallets (id, address, label, is_sample, created_at)
           VALUES ('wallet-1', '0xabc', 'test', 1, '2026-07-03T00:00:00Z')""",
    )
    # Minimal copy_candidates row (referenced by candidate_price_snapshots).
    db.execute(
        """INSERT INTO copy_candidates (
               wallet_id, source, source_trade_id,
               side, source_trade_price, source_trade_quantity,
               source_trade_timestamp, observed_at,
               wallet_score_version, wallet_score, wallet_verdict,
               status, created_at, updated_at
           ) VALUES (
               'wallet-1', 'test-source', 'trade-1',
               'BUY', 0.50, 10.0,
               '2026-07-03T00:00:00Z', '2026-07-03T00:00:00Z',
               '1', 0.0, 'incomplete',
               'pending', '2026-07-03T00:00:00Z', '2026-07-03T00:00:00Z'
           )""",
    )
    return db


def _add_snapshot(db: Database, snapshot_id: str = "snap-1") -> None:
    """Insert a minimal OK snapshot row (FK target for levels)."""
    db.execute(
        """INSERT INTO candidate_price_snapshots (
               id, candidate_id, snapshot_run_id, fetch_status,
               fetch_endpoint, fetch_http_status, fetch_latency_ms,
               request_attempts,
               side, source_trade_price, source_trade_quantity,
               source_trade_timestamp,
               fetched_at, created_at
           ) VALUES (
               ?, 1, 'run-1', 'OK',
               'https://clob.example/book', 200, 50,
               1,
               'BUY', 0.50, 10.0,
               '2026-07-03T00:00:00Z',
               '2026-07-03T00:00:00Z', '2026-07-03T00:00:00Z'
           )""",
        (snapshot_id,),
    )


@pytest.fixture
def db_with_snapshot(tmp_path) -> Database:
    db = _make_db(tmp_path)
    _add_snapshot(db, "snap-1")
    return db


# ── First persistence ──────────────────────────────────────────────────────


class TestFirstPersistence:
    """Case A: no existing rows → INSERT all levels, commit, verify."""

    def test_first_persist_returns_correct_counts(self, db_with_snapshot):
        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10"), ("0.49", "20")],
            raw_asks=[("0.51", "10"), ("0.52", "20")],
        )
        assert err is None
        assert bids == 2
        assert asks == 2

    def test_first_persist_stores_all_rows(self, db_with_snapshot):
        persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10"), ("0.49", "20")],
            raw_asks=[("0.51", "10"), ("0.52", "20")],
        )
        rows = db_with_snapshot.fetchall(
            "SELECT side, level_index, price, size FROM "
            "candidate_price_snapshot_levels WHERE snapshot_id = ? "
            "ORDER BY side, level_index",
            ("snap-1",),
        )
        assert len(rows) == 4
        sides = [r["side"] for r in rows]
        assert sides.count("BID") == 2
        assert sides.count("ASK") == 2

    def test_first_persist_retrieval_equality(self, db_with_snapshot):
        """Persisted levels must round-trip exactly via Decimal equality."""
        raw_bids = [("0.50", "10"), ("0.49", "20")]
        raw_asks = [("0.51", "10"), ("0.52", "20")]
        persist_depth_levels(
            db_with_snapshot, "snap-1", raw_bids, raw_asks,
        )
        bids, asks = get_depth_levels_for_snapshot(
            db_with_snapshot, "snap-1",
        )
        assert len(bids) == 2
        assert len(asks) == 2
        assert bids[0].price == Decimal("0.50")
        assert bids[0].size == Decimal("10")
        assert bids[1].price == Decimal("0.49")
        assert asks[0].price == Decimal("0.51")
        # cumulative values are populated by the inserter
        assert bids[0].cumulative_notional == Decimal("5")  # 0.50 * 10
        assert bids[1].cumulative_notional == Decimal("14.8")  # 5 + 0.49*20


# ── First-level truncation (Phase 5 correction) ────────────────────────────


class TestFirstLevelTruncation:
    """The first level must be subject to the same max_notional cap
    as every other level. If a single level's notional alone exceeds
    the cap, it is truncated to fit exactly.
    """

    def test_first_level_truncated_to_fit_cap(self, db_with_snapshot):
        """A single oversized level is truncated so its cumulative
        notional equals exactly max_notional.
        """
        # Level 1 notional = 0.90 * 100 = 90 > max_notional 10
        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10")],
            raw_asks=[("0.90", "100")],  # notional 90
            max_notional=Decimal("10"),
        )
        assert err is None
        assert asks == 1
        # First (and only) ask level truncated to fit cap
        persisted_bids, persisted_asks = get_depth_levels_for_snapshot(
            db_with_snapshot, "snap-1",
        )
        assert len(persisted_asks) == 1
        # allowed_size = 10 / 0.90 ≈ 11.11
        expected_size = Decimal("10") / Decimal("0.90")
        assert abs(persisted_asks[0].size - expected_size) < Decimal("1e-9")
        # cumulative_notional equals max_notional exactly
        assert persisted_asks[0].cumulative_notional == Decimal("10")

    def test_first_level_truncation_persists_within_bounds(
        self, db_with_snapshot,
    ):
        """After first-level truncation, persisted cumulative_notional
        never exceeds max_notional.
        """
        # Three oversized levels, each alone exceeds cap.
        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "100")],  # notional 50
            raw_asks=[("0.60", "100")],  # notional 60
            max_notional=Decimal("20"),
        )
        assert err is None
        _, persisted_asks = get_depth_levels_for_snapshot(
            db_with_snapshot, "snap-1",
        )
        # First (and only) ask level truncated
        assert len(persisted_asks) == 1
        assert persisted_asks[0].cumulative_notional == Decimal("20")
        assert persisted_asks[0].cumulative_notional <= Decimal("20")


# ── Idempotent repeat ──────────────────────────────────────────────────────


class TestIdempotentRepeat:
    """Case B: existing rows with the same normalized bounded book."""

    def test_identical_repeat_is_idempotent(self, db_with_snapshot):
        raw_bids = [("0.50", "10"), ("0.49", "20")]
        raw_asks = [("0.51", "10")]
        b1, a1, err1 = persist_depth_levels(
            db_with_snapshot, "snap-1", raw_bids, raw_asks,
        )
        assert err1 is None
        rows_before = db_with_snapshot.fetchall(
            "SELECT id FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )

        # Repeat with the SAME raw inputs in DIFFERENT order.
        raw_bids_reordered = list(reversed(raw_bids))
        b2, a2, err2 = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids_reordered, raw_asks,
        )
        assert err2 is None
        assert (b2, a2) == (b1, a1)

        rows_after = db_with_snapshot.fetchall(
            "SELECT id FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )
        # No new rows were inserted.
        assert {r["id"] for r in rows_after} == {r["id"] for r in rows_before}


# ── Mismatch repeat ────────────────────────────────────────────────────────


class TestMismatchRepeat:
    """Case B: existing rows with a different bounded book."""

    def test_mismatched_repeat_returns_snapshot_mismatch(
        self, db_with_snapshot,
    ):
        # First write
        b1, a1, err1 = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.30", "10")],
            raw_asks=[("0.70", "10")],
        )
        assert err1 is None

        # Repeat with a different bid price (still non-crossed):
        # bids ≤ asks requires bid < 0.70.
        b2, a2, err2 = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.25", "10")],
            raw_asks=[("0.70", "10")],
        )
        assert err2 == DEPTH_SNAPSHOT_MISMATCH
        assert b2 == 0 and a2 == 0

        # Original rows are still intact (no overwrite).
        bids, _ = get_depth_levels_for_snapshot(
            db_with_snapshot, "snap-1",
        )
        assert len(bids) == 1
        assert bids[0].price == Decimal("0.30")

    def test_mismatched_repeat_no_writes(self, db_with_snapshot):
        persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.30", "10")],
            raw_asks=[("0.70", "10")],
        )
        before_count = db_with_snapshot.fetchone(
            "SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )["n"]

        persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.20", "10")],
            raw_asks=[("0.70", "10")],
        )
        after_count = db_with_snapshot.fetchone(
            "SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )["n"]
        assert after_count == before_count


# ── Forced failure rollback ─────────────────────────────────────────────────


class TestRollback:
    """Any insert failure must roll back the entire transaction."""

    def test_forced_failure_between_bids_and_asks(
        self, tmp_path, monkeypatch,
    ):
        """If the asks loop fails, the bids already inserted must be
        rolled back.
        """
        db = _make_db(tmp_path)
        _add_snapshot(db, "snap-1")

        # First, persist successfully so the parent snapshot exists.
        persist_depth_levels(
            db, "snap-1",
            raw_bids=[("0.50", "10")],
            raw_asks=[("0.51", "10")],
        )

        # Wipe existing levels to force a fresh insert path.
        db.execute(
            "DELETE FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )
        db.conn.commit()

        # Patch db.execute to raise on the second matching INSERT.
        original_execute = db.execute
        call_count = {"n": 0}

        def failing_execute(sql, params=()):
            if (
                "INSERT INTO candidate_price_snapshot_levels" in sql
                and call_count["n"] == 1
            ):
                call_count["n"] += 1
                raise RuntimeError("simulated bid-2 failure")
            if "INSERT INTO candidate_price_snapshot_levels" in sql:
                call_count["n"] += 1
            return original_execute(sql, params)

        monkeypatch.setattr(db, "execute", failing_execute)

        with pytest.raises(RuntimeError):
            persist_depth_levels(
                db, "snap-1",
                raw_bids=[("0.50", "10"), ("0.49", "20")],
                raw_asks=[("0.51", "10")],
            )

        # No rows should remain after rollback.
        rows = db.fetchall(
            "SELECT * FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )
        assert rows == []

    def test_forced_failure_during_mid_bids(
        self, tmp_path, monkeypatch,
    ):
        """Failure mid-bids leaves no rows."""
        db = _make_db(tmp_path)
        _add_snapshot(db, "snap-1")

        original_execute = db.execute
        call_count = {"n": 0}

        def failing_execute(sql, params=()):
            if "INSERT INTO candidate_price_snapshot_levels" in sql:
                if call_count["n"] == 0:
                    call_count["n"] += 1
                elif call_count["n"] == 1:
                    raise RuntimeError("simulated mid-bid failure")
            return original_execute(sql, params)

        monkeypatch.setattr(db, "execute", failing_execute)

        with pytest.raises(RuntimeError):
            persist_depth_levels(
                db, "snap-1",
                raw_bids=[("0.50", "10"), ("0.49", "20")],
                raw_asks=[],
            )

        rows = db.fetchall(
            "SELECT * FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )
        assert rows == []


# ── Missing parent snapshot ────────────────────────────────────────────────


class TestMissingParentSnapshot:
    """If the parent snapshot row does not exist, persistence returns
    DEPTH_LEVELS_MALFORMED without writing.
    """

    def test_missing_parent_returns_malformed(self, tmp_path):
        db = _make_db(tmp_path)  # No snapshot inserted
        bids, asks, err = persist_depth_levels(
            db, "nonexistent-snap",
            raw_bids=[("0.50", "10")],
            raw_asks=[("0.51", "10")],
        )
        assert err == DEPTH_LEVELS_MALFORMED
        assert bids == 0
        assert asks == 0

    def test_missing_parent_no_rows_written(self, tmp_path):
        db = _make_db(tmp_path)
        persist_depth_levels(
            db, "nonexistent-snap",
            raw_bids=[("0.50", "10")],
            raw_asks=[("0.51", "10")],
        )
        rows = db.fetchall(
            "SELECT * FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("nonexistent-snap",),
        )
        assert rows == []


# ── Old snapshot with no levels ────────────────────────────────────────────


class TestOldSnapshotNoLevels:
    """Snapshots without depth rows must remain valid and surface
    DEPTH_NOT_CAPTURED — never auto-backfilled.
    """

    def test_old_snapshot_returns_depth_not_captured(
        self, db_with_snapshot,
    ):
        # No levels persisted for snap-1 yet.
        snap_id, bids, asks, err = get_latest_depth_levels_for_candidate(
            db_with_snapshot, candidate_id=1,
        )
        assert snap_id == "snap-1"
        assert bids == []
        assert asks == []
        assert err == DEPTH_NOT_CAPTURED

    def test_old_snapshot_no_backfill(self, db_with_snapshot):
        """get_latest_depth_levels_for_candidate must NOT auto-backfill."""
        get_latest_depth_levels_for_candidate(
            db_with_snapshot, candidate_id=1,
        )
        rows = db_with_snapshot.fetchall(
            "SELECT * FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )
        assert rows == []

    def test_has_snapshot_levels_false_for_old(self, db_with_snapshot):
        assert has_snapshot_levels(db_with_snapshot, "snap-1") is False


# ── Malformed existing rows ────────────────────────────────────────────────


class TestMalformedExisting:
    """When stored rows are corrupted, return DEPTH_LEVELS_MALFORMED."""

    def test_noncontiguous_index_detected(self, db_with_snapshot):
        # Insert rows manually with a gap in level_index.
        db_with_snapshot.execute(
            """INSERT INTO candidate_price_snapshot_levels
               (snapshot_id, side, level_index, price, size,
                cumulative_size, cumulative_notional, created_at)
               VALUES ('snap-1', 'BID', 0, 0.5, 10, 10, 5.0, '2026-07-03T00:00:00Z')""",
        )
        db_with_snapshot.execute(
            """INSERT INTO candidate_price_snapshot_levels
               (snapshot_id, side, level_index, price, size,
                cumulative_size, cumulative_notional, created_at)
               VALUES ('snap-1', 'BID', 2, 0.49, 20, 30, 14.8, '2026-07-03T00:00:00Z')""",
        )
        # Commit manually
        db_with_snapshot.conn.commit()

        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10")],
            raw_asks=[],
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_decreasing_cumulative_size_detected(self, db_with_snapshot):
        db_with_snapshot.execute(
            """INSERT INTO candidate_price_snapshot_levels
               (snapshot_id, side, level_index, price, size,
                cumulative_size, cumulative_notional, created_at)
               VALUES ('snap-1', 'BID', 0, 0.5, 10, 30, 5.0, '2026-07-03T00:00:00Z')""",
        )
        db_with_snapshot.execute(
            """INSERT INTO candidate_price_snapshot_levels
               (snapshot_id, side, level_index, price, size,
                cumulative_size, cumulative_notional, created_at)
               VALUES ('snap-1', 'BID', 1, 0.49, 20, 10, 14.8, '2026-07-03T00:00:00Z')""",
        )
        db_with_snapshot.conn.commit()

        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10"), ("0.49", "20")],
            raw_asks=[],
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_decreasing_cumulative_notional_detected(
        self, db_with_snapshot,
    ):
        db_with_snapshot.execute(
            """INSERT INTO candidate_price_snapshot_levels
               (snapshot_id, side, level_index, price, size,
                cumulative_size, cumulative_notional, created_at)
               VALUES ('snap-1', 'ASK', 0, 0.5, 10, 10, 100.0, '2026-07-03T00:00:00Z')""",
        )
        db_with_snapshot.execute(
            """INSERT INTO candidate_price_snapshot_levels
               (snapshot_id, side, level_index, price, size,
                cumulative_size, cumulative_notional, created_at)
               VALUES ('snap-1', 'ASK', 1, 0.49, 20, 30, 50.0, '2026-07-03T00:00:00Z')""",
        )
        db_with_snapshot.conn.commit()

        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[],
            raw_asks=[("0.50", "10"), ("0.49", "20")],
        )
        assert err == DEPTH_LEVELS_MALFORMED

    def test_one_sided_corruption_detected(self, db_with_snapshot):
        """If the caller asks for both sides but only one is stored,
        the existing state is treated as malformed.
        """
        # Persist only bid side
        persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10")],
            raw_asks=[],
        )

        # Now try to re-persist with both sides
        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10")],
            raw_asks=[("0.51", "10")],
        )
        assert err == DEPTH_LEVELS_MALFORMED


# ── Hash comparison (verified through behavior) ─────────────────────────────


class TestHashComparison:
    """The hash comparison in Case B must reflect the bounded book."""

    def test_truncation_change_is_detected_as_mismatch(
        self, db_with_snapshot,
    ):
        """Persisting the same book with different max_notional
        bounds produces different bounded books — the second attempt
        is a mismatch, not a silent re-truncation.
        """
        # First persist with a loose cap so level 2 fits in full.
        b1, a1, err1 = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "1"), ("0.49", "100")],
            raw_asks=[("0.51", "1"), ("0.52", "100")],
            max_notional=Decimal("1000"),
        )
        assert err1 is None
        # level 2 should be persisted in full
        bids, asks = get_depth_levels_for_snapshot(
            db_with_snapshot, "snap-1",
        )
        assert len(bids) == 2
        assert len(asks) == 2

        # Wipe and re-persist with a tighter cap → second attempt is
        # a fresh path with a different bounded book.
        db_with_snapshot.execute(
            "DELETE FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ?",
            ("snap-1",),
        )
        b2, a2, err2 = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "1"), ("0.49", "100")],
            raw_asks=[("0.51", "1"), ("0.52", "100")],
            max_notional=Decimal("5"),
        )
        assert err2 is None
        # level 2 should now be truncated
        bids2, asks2 = get_depth_levels_for_snapshot(
            db_with_snapshot, "snap-1",
        )
        # First level full size = 1; remaining cap = 4; allowed size
        # for level 2 = 4 / 0.49 ≈ 8.16
        assert float(bids2[1].size) < float(Decimal("100"))


# ── Exact row counts and FK enforcement ─────────────────────────────────────


class TestExactCounts:
    """Returned counts reflect actual persisted rows."""

    def test_returned_bid_count_matches_actual_rows(
        self, db_with_snapshot,
    ):
        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10"), ("0.49", "20"), ("0.48", "30")],
            raw_asks=[("0.51", "10")],
        )
        assert err is None
        assert bids == 3
        assert asks == 1
        actual_bids = db_with_snapshot.fetchone(
            "SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ? AND side = 'BID'",
            ("snap-1",),
        )["n"]
        actual_asks = db_with_snapshot.fetchone(
            "SELECT COUNT(*) AS n FROM candidate_price_snapshot_levels "
            "WHERE snapshot_id = ? AND side = 'ASK'",
            ("snap-1",),
        )["n"]
        assert actual_bids == bids
        assert actual_asks == asks

    def test_empty_book_returns_zero_counts(self, db_with_snapshot):
        """A book with both sides empty normalizes to ([], [], None);
        persistence succeeds with zero rows.
        """
        bids, asks, err = persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[], raw_asks=[],
        )
        assert err is None
        assert bids == 0
        assert asks == 0


# ── End-to-end: trade fill from persisted depth ────────────────────────────


class TestTradeFillFromPersistedDepth:
    """The full chain: persist depth → walk for BUY/SELL → INCOMPLETE
    propagation when depth is missing or malformed.
    """

    def test_buy_fill_against_persisted_depth(self, db_with_snapshot):
        persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10")],
            raw_asks=[("0.51", "10"), ("0.52", "100")],
        )
        walk, err = compute_trade_fill_from_depth(
            db_with_snapshot, candidate_id=1, side="BUY",
            intended_notional=Decimal("5"),
        )
        assert err is None
        assert walk is not None
        assert walk.is_complete
        # 5 notional at 0.51 = 9.8 contracts, all from level 1
        assert walk.filled_notional == Decimal("5")
        assert walk.levels_consumed == 1

    def test_sell_partial_fill_against_persisted_depth(
        self, db_with_snapshot,
    ):
        """intended_notional (15) > total captured notional (5 + 9.8 = 14.8)
        → partial fill, not complete.
        """
        persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10"), ("0.49", "20")],
            raw_asks=[("0.51", "10")],
        )
        walk, err = compute_trade_fill_from_depth(
            db_with_snapshot, candidate_id=1, side="SELL",
            intended_notional=Decimal("15"),
        )
        assert err is None
        assert walk is not None
        # Bid 1 notional = 5.0; bid 2 notional = 9.8; total = 14.8
        # intended = 15 → 0.2 short → partial
        assert walk.filled_notional == Decimal("14.8")
        assert walk.remaining_notional == Decimal("0.2")
        assert not walk.is_complete
        assert walk.insufficient_reason == "DEPTH_INSUFFICIENT_FOR_STAKE"
        # fill_percentage = 14.8 / 15
        assert walk.fill_percentage == Decimal("14.8") / Decimal("15")
        assert walk.levels_consumed == 2

    def test_sell_exact_full_fill(self, db_with_snapshot):
        """intended_notional (14.8) == total captured notional → exact fill."""
        persist_depth_levels(
            db_with_snapshot, "snap-1",
            raw_bids=[("0.50", "10"), ("0.49", "20")],
            raw_asks=[("0.51", "10")],
        )
        walk, err = compute_trade_fill_from_depth(
            db_with_snapshot, candidate_id=1, side="SELL",
            intended_notional=Decimal("14.8"),
        )
        assert err is None
        assert walk is not None
        assert walk.is_complete
        assert walk.filled_notional == Decimal("14.8")
        assert walk.remaining_notional == Decimal("0")

    def test_missing_snapshot_returns_depth_not_captured(self, tmp_path):
        db = _make_db(tmp_path)
        walk, err = compute_trade_fill_from_depth(
            db, candidate_id=1, side="BUY",
            intended_notional=Decimal("5"),
        )
        assert walk is None
        assert err == DEPTH_NOT_CAPTURED

    def test_old_snapshot_without_depth_returns_depth_not_captured(
        self, db_with_snapshot,
    ):
        # snap-1 exists but has no levels.
        walk, err = compute_trade_fill_from_depth(
            db_with_snapshot, candidate_id=1, side="BUY",
            intended_notional=Decimal("5"),
        )
        assert walk is None
        assert err == DEPTH_NOT_CAPTURED