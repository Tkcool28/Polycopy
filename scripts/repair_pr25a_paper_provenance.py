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

  * **dry-run by default** — reports the repairable rows and performs ZERO writes,
  * **gated** — a writable production repair requires ``--allow-live --write
    --confirm-production-db`` (all three),
  * **bounded** — one transaction, hard maximum of ``--limit 3`` rows (this is a
    one-time repair for exactly the 3 known production rows),
  * **backed-up** — a verified online backup is created before any writable open,
  * **narrow** — it updates ONLY ``paper_signal_decisions.trade_score_decision_id``
    and nothing else.

Matching hierarchy (per NULL row)
---------------------------------
1. Direct ``candidate_id`` equality (find TC decisions for the same candidate).
2. Direct ``price_snapshot_id`` equality, when both records carry it.
3. TC decision id embedded in the paper idempotency key — NOT USED: the key is a
   sha256 hash of its inputs; the embedded TC id is not recoverable from the hash.
   (Documented for transparency; the bridge embeds ``trade_score_decision_id`` in
   ``extra_params`` pre-hash, but the stored value is a hash.)
4. Exactly-one requirement: after 1+2, there must be EXACTLY ONE matching TC
   decision. Zero or more-than-one => reject that row (ambiguous / missing).

A row is rejected (and never mutated) when:
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
MAX_LIMIT = 3  # one-time repair for exactly the 3 known production rows

# The exact discriminator for PR25A bridge paper rows (bridge_required path).
PR25A_PAPER_REASON = "bridge_required_paper_evidence_incomplete"

# Tables whose content must remain byte-for-byte identical across the repair.
# (The repair may ONLY touch paper_signal_decisions.trade_score_decision_id;
# every other table below is a forbidden-delta target.)
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
    backup: Optional[dict] = None
    rows: list[RowVerdict] = field(default_factory=list)
    updated: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    forbidden_before: dict = field(default_factory=dict)
    forbidden_after: dict = field(default_factory=dict)
    forbidden_identical: bool = True
    error: Optional[str] = None
    started_at: str = field(default_factory=_now_iso)
    finished_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "db_path": self.db_path,
            "production_db": self.production_db,
            "limit": self.limit,
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
            "forbidden_before": self.forbidden_before,
            "forbidden_after": self.forbidden_after,
            "forbidden_identical": self.forbidden_identical,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _is_production_db(db_path: str) -> bool:
    """Heuristic: the canonical production DB lives at <repo>/data/polycopy.db."""
    p = str(Path(db_path).resolve())
    return p.endswith("data/polycopy.db") and "/Polycopy/" in p


def _find_matching_tc(conn: sqlite3.Connection, candidate_id: Optional[int],
                      price_snapshot_id: Optional[str]) -> tuple[list[int], str]:
    """Apply the matching hierarchy; return (tc_ids, reject_reason).

    ``reject_reason`` is empty string when exactly one match is found.
    """
    if candidate_id is None:
        return [], "candidate_id_missing"
    params: list[Any] = [candidate_id]
    sql = "SELECT id, price_snapshot_id, candidate_id FROM trade_copyability_decisions WHERE candidate_id = ?"
    if price_snapshot_id is not None:
        sql += " AND price_snapshot_id = ?"
        params.append(price_snapshot_id)
    tcs = conn.execute(sql, params).fetchall()
    tcs = [dict(t) for t in tcs]
    # Guard against candidate mismatch / other-candidate ownership.
    foreign = [t for t in tcs if t["candidate_id"] != candidate_id]
    if foreign:
        return [], "tc_belongs_to_other_candidate"
    if not tcs:
        return [], "no_matching_tc_decision"
    if len(tcs) > 1:
        return [], "multiple_matching_tc_decisions"
    return [t["id"] for t in tcs], ""


