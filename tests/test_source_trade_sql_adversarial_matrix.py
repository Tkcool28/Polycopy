"""Adversarial bounded-language tests for the source-trade SQL scanner."""
from __future__ import annotations

from pathlib import Path

import pytest

from polycopy.engine.source_trade_sql_architecture import scan_python_file


def _scan(tmp_path: Path, name: str, source: str):
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    return scan_python_file(path)


def _one(findings, *, scope: str, sink: str, resolved: bool, operation: str | None, reason: str | None = None) -> None:
    assert len(findings) == 1
    finding = findings[0]
    assert finding.scope == scope
    assert finding.sink == sink
    assert finding.resolved is resolved
    assert finding.operation == operation
    assert finding.table == "source_trades"
    assert finding.reason == reason


@pytest.mark.parametrize(
    ("name", "source", "resolved", "operation", "reason"),
    [
        ("positional", "def write(db, sql):\n    db.execute(sql)\ndef caller(db):\n    write(db, 'DELETE FROM source_trades')\n", True, "DELETE", None),
        ("keyword", "def write(db, sql):\n    db.execute(sql)\ndef caller(db):\n    write(db=db, sql='DELETE FROM source_trades')\n", True, "DELETE", None),
        ("receiver", "def write(receiver, sql):\n    receiver.execute(sql)\ndef caller(db):\n    write(db, 'DELETE FROM source_trades')\n", True, "DELETE", None),
        ("alias", "def write(db, sql):\n    sink = db.execute\n    sink(sql)\ndef caller(db):\n    write(db, 'DELETE FROM source_trades')\n", True, "DELETE", None),
        ("getattr", "def write(db, sql):\n    getattr(db, 'execute')(sql)\ndef caller(db):\n    write(db, 'DELETE FROM source_trades')\n", True, "DELETE", None),
        ("two_level", "def one(db, sql):\n    db.execute(sql)\ndef two(db, sql):\n    one(db, sql)\ndef caller(db):\n    two(db, 'DELETE FROM source_trades')\n", True, "DELETE", None),
        ("three_level", "def one(db, sql):\n    db.execute(sql)\ndef two(db, sql):\n    one(db, sql)\ndef three(db, sql):\n    two(db, sql)\ndef caller(db):\n    three(db, 'DELETE FROM source_trades')\n", True, "DELETE", None),
        ("after_caller", "def caller(db):\n    write(db, 'DELETE FROM source_trades')\ndef write(db, sql):\n    db.execute(sql)\n", True, "DELETE", None),
        ("class_method", "class Writer:\n    def write(self, db, sql):\n        db.execute(sql)\ndef caller(db):\n    Writer().write(db, 'DELETE FROM source_trades')\n", True, "DELETE", None),
        ("captured_receiver", "def caller(db):\n    def write(sql):\n        db.execute(sql)\n    write('DELETE FROM source_trades')\n", True, "DELETE", None),
        ("sql_keyword", "def write(db, sql):\n    db.execute(sql)\ndef caller(db):\n    write(db, sql='DELETE FROM source_trades')\n", True, "DELETE", None),
        ("query_keyword", "def write(db, query):\n    db.execute(query)\ndef caller(db):\n    write(db, query='DELETE FROM source_trades')\n", True, "DELETE", None),
        ("statement_keyword", "def write(db, statement):\n    db.execute(statement)\ndef caller(db):\n    write(db, statement='DELETE FROM source_trades')\n", True, "DELETE", None),
        ("unresolved", "def write(db, sql):\n    db.execute(sql)\ndef caller(db, unknown):\n    write(db, unknown + ' DELETE FROM source_trades')\n", False, None, "unresolved_source_trade_sql"),
        ("recursive", "def write(db, sql):\n    write(db, sql)\ndef caller(db):\n    write(db, 'DELETE FROM source_trades')\n", False, None, "unresolved_source_trade_sql"),
        ("mutual_recursion", "def one(db, sql):\n    two(db, sql)\ndef two(db, sql):\n    one(db, sql)\ndef caller(db):\n    one(db, 'DELETE FROM source_trades')\n", False, None, "unresolved_source_trade_sql"),
        ("depth_limit", "def one(db, sql):\n    two(db, sql)\ndef two(db, sql):\n    three(db, sql)\ndef three(db, sql):\n    four(db, sql)\ndef four(db, sql):\n    db.execute(sql)\ndef caller(db):\n    one(db, 'DELETE FROM source_trades')\n", False, None, "unresolved_source_trade_sql"),
        ("class_recursion", "class Writer:\n    def write(self, db, sql):\n        self.write(db, sql)\ndef caller(db):\n    Writer().write(db, 'DELETE FROM source_trades')\n", False, None, "unresolved_source_trade_sql"),
        ("safe_select", "def write(db, sql):\n    db.execute(sql)\ndef caller(db):\n    write(db, 'SELECT * FROM source_trades')\n", None, None, None),
        ("unrelated_dynamic", "def write(db, sql):\n    db.execute(sql)\ndef caller(db, unknown):\n    write(db, unknown)\n", None, None, None),
        ("other_dml", "def write(db, sql):\n    db.execute(sql)\ndef caller(db):\n    write(db, 'DELETE FROM wallets')\n", None, None, None),
    ],
)
def test_execution_wrapper_matrix(tmp_path: Path, name: str, source: str, resolved: bool | None, operation: str | None, reason: str | None) -> None:
    findings = _scan(tmp_path, name, source)
    if resolved is None:
        assert findings == []
    else:
        _one(findings, scope="caller", sink="execute", resolved=resolved, operation=operation, reason=reason)


