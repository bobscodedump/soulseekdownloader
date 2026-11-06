import os
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from djlib.db import Crate, CrateKind, File, session_factory
from djlib.scanner import parse_filename, scan


@pytest.mark.parametrize("stem,artist,title", [
    ("02-takt-zero_sum_game_(syep016)-3b204552", "takt", "zero sum game (syep016)"),
    ("03-estella_boersma-cerberus", "estella boersma", "cerberus"),
    ("BCCO, future.666 - XTRA LOOP (Original Mix) [www.dj-promo.org]", "BCCO, future.666", "XTRA LOOP (Original Mix)"),
    ("Cirkle_Fading Trails_03_Machina", "Cirkle", "Machina"),
    ("Duty Paid - Expressions - 04 Earl Grey (Féin-Ghràdh)", "Duty Paid", "Earl Grey (Féin-Ghràdh)"),
    ("82. Phil Berg - Darin", "Phil Berg", "Darin"),
    ("01 - Marcal - Bionic Jungle", "Marcal", "Bionic Jungle"),
    ("20-Danny Wabbit - Solace (Original Mix)", "Danny Wabbit", "Solace (Original Mix)"),
    ("IGLO & Paul Hauck — Neustadt [SIDEB003]", "IGLO & Paul Hauck", "Neustadt [SIDEB003]"),
    ("LONGBEACH DRIVE", None, "LONGBEACH DRIVE"),
])
def test_parse_filename(stem, artist, title):
    assert parse_filename(stem) == (artist, title)


def _lib(tmp_path):
    root = tmp_path / "DJ"
    (root / "17102026").mkdir(parents=True)
    (root / "shranz").mkdir()
    (root / "17102026" / "01-funkhaus-lumen.mp3").write_bytes(b"A" * 5000)
    (root / "shranz" / "KUKO - Paranoid.wav").write_bytes(b"B" * 7000)
    (root / "shranz" / "notes.txt").write_text("not audio")
    (root / "shranz" / "x.flac.incomplete").write_bytes(b"C")
    return root


def test_scan_adds_files_crates_and_skips_non_audio(tmp_path):
    root = _lib(tmp_path)
    S = session_factory(tmp_path / "t.db")
    with S() as s:
        st = scan(s, root)
        assert (st.seen, st.added) == (2, 2)
        kinds = {c.name: c.kind for c in s.scalars(select(Crate))}
        assert kinds == {"17102026": CrateKind.dated, "shranz": CrateKind.genre}
        f = s.scalar(select(File).where(File.path.endswith("Paranoid.wav")))
        assert (f.parsed_artist, f.parsed_title, f.crate.name) == ("KUKO", "Paranoid", "shranz")


def test_rescan_is_incremental_and_detects_moves_and_deletes(tmp_path):
    root = _lib(tmp_path)
    S = session_factory(tmp_path / "t.db")
    with S() as s:
        scan(s, root)
        ids = {Path(f.path).name: f.id for f in s.scalars(select(File))}
        st = scan(s, root)
        assert (st.unchanged, st.added, st.moved, st.removed) == (2, 0, 0, 0)

        shutil.move(root / "shranz" / "KUKO - Paranoid.wav", root / "17102026" / "KUKO - Paranoid.wav")
        os.remove(root / "17102026" / "01-funkhaus-lumen.mp3")
        st = scan(s, root)
        assert (st.moved, st.removed, st.added) == (1, 1, 0)
        f = s.scalar(select(File))
        assert f.id == ids["KUKO - Paranoid.wav"] and f.crate.name == "17102026"


def test_scan_never_modifies_library(tmp_path):
    root = _lib(tmp_path)
    before = {p: (p.stat().st_size, p.stat().st_mtime) for p in root.rglob("*")}
    with session_factory(tmp_path / "t.db")() as s:
        scan(s, root)
    assert before == {p: (p.stat().st_size, p.stat().st_mtime) for p in root.rglob("*")}


def test_untagged_file_still_gets_duration(tmp_path):
    """Regression: mutagen objects with no tags are falsy; duration must still be read."""
    import wave
    p = tmp_path / "LIKE TO PARTY.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000 * 3)
    from djlib.scanner import _tags
    t = _tags(p)
    assert t["tag_artist"] is None and t["duration_s"] == 3.0 and t["sample_rate"] == 8000
