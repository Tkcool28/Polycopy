from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from polycopy.observation import bounded_observation as control

BASE = "6106804001d8bb7fc605be0e698bb24252f14273"
WATCHES = ["sew_2eb71e1d1cdf40fa", "sew_34f5ad48a0794dac", "sew_3cf4e30a919a4579", "sew_7b4c211a470a4dfe", "sew_8d97ffa12fa440db"]
NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)
START = datetime(2026, 8, 3, 6, tzinfo=UTC)


def manifest(**changes):
    value = control.make_manifest(observation_id="bounded-five", created_at=NOW, planned_start_at=START, watch_ids=WATCHES, database_path="/tmp/disposable.db", repository_baseline_sha=BASE, refresh_market_limit=104)
    value.update(changes)
    return value


def planned(tmp_path: Path):
    value = manifest()
    control.create_plan(tmp_path, value, now=NOW)
    return value


def active(tmp_path: Path):
    value = planned(tmp_path)
    return value, control.activate_observation(tmp_path, "bounded-five", now=START)


def state_path(tmp_path: Path) -> Path:
    return control.artifact_paths(tmp_path, "bounded-five")[1]


def test_approved_five_watch_manifest_and_single_watch_validate():
    assert control.validate_manifest(manifest())["watch_ids"] == WATCHES
    one = manifest(watch_ids=[WATCHES[0]])
    assert control.validate_manifest(one)["watch_ids"] == [WATCHES[0]]


@pytest.mark.parametrize("change", [
    {"watch_ids": WATCHES + ["sixth"]}, {"watch_ids": [WATCHES[0], WATCHES[0]]},
    {"watch_ids": [" "]}, {"duration_days": 20}, {"checkpoint_days": [7, 7]},
])
def test_manifest_rejects_invalid_cohort_duration_or_checkpoints(change):
    with pytest.raises(control.ObservationValidationError): control.validate_manifest(manifest(**change))


def test_manifest_rejects_reordered_checkpoints_so_status_uses_earliest_day():
    with pytest.raises(control.ObservationValidationError):
        control.validate_manifest(manifest(checkpoint_days=[14, 7, 21]))


def test_manifest_rejects_invalid_cadence_and_caps():
    bad = manifest(); bad["collection"] = {**bad["collection"], "local_times": ["00:00"]}
    with pytest.raises(control.ObservationValidationError): control.validate_manifest(bad)
    bad = manifest(); bad["collection"] = {**bad["collection"], "max_new_source_trades_per_cohort_per_run": 100}
    with pytest.raises(control.ObservationValidationError): control.validate_manifest(bad)


def test_deterministic_serialization_and_immutable_manifest(tmp_path):
    value = planned(tmp_path)
    assert control.canonical_json(value) == control.canonical_json(dict(value))
    with pytest.raises(control.ObservationConflictError): control.create_plan(tmp_path, value, now=NOW)


def test_plan_publication_failure_rolls_back_coupled_artifacts(tmp_path, monkeypatch):
    value = manifest(); original = control._atomic_write_json
    manifest_path, state_path, pointer_path = control.artifact_paths(tmp_path, "bounded-five")

    def fail_state(path, payload, *, overwrite):
        if path == state_path: raise OSError("state publish failure")
        return original(path, payload, overwrite=overwrite)

    monkeypatch.setattr(control, "_atomic_write_json", fail_state)
    with pytest.raises(OSError): control.create_plan(tmp_path, value, now=NOW)
    assert not manifest_path.exists() and not state_path.exists() and not pointer_path.exists()


def test_pointer_publication_failure_rolls_back_new_plan_but_preserves_old_pointer(tmp_path, monkeypatch):
    value = manifest(); original = control._atomic_write_json
    manifest_path, state_path, pointer_path = control.artifact_paths(tmp_path, "bounded-five")
    pointer_path.write_text('{"previous":true}\n', encoding="utf-8"); before = pointer_path.read_bytes()

    def fail_pointer(path, payload, *, overwrite):
        if path == pointer_path: raise OSError("pointer publish failure")
        return original(path, payload, overwrite=overwrite)

    monkeypatch.setattr(control, "_atomic_write_json", fail_pointer)
    with pytest.raises(OSError): control.create_plan(tmp_path, value, now=NOW)
    assert not manifest_path.exists() and not state_path.exists() and pointer_path.read_bytes() == before


