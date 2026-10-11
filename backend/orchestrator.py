"""The pipeline. Owns the sequence; stages never call each other.

    [1] route -> [2] validate -> [3] write SQL -> [4] check+execute (retry [3] up to N) -> [5] compose

`run()` yields progress events and finishes with {"event": "result", "data": ChatResponse}.
`answer()` is the blocking form used by POST /api/chat and the tests.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

import config
from agents.composer import compose
from agents.router import route
from agents.sql_writer import write_sql
from agents.validator import validate
from llm import LLMStageError
from logging_setup import get_logger, request_id
from models import ChatResponse, CheckExecuteResult, FinalAnswer, HistoryTurn, SqlResult
from tools.db import DatabaseNotConfigured
from tools.chart_check import check_chart
from tools.sql_check import check_and_execute

log = get_logger("orchestrator")

STAGE_LABELS = {
    "route": "Understanding your question…",
    "validate": "Checking what's possible…",
    "sql": "Writing the query…",
    "execute": "Running it…",
    "compose": "Writing up the answer…",
}


class _Run:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.meta: dict[str, Any] = {
            "request_id": uuid.uuid4().hex[:12],
            "stages": {},
            "timings_ms": {},
            "models": {},
            "retry_count": 0,
        }

    def record(self, stage: str, llm_meta: dict | None, elapsed_ms: int) -> None:
        self.meta["timings_ms"][stage] = self.meta["timings_ms"].get(stage, 0) + elapsed_ms
        if llm_meta:
            self.meta["stages"].setdefault(stage, []).append(llm_meta)
            self.meta["models"][stage] = llm_meta.get("model")

    def finish(self, response: ChatResponse) -> ChatResponse:
        self.meta["timings_ms"]["total"] = int((time.perf_counter() - self.started) * 1000)
        response.meta = {**self.meta, **response.meta}
        log.info(f"done type={response.type} total_ms={self.meta['timings_ms']['total']} retries={self.meta['retry_count']}")
        log.debug("response", extra={"data": response.model_dump(mode="json")})
        return response


def _stage_event(stage: str, attempt: int = 1) -> dict:
    return {"event": "stage", "stage": stage, "label": STAGE_LABELS[stage], "attempt": attempt}


def _error_response(run: _Run, message: str, *, stage: str, detail: str, kind: str, sql: str | None = None, extra: dict | None = None) -> ChatResponse:
    run.meta["error"] = {"stage": stage, "type": kind, "detail": detail, **(extra or {})}
    return ChatResponse(type="error", message=message, sql=sql)


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
    history = history or []
    r = _Run()
    rid = r.meta["request_id"]
    yield {"event": "start", "request_id": rid}
    pipeline = _pipeline(r, message.strip(), history)
    while True:
        # Starlette runs each step of a sync generator in a fresh context copy, so the
        # request id is set per step rather than once with set()/reset().
        request_id.set(rid)
        try:
            event = next(pipeline)
        except StopIteration as done:
            yield {"event": "result", "data": done.value}
            return
        yield event


def _pipeline(r: _Run, message: str, history: list[HistoryTurn]) -> Iterator[dict]:
    """Generator that yields progress events and *returns* the final ChatResponse."""
    if not message:
        return r.finish(ChatResponse(type="error", message="Please type a question.", meta={"error": {"stage": "input", "type": "empty"}}))
    if len(message) > config.MAX_QUESTION_CHARS:
        return r.finish(ChatResponse(
            type="error",
            message=f"That message is too long ({len(message)} characters). Please keep questions under {config.MAX_QUESTION_CHARS}.",
            meta={"error": {"stage": "input", "type": "too_long"}},
        ))

    stage = "route"
    sql_text: str | None = None
    try:
        # [1] route
        yield _stage_event("route")
        t = time.perf_counter()
        routed, m = route(message, history)
        r.record("route", m, int((time.perf_counter() - t) * 1000))
        r.meta["route"] = routed.route
        if routed.route == "general":
            return r.finish(ChatResponse(type="general", message=routed.reply))
        if routed.route == "out_of_scope":
            return r.finish(ChatResponse(type="declined", message=routed.reply))
        resolved = routed.resolved_query
        assert resolved is not None
        r.meta["resolved_query"] = resolved

        # [2] validate
        stage = "validate"
        yield _stage_event("validate")
        t = time.perf_counter()
        validation, m = validate(resolved)
        r.record("validate", m, int((time.perf_counter() - t) * 1000))
        r.meta["validation"] = validation.model_dump(mode="json")
        if not validation.answerable:
            return r.finish(ChatResponse(type="not_answerable", message=validation.reason))

        # [3] + [4] with retries
        sql_result: SqlResult | None = None
        executed: CheckExecuteResult | None = None
        previous_sql: str | None = None
        previous_errors: list[str] | None = None
        attempts: list[dict] = []
        for attempt in range(1, config.MAX_SQL_RETRIES + 2):
            stage = "sql"
            yield _stage_event("sql", attempt)
            t = time.perf_counter()
            sql_result, m = write_sql(resolved, validation, previous_sql, previous_errors)
            r.record("sql", m, int((time.perf_counter() - t) * 1000))
            sql_text = sql_result.sql

            stage = "execute"
            yield _stage_event("execute", attempt)
            t = time.perf_counter()
            executed = check_and_execute(sql_result.sql)
            r.record("execute", None, int((time.perf_counter() - t) * 1000))
            attempts.append({"attempt": attempt, "sql": sql_result.sql, "ok": executed.ok,
                             "errors": executed.errors, "warnings": executed.warnings})
            if executed.ok:
                sql_text = executed.normalized_sql or sql_result.sql
                break
            previous_sql, previous_errors = sql_result.sql, executed.errors
            log.info(f"sql attempt {attempt} rejected: {executed.errors}")
        r.meta["sql_attempts"] = attempts
        r.meta["retry_count"] = len(attempts) - 1
        assert sql_result is not None and executed is not None

        if not executed.ok:
            return r.finish(_error_response(
                r,
                "I couldn't build a query that passes the safety and correctness checks for this question. "
                "The last attempt is shown below. Try rephrasing, or narrowing the question.",
                stage="execute", kind="sql_rejected", detail="; ".join(executed.errors), sql=sql_text,
                extra={"attempts": len(attempts)},
            ))
        r.meta["row_count"] = executed.row_count
        r.meta["truncated"] = executed.truncated
        r.meta["sql_warnings"] = executed.warnings

        # [5] compose
        stage = "compose"
        yield _stage_event("compose")
        t = time.perf_counter()
        composed, m = compose(resolved, validation, sql_result, executed)
        r.record("compose", m, int((time.perf_counter() - t) * 1000))
        chart, chart_warnings = check_chart(composed.chart, executed)
        r.meta["chart_suggested"] = composed.chart.type
        if chart_warnings:
            r.meta["chart_warnings"] = chart_warnings
        final = FinalAnswer(
            summary=composed.summary,
            details=composed.details,
            insights=composed.insights,
            caveats=_merge_caveats(validation.caveats, composed.caveats),
            chart=chart,
        )
        return r.finish(ChatResponse(
            type="answer",
            answer=final,
            table={"columns": executed.columns, "rows": executed.rows,
                   "row_count": executed.row_count, "truncated": executed.truncated},
            sql=sql_text,
        ))

    except LLMStageError as e:
        log.exception(f"stage {e.stage} failed")
        return r.finish(_error_response(
            r, f"Something went wrong while {_doing(e.stage)}: {e.message}.",
            stage=e.stage, kind="llm", detail=e.detail, sql=sql_text, extra={"llm": e.meta},
        ))
    except DatabaseNotConfigured as e:
        log.exception("database not configured")
        return r.finish(_error_response(
            r, "The database connection isn't configured on the server (DATABASE_URL is missing).",
            stage=stage, kind="config", detail=str(e), sql=sql_text,
        ))
    except Exception as e:  # surface the real cause in meta, a readable one to the user
        log.exception(f"unexpected failure in stage {stage}")
        return r.finish(_error_response(
            r, f"Something went wrong while {_doing(stage)} ({type(e).__name__}). The details are in the response meta and server log.",
            stage=stage, kind=type(e).__name__, detail=str(e), sql=sql_text,
        ))


def _doing(stage: str) -> str:
    return {
        "route": "understanding your question",
        "validate": "checking whether the data can answer it",
        "sql": "writing the query",
        "execute": "running the query",
        "compose": "writing up the answer",
    }.get(stage, "processing your question")


def answer(message: str, history: list[HistoryTurn] | None = None) -> ChatResponse:
    final: ChatResponse | None = None
    for event in run(message, history):
        if event["event"] == "result":
            final = event["data"]
    assert final is not None
    return final
