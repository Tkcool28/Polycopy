"""Deterministic, bounded fixture builder for the Specialist Paper Execution Spine.

This helper seeds a FIXED temporary SQLite database with a FIXED wallet,
a fixed cap of resolved-market evidence rows, and fixed (mocked) external
market/book/resolution fixtures — enough to satisfy the frozen scoring gates
honestly (including the 30-resolved-market requirement) WITHOUT lowering them,
bypassing the formula, or inserting an eligible paper-signal row by hand.

Everything here runs through the real application code and persistent writers:
approval persistence, source-trade writer, candidate/snapshot/decision bridge,
wallet/category scoring, trade copyability, and paper-signal evaluation.

The external HTTP layer (Gamma market + CLOB book) is faked with deterministic
in-memory stubs so the test never depends on a live history fetch.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from polycopy.db.database import Database
from polycopy.engine.approved_wallet_trade_bridge import (
    process_approved_wallet_trades,
    BridgeDependencies,
    _issue_write_capability,
)
from polycopy.scoring.paper_signal import evaluate_paper_signals_for_candidate
from polycopy.scoring.evaluation_policy import DEFAULT_EVALUATION_POLICY
from polycopy.execution.specialist_approval import create_approval

# Fixed constants (bounded, deterministic).
FIXED_WALLET = "0x" + "a" * 40
RESOLVED_MARKET_COUNT = 40  # >= 30 frozen gate, evenly split win/loss
SPECIALIST_CATEGORY = "politics"
EVIDENCE_EVENT_PREFIX = "evt"


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# --------------------------------------------------------------------------- #
# Deterministic external stubs (no live HTTP)                                  #
# --------------------------------------------------------------------------- #
class _FakeOutcome:
    def __init__(self, token_id: str) -> None:
        self.clob_token_id = token_id
        self.label = "Yes"
        self.price = 0.5
        self.volume = 100.0


import collections.abc  # noqa: E402


class _FakeMarket(collections.abc.Mapping):
    def __init__(self, condition_id: str) -> None:
        # Use a valid 0x-prefixed 64-hex token id derived from the condition id.
        token_id = condition_id if condition_id.startswith("0x") else "0x" + condition_id
        self.id = condition_id
        self.source = "polymarket"
        self.source_id = condition_id
        self.question = "test market"
        self.active = True
        self.closed = False
        self.resolved = False
        self.resolution_outcome = None
        self.volume_24h = 0.0
        self.end_date = datetime.now(timezone.utc) + timedelta(days=10)
        self.fetched_at = datetime.now(timezone.utc)
        self.is_sample = False
        self.outcomes = [_FakeOutcome(token_id)]
        # Trusted PR66 taxonomy/event provenance as a real Gamma market carries.
        self.category = "politics"
        self.tags = ["politics"]
        self.events = [{"id": "evtfake", "slug": "event-fake", "title": "Fake Event"}]
        self.series = [{"id": "srfake", "slug": "series-fake", "title": "Fake Series"}]
        # Mapping protocol so build_metadata_from_gamma_market can read it
        # (production passes a Pydantic Market, which is Mapping-compatible).
        self._mapping = {
            "category": self.category,
            "tags": self.tags,
            "events": self.events,
            "series": self.series,
        }

    def __getitem__(self, key: str) -> Any:
        if key == "category":
            return self.category
        if key == "tags":
            return self.tags
        if key == "events":
            return self.events
        if key == "series":
            return self.series
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)


class FakeGamma:
    def get_market(self, condition_id: str) -> "_FakeMarket":
        return _FakeMarket(condition_id)


from polycopy.adapters.polymarket_clob import ClobBook, ClobBookLevel  # noqa: E402


class FakeBook(ClobBook):
    """Minimal deterministic ClobBook stand-in with one valid two-sided level."""
    def __init__(self) -> None:
        super().__init__(
            token_id="0x" + "f" * 64,
            bids=[ClobBookLevel(price=0.38, size=1000.0)],
            asks=[ClobBookLevel(price=0.42, size=1000.0)],
            http_status=200,
            latency_ms=1,
            request_attempts=1,
            book_hash="fakebookhash",
            error_code=None,
            error_message=None,
        )


class FakeClob:
    async def fetch_book(self, token_id: str) -> "FakeBook":
        return FakeBook()


def bridge_dependencies() -> BridgeDependencies:
    return BridgeDependencies(gamma=FakeGamma(), clob=FakeClob())


# --------------------------------------------------------------------------- #
# Seed helpers                                                                 #
# --------------------------------------------------------------------------- #
def seed_resolved_evidence(db: Any, *, wallet: str = FIXED_WALLET,
                           count: int = RESOLVED_MARKET_COUNT) -> None:
    """Seed `count` resolved BUY source trades with taxonomy + win/loss P&L.

    This is the canonical wallet evidence the frozen wallet/category scorers
    aggregate. It is honest real-shaped data (not a stubbed verdict).
    """
    db.conn.execute(
        "INSERT OR IGNORE INTO wallets (id, address, canonical_address, created_at) "
        "VALUES (?,?,?,?)",
        ("w1", wallet, wallet, _iso(60)),
    )
    for i in range(count):
        # Concentrate the resolved trades across a small set of markets so the
        # wallet exhibits genuine one-sided (directional) dominance on at
        # least one market — honest real-shaped evidence. A wallet spreading
        # 40 trades over 40 distinct markets would read as UNKNOWN and
        # never qualify as copyable, which is the correct frozen behavior.
        market_idx = i % 15
        cond = f"0x{market_idx:062x}"  # 15 distinct markets, ~2-3 trades each
        meta = {
            "taxonomy": {"raw_category": SPECIALIST_CATEGORY},
            "event": {"id": f"{EVIDENCE_EVENT_PREFIX}{i:03d}", "slug": f"event-{i:03d}"},
        }
        won = (i % 5 != 0)  # ~80% win rate → strong wallet score
        db.conn.execute(
            """INSERT OR IGNORE INTO source_trades (
                   id, source, source_trade_id, market_source_id, side, outcome,
                   quantity, price, trader_address, timestamp, is_sample, token_id,
                   metadata_json, resolution_status, resolved_at, winning_token_id,
                   is_winning_trade, realized_pnl, settlement_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"st{i}", "polymarket_data_api_trades_user", f"poly.market:{cond}:{i}",
                cond, "BUY", "Yes", 10.0, 0.4, wallet, _iso(40 - i), 0,
                f"0x{i:062x}", json.dumps(meta),
                "won" if won else "lost", _iso(20 - i),
                f"0x{i:062x}" if won else "other", 1 if won else 0,
                6.0 if won else -4.0, "fixture",
            ),
        )
    db.conn.commit()


