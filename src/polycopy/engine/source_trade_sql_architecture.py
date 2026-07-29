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
    predicate_fingerprint: str | None = None
    predicate_invalid_reason: str | None = None
    reason: str | None = None
    sql: str | None = None


# Approved reconciliation predicate grammars (Finding 3 — exact bounded
# predicate fingerprints). Anything outside this grammar fails closed.
PREDICATE_METADATA_BY_ID = "eq(id,param)"
PREDICATE_METADATA_BY_IDENTITY = "and(eq(source,param),eq(source_trade_id,param))"
PREDICATE_RESOLUTION_BY_ID = "eq(id,param)"
_APPROVED_PREDICATES: frozenset[str] = frozenset(
    {
        PREDICATE_METADATA_BY_ID,
        PREDICATE_METADATA_BY_IDENTITY,
        PREDICATE_RESOLUTION_BY_ID,
    }
)

# Identifier whitelist for equality terms. Only reconciliation columns are
# approved; any other identifier fails closed.
_APPROVED_PREDICATE_IDENTIFIERS: frozenset[str] = frozenset({"id", "source", "source_trade_id"})


@dataclass(frozen=True)
class _PredicateParse:
    fingerprint: str | None
    invalid_reason: str | None

    @classmethod
    def approved(cls, fingerprint: str) -> _PredicateParse:
        return cls(fingerprint, None)

    @classmethod
    def invalid(cls, reason: str) -> _PredicateParse:
        return cls(None, reason)


@dataclass(frozen=True)
class _Helper:
    params: tuple[str, ...]
    returned: ast.AST


@dataclass(frozen=True)
class _ExecutionWrapper:
    params: tuple[str, ...]
    sql_parameter: str
    sink: str


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


# ------------------------------------------------------------------ #
# Predicate fingerprint parser (Finding 3)
#
# Bounded fail-closed grammar. Only ``predicates`` documented as
# approved are accepted. The parser strips benign syntactic noise
# (whitespace, paired parentheses around the whole WHERE body or any
# equality term, and ``"…[`` identifier wrappers) and otherwise requires
# the exact form described in the contract. Anything that does not
# match the grammar yields ``predicate_fingerprint=None`` and a
# ``predicate_invalid_reason`` describing why.
# ------------------------------------------------------------------ #
_RE_PRED_TAIL = re.compile(r"\bWHERE\b(.*)", re.IGNORECASE | re.DOTALL)
_RE_ORDER_TAIL = re.compile(r"\bORDER\s+BY\b.*$", re.IGNORECASE | re.DOTALL)
_RE_LIMIT_TAIL = re.compile(r"\bLIMIT\b\s+[^)]*?[;)]?\s*$", re.IGNORECASE | re.DOTALL)
_RE_TOKEN = re.compile(
    r"""
    (?P<and>\bAND\b)
    | (?P<or>\bOR\b)
    | (?P<in>\bIN\b)
    | (?P<not>\bNOT\b)
    | (?P<is>\bIS\b)
    | (?P<like>\bLIKE\b)
    | (?P<select>\bSELECT\b)
    | (?P<identifier>"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_]*)
    | (?P<param>\?)
    | (?P<eq>=)
    | (?P<paren>\(|\))
    | (?P<comma>,)
    | (?P<string>'[^']*(?:''[^']*)*'|"[^"]*(?:""[^"]*)*")
    | (?P<ws>\s+)
    """,
    re.VERBOSE | re.IGNORECASE,
)
_RE_COLLATE = re.compile(r"\bCOLLATE\b\s+\w+", re.IGNORECASE)


def _strip_identifier_wrappers(identifier: str) -> str:
    if len(identifier) >= 2 and identifier[0] == identifier[-1] and identifier[0] in {'"', '`', '[', "'"}:
        if identifier[0] == '[':
            inner = identifier[1:-1]
            if not inner.endswith(']'):
                return identifier
            return inner[:-1]
        return identifier[1:-1]
    return identifier


