"""Browser test: the M3 gate, "playlist to crate with no terminal", against the real server and built UI.

Uses a fake sockseek (no network) and a temp library. Screenshots go to tests/.screens/.
"""
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from djlib.api import WEB_DIST, Config, create_app
from djlib.db import session_factory
from test_orchestrator import CSV, RESULTS, TRANSFERS, FakeSockseek, wav

pytestmark = pytest.mark.skipif(not (WEB_DIST / "index.html").exists(), reason="UI not built (npm run build)")
SHOTS = Path(__file__).parent / ".screens"


@pytest.fixture
def server(tmp_path):
    lib = tmp_path / "DJ"
    wav(lib / "08092026" / "Kai Van Dongen - Flash (Original Mix).wav", 240)
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "rejected",
                 sockseek_factory=lambda wd: FakeSockseek(wd, RESULTS, TRANSFERS), nicotine_check=lambda: False,
                 background=False)
    app = create_app(session_factory(tmp_path / "t.db"), cfg)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    csv = tmp_path / "tch3.csv"
    csv.write_text(CSV, encoding="utf-8")
    yield f"http://127.0.0.1:{port}", lib, csv
    srv.should_exit = True


def test_playlist_to_crate_without_terminal(server, page):
    url, lib, csv = server
    SHOTS.mkdir(exist_ok=True)
    errors = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(url)
    page.request.post(f"{url}/api/library/scan")
    page.set_input_files("[data-testid=csv-input]", str(csv))
    page.get_by_test_id("import-msg").wait_for()
    msg = page.get_by_test_id("import-msg").inner_text()
    assert "5 tracks" in msg and "1 already in your library" in msg and "4 to download" in msg
    page.screenshot(path=str(SHOTS / "1-playlists.png"), full_page=True)

    row = page.get_by_test_id("playlist-tch3")
    row.get_by_test_id("crate-input").fill("06112026")
    row.get_by_test_id("download-btn").click()

    page.wait_for_url("**#jobs")
    page.wait_for_function("document.querySelector('[data-testid=job-state]')?.textContent === 'finished'", timeout=15000)
    result = page.get_by_test_id("job-result").inner_text()
    assert "2" in result and "filed into 06112026" in result
    assert "FILED" in page.get_by_test_id("job-log").inner_text()
    page.screenshot(path=str(SHOTS / "2-job.png"), full_page=True)
    assert (lib / "06112026" / "SOLYD - Hot Beats (Extended Mix).wav").exists()

    page.get_by_test_id("tab-review").click()
    card = page.get_by_test_id("review-card")
    card.wait_for()
    assert "Obsessed" in card.inner_text() and "duration" in card.inner_text()
    page.screenshot(path=str(SHOTS / "3-review.png"), full_page=True)
    card.get_by_role("button", name="It's right: file it").click()
    page.get_by_test_id("review-empty").wait_for()
    assert (lib / "06112026" / "KUSS & GRUV - Obsessed.wav").exists()

    page.get_by_test_id("tab-failed").click()
    page.get_by_text("Nobody is sharing it right now").wait_for()
    assert "Selva" in page.content() and "Obsessed" not in page.locator("table").inner_text()
    page.screenshot(path=str(SHOTS / "4-failed.png"), full_page=True)

    page.get_by_test_id("tab-library").click()
    page.get_by_test_id("lib-search").fill("hot beats")
    page.wait_for_function("document.querySelector('[data-testid=lib-total]')?.textContent.startsWith('1 item')")
    page.get_by_test_id("lib-search").fill("")
    page.wait_for_function("document.querySelector('[data-testid=lib-total]')?.textContent.startsWith('5 items')")
    page.screenshot(path=str(SHOTS / "5-library.png"), full_page=True)

    # Export for Rekordbox: writes the XML, shows the counts and the one-time Rekordbox steps.
    page.get_by_test_id("export-run").click()
    page.wait_for_function("document.querySelector('[data-testid=export-status]')?.textContent.includes('crates,')")
    status = page.get_by_test_id("export-status").inner_text()
    assert "4 tracks, 2 crates, 1 playlist." in status and "kept up to date" in status, status
    assert "rekordbox.xml" in page.get_by_test_id("export-help").inner_text()
    xml = lib.parent / "data" / "exports" / "rekordbox.xml"
    assert xml.exists() and 'Name="tch3"' in xml.read_text()
    assert page.get_by_test_id("export-run").inner_text() == "Export again"
    page.screenshot(path=str(SHOTS / "8-export.png"), full_page=True)

    assert errors == [], errors


