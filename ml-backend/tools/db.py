"""Read-only database access: connection pool, column introspection, guarded execution.

Read-only is enforced by the database role (sql/readonly_role.sql). The session settings
below (read-only transactions, statement timeout) are a second layer, not the only one.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

import config

_pool: ConnectionPool | None = None
_catalog: dict[str, dict[str, str]] | None = None


def _configure(conn: psycopg.Connection) -> None:
    conn.read_only = True  # every transaction on this connection is READ ONLY


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.DATABASE_URL,
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            configure=_configure,
            kwargs={"application_name": "alankar-analytics"},
            open=True,
            timeout=10,
        )
    return _pool


def close_pool() -> None:
    global _pool, _catalog
    if _pool is not None:
        _pool.close()
    _pool = None
    _catalog = None


def introspect_catalog() -> dict[str, dict[str, str]]:
    """{table: {column: data_type}} for every table in `public` this role can see.

    information_schema only lists objects the current role has privileges on, so the allowed
    table set is exactly what the read-only role was granted - nothing is hardcoded here.
    """
    global _catalog
    if _catalog is not None:
        return _catalog
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        ).fetchall()
    catalog: dict[str, dict[str, str]] = {}
    for table, column, dtype in rows:
        catalog.setdefault(table, {})[column] = dtype
    _catalog = catalog
    return catalog


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (dt.timedelta, uuid.UUID)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (bytes, memoryview)):
        return "<binary>"
    return json.loads(json.dumps(value, default=str))


def _begin_guarded(conn: psycopg.Connection) -> None:
    # SET LOCAL lasts until the end of the current transaction only.
    conn.execute(f"SET LOCAL statement_timeout = '{config.STATEMENT_TIMEOUT}'")


def explain_cost(sql: str) -> float:
    """Planner's estimated total cost. EXPLAIN without ANALYZE never runs the query."""
    with get_pool().connection() as conn:
        with conn.transaction():
            _begin_guarded(conn)
            plan = conn.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchone()[0]
    if isinstance(plan, str):
        plan = json.loads(plan)
    return float(plan[0]["Plan"]["Total Cost"])


def execute(sql: str) -> dict[str, Any]:
    """Run one already-validated SELECT. Returns columns, JSON-safe rows and truncation."""
    max_rows = config.MAX_ROWS
    with get_pool().connection() as conn:
        with conn.transaction():
            _begin_guarded(conn)
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [d.name for d in cur.description] if cur.description else []
                fetched = cur.fetchmany(max_rows + 1)
    rows = [[_json_safe(v) for v in row] for row in fetched[:max_rows]]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": len(fetched) > max_rows}


def health() -> dict[str, Any]:
    """Connectivity plus a check that the connected role really cannot write."""
    with get_pool().connection() as conn:
        user, ro = conn.execute("SELECT current_user, current_setting('transaction_read_only')").fetchone()
        tables = [r[0] for r in conn.execute(
            "SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema='public' ORDER BY 1"
        ).fetchall()]
        writable = [r[0] for r in conn.execute(
            """
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
              AND (has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE')
                   OR has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE'))
            """
        ).fetchall()]
        can_create = conn.execute("SELECT has_schema_privilege('public', 'CREATE')").fetchone()[0]
    return {
        "user": user,
        "transaction_read_only": ro == "on",
        "role_read_only": not writable and not can_create,
        "writable_tables": writable,
        "can_create_in_public": can_create,
        "tables": tables,
    }
