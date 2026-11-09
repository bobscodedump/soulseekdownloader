"""Local HTTP API and web UI. Run: `djlib serve` (http://127.0.0.1:8765)."""
from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from fastapi import FastAPI, File as Upload, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from . import review as review_mod
from .db import Crate, File, Playlist, Setting, Track, TrackStatus, now
from .export import XML_NAME, export_library, library_signature
from .ingest_csv import import_csv, save_playlist
from .jobs import JobRunner
from .library import mark_owned, playlist_tracks, sync_library
from .normalize import length_class, strip_length
from .reasons import summarize
from .orchestrator import run as run_download
from .retries import MAX_RETRIES, run_due
from .orchestrator import PreflightError
from . import setup
from .sockseek import SOCKSEEK, Sockseek, subprocess_runner
from . import duplicates as dupes_mod
from .fingerprint import fpcalc_path
from .upgrades import candidates as upgrade_candidates, run_upgrades
from .spotify import Spotify, SpotifyError, TokenStore, authorize_url, make_verifier

WEB_DIST = Path(__file__).parent / "web"
FAILED = (TrackStatus.failed_no_results, TrackStatus.failed_transfer, TrackStatus.buy)


@dataclass
class Config:
    library_root: Path
    data_dir: Path  # uploaded playlists, staging, logs
    rejected_dir: Path
    sockseek_factory: Callable[[Path], Sockseek] | None = None  # None: real sockseek at the configured path
    nicotine_check: Callable[[], bool] | None = None
    background: bool = True  # periodic rescan + scheduled retries
    rescan_every_s: int = 60
    retry_check_every_s: int = 30 * 60
    port: int = 8765
    spotify_store: object = None  # TokenStore; None = macOS Keychain
    spotify_http: object = None  # httpx.Client; tests pass a mock transport
    export_dir: Path | None = None  # Rekordbox XML + M3U8; None = data_dir / "exports"
    replaced_dir: Path | None = None  # MP3s replaced by lossless upgrades; None = data_dir / "replaced"
    duplicates_dir: Path | None = None  # extra copies moved out of the library; None = data_dir / "duplicates"
    sockseek_fetch: Callable[[str], bytes] | None = None  # tests; None = download from GitHub
    sockseek_conf: Path | None = None  # tests; None = ~/.config/sockseek/sockseek.conf
    fpcalc: str | None = "auto"  # Chromaprint fpcalc; "auto" = look on PATH / Homebrew, None = not installed


class LoginIn(BaseModel):
    username: str
    password: str


class SettingsIn(BaseModel):
    library_root: str | None = None
    auto_retry: bool | None = None
    sockseek_path: str | None = None

class PlaylistIn(BaseModel):
    crate: str

class DownloadIn(BaseModel):
    crate: str | None = None
    dry_run: bool = False

class SpotifyIn(BaseModel):
    client_id: str


class SpotifyImportIn(BaseModel):
    url: str


class UpgradeIn(BaseModel):
    file_ids: list[int]
    dry_run: bool = False


class ResolveIn(BaseModel):
    keep: int
    move: list[int]


class StatusIn(BaseModel):
    status: str  # retry | buy | ignored


def default_config(port: int = 8765) -> Config:
    from .db import db_path
    (Path.home() / "Desktop" / "DJ").mkdir(parents=True, exist_ok=True)  # first run: the folder must exist
    return Config(library_root=Path.home() / "Desktop" / "DJ", data_dir=db_path().parent,
                  rejected_dir=Path.home() / "Desktop" / "soulseek" / "downloads" / "rejected", port=port,
                  export_dir=Path.home() / "Desktop" / "soulseek" / "exports",
                  replaced_dir=Path.home() / "Desktop" / "soulseek" / "downloads" / "replaced",
                  duplicates_dir=Path.home() / "Desktop" / "soulseek" / "downloads" / "duplicates")


def today_crate() -> str:
    return datetime.now().strftime("%d%m%Y")


