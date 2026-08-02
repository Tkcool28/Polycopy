"""Filesystem-only control plane for a bounded specialist observation.

This module deliberately has no database, provider, service-manager, or execution
imports.  A manifest describes an immutable observation; a separate hash-bound
state file records only lifecycle control facts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from polycopy.observation.control_lock import ControlLock

MANIFEST_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
CONTROL_LOCK_PATH = "/tmp/polycopy-specialist-observation-control.lock"
OPERATIONAL_LOCK_PATH = "/tmp/polycopy-operational-jobs.lock"
MAX_WATCHES = 5
STANDARD_DURATION_DAYS = 21
MAX_EXTENSION_DAYS = 7
MAX_OPERATOR_NOTE_LENGTH = 2_000
GLOBAL_REFRESH_MARKET_LIMIT = 500  # Matches refresh_specialist_market_truth.py.
ALLOWED_STATES = frozenset({"planned", "active", "stop_requested", "stopped", "completed", "failed_closed"})
ALLOWED_JOB_TYPES = frozenset({"collection", "refresh"})
PERMITTED_TRANSITIONS = {
    "planned": frozenset({"active", "failed_closed"}),
    "active": frozenset({"active", "stop_requested", "completed", "failed_closed"}),
    "stop_requested": frozenset({"stopped", "failed_closed"}),
    "stopped": frozenset(),
    "completed": frozenset(),
    "failed_closed": frozenset(),
}
STOP_REASON_CATEGORIES = frozenset({
    "manual_operator_stop", "collection_budget_exceeded", "provider_operation_budget_exceeded",
    "operational_lock_failure_threshold", "repeated_job_failure", "disk_safety_threshold",
    "memory_safety_threshold", "database_integrity_concern", "unexpected_sell_evidence",
    "unexpected_execution_artifact", "watch_cohort_drift", "manifest_state_corruption",
    "end_of_authorized_observation_window",
})


class ObservationValidationError(ValueError):
    """A manifest, state, timestamp, or lifecycle contract is invalid."""


class ObservationConflictError(ObservationValidationError):
    """A concurrent/stale or otherwise incompatible control transition occurred."""


class DurabilityConfirmationError(ObservationValidationError):
    """Replacement occurred but directory durability confirmation failed."""


OBSERVATION_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


def canonical_json(value: Any) -> str:
    """Return the sole deterministic JSON representation used for artifacts."""
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ObservationValidationError(f"{field} must be a non-empty timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ObservationValidationError(f"{field} is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ObservationValidationError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_canonical_timestamp(value: object, field: str) -> datetime:
    parsed = _parse_timestamp(value, field)
    if value != utc_timestamp(parsed):
        raise ObservationValidationError(f"{field} must be a canonical UTC timestamp")
    return parsed


def _artifact_root(data_dir: Path) -> Path:
    """Resolve the artifact root once, so generated names cannot escape it."""
    try:
        return Path(data_dir).resolve(strict=False)
    except OSError as exc:
        raise ObservationValidationError(f"unsafe artifact directory: {data_dir}") from exc


def _safe_artifact_path(path: Path) -> None:
    """Reject symlinks and non-regular artifact files before reading or replacing."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ObservationValidationError(f"cannot inspect JSON artifact {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ObservationValidationError(f"unsafe JSON artifact path: {path}")


def utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ObservationValidationError("timestamps must be timezone-aware")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _require_int(value: object, field: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ObservationValidationError(f"{field} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ObservationValidationError(f"{field} must be <= {maximum}")
    return value


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ObservationValidationError(f"{field} must be an object")
    return value


def _validate_timezone(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ObservationValidationError("timezone must be a non-empty IANA timezone")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ObservationValidationError("timezone must be a valid IANA timezone") from exc
    return value


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize an immutable manifest without accessing runtime systems."""
    source = _require_mapping(manifest, "manifest")
    required = {
        "schema_version", "observation_id", "created_at", "timezone", "watch_ids", "planned_start_at",
        "planned_end_at", "duration_days", "checkpoint_days", "extension_policy", "collection",
        "enrichment_policy", "refresh", "lock_path", "daily_operational_ceilings", "database_path",
        "repository_baseline_sha", "status", "creation_does_not_activate",
    }
    missing = sorted(required - set(source))
    if missing:
        raise ObservationValidationError(f"manifest missing required fields: {', '.join(missing)}")
    if source["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ObservationValidationError("unsupported manifest schema_version")
    observation_id = source["observation_id"]
    if not isinstance(observation_id, str) or not OBSERVATION_ID_RE.fullmatch(observation_id):
        raise ObservationValidationError("observation_id must be canonical lowercase ASCII")
    timezone_name = _validate_timezone(source["timezone"])
    created_at = _parse_timestamp(source["created_at"], "created_at")
    start = _parse_timestamp(source["planned_start_at"], "planned_start_at")
    end = _parse_timestamp(source["planned_end_at"], "planned_end_at")
    duration = _require_int(source["duration_days"], "duration_days")
    if duration != STANDARD_DURATION_DAYS:
        raise ObservationValidationError(f"duration_days must be {STANDARD_DURATION_DAYS}")
    if end != start + timedelta(days=duration):
        raise ObservationValidationError("planned_end_at must equal planned_start_at plus duration_days")
    watches = source["watch_ids"]
    if not isinstance(watches, list) or not 1 <= len(watches) <= MAX_WATCHES:
        raise ObservationValidationError("watch_ids must contain one to five IDs")
    if any(not isinstance(item, str) or not item.strip() for item in watches):
        raise ObservationValidationError("watch_ids must not contain blank IDs")
    if len(set(watches)) != len(watches):
        raise ObservationValidationError("watch_ids must be unique")
    checkpoints = source["checkpoint_days"]
    if not isinstance(checkpoints, list) or not checkpoints:
        raise ObservationValidationError("checkpoint_days must be a non-empty list")
    if any(isinstance(day, bool) or not isinstance(day, int) or day < 1 or day > duration for day in checkpoints):
        raise ObservationValidationError("checkpoint_days must be unique days within duration")
    if len(set(checkpoints)) != len(checkpoints):
        raise ObservationValidationError("checkpoint_days must be unique")
    if checkpoints != sorted(checkpoints):
        raise ObservationValidationError("checkpoint_days must be ordered ascending")
    collection = _require_mapping(source["collection"], "collection")
    if collection.get("timezone") != timezone_name or collection.get("runs_per_day") != 4:
        raise ObservationValidationError("collection cadence must be four runs in the declared timezone")
    if collection.get("local_times") != ["00:00", "06:00", "12:00", "18:00"]:
        raise ObservationValidationError("collection local_times must be 00:00, 06:00, 12:00, 18:00")
    per_wallet = _require_int(collection.get("max_new_source_trades_per_wallet_per_run"), "collection max per wallet")
    cohort_cap = _require_int(collection.get("max_new_source_trades_per_cohort_per_run"), "collection cohort cap")
    gamma_run = _require_int(collection.get("max_gamma_enrichment_operations_per_run"), "collection gamma per run")
    if per_wallet * len(watches) > cohort_cap:
        raise ObservationValidationError("per-wallet cap multiplied by cohort exceeds cohort cap")
    if gamma_run > cohort_cap:
        raise ObservationValidationError("collection enrichment cap cannot exceed collection cohort cap")
    if collection.get("buy_only") is not True:
        raise ObservationValidationError("collection must remain buy_only")
    enrichment = _require_mapping(source["enrichment_policy"], "enrichment_policy")
    if enrichment.get("mode") != "inline" or enrichment.get("preserve_honest_classifications") is not True:
        raise ObservationValidationError("enrichment policy must be inline and preserve honest classifications")
    refresh = _require_mapping(source["refresh"], "refresh")
    if refresh.get("timezone") != timezone_name or refresh.get("local_time") != "01:00" or refresh.get("runs_per_day") != 1:
        raise ObservationValidationError("refresh cadence must be daily at 01:00 in declared timezone")
    refresh_limit = _require_int(refresh.get("max_market_limit"), "refresh max_market_limit")
    if refresh_limit > GLOBAL_REFRESH_MARKET_LIMIT:
        raise ObservationValidationError("refresh max_market_limit exceeds global script maximum")
    if refresh.get("requires_explicit_market_limit") is not True:
        raise ObservationValidationError("refresh must require an explicit market limit")
    ceilings = _require_mapping(source["daily_operational_ceilings"], "daily_operational_ceilings")
    daily_collection = _require_int(ceilings.get("max_collection_enrichment_provider_operations"), "daily collection ceiling")
    daily_refresh = _require_int(ceilings.get("max_market_refresh_provider_operations"), "daily refresh ceiling")
    daily_total = _require_int(ceilings.get("max_total_planned_provider_operations"), "daily total ceiling")
    if daily_collection < max(cohort_cap, gamma_run) * 4:
        raise ObservationValidationError("daily collection ceiling is below one full authorized day")
    if daily_total != daily_collection + daily_refresh:
        raise ObservationValidationError("daily total ceiling must equal collection plus refresh ceilings")
    policy = _require_mapping(source["extension_policy"], "extension_policy")
    if policy.get("maximum_days") != MAX_EXTENSION_DAYS or policy.get("automatic") is not False or policy.get("requires_explicit_authorization") is not True:
        raise ObservationValidationError("extension policy must be explicit, non-automatic, and limited to seven days")
    if source["lock_path"] != OPERATIONAL_LOCK_PATH:
        raise ObservationValidationError("manifest lock_path must be the shared operational job lock")
    if source["status"] != "planned" or source["creation_does_not_activate"] is not True:
        raise ObservationValidationError("new manifests are planned and must explicitly not activate work")
    if not isinstance(source["database_path"], str) or not source["database_path"].strip():
        raise ObservationValidationError("database_path must be non-blank")
    if not isinstance(source["repository_baseline_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", source["repository_baseline_sha"]):
        raise ObservationValidationError("repository_baseline_sha must be canonical lowercase 40-character SHA")
    normalized = dict(source)
    normalized.update({"created_at": utc_timestamp(created_at), "planned_start_at": utc_timestamp(start), "planned_end_at": utc_timestamp(end)})
    return normalized


def make_manifest(*, observation_id: str, created_at: datetime, planned_start_at: datetime, watch_ids: list[str], database_path: str, repository_baseline_sha: str, refresh_market_limit: int, collection_per_wallet_cap: int = 25, collection_cohort_cap: int = 125, collection_gamma_cap: int = 125, daily_collection_cap: int = 500, daily_refresh_cap: int = 104) -> dict[str, Any]:
    start = _parse_timestamp(utc_timestamp(planned_start_at), "planned_start_at")
    return validate_manifest({
        "schema_version": MANIFEST_SCHEMA_VERSION, "observation_id": observation_id,
        "created_at": utc_timestamp(created_at), "timezone": "America/Denver", "watch_ids": list(watch_ids),
        "planned_start_at": utc_timestamp(start), "planned_end_at": utc_timestamp(start + timedelta(days=STANDARD_DURATION_DAYS)),
        "duration_days": STANDARD_DURATION_DAYS, "checkpoint_days": [7, 14, 21],
        "extension_policy": {"maximum_days": MAX_EXTENSION_DAYS, "automatic": False, "requires_explicit_authorization": True},
        "collection": {"timezone": "America/Denver", "runs_per_day": 4, "local_times": ["00:00", "06:00", "12:00", "18:00"], "max_new_source_trades_per_wallet_per_run": collection_per_wallet_cap, "max_new_source_trades_per_cohort_per_run": collection_cohort_cap, "max_gamma_enrichment_operations_per_run": collection_gamma_cap, "buy_only": True},
        "enrichment_policy": {"mode": "inline", "preserve_honest_classifications": True},
        "refresh": {"timezone": "America/Denver", "runs_per_day": 1, "local_time": "01:00", "max_market_limit": refresh_market_limit, "requires_explicit_market_limit": True, "safety_behavior": "PR-90"},
        "lock_path": OPERATIONAL_LOCK_PATH,
        "daily_operational_ceilings": {"max_collection_enrichment_provider_operations": daily_collection_cap, "max_market_refresh_provider_operations": daily_refresh_cap, "max_total_planned_provider_operations": daily_collection_cap + daily_refresh_cap},
        "database_path": database_path, "repository_baseline_sha": repository_baseline_sha, "status": "planned", "creation_does_not_activate": True,
    })


def initial_state(manifest: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    validated = validate_manifest(manifest)
    return {"schema_version": STATE_SCHEMA_VERSION, "observation_id": validated["observation_id"], "manifest_sha256": sha256_json(validated), "state": "planned", "created_at": utc_timestamp(now), "updated_at": utc_timestamp(now), "started_at": None, "stopped_at": None, "completed_at": None, "stop_reason": None, "extension_authorized_days": 0, "extension_authorized_at": None, "extension_reason": None, "effective_extended_end_at": None, "checkpoints": [], "last_control_verdict": "planned_not_active", "control_metadata": {"control_plane_only": True}, "transition_sequence": 0}


def validate_state(manifest: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    validated_manifest = validate_manifest(manifest)
    source = _require_mapping(state, "state")
    required = {"schema_version", "observation_id", "manifest_sha256", "state", "created_at", "updated_at", "started_at", "stopped_at", "completed_at", "stop_reason", "extension_authorized_days", "extension_authorized_at", "extension_reason", "effective_extended_end_at", "checkpoints", "last_control_verdict", "control_metadata", "transition_sequence"}
    missing = sorted(required - set(source))
    if missing:
        raise ObservationValidationError(f"state missing required fields: {', '.join(missing)}")
    if source["schema_version"] != STATE_SCHEMA_VERSION or source["observation_id"] != validated_manifest["observation_id"]:
        raise ObservationValidationError("state schema or observation identity mismatch")
    if source["manifest_sha256"] != sha256_json(validated_manifest):
        raise ObservationValidationError("state manifest hash mismatch")
    lifecycle = source["state"]
    if lifecycle not in ALLOWED_STATES:
        raise ObservationValidationError("unsupported state")
    sequence = _require_int(source["transition_sequence"], "transition_sequence", minimum=0)
    minimum_sequence = {"planned": 0, "active": 1, "stop_requested": 2, "stopped": 3, "completed": 2, "failed_closed": 1}[lifecycle]
    if sequence < minimum_sequence or (lifecycle == "planned" and sequence != 0):
        raise ObservationValidationError("state transition sequence is inconsistent with lifecycle")
    created = _parse_canonical_timestamp(source["created_at"], "state created_at")
    updated = _parse_canonical_timestamp(source["updated_at"], "state updated_at")
    if updated < created:
        raise ObservationValidationError("state updated_at precedes created_at")
    timestamp_fields = ("started_at", "stopped_at", "completed_at", "extension_authorized_at", "effective_extended_end_at")
    parsed = {field: _parse_canonical_timestamp(source[field], field) if source[field] is not None else None for field in timestamp_fields}
    if lifecycle == "planned" and any(parsed[name] for name in ("started_at", "stopped_at", "completed_at")):
        raise ObservationValidationError("planned state cannot have lifecycle timestamps")
    if lifecycle == "planned" and source["stop_reason"] is not None:
        raise ObservationValidationError("planned state cannot have a stop reason")
    if lifecycle == "active" and (parsed["started_at"] is None or any(parsed[name] for name in ("stopped_at", "completed_at")) or source["stop_reason"] is not None):
        raise ObservationValidationError("active state requires only started_at and no stop or terminal fields")
    if lifecycle == "stop_requested" and (parsed["started_at"] is None or any(parsed[name] for name in ("stopped_at", "completed_at")) or not isinstance(source["stop_reason"], Mapping)):
        raise ObservationValidationError("stop_requested requires started_at and stop_reason only")
    if lifecycle == "stopped" and (parsed["stopped_at"] is None or not isinstance(source["stop_reason"], Mapping) or parsed["completed_at"] is not None):
        raise ObservationValidationError("stopped requires stopped_at and stop_reason without completion")
    if lifecycle == "completed" and (parsed["completed_at"] is None or parsed["stopped_at"] is not None or source["stop_reason"] is not None):
        raise ObservationValidationError("completed requires completed_at without stop fields")
    started_at = parsed["started_at"]
    if started_at is not None and started_at < created:
        raise ObservationValidationError("started_at precedes state creation")
    if started_at is not None and started_at > updated:
        raise ObservationValidationError("started_at follows updated_at")
    for field in ("stopped_at", "completed_at"):
        event_at = parsed[field]
        if event_at is not None and (started_at is None or event_at < started_at):
            raise ObservationValidationError(f"{field} precedes started_at")
        if event_at is not None and event_at > updated:
            raise ObservationValidationError(f"{field} follows updated_at")
    completed_at = parsed["completed_at"]
    effective_end = parsed["effective_extended_end_at"] or _parse_timestamp(validated_manifest["planned_end_at"], "planned_end_at")
    if lifecycle == "completed" and (completed_at is None or completed_at < effective_end):
        raise ObservationValidationError("completed_at precedes the authorized observation end")
    stop_reason = source["stop_reason"]
    requested_at: datetime | None = None
    if isinstance(stop_reason, Mapping):
        if set(stop_reason) != {"category", "reason", "evidence", "requested_at"}:
            raise ObservationValidationError("stop_reason must contain exactly category, reason, evidence, and requested_at")
        if stop_reason["category"] not in STOP_REASON_CATEGORIES:
            raise ObservationValidationError("stop_reason category is invalid")
        if not isinstance(stop_reason["reason"], str) or not stop_reason["reason"].strip():
            raise ObservationValidationError("stop_reason reason must be a non-blank string")
        if stop_reason["evidence"] is not None and not isinstance(stop_reason["evidence"], str):
            raise ObservationValidationError("stop_reason evidence must be a string or null")
        requested_at = _parse_canonical_timestamp(stop_reason["requested_at"], "stop_reason requested_at")
        if started_at is None or requested_at < started_at:
            raise ObservationValidationError("stop request precedes started_at")
        if requested_at > updated:
            raise ObservationValidationError("stop request follows updated_at")
        stopped_at = parsed["stopped_at"]
    extension_days = _require_int(source["extension_authorized_days"], "extension_authorized_days", minimum=0, maximum=MAX_EXTENSION_DAYS)
    if extension_days == 0:
        if any(parsed[name] is not None for name in ("extension_authorized_at", "effective_extended_end_at")) or source["extension_reason"] is not None:
            raise ObservationValidationError("no extension cannot have extension fields")
    else:
        extension_authorized_at = parsed["extension_authorized_at"]
        effective_extended_end_at = parsed["effective_extended_end_at"]
        original_end = _parse_timestamp(validated_manifest["planned_end_at"], "planned_end_at")
        if lifecycle == "planned":
            raise ObservationValidationError("planned state cannot contain extension metadata")
        if started_at is None or extension_authorized_at is None or effective_extended_end_at is None or not isinstance(source["extension_reason"], str) or not source["extension_reason"].strip():
            raise ObservationValidationError("extension requires started_at, authorization timestamp, end, and reason")
        if extension_authorized_at < started_at or extension_authorized_at >= original_end or extension_authorized_at > updated:
            raise ObservationValidationError("extension authorization chronology is invalid")
        expected = original_end + timedelta(days=extension_days)
        if effective_extended_end_at != expected:
            raise ObservationValidationError("effective extension end is inconsistent")
        stopped_at = parsed["stopped_at"]
        if lifecycle == "stopped" and (stopped_at is None or extension_authorized_at > stopped_at):
            raise ObservationValidationError("extension authorization follows stopped_at")
        if lifecycle in {"stop_requested", "stopped"} and (requested_at is None or extension_authorized_at > requested_at):
            raise ObservationValidationError("extension authorization follows stop request")
        if lifecycle == "completed" and (completed_at is None or completed_at < effective_extended_end_at or extension_authorized_at > completed_at):
            raise ObservationValidationError("completed extension chronology is invalid")
    stopped_at = parsed["stopped_at"]
    if requested_at is not None and stopped_at is not None and requested_at > stopped_at:
        raise ObservationValidationError("stop request follows stopped_at")
    checkpoints = source["checkpoints"]
    if not isinstance(checkpoints, list):
        raise ObservationValidationError("checkpoints must be a list")
    if lifecycle == "planned" and checkpoints:
        raise ObservationValidationError("planned state cannot contain checkpoints")
    seen: set[int] = set()
    for record in checkpoints:
        rec = _require_mapping(record, "checkpoint record")
        if set(rec) != {"day", "recorded_at", "report_path", "report_sha256", "operator_note"}:
            raise ObservationValidationError("checkpoint record has unexpected or missing fields")
        day = _require_int(rec["day"], "checkpoint day")
        if day not in validated_manifest["checkpoint_days"] or day in seen:
            raise ObservationValidationError("checkpoint record is undefined or duplicated")
        seen.add(day)
        recorded_at = _parse_canonical_timestamp(rec["recorded_at"], "checkpoint recorded_at")
        due_at = _parse_timestamp(validated_manifest["planned_start_at"], "planned_start_at") + timedelta(days=day)
        authorized_end_at = parsed["effective_extended_end_at"] or _parse_timestamp(validated_manifest["planned_end_at"], "planned_end_at")
        if started_at is None or recorded_at < started_at:
            raise ObservationValidationError("checkpoint recorded_at precedes activation")
        if recorded_at < due_at or recorded_at > authorized_end_at:
            raise ObservationValidationError("checkpoint recorded_at is outside its authorized window")
        if recorded_at > updated:
            raise ObservationValidationError("checkpoint recorded_at follows updated_at")
        if rec.get("report_path") is not None and not isinstance(rec["report_path"], str):
            raise ObservationValidationError("checkpoint report_path must be a string or null")
        if rec.get("report_sha256") is not None and (not isinstance(rec["report_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", rec["report_sha256"])):
            raise ObservationValidationError("checkpoint report_sha256 must be a canonical lowercase SHA-256 or null")
        if rec["operator_note"] is not None and (not isinstance(rec["operator_note"], str) or len(rec["operator_note"]) > MAX_OPERATOR_NOTE_LENGTH):
            raise ObservationValidationError("checkpoint operator_note must be a string of at most 2000 characters or null")
    return dict(source)


def authorized_end(manifest: Mapping[str, Any], state: Mapping[str, Any]) -> datetime:
    validate_state(manifest, state)
    extension = state["effective_extended_end_at"]
    return _parse_timestamp(extension if extension is not None else manifest["planned_end_at"], "authorized end")


def _atomic_write_json(path: Path, value: Mapping[str, Any], *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_artifact_path(path)
    if not overwrite and path.exists():
        raise ObservationConflictError(f"refusing to overwrite existing immutable manifest: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name); replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(canonical_json(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path); replaced = True
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise DurabilityConfirmationError(f"replacement occurred but directory durability confirmation failed: {path}") from exc
    except DurabilityConfirmationError: raise
    except Exception:
        if not replaced: tmp.unlink(missing_ok=True)
        raise


def load_json(path: Path) -> dict[str, Any]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ObservationValidationError("safe JSON artifact reads require O_NOFOLLOW")
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ObservationValidationError(f"unsafe JSON artifact path: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            value = json.load(stream)
    except ObservationValidationError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ObservationValidationError(f"cannot load JSON artifact {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(value, dict):
        raise ObservationValidationError(f"JSON artifact {path} must be an object")
    return value


def artifact_paths(data_dir: Path, observation_id: str) -> tuple[Path, Path, Path]:
    if not isinstance(observation_id, str) or not OBSERVATION_ID_RE.fullmatch(observation_id):
        raise ObservationValidationError("observation_id must be canonical lowercase ASCII")
    root = _artifact_root(data_dir)
    paths = (root / f"manifest_{observation_id}.json", root / f"state_{observation_id}.json", root / "current.json")
    if any(path.parent != root for path in paths):
        raise ObservationValidationError("artifact path escapes data directory")
    return paths


def create_plan(data_dir: Path, manifest: Mapping[str, Any], *, now: datetime) -> tuple[Path, Path, Path]:
    validated = validate_manifest(manifest)
    manifest_path, state_path, pointer_path = artifact_paths(data_dir, validated["observation_id"])
    with ControlLock(CONTROL_LOCK_PATH, timeout=0):
        for path in (manifest_path, state_path, pointer_path):
            _safe_artifact_path(path)
        if manifest_path.exists() or state_path.exists():
            raise ObservationConflictError("observation artifacts already exist")
        state = initial_state(validated, now=now)
        published: list[Path] = []
        try:
            _atomic_write_json(manifest_path, validated, overwrite=False)
            published.append(manifest_path)
            _atomic_write_json(state_path, state, overwrite=False)
            published.append(state_path)
            _atomic_write_json(pointer_path, {"observation_id": validated["observation_id"], "manifest_path": str(manifest_path), "state_path": str(state_path), "updated_at": utc_timestamp(now)}, overwrite=True)
        except Exception:
            cleanup_error: OSError | None = None
            for path in reversed(published):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                raise ObservationConflictError("plan publication failed and rollback could not remove partial artifacts") from cleanup_error
            raise
    return manifest_path, state_path, pointer_path


def _transition(data_dir: Path, observation_id: str, expected_state: str, now: datetime, mutate: Callable[[dict[str, Any], dict[str, Any]], None]) -> dict[str, Any]:
    manifest_path, state_path, _pointer_path = artifact_paths(data_dir, observation_id)
    with ControlLock(CONTROL_LOCK_PATH, timeout=0):
        manifest = validate_manifest(load_json(manifest_path))
        current = validate_state(manifest, load_json(state_path))
        if current["state"] != expected_state:
            raise ObservationConflictError(f"expected state {expected_state}, found {current['state']}")
        next_state = dict(current)
        mutate(manifest, next_state)
        if next_state["state"] not in PERMITTED_TRANSITIONS[current["state"]]:
            raise ObservationConflictError(f"invalid lifecycle transition {current['state']} -> {next_state['state']}")
        next_state["transition_sequence"] = current["transition_sequence"] + 1
        next_state["updated_at"] = utc_timestamp(now)
        validate_state(manifest, next_state)
        _atomic_write_json(state_path, next_state, overwrite=True)
        return next_state


def activate_observation(data_dir: Path, observation_id: str, *, now: datetime) -> dict[str, Any]:
    """Explicit lifecycle transition only; it does not start any work."""
    def mutate(manifest: dict[str, Any], state: dict[str, Any]) -> None:
        instant = _parse_timestamp(utc_timestamp(now), "now")
        start = _parse_timestamp(manifest["planned_start_at"], "planned_start_at")
        end = _parse_timestamp(manifest["planned_end_at"], "planned_end_at")
        if instant < start or instant >= end:
            raise ObservationValidationError("activation must occur within the planned observation window")
        state.update({"state": "active", "started_at": utc_timestamp(now), "last_control_verdict": "active_control_only"})
    return _transition(data_dir, observation_id, "planned", now, mutate)


def request_stop(data_dir: Path, observation_id: str, *, reason_category: str, reason: str, now: datetime, evidence: str | None = None) -> dict[str, Any]:
    if reason_category not in STOP_REASON_CATEGORIES or not reason.strip():
        raise ObservationValidationError("valid stop reason category and non-blank reason are required")
    manifest_path, state_path, _pointer = artifact_paths(data_dir, observation_id)
    request = {"category": reason_category, "reason": reason, "requested_at": utc_timestamp(now), "evidence": evidence}
    with ControlLock(CONTROL_LOCK_PATH, timeout=0):
        manifest = validate_manifest(load_json(manifest_path)); current = validate_state(manifest, load_json(state_path))
        if current["state"] == "stop_requested":
            previous = current["stop_reason"]
            same = {key: previous.get(key) for key in ("category", "reason", "evidence")} == {key: request.get(key) for key in ("category", "reason", "evidence")}
            if same: return current
            raise ObservationConflictError("conflicting stop request already exists")
        if current["state"] != "active":
            raise ObservationConflictError("stop request is valid only from active state")
        if _parse_timestamp(utc_timestamp(now), "now") < _parse_timestamp(current["started_at"], "started_at"):
            raise ObservationValidationError("stop request precedes activation")
        if "stop_requested" not in PERMITTED_TRANSITIONS[current["state"]]:
            raise ObservationConflictError("invalid lifecycle transition to stop_requested")
        current.update({"state": "stop_requested", "stop_reason": request, "last_control_verdict": "stop_requested_future_units_must_honor" , "transition_sequence": current["transition_sequence"] + 1, "updated_at": utc_timestamp(now)})
        validate_state(manifest, current); _atomic_write_json(state_path, current, overwrite=True)
        return current


def confirm_stopped(data_dir: Path, observation_id: str, *, now: datetime) -> dict[str, Any]:
    def mutate(_manifest: dict[str, Any], state: dict[str, Any]) -> None:
        state.update({"state": "stopped", "stopped_at": utc_timestamp(now), "last_control_verdict": "stopped"})
    return _transition(data_dir, observation_id, "stop_requested", now, mutate)


def record_checkpoint(data_dir: Path, observation_id: str, *, day: int, now: datetime, report_path: str | None = None, report_sha256: str | None = None, operator_note: str | None = None) -> dict[str, Any]:
    def mutate(manifest: dict[str, Any], state: dict[str, Any]) -> None:
        if state["state"] != "active": raise ObservationConflictError("checkpoints require active state")
        if day not in manifest["checkpoint_days"]: raise ObservationValidationError("undefined checkpoint day")
        due = _parse_timestamp(manifest["planned_start_at"], "planned_start_at") + timedelta(days=day)
        if _parse_timestamp(utc_timestamp(now), "now") < due: raise ObservationValidationError("checkpoint is not due yet")
        if _parse_timestamp(utc_timestamp(now), "now") > authorized_end(manifest, state): raise ObservationValidationError("checkpoint is after the authorized window")
        if any(item["day"] == day for item in state["checkpoints"]): raise ObservationConflictError("checkpoint already recorded")
        state["checkpoints"] = [*state["checkpoints"], {"day": day, "recorded_at": utc_timestamp(now), "report_path": report_path, "report_sha256": report_sha256, "operator_note": operator_note}]
        state["last_control_verdict"] = f"checkpoint_day_{day}_recorded_no_qualification_inference"
    return _transition(data_dir, observation_id, "active", now, mutate)


def authorize_extension(data_dir: Path, observation_id: str, *, days: int, reason: str, now: datetime) -> dict[str, Any]:
    def mutate(manifest: dict[str, Any], state: dict[str, Any]) -> None:
        if days < 1 or days > MAX_EXTENSION_DAYS: raise ObservationValidationError("extension must be one to seven days")
        if _parse_timestamp(utc_timestamp(now), "now") >= authorized_end(manifest, state):
            raise ObservationValidationError("extension must be authorized before the current authorized end")
        if state["extension_authorized_days"] != 0: raise ObservationConflictError("only one extension is permitted")
        if not reason.strip(): raise ObservationValidationError("extension reason is required")
        end = _parse_timestamp(manifest["planned_end_at"], "planned_end_at") + timedelta(days=days)
        state.update({"extension_authorized_days": days, "extension_authorized_at": utc_timestamp(now), "extension_reason": reason, "effective_extended_end_at": utc_timestamp(end), "last_control_verdict": "extension_explicitly_authorized"})
    return _transition(data_dir, observation_id, "active", now, mutate)


def complete_observation(data_dir: Path, observation_id: str, *, now: datetime) -> dict[str, Any]:
    def mutate(manifest: dict[str, Any], state: dict[str, Any]) -> None:
        if state["stop_reason"] is not None: raise ObservationConflictError("stop request requires stopped state, not completion")
        if _parse_timestamp(utc_timestamp(now), "now") < authorized_end(manifest, state): raise ObservationValidationError("cannot complete before authorized end")
        state.update({"state": "completed", "completed_at": utc_timestamp(now), "last_control_verdict": "authorized_window_ended_no_qualification_inference"})
    return _transition(data_dir, observation_id, "active", now, mutate)


def may_run_observation_job(manifest: Mapping[str, Any], state: Mapping[str, Any], now: datetime, job_type: str) -> dict[str, Any]:
    """Return the only control-plane permission decision future jobs should consume."""
    if job_type not in ALLOWED_JOB_TYPES:
        return {"allowed": False, "reason_code": "unsupported_job_type", "explanation": "The requested observation job type is unsupported."}
    try:
        m = validate_manifest(manifest); s = validate_state(m, state); instant = _parse_timestamp(utc_timestamp(now), "now")
    except ObservationValidationError as exc:
        reason = "manifest_hash_mismatch" if "hash mismatch" in str(exc) else ("invalid_manifest" if "manifest" in str(exc) else "invalid_state")
        return {"allowed": False, "reason_code": reason, "explanation": f"Control artifacts are invalid: {exc}"}
    lifecycle = s["state"]
    static = {"planned": "planned_but_inactive", "stop_requested": "stop_requested", "stopped": "stopped", "completed": "completed", "failed_closed": "failed_closed"}
    if lifecycle in static:
        return {"allowed": False, "reason_code": static[lifecycle], "explanation": f"Observation lifecycle is {lifecycle}; no job may run."}
    start = _parse_timestamp(m["planned_start_at"], "planned_start_at")
    if instant < start:
        return {"allowed": False, "reason_code": "before_authorized_window", "explanation": "The authorized observation window has not started."}
    started_at = _parse_timestamp(s["started_at"], "started_at")
    if instant < started_at:
        return {"allowed": False, "reason_code": "not_started", "explanation": "The observation's explicit active state has not started yet."}
    if instant >= authorized_end(m, s):
        return {"allowed": False, "reason_code": "after_authorized_window", "explanation": "The authorized observation window has ended."}
    if job_type == "collection" and "collection" not in m:
        return {"allowed": False, "reason_code": "unsupported_job_type", "explanation": "Collection is not configured."}
    if job_type == "refresh" and "refresh" not in m:
        return {"allowed": False, "reason_code": "unsupported_job_type", "explanation": "Refresh is not configured."}
    return {"allowed": True, "reason_code": "active_within_authorized_window", "explanation": "Control-plane gate permits this future job; it does not start it."}


def control_status(manifest: Mapping[str, Any], state: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    m = validate_manifest(manifest); s = validate_state(m, state); instant = _parse_timestamp(utc_timestamp(now), "now")
    start, end = _parse_timestamp(m["planned_start_at"], "planned_start_at"), authorized_end(m, s)
    elapsed = max(timedelta(), min(instant, end) - start) if instant >= start else timedelta()
    recorded_days = {item["day"] for item in s["checkpoints"]}
    schedule = []
    for day in m["checkpoint_days"]:
        due = start + timedelta(days=day)
        if day in recorded_days:
            checkpoint_status = "recorded"
        elif instant < due:
            checkpoint_status = "upcoming"
        elif instant == due:
            checkpoint_status = "due"
        else:
            checkpoint_status = "overdue"
        schedule.append({"day": day, "due_at": utc_timestamp(due), "status": checkpoint_status})
    pending = [item for item in schedule if item["status"] != "recorded"]
    next_item = pending[0] if pending else None
    collection = may_run_observation_job(m, s, instant, "collection"); refresh = may_run_observation_job(m, s, instant, "refresh")
    return {"control_plane_only": True, "observation_id": m["observation_id"], "lifecycle_state": s["state"], "planned_start_at": m["planned_start_at"], "planned_end_at": m["planned_end_at"], "authorized_end_at": utc_timestamp(end), "actual_started_at": s["started_at"], "stopped_at": s["stopped_at"], "completed_at": s["completed_at"], "elapsed_seconds": int(elapsed.total_seconds()), "remaining_seconds": max(0, int((end - instant).total_seconds())), "next_checkpoint_at": next_item["due_at"] if next_item else None, "next_checkpoint_status": next_item["status"] if next_item else None, "checkpoint_schedule": schedule, "extension_authorized_days": s["extension_authorized_days"], "stop_requested": s["state"] == "stop_requested", "manifest_hash_verified": s["manifest_sha256"] == sha256_json(m), "current_control_verdict": s["last_control_verdict"], "activation_allowed_by_state": s["state"] == "planned", "collection_should_continue": collection, "refresh_should_continue": refresh}