def test_initial_state_hash_and_invalid_hash_rejected(tmp_path):
    value = planned(tmp_path)
    state = control.load_json(state_path(tmp_path))
    assert state["state"] == "planned"
    state["manifest_sha256"] = "0" * 64
    with pytest.raises(control.ObservationValidationError): control.validate_state(value, state)
    state = control.initial_state(value, now=NOW)
    state["checkpoints"] = [{"day": 7, "recorded_at": control.utc_timestamp(START + timedelta(days=7)), "report_path": None, "report_sha256": None, "operator_note": None}]
    with pytest.raises(control.ObservationValidationError): control.validate_state(value, state)


def test_atomic_write_failure_keeps_prior_state(tmp_path, monkeypatch):
    value, _ = active(tmp_path)
    target = state_path(tmp_path); before = target.read_bytes()
    def fail_replace(_source, _target): raise OSError("injected replace failure")
    monkeypatch.setattr(control.os, "replace", fail_replace)
    with pytest.raises(OSError): control.request_stop(tmp_path, "bounded-five", reason_category="manual_operator_stop", reason="hold", now=START + timedelta(hours=1))
    assert target.read_bytes() == before
    assert control.validate_state(value, control.load_json(target))["state"] == "active"


def test_stale_expected_transition_fails_closed(tmp_path):
    value, state = active(tmp_path)
    control.request_stop(tmp_path, "bounded-five", reason_category="manual_operator_stop", reason="hold", now=START + timedelta(hours=1))
    with pytest.raises(control.ObservationConflictError):
        control.authorize_extension(tmp_path, "bounded-five", days=1, reason="late", now=START + timedelta(hours=2))
    rollback = dict(state); rollback["state"] = "planned"
    with pytest.raises(control.ObservationValidationError): control.validate_state(value, rollback)


@pytest.mark.parametrize("lifecycle,code", [("planned", "planned_but_inactive"), ("stop_requested", "stop_requested"), ("stopped", "stopped"), ("completed", "completed"), ("failed_closed", "failed_closed")])
def test_gate_denies_nonactive_states(tmp_path, lifecycle, code):
    value = manifest(); state = control.initial_state(value, now=NOW); state["state"] = lifecycle
    if lifecycle != "planned": state["started_at"] = control.utc_timestamp(START)
    if lifecycle == "stop_requested": state["stop_reason"] = {"category": "manual_operator_stop"}
    if lifecycle == "stopped": state.update({"stopped_at": control.utc_timestamp(START), "stop_reason": {"category": "manual_operator_stop"}})
    if lifecycle == "completed": state["completed_at"] = control.utc_timestamp(START + timedelta(days=21))
    # State validation intentionally rejects manually fabricated non-complete forms;
    # gate must still fail closed rather than allow a corrupted artifact.
    verdict = control.may_run_observation_job(value, state, START + timedelta(hours=1), "collection")
    assert not verdict["allowed"]
    assert verdict["reason_code"] in {code, "invalid_state"}


def test_gate_active_window_before_after_and_unsupported(tmp_path):
    value, state = active(tmp_path)
    assert control.may_run_observation_job(value, state, START + timedelta(hours=1), "collection")["allowed"]
    assert control.may_run_observation_job(value, state, START + timedelta(hours=1), "refresh")["allowed"]
    assert control.may_run_observation_job(value, state, START - timedelta(seconds=1), "collection")["reason_code"] == "before_authorized_window"
    assert control.may_run_observation_job(value, state, START + timedelta(days=21), "collection")["reason_code"] == "after_authorized_window"
    assert control.may_run_observation_job(value, state, START, "other")["reason_code"] == "unsupported_job_type"


def test_stop_request_idempotency_and_confirm_stopped_are_distinct(tmp_path):
    active(tmp_path)
    first = control.request_stop(tmp_path, "bounded-five", reason_category="manual_operator_stop", reason="hold", now=START + timedelta(hours=1))
    again = control.request_stop(tmp_path, "bounded-five", reason_category="manual_operator_stop", reason="hold", now=START + timedelta(hours=2))
    assert again == first and again["state"] == "stop_requested"
    with pytest.raises(control.ObservationConflictError): control.request_stop(tmp_path, "bounded-five", reason_category="disk_safety_threshold", reason="different", now=START + timedelta(hours=2))
    stopped = control.confirm_stopped(tmp_path, "bounded-five", now=START + timedelta(hours=3))
    assert stopped["state"] == "stopped"


