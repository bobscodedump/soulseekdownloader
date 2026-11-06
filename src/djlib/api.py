"""Local HTTP API and web UI. Run: `djlib serve` (http://127.0.0.1:8765)."""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from fastapi import FastAPI, File as Upload, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import review as review_mod
from .db import Crate, File, Playlist, Setting, Track, TrackStatus, now
from .ingest_csv import import_csv
from .jobs import JobRunner
from .library import mark_owned, playlist_tracks, sync_library
from .normalize import length_class, strip_length
from .reasons import summarize
from .orchestrator import run as run_download
from .retries import MAX_RETRIES, run_due
from .sockseek import Sockseek

WEB_DIST = Path(__file__).parent / "web"
FAILED = (TrackStatus.failed_no_results, TrackStatus.failed_transfer, TrackStatus.buy)


@dataclass
class Config:
    library_root: Path
    data_dir: Path  # uploaded playlists, staging, logs
    rejected_dir: Path
    sockseek_factory: Callable[[Path], Sockseek]
    nicotine_check: Callable[[], bool] | None = None
    background: bool = True  # periodic rescan + scheduled retries
    rescan_every_s: int = 60
    retry_check_every_s: int = 30 * 60


class SettingsIn(BaseModel):
    library_root: str | None = None
    auto_retry: bool | None = None

class PlaylistIn(BaseModel):
    crate: str

class DownloadIn(BaseModel):
    crate: str | None = None
    dry_run: bool = False

class StatusIn(BaseModel):
    status: str  # retry | buy | ignored


def today_crate() -> str:
    return datetime.now().strftime("%d%m%Y")