def test_wrapper_scope_isolation_and_no_duplicate_definition_finding(tmp_path: Path) -> None:
    findings = _scan(tmp_path, "isolation", "def write(db, sql):\n    db.execute(sql)\ndef first(db):\n    write(db, 'DELETE FROM source_trades')\ndef second(db, sql):\n    sql = 'SELECT 1'\n    write(db, sql)\n")
    _one(findings, scope="first", sink="execute", resolved=True, operation="DELETE")


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("if", "def f(db):\n    if db.execute('DELETE FROM source_trades'):\n        pass\n"),
        ("while", "def f(db):\n    while db.execute('DELETE FROM source_trades'):\n        break\n"),
        ("for", "def f(db):\n    for _ in db.execute('DELETE FROM source_trades'):\n        pass\n"),
        ("with", "def f(db):\n    with db.execute('DELETE FROM source_trades'):\n        pass\n"),
        ("return", "def f(db):\n    return db.execute('DELETE FROM source_trades')\n"),
        ("yield", "def f(db):\n    yield db.execute('DELETE FROM source_trades')\n"),
        ("comprehension_iter", "def f(db):\n    return [x for x in db.execute('DELETE FROM source_trades')]\n"),
        ("comprehension_filter", "def f(db):\n    return [x for x in [1] if db.execute('DELETE FROM source_trades')]\n"),
        ("assignment_expression", "def f(db):\n    if (x := db.execute('DELETE FROM source_trades')):\n        return x\n"),
        ("assert", "def f(db):\n    assert db.execute('DELETE FROM source_trades')\n"),
        ("match_guard", "def f(db, value):\n    match value:\n        case _ if db.execute('DELETE FROM source_trades'):\n            pass\n"),
    ],
)
def test_control_expression_matrix(tmp_path: Path, name: str, source: str) -> None:
    findings = _scan(tmp_path, name, source)
    _one(findings, scope="f", sink="execute", resolved=True, operation="DELETE")


