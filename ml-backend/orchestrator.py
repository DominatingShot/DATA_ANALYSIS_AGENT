"""The pipeline. Owns the sequence; stages never call each other.

    [1] route -> [2] validate -> [3] write SQL -> [4] check+execute (retry [3] up to N) -> [5] compose

`run()` yields progress events and finishes with {"event": "result", "data": ChatResponse}.
`answer()` is the blocking form used by POST /api/chat.
"""

from __future__ import annotations

from collections.abc import Iterator

import config
from agents.composer import compose
from agents.router import route
from agents.sql_writer import write_sql
from agents.validator import validate
from models import ChatResponse, FinalAnswer, HistoryTurn
from tools.chart_check import check_chart
from tools.sql_check import check_and_execute

_DOING = {
    "route": "understanding your question",
    "validate": "checking whether the data can answer it",
    "sql": "writing the query",
    "execute": "running the query",
    "compose": "writing up the answer",
}


def _stage_event(stage: str, attempt: int = 1) -> dict:
    return {"event": "stage", "stage": stage, "attempt": attempt}


def _merge_caveats(mandatory: list[str], composed: list[str]) -> list[str]:
    """Validation caveats are mandatory and verbatim; the composer may only add to them."""
    merged = list(mandatory)
    seen = {c.strip().lower() for c in mandatory}
    for c in composed:
        if c.strip().lower() not in seen:
            merged.append(c)
            seen.add(c.strip().lower())
    return merged


def run(message: str, history: list[HistoryTurn] | None = None) -> Iterator[dict]:
    final = yield from _pipeline(message.strip(), history or [])
    yield {"event": "result", "data": final}


def _pipeline(message: str, history: list[HistoryTurn]) -> Iterator[dict]:
    """Generator that yields progress events and *returns* the final ChatResponse."""
    if not message:
        return ChatResponse(type="error", message="Please type a question.")
    if len(message) > config.MAX_QUESTION_CHARS:
        return ChatResponse(
            type="error",
            message=f"That message is too long ({len(message)} characters). Please keep questions under {config.MAX_QUESTION_CHARS}.",
        )

    stage = "route"
    sql_text: str | None = None
    meta: dict = {}
    try:
        # [1] route
        yield _stage_event("route")
        routed = route(message, history)
        if routed.route == "general":
            return ChatResponse(type="general", message=routed.reply)
        if routed.route == "out_of_scope":
            return ChatResponse(type="declined", message=routed.reply)
        resolved = routed.resolved_query
        meta["resolved_query"] = resolved

        # [2] validate
        stage = "validate"
        yield _stage_event("validate")
        validation = validate(resolved)
        if not validation.answerable:
            return ChatResponse(type="not_answerable", message=validation.reason, meta=meta)

        # [3] + [4] with retries
        previous_sql: str | None = None
        previous_errors: list[str] | None = None
        for attempt in range(1, config.MAX_SQL_RETRIES + 2):
            stage = "sql"
            yield _stage_event("sql", attempt)
            sql_result = write_sql(resolved, validation, previous_sql, previous_errors)
            sql_text = sql_result.sql

            stage = "execute"
            yield _stage_event("execute", attempt)
            executed = check_and_execute(sql_result.sql)
            if executed.ok:
                sql_text = executed.normalized_sql
                break
            previous_sql, previous_errors = sql_result.sql, executed.errors
        else:
            meta["error"] = {"stage": "execute", "type": "sql_rejected", "detail": "; ".join(executed.errors)}
            return ChatResponse(
                type="error",
                message="I couldn't build a query that passes the safety and correctness checks for this question. "
                "The last attempt is shown below. Try rephrasing, or narrowing the question.",
                sql=sql_text,
                meta=meta,
            )

        # [5] compose
        stage = "compose"
        yield _stage_event("compose")
        composed = compose(resolved, validation, sql_result, executed)
        final = FinalAnswer(
            summary=composed.summary,
            details=composed.details,
            insights=composed.insights,
            caveats=_merge_caveats(validation.caveats, composed.caveats),
            chart=check_chart(composed.chart, executed),
        )
        return ChatResponse(
            type="answer",
            answer=final,
            table={"columns": executed.columns, "rows": executed.rows,
                   "row_count": executed.row_count, "truncated": executed.truncated},
            sql=sql_text,
            meta=meta,
        )

    except Exception as e:  # turn any failure into a readable error response instead of a broken stream
        meta["error"] = {"stage": stage, "type": type(e).__name__, "detail": str(e)}
        return ChatResponse(type="error", message=f"Something went wrong while {_DOING[stage]}.", sql=sql_text, meta=meta)


def answer(message: str, history: list[HistoryTurn] | None = None) -> ChatResponse:
    *_, last = run(message, history)
    return last["data"]
