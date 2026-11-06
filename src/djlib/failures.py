"""Bring past failures (still-failing CSVs + sockseek logs) into the track database."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from .db import Track, TrackStatus, now
from .ingest_csv import import_csv
from .library import playlist_tracks

RETRY_AFTER = {TrackStatus.failed_transfer: timedelta(hours=24), TrackStatus.failed_no_results: timedelta(days=7)}
_FAILED = re.compile(r"SongJob: failed \[(?P<reason>[^\]]+)\]: (?P<what>.+?)\s*$")


def status_for(reason: str) -> TrackStatus:
    r = reason.lower()
    if "no search results" in r or "nosearchresults" in r or "wrong" in r:
        return TrackStatus.failed_no_results
    return TrackStatus.failed_transfer


def parse_logs(paths: list[Path]) -> tuple[dict[str, str], Counter, dict[str, datetime]]:
    """'Artist - Title' -> last failure reason, days it failed on (by log date), and the last such date."""
    last, days, latest = {}, {}, {}
    for p in sorted(paths, key=lambda p: Path(p).stat().st_mtime):
        when = datetime.fromtimestamp(Path(p).stat().st_mtime, timezone.utc)
        for line in Path(p).read_text(errors="replace").splitlines():
            m = _FAILED.search(line)
            if m and m["reason"] != "Child jobs failed":
                last[m["what"]] = m["reason"]
                days.setdefault(m["what"], set()).add(when.date())
                latest[m["what"]] = when
    return last, Counter({k: len(v) for k, v in days.items()}), latest


def import_failures(s: Session, csv_path: Path, logs: list[Path], overrides: dict[str, str] | None = None) -> Counter:
    """Mark every track in a still-failing CSV as failed, with its reason and next retry time."""
    reasons, runs, latest = parse_logs(logs)
    overrides = overrides or {}
    pl, _ = import_csv(s, csv_path)
    out = Counter()
    for t in playlist_tracks(s, pl.id):
        if t.status is TrackStatus.owned:
            out["skipped_owned"] += 1
            continue
        label = f"{t.artist} - {t.title}" + (f" - {t.version}" if t.version else "")
        candidates = [label, f"{t.artist} - {t.title}"] + [k for k in reasons if k.startswith(f"{t.artist} - {t.title}")]
        reason = overrides.get(f"{t.artist} - {t.title}") or next((reasons[k] for k in candidates if k in reasons), None)
        status = status_for(reason or "")
        t.status = status
        t.status_reason = reason or "unknown (no log line found)"
        t.retry_count = max(t.retry_count or 0, next((runs[k] for k in candidates if k in runs), 0))
        t.last_failed_at = next((latest[k] for k in candidates if k in latest), t.last_failed_at)
        t.next_retry_at = now() + RETRY_AFTER[status]
        out[status.value] += 1
    s.commit()
    return out
