import { useCallback, useEffect, useRef, useState } from "react";
import { api, fmtDuration, PlaylistRow, STATUS_LABEL, Summary, TrackRow } from "../api";
import StatusTag from "../StatusTag";

export default function Playlists({ summary, onChange }: { summary: Summary | null; onChange: () => void }) {
  const [rows, setRows] = useState<PlaylistRow[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [drag, setDrag] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(() => { api.playlists().then(setRows).catch((e) => setMsg({ kind: "error", text: e.message })); }, []);
  useEffect(load, [load]);
  useEffect(() => { const t = setInterval(load, 4000); return () => clearInterval(t); }, [load]);

  const importFile = async (f: File) => {
    try {
      const r = await api.importCsv(f);
      const review = r.review ? `, ${r.review} to check in Review` : "";
      setMsg({ kind: "ok", text: `Imported ${r.name}: ${r.rows} tracks. ${r.owned} already in your library${review}, ${r.rows - r.owned - r.review} to download.` });
      setOpen(r.id);
      load();
      onChange();
    } catch (e) {
      setMsg({ kind: "error", text: (e as Error).message });
    }
  };

  return (
    <section>
      <div
        className={`drop ${drag ? "over" : ""}`}
        data-testid="dropzone"
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) importFile(f); }}
        onClick={() => fileInput.current?.click()}
      >
        <strong>Import a playlist CSV</strong>
        <span className="muted">Drop it here or click to choose. Tracks you already own are marked automatically.</span>
        <input ref={fileInput} type="file" accept=".csv" hidden data-testid="csv-input"
               onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = ""; }} />
      </div>
      {msg && <div className={`banner ${msg.kind}`} data-testid="import-msg">{msg.text}</div>}

      <table className="grid">
        <thead><tr><th>Playlist</th><th>Tracks</th><th>Progress</th><th>Crate</th><th /></tr></thead>
        <tbody>
          {rows.map((p) => (
            <PlaylistLine key={p.id} p={p} open={open === p.id} toggle={() => setOpen(open === p.id ? null : p.id)}
                          defaultCrate={summary?.today_crate ?? ""} busy={!!summary?.current_job} onChange={() => { load(); onChange(); }} />
          ))}
          {rows.length === 0 && <tr><td colSpan={5} className="muted">No playlists yet.</td></tr>}
        </tbody>
      </table>
    </section>
  );
}

function Progress({ counts, total }: { counts: PlaylistRow["counts"]; total: number }) {
  const owned = counts.owned ?? 0;
  const failed = (counts.failed_no_results ?? 0) + (counts.failed_transfer ?? 0) + (counts.buy ?? 0);
  const pct = (n: number) => `${total ? (100 * n) / total : 0}%`;
  return (
    <div className="progress" title={`${owned} owned, ${failed} failed, ${total - owned - failed} to get`}>
      <div className="bar"><span className="owned" style={{ width: pct(owned) }} /><span className="failed" style={{ width: pct(failed) }} /></div>
      <span className="muted small">{owned}/{total} owned{failed ? ` · ${failed} failed` : ""}</span>
    </div>
  );
}

function PlaylistLine({ p, open, toggle, defaultCrate, busy, onChange }: {
  p: PlaylistRow; open: boolean; toggle: () => void; defaultCrate: string; busy: boolean; onChange: () => void;
}) {
  const [crate, setCrate] = useState(p.crate ?? defaultCrate);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (!p.crate && defaultCrate && !crate) setCrate(defaultCrate); }, [defaultCrate, p.crate, crate]);

  const start = async (dry: boolean) => {
    setErr(null);
    try {
      await api.download(p.id, crate, dry);
      window.location.hash = "jobs";
      onChange();
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  return (
    <>
      <tr className="clickable" data-testid={`playlist-${p.name}`}>
        <td onClick={toggle}><span className="caret">{open ? "▾" : "▸"}</span> <strong>{p.name}</strong></td>
        <td onClick={toggle}>{p.total}</td>
        <td onClick={toggle}><Progress counts={p.counts} total={p.total} /></td>
        <td>
          <input className="crate" value={crate} onChange={(e) => setCrate(e.target.value)} data-testid="crate-input"
                 aria-label="Crate folder" placeholder={defaultCrate} />
        </td>
        <td className="actions">
          <button onClick={() => start(true)} disabled={busy || !crate} data-testid="preview-btn">Preview</button>
          <button className="primary" onClick={() => start(false)} disabled={busy || !crate} data-testid="download-btn">Download</button>
          {err && <div className="error small">{err}</div>}
        </td>
      </tr>
      {open && <tr className="detail"><td colSpan={5}><PlaylistTracks id={p.id} /></td></tr>}
    </>
  );
}

function PlaylistTracks({ id }: { id: number }) {
  const [tracks, setTracks] = useState<TrackRow[] | null>(null);
  useEffect(() => { api.playlist(id).then((p) => setTracks(p.tracks)); }, [id]);
  if (!tracks) return <div className="muted">Loading…</div>;
  return (
    <table className="grid inner">
      <thead><tr><th>Artist</th><th>Title</th><th>Version</th><th className="num">BPM</th><th>Key</th><th className="num">Length</th><th>Status</th></tr></thead>
      <tbody>
        {tracks.map((t) => (
          <tr key={t.id}>
            <td>{t.artist}</td><td>{t.title}</td><td className="muted">{t.version ?? ""}</td>
            <td className="num">{t.bpm ?? ""}</td><td>{t.camelot ?? ""}</td><td className="num">{fmtDuration(t.duration)}</td>
            <td title={t.reason ?? ""}><StatusTag status={t.status} />{t.reason && t.status !== "owned" ? <span className="muted small"> {t.reason}</span> : null}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export { STATUS_LABEL };
