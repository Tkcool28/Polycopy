# PR24X — Source-Trade Ingestion Writer Audit

**Audit version:** PR24X-1  
**Repo root:** `/root/Polycopy`  
**Status:** SAFE / PARKED / PAPER-ONLY / NON-AUTOMATED  
**Mode:** report-only / design-only / audit-only

## 1. DB Safety Layer
- WAL exists: **True** (PRAGMA journal_mode=WAL in `Database.connect()`)
- busy_timeout exists: **30000 ms**
- wal_autocheckpoint exists: **1000**
- foreign_keys=ON: **True**
- WAL sufficient alone: **False** (WAL helps, does NOT make SQLite multi-writer)
- Application-level single-writer rule still required: **YES**

## 2. source_trades Write Path Classification
### production_write_path (4)
- `scripts/backfill_resolution_truth.py:449` — `update` (uses Database.connect: True, sample seed: False)
- `scripts/collect_smart_money_data.py:703` — `insert or ignore into` (uses Database.connect: True, sample seed: False)
- `scripts/live_smoke_pr3_fixes.py:202` — `insert or ignore into` (uses Database.connect: True, sample seed: False)
- `scripts/run_scan.py:1419` — `insert or ignore into` (uses Database.connect: True, sample seed: False)

### sample_test_seed_path (1)
- `scripts/seed_demo_data.py:472` — `insert or replace into` (uses Database.connect: False, sample seed: False)

### migration_schema_path (1)
- `src/polycopy/db/schema.py:432` — `insert into` (uses Database.connect: True, sample seed: False)

### test_temp_db_only (95)
- `tests/test_p01_trade_outcome_identity.py:154` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:365` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:422` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:484` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:542` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:593` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:670` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:738` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:758` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:969` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:1045` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:1076` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:1185` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:1197` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p01_trade_outcome_identity.py:1287` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p02_copy_candidates.py:186` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p02_copy_candidates.py:403` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p03_price_snapshots.py:211` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p03_price_snapshots.py:463` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_behavior_classification.py:612` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_chunk4_runtime_paper_signal.py:112` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_constraints.py:410` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_constraints.py:450` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_constraints.py:491` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_full_contract.py:49` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_immutability.py:50` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_migration_matrix.py:157` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p04_migration_matrix.py:200` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p10_category_schema_migration.py:147` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p10_category_schema_migration.py:405` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:131` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:236` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:324` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:368` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:411` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:435` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:479` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:589` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:755` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:773` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:824` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:1036` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:1096` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:1152` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:1211` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_fixes.py:1279` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_trade_ingestion.py:367` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p21_trade_ingestion.py:382` — `insert or ignore into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:119` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:491` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:578` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:726` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:811` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:927` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1043` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1180` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1190` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1208` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1227` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1246` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1423` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1435` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1615` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_backfill.py:1768` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24a_resolution_truth_schema.py:404` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24b_query_memory_guards.py:719` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24b_query_memory_guards.py:789` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24i_settlement_accounting_ledger.py:55` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24j_wallet_accounting_coverage.py:20` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24j_wallet_accounting_coverage_script.py:22` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24q_trade_copyability_review_report.py:133` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24r_trade_copyability_bridge_audit.py:152` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24r_trade_copyability_bridge_audit.py:202` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24s_trade_copyability_snapshot_evidence_bridge.py:120` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24u_trade_copyability_real_snapshot_collection_bridge.py:88` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24u_trade_copyability_real_snapshot_collection_bridge.py:498` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24v_trade_copyability_market_state_evidence_bridge.py:90` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24v_trade_copyability_market_state_evidence_bridge.py:524` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24w_source_trade_real_coverage_mapping_audit.py:90` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p24x_source_trade_ingestion_writer_audit.py:93` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p30_wallet_identity_normalization.py:241` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p30_wallet_identity_normalization.py:266` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p30_wallet_identity_normalization.py:300` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p31_wallet_uniqueness.py:177` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p31_wallet_uniqueness.py:202` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_p32_wallet_normalization_matrix.py:283` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_pr19_legacy_step5_bound.py:100` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_pr19_legacy_step5_bound.py:715` — `delete from` (uses Database.connect: False, sample seed: False)
- `tests/test_pr19_legacy_step5_bound.py:719` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_pr19_legacy_step5_bound.py:885` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_pr19_legacy_step5_bound.py:1000` — `delete from` (uses Database.connect: False, sample seed: False)
- `tests/test_pr19_legacy_step5_bound.py:1004` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_pr5_pipeline_wiring.py:130` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_pr5_pipeline_wiring.py:670` — `insert into` (uses Database.connect: False, sample seed: False)
- `tests/test_v9_schema_bridge.py:116` — `insert into` (uses Database.connect: False, sample seed: False)

