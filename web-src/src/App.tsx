import { useCallback, useEffect, useState } from "react";
import { api, Summary } from "./api";
import Playlists from "./screens/Playlists";
import Jobs from "./screens/Jobs";
import Library from "./screens/Library";
import Review from "./screens/Review";
import Failed from "./screens/Failed";
import SettingsScreen from "./screens/Settings";

const TABS = ["playlists", "jobs", "library", "review", "failed", "settings"] as const;
type Tab = (typeof TABS)[number];
const LABEL: Record<Tab, string> = {
  playlists: "Playlists", jobs: "Jobs", library: "Library", review: "Review", failed: "Failed", settings: "Settings",
};

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

const fromHash = (): Tab => {
  const h = window.location.hash.replace("#", "").split("?")[0] as Tab;
  return TABS.includes(h) ? h : "playlists";
};

export default function App() {
  const [tab, setTab] = useState<Tab>(fromHash);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api.summary().then((s) => { setSummary(s); setError(null); }).catch((e) => setError(String(e.message ?? e)));
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    const onHash = () => setTab(fromHash());
    window.addEventListener("hashchange", onHash);
    return () => { clearInterval(t); window.removeEventListener("hashchange", onHash); };
  }, [refresh]);

  const failedCount = (summary?.tracks.failed_no_results ?? 0) + (summary?.tracks.failed_transfer ?? 0) + (summary?.tracks.buy ?? 0);
  const badge: Partial<Record<Tab, number>> = { review: summary?.review ?? 0, failed: failedCount };
  const job = summary?.current_job;

  return (
    <div className="app">
      <header className="top">
        <div className="brand">DJ Library</div>
        <nav>
          {TABS.map((t) => (
            <a key={t} href={`#${t}`} className={t === tab ? "active" : ""} data-testid={`tab-${t}`}>
              {LABEL[t]}
              {badge[t] ? <span className="badge">{badge[t]}</span> : null}
            </a>
          ))}
        </nav>
        <div className="jobpill" data-testid="job-pill">
          {job ? <a href="#jobs"><span className="dot running" />{job.title}</a>
               : <span className="muted">{summary ? `${plural(summary.files, "file")} in library` : ""}</span>}
        </div>
      </header>
      {error && <div className="banner error">Can't reach the app server: {error}</div>}
      {summary?.background_error && <div className="banner error">Background library scan failed: {summary.background_error}</div>}
      <main>
        {tab === "playlists" && <Playlists summary={summary} onChange={refresh} />}
        {tab === "jobs" && <Jobs />}
        {tab === "library" && <Library />}
        {tab === "review" && <Review onChange={refresh} />}
        {tab === "failed" && <Failed onChange={refresh} />}
        {tab === "settings" && <SettingsScreen onChange={refresh} />}
      </main>
    </div>
  );
}
