"""Regression guard for the bounded source-trade mutation architecture."""
from __future__ import annotations

import ast
import importlib.util
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from polycopy.ingestion.source_trade_metadata_reconciliation import (
    reconcile_metadata_json,
    trusted_merged_metadata_json,
)

ROOT = Path(__file__).resolve().parents[1]
_MUTATION = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+(?:IGNORE|REPLACE))?|REPLACE|UPDATE|DELETE)\b[\s\S]{0,100}\bsource_trades\b",
    re.IGNORECASE,
)


def _production_sql_mutations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if not isinstance(node.args[0].value, str):
            continue
        sql = node.args[0].value
        if _MUTATION.search(sql):
            out.append(" ".join(sql.split()))
    return out


def test_source_trade_sql_roles_are_exactly_allowlisted() -> None:
    """Executable production SQL cannot create a new writer by accident."""
    allowed = {
        "src/polycopy/ingestion/source_trade_writer.py": {"INSERT"},
        "src/polycopy/ingestion/source_trade_metadata_reconciliation.py": {"UPDATE"},
        "src/polycopy/ingestion/source_trade_resolution.py": {"UPDATE"},
        "src/polycopy/db/schema.py": {"INSERT", "UPDATE", "DELETE", "REPLACE"},
        "src/polycopy/migrations/pr24z_canonical_identity.py": {"UPDATE"},
        "scripts/seed_demo_data.py": {"INSERT", "UPDATE", "DELETE", "REPLACE"},
        "scripts/live_smoke_pr3_fixes.py": {"INSERT"},
    }
    discovered: dict[str, list[str]] = {}
    for base in (ROOT / "src", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            statements = _production_sql_mutations(path)
            if statements:
                discovered[str(path.relative_to(ROOT))] = statements
    assert set(discovered) <= set(allowed), discovered
    for rel, statements in discovered.items():
        for statement in statements:
            kind = statement.split()[0].upper()
            assert kind in allowed[rel], f"{rel}: unauthorized {kind}: {statement}"
    writer_source = (ROOT / "src/polycopy/ingestion/source_trade_writer.py").read_text()
    assert "INSERT OR IGNORE INTO source_trades" in writer_source
    for rel in ("scripts/run_scan.py", "scripts/collect_smart_money_data.py"):
        source = (ROOT / rel).read_text()
        assert "INSERT OR IGNORE INTO source_trades" not in source
        assert "write_valid_rows" in source
    resolution_backfill = (ROOT / "scripts/backfill_resolution_truth.py").read_text()
    assert "UPDATE source_trades" not in resolution_backfill
    assert "apply_existing_resolution_updates" in resolution_backfill
    # The demo/smoke exceptions are deliberately non-production only.
    for rel in ("scripts/seed_demo_data.py", "scripts/live_smoke_pr3_fixes.py"):
        source = (ROOT / rel).read_text()
        assert "_require_disposable_db" in source
        assert "production database" in source


def _db() -> SimpleNamespace:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE source_trades (id TEXT PRIMARY KEY, source TEXT, source_trade_id TEXT, metadata_json TEXT, UNIQUE(source, source_trade_id))"
    )
    return SimpleNamespace(conn=conn)


def test_metadata_reconcilers_are_existing_row_only_and_fail_closed() -> None:
    db = _db()
    forged = {
        "metadata_version": "1", "taxonomy": {}, "event": {}, "series": {},
        "_snapshot": {"provenance": {"provider": "gamma", "exact_match": True}},
    }
    assert reconcile_metadata_json(db, forged, source="s", source_trade_id="missing").status == "missing"
    db.conn.execute("INSERT INTO source_trades VALUES ('row', 's', 'trade', NULL)")
    result = reconcile_metadata_json(db, forged, source="s", source_trade_id="trade")
    assert result.status == "updated"
    saved = db.conn.execute("SELECT metadata_json FROM source_trades WHERE id='row'").fetchone()[0]
    assert "_snapshot" not in saved and "exact_match" not in saved
    # Non-empty evidence is protected from an ordinary mapping downgrade.
    assert reconcile_metadata_json(db, {"eventId": "weaker"}, internal_id="row").status == "conflict"


def test_trusted_merge_can_update_existing_row_but_never_create_one() -> None:
    db = _db()
    merged = trusted_merged_metadata_json({"metadata_version": "1", "_snapshot": {"market": {"condition_id": "x"}}})
    assert reconcile_metadata_json(
        db, merged, internal_id="missing", allow_nonempty_replace=True
    ).status == "missing"
    db.conn.execute("INSERT INTO source_trades VALUES ('row', 's', 'trade', '{}')")
    assert reconcile_metadata_json(
        db, merged, internal_id="row", allow_nonempty_replace=True
    ).status == "updated"
    assert "condition_id" in db.conn.execute("SELECT metadata_json FROM source_trades").fetchone()[0]


def test_demo_and_smoke_production_rejection_is_explicit() -> None:
    for filename in ("seed_demo_data.py", "live_smoke_pr3_fixes.py"):
        spec = importlib.util.spec_from_file_location(filename, ROOT / "scripts" / filename)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            module._require_disposable_db(Path("/root/Polycopy/data/polycopy.db"))
        except ValueError:
            pass
        else:  # pragma: no cover - assertion message is the proof
            raise AssertionError(f"{filename} accepted the canonical production DB")
