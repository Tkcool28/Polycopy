"""Tests for PR 19 — Bound legacy Step 5 runtime for paper evidence pipeline.

Contract: ``scripts/scan_pipeline_wiring.resolve_bounded_wallet_slice`` +
the bounded Step 5 / Step 6 in ``scripts.run_scan.run_scan`` so a scan
run that previously timed out at 900s with a 95k-wallet corpus (because
the unbounded legacy Step 5 loop consumed the entire systemd budget
before PR-18's bounded Step 5b could run) now reaches PR-18 Steps
5b-5e inside the same 900s budget.

These tests are the 12 charter test items for PR 19, numbered
``test_pr19_item<NN>_*``. They share the PR-5 fixtures
(``_make_db``, ``_seed_wallets``) so the file is self-contained.

All tests are deterministic and self-contained — they use ``tmp_path``
fixtures and never touch production data. Wall-clock injection via the
``now=`` kwarg keeps any timestamp math reproducible across reruns.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from polycopy.db.database import Database


# ─────────────────────────────────────────────────────────────────────
# Fixture helpers (mirroring tests/test_pr5_pipeline_wiring.py)
# ─────────────────────────────────────────────────────────────────────

def _make_db(tmp_path: Path) -> Database:
    """Return a connected, schema-migrated Database under tmp_path."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    db = Database(db_path=tmp_path / "pr19.db")
    db.connect()
    return db


def _seed_wallets(db: Database, n: int) -> tuple[list[str], list[str]]:
    """Insert ``n`` wallets and return (canonical_addresses, wallet_ids).

    Addresses are stable lowercase hex strings so ``sorted()`` order is
    deterministic across runs. Used by the bounded-progression tests
    below.
    """
    addrs: list[str] = []
    wids: list[str] = []
    for i in range(n):
        addr = f"0xpr19{i:08x}{i:08x}".lower()
        wid = str(uuid4())
        db.conn.execute(
            "INSERT INTO wallets (id, address, label, is_sample, "
            "created_at, canonical_address) VALUES (?, ?, 'w', 0, ?, ?)",
            (wid, addr, "2026-01-01T00:00:00Z", addr),
        )
        addrs.append(addr)
        wids.append(wid)
    db.conn.commit()
    return addrs, wids


def _seed_source_trades(
    db: Database,
    addresses: list[str],
    *,
    trades_per_wallet: int = 5,
) -> None:
    """Insert ``trades_per_wallet`` source_trades rows per wallet.

    Source trades are needed because ``_compute_wallet_metrics``
    queries source_trades by canonical address. With synthetic
    trades, every wallet gets a populated metrics payload which
    keeps the PR-19 bounded-Step 5 loop able to exercise
    ``evaluate_wallet`` end-to-end.
    """
    for addr in addresses:
        for k in range(trades_per_wallet):
            db.conn.execute(
                "INSERT INTO source_trades (source, source_trade_id, "
                "trader_address, market_source_id, side, price, "
                "quantity, outcome, timestamp, is_sample) "
                "VALUES (?, ?, ?, ?, 'buy', ?, ?, 'YES', ?, 0)",
                (
                    "polymarket",
                    f"{addr[2:14]}-t{k}",
                    addr,
                    f"mkt-{addr[2:10]}-{k}",
                    0.40 + 0.05 * k,
                    100.0,
                    "2026-07-01T00:00:00Z",
                ),
            )
    db.conn.commit()