def repair(
    db_path: str,
    *,
    limit: int = DEFAULT_LIMIT,
    allow_live: bool = False,
    write: bool = False,
    confirm_production_db: bool = False,
    json_out: bool = True,
    backup_helper=None,
) -> RepairReport:
    """Run the repair. Dry-run unless all three write gates are passed.

    ``backup_helper`` is injectable for tests (defaults to the real
    ``create_verified_backup``). The function is hermetic-friendly: callers pass
    a temp DB path and the production gates; it opens the DB read-only first,
    decides matches, then (only when gated) opens writable + verifies a backup.
    """
    if limit > MAX_LIMIT:
        raise ValueError(
            f"limit {limit} exceeds the one-time repair maximum of {MAX_LIMIT}"
        )
    can_write = bool(allow_live and write and confirm_production_db)
    dry_run = not can_write
    prod = _is_production_db(db_path)
    report = RepairReport(
        dry_run=dry_run, db_path=db_path, production_db=prod, limit=limit,
    )

    # --- Read-only first pass: capture forbidden fingerprint + candidates ----
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

    if dry_run:
        report.finished_at = _now_iso()
        return report

    # --- Writable path (all gates passed) -----------------------------------
    if backup_helper is None:
        from polycopy.ingestion.source_trade_writer import create_verified_backup
        backup_helper = create_verified_backup
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "pr25a_provenance_repair_backup_" + ts
    backup_path = str(db_path) + "." + suffix
    bk = backup_helper(db_path, backup_path=backup_path)
    if not getattr(bk, "success", False) or getattr(bk, "integrity_check", "") != "ok" or getattr(bk, "foreign_key_violations", 1):
        err_detail = getattr(bk, "error", "") or "integrity/fk"
        report.error = "verified_backup_failed:" + str(err_detail)
        report.finished_at = _now_iso()
        return report
    report.backup = {
        "path": bk.path,
        "sha256": getattr(bk, "sha256", None),
        "integrity_check": getattr(bk, "integrity_check", None),
        "foreign_key_violations": getattr(bk, "foreign_key_violations", None),
    }

    writable = sqlite3.connect(db_path)
    writable.row_factory = sqlite3.Row
    try:
        writable.execute("PRAGMA foreign_keys = ON")
        cur = writable.cursor()
        for v in report.rows:
            if v.action != "update":
                continue
            cur.execute(
                "UPDATE paper_signal_decisions SET trade_score_decision_id = ? WHERE id = ?",
                (v.matched_tc_id, v.paper_id),
            )
            report.updated.append(
                {
                    "paper_id": v.paper_id,
                    "trade_score_decision_id": v.matched_tc_id,
                    "candidate_id": v.candidate_id,
                    "price_snapshot_id": v.price_snapshot_id,
                }
            )
        writable.commit()
    except sqlite3.Error as exc:
        writable.rollback()
        report.error = f"update_failed:{exc}"
        report.finished_at = _now_iso()
        return report
    finally:
        writable.close()

    # --- Forbidden fingerprint post-check -----------------------------------
    ro2 = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ro2.row_factory = sqlite3.Row
    try:
        report.forbidden_after = {
            t: _fp(ro2, t) for t in FORBIDDEN_FINGERPRINT_TABLES
        }
    finally:
        ro2.close()
    report.forbidden_identical = report.forbidden_before == report.forbidden_after
    report.finished_at = _now_iso()
    return report


def _ro_reopen(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair NULL trade_score_decision_id on PR25A paper rows.",
    )
    parser.add_argument("--db-path", default=str(Path(__file__).resolve().parents[1] / "data" / "polycopy.db"))
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm-production-db", action="store_true")
    parser.add_argument("--json", action="store_true", default=True)
    parser.add_argument("--no-json", dest="json", action="store_false")
    args = parser.parse_args(argv)

    try:
        report = repair(
            args.db_path,
            limit=args.limit,
            allow_live=args.allow_live,
            write=args.write,
            confirm_production_db=args.confirm_production_db,
            json_out=args.json,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 2

    print(json.dumps(report.as_dict(), indent=2, default=str), flush=True)
    if report.error:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