## 3. Collectors / Fetchers
- **PolymarketPublicAdapter** (`src/polycopy/adapters/polymarket.py`): fetcher-only safe
  - reads: True, writes source_trades directly: False
  - tables touched: []
  - Fetches market/trade/resolution data via Gamma+CLOB APIs. No DB writes anywhere in adapters/ (verified).
- **PolymarketClobAdapter.fetch_book** (`src/polycopy/adapters/polymarket_clob.py`): fetcher-only safe
  - reads: True, writes source_trades directly: False
  - tables touched: []
  - Fetches CLOB order book only. No DB writes.
- **BullpenReadOnlyAdapter** (`src/polycopy/adapters/bullpen.py`): fetcher-only safe
  - reads: True, writes source_trades directly: False
  - tables touched: []
  - ReadOnly adapter implementing provider protocols. No writes.
- **RealSnapshotEvidenceCollector** (`src/polycopy/engine/trade_copyability_real_snapshot_collection_bridge.py`): fetcher-only safe
  - reads: True, writes source_trades directly: False
  - tables touched: []
  - Live CLOB book collector used by PR24U bridge. Fetch-only.
- **run_scan.ScanPipeline** (`scripts/run_scan.py`): WRITES DB
  - reads: True, writes source_trades directly: True
  - tables touched: ['source_trades', 'wallets', 'markets']
  - PRODUCTION writer. Calls _persist_trade() (run_scan.py:1419) directly. Also persists wallets/markets. Must be refactored so ingestion delegates to the single shared writer.
- **collect_smart_money_data.run_collection** (`scripts/collect_smart_money_data.py`): WRITES DB
  - reads: True, writes source_trades directly: True
  - tables touched: ['source_trades', 'wallets', 'markets']
  - PRODUCTION writer. Calls _persist_trade() (collect_smart_money_data.py:703) directly. Duplicate of the run_scan writer. Must be refactored to the shared writer.
- **wallet_discovery** (`src/polycopy/discovery/wallet_discovery.py`): fetcher-only safe
  - reads: True, writes source_trades directly: False
  - tables touched: []
  - Reads/normalizes wallet discovery inputs. No source_trades writes.
- **backfill_resolution_truth** (`scripts/backfill_resolution_truth.py`): WRITES DB
  - reads: True, writes source_trades directly: True
  - tables touched: ['source_trades']
  - UPDATEs source_trades resolution columns (line 449). Settlement-stage, not ingestion. Must remain a separate, explicit, single-owner writer.

## 4. Safe Ingestion Architecture
```
Fetcher(s) -> Normalizer -> Validator -> Batch -> Single SourceTrade Writer
```
- **Fetcher(s)** (no writes): Pull raw trade/wallet/activity data from APIs. No DB writes.
- **Normalizer** (no writes): Map raw payloads to the source_trade contract. No DB writes.
- **Validator** (no writes): Reject/flag rows failing contract. No DB writes.
- **Batch** (no writes): Group valid normalized rows into bounded batches. No DB writes.
- **Single SourceTrade Writer** (MAY WRITE): ONLY component allowed to INSERT source_trades. Uses Database.connect() so WAL/busy_timeout apply. Idempotent, one transaction per batch, explicit write flag, dry-run.

