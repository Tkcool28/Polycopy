"""Specialist paper execution spine — canonical end-to-end copy path.

This module is the SINGLE authoritative operational path for the specialist
paper loop. It consumes an eligible ``paper_signal_decisions`` row that was
authorized through the explicit manual gate
``paper_signal_execution_authorizations`` and produces a fully provenance-
linked paper order, fill, and position.

Design principles (frozen milestone scope):

* One canonical path. The legacy ``orders`` / ``positions`` / ``settlement_*
  tables are NOT authoritative for the specialist spine; this module writes and
  reads only the dedicated ``paper_orders`` / ``paper_fills`` / ``paper_positions``
  / ``paper_position_lots`` / ``paper_position_marks`` / ``paper_position_settlements``
  tables. See ``docs/specialist_paper_execution_spine.md``.
* ``paper_signal_decisions.is_approved`` is read-only legacy compatibility state.
  The PR4 force-zero invariant is preserved: signal persistence NEVER grants
  execution authority. Authority comes only from an active row in
  ``paper_signal_execution_authorizations``.
* Fail-closed. Every revalidation failure persists a ``blocked`` execution-risk
  decision and creates no order.
* Exactly-once. A unique constraint on
  ``(paper_signal_decision_id)`` in ``paper_orders`` makes a second order for the
  same signal impossible at the database level, not merely in memory.
* Paper-only. The spine refuses to execute unless the runtime is paper and the
  database is an explicitly-isolated temporary database.

The fill, mark, and settlement *computations* are delegated to the existing P04
bricks (``polycopy.risk.fill_model.FillModel``, ``MarkEngine``,
``SettlementEngine``); this module owns their durable persistence and provenance.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Eligible verdict literals (frozen).
ELIGIBLE_SIGNAL_VERDICT = "copy_candidate"
ELIGIBLE_COPYABILITY_VERDICT = "copy_candidate"

# Resolution outcome taxonomy for paper settlement.
RES_WIN = "resolved_win"
RES_LOSS = "resolved_loss"
RES_VOID = "void"
RES_CONFLICT = "conflict"
RES_ERROR = "error"
RES_UNRESOLVED = "unresolved"


# --------------------------------------------------------------------------- #
# Result carriers                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class ExecutionResult:
    """Outcome of the canonical specialist execution consumer for one signal."""
    paper_signal_decision_id: int
    status: str  # "executed" | "blocked" | "skipped_no_authorization" | "already_executed"
    risk_decision_id: Optional[str] = None
    order_id: Optional[str] = None
    fill_id: Optional[str] = None
    position_id: Optional[str] = None
    rejection_reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarkResult:
    position_id: str
    mark_id: Optional[str]
    mark_price: Optional[float]
    unrealized_pnl: Optional[float]
    status: str  # "marked" | "skipped_no_evidence"


@dataclass
class SettlementOutcome:
    position_id: str
    settlement_id: Optional[int]
    status: str  # "settled" | "already_settled" | "blocked" | "conflict"
    is_winner: Optional[bool] = None
    payout: Optional[float] = None
    realized_pnl: Optional[float] = None
    reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Runtime guard contract                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class ExecutionRuntime:
    """The set of runtime + config facts the spine revalidates at execution time.

    The proof command and tests inject explicit values. Production never reaches
    this code path because the kill switch / paper-mode gates above it remain
    intact and no timer is enabled.
    """
    is_paper: bool = True
    kill_switch_engaged: bool = False
    broker_mode: str = "paper"
    is_live: bool = False
    db_is_temporary: bool = True
    max_order_size: float = 0.0
    max_per_market: float = 0.0
    max_per_wallet: float = 0.0
    max_global: float = 0.0
    snapshot_max_age_seconds: float = 300.0
    allow_production_execution: bool = False
    policy_version: str = "specialist_paper_exec_v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_paper": self.is_paper,
            "kill_switch_engaged": self.kill_switch_engaged,
            "broker_mode": self.broker_mode,
            "is_live": self.is_live,
            "db_is_temporary": self.db_is_temporary,
            "max_order_size": self.max_order_size,
            "max_per_market": self.max_per_market,
            "max_per_wallet": self.max_per_wallet,
            "max_global": self.max_global,
            "snapshot_max_age_seconds": self.snapshot_max_age_seconds,
            "allow_production_execution": self.allow_production_execution,
            "policy_version": self.policy_version,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Depth reconstruction from persisted snapshot levels                          #
# --------------------------------------------------------------------------- #
def _load_depth(db: Any, snapshot_id: str) -> Optional["Any"]:
    """Reconstruct a MarketDepth from persisted ``candidate_price_snapshot_levels``.

    Returns None when no level rows exist (missing depth → fail closed upstream).
    """
    from polycopy.risk.fill_model import MarketDepth, DepthLevel

    rows = db.fetchall(
        "SELECT side, price, size FROM candidate_price_snapshot_levels "
        "WHERE snapshot_id=? ORDER BY level_index ASC",
        (snapshot_id,),
    )
    if not rows:
        return None
    levels = [
        DepthLevel(price=float(r["price"]), volume=float(r["size"])) for r in rows
    ]
    # best price is the first level's price; depth orders worst-first internally.
    best = levels[0].price if levels else 0.0
    return MarketDepth(best_price=best, levels=levels)


# --------------------------------------------------------------------------- #
# 1) Signal execution authorization (explicit manual gate)                     #
# --------------------------------------------------------------------------- #
def create_execution_authorization(
    db: Any,
    *,
    paper_signal_decision_id: int,
    specialist_approval_id: str,
    source_trade_id: str,
    candidate_id: int,
    authorized_by: str,
    authorization_reason: Optional[str] = None,
    review_notes: Optional[str] = None,
    policy_version: str = "specialist_paper_execution_v1",
    now: Optional[datetime] = None,
) -> str:
    """Manually authorize one eligible paper signal for execution.

    Idempotent: re-authorizing the same signal returns the existing active id
    and refuses to create a duplicate authorization.

    Returns the authorization id.
    """
    now = now or _utcnow()
    existing = db.fetchone(
        "SELECT authorization_id, status FROM paper_signal_execution_authorizations "
        "WHERE paper_signal_decision_id=?",
        (paper_signal_decision_id,),
    )
    if existing is not None:
        if existing["status"] == "active":
            logger.info(
                "Execution authorization %s already active for signal %s",
                existing["authorization_id"], paper_signal_decision_id,
            )
            return str(existing["authorization_id"])
        # retired/used/revoked: do NOT resurrect automatically.
        raise ValueError(
            f"signal {paper_signal_decision_id} already has a non-active "
            f"authorization (status={existing['status']}); cannot re-authorize "
            "without explicit operator review."
        )
    authorization_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO paper_signal_execution_authorizations (
               authorization_id, paper_signal_decision_id, specialist_approval_id, source_trade_id,
               candidate_id, authorized_by, authorization_reason, review_notes,
               status, policy_version, approved_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?, ?, ?)""",
        (
            authorization_id, paper_signal_decision_id, specialist_approval_id, source_trade_id,
            candidate_id, authorized_by, authorization_reason, review_notes,
            policy_version, now.isoformat(), now.isoformat(), now.isoformat(),
        ),
    )
    logger.info("Created execution authorization %s for signal %s", authorization_id, paper_signal_decision_id)
    return authorization_id


