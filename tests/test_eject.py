"""Media eject: device validation, the spool handshake, and the API endpoint
(gating, active-job guard, and the container<->watcher round-trip simulated by
writing the result the host watcher would)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coordinator import eject as eject_mod
from coordinator.main import create_app
from tests.conftest import make_config


# --- unit: validation + spool handshake ------------------------------------

@pytest.mark.parametrize("bad", ["", "a/b", "..", "\\x", "with\x00null", "/abs"])
def test_validate_device_rejects_bad(bad):
    with pytest.raises(eject_mod.EjectError):
        eject_mod.validate_device(bad)


def test_validate_device_accepts_leaf():
    assert eject_mod.validate_device(" sda1 ") == "sda1"


def test_write_and_poll_round_trip(tmp_path):
    spool = str(tmp_path / "eject")
    req_id = eject_mod.write_request(spool, "sda1")
    req_file = Path(spool) / "requests" / f"{req_id}.json"
    assert json.loads(req_file.read_text())["device"] == "sda1"

    # Stand in for the host watcher: write the result.
    (Path(spool) / "results" / f"{req_id}.json").write_text(
        json.dumps({"id": req_id, "ok": True, "message": "unmounted"}))
    res = eject_mod.poll_result(spool, req_id, timeout=2)
    assert res.ok and res.message == "unmounted" and not res.pending
    # Result consumed.
    assert not (Path(spool) / "results" / f"{req_id}.json").exists()


def test_poll_result_times_out_pending(tmp_path):
    spool = str(tmp_path / "eject")
    req_id = eject_mod.write_request(spool, "sda1")
    res = eject_mod.poll_result(spool, req_id, timeout=0.5)
    assert res.pending and not res.ok


# --- API endpoint ----------------------------------------------------------

@pytest.fixture
def card(tmp_path):
    """A fake ingest mount with one 'card' folder (sda1)."""
    ingest = tmp_path / "ingest"
    (ingest / "sda1" / "DCIM").mkdir(parents=True)
    return ingest


@pytest.fixture
def eject_client(tmp_path, card):
    cfg = make_config(
        tmp_path,
        eject_spool_dir=str(tmp_path / "spool"),
        browse_roots={
            "ingest": {"path": str(card), "display": "/mnt/ingest",
                       "ejectable": True},
        },
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def _answer_next_request(spool: Path, ok=True, message="unmounted"):
    """Background stand-in for the host watcher: answer the first request."""
    reqs = spool / "requests"
    results = spool / "results"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for r in reqs.glob("*.json"):
            data = json.loads(r.read_text())
            (results / f"{data['id']}.json").write_text(
                json.dumps({"id": data["id"], "ok": ok, "message": message}))
            r.unlink()
            return
        time.sleep(0.05)


def test_roots_report_ejectable(eject_client):
    roots = eject_client.get("/api/v1/browse").json()["roots"]
    assert roots[0]["ejectable"] is True


def test_eject_success(eject_client, tmp_path):
    spool = tmp_path / "spool"
    t = threading.Thread(target=_answer_next_request, args=(spool, True, "unmounted"))
    t.start()
    r = eject_client.post("/api/v1/intake/eject", json={"root": "ingest", "device": "sda1"})
    t.join()
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_eject_unknown_device_404(eject_client):
    r = eject_client.post("/api/v1/intake/eject", json={"root": "ingest", "device": "sdz9"})
    assert r.status_code == 404


def test_eject_rejects_non_ejectable_root(tmp_path):
    cfg = make_config(
        tmp_path,
        eject_spool_dir=str(tmp_path / "spool"),
        browse_roots={"3dData": {"path": str(tmp_path), "display": "/mnt/3dData"}},
    )
    with TestClient(create_app(cfg)) as c:
        r = c.post("/api/v1/intake/eject", json={"root": "3dData", "device": "x"})
    assert r.status_code == 400


def test_eject_disabled_when_unconfigured(tmp_path, card):
    cfg = make_config(
        tmp_path,
        browse_roots={"ingest": {"path": str(card), "display": "/mnt/ingest",
                                 "ejectable": True}},
    )
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/v1/browse").json()["roots"][0]["ejectable"] is False
        r = c.post("/api/v1/intake/eject", json={"root": "ingest", "device": "sda1"})
    assert r.status_code == 404


def test_eject_blocked_by_active_job(eject_client):
    # A running job reading the card must block the eject (409).
    job = eject_client.post("/api/v1/jobs", json={
        "job_type": "INTAKE_COPY",
        "parameters": {"source_folders": ["/mnt/ingest/sda1/DCIM"]},
    }).json()
    # Drive it to RUNNING via a node sync + started report.
    eject_client.post("/api/v1/nodes/NAS-COPY/sync", json={
        "capabilities": ["INTAKE_COPY"], "accepting_jobs": True})
    r = eject_client.post("/api/v1/intake/eject", json={"root": "ingest", "device": "sda1"})
    assert r.status_code == 409
    assert job["job_uuid"][:8] in r.json()["detail"]
