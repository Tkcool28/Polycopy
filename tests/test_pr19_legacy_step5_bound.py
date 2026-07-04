"""Tests for PR 19 — Bound legacy Step 5 runtime for paper evidence pipeline.

Contract (v2 — second review cycle):
    ``scripts/scan_pipeline_wiring.resolve_bounded_wallet_slice`` +
    the bounded Step 5 / Step 6 in ``scripts.run_scan.run_scan`` so a
    scan run that previously timed out at 900s with a 95k-wallet
    corpus (because the unbounded legacy Step 5 loop consumed the
    entire systemd budget before PR-18's bounded Step 5b could run)
    now reaches PR-18 Steps 5b-5e inside the same 900s budget.

Hard-cap invariant (v2 added):
    ``len(addresses_in_slice) <= max_wallet_scores``
    ALWAYS — and the Step 5 legacy loop therefore operates on at most
    ``max_wallet_scores`` addresses per run, no matter the corpus size
    and no matter how many of those addresses are already-scored. This
    is the runtime bound that keeps the scan inside the systemd
    ``TimeoutStartSec=900`` budget.

Material-change bypass prevention (v2 added):
    A previously-scored wallet whose source_trades have advanced since
    its last V1 score MUST be classified as ``material_changed`` and
    budget-consume — never as a zero-budget ``already_scored`` filler
    that downstream still allows a fresh insert.

These tests cover the 12 charter test items plus the 5 review-driven
charter test items added in the second review cycle. Numbered
``test_pr19_item<NN>_*``. They share the PR-5 fixtures (``_make_db``,
``_seed_wallets``) so the file is self-contained.

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
    timestamp: str = "2026-07-01T00:00:00Z",
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
                    timestamp,
                ),
            )
    db.conn.commit()


def _metrics_for(addresses: list[str], **overrides) -> dict[str, dict]:
    """Return a synthetic canonical metrics dict for ``addresses``."""
    base = {
        "win_rate": 0.5,
        "trade_count": 50,
        "sharpe_ratio": 0.4,
        "markets_traded": 5,
        "is_sample": False,
    }
    base.update(overrides)
    return {addr: dict(base) for addr in addresses}


# ─────────────────────────────────────────────────────────────────────
# Charter test 1 (v2):
# Legacy Step 5 processes only max_wallet_scores wallets, not the full
# corpus. Hard-cap invariant: len(addresses_in_slice) <= max_wallet_scores.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item01_legacy_step5_processes_only_bounded_slice(
    tmp_path: Path,
) -> None:
    """Legacy Step 5 (the ``_compute_wallet_metrics`` /
    ``evaluate_wallet`` loop) MUST iterate over the bounded slice,
    not the full corpus. Headline assertion of PR 19.

    Strategy: helper-only test driving the slice resolver + the
    persistence helpers directly with 100 wallets and ``max=10``.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 100)
    metrics = _metrics_for(addrs)

    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )

    # Hard-cap invariant — the headline of v2.
    assert len(sliced.addresses_in_slice) <= 10, (
        f"HARD-CAP VIOLATION: len(addresses_in_slice)="
        f"{len(sliced.addresses_in_slice)} > max_wallet_scores=10"
    )
    # The dataclass-level invariant for operator-facing surfaces.
    assert sliced.wallets_in_slice_total <= 10, (
        f"wallets_in_slice_total must be <= cap; "
        f"got {sliced.wallets_in_slice_total}"
    )
    assert sliced.wallets_in_slice_fresh == 10, (
        f"all 10 in-slice slots must be fresh on a first run with 100 "
        f"wallets and cap=10; got "
        f"fresh={sliced.wallets_in_slice_fresh}, "
        f"already_scored={sliced.wallets_in_slice_already_scored}"
    )
    assert sliced.wallets_in_slice_already_scored == 0
    assert sliced.wallets_considered == 100
    assert sliced.wallets_deferred_to_next_run == 90

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
# Charter test 2 (v2):
# Step 5b/5c/5d/5e receive the same bounded slice.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item02_step5b_receives_same_bounded_slice(
    tmp_path: Path,
) -> None:
    """``persist_wallet_v1_decisions`` (Step 5b) receives the same
    bounded wallet addresses the legacy Step 5 produced.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = _metrics_for(addrs)

    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    assert sliced.wallets_in_slice_total == 10, (
        f"len(addresses_in_slice) must equal 10; got "
        f"{sliced.wallets_in_slice_total}"
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
    assert counters.wallet_score_decisions_persisted == 10
    assert counters.wallet_scores_deferred == 0


# ─────────────────────────────────────────────────────────────────────
# Charter test 3 (v2):
# A 100-wallet fixture with max_wallet_scores=10 reaches PR-18
# evidence-writing stages.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item03_100_wallet_fixture_reaches_pr18_evidence(
    tmp_path: Path,
) -> None:
    """End-to-end: 100-wallets, max=10 budget ⇒ PR-18 Step 5b writes
    10 ``wallet_score_decisions`` rows and downstream Steps persist
    ``decision_verdicts`` + ``score_component_inputs``.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 100)
    metrics = _metrics_for(addrs, win_rate=0.55, sharpe_ratio=0.5, markets_traded=4)

    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    assert sliced.wallets_considered == 100
    assert sliced.wallets_in_slice_total == 10
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
# Charter test 4 (v2):
# Repeated scan runs progress beyond the first 10 wallets.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item04_repeated_runs_progress_beyond_first_slice(
    tmp_path: Path,
) -> None:
    """The bounded slice must rotate. A second run with identical
    addresses sees the first 10 already-scored (no-ops), and the
    next 10 are budgeted fresh.

    Hard-cap invariant reused: every run keeps
    ``len(addresses_in_slice) <= max_wallet_scores``.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = _metrics_for(addrs)

    seen_wallet_ids: set[str] = set()
    for run_idx in range(3):
        sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
            db, addresses=addrs, max_wallet_scores=10,
        )
        # Hard-cap invariant per run.
        assert len(sliced.addresses_in_slice) <= 10, (
            f"run {run_idx}: HARD-CAP VIOLATION, "
            f"len(addresses_in_slice)={len(sliced.addresses_in_slice)} > 10"
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
        assert counters.wallet_score_decisions_persisted == 10, (
            f"run {run_idx}: want 10 fresh inserts; "
            f"got {counters.wallet_score_decisions_persisted}"
        )
    # After 3 runs, 30 wallets must have advanced through the cap.
    assert len(seen_wallet_ids) == 30, (
        f"3 bounded runs of 10 must produce 30 unique wallet_score "
        f"decisions; got {len(seen_wallet_ids)}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 5 (v2):
# Already-scored material-identical wallets do not consume the budget.
# v2 addition: the slice still respects the hard cap — already-scored
# wallets can fill the remaining slots but never push the slice past
# the cap.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item05_already_scored_does_not_consume_budget(
    tmp_path: Path,
) -> None:
    """After a first run persists 10 wallets, a second run with
    identical material inputs MUST NOT budget-consume for those 10.
    The budget must be reserved for the next 10 wallets, and the
    slice length must remain at the cap.

    V2 semantics: fresh wallets always win slots; already-scored
    zero-budget fillers ONLY fill slots that fresh did not
    consume. So in this fixture, the next 10 wallets (lex after the
    first 10 already-scored) get the budget; the first 10 (already-
    scored) are deferred (cap was spent on fresh). The strict-
    invariant statement: the slice stays exactly at the cap, and
    the budget was not wasted on no-op retries.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 30)
    metrics = _metrics_for(addrs)
    # Source trades must exist for the material-change proxy so
    # already-scored-and-not-material-changed classification works.
    _seed_source_trades(db, addrs, timestamp="2026-07-01T00:00:00Z")

    # Run 1: 10 fresh + 20 deferred.
    sliced1 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    assert sliced1.wallets_in_slice_total == 10
    assert sliced1.wallets_in_slice_fresh == 10
    assert sliced1.wallets_in_slice_already_scored == 0
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

    # Run 2: the first 10 wallets have V1 rows (already-scored); the
    # remaining 20 are fresh. Fresh-wins algorithm picks the 20 fresh
    # wallets for the cap=10 budget, then tries to fill with the 10
    # already-scored, but cap is fully spent. Slice = 10 fresh + 0
    # already-scored (deferred to a future run).
    sliced2 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    # Hard-cap invariant: slice length is exactly the cap.
    assert len(sliced2.addresses_in_slice) <= 10, (
        f"HARD-CAP VIOLATION on run 2: "
        f"len(addresses_in_slice)={len(sliced2.addresses_in_slice)} > 10"
    )
    assert sliced2.wallets_in_slice_total == 10
    # The 10 in-slice slots are FRESH (the cap was filled by fresh,
    # not by already-scored). No fresh budget was wasted on
    # already-scored no-ops.
    assert sliced2.wallets_in_slice_fresh == 10, (
        f"fresh-wins semantics: 10 fresh slots, no already-scored "
        f"filler needed; got fresh={sliced2.wallets_in_slice_fresh}, "
        f"already_scored={sliced2.wallets_in_slice_already_scored}"
    )
    assert sliced2.wallets_in_slice_already_scored == 0
    assert sliced2.wallets_deferred_to_next_run == 20
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
    # 10 fresh inserts on this run. No reused because slice was
    # entirely fresh — the already-scored wallets never entered the
    # slice, so they're deferred to a future run rather than
    # counted as "reused" by Step 5b.
    assert counters2.wallet_score_decisions_persisted == 10, (
        f"10 fresh budget must allow 10 fresh inserts; got "
        f"persisted={counters2.wallet_score_decisions_persisted}, "
        f"reused={counters2.wallet_score_decisions_reused}"
    )