def get_active_authorization(db: Any, paper_signal_decision_id: int) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT * FROM paper_signal_execution_authorizations "
        "WHERE paper_signal_decision_id=? AND status='active'",
        (paper_signal_decision_id,),
    )
    return dict(row) if row is not None else None


def consume_authorization(db: Any, authorization_id: str, *, now: Optional[datetime] = None) -> None:
    """Mark an authorization as used once its order is durably committed."""
    now = now or _utcnow()
    db.execute(
        "UPDATE paper_signal_execution_authorizations SET status='used', "
        "used_at=?, updated_at=? WHERE authorization_id=?",
        (now.isoformat(), now.isoformat(), authorization_id),
    )


# --------------------------------------------------------------------------- #
# 2) Eligible-signal consumer + fail-closed risk evaluation                    #
# --------------------------------------------------------------------------- #
def _fetch_signal_context(db: Any, paper_signal_decision_id: int) -> Optional[dict[str, Any]]:
    return db.fetchone(
        "SELECT * FROM paper_signal_decisions WHERE id=?",
        (paper_signal_decision_id,),
    )


def _is_snapshot_fresh(db: Any, snapshot_id: Optional[str], max_age_seconds: float,
                       now: datetime) -> tuple[bool, str]:
    if not snapshot_id:
        return False, "missing_snapshot_id"
    snap = db.fetchone(
        "SELECT fetched_at FROM candidate_price_snapshots WHERE id=?", (snapshot_id,)
    )
    if snap is None or not snap["fetched_at"]:
        return False, "snapshot_not_found"
    try:
        fetched = datetime.fromisoformat(snap["fetched_at"])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
    except ValueError:
        return False, "snapshot_timestamp_unparseable"
    age = (now - fetched).total_seconds()
    if age > max_age_seconds:
        return False, f"stale_snapshot:{age:.0f}s>{max_age_seconds:.0f}s"
    return True, "fresh"


