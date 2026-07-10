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

## Identity strategy
- source_provided_identity_used_count: 3
- transaction_identity_used_count: 7
- strong_identity_used_count: 10
- identity_fallback_used_count: 0
- identity_ambiguous_count: 1
- duplicate_records_in_fetch: 1
- duplicate_records_existing_db: 0
- collision_errors: 0

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
- path: /root/Polycopy/data/polycopy.db.pr24z_online_backup_20260710T061700Z
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
- backup_path: /root/Polycopy/data/polycopy.db.pr24z_online_backup_20260710T061700Z
