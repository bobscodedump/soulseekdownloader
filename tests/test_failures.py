from djlib.db import Track, TrackStatus, session_factory
from djlib.failures import import_failures, parse_logs, status_for

LOG = """[cli] Starting CLI session in local mode
[013] SongJob: failed [All downloads failed]: DAMIANO (DE) - Selftalk
[036] SongJob: failed [No search results]: Chris Flannigan, West Code - Rumba Cuts
[002] Job List: failed [Child jobs failed]: tch
[133] SongJob: failed [All downloads failed]: Tarkno, Remco Beekwilder - SKETCH #14 - Original Mix
"""
LOG2 = "[010] SongJob: failed [No search results]: DAMIANO (DE) - Selftalk\n"
CSV = ("  #,Song,Artist,Duration\n"
       '11,"Selftalk","DAMIANO (DE)",06:07\n'
       '34,"Rumba Cuts","Chris Flannigan, West Code",05:00\n'
       '131,"SKETCH #14 - Original Mix","Tarkno, Remco Beekwilder",05:30\n'
       '99,"Never Logged","Nobody",04:00\n')


def test_parse_logs_keeps_last_reason_and_counts_runs(tmp_path):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    a.write_text(LOG)
    b.write_text(LOG2)
    last, runs = parse_logs([a, b])
    assert last["DAMIANO (DE) - Selftalk"] == "No search results" and runs["DAMIANO (DE) - Selftalk"] == 2
    assert "tch" not in last  # job-list summary lines are not tracks


def test_status_for():
    assert status_for("No search results") is TrackStatus.failed_no_results
    assert status_for("only wrong songs found") is TrackStatus.failed_no_results
    assert status_for("All downloads failed") is TrackStatus.failed_transfer


def test_import_failures_sets_reason_and_retry(tmp_path):
    log, csv = tmp_path / "a.log", tmp_path / "still.csv"
    log.write_text(LOG)
    csv.write_text(CSV)
    with session_factory(tmp_path / "t.db")() as s:
        counts = import_failures(s, csv, [log])
        by = {t.title: t for t in s.query(Track)}
        assert by["Selftalk"].status is TrackStatus.failed_transfer
        assert by["Rumba Cuts"].status is TrackStatus.failed_no_results
        assert by["SKETCH #14"].status_reason == "All downloads failed"  # matched with its "- Original Mix" log label
        assert by["Never Logged"].status_reason.startswith("unknown")
        assert all(t.next_retry_at for t in by.values())
        assert sum(counts.values()) == 4
