# PR67 canonical wallet specialist scoring pipeline

PR67 converges the previously parallel paper-signal paths onto **one canonical
evaluator** (`evaluate_paper_signals_for_candidate`) that drives the frozen
*Wallet Skill Score v1 → trusted category taxonomy → Category Wallet Score v1 →
Trade Copyability v1 → canonical paper decision* flow. It adds a persisted
BUY-evidence resolver, a two-phase approved-wallet bridge, a decision-only CLI,
and the safety proofs that keep production writes out of scope.

## 1. Purpose

Replace the legacy bridge scoring/paper shortcut with a single canonical
specialist evaluation used for **paper/pilot testing only**. No order, position,
approval, live execution, or automation is produced or modified by this PR. The
approved-wallet bridge may create or reuse canonical candidate, snapshot, and
depth-level inputs before decision-only evaluation.

## 2. Canonical scoring flow

```
wallet evidence (BUY-only, source_trades + PR66 metadata_json)
   → Wallet Skill Score v1            (wallet_score_decisions)
   → trusted category taxonomy         (CATEGORY_TAXONOMY_USABLE / PARTIAL / UNAVAILABLE)
   → Category Wallet Score v1          (category_wallet_score_decisions)
   → Trade Copyability v1              (trade_copyability_decisions)
   → canonical paper decision          (paper_signal_decisions)
```

The canonical evaluator is the single source of every paper verdict. The legacy
bridge no longer invents its own scoring/paper shortcut.

## 3. Wallet evidence requirements

`src/polycopy/scoring/wallet_evidence.py` reads **only** `source_trades` and the
PR66 `metadata_json`. It:

- counts **BUY** trades only (SELL is excluded from both BUY accounting *and* P&L
  — SELL evidence is never invented);
- derives win/loss/`realized_pnl`/`win_rate`/`profit_factor`/`active_trading_days`
  /`distinct_events`/`distinct_markets` truthfully from resolved BUY rows;
- computes a deterministic `evidence_fingerprint` (contract `pr67-wallet-evidence-v1`)
  so resolutions are replay-safe;
- never settles trades, guesses SELL P&L, creates candidates, or performs I/O
  beyond the caller's query surface.

Event identity is `event.id` then `event.slug`; **neither is a category label**.

## 4. Trusted taxonomy states

| State | Meaning | Category decision |
|---|---|---|
| `CATEGORY_TAXONOMY_USABLE` | `taxonomy.raw_category` present (normalized) | resolved + persisted |
| `CATEGORY_TAXONOMY_PARTIAL` | only `taxonomy.tags` present (unmapped) | `not_applicable`, no row |
| `CATEGORY_TAXONOMY_UNAVAILABLE` | no taxonomy / malformed | `not_applicable`, no row |

**Tags are never treated as usable.** The snapshot's legacy `category` /
`category_label` is not used for scoring; only the source-trade PR66 taxonomy is.

## 5. Category denominator

- `category_trade_count` = category-scoped **BUY** count
- `overall_trade_count` = **wallet-wide BUY** count (passed explicitly so only
  the category evidence is scoped, never the wallet denominator)

Verified by `test_pr67_canonical_evaluator_wiring.py`:
`test_category_evidence_is_scoped_but_wallet_denominator_is_not`.

## 6. Decision-only persistence policy

`EvaluationExecutionPolicy.decision_only()` persists the three scoring decisions
+ paper decision but **never** shadow/exit-experiment rows. Decision-only runs
record `evaluation_policy_name = "decision_only"` in provenance and keep
`is_approved = 0`.

## 7. Approved-wallet two-phase bridge

`process_approved_wallet_trades` (in `approved_wallet_trade_bridge.py`):

- **Phase A** explicitly begins a transaction, writes input tables
  (candidate, snapshot, depth levels) inside a nested savepoint, then explicitly
  **commits** before Phase B.
- **Phase B** invokes the canonical evaluator with **no active transaction**; a
  Phase B rollback therefore cannot erase Phase A inputs (proven on a fresh
  connection).
- `evaluate_canonical_decisions=False` **defers** evaluation entirely (inputs
  commit, no decision fabricated).

## 8. Deferred and failure behavior

- **Deferred** (`evaluate_canonical_decisions=False`): inputs persist; evaluator
  is not called; no wallet/category/TC/paper decision is fabricated; report
  status is `canonical_evaluation_deferred`.
