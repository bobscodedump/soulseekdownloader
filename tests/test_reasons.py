from djlib.reasons import short, summarize

REAL = [  # stored reasons from the 6 Nov gate run
    ("1 of 2 sources tried, all failed: Download attempt became stale after 120000ms without peer transfer activity: redlofl\\@@aauys\\complete\\sometimesthe\\Alarico - SOLACE PT.1\\05 - Dawn.flac",
     "1 of 2 sources tried; last: stalled (no data for 2 min)"),
    ("0 of 1 sources tried, all failed: None", "1 source found, skipped: their peers failed earlier in the same run"),
    ("1 of 4 sources tried, all failed: User lovelypinksock appears to be offline", "1 of 4 sources tried; last: peer offline"),
    ("1 of 1 sources tried, all failed: Transfer rejected: Too many files", "1 of 1 sources tried; last: refused by peer: Too many files"),
    ("No search results", "No search results"),
]


def test_summarize_real_reasons():
    for raw, want in REAL:
        assert summarize(raw) == want


def test_short_never_returns_long_paths():
    assert len(short("x" * 300)) <= 90
