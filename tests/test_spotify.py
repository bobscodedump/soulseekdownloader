"""Spotify import against a fake Spotify (httpx.MockTransport) shaped like the Nov 2026 API."""
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from djlib.db import Track, session_factory
from djlib.ingest_csv import import_csv, save_playlist
from djlib.spotify import (LIKED, MemoryStore, Spotify, SpotifyError, authorize_url, challenge, make_verifier,
                           playlist_id)

PID = "4YS9ct5n4NWBf1lFN6a2iv"


def tr(i, name, artists, ms, isrc=None):
    return {"type": "track", "id": f"id{i:020d}"[:22], "name": name, "duration_ms": ms,
            "artists": [{"name": a} for a in artists], "album": {"name": "Alb"},
            "external_ids": {"isrc": isrc} if isrc else {}}


ITEMS = [
    {"added_at": "2026-10-17T10:00:00Z", "is_local": False,
     "item": tr(1, "Cryptic - Kobosil Hart Mix", ["KOBOSIL", "Ueberrest"], 302000, "qm6mz2546990")},
    {"added_at": "2026-10-18T10:00:00Z", "is_local": True, "item": tr(2, "my local file", ["me"], 1000)},
    {"added_at": "2026-10-19T10:00:00Z", "is_local": False, "item": {"type": "episode", "name": "A podcast"}},
] + [{"added_at": "2026-10-20T10:00:00Z", "is_local": False, "track": tr(10 + i, f"Song {i}", ["Artist"], 300000)}
     for i in range(60)]  # older "track" key, and enough rows to need two pages


class FakeSpotify:
    def __init__(self, owned=True):
        self.owned, self.calls, self.tokens_issued = owned, [], 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, dict(req.url.params)))
        if req.url.path == "/api/token":
            body = parse_qs(req.content.decode())
            assert "client_secret" not in body  # PKCE: no secret, ever
            if body["grant_type"] == ["authorization_code"]:
                assert 43 <= len(body["code_verifier"][0]) <= 128
            self.tokens_issued += 1
            out = {"access_token": f"at{self.tokens_issued}", "expires_in": 3600}
            if body["grant_type"] == ["authorization_code"]:
                out["refresh_token"] = "rt1"  # refresh responses omit it, like the real API may
            return httpx.Response(200, json=out)
        assert req.headers["Authorization"].startswith("Bearer at")
        if not self.owned and req.url.path.endswith("/items"):
            return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
        if req.url.path == f"/v1/playlists/{PID}":
            return httpx.Response(200, json={"name": "tch3 on Spotify"})
        if req.url.path in (f"/v1/playlists/{PID}/items", "/v1/me/tracks"):
            assert int(req.url.params.get("limit", 50)) <= 50
            off = int(req.url.params.get("offset", 0))
            page = ITEMS[off:off + 50]
            nxt = f"https://api.spotify.com{req.url.path}?offset={off + 50}&limit=50" if off + 50 < len(ITEMS) else None
            return httpx.Response(200, json={"items": page, "next": nxt, "total": len(ITEMS)})
        return httpx.Response(404, json={})


def client(fake, store=None):
    sp = Spotify("cid", "http://127.0.0.1:8765/api/spotify/callback", store or MemoryStore(),
                 httpx.Client(transport=httpx.MockTransport(fake)))
    sp.exchange("code123", make_verifier())
    return sp


def test_pkce_challenge_matches_rfc7636_example():
    assert challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    v = make_verifier()
    assert 43 <= len(v) <= 128


