from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polycopy.observation import bounded_observation as control
from polycopy.observation.control_lock import ControlLock, ControlLockError

BASE = "6106804001d8bb7fc605be0e698bb24252f14273"
NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
START = datetime(2026, 8, 3, 6, tzinfo=UTC)
WATCH = ["sew_2eb71e1d1cdf40fa"]


def manifest(**changes):
    value = control.make_manifest(observation_id="bounded-one", created_at=NOW, planned_start_at=START, watch_ids=WATCH, database_path="/tmp/disposable.db", repository_baseline_sha=BASE, refresh_market_limit=104)
    value.update(changes)
    return value


def test_secure_lock_rejects_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target"; target.write_text("SENTINEL")
    lock = tmp_path / "lock"; lock.symlink_to(target)
    with pytest.raises(ControlLockError), ControlLock(lock): pass
    assert target.read_text() == "SENTINEL"


def test_secure_lock_regular_contention_and_release(tmp_path):
    path = tmp_path / "lock"
    with ControlLock(path), pytest.raises(ControlLockError), ControlLock(path): pass
    with ControlLock(path): pass


def test_secure_lock_requires_exact_owner_only_mode(tmp_path):
    path = tmp_path / "lock"; path.write_text("", encoding="utf-8"); path.chmod(0o640)
    with pytest.raises(ControlLockError), ControlLock(path): pass
    path.chmod(0o600)
    with ControlLock(path): pass


def test_secure_lock_rejects_directory_and_wrong_owner_mock(tmp_path, monkeypatch):
    with pytest.raises(ControlLockError), ControlLock(tmp_path): pass
    lock = tmp_path / "lock"
    original = os.fstat
    monkeypatch.setattr(os, "fstat", lambda fd: type("S", (), {"st_mode": original(fd).st_mode, "st_uid": -1})())
    with pytest.raises(ControlLockError), ControlLock(lock): pass


@pytest.mark.parametrize("value", ["../../../x", "a/b", "a\\b", ".", "..", " a", "a ", "a\n", "é", "a" * 65])
def test_observation_id_grammar_rejects_paths(value):
    with pytest.raises(control.ObservationValidationError): control.validate_manifest(manifest(observation_id=value))


def test_valid_id_paths_are_direct_children(tmp_path):
    paths = control.artifact_paths(tmp_path, "valid_id-1")
    assert all(path.parent == tmp_path.resolve() for path in paths)


def test_artifact_symlinks_are_rejected_for_reads_and_plan_publication(tmp_path):
    outside = tmp_path / "outside.json"; outside.write_text("{}", encoding="utf-8")
    manifest_path, _state_path, _pointer = control.artifact_paths(tmp_path, "bounded-one")
    manifest_path.symlink_to(outside)
    with pytest.raises(control.ObservationValidationError): control.load_json(manifest_path)
    with pytest.raises(control.ObservationValidationError): control.create_plan(tmp_path, manifest(), now=NOW)


@pytest.mark.parametrize("sha", ["", "not-a-git-sha", BASE.upper(), "a" * 39, "a" * 41, "g" * 40, "sha:" + BASE])
def test_baseline_sha_rejected(sha):
    with pytest.raises(control.ObservationValidationError): control.validate_manifest(manifest(repository_baseline_sha=sha))


def test_lifecycle_chronology_and_checkpoint_status(tmp_path):
    control.create_plan(tmp_path, manifest(), now=NOW)
    active = control.activate_observation(tmp_path, "bounded-one", now=START)
    status = control.control_status(manifest(), active, now=START + timedelta(days=7))
    assert status["next_checkpoint_at"] == control.utc_timestamp(START + timedelta(days=7))
    assert status["next_checkpoint_status"] == "due"
    assert [item["status"] for item in status["checkpoint_schedule"]] == ["due", "upcoming", "upcoming"]
    overdue = control.control_status(manifest(), active, now=START + timedelta(days=8))
    assert overdue["next_checkpoint_status"] == "overdue"
    late = tmp_path / "late"; control.create_plan(late, manifest(), now=NOW)
    with pytest.raises(control.ObservationValidationError): control.activate_observation(late, "bounded-one", now=START + timedelta(days=21))


def test_checkpoint_sha_and_stop_lifecycle_chronology_are_strict(tmp_path):
    control.create_plan(tmp_path, manifest(), now=NOW)
    active = control.activate_observation(tmp_path, "bounded-one", now=START)
    with pytest.raises(control.ObservationValidationError):
        control.record_checkpoint(tmp_path, "bounded-one", day=7, now=START + timedelta(days=7), report_sha256="A" * 64)
    bad_stop = dict(active, state="stop_requested", transition_sequence=2,
                    stop_reason={"category": "manual_operator_stop", "reason": "x", "requested_at": control.utc_timestamp(START - timedelta(seconds=1)), "evidence": None},
                    updated_at=control.utc_timestamp(START))
    with pytest.raises(control.ObservationValidationError): control.validate_state(manifest(), bad_stop)


