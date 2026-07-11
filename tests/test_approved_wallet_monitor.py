# ruff: noqa: E702
"""Focused safety-contract tests for the approved-wallet monitor.

The monitor is intentionally built around a small injected Probe interface so
these tests never inspect production systemd, journals, or SQLite files.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from polycopy.monitoring.approved_wallet_monitor import (
    EXPECTED_MARKER_SHA256,
    MonitorConfig,
    Probe,
    SystemProbe,
    build_baseline,
    evaluate,
    write_json_atomic,
)

NOW = datetime(2026, 7, 11, 5, 0, tzinfo=timezone.utc)


class FakeProbe(Probe):
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.disable_calls: list[str] = []
        self.timer = {"enabled": True, "active": True}
        self.service = {
            "active": False, "result": "success", "exit_status": 0,
            "restart_count": 0, "last_success_utc": (NOW - timedelta(minutes=8)).isoformat(),
            "last_completed_utc": (NOW - timedelta(minutes=8)).isoformat(),
            "consecutive_failures": 0,
            "collector_result": {"inserted": 0, "deduplicated": 12, "errors": 0,
                                 "fallback_identities": 0, "ambiguous_identities": 0,
                                 "legacy_aliases_used": 0},
        }
        self.db = {"integrity": "ok", "foreign_key_violations": 0,
                   "canonical_duplicate_groups": 0, "approved_wallet_buy_rows": 21,
                   "approved_wallet_sell_rows": 0,
                   "unapproved_identities": [],
                   "table_counts": {name: 0 for name in MonitorConfig.downstream_tables}}
        self.api = {"healthy": True, "http_status": 200,
                    "safety": {"broker_mode": "paper", "paper_mode": "paper_manual",
                               "order_kill_switch": True, "is_live": False}}
        self.storage_info = {"available_bytes": 20 * 1024**3, "free_percent": 30.0,
                        "database_bytes": 10, "wal_bytes": 0, "shm_bytes": 0,
                        "data_directory_bytes": 100, "backup_directory_bytes": 0,
                        "report_directory_bytes": 0, "journal_bytes": 0}
        self.marker_info = {"exists": True, "sha256": EXPECTED_MARKER_SHA256, "valid": True}
        self.legacy = {name: {"enabled": False, "active": False} for name in MonitorConfig.legacy_timers}
        self.memory_info = {"collector_process_count": 0, "collector_total_rss_bytes": 0,
                       "collector_max_process_rss_bytes": 0, "collector_runtime_seconds": None,
                       "service_memory_current_bytes": None, "service_memory_peak_bytes": None,
                       "service_tasks_current": None, "system_mem_available_bytes": 8 * 1024**3,
                       "system_mem_available_percent": 50.0, "swap_total_bytes": 0,
                       "swap_used_bytes": 0, "oom_events_detected": 0, "processes": []}

    def collector_timer(self): return self.timer
    def collector_service(self): return self.service
    def database(self, *_): return self.db
    def api_health_and_safety(self): return self.api
    def storage(self, *_): return self.storage_info
    def marker(self, *_): return self.marker_info
    def legacy_timers(self): return self.legacy
    def memory(self): return self.memory_info
    def disable_collector_timer(self): self.disable_calls.append("polycopy-approved-wallet-collect.timer"); return True, "disabled"


@pytest.fixture
def config(tmp_path):
    return MonitorConfig(
        db_path=tmp_path / "test.db", data_dir=tmp_path, marker_path=tmp_path / "marker",
        baseline_path=tmp_path / "baseline.json", state_path=tmp_path / "state.json",
        status_json_path=tmp_path / "latest.json", status_text_path=tmp_path / "latest.txt",
        events_path=tmp_path / "events.jsonl", backups_path=tmp_path / "backups",
        reports_path=tmp_path / "reports", now=lambda: NOW,
    )


def baseline(probe, config):
    data = build_baseline(probe, config)
    config.baseline_path.write_text(json.dumps(data))


def test_healthy_green_and_dedup_only_collection(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config)
    result = evaluate(p, config, no_remediation=True)
    assert result["status"] == "GREEN"
    assert result["collector_result"]["inserted"] == 0


def test_timer_disabled_red_but_no_disable_loop(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config); p.timer = {"enabled": False, "active": False}
    r = evaluate(p, config, no_remediation=False)
    assert r["status"] == "RED" and p.disable_calls == []
    assert r["automatic_action"]["attempted"] is False


@pytest.mark.parametrize(("minutes", "expected"), [(36, "YELLOW"), (51, "RED")])
def test_success_recency_thresholds(config, tmp_path, minutes, expected):
    p = FakeProbe(tmp_path); baseline(p, config)
    p.service["last_success_utc"] = (NOW - timedelta(minutes=minutes)).isoformat()
    assert evaluate(p, config, no_remediation=True)["status"] == expected


def test_identity_failure_and_two_failures_red_and_only_targeted_disable(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config)
    p.service["collector_result"]["fallback_identities"] = 1
    p.service["consecutive_failures"] = 2
    r = evaluate(p, config, no_remediation=False)
    assert r["status"] == "RED"
    assert p.disable_calls == ["polycopy-approved-wallet-collect.timer"]
    assert r["automatic_action"]["action"] == "systemctl disable --now polycopy-approved-wallet-collect.timer"


@pytest.mark.parametrize("field,value", [
    ("integrity", "corrupt"), ("foreign_key_violations", 1),
    ("canonical_duplicate_groups", 1), ("approved_wallet_sell_rows", 1),
])
def test_database_hard_failures_are_red(config, tmp_path, field, value):
    p = FakeProbe(tmp_path); baseline(p, config); p.db[field] = value
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"


def test_baseline_allows_existing_unapproved_but_detects_new(config, tmp_path):
    p = FakeProbe(tmp_path); p.db["unapproved_identities"] = [["polymarket", "old"]]; baseline(p, config)
    assert evaluate(p, config, no_remediation=True)["status"] == "GREEN"
    p.db["unapproved_identities"].append(["polymarket", "new"])
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"


def test_downstream_growth_marker_disk_storage_and_legacy_timer_are_red(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config)
    p.db["table_counts"]["orders"] = 1
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"
    p.db["table_counts"]["orders"] = 0; p.marker_info["sha256"] = "0" * 64
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"
    p.marker_info["sha256"] = EXPECTED_MARKER_SHA256; p.storage_info["available_bytes"] = 9 * 1024**3
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"
    p.storage_info["available_bytes"] = 20 * 1024**3; p.legacy["polycopy-scan.timer"] = {"enabled": True, "active": True}
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"


def test_api_transient_then_repeated_failure_and_memory_limits(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config); p.api["healthy"] = False
    assert evaluate(p, config, no_remediation=True)["status"] == "YELLOW"
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"
    p.api["healthy"] = True; p.memory_info["collector_max_process_rss_bytes"] = 513 * 1024**2
    assert evaluate(p, config, no_remediation=True)["status"] == "RED"


def test_systemd_failure_pair_counts_as_one_invocation():
    class JournalProbe(SystemProbe):
        def run(self, args, timeout=10):
            if args[:2] == ["systemctl", "show"]:
                return SimpleNamespace(returncode=0, stderr="", stdout="ActiveState=inactive\nResult=failed\nExecMainStatus=1\nNRestarts=0\nExecMainExitTimestamp=\n")
            return SimpleNamespace(returncode=0, stderr="", stdout=(
                "2026-07-11T05:00:00+00:00 host systemd[1]: Main process exited, code=exited, status=1/FAILURE\n"
                "2026-07-11T05:00:00+00:00 host systemd[1]: polycopy-approved-wallet-collect.service: Failed with result 'exit-code'.\n"
            ))
    assert JournalProbe().collector_service()["consecutive_failures"] == 1


def test_missing_or_invalid_baseline_is_red_but_never_authorizes_disable(config, tmp_path):
    p = FakeProbe(tmp_path)
    report = evaluate(p, config, no_remediation=False)
    assert report["status"] == "RED"
    assert "approved_wallet_monitor_baseline_missing_or_invalid" in report["reasons"]
    assert p.disable_calls == []
    config.baseline_path.write_text('{"schema_version": 1}')
    assert evaluate(p, config, no_remediation=False)["status"] == "RED"
    assert p.disable_calls == []


def test_api_failure_counter_resets_after_recovery(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config)
    p.api["healthy"] = False
    assert evaluate(p, config, no_remediation=True)["status"] == "YELLOW"
    p.api["healthy"] = True
    assert evaluate(p, config, no_remediation=True)["api"]["consecutive_failures"] == 0
    p.api["healthy"] = False
    assert evaluate(p, config, no_remediation=True)["status"] == "YELLOW"


def test_cli_rejects_artifact_override_outside_runtime_directory(config, tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "monitor_approved_wallet_collector.py"
    spec = importlib.util.spec_from_file_location("monitor_cli_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module.argparse.ArgumentParser()
    with pytest.raises(SystemExit):
        module.set_runtime_override(config, "status_json_path", tmp_path / "polycopy.db", parser)


def test_no_remediation_never_calls_systemctl_and_atomic_json(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config); p.db["integrity"] = "bad"
    evaluate(p, config, no_remediation=True)
    assert p.disable_calls == []
    out = tmp_path / "nested" / "status.json"; write_json_atomic(out, {"safe": True})
    assert json.loads(out.read_text()) == {"safe": True}
    assert not list(out.parent.glob(".*.tmp"))


def test_transition_dedup_and_no_secret_output(config, tmp_path):
    p = FakeProbe(tmp_path); baseline(p, config)
    first = evaluate(p, config, no_remediation=True); assert first["status_changed"] is False
    p.db["integrity"] = "bad"; red = evaluate(p, config, no_remediation=True)
    again = evaluate(p, config, no_remediation=True)
    assert red["status_changed"] is True and again["status_changed"] is False
    assert len(config.events_path.read_text().splitlines()) == 1
    assert "PRIVATE_KEY" not in json.dumps(red).upper()
