#!/usr/bin/env python3
"""Non-destructive post-merge ingestion health report for Polycopy.

The default mode is intentionally conservative: it uses an isolated temporary
SQLite database plus repository-local checks, and it does not call live APIs.
Pass ``--live`` to run a small number of read-only HTTP GET probes for manual
release checks. The script never writes to the configured production database,
never places orders, and never creates cron/timer/service state.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from polycopy.db.database import Database  # noqa: E402
from polycopy.db.schema import SCHEMA_VERSION  # noqa: E402
from polycopy.db.wallet_identity import canonical_wallet_address  # noqa: E402


@dataclass(frozen=True)
class CheckResult:
    """A single health-check result."""

    name: str
    status: str
    evidence: str


def _http_get_json(url: str, timeout: float) -> tuple[bool, str]:
    """Return whether a URL responds with a JSON list/dict and short evidence."""
    request = urllib.request.Request(url, headers={"User-Agent": "polycopy-health-check/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = response.getcode()
            body = response.read(512_000)
    except urllib.error.URLError as exc:
        return False, f"request failed: {exc}"

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"HTTP {status_code}, non-JSON response: {exc}"

    if isinstance(payload, list):
        return True, f"HTTP {status_code}, JSON list length={len(payload)}"
    if isinstance(payload, dict):
        keys = ",".join(sorted(payload.keys())[:5])
        return True, f"HTTP {status_code}, JSON object keys={keys}"
    return False, f"HTTP {status_code}, unexpected JSON type={type(payload).__name__}"


def check_temp_database() -> list[CheckResult]:
    """Validate FK enforcement, schema version, and canonical wallet uniqueness."""
    with tempfile.TemporaryDirectory(prefix="polycopy-c3-health-") as tmp_dir:
        db_path = Path(tmp_dir) / "health.db"
        db = Database(db_path=db_path).connect()
        conn = db.conn

        fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        schema_version = conn.execute(
            "SELECT value FROM _meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()

        address = "  0xAbCDEFabcdefABCDEFabcdefABCDEFabcdefabcd  "
        canonical = canonical_wallet_address(address)
        conn.execute(
            """
            INSERT INTO wallets (id, address, canonical_address, label, is_sample, created_at)
            VALUES (?, ?, ?, ?, 0, datetime('now'))
            """,
            ("health-wallet-1", address.strip(), canonical, "health-check"),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO wallets
                (id, address, canonical_address, label, is_sample, created_at)
            VALUES (?, ?, ?, ?, 0, datetime('now'))
            """,
            ("health-wallet-2", address.lower().strip(), canonical, "health-check-duplicate"),
        )
        wallet_count = conn.execute(
            "SELECT COUNT(*) FROM wallets WHERE canonical_address = ?", (canonical,)
        ).fetchone()[0]
        duplicate_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT canonical_address
                FROM wallets
                WHERE canonical_address IS NOT NULL
                GROUP BY canonical_address
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        db.close()

    return [
        CheckResult(
            "database_fk_check_clean",
            "pass" if fk_enabled == 1 and not fk_rows else "fail",
            f"PRAGMA foreign_keys={fk_enabled}; foreign_key_check rows={len(fk_rows)}",
        ),
        CheckResult(
            "schema_version_visible",
            "pass" if str(schema_version) == str(SCHEMA_VERSION) else "fail",
            f"_meta schema_version={schema_version}; expected={SCHEMA_VERSION}",
        ),
        CheckResult(
            "canonical_wallet_duplicate_count_zero",
            "pass" if wallet_count == 1 and duplicate_count == 0 else "fail",
            f"canonical test row count={wallet_count}; duplicate groups={duplicate_count}",
        ),
    ]


def check_repository_coverage() -> list[CheckResult]:
    """Report the existing local code/tests that cover C3 health requirements."""
    expected_paths = {
        "data_health_endpoint": REPO_ROOT / "src/polycopy/api/app.py",
        "data_health_repository": REPO_ROOT / "src/polycopy/api/repository.py",
        "polymarket_health_tests": REPO_ROOT / "tests/test_p15_polymarket_health.py",
        "wallet_identity_tests": REPO_ROOT / "tests/test_p30_wallet_identity_normalization.py",
        "sqlite_fk_tests": REPO_ROOT / "tests/test_p37_sqlite_foreign_key_enforcement.py",
        "live_smoke_script": REPO_ROOT / "scripts/live_smoke_pr3_fixes.py",
    }
    results: list[CheckResult] = []
    for name, path in expected_paths.items():
        results.append(
            CheckResult(
                name,
                "pass" if path.exists() else "fail",
                str(path.relative_to(REPO_ROOT)) if path.exists() else f"missing {path}",
            )
        )

    app_text = (REPO_ROOT / "src/polycopy/api/app.py").read_text(encoding="utf-8")
    repo_text = (REPO_ROOT / "src/polycopy/api/repository.py").read_text(encoding="utf-8")
    results.extend(
        [
            CheckResult(
                "data_api_health_endpoint_visible",
                "pass" if '"/data/health"' in app_text else "fail",
                "GET /data/health registered in src/polycopy/api/app.py",
            ),
            CheckResult(
                "latest_experiment_status_visible",
                "pass" if "def experiments" in repo_text else "fail",
                "DashboardRepository.experiments() available for latest experiment visibility",
            ),
            CheckResult(
                "partial_fetch_not_silent",
                "pass" if "missing_capabilities" in repo_text else "fail",
                "data health reports missing capabilities rather than treating partial fetch as complete",
            ),
        ]
    )
    return results


def check_live_endpoints(timeout: float) -> list[CheckResult]:
    """Run a tiny, read-only live probe set when explicitly requested."""
    gamma_url = "https://gamma-api.polymarket.com/markets?limit=1&active=true"
    data_params = urllib.parse.urlencode({"limit": 1, "takerOnly": "false"})
    data_url = f"https://data-api.polymarket.com/trades?{data_params}"

    gamma_ok, gamma_evidence = _http_get_json(gamma_url, timeout)
    data_ok, data_evidence = _http_get_json(data_url, timeout)

    return [
        CheckResult("gamma_market_endpoint_reachable", "pass" if gamma_ok else "fail", gamma_evidence),
        CheckResult(
            "data_api_reachable_takerOnly_false_accepted",
            "pass" if data_ok else "fail",
            data_evidence,
        ),
    ]


def build_report(include_live: bool, timeout: float) -> dict[str, Any]:
    """Build a structured health report."""
    checks = [*check_temp_database(), *check_repository_coverage()]
    if include_live:
        checks.extend(check_live_endpoints(timeout))
    else:
        checks.append(
            CheckResult(
                "live_api_probes",
                "skipped",
                "pass --live to run two read-only HTTP GET probes with timeout/rate restraint",
            )
        )

    failed = [check for check in checks if check.status == "fail"]
    return {
        "scope": "C3 post-merge ingestion health check",
        "mode": "live" if include_live else "local_safe",
        "safety": {
            "production_database_touched": False,
            "orders_placed": False,
            "cron_timer_service_created": False,
            "live_api_calls": include_live,
        },
        "summary": {
            "status": "fail" if failed else "pass",
            "passed": sum(1 for check in checks if check.status == "pass"),
            "failed": len(failed),
            "skipped": sum(1 for check in checks if check.status == "skipped"),
        },
        "checks": [asdict(check) for check in checks],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run two read-only external API probes")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request live probe timeout")
    parser.add_argument("--json", action="store_true", help="emit compact JSON only")
    args = parser.parse_args()

    report = build_report(include_live=args.live, timeout=args.timeout)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["summary"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
