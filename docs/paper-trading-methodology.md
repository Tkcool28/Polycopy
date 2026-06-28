# Paper Trading Methodology

## Overview

Polycopy simulates copy-trading Polymarket wallets without executing real
orders. This document describes the methodology: how we discover wallets,
score them, generate signals, simulate fills, track P&L, and settle
positions.

All results are paper-only. Paper P&L ≠ real P&L. This is a research
tool, not a trading system.

## 1. Wallet Discovery

### Sources

1. **Manual watchlist** — addresses in `POLYCOPY_MANUAL_WATCHLIST`. Operator
   adds wallets known to be profitable. Never auto-populated.
2. **Trade-based** — scan active Polymarket markets, extract wallet addresses
   from recent trades. Pure read-only, no auth required.
3. **Related-wallet heuristic** — detect wallets that may be the same entity
   across multiple addresses. Conservative: requires ≥2 signals and
   confidence ≥0.4.

### Deduplication

Wallets are deduplicated by address. Within a scan, trades are deduplicated
using a 60-second window with timestamp truncation to 60-second granularity
(`POLYCOPY_DEDUP_WINDOW_SECONDS`, `POLYCOPY_DEDUP_GRANULARITY_SECONDS`).

### Staleness

Trades older than `POLYCOPY_STALENESS_SECONDS` (default 120s) are flagged
`is_stale=True`. Stale trades are still scored but the data_recency
component decays to 0 at 1 hour.

## 2. Copyability Scoring

### Formula (v1)

Seven weighted components sum to a deterministic 0-100 score:

| Component | Weight | Formula |
|-----------|--------|---------|
| sharpe_ratio | 20% | clamp(sharpe / 3.0 × 100, 0, 100) |
| win_rate | 15% | clamp(win_rate × 100, 0, 100) |
| trade_consistency | 15% | ramp(5→50→decay) |
| data_recency | 15% | linear_decay(60s→3600s) |
| data_completeness | 10% | present_count / total_expected × 100 |
| volume_tenure | 10% | clamp(days_active / 30 × 100, 0, 100) |
| market_correlation | 15% | ramp(1→5 markets, 40→100) |

All weights sum to 100. No randomness. Same inputs → same score.

### Data Quality Tags

Each component is labeled with how it was derived:

| Tag | Meaning |
|-----|---------|
| OBSERVED | Directly measured from live/source data |
| CALCULATED | Derived deterministically from observed data |
| INFERRED | Derived from incomplete/heuristic sources |
| UNKNOWN | Field missing, scored as 0 for that component |

### Verdict Rules

1. Any critical missing field (sharpe_ratio, win_rate, trade_count) →
   **INCOMPLETE**
2. Score ≥ 70 AND no critical missing → **COPY_CANDIDATE**
3. Score ≥ 50 AND no critical missing → **WATCHLIST**
4. Score < 50 → **SKIP**

### Missing Field Penalties

| Severity | Impact |
|----------|--------|
| critical | Forces INCOMPLETE regardless of score |
| major | Reduces weighted contribution by 50% |
| minor | Reduces weighted contribution by 30% |

Full formula: `docs/copyability-scoring-v1.md`

## 3. Signal Generation

Signals are generated for COPY_CANDIDATE and WATCHLIST wallets when:

- The wallet has an active trade in a market
- The trade direction is identifiable
- The market has sufficient liquidity (bid/ask available)
- An edge can be computed (wallet's price vs. current market price)

Each signal contains:
- Wallet ID and address
- Market ID and question
- Outcome (Yes/No)
- Direction (buy/sell)
- Edge (price difference)
- Confidence (derived from copyability score)
- Signal strength classification

## 4. Paper Order Execution

### Flow

```
Signal → Preview → [Manual Review] → Approve/Reject → Fill → Position
```

### Preview (POST /paper/preview)

1. **Risk gate check** — kill switch, paper mode, exposure limits
2. **Fill quote** — compute expected fill price:
   - Base price from market bid/ask
   - Slippage based on order size vs. available depth
   - Fee at `POLYCOPY_FILL_FEE_RATE` (default 0.1%)
3. **Review delay** — in `paper_manual` mode, order cannot fill until
   `POLYCOPY_REVIEW_DELAY_SECONDS` (default 30s) after creation

### Approve (POST /paper/approve)

Uses SQLite-backed idempotency (persistent across restarts). If the same
request is replayed, the stored result is returned — no duplicate orders,
positions, or decision log entries are created.

On approval:
1. Order status transitions to FILLED
2. PnlTracker records the FIFO lot
3. Position is created or updated (average price recalculated)
4. DecisionLogEntry is created with signal IDs, rationale, and metrics
5. Counterfactual scenarios are computed for retrospective analysis

### Reject (POST /paper/reject)

Order status transitions to CANCELLED. DecisionLogEntry records the
rejection rationale.

### Paper Modes

| Mode | Behavior |
|------|----------|
| `research_only` | No orders can be created. Read-only. |
| `paper_manual` | Orders require explicit approve after review delay. Default. |
| `paper_auto` | Orders fill automatically after risk gates pass. |

## 5. Fill Model

### Market Depth

The fill model uses `MarketDepth` — a summary of the order book with
best price and depth levels. Each level has a price and available volume.

### Slippage

Slippage is computed based on order size relative to available depth:
- If order quantity ≤ best-level volume → no slippage
- Larger orders walk the book, paying progressively worse prices
- The fill price is the volume-weighted average across consumed levels

### Fees

A flat fee rate is applied to the notional value (price × quantity).
Default: `POLYCOPY_FILL_FEE_RATE=0.001` (0.1%).
Fee is deducted from the effective fill price.

### Conservative Mark

When `POLYCOPY_USE_CONSERVATIVE_MARK=true`, positions are marked at the
bid price (worst-case for longs) instead of mid. This provides a
pessimistic P&L estimate.

## 6. P&L Tracking

### FIFO Lots

Each buy creates a lot with price and quantity. Sells consume lots in
FIFO order:

```
Buy 10 @ 0.60  →  Lot 1: qty=10, price=0.60
Buy 15 @ 0.65  →  Lot 1: qty=10, price=0.60
                  Lot 2: qty=15, price=0.65
Sell 12 @ 0.80 →  Consumes Lot 1 fully (10 × (0.80-0.60) = +2.00)
                  Consumes 2 from Lot 2 (2 × (0.80-0.65) = +0.30)
                  Realized P&L: +2.30
```

### Unrealized P&L

Open positions are marked to market using `MarkEngine`:
- Mid price = (bid + ask) / 2 (default)
- Or bid price (conservative mark mode)
- Unrealized P&L = (mark - avg_price) × quantity

### Portfolio Summary

`POST /portfolio/summary` aggregates:
- Total positions count
- Total realized P&L
- Total unrealized P&L
- Total exposure (notional)
- Per-market breakdown

## 7. Settlement

### Market Resolution

When a Polymarket market resolves, `settle_paper_positions.py`:

1. Detects resolved markets (via public API or config)
2. Finds all open positions for resolved markets
3. Computes settlement using `SettlementEngine`

### Settlement Evidence

Each settlement requires `SettlementEvidence`:
- Source (e.g., "polymarket_gamma")
- Market source ID
- Resolution outcome (e.g., "Yes")
- Evidence hash (SHA-256 of source + market + outcome + timestamp)
- Observed-at timestamp (UTC)

### Idempotency

Settlement is idempotent. Re-running with the same evidence produces the
same result. Conflicting evidence (same position, different outcome) is
flagged as an error.

### P&L Calculation

For a resolved market:
- Winning outcome positions: payoff = quantity × (1.0 - avg_price)
- Losing outcome positions: payoff = -avg_price × quantity
- Plus accumulated fees

## 8. Counterfactual Analysis

For every wallet that receives a verdict, the `CounterfactualTracker`
computes what-if scenarios:

| Scenario | Description |
|----------|-------------|
| full_copy | What if we had fully copied this wallet? |
| skip | What if we had skipped them? (baseline) |
| half_size | What if we had copied at 50% size? |
| quarter_size | What if we had copied at 25% size? |

This enables retrospective analysis:
- Did COPY_CANDIDATE wallets actually outperform?
- Did SKIP wallets we missed have positive EV?
- What sizing fraction maximized risk-adjusted return?

Counterfactual results are stored alongside decision log entries.

## 9. Experiment Tracking

Each scan, portfolio update, and settlement run creates an `ExperimentRun`:

- Run ID (UUID)
- Status (running / completed / failed / partial)
- Start/end timestamps (UTC)
- Metrics dict (wallets scanned, signals generated, positions opened, etc.)
- Error log (partial failures)

This provides a time series of operational metrics for analysis.

## Limitations

1. **No latency modeling.** Paper fills assume instant execution. Real fills
   would experience queue position, partial fills, and latency.
2. **No funding rate.** Polymarket positions don't have funding rates, but
   opportunity cost of capital is not modeled.
3. **No market impact.** The fill model uses observed depth but doesn't
   model the feedback loop of our own trades moving the market.
4. **No slippage variance.** The fill model is deterministic. Real slippage
   varies with market conditions and order timing.
5. **Position sizing is fixed.** No Kelly criterion or dynamic sizing.
   The operator chooses quantity manually.
6. **Bullpen CLI not available.** The Bullpen adapter skeleton exists but
   the CLI tool was not found on the host. Any Bullpen-dependent features
   are stubbed.
