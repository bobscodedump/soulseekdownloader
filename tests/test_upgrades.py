"""Lossless upgrades with a fake Soulseek and real MP3s (made with ffmpeg) in a temp library."""
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from djlib import review
from djlib.db import DownloadAttempt, File, MatchLink, Track, TrackStatus, session_factory
from djlib.export import export_library
from djlib.ingest_csv import TrackRecord, save_playlist
from djlib.library import mark_owned, playlist_tracks, sync_library
from djlib.upgrades import candidates, run_upgrades
from test_orchestrator import R, FakeSockseek, wav

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg to make MP3s")


def mp3(path: Path, seconds: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", str(seconds),
                    "-b:a", "8k", "-y", str(path)], check=True)


RESULTS = {
    "Funkhaus - Lumen": [
        R("lossy", r"m\Funkhaus - Lumen.mp3", 300, 320),                     # not lossless: never downloaded
        R("ext", r"m\Funkhaus - Lumen (Extended Mix).wav", 362),             # another edit: never downloaded
        R("liar", r"m\Funkhaus - Lumen.wav", 300),                            # says 300 s, is 240 s
        R("good", r"m\05-funkhaus-lumen.wav", 301),
    ],
    "Selva - HEMPTOTE": [],
    "HEMPTOTE - Selva": [],
}
TRANSFERS = {
    ("liar", r"m\Funkhaus - Lumen.wav"): 240,
    ("good", r"m\05-funkhaus-lumen.wav"): 300.6,
}


@pytest.fixture
def env(tmp_path):
    lib = tmp_path / "DJ"
    mp3(lib / "17102026" / "Funkhaus - Lumen.mp3", 300)
    mp3(lib / "shranz" / "HEMPTOTE - Selva.mp3", 200)
    mp3(lib / "shranz" / "SOLYD - Hot Beats.mp3", 240)
    wav(lib / "techno" / "SOLYD - Hot Beats.wav", 240)  # already has a lossless copy
    S = session_factory(tmp_path / "t.db")
    s = S()
    sync_library(s, lib)
    pl, _ = save_playlist(s, "csv", "p.csv", "set", [TrackRecord("Funkhaus", "Lumen", bpm=140),
                                                    TrackRecord("HEMPTOTE", "Selva"), TrackRecord("SOLYD", "Hot Beats")])
    mark_owned(s, playlist_tracks(s, pl.id), lib)
    assert all(t.status is TrackStatus.owned for t in playlist_tracks(s, pl.id))
    return s, lib, tmp_path


def by_title(s):
    return {t.title: t for t in s.scalars(select(Track))}


def test_candidates_are_lossy_tracks_without_a_lossless_copy(env):
    s, lib, _ = env
    got = {c.track.title: c.file.format for c in candidates(s, lib)}
    assert got == {"Lumen": "mp3", "Selva": "mp3"}


def test_upgrade_replaces_mp3_and_keeps_the_old_file(env):
    s, lib, tmp = env
    fake = FakeSockseek(tmp / "work", RESULTS, TRANSFERS)
    ids = [c.file.id for c in candidates(s, lib)]
    rep = run_upgrades(s, ids, lib, fake, tmp / "rejected", tmp / "replaced", log=lambda m: None,
                       is_nicotine_running=lambda: False)

    # Only lossless copies of the same length are downloaded; the liar is measured and rejected.
    assert [u for u, _ in fake.downloaded] == ["liar", "good"]
    assert len(rep.upgraded) == 1 and rep.rejected == 1

    old, new, parked = rep.upgraded[0]
    assert new == (lib / "17102026" / "05-funkhaus-lumen.wav").resolve() and new.exists()
    assert not old.exists() and parked == (tmp / "replaced" / "17102026" / "Funkhaus - Lumen.mp3").resolve()
    assert parked.exists()  # moved, never deleted

    t = by_title(s)["Lumen"]
    assert t.status is TrackStatus.owned and t.bpm == 140
    files = s.scalars(select(File).where(File.track_id == t.id)).all()
    assert [Path(f.path).name for f in files] == ["05-funkhaus-lumen.wav"]
    assert not s.scalar(select(File).where(File.path == str(old)))
    links = s.scalars(select(MatchLink).where(MatchLink.track_id == t.id)).all()
    assert {Path(s.get(File, ml.file_id).path).name for ml in links} == {"05-funkhaus-lumen.wav"}

    # Selva: nothing found; the track stays owned and the file remembers the check.
    selva = by_title(s)["Selva"]
    assert selva.status is TrackStatus.owned
    f = s.scalar(select(File).where(File.track_id == selva.id))
    assert f.upgrade_note == "no search results" and f.upgrade_checked_at
    assert (lib / "shranz" / "HEMPTOTE - Selva.mp3").exists()

    # The rejected download is parked but kept out of the review queue (accepting it would add a duplicate).
    rej = s.scalar(select(DownloadAttempt).where(DownloadAttempt.result == "rejected"))
    assert "not the same edit: -60s" in rej.error and Path(rej.local_path).exists()
    assert review.items(s) == []

    # Lumen is no longer a candidate; a rescan agrees with the database; export points at the WAV.
    assert {c.track.title for c in candidates(s, lib)} == {"Selva"}
    st = sync_library(s, lib)
    assert (st.added, st.removed) == (0, 0)
    xml = export_library(s, lib, tmp / "out").xml_path.read_text()
    assert "05-funkhaus-lumen.wav" in xml and "Funkhaus%20-%20Lumen.mp3" not in xml


def test_dry_run_changes_nothing(env):
    s, lib, tmp = env
    fake = FakeSockseek(tmp / "work", RESULTS, TRANSFERS)
    before = sorted(str(p) for p in lib.rglob("*"))
    rep = run_upgrades(s, [c.file.id for c in candidates(s, lib)], lib, fake, tmp / "rejected", tmp / "replaced",
                       dry_run=True, log=lambda m: None, is_nicotine_running=lambda: False)
    assert fake.downloaded == [] and sorted(str(p) for p in lib.rglob("*")) == before
    lumen = next(p for p in rep.plans if p.track.title == "Lumen")
    assert [c.result.username for c in lumen.candidates] == ["liar", "good"]
    assert lumen.dropped == {"not lossless": 1, "different length": 1}
