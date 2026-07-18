# PR #71 — Specialist Evidence Research Plane (Operator Contract)

**Status: DRAFT — awaiting independent final review. Not merged, not marked ready,
not deployed.**

This document describes the *final* state of the `feat/canonical-specialist-evidence-accumulation`
branch after stages S1–S7. It is the authoritative operator reference for the
research plane only. It does **not** describe approval, dispatch, or execution —
those planes are out of scope for this PR.

---

## 1. What this PR is (and is not)

**Is:**
- A research/observability plane that collects, normalizes, scores, and reports
  on specialist trader evidence **for human review**.
- Schema version **21** (adds the research watchlist, market-refresh state,
  source-trade enrichment/provenance, and the (empty) execution-plane tables
  that later stages will use — no rows are ever written to those tables by PR #71).

**Is NOT:**
- Approval creation (no `specialist_approvals` row is ever written).
- Dispatch, candidate creation, paper-signal, execution authorization, risk,
  order, fill, position, mark, or settlement.
- Live execution.
- Automatic promotion from readiness → approval → dispatch → execution.
- A deployment. No systemd units, timers, or services are installed by this PR.

---

## 2. Research-plane flow

```
manage_specialist_evidence_watchlist.py add --wallet-id <id>
        │  (creates an ACTIVE research watch; NO approval)
        ▼
collect_specialist_evidence.py --watch-id <wid> [--write --allow-live --confirm-production-db]
        │  (bounded BUY-only collection; SELL/sample/replay excluded;
        │   canonical Gamma metadata + provenance written atomically)
        ▼
backfill_specialist_trade_taxonomy.py --wallet-id <id> [--write --allow-live --confirm-production-db]
        │  (historical taxonomy fill; one-current-row provenance)
        ▼
enrich_approved_source_trade.py --source-trade-id <id> [--write --allow-live --confirm-production-db]
        │  (per-trade enrichment repair; one Gamma request max; rollback on failure)
        ▼
refresh_specialist_market_truth.py --market-source-id <cond> [--write --allow-live --confirm-production-db]
        │  (market-centric resolution refresh; consistent across linked trades;
        │   no winner fabricated for unresolved markets)
        ▼
evaluate_specialist_evidence_watchlist.py --wallet-id <id> [--write --allow-live --confirm-production-db]
        │  (frozen rescoring; persists wallet + usable category decisions in ONE
        │   caller-owned transaction; formulas/thresholds unchanged)
        ▼
specialist_evidence_status.py --db-path <db> [--json]
        │  (STRICTLY READ-ONLY readiness report; current-evidence based)
        ▼
   human reviews GREEN wallet → (out of scope) approval in a later PR
```

---

## 3. Schema version 21

- Applied automatically by `Database().connect()` via the normal migration path.
- `schema_version` must read exactly **21** from `_meta`; a mismatch is a
  fail-closed error (exit 1), never a silent downgrade.
- v20 → v21 migration preserves all prior rows and foreign-key validity; the
  v21 migration only *adds* tables (it never drops or alters existing data).

---

## 4. Canonical sources of truth

| Data | Canonical source | Notes |
|------|-----------------|-------|
| Trade taxonomy (category/slug/event) | Polymarket Gamma API, via the backfill/enrichment providers | `event.slug` is identity/provenance, never a category. Normalized category is computed, not blindly trusted. |
| Market resolution | Polymarket Gamma API, via `refresh_specialist_market_truth.py` | One market-centric lookup; all linked source trades updated consistently. Unresolved markets get **no** winner. |
| Scoring formulas / thresholds | Frozen in `wallet_evidence.py` / `copyability_scoring_v1.py` | PR #71 does **not** alter any formula, weight, threshold, verdict, reason code, fingerprint, or idempotency identity. |

---

## 5. CLI dry-run / write flags

| CLI | Dry-run (read-only) | Write (gated) |
|-----|--------------------|---------------|
| `collect_specialist_evidence.py` | `--dry-run` | `--write --allow-live --confirm-production-db` |
| `backfill_specialist_trade_taxonomy.py` | `--dry-run` (omit `--write`) | `--write --allow-live --confirm-production-db` |
| `enrich_approved_source_trade.py` | omit `--write` | `--write --allow-live --confirm-production-db` |
| `refresh_specialist_market_truth.py` | `--dry-run` (omit `--write`) | `--write --allow-live --confirm-production-db` |
| `evaluate_specialist_evidence_watchlist.py` | omit `--write` | `--write --allow-live --confirm-production-db` |
| `manage_specialist_evidence_watchlist.py` | `list`/`inspect` subcommands | `add`/`pause`/`resume`/`retire` require `--write --allow-live --confirm-production-db` |
| `specialist_evidence_status.py` | always read-only (no write path) | n/a |

**In dry-run / read-only mode:** `mode=ro`, no migration, no write SQL, no
network where the CLI's dry-run contract forbids it (live Gamma reads require
`--allow-live` and are refused in dry-run).

