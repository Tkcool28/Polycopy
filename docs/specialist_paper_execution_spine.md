# Specialist Paper Execution Spine

Complete canonical system for manual specialist-approval → copyable paper-signal →
authorized → executed → marked → settled paper loop.

This document is the authoritative operator reference for the `feat/specialist-paper-execution-spine`
branch. It spans Pass 1 (scoring + execution engine), Pass 2 (approval → signal
operational wiring), and Pass 3 (operator commands, end-to-end proof, service
templates, this documentation).

---

## Architecture

```
manual specialist approval
  → bounded approved-wallet collection
  → authoritative enrichment
  → durable dispatch
  → candidate / snapshot / scoring
  → copy_candidate paper signal
  → manual execution authorization        (explicit human gate)
  → bounded execution consumer
  → risk decision
  → paper order
  → paper fill
  → paper position (+ lot)
  → marking
  → settlement
  → realized P&L
```

The spine is **paper-only**. It refuses to execute unless the runtime is paper,
the database is an explicitly-isolated temporary database (or production gates are
fully satisfied), and the production kill switch is not engaged. It never creates
an order without an active execution authorization, and `paper_signal_decisions.is_approved`
remains `0` by invariant — authority comes only from `paper_signal_execution_authorizations`.

---

## Authoritative tables

The specialist spine reads and writes **only** the following tables. Legacy
`orders`, `positions`, and `settlement_*` are **sample/demo-only** and are NOT
authoritative for this spine.

| Table | Role |
|---|---|
| `specialist_approvals` | Manual approvals (wallet + category + scoring evidence) |
| `source_trades` | Canonical ingested source trades |
| `source_trade_enrichments` | Idempotent taxonomy/event enrichment (v19) |
| `approved_specialist_trade_dispatches` | Durable dispatch record (v19) |
| `copy_candidates` | Candidate signals |
| `candidate_price_snapshots` | Price snapshots with depth levels |
| `candidate_price_snapshot_levels` | Per-level book depth |
| `wallet_score_decisions` | Wallet scoring decision |
| `category_wallet_score_decisions` | Category scoring decision |
| `trade_copyability_decisions` | Trade-copyability decision |
| `paper_signal_decisions` | Final paper signal (verdict `copy_candidate`) |
| `paper_signal_execution_authorizations` | **Explicit execution gate** |
| `execution_risk_decisions` | Fail-closed risk evaluation |
| `paper_orders` | Authoritative paper order (exactly-once per signal) |
| `paper_fills` | Authoritative paper fill |
| `paper_positions` | Authoritative paper position |
| `paper_position_lots` | Position lot (provenance to fill) |
| `paper_position_marks` | Position mark |
| `paper_position_settlements` | Position settlement (realized P&L) |

---

## Approval semantics

- **Manual only.** A human reviewer creates every approval via
  `scripts/manage_specialist_approvals.py approve` with `--reviewer`,
  `--evidence-fingerprint`, and `--reason`. No discovery/scorer path auto-creates approvals.
- **Reviewer required**, **evidence fingerprint required.**
- **Revocation preserves history.** `revoke` sets `revoked_at`; the row is retained.
  A revoked approval can no longer be used for collection, dispatch, or authorization.
- **Collector and monitor share the approval table.** Both resolve the wallet from
  `specialist_approvals` (enabled, non-revoked). No hardcoded wallet remains in code.

### "is_approved"

`paper_signal_decisions.is_approved` is read-only legacy compatibility state and is
**always `0`** by the PR4 force-zero invariant. Signal persistence NEVER grants
execution authority. The explicit execution gate is `paper_signal_execution_authorizations`.

---

## Safety

- **Paper only.** No live-money execution. The spine verifies `broker_mode == "paper"`
  and `is_live == false`.
- **Kill switch preserved.** The production order kill switch remains authoritative and
  is never silently disabled by any template or CLI.
- **Default limits fail closed.** Exposure limits (`max_order_size`, `max_per_market`,
  `max_per_wallet`, `max_global`) must be positive; otherwise the risk decision blocks.
- **Missing taxonomy blocks.** Enrichment without a usable taxonomy blocks dispatch.
- **Missing depth blocks.** A signal whose snapshot lacks depth levels blocks execution.
- **Stale evidence blocks.** Snapshots older than `snapshot_max_age_seconds` block execution.
- **Revoked approval blocks.** A disabled/revoked approval blocks collection, dispatch,
  and authorization.
- **One signal → at most one order.** A unique constraint on
  `(paper_signal_decision_id)` in `paper_orders` makes a second order impossible.

---

## Operator commands

All CLIs share: `--db-path` (default production path), `--json`, `--dry-run` (default for
writes), `--write`, `--confirm-production-db`. **Dry-run examples first.**

### Approval management
```bash
# Dry run (no write)
python scripts/manage_specialist_approvals.py approve \
  --wallet 0xWALLET --category sports_betting \
  --wallet-score-decision-id W --category-score-decision-id C \
  --formula-name wallet_score --formula-version 1 \
  --evidence-fingerprint FP --reviewer operator --reason "vetted"

# Write (requires --confirm-production-db on production)
python scripts/manage_specialist_approvals.py approve ... --write --confirm-production-db
python scripts/manage_specialist_approvals.py list --exact
python scripts/manage_specialist_approvals.py inspect --approval-id ID
python scripts/manage_specialist_approvals.py revoke --approval-id ID --reason "x"
```

