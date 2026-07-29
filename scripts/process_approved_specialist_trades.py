#!/usr/bin/env python3
"""Bounded orchestration: approve one exact approval end-to-end to a signal.

For one EXACT approval, run the canonical Pass 2 operational chain:
  collect at most one new trade (approval-driven) -> return source-trade UUID
  -> enrich exact source trade -> dispatch exact source trade
  -> produce candidate/signal result.

This command preserves ownership boundaries: it calls the separate collector,
enrichment, and dispatcher modules; it does NOT execute orders or positions.

Safety envelope (carried from PR68 + Pass 2):
  * Dry-run is the DEFAULT. No --write => no writes (even with --allow-live).
  * A production DB write requires --write --confirm-production-db plus the
    approval-driven discovery gates (--approval-id, --max-new-trades 1,
    --allow-live for live network).
  * The complete production-write lifecycle, including database open/close and
    rollback/cleanup, is protected by the shared operational job lock.
  * Bounded: --max-new-trades (default 1, max bounded) + exact --approval-id.
  * Output includes the full artifact chain (approval/source-trade/enrichment/
    dispatch/candidate/paper-signal).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import nullcontext
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from polycopy.config.settings import Settings  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.engine.approved_specialist_dispatcher import dispatch_one  # noqa: E402
from polycopy.execution.specialist_approval import get_approval  # noqa: E402
from polycopy.ingestion.approved_wallet_collector import collect  # noqa: E402
from polycopy.ingestion.source_trade_enrichment import enrich_source_trade_async  # noqa: E402
from polycopy.ingestion.source_trade_writer import write_valid_rows  # noqa: E402
from polycopy.runtime.locks import (  # noqa: E402
    DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S,
    LockError,
    operational_job_lock,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() == PRODUCTION_DB_PATH
    except OSError:
        return False


def _rollback_quietly(db) -> None:
    """Rollback while the caller still owns the outer operational lock."""
    conn = getattr(db, "conn", None)
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        try:
            rollback()
        except Exception:
            pass


def _close_resources(*, db, adapter, runner) -> None:
    """Close network/async/database resources before the outer lock releases."""
    if adapter is not None:
        aclose = getattr(adapter, "aclose", None)
        if callable(aclose) and runner is not None:
            try:
                runner.run(aclose())
            except Exception:
                pass
        else:
            close = getattr(adapter, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
    if runner is not None:
        runner.close()
    if db is not None:
        db.close()


def _run(args) -> dict[str, object] | None:
    adapter = None
    runner = None
    db = None
    failed = False
    try:
        # Database opening is deliberately inside the outer operational lock.
        db = Database(Path(args.db_path)).connect()

        try:
            approval = get_approval(db, args.approval_id)
        except KeyError:
            print("error: unknown approval_id", file=sys.stderr)
            return None
        if not approval.enabled or approval.revoked_at is not None:
            print(
                f"error: approval is {'disabled' if not approval.enabled else 'revoked'}",
                file=sys.stderr,
            )
            return None
        wallet = approval.wallet_address

        if not args.allow_live:
            print("error: --allow-live required for network collection", file=sys.stderr)
            return None

        settings = Settings()
        adapter = PolymarketPublicAdapter(
            gamma_base_url=settings.gamma_base_url,
            clob_base_url=settings.clob_base_url,
            data_api_base_url=settings.data_api_base_url,
            timeout=10.0,
        )
        runner = asyncio.Runner()
        gamma_cache: dict[str, object] = {}

        async def gamma_async(condition_id: str):
            if condition_id not in gamma_cache:
                gamma_cache[condition_id] = await adapter.get_market_raw(condition_id)
            return gamma_cache[condition_id]

        def gamma_sync(condition_id: str):
            if condition_id not in gamma_cache:
                raise RuntimeError(
                    "synchronous dispatch requested uncached Gamma market "
                    f"{condition_id!r}; pre-resolve it before dispatch"
                )
            return gamma_cache[condition_id]

        result = runner.run(collect(adapter, wallet, gamma_resolver=gamma_async))
        accepted = result.accepted_rows[: args.max_new_trades]
        source_trade_internal_id = None
        inserted_trades = 0

        if args.write and accepted:
            from polycopy.ingestion.normalized_source_trade import normalize_source_trade

            pre = {
                (str(row[0]), str(row[1]))
                for row in db.conn.execute(
                    "SELECT source, source_trade_id FROM source_trades WHERE source=?",
                    ("polymarket_data_api_trades_user",),
                )
            }
            norms = [
                normalize_source_trade(
                    trade,
                    requested_wallet=wallet,
                    allow_sell=False,
                    gamma_market=gamma_sync(trade.market_source_id),
                )
                for trade in accepted
            ]
            fresh_norms = [
                norm
                for norm in norms
                if (str(norm.source), str(norm.source_trade_id)) not in pre
            ]
            persisted = write_valid_rows(
                db,
                fresh_norms,
                dry_run=False,
                pre_existing_ids={
                    norm.source_trade_id
                    for norm in fresh_norms
                    if norm.source_trade_id is not None
                },
            )
            inserted_trades = persisted.inserted
            first = norms[0]
            row = db.fetchone(
                "SELECT id FROM source_trades WHERE source=? AND source_trade_id=?",
                (first.source, first.source_trade_id),
            )
            if row:
                source_trade_internal_id = row["id"]

        if source_trade_internal_id is None and args.write and not inserted_trades:
            return {
                "approval_id": args.approval_id,
                "source_trade_internal_id": None,
                "inserted_trades": 0,
                "enrichment_id": None,
                "dispatch_id": None,
                "candidate_id": None,
                "paper_signal_decision_id": None,
                "paper_signal_verdict": None,
                "mode": "write",
            }

        if source_trade_internal_id is not None:
            enrichment = runner.run(
                enrich_source_trade_async(
                    db,
                    source_trade_internal_id,
                    gamma_resolver=gamma_async,
                    dry_run=not args.write,
                )
            )
            dispatch = dispatch_one(
                db,
                approval_id=args.approval_id,
                source_trade_internal_id=source_trade_internal_id,
                gamma_resolver=gamma_sync,
                clob_provider=adapter,
                dry_run=not args.write,
            )
        else:
            enrichment = None
            dispatch = None

        return {
            "approval_id": args.approval_id,
            "source_trade_internal_id": source_trade_internal_id,
            "inserted_trades": inserted_trades,
            "enrichment_id": enrichment.enrichment_id if enrichment else None,
            "enrichment_status": enrichment.status if enrichment else None,
            "dispatch_id": dispatch.dispatch_id if dispatch else None,
            "dispatch_status": dispatch.status if dispatch else None,
            "candidate_id": dispatch.candidate_id if dispatch else None,
            "paper_signal_decision_id": (
                dispatch.paper_signal_decision_id if dispatch else None
            ),
            "paper_signal_verdict": dispatch.paper_signal_verdict if dispatch else None,
            "mode": "write" if args.write else "dry-run",
        }
    except BaseException:
        failed = True
        if db is not None:
            _rollback_quietly(db)
        raise
    finally:
        # Rollback and every cleanup/close operation occur before the enclosing
        # operational_job_lock context exits. Nested writers never own this lock.
        if failed and db is not None:
            _rollback_quietly(db)
        _close_resources(db=db, adapter=adapter, runner=runner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Orchestrate approved-specialist trade -> paper signal (no execution)"
    )
    parser.add_argument("--approval-id", required=True, help="Exact approval_id (UUID)")
    parser.add_argument(
        "--max-new-trades",
        type=int,
        default=1,
        help="Bounded new-trade cap for collection (default 1)",
    )
    parser.add_argument("--write", action="store_true", help="Persist all stages")
    parser.add_argument(
        "--allow-live", action="store_true", help="Authorize live Gamma/CLOB/collection network"
    )
    parser.add_argument(
        "--confirm-production-db", action="store_true", help="Confirm target is the production DB"
    )
    parser.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S,
        help="Seconds to wait for the shared operational lock",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.max_new_trades != 1:
        print(
            "error: --max-new-trades must be exactly 1 for approval-driven discovery",
            file=sys.stderr,
        )
        return 2
    if args.lock_timeout < 0:
        print("error: --lock-timeout must be non-negative", file=sys.stderr)
        return 2

    is_prod = _is_production_db(args.db_path)
    if args.write and is_prod:
        missing = []
        if not args.allow_live:
            missing.append("--allow-live")
        if not args.confirm_production_db:
            missing.append("--confirm-production-db")
        if missing:
            print("error: production write requires: " + ", ".join(missing), file=sys.stderr)
            return 2

    lock_context = (
        operational_job_lock("approved-specialist", timeout=args.lock_timeout)
        if args.write and is_prod
        else nullcontext()
    )
    try:
        with lock_context:
            output = _run(args)
    except LockError as exc:
        print(f"error: operational lock unavailable: {exc}", file=sys.stderr)
        return 1

    if output is None:
        return 2
    if args.json:
        print(json.dumps(output, sort_keys=True))
    elif output["source_trade_internal_id"] is None and args.write:
        print("no new trade collected for approval")
    else:
        for key, value in output.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
