"""Regression guard for the bounded source-trade mutation architecture."""
from __future__ import annotations

import copy
import gc
import importlib.util
import json
import sqlite3
import weakref
from pathlib import Path
from types import SimpleNamespace

from polycopy.engine.source_trade_sql_architecture import (
    SqlFinding,
    contract_violations,
    scan_python_file,
    scan_repository,
)
from polycopy.ingestion.canonical_metadata import _CanonicalMergeMetadata, merge_canonical_metadata
from polycopy.ingestion.source_trade_metadata_reconciliation import (
    reconcile_metadata_json,
    serialize_canonical_merge_metadata,
)

ROOT = Path(__file__).resolve().parents[1]


def test_source_trade_sql_roles_are_exactly_allowlisted() -> None:
    """Executable production SQL cannot create a new writer by accident."""
    findings = scan_repository(ROOT)
    assert contract_violations(findings) == []
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



def test_sql_guard_rejects_indirected_and_dynamic_source_trade_mutations(tmp_path: Path) -> None:
    cases = {
        "literal": 'conn.execute("UPDATE source_trades SET metadata_json=?")',
        "variable": 'sql = "DELETE FROM source_trades"\n    conn.execute(sql)',
        "concat": 'sql = "INSERT " + "OR IGNORE INTO source_trades VALUES (?)"\n    conn.execute(sql)',
        "fstring": 'sql = f"REPLACE INTO {\'source_trades\'} VALUES (?)"\n    conn.execute(sql)',
        "multiline": 'sql = """\\n UPDATE   source_trades SET metadata_json=?\\n """\n    conn.execute(sql)',
        "many": 'sql = "INSERT INTO source_trades VALUES (?)"\n    conn.executemany(sql, [])',
        "script": 'conn.executescript("DELETE FROM source_trades;")',
        "cursor": 'cursor = conn.cursor()\n    cursor.execute("UPDATE source_trades SET metadata_json=?")',
        "keyword": 'db.execute(sql="DELETE FROM source_trades")',
        "wrapper": 'sql = "DELETE FROM source_trades"\n    db.execute(sql)',
        "helper_sql": 'def sql_text():\n        return "UPDATE source_trades SET metadata_json=?"\n    conn.execute(sql_text())',
        "helper_execute": 'def write():\n        db.execute("INSERT OR REPLACE INTO source_trades VALUES (?)")\n    write()',
        "mixed_case": 'conn.execute("  iNsErT OR IgNoRe INTO source_trades VALUES (?)")',
        "dynamic": 'sql = user_supplied + " UPDATE source_trades"\n    conn.execute(sql)',
    }
    for name, body in cases.items():
        path = tmp_path / f"{name}.py"
        path.write_text(f"def run(conn, db, user_supplied):\n    {body}\n")
        findings = scan_python_file(path)
        assert findings, name
        if name == "dynamic":
            assert len(findings) == 1 and not findings[0].resolved


def test_shared_scanner_respects_scope_order_aliases_and_source_only_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "scanner_bypasses.py"
    path.write_text(
        """
def forward(sql):
    return sql

def run(conn, user_sql, unrelated_sql):
    table = "source_trades"
    sql = f"UPDATE {table} SET metadata_json=? WHERE id=?"
    run_sql = getattr(conn, "execute")
    run_sql(sql)
    sql = "SELECT 1"
    run_sql(sql)
    sql = user_sql + " DELETE FROM source_trades"
    conn.execute(query=sql)
    conn.execute(unrelated_sql)
    conn.execute(statement=forward("INSERT OR IGNORE INTO source_trades (id) VALUES (?)"))

class Nested:
    def delete(self, conn):
        conn.execute("DELETE FROM source_trades")
"""
    )
    findings = scan_python_file(path)
    assert [(finding.resolved, finding.operation, finding.sink) for finding in findings] == [
        (True, "UPDATE", "execute"),
        (False, None, "execute"),
        (True, "INSERT OR IGNORE", "execute"),
        (True, "DELETE", "execute"),
    ]
    assert findings[1].reason == "unresolved_source_trade_sql"
    assert [finding.scope for finding in findings] == ["run", "run", "run", "Nested.delete"]



