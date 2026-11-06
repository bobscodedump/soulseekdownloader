from datetime import timedelta

from sqlalchemy import select

from djlib.db import DownloadAttempt, File, MatchLink, Playlist, PlaylistItem, Track, TrackStatus, now, session_factory
from djlib.retries import MAX_RETRIES, due, run_due
from djlib import review
from djlib.scanner import scan


def _setup(tmp_path):
    s = session_factory(tmp_path / "t.db")()
    pl_a = Playlist(name="tch", source="csv", source_ref="/a.csv", crate="06112026")
    pl_b = Playlist(name="shranz", source="csv", source_ref="/b.csv", crate="shranz")
    s.add_all([pl_a, pl_b])
    s.flush()

    def track(title, status, retry_in_h, count=1, playlist=pl_a):
        t = Track(artist="A", title=title, norm_key=title, status=status, retry_count=count,
                  next_retry_at=now() + timedelta(hours=retry_in_h), status_reason="x")
        s.add(t)
        s.flush()
        s.add(PlaylistItem(playlist_id=playlist.id, track_id=t.id, position=1))
        return t

    return s, track, pl_b


def test_due_groups_by_crate_and_respects_retry_time(tmp_path):
    s, track, shranz = _setup(tmp_path)
    track("due-transfer", TrackStatus.failed_transfer, -1)
    track("due-shranz", TrackStatus.failed_no_results, -1, playlist=shranz)
    track("not-yet", TrackStatus.failed_no_results, +48)
    track("owned", TrackStatus.owned, -1)
    worn = track("worn-out", TrackStatus.failed_transfer, -1, count=MAX_RETRIES)
    groups, gave_up, no_crate = due(s)
    assert {c: [t.title for t in ts] for c, ts in groups.items()} == {"06112026": ["due-transfer"], "shranz": ["due-shranz"]}
    assert gave_up == [worn] and worn.status is TrackStatus.buy and no_crate == []


def test_run_due_calls_download_per_crate(tmp_path):
    s, track, shranz = _setup(tmp_path)
    track("a", TrackStatus.failed_transfer, -1)
    track("b", TrackStatus.failed_transfer, -1)
    calls = []
    out = run_due(s, lambda tracks, crate: calls.append((crate, sorted(t.title for t in tracks))))
    assert calls == [("06112026", ["a", "b"])] and out["crates"] == {"06112026": 2}


def test_review_accept_rejected_files_into_crate(tmp_path):
    s, track, _ = _setup(tmp_path)
    lib, rej = tmp_path / "DJ", tmp_path / "rejected"
    (lib / "06112026").mkdir(parents=True)
    rej.mkdir()
    parked = rej / "A - Lumen (Radio Edit).mp3"
    parked.write_bytes(b"x" * 100)
    t = track("Lumen", TrackStatus.failed_transfer, 24)
    s.add(DownloadAttempt(track_id=t.id, result="rejected", error="duration off by 90s", local_path=str(parked)))
    s.commit()
    (item,) = review.items(s)
    assert item.kind == "rejected" and item.reason == "duration off by 90s"
    dest = review.accept_rejected(s, item.id, lib)
    assert dest == lib / "06112026" / parked.name and dest.exists() and not parked.exists()
    assert t.status is TrackStatus.owned and review.items(s) == []


def test_review_dismiss_keeps_file(tmp_path):
    s, track, _ = _setup(tmp_path)
    parked = tmp_path / "wrong.mp3"
    parked.write_bytes(b"x")
    t = track("Whisper", TrackStatus.failed_transfer, 24)
    s.add(DownloadAttempt(track_id=t.id, result="rejected", error="no credited artist in path", local_path=str(parked)))
    s.commit()
    review.dismiss_rejected(s, review.items(s)[0].id)
    assert parked.exists() and review.items(s) == []


def test_review_accept_library_match(tmp_path):
    s, track, _ = _setup(tmp_path)
    lib = tmp_path / "DJ" / "shranz"
    lib.mkdir(parents=True)
    (lib / "01-version_34-expansion__b2_remix.mp3").write_bytes(b"x" * 50)
    scan(s, tmp_path / "DJ")
    t = track("Expansion", TrackStatus.wanted, 0)
    f = s.scalar(select(File))
    s.add(MatchLink(file_id=f.id, track_id=t.id, method="fuzzy_review", score=0.9, reason="file looks like another version: remix"))
    s.commit()
    (item,) = review.items(s)
    review.accept_match(s, item.id)
    assert t.status is TrackStatus.owned and f.track_id == t.id and review.items(s) == []


def test_track_without_crate_retries_into_todays_date_folder(tmp_path):
    s = session_factory(tmp_path / "t.db")()
    t = Track(artist="A", title="Orphan", norm_key="o", status=TrackStatus.failed_transfer, retry_count=1,
              next_retry_at=now() - timedelta(hours=1))
    s.add(t)
    s.commit()
    groups, _, no_crate = due(s)
    assert list(groups) == [now().astimezone().strftime("%d%m%Y")] and no_crate == [t]
