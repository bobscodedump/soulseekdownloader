"""Choose which search results to download, before anything is downloaded."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath

from .matcher import MATCH, Result, Wanted, verify_download
from .normalize import LENGTH_RANK, length_class
from .sockseek import SearchResult

LOSSLESS = {"flac", "wav", "aiff", "aif"}
AUDIO = LOSSLESS | {"mp3", "m4a", "ogg", "opus"}


@dataclass
class Ranked:
    result: SearchResult
    check: Result
    key: tuple


def format_score(r: SearchResult) -> int:
    if r.extension in LOSSLESS:
        return 3
    if r.extension in {"mp3", "m4a", "ogg", "opus"}:
        return 2 if (r.bitrate or 0) >= 320 else 1
    return 0


def preference_key(r: SearchResult) -> tuple:
    """Higher is better: version (extended > original > short), then format, then a free slot, then speed."""
    stem = PureWindowsPath(r.filename).stem
    return (LENGTH_RANK[length_class(stem) or "original"], format_score(r), r.free_slot, r.upload_speed)


def rank(w: Wanted, results: list[SearchResult]) -> tuple[list[Ranked], dict[str, int]]:
    """Acceptable candidates, best first, plus why the others were dropped (for the failure reason)."""
    seen, ok, dropped = set(), [], {}
    for r in results:
        if r.extension not in AUDIO or (r.username, r.filename) in seen:
            continue
        seen.add((r.username, r.filename))
        # Duration from the search result; files without one are still checked after download.
        check = verify_download(w, r.filename, r.length_s)
        if r.length_s is None:
            check.reasons = [x for x in check.reasons if x != "could not measure duration"]
            check.decision = MATCH if not check.reasons else check.decision
        if check.decision == MATCH:
            ok.append(Ranked(r, check, preference_key(r)))
        else:
            for why in check.reasons:
                dropped[why] = dropped.get(why, 0) + 1
    ok.sort(key=lambda x: x.key, reverse=True)
    return ok, dropped
