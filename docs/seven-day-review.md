# Seven-Day Review

This document provides a structured review checklist for the first seven
days of operating the Polycopy paper trading platform. The goal is to
verify that the system works as documented, data flows correctly, and no
safety violations occur.

## Pre-Review: Fresh Start

Before day 1, ensure a clean state:

```bash
# Verify you're on the right branch
git -C /root/Polycopy branch --show-current
# Expected: feat/polycopy-paper-trading-platform

# Verify tests pass
cd /root/Polycopy && python -m pytest tests/ -q
# Expected: 283 passed

# Seed demo data
python scripts/seed_demo_data.py
# Expected: exit 0

# Start API
uvicorn polycopy.api.app:app --host 127.0.0.1 --port 8000 &

# Start dashboard
cd frontend && npm run dev &
# Expected: http://127.0.0.1:5173 loads with PAPER MODE + DEMO DATA banners
```

## Day 1: Smoke Test

### System State

- [ ] Dashboard loads at http://127.0.0.1:5173
- [ ] PAPER MODE banner is visible (amber)
- [ ] DEMO DATA banner is visible (blue)
- [ ] Kill switch banner is NOT visible (kill switch inactive)
- [ ] Health endpoint returns 200: `curl http://127.0.0.1:8000/health`
- [ ] System status shows `broker_mode=paper`: `curl http://127.0.0.1:8000/system/status`

### Demo Data Verification

- [ ] Overview page shows KPI cards with values (not zeros)
- [ ] Wallets page shows demo wallets (≥1 COPY_CANDIDATE, ≥1 WATCHLIST)
- [ ] Signals page shows demo signals
- [ ] Portfolio page shows demo positions
- [ ] Paper Orders page lists demo orders
- [ ] Risk Console page shows gate states
- [ ] Experiments page shows demo experiment runs
- [ ] Settings page shows config (no secrets visible)

### Safety Checks

- [ ] `GET /config` does NOT contain `polymarket_private_key`
- [ ] No `.env` file contains a private key value
- [ ] `DisabledLiveBroker` raises on every operation (verified by tests)

## Day 2: Data Collection Pipeline

### Manual Collection

```bash
python scripts/collect_smart_money_data.py -v
```

- [ ] Script exits with code 0 or 2 (partial success OK)
- [ ] Console output shows markets being fetched
- [ ] `data/snapshots/` contains raw snapshot files (if live API reachable)
- [ ] Running again immediately exits with code 3 (lock held)

### Scan

```bash
python scripts/run_scan.py -v
```

- [ ] Script exits with code 0 or 2
- [ ] Dashboard Overview page shows updated scan count
- [ ] Wallets page shows discovered wallets
- [ ] Signals page shows generated signals

### With `--use-sample`

```bash
python scripts/collect_smart_money_data.py --use-sample -v
```

- [ ] All data is labeled `is_sample=True`
- [ ] DEMO DATA banner appears on dashboard

## Day 3: Paper Order Workflow

### Preview → Approve

1. Go to Paper Orders page in dashboard
2. Fill in preview form (use demo market ID, outcome, side, quantity, price)
3. Click Preview

- [ ] Preview returns a fill quote with slippage and fee breakdown
- [ ] Kill switch is NOT blocking
- [ ] Exposure limits are NOT blocking (unless configured)

4. Click Approve (with idempotency key)

- [ ] Order status changes to FILLED
- [ ] Portfolio page shows the new position
- [ ] Decision log has a new entry for this order

### Preview → Reject

1. Preview another order
2. Click Reject

- [ ] Order status changes to CANCELLED
- [ ] Decision log has a rejection entry

### Duplicate Rejection

1. Use the same idempotency key twice

- [ ] Second request is rejected as duplicate

## Day 4: Risk Gates

### Kill Switch

1. Set `POLYCOPY_ORDER_KILL_SWITCH=true` in environment
2. Restart the API server
3. Try to preview a paper order

