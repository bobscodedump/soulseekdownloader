"""Golden cases taken from real runs against this DJ library (Oct-Nov 2026)."""
import json
from pathlib import Path

import pytest

from djlib.matcher import MATCH, NO_MATCH, REVIEW, Candidate, Wanted, match, parse_duration, verify_download
from djlib.normalize import norm_key, split_artists, split_version

FIX = Path(__file__).parent / "fixtures"


def cand(path: str, duration=None) -> Candidate:
    p = Path(path)
    return Candidate(name=p.stem, context=str(p.parent), duration_s=duration)


@pytest.mark.parametrize("pair", json.loads((FIX / "tch_dupes.json").read_text()), ids=lambda p: p["title"])
def test_known_tch_duplicates_match(pair):
    r = match(Wanted(pair["artist"], pair["title"], duration_s=parse_duration(pair["duration"])), cand(pair["file"]))
    assert r.decision == MATCH, r


@pytest.mark.parametrize("artist,title,file", [
    ("Alarico", "Whisper", "20082026/careless whisper (south east players mix) (reg schranz edit) mstr.wav"),
    ("ANNĒ", "Kinesis", "08092026/01 - regent - kinesis.mp3"),
])
def test_same_title_different_song_is_not_a_match(artist, title, file):
    assert match(Wanted(artist, title), cand(file)).decision == NO_MATCH


def test_extended_counts_as_the_same_rework():
    """User rule: extended > original > short, all accepted. Only remix/rework identity must match."""
    w = Wanted("Lightforce, KSN", "Join Me - Schranz Rework", duration_s=198)
    c = Candidate(name="01. Join Me - Extended Schranz Rework", artist="Lightforce, KSN", duration_s=272)
    r = match(w, c)
    assert r.decision == MATCH, r


def test_short_mix_wanted_original_mix_found_is_accepted():
    w = Wanted("SOLYD, Stoltenhoff", "Hot Beats - Short Mix", duration_s=241)
    assert match(w, Candidate(name="Stoltenhoff, SOLYD - Hot Beats  Original Mix", duration_s=302)).decision == MATCH


def test_extended_file_shorter_than_wanted_is_suspicious():
    w = Wanted("Funkhaus", "Lumen", duration_s=360)
    assert match(w, Candidate(name="funkhaus - lumen (extended mix)", duration_s=200)).decision != MATCH


def test_remix_identity_still_strict():
    w = Wanted("KOBOSIL, Ueberrest", "Cryptic - Kobosil Hart Mix", duration_s=302)
    assert match(w, Candidate(name="ueberrest - cryptic (extended mix)", duration_s=400)).decision != MATCH


def test_remix_file_is_not_the_original():
    r = match(Wanted("ARMA", "No Good"), cand("shranz/Prodigy x ARMA - No Good (Remix).mp3"))
    assert r.decision == REVIEW


def test_original_mix_suffix_is_same_track():
    r = match(Wanted("Kai van Dongen", "Flash"), cand("08092026/Kai Van Dongen - Flash (Original Mix).mp3"))
    assert r.decision == MATCH


def test_duration_mismatch_blocks_auto_match():
    w = Wanted("Funkhaus", "Lumen", duration_s=360)
    assert match(w, cand("08092026/11-funkhaus-lumen.flac", duration=200)).decision != MATCH


# --- download verification: the five files from the M0 spike, with measured durations ---
SPIKE = [
    ("JXXXO", "Mkhitaryan", 316, r"@@music\Techno\202610\JXXXO-atom.13-(A13)-16BIT-WEB-FLAC-2026-PTC\02-jxxxo-mkhitaryan.flac", 316.1, True),
    ("Luciid", "Saw Ritual", 313, r"shared\Labels\B\Be Yourself Music\[2026-04-16] Luciid - REELUV\12 Luciid - Luciid - Saw Ritual [Be Yourself Music].m4a", 313.2, True),
    ("Alarico", "Whisper", 369, r"l:\flac archive\various artists (multi)\m\marvelettes\(2009) forever - complete motown albums volume 1\disc 1\10 - whisper.flac", 156.6, False),
    ("KUSS, GRÜV", "Obsessed", 291, r"Books\Audiobooks\Authors-P\Patterson, James\Patterson, James-Michael Bennett\Patterson, James-Michael Bennett 15-Obsessed\Part 2\James Patterson - Obsessed-Part 2 - 21 of 54 - Chapter 76.mp3", 288.5, False),
    ("Vilchezz", "Back In Town", 318, r"soulseek\sd_Unsorted\V\Voodooninjamunky\Y\Yuko Imai\Yuko Imai (1990.04.21) (album) Do Away [FLAC] {KICS-15}\06 - Snow Train ～Back In Town～.flac", 321.5, False),
    # Earlier wrong matches caught by hand
    ("Alarico", "Dawn", 0, r"www.electronicfresh.com\DJs Chart - November 2024\Sinisa Tamamovic Echoes Of Dawn Chart\Alarico - Carnal Fever (Original Mix).mp3", None, False),
    ("Disguised, FANK", "Pills and Promises", 0, r"NuagistTurtleize\202503\Disguised_x_FANK_x_LEON_SCHANZENBACH-Dissonance_Diaries-(DISG003)-16BIT-WEB-FLAC-2025-DASH\04-disguised_x_fank-lost_in_monochrome_(original_mix).flac", None, False),
    # Correct multi-artist file named differently from the credit
    ("Disguised, FANK", "The Arythmics", 325, r"narey\downloads\VA - BP - Top 100\FANK, Disguised, Vault Records - The Arythmics (Original Mix) [Vault Records (NL)].mp3", 325.0, True),
]


