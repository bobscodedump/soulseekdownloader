"""Export the library for DJ software: one Rekordbox XML file plus M3U8 playlists.

Rekordbox's own database is encrypted and must not be written to; its supported route is
Preferences > Advanced > Database > rekordbox xml, which reads the file written here.
Every crate folder and every imported playlist (in playlist order) becomes a playlist.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Crate, File, MatchLink, Playlist, PlaylistItem, Track
from .library import OWNING_METHODS
from .ranking import LOSSLESS

# Camelot -> the key names Rekordbox writes in its own XML (Tonality attribute).
CAMELOT_TO_KEY = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "Dbm",
    "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}
KIND = {"mp3": "MP3 File", "flac": "FLAC File", "wav": "WAV File", "aiff": "AIFF File", "aif": "AIFF File",
        "m4a": "M4A File", "ogg": "OGG File", "opus": "OPUS File"}
XML_NAME = "rekordbox.xml"


@dataclass
class ExportResult:
    xml_path: Path
    m3u_dir: Path
    tracks: int = 0
    crates: dict[str, int] = field(default_factory=dict)
    playlists: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (exported, missing)


def location(path: str) -> str:
    """Rekordbox wants file://localhost/ followed by the percent-encoded absolute path."""
    return "file://localhost" + quote(path, safe="/")


def track_files(s: Session) -> dict[int, list[File]]:
    """Every file that is a given track, via File.track_id or a confirmed match link."""
    out: dict[int, dict[int, File]] = defaultdict(dict)
    for f in s.scalars(select(File).where(File.track_id.is_not(None))):
        out[f.track_id][f.id] = f
    for f, tid in s.execute(select(File, MatchLink.track_id).join(MatchLink, MatchLink.file_id == File.id)
                            .where(MatchLink.method.in_(OWNING_METHODS))):
        out[tid][f.id] = f
    return {tid: list(fs.values()) for tid, fs in out.items()}


def best_file(files: list[File]) -> File:
    return max(files, key=lambda f: (f.format in LOSSLESS, f.bitrate or 0, f.id))


def _meta(f: File, t: Track | None) -> dict[str, str]:
    p = Path(f.path)
    a: dict[str, str] = {
        "TrackID": str(f.id),
        "Name": (t.title if t else None) or f.tag_title or f.parsed_title or p.stem,
        "Artist": (t.artist if t else None) or f.tag_artist or f.parsed_artist or "",
        "Kind": KIND.get(f.format, f"{f.format.upper()} File"),
        "Size": str(f.size),
        "TotalTime": str(round(f.duration_s or 0)),
        "DateAdded": datetime.fromtimestamp(f.mtime).strftime("%Y-%m-%d"),
        "Location": location(f.path),
    }
    if f.bitrate:
        a["BitRate"] = str(f.bitrate)
    if f.sample_rate:
        a["SampleRate"] = str(f.sample_rate)
    if t:
        if t.bpm:
            a["AverageBpm"] = f"{t.bpm:.2f}"
        if t.camelot and t.camelot.upper() in CAMELOT_TO_KEY:
            a["Tonality"] = CAMELOT_TO_KEY[t.camelot.upper()]
        for attr, val in (("Mix", t.version), ("Album", t.album), ("Label", t.label), ("Genre", t.genres)):
            if val:
                a[attr] = val
    return a


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "playlist"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _m3u(entries: list[tuple[File, dict[str, str]]]) -> bytes:
    lines = ["#EXTM3U"]
    for f, m in entries:
        who = f"{m['Artist']} - {m['Name']}" if m["Artist"] else m["Name"]
        lines += [f"#EXTINF:{m['TotalTime']},{who}", f.path]
    return ("\n".join(lines) + "\n").encode("utf-8")


