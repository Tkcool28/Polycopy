# Canonical `source_trades` write architecture

## Contract

Initial production ingestion has exactly one SQL boundary:

```text
collector-specific adapter → canonical normalization → canonical metadata builder
→ source_trade_writer.write_valid_rows → source_trades
```

`write_valid_rows` owns the sole initial `INSERT OR IGNORE` and deduplicates
only by `(source, source_trade_id)`. Collectors supply raw trade facts;
normalization derives the canonical identity, canonical wallet/side values and
validation status; the writer alone serializes initial metadata through the PR
#79 trusted metadata boundary.

The legacy scan and smart-money collectors are retained only as orchestration
paths. Their local persistence functions now adapt their parsed `SourceTrade`
to `NormalizedSourceTrade` and call the canonical writer. They no longer carry
collector-owned `source_trades` SQL.

## Bounded reconciliation allowlist

| Role | Boundary | Existing-row selector | Allowed fields | Missing row |
| --- | --- | --- | --- | --- |
| Metadata reconciliation | `source_trade_metadata_reconciliation.reconcile_metadata_json` | `(source, source_trade_id)` or immutable `id` | `metadata_json` only | `missing`, never INSERT |
| Approved-wallet duplicate repair | `collect_approved_wallet_trades._enrich_existing_row` | canonical `(source, source_trade_id)` | `metadata_json` only | `missing` |
| Specialist enrichment | `source_trade_enrichment._persist` | immutable `id` | `metadata_json` only | failure/rollback |
| Taxonomy backfill | `backfill_specialist_trade_taxonomy` | immutable `id` | `metadata_json` only | failure/rollback |
| Resolution reconciliation | `source_trade_resolution.apply_existing_resolution_updates` | immutable `id` | `resolution_status`, `resolved_at`, `winning_token_id`, `is_winning_trade`, `realized_pnl`, `settlement_source` | rowcount 0, never INSERT |

Metadata replacement of a non-empty row requires the private carrier emitted
only after `merge_canonical_metadata` has compared persisted evidence with the
authoritative Gamma response. Ordinary mappings and JSON strings are sent
through the PR #79 bounded serializer, so they cannot self-certify `_snapshot`
evidence. Exact existing bytes are a zero-write replay.

`backfill_resolution_truth` remains a maintenance planner for existing market
truth, but delegates its source-trade settlement application to the authorized
resolution boundary and no longer owns a second mutation statement.

## Locking and database targeting

Independently invoked retained evidence mutators own
`/tmp/polycopy-operational-jobs.lock` for their writable lifetime through the
shared lock helper. The cohort obtains it once and marks the nested writable
open as already lock-owned, preventing self-deadlock.

The default settings DB is the resolved repository `data/polycopy.db`, not a
current-working-directory relative path. Evidence commands retain explicit
disposable DB overrides for tests and non-production runs. Demo and smoke
writers reject both the repository and `/root/Polycopy` production DB paths.

## Architecture guard

`tests/test_source_trade_write_architecture.py` scans executable production
SQL call sites with a role-specific allowlist. It distinguishes the canonical
initial insert, metadata-only update boundary, resolution-only update boundary,
schema/migration exceptions, and non-production demo/smoke exceptions. Tests,
docs and comments are outside the production scan.

## Deferred finding

`/root/Polycopy/polycopy.db` is historical evidence of the old relative-path
fallback. It is not modified or removed by this PR.
