# Security

## Architecture

Polycopy is designed with a **fail-closed** security posture: the default
state is safe, and explicit opt-in is required for any potentially
dangerous operation.

## Threat Model

| Threat | Mitigation |
|--------|------------|
| Real-money trade execution | `DisabledLiveBroker` raises on every op. No live path exists. |
| Private key leakage | Config rejects key in paper mode. Never committed to git. |
| Sample data masquerading as live | Every record has `is_sample=True`. Dashboard shows banners. |
| Concurrent script execution corrupts DB | File-based `FileLock` prevents parallel runs. |
| Duplicate order submissions | `X-Idempotency-Key` dedup on state-changing endpoints. |
| Excessive exposure | Configurable per-market/wallet/global exposure limits. |
| Runaway order creation | Kill switch blocks all orders regardless of other settings. |
| API abuse | Rate limiting config (`POLYCOPY_HTTP_RATE_LIMIT_RPS`). |

## Secrets Management

### What We Store

- **Polymarket public endpoints** — `gamma-api.polymarket.com`,
  `clob.polymarket.com`. Not secrets. Hardcoded as defaults.
- **SQLite database path** — `polycopy.db` by default. Local only.
- **Wallet watchlist** — public Ethereum addresses. Not secrets.

### What We Do NOT Store

- Private keys (rejected by config validator in paper mode)
- API keys or tokens
- Seed phrases
- Real wallet balances
- Cookies or session tokens
- Any credential that enables real trading

### Future: Live Trading Secrets

When live trading is enabled (separate PR, see `docs/live-trading-readiness.md`):

- Private key must come from a secrets manager (Vault, AWS Secrets Manager,
  or encrypted environment variable)
- Never stored in `.env` files committed to git
- Never logged, printed, or included in API responses
- The config endpoint (`GET /config`) explicitly excludes secret fields

## Fail-Closed Design

### Config Validation

`Settings` uses Pydantic validators that raise on contradictory or
dangerous combinations:

```python
# This raises ValueError:
POLYCOPY_BROKER_MODE=paper
POLYCOPY_POLYMARKET_PRIVATE_KEY=0x...

# This raises ValueError:
POLYCOPY_PAPER_MODE=invalid_mode
```

Validation happens at import time. The application cannot start with
an unsafe configuration.

### Risk Gates

`RiskGate.check()` is called before any order is created. The evaluation
order is:

1. Kill switch → BLOCK if engaged
2. Paper mode → BLOCK if `research_only`
3. Exposure limits → BLOCK if any limit exceeded

Any error or missing data defaults to BLOCK. There is no "default allow"
path.

### DisabledLiveBroker

The `DisabledLiveBroker` is installed in the live-broker slot. Every
method raises `RuntimeError` with a clear message:

> LIVE EXECUTION IS DISABLED. This broker is a fail-closed guard.

Even if code accidentally routes to this broker (e.g., a bug in the
router), the operation fails safe.

### Kill Switch

The `OrderKillSwitch` is the highest-priority risk gate. When engaged:

- All order creation is blocked, regardless of paper mode, exposure
  limits, or any other setting
- Cannot be bypassed by configuration
- Engages via explicit operator action (API call or config change)
- Resets to inactive on process restart (intentional — requires
  re-engagement after crash/restart)

## Data Integrity

### Provenance

All raw API responses are saved with SHA-256 provenance hashes:
```python
snapshot = RawSnapshot(
    source="polymarket_gamma",
    endpoint="/markets?limit=1",
    data=response_json,
    hash_algo="sha256",  # verified on load
)
```

Corrupt or tampered snapshot files fail verification.

### Sample Data Labeling

Every record from `seed_demo_data.py` has `is_sample=True`. The dashboard
shows a blue DEMO DATA banner when sample data is detected. Scripts that
cannot reach live APIs log the failure and leave data missing — they never
silently substitute fictional values.

### Idempotency

- Settlement is idempotent — same evidence → same result
- API state-changing endpoints use SQLite-backed idempotency keys (persistent
  across restarts). Replays return the stored result without creating
  duplicates.
- Demo data seeding is idempotent with `--force`

## API Security

### Current (Local Only)

The API runs on `127.0.0.1:8000` — not exposed to the internet. No
authentication is required because:

1. The API is read-only + paper-only (no real trades)
2. It runs on localhost, not a public interface
3. The frontend proxies through Vite dev server (also localhost)

### Future (If Exposed)

If the API is ever exposed to a network:

- Add authentication (API key or JWT)
- Add CORS restrictions
- Add rate limiting per client
- Add request size limits
- Do NOT expose the kill switch engage endpoint without auth
- Do NOT expose the config endpoint without auth

## Concurrency Safety

### File Lock

Scripts use `FileLock` (fcntl-based) to prevent concurrent execution:

```python
with FileLock("/tmp/polycopy_scan.lock", timeout=5.0):
    run_scan()
```

If the lock is held, the script exits with code 3 and logs which PID
holds the lock. This prevents database corruption from parallel writes.

### Exit Codes

All scripts use structured exit codes:

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Fatal error (config, DB, etc.) |
| 2 | Partial success (some data failed) |
| 3 | Lock held by another process |

## Database Security

### Current

- SQLite file (`polycopy.db`) stores only paper trading data
- No real balances, no secrets, no credentials
- Single-writer via FileLock
- Not encrypted (local only, no secrets)

### Future (If Sensitive Data)

If live trading stores real wallet info or balances:
- Encrypt the SQLite file at rest
- Use SQLCipher or filesystem-level encryption
- Restrict file permissions to the application user only

## Dependencies

All Python dependencies are pinned to minimum versions in `pyproject.toml`:

| Package | Min Version | Known Issues |
|---------|-------------|--------------|
| fastapi | ≥0.115 | None known |
| pydantic | ≥2.0 | None known |
| httpx | ≥0.28 | None known |
| uvicorn | ≥0.30 | None known |

No dependency has known CVEs at time of writing. The `dev` extras include
`ruff` for linting but no runtime risk.

## Git Safety

### .gitignore Must Exclude

- `*.db` — SQLite databases (paper data, not for git)
- `.env` — environment files (secrets)
- `data/snapshots/` — raw API snapshots (potentially large)
- `__pycache__/` — Python bytecode
- `node_modules/` — frontend dependencies
- `dist/` — build artifacts
- `*.secret` / `*.key` / `*.pem` — credential files

### Commit Precautions

- `ruff` linter runs on commit (configured in `pyproject.toml`)
- No secrets in code (search for `0x`, `private_key`, `secret` in diff)
- API responses in `GET /config` exclude secret fields by design

## Summary

Polycopy's security model is simple: **there is no real-money path, so
the attack surface is minimal.** The primary risks are:

1. Misconfiguration (mitigated by fail-closed validators)
2. Data corruption (mitigated by FileLock and idempotency)
3. Sample data confusion (mitigated by is_sample labeling and banners)

Adding live trading expands the threat model significantly. That transition
must follow the review gates in `docs/live-trading-readiness.md`.