def make_target_trade(side: str = "BUY", *, with_taxonomy: bool = True,
                      resolved: Optional[bool] = None) -> dict[str, Any]:
    """Build the dict for the ONE trade the collector will ingest as the target."""
    cond = "0x" + "f" * 64  # valid 0x-prefixed 64-hex condition id
    meta = (
        {"taxonomy": {"raw_category": SPECIALIST_CATEGORY},
         "event": {"id": "evttarget", "slug": "event-target"}}
        if with_taxonomy else {}
    )
    row = {
        "id": "st_target",
        "source": "polymarket_data_api_trades_user",
        "source_trade_id": f"poly.market:{cond}:target",
        "market_source_id": cond,
        "side": side,
        "outcome": "Yes",
        "quantity": 10.0,
        "price": 0.4,
        "trader_address": FIXED_WALLET,
        "timestamp": _iso(2),
        "is_sample": 0,
        "token_id": "0x" + "f" * 64,  # valid 0x-prefixed 64-hex token id
        "metadata_json": json.dumps(meta),
    }
    if resolved is not None:
        row["resolution_status"] = "won" if resolved else "lost"
        row["winning_token_id"] = "0x" + "f" * 64 if resolved else "other"
        row["is_winning_trade"] = 1 if resolved else 0
        row["realized_pnl"] = 6.0 if resolved else -4.0
        row["settlement_source"] = "fixture"
    return row


def build_target_db(*, tmp_path: Optional[Path] = None) -> tuple[Database, Path]:
    """Create + migrate a fixed temp DB seeded with evidence + target trade.

    Returns (db, path). Caller is responsible for cleanup (tests use tmp_path;
    the proof script deletes the file on completion).
    """
    if tmp_path is None:
        fd, name = tempfile.mkstemp(suffix=".db", prefix="specialist_paper_")
        os.close(fd)
        tmp_path = Path(name)
    else:
        tmp_path = Path(tmp_path) / "specialist_paper_proof.db"
    db = Database(tmp_path).connect()
    seed_resolved_evidence(db)
    return db, tmp_path


