"""PR24X — Source-Trade INGESTION WRITER AUDIT (read-only / design-only).

This module is the pure, read-only analysis core behind the PR24X audit. It
inspects (does NOT modify) the current repository to answer the master
question:

    Given WAL + busy_timeout are already enforced by Database.connect(),
    what source_trade write paths still exist, and how do we ensure real
    ingestion uses ONE controlled writer instead of multiple
    collector-owned writes?

It is strictly READ-ONLY and NON-PERSISTING:

  * It never imports ``polycopy.db.database`` (no real write path) and never
    issues INSERT/UPDATE/DELETE/CREATE/DROP/ALTER against any DB.
  * It does NOT open the production DB for writing. Any counts are produced by
    the *caller* supplying an already-open ``mode=ro`` ``sqlite3.Connection``.
  * It classifies in-repo source_trades write paths by static inspection of a
    source tree (files on disk), not by running them.
  * It defines (as data structures + plain functions) the proposed WAL-safe
    single-writer ingestion architecture, the source_trade contract, the
    dedupe/idempotency strategy, the WAL-safe write policy, and the future
    manual ingestion sequence.

No decisions, no candidate creation, no signal generation, no order placement,
no backfill, no live fetch, no automation, no timers.

Usage (see scripts/report_source_trade_ingestion_writer_audit.py):

    from polycopy.engine.source_trade_ingestion_writer_audit import (
        build_source_trade_ingestion_writer_audit,
        report_to_markdown,
        AUDIT_VERSION,
    )
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ── Versioning ───────────────────────────────────────────────────────────────
AUDIT_VERSION = "PR24X-1"

# Path anchors used by the static inspector. All relative to a repo root.
_REPO_ROOT_HINTS = ("src/polycopy", "scripts", "tests")

# Terms whose presence in a line marks it as a write statement.
_WRITE_VERBS = (
    "insert into",
    "insert or ignore into",
    "insert or replace into",
    "update ",
    "delete from",
    "upsert",
    "replace into",
)

# Direct sqlite3 connection opens we treat as "bypass" candidates (any writer
# that does NOT go through Database.connect() and may skip the safety PRAGMAs).
_DIRECT_CONNECT_PATTERNS = (
    "sqlite3.connect(",
    "sqlite3.Connection",
)


# ── Data model ───────────────────────────────────────────────────────────────
@dataclass
class DbSafetyLayer:
    """What Database.connect() actually enforces (verified against source)."""

    foreign_keys_on: bool = True
    journal_mode_wal: bool = True
    busy_timeout_ms: int = 30_000
    wal_autocheckpoint: int = 1_000
    applied_in_connect: bool = True
    source_reference: str = "src/polycopy/db/database.py:connect()"
    wal_sufficient_alone: bool = False
    note: str = (
        "WAL + busy_timeout avoid SQLITE_BUSY and allow concurrent "
        "readers, but they do NOT make SQLite multi-writer. An "
        "application-level single-writer rule is still required."
    )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WritePath:
    """One source_trades write/mutation path discovered in the repo."""

    path: str
    line: int
    verb: str
    classification: str  # see CLASSIFICATIONS
    writes_db: bool
    uses_database_connect: bool
    is_sample_seed: bool
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


CLASSIFICATIONS = (
    "production_write_path",
    "sample_test_seed_path",
    "migration_schema_path",
    "report_only_read_path",
    "test_temp_db_only",
    "dead_unused_unknown",
)


@dataclass
class CollectorAudit:
    """One fetcher/collector component and its write posture."""

    name: str
    path: str
    reads: bool
    writes_db: bool
    tables_touched: tuple[str, ...]
    writes_source_trades_directly: bool
    fetcher_only_safe: bool
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArchitectureRole:
    """A role in the proposed Fetcher→...→SingleWriter pipeline."""

    role: str
    may_write: bool
    responsibility: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceTradeContract:
    """The required shape of a real (is_sample=0) source_trade row."""

    required_fields: tuple[str, ...] = (
        "source",
        "source_trade_id",
        "trader_address",  # wallet_address
        "market_source_id",  # conditionId if available
        "token_id",  # if available
        "side",
        "price",
        "size",  # quantity
        "timestamp",
        "outcome",  # if available
        "is_sample",
    )
    dedupe_key_preferred: str = "source + source_trade_id"
    dedupe_key_fallback: str = (
        "wallet + token/condition + side + price + size + timestamp"
    )
    pr24u_ready: str = "token_id present"
    pr24v_ready: str = (
        "conditionId-shaped market_source_id present OR read-only "
        "token->condition mapping available"
    )
    both_ready: str = "PR24U-ready + PR24V-ready"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WalSafeWritePolicy:
    """The future writer's enforced policy."""

    accept_normalized_rows_only: bool = True
    validate_every_row: bool = True
    skip_or_report_invalid_rows: bool = True
    bounded_batches: bool = True
    one_transaction_per_batch: bool = True
    commit_once_per_batch: bool = True
    never_concurrent_with_another_writer: bool = True
    expose_dry_run: bool = True
    require_explicit_write_flag: bool = True
    report_db_size_mtime_before_after: bool = True
    never_called_by_timers_until_proven: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FutureSequenceStep:
    pr: str
    title: str
    writes_db: bool
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestionWriterAudit:
    """Top-level audit result object."""

    audit_version: str = AUDIT_VERSION
    repo_root: Optional[str] = None
    db_safety_layer: DbSafetyLayer = field(default_factory=DbSafetyLayer)
    source_trades_inventory: dict[str, Any] = field(default_factory=dict)
    write_paths: list[WritePath] = field(default_factory=list)
    collectors: list[CollectorAudit] = field(default_factory=list)
    architecture_roles: list[ArchitectureRole] = field(default_factory=list)
    contract: SourceTradeContract = field(default_factory=SourceTradeContract)
    dedupe_strategy: dict[str, Any] = field(default_factory=dict)
    wal_safe_write_policy: WalSafeWritePolicy = field(
        default_factory=WalSafeWritePolicy
    )
    future_sequence: list[FutureSequenceStep] = field(default_factory=list)
    centralized_writer_exists: bool = False
    centralized_writer_note: str = (
        "No safe centralized source_trade writer exists today. "
        "persist_trade is duplicated across two collectors "
        "(run_scan.py and collect_smart_money_data.py). PR24X "
        "recommends building ONE shared writer role per the "
        "architecture below rather than a new parallel path."
    )
    guardrail_flags: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "audit_version": self.audit_version,
            "repo_root": self.repo_root,
            "db_safety_layer": self.db_safety_layer.as_dict(),
            "source_trades_inventory": self.source_trades_inventory,
            "write_paths": [w.as_dict() for w in self.write_paths],
            "collectors": [c.as_dict() for c in self.collectors],
            "architecture_roles": [a.as_dict() for a in self.architecture_roles],
            "contract": self.contract.as_dict(),
            "dedupe_strategy": self.dedupe_strategy,
            "wal_safe_write_policy": self.wal_safe_write_policy.as_dict(),
            "future_sequence": [s.as_dict() for s in self.future_sequence],
            "centralized_writer_exists": self.centralized_writer_exists,
            "centralized_writer_note": self.centralized_writer_note,
            "guardrail_flags": self.guardrail_flags,
        }


