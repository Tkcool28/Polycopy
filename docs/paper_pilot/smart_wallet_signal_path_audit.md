# Polycopy Smart-Wallet Signal-Path Architecture Audit

**Generated:** 2026-07-01T16:56:00Z UTC (2026-07-01T10:56:00-06:00 America/Denver)
**Investigator:** nxhermes subagent
**Scope:** everything NOT touched (production code except the new audit reports dir, production DB, production services/timers, production Git state, kill switch, broker_mode, paper_mode, .env/Caddy/Hermes/credentials)

> This is a redacted copy of the durable audit report. It preserves the factual
> architecture findings, identified placeholder logic, and readiness gaps.
> Temporary workspace paths, machine-specific noise, secrets/credentials, and
> any text implying the paper pilot has started have been redacted.

## PART 1 — Cleanup confirmation
- Preserved audit artifacts in the local operational reports directory (filename list elided for brevity).
- Temporary scratch workspace (created for the dry run only) removed and verified gone.
- Secret-grep verification: no real secrets present in any artifact.

## PART 2 — Production safety and repository verification
- `git branch --show-current` = `main`
- `git rev-parse HEAD` = `6ae6eaaf47d7a8b20b8613aa2321ff8b2d180f35`
- `git rev-parse origin/main` = `6ae6eaaf47d7a8b20b8613aa2321ff8b2d180f35` (matches)
- `git status --short` = `?? reports/paper_pilot/` (expected — the new audit dir)
- DB counts via `file:...?mode=ro`: signals=0, orders=0, positions=0, decision_log=0; FK violations=0
- Safety: `broker_mode=paper`, `paper_mode=paper_manual`, `order_kill_switch=True`, `is_live=False` (confirmed from `data/pilot_status_latest.txt` and `src/polycopy/config/settings.py`); no production service/timer stopped or triggered.

## PART 3.1 — Complete pipeline map

| # | Stage | File | Fn/Class | Input | Output | Persisted? | Table | Connector |
|---|-------|------|----------|-------|--------|------------|-------|-----------|
| 1 | Market ingestion | `scripts/run_scan.py` | `_fetch_markets` + `_parse_gamma_market` | Gamma `/markets` HTTP | `Market` objects | no (in-memory) | — | `market.source_id` (Polymarket conditionId) |
| 2 | Market-outcome persistence | `db/market_persistence.py` | `persist_market_preserving_identity` | `Market` | rows | yes | `markets`, `market_outcomes` | `markets.id` (UUID) ← `source_id`; `market_outcomes.market_id` |
| 3 | Wallet discovery | `discovery/wallet_discovery.py` | `WalletDiscovery.add_from_polymarket`, `RelatedWalletDetector` | canonical trader_address | `discovery` registry | no | — | in-memory only |
| 4 | Source-trade ingestion | `scripts/_live_ingest.py` → `adapters/polymarket.py` | `PolymarketPublicAdapter.fetch_trades_for_market` + `_persist_trade` | data-api `/trades?market=…` | `SourceTrade` rows | yes | `source_trades` | `source_trades.market_source_id` (conditionId), `source_trades.source_trade_id` |
| 5 | Wallet feature generation | `scripts/run_scan.py` | `_compute_wallet_metrics` | source_trades rows | `{sharpe_ratio, win_rate, trade_count, latest_trade_ts, first_trade_ts, markets_traded}` | no (in-memory) | — | `trader_address` (canonical lowercase) |
| 6 | Wallet scoring | `src/polycopy/engine/evaluate.py` + `scoring/engine.py` | `evaluate_wallet` → `score_wallet` (formula_version="v1") | wallet metrics | `CopyabilityScore` (0–100) | no (in-memory) | — | `wallet_id` |
| 7 | Verdict assignment | `scoring/engine.py` | `compute_verdict` | score, missing_fields | `Verdict.{COPY_CANDIDATE,WATCHLIST,SKIP,INCOMPLETE}` | no | — | verdict lives only inside `CopyabilityScore` |
| 8 | In-memory copy-candidate generation | **NOT IMPLEMENTED** — see §3.3 | — | — | — | — | — | — |
| 9 | Placeholder signal generation | `scripts/run_scan.py` | `_generate_signals` | `list[Market]` (no wallet data!) | `signals` rows | yes | `signals` | `signals.market_id` (UUID); id is fresh uuid4 each call |
| 10 | Paper-order preview | **NOT IMPLEMENTED** — `paper_mode=paper_manual`; run_scan does not produce orders | — | — | — | — | — | — |
| 11 | Manual approval | not invoked by run_scan (would be in `api/app.py`) | — | — | — | — | — | — |
| 12 | Paper fill | `adapters/paper_broker.py` (`PaperBroker`) | `place_order` etc. | Order | filled Order | yes | `orders` | `orders.signal_id` (FK to `signals.id`) |
| 13 | Paper position | `risk/pnl.py`, `risk/marks.py`, `domain/position.py` | mark-to-market | Order + current prices | `positions` row | yes | `positions` | `positions.market_id`, `wallet_id`, `outcome` |
| 14 | Settlement | `risk/settlement.py` | (settlement logic) | resolved market + positions | realized pnl | yes | `positions.realized_pnl` | `markets.resolution_outcome` |
| 15 | Portfolio update | `risk/pnl.py` | portfolio PnL roll-up | positions | aggregated metrics | yes (derived) | `performance_summaries` | `wallet_id`, `strategy_label` |
| 16 | Monitoring | `scripts/paper_pilot_status.py` | `get_safety_state`, classify | DB + settings | `data/pilot_status_latest.txt` | yes (file) | — | reads all tables |