def test_checkpoint_timing_duplicate_and_metadata(tmp_path):
    active(tmp_path)
    with pytest.raises(control.ObservationValidationError): control.record_checkpoint(tmp_path, "bounded-five", day=7, now=START + timedelta(days=6))
    result = control.record_checkpoint(tmp_path, "bounded-five", day=7, now=START + timedelta(days=7), report_path="/reports/day7.json", report_sha256="a" * 64, operator_note="external")
    record = result["checkpoints"][0]
    assert record["report_path"] == "/reports/day7.json" and record["report_sha256"] == "a" * 64
    with pytest.raises(control.ObservationConflictError): control.record_checkpoint(tmp_path, "bounded-five", day=7, now=START + timedelta(days=8))
    with pytest.raises(control.ObservationValidationError): control.record_checkpoint(tmp_path, "bounded-five", day=8, now=START + timedelta(days=8))


def test_extension_is_explicit_bounded_and_extends_gate_only(tmp_path):
    value, _ = active(tmp_path)
    with pytest.raises(control.ObservationValidationError): control.authorize_extension(tmp_path, "bounded-five", days=8, reason="bad", now=START)
    state = control.authorize_extension(tmp_path, "bounded-five", days=7, reason="authorized", now=START)
    assert control.may_run_observation_job(value, state, START + timedelta(days=24), "refresh")["allowed"]
    assert control.may_run_observation_job(value, state, START + timedelta(days=28), "refresh")["allowed"] is False
    with pytest.raises(control.ObservationConflictError): control.authorize_extension(tmp_path, "bounded-five", days=1, reason="again", now=START)
    expired_dir = tmp_path / "expired"; active(expired_dir)
    with pytest.raises(control.ObservationValidationError):
        control.authorize_extension(expired_dir, "bounded-five", days=1, reason="retroactive", now=START + timedelta(days=21))


def test_completion_is_end_of_window_not_qualification_and_stop_blocks_completion(tmp_path):
    active(tmp_path)
    with pytest.raises(control.ObservationValidationError): control.complete_observation(tmp_path, "bounded-five", now=START + timedelta(days=20))
    complete = control.complete_observation(tmp_path, "bounded-five", now=START + timedelta(days=21))
    assert complete["state"] == "completed"
    assert "qualification" in complete["last_control_verdict"]
    active_dir = tmp_path / "stopped"; active(active_dir)
    control.request_stop(active_dir, "bounded-five", reason_category="manual_operator_stop", reason="hold", now=START)
    with pytest.raises(control.ObservationConflictError): control.complete_observation(active_dir, "bounded-five", now=START + timedelta(days=30))


def test_completed_state_cannot_precede_effective_extension_end(tmp_path):
    value, _ = active(tmp_path)
    extended = control.authorize_extension(tmp_path, "bounded-five", days=7, reason="authorized", now=START)
    premature = dict(extended, state="completed", completed_at=control.utc_timestamp(START + timedelta(days=21)), updated_at=control.utc_timestamp(START + timedelta(days=21)), transition_sequence=extended["transition_sequence"] + 1)
    with pytest.raises(control.ObservationValidationError, match="authorized observation end"):
        control.validate_state(value, premature)


def test_pointer_and_manifest_alone_do_not_activate(tmp_path):
    value = planned(tmp_path); _manifest_path, _state_path, pointer = control.artifact_paths(tmp_path, "bounded-five")
    assert control.load_json(pointer)["observation_id"] == "bounded-five"
    state = control.initial_state(value, now=NOW)
    assert not control.may_run_observation_job(value, state, START + timedelta(hours=1), "collection")["allowed"]


def test_gate_denies_future_activation_and_corrupt_active_state(tmp_path):
    value, state = active(tmp_path)
    delayed = dict(state); delayed["started_at"] = control.utc_timestamp(START + timedelta(hours=2)); delayed["updated_at"] = control.utc_timestamp(START + timedelta(hours=2))
    assert control.validate_state(value, delayed)["state"] == "active"
    assert control.may_run_observation_job(value, delayed, START + timedelta(hours=1), "collection")["reason_code"] == "not_started"
    corrupt = dict(state); corrupt["stopped_at"] = control.utc_timestamp(START)
    with pytest.raises(control.ObservationValidationError): control.validate_state(value, corrupt)
    assert control.may_run_observation_job(value, corrupt, START, "collection")["allowed"] is False