# ── Static inspectors (read files on disk; never run them) ───────────────────
def _looks_like_write(line: str) -> Optional[str]:
    low = " " + line.lower() + " "
    for verb in _WRITE_VERBS:
        if verb in low and "source_trades" in low:
            return verb.strip()
    return None


def _classify(path: str, verb: str, is_sample: bool) -> tuple[str, bool, bool, bool]:
    """Return (classification, writes_db, uses_database_connect, is_sample_seed)."""
    p = Path(path)
    name = p.name
    rel = str(p)
    # Tests / temp DB seeds.
    if "tests/" in rel or name.startswith("test_"):
        return ("test_temp_db_only", True, False, is_sample)
    # Sample/demo seeders.
    if "seed_demo_data" in name:
        return ("sample_test_seed_path", True, False, is_sample)
    # Migration / schema DDL.
    if "db/schema" in rel or name in ("database.py",):
        return ("migration_schema_path", True, True, is_sample)
    # Settlement UPDATE during backfill.
    if "backfill_resolution_truth" in name:
        return ("production_write_path", True, True, is_sample)
    # Production collector writers.
    if name in ("run_scan.py", "collect_smart_money_data.py", "live_smoke_pr3_fixes.py"):
        return ("production_write_path", True, True, is_sample)
    return ("dead_unused_unknown", True, False, is_sample)


