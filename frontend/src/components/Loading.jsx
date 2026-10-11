// Driven by real stage events from the SSE stream.
const STEPS = [
  ["route", "Understanding your question…"],
  ["validate", "Checking what's possible…"],
  ["sql", "Writing the query…"],
  ["execute", "Running it…"],
  ["compose", "Writing up the answer…"],
];

export default function Loading({ stage }) {
  const current = STEPS.findIndex(([key]) => key === stage?.stage);
  const retry =
    stage?.attempt > 1 && (stage.stage === "sql" || stage.stage === "execute")
      ? ` (attempt ${stage.attempt})`
      : "";
  return (
    <div className="card card-loading" role="status" aria-live="polite">
      <ol className="steps">
        {STEPS.map(([key, label], i) => (
          <li key={key} className={i < current ? "done" : i === current ? "active" : ""}>
            {label}
            {i === current ? retry : ""}
          </li>
        ))}
      </ol>
    </div>
  );
}
