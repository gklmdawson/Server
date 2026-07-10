"""Full-stack tests: the real Agent loop / JobRunner talking to the real
coordinator app over HTTP (FastAPI TestClient injected as the transport)."""
import subprocess
import sys
import threading
import time

import psutil
import pytest
from fastapi.testclient import TestClient

from agent.client import CoordinatorClient
from agent.config import AgentConfig
from agent.main import Agent
from agent.runner import JobRunner, write_json_atomic
from coordinator.main import create_app
from processors import build_registry
from tests.conftest import make_config


@pytest.fixture
def stack(tmp_path):
    coord_cfg = make_config(tmp_path, poll_idle_seconds=1, poll_busy_seconds=1)
    app = create_app(coord_cfg)
    with TestClient(app) as http:
        agent_cfg = AgentConfig(node_name="E2E-01",
                                coordinator_url="http://testserver",
                                work_root=str(tmp_path / "agent"),
                                capabilities=["MOCK"])
        agent_cfg.ensure_dirs()
        client = CoordinatorClient("http://testserver", http=http)
        yield http, agent_cfg, client


def _make_job(http, params, max_runtime_minutes=5) -> str:
    r = http.post("/api/v1/jobs", json={
        "job_type": "MOCK", "parameters": params,
        "max_runtime_minutes": max_runtime_minutes,
    })
    assert r.status_code == 201, r.text
    return r.json()["job_uuid"]


def _job_status(http, uuid) -> str:
    return http.get(f"/api/v1/jobs/{uuid}").json()["status"]


def _wait_status(http, uuid, wanted, timeout=30) -> str:
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        status = _job_status(http, uuid)
        if status == wanted:
            return status
        time.sleep(0.2)
    return status


def test_agent_loop_runs_job_end_to_end(stack, tmp_path):
    http, agent_cfg, client = stack
    out = tmp_path / "e2e_output.txt"
    uuid = _make_job(http, {"duration": 1.0, "output_path": str(out)})

    agent = Agent(agent_cfg, client, build_registry(agent_cfg, ["MOCK"]))
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    try:
        assert _wait_status(http, uuid, "SUCCEEDED") == "SUCCEEDED"
        assert out.read_text().startswith("mock output")

        job = http.get(f"/api/v1/jobs/{uuid}").json()
        assert job["assigned_node"] == "E2E-01"
        types = [e["type"] for e in job["events"]]
        for expected in ("CREATED", "ASSIGNED", "STARTED", "SUCCEEDED"):
            assert expected in types

        nodes = http.get("/api/v1/nodes").json()["nodes"]
        assert nodes[0]["node_name"] == "E2E-01"
        assert nodes[0]["online"] is True
    finally:
        agent.request_stop()
        thread.join(timeout=10)


def test_agent_loop_honors_cancel(stack, tmp_path):
    http, agent_cfg, client = stack
    uuid = _make_job(http, {"duration": 60})

    agent = Agent(agent_cfg, client, build_registry(agent_cfg, ["MOCK"]))
    thread = threading.Thread(target=agent.run, daemon=True)
    thread.start()
    try:
        assert _wait_status(http, uuid, "RUNNING") == "RUNNING"
        r = http.post(f"/api/v1/jobs/{uuid}/cancel")
        assert r.status_code == 200
        assert _wait_status(http, uuid, "CANCELLED", timeout=30) == "CANCELLED"
    finally:
        agent.request_stop()
        thread.join(timeout=10)


def test_agent_crash_midjob_readopts_and_finishes(stack, tmp_path):
    """Simulates: agent process dies right after launching the payload; the
    payload keeps running; a fresh agent starts, finds the state file,
    re-adopts the live pid and completes the job. Coordinator never sees a
    duplicate assignment."""
    http, agent_cfg, client = stack
    out = tmp_path / "crash_output.txt"
    uuid = _make_job(http, {"duration": 2.0, "output_path": str(out)})

    # "Crashed agent": claim the job and launch the payload by hand, exactly
    # as the runner would, then vanish without watching it.
    r = http.post("/api/v1/nodes/E2E-01/sync", json={
        "agent_version": "crash-sim", "capabilities": ["MOCK"],
        "active_jobs": [], "accepting_jobs": True, "telemetry": {},
    })
    assign = r.json()["assign"]
    assert assign is not None and assign["job_uuid"] == uuid

    work_dir = agent_cfg.jobs_dir / uuid
    work_dir.mkdir(parents=True, exist_ok=True)
    payload = subprocess.Popen([
        sys.executable, "-c",
        "import sys, time, pathlib; time.sleep(2.0); "
        "pathlib.Path(sys.argv[1]).write_text('mock output\\n')",
        str(out),
    ])
    client.report_started(uuid, pid=payload.pid, agent_version="crash-sim")
    write_json_atomic(agent_cfg.state_file, {
        "job_uuid": uuid, "job_type": "MOCK",
        "parameters": {"duration": 2.0, "output_path": str(out)},
        "max_runtime_minutes": 5,
        "pid": payload.pid,
        "pid_create_time": psutil.Process(payload.pid).create_time(),
        "started_at": "2026-07-10T12:00:00+00:00",
        "work_dir": str(work_dir), "log_path": str(work_dir / "payload.log"),
    })
    assert _job_status(http, uuid) == "RUNNING"

    # "Restarted agent": fresh runner, same work root.
    runner = JobRunner(agent_cfg, client, build_registry(agent_cfg, ["MOCK"]))
    runner.recover_interrupted()
    assert runner.busy, "restarted agent must re-adopt the live payload"

    assert _wait_status(http, uuid, "SUCCEEDED") == "SUCCEEDED"
    payload.wait(timeout=5)
    assert not agent_cfg.state_file.exists()

    # Exactly one assignment ever happened — the job was never duplicated.
    events = http.get(f"/api/v1/jobs/{uuid}").json()["events"]
    assert sum(1 for e in events if e["type"] == "ASSIGNED") == 1
