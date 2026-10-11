"""All stage contracts. Each Pydantic model is also the JSON schema the LLM must fill in:
the tool input_schema / output_config format is derived from it, so it is defined once."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- [1] Route ---------------------------------------------------------------------------


class RouteResult(_Strict):
    route: Literal["general", "database", "out_of_scope"]
    reply: str | None = Field(
        description="User-facing reply. Required for 'general' and 'out_of_scope'; null for 'database'."
    )
    resolved_query: str | None = Field(
        description="Self-contained restatement of the question with references resolved from the "
        "conversation. Required for 'database'; null otherwise."
    )

    @model_validator(mode="after")
    def _check(self) -> RouteResult:
        if self.route == "database":
            if not (self.resolved_query and self.resolved_query.strip()):
                raise ValueError("resolved_query is required when route is 'database'")
            if self.reply is not None:
                raise ValueError("reply must be null when route is 'database'")
        else:
            if not (self.reply and self.reply.strip()):
                raise ValueError(f"reply is required when route is '{self.route}'")
            if self.resolved_query is not None:
                raise ValueError(f"resolved_query must be null when route is '{self.route}'")
        return self


# --- [2] Validation ----------------------------------------------------------------------


class TimeRange(_Strict):
    from_date: str = Field(description="ISO date YYYY-MM-DD, first day included")
    to_date: str = Field(description="ISO date YYYY-MM-DD, last day included (inclusive of intent)")
    label: str = Field(description='Human label, e.g. "last 4 complete quarters"')

    @field_validator("from_date", "to_date")
    @classmethod
    def _iso(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _order(self) -> TimeRange:
        if date.fromisoformat(self.from_date) > date.fromisoformat(self.to_date):
            raise ValueError("time_range.from_date is after to_date")
        return self


class ValidationResult(_Strict):
    answerable: bool
    reason: str | None = Field(
        description="Required when answerable=false: plain-English, user-facing explanation naming what is missing."
    )
    interpretation: str | None = Field(
        description="Required when answerable=true: precisely what will be computed."
    )
    required_columns: list[str] = Field(description='e.g. ["fact_sales.gross_amount", "dim_menu.item_name"]')
    time_range: TimeRange | None = Field(description="Resolved period, or null if the question has none")
    caveats: list[str] = Field(description="User-facing caveats triggered by this question (schema.md §10)")
    notes_for_sql_agent: str | None = Field(description="Specific traps from schema.md that apply")

    @model_validator(mode="after")
    def _check(self) -> ValidationResult:
        if self.answerable and not (self.interpretation and self.interpretation.strip()):
            raise ValueError("interpretation is required when answerable=true")
        if not self.answerable and not (self.reason and self.reason.strip()):
            raise ValueError("reason is required when answerable=false")
        return self


# --- [3] SQL ----------------------------------------------------------------------------


class SqlResult(_Strict):
    sql: str = Field(description="One PostgreSQL SELECT statement")
    explanation: str = Field(description="One or two plain-English sentences, no SQL jargon")
    expected_columns: list[str] = Field(description="Output column aliases, in order")


# --- [4] Check + execute ---------------------------------------------------------------


class CheckExecuteResult(_Strict):
    ok: bool
    errors: list[str] = []
    warnings: list[str] = []
    normalized_sql: str | None = None
    columns: list[str] = []
    rows: list[list[Any]] = []
    row_count: int = 0
    truncated: bool = False
    execution_ms: int = 0


# --- [5] Compose ------------------------------------------------------------------------


ChartType = Literal[
    "bar", "horizontal_bar", "line", "area", "grouped_bar", "stacked_bar", "pie", "stat", "none"
]


class ChartSpec(_Strict):
    type: ChartType = Field(description="Suggested visual form for the returned rows")
    x: str | None = Field(description="Result column for categories or time. null for 'stat' and 'none'")
    y: list[str] = Field(description="Numeric result column(s) to plot, all in the same unit. [] for 'none'")
    series: str | None = Field(
        description="Result column whose values become separate series (long-format rows), or null"
    )
    y_unit: Literal["rupees", "count", "percent", "other"] = Field(description="Unit shared by every y column")
    title: str = Field(description="Short chart title in sentence case, e.g. 'Revenue by month, FY 2025-26'")
    reason: str = Field(description="One sentence for the user: why this chart suits the data")


class FinalAnswer(_Strict):
    summary: str = Field(description="2-3 sentences: the direct answer with the headline numbers")
    details: str = Field(description="Markdown: breakdown, comparisons, what the figures mean")
    insights: list[str] = Field(description="2-4 observations visible in these rows (empty if no rows)")
    caveats: list[str] = Field(description="Validation caveats verbatim, plus any added here")
    chart: ChartSpec = Field(description="Suggested chart for the returned rows")


# --- API ---------------------------------------------------------------------------------


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    history: list[HistoryTurn] = []


class ChatResponse(BaseModel):
    type: Literal["general", "declined", "not_answerable", "answer", "error"]
    message: str | None = None
    answer: FinalAnswer | None = None
    table: dict | None = None
    sql: str | None = None
    meta: dict = {}


# --- Stage tool descriptors ---------------------------------------------------------------


@dataclass(frozen=True)
class StageTool:
    """Name + description + output model for one LLM stage."""

    name: str
    description: str
    output_model: type[BaseModel]


ROUTE_TOOL = StageTool(
    "route_query",
    "Classify the user's message and either reply directly or restate it as a self-contained data question.",
    RouteResult,
)
VALIDATE_TOOL = StageTool(
    "report_feasibility",
    "Report whether the question can be answered from the documented columns, and how.",
    ValidationResult,
)
SQL_TOOL = StageTool("write_sql", "Submit one PostgreSQL SELECT that answers the question.", SqlResult)
COMPOSE_TOOL = StageTool(
    "compose_answer", "Submit the final answer built strictly from the returned rows.", FinalAnswer
)
