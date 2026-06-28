# Polycopy — Polymarket Paper Trading Platform

Paper-only copy-trading harness for Polymarket prediction markets. Discovers
"smart money" wallets, scores copyability, generates signals, and simulates
paper trades — with no real-money execution path at any layer.

**Status:** v0.2.0 — Phases 01–17 complete. Paper trading, risk gates,
persistent API, React dashboard, data collection pipeline, and live-read-only
Polymarket validation operational. 466 tests passing.

## Safety First

- **No real-money trade execution path exists.** The `DisabledLiveBroker`
  raises on every operation. `PaperBroker` simulates fills in SQLite only.
- **Fail-closed by default.** Config rejects private keys in paper mode.
  Risk gates block on error or missing data.
- **Kill switch.** `POLYCOPY_ORDER_KILL_SWITCH=true` blocks all order
  creation regardless of other settings.
- **Sample data is visibly labeled.** Every record carries `is_sample=True`.
  Dashboard shows PAPER MODE / DEMO DATA / KILL SWITCH banners.

## Quick Start

```bash
# Backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Seed demo data (clearly labeled sample — safe)
python scripts/seed_demo_data.py

# Optional: allow API demo fallback responses for an otherwise empty DB.
# Default is false: real empty tables return empty collections and no sample data.
export POLYCOPY_ENABLE_DEMO_DATA=true

# Start API server
uvicorn polycopy.api.app:app --host 127.0.0.1 --port 8000

# Dashboard (separate terminal)
cd frontend && npm install && npm run dev
# → http://127.0.0.1:5173
```

## Daily Workflow

```bash
# 1. Collect smart-money data from Polymarket public API
python scripts/collect_smart_money_data.py

# 2. Run full scan (discover → score → verdict → signal)
python scripts/run_scan.py

# 3. Mark open positions to market
python scripts/update_paper_portfolio.py

# 4. Settle resolved markets (idempotent, safe to re-run)
python scripts/settle_paper_positions.py
```

Each script uses a **file-based concurrency guard** — second invocations
exit with code 3 ("lock held") instead of corrupting the database.

### Scheduling Examples

**Manual** — run scripts from a terminal when ready.

**systemd timer** (verified pattern):

```ini
# /etc/systemd/system/polycopy-scan.service
[Unit]
Description=Polycopy smart-money scan
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/root/Polycopy
ExecStart=/root/Polycopy/.venv/bin/python scripts/run_scan.py
User=root

# /etc/systemd/system/polycopy-scan.timer
[Unit]
Description=Run Polycopy scan every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

**cron** (alternative):

```cron
*/15 * * * * cd /root/Polycopy && .venv/bin/python scripts/run_scan.py >> /var/log/polycopy-scan.log 2>&1
```

## Layout

```
src/polycopy/
  adapters/        → paper_broker, disabled_live_broker, sample, polymarket, bullpen
  api/             → FastAPI app + Pydantic v2 response models
  broker/          → broker interface
  config/          → settings (env-prefixed POLYCOPY_*), fail-closed validation
  db/              → SQLite database + schema
  discovery/       → wallet discovery, trade detection, related-wallet heuristic
  domain/          → core models (market, order, position, signal, wallet, ...)
  engine/          → evaluate: scoring → verdict → signal pipeline
  portfolio/       → portfolio tracking
  providers/       → execution_broker, market_data, resolution, trade_feed, wallet_data
  risk/            → gates, fill_model, marks, pnl, settlement, counterfactual
  scoring/         → deterministic 0-100 copyability scoring engine
  utils/           → FileLock concurrency guard
