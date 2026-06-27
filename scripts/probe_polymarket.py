#!/usr/bin/env python3
"""
Polymarket data capability probe.

Probes documented public read-only endpoints and records availability,
response shape, and latency. No authentication, no trading, no private data.

Run: python scripts/propose_polymarket.py
Output: data/audits/data-capability-audit.json (written after probe)
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

PROBES = [
    ("gamma_markets_active", f"{GAMMA_BASE}/markets?limit=1&active=true&closed=false"),
    ("gamma_markets_closed", f"{GAMMA_BASE}/markets?limit=1&closed=true"),
    ("gamma_markets_top24h", f"{GAMMA_BASE}/markets?limit=1&closed=false&order=volume24hr&ascending=false"),
    ("gamma_events", f"{GAMMA_BASE}/events?limit=1&active=true"),
    ("clob_markets", f"{CLOB_BASE}/markets?next_cursor=MA=="),
]


def http_get(url: str, timeout: float = 15.0) -> dict:
    """GET a URL, return structured result with timing."""
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polycopy-probe/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            return {
                "ok": True,
                "status": resp.status,
                "latency_ms": latency_ms,
                "bytes": len(body),
                "content_type": resp.headers.get("Content-Type", ""),
            }
    except Exception as exc:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc)[:300],
            "latency_ms": latency_ms,
        }


def extract_sample_keys(url: str, label: str) -> dict:
    """Fetch and extract top-level keys from first array element."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "polycopy-probe/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return {label: {"first_element_keys": sorted(data[0].keys())}}
            if isinstance(data, dict):
                return {label: {"top_keys": sorted(data.keys())}}
            return {label: {"type": type(data).__name__}}
    except Exception as exc:
        return {label: {"error": str(exc)[:200]}}


def main() -> None:
    results = {}
    for name, url in PROBES:
        print(f"  probing {name} ...", end=" ", flush=True)
        r = http_get(url)
        print(f"{'OK' if r['ok'] else 'FAIL'} ({r['latency_ms']}ms)")
        results[name] = r

    # Extract schema samples
    schema = {}
    schema.update(extract_sample_keys(f"{GAMMA_BASE}/markets?limit=1&active=true", "gamma_market_schema"))
    schema.update(extract_sample_keys(f"{GAMMA_BASE}/events?limit=1", "gamma_event_schema"))
    schema.update(extract_sample_keys(f"{CLOB_BASE}/markets?next_cursor=MA==", "clob_market_schema"))

    audit = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": "polycopy-probe/0.1",
        "gamma_base": GAMMA_BASE,
        "clob_base": CLOB_BASE,
        "probes": results,
        "schema_samples": schema,
        "notes": [
            "All probes are unauthenticated GET requests to documented public endpoints.",
            "No private interfaces, authenticated endpoints, or order placement were touched.",
            "Bullpen CLI: NOT FOUND on this host (not installed).",
        ],
    }

    out_path = Path(__file__).resolve().parent.parent / "data" / "audits" / "data-capability-audit.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(audit, indent=2))
    print(f"\nAudit written to {out_path}")


if __name__ == "__main__":
    main()