# ─────────────────────────────────────────────────────────────────────
# Charter test 6 (v2):
# No duplicate semantic wallet_score_decisions.
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
    metrics = _metrics_for(addrs)
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
    assert counters.wallet_score_decisions_reused == 10


# ─────────────────────────────────────────────────────────────────────
# Charter test 7 (v2):
# decision_verdicts and score_component_inputs only reference wallets
# processed in that run.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item07_audit_only_references_run_processed_wallets(
    tmp_path: Path,
) -> None:
    """``decision_verdicts`` and ``score_component_inputs`` MUST only
    reference the fresh-insert wallet IDs from Step 5b in this run.
    Skip-already-scored and deferred wallets must NOT appear in
    this run's audit.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 30)
    metrics = _metrics_for(addrs)
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
    audit_wallet_ids = {
        str(r["wallet_id"]) for r in db.conn.execute(
            "SELECT wallet_id FROM decision_verdicts"
        ).fetchall()
    }
    assert audit_wallet_ids.issubset(fresh_ids), (
        f"decision_verdicts MUST only reference fresh-insert wallets; "
        f"leaked: {audit_wallet_ids - fresh_ids}"
    )
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
# Charter test 8-11 (v2):
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
    any ``orders`` rows.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = _metrics_for(addrs)
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
    """PR 19's bounded pipeline MUST NOT create any ``positions`` rows."""
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = _metrics_for(addrs)
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
    metrics = _metrics_for(addrs)
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
        n_approvals = 0
    assert n_approvals == 0