def export_library(s: Session, library_root: str | Path, out_dir: str | Path) -> ExportResult:
    root = str(Path(library_root).resolve())
    out_dir = Path(out_dir)
    res = ExportResult(xml_path=out_dir / XML_NAME, m3u_dir=out_dir / "m3u")

    by_track = track_files(s)
    owner: dict[int, Track] = {}  # file id -> the track it is (first confirmed one)
    for tid, fs in by_track.items():
        t = s.get(Track, tid)
        for f in fs:
            owner.setdefault(f.id, t)
    files = [f for f in s.scalars(select(File).order_by(File.id)) if f.path.startswith(root + os.sep)]
    meta = {f.id: _meta(f, owner.get(f.id)) for f in files}
    res.tracks = len(files)

    crates: dict[str, list[File]] = defaultdict(list)
    for f in files:
        if f.crate:
            crates[f.crate.name].append(f)
    playlists: list[tuple[str, list[File], int]] = []
    for pl in s.scalars(select(Playlist).order_by(Playlist.name)):
        items = s.scalars(select(PlaylistItem.track_id).where(PlaylistItem.playlist_id == pl.id)
                          .order_by(PlaylistItem.position)).all()
        got, seen = [], set()
        for tid in items:
            fs = [f for f in by_track.get(tid, []) if f.id in meta]
            if fs:
                f = best_file(fs)
                if f.id not in seen:  # two playlist rows can resolve to one file (e.g. two edits of a rework)
                    seen.add(f.id)
                    got.append(f)
        if got:
            playlists.append((pl.name, got, len(items) - len(got)))

    # Rekordbox XML
    doc = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(doc, "PRODUCT", Name="DJ Library", Version="1.1", Company="djlib")
    coll = ET.SubElement(doc, "COLLECTION", Entries=str(len(files)))
    for f in files:
        ET.SubElement(coll, "TRACK", meta[f.id])
    top = ET.SubElement(ET.SubElement(doc, "PLAYLISTS"), "NODE", Type="0", Name="ROOT", Count="1")
    lib = ET.SubElement(top, "NODE", Type="0", Name="DJ Library", Count="2")

    def folder(name: str, groups: list[tuple[str, list[File]]]) -> None:
        node = ET.SubElement(lib, "NODE", Type="0", Name=name, Count=str(len(groups)))
        for gname, fs in groups:
            pl = ET.SubElement(node, "NODE", Name=gname, Type="1", KeyType="0", Entries=str(len(fs)))
            for f in fs:
                ET.SubElement(pl, "TRACK", Key=str(f.id))

    crate_groups = sorted(crates.items(), key=lambda kv: _crate_sort(kv[0]))
    folder("Crates", crate_groups)
    folder("Playlists", [(n, fs) for n, fs, _ in playlists])
    ET.indent(doc)
    _write_atomic(res.xml_path, b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding="utf-8"))

    # M3U8, one file per crate and per playlist
    for sub, groups in (("Crates", crate_groups), ("Playlists", [(n, fs) for n, fs, _ in playlists])):
        d = res.m3u_dir / sub
        keep = set()
        for name, fs in groups:
            p = d / f"{_safe_name(name)}.m3u8"
            keep.add(p.name)
            _write_atomic(p, _m3u([(f, meta[f.id]) for f in fs]))
        for old in d.glob("*.m3u8"):  # crates or playlists that no longer exist
            if old.name not in keep:
                old.unlink()

    res.crates = {n: len(fs) for n, fs in crate_groups}
    res.playlists = {n: (len(fs), missing) for n, fs, missing in playlists}
    return res


def _crate_sort(name: str) -> tuple:
    """Dated crates (DDMMYYYY) newest first, then named crates alphabetically."""
    if re.fullmatch(r"\d{8}", name):
        return (0, -int(name[4:] + name[2:4] + name[:2]))
    return (1, name.lower())


def library_signature(s: Session) -> tuple:
    """Cheap fingerprint of what an export contains, to know when it is out of date."""
    return (s.scalar(select(func.count(File.id))), s.scalar(select(func.max(File.id))),
            s.scalar(select(func.sum(File.track_id))), s.scalar(select(func.count(MatchLink.id))),
            s.scalar(select(func.count(PlaylistItem.id))), s.scalar(select(func.count(Crate.id))))
