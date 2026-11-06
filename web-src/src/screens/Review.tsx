import { useCallback, useEffect, useState } from "react";
import { api, fmtDuration, ReviewItem } from "../api";

export default function Review({ onChange }: { onChange: () => void }) {
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const load = useCallback(() => { api.review().then(setItems); }, []);
  useEffect(load, [load]);

  const act = async (i: ReviewItem, action: "accept" | "dismiss") => {
    const r = await api.reviewAction(i.kind, i.id, action);
    setNote(action === "accept"
      ? (r.filed ? `Filed ${i.file} into ${r.filed.split("/").slice(-2, -1)[0]}.` : `Linked ${i.file} to ${i.title}.`)
      : `Dismissed. The file was left where it is.`);
    load();
    onChange();
  };

  if (!items) return <p className="muted">Loading…</p>;
  return (
    <section>
      <p className="muted">Files that might be the right track but did not pass the automatic checks. Nothing here is ever deleted.</p>
      {note && <div className="banner ok">{note}</div>}
      {items.length === 0 && <p data-testid="review-empty">Nothing to review.</p>}
      <div className="cards">
        {items.map((i) => (
          <div className="card" key={`${i.kind}-${i.id}`} data-testid="review-card">
            <div className="kind">{i.kind === "match" ? "Possible match in your library" : "Download rejected by checks"}</div>
            <div className="pair">
              <div><div className="muted small">Wanted</div><b>{i.artist}</b> – {i.title}{i.version ? ` (${i.version})` : ""}
                {i.wanted_duration ? <span className="muted"> · {fmtDuration(i.wanted_duration)}</span> : null}</div>
              <div><div className="muted small">File</div><span className="mono">{i.file}</span></div>
            </div>
            {i.reason && <div className="reason">Why: {i.reason}</div>}
            <div className="actions">
              <button className="primary" onClick={() => act(i, "accept")}>It's right: {i.kind === "match" ? "link it" : "file it"}</button>
              <button onClick={() => act(i, "dismiss")}>Wrong song</button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