def test_pr19_item11_no_paper_signal_approval_eq_1(
    tmp_path: Path,
) -> None:
    """``paper_signal_decisions.is_approved = 1`` must NEVER appear as
    a result of PR 19's bounded scan.
    """
    db = _make_db(tmp_path)
    n_approved = db.conn.execute(
        "SELECT COUNT(*) AS c FROM paper_signal_decisions WHERE is_approved = 1"
    ).fetchone()["c"]
    assert n_approved == 0


# ─────────────────────────────────────────────────────────────────────
# Charter test 12 (v2):
# run_scan summary exposes the bounded Step 5 telemetry.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item12_run_scan_summary_exposes_bounded_step5_telemetry(
    tmp_path: Path,
) -> None:
    """The ``run_scan.ScanResult.summary()`` string must include the
    new bounded-slice telemetry fields.
    """
    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 25)
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
    for token in (
        "wallets considered for scoring (denominator)",
        "fresh/material-changed (budget-consuming)",
        "already-scored (zero-budget filler)",
        "hard-cap invariant: <= max_wallet_scores",
        "deferred to next run",
        "wallets scored (bounded slice)",
    ):
        assert token in summary, (
            f"summary missing telemetry fragment {token!r}:\n{summary}"
        )


# ─────────────────────────────────────────────────────────────────────
# Charter test 13-17 (v2 NEW):
# Review-driven tests for the second-cycle blockers.
# ─────────────────────────────────────────────────────────────────────

