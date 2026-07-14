"""PR67 canonical evaluator wiring and decision-only safety tests."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from polycopy.db.database import Database
from polycopy.scoring.evaluation_policy import EvaluationExecutionPolicy
from polycopy.scoring.paper_signal import (
    evaluate_paper_signal_for_candidate,
    resolve_candidate_taxonomy,
)
from polycopy.scoring.wallet_evidence import (
    CATEGORY_TAXONOMY_PARTIAL,
    CATEGORY_TAXONOMY_UNAVAILABLE,
    CATEGORY_TAXONOMY_USABLE,
    aggregate_category_evidence,
    resolve_category_score_v1,
    resolve_wallet_score_v1,
)
from tests.test_p04_chunk4_runtime_paper_signal import _seed_full


CUT = "2026-07-03T00:00:00Z"


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "pr67-wiring.db").connect()


def _metadata(kind: str) -> str | None:
    if kind == "usable":
        return json.dumps({"event": {"id": "event-1", "slug": "never-category", "title": "Never category"}, "taxonomy": {"raw_category": "Politics"}})
    if kind == "partial":
        return json.dumps({"event": {"slug": "crypto-event", "title": "Crypto title"}, "taxonomy": {"tags": ["politics"]}})
    if kind == "malformed":
        return "{bad-json"
    return None


def _seed(tmp_path: Path, *, taxonomy: str = "usable") -> tuple[Database, int, str, str]:
    db = _db(tmp_path)
    candidate_id, _ = _seed_full(db, fetched_at=CUT)
    candidate = db.fetchone("SELECT wallet_id, source_trade_internal_id FROM copy_candidates WHERE id=?", (candidate_id,))
    wallet_id = str(candidate["wallet_id"])
    trade_id = str(candidate["source_trade_internal_id"])
    db.execute(
        "UPDATE source_trades SET metadata_json=?, resolution_status='won', is_winning_trade=1, realized_pnl=1.0 WHERE id=?",
        (_metadata(taxonomy), trade_id),
    )
    db.conn.commit()
    return db, candidate_id, wallet_id, trade_id


def _count(db: Database, table: str) -> int:
    try:
        return int(db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"])
    except Exception:
        return 0


def _forbidden_counts(db: Database) -> dict[str, int]:
    return {table: _count(db, table) for table in (
        "source_trades", "wallets", "markets", "market_outcomes", "copy_candidates",
        "candidate_price_snapshots", "candidate_price_snapshot_levels", "shadow_decisions",
        "exit_experiment_registrations", "orders", "positions", "settlement_accounting_ledger",
    )}


def test_canonical_wallet_and_category_resolvers_propagate_provenance(tmp_path: Path):
    db, candidate_id, wallet_id, _ = _seed(tmp_path)
    before_wallet = _count(db, "wallet_score_decisions")
    before_category = _count(db, "category_wallet_score_decisions")
    result = evaluate_paper_signal_for_candidate(db, candidate_id, now=datetime(2026, 7, 4, tzinfo=timezone.utc))
    assert result["paper_signal_id"] is not None
    payload = json.loads(db.fetchone("SELECT decision_input_json FROM paper_signal_decisions WHERE id=?", (result["paper_signal_id"],))["decision_input_json"])
    assert payload["wallet_score_decision_id"] is not None
    assert payload["wallet_evidence_fingerprint"]
    assert payload["taxonomy_status"] == CATEGORY_TAXONOMY_USABLE
    assert payload["taxonomy_source"] == "source_trades.metadata_json"
    assert payload["category_score_decision_id"] is not None
    assert payload["category_evidence_fingerprint"]
    assert _count(db, "wallet_score_decisions") == before_wallet + 1
    assert _count(db, "category_wallet_score_decisions") == before_category + 1
    # Canonical category resolver used the source-trade taxonomy, not the
    # snapshot's legacy label. Its denominator is the wallet-wide BUY count.
    category = db.fetchone(
        "SELECT overall_trade_count FROM category_wallet_score_decisions ORDER BY id DESC LIMIT 1"
    )
    assert int(category["overall_trade_count"]) == 1
    db.close()


def test_empty_wallet_evidence_is_truthful_incomplete_and_replay_is_idempotent(tmp_path: Path):
    db = _db(tmp_path)
    db.execute("INSERT INTO wallets (id,address,canonical_address,created_at) VALUES ('w','0xw','0xw',?)", (CUT,))
    db.conn.commit()
    one = resolve_wallet_score_v1(db, "w", cutoff_timestamp=CUT, persist=True, now=datetime.now(timezone.utc))
    two = resolve_wallet_score_v1(db, "w", cutoff_timestamp=CUT, persist=True, now=datetime.now(timezone.utc))
    assert one.status == "incomplete" and one.decision_id is not None
    assert two.reused and two.decision_id == one.decision_id
    assert _count(db, "wallet_score_decisions") == 1
    no_write = resolve_wallet_score_v1(db, "w", cutoff_timestamp="2026-07-04T00:00:00Z", persist=False, now=datetime.now(timezone.utc))
    assert no_write.would_create and _count(db, "wallet_score_decisions") == 1
    db.close()


@pytest.mark.parametrize(("kind", "status"), [("partial", CATEGORY_TAXONOMY_PARTIAL), ("absent", CATEGORY_TAXONOMY_UNAVAILABLE), ("malformed", CATEGORY_TAXONOMY_UNAVAILABLE)])
def test_taxonomy_nonusable_never_persists_category(tmp_path: Path, kind: str, status: str):
    db, candidate_id, wallet_id, _ = _seed(tmp_path, taxonomy=kind)
    inputs = __import__("polycopy.scoring.paper_signal", fromlist=["load_persisted_paper_signal_inputs"]).load_persisted_paper_signal_inputs(db, candidate_id)
    taxonomy = resolve_candidate_taxonomy(inputs)
    assert taxonomy.status == status
    assert taxonomy.category_label is None
    before = _count(db, "category_wallet_score_decisions")
    resolution = resolve_category_score_v1(db, wallet_id, taxonomy, cutoff_timestamp=CUT, persist=True, now=datetime.now(timezone.utc))
    assert resolution.status == "not_applicable"
    assert _count(db, "category_wallet_score_decisions") == before
    db.close()


def test_wallet_incomplete_still_runs_taxonomy_category_and_tc(tmp_path: Path):
    db, candidate_id, _, _ = _seed(tmp_path)
    before_category = _count(db, "category_wallet_score_decisions")
    before_trade = _count(db, "trade_copyability_decisions")
    result = evaluate_paper_signal_for_candidate(db, candidate_id)
    assert result["reason"] == "wallet_incomplete"
    assert _count(db, "category_wallet_score_decisions") == before_category + 1
    assert _count(db, "trade_copyability_decisions") == before_trade + 1
    payload = json.loads(db.fetchone("SELECT decision_input_json FROM paper_signal_decisions")["decision_input_json"])
    assert payload["wallet_score_complete"] is False
    assert "no_resolved_buy_evidence" not in payload["wallet_score_missing_reasons"]
    db.close()


def test_all_decision_persistence_flags_and_shadow_exit_spies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db, candidate_id, _, _ = _seed(tmp_path)
    import polycopy.scoring.paper_signal as module
    shadow_calls: list[object] = []
    exit_calls: list[object] = []
    monkeypatch.setattr(module, "_compute_and_persist_shadow_v2", lambda *a, **k: shadow_calls.append(a) or {})
    monkeypatch.setattr(module, "record_exit_experiments_for_signal", lambda *a, **k: exit_calls.append(a) or 0)
    policy = EvaluationExecutionPolicy(
        persist_wallet_score=False, persist_category_score=False,
        persist_trade_copyability=False, persist_paper_signal=False,
        persist_shadow=False, persist_exit_experiments=False,
    )
    before = {
        table: _count(db, table)
        for table in (
            "wallet_score_decisions", "category_wallet_score_decisions",
            "trade_copyability_decisions", "paper_signal_decisions",
        )
    }
    result = evaluate_paper_signal_for_candidate(db, candidate_id, policy=policy)
    assert result["trade_copyability_would_create"] and result["paper_signal_would_create"]
    for table, count in before.items():
        assert _count(db, table) == count
    assert shadow_calls == [] and exit_calls == []
    db.close()


def test_decision_only_forbidden_tables_unchanged_and_unapproved(tmp_path: Path):
    db, candidate_id, _, _ = _seed(tmp_path)
    before = _forbidden_counts(db)
    result = evaluate_paper_signal_for_candidate(
        db, candidate_id,
        policy=replace(EvaluationExecutionPolicy.decision_only(), persist_shadow=False, persist_exit_experiments=False),
    )
    assert _forbidden_counts(db) == before
    assert result["is_approved"] == 0
    assert int(db.fetchone("SELECT is_approved FROM paper_signal_decisions")["is_approved"]) == 0
    db.close()


def test_category_evidence_is_scoped_but_wallet_denominator_is_not(tmp_path: Path):
    db, candidate_id, wallet_id, _ = _seed(tmp_path)
    # Add a second, distinct-category BUY to prove only category evidence is scoped.
    db.execute(
        "INSERT INTO source_trades (id,source,source_trade_id,market_source_id,side,outcome,quantity,price,trader_address,timestamp,is_sample,token_id,resolution_status,is_winning_trade,realized_pnl,metadata_json) "
        "VALUES ('other','test','other','m2','BUY','YES',1,.5,?, ?,0,'other-token','lost',0,-1,?)",
        (db.fetchone("SELECT canonical_address FROM wallets WHERE id=?", (wallet_id,))["canonical_address"], CUT, _metadata("usable").replace("Politics", "Crypto")),
    )
    db.conn.commit()
    evidence = aggregate_category_evidence(db, wallet_id, "politics", cutoff_timestamp=CUT)
    assert evidence.total_buy_trades == 1
    resolution = resolve_category_score_v1(
        db, wallet_id,
        resolve_candidate_taxonomy(__import__("polycopy.scoring.paper_signal", fromlist=["load_persisted_paper_signal_inputs"]).load_persisted_paper_signal_inputs(db, candidate_id)),
        cutoff_timestamp=CUT, persist=True, now=datetime.now(timezone.utc),
    )
    assert resolution.decision_id is not None
    assert int(db.fetchone("SELECT overall_trade_count FROM category_wallet_score_decisions WHERE id=?", (resolution.decision_id,))["overall_trade_count"]) == 2
    db.close()