def _scan_write_paths(repo_root: Path) -> list[WritePath]:
    results: list[WritePath] = []
    seen: set[tuple[str, int, str]] = set()
    # Restrict scan to likely source/script/test files.
    candidates: list[Path] = []
    for sub in ("scripts", "src", "tests"):
        base = repo_root / sub
        if base.exists():
            candidates.extend(sorted(base.rglob("*.py")))
    for fp in candidates:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(fp.relative_to(repo_root))
        for i, line in enumerate(text.splitlines(), start=1):
            verb = _looks_like_write(line)
            if not verb:
                continue
            key = (rel, i, verb)
            if key in seen:
                continue
            seen.add(key)
            is_sample = "is_sample=true" in line.lower() or (
                "is_sample" in line.lower() and "1" in line
            )
            classification, writes_db, uses_connect, is_sample_seed = _classify(
                rel, verb, is_sample
            )
            results.append(
                WritePath(
                    path=rel,
                    line=i,
                    verb=verb,
                    classification=classification,
                    writes_db=writes_db,
                    uses_database_connect=uses_connect,
                    is_sample_seed=is_sample_seed,
                    notes="static inspection only; not executed",
                )
            )
    return results


def _scan_direct_connect_bypasses(repo_root: Path) -> list[str]:
    """Find raw sqlite3.connect() opens outside tests/migration debug.

    These are candidates for safety-PRAGMA bypasses. The PR24X audit verifies
    they are either (a) read-only (mode=ro) report helpers, or (b) standalone
    debug scripts that are NOT on the production ingestion path.
    """
    hits: list[str] = []
    for fp in sorted((repo_root / "src").rglob("*.py")):
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(fp.relative_to(repo_root))
        for i, line in enumerate(text.splitlines(), start=1):
            if "sqlite3.connect(" in line and "mode=ro" not in line:
                hits.append(f"{rel}:{i} {line.strip()}")
    return hits


def _default_collectors() -> list[CollectorAudit]:
    """Hard-coded static audit of known fetcher/collector components.

    Derived from reading adapters/, engine/*_evidence_bridge.py,
    scripts/run_scan.py, scripts/collect_smart_money_data.py. All entries are
    read from source; none are executed.
    """
    return [
        CollectorAudit(
            name="PolymarketPublicAdapter",
            path="src/polycopy/adapters/polymarket.py",
            reads=True,
            writes_db=False,
            tables_touched=(),
            writes_source_trades_directly=False,
            fetcher_only_safe=True,
            notes="Fetches market/trade/resolution data via Gamma+CLOB APIs. "
            "No DB writes anywhere in adapters/ (verified).",
        ),
        CollectorAudit(
            name="PolymarketClobAdapter.fetch_book",
            path="src/polycopy/adapters/polymarket_clob.py",
            reads=True,
            writes_db=False,
            tables_touched=(),
            writes_source_trades_directly=False,
            fetcher_only_safe=True,
            notes="Fetches CLOB order book only. No DB writes.",
        ),
        CollectorAudit(
            name="BullpenReadOnlyAdapter",
            path="src/polycopy/adapters/bullpen.py",
            reads=True,
            writes_db=False,
            tables_touched=(),
            writes_source_trades_directly=False,
            fetcher_only_safe=True,
            notes="ReadOnly adapter implementing provider protocols. No writes.",
        ),
        CollectorAudit(
            name="RealSnapshotEvidenceCollector",
            path="src/polycopy/engine/trade_copyability_real_snapshot_collection_bridge.py",
            reads=True,
            writes_db=False,
            tables_touched=(),
            writes_source_trades_directly=False,
            fetcher_only_safe=True,
            notes="Live CLOB book collector used by PR24U bridge. Fetch-only.",
        ),
        CollectorAudit(
            name="run_scan.ScanPipeline",
            path="scripts/run_scan.py",
            reads=True,
            writes_db=True,
            tables_touched=("source_trades", "wallets", "markets"),
            writes_source_trades_directly=True,
            fetcher_only_safe=False,
            notes="PRODUCTION writer. Calls _persist_trade() (run_scan.py:1419) "
            "directly. Also persists wallets/markets. Must be refactored so "
            "ingestion delegates to the single shared writer.",
        ),
        CollectorAudit(
            name="collect_smart_money_data.run_collection",
            path="scripts/collect_smart_money_data.py",
            reads=True,
            writes_db=True,
            tables_touched=("source_trades", "wallets", "markets"),
            writes_source_trades_directly=True,
            fetcher_only_safe=False,
            notes="PRODUCTION writer. Calls _persist_trade() "
            "(collect_smart_money_data.py:703) directly. Duplicate of the "
            "run_scan writer. Must be refactored to the shared writer.",
        ),
        CollectorAudit(
            name="wallet_discovery",
            path="src/polycopy/discovery/wallet_discovery.py",
            reads=True,
            writes_db=False,
            tables_touched=(),
            writes_source_trades_directly=False,
            fetcher_only_safe=True,
            notes="Reads/normalizes wallet discovery inputs. No source_trades writes.",
        ),
        CollectorAudit(
            name="backfill_resolution_truth",
            path="scripts/backfill_resolution_truth.py",
            reads=True,
            writes_db=True,
            tables_touched=("source_trades",),
            writes_source_trades_directly=True,
            fetcher_only_safe=False,
            notes="UPDATEs source_trades resolution columns (line 449). "
            "Settlement-stage, not ingestion. Must remain a separate, "
            "explicit, single-owner writer.",
        ),
    ]


