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
