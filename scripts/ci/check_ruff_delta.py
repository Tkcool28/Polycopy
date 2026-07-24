#!/usr/bin/env python3
"""Reject Ruff diagnostics added beyond the pull request's exact base revision.

The repository currently has pre-existing Ruff debt. This module compares the
base and head diagnostic multisets by normalized filename, rule code, and
message. Source locations are deliberately excluded so nearby edits cannot turn
an unchanged diagnostic into a false addition.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from typing import TextIO

Diagnostic: TypeAlias = Mapping[str, object]
DiagnosticKey: TypeAlias = tuple[str, str, str]
DiagnosticCounter: TypeAlias = Counter[DiagnosticKey]

_EXIT_SUCCESS = 0
_EXIT_FAILURE = 1


@dataclass(frozen=True, slots=True)
class RuffDelta:
    """Normalized base, head, added, and removed diagnostic multisets."""

    base: DiagnosticCounter
    head: DiagnosticCounter
    added: DiagnosticCounter
    removed: DiagnosticCounter


def load_diagnostics(path: Path) -> list[dict[str, object]]:
    """Load and minimally validate Ruff's JSON diagnostic list."""
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        detail = f"Unable to read Ruff JSON from {path}: {exc}"
        raise SystemExit(detail) from exc

    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        detail = f"Expected a JSON list of objects from Ruff in {path}"
        raise SystemExit(detail)
    return value


def normalize_filename(filename: str, repository_root: Path) -> str:
    """Return a stable repository-relative filename when possible."""
    candidate = Path(filename)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(repository_root)
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix()


def diagnostic_key(item: Diagnostic, repository_root: Path) -> DiagnosticKey:
    """Convert one Ruff JSON object into its stable comparison fingerprint."""
    values = tuple(item.get(field) for field in ("filename", "code", "message"))
    if not all(isinstance(value, str) and value for value in values):
        detail = f"Malformed Ruff diagnostic: {item!r}"
        raise SystemExit(detail)
    filename, code, message = values
    return (normalize_filename(filename, repository_root), code, message)


def diagnostic_counter(
    items: Sequence[Diagnostic], repository_root: Path
) -> DiagnosticCounter:
    """Count normalized diagnostic fingerprints, preserving duplicates."""
    return Counter(diagnostic_key(item, repository_root) for item in items)


def compare_diagnostics(
    base_items: Sequence[Diagnostic],
    head_items: Sequence[Diagnostic],
    *,
    base_root: Path,
    head_root: Path,
) -> RuffDelta:
    """Compare base and head diagnostics as multisets."""
    base = diagnostic_counter(base_items, base_root.resolve())
    head = diagnostic_counter(head_items, head_root.resolve())
    return RuffDelta(base=base, head=head, added=head - base, removed=base - head)


def format_diagnostic(key: DiagnosticKey, count: int) -> str:
    """Format one diagnostic fingerprint for the CI report."""
    filename, code, message = key
    suffix = f" (x{count})" if count > 1 else ""
    return f"{filename}: {code} {message}{suffix}"


def report_lines(delta: RuffDelta) -> list[str]:
    """Build the human-readable CI report without writing global output."""
    lines = [
        f"Base Ruff diagnostics: {delta.base.total()}",
        f"Head Ruff diagnostics: {delta.head.total()}",
        f"Removed diagnostics: {delta.removed.total()}",
        f"New diagnostics: {delta.added.total()}",
    ]

    if delta.removed:
        lines.extend(("", "Diagnostics removed by this change:"))
        lines.extend(
            f"- {format_diagnostic(key, count)}"
            for key, count in sorted(delta.removed.items())
        )

    if delta.added:
        lines.extend(("", "New Ruff diagnostics introduced by this change:"))
        lines.extend(
            f"- {format_diagnostic(key, count)}"
            for key, count in sorted(delta.added.items())
        )
    else:
        lines.extend(("", "Ruff no-new-debt check passed."))
    return lines


def write_report(delta: RuffDelta, stream: TextIO) -> int:
    """Write the report and return the appropriate process exit code."""
    stream.write("\n".join(report_lines(delta)))
    stream.write("\n")
    return _EXIT_FAILURE if delta.added else _EXIT_SUCCESS


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the Ruff delta comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--head-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line Ruff diagnostic comparison."""
    args = parse_args(argv)
    delta = compare_diagnostics(
        load_diagnostics(args.base),
        load_diagnostics(args.head),
        base_root=args.base_root,
        head_root=args.head_root,
    )
    return write_report(delta, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
