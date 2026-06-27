# Polycopy — Polymarket Paper Trading Platform

Paper-only trading harness for Polymarket prediction markets. No real-money
execution path exists. All broker adapters default to a simulated paper
backend unless explicitly overridden — and even then, live execution is
gated behind fail-closed guards.

## Status

Phase 01 scaffold only. No trading logic implemented yet.

## Layout

```
src/polycopy/     → application code
  broker/         → broker adapters (paper, polymarket stub)
  market/         → market data clients (gamma, CLOB)
  portfolio/      → positions, P&L tracking
  risk/           → risk limits, position sizing
  config/         → settings, env loading
  utils/          → shared helpers
tests/            → pytest suite
docs/             → design docs, audits
data/audits/      → machine-readable capability audit artifacts
scripts/          → operational scripts
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make audit   # run data capability probe
```

## Safety

- No real-money trade execution path exists.
- Live broker adapters fail closed (refuse to execute).
- All sample/fixture data is visibly labeled.
- P06 operational scripts only use sample market/pricing data when invoked with
  `--use-sample`; live provider failures are logged and leave data missing
  rather than substituting fictional sample prices or markets.
