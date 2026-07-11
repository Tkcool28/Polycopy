#!/usr/bin/env python3
# ruff: noqa: E402, E701, E702
"""Run the deterministic local approved-wallet monitor (0 GREEN, 1 YELLOW, 2 RED)."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from polycopy.monitoring.approved_wallet_monitor import MonitorConfig, SystemProbe, build_baseline, evaluate, render_text, write_json_atomic, write_text_atomic


def set_runtime_override(config: MonitorConfig, field: str, candidate: Path, parser: argparse.ArgumentParser) -> None:
    """Permit test overrides only inside that test database's runtime directory."""
    target = candidate.expanduser().resolve(strict=False)
    runtime_dir = config.data_dir.resolve(strict=False)
    permitted_names = {
        "approved_wallet_monitor_latest.json",
        "approved_wallet_monitor_latest.txt",
        "approved_wallet_monitor_events.jsonl",
    }
    if target.parent != runtime_dir or target.name not in permitted_names or target.is_symlink():
        parser.error("monitor artifact overrides must be non-symlink canonical monitor files in the runtime directory")
    setattr(config, field, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print complete JSON status")
    parser.add_argument("--no-remediation", action="store_true", help="never call systemctl disable")
    parser.add_argument("--initialize-baseline", action="store_true", help="write one deliberate baseline")
    parser.add_argument("--force-baseline", action="store_true", help="allow replacing existing baseline")
    parser.add_argument("--status-json-path", type=Path)
    parser.add_argument("--status-text-path", type=Path)
    parser.add_argument("--events-path", type=Path)
    parser.add_argument("--db-path", type=Path)
    args = parser.parse_args(argv)
    config = MonitorConfig()
    if args.db_path is not None:
        config.db_path = args.db_path.expanduser().resolve(strict=False)
        # Test/preview DB overrides receive an isolated sibling runtime state;
        # they cannot cause production monitor artifacts to be overwritten.
        config.data_dir = config.db_path.parent
        config.marker_path = config.data_dir / ".pr24z_canonical_migration_complete"
        config.baseline_path = config.data_dir / "approved_wallet_monitor_baseline.json"
        config.state_path = config.data_dir / "approved_wallet_monitor_state.json"
        config.status_json_path = config.data_dir / "approved_wallet_monitor_latest.json"
        config.status_text_path = config.data_dir / "approved_wallet_monitor_latest.txt"
        config.events_path = config.data_dir / "approved_wallet_monitor_events.jsonl"
    for field, value in (("status_json_path", args.status_json_path), ("status_text_path", args.status_text_path), ("events_path", args.events_path)):
        if value is not None: set_runtime_override(config, field, value, parser)
    probe = SystemProbe()
    if args.initialize_baseline:
        if config.baseline_path.exists() and not args.force_baseline:
            parser.error(f"baseline exists: {config.baseline_path}; refuse overwrite without --force-baseline")
        try:
            write_json_atomic(config.baseline_path, build_baseline(probe, config))
        except ValueError as exc:
            print(f"baseline refused: {exc}", file=sys.stderr); return 2
        print(json.dumps({"baseline": str(config.baseline_path), "created": True}, sort_keys=True))
        return 0
    report = evaluate(probe, config, no_remediation=args.no_remediation)
    write_json_atomic(config.status_json_path, report)
    write_text_atomic(config.status_text_path, render_text(report))
    if args.json: print(json.dumps(report, sort_keys=True))
    else: print(render_text(report), end="")
    return {"GREEN": 0, "YELLOW": 1, "RED": 2}[report["status"]]

if __name__ == "__main__": raise SystemExit(main())