def test_atomic_directory_fsync_failure_is_not_success(tmp_path, monkeypatch):
    target = tmp_path / "state.json"; before = {"before": True}; control._atomic_write_json(target, before, overwrite=True)
    original = control.os.fsync; calls = 0
    def fail_directory(fd):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("directory fsync")
        return original(fd)
    monkeypatch.setattr(control.os, "fsync", fail_directory)
    with pytest.raises(control.DurabilityConfirmationError): control._atomic_write_json(target, {"after": True}, overwrite=True)
    assert control.load_json(target) == {"after": True}


def test_atomic_directory_open_failure_is_not_success(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    control._atomic_write_json(target, {"before": True}, overwrite=True)
    original_open = control.os.open
    def fail_directory_open(path, *args):
        if Path(path) == tmp_path:
            raise OSError("directory open")
        return original_open(path, *args)
    monkeypatch.setattr(control.os, "open", fail_directory_open)
    with pytest.raises(control.DurabilityConfirmationError, match="directory durability"):
        control._atomic_write_json(target, {"after": True}, overwrite=True)
    assert control.load_json(target) == {"after": True}


def test_load_json_reads_the_no_follow_descriptor_even_if_path_is_replaced(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"original": true}\n', encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"replacement": true}\n', encoding="utf-8")
    original_open = control.os.open

    def open_then_replace(path, flags, *args):
        fd = original_open(path, flags, *args)
        if Path(path) == target:
            os.replace(replacement, target)
        return fd

    original_read_text = Path.read_text
    def read_text_then_replace(path, *args, **kwargs):
        if path == target:
            os.replace(replacement, target)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(control.os, "open", open_then_replace)
    monkeypatch.setattr(Path, "read_text", read_text_then_replace)
    assert control.load_json(target) == {"original": True}
    assert json.loads(original_read_text(target, encoding="utf-8")) == {"replacement": True}


def test_stop_reason_and_checkpoint_records_require_exact_canonical_schemas(tmp_path):
    value = manifest()
    active = control.initial_state(value, now=NOW)
    active.update({"state": "active", "started_at": control.utc_timestamp(START), "transition_sequence": 1, "updated_at": control.utc_timestamp(START)})
    stop = dict(active, state="stop_requested", transition_sequence=2, stop_reason={"category": "manual_operator_stop", "reason": "hold", "evidence": None, "requested_at": "2026-08-03T06:00:00+00:00"})
    with pytest.raises(control.ObservationValidationError):
        control.validate_state(value, stop)
    for invalid_reason in (
        {"category": "manual_operator_stop", "reason": "hold", "evidence": None, "requested_at": control.utc_timestamp(START), "extra": True},
        {"category": "unknown", "reason": "hold", "evidence": None, "requested_at": control.utc_timestamp(START)},
        {"category": "manual_operator_stop", "reason": " ", "evidence": None, "requested_at": control.utc_timestamp(START)},
        {"category": "manual_operator_stop", "reason": "hold", "evidence": 1, "requested_at": control.utc_timestamp(START)},
    ):
        stop["stop_reason"] = invalid_reason
        with pytest.raises(control.ObservationValidationError):
            control.validate_state(value, stop)

    checkpoint = dict(active, updated_at=control.utc_timestamp(START + timedelta(days=7)), checkpoints=[{"day": 7, "recorded_at": control.utc_timestamp(START + timedelta(days=7)), "report_path": None, "report_sha256": None, "operator_note": "x"}])
    assert control.validate_state(value, checkpoint)["checkpoints"][0]["operator_note"] == "x"
    checkpoint["checkpoints"][0]["operator_note"] = "x" * (control.MAX_OPERATOR_NOTE_LENGTH + 1)
    with pytest.raises(control.ObservationValidationError):
        control.validate_state(value, checkpoint)
    checkpoint["checkpoints"][0] = {**checkpoint["checkpoints"][0], "operator_note": None, "recorded_at": "2026-08-10T06:00:00+00:00"}
    with pytest.raises(control.ObservationValidationError):
        control.validate_state(value, checkpoint)
    checkpoint["checkpoints"][0] = {**checkpoint["checkpoints"][0], "recorded_at": control.utc_timestamp(START + timedelta(days=7)), "extra": True}
    with pytest.raises(control.ObservationValidationError):
        control.validate_state(value, checkpoint)


def _cli_module():
    path = Path(__file__).parents[1] / "scripts" / "manage_bounded_observation.py"
    spec = importlib.util.spec_from_file_location("manage_bounded_observation", path)
    module = importlib.util.module_from_spec(spec); assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_cli_help_and_invalid_plan_are_zero_write(tmp_path, capsys):
    cli = _cli_module()
    with pytest.raises(SystemExit): cli.main(["--help"])
    capsys.readouterr()
    assert not list(tmp_path.iterdir())
    code = cli.main(["--data-dir", str(tmp_path), "plan", "--observation-id", "../bad", "--start-at", "2026-08-03T06:00:00Z", "--watch-id", WATCH[0], "--database-path", "/tmp/x", "--repository-baseline-sha", BASE, "--refresh-market-limit", "104"])
    assert code == 2 and not list(tmp_path.iterdir())
    assert json.loads(capsys.readouterr().out)["valid"] is False
    assert cli.parser().parse_args(["status", "--observation-id", "bounded-one"]).data_dir == cli.ROOT / "data" / "specialist_observation"