def test_authorize_url_uses_loopback_ip_and_pkce():
    q = parse_qs(urlparse(authorize_url("cid", "http://127.0.0.1:8765/api/spotify/callback", "v" * 50, "st")).query)
    assert q["code_challenge_method"] == ["S256"] and q["response_type"] == ["code"] and q["state"] == ["st"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8765/api/spotify/callback"]
    assert "playlist-read-private" in q["scope"][0]


@pytest.mark.parametrize("link,pid", [
    ("https://open.spotify.com/playlist/4YS9ct5n4NWBf1lFN6a2iv?si=1a778c2c6b69472f", PID),
    ("https://open.spotify.com/intl-de/playlist/4YS9ct5n4NWBf1lFN6a2iv", PID),
    ("spotify:playlist:4YS9ct5n4NWBf1lFN6a2iv", PID),
    ("Liked Songs", LIKED),
])
def test_playlist_links(link, pid):
    assert playlist_id(link) == pid


def test_album_and_junk_links_explain_themselves():
    with pytest.raises(SpotifyError, match="album link"):
        playlist_id("https://open.spotify.com/album/1A2B3C4D5E6F7G8H9I0J1K")
    with pytest.raises(SpotifyError, match="Not a Spotify playlist link"):
        playlist_id("https://example.com")


def test_import_pages_skips_local_and_podcasts_and_keeps_versions(tmp_path):
    fake = FakeSpotify()
    pl = client(fake).playlist(f"https://open.spotify.com/playlist/{PID}?si=x")
    assert pl.name == "tch3 on Spotify" and len(pl.records) == 61 and pl.skipped == 2
    first = pl.records[0]
    assert (first.artist, first.raw_title, first.isrc, first.duration_s, first.added_at) == (
        "KOBOSIL, Ueberrest", "Cryptic - Kobosil Hart Mix", "QM6MZ2546990", 302, "2026-10-17")
    pages = [c for c in fake.calls if c[1].endswith("/items")]
    assert len(pages) == 2 and pages[0][2]["limit"] == "50"
    with session_factory(tmp_path / "t.db")() as s:
        p, st = save_playlist(s, "spotify", PID, pl.name, pl.records)
        t = s.query(Track).filter(Track.isrc == "QM6MZ2546990").one()
        assert (t.title, t.version) == ("Cryptic", "Kobosil Hart Mix") and st.rows == 61


def test_spotify_and_csv_fill_each_others_gaps(tmp_path):
    """Spotify has no BPM/key/energy for new apps; the CSV exporter does. Same ISRC = same track."""
    sp_pl = client(FakeSpotify()).playlist(PID)
    csv = tmp_path / "x.csv"
    csv.write_text('  #,Song,Artist,BPM,Camelot,Energy,Duration,ISRC\n1,"Cryptic - Kobosil Hart Mix","KOBOSIL, Ueberrest",155,11A,100,05:02,QM6MZ2546990\n')
    with session_factory(tmp_path / "t.db")() as s:
        save_playlist(s, "spotify", PID, sp_pl.name, sp_pl.records)
        _, st = import_csv(s, csv)
        assert st.new_tracks == 0 and st.existing_tracks == 1
        t = s.query(Track).filter(Track.isrc == "QM6MZ2546990").one()
        assert (t.bpm, t.camelot, t.energy, t.spotify_id) == (155, "11A", 100, sp_pl.records[0].spotify_id)


def test_not_owned_playlist_gives_actionable_message():
    with pytest.raises(SpotifyError, match="you own or collaborate on"):
        client(FakeSpotify(owned=False)).playlist(PID)


def test_expired_token_refreshes_without_secret_and_keeps_refresh_token():
    fake, store = FakeSpotify(), MemoryStore()
    sp = client(fake, store)
    store.tok["expires_at"] = time.time() - 1
    sp.playlist("liked")
    assert store.tok["access_token"] == "at2" and store.tok["refresh_token"] == "rt1"


def test_not_connected():
    sp = Spotify("cid", "http://127.0.0.1:8765/cb", MemoryStore(), httpx.Client(transport=httpx.MockTransport(FakeSpotify())))
    assert not sp.connected
    with pytest.raises(SpotifyError, match="Not connected"):
        sp.playlist(PID)
