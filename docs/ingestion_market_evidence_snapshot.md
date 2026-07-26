# Ingestion Market Evidence Snapshot

## Contract version

`MARKET_EVIDENCE_SNAPSHOT_CONTRACT_VERSION = "2"` in
`src/polycopy/ingestion/canonical_metadata.py` is the single authoritative
snapshot contract-version constant.

## Exact persisted shape

```json
{
  "metadata_version": "1",
  "taxonomy": {"raw_category": "string|null", "tags": ["string"]},
  "event": {"id": "string|null", "slug": "string|null", "title": "string|null"},
  "series": {
    "id": "string|null",
    "slug": "string|null",
    "title": "string|null",
    "ticker": "string|null"
  },
  "_snapshot": {
    "market": {
      "condition_id": "string|null",
      "provider_market_id": "string|null",
      "question": "string|null",
      "slug": "string|null"
    },
    "outcomes": {
      "labels": ["string"],
      "token_ids": ["string"],
      "ordered": [{"label": "string", "clob_token_id": "string"}],
      "compatible": "bool|null",
      "valid_index": "true|false|null",
      "index_token_agrees": "true|false|null",
      "index_outcome_agrees": "true|false|null",
      "status": "complete|incomplete|invalid",
      "errors": ["string"]
    },
    "lifecycle": {
      "active": "true|false|null",
      "closed": "true|false|null",
      "accepting_orders": "true|false|null",
      "end_date": "string|null"
    },
    "resolution": {
      "resolution_status": "resolved|incomplete|invalid|null",
      "winner_token_id": "string|null",
      "winner_outcome": "string|null",
      "evidence_fields": ["string"],
      "errors": ["string"]
    },
    "provenance": {
      "provider": "gamma",
      "lookup_kind": "condition_id",
      "requested_condition_id": "string|null",
      "exact_match": "true|false",
      "snapshot_contract_version": "2",
      "provider_updated_at": "string (optional)",
      "retrieved_at": "UTC ISO-8601 string (persisted merges only)",
      "trade_response_title": "string (optional)",
      "trade_response_slug": "string (optional)",
      "trade_response_outcome_index": "integer (optional)",
      "trade_response_transaction_hash": "string (optional)"
    }
  }
}
```

When the canonical builder refuses to emit `_snapshot` (initial-ingestion
fail-closed case, see "Condition-id exact-match validation" below) the
persisted metadata row is the v1 PR66 shape (`metadata_version`, `event`,
`taxonomy`, `series`) with no `_snapshot` key.  When `_snapshot` IS emitted
but `exact_match=false`, the authoritative namespaces (`market`, `outcomes`,
`lifecycle`, `resolution`) are explicitly nulled/cleared so a
`exact_match=false` row carries no Gamma-authoritative evidence; only the
provenance key is preserved.

## Field authority

| Snapshot field | Authority |
|---|---|
| `market.condition_id` | Gamma `conditionId` (only after condition-id match) |
| `market.provider_market_id` | Gamma `id` (only after condition-id match) |
| `market.question` | Gamma `question` (only after condition-id match) |
| `market.slug` | Gamma `slug` (only after condition-id match) |
| `outcomes.labels` | Gamma `outcomes` (only after condition-id match) |
| `outcomes.token_ids` | Gamma `clobTokenIds` (only after condition-id match) |
| `lifecycle.active` | Gamma `active` when boolean (only after match) |
| `lifecycle.closed` | Gamma `closed` when boolean (only after match) |
| `lifecycle.accepting_orders` | Gamma `acceptingOrders` when boolean (only after match) |
| `lifecycle.end_date` | Gamma `endDate` (only after match) |
| resolution status | Gamma `resolved` only |
| winner token | Gamma `winnerTokenId` or `winningClobTokenId` |
| winner outcome | Gamma `winnerOutcome` or `resolutionOutcome` |
| `provider_updated_at` | Gamma `updatedAt` only; audit-only and ignored for substantive replay comparison |
| `retrieved_at` | Local UTC observation time, accepted only on first fill or substantive update; audit-only |

