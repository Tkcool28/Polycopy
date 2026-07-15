# PR69 — Short-horizon specialist wallet discovery

PR69 adds a **report-only** discovery path for short-horizon Polymarket
specialists. It does not modify the approved-wallet list and cannot create
candidates, approvals, signals, orders, positions, or any database record.

## Policy and taxonomy

`polycopy.policy.short_horizon.evaluate_short_horizon` is the sole horizon
authority. Markets ending within 14 days are preferred; expected capital lock
is capped at 30 days, including the six-day resolution buffer. A missing,
invalid, ended, or long horizon fails closed. Historical reconciliation applies
the policy at the source trade timestamp. An early sale is not a resolution or
a win and cannot rescue a scheduled-long market.

`OfficialPolymarketTaxonomyResolverV1` accepts only explicit official market,
event, series category/root-tag evidence. Titles, questions, slugs, and
specific tags are never inferred as broad categories. Conflicts fail closed.

## Adapter and the confirmed live `/trades` contract (PR69 STEP 4)

`polycopy.discovery.adapter.DiscoveryAdapter` is the bounded, report-only
read-only adapter. It wraps the production `PolymarketPublicAdapter`
(Gamma + CLOB + data-api clients) and adds the discovery-specific reads
(wallet trades, closed positions, REDEEM activity, event/series lookup,
category leaderboards). It is GET-only, unauthenticated, fail-closed, and
budget-bounded; it never persists or schedules.

### Confirmed live `/trades?user=<wallet>` schema

Established by bounded live inspection during PR69:

- **Endpoint:** `GET https://data-api.polymarket.com/trades`
- **Query param:** `user=<wallet>` (the queried 0x-prefixed, lowercased address).
- **Response envelope:** a **raw JSON list** — `[ { "proxyWallet": ..., ... }, ... ]`.
  No wrapping `data`/`trades` object is guaranteed, so the parser accepts a raw
  list as the canonical shape and treats `{"data":[...]}` only as a defensive
  fallback inside `_extract_rows`.
- **Canonical wallet identity:** `proxyWallet` (the real 0x address).
- **Timestamp:** `timestamp` is a **Unix integer** (seconds); ISO strings are
  parsed only as a secondary fallback by the reconciler.
- **Market identity:** `conditionId` (hex 0x market identifier).
- **Other observed fields:** `side` ("BUY"|"SELL"), `asset` (CLOB token id),
  `size`, `price` (probability [0,1]), `outcome`, `outcomeIndex`,
  `transactionHash` (unique per trade; dedup key), `title`/`slug` (denormalized
  market metadata), `name`/`pseudonym` (user metadata, never address-bound).

### Wallet matching rules (fail-closed)

`DiscoveryAdapter.wallet_trades` normalizes rows against the queried wallet:

1. Compare `proxyWallet` **case-insensitively** to the queried wallet address.
2. Rows where `proxyWallet` is **absent** or **not a string** are **rejected**.
3. Rows where `proxyWallet` is present but **does not match** the queried
   wallet are **rejected**.
4. `makerAddress` / `takerAddress` are **never substituted** for wallet
   identity. They are retained only as secondary provenance inside the row.
5. Non-dict (malformed) rows are dropped (fail closed).

This guarantees a wallet's trade history contains only rows that provably
belong to that wallet under the canonical `proxyWallet` field. The reconciler
(`wallet_history.py`) independently re-verifies every retained row via
`extract_wallet_match_role` and fails closed on `unavailable`.

### Other endpoint contracts

- **Closed positions:** `GET /closed-positions?user=<wallet>` (raw list).
- **REDEEM activity:** `GET /activity?user=<wallet>&type=REDEEM` (raw list).
- **Market-first trades:** `GET /trades?market=<conditionId>&takerOnly=false`
  (per-market; `takerOnly=false` is mandatory so maker-side fills are kept).
- **Category leaderboard:** `GET /v1/leaderboard?category=&timePeriod=&orderBy=&limit=`
  (enum-validated client-side against `LEADERBOARD_CATEGORIES/PERIODS/ORDERS`).
- **Markets:** `GET /markets?active=true&closed=false&limit=&offset=&order=endDate&ascending=true`.
- **Event:** `GET /events?id=`. **Series:** `GET /series/<id>` (best-effort;
  absence is "series n/a", never "category n/a").

### Per-source response statuses

Every parser distinguishes: `complete`, `empty` (`SOURCE_EMPTY`, a valid empty
list — **not** an error), `partial`, `malformed`, HTTP error (`SOURCE_HTTP_ERROR`),
request-budget exhausted (`SOURCE_BUDGET_EXHAUSTED`), and unsupported schema
(`SOURCE_UNSUPPORTED_SCHEMA`). A valid empty list is reported as `empty`, never
as a failure. Each wallet's three sources (trades, closed-positions, REDEEM) emit
**independent** statuses and an independent `source_audit` row.

