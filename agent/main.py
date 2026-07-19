"""Agent entry point: one sync loop that is also the heartbeat.

Run in development:  python -m agent.main --config config/agent.yaml
PyInstaller build:   py build.py agent  ->  DataIntakeAgent.exe
Deployed via Task Scheduler at logon of the processing account
(scripts/install_agent.ps1) so payload GUIs land on the visible desktop.

The loop: recover any interrupted job, then sync forever — each sync carries
telemetry + active-job progress and brings back an assignment and/or cancel
requests. While the coordinator is down the agent keeps watching its running
job and backs off (capped at 60s) without flooding logs.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time

from agent import __version__ as AGENT_VERSION
from agent.client import CoordinatorClient
from agent.config import AgentConfig, load_config
from agent.preflight import basic_telemetry, desktop_errors
from agent.runner import JobRunner
from processors import build_registry
from shared.schemas import SyncRequest

logger = logging.getLogger("agent")


def setup_logging(cfg: AgentConfig) -> None:
    cfg.ensure_dirs()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.handlers.RotatingFileHandler(
            cfg.logs_dir / "agent.log", maxBytes=5_000_000, backupCount=5,
            encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


class Agent:
    def __init__(self, cfg: AgentConfig, client: CoordinatorClient,
                 registry: dict) -> None:
        self.cfg = cfg
        self.client = client
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()   # set by runner on job completion
        self.runner = JobRunner(cfg, client, registry,
                                on_finished=self.wake_event.set)
        self.registry = registry
        self._preflight_reasons: list[str] = []

    # --- readiness -----------------------------------------------------------

    def compute_accepting(self) -> bool:
        """Environment preflight BEFORE requesting work. Failing checks pause
        assignment (visible on the dashboard) instead of failing jobs."""
        reasons: list[str] = []
        needs_desktop = any(p.requires_desktop for p in self.registry.values())
        if needs_desktop:
            reasons += desktop_errors(self.cfg.expected_resolution,
                                      self.cfg.require_dpi_150)
        seen: set[int] = set()
        for processor in self.registry.values():
            if id(processor) in seen:
                continue
            seen.add(id(processor))
            try:
                reasons += processor.ready()
            except Exception as exc:
                reasons.append(f"{type(processor).__name__}.ready() crashed: {exc}")
        if reasons != self._preflight_reasons:
            if reasons:
                logger.warning("Pausing assignment; preflight: %s", "; ".join(reasons))
            else:
                logger.info("Preflight clear — accepting jobs")
            self._preflight_reasons = reasons
        return not reasons

    def build_sync_request(self) -> SyncRequest:
        accepting = self.compute_accepting()
        telemetry = basic_telemetry(str(self.cfg.work_root_path))
        if self._preflight_reasons:
            telemetry["preflight"] = self._preflight_reasons
        return SyncRequest(
            agent_version=AGENT_VERSION,
            computer_name=_computer_name(),
            current_user=_current_user(),
            capabilities=self.cfg.capabilities,
            active_jobs=self.runner.active_jobs(),
            accepting_jobs=accepting,
            telemetry=telemetry,
        )

    # --- main loop -------------------------------------------------------------

    def run(self) -> None:
        logger.info("Agent %s starting: node=%s coordinator=%s capabilities=%s",
                    AGENT_VERSION, self.cfg.node_name, self.cfg.coordinator_url,
                    self.cfg.capabilities)
        self.runner.cleanup_old_job_dirs()
        self.runner.recover_interrupted()

        backoff = 5.0
        while not self.stop_event.is_set():
            try:
                resp = self.client.sync(self.cfg.node_name, self.build_sync_request())
                backoff = 5.0
            except Exception as exc:
                logger.warning("Sync failed (%s); retrying in %.0fs",
                               exc, min(backoff, 60.0))
                self._sleep(min(backoff, 60.0))
                backoff *= 1.7
                continue

            for job_uuid in resp.cancel_job_uuids:
                self.runner.request_cancel(job_uuid)

            if resp.assign is not None:
                if self.runner.start(resp.assign):
                    logger.info("Accepted %s job %s (project %s)",
                                resp.assign.job_type, resp.assign.job_uuid,
                                resp.assign.project_name or "—")
                else:
                    logger.warning("Coordinator assigned %s while busy — will "
                                   "reconcile on next sync", resp.assign.job_uuid)

            self._sleep(max(resp.poll_after_seconds, 1))

        logger.info("Agent stopping (job still running: %s)", self.runner.busy)

    def _sleep(self, seconds: float) -> None:
        # Wake early when a job finishes (or stop is requested) so the next
        # sync happens promptly instead of waiting out the poll interval.
        self.wake_event.clear()
        self.wake_event.wait(timeout=seconds)

    def request_stop(self, *_args) -> None:
        if self.runner.busy:
            logger.warning("Stop requested with a job RUNNING — the payload keeps "
                           "running; the state file will recover it on next start")
        self.stop_event.set()
        self.wake_event.set()


def _computer_name() -> str:
    import platform
    return platform.node()


def _current_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return ""


def run() -> None:
    parser = argparse.ArgumentParser(description="Data Intake Agent")
    parser.add_argument("--config", default=None, help="path to agent YAML config")
    parser.add_argument("--setup", action="store_true",
                        help="open the setup window to enter the coordinator URL "
                             "and node token, then exit")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Setup mode: a local window to enter the URL/token — runnable before the
    # box is fully configured (no node_name/token required yet).
    if args.setup:
        from agent.setup import run_setup
        run_setup(cfg, args.config)
        return

    if not cfg.node_name:
        raise SystemExit("agent config must set node_name (or run --setup)")
    if not cfg.token:
        raise SystemExit("no node token found — run 'DataIntakeAgent.exe --setup' "
                         "to enter it, or set DATA_INTAKE_NODE_TOKEN")
    setup_logging(cfg)

    registry = build_registry(cfg, cfg.capabilities)
    client = CoordinatorClient(cfg.coordinator_url, cfg.token,
                               timeout=cfg.request_timeout_seconds)
    agent = Agent(cfg, client, registry)

    signal.signal(signal.SIGINT, agent.request_stop)
    signal.signal(signal.SIGTERM, agent.request_stop)
    try:
        agent.run()
    finally:
        client.close()


if __name__ == "__main__":
    run()