def _current_exposure(db: Any, wallet_id: str, market_source_id: Optional[str]) -> dict[str, float]:
    """Compute persisted current specialist paper exposure (fail-closed: uses DB)."""
    per_wallet = float(db.fetchone(
        "SELECT COALESCE(SUM(quantity * avg_entry_price),0.0) AS e "
        "FROM paper_positions WHERE wallet_id=? "
        "AND NOT EXISTS (SELECT 1 FROM paper_position_settlements WHERE position_id=id)",
        (wallet_id,),
    )["e"])
    per_market = 0.0
    if market_source_id:
        per_market = float(db.fetchone(
            "SELECT COALESCE(SUM(quantity * avg_entry_price),0.0) AS e "
            "FROM paper_positions WHERE wallet_id=? AND market_id=? "
            "AND NOT EXISTS (SELECT 1 FROM paper_position_settlements WHERE position_id=id)",
            (wallet_id, market_source_id),
        )["e"])
    global_exp = float(db.fetchone(
        "SELECT COALESCE(SUM(quantity * avg_entry_price),0.0) AS e "
        "FROM paper_positions WHERE NOT EXISTS "
        "(SELECT 1 FROM paper_position_settlements WHERE position_id=id)",
    )["e"])
    return {"per_wallet": per_wallet, "per_market": per_market, "global": global_exp}


