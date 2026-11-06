"""One background worker: Soulseek allows a single login, so downloads run one job at a time."""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .db import now


@dataclass
class Job:
    id: int
    kind: str  # download | retry | scan
    title: str
    state: str = "queued"  # queued | running | finished | failed | aborted
    log: list[str] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def view(self, after: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "state": self.state,
                "log": self.log[after:], "log_total": len(self.log), "result": self.result,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None}


class JobRunner:
    def __init__(self):
        self.jobs: dict[int, Job] = {}
        self._lock = threading.Lock()
        self._next = 1
        self._thread: threading.Thread | None = None

    @property
    def current(self) -> Job | None:
        with self._lock:
            return next((j for j in reversed(self.jobs.values()) if j.state in ("queued", "running")), None)

    def submit(self, kind: str, title: str, work: Callable[[Callable[[str], None]], dict | None]) -> Job:
        """Start `work(log)` in the background. Raises RuntimeError if a job is already running."""
        with self._lock:
            if any(j.state in ("queued", "running") for j in self.jobs.values()):
                raise RuntimeError("another job is running")
            job = Job(self._next, kind, title)
            self.jobs[job.id] = job
            self._next += 1
        self._thread = threading.Thread(target=self._run, args=(job, work), daemon=True)
        self._thread.start()
        return job

    def _run(self, job: Job, work) -> None:
        job.state, job.started_at = "running", now()
        try:
            job.result = work(job.log.append) or {}
            job.state = "finished"
        except Exception as e:  # surfaced to the UI; the app keeps running
            job.log.append(f"ERROR: {e}")
            job.log.extend(traceback.format_exc().splitlines()[-6:])
            job.state = "aborted" if e.__class__.__name__ == "PreflightError" else "failed"
            job.result = {"error": str(e)}
        finally:
            job.finished_at = now()

    def wait(self, timeout: float = 30) -> None:
        if self._thread:
            self._thread.join(timeout)