## PART 3.2 — Wallet scoring contract
- **File:** `src/polycopy/scoring/engine.py` (formula_version = "v1"); domain model in `src/polycopy/domain/copyability.py`.
- **Class:** `CopyabilityScore` (pydantic). Composed of 7 weighted `ScoreComponent`s with `DataQuality` tag (OBSERVED/CALCULATED/INFERRED/UNKNOWN).
- **Field classification per wallet:**
  | Field | Source | Class |
  |---|---|---|
  | `wallet_id` (UUID) | `wallets.id` | **REAL** |
  | `score_version` = "v1" | hard-coded | **PLACEHOLDER (literal)** |
  | `sharpe_ratio` | heuristic: `win_rate * sqrt(trade_count) * 0.5` | **PLACEHOLDER** (not a real Sharpe; no return series) |
  | `win_rate` | heuristic: buy & price<0.5 OR sell & price>0.5 | **PLACEHOLDER** (no resolution data) |
  | `trade_count` | COUNT(source_trades WHERE trader_address = wallet) | **REAL** |
  | `latest_trade_ts` | MAX(source_trades.timestamp) | **REAL** |
  | `first_trade_ts` | MIN(source_trades.timestamp) | **REAL** |
  | `markets_traded` | COUNT(DISTINCT source_trades.market_source_id) | **REAL** |
  | specialization, consistency (true), liquidity_behavior, execution_quality, copyability, confidence, realized_performance, EV, unresolved_exposure | — | **NOT IN CONTRACT** (don't exist in `score_wallet`) |
- **Verdict rules** (in `scoring/engine.py`):
  ```
  CRITICAL_FIELDS = {"trade_count", "win_rate", "sharpe_ratio"}
  rule 1: any critical missing → Verdict.INCOMPLETE
  rule 2: score >= 70 → Verdict.COPY_CANDIDATE
  rule 3: score >= 50 → Verdict.WATCHLIST
  rule 4: score <  50 → Verdict.SKIP
  ```
  Missing-field severities: `latest_trade_ts`=major (penalty 7.5), `first_trade_ts`=major (penalty 5), `markets_traded`=minor (penalty 4.5). Component weights sum to 100.

## PART 3.3 — Copy-candidate object and lifecycle
- **Does not exist as a Python type.** Grep for `CopyCandidate`, `copy_candidate`, `class.*Candidate` returns no domain model — only the count `result.copy_candidates` in `ScanResult`.
- The `run_scan` flow goes: discover wallets → score → verdict → **immediately call `_generate_signals(markets, now)`** with `markets` only; wallet verdict is never consulted. There is no (wallet, trade, market, outcome) tuple materialized anywhere.
- Fields needed by a real signal builder:
  - wallet_id (UUID), wallet_score_version, wallet_score, wallet_verdict
  - source_trade_id, trader_address (canonical), side, outcome_label, token_id
  - market_id (UUID), market_source_id (conditionId)
  - observed_trade_price, observed_trade_quantity, observed_trade_timestamp
  - current_price (refreshed), bid, ask, depth, spread
  - expected_fill_price, estimated_slippage, remaining_edge
  - idempotency_key, expires_at, status, rationale

## PART 3.4 — Data capability matrix

| Item | Class | Evidence |
|---|---|---|
| qualifying wallet | AVAILABLE & RELIABLE | `wallets` table; verdict filter selects |
| market identification | AVAILABLE & RELIABLE | `markets` table |
| outcome/side (label) | AVAILABLE & RELIABLE | `source_trades.outcome`, `source_trades.side` |
| trade timestamp | AVAILABLE & RELIABLE | `source_trades.timestamp` ISO8601 UTC |
| observed trade price | AVAILABLE & RELIABLE | `source_trades.price` [0,1] |
| trade quantity | AVAILABLE & RELIABLE | `source_trades.quantity` |
| trade notional | DERIVABLE | quantity × price |
| recency of trade | DERIVABLE | now − timestamp |
| market open | AVAILABLE & RELIABLE | `markets.active`, `markets.closed` |
| market unresolved | AVAILABLE & RELIABLE | `markets.resolved`, `resolution_outcome` |
| outcome mapping (label) | AVAILABLE & RELIABLE | `market_outcomes.label` + `source_trades.outcome` |
| outcome mapping (token_id) | UNAVAILABLE | `market_outcomes` has NO `clob_token_id` / `asset_id` column; `asset_to_outcome_map` exists only in-memory during a scan |
| current market price | AVAILABLE BUT INCOMPLETE | `market_outcomes.price` is price-at-last-scan; not refreshed per candidate |
| current spread | UNAVAILABLE | no bid/ask stored; `providers/bidask.py` exists but is not invoked from `_generate_signals` |
| liquidity / depth | UNAVAILABLE | only `markets.volume_24h`; no order-book depth |
| realistic paper fill price | DERIVABLE in theory | `risk/fill_model.py` exists but never invoked from signal generator |
| remaining edge after slippage | DERIVABLE in theory | needs current price + fill_model — neither wired in |
| duplicate detection (upstream) | AVAILABLE & RELIABLE | UNIQUE(source, source_trade_id) on `source_trades` |
| duplicate detection (signal) | UNAVAILABLE | `signals` table has NO unique constraint; re-running `_generate_signals` inserts duplicates |
| portfolio / risk gate | AVAILABLE | positions, orders, `settings.order_kill_switch`; no candidate-level gate implemented in `run_scan` |

## PART 3.5 — Token-to-outcome mapping
- **Raw Gamma payload** carries `clobTokenIds` (JSON array), `outcomes` (JSON array), `outcomePrices` (JSON array), `conditionId`. `_parse_gamma_market` zips `outcomes[i]` with `outcomePrices[i]` and creates `MarketOutcome(label, price)`. `_build_asset_to_outcome_map` zips `clobTokenIds[i]` with `outcomes[i]` and returns `{token_id: label}`. **The token ID is NOT persisted.**
- **Raw trade payload** carries `conditionId` (= market_source_id), `outcome` (string — often a denormalized team/player name; rewritten to "Yes"/"No" by the asset_to_outcome_map at scan time), `side`, `price`, `size`, `timestamp`, `proxyWallet`.
- **Persistence:**
  - `markets`: stores `source_id` (= conditionId). NO token column.
  - `market_outcomes`: stores `market_id`, `label`, `price`, `volume`. NO token column.
  - `source_trades`: stores `market_source_id` (= conditionId), `outcome` (label string after rewrite), `side`.
- **Can a source_trade be unambiguously mapped to a market_outcome row today?**
  - For binary (Yes/No) markets: **yes, via (source_trades.market_source_id = markets.source_id AND source_trades.outcome = market_outcomes.label).**
  - For multi-outcome markets (sports, esports, elections): **no.** Polymarket's data-api emits raw outcome strings like "T1", "Frances Tiafoe", "Seattle Mariners", "England"; the only way to translate them to canonical "Yes"/"No" labels is the in-memory `asset_to_outcome_map` built fresh every scan. If the trade predates the scan's map build, OR the asset_to_outcome rewrite failed, the join returns 0 rows. There is NO persisted token→outcome mapping.
- **Missing:** `market_outcomes.clob_token_id` (or `asset_id`) column; a persisted `asset_id_to_outcome` mapping table or column on `market_outcomes`; an FK or canonical join from `source_trades.asset` (currently `market_source_id` = conditionId) to a token-level row.

## PART 3.6 — Current edge / confidence implementation

| Term | Class | Evidence |
|---|---|---|
| `edge_estimate` | PLACEHOLDER | `run_scan.py`: `edge = outcome.price - 0.5` |
| `predicted_prob` | PLACEHOLDER | `run_scan.py`: `predicted_prob=outcome.price` |
| `market_prob` | PLACEHOLDER | `run_scan.py`: `market_prob=outcome.price` (identical to predicted_prob) |
| `confidence` | PLACEHOLDER | `run_scan.py`: `confidence = min(outcome.price, 0.95)` |
| `strength` | PLACEHOLDER | `run_scan.py`: `strength = "buy" if edge >= 0.15 else "neutral"`; with `outcome.price >= 0.6` gate, edge is always ≥0.10 and 100% emit "buy" |
| EV / expected value | NOT IMPLEMENTED | no occurrence in `run_scan.py` |
| expected fill price | NOT IMPLEMENTED in signal path | `risk/fill_model.py` exists but is never invoked from `_generate_signals` |
| slippage | NOT IMPLEMENTED in signal path | `risk/fill_model.py` exists, not invoked |
| spread | NOT IMPLEMENTED | no bid/ask in any table; `providers/bidask.py` exists, not invoked |
| liquidity gate | PLACEHOLDER | `outcome.volume >= 10000` (always 0 in source data) or `market.volume_24h >= 10000` after the temporary disposable patch |

## PART 3.7 — Best idempotency identity
- Stable identifiers that already exist in collected data:
  - `source_trades.source_trade_id` (text, e.g. `polymarket:<txhash>`) — unique **within** a `source` value, NOT globally unique. Two providers can legitimately emit the same `source_trade_id` string.
  - `source_trades.source` (e.g. `"polymarket_data_api"`) — the provider identity.
  - **The stable upstream identity is `(source, source_trade_id)`**, enforced by `UNIQUE(source, source_trade_id)` on `source_trades`. Any downstream consumer (resolver, candidate layer, signal idempotency) MUST qualify lookups by both fields unless it has switched to the internal UUID.
  - `source_trades.id` (UUID, internal) — already UNIQUE by PK. Safe to use as a global internal key; carries no upstream semantics.
- Can duplicate upstream trades exist in `source_trades`? **No** — the UNIQUE index on `(source, source_trade_id)` plus `INSERT OR IGNORE` in `_persist_trade` enforces dedup at ingestion.
- **Narrowest correct uniqueness rule for the candidate layer:** `(wallet_id, source_trade_id)` where `wallet_id` = canonical lowercase trader_address. This is correct because: (a) `source_trade_id` is the upstream-stable identifier; (b) a sentinel/NULL trader_address cannot become a wallet_id; (c) if the same upstream trade is observed twice with two distinct trader_address spellings, `canonical_wallet_address()` collapses them to one canonical form, so the same source_trade_id with the same canonical wallet is a single candidate.
- Alternatives considered:
  - `(source_trade_id)` alone is broader but loses the wallet-attribution gate; sentinel rows would be eligible.
  - `(wallet_id, market_id, outcome, ts)` collides for two Yes-trades in the same market within the timestamp granularity.
  - `(wallet_id, market_id, outcome, window)` is acceptable but requires defining a window granularity (5min? 1hr?) — introduces a tunable without benefit.
- **Recommendation:** add `UNIQUE(wallet_id, source_trade_id)` on a new `copy_candidates` table. Do NOT add it to `signals` yet (signals are downstream artifacts; dedup at the candidate stage prevents wasted work).

## PART 3.8 — Decision-log capability
`decision_log` schema: `id, wallet_id, market_id, decision_type, signal_ids (JSON), order_id, rationale, metrics (JSON), created_at, is_sample`. FKs: `wallet_id→wallets`, `market_id→markets`, `order_id→orders`. The `decision_type` column is a free-form string and `metrics` is a JSON blob — both are flexible.

| Outcome | Status | Notes |
|---|---|---|
| candidate accepted | SUPPORTED AS-IS | `decision_type="open_position"`, signal_ids links |
| candidate rejected | SUPPORTED AS-IS | `decision_type="skip"` (already used) |
| stale trade | SUPPORTED AS-IS | `decision_type="stale"` (new string, no schema change) |
| unknown outcome mapping | SUPPORTED AS-IS | `decision_type="unknown_outcome"` |
| insufficient liquidity | SUPPORTED AS-IS | `decision_type="insufficient_liquidity"` |
| excessive spread | SUPPORTED AS-IS | `decision_type="excessive_spread"` |
| insufficient remaining edge | SUPPORTED AS-IS | `decision_type="insufficient_edge"` |
| duplicate | SUPPORTED AS-IS | `decision_type="duplicate"` |
| market closed | SUPPORTED AS-IS | `decision_type="market_closed"` |
| market resolved | SUPPORTED AS-IS | `decision_type="market_resolved"` |
| wallet no longer qualifies | SUPPORTED AS-IS | `decision_type="wallet_no_longer_qualifies"` |
| portfolio risk rejection | SUPPORTED AS-IS | `decision_type="portfolio_risk_rejection"` |
| incomplete data | SUPPORTED AS-IS | `decision_type="incomplete_data"` |

**No new columns or tables needed.** decision_log was designed flexibly; only a typed enum/literal registry for `decision_type` is recommended for downstream parsing.

## PART 3.9 — Minimum real signal contract
Fields classified **required now** can already be populated from existing data (possibly with one schema column added). **Optional now** are useful but not blocking. **Future-only** require data we don't collect yet.

| Field | Where from | Class |
|---|---|---|
| `id` | uuid4 | required now (already in `signals.id`) |
| `market_id` (UUID) | `markets.id` via `source_trades.market_source_id` | required now (already in `signals.market_id`) |
| `wallet_id` (UUID) | `wallets.id` via `source_trades.trader_address` | required now — **NOT in current `signals` schema; add column** |
| `source_trade_id` | `source_trades.source_trade_id` | required now — **NOT in current `signals` schema; add column** |
| `outcome_label` | `source_trades.outcome` | required now — **add column (or reuse semantics)** |
| `token_id` | from `_build_asset_to_outcome_map` (clobTokenIds) | required now for unambiguous join — **NOT stored anywhere; must add `market_outcomes.clob_token_id`** |
| `side` | `source_trades.side` | required now — **add column** |
| `source_trade_price` | `source_trades.price` | required now — **add column** |
| `observed_current_price` | `market_outcomes.price` (refreshed) | required now — **add column; requires fresh fetch per candidate** |
| `expected_fill_price` | `risk/fill_model.py` | required now — **add column; new wiring** |
| `estimated_slippage` | `risk/fill_model.py` | required now — **add column; new wiring** |
| `remaining_edge` | computed: source_trade_price-vs-current-price − slippage − fees | required now — **add column; new wiring** |
| `wallet_score_version` | `CopyabilityScore.formula_version` ("v1") | required now — **add column** |
| `wallet_score` | `CopyabilityScore.score` | required now — **add column** |
| `wallet_verdict` | `CopyabilityScore.verdict` | required now — **add column** |
| `confidence` | currently placeholder | optional now — keep but mark as derived |
| `edge_estimate` | currently placeholder | optional now — keep but mark as derived |
| `predicted_prob` | currently placeholder | optional now — keep |
| `market_prob` | currently placeholder | optional now — keep |
| `reasoning` | currently placeholder | optional now — keep |
| `produced_at` | `now()` | required now (already in `signals.produced_at`) |
| `expires_at` | `produced_at + T` | required now — **add column** |
| `status` | enum: pending / approved / rejected / filled / expired | required now — **add column** |
| `idempotency_key` | hash(wallet_id, source_trade_id) | required now — **add column; `UNIQUE` constraint** |
| `is_sample` | from upstream | required now (already in `signals.is_sample`) |
| `decision_log_link` | `signal_ids` JSON in decision_log | required now (already exists in decision_log) |

**Future-only:** filled_quantity, actual_fill_price, actual_slippage, pnl_after_fill (need fill event), related_wallet_aggregation (need related-wallet detection result).

## PART 3.10 — Logical eligibility sequence

| # | Gate | Status |
|---|------|--------|
| 1 | Wallet verdict = COPY_CANDIDATE | **IMPLEMENTABLE NOW** — `scoring/engine.py:compute_verdict` returns this string |
| 2 | Source trade is new and not previously processed | **IMPLEMENTABLE NOW** — `UNIQUE(source, source_trade_id)` on `source_trades` already prevents upstream dupes; candidate-layer dedup needs the new `copy_candidates` table + `UNIQUE(wallet_id, source_trade_id)` |
| 3 | Market and outcome mapping are valid | **PARTIALLY IMPLEMENTABLE** — label-join works for binary markets; multi-outcome markets need `market_outcomes.clob_token_id` (BLOCKED) |
| 4 | Market is active, open, unresolved | **IMPLEMENTABLE NOW** — `markets.active`, `markets.closed`, `markets.resolved` |
| 5 | Source trade is recent enough | **IMPLEMENTABLE NOW** — `source_trades.timestamp` + `settings.staleness_seconds` |
| 6 | Current price is obtainable | **AVAILABLE BUT INCOMPLETE** — `market_outcomes.price` is the scan-time snapshot; needs fresh fetch per candidate (price_movement_since_scan is currently unbounded) |
| 7 | Liquidity and spread data are available | **BLOCKED ON DATA** — no bid/ask stored; `providers/bidask.py` exists but is not invoked from `_generate_signals`; only `markets.volume_24h` is a coarse liquidity proxy |
| 8 | Expected paper fill can be estimated | **BLOCKED ON 6+7** — `risk/fill_model.py` exists but needs current price + depth |
| 9 | Price has not moved beyond an acceptable amount | **BLOCKED ON 6+8** |
| 10 | Portfolio / risk checks can be previewed | **IMPLEMENTABLE NOW** for read-only portfolio checks (`positions`, `orders`, `settings.order_kill_switch`); but no candidate-level gate exists in `run_scan` today |
| 11 | Signal is persisted idempotently | **NEEDS SCHEMA** — `signals` table has NO unique constraint; need `idempotency_key` column + `UNIQUE` |
| 12 | Accepted and rejected decisions are logged | **IMPLEMENTABLE NOW** — `decision_log` table exists with flexible `decision_type` |

## PART 3.11 — Primary implementation-readiness classification

**#5 — MULTIPLE FOUNDATIONAL GAPS EXIST.**

Concrete evidence (≥3 blocking gaps, confirmed by code inspection):

1. **Token-to-outcome mapping is not persisted** — `market_outcomes` has no `clob_token_id` column; the join from `source_trades` to `market_outcomes` is label-based and only works after the in-memory `asset_to_outcome_map` rewrite. Multi-outcome markets (sports, esports, elections) cannot be unambiguously joined today.
2. **No copy-candidate persistence** — wallet scoring produces a verdict in-memory and discards it; `_generate_signals` never queries `wallets`, `source_trades`, or the verdict. There is no intermediate `(wallet, trade, market, outcome)` tuple anywhere in the schema.
3. **No current-price / spread refresh per candidate** — `_generate_signals` uses the scan-time `market_outcomes.price`. `providers/bidask.py` and `risk/fill_model.py` exist but are not wired in. Realistic copy economics (slippage-adjusted edge) cannot be computed from the current data path.
4. **No signal-level idempotency** — `signals` table has no unique constraint; re-running `_generate_signals` inserts fully-duplicate rows.
5. **Edge / confidence / predicted_prob / market_prob are all placeholders** — `edge = outcome.price - 0.5`, `predicted_prob == market_prob == outcome.price`, confidence = `min(outcome.price, 0.95)`, strength always "buy".

Not #1 (READY) — clearly not. Not #2 alone — gap (3) is independent. Not #3 alone — gaps (1), (2), (4), (5) are independent. Not #4 alone — gap (3) is independent. Not #6 — the evidence is conclusive.

## Recommended next engineering step (exactly ONE)

**Persist the `(wallet_id, source_trade_id, market_id, outcome_label, token_id, side)` tuple as a new `copy_candidates` table with `UNIQUE(wallet_id, source_trade_id)`, write a deterministic job that materializes `token_id` from the existing `asset_to_outcome_map` (and adds `market_outcomes.clob_token_id` so the map can be persisted), and a derivation of `predicted_prob`, `market_prob`, `edge_estimate`, `expected_fill_price`, `estimated_slippage` against fresh per-candidate price/spread fetched via `providers/bidask.py`.**

This is one narrow PR that closes gaps (1), (2), (4), and partially (3) and (5) without touching kill-switch, broker_mode, paper_mode, is_live, the scoring formula, or any threshold. The new `copy_candidates` table is purely additive (no destructive migration, no `DROP COLUMN`, no `market_outcomes.volume` removal). It does not implement any edge logic, does not approve any order, and does not change production behavior — production currently inserts 0 signals because the placeholder logic gates on `outcome.volume >= 10000` which is always 0; that gate is left untouched. Once `copy_candidates` exists with mapped outcomes, a separate follow-up PR can consume it into a real signal generator that emits real edge / confidence values and gates on `order_kill_switch`. Idempotency is established at the candidate stage (cheaper) rather than the signal stage.

## Explicit list — DO NOT INCLUDE IN FIRST IMPLEMENTATION PR

- enabling live trading / setting `is_live=True` / setting `broker_mode=polymarket`
- changing the kill switch (`order_kill_switch`)
- automatic paper-order approval (any path that bypasses `paper_mode=paper_manual`)
- changing thresholds in `scoring/engine.py` (`MAX_SHARPE`, `WEIGHTS`, `CRITICAL_FIELDS`, verdict boundaries)
- order-book / bid-ask redesign beyond the minimal `providers/bidask.py` wiring needed to fetch a current price
- broad schema cleanup (no `DROP COLUMN`, no wholesale renames, no forced migration of existing rows beyond adding the new table and one nullable `market_outcomes.clob_token_id` column)
- dropping `market_outcomes.volume`
- dashboard redesign (frontend untouched)
- external alerts / Telegram / webhooks
- scan-timeout optimization / `http_timeout_seconds` tuning
- unrelated collector fixes (do not touch `collect_smart_money_data.py` beyond the token-map persistence)
- changing the scoring formula (`formula_version`)
- recomputing any production signals
- changing `paper_mode`, `broker_mode`, or any other setting in `.env`
- touching Caddy / systemd / Hermes / production credentials

**Status reminder:** as of this audit, the paper pilot has **not** started. Every artifact
in the operational reports directory is planning/audit evidence; the production database
holds zero signals, zero orders, zero positions, and zero decision_log rows.