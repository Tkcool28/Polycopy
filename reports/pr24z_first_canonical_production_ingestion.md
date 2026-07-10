# PR24Z First Post-Migration Canonical Production Ingestion

- **Generated:** 2026-07-10T18:52:58Z
- **Ingestion version:** PR24Z-1
- **Source:** polymarket_data_api_trades_user
- **Wallet (redacted):** `0x…0579`
- **Main SHA:** 4bb48734dbf28a3734b5fb99e1bc78b80e3fd3c4
- **DB path:** /root/Polycopy/data/polycopy.db
- **Marker SHA:** 4db6f658108c7978b9ed53d2591e0ecd22e3c005ce970d875fcc3e59a9b60274

## Phase A — Bounded Live Dry-Run (no write)
- mode: safety-verification (live)
- network calls attempted/succeeded: 2/2
- raw_records: 25 (BUY 12, SELL 13)
- eligible_buy: 12, rejected_unsupported_side (SELL): 13
- identity: source_provided_used=25, fallback=0, ambiguous=0
- compatibility: examined 14, matched 8, unmatched 4, legacy_aliases_used=0, rerun_would_insert=4
- write: NONE
- DB unchanged (size 528384, mtime_ns 1783708114961708145, source_trades=19 before/after)

## Phase B — One Bounded Production Write
- mode: production-write
- flags: --allow-live --write --confirm-production-db
- marker validated before write
- backup: /root/Polycopy/data/polycopy.db.pr24z_online_backup_20260710T185258Z (sha256 96961311a4e26d5c01191af21c7c9d9ef9c5e2184ea02fe4f3f0e862a8c6f6bb, integrity=ok, fk=0, source_trades=19)
- UNIQUE(source, source_trade_id) constraint present: true
- write.attempted: 12
- write.inserted: 4
- write.deduplicated: 8
- write.rejected: 0
- write.errors: 0
- write.committed: true
- write.rolled_back: false
- existing_duplicates_recognized: 8

## Inserted rows (genuinely new canonical BUY)
| id_prefix | canonical ID prefix | market_source_id prefix | token_id prefix | qty | price | timestamp |
|---|---|---|---|---|---|---|
| 10fd4799 | polymarket:5942 | 0xe883f2fda25a | 10865098848798 | 210.0 | 0.991 | 2026-07-10T18:39:55Z |
| 5d22b729 | polymarket:9b29 | 0x3265b10daeb3 | 90935594925886 | 190.0 | 0.994 | 2026-07-10T16:39:38Z |
| 8f7f1e16 | polymarket:07ea | 0x7f695758278e | 44632793419668 | 190.0 | 0.994 | 2026-07-10T10:06:23Z |
| 1c4091d5 | polymarket:389e | 0x01dffa7abae7 | 10443186053548 | 190.0 | 0.992 | 2026-07-09T09:10:47Z |

All 4 exist exactly once, canonical source_provided identity, side=BUY, is_sample=0, trader=0xcac7…0579.

## Migrated-row duplicate check
- 14 migrated canonical IDs each present exactly once (0 duplicates)
- 0 legacy-format IDs remain

## Downstream tables (unchanged)
trade_copyability_decisions 0, copy_candidates 0, paper_signal_decisions 0,
candidate_price_snapshots 0, candidate_price_snapshot_levels 0, orders 0,
positions 0, wallet_score_decisions 1, settlement_accounting_ledger 0,
market_outcomes 6, decision_log 0.

## Post-write no-write replay (idempotency)
- examined 18, matched 12, unmatched 0, legacy_aliases_used 0, rerun_would_insert 0
- write: NONE, DB unchanged

## Final integrity
- integrity_check: ok
- foreign_key_check: 0
- source_trades: 23 (19 baseline + 4 inserted)
- app schema: 16
- marker: valid, unchanged (sha 4db6f658…)

## API
- restarted: polycopy-api.service active, port 8765 listening, /health HTTP 200
- source_trades stable at 23, integrity ok
- collect/scan/settle/update remain inactive

## Verdict
POST_MIGRATION_CANONICAL_INGESTION_VALIDATED — recurring ingestion still disabled, no timers enabled.
