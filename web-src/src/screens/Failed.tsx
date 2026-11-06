import { useCallback, useEffect, useState } from "react";
import { api, FailedRow } from "../api";
import StatusTag from "../StatusTag";

// Group long, track-specific reasons ("3 of 5 sources tried, all failed: ...") under a short heading.
const group = (r: FailedRow) => {
  if (r.status === "buy") return "Gave up: buy instead";
  const s = r.reason ?? "unknown";
  if (/no search results/i.test(s)) return "Nobody is sharing it right now";
  if (/no acceptable result/i.test(s)) return "Only wrong songs found";
  if (/wrong/i.test(s)) return "Only wrong songs found";
  return "Found, but transfers failed";
};

export default function Failed({ onChange }: { onChange: () => void }) {
  const [rows, setRows] = useState<FailedRow[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => { api.failed().then(setRows); }, []);
  useEffect(load, [load]);

  const act = async (r: FailedRow, s: "retry" | "buy" | "ignored") => {
    await api.setStatus(r.id, s);
    setMsg(s === "retry" ? `${r.title} will be retried in the next retry run.` : `${r.title} marked ${s}.`);
    load();
    onChange();
  };
  const runNow = async () => {
    try { await api.runRetries(); window.location.hash = "jobs"; } catch (e) { setMsg((e as Error).message); }
  };

  if (!rows) return <p className="muted">Loading…</p>;
  const groups = rows.reduce<Record<string, FailedRow[]>>((acc, r) => { (acc[group(r)] ??= []).push(r); return acc; }, {});
  return (
    <section>
      <div className="toolbar">
        <p className="muted">Transfer failures retry after a day (peer quotas reset); "not found" retries weekly. After {rows[0]?.max_tries ?? 5} failed days a track moves to Buy.</p>
        <button className="primary" onClick={runNow} data-testid="run-retries">Run due retries now</button>
      </div>
      {msg && <div className="banner ok">{msg}</div>}
      {rows.length === 0 && <p>No failed tracks.</p>}
      {Object.entries(groups).map(([g, rs]) => (
        <div key={g} className="group">
          <h3>{g} <span className="muted">({rs.length})</span></h3>
          <table className="grid">
            <thead><tr><th>Artist</th><th>Title</th><th>Status</th><th className="num">Failed days</th><th>Next retry</th><th>Reason</th><th /></tr></thead>
            <tbody>
              {rs.map((r) => (
                <tr key={r.id}>
                  <td>{r.artist}</td>
                  <td>{r.title}{r.version ? <span className="muted"> ({r.version})</span> : null}</td>
                  <td><StatusTag status={r.status} /></td>
                  <td className="num">{r.tries}/{r.max_tries}</td>
                  <td className="small">{r.status === "buy" || !r.next_retry_at ? "–"
                    : new Date(r.next_retry_at).toLocaleDateString(undefined, { day: "numeric", month: "short" })}</td>
                  <td className="muted small reasoncell" title={r.reason_full ?? ""}>{r.reason}</td>
                  <td className="actions">
                    <button onClick={() => act(r, "retry")}>Retry</button>
                    <a className="button" href={r.buy.bandcamp} target="_blank" rel="noreferrer">Bandcamp</a>
                    <a className="button" href={r.buy.beatport} target="_blank" rel="noreferrer">Beatport</a>
                    <button onClick={() => act(r, "ignored")} title="Stop looking for this track">Ignore</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
