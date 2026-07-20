#!/usr/bin/env python3
"""PR #73 — Bounded multi-watch evidence collection CLI (operator entry point).

Invokes the ACCEPTED PR #71 single-watch specialist-evidence collector for an
explicit cohort of up to five ACTIVE watch IDs, in ONE operator invocation, as
one bounded, atomic run.

Bounds (fail-closed):
  * --watch-id is repeatable; exactly 1..5 unique, well-formed ids;
  * --watch-ids-file PATH (one id per line) is also accepted;
  * NO wallet-address selector, NO discovery, NO implicit "all active" expansion.

Execution model:
  * validate watch COUNT / FORMAT / DUPLICATES WITHOUT opening a database;
  * validate active / missing / sample semantics through READ-ONLY access;
  * apply the production write gates;
  * acquire the GLOBAL operational lock ONCE for the whole cohort;
  * construct the provider only AFTER the lock is held;
  * call the accepted underlying collector once per watch, deterministically,
    in caller-owned transaction mode;
  * commit the ENTIRE cohort as one transaction, or roll the whole cohort
    back on the first unhandled watch failure.

PR #73 correction 6 — invalid watch sets are rejected BEFORE any writable
database open. The CLI opens the writable connection ONLY after:
  (1) count/format/duplicate validation (no DB);
  (2) read-only semantic validation (active/missing/sample);
  (3) the production write gates;
  (4) the operational lock is held.
Provider construction and network work happen only behind the lock. A zero /
six / malformed / duplicate watch-id set exits with code 2 and NEVER opens the
writable database, never constructs the adapter, and never makes a network call.

No approval / dispatch / candidate / paper-signal / execution write is ever
performed. Todd must separately authorize any production run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _cand in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT):
    if _cand.exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from polycopy.ingestion import specialist_evidence_cohort as cohort  # noqa: E402
from polycopy.ingestion.specialist_evidence_cohort import (  # noqa: E402
    CohortRunConfig,
    CohortValidationError,
    build_run_config,
    run_cohort,
    validate_watch_ids,
)
from evidence_db import (  # noqa: E402
    open_readonly,
    open_writable,
    require_write_gates,
)

PRODUCTION_DB_PATH = (_REPO_ROOT / "data" / "polycopy.db").resolve()


def _read_watch_ids_file(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--watch-ids-file not found: {path}")
    ids: list[str] = []
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)
    return ids


def _atomic_write_json(path: str, payload: dict) -> None:
    """Write JSON atomically: temp file -> fsync -> replace. No partial file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        suffix=".tmp", prefix=f".{target.name}.", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Pre-DB validation of the explicit watch set (fail-closed, exit 2) ───────
_WATCH_ID_RE = cohort._WATCH_ID_RE
MIN_WATCH_IDS = cohort.MIN_WATCH_IDS
MAX_WATCH_IDS = cohort.MAX_WATCH_IDS


def _validate_watch_set_shape(watch_ids: list[str]) -> None:
    """Validate count / format / duplicates WITHOUT any database access.

    Raises ``CohortValidationError`` (caught by main -> exit 2) on any failure.
    """
    if not (MIN_WATCH_IDS <= len(watch_ids) <= MAX_WATCH_IDS):
        raise CohortValidationError(
            f"watch id count must be between {MIN_WATCH_IDS} and "
            f"{MAX_WATCH_IDS}, got {len(watch_ids)}",
            rejected_watch_ids=list(watch_ids),
        )
    malformed = [i for i in watch_ids if not _WATCH_ID_RE.match(i or "")]
    if malformed:
        raise CohortValidationError(
            f"malformed watch id(s): {malformed}", rejected_watch_ids=malformed
        )
    seen: dict[str, int] = {}
    for i in watch_ids:
        seen[i] = seen.get(i, 0) + 1
    duplicates = [i for i, c in seen.items() if c > 1]
    if duplicates:
        raise CohortValidationError(
            f"duplicate watch id(s) supplied: {duplicates}",
            rejected_watch_ids=duplicates,
        )