@pytest.fixture
def spotify_server(tmp_path):
    import httpx

    from djlib.spotify import MemoryStore
    from test_spotify import FakeSpotify
    lib = tmp_path / "DJ"
    wav(lib / "08092026" / "KOBOSIL - Cryptic (Kobosil Hart Mix).wav", 302)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "rejected",
                 sockseek_factory=lambda wd: None, background=False, port=port, spotify_store=MemoryStore(),
                 spotify_http=httpx.Client(transport=httpx.MockTransport(FakeSpotify())))
    app = create_app(session_factory(tmp_path / "t.db"), cfg)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


def test_spotify_setup_connect_and_import(spotify_server, page):
    from urllib.parse import parse_qs, urlparse
    url = spotify_server
    SHOTS.mkdir(exist_ok=True)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.request.post(f"{url}/api/library/scan")

    # Stand-in for Spotify's consent page. Playwright does not route requests reached through a redirect,
    # so intercept the app's login endpoint, check where it sends the browser, and approve as Spotify would.
    def consent(route):
        resp = route.fetch(max_redirects=0)
        loc = resp.headers["location"]
        q = parse_qs(urlparse(loc).query)
        assert loc.startswith("https://accounts.spotify.com/authorize") and q["redirect_uri"] == [f"{url}/api/spotify/callback"]
        route.fulfill(status=302, headers={"Location": f"{url}/api/spotify/callback?state={q['state'][0]}&code=ok"})
    page.route("**/api/spotify/login", consent)

    page.goto(f"{url}/#settings")
    assert page.get_by_test_id("redirect-uri").inner_text() == f"{url}/api/spotify/callback"
    page.screenshot(path=str(SHOTS / "6-spotify-setup.png"), full_page=True)
    page.get_by_test_id("spotify-client-id").fill("0123456789abcdef0123456789abcdef")
    page.get_by_role("button", name="Save", exact=True).last.click()
    page.get_by_text("Client ID saved").wait_for()
    page.get_by_test_id("spotify-connect").click()
    try:
        page.get_by_test_id("spotify-status").wait_for(timeout=5000)
    except Exception:
        page.screenshot(path=str(SHOTS / "debug-connect.png"), full_page=True)
        raise AssertionError(f"not connected; url={page.url}")
    assert "Connected" in page.get_by_test_id("spotify-status").inner_text()

    page.get_by_test_id("tab-playlists").click()
    page.get_by_test_id("spotify-link").fill("https://open.spotify.com/playlist/4YS9ct5n4NWBf1lFN6a2iv?si=1a778c2c6b69472f")
    page.get_by_test_id("spotify-import").click()
    page.get_by_test_id("import-msg").wait_for()
    msg = page.get_by_test_id("import-msg").inner_text()
    assert "Imported tch3 on Spotify: 61 tracks" in msg and "1 already in your library" in msg, msg
    page.screenshot(path=str(SHOTS / "7-spotify-imported.png"), full_page=True)
    assert errors == [], errors


