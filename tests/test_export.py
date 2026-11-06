"""Rekordbox XML + M3U8 export, checked against the file system and an independent XML reader."""
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient
from pyrekordbox.rbxml import RekordboxXml

from djlib.api import Config, create_app
from djlib.db import session_factory
from djlib.export import CAMELOT_TO_KEY, export_library, location
from djlib.ingest_csv import TrackRecord, save_playlist
from djlib.library import mark_owned, playlist_tracks, sync_library
from test_orchestrator import wav

ODD = "Duty Paid - Earl Grey (Féin-Ghràdh) & Co #1.wav"  # accents, &, #, parentheses, spaces


def library(tmp_path):
    lib = tmp_path / "DJ"
    wav(lib / "08092026" / "Kai Van Dongen - Flash (Original Mix).wav", 240)
    wav(lib / "08092026" / ODD, 300)
    wav(lib / "shranz" / "SOLYD - Hot Beats (Extended Mix).wav", 300)
    wav(lib / "shranz" / "untagged thing.wav", 100)
    S = session_factory(tmp_path / "t.db")
    s = S()
    sync_library(s, lib)
    recs = [  # playlist order differs from crate order; one track is not owned
        TrackRecord("SOLYD", "Hot Beats - Extended Mix", bpm=150, camelot="8A"),
        TrackRecord("Nobody", "Not Downloaded", bpm=140),
        TrackRecord("Kai van Dongen", "Flash", bpm=145.5, camelot="11B", duration_s=240),
        TrackRecord("Duty Paid", "Earl Grey (Féin-Ghràdh) & Co #1", duration_s=300),
    ]
    pl, _ = save_playlist(s, "csv", "x.csv", "my set", recs)
    mark_owned(s, playlist_tracks(s, pl.id), lib)
    return lib, S, s


def test_export_writes_rekordbox_xml_and_m3u(tmp_path):
    lib, _, s = library(tmp_path)
    r = export_library(s, lib, tmp_path / "out")
    assert r.tracks == 4
    assert r.crates == {"08092026": 2, "shranz": 2}
    assert r.playlists == {"my set": (3, 1)}

    doc = ET.parse(r.xml_path).getroot()
    coll = {t.get("TrackID"): t for t in doc.find("COLLECTION")}
    for t in coll.values():  # every Location is a real file once the URL is decoded
        loc = t.get("Location")
        assert loc.startswith("file://localhost/")
        assert Path(unquote(loc[len("file://localhost"):])).is_file(), loc
    odd = next(t for t in coll.values() if t.get("Location").endswith(".wav") and "Earl" in t.get("Name"))
    assert "%C3%A9" in odd.get("Location") and "%26" in odd.get("Location") and "%23" in odd.get("Location")

    hot = next(t for t in coll.values() if t.get("Artist") == "SOLYD")
    assert hot.get("AverageBpm") == "150.00" and hot.get("Tonality") == "Am" and hot.get("Mix") == "Extended Mix"
    assert hot.get("Kind") == "WAV File" and hot.get("TotalTime") == "300"
    flash = next(t for t in coll.values() if t.get("Name") == "Flash")
    assert flash.get("Tonality") == "A"
    untagged = next(t for t in coll.values() if "untagged" in t.get("Name"))
    assert untagged.get("AverageBpm") is None  # no track: filename only

    nodes = {n.get("Name"): n for n in doc.iter("NODE")}
    set_keys = [k.get("Key") for k in nodes["my set"]]
    assert [coll[k].get("Artist") for k in set_keys] == ["SOLYD", "Kai van Dongen", "Duty Paid"]  # playlist order
    assert nodes["my set"].get("Entries") == "3" and nodes["my set"].get("KeyType") == "0"
    assert nodes["Crates"].get("Count") == "2"

    # An independent reader of the Rekordbox format agrees on the structure.
    x = RekordboxXml(str(r.xml_path))
    assert x.num_tracks == 4
    top = x.get_playlist("DJ Library", "Playlists", "my set")
    assert len(top.get_tracks()) == 3

    m3u = (r.m3u_dir / "Playlists" / "my set.m3u8").read_text().splitlines()
    assert m3u[0] == "#EXTM3U" and m3u[1] == "#EXTINF:300,SOLYD - Hot Beats"
    assert Path(m3u[2]).is_file() and len(m3u) == 7
    assert (r.m3u_dir / "Crates" / "shranz.m3u8").exists()


def test_reexport_removes_playlists_that_are_gone(tmp_path):
    lib, _, s = library(tmp_path)
    out = tmp_path / "out"
    stale = out / "m3u" / "Crates" / "old crate.m3u8"
    stale.parent.mkdir(parents=True)
    stale.write_text("#EXTM3U\n")
    export_library(s, lib, out)
    assert not stale.exists()


def test_camelot_table_is_complete():
    assert len(CAMELOT_TO_KEY) == 24 and len(set(CAMELOT_TO_KEY.values())) == 24


def test_location_encoding():
    assert location("/Users/me/DJ/A B (x).flac") == "file://localhost/Users/me/DJ/A%20B%20%28x%29.flac"


def test_api_export_and_staleness(tmp_path):
    lib, S, s = library(tmp_path)
    s.close()
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "rej",
                 background=False, export_dir=tmp_path / "exports")
    c = TestClient(create_app(S, cfg))
    st = c.get("/api/export").json()
    assert st["exists"] is False
    r = c.post("/api/export").json()
    assert r["exists"] and not r["stale"] and r["tracks"] == 4
    assert r["playlists"]["my set"] == {"tracks": 3, "missing": 1}
    assert Path(r["xml_path"]) == tmp_path / "exports" / "rekordbox.xml"

    wav(lib / "shranz" / "Someone - New One.wav", 200)
    c.post("/api/library/scan")
    assert c.get("/api/export").json()["stale"] is True
