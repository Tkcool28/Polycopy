#!/usr/bin/env python3
"""Read-only specialist-evidence readiness monitor.

Proves the execution plane is unchanged and reports, per watched wallet, how
close the accumulated canonical evidence is to the frozen scorer's eligibility
gates. This CLI is strictly READ-ONLY: it never writes, never creates an
approval, dispatch, candidate, or execution artifact.

States (per wallet, escalated to the worst across the cohort for the summary):
  * GREEN  — a real (non-sample) watched wallet has BOTH a CURRENT wallet
             resolution ``copy_candidate`` AND a CURRENT supported-category
             resolution ``copy_candidate`` for one of its supported categories.
             This means the evidence is ready for HUMAN review — it is NOT an
             automatic approval.
  * YELLOW — evidence is accumulating but no watched wallet is ready yet
             (e.g. incomplete / watchlist / skip, no RED reason).
  * RED    — collector stale / sustained resolution failure / taxonomy conflict
             / resolution conflict / DB integrity or FK issue / sample wallet in
             cohort / unexpected approval / dispatch / execution artifact.

The monitor MUST explicitly inspect and report counts of the execution-plane
tables so an unexpected artifact surfaces as RED.

Readiness is computed from CURRENT canonical evidence, not historical row
ordering. ``resolve_wallet_score_v1`` / ``resolve_category_score_v1`` are called
with ``persist=False`` so the monitor re-derives the current resolution on every
run. A stale historical ``copy_candidate`` decision can therefore NEVER create
GREEN on its own — the current evidence must still qualify.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from evidence_db import (  # noqa: E402
    DbConn,
    FORBIDDEN_EXECUTION_TABLES,
    REQUIRED_SCHEMA_VERSION,
    open_readonly,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()
from polycopy.scoring.wallet_evidence import (  # noqa: E402
    CATEGORY_TAXONOMY_PARTIAL,
    CATEGORY_TAXONOMY_USABLE,
    WalletVerdict,
    aggregate_category_evidence,
    aggregate_wallet_evidence,
    classify_category_taxonomy,
    resolve_category_score_v1,
    resolve_wallet_score_v1,
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
_EXECUTION_PLANE_TABLES = FORBIDDEN_EXECUTION_TABLES

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

# Staleness policy defaults (S6 §10).
DEFAULT_COLLECTOR_STALE_AFTER_HOURS = 3
DEFAULT_REFRESH_STALE_AFTER_HOURS = 18


def _metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _count_forbidden(db: DbConn, table: str) -> int:
    """COUNT(*) a forbidden table, PROPAGATING real errors (fail-closed).

    Presence is decided via ``sqlite_master`` (see ``DbConn.count_table_optional``
    in evidence_db.py) — never from exception text. A genuinely absent optional
    table is reported as 0; any other SQL/schema/connection error propagates.
    """
    return db.count_table_optional(table)


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


def _taxonomy_completeness(db: DbConn, wallet_id: str) -> dict[str, Any]:
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is None:
        # Orphan watch (no wallets row) — report empty completeness without
        # crashing; build_wallet_status flags missing_wallet_record separately.
        return {
            "buy_count": 0,
            "usable_count": 0,
            "partial_count": 0,
            "unavailable_count": 0,
            "pct": 0.0,
        }
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    rows = db.fetchall(
        "SELECT side, resolution_status, metadata_json FROM source_trades "
        "WHERE lower(trader_address)=? AND upper(side)='BUY'",
        (address,),
    )
    buy = len(rows)
    usable = 0
    partial = 0
    unavailable = 0
    for row in rows:
        classification = classify_category_taxonomy(_metadata(row["metadata_json"]))
        if classification.status == CATEGORY_TAXONOMY_USABLE:
            usable += 1
        elif classification.status == CATEGORY_TAXONOMY_PARTIAL:
            partial += 1
        else:
            unavailable += 1
    pct = (usable / buy * 100.0) if buy else 0.0
    return {
        "buy_count": buy,
        "usable_count": usable,
        "partial_count": partial,
        "unavailable_count": unavailable,
        "pct": round(pct, 2),
    }


def _read_meta_schema_version(db: DbConn) -> int:
    """Read schema_version from the validated _meta row.

    Raises (fail-closed) on a missing row or malformed value so the caller can
    exit 1 rather than silently reporting None. A query error propagates
    naturally (also exit 1).
    """
    row = db.fetchone("SELECT value FROM _meta WHERE key='schema_version'")
    if row is None:
        raise RuntimeError("schema_version row missing from _meta")
    try:
        return int(row[0])
    except (TypeError, ValueError):
        raise RuntimeError(f"malformed schema_version value: {row[0]!r}")


def _integrity_ok(db: DbConn) -> tuple[bool, list[str]]:
    """Run PRAGMA integrity/FK checks ONCE.

    A check that FAILS TO EXECUTE (raises) propagates to the caller so the
    report fails closed (exit 1). An actual integrity/FK FINDING is returned as
    a reportable reason (RED, exit 0) — those are ordinary findings, not
    execution failures (S6 §3).
    """
    reasons: list[str] = []
    ic = db.fetchone("PRAGMA integrity_check")
    if ic is None or str(ic[0]) != "ok":
        reasons.append("integrity_check_not_ok")
    fk = db.fetchall("PRAGMA foreign_key_check")
    if fk:
        reasons.append("foreign_key_violation")
    return (len(reasons) == 0, reasons)


def _global_execution_counts(db: DbConn) -> tuple[dict[str, int], dict[str, int], dict[str, str]]:
    """Run execution-plane counts ONCE (baseline), fail-closed.

    Returns (baseline_counts, {}, errors). The DELTA is captured later in
    ``build_status`` (after the cohort is evaluated) so we can detect any
    research-evidence run that mutates the execution plane (S6 §4).

    A count ERROR propagates as an error entry (fail-closed -> exit 1). A
    stable, pre-existing non-zero count is NOT an error here — it is reported as
    an informational baseline and only becomes RED if it CHANGES during the run.
    """
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for t in _EXECUTION_PLANE_TABLES:
        try:
            counts[t] = _count_forbidden(db, t)
        except Exception as exc:
            errors[t] = str(exc)
    return counts, {}, errors


def _execution_delta(
    baseline: dict[str, int], after: dict[str, int]
) -> dict[str, int]:
    """Return per-table delta (after - baseline) for changed tables only."""
    delta: dict[str, int] = {}
    for t in list(baseline.keys()) + list(after.keys()):
        d = after.get(t, 0) - baseline.get(t, 0)
        if d != 0:
            delta[t] = d
    return delta


def _validate_execution_baseline(
    baseline: dict[str, int],
) -> tuple[bool, list[str]]:
    """Validate the baseline was captured cleanly (no error). Returns reasons."""
    return (len(baseline) == len(_EXECUTION_PLANE_TABLES), [])


# ── Taxonomy / resolution conflict detection (current provenance only) ────────

def _taxonomy_conflict_reason(db: DbConn, wallet_id: str) -> Optional[str]:
    """Return a RED taxonomy-conflict reason linked to THIS wallet's trades.

    Only an explicit ``source_trade_enrichments.status='conflict'`` with a real
    conflict reason code counts. Ordinary partial/unavailable taxonomy is NOT a
    conflict (it is YELLOW evidence incompleteness, handled elsewhere).
    """
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is None:
        return None
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    rows = db.fetchall(
        "SELECT st.id AS source_trade_id, ste.status, ste.reason_codes_json "
        "FROM source_trades st "
        "LEFT JOIN source_trade_enrichments ste ON ste.source_trade_internal_id = st.id "
        "WHERE lower(st.trader_address)=?",
        (address,),
    )
    for row in rows:
        status = row["status"]
        if status == "conflict":
            return f"taxonomy_conflict:source_trade={row['source_trade_id']}"
    return None


def _wallet_resolution_conflict(db: DbConn, wallet_id: str) -> Optional[str]:
    """Return a RED resolution-conflict reason scoped to THIS wallet's markets.

    Looks for current failed/errored market refresh bookkeeping on markets that
    actually belong to this wallet's source trades — never another wallet's.
    """
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is None:
        return None
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    market_rows = db.fetchall(
        "SELECT DISTINCT market_source_id FROM source_trades "
        "WHERE lower(trader_address)=? AND market_source_id IS NOT NULL",
        (address,),
    )
    wallet_markets = {str(r["market_source_id"]) for r in market_rows}
    if not wallet_markets:
        return None

    refresh_rows = db.fetchall(
        "SELECT market_source_id, last_status, last_error, attempt_count, "
        "last_checked_at, next_check_after FROM specialist_market_refresh_state"
    )
    for row in refresh_rows:
        msid = str(row["market_source_id"])
        if msid not in wallet_markets:
            continue  # another wallet's market failure must not RED this wallet
        status = row["last_status"]
        # Current-state authority: only an explicit failed/error/conflict
        # status REDs. A non-null last_error ALONE (e.g. on a recovered row)
        # must NOT reintroduce RED.
        if status in ("conflict", "failed", "error"):
            return f"resolution_conflict:market={msid}:status={status}"
    return None


def _collector_freshness(
    watch: dict[str, Any], *,
    stale_after_hours: int,
) -> tuple[Optional[str], bool]:
    """Return (red_reason_or_None, is_yellow). NEW watches with no collection
    timestamp are YELLOW (not RED). An ancient timestamp or malformed value is
    RED."""
    last = watch.get("last_collection_at")
    if not last:
        # Never collected yet -> YELLOW (accumulating), not RED.
        return None, True
    ts = _parse_ts(last)
    if ts is None:
        # Malformed timestamp -> RED with explicit reason.
        return "collector_timestamp_malformed", False
    age = _utcnow() - ts
    if age > timedelta(hours=stale_after_hours):
        return "collector_stale", False
    return None, False


def _refresh_freshness(
    db: DbConn, wallet_id: str, *,
    stale_after_hours: int,
) -> tuple[list[str], dict[str, Any]]:
    """Wallet-scoped refresh freshness + conflicts.

    Only the wallet's OWN market_source_ids are scoped. An unresolved market is
    not an error. A current failed/overdue refresh is RED. A recovered (later
    successful) status clears staleness. Another wallet's market failure is
    ignored. Returns (red_reasons, detail_dict).
    """
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    reasons: list[str] = []
    detail: dict[str, Any] = {
        "scoped_markets": 0,
        "current_failed": 0,
        "current_overdue": 0,
        "recovered": 0,
        "unresolved_ok": 0,
        "conflict_market_ids": [],
    }
    if wallet is None:
        return reasons, detail
    address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
    market_rows = db.fetchall(
        "SELECT DISTINCT market_source_id FROM source_trades "
        "WHERE lower(trader_address)=? AND market_source_id IS NOT NULL",
        (address,),
    )
    wallet_markets = {str(r["market_source_id"]) for r in market_rows}
    detail["scoped_markets"] = len(wallet_markets)
    if not wallet_markets:
        return reasons, detail

    refresh_rows = db.fetchall(
        "SELECT market_source_id, last_status, last_error, attempt_count, "
        "last_checked_at, next_check_after, resolved_at FROM "
        "specialist_market_refresh_state"
    )
    now = _utcnow()
    for row in refresh_rows:
        msid = str(row["market_source_id"])
        if msid not in wallet_markets:
            continue
        status = row["last_status"]
        last_checked = _parse_ts(row["last_checked_at"])
        next_after = _parse_ts(row["next_check_after"])
        resolved_at = _parse_ts(row["resolved_at"])
        # Malformed timestamp -> explicit RED reason (distinct from missing).
        if row["last_checked_at"] and last_checked is None:
            reasons.append(f"refresh_malformed_timestamp:market={msid}:field=last_checked_at")
            detail["current_failed"] += 1
            detail["conflict_market_ids"].append(msid)
            continue
        if row["next_check_after"] and next_after is None:
            reasons.append(f"refresh_malformed_timestamp:market={msid}:field=next_check_after")
            detail["current_failed"] += 1
            detail["conflict_market_ids"].append(msid)
            continue
        # Malformed resolved_at -> explicit RED (terminal authority is unreadable).
        if row["resolved_at"] and resolved_at is None:
            reasons.append(f"refresh_malformed_timestamp:market={msid}:field=resolved_at")
            detail["current_failed"] += 1
            detail["conflict_market_ids"].append(msid)
            continue
        # Current-state authority: an explicit failed/error/conflict status REDs.
        # A lingering last_error on a recovered row does NOT reintroduce RED.
        if status in ("conflict", "failed", "error"):
            reasons.append(f"refresh_current_failed:market={msid}")
            detail["current_failed"] += 1
            detail["conflict_market_ids"].append(msid)
            continue
        # TERMINAL resolution (S6 §2): a market with authoritative final
        # resolution (resolved/complete) AND a valid resolved_at is terminal. It
        # must NOT become RED merely because last_checked_at ages past the
        # refresh threshold — the resolution is final. A resolved status WITH a
        # missing resolved_at is NOT treated as terminal (defined explicitly):
        # it only escapes RED if it is also recent; an aged resolved-without-
        # resolved_at row falls through to the overdue check below.
        if status in ("resolved", "complete"):
            if resolved_at is not None:
                detail["recovered"] += 1
                continue
            # resolved/complete but no resolved_at: terminal bypass requires the
            # timestamp; without it we fall through to staleness (aged -> RED).
        # Overdue last_checked_at / next_check_after beyond policy -> RED.
        overdue = False
        if last_checked is not None and (now - last_checked) > timedelta(hours=stale_after_hours):
            overdue = True
        if next_after is not None and now > next_after + timedelta(hours=stale_after_hours):
            overdue = True
        # A terminal resolved/complete row (valid resolved_at) that is merely
        # aged is NOT overdue (handled above). For all other statuses:
        if status in ("unresolved", "stale") or overdue:
            if status == "unresolved" and not overdue:
                # Recent unresolved is healthy/informational (not an error).
                detail["unresolved_ok"] += 1
                continue
            if status == "stale" and not overdue:
                # Recent stale is informational too.
                detail["unresolved_ok"] += 1
                continue
            reasons.append(f"refresh_overdue:market={msid}")
            detail["current_overdue"] += 1
            continue
        # Otherwise (resolved / ok / success / complete) -> recovered / healthy.
        if status in ("resolved", "ok", "success", "complete"):
            detail["recovered"] += 1
    return reasons, detail


def _select_best_category(
    category_results: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Deterministic best current category (S6 §8):

      1. copy_candidate verdict first;
      2. current final_score descending;
      3. category label ascending.
    """
    candidates = [c for c in category_results if c.get("verdict") is not None]
    if not candidates:
        return None

    def sort_key(c: dict[str, Any]):
        # copy_candidate first, then higher score; label ASCENDING as the final
        # deterministic tiebreak (S6 §8.3).
        is_cc = 0 if c["verdict"] == WalletVerdict.COPY_CANDIDATE.value else 1
        score = -(c.get("final_score") or 0.0)
        return (is_cc, score, c["category_label"])

    return sorted(candidates, key=sort_key)[0]