Wallet-trade `title`, `slug`, `outcomeIndex`, and `transactionHash` are stored
only as provenance context. The trade token and outcome validate the Gamma
outcome mapping. They are not market identity, taxonomy, lifecycle, or winner
authority.

## Immutable and refreshable fields

The contract version and provider identity are immutable. Gamma observations
are refreshable only from later valid Gamma evidence. Incoming null or empty
values never erase populated evidence. Arbitrary unrelated metadata keys are
preserved.

## Replay and idempotency

A substantively identical Gamma lookup returns `unchanged`. The existing
`retrieved_at` is preserved, and the returned metadata is byte-identical under
the canonical JSON serializer. The persistence caller therefore performs no
metadata rewrite. Only a first fill or substantive accepted evidence update
receives a new local `retrieved_at`.

`provider_updated_at` is separate: it is preserved only when Gamma supplies
`updatedAt`; local wall-clock time is never used to synthesize it. It is
audit metadata rather than substantive market evidence, so a provider update
time change alone returns `unchanged`, preserves the accepted stored value,
and does not churn `retrieved_at`.

## Field-scoped merge behavior

Taxonomy, event, series, and each `_snapshot` sub-namespace are evaluated
independently. A conflict preserves the pre-existing value in that field or
namespace but does not discard valid independent fields accepted elsewhere.
Null incoming values do not conflict and do not erase evidence.

## Outcome validation

A complete ordered mapping requires:

1. a nonempty labels array;
2. a nonempty token array;
3. equal lengths;
4. nonblank string labels;
5. nonblank usable string token IDs;
6. unique token IDs;
7. a valid optional strict integer outcome index within both arrays;
8. agreement between index, trade token, and trade outcome whenever supplied.

Any invalid mapping has an empty `ordered` list. It is never guessed.

## Strict outcome-index contract

`outcomeIndex` is parsed through the SINGLE strict parser
`_strict_trade_index_value` in `canonical_metadata.py`. The same parser feeds
both consumers; the two paths cannot disagree:

| Input | Accepted | Persisted as `trade_response_outcome_index` |
|---|---|---|
| `int` (e.g. `0`, `1`) ≥ 0 | yes | the integer |
| `bool` (`True`/`False`) | no | omitted |
| `float` (including `0.0`, `1.0`) | no | omitted |
| fractional float (e.g. `0.5`, `1.5`) | no | omitted |
| `str` (`"0"`, `"1"`, `"0.5"`, `"0.5.0"`, scientific, `""`) | no | omitted |
| decimal / scientific-notation string | no | omitted |
| negative integer | no | omitted |
| `None` / absent | no | omitted |
| any other shape (list, dict, tuple) | no | omitted |

Invalid values are never persisted as provenance indices. They also never
truncate the validator's view (`0.5` is not coerced to `0`); the strict
parser returns `None` and both paths see `None`, so the validator's
`outcome_index_out_of_range` error does not coexist with a truncated
`trade_response_outcome_index` in the persisted row.

## Condition-id exact-match validation

Initial ingestion calls the canonical builder with
`enforce_exact_condition_match=True`. The builder refuses to emit
`_snapshot` whenever the trade's requested condition id (the trade's
`conditionId` / `market_source_id` after `strip().lower()`) does not match
the supplied Gamma market's `conditionId` after the same normalization.
The two normalization rules are the same rule used by the merge layer
(`_normalize_condition_id`, `_exact_condition_match` in
`canonical_metadata.py`).

Fail-closed behaviour:

* Matching condition ids: snapshot accepted, `provenance.exact_match = true`.
* Mismatched condition ids: NO snapshot emitted (and the v1 namespaces are
  NOT enriched from the Gamma object at all).
* Gamma `conditionId` missing: NO snapshot emitted.
* Requested trade condition id missing: snapshot is emitted (legacy
  contract) but `provenance.exact_match = false`; every authoritative
  namespace (`market`, `outcomes`, `lifecycle`, `resolution`) is explicitly
  nulled/cleared and the namespaces report the
  `exact_match_false_evidence_unavailable` error.

