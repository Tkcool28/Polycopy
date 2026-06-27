# Polycopy Strategy

## Mission

Build a paper-trading platform that identifies profitable "smart money" wallets
on Polymarket, scores their copyability, and simulates following their trades
— without ever executing real orders. The goal is empirical evidence: does
copy-trading these wallets produce positive expected value?

## Core Hypothesis

Some wallets on Polymarket prediction markets consistently trade ahead of
resolution with positive edge. By identifying these wallets (smart-money
discovery), scoring their reliability (copyability scoring), and tracking
paper trades that mirror their positions, we can measure whether copying
them would have been profitable — before risking capital.

## Strategic Principles

### 1. Paper First, Always

No real-money execution path exists in the codebase. The `DisabledLiveBroker`
is the default live broker — it raises on every operation. `PaperBroker`
writes to SQLite. Even if someone misconfigures the system, the fail-closed
validators in `Settings` reject private keys when `broker_mode=paper`.

This is not a toggle — it's an architectural constraint. Live trading
requires a separate implementation PR that replaces `DisabledLiveBroker`
with a real `ExecutionBroker`, which must pass review gates documented
in `docs/live-trading-readiness.md`.

### 2. Fail-Closed Risk Gates

Every order passes through `RiskGate.check()` before execution. The gates
are evaluated in priority order — first block wins:

1. Kill switch (global off switch)
2. Paper mode (`research_only` blocks all orders)
3. Exposure limits (per-market, per-wallet, per-outcome, global, order size)

On error or missing data, the default is BLOCK. There is no "default allow"
path.

### 3. Observable, Not Secret

All data flows are observable:
- Raw API snapshots saved with SHA-256 provenance hashes
- Decision log records every trade rationale with signal IDs and metrics
- Experiment run records capture scoring context and outcomes
- Counterfactual tracker computes what-if scenarios for every verdict

No proprietary magic. The scoring formula is deterministic and fully
documented in `docs/copyability-scoring-v1.md`.

### 4. Sample Data Is Labeled

Every record from `seed_demo_data.py` carries `is_sample=True`. Dashboard
banners show DEMO DATA. Scripts that fail to reach live APIs log the failure
and leave data missing — they never silently substitute fictional prices
or markets.

### 5. Idempotent Operations

- Settlement is idempotent — re-running with the same evidence produces the
  same result.
- Demo data seeding supports `--force` for idempotent replays.
- API state-changing endpoints use `X-Idempotency-Key` to prevent duplicate
  submissions within a 5-minute window.

## Data Pipeline

```
Polymarket Public API (read-only)
    │
    ▼
collect_smart_money_data.py
    │  Fetch markets → trades → wallet balances
    │  Save raw snapshots with SHA-256 provenance
    │
    ▼
run_scan.py
    │  Wallet discovery (multi-source, dedup)
    │  Trade detection (staleness + dedup)
    │  Copyability scoring (deterministic 0-100)
    │  Verdict assignment (COPY_CANDIDATE / WATCHLIST / SKIP / INCOMPLETE)
    │  Signal generation (edge-based)
    │
    ▼
[Manual] Paper order preview → approve/reject
    │
    ▼
update_paper_portfolio.py
    │  Mark-to-market for open positions
    │  Check pending orders for review-delay eligibility
    │
    ▼
settle_paper_positions.py
       Settle resolved markets (idempotent)
```

## Scoring Strategy

The copyability scoring engine (`scoring/engine.py`) produces a deterministic
0-100 score from seven weighted components:

| Component | Weight | Quality Tag |
|-----------|--------|-------------|
| Sharpe ratio | 20% | CALCULATED |
| Win rate | 15% | CALCULATED |
| Trade consistency | 15% | OBSERVED |
| Data recency | 15% | OBSERVED |
| Data completeness | 10% | OBSERVED |
| Volume tenure | 10% | OBSERVED |
| Market correlation | 15% | OBSERVED |

Verdict is deterministic: score ≥ 70 → COPY_CANDIDATE, ≥ 50 → WATCHLIST,
< 50 → SKIP, missing critical fields → INCOMPLETE.

Full formula: `docs/copyability-scoring-v1.md`

## Wallet Discovery Strategy

1. **Manual watchlist** — `POLYCOPY_MANUAL_WATCHLIST` env var with known
   addresses. Never auto-discovered.
2. **Trade-based discovery** — scan active markets, identify wallets from
   recent trades.
3. **Related-wallet heuristic** — conservative: requires ≥2 signals and
   confidence ≥0.4. Types: shared_market, similar_volume, close_timing,
   shared_deposit. Max confidence 0.75. Never confirmed — only flagged.

## Paper Trading Execution Flow

```
Signal detected (COPY_CANDIDATE or WATCHLIST wallet)
    │
    ▼
Paper order preview (POST /paper/preview)
    │  RiskGate.check() — must pass
    │  FillModel.quoteFill() — compute fill with slippage/fees
    │  ReviewDelay — hold for 30s in paper_manual mode
    │
    ▼
[Manual] Approve or Reject
    │
    ▼
Order fills (or is rejected)
    │  PnlTracker.record_buy/sell — FIFO lots
    │  Position updated
    │  Decision log entry recorded
    │  Counterfactual scenarios computed
```

## What This Is Not

- **Not a trading bot.** No auto-trading even in `paper_auto` mode — it
  only auto-fills paper orders that were explicitly previewed and approved.
- **Not a signal service.** Signals are for internal paper evaluation only.
- **Not a backtesting engine.** Positions are tracked forward from the
  moment of discovery. Historical replay is a future capability.
- **Not investment advice.** This is a research tool. Paper P&L does not
  predict real P&L.

## Next Phases

- **P09:** Documentation (this phase)
- **P10:** NxHermes review of P09 docs
- **Beyond:** Live trading readiness review (separate PR, see
  `docs/live-trading-readiness.md`)
