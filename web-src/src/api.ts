// Typed client for the local djlib API.

export type Status =
  | "wanted" | "owned" | "review" | "failed_no_results" | "failed_transfer" | "buy" | "ignored"
  | "queued" | "searching" | "downloading" | "verifying";

export interface Job {
  id: number; kind: string; title: string; state: "queued" | "running" | "finished" | "failed" | "aborted";
  log: string[]; log_total: number; result: Record<string, unknown>; started_at: string | null; finished_at: string | null;
}
export interface Summary {
  library_root: string; files: number; tracks: Partial<Record<Status, number>>; review: number;
  today_crate: string; current_job: Job | null; auto_retry: boolean; background_error: string | null;
}
export interface PlaylistRow {
  id: number; name: string; crate: string | null; source: string; imported_at: string;
  counts: Partial<Record<Status, number>>; total: number;
}
export interface TrackRow {
  id: number; artist: string; title: string; version: string | null; bpm: number | null; camelot: string | null;
  energy: number | null; duration: number | null; status: Status; reason: string | null;
}
export interface LibraryRow {
  kind: "file" | "track"; file_id: number | null; track_id: number | null; artist: string; title: string;
  version: string | null; bpm: number | null; camelot: string | null; energy: number | null; crate: string;
  format: string; bitrate: number | null; duration: number | null; status: Status; path: string | null; reason?: string;
}
export interface Crate { name: string; kind: string; files: number }
export interface ReviewItem {
  kind: "match" | "rejected"; id: number; track_id: number; artist: string; title: string; version: string | null;
  wanted_duration: number | null; file: string; path: string; reason: string | null;
}
export interface FailedRow {
  id: number; artist: string; title: string; version: string | null; status: Status; reason: string | null; reason_full: string | null;
  tries: number; max_tries: number; next_retry_at: string | null; buy: { bandcamp: string; beatport: string };
}
export interface SpotifyStatus { client_id: string; redirect_uri: string; connected: boolean; user: string | null; error?: string }
export interface ImportResult { id: number; name: string; rows: number; new_tracks: number; existing_tracks: number; owned: number; review: number; skipped?: number }
export interface ExportStatus {
  xml_path: string; m3u_dir: string; exists: boolean; exported_at: string | null; stale: boolean;
  tracks?: number; crates?: Record<string, number>; playlists?: Record<string, { tracks: number; missing: number }>;
}
export interface Settings { library_root: string; auto_retry: boolean; rejected_dir: string; sockseek_path: string; sockseek_ok: boolean }

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { msg = (await r.json()).detail ?? msg; } catch { /* not JSON */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json() as Promise<T>;
}
const json = (method: string, body: unknown): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

export const api = {
  summary: () => req<Summary>("/api/summary"),
  crates: () => req<Crate[]>("/api/crates"),
  library: (params: Record<string, string>) =>
    req<{ total: number; rows: LibraryRow[] }>("/api/library?" + new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v !== "")))),
  playlists: () => req<PlaylistRow[]>("/api/playlists"),
  playlist: (id: number) => req<{ id: number; name: string; crate: string | null; tracks: TrackRow[] }>(`/api/playlists/${id}`),
  importCsv: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<ImportResult>(
      "/api/playlists/import", { method: "POST", body: fd });
  },
  setCrate: (id: number, crate: string) => req<{ crate: string }>(`/api/playlists/${id}`, json("PATCH", { crate })),
  download: (id: number, crate: string, dry_run: boolean) =>
    req<Job>(`/api/playlists/${id}/download`, json("POST", { crate, dry_run })),
  jobs: () => req<Job[]>("/api/jobs"),
  job: (id: number, after = 0) => req<Job>(`/api/jobs/${id}?after=${after}`),
  review: () => req<ReviewItem[]>("/api/review"),
  reviewAction: (kind: string, id: number, action: "accept" | "dismiss") =>
    req<{ ok: boolean; filed?: string }>(`/api/review/${kind}/${id}/${action}`, { method: "POST" }),
  failed: () => req<FailedRow[]>("/api/failed"),
  setStatus: (id: number, status: "retry" | "buy" | "ignored") => req(`/api/tracks/${id}/status`, json("POST", { status })),
  runRetries: () => req<Job>("/api/retries/run", { method: "POST" }),
  exportStatus: () => req<ExportStatus>("/api/export"),
  exportNow: () => req<ExportStatus>("/api/export", { method: "POST" }),
  spotify: () => req<SpotifyStatus>("/api/spotify"),
  setSpotifyClient: (client_id: string) => req<SpotifyStatus>("/api/spotify", json("PUT", { client_id })),
  spotifyLogout: () => req("/api/spotify/logout", { method: "POST" }),
  spotifyImport: (url: string) => req<ImportResult>("/api/spotify/import", json("POST", { url })),
  settings: () => req<Settings>("/api/settings"),
  saveSettings: (s: Partial<Settings>) => req<Settings>("/api/settings", json("PUT", s)),
  scan: () => req<{ seen: number; added: number; moved: number; removed: number; needs_tags: number }>(
    "/api/library/scan", { method: "POST" }),
};

export const fmtDuration = (s: number | null) =>
  s == null ? "" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

export const STATUS_LABEL: Record<string, string> = {
  owned: "Owned", wanted: "Wanted", review: "Review", failed_no_results: "Not found", failed_transfer: "Transfer failed",
  buy: "Buy", ignored: "Ignored", queued: "Queued", searching: "Searching", downloading: "Downloading", verifying: "Verifying",
};
