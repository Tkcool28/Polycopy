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
  * Idempotency: for unchanged evidence it reuses the existing row (no
    duplicate). For changed evidence it creates a new auditable row.
  * Production DB guard: refuses BOTH recognized production paths unless the
    explicit ``--write --allow-live --confirm-production-db`` gate is supplied
    (PR68 pattern). Default is dry-run (no writes).
  * Transactional atomicity: every staged write is held in ONE outer
    transaction. When the run succeeds (all scorers + persistence + the
    forbidden-artifact delta check pass), the whole run is committed exactly
    once. On any failure the whole run is rolled back and nothing partial
    survives.

Selector result/reason codes
-----------------------------
The CLI returns explicit selector codes rather than a generic failure:

  * ``unknown_wallet``        — ``--wallet-id`` matches no ``wallets`` row.
  * ``not_watched``           — wallet exists but has no *active* research
                                watch (paused/retired/missing never qualify).
  * ``sample_wallet``         — the watched wallet's ``wallets`` row is a
                                sample; research never scores samples.
  * ``no_active_cohort``      — (default mode) no active real-wallet watches
                                exist to score.

Each is a user-arg/selector problem and exits 2.
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
    FORBIDDEN_EXECUTION_TABLES,
    is_production_db,
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


# ── Pure helpers (deterministic, no DB side effects) ──────────────────────────

