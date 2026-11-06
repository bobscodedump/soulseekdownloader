"""Text normalization shared by import, scanning, matching and verification."""
from __future__ import annotations

import re

from unidecode import unidecode

# Words that say nothing about which song a file is.
NOISE = {
    "original", "mix", "the", "a", "and", "feat", "ft", "featuring", "vs", "x",
    "web", "ptc", "flac", "mp3", "wav", "aiff", "m4a", "16bit", "24bit", "single", "ep",
}
# Words that mark a different version of the same song.
VERSION_WORDS = {"remix", "rework", "edit", "dub", "vip", "bootleg", "extended", "remaster", "remastered", "mixed", "instrumental", "acapella"}
_VERSION_TAIL = re.compile(r"\s+-\s+(?P<v>[^-]*\b(mix|remix|rework|edit|version|dub|mixed|vip|bootleg|remaster(ed)?)\b[^-]*)$", re.I)
_VERSION_PAREN = re.compile(r"\s*[(\[](?P<v>[^)\]]*\b(mix|remix|rework|edit|version|dub|vip|bootleg|remaster(ed)?)\b[^)\]]*)[)\]]\s*$", re.I)
_SITE = re.compile(r"\[[^\]]*www\.[^\]]*\]|www\.\S+|\S+\.(com|org|net)\b", re.I)
_HASH = re.compile(r"\b[0-9a-f]{8}\b")
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\bx\b|\bvs\.?\b|\bfeat\.?\b|\bft\.?\b|\band\b)\s*", re.I)


def fold(s: str) -> str:
    """ASCII-fold and lowercase: 'GRÜV Dārin' -> 'gruv darin'."""
    return unidecode(s or "").lower()


def words(s: str) -> list[str]:
    s = _HASH.sub(" ", _SITE.sub(" ", fold(s)))
    return re.findall(r"[a-z0-9]+", s)


def tokens(s: str) -> set[str]:
    """Meaningful words of a string, without noise words."""
    return set(words(s)) - NOISE


def split_version(title: str) -> tuple[str, str | None]:
    """'Join Me - Extended Schranz Rework' -> ('Join Me', 'Extended Schranz Rework').

    'Original Mix' counts as no version.
    """
    title = (title or "").strip()
    for rx in (_VERSION_TAIL, _VERSION_PAREN):
        m = rx.search(title)
        if m:
            base, version = title[: m.start()].strip(), m.group("v").strip()
            if re.fullmatch(r"original( mix)?", version, re.I):
                return base, None
            return base, version
    return title, None


def version_sig(text: str | None) -> set[str]:
    """The tokens that distinguish a version: 'Kobosil Hart Mix' -> {'kobosil', 'hart'}."""
    return tokens(text or "") - {"mix", "version"}


# Length variants of the same recording. The user prefers extended > original > short, but accepts all.
LENGTH_RANK = {"extended": 3, "original": 2, "short": 1}
_LENGTH_PHRASES = [
    ("extended", re.compile(r"\b(extended|club)(\s+(mix|edit|version|vocal))?\b|\bfull\s+length\b", re.I)),
    ("short", re.compile(r"\b(short|radio)(\s+(mix|edit|version))?\b", re.I)),
    ("original", re.compile(r"\boriginal(\s+(mix|version))?\b", re.I)),
]


def length_class(text: str | None) -> str | None:
    """'Extended Schranz Rework' -> 'extended'; 'Original Radio Edit' -> 'short'; 'Kobosil Hart Mix' -> None."""
    t = fold(text or "").replace("_", " ")
    for name, rx in _LENGTH_PHRASES:
        if rx.search(t):
            return name
    return None


def strip_length(text: str | None) -> str:
    """Remove length-variant phrases, leaving what identifies a version: 'Extended Schranz Rework' -> 'Schranz Rework'."""
    t = fold(text or "").replace("_", " ")
    for _, rx in _LENGTH_PHRASES:
        t = rx.sub(" ", t)
    return t


def identity_sig(version: str | None) -> set[str]:
    """Version tokens that make it a different recording (remixer, rework), ignoring length variants."""
    return version_sig(strip_length(version))


def split_artists(artist: str) -> list[str]:
    parts = [p.strip() for p in _ARTIST_SPLIT.split(artist or "") if p and p.strip()]
    return parts or [artist]


def norm_key(artist: str, title: str, version: str | None = None) -> str:
    """Stable key for exact-ish lookup: sorted artists | title tokens | version tokens."""
    arts = sorted(" ".join(sorted(tokens(a))) for a in split_artists(artist))
    return "|".join([",".join(arts), " ".join(sorted(tokens(title))), " ".join(sorted(version_sig(version)))])