def ingest_target_trade(db: Any, *, trade: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Insert the target trade via the centralized source-trade writer (real path)."""
    from polycopy.ingestion.source_trade_writer import write_valid_rows
    from polycopy.ingestion.normalized_source_trade import normalize_source_trade

    t = trade or make_target_trade()
    gamma = FakeGamma()
    market = gamma.get_market(t["market_source_id"])
    norm = normalize_source_trade(
        t, requested_wallet=FIXED_WALLET, allow_sell=False, gamma_market=market,
    )
    assert norm.validation_status == "valid", (
        f"target trade failed validation: {norm.validation_status} {norm.validation_reasons}"
    )
    res = write_valid_rows(db, [norm], dry_run=False)
    # The persisted source_trade_id is the canonical identity produced by the
    # writer (SOURCE_NAME source + 0x-namespaced id). We look it up using the
    # writer's own normalized value, never independently reconstructing it.
    persisted_sid = norm.source_trade_id
    if res.inserted == 0:
        # already present (idempotent replay)
        existing = db.fetchone(
            "SELECT id FROM source_trades WHERE source_trade_id=?",
            (persisted_sid,),
        )
        return {"source_trade_internal_id": existing["id"] if existing else None,
                "inserted": 0}
    row = db.fetchone(
        "SELECT id FROM source_trades WHERE source_trade_id=?",
        (persisted_sid,),
    )
    return {"source_trade_internal_id": row["id"], "inserted": res.inserted}


def run_bridge_for_target(db: Any, source_trade_internal_id: str) -> dict[str, Any]:
    """Run the real candidate/snapshot/decision bridge for the one target trade.

    Accepts the internal ``source_trades.id``; resolves the canonical stored
    ``source_trade_id`` from the DB so the lookup uses the writer's own
    persisted identity (never independently reconstructed).
    """
    row = db.fetchone(
        "SELECT source_trade_id FROM source_trades WHERE id=?",
        (source_trade_internal_id,),
    )
    if row is None:
        return {}
    stored_source_trade_id = row["source_trade_id"]
    rep = process_approved_wallet_trades(
        db, wallet=FIXED_WALLET, limit=1, dependencies=bridge_dependencies(),
        write=True, write_authorization=_issue_write_capability(),
        source_trade_id=stored_source_trade_id, evaluate_canonical_decisions=True,
    )
    rows = rep.as_dict().get("rows", [])
    return rows[0] if rows else {}


def evaluate_signal(db: Any, candidate_id: int) -> dict[str, Any]:
    """Run the REAL full paper-signal evaluator (no verdict patching)."""
    return evaluate_paper_signals_for_candidate(
        db, candidate_id, policy=DEFAULT_EVALUATION_POLICY
    )


def create_approval_for_target(db: Any, *, formula_name: str = "wallet_score",
                               formula_version: str = "1") -> str:
    """Create a durable manual approval for the fixed wallet (no auto-approve).

    Returns the approval_id (UUID string).
    """
    rec = create_approval(
        db,
        wallet_address=FIXED_WALLET,
        specialist_category=SPECIALIST_CATEGORY,
        wallet_score_decision_id=None,
        category_score_decision_id=None,
        formula_name=formula_name,
        formula_version=formula_version,
        evidence_fingerprint="fixture-fp",
        evidence_report_path="/tmp/fixture-evidence.md",
        reviewer="fixture-operator",
        approval_reason="Deterministic fixture approval for spine test.",
    )
    return rec.approval_id


def paper_runtime(allow: bool = True, *, kill_switch: bool = False,
                  **overrides: Any) -> Any:
    """Build an ExecutionRuntime for isolated paper tests.

    Injects explicit conservative limits so the new path is NOT unlimited.
    `allow=False` produces all-zero limits → fail-closed (exposure not configured).
    """
    from polycopy.execution.specialist_spine import ExecutionRuntime

    limits = {
        "max_order_size": 50.0 if allow else 0.0,
        "max_per_market": 100.0 if allow else 0.0,
        "max_per_wallet": 200.0 if allow else 0.0,
        "max_global": 500.0 if allow else 0.0,
    }
    limits.update(overrides)
    return ExecutionRuntime(
        is_paper=True,
        kill_switch_engaged=kill_switch,
        broker_mode="paper",
        is_live=False,
        db_is_temporary=True,
        allow_production_execution=False,
        snapshot_max_age_seconds=300.0,
        policy_version="specialist_paper_exec_v1",
        **limits,
    )