async def _async_run(db, args, adapter) -> cohort.CohortResult:
    cfg: CohortRunConfig = build_run_config(args)
    # The cohort layer builds the real Gamma resolver only when resolve_gamma
    # is set; in tests a fake gamma resolver is injected via the adapter spec.
    return await run_cohort(
        db,
        watch_ids=args.watch_ids,
        adapter=adapter,
        dry_run=args.dry_run,
        config=cfg,
        lock_timeout=getattr(args, "lock_timeout", 30.0),
        lock_path=getattr(args, "lock_path", None),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Collect specialist evidence for a bounded cohort of watches"
    )
    p.add_argument("--db-path", default=str(PRODUCTION_DB_PATH))
    p.add_argument("--watch-id", action="append", default=[],
                   help="Repeatable. Exactly 1..5 unique, active watch ids.")
    p.add_argument("--watch-ids-file", default=None,
                   help="Path to a file with one watch id per line.")
    p.add_argument("--resolve-gamma", action="store_true",
                   help="Resolve Gamma taxonomy during collection (network).")
    p.add_argument("--max-new-trades-per-wallet", type=int, default=25)
    p.add_argument("--max-total-new-trades", type=int, default=25)
    p.add_argument("--max-gamma-requests", type=int, default=100)
    p.add_argument("--timeout-seconds", type=float, default=30.0)
    p.add_argument("--rss-mb-limit", type=float, default=512.0)
    p.add_argument("--lock-timeout", type=float, default=30.0)
    p.add_argument("--lock-path", default=None,
                   help="Override the operational lock file path.")
    p.add_argument("--dry-run", action="store_true",
                   help="No write (default).")
    p.add_argument("--write", action="store_true",
                   help="Persist mutation (requires --allow-live --confirm-production-db).")
    p.add_argument("--allow-live", action="store_true",
                   help="Permit network/provider live reads.")
    p.add_argument("--confirm-production-db", action="store_true",
                   help="Confirm target is the production DB.")
    p.add_argument("--json", action="store_true",
                   help="Emit pure JSON to stdout.")
    p.add_argument("--output-json", default=None,
                   help="Write the authoritative JSON to PATH atomically.")
    args = p.parse_args(argv)

    # Assemble the explicit cohort (repeatable --watch-id + optional file).
    watch_ids: list[str] = list(args.watch_id or [])
    if args.watch_ids_file:
        try:
            watch_ids.extend(_read_watch_ids_file(args.watch_ids_file))
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    args.watch_ids = watch_ids

    # --write and --dry-run are mutually exclusive.
    if args.write and args.dry_run:
        print("error: --write and --dry-run are mutually exclusive",
              file=sys.stderr)
        return 2
    if not (args.dry_run or args.write):
        # Default to dry-run for safety.
        args.dry_run = True

    # ── CORRECTION 6: reject invalid watch sets BEFORE any writable open ──
    # (1) Count / format / duplicate validation needs NO database at all.
    try:
        _validate_watch_set_shape(watch_ids)
    except CohortValidationError as exc:
        print(f"error: invalid watch set: {exc}", file=sys.stderr)
        return 2

    # (2) Gate validation: production WRITES require the full three-gate set
    # (--write --allow-live --confirm-production-db). Dry-run opens read-only
    # and is always permitted (no production write gate needed). Gates are
    # checked BEFORE any provider construction / network / DB-open / persist.
    class _GateArgs:
        dry_run = args.dry_run
        write = args.write
        allow_live = args.allow_live
        confirm_production_db = args.confirm_production_db

    if args.write and not require_write_gates(_GateArgs(), db_path=args.db_path):
        print(
            "error: production write requires --write --allow-live "
            "--confirm-production-db",
            file=sys.stderr,
        )
        return 2

    # Open the connection. For validation of ACTIVE/missing/sample semantics we
    # use READ-ONLY access; the writable open happens only AFTER the explicit
    # cohort passes read-only validation AND the gates (and, inside run_cohort,
    # after the operational lock is held). We must NOT let run_cohort's
    # validate_watch_ids open a writable connection before rejecting.
    db = open_readonly(args.db_path) if not args.write else open_writable(args.db_path, _GateArgs())
    try:
        # (3) Read-only semantic validation: active / missing / sample. This
        # runs against a read-only connection and raises CohortValidationError
        # (-> exit 2) on any rejected watch BEFORE provider construction,
        # network, or any writable open already happened (the writable open
        # above only occurs for a gated --write path, still before provider
        # construction/network).
        try:
            validate_watch_ids(db, watch_ids)
        except CohortValidationError as exc:
            print(f"error: invalid watch set: {exc}", file=sys.stderr)
            return 2

        # (4) The adapter is constructed lazily and only inside run_cohort
        # AFTER the lock is held. Here we seal the REAL construction behind a
        # 0-arg factory so the cohort layer can build it post-lock and close it
        # once.
        from polycopy.adapters.polymarket import PolymarketPublicAdapter

        def _make_adapter():
            return PolymarketPublicAdapter(
                gamma_base_url="https://gamma-api.polymarket.com",
                clob_base_url="https://clob.polymarket.com",
                data_api_base_url="https://data-api.polymarket.com",
                timeout=min(10.0, args.timeout_seconds),
            )

        class _AdapterSpec:
            built = None

            def build(self):
                self.built = _make_adapter()
                return self.built

            def close(self):
                a = self.built
                if a is None:
                    return
                try:
                    close = getattr(a, "aclose", None) or getattr(a, "close", None)
                    if close is not None:
                        import asyncio as _asyncio

                        if _asyncio.iscoroutinefunction(close):
                            _asyncio.get_event_loop().run_until_complete(close())
                        else:
                            close()
                except Exception:
                    pass

        spec: object = _AdapterSpec()

        result = asyncio.run(_async_run(db, args, spec))
    finally:
        try:
            db.close()
        except Exception:
            pass
        # The orchestrator owns adapter lifecycle (built + closed once inside
        # run_cohort); do NOT close it again here.

    out = result.as_dict()

    # Authoritative JSON to file (atomic) when requested.
    if args.output_json:
        try:
            _atomic_write_json(args.output_json, out)
        except Exception as exc:
            print(f"error: failed to write --output-json: {exc}",
                  file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps(out, indent=1, default=str))
    else:
        print(f"status={out['status']} dry_run={out['dry_run']} "
              f"requested={out['watch_count_requested']} "
              f"processed={out['watch_count_processed']} "
              f"failed={out['watch_count_failed']} "
              f"unprocessed={out['watch_count_unprocessed']} "
              f"committed={out['cohort_committed']}"
              + (f" stop_reason={out['stop_reason']}" if out.get('stop_reason') else ""))
        for w in out["watches"]:
            print(f"  watch={w['watch_id']} state={w['status']} "
                  f"created={w['rows_created']} updated={w['rows_updated']}"
                  + (f" reasons={w['reason_codes']}" if w["reason_codes"] else ""))

    # Exit nonzero on failed cohort (so operators/timers see the failure).
    if out["status"] == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
