"""Import a playlist CSV (the exporter format used today, or common alternatives) into the track DB."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Playlist, PlaylistItem, Track
from .matcher import parse_duration
from .normalize import norm_key, split_version

# Accepted header names per field, compared case-insensitively after stripping.
COLUMNS = {
    "title": ["song", "title", "track name", "track", "name"],
    "artist": ["artist", "artists", "artist name(s)", "artist name"],
    "album": ["album", "album name"],
    "duration": ["duration", "length", "duration (ms)", "track duration (ms)"],
    "bpm": ["bpm", "tempo"],
    "camelot": ["camelot"],
    "key": ["key"],
    "energy": ["energy"],
    "genres": ["genres", "genre"],
    "label": ["label", "record label"],
    "isrc": ["isrc"],
    "spotify_id": ["spotify track id", "track id", "spotify id", "track uri"],
    "added_at": ["added at", "added"],
}


@dataclass
class ImportStats:
    rows: int = 0
    new_tracks: int = 0
    existing_tracks: int = 0
    items_added: int = 0
    skipped_rows: int = 0


def _colmap(headers: list[str]) -> dict[str, str]:
    lower = {h.strip().lower(): h for h in headers}
    out = {}
    for field, names in COLUMNS.items():
        for n in names:
            if n in lower:
                out[field] = lower[n]
                break
    if "title" not in out or "artist" not in out:
        raise ValueError(f"CSV needs a title and an artist column; found {headers}")
    return out


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _duration(v: str | None, header: str) -> float | None:
    if "ms" in header.lower():
        n = _num(v)
        return n / 1000 if n else None
    return parse_duration(v)


def _spotify_id(v: str | None) -> str | None:
    if not v:
        return None
    return v.rsplit(":", 1)[-1].rsplit("/", 1)[-1].split("?")[0] or None


def find_track(s: Session, isrc: str | None, spotify_id: str | None, key: str) -> Track | None:
    if isrc:
        t = s.scalar(select(Track).where(Track.isrc == isrc))
        if t:
            return t
    if spotify_id:
        t = s.scalar(select(Track).where(Track.spotify_id == spotify_id))
        if t:
            return t
    return s.scalar(select(Track).where(Track.norm_key == key))


def import_csv(s: Session, path: Path | str, name: str | None = None) -> tuple[Playlist, ImportStats]:
    path = Path(path).resolve()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, skipinitialspace=True)
        cols = _colmap(reader.fieldnames or [])
        rows = list(reader)

    def get(row, field):
        h = cols.get(field)
        v = row.get(h) if h else None
        return v.strip() if isinstance(v, str) else v

    energy = lambda row: int(_num(get(row, "energy"))) if _num(get(row, "energy")) is not None else None  # noqa: E731
    records = [
        TrackRecord(
            artist=get(row, "artist") or "", raw_title=get(row, "title") or "",
            isrc=(get(row, "isrc") or "").upper() or None, spotify_id=_spotify_id(get(row, "spotify_id")),
            album=get(row, "album"), duration_s=_duration(get(row, "duration"), cols.get("duration", "")),
            bpm=_num(get(row, "bpm")), camelot=get(row, "camelot"), musical_key=get(row, "key"),
            energy=energy(row), genres=get(row, "genres"), label=get(row, "label"), added_at=get(row, "added_at"),
        )
        for row in rows
    ]
    return save_playlist(s, "csv", str(path), name or path.stem.strip(), records)


@dataclass
class TrackRecord:
    """One playlist row, from any source (CSV exporter or Spotify)."""
    artist: str
    raw_title: str  # may carry the version: "Cryptic - Kobosil Hart Mix"
    isrc: str | None = None
    spotify_id: str | None = None
    album: str | None = None
    duration_s: float | None = None
    bpm: float | None = None
    camelot: str | None = None
    musical_key: str | None = None
    energy: int | None = None
    genres: str | None = None
    label: str | None = None
    added_at: str | None = None


def save_playlist(s: Session, source: str, source_ref: str, name: str, records: list[TrackRecord]) -> tuple[Playlist, ImportStats]:
    """Upsert tracks (by ISRC, Spotify ID, then normalized name) and the playlist's membership.

    Sources fill each other's gaps: Spotify gives ISRC and IDs, the CSV exporter gives BPM / key / energy.
    """
    stats = ImportStats()
    pl = s.scalar(select(Playlist).where(Playlist.source_ref == source_ref))
    if not pl:
        pl = Playlist(name=name, source=source, source_ref=source_ref)
        s.add(pl)
        s.flush()
    have = set(s.scalars(select(PlaylistItem.track_id).where(PlaylistItem.playlist_id == pl.id)))

    for pos, r in enumerate(records, 1):
        if not r.artist or not r.raw_title:
            stats.skipped_rows += 1
            continue
        stats.rows += 1
        base, version = split_version(r.raw_title)
        key = norm_key(r.artist, base, version)
        t = find_track(s, r.isrc, r.spotify_id, key)
        if t:
            stats.existing_tracks += 1
        else:
            t = Track(artist=r.artist, title=base, version=version, norm_key=key)
            s.add(t)
            stats.new_tracks += 1
        # Fill gaps only; never overwrite what is already known.
        for k in ("isrc", "spotify_id", "album", "duration_s", "bpm", "camelot", "musical_key", "energy", "genres", "label"):
            v = getattr(r, k)
            if v not in (None, "") and getattr(t, k) in (None, ""):
                setattr(t, k, v)
        s.flush()
        if t.id not in have:
            s.add(PlaylistItem(playlist_id=pl.id, track_id=t.id, position=pos, added_at=r.added_at))
            have.add(t.id)
            stats.items_added += 1
    s.commit()
    return pl, stats