def _persist_risk_decision(
    db: Any, *, paper_signal_decision_id, specialist_approval_id, source_trade_id,
    candidate_id, snapshot_id, decision, reason_codes, requested_quantity,
    requested_price, estimated_fill_price, estimated_slippage, exposure_before,
    configured_limits, kill_switch_state, paper_mode, evidence_timestamp, runtime,
    now,
) -> str:
    risk_decision_id = str(uuid.uuid4())
    # A blocked/no-authorization risk decision may legitimately lack an active
    # approval; normalize the sentinel to NULL so the nullable FK column is valid.
    if specialist_approval_id in (None, "-1", -1):
        specialist_approval_id = None
    db.execute(
        """INSERT INTO execution_risk_decisions (
               risk_decision_id, paper_signal_decision_id, specialist_approval_id, source_trade_id,
               candidate_id, snapshot_id, decision, reason_codes,
               requested_quantity, requested_price, estimated_fill_price,
               estimated_slippage, market_exposure_before, wallet_exposure_before,
               portfolio_exposure_before, configured_limits_json, kill_switch_state,
               paper_mode, evidence_timestamp, evaluated_at, policy_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            risk_decision_id, paper_signal_decision_id, specialist_approval_id, source_trade_id,
            candidate_id, snapshot_id, decision, json.dumps(reason_codes),
            requested_quantity, requested_price, estimated_fill_price,
            estimated_slippage, exposure_before.get("per_market"),
            exposure_before.get("per_wallet"), exposure_before.get("global"),
            json.dumps(configured_limits), kill_switch_state, paper_mode,
            evidence_timestamp, now.isoformat(), runtime.policy_version,
        ),
    )
    return risk_decision_id


def consume_eligible_signal(
    db: Any,
    paper_signal_decision_id: int,
    runtime: ExecutionRuntime,
    *,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> ExecutionResult:
    """Canonical transition: eligible signal → risk decision → provenance order.

    This is the missing link the audit identified. It performs the full
    fail-closed revalidation and, only on ``allow``, atomically creates the
    order, fill, and position inside a single transaction.
    """
    now = now or _utcnow()
    result = ExecutionResult(
        paper_signal_decision_id=paper_signal_decision_id, status="blocked"
    )

    # ---- 0. Database safety guard --------------------------------------- #
    if not runtime.db_is_temporary and not runtime.allow_production_execution:
        result.rejection_reasons.append("db_not_isolated_temporary")
        rid = _persist_risk_decision(
            db, paper_signal_decision_id=paper_signal_decision_id,
            specialist_approval_id=-1, source_trade_id="", candidate_id=-1,
            snapshot_id=None, decision="block",
            reason_codes=result.rejection_reasons, requested_quantity=None,
            requested_price=None, estimated_fill_price=None, estimated_slippage=None,
            exposure_before={}, configured_limits=runtime.as_dict(),
            kill_switch_state=runtime.kill_switch_engaged, paper_mode=runtime.broker_mode,
            evidence_timestamp=None, runtime=runtime, now=now,
        )
        result.risk_decision_id = rid
        return result

    # ---- 1. Signal exists & eligible ------------------------------------ #
    signal = _fetch_signal_context(db, paper_signal_decision_id)
    if signal is None:
        result.rejection_reasons.append("signal_not_found")
        result.status = "blocked"
        result.risk_decision_id = _persist_risk_decision(
            db, paper_signal_decision_id=paper_signal_decision_id,
            specialist_approval_id=-1, source_trade_id="", candidate_id=-1,
            snapshot_id=None, decision="block", reason_codes=result.rejection_reasons,
            requested_quantity=None, requested_price=None, estimated_fill_price=None,
            estimated_slippage=None, exposure_before={}, configured_limits=runtime.as_dict(),
            kill_switch_state=runtime.kill_switch_engaged, paper_mode=runtime.broker_mode,
            evidence_timestamp=None, runtime=runtime, now=now)
        return result

    candidate_id = signal["candidate_id"]
    source_trade_id = signal["source_trade_id"] or ""
    snapshot_id = signal["price_snapshot_id"]
    wallet_id = signal["wallet_id"]

    reasons: list[str] = []

    # ---- 2. Signal verdict eligible ------------------------------------- #
    if signal["final_verdict"] != ELIGIBLE_SIGNAL_VERDICT:
        reasons.append(f"signal_verdict_not_eligible:{signal['final_verdict']}")

    # ---- 3. Explicit manual authorization gate -------------------------- #
    auth = get_active_authorization(db, paper_signal_decision_id)
    approval_id: str = "-1"
    if auth is None:
        reasons.append("no_active_execution_authorization")
    else:
        approval_id = str(auth["specialist_approval_id"])
        # The legacy is_approved column must agree it was never auto-granted.
        # (Read-only: we only assert it is 0, per PR4 invariant.)
        # ``signal`` is a sqlite3.Row here; normalize for the optional-key access.
        signal_d = dict(signal)
        if int(signal_d.get("is_approved", 0)) != 0:
            reasons.append("legacy_is_approved_nonzero:contract_violation")
        # Re-check approval still enabled & unrevoked.
        ap = db.fetchone(
            "SELECT enabled, revoked_at FROM specialist_approvals WHERE approval_id=?",
            (approval_id,),
        )
        if ap is None:
            reasons.append("approval_missing")
        elif int(ap["enabled"]) != 1 or ap["revoked_at"] is not None:
            reasons.append("approval_disabled_or_revoked")

    # ---- 4. Copyability eligible ---------------------------------------- #
    tc = db.fetchone(
        "SELECT id, verdict FROM trade_copyability_decisions WHERE candidate_id=? "
        "ORDER BY id DESC LIMIT 1",
        (candidate_id,),
    )
    if tc is None:
        reasons.append("copyability_decision_missing")
    elif tc["verdict"] != ELIGIBLE_COPYABILITY_VERDICT:
        reasons.append(f"copyability_not_eligible:{tc['verdict']}")

    # ---- 5. Source trade canonical & exists ----------------------------- #
    # paper_signal_decisions.source_trade_id carries the internal source_trades.id
    # (the canonical public identity is source_trades.source_trade_id; the loader
    # resolves the row by the internal id per paper_signal.py contract).
    st = db.fetchone(
        "SELECT id, side, market_source_id, resolution_status FROM source_trades "
        "WHERE id=?",
        (source_trade_id,),
    )
    if st is None:
        reasons.append("source_trade_missing")
        market_source_id = None
        st_d = None
    else:
        market_source_id = st["market_source_id"]
        st_d = dict(st)
        if str(st["side"] or "").upper() != "BUY":
            reasons.append("source_trade_not_buy")

    # ---- 6. Snapshot / depth evidence fresh ----------------------------- #
    fresh, fresh_reason = _is_snapshot_fresh(db, snapshot_id, runtime.snapshot_max_age_seconds, now)
    if not fresh:
        reasons.append(fresh_reason)
    depth = _load_depth(db, snapshot_id) if snapshot_id else None
    if depth is None or not depth.levels:
        reasons.append("depth_missing")

    # ---- 7. Market open/tradable/unresolved ----------------------------- #
    if st_d is not None and str(st_d.get("resolution_status") or "").lower() in {
        "won", "lost", "resolved"
    }:
        reasons.append("source_market_resolved")

    # ---- 8. Runtime guards ---------------------------------------------- #
    if not runtime.is_paper or runtime.is_live:
        reasons.append("runtime_not_paper_only")
    if runtime.broker_mode != "paper":
        reasons.append("broker_mode_not_paper")
    if runtime.kill_switch_engaged:
        reasons.append("kill_switch_engaged")

    # ---- 9. Conservative exposure limits configured (fail-closed) ------- #
    limits = {
        "max_order_size": runtime.max_order_size,
        "max_per_market": runtime.max_per_market,
        "max_per_wallet": runtime.max_per_wallet,
        "max_global": runtime.max_global,
    }
    if any(v <= 0 for v in limits.values()):
        reasons.append("exposure_limits_not_configured")

    # ---- 10. Exactly-once: already executed? ---------------------------- #
    existing_order = db.fetchone(
        "SELECT id FROM paper_orders WHERE paper_signal_decision_id=?",
        (paper_signal_decision_id,),
    )
    if existing_order is not None:
        # Idempotent replay: signal already executed. Return existing artifacts
        # without persisting a duplicate risk/order/fill/position.
        result.status = "already_executed"
        result.order_id = existing_order["id"]
        result.rejection_reasons.append("signal_already_executed")
        # Fetch the linked risk decision for the existing execution.
        # Normalize sqlite3.Row -> dict for optional-key access.
        erow = db.fetchone(
            "SELECT risk_decision_id FROM execution_risk_decisions "
            "WHERE paper_signal_decision_id=? ORDER BY evaluated_at DESC LIMIT 1",
            (paper_signal_decision_id,),
        )
        result.risk_decision_id = (dict(erow) if erow is not None else {}).get("risk_decision_id")
        existing_fill = db.fetchone(
            "SELECT fill_id FROM paper_fills WHERE order_id=?",
            (existing_order["id"],),
        )
        result.fill_id = (dict(existing_fill) if existing_fill is not None else {}).get("fill_id")
        existing_pos = db.fetchone(
            "SELECT id FROM paper_positions WHERE paper_order_id=?",
            (existing_order["id"],),
        )
        result.position_id = (dict(existing_pos) if existing_pos is not None else {}).get("id")
        return result

    # ---- Derive + validate requested quantity/price (fail-closed) ------- #
    exposure_before = _current_exposure(db, wallet_id, market_source_id)
    snap = db.fetchone(
        "SELECT source_trade_price, source_trade_quantity, best_ask, best_bid, mid_price "
        "FROM candidate_price_snapshots WHERE id=?",
        (snapshot_id,),
    )
    import math

    def _finite_pos(v: object) -> Optional[float]:
        if v is None:
            return None
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f) or f <= 0:
            return None
        return f

    requested_qty = _finite_pos(snap["source_trade_quantity"]) if snap else None
    requested_price = _finite_pos(snap["best_ask"]) if snap else None
    if requested_qty is None:
        reasons.append("invalid_request_quantity:missing_or_nonpositive")
    if requested_price is None:
        reasons.append("invalid_request_price:missing_or_nonpositive")
    if requested_qty is not None and requested_price is not None:
        # Cap notional at the configured max order size (fail-closed).
        if requested_qty * requested_price > runtime.max_order_size and runtime.max_order_size > 0:
            requested_qty = runtime.max_order_size / requested_price
    # ---- Persist risk decision (allow or blocked) ----------------------- #
    if reasons:
        rid = _persist_risk_decision(
            db, paper_signal_decision_id=paper_signal_decision_id,
            specialist_approval_id=approval_id,
            source_trade_id=source_trade_id, candidate_id=candidate_id,
            snapshot_id=snapshot_id, decision="block", reason_codes=reasons,
            requested_quantity=None, requested_price=None, estimated_fill_price=None,
            estimated_slippage=None, exposure_before=exposure_before,
            configured_limits=limits, kill_switch_state=runtime.kill_switch_engaged,
            paper_mode=runtime.broker_mode, evidence_timestamp=None, runtime=runtime,
            now=now)
        result.risk_decision_id = rid
        result.rejection_reasons = reasons
        return result

    # ---- ALLOW: compute fill, persist order/fill/position atomically ---- #
    result.rejection_reasons = []  # cleared; we proceed
    if dry_run:
        result.status = "dry_run_allowed"
        rid = _persist_risk_decision(
            db, paper_signal_decision_id=paper_signal_decision_id,
            specialist_approval_id=approval_id,
            source_trade_id=source_trade_id, candidate_id=candidate_id,
            snapshot_id=snapshot_id, decision="allow", reason_codes=["dry_run"],
            requested_quantity=None, requested_price=None, estimated_fill_price=None,
            estimated_slippage=None, exposure_before=exposure_before,
            configured_limits=limits, kill_switch_state=runtime.kill_switch_engaged,
            paper_mode=runtime.broker_mode, evidence_timestamp=None, runtime=runtime,
            now=now)
        result.risk_decision_id = rid
        return result

    from polycopy.risk.fill_model import FillModel
    quote = FillModel().quote_fill(
        side="buy", quantity=requested_qty, depth=depth,  # type: ignore[arg-type]
        is_sample=False,
    )
    if not quote.is_complete_fill:
        # Full-fill-or-reject model for milestone 1.
        rid = _persist_risk_decision(
            db, paper_signal_decision_id=paper_signal_decision_id,
            specialist_approval_id=approval_id,
            source_trade_id=source_trade_id, candidate_id=candidate_id, snapshot_id=snapshot_id,
            decision="block", reason_codes=["depth_insufficient_for_full_fill"],
            requested_quantity=requested_qty, requested_price=requested_price,
            estimated_fill_price=quote.expected_price, estimated_slippage=quote.slippage,
            exposure_before=exposure_before, configured_limits=limits,
            kill_switch_state=runtime.kill_switch_engaged, paper_mode=runtime.broker_mode,
            evidence_timestamp=now.isoformat(), runtime=runtime, now=now)
        result.risk_decision_id = rid
        result.rejection_reasons = ["depth_insufficient_for_full_fill"]
        return result

    # Estimate exposure-after and verify against limits BEFORE writing.
    # At this point requested_qty/requested_price are guaranteed finite-positive
    # (invalid values were rejected into the BLOCKED branch above).
    assert requested_qty is not None and requested_price is not None, "unreachable: invalid qty rejected earlier"
    qty_f = float(requested_qty)
    exposure_after_global = exposure_before["global"] + qty_f * quote.expected_price
    exposure_after_wallet = exposure_before["per_wallet"] + qty_f * quote.expected_price
    exposure_after_market = exposure_before["per_market"] + qty_f * quote.expected_price
    limit_breaches = []
    if runtime.max_order_size > 0 and qty_f * quote.expected_price > runtime.max_order_size:
        limit_breaches.append("max_order_size_exceeded")
    if runtime.max_per_wallet > 0 and exposure_after_wallet > runtime.max_per_wallet:
        limit_breaches.append("max_per_wallet_exceeded")
    if runtime.max_per_market > 0 and exposure_after_market > runtime.max_per_market:
        limit_breaches.append("max_per_market_exceeded")
    if runtime.max_global > 0 and exposure_after_global > runtime.max_global:
        limit_breaches.append("max_global_exceeded")
    if limit_breaches:
        rid = _persist_risk_decision(
            db, paper_signal_decision_id=paper_signal_decision_id,
            specialist_approval_id=approval_id,
            source_trade_id=source_trade_id, candidate_id=candidate_id, snapshot_id=snapshot_id,
            decision="block", reason_codes=limit_breaches, requested_quantity=requested_qty,
            requested_price=requested_price, estimated_fill_price=quote.expected_price,
            estimated_slippage=quote.slippage, exposure_before=exposure_before,
            configured_limits=limits, kill_switch_state=runtime.kill_switch_engaged,
            paper_mode=runtime.broker_mode, evidence_timestamp=now.isoformat(), runtime=runtime,
            now=now)
        result.risk_decision_id = rid
        result.rejection_reasons = limit_breaches
        return result

    # ---- Atomic write: risk(allow) + order + fill + position + lot ------ #
    rid = _persist_risk_decision(
        db, paper_signal_decision_id=paper_signal_decision_id,
        specialist_approval_id=approval_id,
        source_trade_id=source_trade_id, candidate_id=candidate_id, snapshot_id=snapshot_id,
        decision="allow", reason_codes=["all_checks_passed"], requested_quantity=requested_qty,
        requested_price=requested_price, estimated_fill_price=quote.expected_price,
        estimated_slippage=quote.slippage, exposure_before=exposure_before,
        configured_limits=limits, kill_switch_state=runtime.kill_switch_engaged,
        paper_mode=runtime.broker_mode, evidence_timestamp=now.isoformat(), runtime=runtime,
        now=now)

    order_id = str(uuid.uuid4())
    filled_at = now.isoformat()
    fill_id = None
    position_id = None
    try:
        db.execute(
            """INSERT INTO paper_orders (
                   id, paper_signal_decision_id, specialist_approval_id, source_trade_internal_id,
                   copy_candidate_id, candidate_price_snapshot_id, trade_copyability_decision_id,
                   execution_risk_decision_id, source_wallet_id, wallet_id, market_id,
                   side, outcome, quantity, price, requested_quantity, requested_price,
                   status, fill_model_version, policy_version, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, 'BUY', 'Yes', ?,?,?,?, 'filled', 'fill_model_v1', ?, ?)""",
            (
                order_id, paper_signal_decision_id, approval_id,
                source_trade_id, candidate_id, snapshot_id,
                int(tc["id"]) if tc else None, rid, wallet_id, wallet_id, market_source_id,
                requested_qty, requested_price, requested_qty, requested_price,
                runtime.policy_version, now.isoformat(),
            ),
        )
        # Fill (durable, distinct).
        fill_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO paper_fills (
                   fill_id, order_id, quantity, price, fee, slippage, fill_model_version, filled_at)
               VALUES (?,?,?,?,?,?, 'fill_model_v1', ?)""",
            (
                fill_id, order_id, quote.fillable_volume, quote.expected_price, quote.fee,
                quote.slippage, filled_at,
            ),
        )
        # Position (provenance-linked; one-entry position + lot).
        position_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO paper_positions (
                   id, market_id, wallet_id, outcome, quantity, avg_entry_price,
                   current_price, realized_pnl, source_wallet_id, source_trade_internal_id,
                   copy_candidate_id, paper_order_id, paper_fill_id,
                   paper_signal_decision_id, execution_risk_decision_id, opened_at, updated_at)
               VALUES (?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)""",
            (
                position_id, market_source_id, wallet_id, "Yes", requested_qty,
                quote.expected_price, quote.expected_price, wallet_id, source_trade_id,
                candidate_id, order_id, fill_id, paper_signal_decision_id, rid,
                filled_at, now.isoformat(),
            ),
        )
        # Position lot (provenance to the fill).
        lot_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO paper_position_lots (
                   id, position_id, paper_fill_id, quantity, entry_price, opened_at)
               VALUES (?,?,?,?,?,?)""",
            (lot_id, position_id, fill_id, requested_qty, quote.expected_price, filled_at),
        )
        # Consume the authorization now that the order is durably committed.
        consume_authorization(db, auth["authorization_id"], now=now)
        db.commit()
        result.status = "executed"
        result.order_id = order_id
        result.fill_id = fill_id
        result.position_id = position_id
        result.risk_decision_id = rid
        result.detail = {
            "requested_quantity": requested_qty,
            "fill_price": quote.expected_price,
            "fee": quote.fee,
            "slippage": quote.slippage,
            "exposure_after": {
                "global": exposure_after_global,
                "wallet": exposure_after_wallet,
                "market": exposure_after_market,
            },
        }
        logger.info("Specialist paper execution committed: order=%s position=%s", order_id, position_id)
    except Exception as exc:  # pragma: no cover - defensive; transaction must roll back
        db.rollback()
        logger.exception("Specialist paper execution aborted; rolled back")
        result.status = "blocked"
        result.rejection_reasons.append(f"execution_write_failed:{type(exc).__name__}")
    return result


