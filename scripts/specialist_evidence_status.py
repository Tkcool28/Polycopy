#!/usr/bin/env python3
"""Read-only specialist-evidence readiness monitor.

Proves the execution plane is unchanged and reports, per watched wallet, how
close the accumulated canonical evidence is to the frozen scorer's eligibility
gates. This CLI is strictly READ-ONLY: it never writes, never creates an
approval, dispatch, candidate, or execution artifact.

States (per wallet, escalated to the worst across the cohort for the summary):
  * GREEN  — a real (non-sample) watched wallet has BOTH a wallet decision
             ``copy_candidate`` AND a category decision ``copy_candidate`` for
             one of its supported categories. This means the evidence is ready
             for HUMAN review — it is NOT an automatic approval.
  * YELLOW — evidence is accumulating but no watched wallet is approvable yet
             (e.g. incomplete / watchlist / skip, no RED reason).
  * RED    — collector stale / sustained Gamma failures / taxonomy conflict /
             resolution conflict / DB integrity or FK issue / sample wallet in
             cohort / unexpected approval / dispatch / execution artifact.

The monitor MUST explicitly inspect and report counts of the execution-plane
tables so an unexpected artifact surfaces as RED.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from evidence_db import open_readonly, DbConn  # noqa: E402

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()
from polycopy.scoring.wallet_evidence import (  # noqa: E402
    CATEGORY_TAXONOMY_PARTIAL,
    CATEGORY_TAXONOMY_USABLE,
    WalletVerdict,
    aggregate_category_evidence,
    aggregate_wallet_evidence,
    classify_category_taxonomy,
)
from polycopy.scoring.wallet_score_v1 import (  # noqa: E402
    GLOBAL_MIN_ACTIVE_TRADING_DAYS,
    GLOBAL_MIN_DISTINCT_EVENTS,
    GLOBAL_MIN_RESOLVED_MARKETS,
)
from polycopy.scoring.category_wallet_score_v1 import (  # noqa: E402
    CATEGORY_MIN_ACTIVE_DAYS,
    CATEGORY_MIN_DISTINCT_EVENTS,
    CATEGORY_MIN_RESOLVED_MARKETS,
)

# Execution-plane (and approval/dispatch) tables whose row counts MUST be zero
# in the research plane. Any non-zero count forces RED (unexpected artifact).
_EXECUTION_PLANE_TABLES = (
    "specialist_approvals",
    "approved_specialist_trade_dispatches",
    "copy_candidates",
    "paper_signal_decisions",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_position_lots",
    "paper_position_marks",
    "paper_position_settlements",
)

WALLET_GATES = {
    "resolved_markets": GLOBAL_MIN_RESOLVED_MARKETS,
    "active_trading_days": GLOBAL_MIN_ACTIVE_TRADING_DAYS,
    "distinct_events": GLOBAL_MIN_DISTINCT_EVENTS,
}
CATEGORY_GATES = {
    "category_resolved_markets": CATEGORY_MIN_RESOLVED_MARKETS,
    "category_distinct_events": CATEGORY_MIN_DISTINCT_EVENTS,
    "category_active_days": CATEGORY_MIN_ACTIVE_DAYS,
}


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _count(db: DbConn, table: str) -> int:
    try:
        return int(db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"])
    except Exception:
        return 0


def _distance_to_gates(evidence: Any, gates: dict[str, int]) -> dict[str, Any]:
    """Return remaining distance to each frozen gate (0 when met)."""
    out: dict[str, Any] = {}
    for key, minimum in gates.items():
        value = getattr(evidence, key, None)
        v = value if isinstance(value, int) else 0
        remaining = max(0, minimum - v)
        out[key] = {
            "minimum": minimum,
            "value": v,
            "remaining": remaining,
            "met": remaining == 0,
        }
    return out


def _supported_categories(db: DbConn, wallet_id: str) -> list[str]:
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is None:
        return []
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    rows = db.fetchall(
        "SELECT metadata_json FROM source_trades WHERE lower(trader_address)=?",
        (address,),
    )
    labels: dict[str, int] = {}
    for row in rows:
        classification = classify_category_taxonomy(_metadata(row["metadata_json"]))
        if classification.status == CATEGORY_TAXONOMY_USABLE and classification.category_label:
            labels[classification.category_label] = labels.get(classification.category_label, 0) + 1
    return sorted(labels.keys())


def _latest_wallet_verdict(db: DbConn, wallet_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT verdict, final_score, id FROM wallet_score_decisions "
        "WHERE wallet_id=? ORDER BY id DESC LIMIT 1",
        (wallet_id,),
    )
    if row is None:
        return None
    return {
        "verdict": str(row["verdict"]),
        "final_score": row["final_score"],
        "decision_id": int(row["id"]),
    }


def _best_category_decision(db: DbConn, wallet_id: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT category_label, verdict, final_score, id FROM "
        "category_wallet_score_decisions WHERE wallet_id=? "
        "ORDER BY final_score DESC, id DESC LIMIT 1",
        (wallet_id,),
    )
    if row is None:
        return None
    return {
        "category_label": str(row["category_label"]),
        "verdict": str(row["verdict"]),
        "final_score": row["final_score"],
        "decision_id": int(row["id"]),
    }


def _taxonomy_completeness(db: DbConn, wallet_id: str) -> dict[str, Any]:
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is None:
        return {"buy_count": 0, "complete_count": 0, "pct": 0.0}
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    rows = db.fetchall(
        "SELECT side, resolution_status, metadata_json FROM source_trades "
        "WHERE lower(trader_address)=? AND upper(side)='BUY'",
        (address,),
    )
    buy = len(rows)
    complete = 0
    for row in rows:
        classification = classify_category_taxonomy(_metadata(row["metadata_json"]))
        if classification.status == CATEGORY_TAXONOMY_USABLE:
            complete += 1
    pct = (complete / buy * 100.0) if buy else 0.0
    return {"buy_count": buy, "complete_count": complete, "pct": round(pct, 2)}


def _integrity_ok(db: DbConn) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    try:
        ic = db.fetchone("PRAGMA integrity_check")
        if ic is None or str(ic[0]) != "ok":
            reasons.append("integrity_check_not_ok")
    except Exception:
        reasons.append("integrity_check_error")
    try:
        fk = db.fetchall("PRAGMA foreign_key_check")
        if fk:
            reasons.append("foreign_key_violation")
    except Exception:
        reasons.append("foreign_key_check_error")
    return (len(reasons) == 0, reasons)


def _stale_market_refresh_count(db: DbConn) -> int:
    """Count market refresh-state rows that are stuck (error or unresolved)."""
    rows = db.fetchall(
        "SELECT last_status, last_error, attempt_count FROM specialist_market_refresh_state"
    )
    stale = 0
    for row in rows:
        status = row["last_status"]
        if row["last_error"] or status in ("error", "failed", "stale"):
            stale += 1
    return stale


def build_wallet_status(db: DbConn, wallet_id: str, watch: dict[str, Any]) -> dict[str, Any]:
    """Compute the per-wallet readiness record (read-only)."""
    cutoff = None
    wallet_ev = aggregate_wallet_evidence(db, wallet_id, cutoff_timestamp=cutoff)
    supported = _supported_categories(db, wallet_id)
    taxonomy = _taxonomy_completeness(db, wallet_id)

    best_category_label: Optional[str] = None
    best_category_ev: Optional[Any] = None
    if supported:
        # Best category by current resolved-markets evidence (deterministic).
        best_category_label = supported[0]
        best_category_ev = aggregate_category_evidence(
            db, wallet_id, best_category_label, cutoff_timestamp=cutoff
        )

    wallet_verdict = _latest_wallet_verdict(db, wallet_id)
    category_decision = _best_category_decision(db, wallet_id)

    wallet_gate_distance = _distance_to_gates(wallet_ev, WALLET_GATES)
    category_gate_distance = (
        _distance_to_gates(best_category_ev, CATEGORY_GATES) if best_category_ev is not None else {}
    )

    reasons: list[str] = []
    # Sample wallet in cohort -> RED.
    if bool(watch.get("is_sample")):
        reasons.append("sample_wallet_in_cohort")

    # Execution-plane artifacts -> RED.
    exec_counts = {t: _count(db, t) for t in _EXECUTION_PLANE_TABLES}
    unexpected = {t: n for t, n in exec_counts.items() if n > 0}
    if unexpected:
        reasons.append("unexpected_execution_artifact")

    # Integrity / FK.
    ok, integrity_reasons = _integrity_ok(db)
    if not ok:
        reasons.extend(integrity_reasons)

    # Collector stale: an active watch with no / ancient last_collection_at.
    last_collection = watch.get("last_collection_at")
    if watch.get("status") == "active" and not last_collection:
        reasons.append("collector_stale")

    # Stale market refresh.
    stale_refresh = _stale_market_refresh_count(db)
    if stale_refresh > 0:
        reasons.append("stale_market_refresh")

    # Taxonomy / resolution conflict detection.
    if taxonomy["buy_count"] > 0 and taxonomy["complete_count"] == 0 and _has_taxonomy_partial(db, wallet_id):
        reasons.append("taxonomy_conflict")

    # Determine state.
    wv = wallet_verdict["verdict"] if wallet_verdict else None
    cv = category_decision["verdict"] if category_decision else None
    is_green = (
        wv == WalletVerdict.COPY_CANDIDATE.value
        and cv == WalletVerdict.COPY_CANDIDATE.value
    )
    if reasons:
        state = "RED"
    elif is_green:
        state = "GREEN"
    else:
        state = "YELLOW"

    return {
        "wallet_id": wallet_id,
        "watch_id": watch.get("id"),
        "status": watch.get("status"),
        "state": state,
        "red_reasons": reasons,
        "is_sample": bool(watch.get("is_sample")),
        "active_trading_days": wallet_ev.active_trading_days,
        "buy_count": wallet_ev.total_buy_trades,
        "distinct_markets": wallet_ev.distinct_markets,
        "distinct_events": wallet_ev.distinct_events,
        "resolved_markets": wallet_ev.resolved_markets,
        "taxonomy_complete_count": taxonomy["complete_count"],
        "taxonomy_completeness_pct": taxonomy["pct"],
        "supported_categories": supported,
        "best_category": best_category_label,
        "wallet_verdict": wv,
        "best_category_verdict": cv,
        "wallet_gate_distance": wallet_gate_distance,
        "category_gate_distance": category_gate_distance,
        "last_collection_at": last_collection,
        "stale_market_refresh_count": stale_refresh,
        "execution_artifact_counts": exec_counts,
    }


def _has_taxonomy_partial(db: DbConn, wallet_id: str) -> bool:
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is None:
        return False
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    rows = db.fetchall(
        "SELECT metadata_json FROM source_trades WHERE lower(trader_address)=?",
        (address,),
    )
    for row in rows:
        classification = classify_category_taxonomy(_metadata(row["metadata_json"]))
        if classification.status == CATEGORY_TAXONOMY_PARTIAL:
            return True
    return False


def build_status(db: DbConn) -> dict[str, Any]:
    watches = db.fetchall(
        "SELECT w.id, w.wallet_id, w.status, w.last_collection_at, "
        "COALESCE(wl.is_sample, 0) AS is_sample "
        "FROM specialist_evidence_watchlist w "
        "LEFT JOIN wallets wl ON wl.id = w.wallet_id "
        "ORDER BY w.wallet_id, w.id"
    )
    per_wallet: list[dict[str, Any]] = []
    any_red = False
    any_green = False
    for row in watches:
        rec = build_wallet_status(db, str(row["wallet_id"]), dict(row))
        per_wallet.append(rec)
        if rec["state"] == "RED":
            any_red = True
        elif rec["state"] == "GREEN":
            any_green = True

    if not per_wallet:
        # No cohort yet: nothing approvable, not an error.
        overall_state = "YELLOW"
    elif any_red:
        overall_state = "RED"
    elif any_green:
        # At least one watched wallet is ready for human review and none is
        # red -> surface GREEN (plan: GREEN = >=1 copy_candidate match).
        overall_state = "GREEN"
    else:
        overall_state = "YELLOW"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_state": overall_state,
        "watched_count": len(per_wallet),
        "wallets": per_wallet,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only specialist-evidence readiness monitor")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--wallet-id", help="Restrict report to one watched wallet")
    p.add_argument("--json", action="store_true", help="Emit pure JSON")
    args = p.parse_args(argv)

    db = open_readonly(args.db_path)
    try:
        report = build_status(db)
        if args.wallet_id is not None:
            report = {
                **report,
                "wallets": [w for w in report["wallets"] if w["wallet_id"] == args.wallet_id],
            }
        if args.json:
            print(json.dumps(report, indent=1, default=str))
        else:
            print(f"overall_state={report['overall_state']} watched={report['watched_count']}")
            for w in report["wallets"]:
                print(
                    f"  wallet={w['wallet_id']} state={w['state']} "
                    f"wallet_verdict={w['wallet_verdict']} "
                    f"best_category={w['best_category']} "
                    f"category_verdict={w['best_category_verdict']}"
                    + (f" reasons={w['red_reasons']}" if w["red_reasons"] else "")
                )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
