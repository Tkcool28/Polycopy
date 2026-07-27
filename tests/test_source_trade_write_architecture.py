"""Regression guard for the bounded source-trade mutation architecture."""
from __future__ import annotations

import ast
import importlib.util
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from polycopy.ingestion.canonical_metadata import merge_canonical_metadata
from polycopy.ingestion.source_trade_metadata_reconciliation import reconcile_metadata_json

ROOT = Path(__file__).resolve().parents[1]
_MUTATION = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+(?:IGNORE|REPLACE))?|REPLACE|UPDATE|DELETE)\b[\s\S]{0,100}\bsource_trades\b",
    re.IGNORECASE,
)


def _production_sql_mutations(path: Path) -> list[str]:
    """Resolve simple local SQL data flow; unresolved execution fails closed."""
    tree = ast.parse(path.read_text(), filename=str(path))
    values: dict[str, str | None] = {}
    origins: dict[str, ast.AST] = {}
    helpers: dict[str, ast.AST] = {}

    def resolve(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = resolve(node.left), resolve(node.right)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    rendered = resolve(value.value)
                    if rendered is None:
                        return None
                    parts.append(rendered)
                else:
                    return None
            return "".join(parts)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.args:
            returned = helpers.get(node.func.id)
            return resolve(returned) if returned is not None else None
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = getattr(node, "value", None)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None:
                for target in targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = resolve(value)
                        origins[target.id] = value
        elif isinstance(node, ast.FunctionDef) and len(node.body) == 1 and isinstance(node.body[0], ast.Return):
            helpers[node.name] = node.body[0].value

    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        method = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if method not in {"execute", "executemany"}:
            continue
        sql = resolve(node.args[0])
        if sql is None:
            # The architecture guard is source-trade scoped. Dynamic SQL that
            # names the protected table is unsafe because it cannot be proven
            # to be one of the narrow authorized operations.
            origin = origins.get(node.args[0].id, node.args[0]) if isinstance(node.args[0], ast.Name) else node.args[0]
            expression = ast.unparse(origin)
            if "source_trades" in expression.lower() and _MUTATION.search(expression):
                out.append("UNRESOLVED_SQL")
        elif _MUTATION.search(sql):
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



def test_sql_guard_rejects_indirected_and_dynamic_source_trade_mutations(tmp_path: Path) -> None:
    cases = {
        "literal": 'conn.execute("UPDATE source_trades SET metadata_json=?")',
        "variable": 'sql = "DELETE FROM source_trades"\n    conn.execute(sql)',
        "concat": 'sql = "INSERT " + "OR IGNORE INTO source_trades VALUES (?)"\n    conn.execute(sql)',
        "fstring": 'sql = f"REPLACE INTO {\'source_trades\'} VALUES (?)"\n    conn.execute(sql)',
        "multiline": 'sql = """\\n UPDATE   source_trades SET metadata_json=?\\n """\n    conn.execute(sql)',
        "many": 'sql = "INSERT INTO source_trades VALUES (?)"\n    conn.executemany(sql, [])',
        "wrapper": 'sql = "DELETE FROM source_trades"\n    db.execute(sql)',
        "helper_sql": 'def sql_text():\n        return "UPDATE source_trades SET metadata_json=?"\n    conn.execute(sql_text())',
        "helper_execute": 'def write():\n        db.execute("INSERT OR REPLACE INTO source_trades VALUES (?)")\n    write()',
        "mixed_case": 'conn.execute("  iNsErT OR IgNoRe INTO source_trades VALUES (?)")',
        "dynamic": 'sql = user_supplied + " UPDATE source_trades"\n    conn.execute(sql)',
    }
    for name, body in cases.items():
        path = tmp_path / f"{name}.py"
        path.write_text(f"def run(conn, db, user_supplied):\n    {body}\n")
        findings = _production_sql_mutations(path)
        assert findings, name
        if name == "dynamic":
            assert findings == ["UNRESOLVED_SQL"]


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