---

## 6. Production safety gates (per CLI)

Every write CLI enforces, **before opening or modifying the database**:

1. `is_production_db(db_path)` — refuses to write to a recognized production DB
   unless the full gate set is present.
2. `require_write_gates(args, db_path)` — requires `--write --allow-live
   --confirm-production-db`. Missing any → **exit 2**, refusal printed, **no
   DB open / schema read / selector lookup / provider construction / write**.
3. Symlink / path-alias tests confirm recognized production DB detection cannot
   be bypassed.

The status CLI has **no writable-open path** — it opens `mode=ro` only.

---

## 7. Readiness states (YELLOW / GREEN / RED)

Reported by `specialist_evidence_status.py`, strictly from **current** evidence:

- **YELLOW** — insufficient current evidence; not ready for review.
- **GREEN** — `copy_candidate` verdict **and** no RED reason; `ready_for_human_review = true`.
  GREEN means *ready for human approval review only* — **not** an approval.
- **RED** — any RED reason (cohort, integrity, schema, or a post-evaluation
  execution-artifact delta). `ready_for_human_review = false`.

`ready_for_human_review_count == count(state == "GREEN")` is recomputed **after**
all global RED conditions (including execution-artifact deltas) are applied.

### Execution-artifact delta (fail-closed)

- Baseline execution-plane counts are captured **before** any mutation.
- After cohort evaluation, a second count is taken. If a post-evaluation count
  fails (provider/query error), the run raises and **exits 1** — no normal
  report, **zero writes**.
- A non-empty delta (a forbidden table changed vs baseline) marks every
  evaluated wallet RED with an exact `execution_artifact_delta:<table>:delta=<n>`
  reason. Stable preexisting rows remain visible but unchanged; the status
  report is read-only and never mutates them.

---

## 8. Pause / retire a watch

```
manage_specialist_evidence_watchlist.py pause  --watch-id <wid> [--write --allow-live --confirm-production-db]
manage_specialist_evidence_watchlist.py retire --watch-id <wid> [--write --allow-live --confirm-production-db]
```

A paused/retired watch alone never enters the active cohort. Adding a watch for
a wallet that already has an active watch is idempotent (returns the existing
active watch id; no duplicate).

---

## 9. Read-only health / status checks

```
specialist_evidence_status.py --db-path <db> [--json] [--wallet-id <id>]
```

Zero writes; safe to run alongside writers. Instrumented tests prove the
connection executes no INSERT/UPDATE/DELETE/REPLACE, no migration, no network,
and no filesystem write.

---

## 10. Systemd units (review-only, NOT installed)

PR #71 ships **no** unit/timer files in this branch. The research-plane CLIs are
designed to be wrapped by systemd units in a later, separately-reviewed
deployment PR. Any such unit must:

- use the intended absolute production checkout paths;
- call only the intended PR #71 CLI with safe arguments;
- never invoke approval/dispatch/execution;
- carry no live broker credentials;
- not disable the paper kill switch;
- not start automatically merely because the file exists (explicit `systemctl
  enable` required).

