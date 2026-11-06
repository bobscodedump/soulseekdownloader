import { useEffect, useRef, useState } from "react";
import { api, Job } from "../api";

const lineClass = (l: string) =>
  l.includes("FILED") ? "ok" : l.includes("REJECTED") || l.startsWith("ERROR") ? "bad" : l.startsWith("round") ? "head" : "";

export default function Jobs() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [live, setLive] = useState<Job | null>(null);
  const logRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const load = () => api.jobs().then((js) => {
      setJobs(js);
      setSelected((cur) => cur ?? js[0]?.id ?? null);
    });
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [live?.state]); // reload the list as soon as the followed job changes state

  // Follow the selected job's log: fetch only new lines each second.
  useEffect(() => {
    if (selected == null) return;
    let stop = false;
    let acc: Job | null = null;
    const tick = async () => {
      const j = await api.job(selected, acc?.log.length ?? 0);
      acc = acc ? { ...j, log: [...acc.log, ...j.log] } : j;
      if (!stop) setLive(acc);
      if (!stop && (j.state === "queued" || j.state === "running")) setTimeout(tick, 1000);
    };
    tick();
    return () => { stop = true; };
  }, [selected]);

  useEffect(() => { logRef.current?.scrollTo(0, logRef.current.scrollHeight); }, [live?.log.length]);

  return (
    <section className="split">
      <aside>
        <h3>Jobs</h3>
        {jobs.length === 0 && <p className="muted">No jobs since the app started.</p>}
        <ul className="joblist">
          {jobs.map((j) => (
            <li key={j.id} className={j.id === selected ? "active" : ""} onClick={() => setSelected(j.id)}>
              <span className={`dot ${j.state}`} />{j.title}
              <div className="muted small">{j.state}{j.finished_at ? ` · ${new Date(j.finished_at).toLocaleTimeString()}` : ""}</div>
            </li>
          ))}
        </ul>
      </aside>
      <div className="joblog">
        {live ? (
          <>
            <h3 data-testid="job-title">{live.title} <span className={`state ${live.state}`} data-testid="job-state">{live.state}</span></h3>
            {Object.keys(live.result).length > 0 && <JobResult r={live.result} />}
            <pre ref={logRef} data-testid="job-log">
              {live.log.map((l, i) => <div key={i} className={lineClass(l)}>{l}</div>)}
            </pre>
          </>
        ) : <p className="muted">Select a job.</p>}
      </div>
    </section>
  );
}

function JobResult({ r }: { r: Record<string, unknown> }) {
  if ("error" in r) return <div className="banner error">{String(r.error)}</div>;
  if ("plan" in r) return <div className="banner ok">Preview only: nothing was downloaded. {String(r.owned)} already owned.</div>;
  const n = (k: string) => Number(r[k] ?? 0);
  if ("filed" in r) {
    return (
      <div className="stats" data-testid="job-result">
        <div><b>{n("filed")}</b> filed into {String(r.crate)}</div>
        <div><b>{n("rejected")}</b> wrong files rejected</div>
        <div><b>{n("failed")}</b> failed (retry scheduled)</div>
        <div><b>{n("owned")}</b> already owned</div>
      </div>
    );
  }
  return <pre className="muted small">{JSON.stringify(r, null, 1)}</pre>;
}
