# PR25B — Approved-Wallet Bridge Paper Reason Contract

## Problem

The PR25A approved-wallet bridge persisted paper-signal decisions with:

```
signal_reason = "bridge_required_paper_evidence_incomplete"
final_verdict = "incomplete"
```

That reason string was misleading. It implied the trade's market evidence
(CLOB book, bid/ask, spread, depth, market timing, outcome/token mapping,
Trade Copyability provenance) was missing or insufficient. It was not.

## Reality

The approved-wallet bridge is a **bounded evidence-capture** step. It:

1. hydrates the market + exact token→outcome mapping,
2. snapshots the live CLOB book (bid/ask, spread, depth),
3. walks persisted depth to estimate fill,
4. scores **Trade Copyability v1**, and
5. records that v1 decision with full provenance
   (`paper_signal_decisions.trade_score_decision_id`).

It deliberately does **not** invoke the full paper-signal evaluator
(`generate_signal_verdict`), which requires wallet score, category score,
shadow, approval, and order/position synthesis. Those are produced by the
separate `run_scan` Step-7 path, not by the approved-wallet bridge.

So the `incomplete` verdict reflects **evaluation scope**, not missing
evidence.

## Fix

Renamed the hardcoded bridge reason to:

```
signal_reason = "full_paper_evaluation_not_run"
```

Implemented as a single named constant
`BRIDGE_PAPER_REASON_SCOPE_NOT_FULL_EVALUATION` in
`src/polycopy/scoring/paper_signal.py`, referenced by both
`compute_bridge_trade_copyability_and_paper_input` and
`persist_bridge_trade_copyability_v1`.

`final_verdict` stays `"incomplete"`; `is_approved` stays `0`; TC score,
TC verdict, TC rejection reasons, provenance, idempotency, anti-replay
selection, and the full evaluator are all unchanged.

No schema migration, no scoring/formula/threshold change, no production
data mutation.

## Historical rows

Existing production `paper_signal_decisions` rows that retain the legacy
`bridge_required_paper_evidence_incomplete` reason are **valid audit
history** and are intentionally **not backfilled**. Downstream consumers
should treat both literals as the same bridge-incomplete family (the full
evaluator was not run).

## Schema version note

The live production DB reports `PRAGMA schema_version = 130`. That is the
**SQLite internal schema cookie**, which increments on *any* DDL change and
differs between databases (a freshly migrated DB shows ~128). It is **not**
the application migration version. The real application version is
`_meta.schema_version = 16` (== `polycopy.db.schema.SCHEMA_VERSION`), which
matches a freshly migrated DB. Do not equate `PRAGMA schema_version` with the
application schema version.
