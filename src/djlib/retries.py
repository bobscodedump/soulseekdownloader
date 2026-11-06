"""Scheduled retries: failed tracks whose retry time has come are downloaded again into their playlist's crate."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Playlist, PlaylistItem, Track, TrackStatus, now

MAX_RETRIES = 5
FAILED = (TrackStatus.failed_no_results, TrackStatus.failed_transfer)


def _aware(dt: datetime | None) -> datetime | None:
    # SQLite returns naive datetimes; everything is stored in UTC.
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def crate_for_track(s: Session, t: Track) -> str | None:
    """The crate of the most recently imported playlist that wants this track and has one."""
    q = (select(Playlist.crate).join(PlaylistItem, PlaylistItem.playlist_id == Playlist.id)
         .where(PlaylistItem.track_id == t.id, Playlist.crate.isnot(None)).order_by(Playlist.imported_at.desc()))
    return s.scalars(q).first()


def due(s: Session, at: datetime | None = None) -> tuple[dict[str, list[Track]], list[Track], list[Track]]:
    """(crate -> due tracks, tracks moved to 'buy' after too many tries, due tracks with no crate)."""
    at = at or now()
    groups: dict[str, list[Track]] = defaultdict(list)
    gave_up, no_crate = [], []
    for t in s.scalars(select(Track).where(Track.status.in_(FAILED))):
        if (t.retry_count or 0) >= MAX_RETRIES:
            t.status, t.status_reason = TrackStatus.buy, f"gave up after {t.retry_count} tries: {t.status_reason}"
            gave_up.append(t)
            continue
        if _aware(t.next_retry_at) and _aware(t.next_retry_at) > at:
            continue
        crate = crate_for_track(s, t)
        if not crate:
            # The user's convention: downloads with no other home go into today's date folder.
            crate = at.astimezone().strftime("%d%m%Y")
            no_crate.append(t)
        groups[crate].append(t)
    s.commit()
    return dict(groups), gave_up, no_crate


def run_due(s: Session, download: Callable[[list[Track], str], object], at: datetime | None = None) -> dict:
    """Run one download job per crate for everything that is due. `download(tracks, crate)` does the work."""
    groups, gave_up, no_crate = due(s, at)
    for crate, tracks in groups.items():
        download(tracks, crate)
    return {"crates": {c: len(t) for c, t in groups.items()}, "gave_up": len(gave_up), "no_crate": len(no_crate)}