def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _supported_categories(db: DbConn, wallet_id: str) -> list[str]:
    """Return distinct usable PR66 taxonomy labels across a wallet's trades.

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
    # One representative real classification per normalized label (kept for the
    # resolver). Deterministic label order.
    return sorted(labels.keys())


def _supported_classifications(db: DbConn, wallet_id: str) -> dict[str, Any]:
    """Map each supported label to one representative real TaxonomyClassification.

    The classification is built from the wallet's OWN canonical metadata (never
    manufactured from ``{"taxonomy": {"raw_category": label}}``), so the
    resolver receives truthful provenance.
    """
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is None:
        return {}
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    rows = db.fetchall(
        "SELECT metadata_json FROM source_trades WHERE lower(trader_address)=?",
        (address,),
    )
    by_label: dict[str, Any] = {}
    for row in rows:
        classification = classify_category_taxonomy(_metadata(row["metadata_json"]))
        if classification.status == CATEGORY_TAXONOMY_USABLE and classification.category_label:
            # First (deterministic) representative wins.
            by_label.setdefault(classification.category_label, classification)
    return by_label


# ── Failed-closed counting (no blanket-to-zero) ──────────────────────────────

def _count_forbidden(db: DbConn, table: str, *, allow_missing: bool = True) -> int:
    """COUNT(*) a forbidden table, PROPAGATING real errors.

    * Present table -> real COUNT(*).
    * Genuinely absent optional table -> ``sqlite3.OperationalError`` is
      converted to ``0`` ONLY when ``allow_missing`` is True (a table the
      research plane may legitimately not have created yet).
    * Any other SQL/schema/connection error propagates unchanged (fail-closed).
    """
    try:
        return db.count_table(table)
    except Exception as exc:  # pragma: no cover - defensive
        if allow_missing and "no such table" in str(exc).lower():
            return 0
        raise


# ── Score one wallet (stages writes; does NOT commit) ────────────────────────

def evaluate_wallet(
    db: DbConn,
    wallet_id: str,
    *,
    now: datetime,
    write: bool,
) -> dict[str, Any]:
    """Aggregate + score + (optionally stage) honest decisions for one wallet.

    When ``write`` is True the resolvers stage their INSERTs but DO NOT commit
    (``persist_commit=False``); the caller owns the single outer transaction.
    Returns a compact per-wallet readiness record plus the ScoreResolution
    fields needed for honest reporting.
    """
    cutoff = None  # point-in-time scoring over all canonical evidence.

    wallet_res = resolve_wallet_score_v1(
        db, wallet_id, cutoff_timestamp=cutoff, persist=write, now=now,
        persist_commit=False,
    )
    wallet_decision_id = wallet_res.decision_id
    wallet_result = wallet_res.result
    wallet_verdict = wallet_result.verdict.value if wallet_result is not None else "incomplete"
    wallet_would_create = bool(wallet_res.would_create)

    categories: list[dict[str, Any]] = []
    classifications = _supported_classifications(db, wallet_id)
    # Evaluate every distinct current usable category exactly once, in
    # deterministic label order.
    for label in sorted(classifications.keys()):
        classification = classifications[label]
        cat_res = resolve_category_score_v1(
            db, wallet_id, classification, cutoff_timestamp=cutoff,
            persist=write, now=now, persist_commit=False,
        )
        cat_result = cat_res.result
        categories.append({
            "category_label": label,
            "verdict": cat_result.verdict.value if cat_result is not None else "not_applicable",
            "decision_id": cat_res.decision_id,
            "status": cat_res.status,
            "created": cat_res.created,
            "reused": cat_res.reused,
            "would_create": bool(cat_res.would_create),
            "persisted": cat_res.persisted,
            "current": (not write) and cat_res.decision_id is not None,
            "evidence_fingerprint": cat_res.evidence_fingerprint,
            "source_data_timestamp": cat_res.source_data_timestamp,
            "formula_name": cat_res.formula_name,
            "formula_version": cat_res.formula_version,
            "missing_reasons": list(cat_res.missing_reasons),
        })

    # Honest decision intent (S6 §3):
    #   created            — a new row was just staged (write mode)
    #   current_reused     — dry-run saw an existing compatible decision
    #   would_create       — dry-run would have created a new row
    #   none/not_applicable— nothing to decide
    decision_intent: str
    if write:
        decision_intent = "created" if wallet_res.created else ("current_reused" if wallet_res.reused else "would_create")
    elif wallet_res.reused or wallet_decision_id is not None:
        decision_intent = "current_reused"
    elif wallet_would_create:
        decision_intent = "would_create"
    else:
        decision_intent = "none"

    return {
        "wallet_id": wallet_id,
        "wallet_verdict": wallet_verdict,
        "wallet_decision_id": wallet_decision_id,
        "wallet_decision_created": wallet_res.created,
        "wallet_decision_reused": wallet_res.reused,
        "wallet_decision_would_create": wallet_would_create,
        "wallet_decision_current": (not write) and wallet_decision_id is not None,
        "wallet_decision_intent": decision_intent,
        "wallet_evidence_fingerprint": wallet_res.evidence_fingerprint,
        "wallet_source_data_timestamp": wallet_res.source_data_timestamp,
        "wallet_formula_name": wallet_res.formula_name,
        "wallet_formula_version": wallet_res.formula_version,
        "wallet_missing_reasons": list(wallet_res.missing_reasons),
        "wallet_status": wallet_res.status,
        "supported_categories": sorted(classifications.keys()),
        "category_decisions": categories,
    }


def _iter_active_watch_wallets(db: DbConn) -> list[tuple[str, str, bool]]:
    """Return deduplicated (wallet_id, watch_id, is_sample) for active real watches.

    * active watches ONLY (paused/retired excluded)
    * INNER JOIN to wallets (missing wallet row -> skipped)
    * wallets.is_sample = 0 (sample wallets excluded)
    * deterministic ordering by wallet_id, watch_id
    * one evaluation per unique wallet even if duplicate active watch rows exist
    """
    rows = db.fetchall(
        "SELECT w.id AS watch_id, w.wallet_id AS wallet_id, "
        "COALESCE(wl.is_sample, 0) AS is_sample "
        "FROM specialist_evidence_watchlist w "
        "INNER JOIN wallets wl ON wl.id = w.wallet_id "
        "WHERE w.status = 'active' AND COALESCE(wl.is_sample, 0) = 0 "
        "ORDER BY w.wallet_id, w.id"
    )
    seen: set[str] = set()
    out: list[tuple[str, str, bool]] = []
    for r in rows:
        wid = str(r["wallet_id"])
        if wid in seen:
            continue  # dedupe duplicate active watch rows
        seen.add(wid)
        out.append((wid, str(r["watch_id"]), bool(r["is_sample"])))
    return out


def _resolve_selector(
    db: DbConn, wallet_id: Optional[str]
) -> tuple[Optional[list[tuple[str, str, bool]]], Optional[str]]:
    """Resolve the cohort and return (cohort, selector_code).

    ``selector_code`` is set (with a ``None`` cohort) only on a selector error
    that must exit 2. The selector is resolved READ-ONLY before any writable
    open (callers pass a read-only connection here).
    """
    if wallet_id is not None:
        # Exact wallet must exist (real row).
        wallet = db.fetchone("SELECT id, is_sample FROM wallets WHERE id=?", (wallet_id,))
        if wallet is None:
            return None, "unknown_wallet"
        if bool(wallet["is_sample"]):
            return None, "sample_wallet"
        # Must have an ACTIVE research watch.
        watch = db.fetchone(
            "SELECT id FROM specialist_evidence_watchlist "
            "WHERE wallet_id=? AND status='active'",
            (wallet_id,),
        )
        if watch is None:
            return None, "not_watched"
        return [(str(wallet_id), str(watch["id"]), bool(wallet["is_sample"]))], None

    cohort = _iter_active_watch_wallets(db)
    if not cohort:
        return None, "no_active_cohort"
    return cohort, None


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

    # ── Mode + selector semantics (S6 §1) ──────────────────────────────────
    # Reject mutually exclusive flags with exit 2.
    if getattr(args, "write", False) and getattr(args, "dry_run", False):
        print("error: --write and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    # Default (no flag) remains dry-run.
    persist = bool(getattr(args, "write", False))

    # Production write missing any required gate exits 2 BEFORE every DB symbol.
    # require_write_gates() returns False for a normal dry-run (no --write),
    # which is CORRECT and must NOT be treated as a refusal.
    if persist and not require_write_gates(args, db_path=args.db_path):
        if is_production_db(args.db_path):
            msg = (
                "error: production database write requires "
                "--write --allow-live --confirm-production-db"
            )
        else:
            msg = "error: write requires --write"
        print(msg, file=sys.stderr)
        return 2

    # ── Open the connection (read-only default, writable only if persisting) ──
    db = open_writable(args.db_path, args) if persist else open_readonly(args.db_path)
    try:
        now = datetime.now(timezone.utc)

        # ── Resolve selector READ-ONLY first (S6 §1) ───────────────────────
        cohort, selector_code = _resolve_selector(db, getattr(args, "wallet_id", None))
        if selector_code is not None:
            print(f"error: selector={selector_code}", file=sys.stderr)
            return 2

        # ── Forbidden-artifact baseline (before) ───────────────────────────
        try:
            before_forbidden = {
                t: _count_forbidden(db, t) for t in FORBIDDEN_EXECUTION_TABLES
            }
        except Exception as exc:  # count/query failure -> fail-closed, rc 1
            print(f"error: forbidden-artifact count failed: {exc!r}",
                  file=sys.stderr)
            return 1

        # cohort is guaranteed non-None here (selector_code path returns above).
        assert cohort is not None
        results: list[dict[str, Any]] = []
        try:
            for wallet_id, watch_id, is_sample in cohort:
                rec = evaluate_wallet(db, wallet_id, now=now, write=persist)
                rec["watch_id"] = watch_id
                rec["is_sample"] = is_sample
                results.append(rec)
        except Exception as exc:  # scorer/persistence failure -> rollback, exit 1
            db.rollback()
            print(f"error: rescoring failed: {exc!r}", file=sys.stderr)
            return 1

        # ── Forbidden-artifact delta check (S6 §5) ─────────────────────────
        try:
            after_forbidden = {
                t: _count_forbidden(db, t) for t in FORBIDDEN_EXECUTION_TABLES
            }
        except Exception as exc:  # count/query failure -> fail-closed, rc 1
            db.rollback()
            print(f"error: forbidden-artifact count failed: {exc!r}",
                  file=sys.stderr)
            return 1
        delta = {t: n - before_forbidden[t] for t, n in after_forbidden.items()}
        if any(delta.values()):
            db.rollback()
            bad = {t: d for t, d in delta.items() if d}
            print(
                f"error: invariant violated — forbidden execution-artifact "
                f"delta {bad}; rescoring must never authorize execution",
                file=sys.stderr,
            )
            return 1

        # ── Commit exactly once, only when persisting and all work succeeded ─
        if persist:
            try:
                db.commit()
            except Exception as exc:  # commit failure -> nothing persisted
                db.rollback()
                print(f"error: commit failed: {exc!r}", file=sys.stderr)
                return 1

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
                    f"{c['category_label']}={c['verdict']}"
                    f"(id={c['decision_id']},intent={'created' if c['created'] else ('reused' if c['reused'] else 'would_create')})"
                    for c in rec["category_decisions"]
                )
                print(
                    f"wallet={rec['wallet_id']} watch={rec['watch_id']} "
                    f"verdict={rec['wallet_verdict']} "
                    f"intent={rec['wallet_decision_intent']} "
                    f"wallet_decision_id={rec['wallet_decision_id']} "
                    f"categories=[{cats}]"
                )
            print(f"mode={report['mode']} wallets_evaluated={len(results)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