# --------------------------------------------------------------------------- #
# 3) Marking (canonical path)                                                  #
# --------------------------------------------------------------------------- #
def mark_specialist_position(
    db: Any,
    position_id: str,
    *,
    mark_price: float,
    bid_price: float,
    ask_price: float,
    evidence_source: str = "test_fixture",
    conservative: bool = False,
    now: Optional[datetime] = None,
) -> MarkResult:
    """Mark one specialist paper position using authoritative evidence.

    Delegates the computation to ``MarkEngine`` and persists the result into
    ``paper_position_marks``. Missing evidence must never invent a value — the
    caller supplies the mark; this function records it honestly.
    """
    now = now or _utcnow()
    pos = db.fetchone(
        "SELECT * FROM paper_positions WHERE id=?", (position_id,)
    )
    if pos is None:
        return MarkResult(position_id=position_id, mark_id=None, mark_price=None,
                           unrealized_pnl=None, status="skipped_no_evidence")
    from uuid import UUID
    from polycopy.risk.marks import MarkEngine, MarkPrice
    from polycopy.risk.fill_model import FillModel  # noqa: F401  (kept for parity)

    mid = (bid_price + ask_price) / 2.0
    mark = MarkPrice(
        market_id=UUID(int=0),  # placeholder; not used for persistence identity
        outcome=pos["outcome"],
        mark_price=mark_price if mark_price is not None else mid,
        bid_price=bid_price,
        ask_price=ask_price,
        source=evidence_source,
        observed_at=now,
        is_sample=False,
    )
    engine = MarkEngine(use_conservative_mark=conservative)
    engine.update_price(mark)
    pm = engine.mark_position(
        position_id=UUID(int=0),
        market_id=UUID(int=0),
        wallet_id=UUID(int=0),
        outcome=pos["outcome"],
        quantity=float(pos["quantity"]),
        avg_entry_price=float(pos["avg_entry_price"]),
    )
    if pm is None:
        return MarkResult(position_id=position_id, mark_id=None, mark_price=None,
                           unrealized_pnl=None, status="skipped_no_evidence")
    mark_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO paper_position_marks (
               id, position_id, mark_price, bid_price, ask_price, source,
               observed_at, unrealized_pnl, is_sample)
           VALUES (?,?,?,?,?,?,?,?,0)""",
        (
            mark_id, position_id, pm.mark_price, bid_price, ask_price, evidence_source,
            now.isoformat(), pm.unrealized_pnl,
        ),
    )
    return MarkResult(
        position_id=position_id, mark_id=mark_id, mark_price=pm.mark_price,
        unrealized_pnl=pm.unrealized_pnl, status="marked",
    )


# --------------------------------------------------------------------------- #
# 4) Settlement (canonical, exactly-once)                                      #
# --------------------------------------------------------------------------- #
def settle_specialist_position(
    db: Any,
    position_id: str,
    *,
    resolution_outcome: str,
    evidence_source: str,
    raw_evidence: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> SettlementOutcome:
    """Settle one specialist paper position exactly once.

    Uses ``SettlementEngine`` for idempotency/conflict logic and persists the
    result into ``paper_position_settlements`` (linked to order+fill, not just source
    trade). Missing position → blocked. Already settled → returns existing.
    """
    now = now or _utcnow()
    pos = db.fetchone("SELECT * FROM paper_positions WHERE id=?", (position_id,))
    if pos is None:
        return SettlementOutcome(position_id=position_id, settlement_id=None,
                                  status="blocked", reason="position_not_found")
    # Exactly-once: if a settlement for this position already exists, return it.
    existing = db.fetchone(
        "SELECT * FROM paper_position_settlements WHERE position_id=? ORDER BY id DESC LIMIT 1",
        (position_id,),
    )
    if existing:
        return SettlementOutcome(
            position_id=position_id, settlement_id=int(existing["id"]),
            status="already_settled", is_winner=bool(existing["is_winner"]),
            payout=float(existing["payout"]), realized_pnl=float(existing["realized_pnl"]),
            reason="already_settled",
        )
    from uuid import UUID
    from polycopy.risk.settlement import SettlementEngine, SettlementEvidence

    evidence = SettlementEvidence(
        source=evidence_source,
        market_source_id=pos["market_id"] or "",
        resolution_outcome=resolution_outcome,
        raw_evidence=raw_evidence or {},
        observed_at=now,
    )
    engine = SettlementEngine()
    # Reconstruct position identity in engine (placeholder UUIDs; engine keys on
    # position_id string only through evidence_key, which uses position_id).
    res = engine.settle_position(
        position_id=UUID(int=0),
        market_id=UUID(int=0),
        wallet_id=UUID(int=0),
        outcome=pos["outcome"],
        quantity=float(pos["quantity"]),
        avg_entry_price=float(pos["avg_entry_price"]),
        evidence=evidence,
        is_sample=False,
    )
    # Persist (idempotent on evidence_key unique constraint).
    try:
        db.execute(
            """INSERT INTO paper_position_settlements (
                   position_id, paper_order_id, paper_fill_id, market_source_id,
                   outcome, resolution_outcome, is_winner, payout, realized_pnl,
                   fee, evidence_source, evidence_hash, settled_at)
               VALUES (?,?,?,?,?,?,?,?,?, (SELECT COALESCE(fee,0.0) FROM paper_fills
                   WHERE fill_id=(SELECT paper_fill_id FROM paper_positions WHERE id=?)),
                   ?,?,?)""",
            (
                position_id, pos["paper_order_id"], pos["paper_fill_id"],
                pos["market_id"], pos["outcome"], resolution_outcome,
                int(res.is_winner), res.payout, res.payout - float(pos["quantity"]) * float(pos["avg_entry_price"]),
                position_id, evidence_source, evidence.evidence_hash, now.isoformat(),
            ),
        )
        sid = int(db.lastrowid())
    except Exception:
        # Unique constraint on (position_id, evidence_hash) → already settled.
        row = db.fetchone(
            "SELECT * FROM paper_position_settlements WHERE position_id=? AND evidence_hash=?",
            (position_id, evidence.evidence_hash),
        )
        if row is None:
            raise
        sid = int(row["id"])
        res.is_winner = bool(row["is_winner"])
        res.payout = float(row["payout"])
    # Mark position settled (does NOT delete the only position record).
    db.execute(
        "UPDATE paper_positions SET status='settled', settled_at=?, updated_at=? WHERE id=?",
        (now.isoformat(), now.isoformat(), position_id),
    )
    db.commit()
    realized = res.payout - float(pos["quantity"]) * float(pos["avg_entry_price"])
    return SettlementOutcome(
        position_id=position_id, settlement_id=sid, status="settled",
        is_winner=res.is_winner, payout=res.payout, realized_pnl=realized,
        reason="settled",
    )


__all__ = [
    "ELIGIBLE_SIGNAL_VERDICT", "ELIGIBLE_COPYABILITY_VERDICT",
    "RES_WIN", "RES_LOSS", "RES_VOID", "RES_CONFLICT", "RES_ERROR", "RES_UNRESOLVED",
    "ExecutionResult", "MarkResult", "SettlementOutcome", "ExecutionRuntime",
    "create_execution_authorization", "get_active_authorization", "consume_authorization",
    "consume_eligible_signal", "mark_specialist_position", "settle_specialist_position",
]
