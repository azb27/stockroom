import type { ToolCall, ToolResult } from "@/lib/api";

export type Step = { call: ToolCall; result?: ToolResult };

function Args({ call }: { call: ToolCall }) {
  const { query, ...rest } = call.input as { query?: string } & Record<string, unknown>;
  return (
    <>
      {Object.keys(rest).length > 0 && <code>{JSON.stringify(rest)}</code>}
      {typeof query === "string" && <pre>{query.trim()}</pre>}
    </>
  );
}

function Result({ r }: { r: ToolResult }) {
  return (
    <>
      {r.error && <div className="err">Error: {r.error}</div>}
      {r.caveats.map((c, i) => (
        <div className="caveat" key={i}>
          {c}
        </div>
      ))}
      {r.table && (
        <div className="mini">
          <table>
            <thead>
              <tr>{r.table.columns.map((c) => <th key={c}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {r.table.rows.map((row, i) => (
                <tr key={i}>{row.map((v, j) => <td key={j}>{v === null ? "∅" : String(v)}</td>)}</tr>
              ))}
            </tbody>
          </table>
          <div className="ms">
            {r.table.row_count} row{r.table.row_count === 1 ? "" : "s"}
            {r.table.truncated ? " (preview)" : ""}
          </div>
        </div>
      )}
      {r.preview && (
        <details>
          <summary className="ms">Result</summary>
          <pre>{r.preview}</pre>
        </details>
      )}
    </>
  );
}

export default function Trace({ steps, running, summary }: { steps: Step[]; running: boolean; summary: string }) {
  if (steps.length === 0 && !running) return null;
  return (
    <details className="trace" open={running}>
      <summary>
        {running && <span className="spinner" aria-hidden />} {summary}
      </summary>
      {steps.map(({ call, result }) => (
        <div className="step" key={call.id}>
          <div className="step-head">
            <span className="tool">{call.name}</span>
            {result ? <span className="ms">{result.ms.toFixed(0)} ms</span> : <span className="spinner" aria-label="running" />}
          </div>
          <Args call={call} />
          {result && <Result r={result} />}
        </div>
      ))}
    </details>
  );
}
