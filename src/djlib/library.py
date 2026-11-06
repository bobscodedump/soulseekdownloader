"""Link wanted tracks to files already in the library (dedupe)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import File, MatchLink, PlaylistItem, Track, TrackStatus
from .matcher import MATCH, NO_MATCH, REVIEW, Candidate, Result, Wanted, match
from .normalize import tokens

AUTO, PENDING_REVIEW = "fuzzy", "fuzzy_review"


def candidate(f: File) -> Candidate:
    p = Path(f.path)
    name = " ".join(x for x in (f.tag_artist, f.tag_title, p.stem) if x)
    context = " ".join(x for x in (p.parent.name, f.parsed_artist) if x)
    return Candidate(name=name, context=context, duration_s=f.duration_s, artist=f.tag_artist or f.parsed_artist)


def wanted(t: Track) -> Wanted:
    return Wanted(t.artist, t.title, version=t.version, duration_s=t.duration_s)


class FileIndex:
    """Inverted index so each track is only compared with files sharing a title word."""

    def __init__(self, files: list[File]):
        self.files = files
        self.cands = {f.id: candidate(f) for f in files}
        self.by_token: dict[str, list[File]] = defaultdict(list)
        for f in files:
            for tok in tokens(self.cands[f.id].name):
                self.by_token[tok].append(f)

    def best(self, t: Track, exclude_crates: set[str] = frozenset()) -> tuple[Result, File | None]:
        w = wanted(t)
        pool = {f.id: f for tok in tokens(w.base_title) for f in self.by_token.get(tok, [])}
        best: tuple[Result, File | None] = (Result(NO_MATCH, 0, 0, 0), None)
        rank = {MATCH: 2, REVIEW: 1, NO_MATCH: 0}
        for f in pool.values():
            if f.crate and f.crate.name in exclude_crates:
                continue
            r = match(w, self.cands[f.id])
            if (rank[r.decision], r.score) > (rank[best[0].decision], best[0].score):
                best = (r, f)
        return best


def files_in(s: Session, root: str | None = None) -> list[File]:
    q = select(File)
    if root:
        q = q.where(File.path.startswith(str(Path(root).resolve())))
    return list(s.scalars(q))


@dataclass
class DupeRow:
    track: Track
    result: Result
    file: File | None


def dedupe(s: Session, tracks: list[Track], index: FileIndex, exclude_crates: set[str] = frozenset()) -> list[DupeRow]:
    return [DupeRow(t, *index.best(t, exclude_crates)) for t in tracks]


def record_link(s: Session, f: File, t: Track, method: str, score: float, reason: str | None = None) -> MatchLink:
    """Insert or update the (file, track) link; a review link can later become an automatic one."""
    ml = s.scalar(select(MatchLink).where(MatchLink.file_id == f.id, MatchLink.track_id == t.id))
    if ml is None:
        ml = MatchLink(file_id=f.id, track_id=t.id, method=method, score=score)
        s.add(ml)
    ml.method, ml.score, ml.reason = method, score, reason
    return ml


def link(s: Session, rows: list[DupeRow]) -> dict[str, int]:
    """Record matches: auto matches mark the track owned; review matches wait for a person."""
    counts = {"owned": 0, "review": 0}
    for r in rows:
        if not r.file or r.result.decision == NO_MATCH:
            continue
        method = AUTO if r.result.decision == MATCH else PENDING_REVIEW
        record_link(s, r.file, r.track, method, r.result.score, "; ".join(r.result.reasons) or None)
        if method == AUTO:
            r.file.track_id = r.track.id
            r.track.status = TrackStatus.owned
            counts["owned"] += 1
        else:
            counts["review"] += 1
    s.commit()
    return counts


def playlist_tracks(s: Session, playlist_id: int) -> list[Track]:
    q = (select(Track).join(PlaylistItem, PlaylistItem.track_id == Track.id)
         .where(PlaylistItem.playlist_id == playlist_id).order_by(PlaylistItem.position))
    return list(s.scalars(q))
