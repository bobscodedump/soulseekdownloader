"""Audio fingerprints (Chromaprint's fpcalc) to find the same recording under different names or formats.

Name matching cannot see that "A-05 Walking on a Dream[ms].wav" and "Empire of the Sun - Walking on a
Dream.mp3" are one song; the audio can. Fingerprints cover the first 120 s and are compared bit by bit
at small time offsets (leading silence differs between rips).
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import File

FPCALC_PATHS = ("/opt/homebrew/bin/fpcalc", "/usr/local/bin/fpcalc")
LENGTH_S = 120
MAX_OFFSET = 48  # fingerprint frames (~8 per second): about 6 s of leading silence
SAME = 0.93  # unrelated techno peaks at ~0.86 (shared kicks/loops); real copies, even MP3 vs FLAC, score >= 0.99
MAX_DURATION_GAP_S = 20


def fpcalc_path() -> str | None:
    return shutil.which("fpcalc") or next((p for p in FPCALC_PATHS if Path(p).exists()), None)


def compute(path: str | Path, fpcalc: str) -> np.ndarray | None:
    r = subprocess.run([fpcalc, "-raw", "-json", "-length", str(LENGTH_S), str(path)],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    fp = json.loads(r.stdout).get("fingerprint") or []
    return np.array(fp, dtype=np.int64).astype(np.uint32) if fp else None


def encode(fp: np.ndarray) -> str:
    return base64.b64encode(fp.astype("<u4").tobytes()).decode()


def decode(text: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(text), dtype="<u4")


def similarity(a: np.ndarray, b: np.ndarray, max_offset: int = MAX_OFFSET) -> float:
    """1 - the lowest bit error rate over time offsets; unrelated audio scores about 0.5."""
    best = 0.0
    min_overlap = max(16, min(len(a), len(b)) // 2)
    for o in range(-max_offset, max_offset + 1):
        x, y = (a[o:], b) if o >= 0 else (a, b[-o:])
        n = min(len(x), len(y))
        if n < min_overlap:
            continue
        errors = int(np.bitwise_count(x[:n] ^ y[:n]).sum())
        best = max(best, 1 - errors / (32 * n))
    return best


@dataclass
class FingerprintStats:
    done: int = 0
    failed: list[str] | None = None
    skipped: int = 0


def fingerprint_library(s: Session, files: list[File], fpcalc: str, workers: int = 6,
                        progress: Callable[[int, int], None] | None = None) -> FingerprintStats:
    """Fingerprint files that have none yet or whose content changed since."""
    todo = [f for f in files if not f.fingerprint or f.fp_sha1 != f.sha1]
    st = FingerprintStats(failed=[], skipped=len(files) - len(todo))
    with ThreadPoolExecutor(workers) as ex:
        for i, (f, fp) in enumerate(zip(todo, ex.map(lambda f: compute(f.path, fpcalc), todo)), 1):
            if fp is None:
                st.failed.append(f.path)
            else:
                f.fingerprint, f.fp_sha1 = encode(fp), f.sha1
                st.done += 1
            if i % 50 == 0:
                s.commit()
                if progress:
                    progress(i, len(todo))
    s.commit()
    return st


def same_audio_pairs(files: list[File], threshold: float = SAME) -> list[tuple[File, File, float]]:
    """Pairs of fingerprinted files with the same audio. Only files of similar length are compared."""
    fps = [(f, decode(f.fingerprint)) for f in files if f.fingerprint and f.duration_s]
    fps.sort(key=lambda x: x[0].duration_s)
    out = []
    for i, (f, a) in enumerate(fps):
        for g, b in fps[i + 1:]:
            if g.duration_s - f.duration_s > MAX_DURATION_GAP_S:
                break
            sim = similarity(a, b)
            if sim >= threshold:
                out.append((f, g, sim))
    return out


def groups(pairs: list[tuple[File, File, float]]) -> list[list[File]]:
    """Join pairs into groups (A~B and B~C are one group)."""
    parent: dict[int, int] = {}
    files: dict[int, File] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for f, g, _ in pairs:
        files[f.id], files[g.id] = f, g
        parent[find(f.id)] = find(g.id)
    out: dict[int, list[File]] = {}
    for fid, f in files.items():
        out.setdefault(find(fid), []).append(f)
    return sorted(out.values(), key=lambda fs: min(Path(f.path).name.lower() for f in fs))


def all_files(s: Session, root: str | Path) -> list[File]:
    root = str(Path(root).resolve())
    return [f for f in s.scalars(select(File)) if f.path.startswith(root)]
