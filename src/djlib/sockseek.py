"""Adapter over the sockseek CLI (pinned 3.0.6).

Two phases, so the app ranks and verifies candidates itself before anything is downloaded:
  search():   one ordered `--print json-all` run over a CSV -> every result per track
  download(): one `--input-type=list --progress-json` run over exact slsk:// links
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import threading
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

SOCKSEEK = Path.home() / "Desktop" / "soulseek" / "sockseek" / "sockseek"
TIMED_OUT = 124
TEST_QUERY = "Daft Punk - Around the World"


@dataclass
class SearchResult:
    username: str
    filename: str  # remote path, backslash-separated
    length_s: float | None
    size: int | None
    bitrate: int | None
    free_slot: bool
    upload_speed: float

    @property
    def link(self) -> str:
        return f"slsk://{self.username}/{self.filename.replace(chr(92), '/')}"

    @property
    def extension(self) -> str:
        return Path(self.filename.replace("\\", "/")).suffix.lower().lstrip(".")


@dataclass
class DownloadOutcome:
    username: str
    filename: str
    succeeded: bool
    local_path: str | None
    failure: str | None  # e.g. AllDownloadsFailed, stale, rejected reason


@dataclass
class Query:
    artist: str
    title: str  # base title, without version words: sockseek requires every search word
    album: str | None = None


class Runner(Protocol):
    def __call__(self, args: list[str], timeout: float | None = None) -> tuple[int, str, str]: ...


_RUNNING: set[subprocess.Popen] = set()
_CANCELLED = threading.Event()


def cancel_running() -> int:
    """Stop every sockseek process started by this app (the Stop button). Returns how many were stopped."""
    _CANCELLED.set()
    procs = list(_RUNNING)
    for p in procs:
        p.kill()
    return len(procs)


def subprocess_runner(binary: Path = SOCKSEEK) -> Runner:
    def run(args, timeout=None):
        _CANCELLED.clear()
        p = subprocess.Popen([str(binary), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _RUNNING.add(p)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:  # sockseek is killed; keep what it printed so finished files still count
            p.kill()
            out, err = p.communicate()
            return TIMED_OUT, out or "", err or ""
        finally:
            _RUNNING.discard(p)
        return p.returncode, out, err
    return run


def parse_json_arrays(text: str) -> list[list[dict]]:
    """sockseek prints one JSON array per search job, interleaved with nothing else on stdout."""
    dec, pos, out = json.JSONDecoder(), 0, []
    while True:
        i = text.find("[", pos)
        if i < 0:
            return out
        obj, pos = dec.raw_decode(text, i)
        out.append(obj)


def parse_events(text: str) -> list[dict]:
    """--progress-json lines; stdout also carries plain lines (the track list), which are skipped."""
    out = []
    for line in text.splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


_ATTEMPT = re.compile(r"(?:download attempt failed|failed \[[^\]]+\]): .*?: (?P<user>[^\\:]+)\\(?P<path>.+)$")
_DETAIL = re.compile(r"^\s+(?:\w*Exception|Error): (?P<msg>.+)$")


def _basename(remote: str) -> str:
    return remote.replace("/", "\\").rsplit("\\", 1)[-1]


def parse_failure_details(stderr: str) -> dict[tuple[str, str], str]:
    """(user, file name) -> first error message, from sockseek's stderr.

    Paths in those lines are shortened ("user\\..\\folder\\file"), so the file name is the key.
    """
    out, current = {}, None
    for line in stderr.splitlines():
        m = _ATTEMPT.search(line)
        if m:
            current = (m["user"].strip(), _basename(m["path"].strip()))
            continue
        d = _DETAIL.match(line)
        if d and current and current not in out and not d["msg"].startswith(("One or more child", "Some child")):
            out[current] = d["msg"].strip()
        if d and "stale" in d["msg"]:  # stale lines carry the full user\path themselves
            m2 = re.search(r"activity: (?P<user>[^\\]+)\\(?P<path>.+)$", d["msg"])
            if m2:
                out.setdefault((m2["user"], _basename(m2["path"])), d["msg"].split(":")[0])
    return out


class Sockseek:
    def __init__(self, runner: Runner | None = None, workdir: Path | None = None):
        self.run = runner or subprocess_runner()
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="djlib-"))
        self.workdir.mkdir(parents=True, exist_ok=True)

    # -- pre-flight -------------------------------------------------------
    def test_search(self) -> int:
        """Number of results for a query that always has hundreds; 0 means searches are blocked."""
        code, out, err = self.run([TEST_QUERY, "--print", "json-all", "--length-tol=-1"], timeout=180)
        arrays = parse_json_arrays(out)
        return len(arrays[0]) if arrays else 0

    # -- search -----------------------------------------------------------
    def search(self, queries: list[Query], on_progress: Callable[[int], None] | None = None) -> list[list[SearchResult]]:
        path = self.workdir / "search.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Artist", "Title", "Album"])
            for q in queries:
                w.writerow([q.artist, q.title, q.album or ""])
        # --concurrent-jobs 1 keeps one result array per query, in input order.
        code, out, err = self.run([
            str(path), "--input-type=csv", "--artist-col", "Artist", "--title-col", "Title", "--album-col", "Album",
            "--length-tol=-1", "--concurrent-jobs", "1", "--search-timeout", "15000", "--print", "json-all",
        ], timeout=60 * 60)
        self._keep_log("search", out, err)
        arrays = parse_json_arrays(out)
        if len(arrays) != len(queries):
            raise RuntimeError(f"sockseek returned {len(arrays)} result lists for {len(queries)} queries: {err[-500:]}")
        return [[self._result(r) for r in arr] for arr in arrays]

    @staticmethod
    def _result(r: dict) -> SearchResult:
        u, f = r.get("User", {}), r.get("File", {})
        return SearchResult(
            username=u.get("Username", ""), filename=f.get("Filename", ""), length_s=f.get("Length"),
            size=f.get("Size"), bitrate=f.get("Bitrate"), free_slot=bool(u.get("HasFreeUploadSlot")),
            upload_speed=float(u.get("UploadSpeed") or 0),
        )

    # -- download ---------------------------------------------------------
    def download(self, results: list[SearchResult], out_dir: Path, stale_ms: int = 120_000) -> list[DownloadOutcome]:
        out_dir.mkdir(parents=True, exist_ok=True)
        links = self.workdir / "links.txt"
        links.write_text("".join(f'"{r.link}"\n' for r in results), encoding="utf-8")
        code, out, err = self.run([
            str(links), "--input-type=list", "--progress-json", "--max-stale-time", str(stale_ms),
            "-o", str(out_dir), "--no-write-index",
        ], timeout=min(2 * 60 * 60, 10 * 60 + 4 * 60 * len(results)))  # a peer that never answers must not block the app
        self._keep_log("download", out, err)
        outs = self.outcomes(results, parse_events(out), err)
        if code == TIMED_OUT:
            for o in outs:
                if not o.succeeded and o.failure == "no result reported":
                    o.failure = "timed out waiting for the peer"
        return outs

    def _keep_log(self, kind: str, out: str, err: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (self.workdir / "logs").mkdir(exist_ok=True)
        (self.workdir / "logs" / f"{kind}-{stamp}.log").write_text(err + "\n--- stdout ---\n" + out, encoding="utf-8")

    @staticmethod
    def outcomes(results: list[SearchResult], events: list[dict], stderr: str = "") -> list[DownloadOutcome]:
        """One outcome per requested result, matched on (username, remote filename)."""
        final: dict[tuple[str, str], dict] = {}
        for e in events:
            if e.get("type") == "track_state" and e["data"].get("lifecycleState") == "Terminal":
                d = e["data"]
                final[(d.get("username", ""), d.get("filename", ""))] = d
        details = parse_failure_details(stderr)
        out = []
        for r in results:
            d = final.get((r.username, r.filename))
            if d and d.get("terminalOutcome") == "Succeeded":
                out.append(DownloadOutcome(r.username, r.filename, True, d.get("downloadPath"), None))
            else:
                reason = details.get((r.username, _basename(r.filename))) or (d or {}).get("failureReason") or "no result reported"
                out.append(DownloadOutcome(r.username, r.filename, False, None, reason))
        return out
