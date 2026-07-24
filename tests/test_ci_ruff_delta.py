"""Regression tests for the CI Ruff no-new-debt comparator."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from scripts.ci.check_ruff_delta import compare_diagnostics, write_report

_SHIFTED_ROW = 40


def _diagnostic(
    filename: str,
    *,
    code: str = "F401",
    message: str = "unused import",
    row: int = 1,
) -> dict[str, object]:
    return {
        "filename": filename,
        "code": code,
        "message": message,
        "location": {"row": row, "column": 1},
        "end_location": {"row": row, "column": 2},
        "fix": None,
        "noqa_row": row,
        "url": "https://docs.astral.sh/ruff/rules/unused-import",
    }


def test_accepts_unchanged_diagnostics_with_shifted_locations(tmp_path: Path) -> None:
    """Line-number movement must not create false new debt."""
    base_root = tmp_path / "base"
    head_root = tmp_path / "head"
    base = [_diagnostic(str(base_root / "tests" / "example.py"))]
    head = [
        _diagnostic(
            str(head_root / "tests" / "example.py"),
            row=_SHIFTED_ROW,
        )
    ]

    delta = compare_diagnostics(base, head, base_root=base_root, head_root=head_root)

    assert not delta.added
    assert not delta.removed


def test_rejects_a_new_diagnostic(tmp_path: Path) -> None:
    """A fingerprint absent from the base must fail the gate."""
    delta = compare_diagnostics(
        [],
        [_diagnostic("tests/new.py")],
        base_root=tmp_path / "base",
        head_root=tmp_path / "head",
    )
    output = StringIO()

    exit_code = write_report(delta, output)

    assert exit_code == 1
    assert "New diagnostics: 1" in output.getvalue()
    assert "tests/new.py: F401 unused import" in output.getvalue()


def test_accepts_and_reports_removed_debt(tmp_path: Path) -> None:
    """Removing a base diagnostic must pass and appear in the report."""
    delta = compare_diagnostics(
        [_diagnostic("tests/old.py")],
        [],
        base_root=tmp_path / "base",
        head_root=tmp_path / "head",
    )
    output = StringIO()

    exit_code = write_report(delta, output)

    assert exit_code == 0
    assert "Removed diagnostics: 1" in output.getvalue()
    assert "Ruff no-new-debt check passed" in output.getvalue()


def test_duplicate_diagnostics_are_compared_as_a_multiset(tmp_path: Path) -> None:
    """An added duplicate occurrence must not be hidden by an existing one."""
    diagnostic = _diagnostic("tests/repeated.py")

    delta = compare_diagnostics(
        [diagnostic],
        [diagnostic, diagnostic],
        base_root=tmp_path / "base",
        head_root=tmp_path / "head",
    )

    assert delta.added.total() == 1
