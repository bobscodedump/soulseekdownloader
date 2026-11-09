"""Adapter tests replay outputs recorded from real sockseek 3.0.6 runs (Nov 2026)."""
from pathlib import Path

import pytest

from djlib.sockseek import Query, SearchResult, Sockseek, parse_events, parse_json_arrays

FIX = Path(__file__).parent / "fixtures"


class Replay:
    def __init__(self, stdout="", stderr="", code=0):
        self.stdout, self.stderr, self.code, self.calls = stdout, stderr, code, []

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        return self.code, self.stdout, self.stderr


def test_search_returns_one_ordered_list_per_query(tmp_path):
    run = Replay((FIX / "search_three.json").read_text())
    qs = [Query("Nobody Zzqx", "Nonexistent Zzqx Track"), Query("SOLYD, Stoltenhoff", "Hot Beats"), Query("Sicion", "Dancing Shadows")]
    res = Sockseek(run, tmp_path).search(qs)
    assert [len(r) for r in res] == [0, 25, 27]
    top = res[2][0]
    assert top.username == "Grenoybel" and top.length_s == 431 and top.extension == "mp3"
    assert top.link.startswith("slsk://Grenoybel/Scene2/2026/05/Techno/MP3/")
    args = run.calls[0]
    assert "--concurrent-jobs" in args and args[args.index("--concurrent-jobs") + 1] == "1"
    assert "--length-tol=-1" in args  # ranking by length is ours, so extended versions are not filtered out


def test_search_refuses_misaligned_output(tmp_path):
    run = Replay((FIX / "search_three.json").read_text())
    with pytest.raises(RuntimeError, match="3 result lists for 2 queries"):
        Sockseek(run, tmp_path).search([Query("a", "b"), Query("c", "d")])


def test_parse_events_skips_plain_lines():
    ev = parse_events((FIX / "progress_csv_run.jsonl").read_text())
    kinds = {e["type"] for e in ev}
    assert {"track_list", "search_start", "download_start", "track_state", "download_progress"} <= kinds


def test_outcomes_map_success_by_user_and_remote_path():
    ev = parse_events((FIX / "progress_csv_run.jsonl").read_text())
    ok = SearchResult("Mochrie99", r"l:\flac archive\various artists (multi)\m\marvelettes\(2009) forever - complete motown albums volume 1\disc 1\10 - whisper.flac", 156, 1, None, True, 1.0)
    missing = SearchResult("nobody", r"x\y.mp3", 1, 1, None, True, 1.0)
    a, b = Sockseek.outcomes([ok, missing], ev)
    assert a.succeeded and a.local_path.endswith("10 - whisper.flac")
    assert not b.succeeded and b.failure == "no result reported"


def test_outcomes_report_stale_link_download():
    ev = parse_events((FIX / "progress_link_stale.jsonl").read_text())
    r = SearchResult("TheTailorWandersQuietly", r"@@music\Techno\202605\Sicion-Dancing_Shadows-(GRUVEP6)-16BIT-WEB-FLAC-2026-B7R\01-sicion-dancing_shadows_(original_mix).flac", 431, 1, None, True, 1.0)
    (o,) = Sockseek.outcomes([r], ev)
    assert not o.succeeded and o.failure == "AllDownloadsFailed"


def test_parse_json_arrays_handles_empty_lists():
    assert parse_json_arrays('[]\n[{"a":1}]\n[]') == [[], [{"a": 1}], []]


def test_failure_details_from_real_stderr():
    from djlib.sockseek import parse_failure_details
    stale = parse_failure_details((FIX / "stderr_link_stale.log").read_text())
    assert stale[("TheTailorWandersQuietly", "01-sicion-dancing_shadows_(original_mix).flac")].startswith("Download attempt became stale")
    quota = parse_failure_details(
        "[warn] [jobs] [15] SongJob: download attempt failed: Luigi Madonna, BCCO - Flowz: DJ-Promo\\..\\EDM 1609\\Techno\\Luigi Madonna, BCCO - Flowz (Original Mix) [www.dj-promo.org].mp3\n"
        "    Output: /x/y.mp3.incomplete\n    Attempt: 1/1\n    TransferRejectedException: Transfer rejected: Too many files\n")
    assert quota[("DJ-Promo", "Luigi Madonna, BCCO - Flowz (Original Mix) [www.dj-promo.org].mp3")] == "Transfer rejected: Too many files"


def test_timeout_keeps_finished_files_and_reports_the_rest():
    import pathlib
    import tempfile
    from djlib.sockseek import SearchResult, Sockseek, subprocess_runner
    d = pathlib.Path(tempfile.mkdtemp())
    exe = d / "slow"
    exe.write_text("#!/bin/sh\necho started\nsleep 30\n")
    exe.chmod(0o755)
    code, out, err = subprocess_runner(exe)([], timeout=1)
    assert code == 124 and out.strip() == "started"

    r = SearchResult("u", r"x\a.flac", 200, 1, None, True, 0.0)
    outs = Sockseek(lambda args, timeout=None: (124, "", ""), d).download([r], d / "out")
    assert not outs[0].succeeded and outs[0].failure == "timed out waiting for the peer"


def test_runner_streams_stdout_lines_while_running():
    import pathlib
    import tempfile
    import time
    from djlib.sockseek import Query, Sockseek, subprocess_runner
    d = pathlib.Path(tempfile.mkdtemp())
    exe = d / "fake"
    exe.write_text('#!/bin/sh\necho \'{"type":"track_state","data":{}}\'\nsleep 1\necho \'{"type":"track_state","data":{}}\'\necho "[]"\necho "[]"\n')
    exe.chmod(0o755)
    seen = []
    t0 = time.time()
    res = Sockseek(subprocess_runner(exe), d).search([Query("a", "x", None), Query("b", "y", None)],
                                                    lambda n: seen.append((n, time.time() - t0)))
    assert res == [[], []]
    assert [n for n, _ in seen] == [1, 2] and seen[0][1] < 0.8 < seen[1][1]  # first count arrived before the end
