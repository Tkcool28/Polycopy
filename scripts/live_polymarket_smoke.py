#!/usr/bin/env python3
"""Live read-only Polymarket data health smoke.

Probes documented public endpoints, records HTTP status, latency, fetched/persisted
counts, and writes results to the provider_health + raw_snapshots tables.

Run: python scripts/live_polymarket_smoke.py [--db PATH] [--persist]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("polycopy.smoke")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Capabilities we test
CAPABILITIES = [
    ("polymarket", "gamma_markets_active"),
    ("polymarket", "gamma_markets_by_volume"),
    ("polymarket", "gamma_events"),
    ("polymarket", "gamma_resolution_check"),
    ("polymarket", "clob_markets"),
    ("polymarket", "clob_trades"),
]


async def probe_gamma(client, endpoint: str, params: dict) -> dict:
    """Probe a Gamma endpoint, return result dict."""
    start = time.monotonic()
    try:
        resp = await client.get(endpoint, params=params)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "ok": resp.status_code == 200,
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "http_status": resp.status_code,
            "error": None,
            "bytes": len(resp.content) if resp.status_code == 200 else 0,
        }
    except Exception as exc:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "ok": False,
            "status": None,
            "latency_ms": latency_ms,
            "http_status": None,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
        }


async def probe_clob(endpoint: str, params: dict, timeout: float = 10.0) -> dict:
    """Probe a CLOB endpoint with its own client, return result dict."""
    import httpx

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            base_url=CLOB_BASE, timeout=timeout,
            headers={"User-Agent": "polycopy-smoke/0.3"},
        ) as client:
            resp = await client.get(endpoint, params=params)
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return {
                "ok": resp.status_code == 200,
                "status": resp.status_code,
                "latency_ms": latency_ms,
                "http_status": resp.status_code,
                "error": None,
                "bytes": len(resp.content) if resp.status_code == 200 else 0,
            }
    except Exception as exc:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "ok": False,
            "status": None,
            "latency_ms": latency_ms,
            "http_status": None,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
        }


async def run_probes(persist: bool, db_path: Path | None = None) -> dict:
    """Run all probes and optionally persist results to DB."""
    import httpx

    results = {}
    async with httpx.AsyncClient(
        base_url=GAMMA_BASE, timeout=10.0,
        headers={"User-Agent": "polycopy-smoke/0.3"},
    ) as gamma_client:
        # Gamma probes
        r = await probe_gamma(gamma_client, "/markets", {"active": "true", "closed": "false", "limit": 5})
        results["gamma_markets_active"] = r
        logger.info("gamma_markets_active: %s (%sms)", "OK" if r["ok"] else "FAIL", r["latency_ms"])

        r = await probe_gamma(gamma_client, "/markets", {
            "order": "volume24hr", "ascending": "false", "limit": 5,
            "active": "true", "closed": "false",
        })
        results["gamma_markets_by_volume"] = r
        logger.info("gamma_markets_by_volume: %s (%sms)", "OK" if r["ok"] else "FAIL", r["latency_ms"])

        r = await probe_gamma(gamma_client, "/events", {"active": "true", "limit": 3})
        results["gamma_events"] = r
        logger.info("gamma_events: %s (%sms)", "OK" if r["ok"] else "FAIL", r["latency_ms"])

        r = await probe_gamma(gamma_client, "/markets", {"closed": "true", "limit": 1})
        results["gamma_resolution_check"] = r
        logger.info("gamma_resolution_check: %s (%sms)", "OK" if r["ok"] else "FAIL", r["latency_ms"])

        # CLOB probes
        r = await probe_clob("/markets", {"next_cursor": "MA==", "limit": 3})
        results["clob_markets"] = r
        logger.info("clob_markets: %s (%sms)", "OK" if r["ok"] else "FAIL", r["latency_ms"])

        r = await probe_clob("/trades", {"next_cursor": "MA==", "limit": 1})
        results["clob_trades"] = r
        logger.info("clob_trades: %s (%sms)", "OK" if r["ok"] else "FAIL", r["latency_ms"])

    # Persist to DB if requested
    if persist:
        await _persist_results(results, db_path)

    return results


async def _persist_results(results: dict, db_path: Path | None = None) -> None:
    """Write probe results to provider_health table."""
    from polycopy.db.database import get_database
    from polycopy.config.settings import get_settings

    if db_path:
        import os
        os.environ["POLYCOPY_DB_PATH"] = str(db_path)
        get_settings(reload=True)

    db = get_database(reload=True)
    now = datetime.now(timezone.utc).isoformat()

    for capability, result in results.items():
        provider = "polymarket"
        status = "ok" if result["ok"] else "failure"
        if result.get("http_status") == 401:
            status = "disabled"
        elif result.get("http_status") == 404:
            status = "missing"

        http_status = result.get("http_status")
        error_msg = result.get("error", "")

        # Upsert into provider_health
        existing = db.fetchone(
            "SELECT id FROM provider_health WHERE provider = ? AND capability = ?",
            (provider, capability),
        )
        if existing:
            db.execute(
                "UPDATE provider_health SET status = ?, last_attempt = ?, "
                "http_status = ?, error_message = ? WHERE provider = ? AND capability = ?",
                (status, now, http_status, error_msg, provider, capability),
            )
        else:
            db.execute(
                "INSERT INTO provider_health (provider, capability, status, last_attempt, "
                "http_status, error_message) VALUES (?, ?, ?, ?, ?, ?)",
                (provider, capability, status, now, http_status, error_msg),
            )

        # Update last_success on ok
        if result["ok"]:
            db.execute(
                "UPDATE provider_health SET last_success = ? WHERE provider = ? AND capability = ?",
                (now, provider, capability),
            )

    db.conn.commit()
    logger.info("Results persisted to provider_health table.")


def main():
    parser = argparse.ArgumentParser(description="Live Polymarket data health smoke")
    parser.add_argument("--persist", action="store_true", help="Write results to DB")
    parser.add_argument("--db", type=Path, default=None, help="DB path override")
    args = parser.parse_args()

    results = asyncio.run(run_probes(persist=args.persist, db_path=args.db))

    # Print summary
    print("\n=== P15 LIVE SMOKE SUMMARY ===")
    ok_count = sum(1 for r in results.values() if r["ok"])
    fail_count = len(results) - ok_count
    print(f"  Passed: {ok_count}  Failed: {fail_count}")
    for cap, r in results.items():
        status_label = "OK" if r["ok"] else "FAIL"
        http = r.get("http_status") or "N/A"
        print(f"  {cap}: {status_label} (HTTP {http}, {r['latency_ms']}ms)")
        if r.get("error"):
            print(f"    Error: {r['error']}")

    # Exit non-zero on total failure
    if fail_count == len(results):
        print("\nALL PROBES FAILED")
        raise SystemExit(1)
    if fail_count > 0:
        print(f"\n{fail_count} probe(s) failed — check output above")


if __name__ == "__main__":
    main()
