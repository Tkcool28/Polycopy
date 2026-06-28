# Architecture

## System Overview

Polycopy is a two-tier application:

1. **Backend** — Python 3.11+ / FastAPI / SQLite
2. **Frontend** — React / TypeScript / Vite

Both run locally. The backend serves a REST API on port 8000. The frontend
dev server proxies API calls to the backend and serves the SPA on port 5173.

No external services are required beyond Polymarket's public read-only API.

```
┌─────────────────────────────────────────────────┐
│                  Browser                         │
│  ┌─────────────────────────────────────────────┐│
│  │  React SPA (Vite dev :5173 / build :8000)  ││
│  └─────────────┬───────────────────────────────┘│
└────────────────┼────────────────────────────────┘
                 │  HTTP (proxied in dev)
┌────────────────┼────────────────────────────────┐
│                ▼   FastAPI (:8000)               │
│  ┌─────────────────────────────────────────────┐│
│  │  API Layer (20 endpoints, Pydantic v2)      ││
│  └──────┬──────────────────────────────────────┘│
│         │                                       │
│  ┌──────┴──────────────────────────────────┐    │
│  │  Domain Layer                           │    │
│  │  market / order / position / signal     │    │
│  │  wallet / decision_log / experiment     │    │
│  └──────┬──────────────────────────────────┘    │
│         │                                       │
│  ┌──────┴──────────────────────────────────┐    │
│  │  Engine + Scoring + Risk                │    │
│  │  evaluate / scoring.engine / risk.*     │    │
│  └──────┬──────────────────────────────────┘    │
│         │                                       │
│  ┌──────┴──────────────────────────────────┐    │
│  │  Adapters (broker implementations)      │    │
│  │  paper_broker / disabled_live_broker    │    │
│  │  sample / polymarket / bullpen          │    │
│  └──────┬──────────────────────────────────┘    │
│         │                                       │
│  ┌──────┴──────────────────────────────────┐    │
│  │  Providers (data interfaces)            │    │
│  │  execution_broker / market_data         │    │
│  │  resolution / trade_feed / wallet_data  │    │
│  └──────┬──────────────────────────────────┘    │
│         │                                       │
│  ┌──────┴──────┐    ┌────────────────────┐      │
│  │  SQLite DB  │    │  Polymarket API    │      │
│  │  polycopy.db│    │  (read-only,       │      │
│  └─────────────┘    │   no auth)         │      │
│                      └────────────────────┘      │
│  Python 3.11+ Backend                           │
└─────────────────────────────────────────────────┘
```

## Backend Architecture

### Config (`config/settings.py`)

Pydantic Settings with `POLYCOPY_` env prefix. Fail-closed validators:
- Reject `polymarket_private_key` when `broker_mode=paper`
- Validate `paper_mode` ∈ {research_only, paper_manual, paper_auto}
- Validate `log_level` against Python logging levels
- Validate `snapshot_hash_algo` against `hashlib.algorithms_available`

Singleton via `get_settings(reload=False)` — cached for process lifetime.

### Domain Models (`domain/`)

Pure Python dataclasses and Pydantic models. No business logic, no I/O.

| Model | Purpose |
|-------|---------|
| `Market` | Prediction market with outcomes, prices, volume |
| `Order` | Paper order with status lifecycle |
| `Position` | Open position with avg price, quantity, P&L |
| `Signal` | Tradeable signal with edge, confidence, direction |
| `Wallet` | Tracked wallet with balance, verdict |
| `SourceTrade` | Raw trade from upstream wallet |
| `RawSnapshot` | API response with SHA-256 provenance hash |
| `DecisionLogEntry` | Immutable audit entry for trade decisions |
| `ExperimentRun` | Experiment metrics recording |
| `CopyabilityScore` | Score + verdict + component breakdown |

### Scoring Engine (`scoring/engine.py`)

Deterministic 0-100 scoring from 7 weighted components. No randomness.
Outputs a `CopyabilityScore` with per-component values, data quality tags,
missing field penalties, and a verdict (COPY_CANDIDATE / WATCHLIST / SKIP /
INCOMPLETE). See `docs/copyability-scoring-v1.md`.

### Risk System (`risk/`)

| Module | Purpose |
|--------|---------|
| `gates.py` | PaperMode, OrderKillSwitch, ExposureLimits, RiskGate |
| `fill_model.py` | MarketDepth, FillQuote, FillModel, ReviewDelay |
| `marks.py` | MarkEngine — mark-to-market pricing |
| `pnl.py` | PnlTracker — FIFO lot-level P&L |
| `settlement.py` | SettlementEngine — idempotent market resolution |
| `counterfactual.py` | CounterfactualTracker — what-if analysis |

### Adapters (`adapters/`)

