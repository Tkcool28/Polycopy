# PR66 wallet scoring evidence foundation

PR66 adds **evidence only**, not wallet scoring, candidate generation, paper
trading, orders, positions, services, or timers.

## Historical evidence collection (Checkpoint 2)

`ingest_wallet_evidence_history.py` is a **separate** evidence path for exactly
the wallet configured in `POLYCOPY_APPROVED_SOURCE_WALLET`. It has NO relationship
to copy-candidate creation, scoring, resolution, paper evaluation, snapshots, or
candidates. It supports **BUY and SELL** history (the recurring collector remains
strictly BUY-only — proven by a regression test).

### Live-read contract (explicit network gating)

- **No `--allow-live`** (default): the process makes NO HTTP/API call and opens
  NO database. It can run fully offline (optionally against `--input-file`).
  A dry-run in this mode is genuinely offline — `live_read_performed=false`.
- **`--allow-live`** without `--write`: performs a *bounded* live read, stays
  dry-run (`dry_run=true`, `committed=false`, `live_read_performed=true`). It is
  explicitly reported as a live read and is NEVER labeled "offline".
- **`--allow-live --write --confirm-production-db`**: the only combination that
  enables bounded `source_trades` persistence. Requires the operational lock and
  obeys all hard bounds.
- `--write` alone (or without `--confirm-production-db`) is rejected. `--write`
  never performs a live fetch on its own.
- A test seam `--mock-live --input-file` satisfies the live gate with a scripted
  provider (no real network) so the full authorized-write path is exercisable in
  CI without touching production.

### Real offset pagination (Checkpoint 2 fix)

Pagination uses the adapter's **true upstream offset**. `page * page_size` is
forwarded as the data-api `offset` so page 2+ request OLDER records upstream —
no local re-slice of page 0. `page_size` is a distinct bound from `max_records`.

Hard caps: `max_pages ≤ 5`, `max_records ≤ 250`. Defaults: `max_pages = 2`,
`max_records = 100`.

Stop reasons (one always reported): `empty_page`, `short_page`, `max_pages`,
`max_records`, `before_cutoff`, `after_cutoff`, `provider_error`, `completed`.
The data-api `/trades?user=` endpoint returns newest-first, so an `after`
(oldest) boundary enables safe early termination; a `before` (newest) boundary
is a filter-only guard (no premature stop) so the path is robust if upstream
ordering is ever not guaranteed.

### Duplicate classification

- `api_duplicate_count`: same canonical trade repeated within/across fetched pages.
- `db_duplicate_count`: canonical trade already present in `source_trades`
  (counted via a read-only dedup inspection; does not change INSERT OR IGNORE).
- Duplicates never create rows; BUY/SELL with genuinely distinct canonical inputs
  remain distinct; replay is idempotent; metadata-only variation keeps the same
  `source_trade_id` (first insert wins for metadata).

### Report contract (every run returns)

`wallet_prefix`, `pages_fetched`, `raw_records`, `normalized_records`,
`buy_count`, `sell_count`, `rejected_count`, `api_duplicate_count`,
`db_duplicate_count`, `would_insert`, `inserted`, `oldest_timestamp`,
`newest_timestamp`, `errors` (bounded: page, record index, error type, message),
`stop_reason`, `dry_run`, `live_read_performed`, `committed`, `duration_seconds`.
The full wallet address is never exposed (only `wallet_prefix`).

### Metadata preservation contract

The live provider returns raw data-api dicts verbatim so the Checkpoint-1
canonical serializer preserves upstream `event` (id/slug/title), `taxonomy`
(raw_category/raw tags), and `series` (id/slug/title/ticker). Event slug is
NEVER repurposed as a category label; unknown upstream fields are excluded;
metadata differences alone never change `source_trade_id`.

### Write purity

Authorized writes use the canonical source-trade writer and change ONLY
`source_trades`. `copy_candidates`, `candidate_price_snapshots`,
`candidate_price_snapshot_levels`, `trade_copyability_decisions`,
`paper_signal_decisions`, `wallet_score_decisions`,
`category_wallet_score_decisions`, `orders`, `positions`, and
`settlement_accounting_ledger` are provably unchanged (asserted in tests).

### No scoring or resolution

This checkpoint performs NO wallet scoring, NO category scoring, NO candidate
generation, NO paper evaluation, NO snapshots, and NO production write beyond
the bounded `source_trades` resolution below.

## Resolution (Checkpoint 3 — bounded source-trade resolution)

`resolve_source_trades.py` resolves a **bounded** set of `source_trades` rows
against the **trusted market-state evidence path**
`PolymarketPublicAdapter.get_market` — the *same* proven path PR24V/PR24W
reuse via `LiveGammaMarketStateProvider` — **NOT** the legacy
`ResolutionProvider.check_resolution` (which mistakenly hits
`GET /markets/{id}` as a *numeric* Gamma id and 422s on a hex condition id).
Truth is always re-derived via `derive_winner_from_market_payload` from the
provider's live market object. Persisted `markets.resolved` etc. are never the
authority.

