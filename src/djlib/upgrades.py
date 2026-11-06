"""Lossless upgrades: replace an MP3/M4A in the library with a FLAC/WAV/AIFF of the same recording.

The replacement must pass the normal download checks (title, artist, version) and also be the same
length as the file it replaces (within 5 s), so a different edit can never slip in. The old file is
moved to the replaced folder, never deleted, and its place in crates and playlists passes to the new one.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db import DownloadJob, File, MatchLink, Track, now
from .export import track_files
from .library import FileIndex, files_in, record_link
from .matcher import DURATION_TOL_S, MATCH, REVIEW, Result, Wanted, verify_download
from .orchestrator import (Report, TrackPlan, _unique, download_rounds, nicotine_running, preflight)
from .ranking import LOSSLESS, Ranked, rank
from .scanner import index_file
from .sockseek import Query, Sockseek

LOSSY = {"mp3", "m4a", "ogg", "opus"}
COPY_TAGS = ("artist", "title", "album", "genre", "date", "organization", "isrc", "bpm")
REJECT_NOTE = "upgrade check"  # rejected upgrade downloads stay parked but are not put in the review queue


@dataclass
class Candidate:
    file: File
    track: Track


@dataclass
class UpgradeReport:
    upgraded: list[tuple[Path, Path, Path]] = field(default_factory=list)  # old path, new path, where old went
    not_found: list[tuple[File, str]] = field(default_factory=list)
    plans: list[TrackPlan] = field(default_factory=list)
    rejected: int = 0


def candidates(s: Session, library_root: str | Path) -> list[Candidate]:
    """Lossy files that are a known track which has no lossless copy in the library yet."""
    root = str(Path(library_root).resolve())
    lossless = FileIndex([f for f in files_in(s, root) if f.format in LOSSLESS])
    out = []
    for tid, files in track_files(s).items():
        mine = [f for f in files if f.path.startswith(root)]
        if not mine or any(f.format in LOSSLESS for f in mine):
            continue
        lossy = next((f for f in mine if f.format in LOSSY and f.duration_s), None)
        if lossy is None:
            continue
        t = s.get(Track, tid)
        # A lossless copy may already be in the library without being linked (only one file gets linked).
        r, _ = lossless.best(t)
        if r.decision != MATCH:
            out.append(Candidate(lossy, t))
    out.sort(key=lambda c: (c.track.artist.lower(), c.track.title.lower()))
    return out


def same_recording(c: Candidate) -> Wanted:
    """What to look for: the track, at the length of the copy already owned (that fixes the edit)."""
    return Wanted(c.track.artist, c.track.title, version=c.track.version, duration_s=c.file.duration_s)


def check_upgrade(w: Wanted, owned_s: float, remote_path: str, measured: float | None) -> Result:
    r = verify_download(w, remote_path, measured)
    if measured is not None and abs(measured - owned_s) > DURATION_TOL_S:
        r.reasons = [x for x in r.reasons if not x.startswith("duration")] + \
                    [f"not the same edit: {measured - owned_s:+.0f}s against the file you have"]
        r.decision, r.score = REVIEW, 0.0
    return r


def plan_sources(w: Wanted, owned_s: float, results) -> tuple[list[Ranked], dict[str, int]]:
    ok, dropped = rank(w, results)
    keep = []
    for c in ok:
        r = c.result
        if r.extension not in LOSSLESS:
            dropped["not lossless"] = dropped.get("not lossless", 0) + 1
        elif r.length_s is not None and abs(r.length_s - owned_s) > DURATION_TOL_S:
            dropped["different length"] = dropped.get("different length", 0) + 1
        else:
            keep.append(c)
    return keep, dropped


def copy_tags(old: Path, new: Path) -> list[str]:
    """Give an untagged lossless download (WAVs usually are) the tags of the file it replaces.

    Written to a temporary copy first; the download is only swapped for it if it still reads back intact.
    """
    import os

    import mutagen
    from mutagen.aiff import AIFF
    from mutagen.id3 import TALB, TBPM, TCON, TDRC, TIT2, TPE1, TPUB, TSRC
    from mutagen.wave import WAVE
    frames = {"artist": TPE1, "title": TIT2, "album": TALB, "genre": TCON, "date": TDRC,
              "organization": TPUB, "isrc": TSRC, "bpm": TBPM}
    try:
        src = mutagen.File(old, easy=True)
        vals = {k: [str(x) for x in src.tags[k]] for k in COPY_TAGS if src and src.tags and k in src.tags}
        if not vals:
            return []
        before = mutagen.File(new)
        if before is None:
            return []
        tmp = new.with_name(new.name + ".tagging")
        shutil.copy2(new, tmp)
        if isinstance(before, (WAVE, AIFF)):
            dst = mutagen.File(tmp)
            if dst.tags is None:
                dst.add_tags()
            if dst.tags.getall("TPE1") or dst.tags.getall("TIT2"):
                tmp.unlink()
                return []
            for k, v in vals.items():
                dst.tags.add(frames[k](encoding=3, text=v))
        else:
            dst = mutagen.File(tmp, easy=True)
            if dst.tags is None:
                dst.add_tags()
            if dst.tags.get("artist") or dst.tags.get("title"):
                tmp.unlink()
                return []
            for k, v in list(vals.items()):
                try:
                    dst.tags[k] = v
                except (KeyError, ValueError):
                    vals.pop(k)
        dst.save()
        check = mutagen.File(tmp)
        if check is None or abs(check.info.length - before.info.length) > 0.1:
            tmp.unlink()
            return []
        os.replace(tmp, new)
        return sorted(vals)
    except Exception:  # tags are a nicety; never risk the download over them
        new.with_name(new.name + ".tagging").unlink(missing_ok=True)
        return []


def run_upgrades(s: Session, file_ids: list[int], library_root: Path, ss: Sockseek, rejected_dir: Path,
                 replaced_dir: Path, rounds: int = 3, dry_run: bool = False, log: Callable[[str], None] = print,
                 is_nicotine_running: Callable[[], bool] = nicotine_running) -> UpgradeReport:
    library_root, rejected_dir, replaced_dir = (Path(p).resolve() for p in (library_root, rejected_dir, replaced_dir))
    by_file = {c.file.id: c for c in candidates(s, library_root)}
    todo = [by_file[i] for i in file_ids if i in by_file]
    out = UpgradeReport()
    job = DownloadJob(state="preflight", flags=f"upgrade rounds={rounds} dry_run={dry_run}", started_at=now())
    s.add(job)
    s.commit()
    try:
        job.preflight_result = preflight(ss, is_nicotine_running)
    except Exception as e:
        job.preflight_result, job.state, job.finished_at = str(e), "aborted", now()
        s.commit()
        raise
    log(job.preflight_result)
    log(f"looking for lossless copies of {len(todo)} files")

    results = ss.search([Query(c.track.artist, c.track.title, None) for c in todo]) if todo else []
    plan_of: dict[int, Candidate] = {}
    for c, res in zip(todo, results):
        ok, dropped = plan_sources(same_recording(c), c.file.duration_s, res)
        p = TrackPlan(c.track, ok, raw_results=len(res), dropped=dropped)
        plan_of[id(p)] = c
        out.plans.append(p)
        best = ok[0].result.filename.split("\\")[-1] if ok else "-"
        log(f"  {c.track.artist} - {c.track.title} ({c.file.format}): {len(ok)} lossless of {len(res)} -> {best}")
    if dry_run:
        job.state, job.finished_at = "dry_run", now()
        s.commit()
        return out

    def check(p: TrackPlan, cand: Ranked, local: Path, measured: float | None) -> Result:
        c = plan_of[id(p)]
        return check_upgrade(same_recording(c), c.file.duration_s, cand.result.filename, measured)

    def place(p: TrackPlan, cand: Ranked, local: Path) -> Path:
        c = plan_of[id(p)]
        old = c.file
        old_path = Path(old.path)
        tagged = copy_tags(old_path, local)  # still in staging: the library is not touched if this fails
        dest = _unique(old_path.parent / local.name)
        shutil.move(str(local), dest)
        new = index_file(s, library_root, dest)
        new.track_id = old.track_id or c.track.id
        # Everything the old file stood for (it can own several tracks) now points at the new one.
        for ml in s.scalars(select(MatchLink).where(MatchLink.file_id == old.id)).all():
            record_link(s, new, s.get(Track, ml.track_id), ml.method, ml.score, ml.reason)
        record_link(s, new, c.track, "download", 1.0, f"lossless upgrade of {old_path.name} from {cand.result.username}")
        parked = _unique(replaced_dir / old_path.relative_to(library_root))
        parked.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_path), parked)
        s.execute(delete(MatchLink).where(MatchLink.file_id == old.id))
        s.delete(old)
        s.flush()
        out.upgraded.append((old_path, dest, parked))
        log(f"  UPGRADED {c.track.artist} - {c.track.title}: {old_path.name} -> {dest.name}"
            + (f" (tags copied: {', '.join(tagged)})" if tagged else ""))
        return dest

    rep = Report()
    download_rounds(s, job, out.plans, ss, rounds, rejected_dir, rep, log, check, place, reject_note=REJECT_NOTE)
    out.rejected = len(rep.rejected)
    for p in out.plans:
        if p.done:
            continue
        f = plan_of[id(p)].file
        if not p.candidates:
            why = "no lossless copy found" if p.raw_results else "no search results"
        else:
            why = f"{len(p.attempted)} of {len(p.candidates)} lossless copies tried, none worked: {p.last_failure}"
        f.upgrade_checked_at, f.upgrade_note = now(), why
        out.not_found.append((f, why))
    job.state, job.finished_at = "finished", now()
    s.commit()
    log(f"done: {len(out.upgraded)} upgraded, {len(out.not_found)} not upgraded")
    return out