@pytest.fixture
def upgrade_server(tmp_path):
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from djlib.ingest_csv import TrackRecord, save_playlist
    from djlib.library import mark_owned, playlist_tracks, sync_library
    from test_upgrades import RESULTS as UP_RESULTS, TRANSFERS as UP_TRANSFERS, mp3
    lib = tmp_path / "DJ"
    mp3(lib / "17102026" / "Funkhaus - Lumen.mp3", 300)
    mp3(lib / "shranz" / "HEMPTOTE - Selva.mp3", 200)
    S = session_factory(tmp_path / "t.db")
    with S() as s:
        sync_library(s, lib)
        pl, _ = save_playlist(s, "csv", "p.csv", "set", [TrackRecord("Funkhaus", "Lumen"), TrackRecord("HEMPTOTE", "Selva")])
        mark_owned(s, playlist_tracks(s, pl.id), lib)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "rejected",
                 sockseek_factory=lambda wd: FakeSockseek(wd, UP_RESULTS, UP_TRANSFERS), nicotine_check=lambda: False,
                 background=False, port=port, replaced_dir=tmp_path / "replaced")
    srv = uvicorn.Server(uvicorn.Config(create_app(S, cfg), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", lib, tmp_path
    srv.should_exit = True


def test_lossless_upgrade_from_the_ui(upgrade_server, page):
    url, lib, tmp = upgrade_server
    SHOTS.mkdir(exist_ok=True)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{url}/#upgrades")
    table = page.get_by_test_id("upgrade-table")
    table.locator("tbody tr").first.wait_for()
    assert table.locator("tbody tr").count() == 2
    page.screenshot(path=str(SHOTS / "9-upgrades.png"), full_page=True)

    page.get_by_test_id("upgrade-all").check()
    assert page.get_by_test_id("upgrade-run").inner_text() == "Upgrade 2 selected"
    page.get_by_test_id("upgrade-run").click()
    page.get_by_test_id("upgrade-msg").wait_for()

    page.get_by_test_id("tab-jobs").click()
    page.wait_for_function("document.body.innerText.includes('1 upgraded')", timeout=10000)
    assert "UPGRADED Funkhaus - Lumen" in page.content()
    page.screenshot(path=str(SHOTS / "10-upgrade-job.png"), full_page=True)

    assert (lib / "17102026" / "05-funkhaus-lumen.wav").exists()
    assert (tmp / "replaced" / "17102026" / "Funkhaus - Lumen.mp3").exists()

    # Lumen is gone from the list; Selva was checked, so it is hidden until "show already checked".
    page.get_by_test_id("tab-upgrades").click()
    page.get_by_text("Nothing to upgrade that hasn't been checked already").wait_for()
    page.get_by_text("Show already checked (1)").click()
    page.get_by_text("no search results").wait_for()
    assert errors == [], errors


@pytest.fixture
def dupes_server(tmp_path):
    from djlib.fingerprint import fpcalc_path
    if not fpcalc_path():
        pytest.skip("needs fpcalc")
    from test_fingerprint import song, to_mp3, write_wav
    lib = tmp_path / "DJ"
    write_wav(lib / "09082026" / "IN FURCHT - FLY LIK3.wav", song(1))
    to_mp3(lib / "09082026" / "IN FURCHT - FLY LIK3.wav", lib / "16082026" / "A-05 untitled [ms].mp3")
    write_wav(lib / "techno" / "Other Artist - Different Song.wav", song(2))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "rejected",
                 background=False, port=port, duplicates_dir=tmp_path / "dupes")
    app = create_app(session_factory(tmp_path / "t.db"), cfg)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", lib, tmp_path
    srv.should_exit = True


def test_duplicates_from_the_ui(dupes_server, page):
    url, lib, tmp = dupes_server
    SHOTS.mkdir(exist_ok=True)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.request.post(f"{url}/api/library/scan")
    page.goto(f"{url}/#duplicates")
    page.get_by_text("Not checked yet.").wait_for()
    page.get_by_test_id("dupes-scan").click()
    page.get_by_test_id("dup-group").wait_for(timeout=15000)
    group = page.get_by_test_id("dup-group")
    assert group.locator("tbody tr").count() == 2
    assert "09082026/IN FURCHT - FLY LIK3.wav" in group.locator("tbody tr").first.inner_text()  # lossless kept by default
    btn = page.get_by_test_id("dup-resolve")
    assert btn.inner_text() == "Keep WAV in 09082026, move the other copy out"
    page.screenshot(path=str(SHOTS / "11-duplicates.png"), full_page=True)

    btn.click()
    page.get_by_test_id("dupes-none").wait_for()
    assert "Moved 1 other copy" in page.get_by_test_id("dupes-msg").inner_text()
    assert (tmp / "dupes" / "16082026" / "A-05 untitled [ms].mp3").exists()
    assert (lib / "09082026" / "IN FURCHT - FLY LIK3.wav").exists()
    assert errors == [], errors
