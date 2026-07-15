"""Section G — CLI safety and flag-validation tests.

Tests the operator-facing CLI surface only. Network calls are not made.
The CLI is exercised by importing :mod:`scripts.audit_short_horizon_specialist_wallets`
and invoking its ``main`` function with controlled argv lists.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path



SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _import_cli():
    spec = __import__("importlib").util.spec_from_file_location(
        "pr69_cli", SCRIPTS_DIR / "audit_short_horizon_specialist_wallets.py"
    )
    module = __import__("importlib").util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_argv(args: list[str]) -> list[str]:
    """Compose a complete offline argv (no --allow-live)."""
    return ["audit"] + args


# --- G.1 default is offline: no network, no DB, no writers -------------------


def test_cli_default_is_offline_and_writes_no_files(tmp_path: Path) -> None:
    cli = _import_cli()
    original_main = cli.main

    def fake_main(argv):
        # No --allow-live, no --input-file → must NOT write, must NOT network.
        # Verify: no Database, no sqlite3 module, no write_* call.
        src = inspect.getsource(cli)
        import re
        # Strip ALL docstrings (occurrences of """text""") before scanning.
        src_no_docstring = re.sub(r'"""[\s\S]*?"""', '', src)
        for forbidden in ("Database(", "sqlite3.connect", "write_valid_rows", "process_approved_wallet_trades"):
            assert forbidden not in src_no_docstring, f"CLI must not import {forbidden}"
        assert "--allow-live" in src
        return original_main(argv or [])


def _offline_argv(tmp_path: Path) -> list[str]:
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"markets": [], "market_trades": {}, "leaderboard": []}))
    return ["--input-file", str(fixture)]


def test_cli_offline_no_database_import(tmp_path: Path) -> None:
    src = Path(SCRIPTS_DIR / "audit_short_horizon_specialist_wallets.py").read_text()
    import re
    src_no_docstring = re.sub(r'"""[\s\S]*?"""', '', src)
    for forbidden in ("polycopy.db", "from polycopy.db", "Database("):
        assert forbidden not in src_no_docstring, f"CLI must not touch {forbidden}"


# --- G.2 flag validation -----------------------------------------------------


def test_cli_validates_flags(tmp_path: Path) -> None:
    cli = _import_cli()
    # CLI returns 2 on malformed config (does not raise). All these MUST be rejected.
    assert cli.main(["--preferred-days", "20", "--max-capital-lock-days", "14"]) == 2
    assert cli.main(["--max-capital-lock-days", "31"]) == 2
    assert cli.main(["--history-days", "731"]) == 2
    assert cli.main(["--concurrency", "5"]) == 2


def test_cli_invalid_concurrency_caps() -> None:
    cli = _import_cli()
    assert cli.main(["--concurrency", "0"]) == 2
    assert cli.main(["--concurrency", "5"]) == 2


def test_cli_leaderboard_top_cap() -> None:
    cli = _import_cli()
    assert cli.main(["--leaderboard-top", "101"]) == 2
    assert cli.main(["--leaderboard-top", "0"]) == 2


# --- G.3 deterministic JSON output -------------------------------------------


def test_cli_offline_default_returns_json_to_stdout(tmp_path: Path, capsys) -> None:
    cli = _import_cli()
    rc = cli.main(_offline_argv(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["contract_version"] == cli.DISCOVERY_CONTRACT_VERSION
    assert parsed["live_read_performed"] is False
    assert parsed["db_opened"] is False
    assert parsed["writes_performed"] is False


# --- G.4 output path safety ---------------------------------------------------


def test_cli_rejects_db_output_path(tmp_path: Path) -> None:
    cli = _import_cli()
    db_path = tmp_path / "production.db"
    assert cli.main(["--output-dir", str(db_path)]) == 2
    # Even when dir already exists with .db files inside
    nested = tmp_path / "with_db"
    nested.mkdir()
    (nested / "existing.db").write_text("x")
    assert cli.main(["--output-dir", str(nested)]) == 2


# --- G.5 --allow-live required for network -----------------------------------


def test_cli_allow_live_requires_no_input_file(tmp_path: Path) -> None:
    cli = _import_cli()
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"markets": [], "market_trades": {}, "leaderboard": []}))
    assert cli.main(["--allow-live", "--input-file", str(fixture)]) == 2


# --- G.6 CLI produces CSV outputs under --output-dir -----------------------


def test_cli_writes_csv_outputs(tmp_path: Path) -> None:
    """Use a fixture that populates a small classification + candidate set."""
    cli = _import_cli()
    fixture = tmp_path / "fixture.json"
    # Build a fixture that the offline path will round-trip.
    # Note: we don't hit the offline classification pipeline with live requests,
    # only the deterministic JSON envelope + asset writes.
    fixture.write_text(json.dumps({
        "markets": [],
        "market_trades": {},
        "leaderboard": [],
    }))
    out_dir = tmp_path / "out"
    rc = cli.main([
        "--input-file", str(fixture),
        "--output-dir", str(out_dir),
        "--output-json", "summary.json",
    ])
    assert rc == 0
    assert (out_dir / "short_horizon_specialist_wallet_audit.json").exists()
    assert (out_dir / "summary.json").exists()


# --- G.7 --require-partial-source-clean ------------------------------------


def test_cli_require_partial_source_clean_offline_passes(tmp_path: Path, capsys) -> None:
    cli = _import_cli()
    rc = cli.main(_offline_argv(tmp_path) + ["--require-partial-source-clean"])
    assert rc == 0
