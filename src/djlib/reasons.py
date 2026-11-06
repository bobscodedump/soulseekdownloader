"""Short, human reasons for failures. Full detail stays in download_attempt.error."""
from __future__ import annotations

import re

_RULES = [
    (re.compile(r"became stale after (\d+)ms", re.I), lambda m: f"stalled (no data for {int(m[1]) // 60000 or 1} min)"),
    (re.compile(r"Transfer rejected: (.+?)(?:$|\.)", re.I), lambda m: f"refused by peer: {m[1].strip()}"),
    (re.compile(r"appears to be offline", re.I), lambda m: "peer offline"),
    (re.compile(r"reported as failed by remote client", re.I), lambda m: "peer cancelled the transfer"),
    (re.compile(r"timed out", re.I), lambda m: "connection timed out"),
    (re.compile(r"^AllDownloadsFailed$"), lambda m: "transfer failed"),
]


def short(msg: str | None) -> str:
    if not msg:
        return "unknown"
    for rx, fmt in _RULES:
        m = rx.search(msg)
        if m:
            return fmt(m)
    return msg if len(msg) <= 90 else msg[:87] + "…"


_TRIED = re.compile(r"^(\d+) of (\d+) sources tried, all failed: (.*)$", re.S)


def summarize(reason: str | None) -> str:
    """Rewrite stored track reasons (including ones written before this module existed)."""
    if not reason:
        return "unknown"
    m = _TRIED.match(reason)
    if m:
        tried, found, last = int(m[1]), int(m[2]), m[3]
        if tried == 0 or last in ("None", ""):
            return f"{found} source{'s' if found != 1 else ''} found, skipped: their peers failed earlier in the same run"
        return f"{tried} of {found} sources tried; last: {short(last)}"
    return short(reason) if len(reason) > 90 or "stale" in reason else reason