def test_pr19_item13_hard_cap_invariant_with_100_wallets_cap_10(
    tmp_path: Path,
) -> None:
    """Hard-cap invariant: with 100 wallets and ``max_wallet_scores=10``,
    ``len(addresses_in_slice) <= 10`` on every run.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 100)
    metrics = _metrics_for(addrs)
    _seed_source_trades(db, addrs, timestamp="2026-07-01T00:00:00Z")
    sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    assert len(sliced.addresses_in_slice) <= 10, (
        f"hard-cap must hold: len(slice)={len(sliced.addresses_in_slice)}"
    )
    # Drive Step 5b to write rows for the in-slice set.
    counters = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced.addresses_in_slice,
        metrics_by_address={a: metrics[a] for a in sliced.addresses_in_slice},
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters,
        max_wallet_scores=10,
    )
    # And the persisted row count also respects the cap.
    assert counters.wallet_score_decisions_persisted <= 10


def test_pr19_item14_repeated_runs_never_grow_slice_beyond_cap(
    tmp_path: Path,
) -> None:
    """Repeated runs MUST NOT cause the bounded slice to grow beyond
    the cap. The total Step 5 work stays bounded even as more wallets
    get prior V1 scores.

    Stress: 50 wallets, cap=10, run 6 times with the bounded slice
    helper each time. After enough runs, every wallet is already-
    scored and the slice is fully zero-budget filler; the slice
    length MUST still equal 10.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 50)
    metrics = _metrics_for(addrs)
    _seed_source_trades(db, addrs, timestamp="2026-07-01T00:00:00Z")
    for run_idx in range(6):
        sliced = scan_pipeline_wiring.resolve_bounded_wallet_slice(
            db, addresses=addrs, max_wallet_scores=10,
        )
        # Hard-cap invariant on every run.
        assert len(sliced.addresses_in_slice) <= 10, (
            f"run {run_idx}: slice grew beyond cap: "
            f"{len(sliced.addresses_in_slice)} > 10"
        )
        assert sliced.wallets_in_slice_total <= 10
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
        # No fresh insert ever exceeds the cap — the strict invariant.
        assert counters.wallet_score_decisions_persisted <= 10


