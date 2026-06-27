# Copyability Scoring Formula v1

## Overview

The copyability score produces a deterministic 0-100 rating of how suitable a
wallet is as a copy-trading source. Higher scores = more confident the wallet
is worth copying.

## Score Composition

| Component | Weight | Formula | Quality |
|-----------|--------|---------|---------|
| sharpe_ratio | 20% | clamp(sharpe / 3.0 * 100, 0, 100) | CALCULATED |
| win_rate | 15% | clamp(win_rate * 100, 0, 100) | CALCULATED |
| trade_consistency | 15% | ramp(5→50→decay) | OBSERVED |
| data_recency | 15% | linear_decay(60s→3600s) | OBSERVED |
| data_completeness | 10% | present_count / total_expected * 100 | OBSERVED |
| volume_tenure | 10% | clamp(days_active / 30 * 100, 0, 100) | OBSERVED |
| market_correlation | 15% | ramp(1→5 markets, 40→100) | OBSERVED |

**All weights sum to 100.**

## Verdict Rules

Applied deterministically after computing the score:

1. Any critical missing field (sharpe_ratio, win_rate, trade_count) → **INCOMPLETE**
2. Score >= 70 AND no critical missing → **COPY_CANDIDATE**
3. Score >= 50 AND no critical missing → **WATCHLIST**
4. Score < 50 → **SKIP**

## Data Quality Tags

Each component is tagged:

- **OBSERVED**: directly measured from live/source data
- **CALCULATED**: derived deterministically from observed data
- **INFERRED**: derived from incomplete/heuristic sources
- **UNKNOWN**: field missing, capped to 0 for that component

## Missing Field Penalties

| Severity | Impact |
|----------|--------|
| critical | Forces verdict to INCOMPLETE regardless of score |
| major | Reduces weighted contribution by 50% |
| minor | Reduces weighted contribution by 30% |

## Staleness

- Trades older than 120s (configurable) are flagged `is_stale=True`
- Stale trades are still scored but the recency component decays to 0 at 1 hour

## Related Wallet Detection

Conservative heuristic (max confidence 0.75, only plausible >= 0.4 with >= 2 signals):

- shared_market: both wallets trade the same market
- similar_volume: within 20% quantity on same outcome
- close_timing: trades within 30 seconds
- shared_deposit: same funding source (if known)

Related wallets are *never* confirmed — only flagged as candidates for dedup avoidance.
