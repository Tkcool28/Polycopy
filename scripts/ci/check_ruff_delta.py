#!/usr/bin/env python3
"""Fail CI only when a change adds Ruff diagnostics beyond the base revision.

Ruff's repository-wide baseline is currently non-zero. This comparator treats
that existing diagnostic multiset as debt to preserve or reduce, while rejecting
new diagnostics. Line numbers are intentionally excluded so unchanged findings
remain comparable when nearby edits shift their locations.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


DiagnosticKey = tuple[str, str, str]


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to read Ruff JSON from {path}: {exc}") from exc
    if not isinstance(value, list):
        raise SystemExit(f"Expected a JSON list from Ruff in {path}")
    return value


def _normalize_filename(filename: str, repository_root: Path) -> str:
    candidate = Path(filename)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(repository_root)
        except ValueError:
            pass
    return candidate.as_posix()


def _diagnostic_key(item: dict[str, Any], repository_root: Path) -> DiagnosticKey:
    filename = item.get("filename")
    code = item.get("code")
    message = item.get("message")
    if not all(isinstance(value, str) and value for value in (filename, code, message)):
        raise SystemExit(f"Malformed Ruff diagnostic: {item!r}")
    return (_normalize_filename(filename, repository_root), code, message)


def _counter(items: list[dict[str, Any]], repository_root: Path) -> Counter[DiagnosticKey]:
    return Counter(_diagnostic_key(item, repository_root) for item in items)


def _format(key: DiagnosticKey, count: int) -> str:
    filename, code, message = key
    suffix = f" (x{count})" if count > 1 else ""
    return f"{filename}: {code} {message}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument("--head-root", type=Path, required=True)
    args = parser.parse_args()

    base = _counter(_load(args.base), args.base_root.resolve())
    head = _counter(_load(args.head), args.head_root.resolve())

    added = head - base
    removed = base - head

    print(f"Base Ruff diagnostics: {base.total()}")
    print(f"Head Ruff diagnostics: {head.total()}")
    print(f"Removed diagnostics: {removed.total()}")
    print(f"New diagnostics: {added.total()}")

    if removed:
        print("\nDiagnostics removed by this change:")
        for key, count in sorted(removed.items()):
            print(f"- {_format(key, count)}")

    if added:
        print("\nNew Ruff diagnostics introduced by this change:")
        for key, count in sorted(added.items()):
            print(f"- {_format(key, count)}")
        return 1

    print("\nRuff no-new-debt check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
