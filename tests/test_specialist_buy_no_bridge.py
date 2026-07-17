"""Focused BUY-No bridge acceptance tests (PR #70).

Exercises the canonical approved-wallet bridge path against a real binary
Polymarket-shaped market (distinct Yes/No tokens) and asserts the exact
supported-outcome contract:

  * BUY Yes  -> accepted (candidate + paper signal)
  * BUY No   -> accepted (candidate + paper signal)
  * SELL Yes -> rejected (bridge is BUY-only)
  * SELL No  -> rejected (bridge is BUY-only)
  * BUY missing outcome   -> blocked (fail closed)
  * BUY unsupported outcome -> blocked (fail closed)

No production DB is touched; each case uses an isolated temp DB.
"""
from __future__ import annotations

from polycopy.db.database import Database
from polycopy.engine.approved_wallet_trade_bridge import (
    _issue_write_capability,
    process_approved_wallet_trades,
)
from tests.fixtures.specialist_paper_fixtures import (
    BUY_NO_TOKEN,
    FIXED_WALLET,
    bridge_dependencies,
    seed_resolved_evidence,
)

_YES_TOKEN = "0x" + "f" * 64  # the target condition id (Yes token)


def _insert_source_trade(db: Database, *, side: str, outcome: str | None, token: str) -> tuple[str, str]:
    from uuid import uuid4

    st_id = f"st_bridge_{side}_{outcome}_{uuid4().hex[:8]}"
    public = f"poly.market:bridge:{st_id}"
    # The source_trades.outcome column is NOT NULL; a "missing" outcome is
    # represented as an empty string so it still reaches the bridge's
    # fail-closed normalization (which rejects blank labels).
    db.execute(
        """
        INSERT INTO source_trades
            (id, source, source_trade_id, market_source_id, side, outcome,
             quantity, price, trader_address, timestamp, is_sample, token_id)
        VALUES (?, 'polymarket_data_api_trades_user', ?, ?, ?, ?, 10.0, 0.4,
                ?, '2026-07-14T11:30:00+00:00', 0, ?)
        """,
        (st_id, public, _YES_TOKEN, side, outcome if outcome is not None else "", FIXED_WALLET, token),
    )
    db.conn.commit()
    return st_id, public


def _run_bridge(db: Database, *, source_trade_id: str, expect_rows: bool = True) -> list[dict]:
    deps = bridge_dependencies()
    rep = process_approved_wallet_trades(
        db,
        wallet=FIXED_WALLET,
        limit=1,
        dependencies=deps,
        write=True,
        write_authorization=_issue_write_capability(),
        source_trade_id=source_trade_id,
        evaluate_canonical_decisions=True,
    )
    rows = rep.as_dict()["rows"]
    if expect_rows:
        assert rows, "bridge selected no rows for the injected source trade"
        return rows
    return rows


def _accepted(row: dict) -> bool:
    # A trade is "accepted" by the canonical bridge path when it is hydrated,
    # passes the horizon gate, and produces a persisted copy_candidate +
    # evaluated paper signal. We mirror test_pr25a's assertion shape.
    stages = row.get("stages", {})
    return (
        stages.get("trade_copyability") == "persisted"
        and stages.get("paper") == "persisted"
        and row.get("skip_reason") is None
    )


# --------------------------------------------------------------------------- #
# Accepted outcomes                                                           #
# --------------------------------------------------------------------------- #
def test_buy_yes_accepted(tmp_path):
    db = Database(tmp_path / "yes.db").connect()
    seed_resolved_evidence(db)
    _, public = _insert_source_trade(db, side="BUY", outcome="Yes", token=_YES_TOKEN)
    rows = _run_bridge(db, source_trade_id=public)
    row = rows[0]
    assert _accepted(row), row
    assert row.get("skip_reason") is None
    # DB-level proof: a copy_candidate + paper_signal_decision now exist.
    assert db.fetchone("SELECT COUNT(*) AS n FROM copy_candidates")["n"] == 1
    assert db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"] == 1


def test_buy_no_accepted(tmp_path):
    db = Database(tmp_path / "no.db").connect()
    seed_resolved_evidence(db)
    _, public = _insert_source_trade(db, side="BUY", outcome="No", token=BUY_NO_TOKEN)
    rows = _run_bridge(db, source_trade_id=public)
    row = rows[0]
    assert _accepted(row), row
    assert row.get("skip_reason") is None
    # The No trade carries its canonical outcome into the persisted candidate.
    cand = db.fetchone(
        "SELECT outcome_label FROM copy_candidates WHERE source_trade_id=?", (public,))
    assert cand["outcome_label"] == "No", "bridge must carry the exact canonical No outcome"
    assert db.fetchone("SELECT COUNT(*) AS n FROM paper_signal_decisions")["n"] == 1


# --------------------------------------------------------------------------- #
# SELL rejected (BUY-only milestone scope)                                    #
# --------------------------------------------------------------------------- #
def test_sell_yes_rejected(tmp_path):
    db = Database(tmp_path / "sell_yes.db").connect()
    seed_resolved_evidence(db)
    _, public = _insert_source_trade(db, side="SELL", outcome="Yes", token=_YES_TOKEN)
    # The bridge's source-selection gate is BUY-only: a SELL trade is never
    # even selected, so it cannot reach hydration/scoring. Fail-closed.
    rows = _run_bridge(db, source_trade_id=public, expect_rows=False)
    assert rows == [], "SELL trade must not be selected by the BUY-only bridge gate"


def test_sell_no_rejected(tmp_path):
    db = Database(tmp_path / "sell_no.db").connect()
    seed_resolved_evidence(db)
    _, public = _insert_source_trade(db, side="SELL", outcome="No", token=BUY_NO_TOKEN)
    rows = _run_bridge(db, source_trade_id=public, expect_rows=False)
    assert rows == [], "SELL trade must not be selected by the BUY-only bridge gate"


# --------------------------------------------------------------------------- #
# Outcome fail-closed                                                         #
# --------------------------------------------------------------------------- #
def test_buy_missing_outcome_blocked(tmp_path):
    db = Database(tmp_path / "missing.db").connect()
    seed_resolved_evidence(db)
    _, public = _insert_source_trade(db, side="BUY", outcome=None, token=_YES_TOKEN)
    rows = _run_bridge(db, source_trade_id=public)
    row = rows[0]
    assert not _accepted(row)
    assert row.get("skip_reason") in (
        "missing_condition_token_or_outcome",
        "unsupported_outcome",
    ), row.get("skip_reason")


def test_buy_unsupported_outcome_blocked(tmp_path):
    db = Database(tmp_path / "unsupported.db").connect()
    seed_resolved_evidence(db)
    _, public = _insert_source_trade(db, side="BUY", outcome="Maybe", token=_YES_TOKEN)
    rows = _run_bridge(db, source_trade_id=public)
    row = rows[0]
    assert not _accepted(row)
    assert row.get("skip_reason") == "unsupported_outcome", row.get("skip_reason")
