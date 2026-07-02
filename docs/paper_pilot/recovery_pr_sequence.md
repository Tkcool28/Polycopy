# Polycopy Smart-Wallet Signal Pipeline — Recovery PR Sequence
**Status:** PLANNING ONLY.
**Repository:** polycopy (branch main, baseline HEAD 6ae6eaaf47d7a8b20b8613aa2321ff8b2d180f35)
**Companion audit:** docs/paper_pilot/smart_wallet_signal_path_audit.md
**Author:** nxhermes subagent
**Generated:** 2026-07-01T17:07:18Z UTC (2026-07-01T11:07:18-06:00 America/Denver)

> This is a redacted copy of the durable PR-sequence planning doc. It preserves
> the six-PR scope, design principles, and explicit out-of-scope items. Temporary
> workspace paths, machine-specific noise, secrets/credentials, and text implying
> the paper pilot has started have been redacted.

## 0. Scope and non-goals
- This document is planning and scope definition only.
- It does not create or open any PR, branch, commit, or migration.
- It does not change the production database, services, timers, thresholds, paper/live settings, or kill switch.
- It does not authorize anyone to do those things. Every PR described below requires separate explicit approval before any work begins.

## 1. End-state product loop
qualified wallet → specific source trade → persisted token/outcome mapping → persisted copy candidate → fresh copyability evaluation → exactly one traceable signal → manual paper-order preview → explicit manual approval → realistic paper fill → paper position and settlement. Paper pilot is NOT considered started until every link in this chain is proven by an evidence package.

## 2. Design principles
- One PR closes one well-defined gap.
- Each PR has a single acceptance test the reviewer can run.
- Each PR leaves the system in a state where every prior PR still works and the pilot is still safe.

## 2.1 Stable identity contract (applies to every PR)

- The schema's real uniqueness on `source_trades` is `UNIQUE(source, source_trade_id)`. `source_trade_id` is **NOT** globally unique — two providers can legitimately emit the same string.
- The stable upstream identity for any trade is the pair `(source, source_trade_id)`. Any resolver, candidate-key, signal-key, or idempotency check that reads or derives from a source trade MUST qualify by both fields unless it has switched to the internal `source_trades.id` UUID.
- Future copy-candidate idempotency (`PR 2`) MUST preserve both fields; the recommended key is `(wallet_id, source, source_trade_id)` so that two providers with the same `source_trade_id` but different `source` do not collide.
- The canonical helper `resolve_trade_to_outcome(db, *, source, source_trade_id)` enforces this contract today.
- Each PR must NOT approve orders, disable the kill switch, enable live trading, change broker_mode, or change paper_mode.
- All rejection decisions are logged via decision_log with typed decision_type strings.
- Idempotency is established at the earliest layer that has a stable identity (source_trades → copy_candidates → signals).
- No invented probability model. The product's measurable quantity is the trade's price movement between source trade and expected paper fill, plus spread, slippage, trade age, and remaining market duration. Use honest names reflecting what is actually measured (see §11).

## 3. PR sequence overview

| # | PR | Status (as of 2026-07-02) | Companion doc |
|---|---|---|---|
| 1 | Persist source-trade identity and outcome mapping | **MERGED + DEPLOYED** (`main` @ `fa3f2101`) | `smart_wallet_signal_path_audit.md` |
| 1.b | Align production code with additive schema v9 (bridge) | **DRAFT — PR #15** (`fix/align-production-schema-v9` @ `5c0a80a`) — CI green | `recovery_pr_sequence.md` §15 |
| 2 | Persist evaluated copy candidates | **MERGED + DEPLOYED** (`main` @ `3a3c03e`) | `copy_candidate_contract.md` |
| 3 | Persist side-aware candidate price snapshots | **DRAFT — IN FLIGHT** (`feat/candidate-price-snapshots`, uncommitted in worktree) | `candidate_price_snapshot_contract.md` |
| 4 | Build the real signal generator | NOT STARTED | — |
| 5 | Manual paper-order preview and approval path | NOT STARTED | — |
| 6 | Pilot-readiness verification | NOT STARTED | — |

## 4. PR 1 — Persist source-trade identity and outcome mapping

### 4.1 Purpose
Persist the stable identity needed to map a source trade to its market outcome, including the previously-discarded token id, so that downstream stages can join source_trades → market_outcomes unambiguously for both binary and multi-outcome markets.

### 4.2 Exact problem it closes
- `market_outcomes` has no `clob_token_id` column (audit §3.5, §3.9 row `token_id`).
- The token → outcome map (`asset_to_outcome_map`) is rebuilt in memory every scan and is never persisted (audit §3.5).
- Multi-outcome markets (sports, esports, elections) cannot be unambiguously joined today (audit §3.5).
- A specific source trade cannot be traced to a specific token for downstream stages without re-fetching Gamma.

### 4.3 Expected files to be modified
- `src/polycopy/db/schema.py` — add `market_outcomes.clob_token_id` (nullable TEXT); add `source_trades.token_id` (nullable TEXT) so the per-trade identity is recorded.
- `src/polycopy/db/market_persistence.py` — `persist_market_preserving_identity` writes `clob_token_id` from the parsed Gamma payload (zipping `clobTokenIds[i]` with `outcomes[i]`).
- `src/polycopy/adapters/polymarket.py` — `_persist_trade` reads the persisted token from the market row and stores it on the source_trades row.
- `tests/test_p01_clob_token_persistence.py` — verifies token is stored on `market_outcomes` and on `source_trades`.
- `tests/test_p01_multi_outcome_join.py` — verifies a sports multi-outcome trade now joins to exactly one `market_outcomes` row via token.
- `tests/test_p01_no_regression_binary.py` — verifies binary (Yes/No) markets still join correctly via label after the column is added.

### 4.4 Expected migration or schema changes
- Add nullable column `market_outcomes.clob_token_id TEXT` (no default; existing rows get NULL, will be backfilled lazily).
- Add nullable column `source_trades.token_id TEXT` (no default; existing rows get NULL, will be backfilled lazily).
- Add index `idx_market_outcomes_token` on `market_outcomes(clob_token_id)` (NULLs allowed).
- No DROP, no rename, no forced row migration beyond adding the two nullable columns.
- No data backfill in this PR (additive only; backfill job is a separate concern if/when needed).

### 4.5 Domain objects added or modified
- New Pydantic / dataclass fields on `MarketOutcome`: `clob_token_id: Optional[str]`.
- New field on `SourceTrade`: `token_id: Optional[str]`.
- No new top-level domain object.

### 4.6 Inputs and outputs
- Inputs: existing Gamma payloads (`clobTokenIds`, `outcomes`); existing data-api trade payloads (`conditionId`, `outcome`, `side`, `price`, `size`, `timestamp`, `proxyWallet`).
- Outputs: rows in `market_outcomes` (with `clob_token_id` populated) and `source_trades` (with `token_id` populated) so a SQL JOIN `source_trades.token_id = market_outcomes.clob_token_id` returns exactly one row per trade.
- Error modes: malformed `clobTokenIds` (length mismatch with `outcomes`) → log a decision_log entry `decision_type="unknown_outcome"`, skip the row, do NOT raise.

