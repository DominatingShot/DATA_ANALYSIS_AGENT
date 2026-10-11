"""Deterministic check of the composer's chart suggestion against the rows actually returned.

The composer suggests a chart; this decides whether the suggestion can be drawn honestly. An
unusable suggestion is downgraded (or dropped to 'none'), never an error: the answer and table
still stand. Every change is reported back as a warning for meta.
"""

from __future__ import annotations

from models import ChartSpec, CheckExecuteResult

MAX_SERIES = 8          # categorical palette size; more series fold to "Other" in the UI
MAX_PIE_SLICES = 6      # part-to-whole at a glance only
MIN_TIME_POINTS = 2


def _is_numeric_column(rows: list[list], idx: int) -> bool:
    values = [r[idx] for r in rows if r[idx] is not None]
    return bool(values) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _none(spec: ChartSpec, why: str, warnings: list[str]) -> tuple[ChartSpec, list[str]]:
    warnings.append(f"chart dropped: {why}")
    return spec.model_copy(update={"type": "none", "x": None, "y": [], "series": None}), warnings


def check_chart(spec: ChartSpec, result: CheckExecuteResult) -> tuple[ChartSpec, list[str]]:
    warnings: list[str] = []
    if spec.type == "none":
        return spec, warnings
    if result.row_count == 0:
        return _none(spec, "no rows returned", warnings)

    cols = result.columns
    idx = {c: i for i, c in enumerate(cols)}

    missing = [c for c in [spec.x, spec.series, *spec.y] if c is not None and c not in idx]
    if missing:
        return _none(spec, f"unknown column(s) {missing}", warnings)
    if not spec.y:
        return _none(spec, "no y column given", warnings)
    non_numeric = [c for c in spec.y if not _is_numeric_column(result.rows, idx[c])]
    if non_numeric:
        return _none(spec, f"y column(s) not numeric: {non_numeric}", warnings)

    if spec.type == "stat" and result.row_count > 1:
        if spec.x is None:
            return _none(spec, "'stat' needs a single row", warnings)
        warnings.append("'stat' on several rows shown as a bar chart")
        spec = spec.model_copy(update={"type": "bar"})

    # A single row is a figure, not a chart: a one-bar bar chart or a one-slice pie says nothing.
    if spec.type == "stat" or (result.row_count == 1 and spec.series is None):
        if spec.type != "stat":
            warnings.append(f"chart '{spec.type}' on a single row shown as a stat tile")
        return spec.model_copy(update={"type": "stat", "x": None, "series": None}), warnings

    if spec.x is None:
        return _none(spec, f"'{spec.type}' needs an x column", warnings)
    if spec.x in spec.y or spec.series in spec.y or (spec.series is not None and spec.series == spec.x):
        return _none(spec, "x, y and series must be different columns", warnings)

    t = spec.type
    if t in ("grouped_bar", "stacked_bar") and spec.series is None and len(spec.y) < 2:
        warnings.append(f"'{t}' needs a series column or two measures; shown as a bar chart")
        t = "bar"
    if t == "pie":
        reason = None
        if spec.series is not None or len(spec.y) != 1:
            reason = "a pie takes one measure and no series"
        elif result.row_count > MAX_PIE_SLICES:
            reason = f"more than {MAX_PIE_SLICES} slices"
        elif any((r[idx[spec.y[0]]] or 0) < 0 for r in result.rows):
            reason = "negative values"
        if reason:
            warnings.append(f"pie not suitable ({reason}); shown as a horizontal bar chart")
            t = "horizontal_bar"
    if t in ("line", "area"):
        distinct_x = {r[idx[spec.x]] for r in result.rows}
        if len(distinct_x) < MIN_TIME_POINTS:
            warnings.append(f"'{t}' needs at least {MIN_TIME_POINTS} x values; shown as a bar chart")
            t = "bar"
    if t == "area" and (spec.series is not None or len(spec.y) > 1):
        warnings.append("area is for a single series; shown as a line chart")
        t = "line"

    if spec.series is not None:
        n = len({r[idx[spec.series]] for r in result.rows})
        if n > MAX_SERIES:
            warnings.append(f"{n} series; the UI keeps the {MAX_SERIES - 1} largest and folds the rest into 'Other'")

    return spec.model_copy(update={"type": t}), warnings
