#!/usr/bin/env python3
"""Bounded, persisted-evidence-only PR67 scoring pipeline CLI.

This command evaluates existing candidates exclusively through the canonical
``evaluate_paper_signals_for_candidate`` evaluator. It never creates candidates
or snapshots and contains no scoring formula implementation.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Iterator

# Allow direct ``python scripts/...`` execution without depending on an
# editable install. This affects import discovery only; it performs no I/O.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from polycopy.runtime.locks import LockError, operational_job_lock  # noqa: E402
from polycopy.scoring.evaluation_policy import (  # noqa: E402
    DECISION_ONLY_EVALUATION_POLICY,
    EvaluationExecutionPolicy,
)
from polycopy.scoring.paper_signal import evaluate_paper_signals_for_candidate  # noqa: E402

PRODUCTION_DB = (REPO_ROOT / "data" / "polycopy.db").resolve()
DEFAULT_LIMIT = 6
MAX_LIMIT = 50
EXPECTED_PRODUCTION_SCHEMA_VERSION = 17
ALLOWED_WRITE_TABLES = {
    "wallet_score_decisions",
    "category_wallet_score_decisions",
    "trade_copyability_decisions",
    "paper_signal_decisions",
}


class SafetyError(ValueError):
    """Argument or database-identity rejection (CLI exit 2)."""


class SqliteFacade:
    """Minimal existing-connection facade; deliberately has no migrations."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)


def resolve_db_path(value: str) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SafetyError(f"database path is unsafe or unresolvable: {value}") from exc


def is_production_db(path: Path) -> bool:
    return path == PRODUCTION_DB


def dry_run_policy() -> EvaluationExecutionPolicy:
    return EvaluationExecutionPolicy(
        persist_wallet_score=False,
        persist_category_score=False,
        persist_trade_copyability=False,
        persist_paper_signal=False,
        persist_shadow=False,
        persist_exit_experiments=False,
        allow_candidate_creation=False,
        allow_snapshot_creation=False,
        allow_approval=False,
    )