## 5. source_trade Contract
- Required fields: ['source', 'source_trade_id', 'trader_address', 'market_source_id', 'token_id', 'side', 'price', 'size', 'timestamp', 'outcome', 'is_sample']
- Dedupe key (preferred): `source + source_trade_id`
- Dedupe key (fallback): `wallet + token/condition + side + price + size + timestamp`
- PR24U-ready: token_id present
- PR24V-ready: conditionId-shaped market_source_id present OR read-only token->condition mapping available
- both-ready: PR24U-ready + PR24V-ready

## 6. Dedupe / Idempotency
- **unique_key_preferred**: UNIQUE(source, source_trade_id)
- **conflict_behavior**: INSERT OR IGNORE — collision is counted as dedup, never as an overwrite (avoids INSERT OR REPLACE provenance loss proven in run_scan/collect history).
- **repeat_scan_behavior**: Idempotent — re-running the same fetch inserts 0 new rows; existing rows are untouched.
- **fallback_key**: wallet_address + token_id/conditionId + side + price + size + timestamp
- **fallback_when**: Only when upstream source_trade_id is unavailable; must be made deterministic before use.
- **duplicate_prevention**: Single writer + UNIQUE index + INSERT OR IGNORE + bounded transaction scope.

## 7. WAL-Safe Write Policy (future writer)
- accept_normalized_rows_only: **True**
- validate_every_row: **True**
- skip_or_report_invalid_rows: **True**
- bounded_batches: **True**
- one_transaction_per_batch: **True**
- commit_once_per_batch: **True**
- never_concurrent_with_another_writer: **True**
- expose_dry_run: **True**
- require_explicit_write_flag: **True**
- report_db_size_mtime_before_after: **True**
- never_called_by_timers_until_proven: **True**

## 8. Future Manual Ingestion Sequence
- **PR24Y** — Read-only real trade source probe / live-preview (writes DB: False). No DB writes; validate fetch feasibility.
- **PR24Z** — Normalized source_trade candidate generation (writes DB: False). Produce candidate rows in-memory/report only.
- **PR25A** — Guarded single-writer source_trade batch insert (writes DB: True). Explicit --write flag only; dry-run default.
- **later** — Evidence attachment / scoring / decisions (writes DB: True). After ingestion is proven safe and single-owned.

**Centralized writer exists today?** **False**
> No safe centralized source_trade writer exists today. persist_trade is duplicated across two collectors (run_scan.py and collect_smart_money_data.py). PR24X recommends building ONE shared writer role per the architecture below rather than a new parallel path.

## 9. Current Verified DB Inventory
- source_trades: 5
- side_distribution: {'BUY': 1, 'buy': 4}
- trade_copyability_decisions: 0
- copy_candidates: 0
- paper_signal_decisions: 0
- candidate_price_snapshots: 0
- candidate_price_snapshot_levels: 0
- orders: 0
- positions: 0
- wallet_score_decisions: 1
- settlement_accounting_ledger: 0

## Hard Guardrails (all enforced this PR)
- no_deploy: **True**
- no_service_restart: **True**
- no_timer_enablement: **True**
- no_collect_scan_settle_update_restart: **True**
- no_production_db_writes: **True**
- no_source_trades_mutation: **True**
- no_backfill: **True**
- no_real_ingestion_implementation: **True**
- no_live_fetch: **True**
- no_persistence_writer: **True**
- no_scoring: **True**
- no_trade_copyability_decisions: **True**
- no_copy_candidates: **True**
- no_paper_signal_decisions: **True**
- no_candidate_price_snapshots: **True**
- no_candidate_price_snapshot_levels: **True**
- no_orders: **True**
- no_positions: **True**
- no_automation: **True**
- no_broker_order_placement: **True**
- no_candidate_creation: **True**
- no_signal_creation: **True**
