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
      "compatible": true,
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
      "exact_match": true,
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

## Field authority

| Snapshot field | Authority |
|---|---|
| `market.condition_id` | Gamma `conditionId` |
| `market.provider_market_id` | Gamma `id` |
| `market.question` | Gamma `question` |
| `market.slug` | Gamma `slug` |
| `outcomes.labels` | Gamma `outcomes` |
| `outcomes.token_ids` | Gamma `clobTokenIds` |
| `lifecycle.active` | Gamma `active` when boolean |
| `lifecycle.closed` | Gamma `closed` when boolean |
| `lifecycle.accepting_orders` | Gamma `acceptingOrders` when boolean |
| `lifecycle.end_date` | Gamma `endDate` |
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
7. a valid optional integer outcome index within both arrays;
8. agreement between index, trade token, and trade outcome whenever supplied.

Any invalid mapping has an empty `ordered` list. It is never guessed.

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

## Ingestion paths

The approved-wallet path enters `normalize_source_trade(..., gamma_market=...)`,
which delegates to the canonical builder. The specialist evidence collector
enters `ingest_pipeline.run_ingestion`, which reaches the same normalizer and
builder. Tests execute both production boundaries and compare the canonical
snapshot output.

## Guarantees and limitations

The snapshot preserves bounded provider evidence. It does not guarantee
specialist qualification, approval, execution, settlement, realized P&L, or
complete historical recovery. Future backfill can preserve only evidence that
a future Gamma lookup actually returns; it cannot safely infer unavailable
history.

This feature preserves richer evidence for future successful Gamma lookups.
It does not reconstruct the 93 currently unavailable historical markets.
