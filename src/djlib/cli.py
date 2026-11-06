"""djlib command line: the v0.1 interface before the UI exists."""
from __future__ import annotations

from pathlib import Path

import typer
from sqlalchemy import func, select

from .db import Setting, Track, TrackStatus, db_path, session_factory
from .ingest_csv import import_csv
from .library import FileIndex, dedupe, files_in, link, playlist_tracks
from .matcher import MATCH, REVIEW
from .scanner import scan

app = typer.Typer(help="DJ library manager", no_args_is_help=True)
DEFAULT_ROOT = Path.home() / "Desktop" / "DJ"


def _root(s, root: Path | None) -> Path:
    if root:
        s.merge(Setting(key="library_root", value=str(root.resolve())))
        s.commit()
        return root.resolve()
    st = s.get(Setting, "library_root")
    return Path(st.value) if st else DEFAULT_ROOT


@app.command("scan")
def scan_cmd(root: Path = typer.Argument(None, help="DJ folder (remembered after first use)")):
    """Scan the DJ folder into the database (read-only)."""
    with session_factory()() as s:
        r = _root(s, root)
        st = scan(s, r)
        typer.echo(f"{r}: {st.seen} files ({st.added} new, {st.updated} changed, {st.moved} moved, "
                   f"{st.removed} removed, {st.unchanged} unchanged)")
        if st.no_artist_title:
            typer.echo(f"{len(st.no_artist_title)} files need manual tags:")
            for p in st.no_artist_title:
                typer.echo(f"  {Path(p).relative_to(r)}")


@app.command("import")
def import_cmd(csv_path: Path, name: str = typer.Option(None, help="Playlist name (default: file name)")):
    """Import a playlist CSV."""
    with session_factory()() as s:
        pl, st = import_csv(s, csv_path, name)
        typer.echo(f"{pl.name}: {st.rows} rows, {st.new_tracks} new tracks, {st.existing_tracks} already known, "
                   f"{st.items_added} added to playlist")


@app.command("dupes")
def dupes_cmd(
    csv_path: Path,
    exclude_crate: list[str] = typer.Option([], help="Ignore files in this crate (repeatable)"),
    save: bool = typer.Option(False, help="Record matches in the database"),
):
    """Show which tracks of a playlist CSV are already in the library."""
    with session_factory()() as s:
        pl, _ = import_csv(s, csv_path)
        rows = dedupe(s, playlist_tracks(s, pl.id), FileIndex(files_in(s, str(_root(s, None)))), set(exclude_crate))
        owned = [r for r in rows if r.result.decision == MATCH]
        review = [r for r in rows if r.result.decision == REVIEW]
        root = _root(s, None)
        for label, group in (("ALREADY OWNED", owned), ("CHECK BY HAND", review)):
            if group:
                typer.echo(f"\n{label} ({len(group)})")
            for r in group:
                why = f"  [{'; '.join(r.result.reasons)}]" if r.result.reasons else ""
                typer.echo(f"  {r.track.artist} - {r.track.title}{' - ' + r.track.version if r.track.version else ''}"
                           f"\n      -> {Path(r.file.path).relative_to(root)}{why}")
        typer.echo(f"\n{len(owned)} owned, {len(review)} to check, {len(rows) - len(owned) - len(review)} to download "
                   f"(of {len(rows)})")
        if save:
            typer.echo(f"saved: {link(s, rows)}")


@app.command("import-failures")
def import_failures_cmd(
    csv_path: Path,
    log: list[Path] = typer.Option([], help="sockseek log(s) to read failure reasons from (repeatable)"),
    wrong: list[str] = typer.Option([], help="'Artist - Title' whose downloads were wrong songs (repeatable)"),
):
    """Track a still-failing CSV: reason per track and the next retry time."""
    from .failures import import_failures
    with session_factory()() as s:
        counts = import_failures(s, csv_path, log, {w: "only wrong songs found" for w in wrong})
        typer.echo(f"{csv_path.name}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


@app.command("failed")
def failed_cmd():
    """List failed tracks grouped by reason, with when each retries."""
    with session_factory()() as s:
        rows = s.scalars(select(Track).where(Track.status.in_([TrackStatus.failed_no_results, TrackStatus.failed_transfer]))
                         .order_by(Track.status, Track.status_reason, Track.artist)).all()
        group = None
        for t in rows:
            key = (t.status.value, t.status_reason)
            if key != group:
                group = key
                typer.echo(f"\n{t.status.value}: {t.status_reason}")
            v = f" - {t.version}" if t.version else ""
            typer.echo(f"  {t.artist} - {t.title}{v}  (failed {t.retry_count}x, retry {t.next_retry_at:%Y-%m-%d})")
        typer.echo(f"\n{len(rows)} failed tracks")


@app.command("status")
def status_cmd():
    """Counts of tracks by status."""
    with session_factory()() as s:
        typer.echo(f"database: {db_path()}")
        for st, n in s.execute(select(Track.status, func.count()).group_by(Track.status)):
            typer.echo(f"  {st.value:20} {n}")


if __name__ == "__main__":
    app()
