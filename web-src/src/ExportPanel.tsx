import { useEffect, useState } from "react";
import { api, ExportStatus } from "./api";

const n = (k: number, w: string) => `${k} ${w}${k === 1 ? "" : "s"}`;
const when = (iso: string) => new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// Rekordbox XML + M3U8. The app keeps an existing export current; Rekordbox re-reads it on refresh.
export default function ExportPanel() {
  const [st, setSt] = useState<ExportStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [help, setHelp] = useState(false);
  useEffect(() => { api.exportStatus().then(setSt); }, []);
  if (!st) return null;

  const run = async () => {
    setBusy(true); setErr(null);
    try { setSt(await api.exportNow()); setHelp((h) => h || !st.exists); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };
  const lists = st.playlists ? Object.keys(st.playlists).length : null;

  return (
    <div className="export" data-testid="export-panel">
      <div className="row">
        <strong>DJ software</strong>
        <button className="primary" onClick={run} disabled={busy} data-testid="export-run">
          {busy ? "Exporting…" : st.exists ? "Export again" : "Export for Rekordbox"}
        </button>
        <span className="muted small" data-testid="export-status">
          {st.exists ? <>
            {st.tracks !== undefined ? `${n(st.tracks, "track")}, ${n(Object.keys(st.crates ?? {}).length, "crate")}, ${n(lists ?? 0, "playlist")}. ` : ""}
            Last written {when(st.exported_at!)}{st.stale ? " (library changed since; updates within a minute)" : ", kept up to date automatically"}.
          </> : "Writes a Rekordbox XML file and M3U8 playlists (Serato, Traktor). Your Rekordbox library itself is never touched."}
        </span>
        <button className="link" onClick={() => setHelp(!help)}>{help ? "Hide steps" : "How to use"}</button>
      </div>
      {err && <div className="banner error">{err}</div>}
      {help && (
        <div className="small help" data-testid="export-help">
          <p><b>Rekordbox</b> (one-time setup):</p>
          <ol>
            <li>Preferences &gt; Advanced &gt; Database &gt; <i>rekordbox xml</i>: set Imported Library to <code>{st.xml_path}</code></li>
            <li>Preferences &gt; View &gt; Layout: tick <i>rekordbox xml</i> so it shows in the sidebar.</li>
            <li>In the sidebar open rekordbox xml &gt; Playlists &gt; DJ Library, right-click a crate or playlist and choose <i>Import Playlist</i>.</li>
          </ol>
          <p>After new downloads, click the refresh icon next to rekordbox xml and import the playlist again.</p>
          <p><b>Serato / Traktor / others:</b> drag the .m3u8 files from <code>{st.m3u_dir}</code> into the app.</p>
        </div>
      )}
    </div>
  );
}
