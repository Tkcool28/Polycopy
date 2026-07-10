# PR24Y — Real Wallet Trade Source Probe

**Probe version:** PR24Y-1  
**Generated at:** 2026-07-10T02:51:58.340506+00:00  
**Live preview enabled:** False  
**Network calls attempted/succeeded:** 1/1  
**Mode:** read-only source probe (no DB, no writes)

## Source Selection
- selected_source: **polymarket_data_api_trades_user**
- source_selection_verdict: **SOURCE_PARTIAL**
- source_candidates_examined: 5

## Counters
- wallet_count: 1
- record_limit: 25
- pages_fetched: 1
- raw_records: 0
- raw_buy_records: 0
- raw_sell_records: 0
- unknown_side_records: 0
- eligible_buy_records: 0
- excluded_unsupported_side: 0
- excluded_missing_fields: 0

## Field Coverage
- token_id_available_count: 0
- condition_id_available_count: 0
- price_available_count: 0
- size_available_count: 0
- timestamp_available_count: 0
- pr24u_ready_count: 0
- pr24v_ready_count: 0
- both_ready_count: 0

## Stable Identity
- stable_source_trade_id_available: **False**
- identity_field: None
- identity_uniqueness_confidence: none
- fallback_components_available: ['wallet_address', 'token_id/conditionId', 'side', 'price', 'size', 'timestamp']
- collision_risk_notes: 

## Pagination
- pagination_supported: True
- incremental_cursor_supported: True
- response_shape_stable: True
- notes: No records observed in this run. Structural audit confirms the source is suitable; perform a live preview (--allow-live-preview) to confirm field coverage on real data. data-api GET /trades?user=<addr> is unauthenticated, wallet-filterable, paginated (offset+limit), and returns proxyWallet, side, asset (token_id), conditionId, size, price, timestamp, and transactionHash. CLOB /trades requires auth; Gamma is market-metadata only; run_scan/collect are writer-owning collectors (excluded as probe sources).

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
- ready_for_pr24z: **False**
- ready_to_persist_source_trades: **False**
- ready_to_wire_to_automation: **False**

## Sample Previews (first 5 of 0)