- **Canonical routing contract (explicit, shape-based — no heuristic guessing):**
  * hex `0x` + 64 hex → condition-ID query-param lookup
    `GET /markets?condition_ids=<hex>` (list; exact-identity select). **This is
    the route for `source_trades.market_source_id` (Polymarket condition id).**
  * all-digits → numeric Gamma market-ID path lookup `GET /markets/{id}`.
  * anything else → `missing_market_identity`; we never call an incompatible
    endpoint with it.
  Token ids alone are **not** routable (`get_market` keys on conditionId); a
  row with only a token id is reported honestly as `missing_market_identity`
  (token→condition mapping is a deferred helper, mirroring PR24V's
  `unresolvable_token_id_only`). Provider results are de-duplicated by
  canonical identifier within one run (one call per unique market).
- **Error classification (an HTTP routing failure is NOT malformed truth):**
  * `routing_http_error` — endpoint reachable but returned non-2xx for the
    *route we chose* (404/422/400); carries `route_type`, `identifier_prefix`,
    `http_status`, reason. No truth guessed.
  * `provider_unavailable` — transport/5xx/timeout; provider unreachable.
  * `malformed_payload` — a 2xx response we could not parse into a valid
    Market/truth (bad JSON, unparseable, ambiguous selection).
  * `unavailable` — provider returned `None` (unknown / not-found).
- **BUY (resolved with a single winner):** uses the **frozen** helper
  `settle_source_trade_against_truth` unchanged. Persists
  `resolution_status`, `resolved_at`, `winning_token_id`, `is_winning_trade`,
  `realized_pnl`, `settlement_source`. P&L comes from the frozen helper's
  `(1-price)*qty` / `-price*qty` contract — never recomputed in the CLI.
- **SELL:** documentation-only evidence. No BUY settlement accounting is run;
  `is_winning_trade`, `realized_pnl`, and won/lost labels are never written.
  Counted as `unsupported_sell_accounting` and left exactly as-is.
- **Incomplete truth (no guess):** `unresolved` (still open), `unavailable`
  (provider None/404), `routing_http_error` (wrong route / non-2xx),
  `provider_unavailable` (transport/5xx), `malformed_payload` (unparseable
  2xx), `ambiguous` (multiple winning tokens), `missing_winning_token`
  (resolved but no winner derivable), `missing_market_identity` (no routable
  id) all produce **zero** mutations.
- **resolved_at semantics:** set to the provider observation timestamp
  (ISO-8601 `Z`). We do **not** fabricate the market's true resolution time;
  when untrustworthy, `resolved_at` stays null and `missing_resolution_timestamp`
  is incremented. `resolved_at` is an observation time, so it is excluded from
  the idempotency/conflict comparison (only resolution *facts* are compared).
- **Idempotency:** unresolved + complete truth → `would_update` (dry-run) /
  `updated` (apply, exactly once). Replay of identical truth → `identical_noop`.
  Already-resolved identical → `identical_noop`. Already-resolved conflicting →
  `conflicts`, never overwritten, with bounded field-level diff.
- **Apply authorization:** requires **all three** gates —
  `--allow-live` AND `--apply` AND `--confirm-production-db` — behind the
  operational job lock. No `--apply` without all gates. Dry-run is the default
  and uses a read-only SQLite URI (cannot migrate or mutate).
- **Write purity:** apply writes **only** `source_trades` via a plain
  `sqlite3` connection (never the project `Database` class, so no migration
  runs). `markets`, `market_outcomes`, copy candidates, snapshots, decisions,
  scores, signals, orders, positions, and the settlement ledger are never
  touched. Per-row conflict isolation: a conflicting/already-resolved row is
  skipped (rowcount 0) without aborting other eligible rows.
- **Bounds:** `--limit` default 50, hard cap 500 (CLI rejects out of range).
- **No resolution timer.** No production apply during development.

```bash
.venv/bin/python scripts/resolve_source_trades.py --allow-live --limit 10 --unresolved-only --json
.venv/bin/python scripts/resolve_source_trades.py --apply --allow-live --confirm-production-db --limit 50
```

### Routing correction (post-dry-run)

The first bounded live dry-run returned `malformed=10` on every request. Root
cause: `check_resolution` routed hex condition IDs to `GET /markets/{id}`
(numeric-id path) → HTTP 422. Fixed by switching to `get_market`, which routes
condition IDs to `GET /markets?condition_ids=<hex>`. After the fix the same
approved-wallet dry-run returns `routing_http_error=0`, `malformed_payload=0`,
`unresolved=10` (the wallet's markets are genuinely unresolved — correctly
classified, not a route failure), `errors=[]`.
