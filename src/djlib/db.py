"""SQLite schema and session helpers.

A track is a song a DJ wants or owns (artist + title + version); a file is a copy on disk.
"""
from __future__ import annotations

import enum
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

DEFAULT_DB = Path.home() / "Library" / "Application Support" / "djlib" / "library.db"


def now() -> datetime:
    return datetime.now(timezone.utc)


def note_failure(t: "Track", at: datetime | None = None) -> None:
    """Count a failure once per day: same-day retries mostly hit the same peer quotas."""
    at = at or now()
    last = t.last_failed_at.replace(tzinfo=timezone.utc) if t.last_failed_at and t.last_failed_at.tzinfo is None else t.last_failed_at
    if last is None or last.date() != at.date():
        t.retry_count = (t.retry_count or 0) + 1
    t.last_failed_at = at


class TrackStatus(str, enum.Enum):
    wanted = "wanted"
    queued = "queued"
    searching = "searching"
    downloading = "downloading"
    verifying = "verifying"
    owned = "owned"
    review = "review"
    failed_no_results = "failed_no_results"
    failed_transfer = "failed_transfer"
    buy = "buy"
    ignored = "ignored"


class CrateKind(str, enum.Enum):
    dated = "dated"
    genre = "genre"
    review = "review"
    other = "other"


class Base(DeclarativeBase):
    pass


class Track(Base):
    __tablename__ = "track"
    id: Mapped[int] = mapped_column(primary_key=True)
    artist: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    isrc: Mapped[str | None] = mapped_column(String, index=True)
    spotify_id: Mapped[str | None] = mapped_column(String, index=True)
    duration_s: Mapped[float | None] = mapped_column(Float)
    bpm: Mapped[float | None] = mapped_column(Float)
    camelot: Mapped[str | None] = mapped_column(String)
    musical_key: Mapped[str | None] = mapped_column(String)
    energy: Mapped[int | None] = mapped_column(Integer)
    genres: Mapped[str | None] = mapped_column(String)
    label: Mapped[str | None] = mapped_column(String)
    album: Mapped[str | None] = mapped_column(String)
    status: Mapped[TrackStatus] = mapped_column(Enum(TrackStatus), default=TrackStatus.wanted, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None]
    retry_count: Mapped[int] = mapped_column(Integer, default=0)  # days with a failed attempt, not runs
    last_failed_at: Mapped[datetime | None]
    norm_key: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(default=now)

    files: Mapped[list[File]] = relationship(back_populates="track")


class Crate(Base):
    __tablename__ = "crate"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[CrateKind] = mapped_column(Enum(CrateKind), default=CrateKind.other)


class File(Base):
    __tablename__ = "file"
    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("track.id"), index=True)
    crate_id: Mapped[int | None] = mapped_column(ForeignKey("crate.id"))
    path: Mapped[str] = mapped_column(String, unique=True)
    format: Mapped[str] = mapped_column(String)
    bitrate: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[float | None] = mapped_column(Float)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)
    sha1: Mapped[str] = mapped_column(String, index=True)
    tag_artist: Mapped[str | None] = mapped_column(String)
    tag_title: Mapped[str | None] = mapped_column(String)
    parsed_artist: Mapped[str | None] = mapped_column(String)
    parsed_title: Mapped[str | None] = mapped_column(String)
    seen_at: Mapped[datetime] = mapped_column(default=now)
    upgrade_checked_at: Mapped[datetime | None]  # last search for a lossless copy of this (lossy) file
    upgrade_note: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str | None] = mapped_column(Text)  # Chromaprint raw, first 120 s, base64 uint32
    fp_sha1: Mapped[str | None] = mapped_column(String)  # file hash the fingerprint was made from

    track: Mapped[Track | None] = relationship(back_populates="files")
    crate: Mapped[Crate | None] = relationship()


class Playlist(Base):
    __tablename__ = "playlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)  # "csv" | "spotify"
    source_ref: Mapped[str] = mapped_column(String, unique=True)
    imported_at: Mapped[datetime] = mapped_column(default=now)
    crate: Mapped[str | None] = mapped_column(String)  # where this playlist's downloads (and retries) are filed


class PlaylistItem(Base):
    __tablename__ = "playlist_item"
    __table_args__ = (UniqueConstraint("playlist_id", "track_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlist.id"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("track.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[str | None] = mapped_column(String)


class DownloadJob(Base):
    __tablename__ = "download_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int | None] = mapped_column(ForeignKey("playlist.id"))
    crate_id: Mapped[int | None] = mapped_column(ForeignKey("crate.id"))
    flags: Mapped[str] = mapped_column(Text, default="")
    state: Mapped[str] = mapped_column(String, default="created")
    preflight_result: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class DownloadAttempt(Base):
    __tablename__ = "download_attempt"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("download_job.id"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("track.id"), index=True)
    peer: Mapped[str | None] = mapped_column(String)
    remote_path: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    verify_score: Mapped[float | None] = mapped_column(Float)
    local_path: Mapped[str | None] = mapped_column(Text)  # where a rejected file was parked, for review
    reviewed: Mapped[str | None] = mapped_column(String)  # accepted | dismissed, once a person decided
    at: Mapped[datetime] = mapped_column(default=now)


class MatchLink(Base):
    __tablename__ = "match_link"
    __table_args__ = (UniqueConstraint("file_id", "track_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("file.id"), index=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("track.id"), index=True)
    method: Mapped[str] = mapped_column(String)  # isrc | spotify_id | fuzzy | manual
    score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "setting"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def db_path() -> Path:
    return Path(os.environ.get("DJLIB_DB", DEFAULT_DB))


def make_engine(path: Path | str | None = None):
    path = Path(path) if path else db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    _add_missing_columns(engine)
    return engine


def _add_missing_columns(engine) -> None:
    """Additive schema upgrades: new nullable columns are added to existing databases in place."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    if not col.nullable:
                        raise RuntimeError(f"cannot auto-add non-nullable column {table.name}.{col.name}")
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'))


def session_factory(path: Path | str | None = None) -> sessionmaker[Session]:
    return sessionmaker(make_engine(path), expire_on_commit=False)
