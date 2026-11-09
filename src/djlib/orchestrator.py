"""Download a playlist into a crate: pre-flight, dedupe, search, rank, download in rounds, verify, file."""
from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from .db import DownloadAttempt, DownloadJob, Track, TrackStatus, note_failure, now
from .failures import RETRY_AFTER
from .library import FileIndex, files_in, record_link, wanted
from .matcher import MATCH, Result, verify_download
from .ranking import Ranked, rank
from .reasons import summarize
from .scanner import index_file, measure_duration
from .sockseek import DownloadOutcome, Query, Sockseek

MAX_PER_USER_PER_ROUND = 3


def nicotine_running() -> bool:
    # The program itself (nicotine, Nicotine+.app), not any command line that merely mentions the word.
    pattern = r"(^|/)(nicotine|nicotine\+|nicotine-plus)( |$)|Nicotine\+\.app"
    return subprocess.run(["pgrep", "-if", pattern], capture_output=True).returncode == 0


class PreflightError(RuntimeError):
    pass


@dataclass
class TrackPlan:
    track: Track
    candidates: list[Ranked] = field(default_factory=list)
    attempted: set[int] = field(default_factory=set)
    raw_results: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    last_failure: str | None = None
    done: bool = False


@dataclass
class Report:
    owned_before: list[Track] = field(default_factory=list)
    filed: list[tuple[Track, Path]] = field(default_factory=list)
    rejected: list[tuple[Track, str, str]] = field(default_factory=list)  # track, remote path, reasons
    failed: list[tuple[Track, str]] = field(default_factory=list)
    plans: list[TrackPlan] = field(default_factory=list)


def _progress(log: Callable[[str], None], total: int) -> Callable[[int], None]:
    def on(n: int) -> None:
        log(f"searched {n} of {total}")
    return on


def preflight(ss: Sockseek, is_nicotine_running: Callable[[], bool] = nicotine_running) -> str:
    if is_nicotine_running():
        raise PreflightError("Nicotine+ is running and uses the same Soulseek login. Quit it and try again.")
    n = ss.test_search()
    if n == 0:
        raise PreflightError("A test search returned nothing: this network is probably blocking Soulseek peers.")
    return f"ok: test search returned {n} results"


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    i = 2
    while True:
        cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def run(
    s: Session,
    tracks: list[Track],
    library_root: Path,
    crate: str,
    ss: Sockseek,
    rejected_dir: Path,
    rounds: int = 3,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
    is_nicotine_running: Callable[[], bool] = nicotine_running,
) -> Report:
    library_root, rejected_dir = Path(library_root).resolve(), Path(rejected_dir).resolve()
    crate_dir = library_root / crate
    rep = Report()
    job = DownloadJob(state="preflight", flags=f"rounds={rounds} dry_run={dry_run}", started_at=now())
    s.add(job)
    s.commit()

    try:
        job.preflight_result = preflight(ss, is_nicotine_running)
    except PreflightError as e:
        job.preflight_result, job.state, job.finished_at = str(e), "aborted", now()
        s.commit()
        raise
    log(job.preflight_result)

    # 1. Skip anything already in the library.
    index = FileIndex(files_in(s, str(library_root)))
    todo = []
    for t in tracks:
        if t.status is TrackStatus.owned:
            rep.owned_before.append(t)
            continue
        r, f = index.best(t)
        if r.decision == MATCH and f:
            rep.owned_before.append(t)
            if not dry_run:
                f.track_id, t.status = t.id, TrackStatus.owned
                record_link(s, f, t, "fuzzy", r.score)
            continue
        todo.append(t)
    log(f"{len(rep.owned_before)} already owned, {len(todo)} to search")

    # 2. Search once per track (base title: sockseek needs every search word to appear), then rank.
    s.commit()  # never hold the database write lock across a minutes-long search: it 500s every other action
    results = ss.search([Query(t.artist, t.title, None) for t in todo], _progress(log, len(todo))) if todo else []
    plans = []
    for t, res in zip(todo, results):
        ok, dropped = rank(wanted(t), res)
        plans.append(TrackPlan(t, ok, raw_results=len(res), dropped=dropped))
    rep.plans = plans
    for p in plans:
        best = p.candidates[0].result.filename.split("\\")[-1] if p.candidates else "-"
        log(f"  {p.track.artist} - {p.track.title}: {len(p.candidates)} acceptable of {p.raw_results} -> {best}")
    if dry_run:
        job.state, job.finished_at = "dry_run", now()
        s.commit()
        return rep

    def check(p: TrackPlan, cand: Ranked, local: Path, measured: float | None):
        return verify_download(wanted(p.track), cand.result.filename, measured)

    def place(p: TrackPlan, cand: Ranked, local: Path) -> Path:
        t = p.track
        crate_dir.mkdir(parents=True, exist_ok=True)
        dest = _unique(crate_dir / local.name)
        shutil.move(str(local), dest)
        f = index_file(s, library_root, dest)
        f.track_id = t.id
        record_link(s, f, t, "download", 1.0, f"from {cand.result.username}")
        t.status, t.status_reason, t.next_retry_at = TrackStatus.owned, None, None
        log(f"  FILED {t.artist} - {t.title} -> {dest.relative_to(library_root)}")
        return dest

    download_rounds(s, job, plans, ss, rounds, rejected_dir, rep, log, check, place)

    for p in plans:
        if p.done:
            continue
        t = p.track
        if not p.candidates:
            t.status = TrackStatus.failed_no_results
            if p.raw_results == 0:
                t.status_reason = "No search results"
            else:
                top = ", ".join(f"{k} ({v})" for k, v in Counter(p.dropped).most_common(2))
                t.status_reason = f"no acceptable result among {p.raw_results}: {top}"
        else:
            t.status = TrackStatus.failed_transfer
            t.status_reason = summarize(f"{len(p.attempted)} of {len(p.candidates)} sources tried, all failed: {p.last_failure}")
        note_failure(t)
        t.next_retry_at = now() + RETRY_AFTER[t.status]
        rep.failed.append((t, t.status_reason))
    job.state, job.finished_at = "finished", now()
    s.commit()
    return rep


