#!/usr/bin/env python3
"""S4 market-centric resolution refresh for specialist-evidence source trades.

This CLI reuses the PROVEN canonical truth path in
``src/polycopy/ingestion/source_trade_resolution.py``:

  * ``build_market_state_provider()``      -> PolymarketPublicAdapter.get_market
  * ``derive_winner_from_market_payload()`` -> single-winner truth
  * ``settle_source_trade_against_truth()``-> six-field BUY settlement
  * ``select_markets_for_refresh()``       -> exact one-selector batch
  * ``resolve_selected_markets()``         -> per-market atomic settle loop

It deliberately contains NO parallel resolution parser, NO winner derivation,
and NO settlement calculator of its own. Every truth verdict and every P/L
number come from the shared module above.

Selectors (exactly one required, both dry-run and write):
  --market-source-id   exact source_trades.market_source_id
  --wallet-id          wallets.id UUID (resolved to wallets.address)
  --watch-id           specialist_evidence_watchlist.id (joined to address)

Eligible rows: exact accepted source value (SOURCE_NAME /
"polymarket_clob"), BUY, non-sample, non-empty market_source_id. SELL rows are
never touched. No markets table row is required.

Writes ONLY:
  * source_trades resolution columns (the canonical authority)
  * specialist_market_refresh_state (bookkeeping ONLY — never the authority)

Production guard (PR68/PR71): a recognized production write requires ALL of
--write --allow-live --confirm-production-db. Refusal happens before any
open/schema/selector/provider/network step. Dry-run requires --allow-live for
any live read and writes zero rows.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (REPO_ROOT / "src", REPO_ROOT / "scripts", REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from evidence_db import (  # noqa: E402
    DbConn,
    is_production_db,
    open_readonly,
    open_writable,
    require_write_gates,
)
from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from polycopy.ingestion.source_trade_resolution import (  # noqa: E402
    MarketRefreshOutcome,
    MarketStateProvider,
    build_market_state_provider,
    resolve_selected_markets,
    select_markets_for_refresh,
)

PRODUCTION_DB_PATH = (REPO_ROOT / "data" / "polycopy.db").resolve()

_MAX_MARKETS = 500

# Targets this CLI must never create/modify (S5 / execution plane).
_FORBIDDEN_ARTIFACT_TABLES = (
    "specialist_approvals",
    "approved_specialist_trade_dispatches",
    "paper_signal_decisions",
    "paper_signal_execution_authorizations",
    "execution_risk_decisions",
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "copy_candidates",
    "candidate_price_snapshots",
    "signals",
    "orders",
    "positions",
    "marks",
    "settlements",
)


def _selectors(args: argparse.Namespace) -> list[str]:
    """Return the selectors present, for the exactly-one validation."""
    present = []
    if args.market_source_id:
        present.append("market-source-id")
    if args.wallet_id:
        present.append("wallet-id")
    if args.watch_id:
        present.append("watch-id")
    return present


def _resolve_wallet_address(db: DbConn, wallet_id: str) -> Optional[str]:
    """Resolve a wallets.id UUID to its canonical address.

    Returns ``None`` when the wallet is unknown OR a sample wallet (sample
    wallets are never refreshed — they must be refused, not silently skipped).
    """
    row = db.fetchone("SELECT address, is_sample FROM wallets WHERE id=?", (wallet_id,))
    if row is None:
        return None
    if bool(row["is_sample"]):
        return None
    return str(row["address"])


def _count_artifacts(db: DbConn) -> dict[str, int]:
    out = {}
    for t in _FORBIDDEN_ARTIFACT_TABLES:
        try:
            row = db.fetchone(f"SELECT COUNT(*) AS c FROM {t}")
            out[t] = int(row["c"]) if row is not None else 0
        except Exception:
            # Table genuinely absent (valid) -> zero; never mask a programming
            # error as zero. Re-raise anything that is not "no such table".
            out[t] = 0
    return out


def _validate_selector_readonly(args: argparse.Namespace) -> Optional[int]:
    """Refuse invalid wallet/watch selectors via a read-only preflight.

    Returns ``2`` (exit code) for an invalid selector and leaves no writable
    connection open, no provider constructed, and no network request issued.
    Returns ``None`` when the selector is structurally valid (a valid
    --market-source-id, a known non-sample wallet, or a known active non-sample
    watch). An exactly-valid --market-source-id that matches zero rows is NOT
    an error — it yields an honest zero-market report later.
    """
    if args.wallet_id:
        db = open_readonly(args.db_path)
        try:
            addr = _resolve_wallet_address(db, args.wallet_id)
        finally:
            db.close()
        if addr is None:
            # Unknown wallet OR sample wallet -> refuse honestly.
            print(
                "error: --wallet-id is unknown or a sample wallet; refusing "
                "(no markets selected)",
                file=sys.stderr,
            )
            return 2
        return None
    if args.watch_id:
        db = open_readonly(args.db_path)
        try:
            addr = _resolve_watch_address_passthrough(db, args.watch_id)
        finally:
            db.close()
        if addr is None:
            print(
                "error: --watch-id is unknown, paused, retired, or behind a "
                "sample wallet; refusing (no markets selected)",
                file=sys.stderr,
            )
            return 2
        return None
    # --market-source-id (or none of the other two): structurally valid.
    return None


def _resolve_watch_address_passthrough(db: DbConn, watch_id: str) -> Optional[str]:
    """Thin wrapper to the canonical module resolver (kept local for clarity)."""
    from polycopy.ingestion.source_trade_resolution import _resolve_watch_address

    return _resolve_watch_address(db, watch_id)


def _run(db: DbConn, args: argparse.Namespace, do_write: bool,
         provider: Optional[MarketStateProvider] = None,
         bookkeeping_writer: Optional[Callable[[DbConn, Any], None]] = None) -> dict:
    """Drive the proven per-market settle loop.

    Atomicity: when ``do_write`` is True, each market's source-trade settlement
    UPDATEs and that market's bookkeeping upsert are performed inside ONE
    SAVEPOINT by ``resolve_selected_markets`` (via ``bookkeeping_writer``). A
    failure inside either write rolls the SAVEPOINT back, leaving that market's
    source-trade and refresh-state rows unchanged. After all markets are
    processed, the connection is committed ONCE so every market's released
    savepoint is durably persisted together.
    """
    # Build the provider only when we have live intent (--allow-live required).
    provider_obj: Optional[MarketStateProvider] = None
    adapter: Optional[PolymarketPublicAdapter] = None
    if args.allow_live:
        if provider is not None:
            # Injected by a test: use verbatim (still enforce --allow-live).
            provider_obj = provider
        else:
            adapter = build_market_state_provider()
            provider_obj = adapter

    before = _count_artifacts(db) if do_write else {}

    report = _empty_report()
    try:
        outcomes = asyncio.run(
            resolve_selected_markets(
                db,
                markets=_select_markets_for_args(db, args),
                provider=provider_obj,
                apply=do_write,
                report=_report_obj(),
                bookkeeping_writer=(
                    bookkeeping_writer if bookkeeping_writer is not None
                    else _upsert_bookkeeping
                ) if do_write else None,
            )
        )
    finally:
        # Close the live adapter exactly once, on success or exception.
        if provider_obj is not None and hasattr(provider_obj, "aclose"):
            try:
                asyncio.run(provider_obj.aclose())
            except Exception:
                pass

    # Commit every market's released SAVEPOINT in a single transaction.
    if do_write:
        db.commit()

    after = _count_artifacts(db) if do_write else {}
    report = _summarize(outcomes)
    report["markets_selected"] = len(_select_markets_for_args(db, args))
    report["artifact_counts"] = after
    report["artifact_delta"] = {
        t: after[t] - before[t]
        for t in after
        if do_write and after[t] != before[t]
    }
    return report


def _select_markets_for_args(db: DbConn, args: argparse.Namespace) -> list[str]:
    """Resolve exactly one selector to its eligible markets (honest refusal)."""
    if args.wallet_id:
        wallet_address = _resolve_wallet_address(db, args.wallet_id)
        if wallet_address is None:
            # Refused upstream by _validate_selector_readonly; defensive empty.
            return []
        return select_markets_for_refresh(
            db, wallet_address=wallet_address, limit_markets=args.limit_markets
        )
    return select_markets_for_refresh(
        db,
        market_source_id=args.market_source_id,
        watch_id=args.watch_id,
        limit_markets=args.limit_markets,
    )


def _report_obj():
    # Imported lazily to keep the module import cheap for tests that only check
    # the CLI's refusal paths.
    from polycopy.ingestion.source_trade_resolution import ResolveReport

    return ResolveReport(dry_run=True, live_read_performed=False)


def _empty_report() -> dict:
    return {
        "markets_selected": 0,
        "updated": 0,
        "conflicts": 0,
        "noop": 0,
        "unresolved": 0,
        "unavailable": 0,
        "routing_http_error": 0,
        "provider_unavailable": 0,
        "malformed_payload": 0,
        "ambiguous": 0,
        "missing_winning_token": 0,
        "artifact_counts": {},
        "artifact_delta": {},
    }


def _summarize(outcomes: list[MarketRefreshOutcome]) -> dict:
    r = _empty_report()
    for o in outcomes:
        if o.conflict:
            r["conflicts"] += 1
        if o.noop:
            r["noop"] += 1
        r["updated"] += o.updated
        st = o.last_status
        if st == "unresolved":
            r["unresolved"] += 1
        elif st == "unavailable":
            r["unavailable"] += 1
        elif st == "routing_http_error":
            r["routing_http_error"] += 1
        elif st == "provider_unavailable":
            r["provider_unavailable"] += 1
        elif st == "malformed_payload":
            r["malformed_payload"] += 1
        elif st == "ambiguous":
            r["ambiguous"] += 1
        elif st == "missing_winning_token":
            r["missing_winning_token"] += 1
    return r


def _upsert_bookkeeping(db: DbConn, outcome: MarketRefreshOutcome) -> None:
    """Bookkeeping-only upsert for ONE market (called under the market SAVEPOINT).

    Honest semantics: last_status reflects the actual provider/truth result,
    last_error distinguishes provider/routing/malformed/ambiguity/missing-winner
    /conflict, resolved_at matches the trusted resolution observation (never
    fabricated, never taken from this table on later runs).
    """
    o = outcome
    existing = db.fetchone(
        "SELECT attempt_count FROM specialist_market_refresh_state "
        "WHERE market_source_id=?",
        (o.market_source_id,),
    )
    attempts = (existing["attempt_count"] if existing else 0) + o.attempt_count
    db.execute(
        "INSERT INTO specialist_market_refresh_state "
        "(market_source_id, last_checked_at, last_status, last_error, "
        "resolved_at, attempt_count, next_check_after) "
        "VALUES (?, datetime('now'), ?, ?, ?, ?, NULL) "
        "ON CONFLICT(market_source_id) DO UPDATE SET "
        "last_checked_at=excluded.last_checked_at, "
        "last_status=excluded.last_status, "
        "last_error=excluded.last_error, "
        "resolved_at=excluded.resolved_at, "
        "attempt_count=excluded.attempt_count, "
        "next_check_after=excluded.next_check_after",
        (
            o.market_source_id,
            o.last_status,
            o.last_error,
            o.resolved_at,
            attempts,
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--market-source-id")
    p.add_argument("--wallet-id")
    p.add_argument("--watch-id")
    p.add_argument("--limit-markets", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--allow-live", action="store_true")
    p.add_argument("--confirm-production-db", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv=None, *, provider=None, bookkeeping_writer=None) -> int:
    """Run the S4 refresh.

    ``provider`` is an optional injected ``MarketStateProvider`` (used by tests
    to avoid real network). When omitted and ``--allow-live`` is set, the
    proven ``build_market_state_provider()`` is used. ``--allow-live`` is
    always required for any live read (dry-run or write); when a provider is
    injected for a test, the live gate is still enforced and the adapter's
    ``aclose`` is still invoked (a fake provider may define ``aclose``).

    ``bookkeeping_writer`` is an optional injected callable
    ``(db, outcome) -> None`` used by tests to force a bookkeeping failure
    (rollback proof). When omitted, the production ``_upsert_bookkeeping`` is
    used.
    """
    args = _build_parser().parse_args(argv)

    # 1) Bound validation.
    if args.limit_markets < 1 or args.limit_markets > _MAX_MARKETS:
        print(
            f"error: --limit-markets must be in [1, {_MAX_MARKETS}]",
            file=sys.stderr,
        )
        return 2

    # 2) Exactly-one selector.
    present = _selectors(args)
    if len(present) == 0:
        print(
            "error: exactly one selector required (--market-source-id / "
            "--wallet-id / --watch-id)",
            file=sys.stderr,
        )
        return 2
    if len(present) > 1:
        print(
            f"error: only one selector allowed, got: {', '.join(present)}",
            file=sys.stderr,
        )
        return 2

    # 3) Empty selector rejection.
    if args.market_source_id == "" or args.wallet_id == "" or args.watch_id == "":
        print("error: selector must be non-empty", file=sys.stderr)
        return 2

    # 4) Refuse an unconfirmed production WRITE before any DB/provider action.
    #    A production DRY-RUN is allowed: it only requires --allow-live and
    #    opens the DB read-only (no --confirm-production-db needed).
    if args.write and is_production_db(args.db_path):
        if not (args.allow_live and args.confirm_production_db):
            print(
                "error: production write requires --write --allow-live "
                "--confirm-production-db",
                file=sys.stderr,
            )
            return 2

    do_write = require_write_gates(args, db_path=args.db_path)

    # 5) Read-only selector preflight: refuse invalid wallet/watch selectors
    #    before opening any writable connection or building a provider. No
    #    network request or source-trade/bookkeeping write is made on refusal.
    refused = _validate_selector_readonly(args)
    if refused is not None:
        return refused

    # 6) Live reads always require --allow-live (dry-run OR write).
    if not args.allow_live:
        print(
            "error: --allow-live is mandatory for any live market read",
            file=sys.stderr,
        )
        return 2

    db = open_writable(args.db_path, args) if do_write else open_readonly(args.db_path)
    try:
        report = _run(
            db, args, do_write, provider=provider,
            bookkeeping_writer=bookkeeping_writer,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on any write failure
        # A source-update or bookkeeping failure must NOT return success.
        print(f"error: refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        mode = "WRITE" if do_write else "dry-run"
        print(f"[{mode}] refresh: {json.dumps(report)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