scripts/           → operational scripts (collect, scan, update, settle, seed, probe)
frontend/          → React/Vite/TypeScript dark command-center dashboard
tests/             → 466 pytest tests (Phases 01–17)
docs/              → design docs, audits, methodology
data/audits/       → machine-readable capability audit artifacts
```

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health check |
| GET | `/system/status` | Config + kill switch state (secrets excluded) |
| GET | `/config` | Full config view (secrets excluded) |
| GET | `/scans` | Wallet scan results |
| GET | `/wallets` | List tracked wallets |
| GET | `/wallets/{id}` | Wallet detail |
| GET | `/signals` | List signals |
| GET | `/signals/{id}` | Signal detail |
| POST | `/paper/preview` | Preview paper order (risk-checked, no fill) |
| POST | `/paper/approve` | Approve a paper order |
| POST | `/paper/reject` | Reject a paper order |
| GET | `/paper/orders` | List paper orders |
| GET | `/positions` | List positions |
| GET | `/portfolio/summary` | Portfolio summary with P&L |
| GET | `/risk/console` | Risk gate state + exposure limits |
| GET | `/decision-log` | Decision audit trail |
| GET | `/decision-log/export?format=csv\|json` | Export decisions |
| GET | `/experiments` | Experiment run metrics |
| GET | `/data/health` | Data source health |

All state-changing endpoints use SQLite-backed idempotency keys (persistent
across restarts). Duplicate requests replay the stored result.

Dashboard also includes a **Data Health** page (`/data-health`) showing freshness
KPIs and per-source table status.

## Dashboard Pages

| Route | Page | Description |
|-------|------|-------------|
| `/` | Overview | KPI cards: positions, P&L, signals, recent scans |
| `/wallets` | Wallets | Tracked wallet list + detail |
| `/radar` | Trade Radar | Live signal feed with detail view |
| `/signals` | Signals | All signals with edge/confidence |
| `/orders` | Paper Orders | Preview/approve/reject + order list |
| `/portfolio` | Portfolio | Open positions + P&L breakdown |
| `/risk` | Risk Console | Kill switch, gate state, exposure limits |
| `/experiments` | Experiments | Experiment run metrics |
|| `/data-health` | Data Health | Freshness KPIs and per-source table status |
| `/settings` | Settings | Config view (secrets excluded) |

## Paper Trading Modes

| Mode | Orders | Automation | Use Case |
|------|--------|------------|----------|
| `research_only` | Blocked | None | Read-only research |
| `paper_manual` | Require approval | After review delay (30s default) | Careful evaluation |
| `paper_auto` | Fill after risk gates | Automatic | Speed testing |

Set via `POLYCOPY_PAPER_MODE=paper_manual` (default).

## Risk Gates

Evaluated in order — first block wins:

1. **Kill switch** — `POLYCOPY_ORDER_KILL_SWITCH=true` blocks everything
2. **Paper mode** — `research_only` blocks all orders
3. **Exposure limits** — per-market, per-wallet, per-outcome, global, order size

All limits default to `0` (unlimited). Set via env:
```bash
POLYCOPY_MAX_EXPOSURE_PER_MARKET=100
POLYCOPY_MAX_EXPOSURE_GLOBAL=500
POLYCOPY_MAX_ORDER_SIZE=25
```

## Configuration

All settings are env-prefixed `POLYCOPY_*` and loaded from `.env` if present.

| Variable | Default | Description |
|----------|---------|-------------|
| `POLYCOPY_BROKER_MODE` | `paper` | `paper` or `polymarket` |
| `POLYCOPY_PAPER_MODE` | `paper_manual` | `research_only`, `paper_manual`, `paper_auto` |
| `POLYCOPY_ORDER_KILL_SWITCH` | `false` | Global order block |
| `POLYCOPY_DB_PATH` | `polycopy.db` | SQLite path |
| `POLYCOPY_ENABLE_DEMO_DATA` | `false` | Explicit demo/sample API fallback mode. Demo data is returned only when this is `true`; otherwise an empty real DB returns empty collections and accurate data-health. Demo/sample records must carry `is_sample=true` and DEMO DATA / SAMPLE DATA labels. |
| `POLYCOPY_LOG_LEVEL` | `INFO` | Logging level |
| `POLYCOPY_HTTP_RATE_LIMIT_RPS` | `2.0` | Public API rate limit |
| `POLYCOPY_FILL_FEE_RATE` | `0.001` | Paper fill fee (0.1%) |
| `POLYCOPY_REVIEW_DELAY_SECONDS` | `30.0` | Manual mode review window |

See `src/polycopy/config/settings.py` for the full list with validators.

## Testing

```bash
python -m pytest tests/ -v          # 466 tests
python -m pytest tests/ -k test_p04 # Phase 04 risk gates
python -m pytest tests/ -k test_p06 # Phase 06 data collection
python -m pytest tests/ -k test_p08 # Phase 08 dashboard features
python -m pytest tests/ -k test_p17 # P17 end-to-end integration tests
```

## Documents

| Document | Location |
|----------|----------|
| Strategy | `strategy.md` |
| Architecture | `docs/architecture.md` |
| Paper Trading Methodology | `docs/paper-trading-methodology.md` |
| Live Trading Readiness | `docs/live-trading-readiness.md` |
| Security | `docs/security.md` |
| Seven-Day Review | `docs/seven-day-review.md` |
| Copyability Scoring v1 | `docs/copyability-scoring-v1.md` |
| Data Capability Audit | `docs/data-capability-audit.md` |

## License

MIT
