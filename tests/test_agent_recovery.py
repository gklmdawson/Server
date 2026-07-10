"""Crash-recovery: the state file must let a restarted agent either re-adopt a
still-running payload or judge a dead one by its outputs — never lose a job."""
import os
import subprocess
import sys
import time

import psutil
import pytest

from agent.config import AgentConfig
from agent.runner import JobRunner, write_json_atomic
from processors import build_registry
from tests.test_agent_runner import FakeClient


@pytest.fixture
def env(tmp_path):
    cfg = AgentConfig(node_name="T", work_root=str(tmp_path / "work"),
                      capabilities=["MOCK"])
    cfg.ensure_dirs()
    client = FakeClient()
    registry = build_registry(cfg, ["MOCK"])
    runner = JobRunner(cfg, client, registry)
    return cfg, client, runner


def _write_state(cfg, uuid, pid, create_time, params=None):
    work_dir = cfg.jobs_dir / uuid
    work_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(cfg.state_file, {
        "job_uuid": uuid, "job_type": "MOCK",
        "parameters": params or {}, "max_runtime_minutes": 5,
        "pid": pid, "pid_create_time": create_time,
        "started_at": "2026-07-10T12:00:00+00:00",
        "work_dir": str(work_dir), "log_path": str(work_dir / "payload.log"),
    })
    return work_dir


def test_dead_payload_with_valid_outputs_reports_succeeded(env):
    cfg, client, runner = env
    # A live pid with a WRONG create time == pid reuse == treated as dead.
    work_dir = _write_state(cfg, "job-r1", os.getpid(), 1.0)
    (work_dir / "mock_output.txt").write_text("output survived the crash\n")

    runner.recover_interrupted()

    kind, args, kwargs = client.last("succeeded")
    assert args[0] == "job-r1"
    assert "Recovered" in kwargs["message"]
    assert not cfg.state_file.exists()


def test_dead_payload_without_outputs_reports_failed_with_bundle(env):
    cfg, client, runner = env
    work_dir = _write_state(cfg, "job-r2", os.getpid(), 1.0)

    runner.recover_interrupted()

    kind, args, kwargs = client.last("failed")
    assert args[0] == "job-r2"
    assert args[2] == "AGENT_RESTARTED"
    assert (work_dir / "failure" / "job.json").exists()
    assert not cfg.state_file.exists()


def test_alive_payload_is_readopted_and_completes(env):
    cfg, client, runner = env
    work_dir = cfg.jobs_dir / "job-r3"
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / "mock_output.txt"

    # A real orphan-like payload: sleeps, then writes its output.
    proc = subprocess.Popen([
        sys.executable, "-c",
        "import sys, time, pathlib; time.sleep(1.0); "
        "pathlib.Path(sys.argv[1]).write_text('late output')",
        str(out),
    ])
    create_time = psutil.Process(proc.pid).create_time()
    _write_state(cfg, "job-r3", proc.pid, create_time,
                 params={"output_path": str(out)})

    runner.recover_interrupted()
    assert runner.busy, "agent should re-adopt the running payload"
    assert runner.active_jobs()[0].job_uuid == "job-r3"

    deadline = time.monotonic() + 20
    while runner.busy and time.monotonic() < deadline:
        time.sleep(0.1)
    proc.wait(timeout=5)

    kind, args, kwargs = client.last("succeeded")
    assert args[0] == "job-r3"
    assert not cfg.state_file.exists()


def test_corrupt_state_file_is_moved_aside(env):
    cfg, client, runner = env
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text("{not json", encoding="utf-8")

    runner.recover_interrupted()

    assert client.calls == []          # nothing to report
    assert not cfg.state_file.exists()
    assert cfg.state_file.with_suffix(".corrupt").exists()


def test_no_state_file_is_a_clean_start(env):
    cfg, client, runner = env
    runner.recover_interrupted()
    assert client.calls == []
    assert not runner.busy
