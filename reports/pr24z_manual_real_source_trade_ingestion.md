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

## Database safety
- db_path: /root/Polycopy/data/polycopy.db
- size before/after: 528384 / 528384
- mtime before/after: 1783658343 / 1783658343
- integrity_check: ok
- foreign_key_check: 0
- backup_path: /root/Polycopy/data/polycopy.db.pr24z_online_backup_20260710T061626Z
