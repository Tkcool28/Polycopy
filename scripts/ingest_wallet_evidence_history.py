#!/usr/bin/env python3
"""PR66 bounded historical evidence ingestion; dry-run by default.

Four modes
----------
  A. offline / fixture   (default, no --allow-live): NO network, NO DB read,
                          NO write. Operates only against an explicit
                          --input-file (if provided) or a scripted fixture.
  B. live dry-run        (--allow-live, no --write): real bounded upstream read,
                          NO DB write. Reports live_read_performed=true,
                          dry_run=true, committed=false.
  C. live write          (--allow-live --write --confirm-production-db): bounded
                          source_trades persistence only, behind an operational
                          lock, after a read-only dedup inspection.
  D. temp-DB write test  (same logical auth path, --db-path points at an isolated
                          DB): proves the writer without any production side effect.

Hard guardrails
---------------
  * No --allow-live => NO network call, NO database opened. The process must be
    able to run fully offline.
  * --write ALONE (without --allow-live) is rejected (no live fetch by write).
  * --write without --confirm-production-db is rejected for the production DB.
  * Production write requires ALL THREE flags:
        --allow-live --write --confirm-production-db
  * Bounds: default max-pages 2 / max-records 100; hard max 5 / 250.
  * BUY and SELL are both captured as evidence. No scoring, candidates,
    signals, snapshots, orders, positions, approvals, timers, or services.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]

from polycopy.db.database import Database  # noqa: E402
from polycopy.ingestion.approved_wallet_collector import (  # noqa: E402
    UnsafeCollectorConfiguration,
    resolve_wallet,
)
from polycopy.ingestion.normalized_source_trade import SOURCE_NAME  # noqa: E402
from polycopy.ingestion.source_trade_writer import write_valid_rows  # noqa: E402
from polycopy.ingestion.wallet_evidence_history import (  # noqa: E402
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RECORDS,
    HARD_MAX_PAGES,
    HARD_MAX_RECORDS,
    _ErrorRecord,
    collect_historical_evidence,
)
from polycopy.runtime.locks import operational_job_lock  # noqa: E402


class _OfflineProvider:
    """No-network provider used when --allow-live is absent.

    By default it returns an empty page (no records), which yields a clean
    offline dry-run. If --input-file is supplied with a list of raw records,
    it yields exactly one page from that fixture. It NEVER makes a request.
    """

    made_network_call = False

    def __init__(self, fixture: list | None = None) -> None:
        self._pages = [fixture] if fixture else []

    async def fetch_trades(self, wallet: str, *, limit: int, page: int) -> list:
        if page < len(self._pages):
            return self._pages[page][:limit]
        return []


class _MockLiveProvider:
    """Test seam: behaves like the live provider (sets made_network_call=True,
    so live_read_performed logic + the all-flags write gate apply) but returns
    scripted pages from an explicit fixture instead of hitting the network.

    Used by the temp-DB write test to exercise the full authorized write path
    without any real HTTP call.
    """

    made_network_call = True

    def __init__(self, fixture: list) -> None:
        self._pages = [fixture] if fixture else []

    async def fetch_trades(self, wallet: str, *, limit: int, page: int) -> list:
        if page < len(self._pages):
            return self._pages[page][:limit]
        return []

    async def aclose(self) -> None:
        return None


class _LiveProvider:
    """Bounded live wrapper over PolymarketPublicAdapter.get_trades_by_address.

    Forwards ``page * limit`` as the data-api offset so page 2+ requests OLDER
    records upstream (true offset pagination). Returns raw dicts verbatim so the
    canonical metadata serializer can preserve event/taxonomy/series fields.
    """

    made_network_call = True

    def __init__(self, timeout: float = 10.0) -> None:
        from polycopy.adapters.polymarket import PolymarketPublicAdapter

        self._adapter = PolymarketPublicAdapter(
            gamma_base_url="https://gamma-api.polymarket.com",
            clob_base_url="https://clob.polymarket.com",
            data_api_base_url="https://data-api.polymarket.com",
            timeout=timeout,
        )

    async def fetch_trades(self, wallet: str, *, limit: int, page: int) -> list:
        from datetime import datetime as _dt, timezone as _tz

        return await self._adapter.get_trades_by_address(
            wallet,
            since=_dt(2000, 1, 1, tzinfo=_tz.utc),
            limit=limit,
            offset=page * limit,
            return_raw=True,
        )

    async def aclose(self) -> None:
        try:
            await self._adapter.aclose()
        except Exception:
            pass


def _load_input_file(path: str) -> list:
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, list):
        return raw
    # Allow {"pages": [[...], [...]]} or {"records": [...]} shapes.
    if isinstance(raw, dict):
        if "pages" in raw and isinstance(raw["pages"], list):
            return [p if isinstance(p, list) else [] for p in raw["pages"]]
        if "records" in raw and isinstance(raw["records"], list):
            return raw["records"]
    raise ValueError("input file must be a list or {pages:[...]} / {records:[...]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded BUY+SELL evidence for one approved wallet")
    parser.add_argument("--wallet")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--page-size", type=int, default=None,
                        help="upstream page size (default: min(max-records,100)); "
                             "a separate bound from --max-records")
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--allow-live", action="store_true")
    parser.add_argument("--mock-live", action="store_true",
                        help="test seam: satisfy the live gate with a scripted provider "
                             "(no real network); requires --input-file")
    parser.add_argument("--confirm-production-db", action="store_true")
    parser.add_argument("--input-file", default=None, help="explicit offline fixture/records")
    parser.add_argument("--db-path", default=str(ROOT / "data" / "polycopy.db"))
    args = parser.parse_args(argv)

    if not 1 <= args.max_pages <= HARD_MAX_PAGES or not 1 <= args.max_records <= HARD_MAX_RECORDS:
        parser.error("max bounds exceed hard safety limits")
    if args.write and not ((args.allow_live or args.mock_live) and args.confirm_production_db):
        print(
            "error: --write requires --allow-live (or --mock-live) and --confirm-production-db",
            file=sys.stderr,
        )
        return 2

    try:
        wallet = resolve_wallet(args.wallet)
    except UnsafeCollectorConfiguration as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # ── Provider selection (NO live provider without --allow-live/--mock-live) ──
    if args.allow_live:
        if args.input_file:
            print("error: --input-file is an offline fixture; do not combine with --allow-live",
                  file=sys.stderr)
            return 2
        provider = _LiveProvider(timeout=10.0)
    elif args.mock_live:
        if not args.input_file:
            print("error: --mock-live requires --input-file (scripted fixture)", file=sys.stderr)
            return 2
        provider = _MockLiveProvider(_load_input_file(args.input_file))
    else:
        fixture = _load_input_file(args.input_file) if args.input_file else None
        provider = _OfflineProvider(fixture)

    started = time.monotonic()
    try:
        result = asyncio.run(
            collect_historical_evidence(
                provider,  # type: ignore[arg-type]
                wallet,
                max_pages=args.max_pages,
                max_records=args.max_records,
                page_size=args.page_size,
                before=args.before,
                after=args.after,
            )
        )
    finally:
        try:
            asyncio.run(provider.aclose())  # type: ignore[attr-defined]
        except Exception:
            pass

    # ── Live-read contract: report reflects the CLI's live intent ──
    result.live_read_performed = bool(args.allow_live or args.mock_live)

    # ── DB duplicate counting via read-only inspection (no write) ──
    if args.write:
        with operational_job_lock("pr66-wallet-evidence"):
            db = Database(Path(args.db_path)).connect()
            try:
                pre_existing_ids = {
                    row[0]
                    for row in db.conn.execute(
                        "SELECT source_trade_id FROM source_trades WHERE source=?", (SOURCE_NAME,)
                    )
                }
            finally:
                db.close()
        result.db_duplicate_count = sum(
            1 for r in result.accepted_rows if r.source_trade_id in pre_existing_ids
        )
        result.dry_run = False

        db = Database(Path(args.db_path)).connect()
        try:
            outcome = write_valid_rows(
                db,
                result.accepted_rows,
                dry_run=False,
                pre_existing_ids=pre_existing_ids,
            )
        finally:
            db.close()
        result.inserted = outcome.inserted
        result.committed = outcome.committed
        if outcome.errors:
            result.errors.append(
                _ErrorRecord(
                    page=-1,
                    record_index=None,
                    error_type="write_error",
                    message=outcome.error_message or "write errors",
                )
            )

    result.duration_seconds = time.monotonic() - started
    report = result.report()
    report.update(
        {
            "hard_max_pages": HARD_MAX_PAGES,
            "hard_max_records": HARD_MAX_RECORDS,
            "input_file": bool(args.input_file),
        }
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
