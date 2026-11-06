"""Same recording, several files: find them by fingerprint and move the extra copies out (never delete)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db import File, MatchLink, Setting, Track
from .fingerprint import all_files, fingerprint_library, groups, same_audio_pairs
from .library import record_link
from .orchestrator import _unique
from .ranking import LOSSLESS

KEY = "duplicate_groups"  # last result, so the list survives a restart without recomputing


def keep_score(f: File) -> tuple:
    """Which copy to keep: lossless, then bitrate, then one already linked to a track, then the oldest crate."""
    return (f.format in LOSSLESS, f.bitrate or 0, f.track_id is not None, -f.id)


def find(s: Session, root: str | Path, fpcalc: str, log: Callable[[str], None] = print) -> list[list[int]]:
    files = all_files(s, root)
    log(f"fingerprinting {len(files)} files (only new or changed ones are read)")
    st = fingerprint_library(s, files, fpcalc, progress=lambda i, n: log(f"  {i} of {n}"))
    log(f"{st.done} fingerprinted, {st.skipped} already done, {len(st.failed)} unreadable")
    for p in st.failed:
        log(f"  could not read {p}")
    pairs = same_audio_pairs(files)
    found = [[f.id for f in sorted(g, key=keep_score, reverse=True)] for g in groups(pairs)]
    sims = [[f.id, g.id, round(x, 3)] for f, g, x in pairs]
    s.merge(Setting(key=KEY, value=json.dumps({"groups": found, "sims": sims})))
    s.commit()
    log(f"{len(found)} groups of identical audio")
    return found


def saved(s: Session) -> tuple[list[list[File]], dict[frozenset, float]]:
    st = s.get(Setting, KEY)
    if not st:
        return [], {}
    data = json.loads(st.value)
    out = []
    for ids in data["groups"]:
        fs = [f for f in (s.get(File, i) for i in ids) if f and Path(f.path).exists()]
        if len(fs) > 1:  # a group resolved (or files moved by hand) disappears
            out.append(fs)
    return out, {frozenset(p[:2]): p[2] for p in data["sims"]}


def move_extras(s: Session, keep_id: int, move_ids: list[int], library_root: Path, dup_dir: Path) -> list[Path]:
    """Move the given copies out of the library; whatever they stood for passes to the kept file first."""
    keep = s.get(File, keep_id)
    groups_now, _ = saved(s)
    group = next((g for g in groups_now if keep in g), None)
    if group is None or not set(move_ids) <= {f.id for f in group} - {keep_id}:
        raise ValueError("those files are not copies of the one you are keeping")
    library_root, moved = Path(library_root).resolve(), []
    for fid in move_ids:
        f = s.get(File, fid)
        for ml in s.scalars(select(MatchLink).where(MatchLink.file_id == f.id)).all():
            record_link(s, keep, s.get(Track, ml.track_id), ml.method, ml.score,
                        f"same audio as {Path(f.path).name} (fingerprint); {ml.reason or ''}".strip("; "))
        if f.track_id:
            if keep.track_id is None:
                keep.track_id = f.track_id
            elif keep.track_id != f.track_id:
                record_link(s, keep, s.get(Track, f.track_id), "manual", 1.0, f"same audio as {Path(f.path).name} (fingerprint)")
        src = Path(f.path)
        dest = _unique(dup_dir / src.relative_to(library_root))
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dest)
        s.execute(delete(MatchLink).where(MatchLink.file_id == f.id))
        s.delete(f)
        moved.append(dest)
    s.commit()
    return moved
