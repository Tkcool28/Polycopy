# Data Capability Audit

**Generated (UTC):** 2026-06-27T15:53:23Z
**Tool:** polycopy-probe/0.1
**Machine-readable copy:** `data/audits/data-capability-audit.json`

## Summary

| Probe | Endpoint | Status | Latency |
|-------|----------|--------|---------|
| gamma_markets_active | `GET /markets?active=true&closed=false` | OK | 74ms |
| gamma_markets_closed | `GET /markets?closed=true` | OK | 29ms |
| gamma_markets_top24h | `GET /markets?order=volume24hr&ascending=false` | OK | 38ms |
| gamma_events | `GET /events?active=true` | OK | 28ms |
| clob_markets | `GET /markets?next_cursor=MA==` | OK | 221ms |

All 5 documented public read-only endpoints responded successfully with no
authentication. No private interfaces, authenticated endpoints, or order
placement APIs were touched.

## Schema observations

### Gamma `/markets` (active)
- 90 top-level keys per market record.
- Key fields for paper trading: `id`, `question`, `outcomes`, `outcomePrices`,
  `volume24hr`, `active`, `closed`, `bestBid`, `bestAsk`, `spread`,
  `lastTradePrice`, `conditionId`, `slug`.
- `outcomes` and `outcomePrices` are JSON-encoded strings (not native arrays).

### Gamma `/events`
- 47 top-level keys per event record.
- Groups related markets under a common title/ticker.

### CLOB `/markets`
- Top-level shape: `{data, next_cursor, ...}` (4 keys).
- Per-market fields include `condition_id`, `question_id`, `question`,
  `minimum_order_size`, `minimum_tick_size`, `accepting_orders`.

## Tooling audit

| Tool | Available | Notes |
|------|-----------|-------|
| Bullpen CLI | **NOT FOUND** | Not installed on this host. Will need investigation in later phase. |
| Python | 3.11.15 | OK |
| httpx | 0.28.1 | OK |
| requests | 2.33.0 | OK |

## Unknowns / Risks

1. **Bullpen CLI** — not found. Need to determine if it's a separate install,
   a Polymarket-internal tool, or a community package. Blocks any Bullpen-
   dependent integration until resolved.
2. **Rate limits** — not tested. Public endpoints responded fast but sustained
   polling limits are unknown.
3. **CLOB authentication** — only the unauthenticated cursor-paginated market
   list was probed. Order placement / private endpoints were NOT touched.
4. **Historical data depth** — not yet probed. Gamma supports date filters but
   the oldest available data point is unverified.

## Safety

- No real-money trade execution path exists or was triggered.
- All data above is live public market data from Polymarket's documented API.
- No secrets, keys, or authenticated sessions were used.
