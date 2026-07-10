"""One-time PR24Z canonical source_trade_id migration.

This module is deliberately separate from normal ingestion/writer code.  It
changes only ``source_trades.source_trade_id`` for the 14 audited PR24Z rows,
only after the historical trust gate, dependency audit, state classification,
SQLite backup, and post-migration checks all pass.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from polycopy.migrations.pr24z_marker import MARKER_VERSION as MIGRATION_VERSION

SOURCE = "polymarket_data_api_trades_user"
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REFERENCE_PATH = ROOT / "reports" / "pr24z_historical_production_reference.json"
DEFAULT_MARKER_PATH = ROOT / "data" / ".pr24z_canonical_migration_complete"
IMMUTABLE_FIELDS = (
    "source",
    "trader_address",
    "market_source_id",
    "token_id",
    "side",
    "outcome",
    "quantity",
    "price",
    "timestamp",
    "is_sample",
)
AUDIT_TABLES = (
    "trade_copyability_decisions",
    "copy_candidates",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "orders",
    "positions",
    "settlement_accounting_ledger",
    "wallet_score_decisions",
)
STRING_LINK_COLUMNS = ("source_trade_id",)
INTERNAL_ID_COLUMNS = ("source_trade_internal_id",)


class MigrationBlocked(RuntimeError):
    """Raised when a fail-closed migration precondition is not met."""


@dataclass(frozen=True)
class MappingRow:
    row_number: int
    source_trades_id: str | None
    legacy_source_trade_id: str
    historical_transaction_hash: str
    upstream_source_provided_id: str
    canonical_source_trade_id: str
    immutable_fields_match: bool = False
    legacy_row_exists_once: bool = False
    canonical_collision: bool = False
    dependency_count: int = 0
    migration_state: str = "UNKNOWN"
    migration_applied: bool = False
    post_migration_canonical_exists_once: bool = False
    legacy_id_absent_after: bool = False
    immutable_fields_unchanged: bool = False
    replay_would_insert: bool = True

    def as_csv_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class MigrationResult:
    ok: bool
    state: str
    rows_expected: int = 14
    rows_updated: int = 0
    already_migrated: bool = False
    historical_rows_expected: int = 14
    historical_rows_found: int = 0
    immutable_matches: int = 0
    immutable_mismatches: int = 0
    dependency_audit: dict[str, Any] = field(default_factory=dict)
    backup_path: str | None = None
    backup_sha256: str | None = None
    canonical_row_count: int = 0
    legacy_row_count: int = 0
    integrity_result: str | None = None
    foreign_key_result: int | None = None
    source_trades_count: int | None = None
    marker_path: str | None = None
    marker_created: bool = False
    mapping_artifact_sha256: str | None = None
    error: str | None = None
    mapping: list[MappingRow] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["mapping"] = [m.as_csv_dict() for m in self.mapping]
        return d


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def load_reference(path: Path = DEFAULT_REFERENCE_PATH) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or len(rows) != 14:
        raise MigrationBlocked(f"historical reference must contain exactly 14 rows, found {len(rows) if isinstance(rows, list) else 'non-list'}")
    for i, row in enumerate(rows, 1):
        legacy = row.get("source_trade_id") or row.get("historical_stored_source_trade_id")
        tx = row.get("transaction_hash")
        sp = row.get("sourceProvidedTradeId") or row.get("historical_upstream_source_provided_trade_id")
        if not legacy or not tx or not sp or tx != sp:
            raise MigrationBlocked(f"row {i} missing/proves no canonical upstream id from transaction_hash/sourceProvidedTradeId")
    return rows


def _connect(db_path: Path, *, writable: bool) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode={'rw' if writable else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _qmarks(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _fetch_source_trade(conn: sqlite3.Connection, sid: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM source_trades WHERE source=? AND source_trade_id=?", (SOURCE, sid)).fetchall()


def _immutable_match(db_row: sqlite3.Row, hist: dict[str, Any]) -> bool:
    for f in IMMUTABLE_FIELDS:
        a = db_row[f]
        b = hist[f]
        if f in {"price", "quantity"}:
            if abs(float(a) - float(b)) > 1e-12:
                return False
        elif f == "is_sample":
            if int(a) != int(b):
                return False
        else:
            if str(a) != str(b):
                return False
    return True


def build_mapping(conn: sqlite3.Connection, hist_rows: list[dict[str, Any]], *, state: str = "UNKNOWN") -> list[MappingRow]:
    rows: list[MappingRow] = []
    for i, hist in enumerate(hist_rows, 1):
        legacy = hist["source_trade_id"]
        canonical = hist["transaction_hash"]
        db_rows = _fetch_source_trade(conn, legacy)
        canonical_rows = _fetch_source_trade(conn, canonical)
        source_rows = db_rows or canonical_rows
        source_id = source_rows[0]["id"] if len(source_rows) == 1 else None
        imm = len(source_rows) == 1 and _immutable_match(source_rows[0], hist)
        collision = bool(conn.execute(
            "SELECT 1 FROM source_trades WHERE source=? AND source_trade_id=? AND (? IS NULL OR id != ?)",
            (SOURCE, canonical, source_id, source_id),
        ).fetchone())
        rows.append(MappingRow(
            row_number=i,
            source_trades_id=source_id,
            legacy_source_trade_id=legacy,
            historical_transaction_hash=hist["transaction_hash"],
            upstream_source_provided_id=hist.get("sourceProvidedTradeId") or hist["transaction_hash"],
            canonical_source_trade_id=canonical,
            immutable_fields_match=imm,
            legacy_row_exists_once=len(db_rows) == 1,
            canonical_collision=collision,
            dependency_count=0,
            migration_state=state,
        ))
    return rows


def trust_gate(conn: sqlite3.Connection, hist_rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    found = matches = mismatches = 0
    for hist in hist_rows:
        db_rows = _fetch_source_trade(conn, hist["source_trade_id"])
        if not db_rows:
            db_rows = _fetch_source_trade(conn, hist["transaction_hash"])
        if len(db_rows) == 1:
            found += 1
            if _immutable_match(db_rows[0], hist):
                matches += 1
            else:
                mismatches += 1
        elif db_rows:
            mismatches += len(db_rows)
    return found, matches, mismatches


def classify_state(conn: sqlite3.Connection, hist_rows: list[dict[str, Any]]) -> str:
    old_ids = [r["source_trade_id"] for r in hist_rows]
    new_ids = [r["transaction_hash"] for r in hist_rows]
    old_counts = [conn.execute("SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?", (SOURCE, x)).fetchone()[0] for x in old_ids]
    new_counts = [conn.execute("SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?", (SOURCE, x)).fetchone()[0] for x in new_ids]
    if any(c > 1 for c in old_counts + new_counts):
        return "DUPLICATE"
    old_present = sum(old_counts)
    new_present = sum(new_counts)
    if old_present == 14 and new_present == 0:
        return "ALL_LEGACY"
    if old_present == 0 and new_present == 14:
        return "ALL_CANONICAL"
    for hist, canonical in zip(hist_rows, new_ids):
        rows = conn.execute("SELECT * FROM source_trades WHERE source=? AND source_trade_id=?", (SOURCE, canonical)).fetchall()
        if rows and any(not _immutable_match(r, hist) for r in rows):
            return "COLLISION"
    if old_present + new_present < 14:
        return "MISSING"
    if old_present and new_present:
        return "MIXED"
    return "UNKNOWN"


def audit_dependencies(conn: sqlite3.Connection, mapping: list[MappingRow]) -> dict[str, Any]:
    old_ids = [m.legacy_source_trade_id for m in mapping]
    source_ids = [m.source_trades_id for m in mapping if m.source_trades_id]
    result: dict[str, Any] = {"tables": {}, "no_unsafe_dependent_reference_exists": True}
    if not source_ids:
        source_ids = ["__none__"]
    for table in AUDIT_TABLES:
        info: dict[str, Any] = {"exists": _table_exists(conn, table), "references": []}
        cols = _columns(conn, table)
        total = 0
        if "source_trade_id" in cols:
            legacy_count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE source_trade_id IN ({_qmarks(len(old_ids))})", old_ids).fetchone()[0]
            internal_count = 0
            # settlement_accounting_ledger.source_trade_id references source_trades.id.
            if table == "settlement_accounting_ledger":
                internal_count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE source_trade_id IN ({_qmarks(len(source_ids))})", source_ids).fetchone()[0]
            info["references"].append({"column": "source_trade_id", "legacy_source_trade_id_rows": legacy_count, "source_trades_id_rows": internal_count})
            total += legacy_count + internal_count
        if "source_trade_internal_id" in cols:
            c = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE source_trade_internal_id IN ({_qmarks(len(source_ids))})", source_ids).fetchone()[0]
            info["references"].append({"column": "source_trade_internal_id", "source_trades_id_rows": c})
            total += c
        if table == "wallet_score_decisions":
            if "source_trade_id" in cols:
                link = "source_trade_id-keyed"
            elif "source_trade_internal_id" in cols:
                link = "source_trades.id-keyed"
            elif "candidate_id" in cols:
                link = "otherwise trade-linked"
            else:
                link = "wallet-keyed only"
            info["wallet_score_decisions_linkage"] = link
        info["target_reference_count"] = total
        if total:
            result["no_unsafe_dependent_reference_exists"] = False
        result["tables"][table] = info
    return result


def create_verified_backup(db_path: Path) -> tuple[Path, str]:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.with_name(f"{db_path.name}.pr24z_migration_backup_{ts}")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    chk = sqlite3.connect(backup)
    try:
        integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        fk = list(chk.execute("PRAGMA foreign_key_check"))
    finally:
        chk.close()
    if integrity != "ok" or fk:
        raise MigrationBlocked(f"backup verification failed integrity={integrity} fk_rows={len(fk)}")
    return backup, sha256_file(backup)


def _counts(conn: sqlite3.Connection, hist_rows: list[dict[str, Any]]) -> tuple[int, int]:
    old = [r["source_trade_id"] for r in hist_rows]
    new = [r["transaction_hash"] for r in hist_rows]
    legacy = conn.execute(f"SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id IN ({_qmarks(len(old))})", (SOURCE, *old)).fetchone()[0]
    canon = conn.execute(f"SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id IN ({_qmarks(len(new))})", (SOURCE, *new)).fetchone()[0]
    return legacy, canon


def write_mapping_csv(mapping: list[MappingRow], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(MappingRow.__dataclass_fields__.keys())
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in mapping:
            w.writerow(row.as_csv_dict())
    return sha256_file(path)


def canonical_replay_would_insert(conn: sqlite3.Connection, hist_rows: list[dict[str, Any]]) -> int:
    # A canonical replay is duplicate-free only when all canonical target IDs already exist once.
    return sum(1 for r in hist_rows if conn.execute("SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?", (SOURCE, r["transaction_hash"])).fetchone()[0] == 0)


def migrate(db_path: Path, *, reference_path: Path = DEFAULT_REFERENCE_PATH, marker_path: Path | None = None, apply: bool = False, reports_dir: Path | None = None) -> MigrationResult:
    hist_rows = load_reference(reference_path)
    marker_path = marker_path or db_path.with_name(".pr24z_canonical_migration_complete")
    reports_dir = reports_dir or (ROOT / "reports")
    result = MigrationResult(ok=False, state="UNKNOWN")
    try:
        conn = _connect(db_path, writable=apply)
        try:
            found, matches, mismatches = trust_gate(conn, hist_rows)
            result.historical_rows_found, result.immutable_matches, result.immutable_mismatches = found, matches, mismatches
            state = classify_state(conn, hist_rows)
            result.state = state
            mapping = build_mapping(conn, hist_rows, state=state)
            audit = audit_dependencies(conn, mapping)
            result.dependency_audit = audit
            if found != 14 or matches != 14 or mismatches != 0:
                raise MigrationBlocked("historical trust gate failed")
            if not audit["no_unsafe_dependent_reference_exists"]:
                raise MigrationBlocked("unsafe dependent reference exists")
            if state == "ALL_CANONICAL":
                result.ok = True
                result.already_migrated = True
                result.rows_updated = 0
            elif state != "ALL_LEGACY":
                raise MigrationBlocked(f"migration state {state} is not eligible")
            elif not apply:
                result.ok = True
            else:
                before_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in AUDIT_TABLES if _table_exists(conn, t)}
                backup, backup_hash = create_verified_backup(db_path)
                result.backup_path = str(backup)
                result.backup_sha256 = backup_hash
                conn.execute("BEGIN IMMEDIATE")
                snapshots: dict[str, dict[str, Any]] = {}
                updated = 0
                for hist in hist_rows:
                    row = _fetch_source_trade(conn, hist["source_trade_id"])[0]
                    sid = row["id"]
                    snapshots[sid] = {f: row[f] for f in IMMUTABLE_FIELDS}
                    cur = conn.execute(
                        "UPDATE source_trades SET source_trade_id=? WHERE id=? AND source=? AND source_trade_id=?",
                        (hist["transaction_hash"], sid, SOURCE, hist["source_trade_id"]),
                    )
                    if cur.rowcount != 1:
                        raise MigrationBlocked("rowcount mismatch during update")
                    updated += 1
                if updated != 14:
                    raise MigrationBlocked(f"updated {updated}, expected 14")
                # Post-transaction checks before commit.
                legacy, canon = _counts(conn, hist_rows)
                if legacy != 0 or canon != 14:
                    raise MigrationBlocked(f"post counts wrong legacy={legacy} canonical={canon}")
                for hist in hist_rows:
                    row = _fetch_source_trade(conn, hist["transaction_hash"])[0]
                    if any(row[f] != snapshots[row["id"]][f] for f in IMMUTABLE_FIELDS):
                        raise MigrationBlocked("immutable field changed during migration")
                after_counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before_counts}
                if before_counts != after_counts:
                    raise MigrationBlocked("dependent table count changed")
                conn.commit()
                result.ok = True
                result.rows_updated = 14
        finally:
            try:
                conn.close()
            except Exception:
                pass
        # Fresh read-only post-checks.
        conn2 = _connect(db_path, writable=False)
        try:
            legacy, canon = _counts(conn2, hist_rows)
            result.legacy_row_count = legacy
            result.canonical_row_count = canon
            result.integrity_result = conn2.execute("PRAGMA integrity_check").fetchone()[0]
            result.foreign_key_result = len(list(conn2.execute("PRAGMA foreign_key_check")))
            result.source_trades_count = conn2.execute("SELECT COUNT(*) FROM source_trades").fetchone()[0]
            replay = canonical_replay_would_insert(conn2, hist_rows)
            mapping2 = []
            for row in build_mapping(conn2, hist_rows, state=result.state):
                d = row.as_csv_dict()
                d.update({
                    "migration_applied": result.rows_updated == 14,
                    "post_migration_canonical_exists_once": conn2.execute("SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?", (SOURCE, row.canonical_source_trade_id)).fetchone()[0] == 1,
                    "legacy_id_absent_after": conn2.execute("SELECT COUNT(*) FROM source_trades WHERE source=? AND source_trade_id=?", (SOURCE, row.legacy_source_trade_id)).fetchone()[0] == 0,
                    "immutable_fields_unchanged": True if result.ok else row.immutable_fields_match,
                    "replay_would_insert": replay > 0,
                })
                mapping2.append(MappingRow(**d))
            result.mapping = mapping2
            # Hygiene: the approved mapping artifact is a committed historical record of
            # the old -> canonical conversion. Only the real migration (apply=True) may
            # rewrite it; read-only preflight / idempotency runs must NOT mutate it.
            if apply:
                result.mapping_artifact_sha256 = write_mapping_csv(result.mapping, reports_dir / "pr24z_canonical_identity_migration_mapping.csv")
            if result.ok and apply and not result.already_migrated:
                if canon == 14 and legacy == 0 and result.integrity_result == "ok" and result.foreign_key_result == 0 and result.source_trades_count == 19 and replay == 0:
                    marker = {
                        "migration_version": MIGRATION_VERSION,
                        "timestamp_utc": datetime.now(UTC).isoformat(),
                        "migration_commit_sha": git_sha(),
                        "production_db_path": str(db_path),
                        "backup_path": result.backup_path,
                        "backup_sha256": result.backup_sha256,
                        "rows_expected": 14,
                        "rows_updated": result.rows_updated,
                        "already_migrated": result.already_migrated,
                        "canonical_row_count": canon,
                        "legacy_row_count": legacy,
                        "integrity_result": result.integrity_result,
                        "foreign_key_result": result.foreign_key_result,
                        "mapping_artifact_sha256": result.mapping_artifact_sha256,
                    }
                    marker_path.parent.mkdir(parents=True, exist_ok=True)
                    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")
                    result.marker_path = str(marker_path)
                    result.marker_created = True
                else:
                    raise MigrationBlocked("post-migration marker preconditions failed")
        finally:
            conn2.close()
    except Exception as exc:
        result.error = str(exc)
        result.ok = False if not isinstance(exc, MigrationBlocked) else False
    return result


def write_reports(result: MigrationResult, reports_dir: Path, *, allow_write: bool = True) -> None:
    """Write migration result artifacts.

    Hygiene: when ``allow_write`` is False (read-only preflight / idempotency
    run), this function is a no-op and never rewrites the committed result
    artifacts, so a dry-run cannot silently overwrite the recorded production
    migration evidence.
    """
    if not allow_write:
        return
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "pr24z_canonical_identity_migration_result.json").write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True) + "\n")
    (reports_dir / "pr24z_canonical_identity_dependency_audit.json").write_text(json.dumps(result.dependency_audit, indent=2, sort_keys=True) + "\n")
    plan = {
        "migration_version": MIGRATION_VERSION,
        "normal_ingestion_logic_changed": False,
        "production_execution_authorized": False,
        "state_machine": ["ALL_LEGACY", "ALL_CANONICAL", "MIXED", "MISSING", "DUPLICATE", "COLLISION", "UNKNOWN"],
        "marker_expected_by_pr50": str(DEFAULT_MARKER_PATH),
        "marker_json_created_only_after_complete_verification": True,
    }
    (reports_dir / "pr24z_canonical_identity_migration_plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    md = [
        "# PR24Z canonical identity migration result",
        "",
        f"- ok: {result.ok}",
        f"- state: {result.state}",
        f"- rows_updated: {result.rows_updated}",
        f"- already_migrated: {result.already_migrated}",
        f"- trust_gate: found={result.historical_rows_found} immutable_matches={result.immutable_matches} immutable_mismatches={result.immutable_mismatches}",
        f"- dependency_audit_safe: {result.dependency_audit.get('no_unsafe_dependent_reference_exists')}",
        f"- wallet_score_decisions: {result.dependency_audit.get('tables', {}).get('wallet_score_decisions', {}).get('wallet_score_decisions_linkage')}",
        f"- marker_created: {result.marker_created}",
        f"- error: {result.error}",
        "",
        "## Superseding historical report pointer",
        "The original PR24Z write evidence records the legacy IDs written at that time. Those IDs are historical, not current canonical IDs. The authoritative mapping lives in `reports/pr24z_canonical_identity_migration_mapping.csv`; the authoritative result lives in `reports/pr24z_canonical_identity_migration_result.json`.",
    ]
    (reports_dir / "pr24z_canonical_identity_migration_result.md").write_text("\n".join(md) + "\n")
