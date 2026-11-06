"""One matcher for dedupe, filing and download verification, so the rules never drift apart."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PureWindowsPath, PurePosixPath

from rapidfuzz import fuzz

from .normalize import VERSION_WORDS, fold, split_artists, split_version, tokens, version_sig, words

DURATION_TOL_S = 5.0
MATCH, REVIEW, NO_MATCH = "match", "review", "no_match"


@dataclass
class Wanted:
    artist: str
    title: str  # may still carry its version, e.g. "Cryptic - Kobosil Hart Mix"
    version: str | None = None
    duration_s: float | None = None

    def __post_init__(self):
        base, v = split_version(self.title)
        self.base_title = base
        if self.version is None:
            self.version = v


@dataclass
class Candidate:
    """A file to compare against: what its name/tags say, and where it sits."""
    name: str  # filename stem, or "artist - title" from tags
    context: str = ""  # parent folders / full remote path, searched for the artist
    duration_s: float | None = None
    artist: str | None = None  # an explicit artist (tags or parsed name), when the file states one


@dataclass
class Result:
    decision: str
    score: float
    title_score: float
    artist_score: float
    reasons: list[str] = field(default_factory=list)


def _title_score(base_title: str, name: str) -> float:
    t, n = tokens(base_title), tokens(name)
    if not t:
        return 0.0
    cover = len(t & n) / len(t)
    seq = fuzz.partial_ratio(" ".join(words(base_title)), " ".join(words(name))) / 100
    # partial_ratio alone is generous on short titles; only trust it when long enough.
    return max(cover, seq if len(base_title) >= 6 else 0.0)


def _artist_score(artist: str, haystack: str) -> float:
    h = tokens(haystack)
    best = 0.0
    for a in split_artists(artist):
        at = tokens(a)
        if at:
            best = max(best, len(at & h) / len(at))
    return best


def _version_ok(w: Wanted, name: str) -> tuple[bool, str | None]:
    want = version_sig(w.version)
    have_words = tokens(name)
    if want - have_words:
        return False, f"version words missing: {' '.join(sorted(want - have_words))}"
    extra = (have_words & VERSION_WORDS) - want - tokens(w.base_title) - tokens(w.artist)
    if extra:
        return False, f"file looks like another version: {' '.join(sorted(extra))}"
    return True, None


def match(w: Wanted, c: Candidate) -> Result:
    """Is this candidate file the wanted track? Used for dedupe against the library."""
    ts = _title_score(w.base_title, c.name)
    as_ = max(_artist_score(w.artist, c.name), _artist_score(w.artist, c.context), _artist_score(w.artist, c.artist or ""))
    score = 0.65 * ts + 0.35 * as_
    reasons = []
    vok, vwhy = _version_ok(w, c.name)
    if vwhy:
        reasons.append(vwhy)
    dur_ok = None
    if w.duration_s and c.duration_s:
        diff = abs(w.duration_s - c.duration_s)
        dur_ok = diff <= DURATION_TOL_S
        if not dur_ok:
            reasons.append(f"duration off by {diff:.0f}s")

    # The file names its own artist and none of ours is in it: a different song with a similar title.
    other_artist = bool(c.artist) and as_ < 0.5 and _artist_score(w.artist, c.artist) == 0
    # Numbers in a title name the song ("SKETCH #14" is not "SKETCH #6"): all must be present.
    wrong_number = bool({x for x in tokens(w.base_title) if x.isdigit()} - set(words(c.name)))
    if other_artist or wrong_number:
        decision = NO_MATCH
    elif ts >= 0.9 and as_ >= 0.5 and vok and dur_ok is not False:
        decision = MATCH
    elif ts >= 0.75 and (as_ >= 0.5 or dur_ok):
        decision = REVIEW
    elif ts >= 0.9 and len(tokens(w.base_title)) >= 2:
        # An exact multi-word title is rarely a coincidence, even when the file names no artist.
        decision = REVIEW
    else:
        decision = NO_MATCH
    if as_ < 0.5:
        reasons.append("artist not found")
    return Result(decision, round(score, 3), round(ts, 3), round(as_, 3), reasons)


def _path_parts(remote_path: str) -> tuple[str, str]:
    p = PureWindowsPath(remote_path) if "\\" in remote_path else PurePosixPath(remote_path)
    return p.stem, " ".join(p.parts[:-1])


def verify_download(w: Wanted, remote_path: str, measured_duration_s: float | None) -> Result:
    """Strict check before a downloaded file may be filed into a crate.

    Title words must be in the filename, a credited artist anywhere in the remote path,
    the version must match, and the measured length must be within 5 s.
    """
    stem, folders = _path_parts(remote_path)
    reasons = []
    want_title = tokens(w.base_title)
    title_ok = bool(want_title) and want_title <= tokens(stem)
    if not title_ok:
        reasons.append("title words not in filename")
    as_ = _artist_score(w.artist, stem + " " + folders)
    if as_ < 0.5:
        reasons.append("no credited artist in path")
    vok, vwhy = _version_ok(w, stem)
    if vwhy:
        reasons.append(vwhy)
    if w.duration_s and measured_duration_s is not None:
        diff = abs(w.duration_s - measured_duration_s)
        if diff > DURATION_TOL_S:
            reasons.append(f"duration off by {diff:.0f}s")
    elif w.duration_s:
        reasons.append("could not measure duration")
    ok = not reasons
    return Result(MATCH if ok else REVIEW, 1.0 if ok else 0.0, 1.0 if title_ok else 0.0, round(as_, 3), reasons)


def parse_duration(text: str | None) -> float | None:
    """'05:11' or '1:04:35' or '311' -> seconds."""
    if not text:
        return None
    try:
        parts = [float(p) for p in str(text).strip().split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total
