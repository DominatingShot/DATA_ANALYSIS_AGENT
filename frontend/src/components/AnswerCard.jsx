import ReactMarkdown from "react-markdown";
import Chart from "./Chart.jsx";
import ResultTable from "./ResultTable.jsx";

export default function AnswerCard({ response }) {
  const { type, message, answer, table, sql, meta } = response;

  if (type !== "answer") {
    return (
      <div className={`card card-plain ${type === "error" ? "card-error" : ""}`}>
        <p className="plain">{message}</p>
        {type === "error" && sql && (
          <details className="sql">
            <summary>View last attempted SQL</summary>
            <pre>{sql}</pre>
          </details>
        )}
        {type === "error" && meta?.error && (
          <details className="sql">
            <summary>Technical details</summary>
            <pre>{JSON.stringify(meta.error, null, 2)}</pre>
          </details>
        )}
      </div>
    );
  }

  return (
    <div className="card">
      <p className="summary">{answer.summary}</p>
      {answer.details && (
        <div className="details">
          <ReactMarkdown>{answer.details}</ReactMarkdown>
        </div>
      )}
      {answer.insights?.length > 0 && (
        <section>
          <h3>Insights</h3>
          <ul className="insights">
            {answer.insights.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </section>
      )}
      {answer.caveats?.length > 0 && (
        <aside className="caveats">
          <h3>Caveats</h3>
          <ul>
            {answer.caveats.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </aside>
      )}
      <Chart chart={answer.chart} table={table} />
      <ResultTable table={table} />
      {sql && (
        <details className="sql">
          <summary>View SQL</summary>
          <pre>{sql}</pre>
        </details>
      )}
    </div>
  );
}
