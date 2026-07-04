# PR #20 — Specialist Metric Aggregation Layer Audit

**Branch:** `feat/pr20-specialist-metric-aggregation`
**Base:** `main @ 3e54031` (PR #19 deployed, paper-only verified)
**Author:** NxHermes (audit + minimal layer)
**Status:** Draft — not merged, not deployed

---

## 1. Purpose

Audit the existing Polycopy schema and code to determine what specialist
wallet metrics can be computed **honestly** today, what must remain in
shadow, and what is outright blocked by missing data. Then implement
the smallest conservative layer that adds **observable, transparent
evidence** without touching formulas, thresholds, or live trading.

This is the prerequisite for the already-planned specialist wallet
formula (the `category_wallet_score_decisions` and
`trade_copyability_decisions` tables are provisioned but currently
**0 rows**).

**No formula, threshold, gate, schema, or systemd `TimeoutStartSec`
changes are introduced by this PR.** Live trading remains blocked. No
approvals, orders, positions, or fills are produced.

---

## 2. Existing DB state (snapshot from `data/polycopy.db`)

| Table | Rows | Notes |
|---|---:|---|
| `source_trades` | 1,661,952 | Real Polymarket trades; `trader_address` 100% joins to `wallets.canonical_address` |
| `wallets` | 96,791 | Has `canonical_address` (used for joins) |
| `markets` | 315 | **All `resolved=0`, all `closed=0`, all `resolution_outcome=NULL`** |
| `market_outcomes` | 630 | Per-market current prices; **all `clob_token_id=NULL`** |
| `raw_snapshots` | 14,341 | Order-book snapshots |
| `decision_verdicts` | 50 | PR19 produces these |
| `wallet_score_decisions` | 52 | Most nullable fields are NULL |
| `score_component_inputs` | 364 | Component inputs log |
| `category_wallet_score_decisions` | 0 | **Planned table — no rows yet** |
| `trade_copyability_decisions` | 0 | **Planned table — no rows yet** |
| `paper_signal_decisions` | 0 | None yet |
| `copy_candidates` | 1 | One from PR19 smoke |
| `orders` | 0 | ✓ paper-only |
| `positions` | 0 | ✓ paper-only |
| `signals` | 0 | ✓ paper-only |
| `wallet_balances` | 0 | ✓ paper-only |

**Key finding:** the production DB has **zero resolved markets and zero
non-NULL `clob_token_id` rows in `market_outcomes`**. This means
**settlement-anchored P/L, real win rate, drawdown, and profit factor
are not honestly computable from current data**.

---

## 3. Existing code: how metrics flow today

### 3.1 `_compute_wallet_metrics` (`scripts/run_scan.py:920`)

Returns a dict with:
- `sharpe_ratio` — a heuristic `win_rate * sqrt(trade_count) * 0.5`
- `win_rate` — **NOT a real win rate**. Counts `buy < 0.5` and
  `sell > 0.5` as "wins". A price-direction proxy, not a
  settlement-anchored outcome.
- `trade_count`, `latest_trade_ts`, `first_trade_ts`, `markets_traded`

The PR19 production rows confirm this:
- `id=51`: `win_rate=0.0`, `trade_count=3`, `final_score=0.0`, `verdict=skip`
- `id=52`: `win_rate=0.297`, `trade_count=118`, `final_score=15.6`, `verdict=skip`
- `id=53`: `win_rate=0.0`, `trade_count=2`, `final_score=0.0`, `verdict=skip`

**Almost every nullable field on the persisted decisions is NULL.**
The code at `scripts/scan_pipeline_wiring.py:603` (`_wallet_inputs_from_metrics`)
maps only `win_rate`, `trade_count`, and `sharpe_ratio` from the
legacy dict. Everything else (profit_factor, max_drawdown,
sample_fraction, category_*, resolved_*, distinct_events,
active_trading_days) defaults to **None** so the V1 formula
correctly returns **INCOMPLETE** rather than fake zero.

### 3.2 `compute_wallet_score_v1` (`src/polycopy/scoring/wallet_score_v1.py`)

Frozen formula. Weights: 30/15/15/10/10/15/5 summing to 100. Verdict
gates: 75+ COPY, 55–74 WATCHLIST, <55 SKIP, missing essentials →
INCOMPLETE. Global minimums: 30 resolved markets, 20 active trading
days, 15 distinct events.

**No formula weight or threshold changes in this PR.**

### 3.3 `category_wallet_score_v1` (`src/polycopy/scoring/category_wallet_score_v1.py`)

Mirrors wallet-score v1 but per `(wallet, category_label)`. The table
exists with the right columns; **the runtime never writes to it today**.

### 3.4 Category resolution (`src/polycopy/scoring/paper_signal.py:552+`)

`resolve_category_label_for_inputs` checks three paths:
1. `snapshot.book_summary_json.category_label` (or legacy `category`)
2. (Same JSON fallback)
3. `markets.category` via join — **the production schema has no
   `category` column on `markets`**, so step 3 yields `None`.

**Deliberate, documented behavior:** no synthetic `f"market:{market_id}"`
fallback. Missing category → INCOMPLETE.

### 3.5 PR19 bounded Step 5 (`scripts/scan_pipeline_wiring.py`)

`resolve_bounded_wallet_slice` enforces the hard cap
`len(addresses_in_slice) <= max_wallet_scores`. Material-input
bypass prevention distinguishes fresh / already-scored /
material-changed wallets. Reused in this PR — **must not be changed**.

---

## 4. Specialist-metric audit (each requested metric)

| # | Specialist metric | Status | Honest computation today? | Source fields |
|---|---|---|---|---|
| M1 | per-wallet per-category trade count | **READY NOW** | Yes | `source_trades.trader_address` → `wallets.canonical_address`, `markets.id` via `source_trades.market_source_id → markets.source_id` |
| M2 | per-wallet per-category resolved market count | **BLOCKED** | No | `markets.resolved=0` and `markets.resolution_outcome=NULL` for every row in production |
| M3 | per-wallet per-category distinct event count | **PARTIAL** | Conservative: distinct `markets.id` (events ≡ markets in current schema — no separate events table) | `markets.id` |
| M4 | per-wallet per-category active trading days | **READY NOW** | Yes — `COUNT(DISTINCT DATE(timestamp))` joined to markets | `source_trades.timestamp` |
| M5 | per-wallet per-category win rate | **BLOCKED** | No. The current "win rate" in `_compute_wallet_metrics` is a price-direction heuristic, **not** a settlement-anchored win rate. We must not persist it under that name. | requires `markets.resolved` + `markets.resolution_outcome` |
| M6 | per-wallet per-category realized P/L or ROI | **BLOCKED** | No. No settlement data; `market_outcomes.clob_token_id` all NULL; no per-position book-keeping on `positions` (0 rows). | requires settlement feed |
| M7 | per-wallet per-category profit factor | **BLOCKED** | No — derived from M6 | requires M6 |
| M8 | drawdown / loss streak / risk | **BLOCKED** | No — derived from M6 | requires M6 |
| M9 | category concentration (share of trades in category) | **READY NOW** | Yes — `category_trade_count / overall_trade_count`. Already consumed by `_category_specialization_component`. | derived |
| M10 | sample reliability (`is_sample` fraction) | **READY NOW** | Yes — `COUNT(is_sample=0) / COUNT(*)` over the wallet's trades. Already supported by column on `source_trades`. | `source_trades.is_sample` |
| M11 | holding-period quality | **PARTIAL** | Median (first_trade_ts, latest_trade_ts) span across markets **when** at least two timestamps exist. **No per-trade exit timestamps** are available today, so "holding period per trade" is not honest. | derived from `source_trades.timestamp` |
| M12 | copyability after delay/slippage | **PARTIAL** | The infrastructure exists (`trade_copyability_decisions` table, `candidate_price_snapshots`, `candidate_price_snapshot_levels`). Currently 0 rows. We expose the *envelope* of evidence (snapshot exists / not / insufficient depth) without computing a numeric slippage — that's already what the V2 shadow does. **SHADOW-ONLY** here. | `candidate_price_snapshots` |
| M13 | directional vs market-maker/arbitrage/bot behavior | **PARTIAL** | Heuristic: ratio of `side` flips over time windows. Currently no implementation in the legacy path. Conservative classification only — **must remain shadow**. | `source_trades.side`, `timestamp` |
| M14 | market freshness | **READY NOW** | `markets.fetched_at` age vs now; `markets.active/closed/resolved`. Persist as **quality-tagged** input only; do not feed formulas yet. | `markets.fetched_at`, `markets.active`, `markets.closed` |
| M15 | liquidity / spread evidence | **PARTIAL** | Existing `candidate_price_snapshots` + `candidate_price_snapshot_levels` (when present). Coverage in current data is sparse; persist as **observed** vs **unknown**. | `candidate_price_snapshot_levels` |
| M16 | price-improvement quality | **BLOCKED** | The existing `info_score` is the only signal we have. It is already routed through `score_component_inputs`. No additional evidence to add honestly. | `source_trades.price` vs book |
| M17 | outcome availability / settlement reliability | **READY NOW (as a flag)** | `markets.resolved` + `markets.resolution_outcome IS NOT NULL` + `markets.closed`. Persist as `market_resolution_state` (observed/unknown). | `markets.resolved`, `markets.resolution_outcome`, `markets.closed` |

---

## 5. Implementation principle (conservative fallback rules)

1. **No fake numbers.** If a metric cannot be computed honestly, it
   is either omitted entirely from the aggregation row or stored as
   `NULL` with `quality='unknown'`. Never a `0` masquerading as a
   real value.
2. **Missing evidence ⇒ INCOMPLETE.** Any aggregation row that would
   require a blocked metric carries a `missing_essentials` entry; the
   downstream V1 formula is unaffected (it already returns INCOMPLETE
   when essentials are missing).
3. **Categories only from snapshot `book_summary_json`.** No synthetic
   fallback. If no category resolves, the wallet-level aggregation
   still proceeds but category-tagged rows are skipped.
4. **Idempotent writes.** All new aggregation rows write through
   `generate_idempotency_key` keyed on `(wallet_id, category_label,
   source_data_timestamp)` so re-running the scan is a no-op.
5. **Shadow-tagged evidence.** Any new metric flagged as PARTIAL or
   SHADOW in §4 is persisted with `quality='unknown'` or
   `quality='observed'` only; it must never produce a numeric value
   that is consumed by a scoring formula in this PR.

---

## 6. Proposed metric definitions for the minimal layer

Persisted as **aggregation rows** (not formulas) on a new table
`wallet_specialist_aggregations` (see §7 for schema).

```
aggregation_id            INTEGER PK
wallet_id                 TEXT NOT NULL → wallets.id
category_label            TEXT NOT NULL  -- empty string when no category resolvable
formula_name              TEXT NOT NULL  -- 'specialist_metric_v1'
formula_version           TEXT NOT NULL  -- '1'
idempotency_key           TEXT NOT NULL
source_data_timestamp     TEXT NOT NULL  -- MAX(source_trades.timestamp) for this wallet

-- READY NOW (M1, M4, M9, M10, M14, M17)
trade_count               INTEGER        -- COUNT(*) over source_trades
distinct_markets          INTEGER        -- COUNT(DISTINCT markets.id)
distinct_events           INTEGER        -- COUNT(DISTINCT markets.id) (alias; no separate events table today)
active_trading_days       INTEGER        -- COUNT(DISTINCT DATE(timestamp))
category_trade_count      INTEGER        -- subset of trade_count restricted to category
category_distinct_markets INTEGER
category_active_days      INTEGER
category_concentration    REAL           -- category_trade_count / trade_count (NULL when trade_count=0 or no category)
sample_reliability_score  REAL           -- COUNT(is_sample=0)/COUNT(*) over wallet's trades (NULL when trade_count=0)

-- PARTIAL (M3, M11, M15) — persisted with quality='observed' or 'unknown'
holding_period_days       INTEGER        -- (latest_trade_ts - first_trade_ts) in days, NULL when <2 trades
distinct_event_count_v2   INTEGER        -- reserved; same as distinct_markets until events table arrives

-- SHADOW (M12, M13, M16) — not persisted as numeric values; only flagged
behavior_classification  TEXT           -- 'unknown' | 'directional' | 'mixed' (heuristic, shadow-only)
copyability_evidence_state TEXT          -- 'unknown' | 'snapshot_present' | 'snapshot_missing'
price_improvement_state   TEXT           -- 'unknown' | 'book_present' | 'book_missing'

-- BLOCKED (M5, M6, M7, M8) — explicitly NOT persisted in this PR
-- realized_pnl              REAL          -- absent (no settlement)
-- profit_factor             REAL          -- absent
-- max_drawdown              REAL          -- absent
-- win_rate_realized         REAL          -- absent
-- resolved_markets          INTEGER       -- absent (markets.resolved=0 everywhere)

-- Quality + missing-evidence bookkeeping
component_scores_json     TEXT NOT NULL  -- structured evidence dump
quality                   TEXT NOT NULL  -- 'observed' | 'partial' | 'unknown' | 'incomplete'
missing_essentials_json   TEXT NOT NULL  -- list of blocked metric names
created_at                TEXT NOT NULL

UNIQUE(wallet_id, category_label, formula_name, formula_version, idempotency_key)
```

**Rationale:** the table is **evidence** (a transparent log), not a
scoring input. Nothing in this PR feeds it back into
`compute_wallet_score_v1` or `compute_category_wallet_score_v1`.

---

## 7. Schema changes

**One new table**, no destructive migrations:

```sql
CREATE TABLE IF NOT EXISTS wallet_specialist_aggregations (
    aggregation_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id               TEXT    NOT NULL REFERENCES wallets(id),
    category_label          TEXT    NOT NULL,
    formula_name            TEXT    NOT NULL,
    formula_version         TEXT    NOT NULL,
    idempotency_key         TEXT    NOT NULL,
    source_data_timestamp   TEXT    NOT NULL,

    trade_count               INTEGER CHECK (trade_count IS NULL OR trade_count >= 0),
    distinct_markets          INTEGER CHECK (distinct_markets IS NULL OR distinct_markets >= 0),
    distinct_events           INTEGER CHECK (distinct_events IS NULL OR distinct_events >= 0),
    active_trading_days       INTEGER CHECK (active_trading_days IS NULL OR active_trading_days >= 0),
    category_trade_count      INTEGER CHECK (category_trade_count IS NULL OR category_trade_count >= 0),
    category_distinct_markets INTEGER CHECK (category_distinct_markets IS NULL OR category_distinct_markets >= 0),
    category_active_days      INTEGER CHECK (category_active_days IS NULL OR category_active_days >= 0),
    category_concentration    REAL,
    sample_reliability_score  REAL,
    holding_period_days       INTEGER CHECK (holding_period_days IS NULL OR holding_period_days >= 0),
    distinct_event_count_v2   INTEGER CHECK (distinct_event_count_v2 IS NULL OR distinct_event_count_v2 >= 0),

    behavior_classification   TEXT,
    copyability_evidence_state TEXT,
    price_improvement_state   TEXT,

    component_scores_json     TEXT NOT NULL,
    quality                   TEXT NOT NULL,
    missing_essentials_json   TEXT NOT NULL,
    created_at                TEXT NOT NULL,

    UNIQUE(wallet_id, category_label, formula_name, formula_version, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_wsa_wallet ON wallet_specialist_aggregations(wallet_id);
CREATE INDEX IF NOT EXISTS idx_wsa_category ON wallet_specialist_aggregations(category_label);
CREATE INDEX IF NOT EXISTS idx_wsa_quality ON wallet_specialist_aggregations(quality);
```

**No columns added to existing tables. No CHECK constraints tightened.
No destructive rebuilds.** This is purely additive.

---

## 8. Implementation scope (this PR)

- New module: `src/polycopy/scoring/specialist_metrics.py`
  - Pure functions computing each metric over a (wallet, trades,
    markets) bundle. No DB writes here.
- New persistence helper: `src/polycopy/scoring/specialist_metrics_persistence.py`
  - Idempotent INSERT into `wallet_specialist_aggregations`.
- Migration: new file `src/polycopy/db/schema_v13.py` containing only
  the new `CREATE TABLE IF NOT EXISTS` + indexes above. Picked up by
  the existing migration loader.
- Aggregation call site: a single bounded function
  `compute_and_persist_wallet_specialist_aggregations(db, addresses,
  max_aggregations)` that respects the **PR19 hard-cap invariant**
  (`len(processed) <= max_aggregations`) and reuses the existing
  `resolve_bounded_wallet_slice` selection algorithm. Called from
  `run_scan` *alongside* the existing Step 5b, never inside it.
- **No changes** to `compute_wallet_score_v1`,
  `compute_category_wallet_score_v1`, `WEIGHTS`, thresholds, gates,
  `_wallet_inputs_from_metrics`, the bounded slice helper, the
  `run_scan` hard cap, or the systemd unit.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| New table grows unbounded | `max_aggregations` budget mirrors the PR19 hard-cap; re-running is a no-op via idempotency keys. |
| Duplicate semantic rows | UNIQUE(wallet_id, category_label, formula_name, formula_version, idempotency_key). Tests assert no duplicates. |
| Accidentally feeding blocked metrics into V1 | `specialist_metrics.py` returns the dict only; the persistence helper writes the row only. Nothing imports these functions into the V1 path. |
| Changing behavior of `_compute_wallet_metrics` | We do not modify it. The new layer is additive. |
| Live trading accidentally enabled | No `is_approved` flips; no orders/positions writes. A safety test asserts `orders=0, positions=0, signals=0` before and after a representative scan run. |
| Race with PR19 budget | Both layers share the same wallet set but use distinct budget caps. The PR19 budget is unchanged. |
| Category inflation | Empty-string category is the documented fallback; category-resolved wallets produce a *separate* row from category-unresolved wallets. |

---

## 10. Test plan

Unit (`tests/test_pr20_specialist_metrics.py`):
- `compute_per_wallet_per_category_trade_count` over a 5-row fixture
- `compute_active_trading_days` dedupes by date
- `compute_distinct_markets` and `compute_distinct_events` agree
  (events ≡ markets in current schema)
- `compute_category_concentration` returns NULL when `trade_count=0`
- `compute_sample_reliability_score` is `0.0` when all trades are
  sample and `1.0` when none are
- `compute_holding_period_days` returns NULL for 0–1 trades
- All blocked metrics (M5/M6/M7/M8) are NOT in the returned dict

Fixture (`tests/test_pr20_specialist_metrics_persistence.py`):
- In-memory SQLite, write aggregation row for one wallet + one
  category, re-run with same idempotency key → no duplicate row
- Write one aggregation row + one wallet_score_decision row → both
  succeed, no FK violation
- Run with empty source_trades → `trade_count=0`, `quality='incomplete'`

Missing-data tests:
- Source trades present but `markets.source_id` not joined → only
  wallet-level metrics produced, category fields are NULL
- `markets.resolved=0` everywhere → `missing_essentials_json` contains
  `resolved_markets`, `quality='incomplete'`, `component_scores_json`
  does NOT include `win_rate_realized`, `realized_pnl`,
  `profit_factor`, `max_drawdown`

Safety tests:
- Run a synthetic scan, assert `orders=0`, `positions=0`,
  `signals=0`, `wallet_balances=0`, `paper_signal_decisions`
  contains zero rows with `is_approved=1`
- Assert `wallet_score_decisions` rows produced in the run match the
  PR19 formula exactly (no formula or threshold drift)
- Assert the PR19 hard-cap invariant still holds (`len(processed)
  <= max_wallet_scores`)

Regression tests:
- `tests/test_pr19_legacy_step5_bound.py` continues to pass unchanged
- `tests/test_p04_*` continues to pass unchanged

---

## 11. Recommendation for PR20 implementation scope

**Implement the minimal conservative layer as described in §6–§8.**

Concretely:
- `wallet_specialist_aggregations` table (additive schema v13)
- `specialist_metrics.py` — pure aggregation functions
- `specialist_metrics_persistence.py` — idempotent write helper
- Call site: a single bounded function in `run_scan`, **after** Step
  5b, behind a feature flag default-ON for PR20
- **Defer to a later PR:** anything that would consume
  `wallet_specialist_aggregations` into a scoring formula
- **Defer entirely until settlement data exists:** M5, M6, M7, M8

This delivers **observable, transparent evidence** that the
already-planned specialist tables (M1, M4, M9, M10, M14, M17) are
honestly computable, **without** risking the PR19 invariant or
producing fake P/L. The next PR (PR #21 or later) can build the
specialist formula on top of these aggregation rows once settlement
data arrives.

---

## 11. Safety confirmation (re-verified after PR #20 review)

- ❌ No broker credentials added
- ❌ No `is_approved=1` ever written
- ❌ No `orders` rows written
- ❌ No `positions` rows written
- ❌ No fills written
- ❌ No formula weight / threshold / gate changes
- ❌ No `TimeoutStartSec` change
- ❌ No live trading enabled
- ✅ All new writes go to a new evidence table only
- ✅ Existing formulas consume no new inputs in this PR
- ✅ PR19 hard-cap invariant preserved
- ✅ PR19 bounded slice helper unchanged

---

## 12. Production activation — single explicit switch (Option A revised)

**This PR does NOT activate specialist aggregation rows in production by
default.** PR #20 ships the schema, the aggregator, the idempotent
persistence helper, the bounded Step 5f, and a single explicit
activation switch.

The activation switch is
`Settings.specialist_aggregations_enabled` in
`src/polycopy/config/settings.py`. It honors the existing
`POLYCOPY_*` env-var prefix and the `Settings` field is the load-bearing
default.

**Activation is a one-line / one-word change.** No code changes are
required to flip Step 5f on — pick one:

```bash
# Option 1 — env var (preferred for production deploys)
export POLYCOPY_SPECIALIST_AGGREGATIONS_ENABLED=true
export POLYCOPY_SPECIALIST_AGGREGATIONS_MAX_ROWS_PER_RUN=50  # default
```

```python
# Option 2 — flip the field default in settings.py (one word)
# src/polycopy/config/settings.py
-    default=False,
+    default=True,
```

A four-test suite (`SingleLineActivationTests`) pins the contract:

1. `test_default_is_off` — no env var → flag is False.
2. `test_env_var_true_flips_to_on` — `POLYCOPY_SPECIALIST_AGGREGATIONS_ENABLED=true` activates Step 5f.
3. `test_env_var_false_overrides_default` — explicit `false` always disables (no "always-on" footgun).
4. `test_env_var_cap_override` — the row-cap env var overrides the default.

Activation is **deferred until PR #19 24-hour observation is accepted**.
PR #20 may be finalized either with activation OFF as infrastructure-only
or with the single activation switch flipped ON before merge, depending
on review. This PR contains the entire activation surface — no separate
feature PR is required to turn Step 5f on.

**Activation does NOT enable live trading.** It does NOT add a formula
consumer. It does NOT create orders / positions / approvals / fills. It
does NOT touch `TimeoutStartSec`. The safety tests
(`SafetyTests::test_no_orders_positions_signals_balances_writes` and
`SafetyTests::test_no_approved_paper_signals`) assert the paper-only
invariant regardless of activation state.

### Deployment note (only relevant if PR #20 is merged with activation ON)

If PR #20 is finalized with activation ON:

1. **Verify** `POLYCOPY_SPECIALIST_AGGREGATIONS_ENABLED=true` is set
   in the systemd unit's `Environment=` line (or in the
   `Makefile` target's shell).
2. **After first run**, confirm rows appear in
   `wallet_specialist_aggregations`:
   ```sql
   SELECT COUNT(*), MAX(created_at) FROM wallet_specialist_aggregations;
   ```
   A non-zero `COUNT(*)` and a recent `MAX(created_at)` confirm Step 5f
   ran.
3. **Confirm formulas still do NOT consume the new table** by inspecting
   `src/polycopy/scoring/wallet_score_v1.py` and
   `src/polycopy/scoring/category_wallet_score_v1.py` — neither
   references `wallet_specialist_aggregations`. (Verified in PR #20
   review.)
4. **Confirm orders/positions/signals remain zero** with the existing
   systemd post-scan health check.
5. **Revert plan**: unset the env var (or flip the field default back
   to `False`) and restart the scan service. The new table will simply
   stop receiving inserts; existing rows are harmless audit evidence.

### Why this design (vs. "requires a separate feature PR")

The original Option-A wording framed activation as something a
follow-on PR would own. Per the second-review clarification, that
framing was unnecessarily heavy: the activation surface in PR #20 is
already complete. No new consumer, no new formula, no new code path is
required to write rows — only the switch needs to flip. Forcing a
separate feature PR to do that one-line change would add review
overhead without any code-quality or safety benefit.

---

## 13. Idempotency contract (BLOCKER 1 fix)

`persist_wallet_specialist_aggregation` returns **truthy only when a
new row was actually inserted** (verified via `cursor.rowcount` on
the underlying `sqlite3.Cursor` after `INSERT OR IGNORE`):

* `rowcount == 1` → function returns `True` (new insert).
* `rowcount == 0` → function returns `False` (UNIQUE collision, the
  existing row was kept).
* If a future DB wrapper hides `rowcount`, the function falls back
  to a post-insert existence check so the return value is still
  honest.

This is enforced by
`tests/test_pr20_specialist_metrics_persistence.py::ReturnValueTests`
(3 tests) and exercised end-to-end by
`Step5fIntegrationTests` (3 tests). The full set of BLOCKER-1
tests:

1. `test_first_insert_returns_true`
2. `test_second_identical_insert_returns_false`
3. `test_table_row_count_remains_one_after_duplicate`
4. `test_first_run_reports_rows_written_one`
5. `test_rerun_reports_rows_written_zero_skipped_one`
6. `test_max_aggregations_cap_holds`
7. `test_no_orders_positions_signals_balances_writes`
8. `test_no_approved_paper_signals`

All 8 pass on the current branch.