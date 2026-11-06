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
    assert (c.lib / "06112026" / "SOLYD - Hot Beats (Extended Mix).mp3").exists()

    detail = c.get(f"/api/playlists/{pl['id']}").json()
    assert detail["crate"] == "06112026"
    assert sorted(t["status"] for t in detail["tracks"]) == ["failed_no_results", "failed_transfer", "owned", "owned", "owned"]

    failed = c.get("/api/failed").json()
    assert {f["title"] for f in failed} == {"Obsessed", "Selva"}
    assert all(f["buy"]["bandcamp"].startswith("https://bandcamp.com/search?q=") for f in failed)

    lib = c.get("/api/library", params={"crate": "06112026"}).json()
    assert lib["total"] == 2 and {r["format"] for r in lib["rows"]} == {"mp3"}


def test_review_rejected_file_accept_files_it(client):
    c = client
    pl = upload(c)
    wait_job(c, c.post(f"/api/playlists/{pl['id']}/download", json={"crate": "06112026"}).json()["id"])
    items = c.get("/api/review").json()
    (rej,) = [i for i in items if i["kind"] == "rejected"]
    assert rej["title"] == "Obsessed" and "duration" in rej["reason"]
    r = c.post(f"/api/review/rejected/{rej['id']}/accept").json()
    assert r["filed"].endswith("06112026/KUSS & GRUV - Obsessed.flac")
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