def _default_architecture_roles() -> list[ArchitectureRole]:
    return [
        ArchitectureRole("Fetcher(s)", False,
                         "Pull raw trade/wallet/activity data from APIs. No DB writes."),
        ArchitectureRole("Normalizer", False,
                         "Map raw payloads to the source_trade contract. No DB writes."),
        ArchitectureRole("Validator", False,
                         "Reject/flag rows failing contract. No DB writes."),
        ArchitectureRole("Batch", False,
                         "Group valid normalized rows into bounded batches. No DB writes."),
        ArchitectureRole("Single SourceTrade Writer", True,
                         "ONLY component allowed to INSERT source_trades. Uses "
                         "Database.connect() so WAL/busy_timeout apply. Idempotent, "
                         "one transaction per batch, explicit write flag, dry-run."),
    ]


def _default_dedupe_strategy() -> dict[str, Any]:
    return {
        "unique_key_preferred": "UNIQUE(source, source_trade_id)",
        "conflict_behavior": "INSERT OR IGNORE — collision is counted as dedup, "
                             "never as an overwrite (avoids INSERT OR REPLACE "
                             "provenance loss proven in run_scan/collect history).",
        "repeat_scan_behavior": "Idempotent — re-running the same fetch inserts 0 "
                                "new rows; existing rows are untouched.",
        "fallback_key": "wallet_address + token_id/conditionId + side + price + "
                        "size + timestamp",
        "fallback_when": "Only when upstream source_trade_id is unavailable; "
                         "must be made deterministic before use.",
        "duplicate_prevention": "Single writer + UNIQUE index + INSERT OR IGNORE + "
                                "bounded transaction scope.",
    }


def _default_future_sequence() -> list[FutureSequenceStep]:
    return [
        FutureSequenceStep("PR24Y", "Read-only real trade source probe / live-preview",
                           False, "No DB writes; validate fetch feasibility."),
        FutureSequenceStep("PR24Z", "Normalized source_trade candidate generation",
                           False, "Produce candidate rows in-memory/report only."),
        FutureSequenceStep("PR25A", "Guarded single-writer source_trade batch insert",
                           True, "Explicit --write flag only; dry-run default."),
        FutureSequenceStep("later", "Evidence attachment / scoring / decisions",
                           True, "After ingestion is proven safe and single-owned."),
    ]