- **Failure** (canonical evaluator raises): Phase A inputs remain; no fake paper
  row; no legacy bridge TC row; `canonical_evaluation_status =
  canonical_evaluation_failed`.

## 9. SELL evidence versus BUY accounting

SELL trades are excluded from candidate selection in the approved-wallet bridge:
a SELL source trade never creates a candidate, snapshot, or canonical decision,
and the evaluator is never called for it. Within BUY accounting, SELL P&L is
deliberately **not** folded into realized P&L (a SELL does not represent a BUY
outcome), so `realized_pnl` can remain `None` when only SELL evidence exists —
producing an honest `INCOMPLETE` rather than a fabricated score.

## 10. Production CLI gates

`scripts/evaluate_wallet_scoring_pipeline.py` is **dry-run by default** and:

- opens the DB read-only (`file:<path>?mode=ro`, never `immutable=1`);
- `--apply` requires `--confirm-production-db` on the production DB path;
- writes only to the allowlisted decision tables (see §12);
- rejects out-of-range `--limit` / `--offset` and ambiguous wallet prefixes
  (fail-fast);
- contains no `requests`/`httpx`/`aiohttp`/`urllib`, no `Database.connect`, no
  `create_order`/`submit_order`, no `systemctl`/`timer`.

## 11. Dry-run default

`run()` uses `dry_run_policy()` (all `persist_*` = `False`) unless `--apply` is
passed. The dry-run path performs **no writes** — verified by file hash + table
fingerprint before/after.

## 12. Write allowlist

The only tables the canonical evaluator + bridge may write are:

- `wallet_score_decisions`
- `category_wallet_score_decisions`
- `trade_copyability_decisions`
- `paper_signal_decisions`

plus the bridge Phase-A input tables (candidate / snapshot / depth levels).

## 13. Forbidden writes

A SQL trace asserts no DML targets: shadow decision tables, exit experiment
tables, `orders`, `positions`, `settlement_accounting_ledger`, approval state,
broker/execution tables, or unrelated source-truth tables.

## 14. No production apply or write

No production PR67 write or apply occurred. A bounded read-only production
dry-run was performed against `file:/root/Polycopy/data/polycopy.db?mode=ro`, and
all database counts remained unchanged. Collectors/monitors remain masked and
inactive; scans/settlement/updates are disabled. See the post-merge plan (§17).

## 15. Specialist formula vs Alpha shadow

The specialist (paper-testing) formula is **frozen** (Wallet Skill Score v1,
Category Wallet Score v1, Trade Copyability v1). The stronger **Alpha** formula
remains a separate **shadow-only** research system (`shadow_decisions`,
`exit_experiment_registrations`); it never influences v1 verdicts and is never
blended with the specialist path.

## 16. Historical legacy rows

Pre-existing `paper_signal_decisions` / `trade_copyability_decisions` rows from
earlier bridges remain immutable and readable; the new canonical path writes its
own idempotent rows keyed on `candidate_id + idempotency_key`.

## 17. Verdict serialization contract

The canonical machine value is **lowercase** (`SignalVerdict.value`, e.g.
`"incomplete"`), matching the DB `CHECK` constraint on `paper_signal_decisions`
and all persisted/typed representations.

| Surface | Representation | Notes |
|---|---|---|
| internal enum / `final_verdict.value` | lowercase | `SignalVerdict` / `WalletVerdict` / `Verdict` |
| persisted `paper_signal_decisions.verdict` | lowercase | DB `CHECK` constraint |
| `decision_input_json.final_verdict` | lowercase | typed contract |
| `summary["verdict"]` (computed path: missing depth, unknown/missing side, wallet-incomplete) | lowercase | canonical machine verdict |
| `summary["verdict"]` (legacy early-return guards: no candidate / no snapshot / no source trade / no wallet id) | `"INCOMPLETE"` | historical display label, preserved for back-compat |
| API / dashboard / human-facing | `"INCOMPLETE"` (uppercase) | `api/repository.py` scan-surface label |
| bridge report `paper_signal_verdict` | accepts both | bridge normalizes `{incomplete, skip, INCOMPLETE, SKIP}` |

The persistent/storage verdict is always lowercase; uppercase is a display label
retained only where a historical contract requires it. No global `.upper()`
scatter was introduced.

## 18. Post-merge plan

1. Clean VPS sync of this branch.
2. Controlled decision apply via the decision-only CLI against an online backup,
   with before/after proofs.
3. System validation (suite + read-only production diagnostics).
4. Collector restoration **only after explicit approval** — not performed by this
   PR.
