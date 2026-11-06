"""End-to-end orchestration against a fake sockseek that writes real (tiny) WAV files."""
import wave
from pathlib import Path

import pytest
from sqlalchemy import select

from djlib.db import DownloadAttempt, DownloadJob, Track, TrackStatus, session_factory
from djlib.ingest_csv import import_csv
from djlib.library import playlist_tracks
from djlib.orchestrator import PreflightError, run
from djlib.scanner import scan
from djlib.sockseek import DownloadOutcome, SearchResult


def wav(path: Path, seconds: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(1000)
        w.writeframes(b"\x80" * int(seconds * 1000))


def R(user, path, length, ext_bitrate=None, free=True):
    return SearchResult(user, path, length, 1, ext_bitrate, free, 1.0)


class FakeSockseek:
    """search() returns scripted results; download() 'downloads' by writing a WAV of the scripted length."""

    def __init__(self, workdir, results, transfers, test_results=500):
        self.workdir, self.results, self.transfers, self.test_results = Path(workdir), results, transfers, test_results
        self.searched, self.downloaded = [], []

    def test_search(self):
        return self.test_results

    def search(self, queries):
        self.searched += [f"{q.artist} - {q.title}" for q in queries]
        return [self.results.get(f"{q.artist} - {q.title}", []) for q in queries]

    def download(self, results, out_dir):
        out = []
        for r in results:
            self.downloaded.append((r.username, r.filename))
            spec = self.transfers.get((r.username, r.filename))
            if isinstance(spec, (int, float)):
                p = Path(out_dir) / r.filename.split("\\")[-1]
                wav(p, spec)
                out.append(DownloadOutcome(r.username, r.filename, True, str(p), None))
            else:
                out.append(DownloadOutcome(r.username, r.filename, False, None, spec or "AllDownloadsFailed"))
        return out


CSV = ("  #,Song,Artist,Duration\n"
       '1,"Lumen","Funkhaus",05:00\n'
       '2,"Obsessed","KUSS, GRÜV",04:51\n'
       '3,"Selva","HEMPTOTE",05:00\n'
       '4,"Hot Beats - Short Mix","SOLYD, Stoltenhoff",04:01\n'
       '5,"Flash","Kai van Dongen",04:00\n')


@pytest.fixture
def env(tmp_path):
    lib = tmp_path / "DJ"
    wav(lib / "08092026" / "Kai Van Dongen - Flash (Original Mix).wav", 240)  # already owned
    (tmp_path / "pl.csv").write_text(CSV, encoding="utf-8")
    S = session_factory(tmp_path / "t.db")
    s = S()
    scan(s, lib)
    pl, _ = import_csv(s, tmp_path / "pl.csv")
    return s, lib, playlist_tracks(s, pl.id), tmp_path


RESULTS = {
    "Funkhaus - Lumen": [
        R("queued", r"m\Funkhaus - Lumen (Original Mix).flac", 300),          # fails transfer
        R("good", r"m\11-funkhaus-lumen.mp3", 300, 320),                       # then succeeds
    ],
    "KUSS, GRÜV - Obsessed": [
        R("books", r"Books\Audiobooks\James Patterson - Obsessed-Part 2.mp3", 291),  # never downloaded
        R("liar", r"m\KUSS & GRUV - Obsessed.flac", 291),                      # reports 291 s, is really 120 s
    ],
    "HEMPTOTE - Selva": [],
    "SOLYD, Stoltenhoff - Hot Beats": [
        R("short", r"m\SOLYD - Hot Beats (Short Mix).flac", 241),
        R("ext", r"m\SOLYD - Hot Beats (Extended Mix).mp3", 362, 320),
    ],
}
TRANSFERS = {
    ("queued", r"m\Funkhaus - Lumen (Original Mix).flac"): "Transfer rejected: Too many megabytes",
    ("good", r"m\11-funkhaus-lumen.mp3"): 300,
    ("liar", r"m\KUSS & GRUV - Obsessed.flac"): 120,
    ("ext", r"m\SOLYD - Hot Beats (Extended Mix).mp3"): 362,
    ("short", r"m\SOLYD - Hot Beats (Short Mix).flac"): 241,
}


def test_full_run(env):
    s, lib, tracks, tmp = env
    fake = FakeSockseek(tmp / "work", RESULTS, TRANSFERS)
    rep = run(s, tracks, lib, "06112026", fake, tmp / "rejected", log=lambda m: None, is_nicotine_running=lambda: False)
    by = {t.title: t for t in s.scalars(select(Track))}

    # Owned track is never searched; versions are searched by base title.
    assert "Kai van Dongen - Flash" not in fake.searched
    assert "SOLYD, Stoltenhoff - Hot Beats" in fake.searched
    # The audiobook is never downloaded.
    assert not any(u == "books" for u, _ in fake.downloaded)
    # Lumen: first source refused (quota), second filed.
    assert by["Lumen"].status is TrackStatus.owned
    assert (lib / "06112026" / "11-funkhaus-lumen.mp3").exists()
    # Hot Beats: extended preferred over the short mix it was listed as.
    assert by["Hot Beats"].status is TrackStatus.owned
    assert (lib / "06112026" / "SOLYD - Hot Beats (Extended Mix).mp3").exists()
    assert ("short", r"m\SOLYD - Hot Beats (Short Mix).flac") not in fake.downloaded
    # Obsessed: the file lied about its length; measured 120 s -> rejected, never filed.
    assert by["Obsessed"].status is TrackStatus.failed_transfer
    assert (tmp / "rejected" / "KUSS & GRUV - Obsessed.flac").exists()
    rej = s.scalar(select(DownloadAttempt).where(DownloadAttempt.result == "rejected"))
    assert rej.local_path == str((tmp / "rejected" / "KUSS & GRUV - Obsessed.flac").resolve())
    assert not (lib / "06112026" / "KUSS & GRUV - Obsessed.flac").exists()
    assert "wrong file rejected" in by["Obsessed"].status_reason
    # Selva: nothing found.
    assert by["Selva"].status is TrackStatus.failed_no_results and by["Selva"].next_retry_at
    # Every attempt is recorded.
    attempts = s.scalars(select(DownloadAttempt)).all()
    assert sorted(a.result for a in attempts) == ["failed", "filed", "filed", "rejected"]
    assert len(rep.filed) == 2 and len(rep.rejected) == 1


def test_dry_run_changes_nothing(env):
    s, lib, tracks, tmp = env
    fake = FakeSockseek(tmp / "work", RESULTS, TRANSFERS)
    before = sorted(p.name for p in lib.rglob("*"))
    rep = run(s, tracks, lib, "06112026", fake, tmp / "rejected", dry_run=True, log=lambda m: None,
              is_nicotine_running=lambda: False)
    assert fake.downloaded == [] and sorted(p.name for p in lib.rglob("*")) == before
    plan = {p.track.title: [c.result.username for c in p.candidates] for p in rep.plans}
    assert plan["Hot Beats"] == ["ext", "short"] and plan["Obsessed"] == ["liar"]


def test_preflight_blocks_when_searches_return_nothing(env):
    s, lib, tracks, tmp = env
    fake = FakeSockseek(tmp / "work", RESULTS, TRANSFERS, test_results=0)
    with pytest.raises(PreflightError, match="blocking Soulseek"):
        run(s, tracks, lib, "06112026", fake, tmp / "rejected", log=lambda m: None, is_nicotine_running=lambda: False)
    assert fake.searched == [] and s.scalar(select(DownloadJob)).state == "aborted"


def test_preflight_blocks_when_nicotine_is_running(env):
    s, lib, tracks, tmp = env
    with pytest.raises(PreflightError, match="Nicotine"):
        run(s, tracks, lib, "x", FakeSockseek(tmp, {}, {}), tmp / "r", log=lambda m: None, is_nicotine_running=lambda: True)


def test_filing_never_overwrites(env):
    s, lib, tracks, tmp = env
    wav(lib / "06112026" / "11-funkhaus-lumen.mp3", 10)  # unrelated file with the same name
    fake = FakeSockseek(tmp / "work", RESULTS, TRANSFERS)
    run(s, tracks, lib, "06112026", fake, tmp / "rejected", log=lambda m: None, is_nicotine_running=lambda: False)
    assert (lib / "06112026" / "11-funkhaus-lumen (2).mp3").exists()


def test_one_slow_peer_cannot_sink_the_job(env):
    """Real failure from 6 Nov: one peer ranked first for 6 tracks and stalled on every one."""
    s, lib, tracks, tmp = env
    results, transfers = {}, {}
    for t in tracks:
        if t.title == "Flash":
            continue
        key = f"{t.artist} - {t.title}"
        slow = R("slowpeer", rf"m\{t.artist} - {t.title}.flac", t.duration_s)
        good = R(f"ok-{t.id}", rf"m\{t.artist} - {t.title}.mp3", t.duration_s, 320)
        results[key] = [slow, good]
        transfers[(slow.username, slow.filename)] = "Download attempt became stale after 120000ms"
        transfers[(good.username, good.filename)] = t.duration_s
    fake = FakeSockseek(tmp / "work", results, transfers)
    rep = run(s, tracks, lib, "06112026", fake, tmp / "rejected", rounds=2, log=lambda m: None,
              is_nicotine_running=lambda: False)
    slow_tries = [u for u, _ in fake.downloaded if u == "slowpeer"]
    # Round 1 runs in parallel before anyone has failed, so the cap (3) bounds the damage; after that, never again.
    assert len(slow_tries) == 3
    assert len(rep.filed) == 4  # everyone else succeeds within 2 rounds


def test_round_caps_picks_per_peer(env):
    s, lib, tracks, tmp = env
    results = {f"{t.artist} - {t.title}": [R("bigpeer", rf"m\{t.artist} - {t.title}.flac", t.duration_s)]
               for t in tracks if t.title != "Flash"}
    transfers = {(r[0].username, r[0].filename): None for r in results.values()}
    fake = FakeSockseek(tmp / "work", results, transfers)
    run(s, tracks, lib, "x", fake, tmp / "rejected", rounds=1, log=lambda m: None, is_nicotine_running=lambda: False)
    assert len(fake.downloaded) == 3  # MAX_PER_USER_PER_ROUND


def test_review_link_upgrades_to_owned_without_crash(env):
    """Regression (6 Nov gate run): a pending review link for the same file+track made the run crash."""
    from djlib.db import File, MatchLink
    s, lib, tracks, tmp = env
    flash = next(t for t in tracks if t.title == "Flash")
    f = s.scalar(select(File))
    s.add(MatchLink(file_id=f.id, track_id=flash.id, method="fuzzy_review", score=0.8))
    s.commit()
    run(s, tracks, lib, "x", FakeSockseek(tmp / "w", {}, {}), tmp / "r", dry_run=False, log=lambda m: None,
        is_nicotine_running=lambda: False)
    links = s.scalars(select(MatchLink).where(MatchLink.track_id == flash.id)).all()
    assert len(links) == 1 and links[0].method == "fuzzy" and flash.status is TrackStatus.owned