def download_rounds(s: Session, job: DownloadJob, plans: list[TrackPlan], ss: Sockseek, rounds: int,
                    rejected_dir: Path, rep: Report, log: Callable[[str], None],
                    check: Callable[[TrackPlan, Ranked, Path, float | None], Result],
                    place: Callable[[TrackPlan, Ranked, Path], Path], reject_note: str | None = None) -> None:
    """Download each plan's best untried source, verify it, and place it; failed tracks fall back to the next source.

    check() decides whether a downloaded file is the right one; place() moves it into the library.
    """
    staging = ss.workdir / "staging"
    bad_users: set[str] = set()
    for rnd in range(1, rounds + 1):
        batch: list[tuple[TrackPlan, Ranked]] = []
        per_user: Counter = Counter()
        for p in plans:
            if p.done:
                continue
            # Best untried source whose peer has not failed this job, spreading picks across peers.
            pick = next((i for i, c in enumerate(p.candidates)
                         if i not in p.attempted and c.result.username not in bad_users
                         and per_user[c.result.username] < MAX_PER_USER_PER_ROUND), None)
            if pick is None:
                continue
            per_user[p.candidates[pick].result.username] += 1
            p.attempted.add(pick)
            batch.append((p, p.candidates[pick]))
        if not batch:
            break
        log(f"round {rnd}: downloading {len(batch)} files")
        job.state = f"round {rnd}"
        s.commit()  # also releases the write lock before the network transfer
        outcomes = ss.download([c.result for _, c in batch], staging)
        for (p, cand), o in zip(batch, outcomes):
            _handle(s, job, p, cand, o, rejected_dir, rep, bad_users, log, check, place, reject_note)
        s.commit()


def _handle(s, job, p: TrackPlan, cand: Ranked, o: DownloadOutcome, rejected_dir: Path, rep: Report,
            bad_users: set[str], log, check, place, reject_note: str | None) -> None:
    t, r = p.track, cand.result
    attempt = DownloadAttempt(job_id=job.id, track_id=t.id, peer=r.username, remote_path=r.filename, result="failed")
    s.add(attempt)
    if not o.succeeded or not o.local_path or not Path(o.local_path).exists():
        attempt.error = o.failure
        p.last_failure = o.failure
        # One strike: a stalled or refusing peer costs minutes per try, so skip it for the rest of the job.
        bad_users.add(r.username)
        return
    local = Path(o.local_path)
    result = check(p, cand, local, measure_duration(local))
    attempt.verify_score = result.score
    if result.decision != MATCH:
        rejected_dir.mkdir(parents=True, exist_ok=True)
        parked = _unique(rejected_dir / local.name)
        shutil.move(str(local), parked)
        attempt.result, attempt.error, attempt.local_path = "rejected", "; ".join(result.reasons), str(parked)
        attempt.reviewed = reject_note  # None: shown in the review queue
        p.last_failure = f"wrong file rejected ({attempt.error})"
        rep.rejected.append((t, r.filename, attempt.error))
        log(f"  REJECTED {t.artist} - {t.title}: {local.name} ({attempt.error})")
        return
    dest = place(p, cand, local)
    attempt.result, attempt.local_path = "filed", str(dest)
    p.done = True
    rep.filed.append((t, dest))