### 4.7 Persisted identifiers connecting to next stage
- `source_trades.token_id` and `market_outcomes.clob_token_id` are the bridge that PR 2 (copy-candidate persistence) consumes to materialize a `(wallet_id, source_trade_id, market_id, outcome_label, token_id)` tuple without re-parsing Gamma.

### 4.8 Tests required
- Unit: `clob_token_id` parsed correctly from a Gamma fixture with N outcomes.
- Unit: `_persist_trade` writes the correct `token_id` given a known market.
- Parser: malformed payload (`clobTokenIds` shorter than `outcomes`) is logged, not raised.
- Persistence: after ingestion, `SELECT 1 FROM source_trades st JOIN market_outcomes mo ON st.token_id = mo.clob_token_id WHERE st.market_source_id = ?` returns ≥1 row for a multi-outcome trade.
- Integration: a multi-outcome sports fixture that previously returned 0 join rows now returns 1.
- Negative: binary markets continue to join by label.

### 4.9 Exact acceptance test
1. Open a disposable DB (mirror of production schema + the new columns).
2. Run the new migration that adds `market_outcomes.clob_token_id` and `source_trades.token_id`.
3. Replay a recorded Gamma payload containing a sports multi-outcome market (≥3 outcomes, non-Yes/No labels).
4. Replay a recorded data-api trade payload for that market with `outcome="Seattle Mariners"`.
5. Run the canonical helper against the source-qualified identity, e.g. `resolve_trade_to_outcome(db, source="polymarket_data_api", source_trade_id="polymarket:<txhash>")`, and verify exactly one OK result with the correct `label` ("Seattle Mariners") and matching `token_id`. The acceptance query in the implementation guide is equivalent:
   ```sql
   SELECT mo.market_id, mo.label, mo.clob_token_id, st.source_trade_id, st.token_id
   FROM source_trades st
   JOIN market_outcomes mo
     ON st.market_source_id = mo.market_source_id
    AND (st.token_id = mo.clob_token_id OR st.outcome = mo.label)
   WHERE st.source = 'polymarket_data_api'
     AND st.source_trade_id = 'polymarket:<txhash>';
   ```
   The `AND st.source = ?` clause is **mandatory** — `source_trades` is unique on `(source, source_trade_id)`, not on `source_trade_id` alone.
6. Repeat with a binary market and confirm the label-based join still returns exactly one row.
7. (Cross-source regression) Insert two `source_trades` rows with the same `source_trade_id` under two different `source` values pointing at different outcomes. Confirm each `(source, source_trade_id)` resolves to only its own outcome.

### 4.10 Explicit out-of-scope items
- Any change to the scoring formula (`formula_version`), thresholds (`MAX_SHARPE`, `WEIGHTS`, `CRITICAL_FIELDS`, verdict boundaries), or kill switch.
- Any change to `_generate_signals` or `signals` table.
- Any backfill of historical `market_outcomes.clob_token_id` for rows ingested before this PR.
- Any change to `paper_mode`, `broker_mode`, `is_live`, timers, Caddy, systemd, Hermes, .env, credentials.
- Any new paper order, paper fill, or paper position.

### 4.11 Production behavior change
Zero. Production continues to call the existing scan/collect/health/settle timers. The placeholder signal generator is untouched. No rows are written to `signals`, `orders`, or `positions`. The new columns are nullable; existing rows and existing call paths are unaffected.

### 4.12 Rollback strategy
Additive only — `git revert` of the merge commit removes the migration. Because both columns are nullable and no destructive change was made, rollback does not require any DB cleanup beyond dropping the two added columns and the index (the migration is forward-only and reversible by a new down-migration if ever needed; no data loss either way).

### 4.13 Whether production services need restarting
NO. Schema is additive (nullable columns + nullable index); the running Python process continues to read and write the existing columns unchanged. A restart is not required to pick up the new columns for any current code path.

### 4.14 Whether existing timers should remain enabled
YES. `collect`, `scan`, `health`, `settle`, `update`, `pilot-report` timers remain enabled and unchanged. Do not stop or trigger any of them.

### 4.15 Whether the PR can be safely merged independently
YES. The PR is purely additive, the acceptance test passes against a disposable DB, no production code path is altered, no settings change, no destructive migration.

## 5. PR 2 — Persist evaluated copy candidates

### 5.1 Purpose
Persist an evaluated `(wallet_id, source_trade_id, market_id, outcome_label, token_id, side, observed_trade_price, observed_trade_ts)` tuple so downstream stages have a stable, deduped input row that survives between scans.

### 5.2 Exact problem it closes
- No copy-candidate domain object or table exists (audit §3.3, §3.9 gap 2).
- Wallet scoring produces a verdict in memory and discards it (audit §3.3).
- Re-running `_generate_signals` inserts fully-duplicate rows (audit §3.11 gap 4).
- There is no intermediate `(wallet, trade, market, outcome)` tuple anywhere in the schema (audit §3.11 gap 2).

### 5.3 Expected files to be modified
- `src/polycopy/db/schema.py` — add new `copy_candidates` table.
- `src/polycopy/domain/copyability.py` or a new `src/polycopy/domain/copy_candidate.py` — add `CopyCandidate` Pydantic/dataclass model.
- `src/polycopy/engine/evaluate.py` — emit candidates after `score_wallet` returns `CopyabilityScore` with verdict `COPY_CANDIDATE`.
- `src/polycopy/scoring/engine.py` — minor: expose `formula_version` and `verdict` already present (no logic change).
- A new module `src/polycopy/engine/candidate_persistence.py` (or function inside `evaluate.py`) that maps each new source trade of a COPY_CANDIDATE wallet to a row.
- `tests/test_p02_copy_candidate_unique.py` — verifies `UNIQUE(wallet_id, source_trade_id)` enforcement.
- `tests/test_p02_candidate_idempotent.py` — re-running materialization on the same inputs is a no-op.

### 5.4 Expected migration or schema changes
- New table `copy_candidates` with columns:
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `wallet_id` TEXT NOT NULL REFERENCES `wallets(id)`
  - `source_trade_id` TEXT NOT NULL
  - `source` TEXT NOT NULL (default `'polymarket'`)
  - `market_id` TEXT NOT NULL REFERENCES `markets(id)`
  - `outcome_label` TEXT NOT NULL
  - `token_id` TEXT
  - `side` TEXT NOT NULL
  - `observed_trade_price` REAL NOT NULL
  - `observed_trade_quantity` REAL
  - `observed_trade_ts` TEXT NOT NULL (ISO8601 UTC)
  - `wallet_score_version` TEXT NOT NULL
  - `wallet_score` REAL NOT NULL
  - `wallet_verdict` TEXT NOT NULL
  - `status` TEXT NOT NULL DEFAULT 'pending' (pending / accepted / rejected / stale / failed_mapping)
  - `rationale` TEXT
  - `produced_at` TEXT NOT NULL
  - `expires_at` TEXT
  - `is_sample` INTEGER NOT NULL DEFAULT 0
  - `UNIQUE(wallet_id, source_trade_id)` constraint
  - `INDEX(wallet_id, status)`, `INDEX(market_id)`
