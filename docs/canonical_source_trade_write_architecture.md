# Canonical `source_trades` write architecture

## Initial-ingestion contract

Every retained collector follows one explicit boundary:

```text
collector-specific adapter
→ approved shared normalizer for that collector contract
→ honestly validated NormalizedSourceTrade
→ source_trade_writer.write_valid_rows
→ sole initial source_trades INSERT boundary
```

`source_trade_writer.write_valid_rows` owns the sole initial `INSERT OR IGNORE`
for `source_trades` and its `(source, source_trade_id)` duplicate identity. The
writer accepts validated `NormalizedSourceTrade` values only: it performs no
normalization and no collector may hard-code `validation_status="valid"`.

Approved-wallet collectors use the stricter approved-wallet normalizer. The
retained `run_scan` and smart-money collectors use
`normalize_legacy_source_trade`, whose compatibility policy explicitly permits
BUY and SELL, missing wallet addresses, and missing token IDs when the retained
legacy contract requires them. It still rejects invalid price, quantity, or
timestamp before the canonical writer is called. Sentinel wallets become
`None`; legitimate wallet addresses are canonicalized.

## Existing-row reconciliation

Initial ingestion is distinct from bounded existing-row reconciliation:

| Role | Boundary | Selector | Allowed mutation | Missing row |
| --- | --- | --- | --- | --- |
| Metadata reconciliation | `reconcile_metadata_json` | immutable `id` or `(source, source_trade_id)` | `metadata_json` only | `missing`, never INSERT |
| Resolution reconciliation | `apply_existing_resolution_updates` | immutable `id` | resolution fields only | no update, never INSERT |

Metadata replacement authority is a private immutable carrier issued only by
`merge_canonical_metadata` after comparison with authoritative evidence. It
captures deterministic serialized bytes at issuance; inspection returns
detached values and `copy.copy`/`copy.deepcopy` intentionally return the same
immutable authority object. Ordinary mappings, `dict(result)`, and JSON
round-trips have no replacement authority. Exact persisted-byte replay is a
zero-write reuse.

## SQL architecture evidence

`polycopy.engine.source_trade_sql_architecture` is the shared, read-only AST
scanner used by both the architecture tests and the PR24X ingestion-writer
audit. It records structured evidence for supported SQL sinks (`execute`,
`executemany`, `executescript`, `iterquery`, and `query`), including source
path, lexical scope, line, SQL operation, table, touched columns, selector
columns, and resolution status.

The scanner follows statement order, local reassignment, simple aliases,
`getattr` sink aliases, and simple helper returns. It is deliberately
fail-closed for locally evidenced, unresolved source-trade DML. Its contract
permits exactly one writer `INSERT OR IGNORE` column contract and narrowly
specified metadata and resolution update contracts; schema, migration, and
non-production demo/smoke paths are separately recognized. Disposable-tree
negative cases prove that unauthorized inserts, updates, deletes, wrong
columns/selectors, and unresolved relevant SQL make the audit fail.

The PR24X audit derives its status from this scanner evidence. It does not rely
on a hard-coded `centralized_writer_exists` result or collector narrative.

## Writable evidence DB locking

`open_writable(db_path, args, *, operational_lock_already_held=False)` makes
lock ownership explicit. An internally owned writable open acquires the shared
operational lock and `DbConn.close()` releases only that internally acquired
lock. An already-held outer lock is never reacquired or released by `DbConn`.

`discover_research_wallets.py` and
`collect_specialist_evidence_cohort.py` are external lock owners: they acquire
the lock around their outer write sequence and call `open_writable(...,
operational_lock_already_held=True)`. Independent writable CLIs retain default
internal ownership.

## Safety boundary

This architecture documents source-code and disposable-test behavior only. It
does not run production collectors, backfills, or databases, and it does not
alter historical database files.