def test_checkpoint_persisted_timing_window_and_hash_are_validated(tmp_path):
    value, _ = active(tmp_path)
    result = control.record_checkpoint(tmp_path, "bounded-five", day=7, now=START + timedelta(days=7), report_sha256="a" * 64)
    early = dict(result); early["checkpoints"] = [dict(result["checkpoints"][0], recorded_at=control.utc_timestamp(START + timedelta(days=6)))]
    with pytest.raises(control.ObservationValidationError): control.validate_state(value, early)
    nonhex = dict(result); nonhex["checkpoints"] = [dict(result["checkpoints"][0], report_sha256="g" * 64)]
    with pytest.raises(control.ObservationValidationError): control.validate_state(value, nonhex)
    with pytest.raises(control.ObservationValidationError): control.record_checkpoint(tmp_path, "bounded-five", day=14, now=START + timedelta(days=22))


def test_manifest_corruption_denies_and_status_is_control_only(tmp_path):
    value, state = active(tmp_path)
    corrupt = dict(value); corrupt["duration_days"] = 20
    assert control.may_run_observation_job(corrupt, state, START, "collection")["allowed"] is False
    status = control.control_status(value, state, now=START)
    assert status["control_plane_only"] is True and "database" not in status


def _persisted_active(value, *, started_at=START, updated_at=START):
    state = control.initial_state(value, now=NOW)
    state.update({"state": "active", "started_at": control.utc_timestamp(started_at), "updated_at": control.utc_timestamp(updated_at), "transition_sequence": 1})
    return state


def _extended_active(value, *, authorized_at=START, updated_at=START):
    state = _persisted_active(value, updated_at=updated_at)
    state.update({"extension_authorized_days": 1, "extension_authorized_at": control.utc_timestamp(authorized_at), "extension_reason": "approved", "effective_extended_end_at": control.utc_timestamp(START + timedelta(days=22))})
    return state


def test_planned_extensions_are_rejected_and_cannot_be_activated_or_extend_gate(tmp_path):
    value = planned(tmp_path)
    injected = control.initial_state(value, now=NOW)
    injected.update({"extension_authorized_days": 1, "extension_authorized_at": control.utc_timestamp(START), "extension_reason": "injected", "effective_extended_end_at": control.utc_timestamp(START + timedelta(days=22))})
    with pytest.raises(control.ObservationValidationError, match="planned state cannot contain extension metadata"):
        control.validate_state(value, injected)
    partial = control.initial_state(value, now=NOW); partial["extension_reason"] = "injected"
    with pytest.raises(control.ObservationValidationError, match="no extension"):
        control.validate_state(value, partial)
    control._atomic_write_json(state_path(tmp_path), injected, overwrite=True)
    with pytest.raises(control.ObservationValidationError, match="planned state cannot contain extension metadata"):
        control.activate_observation(tmp_path, "bounded-five", now=START)
    for job_type in ("collection", "refresh"):
        verdict = control.may_run_observation_job(value, injected, START + timedelta(days=21, hours=1), job_type)
        assert verdict == {"allowed": False, "reason_code": "invalid_state", "explanation": "Control artifacts are invalid: planned state cannot contain extension metadata"}


def test_stop_flow_preserves_extension_metadata_and_lifecycle_first_gate_denial(tmp_path):
    value, _ = active(tmp_path)
    extended = control.authorize_extension(tmp_path, "bounded-five", days=2, reason="approved", now=START + timedelta(hours=1))
    extension = {name: extended[name] for name in ("extension_authorized_days", "extension_authorized_at", "extension_reason", "effective_extended_end_at")}
    requested = control.request_stop(tmp_path, "bounded-five", reason_category="manual_operator_stop", reason="hold", now=START + timedelta(hours=2))
    assert requested["state"] == "stop_requested"
    assert {name: requested[name] for name in extension} == extension
    for job_type in ("collection", "refresh"):
        assert control.may_run_observation_job(value, requested, START + timedelta(days=22), job_type)["reason_code"] == "stop_requested"
    stopped = control.confirm_stopped(tmp_path, "bounded-five", now=START + timedelta(hours=3))
    assert stopped["state"] == "stopped"
    assert {name: stopped[name] for name in extension} == extension
    for job_type in ("collection", "refresh"):
        assert control.may_run_observation_job(value, stopped, START + timedelta(days=22), job_type)["reason_code"] == "stopped"


