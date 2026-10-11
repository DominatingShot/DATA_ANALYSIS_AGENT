import { formatCell, humanizeColumn } from "../format.js";

export default function ResultTable({ table }) {
  if (!table || !table.columns?.length) return null;
  const { columns, rows, row_count, truncated } = table;
  if (!rows.length) return <p className="table-empty">No rows matched.</p>;
  return (
    <div className="table-block">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {columns.map((c, i) => (
                <th key={i} className={typeof rows[0][i] === "number" ? "num" : ""}>
                  {humanizeColumn(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r}>
                {row.map((v, i) => (
                  <td key={i} className={typeof v === "number" ? "num" : ""}>
                    {formatCell(v, columns[i])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="table-foot">
        {row_count} row{row_count === 1 ? "" : "s"}
        {truncated ? " (truncated)" : ""}
      </p>
    </div>
  );
}
