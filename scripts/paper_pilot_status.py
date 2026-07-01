#!/usr/bin/env python3
"""paper_pilot_status.py — Polycopy paper-pilot status report.

Read-only. Does not mutate the production DB.
Exits 0=GREEN, 1=YELLOW, 2=RED.

Supports --json for machine output and --mock <condition> for testing
classification logic without modifying live state.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/root/Polycopy")
DB = "/root/Polycopy/data/polycopy.db"
PUBLIC_HOST = "polymoney.duckdns.org"
PUBLIC_IP = "209.54.105.179"
LOCAL_API = "http://127.0.0.1:8765"
TIMERS = ["collect", "scan", "health", "settle", "update"]
SERVICES_LONG = ["polycopy-api", "polycopy-dashboard", "caddy"]
LATEST_REPORT_PATH = REPO / "data" / "pilot_status_latest.txt"
FRESHNESS_STALE_SECONDS = 30 * 60  # 30 min
LOOKBACK_HOURS = 12


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
    # Use --resolve trick so curl bypasses local DNS path issue
    cmd = ["curl", "-4", "-sS", "--resolve", f"{PUBLIC_HOST}:443:{PUBLIC_IP}",
           "-o", "/dev/null", "-w", "%{http_code}", f"https://{PUBLIC_HOST}{path}"]
    r = safe_run(cmd, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else None


def read_db_counts():
    out = {}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        out["schema_version"] = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
        for t in ("markets", "market_outcomes", "wallets", "source_trades",
                  "signals", "orders", "positions", "decision_log"):
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        out["orphan_count"] = conn.execute(
            "SELECT COUNT(*) FROM market_outcomes mo LEFT JOIN markets m ON m.id=mo.market_id WHERE m.id IS NULL"
        ).fetchone()[0]
        out["fk_violations"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        # Freshness: newest source_trade timestamp (column is `timestamp` in current schema)
        row = conn.execute("SELECT MAX(timestamp) FROM source_trades").fetchone()
        out["newest_source_trade_at"] = row[0] if row and row[0] else None
    finally:
        conn.close()
    return out


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
        # systemd status lines that include "result=failed"
        if "result=failed" in line.lower() or "failed with result" in line.lower():
            failed.append(line.strip()[:160])
    return failed


def get_safety_state():
    # local /system/status
    code, body = http_get(f"{LOCAL_API}/system/status")
    if code != 200 or not body:
        return None
    try:
        d = json.loads(body)
        return {
            "broker_mode": d.get("broker_mode"),
            "paper_mode": d.get("paper_mode"),
            "order_kill_switch": d.get("order_kill_switch"),
            "is_live": d.get("is_live"),
            "is_sample_data": d.get("is_sample_data"),
        }
    except Exception:
        return None


def classify(report):
    """Apply GREEN/YELLOW/RED rules. Returns ('GREEN'|'YELLOW'|'RED', reasons[])."""
    reasons = []
    status = "GREEN"

    def demote(level):
        nonlocal status
        order = {"GREEN": 0, "YELLOW": 1, "RED": 2}
        if order[level] > order[status]:
            status = level

    safety = report["safety"]
    # RED triggers
    if safety.get("is_live") is True:
        reasons.append("RED: is_live=true")
        demote("RED")
    if safety.get("broker_mode") != "paper":
        reasons.append(f"RED: broker_mode={safety.get('broker_mode')} (expected paper)")
        demote("RED")
    if safety.get("order_kill_switch") is False:
        reasons.append("RED: order_kill_switch=false")
        demote("RED")
    if report["data"]["orders"] > 0:
        reasons.append(f"RED: unexpected orders={report['data']['orders']}")
        demote("RED")
    if report["data"]["positions"] > 0:
        reasons.append(f"RED: unexpected positions={report['data']['positions']}")
        demote("RED")
    if report["data"]["orphan_count"] > 0:
        reasons.append(f"RED: orphan_count={report['data']['orphan_count']}")
        demote("RED")
    if report["data"]["fk_violations"] > 0:
        reasons.append(f"RED: FK violations={report['data']['fk_violations']}")
        demote("RED")
    # Live broker init: any of the active broker classes being a Live* class (other than DisabledLiveBroker)
    # The repo only ships PaperBroker + DisabledLiveBroker, but we still check the loaded class.
    # We can't introspect the running API process without psutil; skip explicit check here (no signal).
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
        # collect and scan are critical; if they fail repeatedly, RED
        if tname in ("collect", "scan") and len(fails) >= 3:
            reasons.append(f"RED: timer {tname} failed {len(fails)} times in {LOOKBACK_HOURS}h")
            demote("RED")
        elif len(fails) >= 1:
            reasons.append(f"YELLOW: timer {tname} had {len(fails)} failure(s) in {LOOKBACK_HOURS}h")
            demote("YELLOW")

    # Freshness YELLOW
    fr = report.get("freshness", {})
    if fr.get("source_trade_age_seconds") is not None and fr["source_trade_age_seconds"] > FRESHNESS_STALE_SECONDS:
        reasons.append(f"YELLOW: newest source_trade is {fr['source_trade_age_seconds']//60} min old")
        demote("YELLOW")
    if fr.get("source_trade_age_seconds") is None:
        reasons.append("YELLOW: no source_trades yet")
        demote("YELLOW")

    if status == "GREEN" and not reasons:
        reasons.append("all checks pass")
    return status, reasons


def build_report(mock=None):
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "release": {"tag": "v0.1.0-paper-pilot",
                    "sha": "948b444fa93bc6ffd80a5f4e029e625f2399712b"},
        "mock": mock,
    }
    # Safety
    safety = get_safety_state() or {}
    # Apply mocks
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
        # Apply timer=fail mock
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
        try:
            ts = datetime.fromisoformat(data["newest_source_trade_at"].replace("Z", "+00:00"))
            age = int((datetime.now(timezone.utc) - ts).total_seconds())
        except Exception:
            pass
    report["freshness"] = {"newest_source_trade_at": data.get("newest_source_trade_at"),
                           "source_trade_age_seconds": age,
                           "stale_threshold_seconds": FRESHNESS_STALE_SECONDS}
    return report


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
        lines.append(f"{t}: enabled={info.get('enabled')} last={info.get('last_result')} fails_12h={fails}")
    lines.append("")
    lines.append("--- Data ---")
    d = report["data"]
    lines.append(f"markets={d.get('markets')} outcomes={d.get('market_outcomes')} "
                 f"wallets={d.get('wallets')} source_trades={d.get('source_trades')}")
    lines.append(f"signals={d.get('signals')} orders={d.get('orders')} "
                 f"positions={d.get('positions')} decision_log={d.get('decision_log')}")
    lines.append(f"orphans={d.get('orphan_count')} FK_violations={d.get('fk_violations')}")
    lines.append("")
    lines.append("--- Freshness ---")
    fr = report["freshness"]
    if fr.get("source_trade_age_seconds") is None:
        lines.append("newest source_trade: none")
    else:
        lines.append(f"newest source_trade: {fr['newest_source_trade_at']} "
                     f"(age {fr['source_trade_age_seconds']//60} min)")
    lines.append("")
    lines.append("--- Reasons / action ---")
    for r in reasons:
        lines.append(f"- {r}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Polycopy paper-pilot status report")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--mock", help="simulate a condition for classification tests",
                    choices=["is_live=true", "kill_switch=false", "orphan=1",
                             "order=1", "fresh=stale", "timer=fail"])
    ap.add_argument("--write-latest", action="store_true",
                    help="write the text report to data/pilot_status_latest.txt")
    args = ap.parse_args()
    report = build_report(mock=args.mock)
    status, reasons = classify(report)
    report["overall"] = status
    report["reasons"] = reasons
    text = render_text(report, status, reasons)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(text)
    if args.write_latest:
        LATEST_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LATEST_REPORT_PATH.write_text(text)
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}[status]


if __name__ == "__main__":
    sys.exit(main())