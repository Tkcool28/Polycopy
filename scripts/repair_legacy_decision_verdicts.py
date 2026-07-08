#!/usr/bin/env python3
"""repair_legacy_decision_verdicts.py — PR24G maintenance cleanup.

Re-derives legacy ``wallet_score_decisions`` rows (and, optionally,
their companion ``decision_verdicts`` rows) through the current shared
PR24F evidence guard, so the persisted shape matches the current
contract.

The legacy pre-PR27/pre-PR24F/pre-PR24E bad shape is::

    wallet_score_decisions.verdict               = 'skip'
    wallet_score_decisions.missing_essentials_json   = '[]'
    wallet_score_decisions.eligibility_failures_json = '[]'
    # And the five evidence columns are usually None.

Such rows are auditable evidence of a contract violation. We do NOT
delete them (that loses history) and we do NOT patch them with raw SQL
(that hides the bug). Instead, this script re-derives the row's verdict
and reason buckets through the shared
:func:`derive_legacy_wallet_decision_repair` helper so the persisted
shape matches the current contract.

Modes:

    --dry-run (default)
        Print before/after plan for every suspect row. Never writes.

    --apply
        Apply the planned UPDATE inside a single transaction, gated by
        the shared operational lock.

Companion-row policy (``decision_verdicts``):

    --include-decision-verdicts
        When set, also update companion ``decision_verdicts`` rows
        whose ``(wallet_id, formula_name, formula_version, source_ref_type,
        source_ref_id)`` tuple matches the repaired parent wallet row.
        Companion verdict / verdict_family / ``exclusion_reasons_json``
        are derived FROM the repaired parent row (not from the
        incomplete child row evidence).

    When linkage is ambiguous (multiple matching ``decision_verdicts``
    rows for the same parent), the script reports the row as ambiguous
    and does NOT modify the companion rows. This is the
    "conservative: report, don't guess" branch from the brief.

Exit codes:

    0 — success (no errors; "nothing to do" still returns 0)
    1 — fatal error (DB / unhandled exception)
    2 — lock held by another job
    3 — RSS limit exceeded
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polycopy.config.settings import get_settings
from polycopy.db.database import Database
from polycopy.runtime.locks import (
    DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S,
    operational_job_lock,
)
from polycopy.runtime.memory import (
    MemoryLimitExceeded,
    check_rss_limit,
    get_max_rss_mb_from_env,
)
from polycopy.scoring.incomplete_verdict_guard import (
    derive_legacy_wallet_decision_repair,
)
from polycopy.utils.concurrency import LockError

logger = logging.getLogger(__name__)


# Columns we SELECT from wallet_score_decisions. Includes all five
# evidence fields plus the JSON buckets the guard inspects.
_WALLET_SELECT_COLUMNS = (
    "id",
    "wallet_id",
    "formula_name",
    "formula_version",
    "verdict",
    "missing_essentials_json",
    "eligibility_failures_json",
    "resolved_markets",
    "category_resolved_markets",
    "sample_fraction",
    "sharpe_ratio",
    "max_drawdown",
)


def setup_logging(verbosity: int = 0) -> None:
    level = logging.WARNING
    if verbosity >= 1:
        level = logging.INFO
    if verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _select_suspect_wallet_rows(
    conn,
    *,
    wallet_id: Optional[str],
    limit: Optional[int],
) -> list[dict[str, Any]]:
    """Return the legacy suspect ``wallet_score_decisions`` rows.

    Suspect criteria (from the brief):

      * ``verdict = 'skip'`` (case-insensitive in practice, but the
        current schema only emits lowercase)
      * ``missing_essentials_json`` empty or null
      * ``eligibility_failures_json`` empty or null

    The guard then re-checks whether the row actually needs repair
    (i.e. the current contract would rewrite either verdict or the
    reason buckets). Rows that already have non-empty buckets are
    filtered out at the DB level because they cannot match.
    """
    cols = ", ".join(_WALLET_SELECT_COLUMNS)
    sql = (
        f"SELECT {cols} FROM wallet_score_decisions "
        "WHERE verdict = 'skip' "
        "  AND COALESCE(missing_essentials_json, '[]') = '[]' "
        "  AND COALESCE(eligibility_failures_json, '[]') = '[]' "
    )
    params: list[Any] = []
    if wallet_id is not None:
        sql += "  AND wallet_id = ? "
        params.append(wallet_id)
    sql += " ORDER BY id DESC"
    if limit is not None:
        # Caller-supplied limit is honoured even when 0 (a 0 limit
        # means "return no rows", not "return everything"). Only
        # ``None`` is the "no limit" sentinel.
        sql += " LIMIT ?"
        params.append(max(0, int(limit)))

    rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        # ``r`` is a sqlite3.Row; cast to plain dict for the helper.
        out.append({k: r[k] for k in r.keys()})
    return out


def _select_companion_decision_rows(
    conn,
    *,
    wallet_id: str,
    formula_name: Optional[str],
    formula_version: Optional[str],
) -> list[dict[str, Any]]:
    """Find companion ``decision_verdicts`` rows linked to a wallet row.

    Linkage is by ``(wallet_id, formula_name, formula_version)``. The
    brief allows ``source_ref_type/source_ref_id`` to be considered,
    but the production schema enforces ``UNIQUE (wallet_id,
    formula_name, formula_version, source_ref_id)`` on
    ``decision_verdicts`` — so two rows with the same source_ref_id
    are impossible to insert. Adding source_ref_type/source_ref_id to
    the match would always return either 0 or 1 rows, defeating the
    ambiguity-detection branch.

    Conservative policy: match by the natural composite key
    ``(wallet_id, formula_name, formula_version)``. When multiple rows
    match the script reports ambiguity rather than picking one.

    Returns ALL matches. The caller decides whether zero or one match
    is unambiguous, and whether to act on a multi-match (ambiguous).
    """
    sql = (
        "SELECT id, wallet_id, formula_name, formula_version, "
        "       verdict, verdict_family, score, "
        "       source_ref_type, source_ref_id, exclusion_reasons_json "
        "FROM decision_verdicts "
        "WHERE wallet_id = ?"
    )
    params: list[Any] = [wallet_id]
    if formula_name is not None:
        sql += " AND COALESCE(formula_name, '') = COALESCE(?, '')"
        params.append(formula_name)
    if formula_version is not None:
        sql += " AND COALESCE(formula_version, '') = COALESCE(?, '')"
        params.append(formula_version)

    rows = conn.execute(sql, tuple(params)).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _format_plan_human(plan: dict[str, Any], ambiguous_companion: bool) -> str:
    """Render a single plan as human-readable lines."""
    lines = []
    rid = plan.get("row_id")
    wid = plan.get("wallet_id")
    lines.append(
        f"wallet_score_decisions id={rid} wallet_id={wid}"
    )
    lines.append(
        "  verdict: "
        f"{plan['old_verdict']!r} -> {plan['new_verdict']!r}"
    )
    lines.append(
        "  missing_essentials: "
        f"{plan['old_missing_essentials']!r} -> "
        f"{plan['new_missing_essentials']!r}"
    )
    lines.append(
        "  eligibility_failures: "
        f"{plan['old_eligibility_failures']!r} -> "
        f"{plan['new_eligibility_failures']!r}"
    )
    if ambiguous_companion:
        lines.append("  companion decision_verdicts: AMBIGUOUS (skipped)")
    return "\n".join(lines)


def _format_plan_json(plan: dict[str, Any], ambiguous_companion: bool) -> str:
    """Render a single plan as a JSON object (single-line)."""
    payload = {
        "row_id": plan.get("row_id"),
        "wallet_id": plan.get("wallet_id"),
        "old_verdict": plan["old_verdict"],
        "new_verdict": plan["new_verdict"],
        "old_missing_essentials": plan["old_missing_essentials"],
        "new_missing_essentials": plan["new_missing_essentials"],
        "old_eligibility_failures": plan["old_eligibility_failures"],
        "new_eligibility_failures": plan["new_eligibility_failures"],
        "ambiguous_companion": ambiguous_companion,
    }
    return json.dumps(payload)


def _apply_wallet_row_update(conn, plan: dict[str, Any]) -> None:
    """Persist the planned update on a single wallet row.

    Note: ``wallet_score_decisions`` does NOT have a ``verdict_family``
    column — only ``decision_verdicts`` does. The helper still computes
    ``new_verdict_family`` (for downstream consumers like the companion
    ``decision_verdicts`` row repair) but we never write it back to
    the wallet table.
    """
    payload = plan["updated_payload"]
    sql = (
        "UPDATE wallet_score_decisions SET "
        "  verdict = ?, "
        "  missing_essentials_json = ?, "
        "  eligibility_failures_json = ? "
        "WHERE id = ?"
    )
    conn.execute(
        sql,
        (
            payload["verdict"],
            payload["missing_essentials_json"],
            payload["eligibility_failures_json"],
            payload["id"],
        ),
    )


def _apply_companion_update(
    conn,
    *,
    companion_row_id: int,
    parent_plan: dict[str, Any],
) -> None:
    """Persist the planned update on a single companion decision row.

    The companion verdict / verdict_family / ``exclusion_reasons_json``
    are derived FROM the repaired parent row (not from the incomplete
    child row evidence, because ``decision_verdicts`` does not carry
    all five evidence fields).
    """
    sql = (
        "UPDATE decision_verdicts SET "
        "  verdict = ?, "
        "  verdict_family = ?, "
        "  exclusion_reasons_json = ? "
        "WHERE id = ?"
    )
    conn.execute(
        sql,
        (
            parent_plan["new_verdict"],
            parent_plan["new_verdict_family"],
            json.dumps(parent_plan["new_eligibility_failures"]),
            companion_row_id,
        ),
    )


def run_repair(
    *,
    db: Database,
    dry_run: bool,
    include_decision_verdicts: bool,
    wallet_id: Optional[str],
    limit: Optional[int],
    as_json: bool,
) -> dict[str, Any]:
    """Execute the repair pass.

    Returns a summary dict with::

        candidates_total (int)
        repairs_planned (int)
        repairs_applied (int)
        companions_repaired (int)
        companions_ambiguous (int)
        dry_run (bool)
    """
    summary = {
        "candidates_total": 0,
        "repairs_planned": 0,
        "repairs_applied": 0,
        "companions_repaired": 0,
        "companions_ambiguous": 0,
        "dry_run": dry_run,
    }
    conn = db.conn

    suspects = _select_suspect_wallet_rows(
        conn, wallet_id=wallet_id, limit=limit
    )
    summary["candidates_total"] = len(suspects)

    if not suspects:
        if as_json:
            print(json.dumps(summary))
        else:
            print("No suspect legacy rows found.")
        return summary

    plans: list[tuple[dict[str, Any], bool]] = []
    for row in suspects:
        plan = derive_legacy_wallet_decision_repair(row)
        if not plan["repair_needed"]:
            continue

        ambiguous = False
        if include_decision_verdicts:
            wid = row.get("wallet_id")
            if wid is not None:
                companions = _select_companion_decision_rows(
                    conn,
                    wallet_id=str(wid),
                    formula_name=row.get("formula_name"),
                    formula_version=row.get("formula_version"),
                )
            else:
                companions = []
            if len(companions) > 1:
                ambiguous = True
                summary["companions_ambiguous"] += 1
                plan["ambiguous_companion"] = True
                logger.warning(
                    "ambiguous companion linkage for wallet_id=%s "
                    "(%d matching decision_verdicts rows); skipping "
                    "companion repair",
                    row.get("wallet_id"),
                    len(companions),
                )
            elif len(companions) == 1:
                plan["_companion_id"] = companions[0]["id"]
        plans.append((plan, ambiguous))
        summary["repairs_planned"] += 1

    # Render plans (always, even in apply mode — operators want to see
    # what happened).
    for plan, ambiguous in plans:
        if as_json:
            print(_format_plan_json(plan, ambiguous))
        else:
            print(_format_plan_human(plan, ambiguous))

    if dry_run:
        if not as_json:
            print(
                f"\nDRY-RUN: planned {summary['repairs_planned']} repair(s); "
                "no writes performed."
            )
        return summary

    # Apply: single transaction, gated by the caller-held lock.
    for plan, ambiguous in plans:
        if plan.get("_companion_id") is not None and not ambiguous:
            _apply_companion_update(
                conn,
                companion_row_id=plan["_companion_id"],
                parent_plan=plan,
            )
            summary["companions_repaired"] += 1
        _apply_wallet_row_update(conn, plan)
        summary["repairs_applied"] += 1
    conn.commit()

    if not as_json:
        print(
            f"\nAPPLIED: {summary['repairs_applied']} wallet row(s); "
            f"{summary['companions_repaired']} companion row(s); "
            f"{summary['companions_ambiguous']} companion(s) ambiguous."
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Re-derive legacy wallet_score_decisions rows through the "
            "PR24F shared evidence guard (PR24G maintenance cleanup)."
        )
    )
    parser.add_argument(
        "--db", type=str, default=None, help="SQLite database path"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help=(
            "Compute the repair plan but do NOT write. Default "
            "behaviour when neither --dry-run nor --apply is given."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply the repair plan to the database. Requires the "
            "operational lock; the script exits nonzero if it cannot "
            "acquire the lock within --lock-timeout seconds."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of suspect rows to process",
    )
    parser.add_argument(
        "--wallet-id",
        type=str,
        default=None,
        help="Restrict the repair pass to a single wallet_id",
    )
    parser.add_argument(
        "--include-decision-verdicts",
        action="store_true",
        help=(
            "Also repair companion decision_verdicts rows whose "
            "(wallet_id, formula_name, formula_version, "
            "source_ref_type, source_ref_id) tuple matches the "
            "repaired parent wallet row. Ambiguous linkages are "
            "reported but not modified."
        ),
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S,
        help="Lock timeout seconds (only used with --apply)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one JSON object per planned repair, plus JSON summary",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    apply = bool(args.apply)
    dry_run = not apply  # Default dry-run; --apply flips it.

    if apply and args.dry_run:
        # argparse would otherwise let "--apply --dry-run" coexist
        # ambiguously. Be explicit: --apply wins.
        dry_run = False

    max_rss_mb = get_max_rss_mb_from_env()
    if max_rss_mb is not None and max_rss_mb > 0:
        try:
            check_rss_limit(
                "repair_legacy_decision_verdicts", max_rss_mb
            )
        except MemoryLimitExceeded as e:
            logger.error("RSS limit exceeded before repair: %s", e)
            print(f"ERROR: {e}", file=sys.stderr)
            return 3

    settings = get_settings()
    db_path = Path(args.db) if args.db else settings.db_path

    # Wrap the DB work in the global operational lock when applying.
    # Dry-run is read-only, so we still grab the lock — same shared
    # primitive — to keep "no two operators running the script at
    # once" semantics consistent. The lock path is configurable via
    # POLYCOPY_OPERATIONAL_LOCK_PATH; the per-call timeout is
    # --lock-timeout (or 30s default).
    try:
        with operational_job_lock(
            "repair-legacy", timeout=args.lock_timeout
        ):
            db = Database(db_path=db_path)
            db.connect()
            try:
                summary = run_repair(
                    db=db,
                    dry_run=dry_run,
                    include_decision_verdicts=args.include_decision_verdicts,
                    wallet_id=args.wallet_id,
                    limit=args.limit,
                    as_json=args.json,
                )
            finally:
                db.close()
    except LockError as e:
        logger.error("Lock held: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except MemoryLimitExceeded as e:
        logger.error("RSS limit exceeded during repair: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001 — top-level guard
        logger.exception("Fatal error during repair: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(summary))

    return 0


__all__ = [
    "main",
    "run_repair",
    "_select_suspect_wallet_rows",
    "_select_companion_decision_rows",
]


if __name__ == "__main__":
    sys.exit(main())