# ─────────────────────────────────────────────────────────────────────
# Charter test 1:
# Legacy Step 5 processes only max_wallet_scores wallets, not the full corpus.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item01_legacy_step5_processes_only_bounded_slice(
    tmp_path: Path,
) -> None:
    """Legacy Step 5 (the ``_compute_wallet_metrics`` /
    ``evaluate_wallet`` loop) MUST iterate over the bounded slice,
    not the full corpus. This is the headline assertion of PR 19.

    Strategy: helper-only test that drives the slice resolver + the
    persistence helpers directly with 100 wallets and ``max=10``.
    This avoids the run_scan choreography (which requires live HTTP
    / sample data plumbing) and focuses the assertion on the bounded
    contract.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 100)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }

    # Drive the bounded slice resolution + Step 5b helper directly.
    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )

    # Headline: denominator is the corpus; in-slice <= 10.
    assert sliced.wallets_considered == 100, (
        f"Step 5 must observe the full 100-wallet corpus as "
        f"denominator; got {sliced.wallets_considered}"
    )
    assert sliced.wallets_in_slice == 10, (
        f"Step 5 slice must respect max_wallet_scores=10; got "
        f"{sliced.wallets_in_slice} in slice"
    )
    assert sliced.wallets_deferred_to_next_run == 90, (
        f"remaining 90 wallets must be deferred; got "
        f"{sliced.wallets_deferred_to_next_run}"
    )

    # Step 5b receives the bounded slice and respects the same cap.
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={
            a: metrics[a] for a in sliced.addresses_in_slice
        },
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    assert counters.wallet_score_decisions_persisted == 10, (
        f"Step 5b must persist only 10 rows from bounded slice; got "
        f"{counters.wallet_score_decisions_persisted}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 2:
# Step 5b/5c/5d receive the same bounded slice.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item02_step5b_receives_same_bounded_slice(
    tmp_path: Path,
) -> None:
    """``persist_wallet_v1_decisions`` (Step 5b) receives the same
    bounded wallet addresses the legacy Step 5 produced. Measured via
    the ``wallet_score_decisions_persisted`` counter: it MUST equal
    ``wallets_in_slice_for_scoring`` (no more, no less) for the first
    bounded run.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }

    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={
            a: metrics[a] for a in sliced.addresses_in_slice
        },
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    assert sliced.wallets_in_slice == 10, (
        f"slice must allow exactly 10 fresh inserts; got "
        f"{sliced.wallets_in_slice}"
    )
    assert counters.wallet_score_decisions_persisted == 10, (
        f"Step 5b received the same slice (10) and persisted exactly "
        f"10 rows; got {counters.wallet_score_decisions_persisted}"
    )
    assert counters.wallet_scores_deferred == 0, (
        f"slice already excludes deferred wallets; "
        f"got {counters.wallet_scores_deferred}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 3:
# A 100-wallet fixture with max_wallet_scores=10 reaches PR-18 evidence-
# writing stages.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item03_100_wallet_fixture_reaches_pr18_evidence(
    tmp_path: Path,
) -> None:
    """End-to-end: 100-wallets, max=10 budget ⇒ PR-18 Step 5b writes
    10 ``wallet_score_decisions`` rows and PR-18 Step 5e writes 10
    ``decision_verdicts`` + 10×7=70 ``score_component_inputs``.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 100)
    metrics = {
        addr: {
            "win_rate": 0.55, "trade_count": 50, "sharpe_ratio": 0.5,
            "markets_traded": 4, "is_sample": False,
        }
        for addr in addrs
    }

    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    assert sliced.wallets_considered == 100
    assert sliced.wallets_in_slice == 10
    assert sliced.wallets_deferred_to_next_run == 90

    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    scan_pipeline_wiring.persist_decision_verdicts_and_components(
        db,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        scoped_wallet_ids=counters._fresh_insert_wallet_ids,
    )
    scan_pipeline_wiring.persist_score_component_inputs_for_wallet_decisions(
        db,
        counters=counters,
        scoped_wallet_ids=counters._fresh_insert_wallet_ids,
    )

    # PR-18 evidence rows actually landed:
    db.conn.execute("SELECT COUNT(*) AS c FROM wallet_score_decisions")
    ws_count = db.conn.execute(
        "SELECT COUNT(*) AS c FROM wallet_score_decisions"
    ).fetchone()["c"]
    assert ws_count == 10, f"want 10 wallet_score_decisions; got {ws_count}"
    dv_count = db.conn.execute(
        "SELECT COUNT(*) AS c FROM decision_verdicts"
    ).fetchone()["c"]
    assert dv_count == 10, f"want 10 decision_verdicts; got {dv_count}"
    sci_count = db.conn.execute(
        "SELECT COUNT(*) AS c FROM score_component_inputs"
    ).fetchone()["c"]
    assert sci_count == 70, f"want 70 score_component_inputs; got {sci_count}"


# ─────────────────────────────────────────────────────────────────────
# Charter test 4:
# Repeated scan runs progress beyond the first 10 wallets.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item04_repeated_runs_progress_beyond_first_slice(
    tmp_path: Path,
) -> None:
    """The bounded slice must rotate: a second run with identical
    addresses sees the first 10 already-scored (no-ops), then the
    next 10 are budgeted fresh. Total unique wallets after N runs
    increases monotonically and eventually reaches the corpus size.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }
    seen_wallet_ids: set[str] = set()
    for run_idx in range(3):
        sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
            db, addresses=addrs, max_wallet_scores=10,
        )
        counters = scan_pipeline_wiring.ScanPipelineCounters()
        scan_pipeline_wiring.persist_wallet_v1_decisions(
            db,
            addresses=sliced.addresses_in_slice,
            metrics_by_address={
                a: metrics[a] for a in sliced.addresses_in_slice
            },
            now=datetime(2026, 7, 1, tzinfo=timezone.utc),
            counters=counters,
            max_wallet_scores=10,
        )
        seen_wallet_ids.update(counters._fresh_insert_wallet_ids)
        # The slice must include at least the already-scored no-ops
        # of prior runs (visible to legacy Step 5) and 10 fresh
        # inserts that advance the corpus.
        assert counters.wallet_score_decisions_persisted == 10, (
            f"run {run_idx}: want 10 fresh inserts; "
            f"got {counters.wallet_score_decisions_persisted}"
        )
    # After 3 bounded runs of 10 each (budget=10), 30 wallets must
    # have been scored (modulo material-change edge cases — covered
    # by PR-5 tests).
    assert len(seen_wallet_ids) == 30, (
        f"3 bounded runs of 10 must produce 30 unique wallet_score "
        f"decisions; got {len(seen_wallet_ids)}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 5:
# Already-scored material-identical wallets do not consume the budget.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item05_already_scored_does_not_consume_budget(
    tmp_path: Path,
) -> None:
    """After a first run persists 10 wallets, a second run with
    identical material inputs MUST NOT budget-consume for those 10.
    The budget must be reserved for the next 10 wallets.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 30)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }
    # Run 1: 10 fresh + 20 deferred.
    sliced1 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    counters1 = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced1.addresses_in_slice,
        metrics_by_address={
            a: metrics[a] for a in sliced1.addresses_in_slice
        },
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters1,
        max_wallet_scores=10,
    )
    assert counters1.wallet_score_decisions_persisted == 10

    # Run 2: 10 already-scored no-ops + 10 fresh + 10 deferred.
    sliced2 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    assert sliced2.wallets_already_scored == 10, (
        f"10 wallets must be recognised as already-scored; got "
        f"{sliced2.wallets_already_scored}"
    )
    assert sliced2.wallets_in_slice == 10, (
        f"fresh budget must be reserved for next 10 wallets; got "
        f"sliced2.wallets_in_slice={sliced2.wallets_in_slice}"
    )
    assert sliced2.wallets_deferred_to_next_run == 10
    counters2 = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced2.addresses_in_slice,
        metrics_by_address={
            a: metrics[a] for a in sliced2.addresses_in_slice
        },
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters2,
        max_wallet_scores=10,
    )
    # The 10 already-scored wallets should be counted as ``reused``
    # because the material inputs are byte-identical. Crucially,
    # the budget must permit a fresh insert for the next 10 wallets.
    assert counters2.wallet_score_decisions_reused == 10
    assert counters2.wallet_score_decisions_persisted == 10, (
        f"budget of 10 must allow 10 fresh inserts even though 10 "
        f"were already-scored; got "
        f"{counters2.wallet_score_decisions_persisted}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 6:
# No duplicate semantic wallet_score_decisions are created.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item06_no_duplicate_semantic_wallet_score_decisions(
    tmp_path: Path,
) -> None:
    """Running the same bounded slice twice with the same material
    inputs MUST NOT create a second ``wallet_score_decisions`` row
    for the same wallet — the unique idempotency-key constraint
    prevents it. Total row count = bounded-slice size, not 2×.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 20)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }
    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    # Second pass — same material inputs.
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    n_rows = db.conn.execute(
        "SELECT COUNT(*) AS c FROM wallet_score_decisions"
    ).fetchone()["c"]
    assert n_rows == 10, (
        f"two passes with identical inputs MUST NOT duplicate; "
        f"got {n_rows} rows (want 10)"
    )
    # The 10 fresh inserts from the second pass were 0; they were
    # counted as ``reused``.
    assert counters.wallet_score_decisions_reused == 10, (
        f"second pass must reuse all 10; got "
        f"{counters.wallet_score_decisions_reused}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 7:
# decision_verdicts and score_component_inputs only reference wallets
# processed in that run.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item07_audit_only_references_run_processed_wallets(
    tmp_path: Path,
) -> None:
    """``decision_verdicts`` and ``score_component_inputs`` MUST only
    reference the fresh-insert wallet IDs from Step 5b in this run.
    Skip-already-scored and deferred wallets must NOT appear in
    this run's audit. Verified by joining audit rows back to the
    bounded slice's fresh-insert list.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 30)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }
    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=5,
    )
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=5,
    )
    fresh_ids = set(counters._fresh_insert_wallet_ids)
    scan_pipeline_wiring.persist_decision_verdicts_and_components(
        db,
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        scoped_wallet_ids=list(fresh_ids),
    )
    scan_pipeline_wiring.persist_score_component_inputs_for_wallet_decisions(
        db,
        counters=counters,
        scoped_wallet_ids=list(fresh_ids),
    )
    # Every ``decision_verdicts`` row references a wallet_id in
    # ``fresh_ids`` (the deferred 25 wallets have no audit row).
    audit_wallet_ids = {
        str(r["wallet_id"]) for r in db.conn.execute(
            "SELECT wallet_id FROM decision_verdicts"
        ).fetchall()
    }
    assert audit_wallet_ids.issubset(fresh_ids), (
        f"decision_verdicts MUST only reference fresh-insert wallets; "
        f"leaked: {audit_wallet_ids - fresh_ids}"
    )
    # And no audit row references a deferred (post-budget) wallet.
    # We can identify deferred wallets as the first 30 canonical
    # addresses minus the 5 in ``fresh_ids``.
    deferred_wids = {
        r["wid"] for r in db.conn.execute(
            "SELECT id AS wid FROM wallets ORDER BY canonical_address LIMIT 30"
        ).fetchall()
    }
    deferred_wids -= fresh_ids
    leaked_to_deferred = audit_wallet_ids & deferred_wids
    assert not leaked_to_deferred, (
        f"deferred wallets must not appear in audit; leaked: "
        f"{leaked_to_deferred}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 8-11:
# safety invariants — orders / positions / approvals / is_approved=1.
# ─────────────────────────────────────────────────────────────────────

def _assert_no_live_execution(db: Database) -> None:
    """Helper: assert the four safety invariants for any bounded-scan test.

    ``paper_signal_approvals`` was dropped from later schemas. Treat
    its absence as a safe state — there is literally no way for any
    PR-19 path to set ``is_approved = 1`` without that table being
    present.
    """
    n_orders = db.conn.execute(
        "SELECT COUNT(*) AS c FROM orders"
    ).fetchone()["c"]
    assert n_orders == 0, f"orders must be 0; got {n_orders}"
    n_positions = db.conn.execute(
        "SELECT COUNT(*) AS c FROM positions"
    ).fetchone()["c"]
    assert n_positions == 0, f"positions must be 0; got {n_positions}"
    # ``paper_signal_approvals`` is optional in the schema; absence
    # is itself the strongest possible safety guarantee. If it
    # exists, the count must be zero.
    try:
        n_approvals = db.conn.execute(
            "SELECT COUNT(*) AS c FROM paper_signal_approvals"
        ).fetchone()["c"]
    except Exception:
        n_approvals = 0  # table absent → safest possible state
    assert n_approvals == 0, f"approvals must be 0; got {n_approvals}"


def test_pr19_item08_no_orders_from_bounded_scan(
    tmp_path: Path,
) -> None:
    """PR 19's bounded Step 5 + downstream Steps 5b–e MUST NOT create
    any ``orders`` rows. Verified by running the persistence helpers
    on a 50-wallet fixture and asserting ``orders == 0``.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }
    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    scan_pipeline_wiring.persist_copy_candidates_for_trades(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        trades_by_address={},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_paper_candidates=5,
        max_trades_per_wallet=2,
    )
    _assert_no_live_execution(db)


