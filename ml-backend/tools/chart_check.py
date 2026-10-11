"""Deterministic check of the composer's chart suggestion against the rows actually returned.

The composer suggests a chart; this decides whether the suggestion can be drawn honestly. An
unusable suggestion is downgraded (or dropped to 'none'), never an error: the answer and table
still stand.
"""

from __future__ import annotations

from models import ChartSpec, CheckExecuteResult

MAX_PIE_SLICES = 6      # part-to-whole at a glance only
MIN_TIME_POINTS = 2


def _is_numeric_column(rows: list[list], idx: int) -> bool:
    values = [r[idx] for r in rows if r[idx] is not None]
    return bool(values) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _none(spec: ChartSpec) -> ChartSpec:
    return spec.model_copy(update={"type": "none", "x": None, "y": [], "series": None})


def check_chart(spec: ChartSpec, result: CheckExecuteResult) -> ChartSpec:
    if spec.type == "none":
        return spec
    if result.row_count == 0:
        return _none(spec)

    idx = {c: i for i, c in enumerate(result.columns)}

    if any(c is not None and c not in idx for c in [spec.x, spec.series, *spec.y]):
        return _none(spec)
    if not spec.y or not all(_is_numeric_column(result.rows, idx[c]) for c in spec.y):
        return _none(spec)

    if spec.type == "stat" and result.row_count > 1:
        if spec.x is None:
            return _none(spec)
        spec = spec.model_copy(update={"type": "bar"})

    # A single row is a figure, not a chart: a one-bar bar chart or a one-slice pie says nothing.
    if spec.type == "stat" or (result.row_count == 1 and spec.series is None):
        return spec.model_copy(update={"type": "stat", "x": None, "series": None})

    if spec.x is None:
        return _none(spec)
    if spec.x in spec.y or spec.series in spec.y or (spec.series is not None and spec.series == spec.x):
        return _none(spec)

    t = spec.type
    if t in ("grouped_bar", "stacked_bar") and spec.series is None and len(spec.y) < 2:
        t = "bar"
    if t == "pie" and (
        spec.series is not None
        or len(spec.y) != 1
        or result.row_count > MAX_PIE_SLICES
        or any((r[idx[spec.y[0]]] or 0) < 0 for r in result.rows)
    ):
        t = "horizontal_bar"
    if t in ("line", "area") and len({r[idx[spec.x]] for r in result.rows}) < MIN_TIME_POINTS:
        t = "bar"
    if t == "area" and (spec.series is not None or len(spec.y) > 1):
        t = "line"

    return spec.model_copy(update={"type": t})