@pytest.mark.parametrize("sql", ["SELECT * FROM source_trades", "SELECT 1"])
def test_control_expression_select_is_safe(tmp_path: Path, sql: str) -> None:
    assert _scan(tmp_path, "safe_control", f"def f(db):\n    if db.execute({sql!r}):\n        pass\n") == []


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        ("safe_if_forbidden_else", "def f(db, flag):\n    if flag:\n        sql = 'SELECT 1'\n    else:\n        sql = 'DELETE FROM source_trades'\n    db.execute(sql)\n", [(True, "DELETE")]),
        ("forbidden_if_safe_else", "def f(db, flag):\n    if flag:\n        sql = 'DELETE FROM source_trades'\n    else:\n        sql = 'SELECT 1'\n    db.execute(sql)\n", [(True, "DELETE")]),
        ("both_forbidden", "def f(db, flag):\n    if flag:\n        sql = 'DELETE FROM source_trades'\n    else:\n        sql = 'UPDATE source_trades SET side=\"x\"'\n    db.execute(sql)\n", [(True, "DELETE"), (True, "UPDATE")]),
        ("unresolved_branch", "def f(db, flag, unknown):\n    if flag:\n        sql = unknown + ' DELETE FROM source_trades'\n    else:\n        sql = 'SELECT 1'\n    db.execute(sql)\n", [(False, None)]),
        ("nested_if", "def f(db, a, b):\n    if a:\n        if b:\n            sql = 'DELETE FROM source_trades'\n        else:\n            sql = 'SELECT 1'\n    else:\n        sql = 'UPDATE source_trades SET side=\"x\"'\n    db.execute(sql)\n", [(True, "DELETE"), (True, "UPDATE")]),
        ("three_alternatives", "def f(db, a, b):\n    if a:\n        sql = 'DELETE FROM source_trades'\n    elif b:\n        sql = 'UPDATE source_trades SET side=\"x\"'\n    else:\n        sql = 'SELECT 1'\n    db.execute(sql)\n", [(True, "DELETE"), (True, "UPDATE")]),
        ("try_safe_except_forbidden", "def f(db):\n    try:\n        sql = 'SELECT 1'\n    except Exception:\n        sql = 'DELETE FROM source_trades'\n    db.execute(sql)\n", [(True, "DELETE")]),
        ("try_forbidden_except_safe", "def f(db):\n    try:\n        sql = 'DELETE FROM source_trades'\n    except Exception:\n        sql = 'SELECT 1'\n    db.execute(sql)\n", [(True, "DELETE")]),
        ("divergent_handlers", "def f(db):\n    try:\n        sql = 'SELECT 1'\n    except ValueError:\n        sql = 'DELETE FROM source_trades'\n    except KeyError:\n        sql = 'UPDATE source_trades SET side=\"x\"'\n    db.execute(sql)\n", [(True, "DELETE"), (True, "UPDATE")]),
        ("loop_replaces_safe", "def f(db, rows):\n    sql = 'SELECT 1'\n    for row in rows:\n        sql = 'DELETE FROM source_trades'\n    db.execute(sql)\n", [(True, "DELETE")]),
        ("loop_zero_preserves", "def f(db, rows):\n    sql = 'UPDATE source_trades SET side=\"x\"'\n    for row in rows:\n        sql = 'DELETE FROM source_trades'\n    db.execute(sql)\n", [(True, "DELETE"), (True, "UPDATE")]),
        ("try_preserves_preexisting", "def f(db):\n    sql = 'DELETE FROM source_trades'\n    try:\n        sql = 'SELECT 1'\n    except Exception:\n        pass\n    db.execute(sql)\n", [(True, "DELETE")]),
        ("branch_sink_alias", "def f(db, flag):\n    if flag:\n        sink = db.execute\n    else:\n        sink = db.execute\n    sink('DELETE FROM source_trades')\n", [(True, "DELETE")]),
        ("branch_reassignment_wrapper", "def write(receiver, sql):\n    receiver.execute(sql)\ndef f(db, flag):\n    if flag:\n        receiver = db\n    else:\n        receiver = db\n    write(receiver, 'DELETE FROM source_trades')\n", [(True, "DELETE")]),
        ("handler_sink", "def f(db):\n    try:\n        pass\n    except Exception:\n        db.execute('DELETE FROM source_trades')\n", [(True, "DELETE")]),
    ],
)
def test_control_state_matrix(tmp_path: Path, name: str, source: str, expected: list[tuple[bool, str | None]]) -> None:
    findings = _scan(tmp_path, name, source)
    assert len(findings) == len(expected)
    assert [(finding.resolved, finding.operation) for finding in findings] == expected
    assert len({(finding.scope, finding.line, finding.operation, finding.reason) for finding in findings}) == len(findings)
    for finding in findings:
        assert finding.scope == "f"
        assert finding.sink == "execute"
        assert finding.table == "source_trades"
        assert finding.reason == (None if finding.resolved else "unresolved_source_trade_sql")


@pytest.mark.parametrize(
    ("name", "script", "expected"),
    [
        ("select_delete", "SELECT 1; DELETE FROM source_trades", ["DELETE"]),
        ("delete_select", "DELETE FROM source_trades; SELECT 1", ["DELETE"]),
        ("writer_then_delete", "INSERT OR IGNORE INTO source_trades (id) VALUES (1); DELETE FROM source_trades", ["INSERT OR IGNORE", "DELETE"]),
        ("multiple", "DELETE FROM source_trades; UPDATE source_trades SET side='x'", ["DELETE", "UPDATE"]),
        ("metadata_then_side", "UPDATE source_trades SET metadata_json='x'; UPDATE source_trades SET side='x'", ["UPDATE", "UPDATE"]),
        ("resolution_then_price", "UPDATE source_trades SET resolution_status='x'; UPDATE source_trades SET price=1", ["UPDATE", "UPDATE"]),
        ("blanks_comments", " ; -- skip\n; /* skip */ ; DELETE FROM source_trades;", ["DELETE"]),
        ("leading_block_comment", "/* harmless */ DELETE FROM source_trades;", ["DELETE"]),
        ("multiline_block_comment", "/* harmless\ncomment */ DELETE FROM source_trades;", ["DELETE"]),
        ("single_quote", "INSERT INTO logs VALUES ('a;b'); DELETE FROM source_trades", ["DELETE"]),
        ("double_quote", 'INSERT INTO logs VALUES ("a;b"); DELETE FROM source_trades', ["DELETE"]),
        ("doubled_single", "INSERT INTO logs VALUES ('a'';b'); DELETE FROM source_trades", ["DELETE"]),
        ("doubled_double", 'INSERT INTO logs VALUES ("a"";b"); DELETE FROM source_trades', ["DELETE"]),
        ("many_quotes", "INSERT INTO logs VALUES ('a;b'); INSERT INTO logs VALUES (\"c;d\"); DELETE FROM source_trades", ["DELETE"]),
        ("unrelated", "DELETE FROM wallets; UPDATE wallets SET name='x'", []),
    ],
)
def test_executescript_statement_matrix(tmp_path: Path, name: str, script: str, expected: list[str]) -> None:
    findings = _scan(tmp_path, name, f"def f(db):\n    db.executescript({script!r})\n")
    assert [finding.operation for finding in findings] == expected
    assert len(findings) == len(expected)
    for finding in findings:
        assert finding.scope == "f"
        assert finding.sink == "executescript"
        assert finding.resolved is True
        assert finding.table == "source_trades"
        assert finding.reason is None


