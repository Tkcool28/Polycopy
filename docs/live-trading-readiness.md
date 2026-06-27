# Live Trading Readiness

## Current State: NOT READY

Polycopy has **no real-money execution path**. The `DisabledLiveBroker`
raises on every operation. This is intentional and correct for the current
research phase.

Transitioning to live trading requires a separate implementation PR that
passes the review gates below. This document defines what must be true
before any real-money order can be placed.

## Review Gates

### Gate 1: Paper Performance Evidence

Before live trading, the operator must have:

- [ ] ≥30 days of continuous paper trading data
- [ ] Positive realized P&L across COPY_CANDIDATE wallets
- [ ] Counterfactual analysis showing COPY_CANDIDATE outperforms SKIP
- [ ] Win rate > 50% on COPY_CANDIDATE signals
- [ ] Sharpe ratio > 1.0 on paper portfolio
- [ ] Max drawdown documented and acceptable (< 20% of peak)
- [ ] Settlement rate > 95% (markets resolving as expected)

### Gate 2: Risk Infrastructure

- [ ] Exposure limits configured and tested (non-zero)
- [ ] Kill switch tested end-to-end (API + broker + dashboard)
- [ ] Position limits per market set (not unlimited)
- [ ] Global exposure cap set (not unlimited)
- [ ] Order size limit set (not unlimited)
- [ ] Rate limits on order placement
- [ ] Circuit breaker: auto-kill-switch on drawdown threshold
- [ ] Alerting on risk gate blocks (> 10 blocks/hour = investigate)

### Gate 3: Implementation Requirements

A live trading PR must include:

1. **Real `ExecutionBroker`** — replaces `DisabledLiveBroker` with a real
   Polymarket CLOB client that:
   - Authenticates with wallet private key (Polygon/EVM)
   - Signs orders with EIP-712 typed data
   - Places orders on the Polymarket CLOB
   - Handles partial fills and rejections
   - Respects position limits from RiskGate
   - Has configurable dry-run mode (sign but don't submit)

2. **Wallet integration** — live trading requires:
   - Funded Polymarket wallet with USDC
   - Private key stored in secrets manager (NOT .env, NOT code)
   - Approval for CLOB contract spending
   - Minimum balance check before order placement
   - Balance monitoring and low-balance alerts

3. **Monitoring and alerting** — live trading requires:
   - Real-time P&L dashboard (not just paper)
   - Order fill confirmation tracking
   - Position sync verification (our state vs. Polymarket state)
   - Stuck order detection and manual cancel workflow
   - Daily reconciliation report

4. **Operational runbook** — live trading requires:
   - Kill switch engagement procedure
   - Loss limit procedure (daily, weekly, total)
   - Market resolution dispute handling
   - Emergency wallet key rotation
   - Incident response playbook

### Gate 4: Review and Approval

- [ ] All Gate 1–3 items completed with evidence
- [ ] Code review by at least one additional reviewer
- [ ] Paper P&L vs. expected EV analysis written up
- [ ] Maximum position size and total exposure approved in writing
- [ ] Loss limit approved in writing (daily + total)
- [ ] Camera/screen-sharing blur guidance documented (see below)
- [ ] No `DisabledLiveBroker` removal — it stays as the safe default;
      live broker is loaded via explicit opt-in config only

## Camera / Screen-Sharing Blur Guidance

If demonstrating or screen-sharing the Polycopy dashboard while live
trading is enabled:

1. **Blur wallet addresses.** Full Ethereum addresses are identifying.
   Show only first/last 4 characters (e.g., `0x1a2b…c9d0`).
2. **Blur or hide the Settings page.** It contains config values that
   could reveal infrastructure details.
3. **Blur real balance amounts.** Show "●●●●" or relative percentages
   instead of exact USDC amounts.
4. **Never show the .env file or secrets.** This should go without
   saying, but: never open `.env`, `secrets.json`, or any file
   containing the wallet private key on a shared screen.
5. **Use demo data for presentations.** Run `seed_demo_data.py` and
   set `POLYCOPY_BROKER_MODE=paper` when presenting. The DEMO DATA
   banner confirms no real values are shown.
6. **OBS Studio blur filter.** If streaming, add a region blur filter
   over the wallet column in the dashboard. Test before going live.

## Gradual Rollout (If Approved)

1. **Week 1:** `research_only` mode, monitoring only. Verify data flows
   and wallet discovery are reliable.
2. **Week 2:** `paper_manual` mode. Operator manually approves every
   paper order. Confirm risk gates trigger correctly.
3. **Week 3:** `paper_auto` mode. Orders fill automatically after risk
   gates. Monitor fill rates and P&L.
4. **Week 4+:** If Gates 1–4 pass, enable live broker in dry-run mode
   (sign, don't submit). Verify order signing.
5. **Week 5+:** Enable live execution with minimum position size.
   Scale up only if performance matches paper.

## What Changes in a Live PR

| Component | Paper (Current) | Live (Future PR) |
|-----------|-----------------|------------------|
| Broker | `PaperBroker` (SQLite fills) | Real `ExecutionBroker` (CLOB API) |
| Live broker slot | `DisabledLiveBroker` (raises) | Real Polymarket CLOB client |
| Private key | Rejected by config validator | Required, from secrets manager |
| Fills | Instant in SQLite | Pending → filled/rejected on chain |
| Slippage | Deterministic from depth | Real market impact |
| Settlement | Idempotent, from evidence | On-chain resolution events |
| Balances | Sample/fictional | Real USDC balance |
| Kill switch | Engage/disengage in memory | Persist to DB/config for crash safety |

## What Does NOT Change

- Risk gates (same code path, same fail-closed logic)
- Scoring engine (same deterministic formula)
- Signal generation (same edge-based logic)
- Dashboard (same components, live data instead of paper)
- API (same endpoints, same response models)
- Counterfactual tracker (same what-if analysis)
- Decision log (same audit trail structure)
- Concurrency guard (same FileLock)

The difference is one adapter swap: `PaperBroker` → real `ExecutionBroker`.
Everything else is the same code path, tested with the same 283+ tests.
