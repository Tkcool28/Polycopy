# PR24Z Manual Real Source-Trade Ingestion — safety-verification

- **Generated:** 2026-07-10T06:17:00.989313+00:00
- **Ingestion version:** PR24Z-1
- **Source:** polymarket_data_api_trades_user
- **Wallet (redacted):** `0x…1111`
- **Live:** False
- **Network calls:** attempted=0 succeeded=0

## Fetch / classification
- raw_records: 11
- raw_buy_records: 9
- raw_sell_records: 1
- unknown_side_records: 1
- eligible_buy_records: 3
- rejected_unsupported_side: 1
- rejected_missing_fields: 1
- rejected_invalid_price: 2
- rejected_invalid_quantity: 2
- rejected_invalid_timestamp: 2
- rejected_wallet_mismatch: 1
- rejected_invalid_fields: 1
- rows_rejected: 8

## Corrected identity strategy
- Source-provided id (`sourceProvidedTradeId`, namespaced `polymarket:<v2id>`) is the **preferred strong id**.
- Real transaction hash (`transactionHash`) is a separate **secondary strong id**.
- Deterministic-composite fallback is used **only when both are absent**.
- source_provided_identity_used_count: 3
- transaction_identity_used_count: 7
- strong_identity_used_count: 10
- identity_fallback_used_count: 0
- identity_ambiguous_count: 1
- duplicate_records_in_fetch: 1
- duplicate_records_existing_db: 0
- collision_errors: 0

## Compatibility & idempotency (read-only replay of the 14 existing production rows)
- The live correction-run wallet returned **zero records** via the data-api `/trades?user=` endpoint (1 network call succeeded, 0 raw records).
- Compatibility was therefore proven by a **read-only replay** of the actual 14 previously inserted PR24Z rows through the corrected identity and dedupe path.
- verification_method: read_only_replay_of_existing_pr24z_rows
- existing_pr24z_rows_examined: 14
- existing_pr24z_rows_matched: 14
- existing_pr24z_rows_unmatched: 0
- canonical_strong_ids_matched: 14
- legacy_identity_aliases_used: 0
- existing_duplicates_recognized: 14
- rerun_would_insert: 0
- production_write_requested: False
- production_write_performed: False
- source_trades_before: 19
- source_trades_after: 19
- reconciliation_error: None
- Dual-ID dedupe safeguard (separately proven): if a rerun carries a *different* canonical id, all 14 still match via the recomputed legacy fallback id (legacy_aliases=14, canonical=0) — zero inserts either way.

## Safety
- downstream_tables_changed: False
- timers_changed: False
- ready_for_scoring: False
- ready_for_automation: False

## UNIQUE constraint preflight
- present: True
- index_name: sqlite_autoindex_source_trades_2
- columns: ['source', 'source_trade_id']
- error: None

## Process gate
- checked: True
- competing_writers_found: False
- safe_to_write: True

## Backup (SQLite online backup)
- method: sqlite_online_backup
- path: /root/Polycopy/data/polycopy.db.pr24z_online_backup_20260710T061626Z
- sha256: fe9d66a355a1802e54dead910be628f21807198d5524cbd423aee5223f3fa66f
- size: 528384
- integrity_check: ok
- foreign_key_violations: 0
- source_trades_count: 19
- success: True
- error: None

## Historical FIRST production write (preserved; never overwritten)
- attempted: 14
- inserted: 14
- deduplicated: 0

## Historical production rows (the REAL 14-row live write, NOT fixtures)
- source_report_commit: 56fbd0ee67770af4df5c2dcd93d65eec4c2df583
- mode: production-write
- live: True
- historical wallet (redacted): `0x…0579`
- rows_inserted: 14
- report→DB rows matched (exact source_trade_id selection): 14
- fixture_rows_found: 0
- reconciliation artifact: reports/pr24z_historical_production_row_reconciliation.json

These 14 rows came from the first live production-write run against wallet `0x…0579` (mode=production-write, live=true, network 2/2). They are proven real by field-for-field reconciliation against the production DB (14/14 exact matches, 0 placeholder/fixture patterns).

## Fixture verification rows (NO production write — separate read-only safety run)
- mode: safety-verification
- live: False
- network_calls_attempted: 0
- fixture wallet (redacted): `0x…1111`
- valid_fixture_rows: 3
- write: null
- rows_written_to_production: 0

The 3 fixture `valid_rows` in the latest safety-verification report are deterministic test fixtures (0x1111…/0x2222…/0x3333…). They were NEVER written to production and must not be mistaken for the historical 14 production rows above.

## Database safety
- db_path: /root/Polycopy/data/polycopy.db
- size before/after: 528384 / 528384
- mtime before/after: 1783658343 / 1783658343
- integrity_check: ok
- foreign_key_check: 0
- backup_path: /root/Polycopy/data/polycopy.db.pr24z_online_backup_20260710T061626Z
