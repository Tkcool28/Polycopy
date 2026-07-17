#!/usr/bin/env python3
"""Frozen rescoring orchestrator for the specialist-evidence watchlist.

This CLI is the *research* end of the evidence plane: it turns a watchlisted
wallet's canonical ``source_trades`` into wallet + category score decisions via
the FROZEN scoring functions in ``polycopy.scoring.wallet_evidence``.

Hard contracts (from the plan / engineering + audit):
  * It reuses ``resolve_wallet_score_v1`` / ``resolve_category_score_v1``
    UNCHANGED. Thresholds and verdict logic are never altered here.
  * It persists honest ``incomplete`` / ``copy_candidate`` / ``watchlist`` /
    ``skip`` / ``not_applicable`` decisions. It NEVER fabricates a category
    decision when the supported taxonomy label is absent.
  * It does NOT create ``specialist_approvals``, ``copy_candidates``,
    ``paper_signal_*``, dispatch, or execution-authorization rows.
  * Idempotency: for unchanged evidence it does not create uncontrolled
    duplicate decisions (the underlying scorer's deterministic idempotency
    key handles that). For changed evidence it creates a new auditable row.
  * Production DB guard: refuses BOTH recognized production paths unless the
    explicit ``--write --confirm-production-db`` gate is supplied (PR68
    pattern). Default is dry-run (no writes).
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

from polycopy.scoring.wallet_evidence import (  # noqa: E402
    CATEGORY_TAXONOMY_USABLE,
    classify_category_taxonomy,
    resolve_category_score_v1,
    resolve_wallet_score_v1,
)
from evidence_db import (  # noqa: E402
    DbConn,
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()

# Tables whose row counts MUST stay zero — this CLI never authorizes execution.
_FORBIDDEN_EXECUTION_TABLES = (
    "specialist_approvals",
    "approved_specialist_trade_dispatches",
    "copy_candidates",
    "paper_signal_decisions",
    "paper_signal_execution_authorizations",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "execution_risk_decisions",
)


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _supported_category_labels(db: DbConn, wallet_id: str) -> list[str]:
    """Return the distinct usable PR66 taxonomy labels across a wallet's trades.

    Only explicit ``metadata_json['taxonomy']['raw_category']`` evidence is
    consulted — never titles or inference. These are the ONLY categories for
    which a category score decision may be created.
    """
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
    # Stable deterministic order by label for reproducibility.
    return sorted(labels.keys())


def _count(db: DbConn, table: str) -> int:
    try:
        return int(db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"])
    except Exception:
        return 0


def _wallet_verdict(db: DbConn, wallet_id: str, *, now: datetime) -> Optional[str]:
    row = db.fetchone(
        "SELECT verdict FROM wallet_score_decisions "
        "WHERE wallet_id=? ORDER BY id DESC LIMIT 1",
        (wallet_id,),
    )
    return str(row["verdict"]) if row is not None else None


def _best_category(db: DbConn, wallet_id: str, *, now: datetime) -> Optional[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT category_label, verdict, final_score FROM "
        "category_wallet_score_decisions WHERE wallet_id=? "
        "ORDER BY final_score DESC, id DESC",
        (wallet_id,),
    )
    if not rows:
        return None
    best = rows[0]
    return {
        "category_label": best["category_label"],
        "verdict": best["verdict"],
        "final_score": best["final_score"],
    }


def evaluate_wallet(
    db: DbConn,
    wallet_id: str,
    *,
    now: datetime,
    write: bool,
) -> dict[str, Any]:
    """Aggregate + score + persist honest decisions for one wallet.

    Returns a compact per-wallet readiness record. ``write`` controls whether
    the scorer persists (the underlying frozen idempotency guarantees no
    uncontrolled duplicate for unchanged evidence).
    """
    cutoff = None  # point-in-time scoring over all canonical evidence.

    wallet_res = resolve_wallet_score_v1(
        db, wallet_id, cutoff_timestamp=cutoff, persist=write, now=now
    )
    wallet_decision_id = wallet_res.decision_id
    wallet_verdict = (
        wallet_res.result.verdict.value
        if wallet_res.result is not None
        else "incomplete"
    )

    categories: list[dict[str, Any]] = []
    supported = _supported_category_labels(db, wallet_id)
    for label in supported:
        # Re-classify to build the TaxonomyClassification the resolver expects.
        # We trust the resolved label is usable (it came from _supported_category_labels).
        classification = classify_category_taxonomy(
            {"taxonomy": {"raw_category": label}}
        )
        cat_res = resolve_category_score_v1(
            db, wallet_id, classification, cutoff_timestamp=cutoff, persist=write, now=now
        )
        categories.append({
            "category_label": label,
            "verdict": cat_res.result.verdict.value if cat_res.result is not None else "not_applicable",
            "decision_id": cat_res.decision_id,
            "status": cat_res.status,
            "created": cat_res.created,
            "reused": cat_res.reused,
        })

    return {
        "wallet_id": wallet_id,
        "wallet_verdict": wallet_verdict,
        "wallet_decision_id": wallet_decision_id,
        "wallet_decision_created": wallet_res.created,
        "wallet_decision_reused": wallet_res.reused,
        "supported_categories": supported,
        "category_decisions": categories,
    }


def _iter_active_watch_wallets(db: DbConn) -> list[tuple[str, str]]:
    """Return list of (wallet_id, watch_id) for active watches."""
    rows = db.fetchall(
        "SELECT id, wallet_id FROM specialist_evidence_watchlist "
        "WHERE status='active' ORDER BY wallet_id, id"
    )
    return [(str(r["wallet_id"]), str(r["id"])) for r in rows]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Frozen rescoring orchestrator for the specialist-evidence watchlist"
    )
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--wallet-id", help="Restrict to a single wallet (must be watched)")
    p.add_argument("--json", action="store_true", help="Emit pure JSON report")
    p.add_argument("--dry-run", action="store_true",
                   help="No writes (default for this CLI; explicit for clarity)")
    p.add_argument("--write", action="store_true",
                   help="Persist honest decisions (still refuses production without gate)")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--allow-live", action="store_true")
    args = p.parse_args(argv)

    # Production guard: write ops require the full three-gate set.
    if not require_write_gates(args, db_path=args.db_path):
        print(
            "error: production database write requires "
            "--write --allow-live --confirm-production-db",
            file=sys.stderr,
        )
        return 2
    persist = bool(getattr(args, "write", False))

    db = open_writable(args.db_path, args) if persist else open_readonly(args.db_path)
    try:
        now = datetime.now(timezone.utc)
        before_forbidden = {t: _count(db, t) for t in _FORBIDDEN_EXECUTION_TABLES}

        watches = _iter_active_watch_wallets(db)
        if args.wallet_id is not None:
            watches = [(w, wid) for (w, wid) in watches if w == args.wallet_id]
            if not watches:
                print(f"error: wallet_id={args.wallet_id} has no active watch",
                      file=sys.stderr)
                return 1

        results: list[dict[str, Any]] = []
        for wallet_id, watch_id in watches:
            rec = evaluate_wallet(db, wallet_id, now=now, write=persist)
            rec["watch_id"] = watch_id
            results.append(rec)

        after_forbidden = {t: _count(db, t) for t in _FORBIDDEN_EXECUTION_TABLES}
        # Hard safety assertion: this CLI must never create execution artifacts.
        for t, n in after_forbidden.items():
            if n != before_forbidden[t]:
                print(
                    f"error: invariant violated — {t} changed "
                    f"({before_forbidden[t]} -> {n}); rescoring must never "
                    f"authorize execution",
                    file=sys.stderr,
                )
                return 3

        report = {
            "mode": "write" if persist else "dry-run",
            "wallets_evaluated": len(results),
            "forbidden_execution_artifact_counts": after_forbidden,
            "wallets": results,
        }
        if args.json:
            print(json.dumps(report, indent=1, default=str))
        else:
            for rec in results:
                cats = ", ".join(
                    f"{c['category_label']}={c['verdict']}" for c in rec["category_decisions"]
                )
                print(
                    f"wallet={rec['wallet_id']} watch={rec['watch_id']} "
                    f"verdict={rec['wallet_verdict']} categories=[{cats}]"
                )
            print(f"mode={report['mode']} wallets_evaluated={len(results)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