- [ ] Preview is blocked with "Kill switch engaged" message
- [ ] KILL SWITCH ACTIVE banner appears on dashboard (red)
- [ ] Risk Console shows kill switch as engaged

4. Set `POLYCOPY_ORDER_KILL_SWITCH=false`
5. Restart

- [ ] Orders can be previewed again

### Research-Only Mode

1. Set `POLYCOPY_PAPER_MODE=research_only`
2. Restart
3. Try to preview a paper order

- [ ] Preview is blocked with "Mode is research_only" message

4. Set `POLYCOPY_PAPER_MODE=paper_manual`
5. Restart

### Exposure Limits

1. Set `POLYCOPY_MAX_EXPOSURE_PER_MARKET=1` (very low)
2. Restart
3. Try to preview an order with notional > 1

- [ ] Order is blocked by exposure limit

4. Reset to `POLYCOPY_MAX_EXPOSURE_PER_MARKET=0` (unlimited)
5. Restart

## Day 5: Portfolio and Settlement

### Mark-to-Market

```bash
python scripts/update_paper_portfolio.py -v
```

- [ ] Open positions get current mark prices
- [ ] Unrealized P&L updates on Portfolio page
- [ ] Script exits with code 0

### Settlement

```bash
python scripts/settle_paper_positions.py -v
```

- [ ] For any demo positions in resolved markets, settlement occurs
- [ ] Realized P&L updates on Portfolio page
- [ ] Re-running the script produces identical results (idempotent)

### Decision Log Export

```bash
curl 'http://127.0.0.1:8000/decision-log/export?format=csv'
curl 'http://127.0.0.1:8000/decision-log/export?format=json'
```

- [ ] CSV export returns valid CSV with headers
- [ ] JSON export returns valid JSON array
- [ ] Both contain all decision log entries

## Day 6: Scheduling and Automation

### Cron or Systemd Timer

Set up a scan on a 15-minute interval (systemd timer example in README.md).

- [ ] Timer fires and runs `run_scan.py`
- [ ] Concurrent invocations are blocked (exit code 3)
- [ ] Dashboard updates within 1 minute of scan completion

### Log Review

- [ ] No unexpected ERROR or CRITICAL log entries
- [ ] FileLock acquisition/release is logged at DEBUG level
- [ ] Rate limiting (if any API calls hit the limit) is logged

## Day 7: Full Review

### Documentation Accuracy

- [ ] README.md quick start works end-to-end
- [ ] strategy.md matches actual system behavior
- [ ] architecture.md matches actual code layout
- [ ] paper-trading-methodology.md matches scoring/fill/settlement behavior
- [ ] live-trading-readiness.md gates are still correct
- [ ] security.md mitigations are still accurate
- [ ] This document's checklist items are all verified

### Data Quality

- [ ] No sample data is mislabeled as live (all is_sample=True correctly)
- [ ] No live data is accidentally stored as sample
- [ ] Raw snapshots have valid SHA-256 provenance hashes
- [ ] Stale trades are flagged correctly

### Safety Audit

- [ ] No real-money execution path exists (grep for `place_order` on
      live broker — should only find `DisabledLiveBroker` raising)
- [ ] Config endpoint excludes secrets
- [ ] Kill switch works
- [ ] Exposure limits work
- [ ] Idempotency keys prevent duplicates

### Performance

- [ ] Scan completes in < 60 seconds (with sample data)
- [ ] Dashboard pages load in < 2 seconds
- [ ] API responses return in < 500ms (local)
- [ ] No memory leaks over 24-hour run (check RSS growth)

### Go / No-Go Decision

After completing Day 7, record:

| Question | Answer |
|----------|--------|
| Are all safety checks passing? | _____ |
| Is paper P&L tracking correctly? | _____ |
| Are risk gates blocking as expected? | _____ |
| Is the data pipeline reliable? | _____ |
| Are docs accurate? | _____ |
| Ready to continue operating in paper mode? | _____ |
| Ready to evaluate live readiness? | _____ (requires separate PR) |

**Decision: _________________________**

**Reviewer: _________________________**

**Date: _________________________**
