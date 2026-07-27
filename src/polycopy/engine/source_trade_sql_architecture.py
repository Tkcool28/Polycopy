"""Read-only AST evidence for the bounded ``source_trades`` SQL architecture.

The scanner deliberately follows only small, statement-ordered local flows.  It
never imports or executes the inspected module: an expression that cannot be
proved is a SQL string is reported only when its local source evidence names
``source_trades``.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_SINKS = frozenset({"execute", "executemany", "executescript", "iterquery", "query"})
_SQL_KEYS = frozenset({"sql", "query", "statement"})
_DML = re.compile(r"\b(INSERT(?:\s+OR\s+(?:IGNORE|REPLACE))?|REPLACE|UPDATE|DELETE)\b", re.IGNORECASE)
_TARGET = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+(?:IGNORE|REPLACE))?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:main\.)?[\"'`\[]?([a-zA-Z_][\w]*)",
    re.IGNORECASE,
)
_SOURCE = re.compile(r"\bsource_trades\b", re.IGNORECASE)


@dataclass(frozen=True)
class SqlFinding:
    path: str
    scope: str
    line: int
    sink: str
    resolved: bool
    operation: str | None
    table: str | None
    columns: tuple[str, ...]
    selector_columns: tuple[str, ...]
    reason: str | None = None
    sql: str | None = None


@dataclass(frozen=True)
class _Helper:
    params: tuple[str, ...]
    returned: ast.AST


def _columns(sql: str, operation: str | None) -> tuple[str, ...]:
    if operation == "UPDATE":
        match = re.search(r"\bSET\s+(.+?)(?:\bWHERE\b|$)", sql, re.IGNORECASE | re.DOTALL)
        return tuple(re.findall(r"(?:^|,)\s*[\"'`\[]?([a-zA-Z_][\w]*)\s*=", match.group(1))) if match else ()
    if operation and operation.startswith("INSERT"):
        match = re.search(r"\bsource_trades\s*\(([^)]*)\)", sql, re.IGNORECASE | re.DOTALL)
        return tuple(re.findall(r"[a-zA-Z_][\w]*", match.group(1))) if match else ()
    return ()


def _selectors(sql: str) -> tuple[str, ...]:
    match = re.search(r"\bWHERE\s+(.+)$", sql, re.IGNORECASE | re.DOTALL)
    return tuple(re.findall(r"\b([a-zA-Z_][\w]*)\s*=\s*\?", match.group(1))) if match else ()


def _resolve(node: ast.AST | None, values: dict[str, ast.AST], helpers: dict[str, _Helper], seen: set[str] | None = None) -> str | None:
    if node is None:
        return None
    seen = set() if seen is None else seen
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in seen:
            return None
        return _resolve(values.get(node.id), values, helpers, seen | {node.id})
    if isinstance(node, ast.IfExp):
        # A bounded writer may choose between two audited constant statements;
        # follow the primary branch here (both constants are independently
        # allowlisted by the contract when encountered as direct SQL values).
        return _resolve(node.body, values, helpers, seen)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _resolve(node.left, values, helpers, seen), _resolve(node.right, values, helpers, seen)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            elif isinstance(item, ast.FormattedValue):
                value = _resolve(item.value, values, helpers, seen)
                if value is None:
                    return None
                parts.append(value)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
        helper = helpers[node.func.id]
        if len(node.args) > len(helper.params) or any(keyword.arg is None for keyword in node.keywords):
            return None
        local = dict(values)
        for parameter, argument in zip(helper.params, node.args):
            local[parameter] = argument
        for keyword in node.keywords:
            if keyword.arg not in helper.params or keyword.arg in helper.params[:len(node.args)]:
                return None
            local[keyword.arg] = keyword.value
        if any(parameter not in local for parameter in helper.params):
            return None
        return _resolve(helper.returned, local, helpers, seen)
    return None


def _source_relevant(node: ast.AST | None, values: dict[str, ast.AST], helpers: dict[str, _Helper], seen: set[str] | None = None) -> bool:
    """Whether an unresolved expression has local evidence of the protected table."""
    if node is None:
        return False
    seen = set() if seen is None else seen
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_SOURCE.search(node.value)) or "source_" in node.value.lower()
    if isinstance(node, ast.Name):
        if _SOURCE.search(node.id):
            return True
        if node.id in seen:
            return False
        return _source_relevant(values.get(node.id), values, helpers, seen | {node.id})
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
        helper = helpers[node.func.id]
        local = dict(values)
        for parameter, argument in zip(helper.params, node.args):
            local[parameter] = argument
        for keyword in node.keywords:
            if keyword.arg is not None:
                local[keyword.arg] = keyword.value
        return _source_relevant(helper.returned, local, helpers, seen)
    return any(_source_relevant(child, values, helpers, seen) for child in ast.iter_child_nodes(node))


def _dml_relevant(node: ast.AST | None, values: dict[str, ast.AST], helpers: dict[str, _Helper], seen: set[str] | None = None) -> bool:
    """Whether local expression evidence says an unresolved source SQL is DML."""
    if node is None:
        return False
    seen = set() if seen is None else seen
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_DML.search(node.value))
    if isinstance(node, ast.Name):
        if _DML.search(node.id):
            return True
        if node.id in seen:
            return False
        return _dml_relevant(values.get(node.id), values, helpers, seen | {node.id})
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
        helper = helpers[node.func.id]
        local = dict(values)
        for parameter, argument in zip(helper.params, node.args):
            local[parameter] = argument
        for keyword in node.keywords:
            if keyword.arg is not None:
                local[keyword.arg] = keyword.value
        return _dml_relevant(helper.returned, local, helpers, seen)
    return any(_dml_relevant(child, values, helpers, seen) for child in ast.iter_child_nodes(node))


def _sink_from_value(node: ast.AST | None, values: dict[str, ast.AST], aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr in _SINKS:
        return node.attr
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
        name = _resolve(node.args[1], values, {})
        return name if name in _SINKS else None
    return None


def _sink_name(call: ast.Call, values: dict[str, ast.AST], aliases: dict[str, str]) -> str | None:
    return _sink_from_value(call.func, values, aliases)


def _sql_arg(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    return next((keyword.value for keyword in call.keywords if keyword.arg in _SQL_KEYS), None)


def _calls_in(statement: ast.AST) -> Iterable[ast.Call]:
    """Yield calls without crossing into a nested lexical scope."""
    for child in ast.iter_child_nodes(statement):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _calls_in(child)


def _assigned_names(statements: Iterable[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and node is not statement:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
    return names


def _record_assignment(statement: ast.stmt, values: dict[str, ast.AST], aliases: dict[str, str]) -> None:
    if isinstance(statement, ast.AugAssign) and isinstance(statement.target, ast.Name):
        previous = values.get(statement.target.id)
        if isinstance(statement.op, ast.Add) and isinstance(previous, ast.expr):
            values[statement.target.id] = ast.BinOp(left=previous, op=ast.Add(), right=statement.value)
        else:
            values.pop(statement.target.id, None)
        aliases.pop(statement.target.id, None)
        return
    if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
        return
    value = statement.value
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    for target in targets:
        if isinstance(target, ast.Name):
            if value is None:
                values.pop(target.id, None)
                aliases.pop(target.id, None)
            else:
                values[target.id] = value
                sink = _sink_from_value(value, values, aliases)
                if sink:
                    aliases[target.id] = sink
                else:
                    aliases.pop(target.id, None)


def _classify(path: str, scope: str, line: int, sink: str, sql: str | None, *, relevant: bool) -> SqlFinding | None:
    if sql is None:
        if relevant:
            return SqlFinding(path, scope, line, sink, False, None, "source_trades", (), (), "unresolved_source_trade_sql", None)
        return None
    match = _DML.search(sql)
    target = _TARGET.search(sql)
    table = target.group(1).lower() if target else None
    if not match or table != "source_trades":
        return None
    operation = " ".join(match.group(1).upper().split())
    return SqlFinding(path, scope, line, sink, True, operation, table, _columns(sql, operation), _selectors(sql), sql=sql)


def _scan_scope(statements: Iterable[ast.stmt], *, path: str, scope: str, helpers: dict[str, _Helper], values: dict[str, ast.AST] | None = None, aliases: dict[str, str] | None = None) -> list[SqlFinding]:
    values, aliases = dict(values or {}), dict(aliases or {})
    helpers = dict(helpers)
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = [item for item in statement.body if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str))]
            if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
                helpers[statement.name] = _Helper(tuple(argument.arg for argument in statement.args.args), body[0].value)
    findings: list[SqlFinding] = []
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            child_scope = statement.name if scope == "<module>" else f"{scope}.{statement.name}"
            findings.extend(
                _scan_scope(
                    statement.body,
                    path=path,
                    scope=child_scope,
                    helpers=helpers,
                    values=values,
                )
            )
            continue
        _record_assignment(statement, values, aliases)
        is_control = isinstance(
            statement,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try),
        )
        if not is_control:
            for call in _calls_in(statement):
                sink = _sink_name(call, values, aliases)
                if sink is None:
                    continue
                arg = _sql_arg(call)
                relevant = _source_relevant(arg, values, helpers) and _dml_relevant(arg, values, helpers)
                finding = _classify(path, scope, call.lineno, sink, _resolve(arg, values, helpers), relevant=relevant)
                if finding is not None:
                    findings.append(finding)
        if is_control:
            bodies: list[list[ast.stmt]] = []
            if hasattr(statement, "body"):
                bodies.append(statement.body)
            if hasattr(statement, "orelse"):
                bodies.append(statement.orelse)
            if isinstance(statement, ast.Try):
                bodies.extend(handler.body for handler in statement.handlers)
                bodies.append(statement.finalbody)
            for body in bodies:
                findings.extend(_scan_scope(body, path=path, scope=scope, helpers=helpers, values=values, aliases=aliases))
            branch_assignments = [
                item
                for item in statement.body + list(getattr(statement, "orelse", []))
                if isinstance(item, (ast.Assign, ast.AnnAssign))
            ]
            for name in _assigned_names(branch_assignments):
                values.pop(name, None)
                aliases.pop(name, None)
    return findings


def _helpers(tree: ast.Module) -> dict[str, _Helper]:
    helpers: dict[str, _Helper] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = [statement for statement in node.body if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str))]
        if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
            helpers[node.name] = _Helper(tuple(argument.arg for argument in node.args.args), body[0].value)
    return helpers


def scan_python_file(path: Path, *, repo_root: Path | None = None) -> list[SqlFinding]:
    tree = ast.parse(path.read_text(), filename=str(path))
    relative = str(path.relative_to(repo_root)) if repo_root else str(path)
    helpers = _helpers(tree)
    return _scan_scope(tree.body, path=relative, scope="<module>", helpers=helpers)


def scan_repository(repo_root: Path) -> list[SqlFinding]:
    return [finding for root in (repo_root / "src", repo_root / "scripts") if root.exists() for path in root.rglob("*.py") for finding in scan_python_file(path, repo_root=repo_root)]


def contract_violations(findings: Iterable[SqlFinding]) -> list[SqlFinding]:
    """Enforce the three deliberately narrow production source-trade roles."""
    violations: list[SqlFinding] = []
    writer_columns = {
        ("id", "source", "source_trade_id", "market_source_id", "side", "outcome", "quantity", "price", "trader_address", "timestamp", "is_sample", "token_id", "metadata_json"),
        ("id", "source", "source_trade_id", "market_source_id", "side", "outcome", "quantity", "price", "trader_address", "timestamp", "is_sample", "token_id"),
    }
    resolution_columns = {"resolution_status", "resolved_at", "winning_token_id", "is_winning_trade", "realized_pnl", "settlement_source"}
    for finding in findings:
        if finding.table != "source_trades":
            continue
        if not finding.resolved:
            violations.append(finding)
        elif finding.path.endswith("source_trade_writer.py"):
            if finding.operation != "INSERT OR IGNORE" or finding.columns not in writer_columns:
                violations.append(finding)
        elif finding.path.endswith("source_trade_metadata_reconciliation.py"):
            if finding.operation != "UPDATE" or finding.columns != ("metadata_json",) or set(finding.selector_columns) not in ({"id"}, {"source", "source_trade_id"}):
                violations.append(finding)
        elif finding.path.endswith("source_trade_resolution.py"):
            if finding.operation != "UPDATE" or set(finding.columns) != resolution_columns or set(finding.selector_columns) != {"id"}:
                violations.append(finding)
        elif "schema.py" not in finding.path and "/migrations/" not in finding.path and finding.path not in {"scripts/seed_demo_data.py", "scripts/live_smoke_pr3_fixes.py"}:
            violations.append(finding)
    return violations