def test_persisted_extension_bearing_stop_states_and_failed_closed_validate():
    value = manifest(); extension_at = START + timedelta(hours=1); requested_at = START + timedelta(hours=2); stopped_at = START + timedelta(hours=3)
    active_state = _extended_active(value, authorized_at=extension_at, updated_at=requested_at)
    request = dict(active_state, state="stop_requested", transition_sequence=2, stop_reason={"category": "manual_operator_stop", "reason": "hold", "evidence": None, "requested_at": control.utc_timestamp(requested_at)})
    assert control.validate_state(value, request) == request
    stopped = dict(request, state="stopped", transition_sequence=3, stopped_at=control.utc_timestamp(stopped_at), updated_at=control.utc_timestamp(stopped_at))
    assert control.validate_state(value, stopped) == stopped
    failed_closed = dict(active_state, state="failed_closed", updated_at=control.utc_timestamp(requested_at))
    assert control.validate_state(value, failed_closed) == failed_closed
    for job_type in ("collection", "refresh"):
        assert control.may_run_observation_job(value, failed_closed, START + timedelta(days=22), job_type)["reason_code"] == "failed_closed"


@pytest.mark.parametrize(("lifecycle", "authorized_at", "requested_at", "stopped_at", "message"), [
    ("stop_requested", START + timedelta(hours=2), START + timedelta(hours=1), None, "extension authorization follows stop request"),
    ("stopped", START + timedelta(hours=3), START + timedelta(hours=2), START + timedelta(hours=4), "extension authorization follows stop request"),
    ("stopped", START + timedelta(hours=3), START + timedelta(hours=4), START + timedelta(hours=2), "extension authorization follows stopped_at"),
])
def test_extension_chronology_must_precede_stop_events(lifecycle, authorized_at, requested_at, stopped_at, message):
    value = manifest(); state = _extended_active(value, authorized_at=authorized_at, updated_at=max(authorized_at, requested_at, stopped_at or requested_at))
    state.update({"state": lifecycle, "transition_sequence": 2 if lifecycle == "stop_requested" else 3, "stop_reason": {"category": "manual_operator_stop", "reason": "hold", "evidence": None, "requested_at": control.utc_timestamp(requested_at)}})
    if stopped_at is not None:
        state["stopped_at"] = control.utc_timestamp(stopped_at)
    with pytest.raises(control.ObservationValidationError, match=message):
        control.validate_state(value, state)


@pytest.mark.parametrize(("authorized_at", "updated_at", "message"), [
    (START - timedelta(seconds=1), START, "extension authorization chronology"),
    (START + timedelta(days=21), START + timedelta(days=21), "extension authorization chronology"),
    (START + timedelta(days=21, seconds=1), START + timedelta(days=21, seconds=1), "extension authorization chronology"),
    (START + timedelta(hours=1), START, "extension authorization chronology"),
])
def test_extension_authorization_chronology_is_strict(authorized_at, updated_at, message):
    value = manifest(); state = _extended_active(value, authorized_at=authorized_at, updated_at=updated_at)
    with pytest.raises(control.ObservationValidationError, match=message):
        control.validate_state(value, state)


def test_valid_active_and_completed_extensions_pass():
    value = manifest(); active_state = _extended_active(value)
    assert control.validate_state(value, active_state) == active_state
    completed = dict(active_state, state="completed", transition_sequence=2, completed_at=control.utc_timestamp(START + timedelta(days=22)), updated_at=control.utc_timestamp(START + timedelta(days=22)))
    assert control.validate_state(value, completed) == completed


@pytest.mark.parametrize("field", ["created_at", "updated_at", "started_at", "stopped_at", "completed_at", "extension_authorized_at", "effective_extended_end_at"])
@pytest.mark.parametrize("noncanonical", ["2026-08-03T06:00:00+00:00", "2026-08-03T07:00:00+01:00", "2026-08-03T06:00:00.000Z"])
def test_persisted_lifecycle_timestamps_require_canonical_utc(field, noncanonical):
    value = manifest(); state = _extended_active(value, updated_at=START + timedelta(days=22))
    if field == "stopped_at":
        state.update({"state": "stopped", "transition_sequence": 3, "stopped_at": control.utc_timestamp(START + timedelta(days=1)), "stop_reason": {"category": "manual_operator_stop", "reason": "hold", "evidence": None, "requested_at": control.utc_timestamp(START)}})
    elif field == "completed_at":
        state.update({"state": "completed", "transition_sequence": 2, "completed_at": control.utc_timestamp(START + timedelta(days=22))})
    state[field] = noncanonical
    with pytest.raises(control.ObservationValidationError, match="canonical UTC"):
        control.validate_state(value, state)


