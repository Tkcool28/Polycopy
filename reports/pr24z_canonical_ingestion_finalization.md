# PR24ZC: Canonical Ingestion Validation — Finalization

**Finalization branch:** `feat/pr24z-finalize-canonical-ingestion`
**Based on:** `4bb48734dbf28a3734b5fb99e1bc78b80e3fd3c4` (main)

## Milestone summary

- **PR #50** (`Merge PR #50 marker validation into PR #51`) and **PR #51** (`PR24Z: canonical ingestion and validated migration marker gate`) are merged into main at the baseline SHA.
- The one-time canonical identity migration (`pr24z_canonical_identity.py`) completed successfully: **14 legacy source_trade_id values converted** to canonical `transaction_hash`-derived IDs, in a single transaction, with the validated completion marker written.
- The first bounded canonical live ingestion (`scripts/ingest_real_source_trades.py`, `--allow-live --write --confirm-production-db`) inserted **4 genuinely new BUY rows** from the live Polymarket data API; **8 overlapping migrated rows were recognized canonically and deduplicated** (INSERT OR IGNORE under UNIQUE(source, source_trade_id)).
- **Final `source_trades` count = 23** (19 after migration + 4 new).
- Post-write no-write replay: `rerun_would_insert = 0` → the live ingestion path is idempotent and the canonical marker gate is fail-closed.
- **No legacy compatibility path remains.** The permanent writer uses canonical `source_provided` identity only; `legacy_identity_aliases_used = 0` on both dry-run and production runs.
- **Downstream logic remains inactive.** `trade_copyability_decisions`, `copy_candidates`, `paper_signal_decisions`, `candidate_price_snapshots`, `candidate_price_snapshot_levels`, `orders`, `positions`, `wallet_score_decisions`, `settlement_accounting_ledger`, `market_outcomes`, `decision_log` are all unchanged by the ingestion write.
- **Recurring collection remains disabled.** `polycopy-collect/scan/settle/update` units and timers are inactive; no timer was enabled by this work.

## Evidence artifact supersession map

This milestone intentionally keeps each artifact scoped to the event it records. Future reviewers should use this map:

| Artifact | What it records | State captured |
|---|---|---|
| `reports/pr24z_historical_production_reference.json` | Approved old → canonical mapping inputs | 14 legacy IDs at the historical moment |
| `reports/pr24z_canonical_identity_migration_mapping.csv` | Old → canonical conversion (finalized) | `migration_state=ALL_CANONICAL` after the one-time migration |
| `reports/pr24z_canonical_identity_migration_result.{json,md}` | The one-time migration transaction | first run updated 14, second run updated 0, `source_trades` stayed 19, marker created, integrity/FK clean (restored to HEAD — do NOT overwrite with ingestion state) |
| `reports/pr24z_post_migration_live_dry_run.json` | Pre-write bounded live dry-run | raw 25 / BUY 12 / SELL 13 / 8 canonical dupes / 4 genuinely new / write attempted = false |
| `reports/pr24z_first_canonical_production_ingestion.{json,md}` | The later 19 → 23 production ingestion event | attempted 12 / inserted 4 / deduplicated 8 / committed true |
| `data/.pr24z_canonical_migration_complete` | Validated completion marker | `canonical_row_count=14`, `legacy_row_count=0`, `rows_updated=14` |

**Clarification (important):** The 4 unmatched rows in the dry-run compatibility block were **genuine new trades** returned by the live source, not failed recognition of migrated rows. They were the only rows inserted by the production write; the 8 "existing canonical duplicates" were the already-migrated rows returned again by the live source and correctly deduped.

## Next milestone

Controlled recurring collection readiness — wiring `polycopy-collect`/`scan` against the now-canonical ingestion path behind the existing marker gate, with downstream scoring/candidate generation still gated separately.

## Production DB untouched by this PR

This finalization PR contains **documentation/report artifacts only**. It performs no migration, no ingestion, and no production DB write. The `data/` directory (DB, marker, backups, WAL/SHM) is excluded from the commit.