def test_scanner_follows_local_execution_wrappers_and_control_headers(tmp_path: Path) -> None:
    path = tmp_path / "wrappers.py"
    path.write_text(
        '''
def execute_sql(db, statement):
    run = db.execute
    run(statement)

def level_two(db, statement):
    execute_sql(db=db, statement=statement)

def run(db, unsafe):
    sql = "DELETE FROM source_trades"
    level_two(db, sql)
    if db.execute("UPDATE source_trades SET side='SELL'"):
        pass
    db.execute("SELECT 1")
    level_two(db, unsafe + " DELETE FROM source_trades")
'''
    )
    findings = scan_python_file(path)
    assert [(f.scope, f.sink, f.resolved, f.operation) for f in findings] == [
        ("run", "execute", True, "DELETE"),
        ("run", "execute", True, "UPDATE"),
        ("run", "execute", False, None),
    ]
    assert findings[-1].reason == "unresolved_source_trade_sql"
    assert contract_violations(findings) == findings
    writer_columns = (
        "id", "source", "source_trade_id", "market_source_id", "side", "outcome",
        "quantity", "price", "trader_address", "timestamp", "is_sample", "token_id", "metadata_json",
    )
    valid = [
        SqlFinding("src/polycopy/ingestion/source_trade_writer.py", "write_valid_rows", 1, "execute", True, "INSERT OR IGNORE", "source_trades", writer_columns, ()),
        SqlFinding("src/polycopy/ingestion/source_trade_metadata_reconciliation.py", "reconcile_metadata_json", 1, "execute", True, "UPDATE", "source_trades", ("metadata_json",), ("id",)),
        SqlFinding("src/polycopy/ingestion/source_trade_resolution.py", "apply_existing_resolution_updates", 1, "execute", True, "UPDATE", "source_trades", ("resolution_status", "resolved_at", "winning_token_id", "is_winning_trade", "realized_pnl", "settlement_source"), ("id",)),
    ]
    assert contract_violations(valid) == []
    invalid = [
        valid[0].__class__(**{**valid[0].__dict__, "operation": "INSERT"}),
        valid[1].__class__(**{**valid[1].__dict__, "columns": ("metadata_json", "side")}),
        valid[2].__class__(**{**valid[2].__dict__, "selector_columns": ("source",)}),
    ]
    assert contract_violations(invalid) == invalid


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
    merged, _status, _reasons = merge_canonical_metadata(
        None,
        {"conditionId": "x", "category": "Politics", "events": [], "series": []},
        condition_id="x",
    )
    assert reconcile_metadata_json(
        db, merged, internal_id="missing", allow_nonempty_replace=True
    ).status == "missing"
    db.conn.execute("INSERT INTO source_trades VALUES ('row', 's', 'trade', '{}')")
    assert reconcile_metadata_json(
        db, merged, internal_id="row", allow_nonempty_replace=True
    ).status == "updated"
    assert "condition_id" in db.conn.execute("SELECT metadata_json FROM source_trades").fetchone()[0]



def test_canonical_merge_authority_is_immutable_and_captured() -> None:
    db = _db()
    db.conn.execute("INSERT INTO source_trades VALUES ('row', 's', 'trade', '{\"prior\":true}')")
    merged, status, _ = merge_canonical_metadata(
        None,
        {"conditionId": "x", "category": "Politics", "events": [], "series": []},
        condition_id="x",
    )
    assert status == "filled"
    expected = dict(merged)
    with __import__("pytest").raises(TypeError):
        merged["forged"] = True  # type: ignore[index]
    inspection = merged["_snapshot"]
    inspection["provenance"]["provider"] = "forged"
    shallow, deep = copy.copy(merged), copy.deepcopy(merged)
    assert shallow is merged
    assert deep is merged
    for reconstructed in (dict(merged), json.loads(json.dumps(dict(merged)))):
        with __import__("pytest").raises(ValueError):
            reconcile_metadata_json(db, reconstructed, internal_id="row", allow_nonempty_replace=True)
    assert reconcile_metadata_json(db, merged, internal_id="row", allow_nonempty_replace=True).status == "updated"
    assert reconcile_metadata_json(db, shallow, internal_id="row", allow_nonempty_replace=True).status == "reused"
    assert reconcile_metadata_json(db, deep, internal_id="row", allow_nonempty_replace=True).status == "reused"
    saved = db.conn.execute("SELECT metadata_json FROM source_trades WHERE id='row'").fetchone()[0]
    assert saved == json.dumps(expected, sort_keys=True, separators=(",", ":"))
    assert "forged" not in saved


