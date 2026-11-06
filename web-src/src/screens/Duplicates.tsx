import { useEffect, useState } from "react";
import { api, Duplicates, DupFile, fmtDuration } from "../api";

const mb = (b: number) => `${(b / 1e6).toFixed(0)} MB`;

// Files with identical audio (by fingerprint), whatever they are called. Extra copies are moved out, never deleted.
export default function DuplicatesScreen({ busy, onChange }: { busy: boolean; onChange: () => void }) {
  const [d, setD] = useState<Duplicates | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const load = () => api.duplicates().then(setD);
  useEffect(() => {
    load();
    if (!busy) setMsg((m) => (m?.text.startsWith("Checking") ? null : m));  // the check finished
  }, [busy]);
  if (!d) return <p className="muted">Loading…</p>;

  const scan = async () => {
    try { await api.scanDuplicates(); setMsg({ kind: "ok", text: "Checking your library. The first run reads every file (about a minute)." }); onChange(); }
    catch (e) { setMsg({ kind: "error", text: (e as Error).message }); }
  };
  const resolve = async (keep: DupFile, others: DupFile[]) => {
    try {
      const r = await api.resolveDuplicates(keep.file_id, others.map((f) => f.file_id));
      setMsg({ kind: "ok", text: `Kept ${keep.rel}. Moved ${r.moved.length} other ${r.moved.length === 1 ? "copy" : "copies"} to ${d.duplicates_dir}.` });
      load(); onChange();
    } catch (e) { setMsg({ kind: "error", text: (e as Error).message }); }
  };

  return (
    <section>
      <div className="filters">
        <p className="muted small grow" style={{ margin: 0 }}>
          Finds files with the same audio even when names or formats differ (an MP3 and a FLAC of one track, the same
          file in two crates, an untitled rip). The copy you don't keep moves to <code>{d.duplicates_dir}</code>;
          nothing is deleted.
        </p>
        <button className="primary" onClick={scan} disabled={busy || !d.fpcalc} data-testid="dupes-scan">
          {d.ran ? "Check again" : "Find duplicates"}
        </button>
      </div>
      {!d.fpcalc && <div className="banner error">This needs Chromaprint's fpcalc. Install it with <code>brew install chromaprint</code>, then reopen this screen.</div>}
      {msg && <div className={`banner ${msg.kind}`} data-testid="dupes-msg">{msg.text}</div>}
      {d.ran && d.groups.length === 0 && <p className="muted" data-testid="dupes-none">No duplicates found.</p>}
      {!d.ran && <p className="muted">Not checked yet.</p>}
      {d.groups.map((g) => <Group key={g.files.map((f) => f.file_id).join("-")} files={g.files} similarity={g.similarity} onResolve={resolve} busy={busy} />)}
    </section>
  );
}

function Group({ files, similarity, onResolve, busy }: {
  files: DupFile[]; similarity: number; busy: boolean; onResolve: (keep: DupFile, others: DupFile[]) => void;
}) {
  const [keep, setKeep] = useState(files[0].file_id);  // the server lists the suggested keeper first
  const kept = files.find((f) => f.file_id === keep)!;
  const others = files.filter((f) => f.file_id !== keep);
  return (
    <div className="dupgroup" data-testid="dup-group">
      <div className="muted small">{files.length} files, same audio ({(similarity * 100).toFixed(1)}% match)</div>
      <table className="grid">
        <tbody>
          {files.map((f) => (
            <tr key={f.file_id} className={f.file_id === keep ? "keep" : "extra"} onClick={() => setKeep(f.file_id)}>
              <td style={{ width: 70 }}><label className="check small"><input type="radio" checked={f.file_id === keep} onChange={() => setKeep(f.file_id)} /> keep</label></td>
              <td>{f.rel}{f.track ? <div className="muted small">{f.track}</div> : null}</td>
              <td>{f.format}{f.bitrate ? <span className="muted small"> {f.bitrate}k</span> : null}</td>
              <td className="num">{fmtDuration(f.duration)}</td>
              <td className="num muted small">{mb(f.size)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button onClick={() => onResolve(kept, others)} disabled={busy} data-testid="dup-resolve">
          Keep {kept.format.toUpperCase()} in {kept.crate || "library"}, move {others.length === 1 ? "the other copy" : `${others.length} others`} out
        </button>
      </div>
    </div>
  );
}
