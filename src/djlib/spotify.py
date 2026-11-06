"""Spotify login (Authorization Code + PKCE, no client secret) and playlist import.

Rules as of Nov 2026 (developer.spotify.com, checked 2026-11-06):
- Development-mode apps only read playlists the user owns or collaborates on (others: 403).
- Playlist items: GET /v1/playlists/{id}/items, max 50 per page; each entry's track is under "item".
- Audio features (BPM/key/energy) and album label are not available to new apps; those come from the CSV exporter.
- Redirect URIs must use 127.0.0.1 (localhost is refused); http is allowed for loopback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .ingest_csv import TrackRecord

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
LIKED = "liked"  # pseudo playlist id for Liked Songs


class SpotifyError(RuntimeError):
    pass


# ---------- token storage ----------
class TokenStore:
    """macOS Keychain via keyring; tests pass a MemoryStore."""

    service = "djlib-spotify"

    def load(self) -> dict | None:
        import keyring
        raw = keyring.get_password(self.service, "token")
        return json.loads(raw) if raw else None

    def save(self, tok: dict) -> None:
        import keyring
        keyring.set_password(self.service, "token", json.dumps(tok))

    def clear(self) -> None:
        import keyring
        try:
            keyring.delete_password(self.service, "token")
        except keyring.errors.PasswordDeleteError:
            pass


class MemoryStore(TokenStore):
    def __init__(self):
        self.tok = None

    def load(self):
        return self.tok

    def save(self, tok):
        self.tok = tok

    def clear(self):
        self.tok = None


# ---------- PKCE ----------
def make_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]  # 43..128 chars of [A-Za-z0-9-_]


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorize_url(client_id: str, redirect_uri: str, verifier: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri, "state": state,
        "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge(verifier),
    })


# ---------- links ----------
_LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?playlist/|spotify:playlist:)(?P<id>[A-Za-z0-9]{22})")


def playlist_id(link: str) -> str:
    """Playlist ID from a share link or URI; 'liked' / 'liked songs' means the user's Liked Songs."""
    link = link.strip()
    if link.lower() in ("liked", "liked songs", "spotify-likes"):
        return LIKED
    m = _LINK.search(link)
    if m:
        return m["id"]
    if re.fullmatch(r"[A-Za-z0-9]{22}", link):
        return link
    if "/album/" in link or ":album:" in link:
        raise SpotifyError("That's an album link. Paste a playlist link (or add the album to a playlist).")
    raise SpotifyError("Not a Spotify playlist link. Use Share > Copy link to playlist in Spotify.")


@dataclass
class SpotifyPlaylist:
    id: str
    name: str
    records: list[TrackRecord]
    skipped: int  # local files, podcasts, unavailable tracks


class Spotify:
    def __init__(self, client_id: str, redirect_uri: str, store: TokenStore | None = None,
                 http: httpx.Client | None = None):
        self.client_id, self.redirect_uri = client_id, redirect_uri
        self.store = store or TokenStore()
        self.http = http or httpx.Client(timeout=20)

    # -- auth --
    def exchange(self, code: str, verifier: str) -> None:
        r = self.http.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                            "redirect_uri": self.redirect_uri, "client_id": self.client_id,
                                            "code_verifier": verifier})
        self._store_token(r)

    def _refresh(self, tok: dict) -> dict:
        r = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                            "client_id": self.client_id})
        if r.status_code == 400 and "invalid_grant" in r.text:
            self.store.clear()
            raise SpotifyError("Spotify login expired. Connect again in Settings.")
        new = self._store_token(r, keep_refresh=tok["refresh_token"])
        return new

    def _store_token(self, r: httpx.Response, keep_refresh: str | None = None) -> dict:
        if r.status_code != 200:
            raise SpotifyError(f"Spotify login failed ({r.status_code}): {r.text[:200]}")
        d = r.json()
        tok = {"access_token": d["access_token"], "expires_at": time.time() + int(d.get("expires_in", 3600)) - 60,
               # A refresh response may omit refresh_token: keep using the old one.
               "refresh_token": d.get("refresh_token") or keep_refresh}
        self.store.save(tok)
        return tok

    @property
    def connected(self) -> bool:
        return self.store.load() is not None

    def _token(self) -> str:
        tok = self.store.load()
        if not tok:
            raise SpotifyError("Not connected to Spotify. Connect in Settings.")
        if time.time() >= tok["expires_at"]:
            tok = self._refresh(tok)
        return tok["access_token"]

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = self.http.get(url if url.startswith("http") else API + url, params=params,
                          headers={"Authorization": f"Bearer {self._token()}"})
        if r.status_code == 401:  # token revoked early: refresh once
            tok = self._refresh(self.store.load())
            r = self.http.get(url if url.startswith("http") else API + url, params=params,
                              headers={"Authorization": f"Bearer {tok['access_token']}"})
        if r.status_code == 403:
            raise SpotifyError(
                "Spotify only lets this app read playlists you own or collaborate on. "
                "Open the playlist in Spotify, choose Add to other playlist > New playlist, and import that copy "
                "(or export it to CSV).")
        if r.status_code == 404:
            raise SpotifyError("Spotify can't find that playlist. Check the link, or whether it was deleted.")
        if r.status_code == 429:
            raise SpotifyError(f"Spotify rate limit reached; try again in {r.headers.get('Retry-After', 'a few')} seconds.")
        if r.status_code >= 400:
            raise SpotifyError(f"Spotify error {r.status_code}: {r.text[:200]}")
        return r.json()

    def me(self) -> dict:
        return self._get("/me")

    # -- import --
    def playlist(self, link: str) -> SpotifyPlaylist:
        pid = playlist_id(link)
        if pid == LIKED:
            name, url, params = "Liked Songs", "/me/tracks", {"limit": 50, "market": "from_token"}
        else:
            name = self._get(f"/playlists/{pid}", {"fields": "name"}).get("name") or pid
            url, params = f"/playlists/{pid}/items", {"limit": 50, "market": "from_token", "additional_types": "track"}
        records, skipped = [], 0
        while url:
            page = self._get(url, params)
            for it in page.get("items", []):
                tr = it.get("item") or it.get("track")  # "track" is the deprecated name
                if not tr or tr.get("type", "track") != "track" or it.get("is_local") or not tr.get("name"):
                    skipped += 1
                    continue
                records.append(TrackRecord(
                    artist=", ".join(a["name"] for a in tr.get("artists", []) if a.get("name")),
                    raw_title=tr["name"],
                    isrc=((tr.get("external_ids") or {}).get("isrc") or "").upper() or None,
                    spotify_id=tr.get("id"),
                    album=(tr.get("album") or {}).get("name"),
                    duration_s=(tr.get("duration_ms") or 0) / 1000 or None,
                    added_at=(it.get("added_at") or "")[:10] or None,
                ))
            url, params = page.get("next"), None  # "next" already carries the query
        return SpotifyPlaylist(pid, name, records, skipped)