## Phased request budgeting

The operator CLI owns a single shared `_RequestBudget` (per-attempt decrement,
optional per-phase caps). Phase identifiers:

| Phase | Scope |
| --- | --- |
| `universe_taxonomy` | market universe + taxonomy (25%) |
| `market_first_trades` | market-first trade discovery (15%) |
| `leaderboards` | category leaderboards (15%) |
| `histories` | wallet `/trades` history (25%) |
| `closed_positions` | `/closed-positions` (8%) |
| `redeems` | `/activity?type=REDEEM` (7%) |
| `referenced_metadata` | referenced market metadata (5%) |

Every phase is allocated budget so the audit exercises all three wallet sources
and the referenced-market metadata path, not just the trade endpoint.

## Trades / closed-position / REDEEM reconciliation

`WalletHistoryFetcher` fetches the three sources per wallet in sequence and
reconciles them:

- **REDEEM-confirmed** (`/activity` row with `type=REDEEM` and matching
  `proxyWallet`) → a trade is promoted to **settled** evidence (win/loss from
  the `winning` marker).
- **Closed-position-only** (`/closed-positions` with `realizedPnl`, no REDEEM) →
  tagged **early-exit** (realized PnL kept, never labeled a settled win/loss).
- **Neither** → **unresolved** (retained for coverage, excluded from settled
  scoring inputs).
- Dedup is by exact public trade identity (tx hash + asset + conditionId + side
  + ts + price + size); closed-position + REDEEM cannot double-count a fill.
- A wallet whose `proxyWallet` role is `unavailable` across all three sources is
  surfaced as a hard `identity=unavailable` audit signal.

## No raw live payload fixtures committed

PR69 does **not** commit any raw live API payloads to the repository. Tests use
deterministic mocked-transport fixtures (`httpx.MockTransport`) and offline JSON
fixtures only. The live audit writes its report to an explicitly-named output
directory outside the repo (`/tmp/...`); those artifacts are never committed.

## Audit command

```bash
python scripts/audit_short_horizon_specialist_wallets.py
```

The default command performs **no network access, no DB access, and no file
writes**. For an offline deterministic fixture use `--input-file fixture.json`.

A bounded public-read audit is opt-in:

```bash
python scripts/audit_short_horizon_specialist_wallets.py \
  --allow-live --output-dir /tmp/polycopy-pr69-short-horizon-audit
```

Live mode is bounded by `--max-requests`, `--max-wallets`, `--history-days`,
`--leaderboard-top`, `--concurrency`, `--preferred-days`,
`--max-capital-lock-days`, and `--resolution-buffer-days`. It uses no
authenticated endpoint and writes only the explicit local output directory.
The report labels any unavailable realized-resolution evidence as incomplete.

### Live audit V3 parameters used in PR69

```
--allow-live \
--max-requests 300 --max-wallets 25 --history-days 365 \
--leaderboard-top 25 --concurrency 2 \
--preferred-days 14 --max-capital-lock-days 30 --resolution-buffer-days 6
```

Output: `/tmp/polycopy-pr69-short-horizon-audit-v3/`.

## Scoring and reconciliation

The pure `discover_short_horizon_specialists` engine accepts caller-provided
market payloads, market-first trades, and leaderboard seeds. It does not open a
DB or make HTTP calls. Exact public trade identity is the only dedupe key;
there is no early-exit-as-win shortcut. Only eligible, official-taxonomy
reconciled evidence reaches the existing frozen `wallet_score_v1` and
`category_wallet_score_v1` implementations. Their formulas are reused without
modification; incomplete evidence remains incomplete.

## Automation gate

PR69 is **report-only**. `ready_to_wire_to_automation` is `false` until a
separate review promotes it. No automatic approval, no DB writes, no bridge, no
orders, no positions.

## Known limitations

- The live `/trades` envelope was observed as a raw list during PR69; the parser
  also tolerates `{"data":[...]}` defensively, but no other undocumented
  alternate query parameters are assumed without bounded live proof.
- `timestamp` is consumed as a Unix integer; ISO-string timestamps are parsed
  only as a secondary fallback by the reconciler, not by the adapter.
- Gamma `end_date_min`/`end_date_max` filters are not honored by the live
  endpoint for the `active=true&closed=false` slice; temporal filtering is
  applied client-side.
- Series metadata is best-effort; its absence does not degrade category
  taxonomy classification.
