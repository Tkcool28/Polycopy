"""Durable dispatcher: approved source trade -> enrichment -> bridge -> signal.

This module owns the operational path between a durable specialist approval and
the proven Pass 1 execution spine. It does NOT execute orders or positions; it
stops at producing a ``copy_candidate`` paper signal (``execution_pending``
dispatch status). The execution spine (Pass 1) consumes that signal separately.

Strict ownership boundaries:
  * Reads ONLY enabled, non-revoked approvals.
  * Matches source-trade wallet to the approval wallet.
  * Matches candidate category to the approval category (via enrichment).
  * BUY-only (inherited from the bridge selection).
  * Enriches BEFORE the bridge; never invokes the bridge if enrichment is
    incomplete.
  * Invokes the existing canonical bridge (``process_approved_wallet_trades``).
  * Does NOT duplicate candidates/snapshots/decisions/signals on replay — the
    bridge's anti-replay cursor and the dispatch UNIQUE(approval, source_trade)
    guard both protect this.
  * Persists incomplete/failure status; increments attempt count safely.
  * Marks bridge completion only after a successful bridge transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from polycopy.execution.specialist_approval import (
    get_active_approval,
    get_approval,
)
from polycopy.ingestion.source_trade_enrichment import (
    STATUS_COMPLETE,
    enrich_source_trade,
)
from polycopy.engine.approved_wallet_trade_bridge import (
    BridgeDependencies,
    _issue_write_capability,
    process_approved_wallet_trades,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    from uuid import uuid4

    return str(uuid4())


# Dispatch status machine.
DISPATCH_PENDING = "pending"
DISPATCH_ENRICHMENT_INCOMPLETE = "enrichment_incomplete"
DISPATCH_READY_FOR_BRIDGE = "ready_for_bridge"
DISPATCH_BRIDGE_COMPLETE = "bridge_complete"
DISPATCH_EXECUTION_PENDING = "execution_pending"
DISPATCH_COMPLETE = "complete"
DISPATCH_FAILED = "failed"


def _select_source_trades_for_wallet(
    db: Any, wallet_address: str, *, limit: int, exclude_dispatched: bool = True,
) -> list[dict[str, Any]]:
    """Return BUY source trades for the wallet, optionally excluding already
    dispatched internal ids. Uses the canonical source_trades columns."""
    where = [
        "source = ?",
        "lower(trader_address) = ?",
        "side = 'BUY'",
        "COALESCE(is_sample, 0) = 0",
    ]
    params: list[Any] = ["polymarket_data_api_trades_user", wallet_address.lower()]
    if exclude_dispatched:
        where.append(
            "st.id NOT IN (SELECT source_trade_internal_id FROM "
            "approved_specialist_trade_dispatches)"
        )
    sql = ("SELECT st.id, st.source_trade_id, st.market_source_id, st.trader_address "
           "FROM source_trades st WHERE " + " AND ".join(where) +
           " ORDER BY st.timestamp ASC, st.id ASC LIMIT ?")
    params.append(limit)
    rows = db.fetchall(sql, tuple(params))
    return [dict(r) for r in rows]


def get_dispatch(db: Any, dispatch_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT * FROM approved_specialist_trade_dispatches WHERE dispatch_id=?",
        (dispatch_id,),
    )
    return dict(row) if row is not None else None


def _find_existing_dispatch(db: Any, approval_id: str, source_trade_internal_id: str):
    row = db.fetchone(
        "SELECT * FROM approved_specialist_trade_dispatches "
        "WHERE specialist_approval_id=? AND source_trade_internal_id=?",
        (approval_id, source_trade_internal_id),
    )
    return dict(row) if row is not None else None


def _create_dispatch(db: Any, approval_id: str, source_trade_internal_id: str,
                    *, wallet: str, category: str) -> str:
    now = _now_iso()
    did = _uuid()
    db.conn.execute(
        """INSERT INTO approved_specialist_trade_dispatches (
               dispatch_id, specialist_approval_id, source_trade_internal_id,
               wallet, category, status, attempt_count, last_attempt_at,
               created_at, updated_at
           ) VALUES (?,?,?,?,?,?,0,?,?,?)""",
        (did, approval_id, source_trade_internal_id, wallet, category,
         DISPATCH_PENDING, now, now, now),
    )
    db.conn.commit()
    return did


def _set_dispatch(
    db: Any, dispatch_id: str, *, status: str,
    enrichment_id: Optional[str] = None,
    candidate_id: Optional[int] = None,
    paper_signal_decision_id: Optional[int] = None,
    reason_codes: Optional[list[str]] = None,
    error_message: Optional[str] = None,
    increment_attempt: bool = False,
    completed: bool = False,
) -> None:
    now = _now_iso()
    sets = ["status=?", "updated_at=?", "last_attempt_at=?"]
    params: list[Any] = [status, now, now]
    if enrichment_id is not None:
        sets.append("enrichment_id=?")
        params.append(enrichment_id)
    if candidate_id is not None:
        sets.append("candidate_id=?")
        params.append(candidate_id)
    if paper_signal_decision_id is not None:
        sets.append("paper_signal_decision_id=?")
        params.append(paper_signal_decision_id)
    if reason_codes is not None:
        sets.append("reason_codes_json=?")
        params.append(json.dumps(reason_codes, sort_keys=True))
    if error_message is not None:
        sets.append("error_message=?")
        params.append(error_message)
    if increment_attempt:
        sets.append("attempt_count = attempt_count + 1")
    if completed:
        sets.append("completed_at=?")
        params.append(now)
    params.append(dispatch_id)
    db.conn.execute(
        f"UPDATE approved_specialist_trade_dispatches SET {', '.join(sets)} "
        "WHERE dispatch_id=?",
        tuple(params),
    )
    db.conn.commit()


@dataclass
class DispatchResult:
    dispatch_id: str
    source_trade_internal_id: str
    enrichment_id: Optional[str]
    enrichment_status: Optional[str]
    status: str
    candidate_id: Optional[int]
    paper_signal_decision_id: Optional[int]
    paper_signal_verdict: Optional[str]
    reason_codes: list[str] = field(default_factory=list)
    error_message: Optional[str] = None
    created: bool = False
    wrote: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "source_trade_internal_id": self.source_trade_internal_id,
            "enrichment_id": self.enrichment_id,
            "enrichment_status": self.enrichment_status,
            "status": self.status,
            "candidate_id": self.candidate_id,
            "paper_signal_decision_id": self.paper_signal_decision_id,
            "paper_signal_verdict": self.paper_signal_verdict,
            "reason_codes": self.reason_codes,
            "error_message": self.error_message,
            "created": self.created,
            "wrote": self.wrote,
        }


def _resolve_approval(db: Any, *, approval_id: Optional[str],
                      wallet_address: Optional[str], category: Optional[str],
                      formula_version: str) -> Optional[Any]:
    if approval_id is not None:
        try:
            rec = get_approval(db, approval_id)
        except KeyError:
            return None
        # Disabled or revoked approvals are NOT dispatchable. The monitor and
        # collector read the same enabled/non-revoked gate; the dispatcher must
        # enforce it too so a stale approval can never mint a signal.
        if not rec.enabled or rec.revoked_at is not None:
            return None
        return rec
    if wallet_address is not None and category is not None:
        return get_active_approval(
            db, wallet_address=wallet_address,
            specialist_category=category, formula_version=formula_version,
        )
    return None


def dispatch_one(
    db: Any,
    *,
    approval_id: Optional[str] = None,
    source_trade_internal_id: Optional[str] = None,
    gamma_resolver: Optional[Any] = None,
    clob_provider: Optional[Any] = None,
    dry_run: bool = False,
    formula_version: str = "1",
) -> DispatchResult:
    """Dispatch exactly one approved source trade through enrichment + bridge.

    Returns a DispatchResult. On a dry run, no dispatch/enrichment rows are
    written and the bridge is never invoked (the result reports what WOULD
    happen). On a real run, the dispatch + enrichment are persisted and the
    bridge is invoked (writing candidate/snapshot/decision rows).
    """
    approval = _resolve_approval(
        db, approval_id=approval_id,
        wallet_address=None, category=None, formula_version=formula_version,
    )
    if approval is None:
        return DispatchResult(
            dispatch_id="", source_trade_internal_id=source_trade_internal_id or "",
            enrichment_id=None, enrichment_status=None, status=DISPATCH_FAILED,
            candidate_id=None, paper_signal_decision_id=None,
            paper_signal_verdict=None,
            reason_codes=["approval_not_found_or_inactive"],
            error_message="no active approval resolved",
        )

    # Wallet/category match is enforced by using the approval's own wallet +
    # category. Source trades are selected for the approval wallet.
    wallet = approval.wallet_address
    category = approval.specialist_category

    # Resolve the exact source trade, or pick the next undispatched one.
    if source_trade_internal_id is not None:
        row = db.fetchone(
            "SELECT id, trader_address FROM source_trades WHERE id=? AND side='BUY' "
            "AND lower(trader_address)=? AND COALESCE(is_sample,0)=0",
            (source_trade_internal_id, wallet.lower()),
        )
        if row is None:
            return DispatchResult(
                dispatch_id="", source_trade_internal_id=source_trade_internal_id,
                enrichment_id=None, enrichment_status=None, status=DISPATCH_FAILED,
                candidate_id=None, paper_signal_decision_id=None,
                paper_signal_verdict=None,
                reason_codes=["wallet_or_side_mismatch"],
                error_message="source trade does not match approval wallet / BUY",
            )
        trades = [{"id": row["id"]}]
    else:
        trades = _select_source_trades_for_wallet(db, wallet, limit=1)

    if not trades:
        return DispatchResult(
            dispatch_id="", source_trade_internal_id="",
            enrichment_id=None, enrichment_status=None, status=DISPATCH_FAILED,
            candidate_id=None, paper_signal_decision_id=None,
            paper_signal_verdict=None,
            reason_codes=["no_eligible_source_trade"],
            error_message="no undispatched BUY source trade for approval wallet",
        )

    st_id = trades[0]["id"]

    # Idempotent dispatch record (UNIQUE(approval, source_trade)).
    existing = _find_existing_dispatch(db, approval.approval_id, st_id)
    if existing is not None:
        # Replay: return existing state (no duplicate).
        return _result_from_row(db, existing, created=False, wrote=False,
                                dry_run=dry_run)
    if dry_run:
        return DispatchResult(
            dispatch_id="", source_trade_internal_id=st_id, enrichment_id=None,
            enrichment_status=None, status=DISPATCH_READY_FOR_BRIDGE,
            candidate_id=None, paper_signal_decision_id=None,
            paper_signal_verdict=None,
            reason_codes=["dry_run"], wrote=False, created=False,
        )
    dispatch_id = _create_dispatch(
        db, approval.approval_id, st_id,
        wallet=approval.wallet_address, category=approval.specialist_category,
    )

    # ── Enrich BEFORE bridge ──
    enrichment = enrich_source_trade(db, st_id, gamma_resolver=gamma_resolver,
                                     dry_run=False)
    if enrichment.status != STATUS_COMPLETE:
        _set_dispatch(
            db, dispatch_id, status=DISPATCH_ENRICHMENT_INCOMPLETE,
            enrichment_id=enrichment.enrichment_id,
            reason_codes=["enrichment_incomplete"] + enrichment.reason_codes,
            increment_attempt=True,
        )
        # No bridge call when enrichment is incomplete.
        return DispatchResult(
            dispatch_id=dispatch_id, source_trade_internal_id=st_id,
            enrichment_id=enrichment.enrichment_id,
            enrichment_status=enrichment.status, status=DISPATCH_ENRICHMENT_INCOMPLETE,
            candidate_id=None, paper_signal_decision_id=None,
            paper_signal_verdict=None,
            reason_codes=["enrichment_incomplete"] + enrichment.reason_codes,
            created=True, wrote=True,
        )

    _set_dispatch(db, dispatch_id, status=DISPATCH_READY_FOR_BRIDGE,
                  enrichment_id=enrichment.enrichment_id, increment_attempt=True)

    # Category guard: only proceed if enriched category matches approval.
    if category and enrichment.evidence.get("normalized_category") != category:
        _set_dispatch(
            db, dispatch_id, status=DISPATCH_FAILED,
            enrichment_id=enrichment.enrichment_id,
            reason_codes=["category_mismatch",
                          enrichment.evidence.get("normalized_category", "none")],
            error_message=f"enriched category does not match approval category {category}",
            increment_attempt=True,
        )
        return DispatchResult(
            dispatch_id=dispatch_id, source_trade_internal_id=st_id,
            enrichment_id=enrichment.enrichment_id,
            enrichment_status=enrichment.status, status=DISPATCH_FAILED,
            candidate_id=None, paper_signal_decision_id=None,
            paper_signal_verdict=None,
            reason_codes=["category_mismatch"], created=True, wrote=True,
        )

    # ── Invoke the canonical bridge (writes candidate/snapshot/decisions) ──
    stored = db.fetchone(
        "SELECT source_trade_id FROM source_trades WHERE id=?", (st_id,)
    )
    stored_sid = stored["source_trade_id"] if stored else None
    deps = _build_dependencies(gamma_resolver, clob_provider)

    rep = process_approved_wallet_trades(
        db, wallet=wallet, limit=1, dependencies=deps,
        write=True, write_authorization=_issue_write_capability(),
        source_trade_id=stored_sid, evaluate_canonical_decisions=True,
    )
    rows = rep.as_dict().get("rows", [])
    row0 = rows[0] if rows else None
    candidate_id = int(row0["candidate_id"]) if row0 and row0.get("candidate_id") else None
    psd_id = int(row0["paper_signal_decision_id"]) if row0 and row0.get("paper_signal_decision_id") else None
    verdict = row0.get("paper_signal_verdict") if row0 else None

    if candidate_id is None or psd_id is None:
        _set_dispatch(
            db, dispatch_id, status=DISPATCH_FAILED,
            enrichment_id=enrichment.enrichment_id,
            candidate_id=candidate_id, paper_signal_decision_id=psd_id,
            reason_codes=["bridge_produced_no_signal"],
            error_message="bridge did not produce a candidate/signal",
            increment_attempt=True,
        )
        return DispatchResult(
            dispatch_id=dispatch_id, source_trade_internal_id=st_id,
            enrichment_id=enrichment.enrichment_id,
            enrichment_status=enrichment.status, status=DISPATCH_FAILED,
            candidate_id=candidate_id, paper_signal_decision_id=psd_id,
            paper_signal_verdict=verdict,
            reason_codes=["bridge_produced_no_signal"], created=True, wrote=True,
        )

    # Mark bridge completion; a copy_candidate signal is execution-ready.
    final_status = DISPATCH_EXECUTION_PENDING if verdict == "copy_candidate" else DISPATCH_BRIDGE_COMPLETE
    _set_dispatch(
        db, dispatch_id, status=final_status,
        enrichment_id=enrichment.enrichment_id,
        candidate_id=candidate_id, paper_signal_decision_id=psd_id,
        reason_codes=[f"verdict:{verdict}"], completed=(final_status == DISPATCH_EXECUTION_PENDING),
        increment_attempt=True,
    )
    return DispatchResult(
        dispatch_id=dispatch_id, source_trade_internal_id=st_id,
        enrichment_id=enrichment.enrichment_id,
        enrichment_status=enrichment.status, status=final_status,
        candidate_id=candidate_id, paper_signal_decision_id=psd_id,
        paper_signal_verdict=verdict, created=True, wrote=True,
    )


def _build_dependencies(gamma_resolver, clob_provider=None):
    """Build a BridgeDependencies from a gamma resolver callable (+ clob).

    The dispatcher's ``gamma_resolver`` is a ``Callable[[condition_id] -> market|None``
    (the same contract enrichment uses; see ``_raw_gamma_resolver_adapter``).
    ``clob_provider`` is the bridge's optional book provider (None is allowed
    for dry-run / read-only paths but the bridge SKIPS a row without a book).
    Wrap both into the bridge's GammaProvider/ClobProvider. A ``BridgeDependencies``
    is returned as-is."""
    if gamma_resolver is None:
        raise ValueError("dispatcher requires a gamma resolver (test or live)")
    if isinstance(gamma_resolver, BridgeDependencies):
        return gamma_resolver

    class _Provider:
        def get_market(self, condition_id: str):
            return gamma_resolver(condition_id)

    return BridgeDependencies(gamma=_Provider(), clob=clob_provider)


def _result_from_row(db: Any, row: dict[str, Any], *, created: bool,
                     wrote: bool, dry_run: bool) -> DispatchResult:
    psd_id = row.get("paper_signal_decision_id")
    verdict = None
    if psd_id is not None:
        pr = db.fetchone(
            "SELECT final_verdict FROM paper_signal_decisions WHERE id=?", (psd_id,)
        )
        verdict = pr["final_verdict"] if pr else None
    rc = []
    if row.get("reason_codes_json"):
        try:
            rc = json.loads(row["reason_codes_json"])
        except (json.JSONDecodeError, ValueError):
            rc = []
    return DispatchResult(
        dispatch_id=row["dispatch_id"],
        source_trade_internal_id=row["source_trade_internal_id"],
        enrichment_id=row.get("enrichment_id"),
        enrichment_status=None,
        status=row["status"],
        candidate_id=row.get("candidate_id"),
        paper_signal_decision_id=psd_id,
        paper_signal_verdict=verdict,
        reason_codes=rc,
        error_message=row.get("error_message"),
        created=created, wrote=wrote,
    )


def dispatch_batch(
    db: Any,
    *,
    approval_id: Optional[str] = None,
    source_trade_internal_id: Optional[str] = None,
    limit: int = 1,
    gamma_resolver: Optional[Any] = None,
    clob_provider: Optional[Any] = None,
    dry_run: bool = False,
    formula_version: str = "1",
) -> list[DispatchResult]:
    """Bounded batch dispatcher. With an explicit source_trade_internal_id, the
    limit is forced to 1. Otherwise dispatches up to ``limit`` undispatched
    trades for the resolved approval."""
    if source_trade_internal_id is not None:
        return [dispatch_one(
            db, approval_id=approval_id, source_trade_internal_id=source_trade_internal_id,
            gamma_resolver=gamma_resolver, dry_run=dry_run,
            formula_version=formula_version,
        )]
    results: list[DispatchResult] = []
    for _ in range(max(1, limit)):
        res = dispatch_one(
            db, approval_id=approval_id, gamma_resolver=gamma_resolver,
            dry_run=dry_run, formula_version=formula_version,
        )
        results.append(res)
        if res.status in (DISPATCH_FAILED, DISPATCH_ENRICHMENT_INCOMPLETE) or not res.dispatch_id:
            break
        if res.reason_codes and res.reason_codes[0] == "no_eligible_source_trade":
            break
    return results
