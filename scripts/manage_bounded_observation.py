"""Thin, control-plane-only CLI for bounded specialist observation artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from polycopy.observation.bounded_observation import (
    DurabilityConfirmationError,
    ObservationConflictError,
    ObservationValidationError,
    artifact_paths,
    authorize_extension,
    complete_observation,
    control_status,
    create_plan,
    load_json,
    make_manifest,
    record_checkpoint,
    request_stop,
    validate_manifest,
    validate_state,
)
from polycopy.observation.control_lock import ControlLockError


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO-8601 timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must be timezone-aware")
    return parsed


def _paths(args: argparse.Namespace) -> tuple[dict, dict]:
    manifest_path, state_path, _ = artifact_paths(args.data_dir, args.observation_id)
    manifest = validate_manifest(load_json(manifest_path))
    return manifest, validate_state(manifest, load_json(state_path))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Control-plane-only bounded observation manager; never starts jobs.")
    root.add_argument("--data-dir", type=Path, default=ROOT / "data" / "specialist_observation")
    sub = root.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Write an immutable planned manifest and initial state.")
    plan.add_argument("--observation-id", required=True)
    plan.add_argument("--start-at", required=True, type=_timestamp)
    plan.add_argument("--watch-id", action="append", required=True)
    plan.add_argument("--database-path", required=True)
    plan.add_argument("--repository-baseline-sha", required=True)
    plan.add_argument("--refresh-market-limit", required=True, type=int)
    for name in ("status", "validate", "request-stop", "checkpoint", "complete", "authorize-extension", "confirm-stopped"):
        command = sub.add_parser(name)
        command.add_argument("--observation-id", required=True)
    stop = sub.choices["request-stop"]
    stop.add_argument("--reason-category", required=True)
    stop.add_argument("--reason", required=True)
    stop.add_argument("--evidence")
    checkpoint = sub.choices["checkpoint"]
    checkpoint.add_argument("--day", required=True, type=int)
    checkpoint.add_argument("--report-path")
    checkpoint.add_argument("--report-sha256")
    checkpoint.add_argument("--operator-note")
    extension = sub.choices["authorize-extension"]
    extension.add_argument("--days", required=True, type=int)
    extension.add_argument("--reason", required=True)
    return root


def _emit(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    now = _now()
    try:
        if args.command == "plan":
            manifest = make_manifest(observation_id=args.observation_id, created_at=now, planned_start_at=args.start_at, watch_ids=args.watch_id, database_path=args.database_path, repository_baseline_sha=args.repository_baseline_sha, refresh_market_limit=args.refresh_market_limit)
            manifest_path, state_path, pointer_path = create_plan(args.data_dir, manifest, now=now)
            _emit({"control_plane_only": True, "manifest_path": str(manifest_path), "state_path": str(state_path), "pointer_path": str(pointer_path), "activated": False})
        elif args.command == "status":
            manifest, state = _paths(args); _emit(control_status(manifest, state, now=now))
        elif args.command == "validate":
            manifest, state = _paths(args); _emit({"valid": True, "observation_id": manifest["observation_id"], "manifest_sha256": state["manifest_sha256"], "control_plane_only": True})
        elif args.command == "request-stop":
            _emit(request_stop(args.data_dir, args.observation_id, reason_category=args.reason_category, reason=args.reason, evidence=args.evidence, now=now))
        elif args.command == "checkpoint":
            _emit(record_checkpoint(args.data_dir, args.observation_id, day=args.day, report_path=args.report_path, report_sha256=args.report_sha256, operator_note=args.operator_note, now=now))
        elif args.command == "authorize-extension":
            _emit(authorize_extension(args.data_dir, args.observation_id, days=args.days, reason=args.reason, now=now))
        elif args.command == "complete":
            _emit(complete_observation(args.data_dir, args.observation_id, now=now))
        else:
            from polycopy.observation.bounded_observation import confirm_stopped
            _emit(confirm_stopped(args.data_dir, args.observation_id, now=now))
        return 0
    except (ObservationValidationError, ObservationConflictError, ControlLockError, DurabilityConfirmationError) as exc:
        _emit({"valid": False, "error": str(exc), "control_plane_only": True})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
