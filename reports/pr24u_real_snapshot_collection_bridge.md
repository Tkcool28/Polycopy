# PR24U — Trade Copyability REAL Snapshot/Depth/Current-Price Collection Bridge

**Status:** REPORT-ONLY / DRY-RUN / NON-AUTOMATED
**Branch:** `feat/trade-copyability-real-snapshot-collection-bridge`
**Base:** `f6aec9b3af0c827a991eb628745ac0f09c2fbb09` (main, requirement-confirmed)
**Mode:** Default = dry-run (no network). `--allow-live-preview` = real read-only `/book` fetch, still non-persisting.

## What this PR does

PR24S proved the *evidence-consuming* path works (it shaped offline synthetic
depth into the PR24S `SnapshotEvidenceResult`). But production carried **no real
snapshot/depth/current-price evidence** to collect. PR24U closes that gap by
**proving whether real evidence can be collected** for eligible `source_trades`
rows and **shaped into the PR24S evidence structures**.

It is explicitly **NOT** a scoring PR, **NOT** a persistence PR, and **NOT** an
automation PR.

## Client / path reused (requirement #2, #3)

- **Reused** `polycopy.adapters.polymarket_clob.PolymarketClobClient` — the
  existing read-only `GET https://clob.polymarket.com/book?token=<token_id>`
  adapter. It returns a validated `ClobBook` (bids/asks, best_bid/best_ask,
  spread, book_hash, fetched_at, bounded error_code). No duplicate client was
  invented.
- **Reused** `polycopy.engine.trade_copyability_snapshot_evidence_bridge.
  build_snapshot_evidence` + `SnapshotEvidenceResult` so collected `/book` data
  is shaped into the exact PR24S evidence structures (compatibility verified by
  tests).

Two collection modes:
- Default dry-run: **no network call**. Eligibility + field-availability proven
  structurally against `source_trades` via an injectable `RealSnapshotEvidenceCollector`
  (offline default).
- `--allow-live-preview`: a real `ClobBook` is fetched per eligible token via
  `LiveClobBookCollector` wrapping `PolymarketClobClient`. Still non-persisting;
  failures captured per-row, never crash the batch.

## Eligible / ineligible rows in dry-run (production)

- `source_trades` = 5 (raw `buy=4 / BUY=1`, canonical `BUY=5`).
- **Eligible for collection: 1** — `test_trade_1` (carries a real 77-char
  `token_id = 72753295727566659208677964635039361717871718602259295378609650323504626128275`).
- **Ineligible: 4** — all blocked by `missing_token_id` (NULL in production).
  Honestly blocked, never invented.
- `ingestion_side_inconsistency_present = True` (mixed `buy`/`BUY` casing).
  NOT backfilled — PR24T left existing rows; future writes normalize.

## Evidence fields available / missing (live `/book` would provide)

| Field | Available from `/book` | Notes |
|---|---|---|
| current price (best ask) | YES (when depth present) | collected live |
| depth / liquidity levels | YES (when book has levels) | collected live |
| spread | YES (best_ask − best_bid) | collected live |
| snapshot timestamp | YES (`fetched_at`) | collected live |
| token_id / market identifiers | YES (from `source_trades`) | already present for 1 row |
| market state (active/closed/resolved) | **NO** | `/book` does NOT expose it |
| seconds_to_market_end | **NO** | needs Gamma market-state source |

**Deferred / Not Included:** market-state (active/closed/resolved) and
`seconds_to_market_end` are NOT available from the CLOB `/book` endpoint. They
require a separate Gamma market-state client (recommended as PR24V). PR24U does
not invent them; the evidence keeps them `None` and reports the gap honestly.

## PR24S evidence compatibility

Collected `/book` data is shaped into `SnapshotEvidenceResult` via the reused
`build_snapshot_evidence`. In the offline dry-run, the single eligible row has
no live levels, so `pr24s_evidence_compatibility = incompatible`; the test
suite proves `compatible` shaping with synthetic books. The compatibility path
is verified, not stubbed.

## Confirmation: no production persistence (guardrails)

- No write to production tables. DB file `size` + `mtime` **unchanged** before
  vs after both reports (verified: `DB UNCHANGED: PASS`).
- All guarded counts remain at production baseline:
  `trade_copyability_decisions=0`, `copy_candidates=0`, `paper_signal_decisions=0`,
  `candidate_price_snapshots=0`, `candidate_price_snapshot_levels=0`,
  `orders=0`, `positions=0`.
- All three `ready_*` flags = `False`.
- No timers / services / collect / scan / settle / update changed or enabled
  (this PR is additive read-only code only).

## Tests run and results

- **PR24U targeted suite:** `tests/test_p24u_trade_copyability_real_snapshot_collection_bridge.py`
  → **17 passed**.
- Coverage includes every required behavior:
  eligible row → evidence structure + report row;
  missing `token_id` skipped with clear reason;
  market/depth client failure controlled (no crash of whole run);
  dry-run creates no DB writes;
  no `trade_copyability_decisions` / `copy_candidates` / `paper_signal_decisions` /
  `candidate_price_snapshots`(+levels) / `orders` / `positions` created;
  purity (no mutating SQL, no `import polycopy.db.database`, no wiring/broker tokens);
  reuse of existing `PolymarketClobClient` (not duplicated); JSON valid + flags False.
- **Full suite:** `pytest tests -q` → **2547 passed, 2 skipped, 0 failed** (160.7s). No regressions
  introduced by the additive PR24U files.
- `ruff check src scripts tests` → **All checks passed**.

## Files added (additive only — 3 new files, no edits to existing scoring/writing code)

1. `src/polycopy/engine/trade_copyability_real_snapshot_collection_bridge.py` — pure module.
2. `scripts/report_trade_copyability_real_snapshot_collection_bridge.py` — CLI (`--json`, `--db-path`, `--limit`, `--allow-live-preview`).
3. `tests/test_p24u_trade_copyability_real_snapshot_collection_bridge.py` — 17 tests.

## Recommended next step

A guarded persistence writer that lands collected `/book` evidence into
`candidate_price_snapshots` (read-only by PR24S), plus a Gamma market-state
client for market state + `seconds_to_market_end`. Do not wire automation or
persist decisions until those land and are reviewed.

## Deferred / Not Included (ambiguity log)

- Market-state (active/closed/resolved) + `seconds_to_market_end`: not provided
  by `/book`; deferred to a future Gamma-client PR (suggested PR24V).
- Live `--allow-live-preview` network run was NOT executed against production as
  the default safe mode is offline dry-run; the live code path is unit-tested
  against synthetic books and the real `PolymarketClobClient` import, but the
  actual outbound HTTP call is opt-in only and was intentionally not fired in
  this report-only PR to honor the SAFE/PARKED/PAPER-ONLY posture.
- No production backfill, no `source_trades.side` backfill, no deploy, no service
  restart, no timer enablement.