- No destructive migration. No DROP. No rename of any existing column.

### 5.5 Domain objects added or modified
- New `CopyCandidate` Pydantic model mirroring the table columns.
- New `CopyCandidateStatus` literal type (`'pending' | 'accepted' | 'rejected' | 'stale' | 'failed_mapping'`).
- No modification of existing `CopyabilityScore` or `Verdict` types.

### 5.6 Inputs and outputs
- Inputs: `CopyabilityScore` for a wallet; the wallet's source_trades rows since last materialization.
- Outputs: zero or more `copy_candidates` rows (one per source_trade_id) inserted with `INSERT OR IGNORE` so reruns are safe.
- Error modes: missing wallet, missing source_trade, missing market join → no row inserted; emit a `decision_log` entry with `decision_type="incomplete_data"` or `"unknown_outcome"`.

### 5.7 Persisted identifiers connecting to next stage
- `copy_candidates.id` (PK) and `(wallet_id, source_trade_id)` (unique key) are the stable inputs to PR 3 (fresh current-price refresh) and PR 4 (real signal generator).
- `copy_candidates.token_id`, `copy_candidates.market_id`, `copy_candidates.outcome_label`, `copy_candidates.side`, `copy_candidates.observed_trade_price` are the exact columns PR 3 reads.

### 5.8 Tests required
- Unit: building a `CopyCandidate` from a `(CopyabilityScore, SourceTrade, Market, MarketOutcome)` tuple produces the expected fields.
- Persistence: `INSERT OR IGNORE` on the same `(wallet_id, source_trade_id)` twice yields exactly one row.
- Persistence: FK violations on `wallet_id` or `market_id` raise.
- Integration: end-to-end materialization over a wallet with N trades produces N rows (no more, no less) and a second run is a no-op.
- Negative: source_trades with `token_id IS NULL` (pre-PR-1 rows) are NOT inserted into `copy_candidates`; emit `decision_log` with `decision_type="unknown_outcome"`.

### 5.9 Exact acceptance test
1. Open a disposable DB with PR 1 already applied.
2. Insert one COPY_CANDIDATE wallet, three markets (one binary, one multi-outcome sports, one closed), and six source_trades (two per market).
3. Run the candidate materialization function once. Verify exactly 6 rows in `copy_candidates`, all `status='pending'`.
4. Run the same function again. Verify still exactly 6 rows, no error, no duplicate.
5. Manually `INSERT INTO copy_candidates (wallet_id, source_trade_id, …)` a row that violates the unique constraint and verify the DB raises.
6. Verify a `decision_log` row exists for any pre-PR-1 source_trade with `decision_type='unknown_outcome'`.

