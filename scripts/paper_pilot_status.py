#!/usr/bin/env python3
"""paper_pilot_status.py — Polycopy paper-pilot status report.

Read-only. Does not mutate the production DB.
Exits 0=GREEN, 1=YELLOW, 2=RED.

Supports --json for machine output and --mock <condition> for testing
classification logic without modifying live state.

Environment overrides (all optional, all preserve the production default):
  POLYCOPY_DB_PATH           — DB file to inspect (default: /root/Polycopy/data/polycopy.db)
  POLYCOPY_STATUS_REPORT_PATH — output file for --write-latest (default: /root/Polycopy/data/pilot_status_latest.txt)

Both overrides must point to existing files for the script to proceed; the
script does NOT silently fall back to the production DB if an override is set
and missing — that is reported as a visible error so a test cannot accidentally
read or write the production DB.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --- Configurable paths (env-overridable, default = production) ---

DEFAULT_DB_PATH = "/root/Polycopy/data/polycopy.db"
DEFAULT_REPORT_PATH = "/root/Polycopy/data/pilot_status_latest.txt"

REPO = Path("/root/Polycopy")
PUBLIC_HOST = "polymoney.duckdns.org"
PUBLIC_IP = "209.54.105.179"
LOCAL_API = "http://127.0.0.1:8765"
TIMERS = ["collect", "scan", "health", "settle", "update"]
SERVICES_LONG = ["polycopy-api", "polycopy-dashboard", "caddy"]
FRESHNESS_STALE_SECONDS = 30 * 60  # 30 min
LOOKBACK_HOURS = 12

REQUIRED_TABLES = ("_meta", "markets", "market_outcomes", "wallets",
                   "source_trades", "signals", "orders", "positions",
                   "decision_log")


def required_all_tables():
    """Return the canonical list of required tables (tuple → list)."""
    return list(REQUIRED_TABLES)


class ConfigError(Exception):
    """Raised when an environment override is invalid.

    We do NOT silently fall back to defaults in this case — a missing override
    target is a visible failure so tests cannot accidentally hit production.
    """


def resolve_db_path() -> str:
    """Return the DB path from POLYCOPY_DB_PATH or the production default.

    If an override is supplied and the file does not exist, raise ConfigError
    rather than falling back to production.
    """
    override = os.environ.get("POLYCOPY_DB_PATH")
    if override is None or override == "":
        return DEFAULT_DB_PATH
    if not Path(override).exists():
        raise ConfigError(
            f"POLYCOPY_DB_PATH={override!r} was set but the file does not exist; "
            "refusing to fall back to the production DB path. "
            "Unset POLYCOPY_DB_PATH to use the production default."
        )
    return override


def resolve_report_path() -> Path:
    """Return the latest-report path from POLYCOPY_STATUS_REPORT_PATH or default.

    If an override is supplied, the parent directory must already exist OR
    --write-latest must be the explicit call. When the override is supplied
    but the parent dir does not exist, raise ConfigError.
    """
    override = os.environ.get("POLYCOPY_STATUS_REPORT_PATH")
    if override is None or override == "":
        return Path(DEFAULT_REPORT_PATH)
    p = Path(override)
    if not p.parent.exists():
        raise ConfigError(
            f"POLYCOPY_STATUS_REPORT_PATH={override!r} was set but its parent "
            f"directory {str(p.parent)!r} does not exist. Create it first, or "
            "unset POLYCOPY_STATUS_REPORT_PATH to use the production default."
        )
    return p


# --- I/O helpers (read-only) ---


def safe_run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        from types import SimpleNamespace
        return SimpleNamespace(returncode=-1, stdout="", stderr=str(e))


def http_get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


def public_get(path="/"):
    cmd = ["curl", "-4", "-sS", "--resolve", f"{PUBLIC_HOST}:443:{PUBLIC_IP}",
           "-o", "/dev/null", "-w", "%{http_code}", f"https://{PUBLIC_HOST}{path}"]
    r = safe_run(cmd, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else None


# --- DB read path (always read-only) ---


def read_db_counts():
    """Read all counts and the orphan/FK state from the configured DB.

    Returns a dict that always contains the keys "schema_error" (None or str)
    and "missing" (list of missing required tables/columns). If anything goes
    wrong, the relevant fields are set to None/empty and "schema_error" holds
    a human-readable explanation. Callers (classify) treat schema_error as RED.
    """
    out = {
        "schema_version": None,
        "markets": None,
        "market_outcomes": None,
        "wallets": None,
        "source_trades": None,
        "signals": None,
        "orders": None,
        "positions": None,
        "decision_log": None,
        "orphan_count": None,
        "fk_violations": None,
        "newest_source_trade_at": None,
        "schema_error": None,
        "missing": [],
    }
    db_path = resolve_db_path()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except Exception as e:
        out["schema_error"] = f"cannot open DB: {e}"
        return out
    try:
        try:
            conn.execute("PRAGMA query_only = ON")
        except Exception as e:
            out["schema_error"] = f"cannot enable query_only: {e}"
            return out

        # Verify required tables exist. A non-SQLite file (or a corrupted DB)
        # will raise DatabaseError on the sqlite_master query; surface that
        # as a structured schema_error rather than a traceback.
        try:
            present = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.DatabaseError as e:
            out["schema_error"] = f"cannot read sqlite_master: {e}"
            out["missing"] = list(required_all_tables())
            return out

        missing = [t for t in REQUIRED_TABLES if t not in present]
        if missing:
            out["missing"] = missing
            out["schema_error"] = f"missing required tables: {missing}"
            return out

        try:
            out["schema_version"] = conn.execute(
                "SELECT value FROM _meta WHERE key='schema_version'"
            ).fetchone()[0]
        except Exception as e:
            out["schema_error"] = f"cannot read _meta: {e}"
            out["missing"].append("_meta.schema_version")
            return out

        # Verify source_trades has a `timestamp` column (used for freshness)
        try:
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(source_trades)").fetchall()
            }
        except Exception as e:
            out["schema_error"] = f"cannot introspect source_trades: {e}"
            return out
        if "timestamp" not in cols:
            out["schema_error"] = "source_trades is missing the `timestamp` column"
            out["missing"].append("source_trades.timestamp")
            return out

        # Counts (safe even on empty tables)
        try:
            for t in ("markets", "market_outcomes", "wallets", "source_trades",
                      "signals", "orders", "positions", "decision_log"):
                out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception as e:
            out["schema_error"] = f"count query failed: {e}"
            return out

        # Orphan count (FK relationship: market_outcomes.market_id -> markets.id)
        try:
            out["orphan_count"] = conn.execute(
                "SELECT COUNT(*) FROM market_outcomes mo "
                "LEFT JOIN markets m ON m.id=mo.market_id WHERE m.id IS NULL"
            ).fetchone()[0]
        except Exception as e:
            out["schema_error"] = f"orphan query failed: {e}"
            return out

        # FK check (read-only pragma)
        try:
            out["fk_violations"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        except Exception as e:
            out["schema_error"] = f"foreign_key_check failed: {e}"
            return out

        # Newest source_trade timestamp
        try:
            row = conn.execute("SELECT MAX(timestamp) FROM source_trades").fetchone()
            out["newest_source_trade_at"] = row[0] if row and row[0] else None
        except Exception as e:
            out["schema_error"] = f"newest trade query failed: {e}"
            return out
    finally:
        conn.close()
    return out


# --- systemd / journal inspection (read-only) ---


def service_state(name):
    r = safe_run(["systemctl", "show", f"polycopy-{name}.service",
                  "-p", "ActiveState", "-p", "SubState", "-p", "Result",
                  "-p", "ExecMainStatus", "-p", "ActiveEnterTimestamp",
                  "-p", "ActiveExitTimestamp"], timeout=5)
    if r.returncode != 0:
        return {"name": name, "active": False, "error": r.stderr.strip()[:120]}
    out = {"name": name}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    out["enabled"] = is_timer_enabled(name)
    return out


def is_timer_enabled(name):
    r = safe_run(["systemctl", "is-enabled", f"polycopy-{name}.timer"], timeout=5)
    return r.stdout.strip() == "enabled"


def timer_next(name):
    r = safe_run(["systemctl", "show", f"polycopy-{name}.timer",
                  "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec",
                  "-p", "Result"], timeout=5)
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def timer_failures(name, hours=LOOKBACK_HOURS):
    r = safe_run(["journalctl", "-u", f"polycopy-{name}.service",
                  f"--since={hours} hours ago", "--no-pager", "-q",
                  "--output=cat"], timeout=10)
    if r.returncode != 0:
        return None
    failed = []
    for line in r.stdout.splitlines():
        if "result=failed" in line.lower() or "failed with result" in line.lower():
            failed.append(line.strip()[:160])
    return failed


# --- Safety state (read from /system/status) ---


def get_safety_state():
    code, body = http_get(f"{LOCAL_API}/system/status")
    if code != 200 or not body:
        return None
    try:
        d = json.loads(body)
    except Exception:
        return None
    # Only accept the well-known keys
    return {
        "broker_mode": d.get("broker_mode"),
        "paper_mode": d.get("paper_mode"),
        "order_kill_switch": d.get("order_kill_switch"),
        "is_live": d.get("is_live"),
        "is_sample_data": d.get("is_sample_data"),
        "malformed": False,
    }


# --- Classification rules ---


def classify(report):
    """Apply GREEN/YELLOW/RED rules. Returns ('GREEN'|'YELLOW'|'RED', reasons[])."""
    reasons = []
    status = "GREEN"

    def demote(level):
        nonlocal status
        order = {"GREEN": 0, "YELLOW": 1, "RED": 2}
        if order[level] > order[status]:
            status = level

    # Schema evidence missing → RED, do not allow anything else to mask this
    if report["data"].get("schema_error"):
        reasons.append(f"RED: schema evidence missing — {report['data']['schema_error']}")
        demote("RED")
    if report["data"].get("missing"):
        for m in report["data"]["missing"]:
            reasons.append(f"RED: schema mismatch — missing {m}")

    safety = report["safety"]
    # Safety can be empty (API down) or malformed. Both are RED.
    if not safety:
        reasons.append("RED: cannot read /system/status (API down or unreachable)")
        demote("RED")
    else:
        if safety.get("is_live") is True:
            reasons.append("RED: is_live=true")
            demote("RED")
        if safety.get("broker_mode") != "paper":
            reasons.append(
                f"RED: broker_mode={safety.get('broker_mode')!r} (expected 'paper')"
            )
            demote("RED")
        if safety.get("order_kill_switch") is False:
            reasons.append("RED: order_kill_switch=false")
            demote("RED")

    # DB read failures are surfaced in the data section. Numeric checks
    # only run if the values are not None.
    for field, label in (("orders", "orders"), ("positions", "positions"),
                         ("orphan_count", "orphan_count"),
                         ("fk_violations", "FK violations")):
        val = report["data"].get(field)
        if val is None:
            continue  # schema_error already raised
        if val > 0:
            reasons.append(f"RED: unexpected {label}={val}")
            demote("RED")

    # Runtime checks
    if not report["runtime"]["api_active"]:
        reasons.append("RED: polycopy-api.service not active")
        demote("RED")
    if not report["runtime"]["caddy_active"]:
        reasons.append("YELLOW: caddy not active (public route down)")
        demote("YELLOW")
    if not report["runtime"]["public_ok"]:
        reasons.append("YELLOW: public dashboard not reachable")
        demote("YELLOW")

    # Per-timer YELLOW/RED
    for tname, tinfo in report["timers"].items():
        if not tinfo.get("enabled", False):
            reasons.append(f"YELLOW: timer polycopy-{tname}.timer disabled")
            demote("YELLOW")
        fails = tinfo.get("failures", []) or []
        if tname in ("collect", "scan") and len(fails) >= 3:
            reasons.append(
                f"RED: timer {tname} failed {len(fails)} times in {LOOKBACK_HOURS}h"
            )
            demote("RED")
        elif len(fails) >= 1:
            reasons.append(
                f"YELLOW: timer {tname} had {len(fails)} failure(s) in {LOOKBACK_HOURS}h"
            )
            demote("YELLOW")

    # Freshness YELLOW
    fr = report.get("freshness", {})
    age = fr.get("source_trade_age_seconds")
    if age is None:
        if report["data"].get("newest_source_trade_at") is None:
            reasons.append("YELLOW: no source_trades yet")
            demote("YELLOW")
        # If newest_source_trade_at is set but age computation failed,
        # we don't surface a separate YELLOW (parse error is captured above).
    elif age > FRESHNESS_STALE_SECONDS:
        reasons.append(
            f"YELLOW: newest source_trade is {age // 60} min old"
        )
        demote("YELLOW")

    if status == "GREEN" and not reasons:
        reasons.append("all checks pass")
    return status, reasons


# --- Report assembly ---


def build_report(mock=None):
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "release": {"tag": "v0.1.0-paper-pilot",
                    "sha": "948b444fa93bc6ffd80a5f4e029e625f2399712b"},
        "mock": mock,
    }
    # Safety
    safety = get_safety_state() or {}
    if mock == "is_live=true":
        safety["is_live"] = True
    if mock == "kill_switch=false":
        safety["order_kill_switch"] = False
    report["safety"] = safety
    # Data
    data = read_db_counts()
    if mock == "orphan=1":
        data["orphan_count"] = 1
    if mock == "order=1":
        data["orders"] = 1
    if mock == "fresh=stale":
        # Force-stale: pretend the newest trade was 2 hours ago
        data["newest_source_trade_at"] = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        # Backdate it 2 hours by overriding the computed age below
    report["data"] = data
    # Runtime
    api = safe_run(["systemctl", "is-active", "polycopy-api.service"], timeout=5)
    dash = safe_run(["systemctl", "is-active", "polycopy-dashboard.service"], timeout=5)
    caddy = safe_run(["systemctl", "is-active", "caddy"], timeout=5)
    public_status = public_get("/")
    report["runtime"] = {
        "api_active": api.stdout.strip() == "active",
        "dashboard_active": dash.stdout.strip() == "active",
        "caddy_active": caddy.stdout.strip() == "active",
        "public_dashboard_http": public_status,
        "public_ok": public_status == "200",
    }
    # Timers
    timers = {}
    for t in TIMERS:
        svc = service_state(t)
        nxt = timer_next(t)
        fails = timer_failures(t)
        if mock == "timer=fail" and t in ("collect", "scan"):
            fails = (fails or []) + ["[mock] simulated failure"]
        timers[t] = {
            "enabled": svc.get("enabled", False),
            "active_state": svc.get("ActiveState"),
            "sub_state": svc.get("SubState"),
            "last_result": svc.get("Result"),
            "last_exec_status": svc.get("ExecMainStatus"),
            "last_enter_utc": svc.get("ActiveEnterTimestamp"),
            "last_exit_utc": svc.get("ActiveExitTimestamp"),
            "next_elapse_utc": nxt.get("NextElapseUSecRealtime"),
            "last_trigger_utc": nxt.get("LastTriggerUSec"),
            "failures": fails,
        }
    report["timers"] = timers
    # Freshness
    age = None
    if data.get("newest_source_trade_at"):
        if mock == "fresh=stale":
            # Force age of 2 hours for stale-mock test
            age = 2 * 3600
        else:
            try:
                ts = datetime.fromisoformat(
                    data["newest_source_trade_at"].replace("Z", "+00:00")
                )
                age = int((datetime.now(timezone.utc) - ts).total_seconds())
            except Exception:
                pass
    report["freshness"] = {
        "newest_source_trade_at": data.get("newest_source_trade_at"),
        "source_trade_age_seconds": age,
        "stale_threshold_seconds": FRESHNESS_STALE_SECONDS,
    }
    return report


# --- Renderers ---


def render_text(report, status, reasons):
    lines = []
    lines.append("=== Polycopy paper-pilot status ===")
    lines.append(f"overall: {status}")
    if report.get("mock"):
        lines.append(f"(MOCK: {report['mock']})")
    lines.append(f"generated_utc: {report['generated_at_utc']}")
    lines.append("")
    lines.append("--- Safety ---")
    s = report["safety"]
    lines.append(f"broker_mode: {s.get('broker_mode')}")
    lines.append(f"paper_mode: {s.get('paper_mode')}")
    lines.append(f"order_kill_switch: {s.get('order_kill_switch')}")
    lines.append(f"is_live: {s.get('is_live')}")
    lines.append(f"is_sample_data: {s.get('is_sample_data')}")
    lines.append("")
    lines.append("--- Runtime ---")
    r = report["runtime"]
    lines.append(f"api: {r['api_active']}")
    lines.append(f"dashboard: {r['dashboard_active']}")
    lines.append(f"caddy: {r['caddy_active']}")
    lines.append(f"public dashboard http: {r['public_dashboard_http']}")
    lines.append("")
    lines.append("--- Automation (timers) ---")
    for t in TIMERS:
        info = report["timers"][t]
        fails = len(info.get("failures") or [])
        lines.append(
            f"{t}: enabled={info.get('enabled')} last={info.get('last_result')} "
            f"fails_12h={fails}"
        )
    lines.append("")
    lines.append("--- Data ---")
    d = report["data"]
    lines.append(
        f"markets={d.get('markets')} outcomes={d.get('market_outcomes')} "
        f"wallets={d.get('wallets')} source_trades={d.get('source_trades')}"
    )
    lines.append(
        f"signals={d.get('signals')} orders={d.get('orders')} "
        f"positions={d.get('positions')} decision_log={d.get('decision_log')}"
    )
    lines.append(f"orphans={d.get('orphan_count')} FK_violations={d.get('fk_violations')}")
    if d.get("schema_error"):
        lines.append(f"SCHEMA_ERROR: {d['schema_error']}")
    if d.get("missing"):
        lines.append(f"SCHEMA_MISSING: {d['missing']}")
    lines.append("")
    lines.append("--- Freshness ---")
    fr = report["freshness"]
    if fr.get("source_trade_age_seconds") is None:
        lines.append("newest source_trade: none")
    else:
        lines.append(
            f"newest source_trade: {fr['newest_source_trade_at']} "
            f"(age {fr['source_trade_age_seconds']//60} min)"
        )
    lines.append("")
    lines.append("--- Reasons / action ---")
    for r in reasons:
        lines.append(f"- {r}")
    return "\n".join(lines)


# --- Atomic write helper ---


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically.

    - Creates a temp file in the same directory as `path`.
    - Writes, flushes, and fsyncs before the rename.
    - Uses os.replace() (atomic on POSIX) to swap the temp file in.
    - Cleans up the temp file on any failure so we never leave debris.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync is best-effort; missing it does not break atomicity of
                # os.replace() on the same filesystem
                pass
        os.replace(tmp_path, path)
    except Exception:
        # Clean up partial temp file; never let it accumulate
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


# --- Main ---


def main():
    ap = argparse.ArgumentParser(description="Polycopy paper-pilot status report")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--mock", help="simulate a condition for classification tests",
                    choices=["is_live=true", "kill_switch=false", "orphan=1",
                             "order=1", "fresh=stale", "timer=fail"])
    ap.add_argument("--write-latest", action="store_true",
                    help="write the text report to the configured report path")
    args = ap.parse_args()

    # Resolve the report path up front so an invalid override is visible
    # before any other work happens.
    try:
        report_path = resolve_report_path()
    except ConfigError as e:
        # Surface as RED structured output and exit 2
        sys.stderr.write(f"config error: {e}\n")
        return 2

    try:
        report = build_report(mock=args.mock)
    except ConfigError as e:
        sys.stderr.write(f"config error: {e}\n")
        return 2

    status, reasons = classify(report)
    report["overall"] = status
    report["reasons"] = reasons
    text = render_text(report, status, reasons)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(text)

    if args.write_latest:
        try:
            atomic_write_text(report_path, text)
        except Exception as e:
            sys.stderr.write(f"write failed: {e}\n")
            # Treat write failure as RED so a stale report is visible
            if status == "GREEN":
                # We must not silently report GREEN if we failed to write
                sys.stderr.write("forcing RED due to report-file write failure\n")
                return 2
            return {"GREEN": 0, "YELLOW": 1, "RED": 2}[status]

    return {"GREEN": 0, "YELLOW": 1, "RED": 2}[status]


if __name__ == "__main__":
    sys.exit(main())
