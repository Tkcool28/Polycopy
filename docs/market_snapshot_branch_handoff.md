# Market Snapshot Branch Handoff

## Branch purpose

Preserve authoritative wallet-trade and Gamma market evidence at ingestion time.

## Base

`2873081da508e3cb77007882e6255003da966a5f`

## Known failing test

`tests/test_pXX_s7_final_integration.py::test_s7_disposable_e2e_full_lifecycle`

## Exact observed mismatch

- backfill reports one conflict;
- persisted provenance row remains `status="complete"`.

## Known successful validation

Previous full run:

- 3721 passed
- 0 failed
- 3 skipped
- 4 warnings
- 393.39 seconds

Final run after later changes:

- 3721 passed
- 1 failed
- 3 skipped
- 4 warnings
- 383.53 seconds

## Known focused validation

- Replay/provider-time tests: 5 passed
- Directly affected suite: 215 passed
- Ruff: passed
- `git diff --check`: passed

## Current status

Not ready to merge.
Published for external repository-side investigation and repair.

## Safety

No production database write, schema migration, deployment, service or timer
action, production backfill, or approval or execution change occurred.