`systemd-analyze verify` is run against any proposed unit in that later PR.

---

## 11. Rollout after merge (separate, controlled operation)

1. Merge PR #71 (research plane only).
2. Run `Database().connect()` migration on a staging copy; verify `schema_version == 21`.
3. Backfill/curate the research watchlist (`manage ... add`).
4. Schedule collectors/refresh/rescore/status via the (separate) deployment PR's
   units, with the production write gates enforced.
5. Review GREEN wallets manually before any approval-stage PR.

---

## 12. Rollback procedure

- PR #71 is additive (schema v21 adds tables; no destructive change).
- To roll back the branch: `git revert` the merge on `main`; the v21 tables
  remain harmless (empty execution-plane tables) and the migration runner will
  not auto-downgrade. A future PR may provide an explicit down-migration if
  needed.
- No production DB writes occur during normal research-plane operation except
  the bounded, gated research/canonical-evidence tables.

---

## 13. S7 deterministic integration proof (honest coverage)

`tests/test_pXX_s7_final_integration.py::test_s7_disposable_e2e_full_lifecycle`
proves the entire research pipeline **end to end** against a disposable temp DB
(no real network, no production path touched). It uses injected fake providers
and asserts exact behavior at every stage:

| Stage | What the test proves (deterministically) |
|-------|-------------------------------------------|
| Initial state | Fresh wallet/watch → `YELLOW` (no evidence). |
| Collection | 2 BUY-only trades inserted via fake provider; taxonomy + provenance written atomically. |
| Backfill | Fake Gamma adapter (patched `_make_adapter`, invoked exactly once, serves both CIDs) fills canonical metadata via the REAL selection/normalization/merge/provenance/transaction path; conflict path preserves prior taxonomy (no overwrite). |
| Enrichment | Fake resolver called exactly once; upserts the single current provenance row (`status=complete`, usable category); replay issues one more request and writes **zero** new rows. |
| Refresh | Unresolved market → `last_status=unresolved`, `resolved_at` NULL (no fabricated winner); resolved market → `resolved_at` set (non-null). |
| Rescore | Dry-run writes 0 decisions; GREEN write + replay is idempotent (1 decision row); a forced commit failure rolls back all staged decisions (exit 1, prior decision survives). |
| Status | Deterministic `GREEN`→`copy_candidate` transition; an injected `conflict` enrichment row flips the wallet to `RED`, `ready_for_human_review=false`, `ready_count=0`. |
| Execution-plane isolation | All 13 execution-plane tables have **zero** deltas across the whole lifecycle. |

The fake providers are injected via the production-accepted seams
(`backfill._make_adapter`, `enrich_source_trade(gamma_resolver=...)`,
`refresh.main(provider=...)`) — **no production code path is bypassed**. The
refusal test (`test_s7_production_refusal_matrix`) builds its own isolated temp
fixture and patches the gate seams to prove every write CLI exits 2 **before**
opening any DB; it never touches the repo's real `data/polycopy.db`.

Base-vs-head: the focused PR #71 suite and all remaining tests are green with
**zero PR-caused failures**. The full repository suite shows one identical,
proven base/environment-dependent failure (`test_p24o`) that is not in the PR
diff and fails identically on `main`.

---

## 14. Explicit non-actions (this PR does NOT)

- Does not introduce schema v22.
- Does not change frozen scoring formulas, weights, thresholds, verdicts,
  reason codes, fingerprints, or idempotency identities.
- Does not redesign accepted S1–S6 behavior.
- Does not create specialist approvals.
- Does not invoke dispatch, bridge, candidate, paper-signal, authorization,
  risk, order, fill, position, mark, or settlement paths.
- Does not add automatic promotion from readiness to approval.
- Does not add live execution.
- Does not install/enable/start/stop/reload/modify systemd on the VPS.
- Does not touch `/root/Polycopy`, the production DB, or production `.env`.
- Does not merge or mark PR #71 ready.
- Does not force-push.
- Does not open or modify the production DB during development/testing.