def test_canonical_merge_authority_cannot_be_directly_constructed() -> None:
    with __import__("pytest").raises(TypeError):
        _CanonicalMergeMetadata({"forged": True}, _token=object())
    with __import__("pytest").raises(TypeError):
        serialize_canonical_merge_metadata({"forged": True})  # type: ignore[arg-type]


def test_arbitrary_mapping_cannot_forge_canonical_merge_authority() -> None:
    db = _db()
    db.conn.execute("INSERT INTO source_trades VALUES ('row', 's', 'trade', '{\"authoritative\":true}')")
    forged = {"authoritative": False}
    try:
        reconcile_metadata_json(db, forged, internal_id="row", allow_nonempty_replace=True)
    except ValueError:
        pass
    else:
        raise AssertionError("ordinary mapping authorized non-empty replacement")
    assert db.conn.execute("SELECT metadata_json FROM source_trades WHERE id='row'").fetchone()[0] == '{"authoritative":true}'


def test_issued_merge_authority_rejects_unregistered_forgeries_and_is_weak() -> None:
    import polycopy.ingestion.canonical_metadata as metadata_module

    assert not hasattr(metadata_module, "_MERGED_METADATA_TOKEN")
    assert not hasattr(metadata_module, "_issue_canonical_merge_metadata")
    assert not hasattr(metadata_module, "_merge_issue_at_definition")
    with __import__("pytest").raises(TypeError):
        _CanonicalMergeMetadata({"forged": True})
    forged = object.__new__(_CanonicalMergeMetadata)
    forged._serialized = '{"forged":true}'
    forged._view = {}
    with __import__("pytest").raises(TypeError):
        serialize_canonical_merge_metadata(forged)

    issued, status, _ = merge_canonical_metadata(
        None, {"conditionId": "x", "events": [], "series": []}, condition_id="x"
    )
    assert status == "filled"
    assert copy.copy(issued) is issued and copy.deepcopy(issued) is issued
    ref = weakref.ref(issued)
    del issued
    gc.collect()
    assert ref() is None


def test_merge_authority_subclass_spoof_and_reconstruction_cannot_replace() -> None:
    db = _db()
    db.conn.execute("INSERT INTO source_trades VALUES ('row', 's', 'trade', '{\"prior\":true}')")

    class ForgedAuthority(_CanonicalMergeMetadata):  # type: ignore[misc]
        pass

    forged = object.__new__(ForgedAuthority)
    forged._serialized = '{"forged":true}'
    forged._view = {"forged": True}
    for candidate in (forged, {"forged": True}, json.loads('{"forged":true}')):
        with __import__("pytest").raises(ValueError):
            reconcile_metadata_json(
                db, candidate, internal_id="row", allow_nonempty_replace=True
            )
    assert db.conn.execute("SELECT metadata_json FROM source_trades WHERE id='row'").fetchone()[0] == '{"prior":true}'


def test_seed_demo_callable_rejects_every_production_alias_before_mutation() -> None:
    spec = importlib.util.spec_from_file_location("seed_demo_data", ROOT / "scripts" / "seed_demo_data.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class ProtectedDb:
        def __init__(self, path: Path) -> None:
            self.db_path = path
            self.mutations: list[str] = []

        def execute(self, sql: str, *args: object) -> None:
            self.mutations.append(sql)
            raise AssertionError("seed attempted mutation before protection")

    for alias in (module.PRODUCTION_DB_PATH, module.REAL_PRODUCTION_DB_PATH):
        for force in (False, True):
            db = ProtectedDb(alias)
            try:
                module.seed_demo_data(db, force=force)
            except ValueError:
                pass
            else:
                raise AssertionError(f"protected alias accepted: {alias}")
            assert db.mutations == []


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