def create_app(Session_: sessionmaker[Session], cfg: Config) -> FastAPI:
    app = FastAPI(title="DJ Library Manager")
    runner = JobRunner()
    app.state.runner, app.state.cfg, app.state.Session = runner, cfg, Session_

    def root(s: Session) -> Path:
        st = s.get(Setting, "library_root")
        return Path(st.value) if st else cfg.library_root

    def auto_retry(s: Session) -> bool:
        st = s.get(Setting, "auto_retry")
        return st is None or st.value == "1"

    # ---------- work run on the job thread ----------
    def download_work(track_ids: list[int], crate: str, dry_run: bool = False):
        def work(log):
            with Session_() as s:
                tracks = [s.get(Track, i) for i in track_ids]
                kwargs = {"is_nicotine_running": cfg.nicotine_check} if cfg.nicotine_check else {}
                ss = cfg.sockseek_factory(cfg.data_dir / "staging")
                rep = run_download(s, tracks, root(s), crate, ss, cfg.rejected_dir, dry_run=dry_run, log=log, **kwargs)
                if dry_run:
                    return {"plan": [{"track": f"{p.track.artist} - {p.track.title}", "candidates": len(p.candidates),
                                      "best": p.candidates[0].result.filename.split("\\")[-1] if p.candidates else None}
                                     for p in rep.plans], "owned": len(rep.owned_before)}
                return {"filed": len(rep.filed), "rejected": len(rep.rejected), "failed": len(rep.failed),
                        "owned": len(rep.owned_before), "crate": crate}
        return work

    def retry_work(log):
        with Session_() as s:
            out = {}

            def dl(tracks, crate):
                log(f"retrying {len(tracks)} tracks into {crate}")
                kwargs = {"is_nicotine_running": cfg.nicotine_check} if cfg.nicotine_check else {}
                rep = run_download(s, tracks, root(s), crate, cfg.sockseek_factory(cfg.data_dir / "staging"),
                                   cfg.rejected_dir, log=log, **kwargs)
                out[crate] = {"filed": len(rep.filed), "failed": len(rep.failed)}

            summary = run_due(s, dl)
            if not summary["crates"]:
                log("nothing due")
            return {**summary, "results": out}

    # ---------- background loop ----------
    def background():
        last_retry = 0.0
        while True:
            time.sleep(cfg.rescan_every_s)
            if runner.current:
                continue
            try:
                with Session_() as s:
                    sync_library(s, root(s))
                    if auto_retry(s) and time.time() - last_retry > cfg.retry_check_every_s:
                        last_retry = time.time()
                        from .retries import due
                        groups, _, _ = due(s)
                        if groups:
                            runner.submit("retry", "Scheduled retry", retry_work)
            except Exception as e:  # keep the loop alive, but never hide the problem
                app.state.background_error = f"{datetime.now():%H:%M} {type(e).__name__}: {e}"
                logging.getLogger("djlib").exception("background scan/retry failed")

    if cfg.background:
        threading.Thread(target=background, daemon=True).start()

    # ---------- read endpoints ----------
    @app.get("/api/summary")
    def summary():
        with Session_() as s:
            by_status = dict(s.execute(select(Track.status, func.count()).group_by(Track.status)).all())
            cur = runner.current
            return {
                "library_root": str(root(s)), "files": s.scalar(select(func.count(File.id))),
                "tracks": {k.value: v for k, v in by_status.items()},
                "review": len(review_mod.items(s)), "today_crate": today_crate(),
                "current_job": cur.view() if cur else None, "auto_retry": auto_retry(s),
                "background_error": getattr(app.state, "background_error", None),
            }

    @app.get("/api/crates")
    def crates():
        with Session_() as s:
            rows = s.execute(select(Crate.name, Crate.kind, func.count(File.id)).join(File, File.crate_id == Crate.id, isouter=True)
                             .group_by(Crate.id).order_by(Crate.name)).all()
            return [{"name": n, "kind": k.value, "files": c} for n, k, c in rows]

    @app.get("/api/library")
    def library(q: str = "", status: str = "", crate: str = "", fmt: str = "", camelot: str = "",
                bpm_min: float | None = None, bpm_max: float | None = None, limit: int = 500):
        """Every file in the library (with its track's DJ metadata when known) plus wanted tracks with no file."""
        with Session_() as s:
            rows = []
            for f, t, c in s.execute(select(File, Track, Crate).join(Track, Track.id == File.track_id, isouter=True)
                                     .join(Crate, Crate.id == File.crate_id, isouter=True)):
                rows.append({
                    "kind": "file", "file_id": f.id, "track_id": t.id if t else None,
                    "artist": (t.artist if t else None) or f.tag_artist or f.parsed_artist or "",
                    "title": (t.title if t else None) or f.tag_title or f.parsed_title or Path(f.path).stem,
                    "version": _file_version(t, f), "bpm": t.bpm if t else None, "camelot": t.camelot if t else None,
                    "energy": t.energy if t else None, "crate": c.name if c else "", "format": f.format,
                    "bitrate": f.bitrate, "duration": f.duration_s, "status": "owned", "path": f.path,
                })
            for t in s.scalars(select(Track).where(Track.status != TrackStatus.owned)):
                rows.append({"kind": "track", "file_id": None, "track_id": t.id, "artist": t.artist, "title": t.title,
                             "version": t.version, "bpm": t.bpm, "camelot": t.camelot, "energy": t.energy, "crate": "",
                             "format": "", "bitrate": None, "duration": t.duration_s, "status": t.status.value,
                             "path": None, "reason": t.status_reason})
        ql = q.lower()

        def keep(r):
            return ((not ql or ql in f"{r['artist']} {r['title']} {r['version'] or ''}".lower())
                    and (not status or r["status"] == status) and (not crate or r["crate"] == crate)
                    and (not fmt or r["format"] == fmt) and (not camelot or r["camelot"] == camelot)
                    and (bpm_min is None or (r["bpm"] or 0) >= bpm_min) and (bpm_max is None or (r["bpm"] or 1e9) <= bpm_max))

        out = [r for r in rows if keep(r)]
        return {"total": len(out), "rows": out[:limit]}

    @app.get("/api/playlists")
    def playlists():
        with Session_() as s:
            out = []
            for pl in s.scalars(select(Playlist).order_by(Playlist.imported_at.desc())):
                c = Counter(t.status.value for t in playlist_tracks(s, pl.id))
                out.append({"id": pl.id, "name": pl.name, "crate": pl.crate, "source": pl.source,
                            "imported_at": pl.imported_at.isoformat(), "counts": dict(c), "total": sum(c.values())})
            return out

    @app.get("/api/playlists/{pid}")
    def playlist(pid: int):
        with Session_() as s:
            pl = s.get(Playlist, pid) or _404("playlist")
            return {"id": pl.id, "name": pl.name, "crate": pl.crate, "tracks": [
                {"id": t.id, "artist": t.artist, "title": t.title, "version": t.version, "bpm": t.bpm,
                 "camelot": t.camelot, "energy": t.energy, "duration": t.duration_s, "status": t.status.value,
                 "reason": t.status_reason} for t in playlist_tracks(s, pid)]}

    @app.get("/api/failed")
    def failed():
        with Session_() as s:
            rows = s.scalars(select(Track).where(Track.status.in_(FAILED)).order_by(Track.status, Track.artist)).all()
            return [{"id": t.id, "artist": t.artist, "title": t.title, "version": t.version, "status": t.status.value,
                     "reason": summarize(t.status_reason), "reason_full": t.status_reason, "tries": t.retry_count, "max_tries": MAX_RETRIES,
                     "next_retry_at": t.next_retry_at.isoformat() if t.next_retry_at else None,
                     "buy": {"bandcamp": f"https://bandcamp.com/search?q={quote_plus(t.artist + ' ' + t.title)}",
                             "beatport": f"https://www.beatport.com/search?q={quote_plus(t.artist + ' ' + t.title)}"}}
                    for t in rows]

    @app.get("/api/review")
    def review_items():
        with Session_() as s:
            return [{"kind": i.kind, "id": i.id, "track_id": i.track.id, "artist": i.track.artist,
                     "title": i.track.title, "version": i.track.version, "wanted_duration": i.track.duration_s,
                     "file": Path(i.path).name, "path": i.path, "reason": i.reason} for i in review_mod.items(s)]

    @app.get("/api/jobs")
    def jobs():
        return [j.view() | {"log": [], "log_total": len(j.log)} for j in reversed(runner.jobs.values())]

    @app.get("/api/jobs/{jid}")
    def job(jid: int, after: int = 0):
        j = runner.jobs.get(jid) or _404("job")
        return j.view(after)

    @app.get("/api/settings")
    def get_settings():
        with Session_() as s:
            return {"library_root": str(root(s)), "auto_retry": auto_retry(s), "rejected_dir": str(cfg.rejected_dir)}

    # ---------- write endpoints ----------
    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        with Session_() as s:
            if body.library_root is not None:
                p = Path(body.library_root).expanduser()
                if not p.is_dir():
                    raise HTTPException(400, f"not a folder: {p}")
                s.merge(Setting(key="library_root", value=str(p.resolve())))
            if body.auto_retry is not None:
                s.merge(Setting(key="auto_retry", value="1" if body.auto_retry else "0"))
            s.commit()
        return get_settings()

    @app.post("/api/library/scan")
    def scan_now():
        with Session_() as s:
            st = sync_library(s, root(s))
            return {"seen": st.seen, "added": st.added, "moved": st.moved, "removed": st.removed,
                    "needs_tags": len(st.no_artist_title)}

    @app.post("/api/playlists/import")
    async def import_playlist(file: UploadFile = Upload(...)):
        name = Path(file.filename or "playlist.csv").name
        if not name.lower().endswith(".csv"):
            raise HTTPException(400, "upload a .csv file")
        dest_dir = cfg.data_dir / "playlists"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        dest.write_bytes(await file.read())
        with Session_() as s:
            try:
                pl, st = import_csv(s, dest)
            except ValueError as e:
                raise HTTPException(400, str(e))
            marked = mark_owned(s, playlist_tracks(s, pl.id), root(s))
            return {"id": pl.id, "name": pl.name, "rows": st.rows, "new_tracks": st.new_tracks,
                    "existing_tracks": st.existing_tracks, "owned": marked["owned"], "review": marked["review"]}

    @app.patch("/api/playlists/{pid}")
    def set_crate(pid: int, body: PlaylistIn):
        with Session_() as s:
            pl = s.get(Playlist, pid) or _404("playlist")
            pl.crate = _clean_crate(body.crate)
            s.commit()
            return {"id": pl.id, "crate": pl.crate}

    @app.post("/api/playlists/{pid}/download")
    def download(pid: int, body: DownloadIn):
        with Session_() as s:
            pl = s.get(Playlist, pid) or _404("playlist")
            crate = _clean_crate(body.crate or pl.crate or today_crate())
            if not body.dry_run:
                pl.crate = crate
                s.commit()
            ids = [t.id for t in playlist_tracks(s, pid)]
            name = pl.name
        try:
            job = runner.submit("download", f"{'Preview' if body.dry_run else 'Download'} {name} -> {crate}",
                                download_work(ids, crate, body.dry_run))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return job.view()

    @app.post("/api/retries/run")
    def retry_now():
        try:
            return runner.submit("retry", "Retry due tracks", retry_work).view()
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/tracks/{tid}/status")
    def set_status(tid: int, body: StatusIn):
        with Session_() as s:
            t = s.get(Track, tid) or _404("track")
            if body.status == "retry":
                t.next_retry_at = now()  # due now; picked up by the next retry run
            elif body.status in ("buy", "ignored"):
                t.status = TrackStatus(body.status)
            else:
                raise HTTPException(400, "status must be retry, buy or ignored")
            s.commit()
            return {"id": t.id, "status": t.status.value}

    @app.post("/api/review/{kind}/{iid}/{action}")
    def review_action(kind: str, iid: int, action: str):
        with Session_() as s:
            if (kind, action) == ("match", "accept"):
                review_mod.accept_match(s, iid)
            elif (kind, action) == ("match", "dismiss"):
                review_mod.dismiss_match(s, iid)
            elif (kind, action) == ("rejected", "accept"):
                try:
                    dest = review_mod.accept_rejected(s, iid, root(s), None)
                except ValueError:
                    dest = review_mod.accept_rejected(s, iid, root(s), today_crate())
                return {"ok": True, "filed": str(dest)}
            elif (kind, action) == ("rejected", "dismiss"):
                review_mod.dismiss_rejected(s, iid)
            else:
                raise HTTPException(400, "unknown review action")
            return {"ok": True}

    # ---------- web UI ----------
    if (WEB_DIST / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            return FileResponse(WEB_DIST / "index.html")

    return app


def _file_version(t: Track | None, f: File) -> str | None:
    """What the file actually is, noting when it differs from the version the playlist listed."""
    name = " ".join(x for x in (f.tag_title, Path(f.path).stem) if x)
    have = length_class(name)
    if t is None:
        return have.capitalize() if have and have != "original" else None
    want = length_class(t.version) or "original"
    if have and have != want:
        rest = strip_length(t.version).strip(" -").title() if t.version else ""
        return f"{have.capitalize()}{' ' + rest if rest else ''} (listed: {t.version or 'Original'})"
    return t.version


def _404(what: str):
    raise HTTPException(404, f"{what} not found")


def _clean_crate(name: str) -> str:
    name = name.strip().strip("/")
    if not name or "/" in name or name.startswith(".") or ".." in name:
        raise HTTPException(400, "crate must be a single folder name, e.g. 06112026 or shranz")
    return name
