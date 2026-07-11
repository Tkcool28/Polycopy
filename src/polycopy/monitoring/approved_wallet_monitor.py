# ruff: noqa: E701, E702, E703
"""Deterministic, local-only safety monitor for the approved-wallet collector.

All production DB access uses SQLite URI ``mode=ro`` plus ``query_only``.
Normal execution writes only its explicitly configured status/state/event files.
The sole remediation command is intentionally isolated in ``SystemProbe``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

APPROVED_WALLET = "0xcac76b761231464900cce5da7c20233d59b20579"
EXPECTED_MARKER_SHA256 = "4db6f658108c7978b9ed53d2591e0ecd22e3c005ce970d875fcc3e59a9b60274"
COLLECTOR_TIMER = "polycopy-approved-wallet-collect.timer"
COLLECTOR_SERVICE = "polycopy-approved-wallet-collect.service"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def bytes_dir(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


@dataclass
class MonitorConfig:
    db_path: Path = Path("/root/Polycopy/data/polycopy.db")
    data_dir: Path = Path("/root/Polycopy/data")
    marker_path: Path = Path("/root/Polycopy/data/.pr24z_canonical_migration_complete")
    baseline_path: Path = Path("/root/Polycopy/data/approved_wallet_monitor_baseline.json")
    state_path: Path = Path("/root/Polycopy/data/approved_wallet_monitor_state.json")
    status_json_path: Path = Path("/root/Polycopy/data/approved_wallet_monitor_latest.json")
    status_text_path: Path = Path("/root/Polycopy/data/approved_wallet_monitor_latest.txt")
    events_path: Path = Path("/root/Polycopy/data/approved_wallet_monitor_events.jsonl")
    backups_path: Path = Path("/root/Polycopy/backups")
    reports_path: Path = Path("/root/Polycopy/reports")
    now: Callable[[], datetime] = utcnow
    downstream_tables: tuple[str, ...] = (
        "trade_copyability_decisions", "copy_candidates", "paper_signal_decisions",
        "candidate_price_snapshots", "candidate_price_snapshot_levels", "orders", "positions",
        "settlement_accounting_ledger", "decision_log", "wallet_score_decisions", "decision_verdicts",
    )
    legacy_timers: tuple[str, ...] = (
        "polycopy-collect.timer", "polycopy-scan.timer", "polycopy-settle.timer", "polycopy-update.timer",
    )


class Probe:
    """Narrow, injectable system boundary. Implementations must never write DB data."""
    def collector_timer(self) -> dict[str, Any]: raise NotImplementedError
    def collector_service(self) -> dict[str, Any]: raise NotImplementedError
    def database(self, config: MonitorConfig) -> dict[str, Any]: raise NotImplementedError
    def api_health_and_safety(self) -> dict[str, Any]: raise NotImplementedError
    def storage(self, config: MonitorConfig) -> dict[str, Any]: raise NotImplementedError
    def marker(self, config: MonitorConfig) -> dict[str, Any]: raise NotImplementedError
    def legacy_timers(self) -> dict[str, dict[str, bool]]: raise NotImplementedError
    def memory(self) -> dict[str, Any]: raise NotImplementedError
    def disable_collector_timer(self) -> tuple[bool, str]: raise NotImplementedError


def count_collector_oom_events(journal_text: str, *, context_lines: int = 8) -> int:
    """Count bounded, collector-attributed kernel OOM events exactly once.

    Kernel OOM diagnostics are multi-line.  A collector service cgroup can
    appear shortly before a generic ``python3`` kill line, so attribution is
    made over a small preceding window rather than from generic Python alone.
    Consecutive OOM marker lines in the same block are deduplicated.
    """
    lines = journal_text.lower().splitlines()
    collector_markers = ("polycopy-approved-wallet-collect.service", "collect_approved_wallet_trades.py")
    count = 0
    last_counted_marker = -context_lines - 1
    for index, line in enumerate(lines):
        if "out of memory" not in line:
            continue
        window = "\n".join(lines[max(0, index - context_lines) : min(len(lines), index + 3)])
        if not any(marker in window for marker in collector_markers):
            continue
        if index - last_counted_marker <= context_lines:
            continue
        count += 1
        last_counted_marker = index
    return count


class SystemProbe(Probe):
    def run(self, args: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)

    def show(self, unit: str, *properties: str) -> dict[str, str]:
        result = self.run(["systemctl", "show", unit, *[part for item in properties for part in ("-p", item)]], 8)
        values = {"_error": result.stderr.strip()[:300]} if result.returncode else {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1); values[key] = value
        return values

    @staticmethod
    def true(value: str | None) -> bool:
        return value in {"enabled", "enabled-runtime", "active", "yes", "true"}

    def unit(self, name: str) -> dict[str, bool]:
        data = self.show(name, "ActiveState", "UnitFileState")
        return {"enabled": self.true(data.get("UnitFileState")), "active": data.get("ActiveState") == "active"}

    def collector_timer(self) -> dict[str, Any]: return self.unit(COLLECTOR_TIMER)

    def collector_service(self) -> dict[str, Any]:
        data = self.show(COLLECTOR_SERVICE, "ActiveState", "Result", "ExecMainStatus", "NRestarts", "ExecMainExitTimestamp")
        journal = self.run(["journalctl", "-u", COLLECTOR_SERVICE, "-n", "120", "--no-pager", "-o", "short-iso"], 10)
        successes: list[datetime] = []; failure = 0; latest: dict[str, Any] | None = None
        for line in journal.stdout.splitlines():
            if "{" in line:
                try:
                    latest = json.loads(line[line.index("{"):])
                except json.JSONDecodeError:
                    latest = {"malformed": True}
            if "Finished polycopy-approved-wallet-collect.service" in line:
                stamp = parse_time(line[:25].strip());
                if stamp: successes.append(stamp)
                failure = 0
            elif "Failed with result" in line:
                # systemd commonly emits both "Main process exited" and
                # "Failed with result" for one activation.  Only the terminal
                # unit-failure record counts toward consecutive invocations.
                failure += 1
        last = max(successes) if successes else None
        try: exit_status = int(data.get("ExecMainStatus", ""))
        except ValueError: exit_status = None
        try: restarts = int(data.get("NRestarts", "0"))
        except ValueError: restarts = None
        return {"active": data.get("ActiveState") == "active", "result": data.get("Result") or None,
                "exit_status": exit_status, "restart_count": restarts, "last_success_utc": iso(last) if last else None,
                "last_completed_utc": data.get("ExecMainExitTimestamp") or None,
                "consecutive_failures": failure, "collector_result": latest}

    def database(self, config: MonitorConfig) -> dict[str, Any]:
        out: dict[str, Any] = {"integrity": None, "foreign_key_violations": None,
             "canonical_duplicate_groups": None, "approved_wallet_buy_rows": None,
             "approved_wallet_sell_rows": None, "unapproved_identities": [], "table_counts": {}, "error": None}
        try:
            conn = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only=ON")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            out["integrity"] = integrity[0] if integrity else None
            out["foreign_key_violations"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            out["canonical_duplicate_groups"] = conn.execute("SELECT COUNT(*) FROM (SELECT source, source_trade_id FROM source_trades GROUP BY source, source_trade_id HAVING COUNT(*) > 1)").fetchone()[0]
            out["approved_wallet_buy_rows"] = conn.execute("SELECT COUNT(*) FROM source_trades WHERE lower(trader_address)=? AND upper(side)='BUY'", (APPROVED_WALLET,)).fetchone()[0]
            out["approved_wallet_sell_rows"] = conn.execute("SELECT COUNT(*) FROM source_trades WHERE lower(trader_address)=? AND upper(side)='SELL'", (APPROVED_WALLET,)).fetchone()[0]
            out["unapproved_identities"] = [list(row) for row in conn.execute("SELECT source, source_trade_id FROM source_trades WHERE lower(COALESCE(trader_address,'')) != ? ORDER BY source, source_trade_id", (APPROVED_WALLET,))]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in config.downstream_tables:
                out["table_counts"][table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else None
        except (sqlite3.Error, OSError) as exc: out["error"] = f"database_read_failed:{type(exc).__name__}"
        finally:
            try: conn.close()
            except UnboundLocalError: pass
        return out

    def api_health_and_safety(self) -> dict[str, Any]:
        def get(url: str) -> tuple[int | None, Any]:
            try:
                with urllib.request.urlopen(url, timeout=3) as r:
                    return r.status, json.loads(r.read(65536).decode("utf-8"))
            except (urllib.error.URLError, OSError, json.JSONDecodeError): return None, None
        code, health = get("http://127.0.0.1:8765/health")
        status_code, safety = get("http://127.0.0.1:8765/system/status")
        valid = isinstance(health, dict) and health.get("status") == "ok"
        safety_out = {key: safety.get(key) if isinstance(safety, dict) else None for key in ("broker_mode", "paper_mode", "order_kill_switch", "is_live")}
        return {"healthy": code == 200 and valid, "http_status": code, "safety_http_status": status_code, "safety": safety_out}

    def storage(self, config: MonitorConfig) -> dict[str, Any]:
        usage = shutil.disk_usage(config.data_dir)
        database = config.db_path.stat().st_size if config.db_path.exists() else 0
        wal = Path(str(config.db_path) + "-wal").stat().st_size if Path(str(config.db_path) + "-wal").exists() else 0
        shm = Path(str(config.db_path) + "-shm").stat().st_size if Path(str(config.db_path) + "-shm").exists() else 0
        journal = self.run(["journalctl", "--disk-usage", "--no-pager"], 8)
        import re
        match = re.search(r"([0-9.]+)([KMGTP])", journal.stdout)
        scale = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
        journal_bytes = int(float(match.group(1)) * scale[match.group(2)]) if match else None
        return {"available_bytes": usage.free, "free_percent": usage.free * 100 / usage.total,
                "database_bytes": database, "wal_bytes": wal, "shm_bytes": shm, "data_directory_bytes": bytes_dir(config.data_dir),
                "backup_directory_bytes": bytes_dir(config.backups_path), "report_directory_bytes": bytes_dir(config.reports_path), "journal_bytes": journal_bytes}

    def marker(self, config: MonitorConfig) -> dict[str, Any]:
        if not config.marker_path.is_file(): return {"exists": False, "sha256": None, "valid": False}
        digest = hashlib.sha256(config.marker_path.read_bytes()).hexdigest()
        valid = False
        try:
            from polycopy.migrations.pr24z_marker import validate_pr24z_migration_marker
            valid = validate_pr24z_migration_marker(config.marker_path, config.db_path).valid
        except (ImportError, OSError, ValueError): pass
        return {"exists": True, "sha256": digest, "valid": valid}

    def legacy_timers(self) -> dict[str, dict[str, bool]]: return {name: self.unit(name) for name in MonitorConfig.legacy_timers}

    def memory(self) -> dict[str, Any]:
        service = self.show(COLLECTOR_SERVICE, "MemoryCurrent", "MemoryPeak", "TasksCurrent")
        def integer(name: str) -> int | None:
            try: return int(service[name]) if service.get(name) not in (None, "[not set]", "") else None
            except ValueError: return None
        processes = []
        ps = self.run(["ps", "-eo", "pid=,etimes=,rss=,args="], 8)
        for line in ps.stdout.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) == 4 and "collect_approved_wallet_trades.py" in parts[3]:
                processes.append({"pid": int(parts[0]), "elapsed_seconds": int(parts[1]), "rss_bytes": int(parts[2]) * 1024})
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1); mem[key] = int(value.split()[0]) * 1024
        available = mem.get("MemAvailable", 0); total = mem.get("MemTotal", 0)
        oom = self.run(["journalctl", "-k", "-n", "500", "--no-pager", "-o", "cat"], 8)
        oom_count = count_collector_oom_events(oom.stdout)
        return {"collector_process_count": len(processes), "collector_total_rss_bytes": sum(p["rss_bytes"] for p in processes),
          "collector_max_process_rss_bytes": max((p["rss_bytes"] for p in processes), default=0), "collector_runtime_seconds": max((p["elapsed_seconds"] for p in processes), default=None),
          "service_memory_current_bytes": integer("MemoryCurrent"), "service_memory_peak_bytes": integer("MemoryPeak"), "service_tasks_current": integer("TasksCurrent"),
          "system_mem_available_bytes": available, "system_mem_available_percent": available * 100 / total if total else 0,
          "swap_total_bytes": mem.get("SwapTotal", 0), "swap_used_bytes": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), "oom_events_detected": oom_count, "processes": processes}

    def disable_collector_timer(self) -> tuple[bool, str]:
        result = self.run(["systemctl", "disable", "--now", COLLECTOR_TIMER], 20)
        return result.returncode == 0, ("disabled approved-wallet collector timer" if result.returncode == 0 else result.stderr.strip()[:300])


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError): return default


def build_baseline(probe: Probe, config: MonitorConfig) -> dict[str, Any]:
    db = probe.database(config)
    if db.get("integrity") != "ok" or db.get("foreign_key_violations") != 0 or db.get("error"):
        raise ValueError("refusing baseline: database integrity/FK validation failed")
    marker = probe.marker(config)
    return {"schema_version": 1, "created_at_utc": iso(config.now()), "approved_wallet": APPROVED_WALLET,
      "marker_sha256": marker.get("sha256"), "unapproved_source_trade_identities": db["unapproved_identities"],
      "downstream_table_counts": db["table_counts"], "expected_disabled_timers": list(config.legacy_timers)}


def valid_baseline(baseline: dict[str, Any]) -> bool:
    """A baseline is an administrative trust boundary, never an empty default."""
    return (
        baseline.get("schema_version") == 1
        and baseline.get("approved_wallet") == APPROVED_WALLET
        and baseline.get("marker_sha256") == EXPECTED_MARKER_SHA256
        and isinstance(baseline.get("unapproved_source_trade_identities"), list)
        and isinstance(baseline.get("downstream_table_counts"), dict)
        and baseline.get("expected_disabled_timers") == list(MonitorConfig.legacy_timers)
    )


def classify_reason(levels: list[tuple[str, str]], level: str, reason: str) -> None: levels.append((level, reason))


def evaluate(probe: Probe, config: MonitorConfig, *, no_remediation: bool) -> dict[str, Any]:
    now = config.now(); state = load_json(config.state_path, {})
    timer, service, db = probe.collector_timer(), probe.collector_service(), probe.database(config)
    api, storage, marker, legacy, memory = probe.api_health_and_safety(), probe.storage(config), probe.marker(config), probe.legacy_timers(), probe.memory()
    baseline = load_json(config.baseline_path, {})
    baseline_is_valid = valid_baseline(baseline)
    levels: list[tuple[str, str]] = []; eligible = False
    if not timer.get("enabled") or not timer.get("active"): classify_reason(levels, "RED", "approved_wallet_timer_disabled_or_inactive")
    last_success = parse_time(service.get("last_success_utc")); age = (now-last_success).total_seconds()/60 if last_success else None
    if age is None or age > 50: classify_reason(levels, "RED", "collector_success_missing_or_older_than_50_minutes")
    elif age > 35: classify_reason(levels, "YELLOW", "collector_success_older_than_35_minutes")
    result = service.get("collector_result")
    if not isinstance(result, dict): classify_reason(levels, "RED", "collector_result_malformed"); eligible = True
    else:
        for key in ("errors", "fallback_identities", "ambiguous_identities", "legacy_aliases_used"):
            if result.get(key) not in (0, None): classify_reason(levels, "RED", f"collector_{key}_nonzero"); eligible = True
    if service.get("exit_status") not in (0, None) or service.get("result") in {"failed", "timeout"}: classify_reason(levels, "RED", "collector_service_failure"); eligible = True
    if (service.get("consecutive_failures") or 0) >= 2: classify_reason(levels, "RED", "two_consecutive_collector_failures"); eligible = True
    if (service.get("restart_count") or 0) > 0: classify_reason(levels, "RED", "collector_restart_detected"); eligible = True
    if db.get("integrity") != "ok": classify_reason(levels, "RED", "database_integrity_check_failed"); eligible = True
    if db.get("foreign_key_violations") != 0: classify_reason(levels, "RED", "foreign_key_violations"); eligible = True
    if (db.get("canonical_duplicate_groups") or 0) > 0: classify_reason(levels, "RED", "canonical_duplicate_groups"); eligible = True
    if (db.get("approved_wallet_sell_rows") or 0) > 0: classify_reason(levels, "RED", "approved_wallet_sell_rows"); eligible = True
    new_unapproved: list[tuple[str, str]] = []
    if not baseline_is_valid:
        # A missing/corrupt administrative baseline is RED for observation, but
        # it cannot itself authorize shutdown of an otherwise healthy collector.
        classify_reason(levels, "RED", "approved_wallet_monitor_baseline_missing_or_invalid")
    else:
        expected_ids = {tuple(value) for value in baseline["unapproved_source_trade_identities"]}
        current_ids = {tuple(value) for value in db.get("unapproved_identities", [])}
        new_unapproved = sorted(current_ids - expected_ids)
        if new_unapproved: classify_reason(levels, "RED", "new_non_approved_wallet_source_trade"); eligible = True
        for table, count in db.get("table_counts", {}).items():
            before = baseline["downstream_table_counts"].get(table)
            if before is not None and count is not None and count > before: classify_reason(levels, "RED", f"unexpected_downstream_write:{table}"); eligible = True
    if not marker.get("exists") or marker.get("sha256") != EXPECTED_MARKER_SHA256 or not marker.get("valid"):
        classify_reason(levels, "RED", "canonical_migration_marker_invalid"); eligible = True
    if storage.get("available_bytes", 0) < 10*1024**3 or storage.get("free_percent", 0) < 20:
        classify_reason(levels, "RED", "disk_below_hard_minimum"); eligible = True
    elif storage.get("available_bytes", 0) < 15*1024**3 or storage.get("free_percent", 0) < 25: classify_reason(levels, "YELLOW", "disk_below_warning_threshold")
    previous_storage = state.get("storage", {})
    for key in ("database_bytes", "data_directory_bytes", "backup_directory_bytes", "report_directory_bytes"):
        if storage.get(key, 0) - previous_storage.get(key, storage.get(key, 0)) > 250*1024**2:
            classify_reason(levels, "RED", f"unexpected_storage_growth:{key}"); eligible = True
    if storage.get("wal_bytes", 0) > previous_storage.get("wal_bytes", storage.get("wal_bytes", 0)) and service.get("active") is False and storage.get("wal_bytes", 0) > 0:
        if state.get("growing_wal_once"): classify_reason(levels, "RED", "persistent_growing_wal"); eligible = True
    for name, data in legacy.items():
        if data.get("enabled") or data.get("active"): classify_reason(levels, "RED", f"legacy_or_downstream_timer_active:{name}")
    failures = 0 if api.get("healthy") else int(state.get("api_consecutive_failures", 0)) + 1
    if not api.get("healthy"):
        classify_reason(levels, "RED" if failures >= 2 else "YELLOW", "api_health_unavailable")
    safety = api.get("safety", {})
    if safety.get("broker_mode") != "paper" or safety.get("paper_mode") not in {"paper_manual", "research_only"} or safety.get("order_kill_switch") is not True or safety.get("is_live") is not False:
        classify_reason(levels, "RED", "unsafe_live_trading_configuration"); eligible = True
    if memory.get("collector_process_count", 0) > 1 or memory.get("collector_total_rss_bytes", 0) > 512*1024**2 or memory.get("collector_max_process_rss_bytes", 0) > 512*1024**2 or (memory.get("collector_runtime_seconds") or 0) > 300 or memory.get("oom_events_detected", 0) > 0 or memory.get("system_mem_available_percent", 0) < 10:
        classify_reason(levels, "RED", "memory_or_process_safety_violation"); eligible = True
    elif memory.get("collector_max_process_rss_bytes", 0) >= 384*1024**2 or (memory.get("collector_runtime_seconds") or 0) > 60 or memory.get("system_mem_available_percent", 100) < 20: classify_reason(levels, "YELLOW", "memory_warning")
    rank = {"GREEN": 0, "YELLOW": 1, "RED": 2}; status = max((level for level, _ in levels), key=lambda x: rank[x], default="GREEN")
    reasons = [reason for level, reason in levels if level == "RED"]; warnings = [reason for level, reason in levels if level == "YELLOW"]
    fingerprint = hashlib.sha256("\0".join(sorted(reasons or warnings)).encode()).hexdigest()[:16]
    previous_status = state.get("status"); changed = previous_status is not None and (previous_status != status or state.get("fingerprint") != fingerprint)
    action: dict[str, Any] = {"attempted": False, "action": None, "succeeded": None, "details": None}
    if status == "RED" and eligible:
        action["action"] = f"systemctl disable --now {COLLECTOR_TIMER}"
        if not timer.get("enabled") and not timer.get("active"): action["details"] = "timer already disabled/inactive"
        elif no_remediation: action["details"] = "no-remediation mode"
        else:
            action["attempted"] = True; action["succeeded"], action["details"] = probe.disable_collector_timer()
    report = {"schema_version": 1, "status": status, "checked_at_utc": iso(now), "hostname": os.uname().nodename, "monitor_version": "1.0.0", "approved_wallet": APPROVED_WALLET,
      "collector_timer": timer, "collector_service": {**service, "last_success_age_minutes": round(age, 1) if age is not None else None}, "collector_result": result,
      "database": {"integrity": db.get("integrity"), "foreign_key_violations": db.get("foreign_key_violations"), "canonical_duplicate_groups": db.get("canonical_duplicate_groups"), "approved_wallet_buy_rows": db.get("approved_wallet_buy_rows"), "approved_wallet_sell_rows": db.get("approved_wallet_sell_rows"), "new_unapproved_wallet_rows": len(new_unapproved)},
      "downstream": {"unexpected_changes": [x for x in reasons if x.startswith("unexpected_downstream_write:")]}, "api": {"healthy": api.get("healthy"), "http_status": api.get("http_status"), "consecutive_failures": failures}, "storage": storage, "marker": marker, "safety": safety, "memory": {key: value for key, value in memory.items() if key != "processes"}, "reasons": reasons, "warnings": warnings, "automatic_action": action,
      "event_id": fingerprint, "status_changed": changed, "previous_status": previous_status, "first_seen_utc": state.get("first_seen_utc") if not changed else iso(now), "last_changed_utc": state.get("last_changed_utc") if not changed else iso(now)}
    new_state = {"status": status, "fingerprint": fingerprint, "first_seen_utc": report["first_seen_utc"], "last_changed_utc": report["last_changed_utc"], "api_consecutive_failures": failures, "storage": storage, "growing_wal_once": storage.get("wal_bytes", 0) > previous_storage.get("wal_bytes", storage.get("wal_bytes", 0))}
    write_json_atomic(config.state_path, new_state)
    if changed or action["attempted"]:
        config.events_path.parent.mkdir(parents=True, exist_ok=True)
        if config.events_path.exists() and config.events_path.stat().st_size > 1_000_000:
            # Preserve one bounded prior log; monitor-only artifact, never production data.
            os.replace(config.events_path, config.events_path.with_suffix(".jsonl.1"))
        with config.events_path.open("a", encoding="utf-8") as stream: stream.write(json.dumps({"checked_at_utc": report["checked_at_utc"], "status": status, "event_id": fingerprint, "reasons": reasons, "automatic_action": action}) + "\n")
    return report


def render_text(report: dict[str, Any]) -> str:
    timer = report["collector_timer"]; db = report["database"]; storage = report["storage"]; action = report["automatic_action"]
    gib = storage.get("available_bytes", 0) / 1024**3
    lines = [f"POLYCOPY APPROVED-WALLET MONITOR: {report['status']}", f"Checked: {report['checked_at_utc']}", f"Collector timer: {'enabled' if timer.get('enabled') else 'disabled'} / {'active' if timer.get('active') else 'inactive'}", f"Last successful collection: {report['collector_service'].get('last_success_age_minutes')} minutes ago", f"Database integrity: {db['integrity']}", f"Foreign-key violations: {db['foreign_key_violations']}", f"Approved-wallet SELL rows: {db['approved_wallet_sell_rows']}", f"Canonical duplicate groups: {db['canonical_duplicate_groups']}", f"API: {'healthy' if report['api'].get('healthy') else 'unhealthy'}", f"Disk: {storage.get('free_percent', 0):.1f}% free, {gib:.1f} GiB available", f"Automatic action: {action.get('details') or 'none'}"]
    lines.extend(f"Reason: {item}" for item in report["reasons"] + report["warnings"])
    return "\n".join(lines) + "\n"
