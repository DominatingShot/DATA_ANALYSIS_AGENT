"""[3] SQL agent - one PostgreSQL SELECT that answers the validated question."""

from __future__ import annotations

import json

import config
from llm import call_structured
from logging_setup import get_logger, log_stage
from models import SQL_TOOL, SqlResult, ValidationResult

log = get_logger("sql_writer")

INSTRUCTIONS = f"""\
You write one PostgreSQL query for a restaurant analytics chatbot. The database is documented \
in schema.md (provided below). A feasibility check has already run; its result is in the \
message and is binding.

Rules:
- Exactly one SELECT statement (WITH ... SELECT is fine). No DDL, no DML, no SET, no \
transactions, no multiple statements, no trailing commentary. No SELECT * - name every column.
- Use only the tables and columns documented in schema.md.
- Follow every critical rule in schema.md §2. They produce wrong numbers without raising errors.
- Use the exact dates in `time_range` from the feasibility result; do not re-derive them. The \
range is inclusive at both ends, so filter sale_date half-open: \
sale_date >= from_date AND sale_date < (to_date + 1 day), written as literal DATE values.
- Follow `notes_for_sql_agent` and the chosen definitions in `interpretation`.
- Always include a LIMIT (at most {config.MAX_ROWS}) unless the query returns a single \
aggregate row.
- Alias every output column to a readable snake_case name (revenue, units_sold, month, \
item_name), never sum / count / ?column?. Round money to 2 decimals.
- Order rows meaningfully: time series chronologically, rankings by the ranked measure.
- Return the rows the answer needs and no more; for "which X is top" include a short ranked \
list (top 5-10) rather than only one row, so the answer has context.
- `explanation`: one or two plain-English sentences on what the query computes, no SQL jargon.
- `expected_columns`: the output column aliases in order.

If the message includes a previous attempt and its error: read the error, fix the actual \
cause, and submit a different query. Never resubmit the same query.

schema.md follows.
"""


def write_sql(
    resolved_query: str,
    validation: ValidationResult,
    previous_sql: str | None = None,
    error: list[str] | None = None,
) -> tuple[SqlResult, dict]:
    parts = [
        f"<question>\n{resolved_query}\n</question>",
        "<feasibility_result>\n" + json.dumps(validation.model_dump(mode="json"), indent=2) + "\n</feasibility_result>",
    ]
    if previous_sql is not None:
        parts.append(
            "<previous_attempt>\n"
            f"<sql>\n{previous_sql}\n</sql>\n"
            "<errors>\n" + "\n".join(f"- {e}" for e in (error or [])) + "\n</errors>\n"
            "</previous_attempt>\n"
            "The previous attempt failed. Fix the cause and submit a corrected query."
        )
    user = "\n\n".join(parts)
    log_stage(log, "sql", "input", {
        "resolved_query": resolved_query, "previous_sql": previous_sql, "error": error,
    })
    result, meta = call_structured(
        stage="sql",
        tool=SQL_TOOL,
        model=config.MODEL_SQL,
        instructions=INSTRUCTIONS,
        cached_context=config.load_schema_md(),
        user=user,
        effort=config.EFFORT_SQL,
    )
    log_stage(log, "sql", "output", result)
    return result, meta