def test_event_timestamps_must_not_follow_updated_at():
    value = manifest()
    late_start = _persisted_active(value, started_at=START + timedelta(seconds=1), updated_at=START)
    with pytest.raises(control.ObservationValidationError, match="started_at follows updated_at"): control.validate_state(value, late_start)
    stopped = _persisted_active(value, updated_at=START)
    stopped.update({"state": "stopped", "transition_sequence": 3, "stopped_at": control.utc_timestamp(START), "stop_reason": {"category": "manual_operator_stop", "reason": "hold", "evidence": None, "requested_at": control.utc_timestamp(START + timedelta(seconds=1))}})
    with pytest.raises(control.ObservationValidationError, match="stop request follows updated_at"): control.validate_state(value, stopped)
    stopped["stop_reason"]["requested_at"] = control.utc_timestamp(START); stopped["stopped_at"] = control.utc_timestamp(START + timedelta(seconds=1))
    with pytest.raises(control.ObservationValidationError, match="stopped_at follows updated_at"): control.validate_state(value, stopped)
    completed = _persisted_active(value, updated_at=START)
    completed.update({"state": "completed", "transition_sequence": 2, "completed_at": control.utc_timestamp(START + timedelta(days=21))})
    with pytest.raises(control.ObservationValidationError, match="completed_at follows updated_at"): control.validate_state(value, completed)
    late_extension = _extended_active(value, authorized_at=START + timedelta(hours=1), updated_at=START)
    with pytest.raises(control.ObservationValidationError, match="extension authorization chronology"): control.validate_state(value, late_extension)


def test_delayed_activation_rejects_retroactive_checkpoint_and_allows_post_activation():
    value = manifest(); delayed_start = START + timedelta(days=8)
    state = _persisted_active(value, started_at=delayed_start, updated_at=delayed_start)
    checkpoint = {"day": 7, "recorded_at": control.utc_timestamp(START + timedelta(days=7)), "report_path": None, "report_sha256": None, "operator_note": None}
    state["checkpoints"] = [checkpoint]
    with pytest.raises(control.ObservationValidationError, match="precedes activation"): control.validate_state(value, state)
    state["checkpoints"] = [dict(checkpoint, recorded_at=control.utc_timestamp(delayed_start))]
    assert control.validate_state(value, state)["checkpoints"] == state["checkpoints"]
    stale_update = dict(state, checkpoints=[dict(checkpoint, recorded_at=control.utc_timestamp(delayed_start + timedelta(seconds=1)))], updated_at=control.utc_timestamp(delayed_start))
    with pytest.raises(control.ObservationValidationError, match="checkpoint recorded_at follows updated_at"): control.validate_state(value, stale_update)


def test_inactive_example_freezes_policy_and_cannot_validate_as_live_manifest():
    path = Path(__file__).parents[1] / "config" / "specialist_observation.example.json"
    template = json.loads(path.read_text(encoding="utf-8"))
    assert template["watch_ids"] == WATCHES
    assert template["duration_days"] == 21
    assert template["checkpoint_days"] == [7, 14, 21]
    assert template["collection"]["local_times"] == ["00:00", "06:00", "12:00", "18:00"]
    assert template["collection"]["max_new_source_trades_per_wallet_per_run"] == 25
    assert template["collection"]["max_new_source_trades_per_cohort_per_run"] == 125
    assert template["collection"]["max_gamma_enrichment_operations_per_run"] == 125
    assert template["refresh"]["local_time"] == "01:00"
    assert template["refresh"]["max_market_limit"] == 104
    assert template["daily_operational_ceilings"] == {"max_collection_enrichment_provider_operations": 500, "max_market_refresh_provider_operations": 104, "max_total_planned_provider_operations": 604}
    assert template["lock_path"] == control.OPERATIONAL_LOCK_PATH
    assert template["database_path"] == "/root/Polycopy/data/polycopy.db"
    assert template["activation"] is False
    with pytest.raises(control.ObservationValidationError):
        control.validate_manifest(template)
