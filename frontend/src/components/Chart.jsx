import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  MAX_BARS,
  TYPE_LABELS,
  buildChartData,
  compatibleTypes,
  humanize,
  makeValueFormatters,
  niceTicks,
  SERIES_COLORS,
} from "../chartData.js";
import { formatCell } from "../format.js";

// Chart chrome (light surface).
const SURFACE = "#ffffff";
const GRID = "#e9e8e3";
const AXIS = "#c3c2b7";
const MUTED = "#898781";
const TICK = { fill: MUTED, fontSize: 12 };

export default function Chart({ chart, table }) {
  const types = useMemo(() => (chart && chart.type !== "none" ? compatibleTypes(chart, table) : []), [chart, table]);
  const [type, setType] = useState(chart?.type);
  if (!chart || chart.type === "none" || !table?.rows?.length) return null;

  return (
    <section className="chart">
      <div className="chart-head">
        <div>
          <h4 className="chart-title">{chart.title}</h4>
          {/* The reason explains the suggested form, so it only shows on that form. */}
          <p className="chart-reason">{type === chart.type ? chart.reason : `Showing as ${TYPE_LABELS[type].toLowerCase()} chart.`}</p>
        </div>
        {types.length > 1 && (
          <div className="chart-switch" role="radiogroup" aria-label="Chart type">
            {types.map((t) => (
              <button
                key={t}
                role="radio"
                aria-checked={t === type}
                className={t === type ? "on" : ""}
                onClick={() => setType(t)}
                title={t === chart.type ? "Suggested" : undefined}
              >
                {TYPE_LABELS[t]}
                {t === chart.type ? <span className="suggested-dot" aria-label="suggested" /> : null}
              </button>
            ))}
          </div>
        )}
      </div>
      {type === "stat" ? <StatTiles chart={chart} table={table} /> : <Plot type={type} chart={chart} table={table} />}
    </section>
  );
}

function StatTiles({ chart, table }) {
  const row = table.rows[0];
  return (
    <div className="stat-row">
      {chart.y.map((name) => {
        const i = table.columns.indexOf(name);
        return (
          <div className="stat" key={name}>
            <div className="stat-label">{humanize(name)}</div>
            <div className="stat-value">{formatCell(row[i], name)}</div>
          </div>
        );
      })}
    </div>
  );
}

function ChartTooltip({ active, payload, label, exact }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tooltip">
      {label !== undefined && <div className="tt-label">{label}</div>}
      {payload.map((p) => (
        <div className="tt-row" key={p.dataKey ?? p.name}>
          <span className="tt-swatch" style={{ background: p.color || p.payload?.fill }} />
          <span className="tt-name">{p.name}</span>
          <span className="tt-value">{p.value === null || p.value === undefined ? "—" : exact(p.value)}</span>
        </div>
      ))}
    </div>
  );
}

