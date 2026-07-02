# Copy Candidate Contract (PR 2 of 6)

**Companion to:** `docs/paper_pilot/recovery_pr_sequence.md`
**Status:** PR 2 implemented as draft. NOT MERGED, NOT DEPLOYED.
**Code branch:** `feat/persist-copy-candidates`
**Schema version:** v7 → v8 (additive, idempotent)

This document defines the persisted artifact introduced by Recovery PR 2.
It is the durable contract between the wallet-verdict stage (PR 1) and the
fresh-price/slippage pipeline (PR 3+).

---

## 1. Identity contract

The copy-candidate table's idempotency key is the triple:

```
(wallet_id, source, source_trade_id)
```

- `wallet_id` is `wallets.id` (TEXT UUID).
- `source` is the source-trade provider key, e.g. `"polymarket_data_api"`.
- `source_trade_id` is the upstream-stable identifier within `source`
  (e.g. `"polymarket:<txhash>"`).

`source_trade_id` is **NOT** globally unique (two providers can legitimately
emit the same string — see PR 1's cross-source regression test).
`wallet_id` alone is insufficient (a wallet can have trades from multiple
sources). The triple is the only correct bounded idempotency key.

The schema enforces this with:

```sql
UNIQUE(wallet_id, source, source_trade_id)
```

Persistence uses `INSERT OR IGNORE`. Reruns are safe; the duplicate
path is a complete NO-OP for the audit log — see §5.

**Wallet ownership is canonically verified.** Before resolver success
can produce `PENDING_PRICE_CHECK`, the evaluator MUST confirm that
the candidate trade's `trader_address` matches the wallet's
`address` after both are passed through
`polycopy.db.wallet_identity.canonical_wallet_address` (lowercase,
strip whitespace, reject sentinel values). A trade from a different
wallet can NEVER become a candidate for that wallet — the candidate
status is `REJECTED_WALLET_TRADE_MISMATCH`.

This means a single source-trade row in `source_trades` may produce
candidate rows only for the wallets that canonically own the trade
(by `trader_address`). Two wallets with the same canonical address
(aliases) share the same `trader_address` and each can produce a
candidate row referencing the same `source_trade_internal_id`; two
distinct canonical addresses (truly different wallets) can never
share a candidate row for the same trade.

---

## 2. Bounded status set

PR 2 introduces a bounded `CandidateStatus` enum. Each candidate row has
exactly one status from this set:

| Status | Meaning |
|---|---|
| `PENDING_PRICE_CHECK` | COPY_CANDIDATE verdict, resolver OK, market active, trade valid. Awaiting PR 3's fresh-price/slippage stage. |
| `REJECTED_WALLET` | Wallet verdict ∈ {WATCHLIST, SKIP, INCOMPLETE}, OR an unknown / non-enum verdict string. Strict: only `Verdict.COPY_CANDIDATE` advances toward PENDING. |
| `REJECTED_WALLET_TRADE_MISMATCH` | Trade's canonical `trader_address` does not match the wallet's canonical `address` (or the trade has no `trader_address`). Hard rejection — a trade from Wallet A can never become a candidate for Wallet B. |
| `REJECTED_UNRESOLVED_OUTCOME` | Resolver returned INCOMPLETE (token not found, market unknown, etc.). |
| `REJECTED_AMBIGUOUS_OUTCOME` | Resolver returned AMBIGUOUS (multiple outcomes match the same token). |
| `REJECTED_MARKET_CLOSED` | The actual DB row for the resolved market indicates `closed = 1`, `resolved = 1`, or `active = 0`. The supplied `Market` object's id MUST match the resolver's `market_id` — a mismatched Market is also rejected. |
| `REJECTED_STALE_TRADE` | Reserved for future use; not emitted yet. PR 2 does NOT invent a recency threshold. |
| `REJECTED_INVALID_TRADE` | `price <= 0`, `quantity <= 0`, missing `timestamp`, or invalid `side`. |

PR 2 does NOT use `APPROVED` / `APPROVED_MANUALLY` — no order or signal is
approved in PR 2.

PR 3+ may add statuses (e.g. `REJECTED_PRICE_DRIFT`, `REJECTED_NO_LIQUIDITY`)
once fresh price data exists.

---

## 3. Fields (canonical columns)

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | NO | Set after insert. |
| `wallet_id` | TEXT | NO | FK → `wallets(id)`. |
| `source` | TEXT | NO | Provider key. |
| `source_trade_id` | TEXT | NO | Upstream-stable id within `source`. |
| `source_trade_internal_id` | TEXT | YES | FK → `source_trades(id)`. The internal UUID used for fast joins. |
| `market_id` | TEXT | YES | FK → `markets(id)`. NULL when rejected at upstream stages. |
| `market_outcome_id` | INTEGER | YES | FK → `market_outcomes(id)`. |
| `market_source_id` | TEXT | YES | `markets.source_id` (conditionId) snapshot. |
| `token_id` | TEXT | YES | `market_outcomes.clob_token_id` snapshot. |
| `outcome_label` | TEXT | YES | E.g. "Yes", "Hanwha Eagles". |
| `side` | TEXT | NO | "BUY" or "SELL". |
| `source_trade_price` | REAL | NO | Observed trade price. |
| `source_trade_quantity` | REAL | NO | Observed trade quantity. |
| `source_trade_notional` | REAL | YES | `price * quantity` if not stored upstream. |
| `source_trade_timestamp` | TEXT | NO | `source_trades.timestamp` (ISO-8601 UTC). |
| `observed_at` | TEXT | NO | Insertion time (ISO-8601 UTC). |
| `wallet_score_version` | TEXT | NO | E.g. `"v1"`. |
| `wallet_score` | REAL | NO | 0..100, snapshot. |
| `wallet_verdict` | TEXT | NO | COPY_CANDIDATE / WATCHLIST / SKIP / INCOMPLETE. |
| `status` | TEXT | NO | From the bounded CandidateStatus enum. |
| `status_reason` | TEXT | YES | Short human-readable reason. |
| `metrics_json` | TEXT | YES | JSON-encoded metrics (trade_age_seconds, resolver_status, etc.). |
| `created_at` | TEXT | NO | Insertion time. |
| `updated_at` | TEXT | NO | Last update time. |

**Historical score snapshot**: the wallet score, verdict, and formula
version are snapshot at evaluation time. Subsequent reruns do NOT silently
rewrite history; `INSERT OR IGNORE` preserves the original row.

---

## 4. Foreign keys

All FK columns are nullable on purpose. A rejected candidate (e.g.
resolver INCOMPLETE) has no market/outcome to reference, so the FK
columns are NULL while the constraints are still declared.

SQLite enforces FK only when `PRAGMA foreign_keys=ON`. The `Database`
class sets this on connect.

| FK | ON DELETE |
|---|---|
| `wallet_id` → `wallets(id)` | (default — RESTRICT in SQLite if declared; verify) |
| `source_trade_internal_id` → `source_trades(id)` | (default) |
| `market_id` → `markets(id)` | (default) |
| `market_outcome_id` → `market_outcomes(id)` | (default) |

PR 2 does NOT add `ON DELETE CASCADE`; foreign keys are preserved by
default. If a market or wallet is later deleted, the FK constraint will
reject the delete (or set the column to NULL if declared that way) —
which is the correct behavior for a persistent audit log.

---

## 5. Decision-log behavior

PR 2 emits bounded decision types via the existing `decision_log`
table. The full vocabulary:

```
COPY_CANDIDATE_CREATED
COPY_CANDIDATE_REJECTED_WALLET
COPY_CANDIDATE_REJECTED_WALLET_TRADE_MISMATCH
COPY_CANDIDATE_REJECTED_UNRESOLVED_OUTCOME
COPY_CANDIDATE_REJECTED_AMBIGUOUS_OUTCOME
COPY_CANDIDATE_REJECTED_MARKET_CLOSED
COPY_CANDIDATE_REJECTED_STALE_TRADE
COPY_CANDIDATE_REJECTED_INVALID_TRADE
```

`COPY_CANDIDATE_DUPLICATE_SKIPPED` was removed from the persisted
vocabulary in this revision. A duplicate rerun does NOT append any
additional `decision_log` row — that was previously claimed to be
"no flood" but in practice still wrote one row per scheduled scan.
The current behavior is genuinely idempotent.

**FK safety.** `decision_log.market_id` is `NOT NULL REFERENCES
markets(id)` with `PRAGMA foreign_keys=ON` enforced. For rejected
evaluations that have no real `market_id` (i.e. the rejection
happened before resolver-OK attribution — ownership mismatch,
invalid trade, unresolved outcome, ambiguous outcome), the helper
`record_candidate_decision_log` returns `None` and does NOT insert a
`decision_log` row. The `copy_candidates` row itself (with
`status`, `status_reason`, `metrics_json`) is the durable audit
artifact for these pre-attribution rejections. We never invent a
fake market id.

For candidates that DO have a real `market_id` (PENDING_PRICE_CHECK
and REJECTED_MARKET_CLOSED), the helper enforces app-level
idempotency keyed on `(wallet_id, source, source_trade_id,
decision_type)`: if a row with the same identity has already been
recorded, the call returns `None` without inserting. The first
candidate insert writes one bounded decision event (CREATED or
REJECTED_*); subsequent reruns that hit the unique-key collision
write nothing.

Each persisted decision_log row carries a `metrics_json` payload
with relevant evidence: source, source_trade_id, timestamp, age,
price, quantity, market/outcome identity, wallet score, formula
version, verdict, resolver status.

The `decision_type_for_status()` helper is the single source of
truth that maps each CandidateStatus to its decision_type string.

---

## 6. Market-state verification (real DB lookup)

The candidate can only become `PENDING_PRICE_CHECK` after the
actual resolved market row in the database is verified as open.
Concretely:

- The canonical resolver returns `market_id` (or `INCOMPLETE` /
  `AMBIGUOUS`, which short-circuit earlier).
- The evaluator loads the market row from `markets` by that
  `market_id` and verifies `active = 1`, `closed = 0`,
  `resolved = 0`.
- If a `Market` object is also supplied by the caller, the
  evaluator verifies the supplied `Market.id` matches the
  resolver's `market_id` — a mismatched open Market cannot
  override a closed/resolved DB market.
- Passing `market=None` is allowed and the DB lookup is
  authoritative. `market=None` does NOT silently mean "open market".
- If the resolved market row is missing in the DB (data integrity
  issue), the candidate is `REJECTED_UNRESOLVED_OUTCOME`.

Tests covering this gate: `test_closed_market_blocks_pending_price_check`,
`test_inactive_market_blocks_pending`, `test_resolved_market_blocks_pending`,
`test_unrelated_market_object_cannot_override_resolved_market`,
`test_market_none_does_not_bypass_closed_market`,
`test_missing_market_row_blocks_pending`.

---

## 7. What this PR does NOT calculate

PR 2 is a persistence layer, not a pricing layer. It deliberately does
NOT compute or persist:

- `predicted_prob`
- `market_prob`
- `expected_value`
- `edge_estimate`
- `expected_fill_price`
- `spread`
- `slippage`
- `signal_id` (no signal is created)

These belong to PR 3+ once fresh price data exists.

PR 2 also does NOT:

- Approve any paper order.
- Create positions.
- Trigger the kill switch.
- Enable live trading.
- Recompute or alter the wallet scoring formula or its thresholds.
- Reinterpret verdict boundaries.

---

## 8. Boundary between PR 2 and PR 3

| Concern | PR 2 (this) | PR 3 (future) |
|---|---|---|
| Persist candidate row | YES | — |
| Eligibility checks (wallet verdict, resolver, market state, trade validity) | YES | — |
| Snapshot historical wallet score/verdict/formula_version | YES | — |
| Fetch fresh bid/ask/price | NO | YES |
| Compute slippage estimate | NO | YES |
| Compute expected fill price | NO | YES |
| Compute edge / predicted_prob / expected_value | NO | YES |
| Promote PENDING_PRICE_CHECK → actionable signal | NO | YES |
| Replace `_generate_signals` placeholder in `scripts/run_scan.py` | NO (placeholder byte-for-byte unchanged) | NO (PR 4) |

PR 3 will read PENDING_PRICE_CHECK rows, attach fresh price data,
compute slippage and edge, and (via PR 4) hand off to a real signal
generator that replaces the placeholder.

---

## 8. Production behavior statement

**Merging PR 2 alone changes ZERO production behavior.**

- The candidate-persistence code is NOT called from `scripts/run_scan.py`
  or any production timer.
- The placeholder `_generate_signals(markets)` in `run_scan.py` is
  byte-for-byte unchanged.
- The new code path is reachable only from tests and from a future
  controlled roll-out (PR 3+).
- The `copy_candidates` table is created (additive) but remains empty
  in production until PR 3+ wires up scan persistence.

Deployment of candidate generation into the live scan flow is a
separate, future PR that requires explicit approval and a bounded dry run.

---

## 9. Out-of-band schema migration disclosure (PR 2 development)

During the development of PR 2, the production DB schema was
accidentally upgraded from v7 to v8 by a stray connection during
implementation. The migration was additive (no data loss), the new
`copy_candidates` table is empty (`count=0`), and no
signals/orders/positions/decision_log rows were created.

Production code (still at `main` head `fa3f2101`, SCHEMA_VERSION=7) is
now behind by one schema version (DB schema_version=8). The proper fix
is a controlled code fast-forward to `feat/persist-copy-candidates`
once this PR is approved and merged — same pattern as the PR #13
repair (code/schema alignment is a deployment step, not a code step).

This disclosure is repeated in the PR body and in the parent task's
final report.

---

## 10. Safety invariants

- `broker_mode=paper`
- `paper_mode=paper_manual`
- `order_kill_switch=True`
- `is_live=False`
- Production `signals=0`, `orders=0`, `positions=0`, `decision_log=0`
  (verified via read-only SQL at every step).
- No production service, timer, Caddy rule, systemd unit, .env, or
  Hermes gateway modified.
- No paper order approved. No live trade placed. No kill switch
  disabled. No thresholds changed. No scoring formula changed.
- Paper pilot has NOT started.

---

## 11. PR 3 prerequisite status

PR 3 (fresh price/spread/fill/slippage) is **NOT STARTED**. It requires
PR 2 to be merged and deployed before it can read PENDING_PRICE_CHECK
rows from production.

The 12-point pilot-readiness gate from `recovery_pr_sequence.md` § 12
is not yet satisfied. The paper pilot has not started.