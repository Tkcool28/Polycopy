from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_ruff_delta.py"


def _diagnostic(filename: str, code: str = "F401", message: str = "unused import") -> dict:
    return {
        "filename": filename,
        "code": code,
        "message": message,
        "location": {"row": 1, "column": 1},
        "end_location": {"row": 1, "column": 2},
        "fix": None,
        "noqa_row": 1,
        "url": "https://docs.astral.sh/ruff/rules/unused-import",
    }


def _run(tmp_path: Path, base: list[dict], head: list[dict]) -> subprocess.CompletedProcess[str]:
    base_path = tmp_path / "base.json"
    head_path = tmp_path / "head.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    head_path.write_text(json.dumps(head), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--base",
            str(base_path),
            "--head",
            str(head_path),
            "--base-root",
            str(tmp_path / "base-root"),
            "--head-root",
            str(tmp_path / "head-root"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_accepts_unchanged_diagnostics_with_shifted_locations(tmp_path: Path) -> None:
    base = [_diagnostic("tests/example.py")]
    head = [_diagnostic("tests/example.py")]
    head[0]["location"]["row"] = 40

    result = _run(tmp_path, base, head)

    assert result.returncode == 0
    assert "New diagnostics: 0" in result.stdout


def test_rejects_a_new_diagnostic(tmp_path: Path) -> None:
    result = _run(tmp_path, [], [_diagnostic("tests/new.py")])

    assert result.returncode == 1
    assert "New diagnostics: 1" in result.stdout
    assert "tests/new.py: F401 unused import" in result.stdout


def test_accepts_and_reports_removed_debt(tmp_path: Path) -> None:
    result = _run(tmp_path, [_diagnostic("tests/old.py")], [])

    assert result.returncode == 0
    assert "Removed diagnostics: 1" in result.stdout
    assert "Ruff no-new-debt check passed" in result.stdout


def test_duplicate_diagnostics_are_compared_as_a_multiset(tmp_path: Path) -> None:
    diagnostic = _diagnostic("tests/repeated.py")
    result = _run(tmp_path, [diagnostic], [diagnostic, diagnostic])

    assert result.returncode == 1
    assert "New diagnostics: 1" in result.stdout
