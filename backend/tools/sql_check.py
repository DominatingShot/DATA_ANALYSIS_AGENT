"""[4] Check + execute: deterministic, no LLM.

Phase 1 - static check with sqlglot (postgres dialect). Never skipped.
Phase 2 - EXPLAIN cost guard, then execution on the read-only pool.

The allowed tables and columns come from `db.introspect_catalog()` (information_schema),
never from a hardcoded list.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import psycopg
import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

import config
from logging_setup import get_logger, log_stage
from models import CheckExecuteResult
from tools import db

log = get_logger("sql_check")

Catalog = dict[str, dict[str, str]]

# Nodes that write, lock or change state. A Select root can still hide these in a CTE.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command",
                 "TruncateTable", "Into", "Lock", "Grant", "Copy", "Set", "Transaction",
                 "Commit", "Rollback", "Use", "Pragma")
    if hasattr(exp, name)
)

# Functions with side effects or server-file / network access, plus pg_sleep.
_DENIED_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_read_file", "pg_read_binary_file",
    "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "lo_get", "set_config",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "dblink", "dblink_exec", "dblink_connect", "pg_advisory_lock", "pg_advisory_xact_lock",
    "txid_current", "nextval", "setval", "query_to_xml", "pg_notify",
}

# Table-valued functions allowed in FROM.
_ALLOWED_TABLE_FUNCTIONS = {"generate_series", "unnest"}

JOIN_KEY = "menu_sk"
JOINED_TABLES = ("dim_menu", "fact_sales")


@dataclass
class StaticCheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized_sql: str | None = None


def _func_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return (node.sql_name() if hasattr(node, "sql_name") else type(node).__name__).lower()


def _check_tables(ast: exp.Expression, catalog: Catalog, errors: list[str]) -> None:
    cte_names = {cte.alias_or_name.lower() for cte in ast.find_all(exp.CTE)}
    for table in ast.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            name = _func_name(table.this)
            # sqlglot may name these e.g. "exploding_generate_series"
            if not any(name == f or name.endswith("_" + f) for f in _ALLOWED_TABLE_FUNCTIONS):
                errors.append(f"Table function `{name}` is not allowed")
            continue
        name = table.name.lower()
        schema = (table.db or "").lower()
        if table.catalog:
            errors.append(f"Unknown table `{table.sql(dialect='postgres')}`")
            continue
        if not schema and name in cte_names:
            continue
        if schema not in ("", "public") or name not in catalog:
            errors.append(f"Unknown table `{table.sql(dialect='postgres')}`")


def _check_no_star(ast: exp.Expression, errors: list[str]) -> None:
    for select in ast.find_all(exp.Select):
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
            ):
                errors.append("Explicit columns required: SELECT * is not allowed")
                return


def _column_table(col: exp.Column, alias_map: dict[str, str], catalog: Catalog) -> str | None:
    """Physical table a column belongs to within one scope, if it is one of the joined tables."""
    if col.table:
        return alias_map.get(col.table.lower())
    owners = {t for t in set(alias_map.values()) if col.name.lower() in catalog.get(t, {})}
    return owners.pop() if len(owners) == 1 else None


def _check_join_key(ast: exp.Expression, catalog: Catalog, errors: list[str]) -> None:
    """If dim_menu and fact_sales meet in one FROM clause, they must be linked on menu_sk only."""
    try:
        scopes = list(traverse_scope(ast))
    except Exception as e:  # scope building failed; qualify will report the underlying problem
        log.debug("scope traversal failed", extra={"data": str(e)})
        return
    for scope in scopes:
        alias_map: dict[str, str] = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table) and source.name.lower() in JOINED_TABLES:
                alias_map[alias.lower()] = source.name.lower()
        if set(alias_map.values()) != set(JOINED_TABLES):
            continue

        select = scope.expression
        conditions: list[exp.Expression] = []
        good_links = 0
        for join in select.args.get("joins") or []:
            if join.args.get("on"):
                conditions.append(join.args["on"])
            using = join.args.get("using") or []
            if using:
                names = {u.name.lower() for u in using}
                if names == {JOIN_KEY}:
                    good_links += 1
                else:
                    errors.append(
                        f"Join must be on {JOIN_KEY} - see schema.md §2.1 (found USING {sorted(names)})"
                    )
        if select.args.get("where"):
            conditions.append(select.args["where"].this)

        for cond in conditions:
            for eq in cond.find_all(exp.EQ):
                left, right = eq.left, eq.right
                if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                    continue
                lt, rt = _column_table(left, alias_map, catalog), _column_table(right, alias_map, catalog)
                if {lt, rt} != set(JOINED_TABLES):
                    continue
                if left.name.lower() == JOIN_KEY and right.name.lower() == JOIN_KEY:
                    good_links += 1
                else:
                    errors.append(
                        f"Join must be on {JOIN_KEY} - see schema.md §2.1 "
                        f"(found {eq.sql(dialect='postgres')})"
                    )
        if good_links == 0 and not any("Join must be on" in e for e in errors):
            errors.append(
                f"Join must be on {JOIN_KEY} - see schema.md §2.1 "
                "(dim_menu and fact_sales are combined without a menu_sk join)"
            )


def _check_functions(ast: exp.Expression, errors: list[str]) -> None:
    for func in ast.find_all(exp.Func):
        name = _func_name(func)
        if name in _DENIED_FUNCTIONS or name.startswith("dblink"):
            errors.append(f"Function `{name}` is not allowed")


def _check_columns(ast: exp.Expression, catalog: Catalog, errors: list[str]) -> None:
    try:
        qualify(
            ast.copy(),
            schema=catalog,
            dialect="postgres",
            validate_qualify_columns=True,
            quote_identifiers=False,
            expand_stars=False,
        )
    except OptimizeError as e:
        msg = str(e)
        if "could not be resolved" in msg or "Unknown column" in msg:
            name = msg.split("'")[1] if msg.count("'") >= 2 else msg.split(":")[-1].strip()
            errors.append(f"Unknown column `{name}` ({msg})")
        else:
            errors.append(f"Column resolution failed: {msg}")


def _is_bare_aggregate(select: exp.Select) -> bool:
    if select.args.get("group"):
        return False
    return bool(select.expressions) and all(
        p.find(exp.AggFunc) is not None and p.find(exp.Window) is None for p in select.expressions
    )


def static_check(sql: str, catalog: Catalog, row_cap: int | None = None) -> StaticCheckResult:
    row_cap = row_cap or config.MAX_ROWS
    errors: list[str] = []
    warnings: list[str] = []

    if not sql or not sql.strip():
        return StaticCheckResult(False, ["Syntax error: empty query"])

    try:
        statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except ParseError as e:
        return StaticCheckResult(False, [f"Syntax error: {e}"])

    if len(statements) != 1:
        return StaticCheckResult(False, ["Multiple statements not allowed" if statements else "Syntax error: no statement"])
    ast = statements[0]

    if not isinstance(ast, exp.Select):
        return StaticCheckResult(False, [f"Only SELECT queries are permitted (got {type(ast).__name__.upper()})"])

    forbidden = sorted({type(n).__name__.upper() for n in ast.find_all(*_FORBIDDEN_NODES)})
    if forbidden:
        return StaticCheckResult(False, [f"Only SELECT queries are permitted (found {', '.join(forbidden)})"])

    _check_tables(ast, catalog, errors)
    _check_no_star(ast, errors)
    _check_functions(ast, errors)
    _check_join_key(ast, catalog, errors)
    if not any(e.startswith("Unknown table") for e in errors):
        _check_columns(ast, catalog, errors)

    if errors:
        return StaticCheckResult(False, list(dict.fromkeys(errors)), warnings)

    if not ast.args.get("limit") and not ast.args.get("fetch") and not _is_bare_aggregate(ast):
        ast = ast.limit(row_cap)
        warnings.append(f"No LIMIT given; injected LIMIT {row_cap}")

    return StaticCheckResult(True, [], warnings, ast.sql(dialect="postgres", pretty=True))


def check_and_execute(sql: str, catalog: Catalog | None = None) -> CheckExecuteResult:
    """The deterministic stage [4]. Static check, cost guard, then guarded execution."""
    log_stage(log, "check_execute", "input", {"sql": sql})
    started = time.perf_counter()
    if catalog is None:
        catalog = db.introspect_catalog()

    check = static_check(sql, catalog)
    if not check.ok:
        result = CheckExecuteResult(ok=False, errors=check.errors, warnings=check.warnings)
        log_stage(log, "check_execute", "output", result)
        return result
    normalized = check.normalized_sql
    assert normalized is not None

    try:
        cost = db.explain_cost(normalized)
    except psycopg.Error as e:
        result = CheckExecuteResult(
            ok=False, errors=[f"Database rejected the query at EXPLAIN: {_pg_message(e)}"],
            warnings=check.warnings, normalized_sql=normalized,
        )
        log_stage(log, "check_execute", "output", result)
        return result
    if cost > config.MAX_QUERY_COST:
        result = CheckExecuteResult(
            ok=False,
            errors=[f"Query too expensive: estimated cost {cost:,.0f} exceeds limit {config.MAX_QUERY_COST:,.0f}. "
                    "Aggregate more, filter more, or avoid row-multiplying joins."],
            warnings=check.warnings, normalized_sql=normalized,
        )
        log_stage(log, "check_execute", "output", result)
        return result

    try:
        out = db.execute(normalized)
    except psycopg.errors.QueryCanceled:
        result = CheckExecuteResult(
            ok=False, errors=[f"Query exceeded the {config.STATEMENT_TIMEOUT} statement timeout"],
            warnings=check.warnings, normalized_sql=normalized,
        )
        log_stage(log, "check_execute", "output", result)
        return result
    except psycopg.Error as e:
        result = CheckExecuteResult(
            ok=False, errors=[f"Database error: {_pg_message(e)}"],
            warnings=check.warnings, normalized_sql=normalized,
        )
        log_stage(log, "check_execute", "output", result)
        return result

    warnings = list(check.warnings)
    if out["truncated"]:
        warnings.append(f"Result truncated to {config.MAX_ROWS} rows")
    result = CheckExecuteResult(
        ok=True, errors=[], warnings=warnings, normalized_sql=normalized,
        columns=out["columns"], rows=out["rows"], row_count=out["row_count"],
        truncated=out["truncated"], execution_ms=int((time.perf_counter() - started) * 1000),
    )
    log_stage(log, "check_execute", "output", {**result.model_dump(mode="json"), "rows": f"<{result.row_count} rows>", "explain_cost": cost})
    return result


def _pg_message(e: psycopg.Error) -> str:
    diag = getattr(e, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag else None
    hint = getattr(diag, "message_hint", None) if diag else None
    msg = primary or str(e).strip()
    return f"{msg} (hint: {hint})" if hint else msg