### Collection (approval-driven)
```bash
python scripts/collect_approved_wallet_trades.py --approval-id ID --max-new-trades 1   # dry run
python scripts/collect_approved_wallet_trades.py --approval-id ID --max-new-trades 1 \
  --write --allow-live --confirm-production-db
```

### Enrichment
```bash
python scripts/enrich_approved_source_trade.py --source-trade-id STID            # dry run
python scripts/enrich_approved_source_trade.py --source-trade-id STID --write --confirm-production-db
```

### Dispatch
```bash
# Discovery + enrichment + dispatch for an approval (dry run by default; omit --write)
python scripts/process_approved_specialist_trades.py --approval-id ID
# Write (persists all stages; requires --confirm-production-db on production)
python scripts/process_approved_specialist_trades.py --approval-id ID --write --confirm-production-db
# Optionally bound newly-collected trades
python scripts/process_approved_specialist_trades.py --approval-id ID --max-new-trades 1 --write --confirm-production-db
```

### Authorization (explicit execution gate)
```bash
python scripts/manage_paper_signal_authorizations.py authorize \
  --paper-signal-decision-id 1 --specialist-approval-id ID \
  --reviewer operator --reason "vetted"            # dry run
python scripts/manage_paper_signal_authorizations.py authorize ... --write --confirm-production-db
python scripts/manage_paper_signal_authorizations.py list
python scripts/manage_paper_signal_authorizations.py inspect --authorization-id AID
# revoke only before use:
python scripts/manage_paper_signal_authorizations.py revoke --authorization-id AID --reason "x" --write --confirm-production-db
```

### Execution (paper only)
```bash
python scripts/execute_authorized_specialist_signals.py --authorization-id AID --dry-run
python scripts/execute_authorized_specialist_signals.py --authorization-id AID \
  --write --confirm-production-db --allow-paper-execution
```
Production execution additionally requires `specialist_paper_allow_production_execution=true`.

### Marking
```bash
python scripts/mark_specialist_paper_positions.py --position-id PID \
  --mark-price 0.55 --bid-price 0.50 --ask-price 0.60 --evidence-source authoritative   # dry run
python scripts/mark_specialist_paper_positions.py --position-id PID ... --write --confirm-production-db
```

### Settlement
```bash
python scripts/settle_specialist_paper_positions.py --position-id PID \
  --resolution-outcome Yes --evidence-source authoritative   # dry run
python scripts/settle_specialist_paper_positions.py --position-id PID ... --write --confirm-production-db
```

### Proof command
```bash
# Requires an explicit --db-path; refuses /root/Polycopy/data/polycopy.db.
python scripts/run_specialist_paper_execution_proof.py --db-path /tmp/specialist_proof.db --json
# Replay is idempotent (already_complete, no duplicate rows).
python scripts/run_specialist_paper_execution_proof.py --db-path /tmp/specialist_proof.db --json
```

### Monitoring
```bash
python scripts/monitor_approved_wallet_collector.py --json
```

---

## Service templates

Repository templates live in `deploy-units/*.service.template`. They are **review-only**:
do not install, enable, unmask, or start during this PR. Each is a bounded `oneshot` job
with explicit `TimeoutStartSec`, `POLYCOPY_MAX_RSS_MB` resource limit, `NoNewPrivileges=true`,
and paper-only environment. The execution template never disables the kill switch.

| Unit | Stage |
|---|---|
| `polycopy-approved-wallet-collect.service` | collection |
| `polycopy-approved-specialist-dispatch.service` | enrichment + dispatch |
| `polycopy-specialist-paper-execute.service` | authorized execution |
| `polycopy-specialist-paper-mark.service` | marking |
| `polycopy-specialist-paper-settle.service` | settlement |
| `polycopy-approved-wallet-monitor.service` | safety monitoring |

Timers may be supplied for controlled rollout but remain templates only and are not enabled.

---

## Rollout sequence

1. Merge the branch (no timers enabled by merge).
2. Controlled VPS sync of the new code + templates.
3. Schema migration **under backup** (the spine is additive v18 → v19).
4. One manual approval (`manage_specialist_approvals.py approve`).
5. One exact collection (`collect_approved_wallet_trades.py --approval-id ...`).
6. One exact enrichment/dispatch (`process_approved_specialist_trades.py`).
7. One authorization (`manage_paper_signal_authorizations.py authorize`).
8. One paper execution (`execute_authorized_specialist_signals.py`).
9. One mark (`mark_specialist_paper_positions.py`).
10. One settlement proof (`settle_specialist_paper_positions.py`).
11. Monitor verification (`monitor_approved_wallet_collector.py`).
12. Only then consider enabling timers for any stage.

**No timer should be enabled merely because the PR merges.**

---

## Rollback

- Disable any enabled timers.
- Preserve all audit rows (`specialist_approvals`, dispatches, authorizations,
  risk decisions, orders, fills, positions, settlements).
- **Do not delete** orders/fills/positions.
- Revoke approvals to block future collection/dispatch/authorization.
- Engage the kill switch to block any further execution.
- Restore the prior application commit if necessary (git revert / checkout).

---

## End-to-end proof

`scripts/run_specialist_paper_execution_proof.py` runs the complete lifecycle in one
persistent temporary SQLite database using production code paths (no invented
orders/fills/positions/settlements). It requires `--db-path` and **refuses**
`/root/Polycopy/data/polycopy.db`. Running it twice against the same database reports
`already_complete` with identical artifact IDs and **no duplicate operational rows**.
