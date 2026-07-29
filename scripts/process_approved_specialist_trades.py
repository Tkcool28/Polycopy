#!/usr/bin/env python3
"""Bounded orchestration: approve one exact approval end-to-end to a signal.

For one exact approval, run the canonical operational chain: collect at most one
trade, persist its canonical source row, enrich it, and dispatch it to the
paper-only candidate/signal bridge. This command never executes orders or
positions.

Dry-run is the default. A production write requires --write --allow-live and
--confirm-production-db. The whole production-write lifecycle, from writable DB
open through cleanup and close, is held by the shared operational job lock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from polycopy.config.settings import Settings  # noqa: E402
from polycopy.db.database import Database  # noqa: E402
from polycopy.engine.approved_specialist_dispatcher import dispatch_one  # noqa: E402
from polycopy.engine.approved_wallet_trade_bridge import _issue_write_capability  # noqa: E402
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


def _cleanup(adapter: Any, runner: Any, db: Any, *, pipeline_error: BaseException | None) -> None:
    """Attempt every cleanup step, preserving a pre-existing pipeline failure.

    If the pipeline succeeded, surface the first cleanup error after all resources
    have been attempted. If it failed, cleanup errors are deliberately secondary.
    """
    cleanup_error: BaseException | None = None

    def attempt(callback: Any) -> None:
        nonlocal cleanup_error
        try:
            callback()
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc

    if adapter is not None:
        aclose = getattr(adapter, "aclose", None)
        if callable(aclose) and runner is not None:
            attempt(lambda: runner.run(aclose()))
        elif callable(getattr(adapter, "close", None)):
            attempt(adapter.close)
    if runner is not None:
        attempt(runner.close)
    if db is not None:
        attempt(db.close)

    if pipeline_error is None and cleanup_error is not None:
        raise cleanup_error


def _run_pipeline(
    args: argparse.Namespace,
    *,
    write_authorization: object | None,
) -> dict[str, Any]:
    """Run one invocation after any required outer operational lock is acquired.

    The processor owns DB open, outer rollback, final commit, adapter cleanup,
    runner cleanup, and DB close. The canonical writer keeps its established
    per-stage commit semantics; every such commit remains inside this outer
    lifecycle. The opaque bridge capability is issued by this caller and passed
    through the dispatcher rather than reacquiring a lock below.
    """
    adapter = None
    runner = None
    db = None
    try:
        db = Database(Path(args.db_path)).connect()
        try:
            approval = get_approval(db, args.approval_id)
        except KeyError:
            print("error: unknown approval_id", file=sys.stderr)
            return {"_exit_code": 2}
        if not approval.enabled or approval.revoked_at is not None:
            print(
                f"error: approval is {'disabled' if not approval.enabled else 'revoked'}",
                file=sys.stderr,
            )
            return {"_exit_code": 2}
        if not args.allow_live:
            print("error: --allow-live required for network collection", file=sys.stderr)
            return {"_exit_code": 2}

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

        # ── Stage 1: collect at most one new trade (approval-driven) ──
        result = runner.run(collect(adapter, approval.wallet_address, gamma_resolver=gamma_async))
        accepted = result.accepted_rows[:args.max_new_trades]
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
                    requested_wallet=approval.wallet_address,
                    allow_sell=False,
                    gamma_market=gamma_sync(trade.market_source_id),
                )
                for trade in accepted
            ]
            fresh_norms = [
                norm for norm in norms if (str(norm.source), str(norm.source_trade_id)) not in pre
            ]
            out = write_valid_rows(
                db,
                fresh_norms,
                dry_run=False,
                pre_existing_ids={
                    norm.source_trade_id
                    for norm in fresh_norms
                    if norm.source_trade_id is not None
                },
            )
            inserted_trades = out.inserted
            first = norms[0]
            row = db.fetchone(
                "SELECT id FROM source_trades WHERE source=? AND source_trade_id=?",
                (first.source, first.source_trade_id),
            )
            if row:
                source_trade_internal_id = row["id"]

        if source_trade_internal_id is None and args.write and not inserted_trades:
            db.conn.commit()
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
            disp = dispatch_one(
                db,
                approval_id=args.approval_id,
                source_trade_internal_id=source_trade_internal_id,
                gamma_resolver=gamma_sync,
                clob_provider=adapter,
                dry_run=not args.write,
                write_authorization=write_authorization,
            )
        else:
            enrichment = None
            disp = None

        if args.write:
            db.conn.commit()
        return {
            "approval_id": args.approval_id,
            "source_trade_internal_id": source_trade_internal_id,
            "inserted_trades": inserted_trades,
            "enrichment_id": enrichment.enrichment_id if enrichment else None,
            "enrichment_status": enrichment.status if enrichment else None,
            "dispatch_id": disp.dispatch_id if disp else None,
            "dispatch_status": disp.status if disp else None,
            "candidate_id": disp.candidate_id if disp else None,
            "paper_signal_decision_id": disp.paper_signal_decision_id if disp else None,
            "paper_signal_verdict": disp.paper_signal_verdict if disp else None,
            "mode": "write" if args.write else "dry-run",
        }
    except BaseException:
        if args.write and db is not None:
            try:
                db.conn.rollback()
            except Exception:
                pass
        raise
    finally:
        _cleanup(adapter, runner, db, pipeline_error=sys.exc_info()[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Orchestrate approved-specialist trade -> paper signal (no execution)"
    )
    parser.add_argument("--approval-id", required=True, help="Exact approval_id (UUID)")
    parser.add_argument("--max-new-trades", type=int, default=1)
    parser.add_argument("--write", action="store_true", help="Persist all stages")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--confirm-production-db", action="store_true")
    parser.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    parser.add_argument("--lock-timeout", type=float, default=DEFAULT_OPERATIONAL_LOCK_TIMEOUT_S)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.max_new_trades != 1:
        print(
            "error: --max-new-trades must be exactly 1 for approval-driven discovery",
            file=sys.stderr,
        )
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

    try:
        if args.write and is_prod:
            # The lock starts before Database.connect(), which can apply PRAGMAs
            # and migrations. Its context exits only after _run_pipeline closes
            # the DB and all async/client resources.
            with operational_job_lock("scan", timeout=args.lock_timeout):
                result = _run_pipeline(args, write_authorization=_issue_write_capability())
        else:
            # Keep the established dry-run contract focused on logical writes:
            # no canonical persistence, enrichment, dispatch, or bridge DML.
            result = _run_pipeline(
                args,
                write_authorization=_issue_write_capability() if args.write else None,
            )
    except LockError as exc:
        print(f"error: global operational lock unavailable: {exc}", file=sys.stderr)
        return 3

    exit_code = int(result.pop("_exit_code", 0))
    if exit_code:
        return exit_code
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result["source_trade_internal_id"] is None and args.write:
        print("no new trade collected for approval")
    else:
        for key, value in result.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
