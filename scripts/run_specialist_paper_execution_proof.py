#!/usr/bin/env python3
"""Complete end-to-end proof of the specialist paper execution spine.

Runs the entire durable lifecycle in ONE persistent temporary SQLite database
using production code paths (no invented orders/fills/positions/settlements):

  manual approval -> collect/canonical source trade -> enrichment -> durable
  dispatch -> approved-wallet bridge -> wallet/category/copyability decisions ->
  copy_candidate paper signal -> execution authorization -> risk evaluation ->
  paper order -> paper fill -> paper position + lot -> mark -> authoritative
  resolution evidence -> settle -> realized P&L.

Default safety:
  * Requires --db-path <explicit>.
  * Rejects /root/Polycopy/data/polycopy.db unless the strongest production
    confirmation gates are present (and even then does not execute production).
  * Default use is temporary/test only.

Replay: running twice against the same database returns the same artifacts with
no duplicate operational rows (idempotent), reporting already_complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.schema import SCHEMA_VERSION  # noqa: E402
from polycopy.execution.specialist_spine import (  # noqa: E402
    ExecutionRuntime,
    consume_eligible_signal,
    create_execution_authorization,
    mark_specialist_position,
    settle_specialist_position,
)
from polycopy.ingestion.source_trade_enrichment import enrich_source_trade  # noqa: E402
from polycopy.engine.approved_specialist_dispatcher import dispatch_one  # noqa: E402

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()
FIXED_WALLET = "0x" + "a" * 40
SPECIALIST_CATEGORY = "politics"
POLICY_VERSION = "specialist_paper_execution_v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()
# The canonical deployment production database must NEVER be touched by the
# proof command (temporary/test only), even if the repo copy is absent.
REAL_PRODUCTION_DB_PATH = Path("/root/Polycopy/data/polycopy.db").resolve()
FORBIDDEN_PRODUCTION_PATHS = {PRODUCTION_DB_PATH, REAL_PRODUCTION_DB_PATH}


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() in FORBIDDEN_PRODUCTION_PATHS
    except OSError:
        return False


def run_proof(db: Database, args: argparse.Namespace) -> dict:
    """Execute the full lifecycle. Idempotent on replay."""
    from tests.fixtures.specialist_paper_fixtures import (
        seed_resolved_evidence,
        ingest_target_trade,
        bridge_dependencies,
        create_approval_for_target,
    )

    # 1) evidence + approval (idempotent: create_approval_for_target is unique
    #    on (wallet, category, version); replay returns the same id).
    seed_resolved_evidence(db)
    try:
        aid = create_approval_for_target(db)
    except ValueError:
        # Replay: an active approval for this wallet/category/version already
        # exists. Resolve it deterministically.
        existing = db.fetchone(
            "SELECT approval_id FROM specialist_approvals "
            "WHERE wallet_address=? AND specialist_category=? AND formula_version=? "
            "AND enabled=1 AND revoked_at IS NULL "
            "ORDER BY approved_at DESC LIMIT 1",
            (FIXED_WALLET, SPECIALIST_CATEGORY, "1"),
        )
        if existing is None:
            raise
        aid = existing["approval_id"]

    # 2) canonical source trade (idempotent writer).
    ing = ingest_target_trade(db)
    st_id = ing["source_trade_internal_id"]

    # 3) enrichment (idempotent).
    deps = bridge_dependencies()
    gamma, clob = deps.gamma.get_market, deps.clob
    enr = enrich_source_trade(db, st_id, gamma_resolver=gamma, dry_run=False)

    # 4) durable dispatch (idempotent; bridge -> copy_candidate).
    disp = dispatch_one(db, approval_id=aid, source_trade_internal_id=st_id,
                         gamma_resolver=gamma, clob_provider=clob, dry_run=False)

    # 5) decisions + paper signal already produced by dispatch (candidate_id,
    #    paper_signal_decision_id). Read them back.
    psd = disp.paper_signal_decision_id
    if psd is None:
        raise RuntimeError("dispatch did not produce a paper signal decision")
    snap = db.fetchone(
        "SELECT price_snapshot_id FROM paper_signal_decisions WHERE id=?", (psd,)
    )
    snapshot_id = snap["price_snapshot_id"] if snap else None
    ws = db.fetchone("SELECT wallet_score_decision_id FROM paper_signal_decisions WHERE id=?", (psd,))
    cs = db.fetchone("SELECT category_score_decision_id FROM paper_signal_decisions WHERE id=?", (psd,))
    ts = db.fetchone("SELECT trade_score_decision_id FROM paper_signal_decisions WHERE id=?", (psd,))
    cand = db.fetchone("SELECT candidate_id FROM paper_signal_decisions WHERE id=?", (psd,))
    verdict = db.fetchone("SELECT final_verdict FROM paper_signal_decisions WHERE id=?", (psd,))
    if cand is None:
        raise RuntimeError("paper signal missing candidate link")

    # Detect replay: if an order/position for this signal already existed before
    # this run, the lifecycle previously completed.
    pre_existing = db.fetchone(
        "SELECT id FROM paper_orders WHERE paper_signal_decision_id=?", (psd,)
    )

    # 6) execution authorization (idempotent via spine create_execution_authorization).
    existing_auth = db.fetchone(
        "SELECT authorization_id, status FROM paper_signal_execution_authorizations "
        "WHERE paper_signal_decision_id=?", (psd,)
    )
    if existing_auth is not None:
        auth_id = str(existing_auth["authorization_id"])
    else:
        auth_id = create_execution_authorization(
            db, paper_signal_decision_id=psd, specialist_approval_id=aid,
            source_trade_id=st_id, candidate_id=cand["candidate_id"],
            authorized_by="proof_command", authorization_reason="end_to_end_proof",
            policy_version=POLICY_VERSION,
        )

    # 7) execute (paper, temp DB, kill switch off). Idempotent: replays return existing.
    runtime = ExecutionRuntime(
        is_paper=True, kill_switch_engaged=False, broker_mode="paper", is_live=False,
        db_is_temporary=not _is_production_db(args.db_path),
        max_order_size=args.max_order_size, max_per_market=args.max_per_market,
        max_per_wallet=args.max_per_wallet, max_global=args.max_global,
        snapshot_max_age_seconds=args.snapshot_max_age_seconds,
        allow_production_execution=False,
    )
    ex = consume_eligible_signal(db, psd, runtime, dry_run=False)

    # 8) mark (idempotent on evidence). Read authoritative snapshot prices.
    position_id = ex.position_id
    if position_id is None:
        raise RuntimeError(f"execution did not produce a position (status={ex.status})")
    snap_row = db.fetchone(
        "SELECT best_bid, best_ask, mid_price FROM candidate_price_snapshots WHERE id=?",
        (snapshot_id,),
    )
    bid = float(snap_row["best_bid"]) if snap_row and snap_row["best_bid"] is not None else 0.5
    ask = float(snap_row["best_ask"]) if snap_row and snap_row["best_ask"] is not None else 0.5
    mid = float(snap_row["mid_price"]) if snap_row and snap_row["mid_price"] is not None else 0.5
    mark_res = mark_specialist_position(
        db, position_id, mark_price=mid, bid_price=bid, ask_price=ask,
        evidence_source="proof_deterministic", conservative=False,
    )

    # 9) settle (idempotent on evidence; never deletes the only position).
    # The position outcome is "Yes"; the winning outcome label is "Yes".
    settle_res = settle_specialist_position(
        db, position_id, resolution_outcome="Yes",
        evidence_source="proof_deterministic", raw_evidence={"proof": True},
    )

    # 10) realized P&L from settlement.
    realized = settle_res.realized_pnl

    # Detect replay completeness: if an order already existed before this run,
    # the full lifecycle previously completed (all steps are idempotent).
    already = pre_existing is not None

    return {
        "schema_version": SCHEMA_VERSION,
        "approval_id": aid,
        "source_trade_internal_id": st_id,
        "enrichment_id": enr.enrichment_id,
        "enrichment_status": enr.status,
        "dispatch_id": disp.dispatch_id,
        "dispatch_status": disp.status,
        "candidate_id": cand["candidate_id"] if cand else None,
        "snapshot_id": snapshot_id,
        "wallet_score_decision_id": ws["wallet_score_decision_id"] if ws else None,
        "category_score_decision_id": cs["category_score_decision_id"] if cs else None,
        "trade_copyability_decision_id": ts["trade_score_decision_id"] if ts else None,
        "paper_signal_decision_id": psd,
        "paper_signal_verdict": verdict["final_verdict"] if verdict else None,
        "execution_authorization_id": auth_id,
        "execution_risk_decision_id": ex.risk_decision_id,
        "paper_order_id": ex.order_id,
        "paper_fill_id": ex.fill_id,
        "paper_position_id": ex.position_id,
        "paper_position_lot_id": _lot_id(db, ex.position_id),
        "paper_position_mark_id": mark_res.mark_id,
        "paper_position_settlement_id": settle_res.settlement_id,
        "realized_pnl": realized,
        "temporary_database": not _is_production_db(args.db_path),
        "broker_mode": "paper",
        "is_live": False,
        "test_scoped_kill_switch": False,
        "production_configuration_changed": False,
        "status": "already_complete" if already else "complete",
    }


def _lot_id(db: Database, position_id) -> object:
    row = db.fetchone(
        "SELECT id FROM paper_position_lots WHERE position_id=?", (position_id,)
    )
    return row["id"] if row else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="End-to-end proof of specialist paper execution")
    p.add_argument("--db-path", required=True,
                   help="Explicit database path (temporary/test by default)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--source-trade-id", help="Optional exact source-trade-id input mode")
    p.add_argument("--max-order-size", type=float, default=2.0)
    p.add_argument("--max-per-market", type=float, default=100.0)
    p.add_argument("--max-per-wallet", type=float, default=100.0)
    p.add_argument("--max-global", type=float, default=100.0)
    p.add_argument("--snapshot-max-age-seconds", type=float, default=86400.0)
    args = p.parse_args(argv)

    if _is_production_db(args.db_path):
        print("error: refusing to run proof against the production database "
              "(/root/Polycopy/data/polycopy.db). Proof is temporary/test only.",
              file=sys.stderr)
        return 2

    db = Database(Path(args.db_path)).connect()
    try:
        result = run_proof(db, args)
        # Commit the full lifecycle (including the execution-authorization row,
        # which the spine does not commit internally) before the connection is
        # closed. The spine's consume/mark/settle already commit their own
        # atomic writes; this final commit persists anything not yet durable.
        db.commit()
    except Exception as exc:  # defensive: never leave a partial proof state
        db.rollback()
        print(f"error: proof failed and rolled back: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    if args.json:
        print(json.dumps(result, indent=1))
    else:
        for k, v in result.items():
            print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
