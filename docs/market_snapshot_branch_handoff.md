# Market Snapshot Branch Handoff

## Branch purpose

Preserve authoritative wallet-trade and Gamma market evidence at ingestion time.

## Base

`2873081da508e3cb77007882e6255003da966a5f`

## Resolved failing test

`tests/test_pXX_s7_final_integration.py::test_s7_disposable_e2e_full_lifecycle`

## Root cause

The integration test sorted randomly generated source-trade UUIDs and assigned
the first two rows to `CT1` / `CT2`, then later assumed `CT2` was always the
`COND_B` trade. The production backfill correctly counted and persisted the
conflict on the authoritative `COND_B` row, while the nondeterministically
selected assertion row could correctly remain `complete`.

The regression now resolves the target trade by its persisted condition
identity, selects that exact source-trade ID, and proves the counter, protected
metadata, field-scoped merge result, current provenance, and replay behavior.

The audit also corrected contract gaps in the feature implementation:

- a conflict with no unrelated fill can no longer be mislabeled `unchanged`;
- immutable snapshot provenance fields conflict instead of being refreshed;
- outcome/token validation requires string token IDs and validates trade
  token/outcome agreement even without an outcome index;
- an invalid trade outcome index is not coerced;
- Gamma `id` cannot substitute for a missing `conditionId`.

## Final validation

- Focused affected suite: `114 passed`
- Complete Python suite: `3728 passed, 3 skipped, 4 warnings`
- Ruff no-new-debt comparison against base: passed, zero new diagnostics
- `git diff --check`: passed

The complete suite ran with deterministic fake Gamma evidence in CLI tests and
a disposable `curl` stub so validation made no production/public-host request.

## Status

Ready for a draft pull request and human review. Do not merge without reviewing
the exact final commit and its checks.

## Safety

No production database write, schema migration, deployment, service or timer
action, production backfill, or approval or execution change occurred.
