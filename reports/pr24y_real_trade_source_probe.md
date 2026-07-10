# PR24Y — Real Wallet Trade Source Probe

**Probe version:** PR24Y-1  
**Generated at:** 2026-07-10T03:37:41.422043+00:00  
**Live preview enabled:** True  
**Network calls attempted/succeeded:** 1/1  
**Mode:** read-only source probe (no DB, no writes)

## Source Selection
- selected_source: **polymarket_data_api_trades_user**
- source_selection_verdict: **SOURCE_CONFIRMED**
- source_candidates_examined: 5

## Counters
- wallet_count: 1
- record_limit: 25
- pages_fetched: 1
- raw_records: 25
- raw_buy_records: 14
- raw_sell_records: 11
- unknown_side_records: 0
- eligible_buy_records: 14
- excluded_unsupported_side: 11
- excluded_missing_fields: 0

## Field Coverage
- token_id_available_count: 25
- condition_id_available_count: 25
- price_available_count: 25
- size_available_count: 25
- timestamp_available_count: 25
- pr24u_ready_count: 25
- pr24v_ready_count: 25
- both_ready_count: 25

## Stable Identity
- stable_source_trade_id_available: **True**
- identity_field: transactionHash (source trade/fill id)
- identity_uniqueness_confidence: high
- fallback_components_available: ['wallet_address', 'token_id/conditionId', 'side', 'price', 'size', 'timestamp']
- collision_risk_notes: transactionHash unique across observed records; suitable as natural dedup key (UNIQUE(source, source_trade_id)).

## Pagination
- pagination_supported: True
- incremental_cursor_supported: True
- response_shape_stable: True
- notes: Observed 25 records across 1 page(s); 14 eligible BUY. data-api GET /trades?user=<addr> is unauthenticated, wallet-filterable, paginated (offset+limit), and returns proxyWallet, side, asset (token_id), conditionId, size, price, timestamp, and transactionHash. CLOB /trades requires auth; Gamma is market-metadata only; run_scan/collect are writer-owning collectors (excluded as probe sources).

## Source Candidates Examined
- **polymarket_data_api_trades_user** (`PolymarketPublicAdapter.get_trades_by_address`)
  - endpoint: GET https://data-api.polymarket.com/trades?user=<addr>
  - auth_required: False | wallet_filter: True | pagination: True
  - returns side/price/size/token_id/condition_id/tx_hash: True/True/True/True/True/True
  - fetch_only_safe: True | used_by_production_scan: False
  - Unauthenticated wallet-attributed trades; best PR24Y fit.
- **polymarket_data_api_trades_market** (`PolymarketPublicAdapter.fetch_trades_for_market`)
  - endpoint: GET https://data-api.polymarket.com/trades?market=<conditionId>
  - auth_required: False | wallet_filter: False | pagination: True
  - returns side/price/size/token_id/condition_id/tx_hash: True/True/True/True/True/True
  - fetch_only_safe: True | used_by_production_scan: True
  - Used by collectors; market-scoped, not wallet-scoped.
- **polymarket_clob_trades** (`PolymarketClobAdapter.fetch_book (book only)`)
  - endpoint: GET https://clob.polymarket.com/trades
  - auth_required: True | wallet_filter: False | pagination: False
  - returns side/price/size/token_id/condition_id/tx_hash: False/True/True/True/False/False
  - fetch_only_safe: True | used_by_production_scan: False
  - Authenticated; order book only, not trade history.
- **polymarket_gamma_markets** (`PolymarketPublicAdapter (Gamma)`)
  - endpoint: GET https://gamma-api.polymarket.com/markets
  - auth_required: False | wallet_filter: False | pagination: True
  - returns side/price/size/token_id/condition_id/tx_hash: False/False/False/False/True/False
  - fetch_only_safe: True | used_by_production_scan: False
  - Market metadata; no trade history.
- **run_scan_collector_writer** (`scripts/run_scan.py / collect_smart_money_data.py`)
  - endpoint: collector-owned source_trades writers (duplicated)
  - auth_required: False | wallet_filter: True | pagination: True
  - returns side/price/size/token_id/condition_id/tx_hash: True/True/True/True/True/True
  - fetch_only_safe: False | used_by_production_scan: True
  - PR24X: duplicated collector-owned writers; NOT a probe source. Future ingestion must delegate to one centralized writer.

## Safety / Guardrails
- production_db_opened: **False**
- production_db_written: **False**
- main_db_size_before: **520192**
- main_db_mtime_before: **1783652676**
- main_db_size_after: **520192**
- main_db_mtime_after: **1783652676**
- db_mtime_change_mechanism: **None**
- adapter_gap_notes: **None**
- ready_for_pr24z: **True**
- ready_to_persist_source_trades: **False**
- ready_to_wire_to_automation: **False**

## Sample Previews (first 5 of 25)
- #0 side=BUY token_id=YES cond=YES elig=eligible_buy pr24u=True pr24v=True
- #2 side=SELL token_id=YES cond=YES elig=excluded_unsupported_side pr24u=True pr24v=True
- #4 side=BUY token_id=YES cond=YES elig=eligible_buy pr24u=True pr24v=True
- #6 side=BUY token_id=YES cond=YES elig=eligible_buy pr24u=True pr24v=True
- #8 side=SELL token_id=YES cond=YES elig=excluded_unsupported_side pr24u=True pr24v=True