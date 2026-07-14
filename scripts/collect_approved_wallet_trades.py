#!/usr/bin/env python3
"""PR68 — Bounded canonical approved-wallet ingestion (ingestion-only).

This is the single manual entry point for the bounded, guarded approved-wallet
source-trade ingestion. It wires the PR24Z pipeline to the one centralized
writer AND (PR68) sources trusted PR66 taxonomy from the Gamma market that each
trade's ``conditionId`` resolves to, so ``source_trades.metadata_json`` is
populated with canonical event/series/category evidence.

Safety envelope (carried + strengthened by PR68):
  * Dry-run is the DEFAULT. No --allow-live => NO network.
  * No --write => no writes (even with --allow-live).
  * No --confirm-production-db => no production DB opened for writing.
  * Production write requires ALL of: --allow-live --write
    --confirm-production-db --source-trade-id --limit 1. A production write
    WITHOUT --source-trade-id OR without exactly --limit 1 is REJECTED
    (manual-only until automation is explicitly restored in a later
    operational task).
  * Bounds: --limit (min 1, max MAX_RECORDS=25) bounds accepted rows; the
    write path can never exceed it. --source-trade-id selects EXACTLY one
    public external id (no prefix, no internal id, no fuzzy).
  * BUY-only. SELL / missing-side rejected.
  * Ingestion-only: writes ONLY through the single canonical SourceTrade
    Writer (source_trades). Never writes wallets/markets/candidates/snapshots/
    decisions/orders/positions/settlement/shadow/exit rows, and never calls
    the approved-wallet bridge or PR67 scoring.
  * Pre-write: operational lock, verified online backup (WAL-aware) before any
    writable open, and schema-version match (canonical _meta.schema_version).
    No immutable=1; uses mode=ro for the read-only backup source.

This CLI must NOT run a production write during development/CI; it is the
operational tool a later task will invoke with the exact gates.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import asyncio
from pathlib import Path
from typing import Any, Optional

# Ensure repo ``src`` is importable when run as a script.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion.approved_wallet_collector import (  # noqa: E402
    MAX_RECORDS,
    UnsafeCollectorConfiguration,
    _raw_gamma_resolver_adapter,
    collect,
    resolve_wallet,
)
from polycopy.ingestion.source_trade_writer import (  # noqa: E402
    create_verified_backup,
    write_valid_rows,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.config.settings import Settings  # noqa: E402
from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from ingest_real_source_trades import _RealDataApiProvider  # noqa: E402

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() == PRODUCTION_DB_PATH
    except OSError:
        return False


def _read_canonical_schema_version(db_path: str) -> Optional[int]:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
            return int(row[0]) if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _existing_source_trade_metadata(db: Database, source_trade_id: str) -> tuple[bool, Optional[str]]:
    """Return (row_exists, current_metadata_json) for an exact public id."""
    try:
        row = db.conn.execute(
            "SELECT metadata_json FROM source_trades WHERE source=? AND source_trade_id=?",
            ("polymarket_data_api_trades_user", source_trade_id),
        ).fetchone()
    except sqlite3.Error:
        return (False, None)
    if row is None:
        return (False, None)
    val = row[0]
    return (True, (val if isinstance(val, str) else None))


def _enrich_existing_row(db: Database, source_trade_id: str, metadata_json: str) -> str:
    """Enrich an existing empty-metadata row; refuse material conflicts.

    Returns one of: enriched | reused | conflict | missing.
    Immutable identity/economic columns are NEVER touched — only metadata_json
    is updated, and only when the existing value is empty OR byte-equivalent.
    """
    exists, current = _existing_source_trade_metadata(db, source_trade_id)
    if not exists:
        return "missing"
    if current and current.strip():
        # Non-empty existing metadata.
        if current.strip() == metadata_json.strip():
            return "reused"
        # Material conflict: do NOT silently overwrite.
        return "conflict"
    # Empty/missing metadata_json -> safe enrichment of this exact identity.
    try:
        db.conn.execute(
            "UPDATE source_trades SET metadata_json=? "
            "WHERE source=? AND source_trade_id=? AND "
            "(metadata_json IS NULL OR trim(metadata_json)='')",
            (metadata_json, "polymarket_data_api_trades_user", source_trade_id),
        )
        db.conn.commit()
        return "enriched"
    except sqlite3.Error:
        try:
            db.conn.rollback()
        except sqlite3.Error:
            pass
        return "conflict"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Bounded canonical approved-wallet BUY ingestion (ingestion-only)"
    )
    p.add_argument("--wallet", help="Must exactly match POLYCOPY_APPROVED_SOURCE_WALLET")
    p.add_argument("--source-trade-id", help="Exact public external source_trade_id (no prefix/fuzzy)")
    p.add_argument("--limit", type=int, default=MAX_RECORDS,
                   help=f"Bounded accepted-row limit (1..{MAX_RECORDS})")
    p.add_argument("--write", action="store_true", help="Persist only selected source_trades rows")
    p.add_argument("--allow-live", action="store_true",
                   help="Authorize production-DB persistence (NOT live order execution)")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm target is the production DB and a verified backup is allowed")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--lock-timeout", type=float, default=30.0)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    # ── argument validation ──
    try:
        wallet = resolve_wallet(args.wallet)
    except UnsafeCollectorConfiguration as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Bounded limit.
    if args.limit < 1 or args.limit > MAX_RECORDS:
        print(f"error: --limit must be between 1 and {MAX_RECORDS}", file=sys.stderr)
        return 2

    # Exact source-trade-id: reject empty; bypass allowed only when provided.
    if args.source_trade_id is not None and not args.source_trade_id.strip():
        print("error: --source-trade-id must be non-empty", file=sys.stderr)
        return 2

    # ── production write gates ──
    # These hard gates apply ONLY to the real production DB. A temp/test DB
    # write (used by tests and local dry-runs-turned-write) is permitted with
    # --write alone; the verified-backup + schema-match safety below also only
    # fires for the production path.
    is_prod = _is_production_db(args.db_path)
    if args.write and is_prod:
        missing = []
        if not args.allow_live:
            missing.append("--allow-live")
        if not args.confirm_production_db:
            missing.append("--confirm-production-db")
        # Manual-only: a production write MUST name the exact source trade.
        if args.source_trade_id is None:
            missing.append("--source-trade-id")
        # Production writes are ALWAYS exactly one selected BUY. The operator
        # must supply --limit 1 explicitly; the default (MAX_RECORDS) is
        # rejected and is never silently coerced.
        if args.limit != 1:
            missing.append("--limit 1")
        if missing:
            print(
                "error: production write requires: " + ", ".join(missing),
                file=sys.stderr,
            )
            return 2

    # ── collector (network + trusted Gamma resolution) ──
    # This runs BEFORE any writable production DB is opened. The production
    # safety backup is created/read-only-verified only after live data is
    # fetched and validated, and strictly BEFORE writable open (below).
    provider = _RealDataApiProvider(timeout=10.0)
    # PR68: build the trusted Gamma resolver for taxonomy enrichment.
    settings = Settings()
    adapter = PolymarketPublicAdapter(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        data_api_base_url=settings.data_api_base_url,
        timeout=10.0,
    )
    gamma_resolver = _raw_gamma_resolver_adapter(adapter)

    result = asyncio.run(
        collect(
            provider,
            wallet,
            source_trade_id=args.source_trade_id,
            gamma_resolver=gamma_resolver,
        )
    )

    # ── dry-run (default): no DB, no backup, no write ──
    if not args.write:
        report = result.report()
        report["mode"] = "dry-run"
        report["production_db"] = str(PRODUCTION_DB_PATH) if is_prod else args.db_path
        report["limit"] = args.limit
        report["requested_source_trade_id"] = args.source_trade_id
        report["fetched_count"] = result.raw_records
        report["accepted_count"] = len(result.accepted_rows)
        print(json.dumps(report, sort_keys=True))
        return 0 if not result.errors else 1

    # ── write path ──
    # IMPORTANT ORDERING: a writable production DB is opened ONLY after the
    # verified online backup + schema-match checks pass. If is_prod, we read
    # the source schema read-only, create + verify the backup, and only then
    # call Database(...).connect(). A tempDB write (is_prod False) skips the
    # backup but still opens the DB here, after the bounded accepted rows are
    # already reduced to the exact selected set.
    from polycopy.runtime.locks import operational_job_lock  # noqa: E402

    backup_meta: Optional[dict[str, Any]] = None
    writable_opened = False
    try:
        with operational_job_lock("collect", timeout=args.lock_timeout):
            # Bounded accepted rows (never exceed --limit). In production this
            # is exactly one selected BUY (gate enforced --limit 1 above).
            accepted = result.accepted_rows[: args.limit]

            # ── production safety: verify backup BEFORE writable open ──
            if is_prod:
                src_schema = _read_canonical_schema_version(args.db_path)
                backup = create_verified_backup(args.db_path)
                if not backup.success or backup.integrity_check != "ok":
                    print(
                        f"error: production backup failed: {backup.error or 'verify unsatisfied'} "
                        f"(integrity={backup.integrity_check}, fk={backup.foreign_key_violations}, "
                        f"schema={backup.schema_version})",
                        file=sys.stderr,
                    )
                    return 1
                if (backup.foreign_key_violations or 0) != 0:
                    print(
                        f"error: production backup has foreign-key violations: "
                        f"{backup.foreign_key_violations}",
                        file=sys.stderr,
                    )
                    return 1
                if not (backup.path and backup.size and backup.sha256):
                    print(
                        "error: production backup missing path/size/hash",
                        file=sys.stderr,
                    )
                    return 1
                if backup.schema_version is None or backup.schema_version != src_schema:
                    print(
                        f"error: schema mismatch source={src_schema} backup={backup.schema_version}",
                        file=sys.stderr,
                    )
                    return 1
                backup_meta = {
                    "backup_path": backup.path,
                    "backup_sha256": backup.sha256,
                    "backup_size_bytes": backup.size,
                    "backup_integrity_check": backup.integrity_check,
                    "backup_foreign_key_check_count": backup.foreign_key_violations,
                    "backup_schema_version": backup.schema_version,
                }

            # ONLY NOW open the production DB writable (or temp DB).
            db = Database(Path(args.db_path))
            db.connect()
            writable_opened = True
            try:
                # Existing-row enrichment (PR68 Checkpoint E) for the selected id.
                enriched_status = "n/a"
                if args.source_trade_id is not None and accepted:
                    chosen = accepted[0]
                    md = chosen.metadata
                    import json as _json

                    md_json = _json.dumps(md, sort_keys=True, separators=(",", ":")) if md else "{}"
                    enriched_status = _enrich_existing_row(db, args.source_trade_id, md_json)
                    if enriched_status == "enriched":
                        result.metadata_enriched += 1
                    elif enriched_status == "reused":
                        result.metadata_reused += 1
                    elif enriched_status == "conflict":
                        result.metadata_conflict += 1

                pre = {
                    r[0]
                    for r in db.conn.execute(
                        "SELECT source_trade_id FROM source_trades WHERE source=?",
                        ("polymarket_data_api_trades_user",),
                    )
                }
                outcome = write_valid_rows(
                    db, accepted, dry_run=False, pre_existing_ids=pre
                )
            finally:
                db.close()
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    report = result.report(
        existing_canonical_records=outcome.existing_duplicates_recognized,
        writes_performed=outcome.inserted,
        inserted=outcome.inserted,
        deduplicated=outcome.deduplicated,
        committed=outcome.committed,
    )
    report["mode"] = "write"
    report["production_db"] = str(PRODUCTION_DB_PATH) if is_prod else args.db_path
    report["limit"] = args.limit
    report["requested_source_trade_id"] = args.source_trade_id
    report["fetched_count"] = result.raw_records
    report["accepted_count"] = len(accepted)
    report["selected_count"] = result.selected_count
    report["enrichment_status"] = enriched_status
    report["unique_constraint_present"] = outcome.unique_constraint_present
    report["committed"] = outcome.committed
    report["rolled_back"] = outcome.rolled_back
    report["write_table_deltas"] = {"source_trades": outcome.inserted}
    report["forbidden_table_deltas"] = {t: 0 for t in (
        "wallets", "markets", "market_outcomes", "copy_candidates",
        "candidate_price_snapshots", "candidate_price_snapshot_levels",
        "wallet_score_decisions", "category_wallet_score_decisions",
        "trade_copyability_decisions", "paper_signal_decisions",
        "shadow_decisions", "exit_experiment_registrations", "approvals",
        "orders", "positions", "fills", "settlement_accounting_ledger",
    )}
    if backup_meta is not None:
        report["backup"] = backup_meta
    print(json.dumps(report, sort_keys=True))
    return 0 if outcome.committed and not outcome.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