def _tokenize_predicate(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    for match in _RE_TOKEN.finditer(text):
        if match.start() != pos:
            return []  # unrecognized token in between
        kind = match.lastgroup
        value = match.group()
        if kind == "ws":
            pos = match.end()
            continue
        tokens.append((str(kind), value))
        pos = match.end()
    if pos != len(text):
        return []
    return tokens


def _strip_balanced_parens(tokens: list[tuple[str, str]]) -> list[tuple[str, str]] | None:
    """Strip one layer of balanced outer parentheses around ``tokens``.

    Returns the inner tokens when the list is enclosed by a single
    well-balanced ``( … )`` pair; ``None`` when parens are not balanced or
    more than one outer layer is present.
    """
    if not tokens:
        return tokens
    if tokens[0][0] != "paren" or tokens[0][1] != "(":
        return tokens
    depth = 0
    last_paren_index = -1
    for index, (kind, value) in enumerate(tokens):
        if kind == "paren" and value == "(":
            depth += 1
        elif kind == "paren" and value == ")":
            depth -= 1
            if depth == 0:
                last_paren_index = index
                break
        if depth < 0:
            return None
    if last_paren_index != len(tokens) - 1:
        return None
    return tokens[1:last_paren_index]  # type: ignore[return-value]


def _parse_equality_term(tokens: list[tuple[str, str]]) -> tuple[str, str] | None:
    """Parse ``approved_identifier = ?`` and return the lower-cased column name."""
    if len(tokens) != 3:
        return None
    ident_tok, eq_tok, param_tok = tokens
    if ident_tok[0] != "identifier":
        return None
    if eq_tok[0] != "eq" or param_tok[0] != "param":
        return None
    column = _strip_identifier_wrappers(ident_tok[1]).lower()
    return column, f"eq({column},param)"


def parse_predicate_fingerprint(sql: str) -> _PredicateParse:
    """Return an exact normalized predicate fingerprint, or a closed-fail reason."""
    if _RE_COLLATE.search(sql) is not None:
        return _PredicateParse.invalid("collate_clause_disallowed")
    where_match = _RE_PRED_TAIL.search(sql)
    if where_match is None:
        return _PredicateParse.invalid("missing_where_clause")
    predicate_text = where_match.group(1)
    predicate_text = _RE_ORDER_TAIL.sub("", predicate_text)
    predicate_text = _RE_LIMIT_TAIL.sub("", predicate_text)
    predicate_text = predicate_text.strip().rstrip(";").strip()
    if not predicate_text:
        return _PredicateParse.invalid("empty_where_clause")
    tokens = _tokenize_predicate(predicate_text)
    if not tokens:
        return _PredicateParse.invalid("unrecognized_predicate_tokens")
    # Fail-closed: any other connector or sub-expression ⇒ invalid.
    forbidden_kinds = {"or", "not", "in", "is", "like", "select", "string", "comma"}
    if any(kind in forbidden_kinds for kind, _ in tokens):
        return _PredicateParse.invalid("disallowed_predicate_construct")
    # Peel a single, balanced outer pair of parentheses.
    stripped = _strip_balanced_parens(tokens)
    if stripped is None:
        return _PredicateParse.invalid("unbalanced_or_stray_parens")
    tokens = stripped
    # Empty after stripping? Already handled above (empty_where_clause).
    if not tokens:
        return _PredicateParse.invalid("empty_predicate_after_normalization")
    # Single term?
    equality = _parse_equality_term(tokens)
    if equality is not None:
        column, fp = equality
        if column not in _APPROVED_PREDICATE_IDENTIFIERS:
            return _PredicateParse.invalid("identifier_not_in_approval_set")
        if fp not in _APPROVED_PREDICATES:
            return _PredicateParse.invalid("single_term_fingerprint_not_approved")
        return _PredicateParse.approved(fp)
    # Must be exactly ``equality AND equality`` — seven tokens total
    # (identifier, =, ?, AND, identifier, =, ?).
    if len(tokens) != 7:
        return _PredicateParse.invalid("predicate_token_count_outside_grammar")
    left = tokens[:3]
    and_tok = tokens[3]
    right_pair = tokens[4:]
    if and_tok[0] != "and":
        return _PredicateParse.invalid("missing_top_level_and")
    left_eq = _parse_equality_term(left)
    right_eq = _parse_equality_term(right_pair)
    if left_eq is None or right_eq is None:
        return _PredicateParse.invalid("and_branch_malformed_equality")
    left_col, left_fp = left_eq
    right_col, right_fp = right_eq
    for column in (left_col, right_col):
        if column not in _APPROVED_PREDICATE_IDENTIFIERS:
            return _PredicateParse.invalid("identifier_not_in_approval_set")
    # Disallow the same column twice; require exactly the identity pair.
    if {left_col, right_col} != {"source", "source_trade_id"}:
        return _PredicateParse.invalid("and_columns_do_not_match_identity_pair")
    canonical = f"and({min(left_fp, right_fp)},{max(left_fp, right_fp)})"
    if canonical not in _APPROVED_PREDICATES:
        return _PredicateParse.invalid("identity_fingerprint_not_approved")
    return _PredicateParse.approved(canonical)


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


def _resolve_all(node: ast.AST | None, values: dict[str, ast.AST], helpers: dict[str, _Helper], seen: set[str] | None = None) -> tuple[str, ...]:
    """Return every bounded static SQL alternative; never choose one branch."""
    if node is None:
        return ()
    seen = set() if seen is None else seen
    if isinstance(node, ast.IfExp):
        return tuple(dict.fromkeys(
            _resolve_all(node.body, values, helpers, seen)
            + _resolve_all(node.orelse, values, helpers, seen)
        ))
    if isinstance(node, ast.Name):
        if node.id in seen:
            return ()
        return _resolve_all(values.get(node.id), values, helpers, seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return tuple(left + right for left in _resolve_all(node.left, values, helpers, seen) for right in _resolve_all(node.right, values, helpers, seen))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
        helper = helpers[node.func.id]
        local = dict(values)
        for parameter, argument in zip(helper.params, node.args):
            local[parameter] = argument
        for keyword in node.keywords:
            if keyword.arg is not None:
                local[keyword.arg] = keyword.value
        return _resolve_all(helper.returned, local, helpers, seen)
    resolved = _resolve(node, values, helpers, seen)
    return (resolved,) if resolved is not None else ()


def _has_unresolved_alternative(node: ast.AST | None, values: dict[str, ast.AST], helpers: dict[str, _Helper], seen: set[str] | None = None) -> bool:
    """Whether a bounded alternative remains dynamic beside any static ones."""
    if node is None:
        return True
    seen = set() if seen is None else seen
    if isinstance(node, ast.IfExp):
        return _has_unresolved_alternative(node.body, values, helpers, seen) or _has_unresolved_alternative(node.orelse, values, helpers, seen)
    if isinstance(node, ast.Name):
        return node.id in seen or _has_unresolved_alternative(values.get(node.id), values, helpers, seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _resolve(node, values, helpers, seen) is None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in helpers:
        helper = helpers[node.func.id]
        local = dict(values)
        for parameter, argument in zip(helper.params, node.args):
            local[parameter] = argument
        for keyword in node.keywords:
            if keyword.arg is not None:
                local[keyword.arg] = keyword.value
        return _has_unresolved_alternative(helper.returned, local, helpers, seen)
    return _resolve(node, values, helpers, seen) is None


def _script_statements(sql: str) -> tuple[str, ...]:
    """Bounded splitter for executescript: comments/empty fragments are ignored."""
    fragments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            current.append(char)
            if char == quote:
                if nxt == quote:  # SQLite doubled quote escape.
                    current.append(nxt)
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == ";":
            fragment = re.sub(r"--[^\n]*(?:\n|$)", "", "".join(current))
            fragment = re.sub(r"/\*.*?\*/", "", fragment, flags=re.DOTALL).strip()
            if fragment:
                fragments.append(fragment)
            current = []
        else:
            current.append(char)
        index += 1
    fragment = re.sub(r"--[^\n]*(?:\n|$)", "", "".join(current))
    fragment = re.sub(r"/\*.*?\*/", "", fragment, flags=re.DOTALL).strip()
    if fragment:
        fragments.append(fragment)
    return tuple(fragments)


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


def _execution_wrappers(statements: Iterable[ast.stmt], values: dict[str, ast.AST], aliases: dict[str, str]) -> dict[str, _ExecutionWrapper]:
    """Recognize only local parameter-to-sink forwarding wrappers."""
    wrappers: dict[str, _ExecutionWrapper] = {}
    # Include methods and lexically nested functions.  The scanner remains
    # lexical/read-only; callers are still resolved only through recognized
    # parameter-to-sink forwarding below.
    defs = [
        node
        for statement in statements
        for node in ast.walk(statement)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    # The bounded model deliberately follows at most three forwarding hops.
    # A longer path remains one fail-closed caller finding rather than a
    # speculative sink execution.
    max_wrapper_depth = 3
    depths: dict[str, int] = {}
    by_name = {node.name: node for node in defs}
    forwarding_edges: dict[str, set[str]] = {name: set() for name in by_name}
    for node in defs:
        params = {arg.arg for arg in node.args.args}
        for call in _calls_in(node):
            target = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else call.func.attr if isinstance(call.func, ast.Attribute) else None
            )
            if target in by_name and any(
                isinstance(argument, ast.Name) and argument.id in params
                for argument in call.args
            ):
                forwarding_edges[node.name].add(target)

    def reaches(origin: str, current: str, seen: set[str]) -> bool:
        if current == origin:
            return True
        return any(
            child == origin or reaches(origin, child, seen | {current})
            for child in forwarding_edges[current]
            if child not in seen or child == origin
        )

    for name, node in by_name.items():
        if any(reaches(name, child, {name}) for child in forwarding_edges[name]):
            params = tuple(arg.arg for arg in node.args.args)
            if params:
                wrappers[name] = _ExecutionWrapper(params, params[-1], "__recursive__")
                depths[name] = 0
    changed = True
    while changed:
        changed = False
        for node in defs:
            if node.name in wrappers:
                continue
            params = tuple(arg.arg for arg in node.args.args)
            local_aliases: dict[str, str] = {}
            for item in node.body:
                _record_assignment(item, {}, local_aliases)
                for call in _calls_in(item):
                    sink = _sink_name(call, {}, local_aliases)
                    arg = _sql_arg(call)
                    if sink and isinstance(arg, ast.Name) and arg.id in params:
                        wrappers[node.name] = _ExecutionWrapper(params, arg.id, sink)
                        depths[node.name] = 1
                        changed = True
                        break
                    if isinstance(call.func, ast.Name) and call.func.id == node.name:
                        recursive_arg = next((value for value in call.args if isinstance(value, ast.Name) and value.id == params[-1]), None)
                        if isinstance(recursive_arg, ast.Name) and recursive_arg.id in params:
                            wrappers[node.name] = _ExecutionWrapper(params, recursive_arg.id, "__recursive__")
                            changed = True
                            break
                    if isinstance(call.func, ast.Name) and call.func.id in wrappers:
                        inner = wrappers[call.func.id]
                        position = inner.params.index(inner.sql_parameter)
                        forwarded = call.args[position] if len(call.args) > position else next((kw.value for kw in call.keywords if kw.arg == inner.sql_parameter), None)
                        if isinstance(forwarded, ast.Name) and forwarded.id in params:
                            depth = depths.get(call.func.id, max_wrapper_depth) + 1
                            wrappers[node.name] = _ExecutionWrapper(
                                params,
                                forwarded.id,
                                inner.sink if depth <= max_wrapper_depth else "__depth__",
                            )
                            depths[node.name] = depth
                            changed = True
                            break
    return wrappers


def _wrapper_argument(call: ast.Call, wrapper: _ExecutionWrapper) -> ast.AST | None:
    position = wrapper.params.index(wrapper.sql_parameter)
    if isinstance(call.func, ast.Attribute) and wrapper.params and wrapper.params[0] in {"self", "cls"}:
        position -= 1
    if len(call.args) > position:
        return call.args[position]
    return next((kw.value for kw in call.keywords if kw.arg == wrapper.sql_parameter), None)


def _returned_wrapper_name(call: ast.Call) -> str | None:
    """Recognize ``factory(receiver)(sql)`` for a lexical returned wrapper."""
    if not isinstance(call.func, ast.Call) or not isinstance(call.func.func, ast.Name):
        return None
    # The caller supplies a local factory whose body returns a nested wrapper.
    # The name is resolved by the bounded AST pass in ``_scan_scope``.
    return call.func.func.id


def _control_expressions(statement: ast.stmt) -> tuple[ast.AST, ...]:
    if isinstance(statement, (ast.If, ast.While)):
        return (statement.test,)
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return (statement.iter,)
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return tuple(item.context_expr for item in statement.items)
    return ()


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
            return SqlFinding(path, scope, line, sink, False, None, "source_trades", (), (), None, None, "unresolved_source_trade_sql", None)
        return None
    match = _DML.search(sql)
    target = _TARGET.search(sql)
    table = target.group(1).lower() if target else None
    if not match or table != "source_trades":
        return None
    operation = " ".join(match.group(1).upper().split())
    parsed = parse_predicate_fingerprint(sql) if operation == "UPDATE" else _PredicateParse(None, None)
    return SqlFinding(
        path,
        scope,
        line,
        sink,
        True,
        operation,
        table,
        _columns(sql, operation),
        _selectors(sql),
        parsed.fingerprint,
        parsed.invalid_reason,
        sql=sql,
    )


def _scan_scope(statements: Iterable[ast.stmt], *, path: str, scope: str, helpers: dict[str, _Helper], values: dict[str, ast.AST] | None = None, aliases: dict[str, str] | None = None, wrappers: dict[str, _ExecutionWrapper] | None = None) -> list[SqlFinding]:
    values, aliases = dict(values or {}), dict(aliases or {})
    helpers = dict(helpers)
    wrappers = {**dict(wrappers or {}), **_execution_wrappers(statements, values, aliases)}
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
                    wrappers=wrappers,
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
                arg = _sql_arg(call)
                wrapper_name = call.func.id if isinstance(call.func, ast.Name) else call.func.attr if isinstance(call.func, ast.Attribute) else None
                force_unresolved = False
                if sink is None and wrapper_name in wrappers:
                    wrapper = wrappers[wrapper_name]
                    force_unresolved = wrapper.sink in {"__recursive__", "__depth__"}
                    sink = "execute" if force_unresolved else wrapper.sink
                    arg = _wrapper_argument(call, wrapper)
                if sink is None:
                    continue
                relevant = _source_relevant(arg, values, helpers) and _dml_relevant(arg, values, helpers)
                resolved_sql = () if force_unresolved else _resolve_all(arg, values, helpers)
                if resolved_sql:
                    for sql in resolved_sql:
                        statements_to_classify = _script_statements(sql) if sink == "executescript" else (sql,)
                        for statement_sql in statements_to_classify:
                            finding = _classify(path, scope, call.lineno, sink, statement_sql, relevant=relevant)
                            if finding is not None:
                                findings.append(finding)
                    if relevant and _has_unresolved_alternative(arg, values, helpers):
                        finding = _classify(path, scope, call.lineno, sink, None, relevant=True)
                        if finding is not None:
                            findings.append(finding)
                else:
                    finding = _classify(path, scope, call.lineno, sink, None, relevant=relevant)
                    if finding is not None:
                        findings.append(finding)
        if is_control:
            # Header expressions are executable too; scan them once, separately
            # from cloned branch bodies so a control call cannot evade inspection.
            for expression in _control_expressions(statement):
                findings.extend(_scan_scope([ast.Expr(value=expression)], path=path, scope=scope, helpers=helpers, values=values, aliases=aliases))
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
            # Preserve every bounded post-control value. A loop may execute
            # zero times, and an ``if`` without ``else`` retains the incoming
            # value on its false path. The synthetic conditional is only an
            # alternative carrier for ``_resolve_all``; it is never executed.
            branch_values: dict[str, list[ast.expr]] = {}
            for body in bodies:
                for item in ast.walk(ast.Module(body=body, type_ignores=[])):
                    if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
                        targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                        for target in targets:
                            if isinstance(target, ast.Name):
                                branch_values.setdefault(target.id, []).append(item.value)
            preserve_incoming = (
                isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.Try))
                or (isinstance(statement, ast.If) and not statement.orelse)
            )
            for name, alternatives in branch_values.items():
                incoming = values.get(name)
                if preserve_incoming and isinstance(incoming, ast.expr):
                    alternatives.append(incoming)
                alias_sinks = {
                    sink
                    for alternative in alternatives
                    if (sink := _sink_from_value(alternative, values, aliases)) is not None
                }
                if len(alias_sinks) == 1:
                    aliases[name] = alias_sinks.pop()
                else:
                    aliases.pop(name, None)
                if len(alternatives) == 1:
                    values[name] = alternatives[0]
                    continue
                value = alternatives[-1]
                for alternative in reversed(alternatives[:-1]):
                    value = ast.IfExp(test=ast.Constant(value=True), body=alternative, orelse=value)
                values[name] = value
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


def _is_explicit_schema_or_demo_role(finding: SqlFinding) -> bool:
    """Exact non-runtime exceptions; similar names never confer write authority."""
    if finding.path in {"scripts/seed_demo_data.py", "scripts/live_smoke_pr3_fixes.py"}:
        return True
    if finding.path == "src/polycopy/db/schema.py":
        return finding.operation in {"INSERT", "INSERT OR IGNORE"}
    # PR24Z is the sole audited migration that repairs canonical identity.
    return (
        finding.path == "src/polycopy/migrations/pr24z_canonical_identity.py"
        and finding.operation == "UPDATE"
        and finding.columns == ("source_trade_id",)
    )


def contract_violations(findings: Iterable[SqlFinding]) -> list[SqlFinding]:
    """Enforce the three deliberately narrow production source-trade roles.

    Reconciliation statements are authorized only when their predicate
    fingerprint matches the exact bounded grammar. Selector-column sets
    alone never certify a finding as approved.
    """
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
        elif finding.path == "src/polycopy/ingestion/source_trade_writer.py":
            if finding.operation != "INSERT OR IGNORE" or finding.columns not in writer_columns:
                violations.append(finding)
        elif finding.path == "src/polycopy/ingestion/source_trade_metadata_reconciliation.py":
            approved_fps = {
                PREDICATE_METADATA_BY_ID,
                PREDICATE_METADATA_BY_IDENTITY,
            }
            if (
                finding.operation != "UPDATE"
                or finding.columns != ("metadata_json",)
                or finding.predicate_fingerprint is None
                or finding.predicate_fingerprint not in approved_fps
            ):
                violations.append(finding)
        elif finding.path == "src/polycopy/ingestion/source_trade_resolution.py":
            if (
                finding.operation != "UPDATE"
                or set(finding.columns) != resolution_columns
                or finding.predicate_fingerprint != PREDICATE_RESOLUTION_BY_ID
            ):
                violations.append(finding)
        elif not _is_explicit_schema_or_demo_role(finding):
            violations.append(finding)
    return violations