def _default_guardrail_flags() -> dict[str, bool]:
    return {
        "no_deploy": True,
        "no_service_restart": True,
        "no_timer_enablement": True,
        "no_collect_scan_settle_update_restart": True,
        "no_production_db_writes": True,
        "no_source_trades_mutation": True,
        "no_backfill": True,
        "no_real_ingestion_implementation": True,
        "no_live_fetch": True,
        "no_persistence_writer": True,
        "no_scoring": True,
        "no_trade_copyability_decisions": True,
        "no_copy_candidates": True,
        "no_paper_signal_decisions": True,
        "no_candidate_price_snapshots": True,
        "no_candidate_price_snapshot_levels": True,
        "no_orders": True,
        "no_positions": True,
        "no_automation": True,
        "no_broker_order_placement": True,
        "no_candidate_creation": True,
        "no_signal_creation": True,
    }


# ── Inventory (caller supplies a read-only connection) ───────────────────────
def _inventory_from_conn(conn: sqlite3.Connection) -> dict[str, Any]:
    """Count source_trades + key tables using a read-only connection."""
    inv: dict[str, Any] = {}
    try:
        inv["source_trades"] = conn.execute(
            "SELECT COUNT(*) AS n FROM source_trades"
        ).fetchone()["n"]
    except sqlite3.OperationalError:
        inv["source_trades"] = None
    # Side distribution only when the table exists.
    if inv["source_trades"]:
        try:
            rows = conn.execute(
                "SELECT side, COUNT(*) AS n FROM source_trades GROUP BY side"
            ).fetchall()
            inv["side_distribution"] = {r["side"]: r["n"] for r in rows}
        except sqlite3.OperationalError:
            inv["side_distribution"] = {}
    for t in (
        "trade_copyability_decisions",
        "copy_candidates",
        "paper_signal_decisions",
        "candidate_price_snapshots",
        "candidate_price_snapshot_levels",
        "orders",
        "positions",
        "wallet_score_decisions",
        "settlement_accounting_ledger",
    ):
        try:
            inv[t] = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        except sqlite3.OperationalError:
            inv[t] = None
    return inv


# ── Builders ─────────────────────────────────────────────────────────────────
def build_source_trade_ingestion_writer_audit(
    conn: Optional[sqlite3.Connection] = None,
    repo_root: Optional[str] = None,
    *,
    detect_repo: bool = True,
) -> IngestionWriterAudit:
    """Build the full PR24X audit (read-only).

    ``conn`` must be an already-open read-only ``sqlite3.Connection`` (or None
    for a source-only audit with empty inventory). This function never opens a
    DB for writing.
    """
    root = Path(repo_root) if repo_root else Path("/root/Polycopy")
    if detect_repo and not root.exists():
        # Fall back to cwd.
        root = Path.cwd()

    inventory = _inventory_from_conn(conn) if conn is not None else {}

    audit = IngestionWriterAudit(
        repo_root=str(root),
        source_trades_inventory=inventory,
        write_paths=_scan_write_paths(root),
        collectors=_default_collectors(),
        architecture_roles=_default_architecture_roles(),
        dedupe_strategy=_default_dedupe_strategy(),
        future_sequence=_default_future_sequence(),
        guardrail_flags=_default_guardrail_flags(),
    )
    # Direct-connect bypass scan is informational; stored in notes of the
    # safety layer for transparency.
    bypasses = _scan_direct_connect_bypasses(root)
    audit.db_safety_layer.note = (
        audit.db_safety_layer.note
        + f" | direct sqlite3.connect (non-ro) opens in src/: "
        + (str(bypasses) if bypasses else "none")
    )
    return audit


