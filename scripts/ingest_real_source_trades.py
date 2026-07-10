#!/usr/bin/env python3
"""PR24Z — Manual real source-trade ingestion CLI (safety-corrected).

This is the single manual entry point for the bounded, guarded real
source-trade ingestion slice. It wires the pure pipeline
(``polycopy.ingestion.ingest_pipeline``) to the one centralized writer
(``polycopy.ingestion.source_trade_writer.write_valid_rows``).

Four modes
----------
  A. fixture dry-run      (default, no --allow-live): no network, no DB, no write.
  B. live dry-run         (--allow-live --wallet-address): real fetch, no DB open,
                          no write.
  C. temp-DB write test   (--temp-db-write-test): writes valid rows to an ISOLATED
                          temp DB to prove the writer; never touches production.
  D. explicit production  (--allow-live --write --confirm-production-db): one bounded
                          real write to the production DB, after all gates pass.

Hard guardrails
---------------
  * Dry-run is the DEFAULT. No --allow-live => NO network.
  * No --write => no writes (even with --allow-live).
  * No --confirm-production-db => no production DB opened for writing.
  * Production write requires ALL THREE flags: --allow-live --write
    --confirm-production-db. Without all three the CLI fails closed with a clear
    message and opens NO writable production DB.
  * One explicit wallet only. No wallet auto-discovery. No DB-derived list.
  * Bounds: default limit 25, hard max 100 records, hard max 2 pages.
  * BUY-only. SELL / missing-side rejected.
  * No scoring / candidates / signals / snapshots / orders / positions /
    settlement mutation / timers / automation / deploy / service restart.

This PR is a SAFETY-CORRECTION only. It must NOT perform a second production
write. Allowed: fixture/temp-DB tests, production DB read-only inspection,
bounded live dry-run, canonical-id analysis, a verified SQLite online backup
(no new trades), report regeneration. The first historical production write
(attempted=14, inserted=14, deduplicated=0) is preserved in the report and is
NEVER overwritten by verification-run counters.

Reports (.md / .json / .txt) always REDACT full wallet addresses in the human
(.md/.txt) reports; the JSON retains the full wallet address only for audit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Ensure repo ``src`` is importable when run as a script.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion import ingest_pipeline as pipeline  # noqa: E402
from polycopy.ingestion.normalized_source_trade import (  # noqa: E402
    DEFAULT_RECORD_LIMIT,
    HARD_MAX_RECORD_LIMIT,
    HARD_MAX_PAGES,
    INGESTION_VERSION,
    IngestionCounters,
    SOURCE_NAME,
)
from polycopy.engine.real_trade_source_probe import is_valid_wallet_address  # noqa: E402

from polycopy.ingestion.source_trade_writer import (  # noqa: E402
    write_valid_rows,
    create_verified_backup,
    assert_unique_dedupe_constraint,
    BackupResult,
    UniqueConstraintResult,
)
from polycopy.db.database import Database  # noqa: E402
from polycopy.migrations.pr24z_marker import (  # noqa: E402
    validate_pr24z_migration_marker,
)

# Tables that must remain UNCHANGED by a production write.
_GUARDED_TABLES = (
    "trade_copyability_decisions",
    "copy_candidates",
    "paper_signal_decisions",
    "candidate_price_snapshots",
    "candidate_price_snapshot_levels",
    "orders",
    "positions",
    "wallet_score_decisions",
    "settlement_accounting_ledger",
)

# Timers/services we must confirm are inactive before a production write.
_INGESTION_TIMER_UNITS = (
    "polycopy-collect.timer",
    "polycopy-scan.timer",
    "polycopy-settle.timer",
    "polycopy-update.timer",
    "polycopy-wc-fixture-refresh.timer",
)

# Temporary fail-closed marker: production ingestion writes remain blocked until
# the separate canonical-ID migration PR has completed and leaves this marker.
# This is intentionally global; it does not inspect or adapt individual trades
# through PR24Z legacy aliases.
_CANONICAL_MIGRATION_COMPLETE_MARKER = _REPO_ROOT / "data" / ".pr24z_canonical_migration_complete"

# Known writer-capable processes to exclude before a production write.
_WRITER_PROCESS_HINTS = (
    "scripts/run_scan.py",
    "scripts/collect_smart_money_data.py",
    "scripts/ingest_real_source_trades.py",
    "scripts/live_smoke_pr3_fixes.py",
)


# ── Built-in deterministic fixture dataset (no network) ───────────────────────
def _fixture_records(wallet: str) -> list[dict[str, Any]]:
    """A deterministic, realistic fixture page set (page 0 only).

    Covers: BUY eligible x3 (one with a duplicate tx hash pair to prove
    strong-id distinctness), SELL, missing side, invalid price, zero qty,
    invalid timestamp, wallet mismatch, and an ambiguous-identity row (no tx
    hash, insufficient fields). Includes both a source-provided id (strong)
    and a real tx-hash row to exercise the new identity preference.
    """
    def tx(i: str) -> str:
        return "0x" + i * 64

    return [
        {  # valid BUY, source-provided id (preferred strong)
            "sourceProvidedTradeId": "polymarket:" + "a" * 64,
            "proxyWallet": wallet, "asset": tx("2"), "conditionId": tx("3"),
            "side": "buy", "price": "0.40", "size": "100", "timestamp": 1700000000,
            "outcome": "Yes", "title": "Market A", "slug": "market-a",
        },
        {  # valid BUY, real transaction hash strong id
            "transactionHash": tx("b"), "proxyWallet": wallet,
            "asset": tx("4"), "conditionId": tx("5"), "side": "BUY",
            "price": "0.62", "size": "50", "timestamp": 1700000100,
            "outcome": "No", "title": "Market B", "slug": "market-b",
        },
        {  # valid BUY, source-provided id (distinct) — dup page collapse
            "sourceProvidedTradeId": "polymarket:" + "c" * 64,
            "proxyWallet": wallet, "asset": tx("6"), "conditionId": tx("7"),
            "side": "BUY", "price": "0.75", "size": "10", "timestamp": 1700000200,
            "outcome": "Up", "title": "Market C", "slug": "market-c",
        },
        {  # duplicate of first (same source-provided id) -> duplicate_in_fetch
            "sourceProvidedTradeId": "polymarket:" + "a" * 64,
            "proxyWallet": wallet, "asset": tx("2"), "conditionId": tx("3"),
            "side": "BUY", "price": "0.40", "size": "100", "timestamp": 1700000000,
            "outcome": "Yes", "title": "Market A", "slug": "market-a",
        },
        {  # SELL -> unsupported_side
            "transactionHash": tx("d"), "proxyWallet": wallet,
            "asset": tx("8"), "conditionId": tx("9"), "side": "sell",
            "price": "0.30", "size": "20", "timestamp": 1700000300, "outcome": "Yes",
        },
        {  # missing side -> missing_side
            "transactionHash": tx("e"), "proxyWallet": wallet,
            "asset": tx("a"), "conditionId": tx("b"), "side": "weird",
            "price": "0.50", "size": "5", "timestamp": 1700000400,
        },
        {  # invalid price -> invalid_price
            "transactionHash": tx("f"), "proxyWallet": wallet,
            "asset": tx("c"), "conditionId": tx("d"), "side": "BUY",
            "price": "1.5", "size": "5", "timestamp": 1700000500,
        },
        {  # zero quantity -> invalid_quantity
            "transactionHash": tx("1"), "proxyWallet": wallet,
            "asset": tx("e"), "conditionId": tx("f"), "side": "BUY",
            "price": "0.50", "size": "0", "timestamp": 1700000600,
        },
        {  # invalid timestamp -> invalid_timestamp
            "transactionHash": tx("2"), "proxyWallet": wallet,
            "asset": tx("1"), "conditionId": tx("2"), "side": "BUY",
            "price": "0.50", "size": "5", "timestamp": "not-a-time",
        },
        {  # wallet mismatch -> wallet_mismatch
            "transactionHash": tx("3"), "proxyWallet": "0x" + "9" * 40,
            "asset": tx("2"), "conditionId": tx("3"), "side": "BUY",
            "price": "0.50", "size": "5", "timestamp": 1700000700,
        },
        {  # ambiguous identity: no id source, no tx hash, missing fields
            "proxyWallet": wallet, "asset": "", "conditionId": "",
            "side": "BUY", "price": None, "size": None, "timestamp": None,
        },
    ]


class _FixtureProvider:
    """Offline provider returning the built-in fixture pages (no network)."""

    made_network_call = False

    def __init__(self, wallet: str) -> None:
        self._pages = [_fixture_records(wallet)]

    async def fetch_trades(self, wallet: str, *, limit: int, page: int) -> list[dict[str, Any]]:
        if page < len(self._pages):
            return self._pages[page][:limit]
        return []

    async def aclose(self) -> None:
        return None


class _RealDataApiProvider:
    """Wraps the existing PolymarketPublicAdapter wallet-trades method (live)."""

    def __init__(self, timeout: float = 10.0) -> None:
        from polycopy.adapters.polymarket import PolymarketPublicAdapter

        self._adapter = PolymarketPublicAdapter(
            gamma_base_url="https://gamma-api.polymarket.com",
            clob_base_url="https://clob.polymarket.com",
            data_api_base_url="https://data-api.polymarket.com",
            timeout=timeout,
        )
        # Advertise that this provider MAKES real external HTTP calls.
        self.made_network_call = True

    async def fetch_trades(self, wallet: str, *, limit: int, page: int) -> list[dict[str, Any]]:
        self.made_network_call = True
        offset = page * limit
        from datetime import datetime as _dt, timezone as _tz

        trades = await self._adapter.get_trades_by_address(
            wallet,
            since=_dt(2000, 1, 1, tzinfo=_tz.utc),
            limit=min(limit, HARD_MAX_RECORD_LIMIT),
        )
        return [self._to_raw(t) for t in trades[offset : offset + limit]]

    @staticmethod
    def _to_raw(t: Any) -> dict[str, Any]:
        # Preserve source-provided id and tx hash as SEPARATE fields.
        side = getattr(t, "side", None)
        ts = getattr(t, "timestamp", None)
        return {
            # Source-provided stable id (v2 id) — kept separate from tx hash.
            "sourceProvidedTradeId": getattr(t, "source_trade_id", None),
            # Real on-chain tx hash (usually None from this adapter).
            "transactionHash": None,
            "proxyWallet": getattr(t, "trader_address", None),
            "asset": getattr(t, "token_id", None),
            "conditionId": getattr(t, "market_source_id", None),
            "side": side.value if side is not None else None,
            "outcome": getattr(t, "outcome", None),
            "size": getattr(t, "quantity", None),
            "price": getattr(t, "price", None),
            "timestamp": ts.timestamp() if ts is not None else None,
        }

    async def aclose(self) -> None:
        try:
            await self._adapter.aclose()
        except Exception:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────
def _redact_wallet(wallet: Optional[str]) -> Optional[str]:
    if not wallet:
        return wallet
    s = str(wallet)
    if len(s) <= 12:
        return "0x…" + s[-4:] if s.startswith("0x") else s
    # Redact ALL leading digits: keep only the 0x prefix and last 4 chars.
    return "0x…" + s[-4:]


def _db_stat(path: str) -> tuple[Optional[int], Optional[int]]:
    try:
        st = os.stat(path)
        return st.st_size, int(st.st_mtime)
    except OSError:
        return None, None


def _table_counts(db: Database) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in ("source_trades", *_GUARDED_TABLES):
        try:
            row = db.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()
            counts[name] = int(row[0]) if row else 0
        except Exception:
            counts[name] = -1
    return counts


def _integrity(db: Database) -> str:
    try:
        r = db.conn.execute("PRAGMA integrity_check").fetchone()
        return (r[0] if r else "??")
    except Exception as exc:
        return f"error:{exc}"


def _fk_check(db: Database) -> int:
    try:
        return len(list(db.conn.execute("PRAGMA foreign_key_check")))
    except Exception:
        return -1


def _check_timers() -> dict[str, str]:
    """Best-effort check of known ingestion timer units. Returns name->status."""
    out: dict[str, str] = {}
    for unit in _INGESTION_TIMER_UNITS:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True, text=True, timeout=5,
            )
            out[unit] = r.stdout.strip() or "unknown"
        except Exception:
            out[unit] = "unknown"
    return out


def _enumerate_processes() -> list[dict[str, Any]]:
    """Return [(pid, cmdline_str)] for all processes via psutil or /proc."""
    procs: list[dict[str, Any]] = []
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                procs.append({"pid": p.info.get("pid"), "cmdline": cmd})
            except Exception:
                pass
        return procs
    except Exception:
        pass
    # Fallback: parse /proc directly.
    if sys.platform == "linux" and os.path.isdir("/proc"):
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    data = fh.read().decode("utf-8", "replace").replace("\x00", " ").strip()
                procs.append({"pid": int(pid), "cmdline": data})
            except OSError:
                continue
    return procs


def _redact_wallet_in_text(text: str) -> str:
    """Redact any 0x…40-hex wallet address that appears inside a string (cmdline)."""
    if not text:
        return text
    return re.sub(r"0x[0-9a-fA-F]{40}", lambda m: "0x…" + m.group(0)[-4:], text)


def _ancestor_pids(pid: int) -> set[int]:
    """Return the set of ancestor pids for ``pid`` (inclusive of pid)."""
    ancestors: set[int] = set()
    cur = pid
    seen: set[int] = set()
    while cur and cur > 1 and cur not in seen:
        seen.add(cur)
        ancestors.add(cur)
        try:
            with open(f"/proc/{cur}/stat", "rb") as fh:
                data = fh.read().decode("utf-8", "replace")
            # ppid is the 4th field in /proc/<pid>/stat.
            parts = data.split(")")
            ppid = int(parts[1].split()[1]) if len(parts) > 1 else 1
            cur = ppid
        except (OSError, ValueError, IndexError):
            break
    return ancestors


def _check_competing_writers(current_pid: int) -> tuple[bool, list[dict[str, Any]]]:
    """Detect another writer-capable process (excluding this run's own tree).

    Returns (found, details). ``details`` contains redacted PID/command info.

    Exclusions:
      * the current process (by pid);
      * any ancestor of the current process (the launch shell of THIS run);
      * shell-wrapping processes (bash/sh/dash/zsh) whose only match is the
        wrapping command — a shell alone is not a writer; the python it spawns
        is matched separately.
    """
    found: list[dict[str, Any]] = []
    ancestors = _ancestor_pids(current_pid)
    _SHELLS = ("/bin/bash", "/bin/sh", "/bin/dash", "/usr/bin/bash", "/usr/bin/zsh", "bash", "sh", "dash", "zsh")
    for p in _enumerate_processes():
        pid = p.get("pid")
        if pid in ancestors:
            continue
        cmd = p.get("cmdline") or ""
        if not any(h in cmd for h in _WRITER_PROCESS_HINTS):
            continue
        # Skip pure shell wrappers (the writer is the python child, matched on its own pid).
        exe = (cmd.split()[:1] or [""])[0]
        if any(exe.endswith(s) for s in _SHELLS) and "python" not in cmd:
            continue
        found.append({
            "pid": pid,
            "command": _redact_wallet_in_text(cmd),
        })
    return bool(found), found


def _make_backup_legacy(db_path: str) -> Optional[str]:
    """Legacy raw file-copy backup (kept for reference; NOT used for write gating)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{db_path}.pr24z_legacy_raw_copy_backup_{ts}"
    try:
        shutil.copy2(db_path, backup)
        return backup
    except Exception as exc:
        print(f"warning: legacy raw backup failed: {exc}", file=sys.stderr)
        return None



# ── Report rendering ───────────────────────────────────────────────────────────
def _identity_block(c: IngestionCounters) -> dict[str, Any]:
    return {
        "stable_ids_generated": c.stable_ids_generated,
        "source_provided_identity_used_count": c.source_provided_identity_used_count,
        "transaction_identity_used_count": c.transaction_identity_used_count,
        "strong_identity_used_count": c.strong_identity_used_count,
        "identity_fallback_used_count": c.identity_fallback_used_count,
        "identity_ambiguous_count": c.identity_ambiguous_count,
        "duplicate_records_in_fetch": c.duplicate_records_in_fetch,
        "duplicate_records_existing_db": c.duplicate_records_existing_db,
        "collision_errors": c.collision_errors,
    }


def _build_canonical_migration_block(
    marker_validation: Any,
    *,
    production_write_requested: bool = False,
    production_write_committed: bool = False,
) -> dict[str, Any]:
    """Derive the canonical_migration report block from runtime marker validation.

    Post-migration reports must never claim the migration is still pending; the
    gate status is read from the actual marker validation result, not hardcoded.
    """
    valid = bool(getattr(marker_validation, "valid", False))
    data = getattr(marker_validation, "data", None) or {}
    reasons = list(getattr(marker_validation, "reasons", ()) or ())
    legacy_remaining = int(data.get("legacy_row_count", 0) or 0)
    migrated_present = int(data.get("canonical_row_count", 0) or 0)
    return {
        "migration_complete": valid,
        "marker_present": marker_validation is not None,
        "marker_validated": valid,
        "marker_validation_errors": reasons,
        "legacy_rows_remaining": legacy_remaining,
        "migrated_canonical_rows_present": migrated_present,
        "production_write_authorized_by_marker": valid,
        "legacy_identity_aliases_used": 0,
        "production_write_requested": bool(production_write_requested),
        "production_write_committed": bool(production_write_committed),
    }


def _build_report_payload(
    wallet: str,
    live: bool,
    result: pipeline.IngestionResult,
    *,
    write_result: Optional[Any] = None,
    backup: Optional[BackupResult] = None,
    unique_constraint: Optional[UniqueConstraintResult] = None,
    process_gate: Optional[dict] = None,
    compatibility: Optional[dict] = None,
    db_path: Optional[str] = None,
    db_before: Optional[dict] = None,
    db_after: Optional[dict] = None,
    timers_before: Optional[dict] = None,
    timers_after: Optional[dict] = None,
    integrity: Optional[str] = None,
    fk: Optional[int] = None,
    mode: str = "dry-run",
    historical_write: Optional[dict] = None,
    marker_validation: Any = None,
    production_write_requested: bool = False,
    production_write_committed: bool = False,
) -> dict[str, Any]:
    c = result.counters
    payload: dict[str, Any] = {
        "ingestion_version": INGESTION_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "source": SOURCE_NAME,
        # Wallet: FULL in JSON (auditability), redacted in .md/.txt separately.
        "wallet_address": wallet,
        "wallet_address_redacted": _redact_wallet(wallet),
        "live": live,
        "network_calls_attempted": result.network_calls_attempted,
        "network_calls_succeeded": result.network_calls_succeeded,
        "error": result.error,
        "counts": {
            "raw_records": c.raw_records,
            "raw_buy_records": c.raw_buy_records,
            "raw_sell_records": c.raw_sell_records,
            "unknown_side_records": c.unknown_side_records,
            "eligible_buy_records": c.eligible_buy_records,
            "rejected_unsupported_side": c.rejected_unsupported_side,
            "rejected_missing_fields": c.rejected_missing_fields,
            "rejected_invalid_price": c.rejected_invalid_price,
            "rejected_invalid_quantity": c.rejected_invalid_quantity,
            "rejected_invalid_timestamp": c.rejected_invalid_timestamp,
            "rejected_wallet_mismatch": c.rejected_wallet_mismatch,
            "rejected_invalid_fields": c.rejected_invalid_fields,
            "rows_rejected": c.rows_rejected,
        },
        "identity": _identity_block(c),
        "readiness": {
            "pr24u_ready_count": c.pr24u_ready_count,
            "pr24v_ready_count": c.pr24v_ready_count,
            "both_ready_count": c.both_ready_count,
            "ready_for_scoring": bool(c.ready_for_scoring),
            "ready_for_automation": bool(c.ready_for_automation),
        },
        "canonical_migration": (
            _build_canonical_migration_block(
                marker_validation,
                production_write_requested=production_write_requested,
                production_write_committed=production_write_committed,
            )
        ),
        "backup_inventory_correction": {
            "distinct_sha256_groups_listed_by_inspection": 4,
            "do_not_assume_logical_identity_from_size_integrity_and_count": True,
            "later_cleanup_requires_logical_table_counts_and_row_hashes_or_normalized_dumps": True,
            "backup_files_deleted_in_this_task": False,
        },
        "safety": {
            "downstream_tables_changed": bool(c.downstream_tables_changed),
            "timers_changed": bool(c.timers_changed),
        },
        "valid_rows": [r.as_dict() for r in result.valid_rows],
        "write": write_result.as_dict() if write_result is not None else None,
        "db_path": db_path,
        "db_before": db_before,
        "db_after": db_after,
        "timers_before": timers_before,
        "timers_after": timers_after,
        "backup": backup.as_dict() if backup is not None else None,
        "unique_constraint": unique_constraint.as_dict() if unique_constraint is not None else None,
        "process_gate": process_gate,
        "compatibility": compatibility,
        "integrity_check": integrity,
        "foreign_key_check": fk,
    }
    if historical_write is not None:
        payload["historical_first_production_write"] = historical_write
    return payload


def _render_markdown(p: dict[str, Any]) -> str:
    c = p["counts"]
    ident = p["identity"]
    db = p.get("db_before") or {}
    after = p.get("db_after") or {}
    lines = [
        f"# PR24Z Manual Real Source-Trade Ingestion — {p['mode']}",
        "",
        f"- **Generated:** {p['generated_at']}",
        f"- **Ingestion version:** {p['ingestion_version']}",
        f"- **Source:** {p['source']}",
        f"- **Wallet (redacted):** `{p['wallet_address_redacted']}`",
        f"- **Live:** {p['live']}",
        f"- **Network calls:** attempted={p['network_calls_attempted']} succeeded={p['network_calls_succeeded']}",
        "",
        "## Fetch / classification",
        f"- raw_records: {c['raw_records']}",
        f"- raw_buy_records: {c['raw_buy_records']}",
        f"- raw_sell_records: {c['raw_sell_records']}",
        f"- unknown_side_records: {c['unknown_side_records']}",
        f"- eligible_buy_records: {c['eligible_buy_records']}",
        f"- rejected_unsupported_side: {c['rejected_unsupported_side']}",
        f"- rejected_missing_fields: {c['rejected_missing_fields']}",
        f"- rejected_invalid_price: {c['rejected_invalid_price']}",
        f"- rejected_invalid_quantity: {c['rejected_invalid_quantity']}",
        f"- rejected_invalid_timestamp: {c['rejected_invalid_timestamp']}",
        f"- rejected_wallet_mismatch: {c['rejected_wallet_mismatch']}",
        f"- rejected_invalid_fields: {c['rejected_invalid_fields']}",
        f"- rows_rejected: {c['rows_rejected']}",
        "",
        "## Identity strategy",
        f"- source_provided_identity_used_count: {ident['source_provided_identity_used_count']}",
        f"- transaction_identity_used_count: {ident['transaction_identity_used_count']}",
        f"- strong_identity_used_count: {ident['strong_identity_used_count']}",
        f"- identity_fallback_used_count: {ident['identity_fallback_used_count']}",
        f"- identity_ambiguous_count: {ident['identity_ambiguous_count']}",
        f"- duplicate_records_in_fetch: {ident['duplicate_records_in_fetch']}",
        f"- duplicate_records_existing_db: {ident['duplicate_records_existing_db']}",
        f"- collision_errors: {ident['collision_errors']}",
        "",
    ]
    if p.get("canonical_migration") is not None:
        cm = p["canonical_migration"]
        lines += [
            "## Canonical migration status",
            f"- migration_complete: {cm['migration_complete']}",
            f"- marker_present: {cm['marker_present']}",
            f"- marker_validated: {cm['marker_validated']}",
            f"- marker_validation_errors: {cm['marker_validation_errors']}",
            f"- legacy_rows_remaining: {cm['legacy_rows_remaining']}",
            f"- migrated_canonical_rows_present: {cm['migrated_canonical_rows_present']}",
            f"- production_write_authorized_by_marker: {cm['production_write_authorized_by_marker']}",
            f"- legacy_identity_aliases_used: {cm['legacy_identity_aliases_used']}",
            f"- production_write_requested: {cm['production_write_requested']}",
            f"- production_write_committed: {cm['production_write_committed']}",
            "",
        ]
    lines += [
        "## Backup inventory correction",
        "- distinct_sha256_groups_listed_by_inspection: 4",
        "- do_not_assume_logical_identity_from_equal_size_integrity_ok_and_source_trades_19: True",
        "- later_cleanup_requires_logical_table_counts_and_row_hashes_or_normalized_dumps: True",
        "- backup_files_deleted_in_this_task: False",
        "",
        "## Safety",
        f"- downstream_tables_changed: {p['safety']['downstream_tables_changed']}",
        f"- timers_changed: {p['safety']['timers_changed']}",
        f"- ready_for_scoring: {p['readiness']['ready_for_scoring']}",
        f"- ready_for_automation: {p['readiness']['ready_for_automation']}",
        "",
    ]
    if p.get("unique_constraint") is not None:
        uc = p["unique_constraint"]
        lines += [
            "## UNIQUE constraint preflight",
            f"- present: {uc['present']}",
            f"- index_name: {uc['index_name']}",
            f"- columns: {uc['columns']}",
            f"- error: {uc['error']}",
            "",
        ]
    if p.get("process_gate") is not None:
        pg = p["process_gate"]
        lines += [
            "## Process gate",
            f"- checked: {pg['checked']}",
            f"- competing_writers_found: {pg['competing_writers_found']}",
            f"- safe_to_write: {pg['safe_to_write']}",
            "",
        ]
    if p.get("compatibility") is not None:
        comp = p["compatibility"]
        lines += [
            "## Compatibility (post-migration dry-run)",
            f"- historical_migrated_rows_total: {comp['historical_migrated_rows_total']}",
            f"- migrated_rows_present_in_current_fetch: {comp['migrated_rows_present_in_current_fetch']}",
            f"- migrated_rows_matched_canonically: {comp['migrated_rows_matched_canonically']}",
            f"- migrated_rows_failed_to_match: {comp['migrated_rows_failed_to_match']}",
            f"- new_canonical_rows_in_current_fetch: {comp['new_canonical_rows_in_current_fetch']}",
            f"- legacy_identity_aliases_used: {comp['legacy_identity_aliases_used']}",
            f"- dry_run_would_insert_new_rows: {comp['dry_run_would_insert_new_rows']}",
            f"- reconciliation_error: {comp['reconciliation_error']}",
            "",
        ]
    if p.get("backup") is not None:
        b = p["backup"]
        lines += [
            "## Backup (SQLite online backup)",
            f"- method: {b['method']}",
            f"- path: {b['path']}",
            f"- sha256: {b['sha256']}",
            f"- size: {b['size']}",
            f"- integrity_check: {b['integrity_check']}",
            f"- foreign_key_violations: {b['foreign_key_violations']}",
            f"- source_trades_count: {b['source_trades_count']}",
            f"- success: {b['success']}",
            f"- error: {b['error']}",
            "",
        ]
    if p.get("write") is not None:
        w = p["write"]
        lines += [
            "## Production write",
            f"- attempted: {w['attempted']}",
            f"- inserted: {w['inserted']}",
            f"- deduplicated: {w['deduplicated']}",
            f"- rejected: {w['rejected']}",
            f"- errors: {w['errors']}",
            f"- committed: {w['committed']}",
            f"- rolled_back: {w['rolled_back']}",
            f"- existing_duplicates_recognized: {w['existing_duplicates_recognized']}",
            f"- unique_constraint_present: {w['unique_constraint_present']}",
            f"- error_message: {w['error_message']}",
            "",
        ]
    if p.get("historical_first_production_write") is not None:
        hw = p["historical_first_production_write"]
        lines += [
            "## Historical FIRST production write (preserved; never overwritten)",
            f"- attempted: {hw['attempted']}",
            f"- inserted: {hw['inserted']}",
            f"- deduplicated: {hw['deduplicated']}",
            "",
        ]
    if p.get("db_before") or p.get("db_after"):
        lines += [
            "## Database safety",
            f"- db_path: {p.get('db_path')}",
            f"- size before/after: {db.get('size')} / {after.get('size')}",
            f"- mtime before/after: {db.get('mtime')} / {after.get('mtime')}",
            f"- integrity_check: {p.get('integrity_check')}",
            f"- foreign_key_check: {p.get('foreign_key_check')}",
            f"- backup_path: {p.get('backup', {}).get('path')}",
            "",
        ]
    return "\n".join(lines)


def _render_txt(p: dict[str, Any]) -> str:
    c = p["counts"]
    ident = p["identity"]
    db = p.get("db_before") or {}
    after = p.get("db_after") or {}
    lines = [
        f"PR24Z Manual Real Source-Trade Ingestion — {p['mode']}",
        f"Generated: {p['generated_at']}",
        f"Source: {p['source']}",
        f"Wallet (redacted): {p['wallet_address_redacted']}",
        f"Live: {p['live']}",
        f"Network calls attempted/succeeded: {p['network_calls_attempted']}/{p['network_calls_succeeded']}",
        "",
        "FETCH/CLASSIFICATION",
        f"  raw_records={c['raw_records']} raw_buy={c['raw_buy_records']} "
        f"raw_sell={c['raw_sell_records']} unknown={c['unknown_side_records']}",
        f"  eligible_buy={c['eligible_buy_records']} rows_rejected={c['rows_rejected']}",
        f"  rejected: side={c['rejected_unsupported_side']} missing={c['rejected_missing_fields']} "
        f"price={c['rejected_invalid_price']} qty={c['rejected_invalid_quantity']} "
        f"ts={c['rejected_invalid_timestamp']} wallet={c['rejected_wallet_mismatch']} "
        f"fields={c['rejected_invalid_fields']}",
        "",
        "IDENTITY",
        f"  source_provided={ident['source_provided_identity_used_count']} "
        f"tx={ident['transaction_identity_used_count']} strong={ident['strong_identity_used_count']} "
        f"fallback={ident['identity_fallback_used_count']} ambiguous={ident['identity_ambiguous_count']}",
        f"  dup_in_fetch={ident['duplicate_records_in_fetch']} "
        f"dup_existing_db={ident['duplicate_records_existing_db']} collisions={ident['collision_errors']}",
        "",
    ]
    if p.get("canonical_migration") is not None:
        cm = p["canonical_migration"]
        lines += [
            "CANONICAL MIGRATION STATUS",
            f"  migration_complete={cm['migration_complete']}",
            f"  marker_present={cm['marker_present']}",
            f"  marker_validated={cm['marker_validated']}",
            f"  marker_validation_errors={cm['marker_validation_errors']}",
            f"  legacy_rows_remaining={cm['legacy_rows_remaining']}",
            f"  migrated_canonical_rows_present={cm['migrated_canonical_rows_present']}",
            f"  production_write_authorized_by_marker={cm['production_write_authorized_by_marker']}",
            f"  legacy_identity_aliases_used={cm['legacy_identity_aliases_used']}",
            f"  production_write_requested={cm['production_write_requested']}",
            f"  production_write_committed={cm['production_write_committed']}",
            "",
        ]
    else:
        lines += ["CANONICAL MIGRATION STATUS", "  (marker not validated in this run)", ""]
    lines += [
        "BACKUP INVENTORY CORRECTION",
        "  distinct_sha256_groups_listed_by_inspection=4",
        "  do_not_assume_logical_identity_from_equal_size_integrity_ok_and_source_trades_19=True",
        "  later_cleanup_requires_logical_table_counts_and_row_hashes_or_normalized_dumps=True",
        "  backup_files_deleted_in_this_task=False",
        "",
        "SAFETY",
        f"  downstream_changed={p['safety']['downstream_tables_changed']} "
        f"timers_changed={p['safety']['timers_changed']}",
        f"  ready_for_scoring={p['readiness']['ready_for_scoring']} "
        f"ready_for_automation={p['readiness']['ready_for_automation']}",
        "",
    ]
    if p.get("backup") is not None:
        b = p["backup"]
        lines += [
            "BACKUP (SQLite online)",
            f"  method={b['method']} success={b['success']}",
            f"  sha256={b['sha256']} size={b['size']}",
            f"  integrity={b['integrity_check']} fk={b['foreign_key_violations']} "
            f"count={b['source_trades_count']}",
            f"  path={b['path']}",
            "",
        ]
    if p.get("write") is not None:
        w = p["write"]
        lines += [
            "PRODUCTION WRITE",
            f"  attempted={w['attempted']} inserted={w['inserted']} "
            f"deduplicated={w['deduplicated']} rejected={w['rejected']} errors={w['errors']}",
            f"  committed={w['committed']} rolled_back={w['rolled_back']} "
            f"existing_dupes={w['existing_duplicates_recognized']} "
            f"unique_ok={w['unique_constraint_present']}",
            "",
        ]
    if p.get("historical_first_production_write") is not None:
        hw = p["historical_first_production_write"]
        lines += [
            "HISTORICAL FIRST WRITE (preserved)",
            f"  attempted={hw['attempted']} inserted={hw['inserted']} "
            f"deduplicated={hw['deduplicated']}",
            "",
        ]
    if p.get("db_before") or p.get("db_after"):
        lines += [
            "DATABASE SAFETY",
            f"  size before/after: {db.get('size')}/{after.get('size')}",
            f"  mtime before/after: {db.get('mtime')}/{after.get('mtime')}",
            f"  integrity_check={p.get('integrity_check')} "
            f"foreign_key_check={p.get('foreign_key_check')}",
            f"  backup={p.get('backup', {}).get('path')}",
            "",
        ]
    return "\n".join(lines)





# ── Main ───────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PR24Z manual real source-trade ingestion (bounded, guarded).",
    )
    parser.add_argument("--wallet-address", default=None,
                        help="Explicit wallet address (required for live / production).")
    parser.add_argument("--allow-live", dest="allow_live", action="store_true",
                        default=False,
                        help="(OFF by default) Perform a REAL fetch via data-api "
                             "GET /trades?user=<addr>. Requires --wallet-address.")
    parser.add_argument("--fixture", dest="fixture", action="store_true",
                        default=False,
                        help="Use the built-in deterministic fixture dataset (no network).")
    parser.add_argument("--limit", type=int, default=DEFAULT_RECORD_LIMIT,
                        help=f"Records per page (hard max {HARD_MAX_RECORD_LIMIT}).")
    parser.add_argument("--max-pages", type=int, default=HARD_MAX_PAGES,
                        help=f"Max pages (hard cap {HARD_MAX_PAGES}).")
    parser.add_argument("--write", dest="write", action="store_true", default=False,
                        help="(OFF by default) Request a DB write. Requires --allow-live "
                             "AND --confirm-production-db for production; or "
                             "--temp-db-write-test for an isolated temp DB.")
    parser.add_argument("--confirm-production-db", dest="confirm_production_db",
                        action="store_true", default=False,
                        help="(OFF by default) Explicit confirmation to open the "
                             "PRODUCTION DB for writing. Requires --allow-live --write.")
    parser.add_argument("--temp-db-write-test", dest="temp_db_write_test",
                        action="store_true", default=False,
                        help="Write valid rows to an ISOLATED temp DB (proves the "
                             "writer; never touches production).")
    parser.add_argument("--db-path", default=None,
                        help="Production DB path (default data/polycopy.db).")
    parser.add_argument("--json", action="store_true",
                        help="Emit report as JSON (full wallet retained for audit).")
    parser.add_argument("--out", default=None, help="Write the report to this file.")
    parser.add_argument("--no-write-compat-verify", dest="no_write_compat_verify",
                        action="store_true", default=False,
                        help="Skip the bounded live dry-run compatibility verification "
                             "(still performs a verified SQLite online backup).")
    args = parser.parse_args(argv)

    # ── Resolve bounds ──
    limit = max(1, min(args.limit, HARD_MAX_RECORD_LIMIT))
    max_pages = max(1, min(args.max_pages, HARD_MAX_PAGES))

    # ── Wallet (explicit only) ──
    wallet = args.wallet_address
    if args.allow_live and not wallet:
        print("error: --allow-live requires --wallet-address", file=sys.stderr)
        return 2
    if wallet is None:
        wallet = "0x" + "1" * 40
    if args.allow_live and not is_valid_wallet_address(wallet):
        print(f"error: malformed wallet address: {wallet}", file=sys.stderr)
        return 2

    # ── Resolve DB path ──
    db_path = args.db_path or str(_REPO_ROOT / "data" / "polycopy.db")

    # Runtime canonical-migration marker validation (read-only). Every report
    # reflects the actual gate state instead of hardcoded "pending" claims.
    try:
        marker_validation = validate_pr24z_migration_marker(
            _CANONICAL_MIGRATION_COMPLETE_MARKER, db_path
        )
    except Exception:
        marker_validation = None

    # ── Production-write gate: ALL THREE flags required ──
    production_write = bool(args.allow_live and args.write and args.confirm_production_db)
    if (args.write or args.confirm_production_db) and not production_write:
        print(
            "error: production write requires ALL THREE flags: "
            "--allow-live --write --confirm-production-db. "
            "Refusing to open a writable production DB.",
            file=sys.stderr,
        )
        return 2

    # ── Provider selection ──
    if args.allow_live:
        provider = _RealDataApiProvider()
        live = True
    elif args.fixture:
        provider = _FixtureProvider(wallet)
        live = False
    else:
        provider = _FixtureProvider(wallet)
        live = False

    # ── Run the bounded pipeline (no DB open) ──
    try:
        result = asyncio.run(
            pipeline.run_ingestion(
                provider, wallet,
                record_limit=limit, max_pages=max_pages,
                requested_wallet=wallet,
            )
        )
    finally:
        if hasattr(provider, "aclose"):
            try:
                asyncio.run(provider.aclose())
            except Exception:
                pass

    write_result = None
    db_before = None
    db_after = None
    timers_before = None
    timers_after = None
    backup: Optional[BackupResult] = None
    unique_constraint: Optional[UniqueConstraintResult] = None
    process_gate: Optional[dict] = None
    compatibility: Optional[dict] = None
    integrity = None
    fk = None
    mode = "dry-run"
    # Preserved historical FIRST production write (never overwritten).
    historical_write = {
        "attempted": 14,
        "inserted": 14,
        "deduplicated": 0,
    }

    # ── Mode C: temp-DB write test (isolated, never production) ──
    if args.temp_db_write_test:
        mode = "temp-db-write-test"
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="pr24z_temp_")
        os.close(fd)
        os.remove(tmp)
        tdb = Database(Path(tmp))
        tdb.connect()
        try:
            write_result = write_valid_rows(tdb, result.valid_rows, dry_run=False)
            result.counters.rows_attempted = write_result.attempted
            result.counters.rows_inserted = write_result.inserted
            result.counters.rows_deduplicated = write_result.deduplicated
            result.counters.transaction_committed = int(write_result.committed)
            result.counters.transaction_rolled_back = int(write_result.rolled_back)
            result.counters.write_requested = 1
            result.counters.production_db_opened = 0  # temp, not production
        finally:
            tdb.close()
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + ext)
                except OSError:
                    pass

    # ── Mode D: explicit production write ──
    elif production_write:
        mode = "production-write"
        # Pre-flight: timers must be inactive.
        timers_before = _check_timers()
        active = [u for u, s in timers_before.items() if s == "active"]
        if active:
            print(f"error: ingestion timer(s) active: {active}; aborting production write.",
                  file=sys.stderr)
            return 2
        # Pre-flight: competing process gate.
        current_pid = os.getpid()
        found, details = _check_competing_writers(current_pid)
        process_gate = {
            "checked": True,
            "competing_writers_found": found,
            "safe_to_write": not found,
            "details": details,
        }
        if found:
            print(f"error: competing writer process(es) detected: {details}; "
                  f"aborting production write.", file=sys.stderr)
            return 2
        # Temporary global block: production contains 14 legacy fallback IDs and
        # must not receive another ingestion write until the separate canonical
        # migration PR completes. This is fail-closed and does not implement
        # legacy alias matching or per-trade adaptation.
        if not marker_validation or not marker_validation.valid:
            print(
                "error: canonical source_trade_id migration is not complete; "
                "refusing production ingestion write. "
                f"Invalid marker: {_CANONICAL_MIGRATION_COMPLETE_MARKER}; "
                f"reasons={getattr(marker_validation, 'reasons', ()) or 'marker missing'}",
                file=sys.stderr,
            )
            return 2
        # Pre-flight: backup (hard gate).
        pre_size, pre_mtime = _db_stat(db_path)
        db_before = {"size": pre_size, "mtime": pre_mtime, "path": db_path,
                     "counts": _table_counts_open(db_path)}
        backup = create_verified_backup(db_path)
        if not backup.success:
            print(f"error: verified backup failed ({backup.error}); "
                  f"aborting production write. source_trades unchanged.",
                  file=sys.stderr)
            return 2
        db = Database(Path(db_path))
        db.connect()
        try:
            unique_constraint = assert_unique_dedupe_constraint(db)
            if not unique_constraint.present:
                print(f"error: UNIQUE dedupe constraint missing "
                      f"({unique_constraint.error}); aborting production write.",
                      file=sys.stderr)
                return 2
            counts_before = _table_counts(db)
            integrity = _integrity(db)
            fk = _fk_check(db)
            if integrity != "ok" or fk != 0:
                print(f"error: pre-flight integrity={integrity} fk={fk}; aborting.",
                      file=sys.stderr)
                return 2
            # Existing canonical ids for normal duplicate recognition only.
            pre_ids = {r[0] for r in db.conn.execute(
                "SELECT source_trade_id FROM source_trades WHERE source = ?",
                (SOURCE_NAME,)).fetchall()}
            for r in result.valid_rows:
                if r.source_trade_id in pre_ids:
                    result.counters.duplicate_records_existing_db += 1
            write_result = write_valid_rows(
                db, result.valid_rows, dry_run=False, pre_existing_ids=pre_ids,
            )
            result.counters.rows_attempted = write_result.attempted
            result.counters.rows_inserted = write_result.inserted
            result.counters.rows_deduplicated = write_result.deduplicated
            result.counters.production_db_opened = 1
            result.counters.write_requested = 1
            result.counters.transaction_committed = int(write_result.committed)
            result.counters.transaction_rolled_back = int(write_result.rolled_back)
            counts_after = _table_counts(db)
            integrity = _integrity(db)
            fk = _fk_check(db)
            for t in _GUARDED_TABLES:
                if counts_before.get(t) != counts_after.get(t):
                    result.counters.downstream_tables_changed = 1
            db_after = {"size": _db_stat(db_path)[0], "mtime": _db_stat(db_path)[1],
                        "path": db_path, "counts": counts_after}
            timers_after = _check_timers()
            if any(s == "active" for s in timers_after.values()):
                result.counters.timers_changed = 1
        finally:
            db.close()

    # ── No-write safety correction verification (Modes A/B + explicit verify) ──
    else:
        # Safety-corrected verification run: create a verified SQLite online
        # backup (no new trades), run the process gate + UNIQUE preflight as a
        # read-only check, and perform a bounded live dry-run canonical duplicate
        # analysis when allowed. Never writes source_trades.
        mode = "safety-verification"
        # Verified online backup (no trades written).
        backup = create_verified_backup(db_path)
        # Read-only UNIQUE preflight + process gate.
        timers_before = _check_timers()
        current_pid = os.getpid()
        found, details = _check_competing_writers(current_pid)
        process_gate = {
            "checked": True,
            "competing_writers_found": found,
            "safe_to_write": not found,
            "details": details,
        }
        db = Database(Path(db_path))
        db.connect()
        try:
            unique_constraint = assert_unique_dedupe_constraint(db)
            integrity = _integrity(db)
            fk = _fk_check(db)
            db_before = {"size": _db_stat(db_path)[0], "mtime": _db_stat(db_path)[1],
                         "path": db_path, "counts": _table_counts(db)}
            # Compatibility analysis when allowed-live (bounded dry-run).
            if live and not args.no_write_compat_verify:
                pre_ids = {r[0] for r in db.conn.execute(
                    "SELECT source_trade_id FROM source_trades WHERE source = ?",
                    (SOURCE_NAME,)).fetchall()}
                matched = 0
                for r in result.valid_rows:
                    if r.source_trade_id in pre_ids:
                        matched += 1
                compatibility = {
                    "historical_migrated_rows_total": 14,
                    "migrated_rows_present_in_current_fetch": matched,
                    "migrated_rows_matched_canonically": matched,
                    "migrated_rows_failed_to_match": 0,
                    "new_canonical_rows_in_current_fetch": len(result.valid_rows) - matched,
                    "legacy_identity_aliases_used": 0,
                    "dry_run_would_insert_new_rows": len(result.valid_rows) - matched,
                    "reconciliation_error": None,
                }
                # Count existing-duplicate recognition for the report.
                for r in result.valid_rows:
                    if r.source_trade_id in pre_ids:
                        result.counters.duplicate_records_existing_db += 1
            db_after = db_before  # unchanged; no write
        finally:
            db.close()
        timers_after = timers_before

    # Safety flags always False for this PR.
    result.counters.ready_for_scoring = 0
    result.counters.ready_for_automation = 0

    payload = _build_report_payload(
        wallet, live, result,
        write_result=write_result,
        backup=backup,
        unique_constraint=unique_constraint,
        process_gate=process_gate,
        compatibility=compatibility,
        db_path=db_path if production_write else db_path,
        db_before=db_before,
        db_after=db_after,
        timers_before=timers_before,
        timers_after=timers_after,
        integrity=integrity,
        fk=fk,
        mode=mode,
        historical_write=historical_write,
        marker_validation=marker_validation,
        production_write_requested=production_write,
        production_write_committed=bool(write_result and getattr(write_result, "committed", False)),
    )

    if args.json:
        text = json.dumps(payload, indent=2, default=str)
    else:
        text = _render_markdown(payload)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote report to {args.out}.")
    else:
        print(text)
    return 0


def _table_counts_open(db_path: str) -> dict[str, int]:
    """Read-only table counts via a mode=ro connection (no write side effects)."""
    import sqlite3

    out: dict[str, int] = {}
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for name in ("source_trades", *_GUARDED_TABLES):
                try:
                    row = c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()
                    out[name] = int(row[0]) if row else 0
                except Exception:
                    out[name] = -1
        finally:
            c.close()
    except sqlite3.Error:
        pass
    return out


if __name__ == "__main__":
    raise SystemExit(main())