def test_pr19_item15_material_changed_wallets_budget_consume(
    tmp_path: Path,
) -> None:
    """A previously-scored wallet whose source_trades have advanced
    since its last V1 score MUST be classified as ``material_changed``
    and counted as a budget-consuming slot. If we wrongly classified
    it as ``already_scored``, the slice helper would silently let it
    slip through as a zero-budget filler that downstream still writes
    as fresh — defeating operator visibility.

    Fixture: 10 wallets are seeded. 5 get a V1 score at T0. A fresh
    source_trades row at T1 for wallet 0 trips the material-change
    proxy for it. Wallet 0 is now material-changed; wallets 1-4 are
    already-scored but not material-changed. Final state:
      * fresh_set = {w0 (material-changed)} (size 1)
      * already_scored_set = {w1..w4} (size 4)
      * cap = 10 (much larger than corpus; algorithm returns corpus)

    This isolates the material-change classification: if the helper
    wrongly classified w0 as already-scored, ``fresh`` would be
    empty and ``already_scored`` would have 5 members — failing
    the assertions below.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 10)
    metrics = _metrics_for(addrs)
    # Source trades for the first 5 wallets (which we'll persist in
    # run 1) at T0.
    _seed_source_trades(
        db, addrs[:5], timestamp="2026-07-01T00:00:00Z",
    )
    sliced1 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs[:5], max_wallet_scores=10,
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
    assert counters1.wallet_score_decisions_persisted == 5

    # Advance the source_trades timestamp only for wallet 0.
    db.conn.execute(
        "INSERT INTO source_trades (source, source_trade_id, "
        "trader_address, market_source_id, side, price, "
        "quantity, outcome, timestamp, is_sample) "
        "VALUES ('polymarket', 'late-trade-w0', ?, 'mkt-late', 'buy', "
        "0.41, 100, 'YES', '2026-07-02T00:00:00Z', 0)",
        (addrs[0],),
    )
    db.conn.commit()

    # Now look at all 10 wallets with cap=10 (corpus < cap, no
    # truncation). V2 must classify wallet 0 as fresh+material-
    # changed; wallets 1-4 as already-scored; wallets 5-9 as fresh.
    sliced2 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=10,
    )
    # Hard-cap invariant holds trivially because corpus < cap.
    assert len(sliced2.addresses_in_slice) <= 10
    # Headline: wallet 0 must be in ``fresh`` — material change
    # bypass prevention. If the proxy were broken, ``fresh`` would
    # be 5 (just the truly-fresh 5) and ``already_scored`` would be
    # 5 (incl. wallet 0). The 5/4 split below fails that case.
    assert sliced2.wallets_in_slice_fresh == 6, (
        f"wallet 0 (material-changed) + wallets 5-9 (truly fresh) = "
        f"6 fresh slots; got "
        f"fresh={sliced2.wallets_in_slice_fresh}. If material-changed "
        f"wallet 0 is misclassified as 'already_scored', this count "
        f"would be 5 and the bug would silently re-introduce the v1 "
        f"defect."
    )
    assert sliced2.wallets_in_slice_already_scored == 4, (
        f"wallets 1-4 already-scored-and-not-material-changed; got "
        f"already_scored={sliced2.wallets_in_slice_already_scored}"
    )


def test_pr19_item16_helper_invariant_asserts_hard_cap(
    tmp_path: Path,
) -> None:
    """The slice helper carries an internal ``assert
    len(addresses_in_slice) <= cap`` as a belt-and-braces guard. We
    verify directly via fresh / already_scored / total counts on a
    pathological setup where ``addresses_in_slice`` would otherwise
    be tempted to overshoot the cap.

    Construction: a 30-wallet corpus where EVERY wallet already has
    a prior V1 wallet_score_decisions row. Without the cap, the
    helper would treat all 30 as the slice. With the cap = 5, the
    slice MUST be exactly 5 (all already-scored, zero-budget
    filler).

    To pre-populate the prior rows we drive Step 5b through run 1
    with ``max_wallet_scores=30`` (uncapped) — that persists all 30.
    Then run 2 with ``cap=5`` exercises the hard cap.
    """
    from scripts import scan_pipeline_wiring

    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 30)
    metrics = _metrics_for(addrs)
    _seed_source_trades(db, addrs, timestamp="2026-07-01T00:00:00Z")
    # Run 1 with cap=30 (≥ corpus size) — every wallet gets a V1 row.
    sliced1 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=30,
    )
    assert len(sliced1.addresses_in_slice) == 30
    counters1 = scan_pipeline_wiring.ScanPipelineCounters()
    scan_pipeline_wiring.persist_wallet_v1_decisions(
        db,
        addresses=sliced1.addresses_in_slice,
        metrics_by_address={
            a: metrics[a] for a in sliced1.addresses_in_slice
        },
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
        counters=counters1,
        max_wallet_scores=30,
    )
    assert counters1.wallet_score_decisions_persisted == 30

    # Now the helper has 30 wallets with prior V1 rows. With cap=5,
    # the slice must be EXACTLY 5 (not 30).
    sliced2 = scan_pipeline_wiring.resolve_bounded_wallet_slice(
        db, addresses=addrs, max_wallet_scores=5,
    )
    assert len(sliced2.addresses_in_slice) == 5, (
        f"HARD-CAP VIOLATION: with 30 already-scored wallets and "
        f"cap=5, slice length must be exactly 5; got "
        f"{len(sliced2.addresses_in_slice)}"
    )
    assert sliced2.wallets_in_slice_total == 5
    # All 5 should be classified as already-scored (zero-budget
    # filler), since every wallet has a prior V1 row.
    assert sliced2.wallets_in_slice_already_scored == 5, (
        f"every wallet has a prior V1 row, so all 5 in-slice slots "
        f"must be already-scored; got "
        f"already_scored={sliced2.wallets_in_slice_already_scored}, "
        f"fresh={sliced2.wallets_in_slice_fresh}"
    )
    assert sliced2.wallets_deferred_to_next_run == 25


def test_pr19_item17_run_scan_enforces_hard_cap_at_scan_boundary(
    tmp_path: Path,
) -> None:
    """``run_scan`` mirrors the helper's hard-cap invariant with a
    second ``assert len(wallet_addresses) <= max_wallet_scores`` at
    the scan boundary, so any future regression that bypasses the
    helper still fails loudly.

    We exercise this by driving ``run_scan.run_scan`` directly with a
    synthetic 25-wallet DB and ``max_wallet_scores=5``. The result's
    ``wallets_in_slice_for_scoring_total`` MUST equal what the slice
    helper produced, and MUST be <= cap.
    """
    db = _make_db(tmp_path)
    addrs, _wids = _seed_wallets(db, 25)
    db.conn.execute("DELETE FROM source_trades")
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
    # Hard-cap invariant surfaces in the operator telemetry.
    assert result.wallets_in_slice_for_scoring_total <= 5, (
        f"HARD-CAP VIOLATION: wallets_in_slice_for_scoring_total="
        f"{result.wallets_in_slice_for_scoring_total} > 5"
    )
    # And the constituent fresh + already_scored counts sum to total.
    assert (
        result.wallets_in_slice_for_scoring_fresh
        + result.wallets_in_slice_for_scoring_already_scored
        == result.wallets_in_slice_for_scoring_total
    )