def build_wallet_status(
    db: DbConn,
    wallet_id: str,
    watch: dict[str, Any],
    *,
    global_health: dict[str, Any],
    collector_stale_after_hours: int,
    refresh_stale_after_hours: int,
) -> dict[str, Any]:
    """Compute the per-wallet readiness record (read-only, current evidence)."""
    cutoff = None
    wallet_ev = aggregate_wallet_evidence(db, wallet_id, cutoff_timestamp=cutoff)
    supported = _supported_categories(db, wallet_id)
    taxonomy = _taxonomy_completeness(db, wallet_id)

    supported_classifications: dict[str, Any] = {}
    wallet = db.fetchone(
        "SELECT address, canonical_address FROM wallets WHERE id=?", (wallet_id,)
    )
    if wallet is not None:
        address = str(wallet["canonical_address"] or wallet["address"] or "").lower()
        rows = db.fetchall(
            "SELECT metadata_json FROM source_trades WHERE lower(trader_address)=?",
            (address,),
        )
        for row in rows:
            classification = classify_category_taxonomy(_metadata(row["metadata_json"]))
            if classification.status == CATEGORY_TAXONOMY_USABLE and classification.category_label:
                supported_classifications.setdefault(classification.category_label, classification)

    # CURRENT wallet resolution (persist=False) — the single source of truth.
    wallet_res = resolve_wallet_score_v1(
        db, wallet_id, cutoff_timestamp=cutoff, persist=False, now=_utcnow()
    )
    wallet_result = wallet_res.result
    wallet_verdict = (
        wallet_result.verdict.value if wallet_result is not None else "incomplete"
    )

    # CURRENT category resolutions (persist=False), every distinct usable
    # category exactly once, in deterministic order.
    category_results: list[dict[str, Any]] = []
    for label in sorted(supported_classifications.keys()):
        classification = supported_classifications[label]
        cat_res = resolve_category_score_v1(
            db, wallet_id, classification, cutoff_timestamp=cutoff,
            persist=False, now=_utcnow(),
        )
        cat_result = cat_res.result
        cat_ev = aggregate_category_evidence(
            db, wallet_id, label, cutoff_timestamp=cutoff
        ) if cat_result is not None else None
        category_results.append({
            "category_label": label,
            "verdict": cat_result.verdict.value if cat_result is not None else "not_applicable",
            "final_score": cat_result.score if cat_result is not None else None,
            "status": cat_res.status,
            "missing_reasons": list(cat_res.missing_reasons),
            "evidence_fingerprint": cat_res.evidence_fingerprint,
            "decision_id": cat_res.decision_id,  # set only when current-compatible
            "formula_name": cat_res.formula_name,
            "formula_version": cat_res.formula_version,
            "source_data_timestamp": cat_res.source_data_timestamp,
            "gate_distance": _distance_to_gates(cat_ev, CATEGORY_GATES) if cat_ev is not None else {},
            "ready_for_human_review": (
                cat_result is not None
                and cat_result.verdict == WalletVerdict.COPY_CANDIDATE
            ),
        })

    best_category = _select_best_category(category_results)

    wallet_gate_distance = _distance_to_gates(wallet_ev, WALLET_GATES)

    reasons: list[str] = []
    # Sample wallet in cohort -> RED (should not happen; cohort excludes, but
    # if an explicit --wallet-id is sample we still guard).
    if bool(watch.get("is_sample")):
        reasons.append("sample_wallet_in_cohort")

    # Execution-plane artifacts are reported at the GLOBAL level as
    # informational baseline/delta (S6 §4). A STABLE, pre-existing nonzero count
    # does NOT by itself RED a wallet — only an execution-plane COUNT DELTA
    # during the run (detected in build_status) does. Count errors already
    # fail-closed earlier (exec_errors -> exit 1). So no per-wallet
    # unexpected_execution_artifact reason is raised here.
    exec_errors = global_health["execution_artifact_errors"]
    if exec_errors:
        reasons.append("execution_artifact_count_error")

    # Integrity / FK (from one-shot global health).
    reasons.extend(global_health["integrity_reasons"])

    # Missing wallet record behind an active watch -> RED.
    if watch.get("missing_wallet_record"):
        reasons.append("missing_wallet_record")

    # Collector freshness.
    coll_reason, coll_yellow = _collector_freshness(
        watch, stale_after_hours=collector_stale_after_hours
    )
    if coll_reason:
        reasons.append(coll_reason)

    # Refresh freshness (wallet-scoped).
    refresh_reasons, refresh_detail = _refresh_freshness(
        db, wallet_id, stale_after_hours=refresh_stale_after_hours
    )
    reasons.extend(refresh_reasons)

    # Current-provenance conflicts.
    tax_conflict = _taxonomy_conflict_reason(db, wallet_id)
    if tax_conflict:
        reasons.append(tax_conflict)
    res_conflict = _wallet_resolution_conflict(db, wallet_id)
    if res_conflict:
        reasons.append(res_conflict)

    # YELLOW evidence-incompleteness (ordinary partial/unavailable taxonomy).
    yellow_reasons: list[str] = []
    if taxonomy["partial_count"] > 0:
        yellow_reasons.append("taxonomy_partial")
    elif taxonomy["usable_count"] == 0 and taxonomy["buy_count"] > 0:
        yellow_reasons.append("taxonomy_unavailable")
    if coll_yellow and not coll_reason:
        yellow_reasons.append("collector_not_yet_collected")

    # Determine state.
    is_green = (
        wallet_verdict == WalletVerdict.COPY_CANDIDATE.value
        and best_category is not None
        and best_category["verdict"] == WalletVerdict.COPY_CANDIDATE.value
    )
    score_pair_candidate = is_green
    if reasons:
        state = "RED"
    elif score_pair_candidate:
        state = "GREEN"
    else:
        state = "YELLOW"

    # ready_for_human_review is derived from the FINAL state, never directly
    # from score_pair_candidate (S6 §2).
    ready_for_human_review = (state == "GREEN")

    return {
        "wallet_id": wallet_id,
        "watch_id": watch.get("id"),
        "watch_status": watch.get("status"),
        "state": state,
        "ready_for_human_review": ready_for_human_review,
        "red_reasons": reasons,
        "yellow_reasons": yellow_reasons,
        "is_sample": bool(watch.get("is_sample")),
        "active_trading_days": wallet_ev.active_trading_days,
        "buy_count": wallet_ev.total_buy_trades,
        "distinct_markets": wallet_ev.distinct_markets,
        "distinct_events": wallet_ev.distinct_events,
        "resolved_markets": wallet_ev.resolved_markets,
        "taxonomy_complete_count": taxonomy["usable_count"],
        "taxonomy_partial_count": taxonomy["partial_count"],
        "taxonomy_unavailable_count": taxonomy["unavailable_count"],
        "taxonomy_completeness_pct": taxonomy["pct"],
        "supported_categories": supported,
        "current_wallet_resolution": {
            "verdict": wallet_verdict,
            "final_score": wallet_result.score if wallet_result is not None else None,
            "status": wallet_res.status,
            "missing_reasons": list(wallet_res.missing_reasons),
            "evidence_fingerprint": wallet_res.evidence_fingerprint,
            "decision_id": wallet_res.decision_id,
            "formula_name": wallet_res.formula_name,
            "formula_version": wallet_res.formula_version,
            "source_data_timestamp": wallet_res.source_data_timestamp,
            "created": wallet_res.created,
            "reused": wallet_res.reused,
            "would_create": wallet_res.would_create,
            "persisted": wallet_res.persisted,
        },
        "wallet_gate_distance": wallet_gate_distance,
        "current_category_results": category_results,
        "selected_best_category": best_category,
        "refresh_detail": refresh_detail,
        "last_collection_at": watch.get("last_collection_at"),
        "approval_created": False,
        "dispatch_created": False,
        "execution_authorized": False,
    }