### 5.10 Explicit out-of-scope items
- Computing or storing any edge / price_movement / copy_cost (PR 3's job).
- Refreshing current price (PR 3's job).
- Any change to `signals` table or `_generate_signals`.
- Any change to thresholds, kill switch, paper_mode, broker_mode, is_live, timers.
- Backfilling `market_outcomes.clob_token_id` for pre-PR-1 rows.

### 5.11 Production behavior change
Zero. The new function writes only to `copy_candidates` and `decision_log`. No existing call path is invoked by production timers; the function is only runnable via a new explicit script path that PR 2 itself does not yet schedule. The existing `_generate_signals` continues to be the only writer to `signals` and is untouched.

### 5.12 Rollback strategy
`git revert` removes the table via a down-migration (DROP TABLE). Because no production code path populates `copy_candidates` between merge of PR 2 and merge of PR 3, the table will be empty in production unless someone has run the new script manually — in which case the down-migration's behavior is well-defined: drop the empty (or small) table cleanly. No other table is touched.

### 5.13 Whether production services need restarting
NO. New table is additive; existing services do not read or write it. A restart is not required to keep the running process consistent.

### 5.14 Whether existing timers should remain enabled
YES. Existing timers remain enabled and unchanged. PR 2 introduces no new scheduled job.

### 5.15 Whether the PR can be safely merged independently
YES. The PR is purely additive, the acceptance test passes against a disposable DB, and no existing call path is altered.

## 6. PR 3 — Obtain fresh current-price and slippage inputs

### 6.1 Purpose
For each `copy_candidates` row, fetch a fresh current price (and ideally bid/ask) at the time the candidate is evaluated, and compute expected fill price + estimated slippage, persisting the observations so PR 4 has auditable inputs.

### 6.2 Exact problem it closes
- `_generate_signals` uses the scan-time `market_outcomes.price`, which is unbounded in staleness (audit §3.4 rows "current market price", "current spread"; §3.10 gate 6).
- No bid/ask is stored anywhere (audit §3.6).
- `providers/bidask.py` exists but is never invoked from the signal path (audit §3.6).
- `risk/fill_model.py` exists but is never invoked (audit §3.6).
- Liquidity / depth data is unavailable (audit §3.4 row "liquidity / depth").

### 6.3 Expected files to be modified
- `src/polycopy/db/schema.py` — add `price_observations` table (see §6.4).
- `src/polycopy/providers/bidask.py` — minimal wiring: add a function `fetch_book(token_id) -> BookSnapshot` that returns `{best_bid, best_ask, midpoint, depth_bid, depth_ask, observed_at}`. (Function may already exist; this PR verifies it and adds missing error handling if needed — no behavior change for code paths outside the candidate refresh.)
- `src/polycopy/risk/fill_model.py` — verify `expected_fill_price(side, book, size)` and `estimated_slippage(...)` functions are REAL (not `return 0.0` / `pass`). If they are placeholders, flag them in PR 3 review and route to a minimal honest implementation. (Per §10, several functions here are placeholders despite production-looking names.)
- A new module `src/polycopy/engine/price_refresh.py` — fetches a fresh observation per candidate and persists it.
- `tests/test_p03_price_observation_recorded.py` — verifies observation stored.
- `tests/test_p03_fill_price_uses_book.py` — verifies expected fill is derived from book (not from stale scan-time price).

### 6.4 Expected migration or schema changes
- New table `price_observations`:
  - `id` INTEGER PRIMARY KEY AUTOINCREMENT
  - `market_id` TEXT NOT NULL REFERENCES `markets(id)`
  - `outcome_label` TEXT NOT NULL
  - `token_id` TEXT
  - `best_bid` REAL
  - `best_ask` REAL
  - `midpoint` REAL
  - `depth_bid` REAL
  - `depth_ask` REAL
  - `observed_at` TEXT NOT NULL
  - `source` TEXT NOT NULL DEFAULT 'bidask_provider'
  - `INDEX(market_id, outcome_label, observed_at)`
- Add nullable columns on `copy_candidates`:
  - `price_observation_id` INTEGER REFERENCES `price_observations(id)`
  - `expected_fill_price` REAL
  - `estimated_slippage` REAL
  - `price_movement` REAL (signed: source_trade_price − current_midpoint_or_ask)
  - `copy_cost` REAL (signed: expected_fill_price − source_trade_price)
  - `refresh_attempted_at` TEXT
  - `refresh_failed_reason` TEXT
- No destructive migration. No DROP. No rename.

### 6.5 Domain objects added or modified
- New `PriceObservation` Pydantic model.
- New `BookSnapshot` value object in `providers/bidask.py` (or Pydantic if cleaner).
- New `FillEstimate` value object in `risk/fill_model.py`.
- `CopyCandidate` gains the four nullable fields above.

### 6.6 Inputs and outputs
- Inputs: a list of `copy_candidates` rows with `status='pending'`, `token_id IS NOT NULL`, and `expires_at > now`.
- Outputs: per candidate, one new `price_observations` row; the candidate's `price_observation_id`, `expected_fill_price`, `estimated_slippage`, `price_movement`, `copy_cost` populated. On fetch failure, populate `refresh_failed_reason` and emit `decision_log` with `decision_type="price_refresh_failed"`.
- Error modes: HTTP timeout → log + skip; malformed book → log + skip; both are recoverable on next refresh.

### 6.7 Persisted identifiers connecting to next stage
- `copy_candidates.price_observation_id`, `expected_fill_price`, `estimated_slippage`, `price_movement`, `copy_cost` are the exact inputs PR 4's signal generator reads.
- `price_observations.id` is referenced by `copy_candidates.price_observation_id`.

### 6.8 Tests required
- Unit: `fetch_book(token_id)` against a fixture returns a `BookSnapshot` with the expected fields.
- Unit: `expected_fill_price(side='buy', book, size)` increases with size and spread (monotonicity).
- Unit: `price_movement = source_trade_price − midpoint` (signed).
- Persistence: `price_observations` row is written before `copy_candidates` fields are populated (transaction order).
- Integration: refresh fails gracefully when bidask endpoint returns 5xx — `refresh_failed_reason` populated, candidate still exists, no exception.
- Negative: candidate with `token_id IS NULL` is not refreshed (skipped).

### 6.9 Exact acceptance test
1. Open a disposable DB with PR 1 + PR 2 applied and a candidate seeded.
2. Run the price-refresh function. Verify a new `price_observations` row exists with non-null `midpoint` (or null with `refresh_failed_reason` set on failure path).
3. Verify the candidate's `price_movement`, `copy_cost`, `expected_fill_price`, `estimated_slippage` are populated (or `refresh_failed_reason` is set).
4. Verify a `decision_log` row exists with `decision_type='price_refresh_failed'` when the bidask endpoint is stubbed to 5xx.
5. Verify the function is idempotent — re-running updates the `price_observation_id` reference (or adds a new observation row) without raising or duplicating.

### 6.10 Explicit out-of-scope items
- Computing any acceptance / rejection decision (PR 4's job).
- Changing `signals` table or `_generate_signals`.
- Optimizing scan timeouts.
- Changing the scoring formula or thresholds.
- Changing `paper_mode`, `broker_mode`, `is_live`, kill switch, timers, Caddy, systemd, Hermes, .env, credentials.

### 6.11 Production behavior change
Zero. The new module runs only when explicitly invoked (a new script). No production timer is scheduled to call it. Production behavior is identical to pre-PR-3 until PR 4 wires the signal generator to consume the refreshed observations.

### 6.12 Rollback strategy
Additive. `git revert` removes the table and nullable columns via a down-migration. Any rows in `price_observations` and any populated nullable `copy_candidates` columns are dropped — no data loss elsewhere.

### 6.13 Whether production services need restarting
NO. New table is additive; existing services do not read or write `price_observations` or the new `copy_candidates` columns. A restart is not required.

### 6.14 Whether existing timers should remain enabled
YES. Existing timers remain enabled and unchanged. PR 3 introduces no new scheduled job.

### 6.15 Whether the PR can be safely merged independently
YES. Purely additive; no existing call path is altered; the acceptance test passes against a disposable DB; no production settings change.

## 7. PR 4 — Build the real signal generator

### 7.1 Purpose
Replace the placeholder `_generate_signals` with a real generator that consumes `copy_candidates` (post-PR-3 refresh) and emits one `signals` row per accepted candidate, using real `price_movement` and `copy_cost` values and respecting idempotency.

### 7.2 Exact problem it closes
- `signals` table has no unique constraint; re-running inserts duplicates (audit §3.11 gap 4, §3.10 gate 11).
- All edge / confidence / predicted_prob / market_prob values are placeholders (audit §3.6, §3.11 gap 5).
- Placeholder `_generate_signals` ignores wallet verdicts entirely (audit §3.3).
- Strength is always "buy" (audit §3.6 row `strength`).

### 7.3 Expected files to be modified
- `src/polycopy/db/schema.py` — add `idempotency_key` column on `signals` with `UNIQUE` constraint; add `status`, `expires_at`, `wallet_id`, `source_trade_id`, `outcome_label`, `token_id`, `side`, `source_trade_price`, `observed_current_price`, `expected_fill_price`, `estimated_slippage`, `remaining_edge`, `price_movement`, `copy_cost`, `wallet_score_version`, `wallet_score`, `wallet_verdict`, `refresh_attempted_at` columns. All nullable for backward compatibility; do NOT drop existing columns.
- `scripts/run_scan.py` — replace the placeholder `_generate_signals` body with a real generator that:
  1. Reads `copy_candidates` with `status='pending'` and `expires_at > now`.
  2. Filters by eligibility gates (see §7.5).
  3. Computes `idempotency_key = sha256(f"{wallet_id}:{source_trade_id}")`.
  4. Inserts with `INSERT OR IGNORE`.
- `src/polycopy/scoring/engine.py` — no change to formula or thresholds; only expose `formula_version`, `verdict`, `score` for emission.
- A new module `src/polycopy/engine/signal_generator.py` — the real generator (keeps run_scan thin).
- `tests/test_p04_signal_idempotent.py` — verifies `UNIQUE(idempotency_key)`.
- `tests/test_p04_signal_uses_real_price.py` — verifies `expected_fill_price` and `price_movement` come from `copy_candidates`, not from `market_outcomes.price` at scan time.
- `tests/test_p04_signal_wallet_gated.py` — verifies only `COPY_CANDIDATE` wallets produce signals.

### 7.4 Expected migration or schema changes
- Add nullable column `signals.idempotency_key TEXT` with `UNIQUE` constraint.
- Add nullable columns: `signals.wallet_id TEXT`, `signals.source_trade_id TEXT`, `signals.outcome_label TEXT`, `signals.token_id TEXT`, `signals.side TEXT`, `signals.source_trade_price REAL`, `signals.observed_current_price REAL`, `signals.expected_fill_price REAL`, `signals.estimated_slippage REAL`, `signals.remaining_edge REAL`, `signals.price_movement REAL`, `signals.copy_cost REAL`, `signals.wallet_score_version TEXT`, `signals.wallet_score REAL`, `signals.wallet_verdict TEXT`, `signals.refresh_attempted_at TEXT`, `signals.status TEXT`, `signals.expires_at TEXT`, `signals.copy_candidate_id INTEGER REFERENCES copy_candidates(id)`.
- All columns nullable for backward compatibility with placeholder rows.
- No DROP. No rename. No forced row migration beyond adding nullable columns.
- IMPORTANT: existing placeholder rows in `signals` (if any) are left untouched. The new generator only inserts new rows with the new columns populated.

### 7.5 Domain objects added or modified
- New `Signal` Pydantic model with the new fields.
- New `SignalStatus` literal type (`'pending' | 'approved' | 'rejected' | 'filled' | 'expired'`).
- `CopyabilityScore` is unchanged (read-only here).
- `_generate_signals` in `run_scan.py` is replaced (the placeholder is documented as a placeholder; this PR does the real replacement).

### 7.6 Inputs and outputs
- Inputs: rows from `copy_candidates` with `status='pending'`, `refresh_failed_reason IS NULL`, `expires_at > now`, joined to `wallets` (verdict = `COPY_CANDIDATE`), joined to `price_observations` (latest per market/outcome).
- Outputs: zero or more new `signals` rows. Each row has `idempotency_key` populated, `status='pending'`, real `price_movement`, `copy_cost`, `expected_fill_price`, `estimated_slippage`. Reruns are no-ops.
- Eligibility gates applied (in this order, with the corresponding `decision_log.decision_type`):
  1. Wallet verdict = COPY_CANDIDATE; otherwise `wallet_no_longer_qualifies`.
  2. Market not closed, not resolved; otherwise `market_closed` / `market_resolved`.
  3. Source trade not stale (compared to `settings.staleness_seconds`); otherwise `stale` and the candidate is marked `stale`.
  4. Current price observable (refresh_attempted_at set, refresh_failed_reason null); otherwise skip silently (PR 3 will retry).
  5. Liquidity check: `markets.volume_24h >= settings.min_volume_24h` (existing gate, threshold unchanged); otherwise `insufficient_liquidity`.
  6. Spread check: if best_bid and best_ask present, `(best_ask - best_bid) <= settings.max_spread`; otherwise `excessive_spread`.
  7. Remaining edge check: `copy_cost <= settings.max_copy_cost` (signed; "still profitable enough"); otherwise `insufficient_edge`.
  8. Idempotency: `INSERT OR IGNORE` on `idempotency_key`; on conflict, no decision_log entry (silent dedup).

### 7.7 Persisted identifiers connecting to next stage
- `signals.id` (PK) and `signals.idempotency_key` are the inputs to PR 5's manual paper-order preview path.
- `signals.copy_candidate_id` joins back to `copy_candidates.id`.
- `signals.wallet_id`, `signals.source_trade_id`, `signals.market_id`, `signals.token_id`, `signals.outcome_label`, `signals.side`, `signals.source_trade_price`, `signals.expected_fill_price`, `signals.estimated_slippage` are the inputs to the broker preview.

### 7.8 Tests required
- Unit: `idempotency_key = sha256(f"{wallet_id}:{source_trade_id}")` matches a known fixture.
- Unit: each eligibility gate produces the expected `decision_log.decision_type` and `copy_candidates.status` transition.
- Persistence: inserting a signal with a duplicate `idempotency_key` is silently ignored.
- Integration: a candidate that passes all gates produces exactly one signal on first run and zero on the second.
- Integration: a candidate that fails the spread gate produces zero signals and a `decision_log` row with `decision_type='excessive_spread'`.
- Negative: a wallet with verdict `WATCHLIST` produces zero signals.
- Negative: the placeholder `_generate_signals` (if invoked) does not produce new-style rows; the new generator is the only path that writes the new columns.

### 7.9 Exact acceptance test
1. Open a disposable DB with PR 1–3 applied.
2. Seed one COPY_CANDIDATE wallet, one pending candidate with refresh fields populated, one price_observation.
3. Run the new generator once. Verify exactly 1 row in `signals` with `status='pending'`, real `price_movement`, real `copy_cost`, real `expected_fill_price`.
4. Run the generator again. Verify still exactly 1 row (idempotency).
5. Seed a candidate with `markets.closed=1` and verify a `decision_log` row with `decision_type='market_closed'` and zero new signals.
6. Seed a candidate with an excessive spread and verify `decision_type='excessive_spread'` and zero new signals.
7. Verify `decision_log` is populated for every rejection path tested.
8. Verify `signals.idempotency_key` is the sha256 of `f"{wallet_id}:{source_trade_id}"` for each accepted signal.

### 7.10 Explicit out-of-scope items
- Manual approval flow (PR 5's job).
- Pilot-readiness evidence package (PR 6's job).
- Changing the scoring formula, thresholds, kill switch, paper_mode, broker_mode, is_live, timers, Caddy, systemd, Hermes, .env, credentials.
- Dropping `market_outcomes.volume`.
- Dashboard changes.
- Alerts / webhooks.

### 7.11 Production behavior change
Production now emits real signals — but ONLY when the new generator is invoked by a new script. This PR does NOT yet wire the new generator into an existing timer. Production signal flow continues to use the placeholder `_generate_signals` (which produces zero signals today because the `outcome.volume >= 10000` gate is always 0). After PR 4 merges, a separate explicit step — gated by separate explicit approval — is required to switch the scan pipeline over to the new generator.

Until that switch happens (which is PR 5's territory or a later step), production behavior is unchanged.

### 7.12 Rollback strategy
Additive columns + UNIQUE constraint + new generator function behind a feature flag (or behind a new script path not yet wired into timers). `git revert` of the merge commit removes the generator and the down-migration drops the added columns and UNIQUE constraint. Existing placeholder `signals` rows remain valid (all new columns nullable).

### 7.13 Whether production services need restarting
NO for additive columns. The new generator runs only from the new script path; existing services are unaffected. (If a future step wires the new generator into an existing timer, that is a separate PR and explicitly out-of-bundle here.)

### 7.14 Whether existing timers should remain enabled
YES. Existing timers remain enabled and continue to invoke the placeholder `_generate_signals` (which produces zero signals in production today). PR 4 does not rewire any timer.

### 7.15 Whether the PR can be safely merged independently
YES. The new generator is additive behind a new script path. The placeholder `_generate_signals` continues to run unchanged in production. Idempotency test passes. No settings change.

## 8. PR 5 — Manual paper-order preview and approval path

### 8.1 Purpose
Provide an explicit, manual approval path from a `signals` row to a `PaperBroker.place_order` call, with a kill-switch gate and a full preview of every trade field, so that paper positions are created only when a human has reviewed and approved the trade.

### 8.2 Exact problem it closes
- `paper_mode=paper_manual` is set but no path produces a paper-order preview from a real signal (audit §3.1 row 10).
- No link from a `signals` row to a `orders` row exists today (audit §3.1 rows 10–11).
- The kill switch is not consulted from any signal-to-order path (audit §3.10 gate 10).

### 8.3 Expected files to be modified
- `src/polycopy/adapters/paper_broker.py` — verify `PaperBroker.place_order` is REAL (per §10, several functions there are placeholders despite production-looking names). If it is a placeholder, replace with an honest implementation that writes an `orders` row with `signal_id` FK and a filled price derived from `expected_fill_price + estimated_slippage`.
- `src/polycopy/engine/approval.py` (new) — function `preview(signal_id) -> PreviewDict` and function `approve(signal_id, approver_id) -> Order`. Both must check `settings.order_kill_switch` first.
- `src/polycopy/api/app.py` — add a single endpoint `POST /preview` and `POST /approve` (read-only / state-changing respectively). Wire to `approval.preview` / `approval.approve`.
- `src/polycopy/risk/portfolio_gate.py` (new or extracted from existing risk code) — read-only portfolio check that returns `{ok, reason}`; not enforced as a hard gate in this PR, only surfaced in the preview.
- `tests/test_p05_kill_switch_blocks.py` — negative test.
- `tests/test_p05_approval_creates_one_order.py` — positive test (with `paper_broker.PaperBroker` verified not to import a Polymarket key).

### 8.4 Expected migration or schema changes
- No new tables.
- Add nullable column `orders.approved_by TEXT`, `orders.approved_at TEXT`, `orders.kill_switch_state TEXT` (so the order row records that the kill switch was checked at approval time).
- No DROP. No rename. No forced row migration.

### 8.5 Domain objects added or modified
- New `PreviewDict` value object with every field a human would need to review.
- New `ApprovalRequest` Pydantic model.
- `PaperBroker.place_order` may be replaced if currently a placeholder (see §10).

### 8.6 Inputs and outputs
- `preview(signal_id)` input: `signal_id` (UUID). Output: dict with `wallet_id`, `market_id`, `outcome_label`, `side`, `source_trade_price`, `expected_fill_price`, `estimated_slippage`, `price_movement`, `copy_cost`, `wallet_score`, `wallet_verdict`, `wallet_score_version`, `kill_switch_state`, `portfolio_check`, `idempotency_key`. Error: 404 if `signal_id` not found; 409 if `signal.status != 'pending'`.
- `approve(signal_id, approver_id)` input: `signal_id`, `approver_id`. Output: a new `orders` row. Behavior: if `settings.order_kill_switch=True`, returns 409 with reason `kill_switch_enabled` and emits `decision_log` with `decision_type='kill_switch_blocked'`. Otherwise, calls `PaperBroker.place_order`, sets `signal.status='approved'` then `'filled'`, and emits `decision_log` with `decision_type='open_position'`.

### 8.7 Persisted identifiers connecting to next stage
- `orders.id` (PK) and `orders.signal_id` (FK) are the inputs to PR 6's verification step and to existing settlement logic in `risk/settlement.py`.

### 8.8 Tests required
- Unit: `preview` returns the expected dict shape for a known signal.
- Unit: `approve` with `order_kill_switch=True` returns 409 and emits `decision_log.decision_type='kill_switch_blocked'`.
- Unit: `approve` with `order_kill_switch=False` calls `PaperBroker.place_order` exactly once.
- Integration: a full happy path from `preview` → `approve` → `PaperBroker.place_order` produces exactly one `orders` row, one `signals.status='filled'`, one `decision_log` row with `decision_type='open_position'`.
- Negative: `approve` of the same `signal_id` twice is rejected (idempotency).
- Negative: `PaperBroker` does not import or reference any Polymarket private key or live client.

### 8.9 Exact acceptance test
1. Open a disposable DB with PR 1–4 applied and one `signals` row with `status='pending'`.
2. Call `preview(signal_id)`. Verify every field listed in §8.6 is present.
3. With `settings.order_kill_switch=True`, call `approve(signal_id, 'reviewer1')`. Verify 409, no `orders` row, `decision_log` row with `decision_type='kill_switch_blocked'`.
4. Flip `settings.order_kill_switch=False` (in the disposable env only), call `approve(signal_id, 'reviewer1')`. Verify exactly one `orders` row with `signal_id` matching, `signal.status='filled'`, `decision_log` row with `decision_type='open_position'`.
5. Call `approve(signal_id, 'reviewer2')` again. Verify 409 (idempotency).
6. Verify no `POLYMARKET_PRIVATE_KEY` or live-client reference exists in `PaperBroker` source.

### 8.10 Explicit out-of-scope items
- Auto-approval (any path that bypasses human approval).
- Disabling the kill switch.
- Enabling live trading, `broker_mode=polymarket`, or `is_live=True`.
- Any threshold change.
- Settlement changes (existing settlement logic is untouched).
- Dashboard redesign (only minimal API additions).
- Alerts / webhooks.

### 8.11 Production behavior change
Until the new approval path is invoked, production behavior is unchanged. After explicit approval and PR-5 deployment, a human can approve a signal and create a paper order — but ONLY after a separate explicit decision to invoke the new approval endpoint. No timer schedules approvals.

### 8.12 Rollback strategy
`git revert` removes the API endpoints, the approval module, and the added nullable columns on `orders`. No existing code path is altered.

### 8.13 Whether production services need restarting
NO. The new endpoints are additive on the existing API process; the running process does not need to be restarted to keep existing behavior consistent. (A restart may be desired to pick up the new endpoints, but is not required for safety.)

### 8.14 Whether existing timers should remain enabled
YES. Existing timers remain enabled. No timer is modified to invoke the approval path.

### 8.15 Whether the PR can be safely merged independently
YES. The approval path is additive; the kill switch is enforced; no auto-approval; no live broker; no production settings change.

## 9. PR 6 — Pilot-readiness verification

### 9.1 Purpose
Produce a single evidence package proving every link in the end-state product loop (§1) works end-to-end on a disposable environment, with no production calls, so the human owner can decide whether to declare the paper pilot ready.

### 9.2 Exact problem it closes
There is currently no evidence package proving the full chain works. PRs 1–5 add the building blocks; PR 6 proves they compose into the loop.

### 9.3 Expected files to be modified
- `scripts/pilot_readiness_check.py` (new) — runs the full chain in a disposable environment and writes an evidence package.
- Reports dir (new file under existing untracked audit dir) — the generated evidence package.
- `scripts/paper_pilot_status.py` — minor: extend `pilot_status_latest.txt` schema to include the new chain steps when present (do not remove existing fields).
- `tests/test_p06_chain_end_to_end.py` — runs the chain in a test DB and asserts each step's outputs.

### 9.4 Expected migration or schema changes
None. PR 6 is verification only.

### 9.5 Domain objects added or modified
- A `PilotReadinessReport` value object (in the new script) that collects evidence per stage.
- No modifications to existing domain objects.

### 9.6 Inputs and outputs
- Inputs: a disposable DB built fresh; a recorded set of fixtures (markets, wallets, source_trades); a recorded bidask stub.
- Outputs: a markdown evidence package containing: one row per stage with timestamps, row counts, sample IDs (truncated hashes), and pass/fail; a kill-switch negative test result; an auto-approval-absence test result; a "no live broker in memory" test result.

### 9.7 Persisted identifiers connecting to next stage
None. PR 6 is the final PR. There is no "next stage" in this sequence.

### 9.8 Tests required
- All PR 1–5 acceptance tests run as part of the chain.
- Chain-level test: starting from an empty DB, run ingestion → candidate materialization → price refresh → signal generation → approval → fill, and verify each stage's persisted evidence matches the stage's acceptance test.
- Negative tests: kill switch blocks approval; auto-approval is not invoked; live broker is not loaded.

### 9.9 Exact acceptance test
1. Run `scripts/pilot_readiness_check.py` against a fresh disposable DB.
2. Verify the script produces an evidence package with the following sections, each populated:
   - One `wallet_id` with verdict `COPY_CANDIDATE` and `formula_version="v1"`.
   - One `source_trades.source_trade_id` linked to that wallet.
   - One `market_outcomes` row with `clob_token_id` populated (PR 1 evidence).
   - One `copy_candidates` row with `status='pending'` and `token_id` populated (PR 2 evidence).
   - One `price_observations` row, candidate's `expected_fill_price`/`estimated_slippage`/`price_movement`/`copy_cost` populated (PR 3 evidence).
   - One `signals` row with `idempotency_key`, `status='filled'`, real `price_movement`/`copy_cost` (PR 4 evidence).
   - One `orders` row with `signal_id` matching the signal (PR 5 evidence).
   - One `decision_log` row per stage.
   - Kill-switch negative test: with `order_kill_switch=True`, `approve` is blocked and a `decision_log` row exists with `decision_type='kill_switch_blocked'`.
   - Auto-approval absence test: no code path in the chain invokes `approve` automatically (verified by reading `run_scan` and the candidate materialization code paths).
   - Live broker absence test: `PaperBroker` source does not import or reference any Polymarket live client or private key.

### 9.10 Explicit out-of-scope items
- Any change to the scoring formula, thresholds, kill switch, paper_mode, broker_mode, is_live, timers, Caddy, systemd, Hermes, .env, credentials.
- Any production DB write.
- Any production timer invocation.
- Any auto-approval.
- Any new alert or webhook.
- Dashboard redesign beyond the minimal `pilot_status_latest.txt` extension.

### 9.11 Production behavior change
Zero. PR 6 is verification only; nothing it produces is wired into production.

### 9.12 Rollback strategy
`git revert` of the merge commit removes the new script and any minor additions to `paper_pilot_status.py`. No destructive change.

### 9.13 Whether production services need restarting
NO.

### 9.14 Whether existing timers should remain enabled
YES.

### 9.15 Whether the PR can be safely merged independently
YES — but its value depends on PRs 1–5 already being merged. If merged before PRs 1–5, the readiness check will fail and the evidence package will report the missing pieces. That is acceptable (the script is meant to surface gaps).

## 10. Critical design questions

- **Exact stable upstream source-trade identifier.** Audit §3.7 confirms `source_trades.source_trade_id` of form `polymarket:<txhash>` — globally unique per source. Confirmed by the existing `UNIQUE(source, source_trade_id)` index and `INSERT OR IGNORE` semantics in `_persist_trade`. The exact format (`polymarket:<txhash>`) is a textual convention observed in the data — to be re-verified against `adapters/polymarket.py` and `scripts/_live_ingest.py` during PR 1 implementation. **EVIDENCE MISSING — requires PR 1 to fully verify the prefix string convention by file/line.**

- **Globally unique vs unique-within-source for source_trade_id.** Audit §3.7: the UNIQUE constraint is on `(source, source_trade_id)`, so uniqueness is scoped to `source` (Polymarket today). Across sources, a txhash collision is theoretically possible but not in practice for a single-deployment paper pilot. Recommendation: treat as unique-within-source; the `(source, source_trade_id)` tuple is the true identity.

- **Does source_trades already persist wallet_id/token_id/market_source_id/price/quantity/side/timestamp?** Audit §3.5 + §3.4 confirm `market_source_id` (conditionId), `outcome`, `side`, `price`, `quantity`, `timestamp` are already persisted. `wallet_id` (FK to `wallets.id`) is NOT directly on `source_trades` — `trader_address` is the canonical lowercase address and the join to `wallets.id` is by canonical address. `token_id` is NOT persisted (audit §3.5). PR 1 adds `token_id`; PR 2 derives `wallet_id` via canonical join.

- **Correct uniqueness key for copy_candidates.** Audit §3.7 recommends `UNIQUE(wallet_id, source_trade_id)`. PR 2 uses this.

- **Can one source_trade generate more than one candidate?** Default: NO. Same trade → same wallet → same `source_trade_id` → exactly one candidate. If a wallet appears under two canonical addresses, `canonical_wallet_address()` collapses them first (audit §3.7). EVIDENCE MISSING for the exact location of `canonical_wallet_address` — requires PR 2 to confirm.

- **Correct uniqueness key for signals.** Audit §3.7 recommends `idempotency_key = hash(wallet_id, source_trade_id)` with `UNIQUE`. PR 4 uses this.

- **Rejected source_trades → candidate row with status, or decision_log only?** PR 2 recommendation: candidate rows for accepted + soft-rejected (stale, price-moved, refresh-failed) with status set to `stale`/`failed_mapping`; decision_log entries for hard-rejected (unknown outcome, insufficient liquidity, excessive spread, insufficient edge, market closed, market resolved, wallet no longer qualifies, incomplete data, kill switch blocked, duplicate, portfolio risk rejection). Both layers are populated in their respective PRs. This split makes the candidate table a single source of truth for "trades we've evaluated" and decision_log a single source of truth for "evaluations we've explained".

- **Current-price observations: candidate / signal / separate table?** PR 3 recommendation: separate `price_observations` table keyed by `(market_id, outcome_label, observed_at)` with TTL-style retention (no auto-pruning in this PR — older observations are simply not joined to by fresh candidates). Rationale: (a) a single observation can be reused by multiple candidates (e.g., 100 candidates on the same market); storing it once avoids N API calls and N denormalized rows; (b) historical observations are auditable; (c) separating observations from signals avoids mutating `signals` rows when a price refresh happens later.

- **What does "remaining copy edge" mean without an independent probability model?** It does not. Audit §3.6 confirms there is no real probability model. PR 4 replaces the placeholder `edge` with the honest measurement `price_movement` (signed) and the derived `copy_cost` (signed). No `predicted_prob`, `model_prob`, or `expected_value` is defined in this PR sequence.

- **Rename "edge" → ?** PR 4 + §11 below: rename to `price_movement` and `copy_cost`. The DB column formerly named `edge_estimate` keeps its name but its semantics change to `price_movement`; a code comment explains the rename.

- **Which existing risk and fill-model functions are trustworthy to reuse?** EVIDENCE MISSING — requires PR 3 to inspect `src/polycopy/risk/fill_model.py` line-by-line. Audit §3.6 flags `expected_fill_price`, `estimated_slippage` as "NOT IMPLEMENTED in signal path" and the file as "exists but never invoked". PR 3 must classify each function in `risk/fill_model.py` as REAL or PLACEHOLDER and replace placeholders with honest implementations. Likely candidates to be placeholders (require PR 3 verification): `expected_fill_price()` (suspected: returns `0.0` or `signal.expected_fill_price`), `estimated_slippage()` (suspected: returns `0.0`). **EVIDENCE MISSING — requires PR 3.**

- **Which existing functions are placeholders despite production-looking names?** EVIDENCE MISSING — requires PR 3 to inspect `src/polycopy/risk/fill_model.py`, `src/polycopy/risk/pnl.py`, `src/polycopy/risk/marks.py`, `src/polycopy/providers/bidask.py`, `src/polycopy/adapters/paper_broker.py` line-by-line. Known suspicious names: `expected_fill_price`, `estimated_slippage`, `mark_to_market`, `compute_portfolio_var`, `fetch_book`, `place_order`. PR 3 (and PR 5 for `place_order`) must classify each as REAL or PLACEHOLDER. **EVIDENCE MISSING — requires PRs 3 and 5.**

## 11. Naming decision: rename "edge"
Placeholder `edge = outcome.price - 0.5` is misleading. Rename semantics to:
- `price_movement = source_trade_price − current_midpoint_or_ask` (signed, in price units)
- `copy_cost = expected_fill_price − source_trade_price` (signed, in price units, where positive means the copy is worse than the source trade by `copy_cost`)

Do NOT define `predicted_prob` / `model_prob` / `expected_value` until an independent model exists.

Explicit rename rules for this PR series:
- The DB column formerly named `edge_estimate` (placeholder) is RENAMED semantics-wise to `price_movement` (real). Do not rename the column in this PR series — keep the column name `edge_estimate` for schema compatibility, but rename the field's meaning in code and add a code comment explaining the change. A future PR may rename the column itself when the placeholder rows are confirmed gone.
- `predicted_prob`, `market_prob`, and `confidence` from the placeholder signal become: `source_trade_price`, `current_midpoint`, and `evidence_quality` respectively, or are removed entirely if unused. PR 4 implements this.
- The `strength` field (currently always "buy") is replaced by `side` (BUY/SELL, copied from `source_trades.side`).

## 12. Pilot-readiness gate
Paper pilot is ready ONLY when an evidence package proves the full chain listed in §1. The evidence package is produced by PR 6's `scripts/pilot_readiness_check.py` and includes:
- One wallet with `verdict = COPY_CANDIDATE` under `formula_version = "v1"`.
- One specific source trade from that wallet identified by `source_trades.source_trade_id`.
- The source trade mapped to one persisted market outcome (`PR 1` evidence: `market_outcomes.clob_token_id` populated, multi-outcome join returns 1 row).
- A copy candidate persisted exactly once (`PR 2` evidence: `copy_candidates` has exactly one row per `(wallet_id, source_trade_id)`).
- Fresh current-price information obtained (`PR 3` evidence: `price_observations` row exists, `copy_candidates.expected_fill_price` / `estimated_slippage` / `price_movement` / `copy_cost` populated).
- An accepted or rejected signal decision persisted (`PR 4` evidence: `signals` row exists with `idempotency_key`, real `price_movement` and `copy_cost`).
- Rerunning produces no duplicate candidate or signal (`PR 2` + `PR 4` idempotency tests).
- A manual preview shows complete trade information (`PR 5` evidence: `preview(signal_id)` returns every required field).
- The kill switch blocks approval when enabled (`PR 5` negative test).
- After a separate explicit decision, one controlled paper approval creates exactly one order and one position (`PR 5` positive test).
- No live broker was initialized or called (verified via `paper_broker.PaperBroker` — no Polymarket key in memory).
- Monitoring and decision logs reflect the entire chain (`pilot_status_latest.txt` shows the chain, `decision_log` has every stage).

Until every line above is verified, the pilot is NOT considered started.

## 13. Out-of-bundle
The following items MUST NOT appear in any of the 6 PRs above:
- Enabling live trading
- Disabling the kill switch
- Automatic paper-order approval (any path that bypasses `paper_mode=paper_manual`)
- Threshold calibration (changing `MAX_SHARPE`, `WEIGHTS`, `CRITICAL_FIELDS`, verdict boundaries, or any `>=`/`<` numeric in scoring)
- Dropping `market_outcomes.volume`
- Renaming existing columns in destructive migrations
- Broad schema cleanup
- Dashboard redesign
- External alerts / Telegram / webhooks
- Scan-timeout optimization
- Collector fixes unrelated to token/outcome persistence

## 14. Final recommendation: exactly ONE first PR

**Recommended first PR:** PR 1 — Persist source-trade identity and outcome mapping.

**Why this is the smallest foundational dependency:**
- It does not generate signals.
- It does not approve orders.
- It is purely additive (two nullable columns + one nullable index).
- It unblocks PR 2's identity-mapping needs: without `market_outcomes.clob_token_id` and `source_trades.token_id`, PR 2 cannot reliably materialize `(wallet, trade, market, outcome)` for multi-outcome markets.
- It produces no production-side behavior change (no existing code path reads the new columns).
- Its acceptance test is concrete and runs in a disposable DB in under a minute.
- It does not touch the kill switch, broker_mode, paper_mode, is_live, the scoring formula, or any threshold.

**Approximate file list for PR 1:**
- `src/polycopy/db/schema.py` (add two nullable columns + one nullable index)
- `src/polycopy/db/market_persistence.py` (write `clob_token_id` from parsed Gamma)
- `src/polycopy/adapters/polymarket.py` (write `token_id` in `_persist_trade`)
- `tests/test_p01_clob_token_persistence.py`
- `tests/test_p01_multi_outcome_join.py`
- `tests/test_p01_no_regression_binary.py`

**Acceptance test for PR 1:** (Concrete reviewer steps)
1. Open a disposable DB (mirror of production schema + the new columns).
2. Run the new migration that adds `market_outcomes.clob_token_id` and `source_trades.token_id`.
3. Replay a recorded Gamma payload containing a sports multi-outcome market (≥3 outcomes, non-Yes/No labels).
4. Replay a recorded data-api trade payload for that market with `outcome="Seattle Mariners"`.
5. Run: `SELECT mo.market_id, mo.label, mo.clob_token_id, st.source_trade_id, st.token_id FROM source_trades st JOIN market_outcomes mo ON st.market_source_id = mo.market_source_id AND (st.token_id = mo.clob_token_id OR st.outcome = mo.label) WHERE st.source_trade_id = 'polymarket:<txhash>';` and verify exactly one row with the correct `label` ("Seattle Mariners") and matching `token_id`.
6. Repeat with a binary market and confirm the label-based join still returns exactly one row.
7. Verify the existing production code paths (run_scan, _live_ingest, collect) still execute against the disposable DB without errors (no regression).

**Status reminder:** as of this document, the paper pilot has **not** started. Every
artifact in the operational reports directory is planning/audit evidence; the
production database holds zero signals, zero orders, zero positions, and zero
decision_log rows. None of PR 1, PR 2, PR 3, PR 4, PR 5, or PR 6 has been
merged or scheduled.