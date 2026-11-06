"""Fingerprint duplicates with synthetic songs: the same audio as WAV, MP3 and with extra leading silence."""
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select

from djlib import duplicates
from djlib.db import File, MatchLink, Track, TrackStatus, session_factory
from djlib.fingerprint import compute, fpcalc_path, similarity
from djlib.ingest_csv import TrackRecord, save_playlist
from djlib.library import mark_owned, playlist_tracks, sync_library

FPCALC = fpcalc_path()
pytestmark = pytest.mark.skipif(not FPCALC or not shutil.which("ffmpeg"), reason="needs fpcalc and ffmpeg")
RATE = 11025


def song(seed: int, seconds: float = 150, lead_silence: float = 0) -> np.ndarray:
    """A deterministic 'track': chords that change every half second, plus a kick."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(0.5 * RATE)) / RATE
    parts = []
    for _ in range(int(seconds * 2)):
        freqs = 110 * 2 ** (rng.integers(0, 36, 3) / 12)
        parts.append(sum(np.sin(2 * np.pi * f * t) for f in freqs) / 3 + 0.5 * np.sin(2 * np.pi * 50 * t) * np.exp(-t * 20))
    x = np.concatenate([np.zeros(int(lead_silence * RATE))] + parts)
    return (x / np.abs(x).max() * 0.8 * 32767).astype(np.int16)


def write_wav(path: Path, x: np.ndarray):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(x.tobytes())


def to_mp3(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-b:a", "128k", "-y", str(dest)], check=True)


@pytest.fixture
def lib(tmp_path):
    lib = tmp_path / "DJ"
    write_wav(lib / "09082026" / "IN FURCHT - FLY LIK3.wav", song(1))
    to_mp3(lib / "09082026" / "IN FURCHT - FLY LIK3.wav", lib / "16082026" / "A-05 untitled [ms].mp3")  # no usable name
    write_wav(lib / "techno" / "fly lik3 rip.wav", song(1, lead_silence=2))
    write_wav(lib / "techno" / "Other Artist - Different Song.wav", song(2))
    return lib


def test_same_audio_scores_high_and_different_audio_low(lib):
    a = compute(lib / "09082026" / "IN FURCHT - FLY LIK3.wav", FPCALC)
    mp3 = compute(lib / "16082026" / "A-05 untitled [ms].mp3", FPCALC)
    padded = compute(lib / "techno" / "fly lik3 rip.wav", FPCALC)
    other = compute(lib / "techno" / "Other Artist - Different Song.wav", FPCALC)
    assert similarity(a, mp3) > 0.93
    assert similarity(a, padded) > 0.93  # found at an offset
    assert similarity(a, other) < 0.75


def test_find_and_move_extra_copies(lib, tmp_path):
    s = session_factory(tmp_path / "t.db")()
    sync_library(s, lib)
    pl, _ = save_playlist(s, "csv", "p", "p", [TrackRecord("IN FURCHT", "FLY LIK3")])
    mark_owned(s, playlist_tracks(s, pl.id), lib)
    track = s.scalar(select(Track))
    assert track.status is TrackStatus.owned

    found = duplicates.find(s, lib, FPCALC, log=lambda m: None)
    assert len(found) == 1
    group = [Path(s.get(File, i).path).name for i in found[0]]
    assert sorted(group) == sorted(["IN FURCHT - FLY LIK3.wav", "fly lik3 rip.wav", "A-05 untitled [ms].mp3"])
    assert group[-1] == "A-05 untitled [ms].mp3"  # the lossy copy is suggested for moving out

    # Keep the unnamed rip; move the named WAV (the one linked to the track) and the MP3.
    keep = s.scalar(select(File).where(File.path.endswith("fly lik3 rip.wav")))
    others = [i for i in found[0] if i != keep.id]
    with pytest.raises(ValueError):
        duplicates.move_extras(s, keep.id, [s.scalar(select(File).where(File.path.endswith("Different Song.wav"))).id],
                               lib, tmp_path / "dupes")
    moved = duplicates.move_extras(s, keep.id, others, lib, tmp_path / "dupes")
    assert all(p.exists() for p in moved) and len(moved) == 2
    assert (tmp_path / "dupes" / "09082026" / "IN FURCHT - FLY LIK3.wav").exists()
    assert not (lib / "09082026" / "IN FURCHT - FLY LIK3.wav").exists()

    # The track is still owned, now by the kept file, and a rescan does not undo that.
    sync_library(s, lib)
    s.refresh(track)
    assert track.status is TrackStatus.owned
    links = s.scalars(select(MatchLink).where(MatchLink.track_id == track.id)).all()
    assert [ml.file_id for ml in links] == [keep.id]
    assert duplicates.saved(s)[0] == []  # the group is resolved

    # Fingerprints are only computed once.
    again = duplicates.find(s, lib, FPCALC, log=(log := []).append)
    assert again == [] and any("0 fingerprinted, 2 already done" in m for m in log)
