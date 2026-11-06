import { useEffect, useMemo, useState } from "react";
import { api, fmtDuration, UpgradeRow } from "../api";

const day = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });

// MP3/M4A files that could be swapped for a lossless copy of the same edit.
export default function Upgrades({ busy, onChange }: { busy: boolean; onChange: () => void }) {
  const [rows, setRows] = useState<UpgradeRow[] | null>(null);
  const [replacedDir, setReplacedDir] = useState("");
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [q, setQ] = useState("");
  const [showChecked, setShowChecked] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const load = () => api.upgrades().then((r) => { setRows(r.rows); setReplacedDir(r.replaced_dir); });
  useEffect(() => { load(); }, [busy]);

  const shown = useMemo(() => (rows ?? []).filter((r) =>
    (showChecked || !r.checked_at) && (!q || `${r.artist} ${r.title}`.toLowerCase().includes(q.toLowerCase()))), [rows, q, showChecked]);
  if (!rows) return <p className="muted">Loading…</p>;

  const checkedCount = rows.filter((r) => r.checked_at).length;
  const allOn = shown.length > 0 && shown.every((r) => sel.has(r.file_id));
  const toggleAll = () => setSel(allOn ? new Set() : new Set(shown.map((r) => r.file_id)));
  const toggle = (id: number) => { const n = new Set(sel); n.has(id) ? n.delete(id) : n.add(id); setSel(n); };
  const start = async (dry: boolean) => {
    try {
      await api.startUpgrades([...sel], dry);
      setMsg({ kind: "ok", text: `${dry ? "Preview" : "Upgrade"} started for ${sel.size} files. Follow it on the Jobs screen.` });
      setSel(new Set());
      onChange();
    } catch (e) { setMsg({ kind: "error", text: (e as Error).message }); }
  };

  return (
    <section>
      <p className="muted small">
        {rows.length} tracks in your library are only MP3 or M4A. The app looks for a FLAC, WAV or AIFF of the
        <b> same edit</b> (same length within 5 s, same artist, title and remix) and puts it next to the old file.
        The old file moves to <code>{replacedDir}</code>; nothing is deleted. If the track is already in Rekordbox,
        right-click it there and choose Relocate to point it at the new file and keep your cues.
      </p>
      {msg && <div className={`banner ${msg.kind}`} data-testid="upgrade-msg">{msg.text}</div>}
      <div className="filters">
        <input placeholder="Search artist or title" value={q} onChange={(e) => setQ(e.target.value)} className="grow" />
        <label className="check small">
          <input type="checkbox" checked={showChecked} onChange={(e) => setShowChecked(e.target.checked)} />
          Show already checked ({checkedCount})
        </label>
        <button onClick={() => start(true)} disabled={!sel.size || busy}>Preview</button>
        <button className="primary" onClick={() => start(false)} disabled={!sel.size || busy} data-testid="upgrade-run">
          Upgrade {sel.size || ""} selected
        </button>
      </div>
      {busy && <p className="muted small">Another job is running; start this when it finishes.</p>}
      <table className="grid" data-testid="upgrade-table">
        <thead><tr>
          <th><input type="checkbox" checked={allOn} onChange={toggleAll} aria-label="Select all" data-testid="upgrade-all" /></th>
          <th>Artist</th><th>Title</th><th>Crate</th><th>Now</th><th className="num">Length</th><th>Last check</th>
        </tr></thead>
        <tbody>
          {shown.map((r) => (
            <tr key={r.file_id} title={r.file} onClick={() => toggle(r.file_id)} className="clickable">
              <td><input type="checkbox" checked={sel.has(r.file_id)} onChange={() => toggle(r.file_id)} onClick={(e) => e.stopPropagation()} /></td>
              <td>{r.artist}</td>
              <td>{r.title}{r.version ? <span className="muted"> ({r.version})</span> : null}</td>
              <td>{r.crate}</td>
              <td>{r.format}{r.bitrate ? <span className="muted small"> {r.bitrate}k</span> : null}</td>
              <td className="num">{fmtDuration(r.duration)}</td>
              <td className="muted small">{r.checked_at ? `${day(r.checked_at)}: ${r.note}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown.length === 0 && <p className="muted">Nothing to upgrade{rows.length ? " that hasn't been checked already" : ""}.</p>}
    </section>
  );
}