@pytest.mark.parametrize("artist,title,dur,remote,measured,ok", SPIKE, ids=[s[1] for s in SPIKE])
def test_verify_download(artist, title, dur, remote, measured, ok):
    r = verify_download(Wanted(artist, title, duration_s=dur or None), remote, measured)
    assert (r.decision == MATCH) is ok, r


def test_verify_requires_version_words():
    w = Wanted("KOBOSIL, Ueberrest", "Cryptic - Kobosil Hart Mix", duration_s=302)
    assert verify_download(w, r"x\01-ueberrest-cryptic__kobosil_hart_mix.mp3", 302).decision == MATCH
    assert verify_download(w, r"x\01-ueberrest-cryptic_(original_mix).mp3", 302).decision == REVIEW


@pytest.mark.parametrize("title,base,version", [
    ("Join Me - Extended Schranz Rework", "Join Me", "Extended Schranz Rework"),
    ("Let It Slide (Vlace Remix)", "Let It Slide", "Vlace Remix"),
    ("SKETCH #14 - Original Mix", "SKETCH #14", None),
    ("Bruce - All Right", "Bruce - All Right", None),
    ("Cirkle - Skyland - Mixed", "Cirkle - Skyland", "Mixed"),
])
def test_split_version(title, base, version):
    assert split_version(title) == (base, version)


def test_split_artists_and_norm_key():
    assert split_artists("Tarkno, Frenzy & Remco Beekwilder") == ["Tarkno", "Frenzy", "Remco Beekwilder"]
    assert norm_key("FANK, Disguised", "The Arythmics") == norm_key("Disguised, FANK", "The Arythmics")


def test_parse_duration():
    assert parse_duration("05:11") == 311 and parse_duration("1:04:35") == 3875 and parse_duration("") is None


def test_tagged_other_artist_never_matches_even_with_same_length():
    """Sciahri - Two Letters vs a Half Life & BCCO file whose length happened to match (326 vs 325 s)."""
    w = Wanted("Sciahri", "Two Letters", duration_s=326)
    c = Candidate(name="Half Life & Bcco Letters To God (Original Mix) 08_half_life_and_bcco_-_letters_to_god_(original_mix)",
                  context="05112026", duration_s=325.2, artist="Half Life & Bcco")
    assert match(w, c).decision == NO_MATCH


def test_tags_supply_the_artist_when_filename_lacks_it():
    """Alarico - Lost In Time: file '04 - Lost In Time.flac' is only identifiable by its tags."""
    w = Wanted("Alarico", "Lost In Time", duration_s=291)
    c = Candidate(name="Alarico Lost in Time 04 - Lost In Time", context="08092026", duration_s=291.67, artist="Alarico")
    assert match(w, c).decision == MATCH


def test_numbered_titles_must_match_their_number():
    w = Wanted("Tarkno, Remco Beekwilder", "SKETCH #14 - Original Mix", duration_s=330)
    assert match(w, cand("17102026/06.SKETCH #6 (Original Mix).mp3", duration=312)).decision == NO_MATCH
    w6 = Wanted("Tarkno, Remco Beekwilder", "SKETCH #6 - Original Mix")
    assert match(w6, Candidate(name="06.SKETCH #6 (Original Mix)", artist="Tarkno & Remco Beekwilder")).decision == MATCH


@pytest.mark.parametrize("text,cls", [
    ("Extended Schranz Rework", "extended"), ("Original Radio Edit", "short"), ("Short Mix", "short"),
    ("Original Mix", "original"), ("Kobosil Hart Mix", None), ("02-stoltenhoff_and_solyd-hot_beats_(extended_mix)", "extended"),
    ("Club Mix", "extended"), (None, None),
])
def test_length_class(text, cls):
    from djlib.normalize import length_class
    assert length_class(text) == cls
