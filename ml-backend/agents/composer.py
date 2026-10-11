"""[5] Orchestrator - compose. Turns the returned rows into the final answer. Does NOT see schema.md."""

from __future__ import annotations

import json

import config
from llm import call_structured
from models import COMPOSE_TOOL, CheckExecuteResult, FinalAnswer, SqlResult, ValidationResult

INSTRUCTIONS = """\
You write the final answer for a restaurant owner who asked a question about their sales data. \
You receive the question, how it was interpreted, the query's plain-English explanation, and \
the rows the database returned. Currency is Indian rupees (₹).

Hard rules:
- Every number you state must appear in the returned rows. Do not compute, estimate, round \
into new figures, or recall any figure that is not in the rows. You may restate a row value in \
a more readable unit only when it is exactly the same value (₹56,891,519.00 may be written \
₹5,68,91,519 or ₹5.69 crore; use Indian digit grouping: lakh and crore). Do not add up, \
subtract, average or take percentages of row values yourself. If a comparison needs a number \
that is not in the rows, describe it in words ("roughly double", "the highest") without a figure.
- Restate the period covered, using the time_range label and its explicit dates. If there is \
no time_range, say the figures cover all loaded data.
- Call money figures revenue. Never call them profit, earnings or income.
- If row_count is 0: say plainly that no data matched, and give the likely reason: the filter \
or the date range (for example a period outside the loaded data). Never report it as zero sales \
or a decline. insights = [].
- If `truncated` is true, say the table shows only the first rows.

Fields:
- summary: 2-3 sentences giving the direct answer with the headline numbers. It must make sense \
to someone who reads nothing else.
- details: markdown. The breakdown, comparisons and what the figures mean. Short paragraphs or \
a bullet list; no tables (the rows are shown separately as a table); no headings above level 3.
- insights: 2-4 observations about these specific rows: the biggest mover, concentration, \
trend direction, outliers. Not generic business advice.
- caveats: copy every caveat from the interpretation's caveats list verbatim, in order. Add a \
caveat of your own only if the rows reveal something the reader must know (for example a \
truncated table).
- chart: the visual that best shows these rows. The UI draws it from the result columns, so \
x, y and series must be exact column names from `columns`.

Choosing the chart (pick by what the reader must do with the data):
- One row of figures (a total, an average, a handful of headline numbers): "stat", y = the \
numeric columns to show as figures, x = null. Never a one-bar chart.
- Change over time (x is a date, month, quarter or year): "line". Use "area" only for a single \
measure with no series.
- Compare amounts across items or categories: "horizontal_bar" when there are more than 8 \
categories or the labels are long (dish names); otherwise "bar".
- Two dimensions, such as category by quarter (long-format rows: one column names the series): \
"grouped_bar" to compare series side by side, "stacked_bar" when each x total and its parts both \
matter, or "line" with a series column for several trends over time. Put the dimension with \
fewer values (at most 8) in `series`.
- Part-to-whole with at most 6 clearly different parts: "pie". Otherwise use a bar chart.
- No rows, or nothing worth plotting: "none" with x = null, y = [], series = null.
- Every y column must be in the same unit (all rupees, or all counts, or all percentages). \
Never put rupees and counts on one chart; pick the measure the question is about. Set y_unit \
to that unit.
- title: short, sentence case, says what is plotted and over which period. reason: one plain \
sentence telling the user why this chart suits the data.
"""


def compose(
    resolved_query: str,
    validation: ValidationResult,
    sql: SqlResult,
    result: CheckExecuteResult,
) -> FinalAnswer:
    rows = result.rows[: config.COMPOSE_MAX_ROWS]
    payload = {
        "question": resolved_query,
        "interpretation": validation.model_dump(mode="json", exclude={"notes_for_sql_agent", "required_columns"}),
        "query_explanation": sql.explanation,
        "result": {
            "columns": result.columns,
            "rows": rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "rows_shown_to_you": len(rows),
        },
    }
    user = "<data>\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n</data>"
    return call_structured(
        tool=COMPOSE_TOOL,
        model=config.MODEL_COMPOSE,
        instructions=INSTRUCTIONS,
        user=user,
        effort=config.EFFORT_COMPOSE,
    )
