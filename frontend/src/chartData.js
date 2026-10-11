// Turns the result table + the server's chart spec into data a chart can draw.
// Pure functions: no React here.

// Categorical palette, fixed order (validated on the white card surface; see README).
export const SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
export const OTHER_COLOR = "#a8a69f";
export const MAX_SERIES = 8;
export const MAX_BARS = 40;

export const TYPE_LABELS = {
  bar: "Column",
  horizontal_bar: "Bar",
  line: "Line",
  area: "Area",
  grouped_bar: "Grouped",
  stacked_bar: "Stacked",
  pie: "Pie",
  stat: "Figures",
};

const ISO_DATE = /^\d{4}-\d{2}(-\d{2})?$|^\d{4}-\d{2}-\d{2}T/;
const TIME_NAME = /(date|day|week|month|quarter|year|period|hour|time)/i;

export function isTemporal(xName, values) {
  return values.length > 0 && (values.every((v) => typeof v === "string" && ISO_DATE.test(v)) || TIME_NAME.test(xName));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Label for one x value; dates collapse to month or quarter when every value is a period start.
export function makeXFormatter(xName, values) {
  const dates = values.every((v) => typeof v === "string" && ISO_DATE.test(v));
  if (!dates) return (v) => (v === null || v === undefined ? "—" : String(v));
  // "2025-10" (month) or "2025-10-01" (period start) both read as a month.
  const firstOfMonth = values.every((v) => v.length === 7 || v.slice(8, 10) === "01");
  const quarterStarts = firstOfMonth && values.every((v) => ["01", "04", "07", "10"].includes(v.slice(5, 7)));
  const quarterly = quarterStarts && /quarter|qtr/i.test(xName);
  return (v) => {
    const y = v.slice(0, 4);
    const m = Number(v.slice(5, 7));
    if (quarterly) return `Q${Math.floor((m - 1) / 3) + 1} ${y}`;
    if (firstOfMonth) return `${MONTHS[m - 1]} ${y}`;
    return `${Number(v.slice(8, 10))} ${MONTHS[m - 1]} ${y}`;
  };
}

const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

function compactRupees(v) {
  const a = Math.abs(v);
  if (a >= 1e7) return `₹${inr.format(+(v / 1e7).toFixed(2))}Cr`;
  if (a >= 1e5) return `₹${inr.format(+(v / 1e5).toFixed(1))}L`;
  if (a >= 1e3) return `₹${inr.format(+(v / 1e3).toFixed(1))}K`;
  return `₹${inr.format(v)}`;
}

function compactCount(v) {
  const a = Math.abs(v);
  if (a >= 1e7) return `${inr.format(+(v / 1e7).toFixed(2))}Cr`;
  if (a >= 1e5) return `${inr.format(+(v / 1e5).toFixed(1))}L`;
  if (a >= 1e3) return `${inr.format(+(v / 1e3).toFixed(1))}K`;
  return inr.format(v);
}

// Round axis ticks from 0: steps of 1, 2, 2.5 or 5 x 10^k, so lakh/crore labels stay clean.
export function niceTicks(maxValue, target = 4) {
  if (!(maxValue > 0)) return null;
  const raw = maxValue / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const ticks = [];
  for (let t = 0; t < maxValue + step; t += step) ticks.push(+t.toPrecision(12));
  return ticks;
}

// Axis ticks are compact; tooltips are exact.
export function makeValueFormatters(unit) {
  if (unit === "rupees") return { tick: compactRupees, exact: (v) => `₹${inr.format(v)}` };
  if (unit === "percent") return { tick: (v) => `${inr.format(v)}%`, exact: (v) => `${inr.format(v)}%` };
  return { tick: compactCount, exact: (v) => inr.format(v) };
}

export function humanize(name) {
  return String(name).replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

// Chart types that honestly fit this shape of data. The suggested type is always included.
export function compatibleTypes(spec, table) {
  if (spec.type === "stat") return ["stat"];
  const xi = table.columns.indexOf(spec.x);
  const xs = table.rows.map((r) => r[xi]);
  const multi = spec.series || spec.y.length > 1;
  const temporal = isTemporal(spec.x, xs);
  let types;
  if (multi) {
    types = ["grouped_bar", "stacked_bar"];
    if (temporal) types.push("line");
  } else {
    types = ["bar", "horizontal_bar"];
    if (temporal && new Set(xs).size >= 2) types.push("line", "area");
    const yi = table.columns.indexOf(spec.y[0]);
    if (table.rows.length <= 6 && table.rows.every((r) => (r[yi] ?? 0) >= 0)) types.push("pie");
  }
  if (!types.includes(spec.type)) types.unshift(spec.type);
  return types;
}

// Builds { data, series, xFormat } where data rows are { x, s0, s1, ... } and
// series is [{ key, name, color }]. Long-format rows are pivoted on spec.series;
// series beyond MAX_SERIES fold into "Other". Colour follows the entity, so it
// does not change when the chart type is switched.
export function buildChartData(spec, table) {
  const col = (name) => table.columns.indexOf(name);
  const xi = col(spec.x);
  const rawXs = table.rows.map((r) => r[xi]);
  const xFormat = makeXFormatter(spec.x || "", rawXs.filter((v) => v !== null && v !== undefined));
  const temporal = isTemporal(spec.x || "", rawXs);

  let seriesDefs; // [{ key, name, ... }]
  const rows = table.rows;

  if (spec.series) {
    const si = col(spec.series);
    const yi = col(spec.y[0]);
    const totals = new Map();
    for (const r of rows) totals.set(r[si], (totals.get(r[si]) || 0) + (r[yi] || 0));
    const order = [...totals.keys()]; // first appearance in the SQL's order
    let kept = order;
    let folded = new Set();
    if (order.length > MAX_SERIES) {
      const byTotal = [...order].sort((a, b) => totals.get(b) - totals.get(a));
      const keep = new Set(byTotal.slice(0, MAX_SERIES - 1));
      kept = order.filter((s) => keep.has(s));
      folded = new Set(order.filter((s) => !keep.has(s)));
    }
    seriesDefs = kept.map((s, i) => ({ key: `s${i}`, name: String(s), match: (v) => v === s }));
    if (folded.size) seriesDefs.push({ key: "other", name: "Other", match: (v) => folded.has(v), other: true });

    const byX = new Map();
    for (const r of rows) {
      const x = r[xi];
      if (!byX.has(x)) byX.set(x, { raw: x, x: xFormat(x) });
      const point = byX.get(x);
      const def = seriesDefs.find((d) => d.match(r[si]));
      if (def) point[def.key] = (point[def.key] || 0) + (r[yi] ?? 0);
    }
    let data = [...byX.values()];
    if (temporal) data.sort((a, b) => (a.raw < b.raw ? -1 : a.raw > b.raw ? 1 : 0));
    return finish(data, seriesDefs, xFormat, rows.length);
  }

  seriesDefs = spec.y.map((name, i) => ({ key: `s${i}`, name: humanize(name), yi: col(name) }));
  let data = rows.map((r) => {
    const point = { raw: r[xi], x: xFormat(r[xi]) };
    for (const d of seriesDefs) point[d.key] = r[d.yi];
    return point;
  });
  if (temporal) data.sort((a, b) => (a.raw < b.raw ? -1 : a.raw > b.raw ? 1 : 0));
  return finish(data, seriesDefs, xFormat, rows.length);
}

function finish(data, seriesDefs, xFormat, rowCount) {
  let colorIndex = 0;
  const series = seriesDefs.map((d) => ({
    key: d.key,
    name: d.name,
    color: d.other ? OTHER_COLOR : SERIES_COLORS[colorIndex++ % SERIES_COLORS.length],
  }));
  return { data, series, xFormat, rowCount };
}