def _dedupe_cohort(active_rows):
    """Deterministically dedupe active rows by wallet_id, choosing the LOWEST
    active watch id (S6 §1). Paused/retired rows must never win simply because
    they sort first — callers pass ONLY active rows here."""
    by_wallet: dict[str, dict] = {}
    for row in active_rows:
        wid = str(row["wallet_id"])
        cur = by_wallet.get(wid)
        if cur is None or str(row["id"]) < str(cur["id"]):
            by_wallet[wid] = row
    return list(by_wallet.values())


def build_status(
    db: DbConn, *,
    wallet_id: Optional[str] = None,
    collector_stale_after_hours: int = DEFAULT_COLLECTOR_STALE_AFTER_HOURS,
    refresh_stale_after_hours: int = DEFAULT_REFRESH_STALE_AFTER_HOURS,
) -> dict[str, Any]:
    # One-shot global health (S6 §12) — run each expensive check ONCE.
    # Integrity / schema are FAIL-CLOSED: a query error or schema mismatch
    # raises and is caught by main() -> exit 1.
    ok, integrity_reasons = _integrity_ok(db)
    exec_baseline, _, exec_errors = _global_execution_counts(db)
    schema_version = _read_meta_schema_version(db)
    if schema_version != REQUIRED_SCHEMA_VERSION:
        raise RuntimeError(
            f"schema version mismatch: required exactly {REQUIRED_SCHEMA_VERSION}, "
            f"found {schema_version}"
        )
    global_health = {
        "integrity_ok": ok,
        "integrity_reasons": integrity_reasons,
        "execution_artifact_counts": exec_baseline,
        "execution_artifact_errors": exec_errors,
        "schema_version": schema_version,
    }
    # Any execution-plane count ERROR makes the report untrustworthy ->
    # fail closed (exit 1), never a silent zero (S6 §4.5).
    if exec_errors:
        raise RuntimeError(
            f"execution_artifact_count_error: {', '.join(sorted(exec_errors))}"
        )

    # Cohort query: retain RAW active/paused/retired WATCH ROW counts for
    # informational fields; the readiness cohort is built separately from
    # deduplicated active rows (S6 §1).
    raw = db.fetchall(
        "SELECT w.id, w.wallet_id, w.status, w.last_collection_at, "
        "wl.id AS wallet_row_id, wl.is_sample AS is_sample "
        "FROM specialist_evidence_watchlist w "
        "LEFT JOIN wallets wl ON wl.id = w.wallet_id "
        "ORDER BY w.wallet_id, w.id"
    )

    # Explicit --wallet-id validation BEFORE evaluating (S6 §4):
    #   unknown wallet / no active watch / sample wallet / not-watched -> exit 2.
    # Wallet existence and sample status are read from explicit columns, NOT
    # from watch-row ordering (an older paused/retired row must not win).
    if wallet_id is not None:
        matched = [r for r in raw if str(r["wallet_id"]) == wallet_id]
        if not matched:
            return {
                "generated_at": _utcnow().isoformat(),
                "overall_state": "RED",
                "schema_version": schema_version,
                "watched_count": 0,
                "ready_for_human_review_count": 0,
                "active_watch_count": 0,
                "paused_watch_count": 0,
                "retired_watch_count": 0,
                "global_integrity": {"ok": ok, "reasons": integrity_reasons},
                "execution_artifact_baseline_counts": exec_baseline,
                "execution_artifact_counts": exec_baseline,
                "execution_artifact_delta": {},
                "execution_artifact_errors": exec_errors,
                "wallets": [],
                "selector_error": "unknown_or_unwatched_wallet",
            }
        # Wallet existence + sample status from explicit columns (NOT matched[0]).
        matched_active = [r for r in matched if r["status"] == "active"]
        # Choose the lowest active watch id deterministically.
        chosen = min(matched_active, key=lambda r: str(r["id"])) if matched_active else matched[0]
        if chosen["wallet_row_id"] is None:
            sel_err = "missing_wallet_record"
        elif bool(chosen["is_sample"]):
            sel_err = "sample_wallet"
        elif chosen["status"] != "active":
            sel_err = "not_watched"
        else:
            sel_err = None
        if sel_err is not None:
            return {
                "generated_at": _utcnow().isoformat(),
                "overall_state": "RED",
                "schema_version": schema_version,
                "watched_count": 0,
                "ready_for_human_review_count": 0,
                "active_watch_count": 0,
                "paused_watch_count": 0,
                "retired_watch_count": 0,
                "global_integrity": {"ok": ok, "reasons": integrity_reasons},
                "execution_artifact_baseline_counts": exec_baseline,
                "execution_artifact_counts": exec_baseline,
                "execution_artifact_delta": {},
                "execution_artifact_errors": exec_errors,
                "wallets": [],
                "selector_error": sel_err,
            }
        raw = [chosen]

    # Readiness cohort: active rows deduplicated by wallet_id (lowest active id).
    active_rows = [r for r in raw if r["status"] == "active"]
    paused_rows = [r for r in raw if r["status"] == "paused"]
    retired_rows = [r for r in raw if r["status"] == "retired"]
    cohort = _dedupe_cohort(active_rows)

    per_wallet: list[dict[str, Any]] = []
    for row in cohort:
        watch: dict[str, Any] = dict(row)
        # Explicit missing-wallet detection (S6 §4): wallet_row_id IS NULL.
        if row["wallet_row_id"] is None:
            watch["missing_wallet_record"] = True
        rec = build_wallet_status(
            db, str(row["wallet_id"]), watch,
            global_health=global_health,
            collector_stale_after_hours=collector_stale_after_hours,
            refresh_stale_after_hours=refresh_stale_after_hours,
        )
        per_wallet.append(rec)

    # Paused/retired watches are informational counts only (cannot drive state).
    any_red = any(r["state"] == "RED" for r in per_wallet)
    any_green = any(r["state"] == "GREEN" for r in per_wallet)

    if not per_wallet:
        overall_state = "YELLOW"  # no active cohort yet: nothing approvable
    elif any_red:
        overall_state = "RED"
    elif any_green:
        overall_state = "GREEN"
    else:
        overall_state = "YELLOW"

    ready_count = sum(1 for r in per_wallet if r["ready_for_human_review"])

    # Execution-artifact DELTA semantics (S6 §4): recapture counts after the
    # cohort is evaluated. A nonzero delta proves the run mutated the execution
    # plane -> RED with the exact changed table. A stable nonzero baseline stays
    # visible/informational and does NOT by itself RED each wallet.
    exec_after, _, exec_errors2 = _global_execution_counts(db)
    delta = _execution_delta(exec_baseline, exec_after)
    exec_counts_final = exec_after
    exec_errors_final = {**exec_errors, **exec_errors2}
    delta_red_tables = [t for t, d in delta.items() if d != 0]

    if delta_red_tables:
        for w in per_wallet:
            for t in delta_red_tables:
                w["red_reasons"].append(f"execution_artifact_delta:{t}:delta={delta[t]}")
        overall_state = "RED"

    return {
        "generated_at": _utcnow().isoformat(),
        "schema_version": schema_version,
        "overall_state": overall_state,
        "watched_count": len(per_wallet),
        "ready_for_human_review_count": ready_count,
        "active_watch_count": len(active_rows),
        "paused_watch_count": len(paused_rows),
        "retired_watch_count": len(retired_rows),
        "global_integrity": {"ok": ok, "reasons": integrity_reasons},
        "execution_artifact_baseline_counts": exec_baseline,
        "execution_artifact_counts": exec_counts_final,
        "execution_artifact_delta": delta,
        "execution_artifact_errors": exec_errors_final,
        "wallets": per_wallet,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only specialist-evidence readiness monitor")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--wallet-id", help="Restrict report to one watched wallet")
    p.add_argument("--json", action="store_true", help="Emit pure JSON")
    p.add_argument("--collector-stale-after-hours",
                   type=int, default=DEFAULT_COLLECTOR_STALE_AFTER_HOURS)
    p.add_argument("--refresh-stale-after-hours",
                   type=int, default=DEFAULT_REFRESH_STALE_AFTER_HOURS)
    args = p.parse_args(argv)

    # Positive stale-hour arguments; non-positive -> exit 2 (S6 §6).
    if args.collector_stale_after_hours <= 0 or args.refresh_stale_after_hours <= 0:
        print("error: stale-after-hours must be a positive integer", file=sys.stderr)
        return 2

    # Open read-only; catch open/schema failures -> controlled exit 1.
    try:
        db = open_readonly(args.db_path)
    except Exception as exc:
        print(f"error: cannot open database read-only: {exc!r}", file=sys.stderr)
        return 1

    try:
        try:
            report = build_status(
                db,
                wallet_id=getattr(args, "wallet_id", None),
                collector_stale_after_hours=args.collector_stale_after_hours,
                refresh_stale_after_hours=args.refresh_stale_after_hours,
            )
            globals()["_LAST_REPORT"] = report  # for test introspection of the exact run
        except Exception as exc:  # build/schema failure -> controlled exit 1
            print(f"error: failed to build status report: {exc!r}", file=sys.stderr)
            return 1

        # Invalid selector (unknown / no active watch / sample) -> exit 2.
        # Validation happens BEFORE the full report is built (see build_status).
        if report.get("selector_error") is not None:
            print(f"error: selector: {report['selector_error']}", file=sys.stderr)
            return 2

        # Count/exec error -> report is untrustworthy -> exit 1 (NOT exit 2).
        if report.get("fail_closed") is not None:
            print(f"error: {report['fail_closed']}", file=sys.stderr)
            return 1

        if args.json:
            print(json.dumps(report, indent=1, default=str))
        else:
            print(
                f"overall_state={report['overall_state']} "
                f"watched={report['watched_count']} "
                f"ready={report['ready_for_human_review_count']}"
            )
            for w in report["wallets"]:
                best = w.get("selected_best_category") or {}
                print(
                    f"  wallet={w['wallet_id']} state={w['state']} "
                    f"wallet_verdict={w['current_wallet_resolution']['verdict']} "
                    f"best_category={best.get('category_label')} "
                    f"category_verdict={best.get('verdict')}"
                    + (f" reasons={w['red_reasons']}" if w["red_reasons"] else "")
                    + (f" yellow={w['yellow_reasons']}" if w["yellow_reasons"] else "")
                )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