Identity is never inferred from any other Gamma field (slug, question, title,
token). The same condition-id normalization handles casing variations
(uppercase / mixed-case collapse to lowercase) and surrounding whitespace
(strip before compare). Two strings that differ only by case or whitespace
and a Gamma `conditionId` that differs in those dimensions are the same
identity.

## Resolution rules

Lifecycle is observational state, not winner evidence. `closed=true`,
`active=false`, disabled order acceptance, or an elapsed end date never imply
resolution. Explicit Gamma `resolved=true` is required. A single winner must be
derivable and consistent with the validated Gamma outcome mapping. Missing
winner evidence is incomplete; multiple or contradictory winners are invalid.
Ingestion writes no settlement status and no realized P&L.

## Taxonomy rules

Only the official Gamma taxonomy resolver populates taxonomy. Titles,
questions, and slugs identify content; they are not taxonomy evidence and are
never used to infer a category.

## Persistence boundary

The canonical serializer is
`polycopy.ingestion.source_trade_metadata.serialize_source_trade_metadata`.
It is the SINGLE deterministic JSON boundary between in-memory canonical
metadata and the `source_trades.metadata_json` column:

* When the input dict is a canonical PR66 payload (carries `_snapshot`), the
  serializer emits the exact bytes of that payload with `sort_keys=True` and
  `separators=(",", ":")`. `_snapshot` and every other canonical key survive
  persistence.
* When the input is an upstream-like raw dict (no `_snapshot`), the
  serializer routes through `normalize_source_trade_metadata` to preserve
  the legacy PR66 v1 contract.

The `source_trade_writer._row_tuple` helper is the only caller of the
serializer; the writer itself makes NO normalization, NO validation, NO
network access. The serializer is therefore the entire persistence-boundary
contract — no duplicates, no fallback paths.

## Ingestion paths

Both the approved-wallet ingestion path AND the specialist evidence
ingestion path converge on the same canonical writer and serializer
boundary:

* **Approved-wallet path**: `approve_wallet_collector.collect` →
  `ingest_pipeline.run_ingestion` → `normalize_source_trade(gamma_market=...)`
  with `enforce_exact_condition_match=True` → canonical builder →
  `source_trade_writer.write_valid_rows(dry_run=False)` →
  `serialize_source_trade_metadata` → `source_trades.metadata_json`.
* **Specialist / cohort path**: `collect_evidence` →
  `ingest_pipeline.run_ingestion` → `normalize_source_trade(gamma_market=...)`
  with `enforce_exact_condition_match=True` → canonical builder →
  `write_valid_rows(dry_run=False)` → `serialize_source_trade_metadata` →
  `source_trades.metadata_json`. The cohort then runs
  `enrich_source_trade_async` for idempotent enrichment, which routes
  through the merge layer (which already enforces condition-id identity
  upstream of the canonical builder).

The PR #79 persisted-row test suite (`tests/test_pr79_merge_blocker_persisted_row.py`)
exercises both paths end-to-end:

* `test_approved_wallet_writer_persists_full_snapshot_through_real_db` —
  approved-wallet boundary, `dry_run=False`, isolated temp DB, real
  readback of `metadata_json` to verify `_snapshot.market`,
  `_snapshot.outcomes`, `_snapshot.lifecycle`, `_snapshot.resolution`,
  `_snapshot.provenance`, plus `trade_response_*` provenance context.
* `test_specialist_cohort_writer_persists_full_snapshot_through_real_db` —
  specialist cohort boundary, `dry_run=False`, isolated temp DB, real
  readback. Equivalent snapshot semantics for equivalent inputs.

Both tests are TRUE write-mode database round-trip tests; neither uses
`dry_run=True`, neither compares only in-memory candidates. They prove
the canonical serialization boundary is real for both ingestion
boundaries and that the condition-id exact-match validation gates both
paths through the same canonical builder.

## Guarantees and limitations

The snapshot preserves bounded provider evidence. It does not guarantee
specialist qualification, approval, execution, settlement, realized P&L, or
complete historical recovery. Future backfill can preserve only evidence that
a future Gamma lookup actually returns; it cannot safely infer unavailable
history.

This feature preserves richer evidence for future successful Gamma lookups.
It does not reconstruct the 93 currently unavailable historical markets.