def open_connection(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        # Exact read-only URI: it sees active WAL content while SQLite rejects
        # writes. ``immutable=1`` would risk hiding committed WAL state.
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def read_schema_version(conn: sqlite3.Connection) -> int:
    """Read the canonical application schema version from ``_meta``."""
    try:
        row = conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        raise SafetyError(f"cannot read _meta.schema_version: {exc}") from exc
    if row is None:
        raise SafetyError("missing _meta.schema_version")
    raw = row[0]
    if not isinstance(raw, str) or not raw.isdigit() or int(raw) <= 0:
        raise SafetyError(f"invalid _meta.schema_version: {raw!r}")
    return int(raw)


def validate_args(args: argparse.Namespace, path: Path) -> None:
    if args.limit < 1 or args.limit > MAX_LIMIT:
        raise SafetyError(f"--limit must be between 1 and {MAX_LIMIT}")
    if args.offset < 0:
        raise SafetyError("--offset must be >= 0")
    if args.candidate_id is not None and args.offset:
        raise SafetyError("--candidate-id cannot be combined with --offset")
    if args.confirm_production_db and not args.apply:
        raise SafetyError("--confirm-production-db requires --apply")
    if args.apply and is_production_db(path) and not args.confirm_production_db:
        raise SafetyError("production apply requires --confirm-production-db")


def _wallet_rows(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    normalized = query.strip().lower()
    if not normalized:
        raise SafetyError("--wallet must be non-empty")
    exact = conn.execute(
        "SELECT id, address, canonical_address FROM wallets "
        "WHERE lower(COALESCE(canonical_address, address))=? ORDER BY id",
        (normalized,),
    ).fetchall()
    if exact:
        return exact
    return conn.execute(
        "SELECT id, address, canonical_address FROM wallets "
        "WHERE lower(COALESCE(canonical_address, address)) LIKE ? ORDER BY id",
        (normalized + "%",),
    ).fetchall()


def select_candidate_ids(conn: sqlite3.Connection, args: argparse.Namespace) -> list[int]:
    if args.candidate_id is not None:
        row = conn.execute("SELECT id FROM copy_candidates WHERE id=?", (args.candidate_id,)).fetchone()
        if row is None:
            raise SafetyError(f"candidate not found: {args.candidate_id}")
        return [int(row["id"])]
    where = ""
    params: list[Any] = []
    if args.wallet:
        wallets = _wallet_rows(conn, args.wallet)
        if not wallets:
            raise SafetyError(f"wallet not found: {args.wallet}")
        if len(wallets) != 1:
            raise SafetyError(f"wallet prefix is ambiguous: {args.wallet}")
        where = "WHERE wallet_id=?"
        params.append(str(wallets[0]["id"]))
    rows = conn.execute(
        "SELECT id FROM copy_candidates " + where + " ORDER BY COALESCE(created_at, ''), id LIMIT ? OFFSET ?",
        tuple(params + [args.limit, args.offset]),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _candidate_identity(conn: sqlite3.Connection, candidate_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT c.id, c.source_trade_id, c.wallet_id, s.id AS snapshot_id, s.fetched_at AS snapshot_timestamp "
        "FROM copy_candidates c LEFT JOIN candidate_price_snapshots s "
        "ON s.id=(SELECT id FROM candidate_price_snapshots "
        "WHERE candidate_id=c.id ORDER BY fetched_at DESC, id DESC LIMIT 1) WHERE c.id=?",
        (candidate_id,),
    ).fetchone()
    if row is None:
        return {"candidate_id": candidate_id}
    wallet = str(row["wallet_id"] or "")
    return {"candidate_id": candidate_id, "source_trade_id": row["source_trade_id"], "wallet_id": wallet, "wallet_prefix": wallet[:10], "snapshot_id": row["snapshot_id"], "snapshot_timestamp": row["snapshot_timestamp"]}


def _details(summary: dict[str, Any], identity: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    detail = dict(identity)
    detail.update(summary.get("decision_details") or {})
    detail["paper_verdict"] = summary.get("verdict")
    detail["paper_reason"] = summary.get("reason")
    detail["paper_decision_id"] = summary.get("paper_signal_id")
    detail["paper_would_create"] = bool(summary.get("paper_signal_would_create", False))
    detail["paper_persisted"] = summary.get("paper_signal_id") is not None
    detail["is_approved"] = int(summary.get("is_approved", 0))
    detail["errors"] = [summary["reason"]] if summary.get("outcome_kind") == "failed" else []
    detail["dry_run"] = dry_run
    detail["committed"] = not dry_run and summary.get("outcome_kind") != "failed"
    return detail


def aggregate(details: list[dict[str, Any]], *, dry_run: bool, production: bool, duration: float) -> dict[str, Any]:
    successful = [item for item in details if not item["errors"]]
    return {
        "mode": "dry_run" if dry_run else "apply",
        "production_db": production,
        "examined": len(details), "succeeded": len(successful), "failed": len(details) - len(successful),
        "wallet_complete": sum(item.get("wallet_score_status") == "complete" for item in details),
        "wallet_incomplete": sum(item.get("wallet_score_status") == "incomplete" for item in details),
        "taxonomy": dict(Counter(item.get("taxonomy_status") or "unavailable" for item in details)),
        "category": dict(Counter(item.get("category_score_status") or "not_applicable" for item in details)),
        "tc_verdicts": dict(Counter(item.get("tc_verdict") or "unknown" for item in details)),
        "paper_verdicts": dict(Counter(item.get("paper_verdict") or "unknown" for item in details)),
        "decisions_reused": sum(sum(bool(item.get(key)) for key in ("wallet_reused", "category_reused")) for item in details),
        "decisions_would_create": sum(sum(bool(item.get(key)) for key in ("wallet_would_create", "category_would_create", "tc_would_create", "paper_would_create")) for item in details),
        "decisions_inserted": sum(sum(bool(item.get(key)) for key in ("wallet_created", "category_created")) for item in details),
        "errors": [error for item in details for error in item["errors"]],
        "duration_seconds": round(duration, 6), "dry_run": dry_run, "committed": not dry_run,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    path = resolve_db_path(args.db_path)
    validate_args(args, path)
    production = is_production_db(path)
    dry_run = not args.apply
    conn = open_connection(path, readonly=dry_run)
    facade = SqliteFacade(conn)
    started = time.monotonic()
    try:
        schema_version = read_schema_version(conn)
        if production and schema_version != EXPECTED_PRODUCTION_SCHEMA_VERSION:
            raise SafetyError(
                "unexpected production _meta.schema_version: "
                f"{schema_version}; expected {EXPECTED_PRODUCTION_SCHEMA_VERSION}"
            )
        candidate_ids = select_candidate_ids(conn, args)
        policy = dry_run_policy() if dry_run else DECISION_ONLY_EVALUATION_POLICY
        lock: Iterator[Any] = nullcontext()
        if args.apply and production:
            lock = operational_job_lock("pr67-scoring-pipeline", timeout=0.0)
        details: list[dict[str, Any]] = []
        try:
            with lock:
                for candidate_id in candidate_ids:
                    identity = _candidate_identity(conn, candidate_id)
                    summary = evaluate_paper_signals_for_candidate(facade, candidate_id, policy=policy)
                    detail = _details(summary, identity, dry_run=dry_run)
                    details.append(detail)
                    if detail["errors"] and args.fail_fast:
                        break
        except LockError as exc:
            raise SafetyError(f"operational lock unavailable: {exc}") from exc
        result = {
            "aggregate": aggregate(
                details,
                dry_run=dry_run,
                production=production,
                duration=time.monotonic() - started,
            )
        }
        result["aggregate"]["schema_version"] = schema_version
        if args.include_details or args.json:
            result["candidates"] = details
        return result
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="SQLite database path")
    parser.add_argument("--candidate-id", type=int)
    parser.add_argument("--wallet")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-production-db", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-details", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except SafetyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"error: sqlite failure: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True, allow_nan=False, separators=(",", ":"), default=str))
    else:
        aggregate_result = result["aggregate"]
        print("mode={mode} examined={examined} succeeded={succeeded} failed={failed} dry_run={dry_run}".format(**aggregate_result))
        if args.include_details:
            for item in result.get("candidates", []):
                print(f"candidate={item['candidate_id']} paper={item['paper_verdict']} reason={item['paper_reason']}")
    return 1 if result["aggregate"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