| Adapter | Purpose |
|---------|---------|
| `paper_broker.py` | PaperBroker — simulated fills in SQLite (is_live=False) |
| `disabled_live_broker.py` | DisabledLiveBroker — raises on every op (fail-closed guard) |
| `sample.py` | Sample adapters — fictional data with is_sample=True |
| `polymarket.py` | Polymarket public API adapter (read-only Gamma + CLOB) |
| `bullpen.py` | Bullpen CLI skeleton (CLI not found on host; raises on use) |
| `snapshot_provenance.py` | Raw API snapshot persistence with SHA-256 verification |

### API (`api/`)

FastAPI application with 20 typed endpoints. All response models are Pydantic
v2. State-changing endpoints use SQLite-backed idempotency keys (persistent
across restarts). Config endpoint excludes secrets. Includes persistent
`IdempotencyStore` backed by SQLite `idempotency_keys` table.

### Discovery (`discovery/`)

| Module | Purpose |
|--------|---------|
| `wallet_discovery.py` | Multi-source wallet discovery with dedup |
| `models.py` | Discovery result types |

Related-wallet detection: conservative heuristic with max confidence 0.75.
Types: shared_market, similar_volume, close_timing, shared_deposit.

### Database (`db/`)

SQLite via Python stdlib `sqlite3`. Schema defined in `db/schema.py`.
Single-file database, no migrations — schema created on first connection.

### Concurrency (`utils/concurrency.py`)

File-based `FileLock` using `fcntl` (Unix). Prevents concurrent script
execution. Second invocation exits with code 3 ("lock held").

## Frontend Architecture

### Stack

- React 19 + TypeScript
- Vite 6 with `@vitejs/plugin-react`
- React Router v7 (SPA routing)
- No UI framework — custom dark theme CSS

### Component Tree

```
App
└── Layout
    ├── Banners (PAPER MODE / DEMO DATA / KILL SWITCH)
    ├── Header (global KPI metrics)
    ├── Nav (bottom navigation bar)
    └── <Page>
        ├── OverviewPage    — KPI cards, recent scans/signals
        ├── WalletsPage     — wallet list + detail
        ├── TradeRadarPage  — live signal feed
        ├── TradeDetailPage — single signal detail
        ├── SignalsPage     — all signals
        ├── PaperOrdersPage — preview/approve/reject
        ├── PortfolioPage   — positions + P&L
        ├── RiskConsolePage — kill switch, gates, limits
        ├── ExperimentsPage — experiment metrics
        ├── DataHealthPage  — freshness KPIs and per-source table status
        └── SettingsPage    — config (secrets excluded)
```

### API Client

`frontend/src/lib/api.ts` — typed fetch wrapper. All requests go through
the Vite dev proxy (`/health`, `/system`, `/wallets`, etc. → `:8000`).

### Styling

Mobile-first dark theme. CSS custom properties for colors. Banners use
distinct background colors (amber for paper, blue for demo, red for kill
switch).

## Data Flow

### Smart-Money Collection

```
Polymarket Gamma API → collect_smart_money_data.py
  → markets (active, top volume)
  → trades per market
  → wallet balances (USDC)
  → raw snapshots (SHA-256)
  → SQLite
```

### Scanning Pipeline

```
run_scan.py
  → wallet discovery (multi-source, dedup)
  → trade detection (staleness > 120s flagged, dedup 60s window)
  → copyability scoring (7 components, deterministic 0-100)
  → verdict (COPY_CANDIDATE / WATCHLIST / SKIP / INCOMPLETE)
  → signal generation (edge-based)
  → decision log recording
  → experiment run recording
  → missing data logging
```

### Paper Trading

```
POST /paper/preview
  → RiskGate.check() [kill switch → paper mode → exposure limits]
  → FillModel.quoteFill() [bid/ask + slippage + fees]
  → ReviewDelay (paper_manual: 30s wait)

POST /paper/approve (with SQLite-backed idempotency)
  → PaperBroker.place_order()
  → PnlTracker.record_buy/sell (FIFO lots)
  → Position updated
  → DecisionLogEntry created
  → Counterfactual scenarios computed
  → IdempotencyStore.check_and_store() (persistent replay protection)

GET /paper/orders → persistent order list (survives API restart)
GET /positions → persistent position list (survives API restart)
```

### Settlement

```
settle_paper_positions.py
  → check Polymarket for resolved markets
  → for each resolved market, find open positions
  → SettlementEngine.settle() (idempotent)
  → update position with realized P&L
```

## External Dependencies

| Dependency | Version | Purpose |
|------------|---------|---------|
| FastAPI | ≥0.115 | REST API framework |
| Pydantic | ≥2.0 | Data validation, settings |
| pydantic-settings | ≥2.0 | Env-based config |
| httpx | ≥0.28 | Async HTTP client |
| uvicorn | ≥0.30 | ASGI server |
| React | 19 | Frontend SPA |
| Vite | 6 | Frontend build tool |

## File Sizes (Approximate)

```
src/polycopy/    — ~5,000 lines Python
frontend/src/    — ~1,500 lines TypeScript/CSS
scripts/         — ~1,200 lines Python
tests/           — ~2,600 lines Python (466 tests)
docs/            — ~2,000 lines Markdown
```
