"""The whole flow through HTTP, with a temp library and a fake sockseek (no network)."""
import time

import pytest
from fastapi.testclient import TestClient

from djlib.api import Config, create_app
from djlib.db import session_factory
from test_orchestrator import CSV, RESULTS, TRANSFERS, FakeSockseek, wav


@pytest.fixture
def client(tmp_path):
    lib = tmp_path / "DJ"
    wav(lib / "08092026" / "Kai Van Dongen - Flash (Original Mix).wav", 240)
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "rejected",
                 sockseek_factory=lambda wd: FakeSockseek(wd, RESULTS, TRANSFERS), nicotine_check=lambda: False,
                 background=False)
    app = create_app(session_factory(tmp_path / "t.db"), cfg)
    c = TestClient(app)
    c.post("/api/library/scan")
    c.lib, c.tmp = lib, tmp_path
    return c


def wait_job(c, jid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["state"] not in ("queued", "running"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def upload(c, name="tch3.csv"):
    r = c.post("/api/playlists/import", files={"file": (name, CSV.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def test_import_preview_download_and_failed_list(client):
    c = client
    pl = upload(c)
    assert pl["rows"] == 5

    prev = c.post(f"/api/playlists/{pl['id']}/download", json={"crate": "06112026", "dry_run": True}).json()
    done = wait_job(c, prev["id"])
    assert done["state"] == "finished" and done["result"]["owned"] == 1
    assert not (c.lib / "06112026").exists()  # preview touches nothing

    job = c.post(f"/api/playlists/{pl['id']}/download", json={"crate": "06112026"}).json()
    done = wait_job(c, job["id"])
    assert done["state"] == "finished", done["log"]
    assert done["result"] == {"filed": 2, "rejected": 1, "failed": 2, "owned": 1, "crate": "06112026"}
    assert any("FILED" in line for line in done["log"])
    assert (c.lib / "06112026" / "SOLYD - Hot Beats (Extended Mix).wav").exists()

    detail = c.get(f"/api/playlists/{pl['id']}").json()
    assert detail["crate"] == "06112026"
    assert sorted(t["status"] for t in detail["tracks"]) == ["failed_no_results", "failed_transfer", "owned", "owned", "owned"]

    failed = c.get("/api/failed").json()
    assert {f["title"] for f in failed} == {"Obsessed", "Selva"}
    assert all(f["buy"]["bandcamp"].startswith("https://bandcamp.com/search?q=") for f in failed)

    lib = c.get("/api/library", params={"crate": "06112026"}).json()
    assert lib["total"] == 2 and {r["format"] for r in lib["rows"]} == {"wav"}


def test_review_rejected_file_accept_files_it(client):
    c = client
    pl = upload(c)
    wait_job(c, c.post(f"/api/playlists/{pl['id']}/download", json={"crate": "06112026"}).json()["id"])
    items = c.get("/api/review").json()
    (rej,) = [i for i in items if i["kind"] == "rejected"]
    assert rej["title"] == "Obsessed" and "duration off by 171s" in rej["reason"]
    r = c.post(f"/api/review/rejected/{rej['id']}/accept").json()
    assert r["filed"].endswith("06112026/KUSS & GRUV - Obsessed.wav")
    assert c.get("/api/review").json() == []
    assert "Obsessed" not in {f["title"] for f in c.get("/api/failed").json()}


def test_only_one_job_at_a_time(client):
    c = client
    pl = upload(c)
    import threading
    gate = threading.Event()
    runner = c.app.state.runner
    runner.submit("download", "blocker", lambda log: gate.wait(5))
    r = c.post(f"/api/playlists/{pl['id']}/download", json={"crate": "x"})
    assert r.status_code == 409
    gate.set()
    runner.wait()


def test_preflight_failure_is_reported_not_crashed(client):
    c = client
    c.app.state.cfg.sockseek_factory = lambda wd: FakeSockseek(wd, {}, {}, test_results=0)
    pl = upload(c)
    done = wait_job(c, c.post(f"/api/playlists/{pl['id']}/download", json={}).json()["id"])
    assert done["state"] == "aborted" and "blocking Soulseek" in done["result"]["error"]


def test_bad_inputs_are_rejected(client):
    c = client
    assert c.post("/api/playlists/import", files={"file": ("x.txt", b"a", "text/plain")}).status_code == 400
    assert c.post("/api/playlists/import", files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")}).status_code == 400
    pl = upload(c)
    assert c.patch(f"/api/playlists/{pl['id']}", json={"crate": "../etc"}).status_code == 400
    assert c.put("/api/settings", json={"library_root": "/does/not/exist"}).status_code == 400
    assert c.get("/api/playlists/999").status_code == 404


def test_summary_and_crates(client):
    c = client
    s = c.get("/api/summary").json()
    assert s["files"] == 1 and s["current_job"] is None and len(s["today_crate"]) == 8
    assert c.get("/api/crates").json() == [{"name": "08092026", "kind": "dated", "files": 1}]


def test_library_shows_the_version_actually_owned(client):
    c = client
    pl = upload(c)
    wait_job(c, c.post(f"/api/playlists/{pl['id']}/download", json={"crate": "06112026"}).json()["id"])
    rows = c.get("/api/library", params={"q": "hot beats"}).json()["rows"]
    assert [r["version"] for r in rows] == ["Extended (listed: Short Mix)"]


# ---------- Spotify ----------
from urllib.parse import parse_qs, urlparse

import httpx

from djlib.spotify import MemoryStore
from test_spotify import PID, FakeSpotify


@pytest.fixture
def sclient(tmp_path):
    lib = tmp_path / "DJ"
    wav(lib / "08092026" / "KOBOSIL - Cryptic (Kobosil Hart Mix).wav", 302)
    fake = FakeSpotify()
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "rejected",
                 sockseek_factory=lambda wd: None, background=False, spotify_store=MemoryStore(),
                 spotify_http=httpx.Client(transport=httpx.MockTransport(fake)))
    c = TestClient(create_app(session_factory(tmp_path / "t.db"), cfg), follow_redirects=False)
    c.post("/api/library/scan")
    c.fake = fake
    return c


def test_spotify_connect_and_import(sclient):
    c = sclient
    assert c.put("/api/spotify", json={"client_id": "not-an-id"}).status_code == 400
    st = c.put("/api/spotify", json={"client_id": "0123456789abcdef0123456789abcdef"}).json()
    assert st["redirect_uri"] == "http://127.0.0.1:8765/api/spotify/callback" and st["connected"] is False

    r = c.get("/api/spotify/login")
    assert r.status_code == 307
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith("https://accounts.spotify.com/authorize")
    assert q["client_id"] == ["0123456789abcdef0123456789abcdef"] and q["code_challenge_method"] == ["S256"]

    cb = c.get("/api/spotify/callback", params={"state": q["state"][0], "code": "abc"})
    assert cb.headers["location"] == "/#settings?spotify=connected"
    assert c.get("/api/spotify").json()["connected"] is True

    imp = c.post("/api/spotify/import", json={"url": f"https://open.spotify.com/playlist/{PID}?si=1"}).json()
    assert imp["name"] == "tch3 on Spotify" and imp["rows"] == 61 and imp["skipped"] == 2
    assert imp["owned"] == 1  # Cryptic (Kobosil Hart Mix) is already in the library
    again = c.post("/api/spotify/import", json={"url": PID}).json()
    assert again["id"] == imp["id"] and again["new_tracks"] == 0


def test_spotify_callback_rejects_unknown_state(sclient):
    c = sclient
    c.put("/api/spotify", json={"client_id": "0123456789abcdef0123456789abcdef"})
    cb = c.get("/api/spotify/callback", params={"state": "forged", "code": "abc"})
    assert "spotify_error" in cb.headers["location"] and c.get("/api/spotify").json()["connected"] is False


def test_spotify_not_owned_playlist_message(sclient):
    c = sclient
    c.fake.owned = False
    c.put("/api/spotify", json={"client_id": "0123456789abcdef0123456789abcdef"})
    q = parse_qs(urlparse(c.get("/api/spotify/login").headers["location"]).query)
    c.get("/api/spotify/callback", params={"state": q["state"][0], "code": "abc"})
    r = c.post("/api/spotify/import", json={"url": PID})
    assert r.status_code == 400 and "you own or collaborate on" in r.json()["detail"]


def test_missing_sockseek_stops_with_clear_message(tmp_path):
    lib = tmp_path / "DJ"
    lib.mkdir()
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "r", background=False,
                 nicotine_check=lambda: False)
    c = TestClient(create_app(session_factory(tmp_path / "t.db"), cfg))
    assert c.put("/api/settings", json={"sockseek_path": str(tmp_path / "nope")}).status_code == 400
    exe = tmp_path / "fake-sockseek"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o644)
    assert c.put("/api/settings", json={"sockseek_path": str(exe)}).status_code == 400  # not executable
    exe.chmod(0o755)
    st = c.put("/api/settings", json={"sockseek_path": str(exe)}).json()
    assert st["sockseek_ok"] is True and st["sockseek_path"] == str(exe.resolve())
    exe.unlink()
    pl = upload(c)
    done = wait_job(c, c.post(f"/api/playlists/{pl['id']}/download", json={"crate": "x"}).json()["id"])
    assert done["state"] == "aborted" and "sockseek was not found" in done["result"]["error"]


def test_first_run_setup_installs_sockseek_and_saves_login(tmp_path):
    import io, os, stat, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo("sockseek_3.0.6_osx-arm64/sockseek")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    conf = tmp_path / "cfg" / "sockseek.conf"
    conf.parent.mkdir()
    conf.write_text("concurrent-jobs = 1\npassword = old\n")
    lib = tmp_path / "DJ"
    lib.mkdir()
    cfg = Config(library_root=lib, data_dir=tmp_path / "data", rejected_dir=tmp_path / "r", background=False,
                 sockseek_fetch=lambda url: buf.getvalue(), sockseek_conf=conf)
    c = TestClient(create_app(session_factory(tmp_path / "t.db"), cfg))

    st = c.post("/api/setup/sockseek").json()
    exe = tmp_path / "data" / "bin" / "sockseek"
    assert st["sockseek_ok"] and st["sockseek_path"] == str(exe) and os.access(exe, os.X_OK)

    assert c.put("/api/setup/soulseek", json={"username": "", "password": "x"}).status_code == 400
    st = c.put("/api/setup/soulseek", json={"username": "dj_me", "password": "s3cret!"}).json()
    assert st["soulseek_user"] == "dj_me" and "password" not in str(st)
    text = conf.read_text()
    assert "username = dj_me" in text and "password = s3cret!" in text and "old" not in text
    assert "concurrent-jobs = 1" in text  # other settings survive
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
