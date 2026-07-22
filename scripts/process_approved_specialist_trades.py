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
  * Bounded: --max-new-trades (default 1, max bounded) + exact --approval-id.
  * Output includes the full artifact chain (approval/source-trade/enrichment/
    dispatch/candidate/paper-signal).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.db.database import Database  # noqa: E402
from polycopy.execution.specialist_approval import (  # noqa: E402
    get_approval,
)
from polycopy.ingestion.approved_wallet_collector import collect  # noqa: E402
from polycopy.ingestion.source_trade_writer import write_valid_rows  # noqa: E402
from polycopy.ingestion.source_trade_enrichment import enrich_source_trade_async  # noqa: E402
from polycopy.engine.approved_specialist_dispatcher import dispatch_one  # noqa: E402
from polycopy.config.settings import Settings  # noqa: E402
from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _is_production_db(db_path: str) -> bool:
    try:
        return Path(db_path).resolve() == PRODUCTION_DB_PATH
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Orchestrate approved-specialist trade -> paper signal (no execution)"
    )
    p.add_argument("--approval-id", required=True, help="Exact approval_id (UUID)")
    p.add_argument("--max-new-trades", type=int, default=1,
                   help="Bounded new-trade cap for collection (default 1)")
    p.add_argument("--write", action="store_true", help="Persist all stages")
    p.add_argument("--allow-live", action="store_true",
                   help="Authorize live Gamma/CLOB/collection network")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm target is the production DB")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if args.max_new_trades < 1 or args.max_new_trades > 1:
        print("error: --max-new-trades must be exactly 1 for approval-driven discovery",
              file=sys.stderr)
        return 2

    is_prod = _is_production_db(args.db_path)
    if args.write and is_prod:
        missing = []
        if not args.allow_live:
            missing.append("--allow-live")
        if not args.confirm_production_db:
            missing.append("--confirm-production-db")
        if missing:
            print("error: production write requires: " + ", ".join(missing),
                  file=sys.stderr)
            return 2

    adapter = None
    runner = None
    db = Database(Path(args.db_path)).connect()
    try:
        # Resolve approval (reject unknown/disabled/revoked via active check).
        try:
            approval = get_approval(db, args.approval_id)
        except KeyError:
            print("error: unknown approval_id", file=sys.stderr)
            return 2
        if not approval.enabled or approval.revoked_at is not None:
            print(f"error: approval is {'disabled' if not approval.enabled else 'revoked'}",
                  file=sys.stderr)
            return 2
        wallet = approval.wallet_address

        if not args.allow_live:
            print("error: --allow-live required for network collection", file=sys.stderr)
            return 2

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
            # Dispatcher/scoring stays synchronous and must never invoke the
            # async adapter. Collection/enrichment pre-resolve the exact trade.
            if condition_id not in gamma_cache:
                raise RuntimeError(
                    "synchronous dispatch requested uncached Gamma market "
                    f"{condition_id!r}; pre-resolve it before dispatch"
                )
            return gamma_cache[condition_id]

        # ── Stage 1: collect at most one new trade (approval-driven) ──
        result = runner.run(collect(adapter, wallet, gamma_resolver=gamma_async))
        accepted = result.accepted_rows[:args.max_new_trades]
        source_trade_internal_id = None
        inserted_trades = 0
        if args.write and accepted:
            from polycopy.ingestion.normalized_source_trade import normalize_source_trade  # noqa
            pre = {
                (str(r[0]), str(r[1]))
                for r in db.conn.execute(
                    "SELECT source, source_trade_id FROM source_trades WHERE source=?",
                    ("polymarket_data_api_trades_user",),
                )
            }
            norms = [normalize_source_trade(t, requested_wallet=wallet, allow_sell=False,
                                            gamma_market=gamma_sync(t.market_source_id))
                     for t in accepted]
            # Replay-safe script boundary uses the exact canonical writer key:
            # (source, source_trade_id), never an ID-only approximation.
            fresh_norms = [
                n for n in norms if (str(n.source), str(n.source_trade_id)) not in pre
            ]
            out = write_valid_rows(
                db, fresh_norms, dry_run=False,
                pre_existing_ids={
                    n.source_trade_id for n in fresh_norms if n.source_trade_id is not None
                },
            )
            inserted_trades = out.inserted
            # Resolve the persisted internal id of the first accepted trade.
            first = norms[0]
            row = db.fetchone(
                "SELECT id FROM source_trades WHERE source=? AND source_trade_id=?",
                (first.source, first.source_trade_id),
            )
            if row:
                source_trade_internal_id = row["id"]
        elif accepted:
            source_trade_internal_id = None  # dry-run: no persistence

        if source_trade_internal_id is None and args.write and not inserted_trades:
            # No new trade collected; nothing to enrich/dispatch.
            out = {
                "approval_id": args.approval_id,
                "source_trade_internal_id": None,
                "inserted_trades": 0,
                "enrichment_id": None,
                "dispatch_id": None,
                "candidate_id": None,
                "paper_signal_decision_id": None,
                "paper_signal_verdict": None,
                "mode": "write" if args.write else "dry-run",
            }
            print(json.dumps(out, sort_keys=True) if args.json else
                  "no new trade collected for approval")
            return 0

        # ── Stage 2 + 3: enrich + dispatch the exact source trade ──
        if source_trade_internal_id is not None:
            enrichment = runner.run(enrich_source_trade_async(
                db, source_trade_internal_id,
                gamma_resolver=gamma_async, dry_run=not args.write))
            disp = dispatch_one(
                db, approval_id=args.approval_id,
                source_trade_internal_id=source_trade_internal_id,
                gamma_resolver=gamma_sync, clob_provider=adapter,
                dry_run=not args.write)
        else:
            enrichment = None
            disp = None

        out = {
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
    finally:
        if adapter is not None:
            aclose = getattr(adapter, "aclose", None)
            if callable(aclose) and runner is not None:
                try:
                    runner.run(aclose())
                except Exception:
                    pass
            elif callable(getattr(adapter, "close", None)):
                try:
                    adapter.close()
                except Exception:
                    pass
        if runner is not None:
            runner.close()
        db.close()

    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        for k, v in out.items():
            print(f"{k}={v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