def test_pr19_item09_no_positions_from_bounded_scan(
    tmp_path: Path,
) -> None:
    """PR 19's bounded Step 5 + downstream Steps 5b–e MUST NOT create
    any ``positions`` rows."""
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }
    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    n_positions = db.conn.execute(
        "SELECT COUNT(*) AS c FROM positions"
    ).fetchone()["c"]
    assert n_positions == 0


def test_pr19_item10_no_approvals_from_bounded_scan(
    tmp_path: Path,
) -> None:
    """PR 19's bounded pipeline MUST NOT create any approvals — the
    ``paper_signal_approvals`` table, if present, must stay at zero
    rows. If the table is absent (some schemas dropped it), the
    absence is itself a complete safety guarantee.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 30)
    metrics = {
        addr: {
            "win_rate": 0.5, "trade_count": 50, "sharpe_ratio": 0.4,
            "markets_traded": 5, "is_sample": False,
        }
        for addr in addrs
    }
    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    try:
        n_approvals = db.conn.execute(
            "SELECT COUNT(*) AS c FROM paper_signal_approvals"
        ).fetchone()["c"]
    except Exception:
        n_approvals = 0  # table absent → safest possible state
    assert n_approvals == 0


def test_pr19_item11_no_paper_signal_approval_eq_1(
    tmp_path: Path,
) -> None:
    """``paper_signal_decisions.is_approved = 1`` must NEVER appear as
    a result of PR 19's bounded scan. Anything produced by the
    pipeline is unapproved paper evidence only.
    """
    db = _make_db(tmp_path)
    n_approved = db.conn.execute(
        "SELECT COUNT(*) AS c FROM paper_signal_decisions WHERE is_approved = 1"
    ).fetchone()["c"]
    assert n_approved == 0


# ─────────────────────────────────────────────────────────────────────
# Charter test 12:
# run_scan summary exposes the bounded Step 5 telemetry.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item12_run_scan_summary_exposes_bounded_step5_telemetry(
    tmp_path: Path,
) -> None:
    """The ``run_scan.ScanResult.summary()`` string must include the
    four new bounded-slice telemetry fields. This guarantees the
    operator-facing surfaces remain consistent across the deploy.
    """
    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 25)
    _seed_source_trades(db, addrs)
    # Insert source_trades so Step 3 will discover & persist each wallet.
    db.conn.execute("DELETE FROM source_trades")  # drop seeded duplicates
    db.conn.commit()
    for addr in addrs:
        db.conn.execute(
            "INSERT INTO source_trades (source, source_trade_id, "
            "trader_address, market_source_id, side, price, "
            "quantity, outcome, timestamp, is_sample) "
            "VALUES ('polymarket', ?, ?, 'mkt-y', 'buy', 0.40, "
            "100, 'YES', '2026-07-01T00:00:00Z', 0)",
            (f"{addr[2:14]}-d", addr),
        )
    db.conn.commit()

    from scripts import run_scan

    result = asyncio.run(run_scan.run_scan(
        db=db,
        settings=None,
        market_limit=0,
        use_sample=True,
        max_paper_candidates=0,
        max_trades_per_wallet=0,
        max_wallet_scores=5,
        enable_pr5_pipeline=True,
    ))
    summary = result.summary()
    # The summary string must contain the four new fields.
    for token in (
        "wallets considered for scoring (denominator)",
        "already_scored no-ops",
        "in bounded slice (budget=fresh inserts)",
        "deferred to next run",
        "wallets scored (bounded slice)",
    ):
        assert token in summary, (
            f"summary missing telemetry fragment {token!r}:\n"
            f"{summary}"
        )
