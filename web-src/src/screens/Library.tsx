import { useEffect, useState } from "react";
import { api, Crate, fmtDuration, LibraryRow } from "../api";
import StatusTag from "../StatusTag";

const EMPTY = { q: "", status: "", crate: "", fmt: "", camelot: "", bpm_min: "", bpm_max: "" };
const CAMELOT = Array.from({ length: 12 }, (_, i) => [`${i + 1}A`, `${i + 1}B`]).flat();

export default function Library() {
  const [f, setF] = useState(EMPTY);
  const [rows, setRows] = useState<LibraryRow[]>([]);
  const [total, setTotal] = useState(0);
  const [crates, setCrates] = useState<Crate[]>([]);
  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });

  useEffect(() => { api.crates().then(setCrates); }, []);
  useEffect(() => {
    const t = setTimeout(() => api.library(f).then((r) => { setRows(r.rows); setTotal(r.total); }), 150);
    return () => clearTimeout(t);
  }, [f]);

  return (
    <section>
      <div className="filters">
        <input placeholder="Search artist, title, version" value={f.q} onChange={set("q")} className="grow" data-testid="lib-search" />
        <select value={f.crate} onChange={set("crate")} aria-label="Crate">
          <option value="">All crates</option>
          {crates.map((c) => <option key={c.name} value={c.name}>{c.name} ({c.files})</option>)}
        </select>
        <select value={f.status} onChange={set("status")} aria-label="Status">
          <option value="">Any status</option>
          <option value="owned">Owned</option><option value="wanted">Wanted</option>
          <option value="failed_no_results">Not found</option><option value="failed_transfer">Transfer failed</option>
          <option value="buy">Buy</option>
        </select>
        <select value={f.fmt} onChange={set("fmt")} aria-label="Format">
          <option value="">Any format</option>
          {["flac", "wav", "aiff", "aif", "mp3", "m4a"].map((x) => <option key={x}>{x}</option>)}
        </select>
        <select value={f.camelot} onChange={set("camelot")} aria-label="Key">
          <option value="">Any key</option>
          {CAMELOT.map((k) => <option key={k}>{k}</option>)}
        </select>
        <input type="number" placeholder="BPM from" value={f.bpm_min} onChange={set("bpm_min")} className="bpm" />
        <input type="number" placeholder="to" value={f.bpm_max} onChange={set("bpm_max")} className="bpm" />
        {JSON.stringify(f) !== JSON.stringify(EMPTY) && <button onClick={() => setF(EMPTY)}>Clear</button>}
      </div>
      <p className="muted small" data-testid="lib-total">{total} {total === 1 ? "item" : "items"}{total > rows.length ? `, showing ${rows.length}` : ""}</p>
      <table className="grid">
        <thead><tr>
          <th>Artist</th><th>Title</th><th>Version</th><th className="num">BPM</th><th>Key</th><th className="num">Energy</th>
          <th>Crate</th><th>Format</th><th className="num">Length</th><th>Status</th>
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.kind}-${r.file_id ?? r.track_id}`} title={r.path ?? r.reason ?? ""}>
              <td>{r.artist}</td><td>{r.title}</td><td className="muted">{r.version ?? ""}</td>
              <td className="num">{r.bpm ?? ""}</td><td>{r.camelot ?? ""}</td><td className="num">{r.energy ?? ""}</td>
              <td>{r.crate}</td>
              <td>{r.format}{r.bitrate && !["flac", "wav", "aiff", "aif"].includes(r.format) ? <span className="muted small"> {r.bitrate}k</span> : null}</td>
              <td className="num">{fmtDuration(r.duration)}</td>
              <td><StatusTag status={r.status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
