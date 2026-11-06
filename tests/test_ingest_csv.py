from pathlib import Path

import pytest
from sqlalchemy import func, select

from djlib.db import PlaylistItem, Track, session_factory
from djlib.ingest_csv import import_csv

REAL = Path(__file__).resolve().parents[2]  # ~/Desktop/soulseek, where the real playlists live
HEADER = "  #,Song,Artist,BPM,Camelot,Energy,Added At,Duration,Popularity,Genres,Parent Genres,Album,Album Date,Dance,Acoustic,Instrumental,Valence,Speech,Live,Loud (Db),Key,Time Signature,Spotify Track Id,Label,ISRC,Explicit\n"


@pytest.fixture
def S(tmp_path):
    return session_factory(tmp_path / "t.db")


def write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("﻿" + HEADER + "".join(rows), encoding="utf-8")
    return p


ROW_CRYPTIC = '32,"Cryptic - Kobosil Hart Mix","KOBOSIL, Ueberrest",155,11A,100,2026-01-24,05:02,52,"hard techno","Electronic","Cryptic",2025-03-21,80,1,77,52,10,30,-4,F#/G♭ minor,4,7fXK4nSADxslMaAHshrkZI,"44 LABEL GROUP",QM6MZ2546990,no\n'
ROW_DARIN = '29,"Dārin","Phil Berg",140,8A,90,2026-08-04,05:00,20,"techno","Electronic","Dārin",2026-07-17,60,1,90,30,10,10,-8,A minor,4,abc123,"Mutual Rytm",DEH000000001,no\n'


def test_import_parses_dj_metadata_and_version(S, tmp_path):
    p = write(tmp_path, "pl.csv", [ROW_CRYPTIC, ROW_DARIN])
    with S() as s:
        pl, st = import_csv(s, p)
        assert (st.rows, st.new_tracks, st.items_added) == (2, 2, 2)
        t = s.scalar(select(Track).where(Track.isrc == "QM6MZ2546990"))
        assert (t.title, t.version, t.duration_s, t.bpm, t.camelot, t.energy, t.label) == (
            "Cryptic", "Kobosil Hart Mix", 302, 155, "11A", 100, "44 LABEL GROUP")


def test_reimport_never_duplicates(S, tmp_path):
    p = write(tmp_path, "pl.csv", [ROW_CRYPTIC, ROW_DARIN])
    with S() as s:
        import_csv(s, p)
        _, st = import_csv(s, p)
        assert (st.new_tracks, st.existing_tracks, st.items_added) == (0, 2, 0)
        assert s.scalar(select(func.count(Track.id))) == 2
        assert s.scalar(select(func.count(PlaylistItem.id))) == 2


def test_same_track_in_two_playlists_is_one_track(S, tmp_path):
    a = write(tmp_path, "a.csv", [ROW_CRYPTIC])
    b = write(tmp_path, "b.csv", [ROW_CRYPTIC, ROW_DARIN])
    with S() as s:
        import_csv(s, a)
        _, st = import_csv(s, b)
        assert (st.new_tracks, st.existing_tracks) == (1, 1)
        assert s.scalar(select(func.count(Track.id))) == 2


@pytest.mark.parametrize("name,count", [("tch .csv", 134), ("shranz.csv", 177), ("tch2.csv", 33)])
def test_real_playlists_import_completely(S, name, count):
    path = REAL / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    with S() as s:
        _, st = import_csv(s, path)
        assert st.rows == count and st.skipped_rows == 0
        assert st.items_added == st.new_tracks + st.existing_tracks