function truncate(s, n) {
  s = String(s);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function Plot({ type, chart, table }) {
  const { data: allData, series } = useMemo(() => buildChartData(chart, table), [chart, table]);
  const fmt = makeValueFormatters(chart.y_unit);
  // Round ticks from zero; stacks are measured by their totals. Negative data keeps Recharts' own scale.
  const values = allData.flatMap((d) => series.map((s) => d[s.key] ?? 0));
  const peak = type === "stacked_bar"
    ? Math.max(...allData.map((d) => series.reduce((sum, s) => sum + (d[s.key] ?? 0), 0)))
    : Math.max(...values);
  const ticks = values.some((v) => v < 0) ? null : niceTicks(peak);
  const scale = ticks ? { ticks, domain: [0, ticks[ticks.length - 1]] } : {};
  const multi = series.length > 1;
  const legend = multi ? (
    <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12.5, color: "#52514e", paddingTop: 8 }} />
  ) : null;
  const tooltip = <Tooltip content={<ChartTooltip exact={fmt.exact} />} cursor={{ fill: "rgba(11,11,11,0.04)" }} />;

  const isBarLike = type === "bar" || type === "horizontal_bar" || type === "grouped_bar" || type === "stacked_bar";
  const data = isBarLike && allData.length > MAX_BARS ? allData.slice(0, MAX_BARS) : allData;
  const clipped = data.length < allData.length;

  if (type === "pie") {
    const s = series[0];
    const slices = data.map((d, i) => ({ name: d.x, value: d[s.key], fill: SERIES_COLORS[i % SERIES_COLORS.length] }));
    return (
      <ResponsiveContainer width="100%" height={300}>
        <PieChart>
          <Pie data={slices} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="85%"
               stroke={SURFACE} strokeWidth={2} isAnimationActive={false}>
            {slices.map((sl) => <Cell key={sl.name} fill={sl.fill} />)}
          </Pie>
          <Tooltip content={<ChartTooltip exact={fmt.exact} />} />
          <Legend iconType="circle" iconSize={8} layout="vertical" align="right" verticalAlign="middle"
                  wrapperStyle={{ fontSize: 12.5, color: "#52514e" }} />
        </PieChart>
      </ResponsiveContainer>
    );
  }

  if (type === "horizontal_bar") {
    const height = Math.max(160, data.length * (multi ? 22 * series.length + 10 : 30) + 60);
    return (
      <>
        <ResponsiveContainer width="100%" height={height}>
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }} barGap={2}>
            <CartesianGrid horizontal={false} stroke={GRID} />
            <XAxis type="number" tick={TICK} tickFormatter={fmt.tick} axisLine={{ stroke: AXIS }} tickLine={false} {...scale} />
            <YAxis type="category" dataKey="x" width={190} tick={TICK} tickLine={false} axisLine={{ stroke: AXIS }}
                   tickFormatter={(v) => truncate(v, 26)} interval={0} />
            {tooltip}
            {legend}
            {series.map((s) => (
              <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} maxBarSize={24}
                   radius={[0, 4, 4, 0]} isAnimationActive={false} />
            ))}
          </BarChart>
        </ResponsiveContainer>
        {clipped && <p className="chart-note">Showing the first {data.length} of {allData.length}. The table has every row.</p>}
      </>
    );
  }

  const xAxis = (
    <XAxis dataKey="x" tick={TICK} tickLine={false} axisLine={{ stroke: AXIS }}
           interval="preserveStartEnd" minTickGap={12}
           tickFormatter={(v) => truncate(v, 14)} />
  );
  const yAxis = <YAxis tick={TICK} tickFormatter={fmt.tick} axisLine={false} tickLine={false} width={64} {...scale} />;
  const grid = <CartesianGrid vertical={false} stroke={GRID} />;
  const margin = { top: 8, right: 28, bottom: 4, left: 4 }; // room for the last x label

  if (type === "line" || type === "area") {
    const ChartEl = type === "area" ? AreaChart : LineChart;
    const showDots = data.length <= 16;
    return (
      <ResponsiveContainer width="100%" height={300}>
        <ChartEl data={data} margin={margin}>
          {grid}
          {xAxis}
          {yAxis}
          <Tooltip content={<ChartTooltip exact={fmt.exact} />} cursor={{ stroke: AXIS, strokeWidth: 1 }} />
          {legend}
          {series.map((s) =>
            type === "area" ? (
              <Area key={s.key} type="monotone" dataKey={s.key} name={s.name} stroke={s.color} strokeWidth={2}
                    fill={s.color} fillOpacity={0.1} dot={false}
                    activeDot={{ r: 5, stroke: SURFACE, strokeWidth: 2 }} isAnimationActive={false} />
            ) : (
              <Line key={s.key} type="monotone" dataKey={s.key} name={s.name} stroke={s.color} strokeWidth={2}
                    strokeLinecap="round" strokeLinejoin="round" connectNulls
                    dot={showDots ? { r: 4, fill: s.color, stroke: SURFACE, strokeWidth: 2 } : false}
                    activeDot={{ r: 5, stroke: SURFACE, strokeWidth: 2 }} isAnimationActive={false} />
            )
          )}
        </ChartEl>
      </ResponsiveContainer>
    );
  }

  // bar / grouped_bar / stacked_bar (vertical columns)
  const stacked = type === "stacked_bar";
  const lastKey = series[series.length - 1]?.key;
  return (
    <>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={data} margin={margin} barGap={2}>
          {grid}
          {xAxis}
          {yAxis}
          {tooltip}
          {legend}
          {series.map((s) => (
            <Bar key={s.key} dataKey={s.key} name={s.name} fill={s.color} maxBarSize={24}
                 stackId={stacked ? "stack" : undefined}
                 // Stacks: a 2px surface gap between segments, rounded only at the top.
                 stroke={stacked ? SURFACE : undefined} strokeWidth={stacked ? 1 : 0}
                 radius={!stacked || s.key === lastKey ? [4, 4, 0, 0] : 0}
                 isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      {clipped && <p className="chart-note">Showing the first {data.length} of {allData.length}. The table has every row.</p>}
    </>
  );
}
