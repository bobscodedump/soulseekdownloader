import { useEffect, useState } from "react";
import { api, SpotifyStatus } from "./api";

// One-time setup: Spotify requires each user to register their own (free) developer app.
export default function SpotifySettings() {
  const [st, setSt] = useState<SpotifyStatus | null>(null);
  const [cid, setCid] = useState("");
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(() => {
    const q = new URLSearchParams(window.location.hash.split("?")[1] ?? "");
    if (q.get("spotify") === "connected") return { kind: "ok", text: "Connected to Spotify." };
    if (q.get("spotify_error")) return { kind: "error", text: `Spotify login failed: ${q.get("spotify_error")}` };
    return null;
  });
  const load = () => api.spotify().then((x) => { setSt(x); setCid(x.client_id); });
  useEffect(() => { load(); }, []);
  if (!st) return null;

  const saveId = async () => {
    try { setSt(await api.setSpotifyClient(cid)); setMsg({ kind: "ok", text: "Client ID saved. Now press Connect." }); }
    catch (e) { setMsg({ kind: "error", text: (e as Error).message }); }
  };
  const copy = () => navigator.clipboard?.writeText(st.redirect_uri).then(() => setMsg({ kind: "ok", text: "Redirect URI copied." }));

  return (
    <div className="spotify" data-testid="spotify-settings">
      <strong>Spotify</strong>
      {msg && <div className={`banner ${msg.kind}`}>{msg.text}</div>}
      {st.connected ? (
        <div className="row">
          <span className="ok" data-testid="spotify-status">Connected{st.user ? ` as ${st.user}` : ""}.</span>
          <span className="muted small">Paste playlist links on the Playlists screen.</span>
          <button onClick={() => api.spotifyLogout().then(load)}>Disconnect</button>
        </div>
      ) : (
        <>
          <span className="muted small" style={{ fontWeight: 400 }}>
            One-time setup (about 2 minutes). Spotify only lets apps read playlists you own or collaborate on,
            and gives no BPM or key; keep using your CSV exporter for those.
          </span>
          <ol>
            <li>Open the <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noreferrer">Spotify developer dashboard</a> and log in.</li>
            <li>Create app: any name, select <b>Web API</b>, and add this Redirect URI:{" "}
              <code data-testid="redirect-uri">{st.redirect_uri}</code> <button onClick={copy}>Copy</button></li>
            <li>Open the app's Settings, copy the <b>Client ID</b>, and paste it here:</li>
          </ol>
          <div className="row">
            <input value={cid} onChange={(e) => setCid(e.target.value)} placeholder="32-character Client ID" className="grow" data-testid="spotify-client-id" />
            <button onClick={saveId} disabled={!cid || cid === st.client_id}>Save</button>
            <a className={`button primary ${st.client_id ? "" : "disabled"}`} href={st.client_id ? "/api/spotify/login" : undefined}
               data-testid="spotify-connect">Connect</a>
          </div>
        </>
      )}
    </div>
  );
}
