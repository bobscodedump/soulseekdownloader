import { useEffect, useState } from "react";
import { api, Settings } from "../api";
import SpotifySettings from "../SpotifySettings";

export default function SettingsScreen({ onChange }: { onChange: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [root, setRoot] = useState("");
  const [ssPath, setSsPath] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.settings().then((x) => { setS(x); setRoot(x.library_root); setSsPath(x.sockseek_path); }); }, []);
  if (!s) return <p className="muted">Loading…</p>;

  const save = async (patch: Partial<Settings>) => {
    try {
      const x = await api.saveSettings(patch);
      setS(x);
      setMsg({ kind: "ok", text: "Saved." });
      onChange();
    } catch (e) { setMsg({ kind: "error", text: (e as Error).message }); }
  };
  const run = async (f: () => Promise<Settings>, done: string) => {
    setBusy(true);
    try { setS(await f()); setMsg({ kind: "ok", text: done }); onChange(); }
    catch (e) { setMsg({ kind: "error", text: (e as Error).message }); }
    setBusy(false);
  };
  const rescan = async () => {
    const r = await api.scan();
    setMsg({ kind: "ok", text: `Scanned ${r.seen} files: ${r.added} new, ${r.moved} moved, ${r.removed} removed. ${r.needs_tags} need tags.` });
    onChange();
  };

  return (
    <section className="settings">
      {msg && <div className={`banner ${msg.kind}`}>{msg.text}</div>}
      <div className="setup" data-testid="soulseek-setup">
        <h3>Soulseek setup</h3>
        <p className="muted small">Downloads need two things: the downloader program, and your free Soulseek account
          (make one at <a href="https://www.slsknet.org" target="_blank" rel="noreferrer">slsknet.org</a>; any new
          username and password work). Your password stays on this Mac.</p>
        <div className="row">
          <span>{s.sockseek_ok ? "✓ Downloader installed" : "✗ Downloader missing"}</span>
          {!s.sockseek_ok && <button disabled={busy} onClick={() => run(api.installSockseek, "Downloader installed.")}>
            {busy ? "Downloading…" : "Install downloader"}</button>}
        </div>
        <div className="row">
          <input placeholder="Soulseek username" value={user} onChange={(e) => setUser(e.target.value)} data-testid="ss-user" />
          <input type="password" placeholder={s.soulseek_user ? "New password" : "Password"} value={pass}
                 onChange={(e) => setPass(e.target.value)} data-testid="ss-pass" />
          <button disabled={busy || !user || !pass} onClick={() => run(() => api.saveLogin(user, pass), "Login saved.").then(() => setPass(""))}>
            Save login</button>
        </div>
        <span className="muted small">{s.soulseek_user ? `Signed in as ${s.soulseek_user}.` : "No Soulseek login saved yet."}</span>
      </div>
      <label>DJ folder
        <div className="row">
          <input value={root} onChange={(e) => setRoot(e.target.value)} className="grow" />
          <button onClick={() => save({ library_root: root })} disabled={root === s.library_root}>Save</button>
          <button onClick={rescan}>Rescan now</button>
        </div>
        <span className="muted small">Read-only scan; the app rescans every minute. Downloads are filed into crate folders inside it.</span>
      </label>
      <label>Downloader location (leave as is)
        <div className="row">
          <input value={ssPath} onChange={(e) => setSsPath(e.target.value)} className="grow" data-testid="sockseek-path" />
          <button onClick={() => save({ sockseek_path: ssPath })} disabled={ssPath === s.sockseek_path}>Save</button>
        </div>
        <span className={`small ${s.sockseek_ok ? "muted" : "error"}`}>
          {s.sockseek_ok ? "Found."
                         : "Not found at this path: downloads cannot start until it is set."}
        </span>
      </label>
      <SpotifySettings />
      <label className="check">
        <input type="checkbox" checked={s.auto_retry} onChange={(e) => save({ auto_retry: e.target.checked })} />
        Retry failed tracks automatically when they are due
      </label>
      <label>Rejected downloads folder
        <input value={s.rejected_dir} readOnly className="grow" />
        <span className="muted small">Wrong files are parked here for review. Nothing is deleted.</span>
      </label>
    </section>
  );
}
