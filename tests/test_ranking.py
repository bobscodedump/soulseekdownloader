from pathlib import Path

from djlib.matcher import Wanted
from djlib.ranking import rank
from djlib.sockseek import SearchResult, Sockseek

FIX = Path(__file__).parent / "fixtures"


def R(user, path, length=300, bitrate=320, free=True, speed=1.0):
    return SearchResult(user, path, length, 1, bitrate, free, speed)


def test_prefers_extended_then_original_then_short_then_format():
    w = Wanted("SOLYD, Stoltenhoff", "Hot Beats - Short Mix", duration_s=241)
    results = [
        R("a", r"x\SOLYD - Hot Beats (Short Mix).flac", 241, None),
        R("b", r"x\Stoltenhoff, SOLYD - Hot Beats  Original Mix.mp3", 302),
        R("c", r"x\SOLYD - Hot Beats (Extended Mix).mp3", 360),
        R("d", r"x\SOLYD - Hot Beats (Original Mix).flac", 302, None),
    ]
    ok, _ = rank(w, results)
    assert [r.result.username for r in ok] == ["c", "d", "b", "a"]


def test_drops_wrong_songs_with_reasons():
    w = Wanted("KUSS, GRÜV", "Obsessed", duration_s=291)
    results = [
        R("audiobook", r"Books\Audiobooks\Patterson, James\James Patterson - Obsessed-Part 2 - 21 of 54 - Chapter 76.mp3", 288),
        R("good", r"music\KUSS & GRUV - Obsessed (Original Mix).flac", 291, None),
        R("cover", r"x\cover.jpg", None),
    ]
    ok, dropped = rank(w, results)
    assert [r.result.username for r in ok] == ["good"]
    assert dropped == {"no credited artist in path": 1}


def test_real_search_results_rank_sensibly():
    """Recorded results for 'Hot Beats' (wanted: Short Mix, 241 s) include other tracks from the same EP."""
    run = lambda args, timeout=None: (0, (FIX / "search_three.json").read_text(), "")
    from djlib.sockseek import Query
    res = Sockseek(run, FIX.parent / ".tmp").search([Query("a", "a")] * 3)
    ok, dropped = rank(Wanted("SOLYD, Stoltenhoff", "Hot Beats - Short Mix", duration_s=241), res[1])
    assert ok, dropped
    assert all("hot" in r.result.filename.lower() for r in ok)  # bass_till_i_come etc. are dropped
    assert dropped.get("title words not in filename", 0) > 0
