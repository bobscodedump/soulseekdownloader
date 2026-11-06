"""Review queue: uncertain library matches and downloads that failed verification. Nothing is ever deleted."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import DownloadAttempt, File, MatchLink, Track, TrackStatus
from .library import PENDING_REVIEW, record_link
from .retries import crate_for_track
from .scanner import index_file


@dataclass
class ReviewItem:
    kind: str  # "match" (a library file that may be this track) | "rejected" (a download that failed checks)
    id: int
    track: Track
    path: str
    reason: str | None


def items(s: Session) -> list[ReviewItem]:
    out = []
    for ml, f, t in s.execute(select(MatchLink, File, Track).join(File, File.id == MatchLink.file_id)
                              .join(Track, Track.id == MatchLink.track_id).where(MatchLink.method == PENDING_REVIEW)):
        if t.status is not TrackStatus.owned:
            out.append(ReviewItem("match", ml.id, t, f.path, ml.reason))
    for a, t in s.execute(select(DownloadAttempt, Track).join(Track, Track.id == DownloadAttempt.track_id)
                          .where(DownloadAttempt.result == "rejected", DownloadAttempt.reviewed.is_(None))):
        if a.local_path and Path(a.local_path).exists():
            out.append(ReviewItem("rejected", a.id, t, a.local_path, a.error))
    return out


def accept_match(s: Session, link_id: int) -> Track:
    ml = s.get(MatchLink, link_id)
    f, t = s.get(File, ml.file_id), s.get(Track, ml.track_id)
    record_link(s, f, t, "manual", 1.0, "accepted in review")
    f.track_id, t.status, t.status_reason, t.next_retry_at = t.id, TrackStatus.owned, None, None
    s.commit()
    return t


def dismiss_match(s: Session, link_id: int) -> None:
    s.get(MatchLink, link_id).method = "dismissed"
    s.commit()


def accept_rejected(s: Session, attempt_id: int, library_root: Path, crate: str | None = None) -> Path:
    """The person says the parked file is right after all: file it into the track's crate."""
    a = s.get(DownloadAttempt, attempt_id)
    t = s.get(Track, a.track_id)
    crate = crate or crate_for_track(s, t)
    if not crate:
        raise ValueError("no crate known for this track; pass one")
    src = Path(a.local_path)
    dest_dir = Path(library_root) / crate
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    i = 2
    while dest.exists():
        dest = dest_dir / f"{src.stem} ({i}){src.suffix}"
        i += 1
    shutil.move(str(src), dest)
    f = index_file(s, library_root, dest)
    record_link(s, f, t, "manual", 1.0, "rejected download accepted in review")
    f.track_id, t.status, t.status_reason, t.next_retry_at = t.id, TrackStatus.owned, None, None
    a.reviewed, a.local_path = "accepted", str(dest)
    s.commit()
    return dest


def dismiss_rejected(s: Session, attempt_id: int) -> None:
    """Confirm it is the wrong file. It stays in the rejected folder; nothing is deleted."""
    s.get(DownloadAttempt, attempt_id).reviewed = "dismissed"
    s.commit()
