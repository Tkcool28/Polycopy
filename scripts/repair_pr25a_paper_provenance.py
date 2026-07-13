#!/usr/bin/env python3
"""Repair NULL ``paper_signal_decisions.trade_score_decision_id`` for PR25A rows.

Context
-------
PR25A's bounded write (``persist_bridge_trade_copyability_v1``) persisted the
Trade Copyability v1 decision and the canonical paper-signal row, but the
bridge path never fed the persisted TC decision id back into the frozen
``PaperSignalDecisionInput``. The result: the 3 PR25A paper rows carry
``trade_score_decision_id = NULL`` even though the TC decision exists.

This utility is a SEPARATE, single-purpose repair. It is:

  * **dry-run by default** -- reports the repairable rows and performs ZERO writes,
  * **gated** -- a writable production repair requires ``--allow-live --write
    --confirm-production-db`` (all three) AND the exact canonical production path,
  * **bounded** -- one transaction, hard maximum of ``--limit 3`` rows (this is a
    one-time repair for exactly the 3 known production rows; ``1 <= limit <= 3``),
  * **all-or-nothing** -- every selected row must pre-validate deterministically;
    if ANY row is missing/ambiguous/mismatched, ZERO updates are committed,
  * **backed-up** -- a verified online backup is created before any writable open,
  * **narrow** -- it updates ONLY ``paper_signal_decisions.trade_score_decision_id``
    and nothing else, with a per-row before/after column proof,
  * **verified** -- post-repair ``PRAGMA integrity_check`` / ``foreign_key_check``
    must be clean before success is declared.

Matching hierarchy (per NULL row)
--------------------------------
1. Direct ``candidate_id`` equality (find TC decisions for the same candidate).
2. Direct ``price_snapshot_id`` equality, when both records carry it.
3. TC decision id embedded in the paper idempotency key -- NOT USED: the key is a
   sha256 hash of its inputs; the embedded TC id is not recoverable from the hash.
   (Documented for transparency; the bridge embeds ``trade_score_decision_id`` in
   ``extra_params`` pre-hash, but the stored value is a hash.)
4. Exactly-one requirement: after 1+2, there must be EXACTLY ONE matching TC
   decision. Zero or more-than-one => reject that row (ambiguous / missing).

A row is rejected (and never mutated) when:
  * candidate_id missing,
  * price_snapshot_id missing,
  * zero matching TC decisions,
  * multiple matching TC decisions (ambiguous),
  * candidate mismatch,
  * price_snapshot mismatch,
  * the referenced TC decision is missing,
  * the referenced TC decision belongs to another candidate.

Only ``paper_signal_decisions.trade_score_decision_id`` is written. No other
column, no other table. The paper verdict / reason / scores are left untouched.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_LIMIT = 3
MIN_LIMIT = 1
MAX_LIMIT = 3  # one-time repair for exactly the 3 known production rows

# Exact canonical production DB (resolved-path equality, never a substring match).
CANONICAL_PRODUCTION_DB = Path("/root/Polycopy/data/polycopy.db").resolve()

# The exact discriminator for PR25A bridge paper rows (bridge_required path).
PR25A_PAPER_REASON = "bridge_required_paper_evidence_incomplete"

# Columns of paper_signal_decisions that are compared in the before/after
# proof. ``trade_score_decision_id`` is included so the proof can confirm it
# transitions NULL -> matched TC id and that NO other column changed.
PAPER_COLUMNS = [
    "id", "candidate_id", "price_snapshot_id", "verdict", "signal_reason",
    "score", "score_inputs", "idempotency_key", "created_at", "updated_at",
    "metadata", "payload", "notes", "trade_score_decision_id",
]

# Tables whose content must remain byte-for-byte identical across the repair.
# (The repair may ONLY touch paper_signal_decisions.trade_score_decision_id;
# every other table below is a forbidden-delta target. paper_signal_decisions is
# handled separately via the per-row before/after column proof.)
FORBIDDEN_FINGERPRINT_TABLES = (
    "orders",
    "positions",
    "decision_log",
    "settlement_accounting_ledger",
    "source_trades",
    "copy_candidates",
    "candidate_price_snapshots",
    "trade_copyability_decisions",
    "candidate_price_snapshot_levels",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fp(conn: sqlite3.Connection, table: str) -> str:
    """Deterministic content fingerprint for a table (empty -> stable sentinel).

    Uses ORDER BY rowid so a no-op produces byte-identical output. A missing
    table yields the canonical empty sentinel so the repair never errors on an
    absent forbidden table.
    """
    import hashlib

    try:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    except sqlite3.Error:
        return "MISSING"
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode())
    if not rows:
        return "EMPTY"
    return h.hexdigest()[:32]


@dataclass
class RowVerdict:
    paper_id: int
    candidate_id: Optional[int]
    price_snapshot_id: Optional[str]
    matched_tc_id: Optional[int] = None
    action: str = "skip"  # "update" | "skip"
    reason: str = ""


@dataclass
class RepairReport:
    dry_run: bool
    db_path: str
    production_db: bool
    limit: int
    requested_write: bool = False
    gates_complete: bool = False
    status: str = "ok"
    verdict: str = ""
    selected_count: int = 0
    validated_count: int = 0
    updated_count: int = 0
    committed: bool = False
    rolled_back: bool = False
    integrity_check: Optional[str] = None
    foreign_key_check_count: Optional[int] = None
    changed_columns_valid: Optional[bool] = None
    backup: Optional[dict] = None
    rows: list[RowVerdict] = field(default_factory=list)
    updated: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    proofs: list[dict] = field(default_factory=list)
    forbidden_before: dict = field(default_factory=dict)
    forbidden_after: dict = field(default_factory=dict)
    forbidden_identical: bool = True
    error: Optional[str] = None
    started_at: str = field(default_factory=_now_iso)
    finished_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "dry_run": self.dry_run,
            "requested_write": self.requested_write,
            "gates_complete": self.gates_complete,
            "db_path": self.db_path,
            "production_db": self.production_db,
            "limit": self.limit,
            "selected_count": self.selected_count,
            "validated_count": self.validated_count,
            "updated_count": self.updated_count,
            "committed": self.committed,
            "rolled_back": self.rolled_back,
            "integrity_check": self.integrity_check,
            "foreign_key_check_count": self.foreign_key_check_count,
            "changed_columns_valid": self.changed_columns_valid,
            "backup": self.backup,
            "rows": [
                {
                    "paper_id": r.paper_id,
                    "candidate_id": r.candidate_id,
                    "price_snapshot_id": r.price_snapshot_id,
                    "matched_tc_id": r.matched_tc_id,
                    "action": r.action,
                    "reason": r.reason,
                }
                for r in self.rows
            ],
            "updated": self.updated,
            "skipped": self.skipped,
            "proofs": self.proofs,
            "forbidden_before": self.forbidden_before,
            "forbidden_after": self.forbidden_after,
            "forbidden_identical": self.forbidden_identical,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _is_production_db(db_path: str) -> bool:
    """Exact resolved-path equality against the canonical production DB.

    No substring/suffix heuristic. A path is production only if its fully
    resolved absolute form equals the canonical production DB's resolved form.
    Symlinks are followed by ``Path.resolve()``; relative paths are resolved
    against the current working directory.
    """
    try:
        return Path(db_path).resolve() == CANONICAL_PRODUCTION_DB
    except OSError:
        return False


def _find_matching_tc(conn: sqlite3.Connection, candidate_id: Optional[int],
                      price_snapshot_id: Optional[str]) -> tuple[list[int], str]:
    """Apply the matching hierarchy; return (tc_ids, reject_reason).

    ``reject_reason`` is empty string when exactly one match is found.
    """
    if candidate_id is None:
        return [], "candidate_id_missing"
    if price_snapshot_id is None:
        return [], "price_snapshot_id_missing"
    params: list[Any] = [candidate_id]
    sql = ("SELECT id, price_snapshot_id, candidate_id FROM "
           "trade_copyability_decisions WHERE candidate_id = ?")
    if price_snapshot_id is not None:
        sql += " AND price_snapshot_id = ?"
        params.append(price_snapshot_id)
    tcs = [dict(t) for t in conn.execute(sql, params).fetchall()]
    # Guard against candidate mismatch / other-candidate ownership.
    foreign = [t for t in tcs if t["candidate_id"] != candidate_id]
    if foreign:
        return [], "tc_belongs_to_other_candidate"
    if not tcs:
        return [], "no_matching_tc_decision"
    if len(tcs) > 1:
        return [], "multiple_matching_tc_decisions"
    return [t["id"] for t in tcs], ""


def _read_paper_row(conn: sqlite3.Connection, paper_id: int) -> dict:
    cols = ", ".join(PAPER_COLUMNS)
    row = conn.execute(
        f"SELECT {cols} FROM paper_signal_decisions WHERE id = ?", (paper_id,)
    ).fetchone()
    if row is None:
        return {}
    return dict(zip(PAPER_COLUMNS, row))


def _proof_changed_columns(before: dict, after: dict) -> list[str]:
    changed = []
    for c in PAPER_COLUMNS:
        b = before.get(c)
        a = after.get(c)
        # Coerce None vs empty-string variance only for the untouched columns;
        # trade_score_decision_id transition NULL -> int is an expected change.
        if b != a:
            changed.append(c)
    return changed


def repair(
    db_path: str,
    *,
    limit: int = DEFAULT_LIMIT,
    allow_live: bool = False,
    write: bool = False,
    confirm_production_db: bool = False,
    json_out: bool = True,
    backup_helper=None,
) -> tuple[RepairReport, int]:
    """Run the repair. Returns (report, exit_code).

    Dry-run unless all gates are satisfied for an explicit ``--write``.

    ``backup_helper`` is injectable for tests (defaults to the real
    ``create_verified_backup``).
    """
    report = RepairReport(
        dry_run=True, db_path=db_path,
        production_db=_is_production_db(db_path), limit=limit,
        requested_write=bool(write),
    )

    # --- Argument validation (before any DB work) -------------------------
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        report.status = "invalid_limit"
        report.verdict = "limit_out_of_range"
        report.error = (
            f"limit {limit} out of allowed range [{MIN_LIMIT}, {MAX_LIMIT}]"
        )
        report.finished_at = _now_iso()
        return report, 2

    # --- --write gate enforcement (no silent downgrade) -------------------
    # Dry-run is only the default when --write is ABSENT. If --write is
    # explicitly requested, every required gate must be present or we fail
    # nonzero BEFORE any backup or writable open.
    if write:
        gates_ok = bool(allow_live and confirm_production_db
                        and _is_production_db(db_path))
        report.gates_complete = gates_ok
        if not gates_ok:
            report.status = "incomplete_write_gates"
            report.verdict = "write_requested_but_gates_incomplete"
            report.error = (
                "explicit --write requires --allow-live, --confirm-production-db, "
                "and the exact canonical production DB path; one or more missing"
            )
            report.finished_at = _now_iso()
            return report, 2
        report.dry_run = False

    # --- Read-only first pass: forbidden fingerprint + candidates ----------
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        report.forbidden_before = {
            t: _fp(ro, t) for t in FORBIDDEN_FINGERPRINT_TABLES
        }
        candidates = ro.execute(
            """
            SELECT id, candidate_id, price_snapshot_id
            FROM paper_signal_decisions
            WHERE trade_score_decision_id IS NULL
              AND signal_reason = ?
            ORDER BY id
            LIMIT ?
            """,
            (PR25A_PAPER_REASON, limit),
        ).fetchall()
    finally:
        ro.close()

    report.selected_count = len(candidates)
    valid_rows: list[RowVerdict] = []
    for c in candidates:
        paper_id = int(c["id"])
        candidate_id = c["candidate_id"]
        snapshot_id = c["price_snapshot_id"]
        tc_ids, reason = _find_matching_tc(
            _ro_reopen(db_path), candidate_id, snapshot_id
        )
        if reason:
            v = RowVerdict(
                paper_id=paper_id, candidate_id=candidate_id,
                price_snapshot_id=snapshot_id, action="skip", reason=reason,
            )
            report.rows.append(v)
            report.skipped.append(
                {"paper_id": paper_id, "reason": reason,
                 "candidate_id": candidate_id, "price_snapshot_id": snapshot_id}
            )
            continue
        v = RowVerdict(
            paper_id=paper_id, candidate_id=candidate_id,
            price_snapshot_id=snapshot_id, matched_tc_id=tc_ids[0],
            action="update", reason="exact_tc_match",
        )
        report.rows.append(v)
        valid_rows.append(v)

    report.validated_count = len(valid_rows)

    # --- All-or-nothing: if any selected row failed validation, ZERO ------
    if report.selected_count > 0 and report.validated_count != report.selected_count:
        report.status = "batch_validation_failed"
        report.verdict = "all_or_nothing_aborted"
        report.error = (
            f"{report.selected_count} row(s) selected, only "
            f"{report.validated_count} deterministically repairable; "
            "performing zero updates (all-or-nothing)"
        )
        report.finished_at = _now_iso()
        # No backup created, no writable transaction opened.
        return report, 1

    if report.dry_run:
        report.verdict = "dry_run_complete" if report.validated_count else "dry_run_no_rows"
        if not report.validated_count:
            report.status = "no_rows"
        report.finished_at = _now_iso()
        return report, 0

    # --- Writable path (all gates passed, all rows valid) -----------------
    if backup_helper is None:
        from polycopy.ingestion.source_trade_writer import create_verified_backup
        backup_helper = create_verified_backup
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "pr25a_provenance_repair_backup_" + ts
    backup_path = str(db_path) + "." + suffix
    bk = backup_helper(db_path, backup_path=backup_path)
    if (not getattr(bk, "success", False)
            or getattr(bk, "integrity_check", "") != "ok"
            or getattr(bk, "foreign_key_violations", 1)):
        err_detail = getattr(bk, "error", "") or "integrity/fk"
        report.status = "backup_failed"
        report.verdict = "verified_backup_failed"
        report.error = "verified_backup_failed:" + str(err_detail)
        report.finished_at = _now_iso()
        return report, 1
    report.backup = {
        "path": bk.path,
        "sha256": getattr(bk, "sha256", None),
        "integrity_check": getattr(bk, "integrity_check", None),
        "foreign_key_violations": getattr(bk, "foreign_key_violations", None),
    }

    writable = sqlite3.connect(db_path)
    writable.row_factory = sqlite3.Row
    committed = False
    try:
        writable.execute("PRAGMA foreign_keys = ON")
        cur = writable.cursor()
        for v in valid_rows:
            before = _read_paper_row(writable, v.paper_id)
            cur.execute(
                """
                UPDATE paper_signal_decisions
                SET trade_score_decision_id = ?
                WHERE id = ?
                  AND trade_score_decision_id IS NULL
                  AND candidate_id = ?
                  AND price_snapshot_id = ?
                """,
                (v.matched_tc_id, v.paper_id, v.candidate_id,
                 v.price_snapshot_id),
            )
            if cur.rowcount != 1:
                raise sqlite3.IntegrityError(
                    f"rowcount {cur.rowcount} for paper_id {v.paper_id} "
                    "(expected 1; stale/non-NULL row or mismatch)"
                )
            after = _read_paper_row(writable, v.paper_id)
            changed = _proof_changed_columns(before, after)
            if changed != ["trade_score_decision_id"]:
                raise sqlite3.IntegrityError(
                    f"unexpected column changes for paper_id {v.paper_id}: "
                    f"{changed}"
                )
            # Only record a successful, proven update (cleared on rollback below).
            report.proofs.append({
                "paper_id": v.paper_id,
                "matched_tc_id": v.matched_tc_id,
                "before_trade_score_decision_id": before.get(
                    "trade_score_decision_id"),
                "after_trade_score_decision_id": after.get(
                    "trade_score_decision_id"),
                "changed_columns": changed,
            })
            report.updated.append({
                "paper_id": v.paper_id,
                "trade_score_decision_id": v.matched_tc_id,
                "candidate_id": v.candidate_id,
                "price_snapshot_id": v.price_snapshot_id,
            })
        writable.commit()
        committed = True
    except sqlite3.Error as exc:
        writable.rollback()
        report.rolled_back = True
        report.committed = False
        report.proofs = []
        report.updated = []
        report.status = "transaction_rollback"
        report.verdict = "update_failed_rolled_back"
        report.error = f"update_failed:{exc}"
        report.finished_at = _now_iso()
        return report, 1
    finally:
        writable.close()

    report.committed = committed
    report.updated_count = len(report.updated)

    # --- Post-repair integrity / FK verification ---------------------------
    ro2 = _ro_reopen(db_path)
    ro2.row_factory = sqlite3.Row
    try:
        ic = ro2.execute("PRAGMA integrity_check").fetchall()
        integrity = "ok" if len(ic) == 1 and ic[0][0] == "ok" else "fail"
        fk_rows = ro2.execute("PRAGMA foreign_key_check").fetchall()
        fk_count = len(fk_rows)
        report.integrity_check = integrity
        report.foreign_key_check_count = fk_count
        report.forbidden_after = {
            t: _fp(ro2, t) for t in FORBIDDEN_FINGERPRINT_TABLES
        }
    finally:
        ro2.close()

    report.forbidden_identical = report.forbidden_before == report.forbidden_after
    report.changed_columns_valid = all(
        p["changed_columns"] == ["trade_score_decision_id"] for p in report.proofs
    )

    if integrity != "ok" or fk_count != 0 or not report.forbidden_identical \
            or not report.changed_columns_valid:
        report.status = "post_repair_verification_failed"
        report.verdict = "integrity_or_fk_failure"
        report.error = (
            f"post-repair verification failed: integrity={integrity} "
            f"fk_violations={fk_count} "
            f"forbidden_identical={report.forbidden_identical} "
            f"changed_columns_valid={report.changed_columns_valid}"
        )
        report.finished_at = _now_iso()
        # Committed but flagged: do not claim success. Recovery via backup.
        return report, 1

    report.status = "complete"
    report.verdict = "repair_complete"
    report.finished_at = _now_iso()
    return report, 0


def _ro_reopen(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair NULL trade_score_decision_id on PR25A paper rows.",
    )
    parser.add_argument(
        "--db-path",
        default=str(Path(__file__).resolve().parents[1] / "data" / "polycopy.db"),
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm-production-db", action="store_true")
    parser.add_argument("--json", action="store_true", default=True)
    parser.add_argument("--no-json", dest="json", action="store_false")
    args = parser.parse_args(argv)

    try:
        report, code = repair(
            args.db_path,
            limit=args.limit,
            allow_live=args.allow_live,
            write=args.write,
            confirm_production_db=args.confirm_production_db,
            json_out=args.json,
        )
    except ValueError as exc:
        print(json.dumps({"status": "invalid_arguments", "error": str(exc)}),
              flush=True)
        return 2

    print(json.dumps(report.as_dict(), indent=2, default=str), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
