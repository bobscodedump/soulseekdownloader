"""Read-only scan of the DJ folder into the file table. Never writes to the library."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Crate, CrateKind, File, now

AUDIO = {".mp3", ".flac", ".wav", ".aif", ".aiff", ".m4a", ".ogg", ".opus"}
_CHUNK = 1 << 20


def quick_hash(path: Path, size: int) -> str:
    """Size + first and last 1 MB: enough to recognise a moved file without reading 13 GB."""
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(_CHUNK))
        if size > 2 * _CHUNK:
            fh.seek(-_CHUNK, os.SEEK_END)
            h.update(fh.read(_CHUNK))
    return h.hexdigest()


_SCENE = re.compile(r"^\d{1,3}-(?P<artist>[^-]+)-(?P<title>.+?)(?:-[0-9a-f]{8})?$", re.I)  # 02-takt-zero_sum_game_(syep016)
_NUMBERED = re.compile(r"^\d{1,3}(?:\s*[-.]\s*|\s+)(?P<rest>.+)$")  # "01 - X", "82. X", "02 X"
_SITE = re.compile(r"\s*\[[^\]]*www\.[^\]]*\]|\s*-\s*www\.\S+$", re.I)
_UNDERSCORE_ALBUM = re.compile(r"^(?P<artist>[^_]+)_[^_]+_\d{1,3}_(?P<title>.+)$")  # Cirkle_Fading Trails_03_Machina


def parse_filename(stem: str) -> tuple[str | None, str]:
    """Best-effort (artist, title) from the naming patterns found in the library."""
    s = _SITE.sub("", stem).strip()
    m = _SCENE.match(s)
    if m and "_" in s:
        return m["artist"].replace("_", " ").strip(), m["title"].replace("_", " ").strip()
    m = _UNDERSCORE_ALBUM.match(s)
    if m:
        return m["artist"].strip(), m["title"].strip()
    m = _NUMBERED.match(s)
    if m:
        s = m["rest"]
    parts = [p.strip() for p in re.split(r"\s+[-—–]\s+", s) if p.strip()]
    if len(parts) >= 2:
        # "Artist - Title", or "Artist - Album - 04 Title": the title is the last part.
        title = re.sub(r"^\d{1,3}[.\s]+", "", parts[-1])
        return parts[0], title
    return None, s.replace("_", " ").strip()


def _tags(path: Path) -> dict:
    out = {"tag_artist": None, "tag_title": None, "duration_s": None, "bitrate": None, "sample_rate": None}
    try:
        m = mutagen.File(path, easy=True)
    except Exception:
        return out
    if m is None:  # not `if not m`: a mutagen file with no tags is falsy but still has info
        return out
    info = getattr(m, "info", None)
    if info:
        out["duration_s"] = round(getattr(info, "length", 0) or 0, 2) or None
        br = getattr(info, "bitrate", None)
        out["bitrate"] = int(br / 1000) if br else None
        out["sample_rate"] = getattr(info, "sample_rate", None)
    tags = m.tags or {}
    try:
        out["tag_artist"] = (tags.get("artist") or [None])[0]
        out["tag_title"] = (tags.get("title") or [None])[0]
    except (AttributeError, TypeError):
        pass
    return out


def crate_for(s: Session, root: Path, path: Path) -> Crate | None:
    rel = path.relative_to(root)
    if len(rel.parts) < 2:
        return None
    top = root / rel.parts[0]
    crate = s.scalar(select(Crate).where(Crate.path == str(top)))
    if not crate:
        name = rel.parts[0]
        kind = CrateKind.dated if re.fullmatch(r"\d{8}", name) else CrateKind.genre
        crate = Crate(name=name, path=str(top), kind=kind)
        s.add(crate)
        s.flush()
    return crate


@dataclass
class ScanStats:
    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    moved: int = 0
    removed: int = 0
    no_artist_title: list[str] = field(default_factory=list)


def scan(s: Session, root: Path | str) -> ScanStats:
    root = Path(root).resolve()
    st = ScanStats()
    known = {f.path: f for f in s.scalars(select(File).where(File.path.startswith(str(root) + os.sep)))}
    seen_paths: set[str] = set()
    pending_new: list[tuple[Path, os.stat_result]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if name.startswith(".") or p.suffix.lower() not in AUDIO:
                continue
            stat = p.stat()
            st.seen += 1
            seen_paths.add(str(p))
            f = known.get(str(p))
            if f and f.size == stat.st_size and f.mtime == stat.st_mtime:
                st.unchanged += 1
                f.seen_at = now()
                continue
            pending_new.append((p, stat))

    # Files that vanished: candidates for a move target, else removed.
    vanished = {f.sha1: f for path, f in known.items() if path not in seen_paths}
    for p, stat in pending_new:
        qh = quick_hash(p, stat.st_size)
        f = known.get(str(p))
        if f is None and qh in vanished:
            f = vanished.pop(qh)  # same content at a new path: a move
            f.path = str(p)
            st.moved += 1
        elif f is None:
            f = File(path=str(p), format=p.suffix.lower().lstrip("."), size=0, mtime=0, sha1=qh)
            s.add(f)
            st.added += 1
        else:
            st.updated += 1
        artist, title = parse_filename(p.stem)
        for k, v in {**_tags(p), "parsed_artist": artist, "parsed_title": title, "size": stat.st_size,
                     "mtime": stat.st_mtime, "sha1": qh, "format": p.suffix.lower().lstrip("."), "seen_at": now()}.items():
            setattr(f, k, v)
        f.crate = crate_for(s, root, p)

    for f in vanished.values():
        s.delete(f)
        st.removed += 1
    s.flush()

    for f in s.scalars(select(File).where(File.path.startswith(str(root) + os.sep))):
        if not ((f.tag_artist and f.tag_title) or (f.parsed_artist and f.parsed_title)):
            st.no_artist_title.append(f.path)
    s.commit()
    return st