def create_app(Session_: sessionmaker[Session], cfg: Config) -> FastAPI:
    app = FastAPI(title="DJ Library Manager")
    runner = JobRunner()
    app.state.runner, app.state.cfg, app.state.Session = runner, cfg, Session_

    def root(s: Session) -> Path:
        st = s.get(Setting, "library_root")
        return Path(st.value) if st else cfg.library_root

    def sockseek_path(s: Session) -> Path:
        st = s.get(Setting, "sockseek_path")
        if st:
            return Path(st.value)
        return managed() if managed().is_file() else SOCKSEEK

    def managed() -> Path:
        return cfg.data_dir / "bin" / "sockseek"

    def conf() -> Path:
        return cfg.sockseek_conf or setup.CONF

    def make_sockseek(s: Session) -> Sockseek:
        if cfg.sockseek_factory:
            return cfg.sockseek_factory(cfg.data_dir / "staging")
        path = sockseek_path(s)
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise PreflightError(f"sockseek was not found at {path}. Set its location in Settings.")
        return Sockseek(subprocess_runner(path), cfg.data_dir / "staging")

    def auto_retry(s: Session) -> bool:
        st = s.get(Setting, "auto_retry")
        return st is None or st.value == "1"

    export_dir = cfg.export_dir or cfg.data_dir / "exports"
    replaced_dir = cfg.replaced_dir or cfg.data_dir / "replaced"
    duplicates_dir = cfg.duplicates_dir or cfg.data_dir / "duplicates"

    def fpcalc() -> str | None:
        return fpcalc_path() if cfg.fpcalc == "auto" else cfg.fpcalc

    def dupes_work(log):
        with Session_() as s:
            fp = fpcalc() or _missing_fpcalc()
            found = dupes_mod.find(s, root(s), fp, log=log)
            return {"groups": len(found), "files": sum(len(g) for g in found)}
    export_lock = threading.Lock()

    def do_export(s: Session):
        with export_lock:
            sig = library_signature(s)
            r = export_library(s, root(s), export_dir)
            app.state.export_sig = sig
            return r

    def export_stale(s: Session) -> bool:
        return getattr(app.state, "export_sig", None) != library_signature(s)

    # ---------- work run on the job thread ----------
    def download_work(track_ids: list[int], crate: str, dry_run: bool = False):
        def work(log):
            with Session_() as s:
                tracks = [s.get(Track, i) for i in track_ids]
                kwargs = {"is_nicotine_running": cfg.nicotine_check} if cfg.nicotine_check else {}
                ss = make_sockseek(s)
                rep = run_download(s, tracks, root(s), crate, ss, cfg.rejected_dir, dry_run=dry_run, log=log, **kwargs)
                if dry_run:
                    return {"plan": [{"track": f"{p.track.artist} - {p.track.title}", "candidates": len(p.candidates),
                                      "best": p.candidates[0].result.filename.split("\\")[-1] if p.candidates else None}
                                     for p in rep.plans], "owned": len(rep.owned_before)}
                return {"filed": len(rep.filed), "rejected": len(rep.rejected), "failed": len(rep.failed),
                        "owned": len(rep.owned_before), "crate": crate}
        return work

    def upgrade_work(file_ids: list[int], dry_run: bool):
        def work(log):
            with Session_() as s:
                kwargs = {"is_nicotine_running": cfg.nicotine_check} if cfg.nicotine_check else {}
                rep = run_upgrades(s, file_ids, root(s), make_sockseek(s), cfg.rejected_dir, replaced_dir,
                                   dry_run=dry_run, log=log, **kwargs)
                if dry_run:
                    return {"plan": [{"track": f"{p.track.artist} - {p.track.title}", "lossless": len(p.candidates)}
                                     for p in rep.plans]}
                return {"upgraded": len(rep.upgraded), "not_found": len(rep.not_found), "rejected": rep.rejected,
                        "replaced_dir": str(replaced_dir)}
        return work

    def retry_work(log):
        with Session_() as s:
            out = {}

            def dl(tracks, crate):
                log(f"retrying {len(tracks)} tracks into {crate}")
                kwargs = {"is_nicotine_running": cfg.nicotine_check} if cfg.nicotine_check else {}
                rep = run_download(s, tracks, root(s), crate, make_sockseek(s),
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
                    # Keep an existing Rekordbox export current; Rekordbox re-reads it when refreshed.
                    if (export_dir / XML_NAME).exists() and export_stale(s):
                        do_export(s)
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

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: int):
        if not runner.cancel(jid):
            raise HTTPException(409, "that job is not running")
        from .sockseek import cancel_running
        cancel_running()  # kills the search/download in progress; the job then ends as "stopped"
        return {"ok": True}

    @app.get("/api/settings")
    def get_settings():
        with Session_() as s:
            p = sockseek_path(s)
            return {"library_root": str(root(s)), "auto_retry": auto_retry(s), "rejected_dir": str(cfg.rejected_dir),
                    "sockseek_path": str(p), "sockseek_ok": p.is_file() and os.access(p, os.X_OK),
                    "soulseek_user": setup.soulseek_user(conf())}

    @app.get("/api/upgrades")
    def upgrades():
        with Session_() as s:
            return {"replaced_dir": str(replaced_dir), "rows": [{
                "file_id": c.file.id, "track_id": c.track.id, "artist": c.track.artist, "title": c.track.title,
                "version": c.track.version, "crate": c.file.crate.name if c.file.crate else "",
                "format": c.file.format, "bitrate": c.file.bitrate, "duration": c.file.duration_s,
                "file": Path(c.file.path).name, "note": c.file.upgrade_note,
                "checked_at": c.file.upgrade_checked_at.isoformat() if c.file.upgrade_checked_at else None,
            } for c in upgrade_candidates(s, root(s))]}

    @app.get("/api/duplicates")
    def duplicates():
        with Session_() as s:
            gs, sims = dupes_mod.saved(s)
            ran = s.get(Setting, dupes_mod.KEY) is not None
            lib = root(s).resolve()

            def row(f: File):
                t = s.get(Track, f.track_id) if f.track_id else None
                p = Path(f.path)
                return {"file_id": f.id, "path": f.path, "rel": str(p.relative_to(lib)) if p.is_relative_to(lib) else f.path,
                        "crate": f.crate.name if f.crate else "", "format": f.format, "bitrate": f.bitrate,
                        "duration": f.duration_s, "size": f.size, "track": f"{t.artist} - {t.title}" if t else None}

            return {"fpcalc": fpcalc(), "ran": ran, "duplicates_dir": str(duplicates_dir), "groups": [{
                "files": [row(f) for f in g],
                "similarity": min((sims.get(frozenset((a.id, b.id)), 1.0) for a in g for b in g if a.id < b.id
                                   and frozenset((a.id, b.id)) in sims), default=1.0),
            } for g in gs]}

    @app.get("/api/export")
    def export_status():
        xml = export_dir / XML_NAME
        with Session_() as s:
            return {"xml_path": str(xml), "m3u_dir": str(export_dir / "m3u"), "exists": xml.exists(),
                    "exported_at": datetime.fromtimestamp(xml.stat().st_mtime).isoformat() if xml.exists() else None,
                    "stale": xml.exists() and export_stale(s)}

    # ---------- write endpoints ----------
    @app.post("/api/export")
    def export_now():
        with Session_() as s:
            r = do_export(s)
        return export_status() | {"tracks": r.tracks, "crates": r.crates,
                                  "playlists": {k: {"tracks": a, "missing": b} for k, (a, b) in r.playlists.items()}}

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
            if body.sockseek_path is not None:
                p = Path(body.sockseek_path).expanduser()
                if not (p.is_file() and os.access(p, os.X_OK)):
                    raise HTTPException(400, f"not an executable file: {p}")
                s.merge(Setting(key="sockseek_path", value=str(p.resolve())))
            s.commit()
        return get_settings()

    @app.post("/api/setup/sockseek")
    def install_sockseek():
        try:
            path = setup.install_sockseek(cfg.sockseek_fetch or setup.download, managed())
        except Exception as e:
            raise HTTPException(502, f"Could not download sockseek ({e}). Check your internet connection.")
        with Session_() as s:
            s.merge(Setting(key="sockseek_path", value=str(path)))
            s.commit()
        return get_settings()

    @app.put("/api/setup/soulseek")
    def save_soulseek(body: LoginIn):
        try:
            setup.save_login(body.username, body.password, conf())
        except ValueError as e:
            raise HTTPException(400, str(e))
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

    @app.post("/api/upgrades")
    def start_upgrades(body: UpgradeIn):
        if not body.file_ids:
            raise HTTPException(400, "choose at least one file")
        try:
            job = runner.submit("upgrade", f"{'Preview' if body.dry_run else 'Find'} lossless copies of "
                                f"{len(body.file_ids)} file{'s' if len(body.file_ids) != 1 else ''}",
                                upgrade_work(body.file_ids, body.dry_run))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return job.view()

    @app.post("/api/duplicates/scan")
    def scan_duplicates():
        if not fpcalc():
            _missing_fpcalc()
        try:
            return runner.submit("duplicates", "Find duplicate audio", dupes_work).view()
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/duplicates/resolve")
    def resolve_duplicates(body: ResolveIn):
        if runner.current:
            raise HTTPException(409, "wait for the running job to finish")
        with Session_() as s:
            try:
                moved = dupes_mod.move_extras(s, body.keep, body.move, root(s), duplicates_dir)
            except ValueError as e:
                raise HTTPException(400, str(e))
            return {"moved": [str(p) for p in moved]}

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

    # ---------- Spotify ----------
    pkce: dict[str, str] = {}  # state -> code verifier, for logins in progress

    def redirect_uri() -> str:
        return f"http://127.0.0.1:{cfg.port}/api/spotify/callback"

    def spotify(s: Session) -> Spotify:
        st = s.get(Setting, "spotify_client_id")
        if not st or not st.value:
            raise SpotifyError("Add your Spotify app's Client ID in Settings first.")
        return Spotify(st.value, redirect_uri(), cfg.spotify_store, cfg.spotify_http)

    @app.get("/api/spotify")
    def spotify_status():
        with Session_() as s:
            st = s.get(Setting, "spotify_client_id")
            out = {"client_id": st.value if st else "", "redirect_uri": redirect_uri(), "connected": False, "user": None}
            if st and st.value:
                sp = spotify(s)
                out["connected"] = sp.connected
                if sp.connected:
                    try:
                        out["user"] = sp.me().get("display_name")
                    except SpotifyError as e:
                        out["connected"], out["error"] = sp.connected, str(e)
            return out

    @app.put("/api/spotify")
    def spotify_set(body: SpotifyIn):
        cid = body.client_id.strip()
        if not re.fullmatch(r"[0-9a-f]{32}", cid):
            raise HTTPException(400, "A Spotify Client ID is 32 characters of 0-9 and a-f (Dashboard > your app > Settings).")
        with Session_() as s:
            old = s.get(Setting, "spotify_client_id")
            if old and old.value != cid:
                (cfg.spotify_store or TokenStore()).clear()  # tokens belong to the old app
            s.merge(Setting(key="spotify_client_id", value=cid))
            s.commit()
        return spotify_status()

    @app.get("/api/spotify/login")
    def spotify_login():
        with Session_() as s:
            try:
                sp = spotify(s)
            except SpotifyError as e:
                raise HTTPException(400, str(e))
        state, verifier = secrets.token_urlsafe(16), make_verifier()
        pkce[state] = verifier
        return RedirectResponse(authorize_url(sp.client_id, redirect_uri(), verifier, state))

    @app.get("/api/spotify/callback")
    def spotify_callback(state: str = "", code: str = "", error: str = ""):
        verifier = pkce.pop(state, None)
        if error or not code or not verifier:
            why = error or ("login expired, try again" if not verifier else "no code returned")
            return RedirectResponse(f"/#settings?spotify_error={quote_plus(why)}")
        with Session_() as s:
            try:
                spotify(s).exchange(code, verifier)
            except SpotifyError as e:
                return RedirectResponse(f"/#settings?spotify_error={quote_plus(str(e))}")
        return RedirectResponse("/#settings?spotify=connected")

    @app.post("/api/spotify/logout")
    def spotify_logout():
        (cfg.spotify_store or TokenStore()).clear()
        return {"ok": True}

    @app.post("/api/spotify/import")
    def spotify_import(body: SpotifyImportIn):
        with Session_() as s:
            try:
                pl = spotify(s).playlist(body.url)
            except SpotifyError as e:
                raise HTTPException(400, str(e))
            p, st = save_playlist(s, "spotify", f"spotify:playlist:{pl.id}", pl.name, pl.records)
            marked = mark_owned(s, playlist_tracks(s, p.id), root(s))
            return {"id": p.id, "name": p.name, "rows": st.rows, "new_tracks": st.new_tracks,
                    "existing_tracks": st.existing_tracks, "owned": marked["owned"], "review": marked["review"],
                    "skipped": pl.skipped}

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


def _missing_fpcalc():
    raise HTTPException(400, "Finding duplicates needs Chromaprint's fpcalc. Install it with: brew install chromaprint")


def _404(what: str):
    raise HTTPException(404, f"{what} not found")


def _clean_crate(name: str) -> str:
    name = name.strip().strip("/")
    if not name or "/" in name or name.startswith(".") or ".." in name:
        raise HTTPException(400, "crate must be a single folder name, e.g. 06112026 or shranz")
    return name
