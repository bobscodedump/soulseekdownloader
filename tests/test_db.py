from sqlalchemy import inspect

from djlib.db import File, Track, TrackStatus, make_engine, session_factory


def test_schema_creates_all_tables(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    assert set(inspect(engine).get_table_names()) == {
        "track", "file", "crate", "playlist", "playlist_item",
        "download_job", "download_attempt", "match_link", "setting",
    }


def test_track_file_roundtrip(tmp_path):
    Session = session_factory(tmp_path / "t.db")
    with Session() as s:
        t = Track(artist="Phil Berg", title="Tanoo", norm_key="phil berg|tanoo|")
        s.add(t)
        s.flush()
        s.add(File(track_id=t.id, path="/x/tanoo.flac", format="flac", size=1, mtime=0, sha1="ab"))
        s.commit()
    with Session() as s:
        t = s.query(Track).one()
        assert t.status is TrackStatus.wanted
        assert [f.path for f in t.files] == ["/x/tanoo.flac"]


def test_existing_database_gains_new_columns(tmp_path):
    """A database created before a column existed is upgraded in place, keeping its rows."""
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE playlist (id INTEGER PRIMARY KEY, name VARCHAR, source VARCHAR, source_ref VARCHAR UNIQUE, imported_at DATETIME)")
    con.execute("INSERT INTO playlist VALUES (1, 'tch', 'csv', '/x.csv', '2026-10-17')")
    con.commit()
    con.close()
    from djlib.db import Playlist
    with session_factory(path)() as s:
        p = s.get(Playlist, 1)
        assert p.name == "tch" and p.crate is None
        p.crate = "17102026"
        s.commit()
