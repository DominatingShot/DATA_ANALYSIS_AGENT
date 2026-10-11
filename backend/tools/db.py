"""Read-only database access: connection pool, column introspection, guarded execution.

Read-only is enforced by the database role (backend/sql/readonly_role.sql). The session settings
below (read-only transactions, statement timeout) are a second layer, not the only one.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

import config
from logging_setup import get_logger

log = get_logger("db")

_pool: ConnectionPool | None = None
_catalog: dict[str, dict[str, str]] | None = None


class DatabaseNotConfigured(RuntimeError):
    pass


def _configure(conn: psycopg.Connection) -> None:
    conn.read_only = True  # every transaction on this connection is READ ONLY


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not config.DATABASE_URL:
            raise DatabaseNotConfigured("DATABASE_URL is not set")
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


def introspect_catalog(refresh: bool = False) -> dict[str, dict[str, str]]:
    """{table: {column: data_type}} for every table in `public` this role can see.

    information_schema only lists objects the current role has privileges on, so the allowed
    table set is exactly what the read-only role was granted - nothing is hardcoded here.
    """
    global _catalog
    if _catalog is not None and not refresh:
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
    log.debug("catalog.introspected", extra={"data": catalog})
    return catalog


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
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


def execute(sql: str, max_rows: int | None = None) -> dict[str, Any]:
    """Run one already-validated SELECT. Returns columns, JSON-safe rows, truncation and timing."""
    max_rows = max_rows or config.MAX_ROWS
    started = time.perf_counter()
    with get_pool().connection() as conn:
        with conn.transaction():
            _begin_guarded(conn)
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [d.name for d in cur.description] if cur.description else []
                fetched = cur.fetchmany(max_rows + 1)
    truncated = len(fetched) > max_rows
    rows = [[_json_safe(v) for v in row] for row in fetched[:max_rows]]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "execution_ms": int((time.perf_counter() - started) * 1000),
    }


def health() -> dict[str, Any]:
    """Connectivity plus a check that the connected role really cannot write."""
    try:
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
        role_read_only = not writable and not can_create
        return {
            "connected": True,
            "user": user,
            "transaction_read_only": ro == "on",
            "role_read_only": role_read_only,
            "writable_tables": writable,
            "can_create_in_public": can_create,
            "tables": tables,
        }
    except DatabaseNotConfigured as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:  # report the real cause to the health endpoint
        return {"connected": False, "error": f"{type(e).__name__}: {e}"}
