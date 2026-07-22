#!/usr/bin/env python3
"""Enrich exactly one approved source trade (authoritative evidence resolution).

This is the bounded enrichment entry point. It operates on ONE exact
``source_trades.id`` (the canonical internal UUID), never an arbitrary wallet
history. It resolves + persists the durable ``source_trade_enrichments``
record AND the scorer-visible ``source_trades.metadata_json`` (S5) inside one
transaction. It does NOT ingest trades, call the bridge, or execute anything.

Safety envelope (S5 repair)
---------------------------
* Dry-run is the DEFAULT. No --allow-live => no Gamma network resolution.
* No --write => no writes (even with --allow-live).
* A production DB write requires ALL THREE gates:
    --write --allow-live --confirm-production-db
* Recognized production write refusal order (before file existence / DB open /
  schema read / source-trade lookup / adapter creation / network request):
    * missing any of --write/--allow-live/--confirm-production-db
* Reads use the shared research-plane DB helper (``evidence_db``):
    * raw SQLite mode=ro for dry-run / read paths
    * schema must already be exactly v21 (no creation / no migration)
    * writable open only after the write gates pass
* Bounded: at most one source trade, at most one Gamma resolve.
* Exit codes:
    * invalid arguments / selection / safety refusal -> exit 2
    * DB / provider / write failure                -> controlled nonzero
    * completed honest outcome (incl. conflict/unavailable) -> structured
      report without a raw traceback
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

from evidence_db import (  # noqa: E402
    DbConn,
    is_production_db,
    open_readonly,
    open_writable,
    require_write_gates,
)
from polycopy.ingestion.source_trade_enrichment import enrich_source_trade_async  # noqa: E402

# Explicit module-bound injection seam for tests. The CLI invokes this async
# callable only via its single asyncio.Runner boundary.
enrichment_async_fn = enrich_source_trade_async
from polycopy.adapters.polymarket import PolymarketPublicAdapter  # noqa: E402
from polycopy.config.settings import Settings  # noqa: E402

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _close_adapter(runner: asyncio.Runner, adapter) -> None:
    """Close an async adapter on this CLI's single primary event loop."""
    if adapter is None:
        return
    aclose = getattr(adapter, "aclose", None)
    if callable(aclose):
        try:
            runner.run(aclose())
        except Exception:
            pass
        return
    close = getattr(adapter, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _make_adapter():
    settings = Settings()
    return PolymarketPublicAdapter(
        gamma_base_url=settings.gamma_base_url,
        clob_base_url=settings.clob_base_url,
        data_api_base_url=settings.data_api_base_url,
        timeout=10.0,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Enrich exactly one approved source trade (authoritative evidence)"
    )
    p.add_argument("--source-trade-id", required=True,
                   help="Exact internal source_trades.id UUID")
    p.add_argument("--write", action="store_true", help="Persist enrichment record")
    p.add_argument("--allow-live", action="store_true",
                   help="Authorize bounded Gamma network resolution")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm target is the production DB")
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    if not args.source_trade_id.strip():
        print("error: --source-trade-id must be non-empty", file=sys.stderr)
        return 2

    is_prod = is_production_db(args.db_path)

    # ── Write-gate refusal (BEFORE any DB open / schema read / lookup / ──
    # adapter / network). Every write — production OR non-production — requires
    # --write AND --allow-live. A recognized production write additionally
    # requires --confirm-production-db. Refuse with exit 2 before touching
    # open_readonly / open_writable / the resolver / the DB at all.
    if args.write:
        missing = []
        if not args.allow_live:
            missing.append("--allow-live")
        if is_prod and not args.confirm_production_db:
            missing.append("--confirm-production-db")
        if missing:
            parts = " ".join(missing)
            scope = "production" if is_prod else "non-production"
            print(
                f"error: {scope} enrichment write requires: --write {parts}",
                file=sys.stderr,
            )
            return 2

    # Open read-only first (fail-closed: schema must already be exactly v21,
    # no creation, no migration). The shared helper refuses a missing file.
    try:
        db: DbConn = open_readonly(args.db_path)
    except Exception as exc:  # missing file / schema mismatch / creation refused
        print(f"error: cannot open database: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    do_write = require_write_gates(args, db_path=args.db_path)
    if args.write and not do_write:
        # Gates not satisfied for this target (e.g. production missing a gate,
        # caught above with exit 2; this is a defensive secondary refusal).
        db.close()
        print(
            "error: enrichment write refused — requires --write"
            + (" --allow-live --confirm-production-db" if is_prod else " --allow-live"),
            file=sys.stderr,
        )
        return 2

    # Re-open writable only if we are actually going to write.
    if do_write:
        db.close()
        try:
            db = open_writable(args.db_path, args)
        except Exception as exc:
            print(f"error: cannot open writable: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 2

    # Build the bounded Gamma resolver (only when live resolution authorized).
    gamma_resolver = None
    adapter = None
    runner = asyncio.Runner()
    if args.allow_live:
        try:
            adapter = _make_adapter()
            # Thin wrapper around the real get_market_raw route. It does NOT
            # swallow provider exceptions into None (that would convert a
            # provider failure into ordinary missing evidence).
            async def _resolver(condition_id: str):
                return await adapter.get_market_raw(condition_id)
            gamma_resolver = _resolver
        except Exception as exc:
            _close_adapter(runner, adapter)
            runner.close()
            db.close()
            print(f"error: adapter init failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1

    try:
        result = runner.run(
            enrichment_async_fn(
                db, args.source_trade_id,
                gamma_resolver=gamma_resolver,
                dry_run=not do_write,
            )
        )
    except Exception as exc:
        _close_adapter(runner, adapter)
        runner.close()
        db.close()
        print(f"error: enrichment failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    # A persistence/operational failure (incl. a SAVEPOINT that rolled back)
    # must NOT be committed. The object returned, but the atomic operation did
    # not succeed, so we leave the transaction uncommitted and exit nonzero.
    # A Gamma provider error is likewise a hard failure: the CLI rolls the
    # transaction back and returns exit 1, so the audit row created in the
    # caller-owned transaction is NOT durably persisted by this CLI.
    if getattr(result, "operational_error", False) or getattr(result, "provider_error", False):
        db.conn.rollback()
        _close_adapter(runner, adapter)
        runner.close()
        db.close()
        print(f"error: {result.error_message or result.status}", file=sys.stderr)
        return 1

    # An invalid selection (unknown trade, unsupported source, SELL, sample
    # trade, or missing market identity) is a controlled refusal: the library
    # can create an audit row in the caller-owned transaction, but this CLI
    # rolls the transaction back and returns exit 2. It must not be committed,
    # and no provider request occurred (eligibility is checked before
    # resolve_gamma_state). Use the explicit typed flag, not a generic status.
    if getattr(result, "selection_error", False):
        try:
            db.conn.rollback()
        except Exception:
            pass
        _close_adapter(runner, adapter)
        runner.close()
        db.close()
        print(
            f"error: invalid selection: {result.error_message or result.reason_codes}",
            file=sys.stderr,
        )
        return 2

    # The canonical repair commits only after the atomic metadata+provenance
    # SAVEPOINT succeeded inside enrich_source_trade. For a real write we
    # explicitly commit the outer transaction here.
    if do_write:
        try:
            db.conn.commit()
        except Exception as exc:
            db.conn.rollback()
            _close_adapter(runner, adapter)
            runner.close()
            db.close()
            print(f"error: commit failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1

    # Close the adapter on success through the same primary CLI event loop.
    _close_adapter(runner, adapter)
    runner.close()
    db.close()

    out = result.as_dict()
    out["mode"] = "write" if do_write else "dry-run"
    out["production_db"] = str(PRODUCTION_DB_PATH) if is_prod else args.db_path
    if args.json:
        print(json.dumps(out, sort_keys=True))
    else:
        print(f"source_trade_internal_id={out['source_trade_internal_id']}")
        print(f"enrichment_id={out['enrichment_id']}")
        print(f"status={out['status']}")
        print(f"created={out['created']} updated={out['updated']}")
        print(f"metadata_changed={out.get('metadata_changed')}")
        print(f"reason_codes={out['reason_codes']}")
        if out["error_message"]:
            print(f"error={out['error_message']}")
    # An honest completed outcome (complete/incomplete/unavailable/conflict)
    # exits 0. provider_error / operational_error exit nonzero (handled above).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