def test_executescript_dynamic_source_trade_dml_is_one_unresolved_finding(tmp_path: Path) -> None:
    findings = _scan(tmp_path, "dynamic_script", "def f(db, unknown):\n    db.executescript(unknown + ' DELETE FROM source_trades')\n")
    _one(findings, scope="f", sink="executescript", resolved=False, operation=None, reason="unresolved_source_trade_sql")


@pytest.mark.parametrize(
    ("relative", "body", "operation", "sink"),
    [
        ("scripts/rogue_schema.py", "db.execute('DELETE FROM source_trades')", "DELETE", "execute"),
        ("src/polycopy/engine/schema.py", "db.execute('UPDATE source_trades SET side=\"x\"')", "UPDATE", "execute"),
        ("src/polycopy/fake_migrations/repair.py", "db.execute('INSERT INTO source_trades (id) VALUES (1)')", "INSERT", "execute"),
        ("src/polycopy/db/schema.py", "db.execute('DELETE FROM source_trades')", "DELETE", "execute"),
        ("src/polycopy/migrations/pr24z_canonical_identity.py", "db.execute('UPDATE source_trades SET metadata_json=\"x\"')", "UPDATE", "execute"),
        ("src/polycopy/migrations/pr24z_canonical_identity.py", "db.execute('INSERT INTO source_trades (id) VALUES (1)')", "INSERT", "execute"),
        ("src/polycopy/db/schema.py", "def helper(db):\n    db.execute('DELETE FROM source_trades')\nhelper(db)", "DELETE", "execute"),
        ("src/polycopy/migrations/pr24z_canonical_identity.py", "db.executescript('SELECT 1; DELETE FROM source_trades')", "DELETE", "executescript"),
        ("src/polycopy/db/schema_like.py", "db.execute('INSERT INTO source_trades (id) VALUES (1)')", "INSERT", "execute"),
        ("src/polycopy/migrationish/repair.py", "db.execute('UPDATE source_trades SET side=\"x\"')", "UPDATE", "execute"),
    ],
)
def test_schema_and_migration_exact_path_matrix(tmp_path: Path, relative: str, body: str, operation: str, sink: str) -> None:
    from polycopy.engine.source_trade_sql_architecture import contract_violations, scan_repository

    root = tmp_path / "repo"
    target = root / relative
    target.parent.mkdir(parents=True)
    source = body if body.startswith("def ") else f"def f(db):\n    {body}\n"
    target.write_text(source)
    findings = contract_violations(scan_repository(root))
    _one(findings, scope="helper" if body.startswith("def ") else "f", sink=sink, resolved=True, operation=operation)
    assert findings[0].path == relative


@pytest.mark.parametrize(
    ("relative", "body", "operation"),
    [
        ("src/polycopy/db/schema.py", "db.execute('INSERT INTO source_trades (id) VALUES (1)')", "INSERT"),
        ("src/polycopy/migrations/pr24z_canonical_identity.py", "db.execute('UPDATE source_trades SET source_trade_id=? WHERE id=?')", "UPDATE"),
    ],
)
def test_exact_schema_and_migration_positive_operations_are_permitted(tmp_path: Path, relative: str, body: str, operation: str) -> None:
    from polycopy.engine.source_trade_sql_architecture import contract_violations, scan_repository

    root = tmp_path / "repo"
    target = root / relative
    target.parent.mkdir(parents=True)
    target.write_text(f"def f(db):\n    {body}\n")
    findings = scan_repository(root)
    assert len(findings) == 1
    assert findings[0].operation == operation
    assert findings[0].path == relative
    assert contract_violations(findings) == []
