import { useEffect, useState } from "react";
import { api, Settings } from "../api";

export default function SettingsScreen({ onChange }: { onChange: () => void }) {
  const [s, setS] = useState<Settings | null>(null);
  const [root, setRoot] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => { api.settings().then((x) => { setS(x); setRoot(x.library_root); }); }, []);
  if (!s) return <p className="muted">Loading…</p>;

  const save = async (patch: Partial<Settings>) => {
    try {
      const x = await api.saveSettings(patch);
      setS(x);
      setMsg({ kind: "ok", text: "Saved." });
      onChange();
    } catch (e) { setMsg({ kind: "error", text: (e as Error).message }); }
  };
  const rescan = async () => {
    const r = await api.scan();
    setMsg({ kind: "ok", text: `Scanned ${r.seen} files: ${r.added} new, ${r.moved} moved, ${r.removed} removed. ${r.needs_tags} need tags.` });
    onChange();
  };

  return (
    <section className="settings">
      {msg && <div className={`banner ${msg.kind}`}>{msg.text}</div>}
      <label>DJ folder
        <div className="row">
          <input value={root} onChange={(e) => setRoot(e.target.value)} className="grow" />
          <button onClick={() => save({ library_root: root })} disabled={root === s.library_root}>Save</button>
          <button onClick={rescan}>Rescan now</button>
        </div>
        <span className="muted small">Read-only scan; the app rescans every minute. Downloads are filed into crate folders inside it.</span>
      </label>
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
