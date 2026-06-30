# run_scan Raw Snapshot Provenance Decision

## Decision

Do **not** add scanner-side raw trade snapshot writes in this card.

The current best next step is to keep `run_scan` runtime behavior unchanged and treat collector-side per-market Data API trade snapshots as the canonical raw-payload provenance path. If a later audit or release process needs to prove which raw snapshot backed a particular `run_scan`, prefer adding lightweight request/snapshot metadata or an explicit snapshot reference before duplicating raw payload files from the scanner.

## Why this is research-only

C2 was scoped as a design/research decision. The card explicitly says not to add scanner snapshots unless Todd/default approves after the decision report. This report therefore makes a recommendation and leaves runtime behavior unchanged.

## Current behavior with evidence

### Shared trade ingestion path

`run_scan` and the collector both use the shared live ingestion helper:

- `scripts/_live_ingest.py` says both `scripts/run_scan.py` and `scripts/collect_smart_money_data.py` must consume the same `PolymarketPublicAdapter` path.
- `scripts/run_scan.py::_fetch_trades` calls `fetch_recent_trades_for_market(...)` with `market_source_id`, epoch-zero `since`, `limit=200`, and the market's asset-to-outcome map.
- `scripts/collect_smart_money_data.py::collect_trades` uses the same adapter fetch path and branches on the same complete/partial/failed contract.

This means the scanner and collector already share the parsing, normalization, row identity, `takerOnly=false`, pagination, and partial-fetch handling rules.

### Collector already snapshots raw per-market trade payloads

The collector saves first-page raw trade provenance for each real per-market upstream fetch:

- `scripts/collect_smart_money_data.py::_snapshot_market_first_page(...)` performs a direct `GET /trades` call using the adapter data client.
- It uses `build_market_trade_params(...)`, the same helper used by `PolymarketPublicAdapter.fetch_trades_for_market`.
- The saved raw snapshot source is `polymarket_data_api`, endpoint `/trades`, with `market`, `limit`, `offset=0`, and `takerOnly=false` preserved in `raw_snapshots` metadata.
- Snapshot writes are best-effort and do not block ingestion when snapshot persistence fails.

This is stronger than the older global-window behavior: it is per-market and uses the same maker-inclusive request contract as the persisted/scored trade path.

### run_scan does not currently save raw trade snapshots

`run_scan` fetches and persists/scans normalized trades, but it does not call `_snapshot_market_first_page` or write `raw_snapshots` rows during scanner execution. Its evidence trail is therefore:

- normalized `source_trades` rows;
- deterministic source trade IDs;
- market fetch status counters and error/missing-data handling;
- decision/scoring outputs;
- collector-side raw Data API snapshots when the collector has run for the same market/request shape.

## Option evaluation

### Option 1 — Leave collector-only snapshots intentional

**Pros**

- No runtime change, no new disk writes, and no additional HTTP calls.
- Avoids duplicate raw payload storage when the collector has already captured per-market `/trades` provenance.
- Keeps `run_scan` focused on scanning/scoring instead of becoming a second provenance writer.
- Maintains the existing safety posture: snapshot failures cannot influence scanner behavior.

**Cons**

- A standalone scanner run has no scanner-owned raw payload file for the exact moment it fetched trades.
- Debugging a scanner-only run may require correlating with collector snapshots or re-running safe local fixtures.

**Assessment**: Best current default unless a concrete audit requirement demands scanner-owned raw files.

### Option 2 — Have run_scan reference collector snapshots

**Pros**

- Avoids duplicate payload files.
- Makes the existing provenance store more discoverable from scanner outputs.
- Could give scan reports a stable pointer to the collector's raw snapshot hash/file.

**Cons**

- Requires a matching strategy: market, limit, offset, `takerOnly=false`, time window, and content hash.
- Collector snapshots may not exist for a scanner-only workflow.
- If implemented naively, it can create a false sense that the snapshot and scan used the exact same upstream response.

**Assessment**: Good future direction if the project wants traceability without extra raw writes, but it needs explicit matching rules.

### Option 3 — Add scanner snapshots

**Pros**

- Produces scanner-owned raw evidence for standalone scanner runs.
- Simplifies forensic review when the question is strictly "what did this scan fetch?"

**Cons**

- Adds another outbound `/trades` request or captures a second raw path unless the adapter API changes to return raw payloads alongside parsed trades.
- Duplicates storage for markets the collector already snapshots.
- Adds runtime/file-growth risk to a scanner path that may run more frequently.
- Requires careful tests to ensure snapshot failures never change scan/paper-trading behavior.

**Assessment**: Justified only if Todd/default decide scanner-owned raw files are required for audit/compliance or if a future adapter refactor can capture raw payloads without a duplicate HTTP call.

### Option 4 — Record request metadata without duplicating raw payload files

**Pros**

- Captures the request contract (`market`, `limit`, `offset`, `takerOnly=false`, adapter version/commit, fetch status) without new raw payload files.
- Avoids duplicate storage and extra live API calls.
- Can include an optional pointer to an existing collector snapshot when a safe exact/near-exact match exists.

**Cons**

- Metadata alone cannot reconstruct an upstream payload.
- Requires careful wording so reviewers do not confuse metadata with raw provenance.

**Assessment**: Best future implementation shape if the gap becomes material but raw duplication remains undesirable.

## Recommendation

### Short term

Keep collector-only raw snapshots intentional and document the boundary:

- Collector owns raw per-market Data API `/trades` snapshots.
- `run_scan` owns scanning, persistence/scoring, and decision outputs.
- `run_scan` should not add duplicate raw snapshot writes without explicit approval.

### If the gap becomes material

Prefer a small scanner metadata/snapshot-reference feature before raw duplication:

1. record the exact `build_market_trade_params(...)` request contract used by a scan;
2. record fetch status (`complete`, `partial`, `failed`), pages fetched, and rows fetched;
3. optionally link to an existing `raw_snapshots` row only when request-shape matching is explicit;
4. keep snapshot/reference failures non-blocking and never change paper-trading/scoring behavior.

### Only add scanner-owned raw payload files if

- the project needs standalone scan forensic evidence independent of collector runs; or
- a future adapter refactor can return raw first-page payloads from the same HTTP response used for parsing, avoiding duplicate calls.

## Test plan for any future implementation

If Todd/default later approves code changes, require tests that prove:

- scanner and collector still use identical `build_market_trade_params(...)` values;
- `run_scan` does not treat partial fetches as complete;
- snapshot or metadata failures never block scanning/scoring;
- no production DB path, broker behavior, workflows, cron, timers, or services are touched;
- scanner/collector parsed trade identity remains byte-identical for the same raw rows.

## Final answer

No code change is justified in C2. Document the current boundary and defer runtime changes until a concrete audit requirement exists.
