"""JobRunner unit tests: success, failure modes, timeout, cancel — using the
mock processor and a fake coordinator client."""
import time

import pytest

from agent.config import AgentConfig
from agent.runner import JobRunner
from processors import build_registry
from shared.schemas import JobAssignment


class FakeClient:
    """Records every report the runner sends."""
    def __init__(self):
        self.calls = []

    def _rec(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))

    def report_started(self, *a, **k): self._rec("started", a, k)
    def report_progress(self, *a, **k): self._rec("progress", a, k)
    def report_succeeded(self, *a, **k): self._rec("succeeded", a, k)
    def report_failed(self, *a, **k): self._rec("failed", a, k)
    def report_cancelled(self, *a, **k): self._rec("cancelled", a, k)

    def kinds(self):
        return [c[0] for c in self.calls]

    def last(self, kind):
        return next(c for c in reversed(self.calls) if c[0] == kind)


@pytest.fixture
def env(tmp_path):
    cfg = AgentConfig(node_name="T", work_root=str(tmp_path / "work"),
                      capabilities=["MOCK"])
    cfg.ensure_dirs()
    client = FakeClient()
    registry = build_registry(cfg, ["MOCK"])
    runner = JobRunner(cfg, client, registry)
    return cfg, client, registry, runner


def _ctx(runner, uuid="job-1", params=None, max_seconds=120.0):
    ctx = runner._make_context(uuid, "MOCK", params or {}, 60)
    ctx.max_runtime_seconds = max_seconds
    return ctx


def test_success_path(env):
    cfg, client, registry, runner = env
    ctx = _ctx(runner, params={"duration": 0.3})
    runner._execute(ctx, registry["MOCK"])

    kinds = client.kinds()
    assert kinds[0] == "started"
    assert kinds[-1] == "succeeded"
    _, args, kwargs = client.last("succeeded")
    assert args[0] == "job-1"
    assert kwargs["outputs"], "validated outputs should be reported"
    assert not cfg.state_file.exists(), "state file must be cleared after success"
    assert ctx.log_path.exists()


def test_nonzero_exit_fails_with_bundle(env):
    cfg, client, registry, runner = env
    ctx = _ctx(runner, params={"duration": 0.1, "fail": True})
    runner._execute(ctx, registry["MOCK"])

    _, args, _ = client.last("failed")
    assert args[1] == 1                      # exit code
    assert args[2] == "NONZERO_EXIT"
    bundle = args[4]
    assert bundle and (ctx.work_dir / "failure" / "job.json").exists()
    assert (ctx.work_dir / "failure" / "payload_tail.txt").exists()
    assert not cfg.state_file.exists()


def test_missing_output_fails_validation(env):
    cfg, client, registry, runner = env
    ctx = _ctx(runner, params={"duration": 0.1, "skip_output": True})
    runner._execute(ctx, registry["MOCK"])

    _, args, _ = client.last("failed")
    assert args[2] == "VALIDATION_FAILED"
    assert "missing" in args[3]


def test_timeout_kills_payload(env):
    cfg, client, registry, runner = env
    ctx = _ctx(runner, params={"duration": 30}, max_seconds=1.0)
    start = time.monotonic()
    runner._execute(ctx, registry["MOCK"])
    assert time.monotonic() - start < 15

    _, args, _ = client.last("failed")
    assert args[2] == "TIMEOUT"
    assert not cfg.state_file.exists()


def test_cancel_kills_payload(env):
    cfg, client, registry, runner = env
    assignment = JobAssignment(job_uuid="job-c", job_type="MOCK",
                               parameters={"duration": 30},
                               max_runtime_minutes=5)
    assert runner.start(assignment)
    # Wait for the payload to actually start (state file written).
    deadline = time.monotonic() + 10
    while not cfg.state_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cfg.state_file.exists()
    assert runner.busy
    assert runner.active_jobs()[0].job_uuid == "job-c"

    runner.request_cancel("job-c")
    runner._thread.join(timeout=15)
    assert not runner.busy
    assert "cancelled" in client.kinds()
    assert not cfg.state_file.exists()


def test_preflight_failure_reports_failed(env):
    cfg, client, registry, runner = env

    class PickyProcessor(type(registry["MOCK"])):
        def preflight(self, ctx):
            return ["source path unreachable"]

    picky = PickyProcessor(cfg)
    ctx = _ctx(runner)
    runner._execute(ctx, picky)
    _, args, _ = client.last("failed")
    assert args[2] == "PREFLIGHT_FAILED"
    assert "unreachable" in args[3]
    assert "started" not in client.kinds(), "payload must not launch after failed preflight"


def test_prepare_runs_before_launch(env):
    cfg, client, registry, runner = env
    marks = []

    class PreppingProcessor(type(registry["MOCK"])):
        def prepare(self, ctx, cancelled):
            marks.append("prepared")

    ctx = _ctx(runner, params={"duration": 0.2})
    runner._execute(ctx, PreppingProcessor(cfg))
    assert marks == ["prepared"]
    assert client.kinds()[0] == "started" and client.kinds()[-1] == "succeeded"


def test_prepare_failure_reports_failed_without_launch(env):
    cfg, client, registry, runner = env
    from processors.base import ProcessorError

    class FailingPrep(type(registry["MOCK"])):
        def prepare(self, ctx, cancelled):
            raise ProcessorError("scratch drive full")

    ctx = _ctx(runner)
    runner._execute(ctx, FailingPrep(cfg))
    _, args, _ = client.last("failed")
    assert args[2] == "PREPARE_FAILED"
    assert "scratch drive full" in args[3]
    assert "started" not in client.kinds(), "payload must not launch after failed prepare"


def test_state_file_present_while_running(env):
    cfg, client, registry, runner = env
    assignment = JobAssignment(job_uuid="job-s", job_type="MOCK",
                               parameters={"duration": 1.5},
                               max_runtime_minutes=5)
    runner.start(assignment)
    deadline = time.monotonic() + 10
    while not cfg.state_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    state = runner.read_state()
    assert state["job_uuid"] == "job-s"
    assert state["pid"] > 0
    assert state["pid_create_time"] is not None
    runner._thread.join(timeout=15)
    assert client.kinds()[-1] == "succeeded"