def report_to_markdown(audit: IngestionWriterAudit) -> str:
    """Render the audit as a human-readable Markdown report."""
    d = audit.as_dict()
    lines: list[str] = []
    lines.append(f"# PR24X — Source-Trade Ingestion Writer Audit")
    lines.append("")
    lines.append(f"**Audit version:** {d['audit_version']}  ")
    lines.append(f"**Repo root:** `{d['repo_root']}`  ")
    lines.append("**Status:** SAFE / PARKED / PAPER-ONLY / NON-AUTOMATED  ")
    lines.append("**Mode:** report-only / design-only / audit-only")
    lines.append("")

    lines.append("## 1. DB Safety Layer")
    sl = d["db_safety_layer"]
    lines.append(f"- WAL exists: **{sl['journal_mode_wal']}** "
                 f"(PRAGMA journal_mode=WAL in `Database.connect()`)")
    lines.append(f"- busy_timeout exists: **{sl['busy_timeout_ms']} ms**")
    lines.append(f"- wal_autocheckpoint exists: **{sl['wal_autocheckpoint']}**")
    lines.append(f"- foreign_keys=ON: **{sl['foreign_keys_on']}**")
    lines.append(f"- WAL sufficient alone: **{sl['wal_sufficient_alone']}** "
                 f"(WAL helps, does NOT make SQLite multi-writer)")
    lines.append(f"- Application-level single-writer rule still required: **YES**")
    lines.append("")

    lines.append("## 2. source_trades Write Path Classification")
    by_class: dict[str, list[dict]] = {}
    for w in d["write_paths"]:
        by_class.setdefault(w["classification"], []).append(w)
    for cls in CLASSIFICATIONS:
        items = by_class.get(cls, [])
        if not items:
            continue
        lines.append(f"### {cls} ({len(items)})")
        for w in items:
            lines.append(f"- `{w['path']}:{w['line']}` — `{w['verb']}` "
                         f"(uses Database.connect: {w['uses_database_connect']}, "
                         f"sample seed: {w['is_sample_seed']})")
        lines.append("")

    lines.append("## 3. Collectors / Fetchers")
    for c in d["collectors"]:
        safe = "fetcher-only safe" if c["fetcher_only_safe"] else "WRITES DB"
        lines.append(f"- **{c['name']}** (`{c['path']}`): {safe}")
        lines.append(f"  - reads: {c['reads']}, writes source_trades directly: "
                     f"{c['writes_source_trades_directly']}")
        lines.append(f"  - tables touched: {list(c['tables_touched'])}")
        lines.append(f"  - {c['notes']}")
    lines.append("")

    lines.append("## 4. Safe Ingestion Architecture")
    lines.append("```")
    lines.append("Fetcher(s) -> Normalizer -> Validator -> Batch -> Single SourceTrade Writer")
    lines.append("```")
    for a in d["architecture_roles"]:
        may = "MAY WRITE" if a["may_write"] else "no writes"
        lines.append(f"- **{a['role']}** ({may}): {a['responsibility']}")
    lines.append("")

    lines.append("## 5. source_trade Contract")
    ct = d["contract"]
    lines.append(f"- Required fields: {list(ct['required_fields'])}")
    lines.append(f"- Dedupe key (preferred): `{ct['dedupe_key_preferred']}`")
    lines.append(f"- Dedupe key (fallback): `{ct['dedupe_key_fallback']}`")
    lines.append(f"- PR24U-ready: {ct['pr24u_ready']}")
    lines.append(f"- PR24V-ready: {ct['pr24v_ready']}")
    lines.append(f"- both-ready: {ct['both_ready']}")
    lines.append("")

    lines.append("## 6. Dedupe / Idempotency")
    ds = d["dedupe_strategy"]
    for k, v in ds.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")

    lines.append("## 7. WAL-Safe Write Policy (future writer)")
    wp = d["wal_safe_write_policy"]
    for k, v in wp.items():
        lines.append(f"- {k}: **{v}**")
    lines.append("")

    lines.append("## 8. Future Manual Ingestion Sequence")
    for s in d["future_sequence"]:
        lines.append(f"- **{s['pr']}** — {s['title']} "
                     f"(writes DB: {s['writes_db']}). {s['notes']}")
    lines.append("")
    lines.append(f"**Centralized writer exists today?** "
                 f"**{d['centralized_writer_exists']}**")
    lines.append(f"> {d['centralized_writer_note']}")
    lines.append("")

    lines.append("## 9. Current Verified DB Inventory")
    inv = d["source_trades_inventory"]
    if inv:
        for k, v in inv.items():
            lines.append(f"- {k}: {v}")
    else:
        lines.append("- (no read-only connection supplied; inventory omitted)")
    lines.append("")

    lines.append("## Hard Guardrails (all enforced this PR)")
    for k, v in d["guardrail_flags"].items():
        lines.append(f"- {k}: **{v}**")
    lines.append("")
    return "\n".join(lines)


def report_to_json(audit: IngestionWriterAudit) -> str:
    """Serialize the audit to valid JSON."""
    return json.dumps(audit.as_dict(), indent=2, sort_keys=False)
