"""JobRunner: executes one assigned job at a time and never loses one.

Lifecycle of a normal run (worker thread):
  preflight -> write state file (atomic) -> launch payload with stdout/stderr
  redirected to a per-job log file (children never inherit our pipes — see the
  3DR deadlock note in DESIGN.md) -> report started -> watchdog loop (progress
  polling, cancel, max-runtime kill) -> processor.after_exit completion wait ->
  validate outputs -> report succeeded/failed -> clear state file.

Crash safety: the state file names the job, pid, and the pid's create time.
On startup recover_interrupted() either re-adopts a still-running payload
(watches the pid to completion) or judges the finished/dead one by its outputs
and reports the result. Terminal reports are retried with backoff; if the
coordinator can't be reached the state file stays put so the next agent start
reports it — a job is never silently dropped.

On failure a bundle (job params, payload log tail, screenshot on Windows) is
written under the job dir and its path is included in the failure report.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psutil

from agent import __version__ as AGENT_VERSION
from agent.client import CoordinatorClient, ReportConflict
from processors.base import JobContext, Processor, ProcessorError, Validation
from shared.schemas import ActiveJobInfo, JobAssignment

logger = logging.getLogger("agent.runner")

PID_CREATE_TIME_TOLERANCE = 2.0  # seconds; guards against pid reuse


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def kill_tree(pid: int) -> None:
    """Kill a process and all its descendants."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True)
        return
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = proc.children(recursive=True)
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        proc.kill()
    except psutil.NoSuchProcess:
        pass


def pid_alive(pid: Optional[int], expected_create_time: Optional[float]) -> bool:
    """True when `pid` is a live process AND matches the create time we
    recorded — a reused pid after reboot must not be mistaken for our payload,
    and a zombie (exited but unreaped, POSIX) counts as dead."""
    if not pid or not psutil.pid_exists(pid):
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if expected_create_time is not None:
            return abs(proc.create_time() - expected_create_time) < PID_CREATE_TIME_TOLERANCE
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Exists but not inspectable — assume alive and keep watching.
        return True


def capture_screenshot(dest: Path) -> bool:
    """Best-effort desktop screenshot (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(str(dest))
        return True
    except Exception as exc:  # never let diagnostics break failure handling
        logger.warning("Screenshot capture failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# JobRunner
# ---------------------------------------------------------------------------

class JobRunner:
    def __init__(self, cfg, client: CoordinatorClient,
                 registry: dict[str, Processor],
                 on_finished: Optional[Callable[[], None]] = None):
        self.cfg = cfg
        self.client = client
        self.registry = registry
        self.on_finished = on_finished
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._active: Optional[dict[str, Any]] = None  # {"uuid","percent","message"}

    # --- public surface -----------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def active_jobs(self) -> list[ActiveJobInfo]:
        with self._lock:
            if self._active is None or self._thread is None or not self._thread.is_alive():
                return []
            return [ActiveJobInfo(
                job_uuid=self._active["uuid"],
                progress_percent=self._active.get("percent"),
                progress_message=self._active.get("message", ""),
            )]

    def request_cancel(self, job_uuid: str) -> None:
        with self._lock:
            active = self._active
        if active and active["uuid"] == job_uuid:
            logger.info("Cancel requested for running job %s", job_uuid)
            self._cancel_event.set()

    def start(self, assignment: JobAssignment) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel_event = threading.Event()
            self._active = {"uuid": assignment.job_uuid, "percent": None, "message": ""}
            self._thread = threading.Thread(
                target=self._run_assignment, args=(assignment,),
                name=f"job-{assignment.job_uuid[:8]}", daemon=True,
            )
            self._thread.start()
        return True

    # --- context plumbing ----------------------------------------------------

    def _make_context(self, job_uuid: str, job_type: str, parameters: dict,
                      max_runtime_minutes: float) -> JobContext:
        work_dir = self.cfg.jobs_dir / job_uuid
        work_dir.mkdir(parents=True, exist_ok=True)
        return JobContext(
            job_uuid=job_uuid,
            job_type=job_type,
            parameters=parameters or {},
            work_dir=work_dir,
            log_path=work_dir / "payload.log",
            max_runtime_seconds=max(float(max_runtime_minutes), 0.01) * 60.0,
        )

    def _set_progress(self, percent: Optional[float], message: str) -> None:
        with self._lock:
            if self._active is not None:
                if percent is not None:
                    self._active["percent"] = percent
                if message:
                    self._active["message"] = message

    # --- reporting with retry -------------------------------------------------

    def _send_with_retry(self, what: str, send: Callable[[], None],
                         attempts: int = 30) -> bool:
        """Retry a report until it lands. 409 counts as landed (server-side
        state wins); 401 aborts (config problem a retry won't fix)."""
        delay = 2.0
        for attempt in range(attempts):
            try:
                send()
                return True
            except ReportConflict as exc:
                if exc.status_code == 409:
                    logger.warning("Coordinator refused %s report (%s) — accepting server state",
                                   what, exc.detail)
                    return True
                logger.error("Unauthorized sending %s report: %s", what, exc.detail)
                return False
            except Exception as exc:
                logger.warning("Could not send %s report (attempt %d): %s",
                               what, attempt + 1, exc)
                time.sleep(min(delay, 60.0))
                delay *= 1.6
        logger.error("Giving up sending %s report after %d attempts", what, attempts)
        return False

    def _report_progress_maybe(self, ctx: JobContext, percent: Optional[float],
                               stage: str, message: str,
                               state: dict[str, Any]) -> None:
        now = time.monotonic()
        changed = (
            (percent is not None and (state["percent"] is None
                                      or abs(percent - state["percent"]) >= 1.0))
            or (message and message != state["message"])
        )
        if changed and now - state["last_sent"] >= 4.0:
            try:
                self.client.report_progress(ctx.job_uuid, percent, stage, message)
                state.update(percent=percent, message=message, last_sent=now)
            except Exception as exc:
                logger.debug("Progress report failed (non-fatal): %s", exc)

    # --- failure bundle ---------------------------------------------------------

    def _make_failure_bundle(self, ctx: JobContext, reason: str) -> str:
        try:
            bundle = ctx.work_dir / "failure"
            bundle.mkdir(parents=True, exist_ok=True)
            write_json_atomic(bundle / "job.json", {
                "job_uuid": ctx.job_uuid, "job_type": ctx.job_type,
                "parameters": ctx.parameters, "reason": reason,
                "exit_code": ctx.exit_code, "pid": ctx.pid,
                "captured_at": utcnow_iso(), "agent_version": AGENT_VERSION,
            })
            for log in sorted(ctx.work_dir.glob("*.log")):
                tail = log.read_text(encoding="utf-8", errors="replace")[-64_000:]
                (bundle / f"{log.stem}_tail.txt").write_text(tail, encoding="utf-8")
            capture_screenshot(bundle / "screenshot.png")
            return str(bundle)
        except Exception as exc:
            logger.warning("Failure-bundle creation failed: %s", exc)
            return ""

    # --- main execution path ------------------------------------------------------

    def _run_assignment(self, assignment: JobAssignment) -> None:
        try:
            ctx = self._make_context(assignment.job_uuid, assignment.job_type,
                                     assignment.parameters,
                                     assignment.max_runtime_minutes)
            processor = self.registry.get(assignment.job_type)
            if processor is None:
                self._send_with_retry("failed", lambda: self.client.report_failed(
                    ctx.job_uuid, None, "NO_PROCESSOR",
                    f"Agent has no processor for job type {assignment.job_type}"))
                return
            self._execute(ctx, processor)
        except Exception:
            logger.exception("Unhandled error running job %s", assignment.job_uuid)
        finally:
            with self._lock:
                self._active = None
            if self.on_finished:
                try:
                    self.on_finished()
                except Exception:
                    pass

    def _execute(self, ctx: JobContext, processor: Processor) -> None:
        """Run one job to a terminal report. Testable directly with a custom
        JobContext (e.g. a short max_runtime_seconds)."""
        errors = processor.preflight(ctx)
        if errors:
            logger.error("Preflight failed for %s: %s", ctx.job_uuid, errors)
            self._send_with_retry("failed", lambda: self.client.report_failed(
                ctx.job_uuid, None, "PREFLIGHT_FAILED", "; ".join(errors)))
            return

        try:
            cmd = processor.build_command(ctx)
        except Exception as exc:
            self._send_with_retry("failed", lambda: self.client.report_failed(
                ctx.job_uuid, None, "BAD_PARAMETERS", f"build_command failed: {exc}"))
            return

        logger.info("Launching %s job %s: %s", ctx.job_type, ctx.job_uuid, cmd[0])
        try:
            log_file = open(ctx.log_path, "ab")
        except OSError as exc:
            self._send_with_retry("failed", lambda: self.client.report_failed(
                ctx.job_uuid, None, "WORKDIR_ERROR", f"cannot open payload log: {exc}"))
            return

        with log_file:
            try:
                proc = subprocess.Popen(
                    cmd, stdout=log_file, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, cwd=str(ctx.work_dir),
                )
            except Exception as exc:
                artifacts = self._make_failure_bundle(ctx, f"launch failed: {exc}")
                self._send_with_retry("failed", lambda: self.client.report_failed(
                    ctx.job_uuid, None, "LAUNCH_FAILED", str(exc), artifacts))
                return

            ctx.pid = proc.pid
            ctx.started_wall = time.time()
            try:
                create_time = psutil.Process(proc.pid).create_time()
            except psutil.NoSuchProcess:
                create_time = None
            write_json_atomic(self.cfg.state_file, {
                "job_uuid": ctx.job_uuid, "job_type": ctx.job_type,
                "parameters": ctx.parameters,
                "max_runtime_minutes": ctx.max_runtime_seconds / 60.0,
                "pid": ctx.pid, "pid_create_time": create_time,
                "started_at": utcnow_iso(),
                "work_dir": str(ctx.work_dir), "log_path": str(ctx.log_path),
                "agent_version": AGENT_VERSION,
            })

            self._send_with_retry("started", lambda: self.client.report_started(
                ctx.job_uuid, pid=ctx.pid, processor_version=processor.version,
                agent_version=AGENT_VERSION))

            # --- watchdog loop ---
            started = time.monotonic()
            progress_state = {"percent": None, "message": "", "last_sent": 0.0}
            while True:
                rc = proc.poll()
                if rc is not None:
                    ctx.exit_code = rc
                    break

                if self._cancel_event.is_set():
                    logger.info("Killing job %s (cancel requested)", ctx.job_uuid)
                    kill_tree(proc.pid)
                    proc.wait(timeout=30)
                    self._send_with_retry("cancelled", lambda: self.client.report_cancelled(
                        ctx.job_uuid, "Cancelled by coordinator; process tree killed"))
                    self._clear_state()
                    return

                elapsed = time.monotonic() - started
                if elapsed > ctx.max_runtime_seconds:
                    logger.error("Job %s exceeded max runtime (%.0fs); killing",
                                 ctx.job_uuid, ctx.max_runtime_seconds)
                    kill_tree(proc.pid)
                    proc.wait(timeout=30)
                    ctx.exit_code = proc.returncode
                    artifacts = self._make_failure_bundle(ctx, "max runtime exceeded")
                    self._send_with_retry("failed", lambda: self.client.report_failed(
                        ctx.job_uuid, ctx.exit_code, "TIMEOUT",
                        f"Exceeded max runtime of {ctx.max_runtime_seconds / 60:.0f} minutes",
                        artifacts))
                    self._clear_state()
                    return

                progress = processor.poll(ctx, elapsed)
                if progress is not None:
                    self._set_progress(progress.percent, progress.message)
                    self._report_progress_maybe(ctx, progress.percent,
                                                progress.stage, progress.message,
                                                progress_state)
                time.sleep(0.5)

        self._finish(ctx, processor)

    # --- completion / validation / terminal reports ----------------------------

    def _exit_ok(self, processor: Processor, ctx: JobContext) -> bool:
        return ctx.exit_code in (0, None)

    def _finish(self, ctx: JobContext, processor: Processor) -> None:
        if not self._exit_ok(processor, ctx):
            artifacts = self._make_failure_bundle(ctx, f"exit code {ctx.exit_code}")
            if self._send_with_retry("failed", lambda: self.client.report_failed(
                    ctx.job_uuid, ctx.exit_code, "NONZERO_EXIT",
                    f"Payload exited with code {ctx.exit_code}", artifacts)):
                self._clear_state()
            return

        try:
            processor.after_exit(ctx, self._cancel_event.is_set)
        except ProcessorError as exc:
            artifacts = self._make_failure_bundle(ctx, f"after_exit: {exc}")
            if self._send_with_retry("failed", lambda: self.client.report_failed(
                    ctx.job_uuid, ctx.exit_code, "COMPLETION_WAIT_FAILED",
                    str(exc), artifacts)):
                self._clear_state()
            return

        if self._cancel_event.is_set():
            self._send_with_retry("cancelled", lambda: self.client.report_cancelled(
                ctx.job_uuid, "Cancelled during completion wait"))
            self._clear_state()
            return

        try:
            validation: Validation = processor.validate_outputs(ctx)
        except Exception as exc:
            validation = Validation(ok=False, errors=[f"validation crashed: {exc}"])

        if validation.ok:
            if self._send_with_retry("succeeded", lambda: self.client.report_succeeded(
                    ctx.job_uuid, ctx.exit_code,
                    message="; ".join(validation.errors) or "outputs validated",
                    outputs=validation.outputs, validation=validation.summary)):
                self._clear_state()
        else:
            artifacts = self._make_failure_bundle(
                ctx, "validation failed: " + "; ".join(validation.errors))
            if self._send_with_retry("failed", lambda: self.client.report_failed(
                    ctx.job_uuid, ctx.exit_code, "VALIDATION_FAILED",
                    "; ".join(validation.errors) or "output validation failed",
                    artifacts)):
                self._clear_state()

    def _clear_state(self) -> None:
        try:
            self.cfg.state_file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove state file: %s", exc)

    # --- startup recovery ---------------------------------------------------------

    def read_state(self) -> Optional[dict[str, Any]]:
        try:
            return json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.error("State file unreadable (%s) — moving aside", exc)
            try:
                os.replace(self.cfg.state_file,
                           self.cfg.state_file.with_suffix(".corrupt"))
            except OSError:
                pass
            return None

    def recover_interrupted(self) -> None:
        """Called once at startup, before the sync loop."""
        state = self.read_state()
        if state is None:
            return
        job_uuid = state.get("job_uuid", "?")
        processor = self.registry.get(state.get("job_type", ""))
        logger.warning("Found interrupted job %s (%s) in state file",
                       job_uuid, state.get("job_type"))

        ctx = self._make_context(job_uuid, state.get("job_type", ""),
                                 state.get("parameters") or {},
                                 float(state.get("max_runtime_minutes") or 1440))
        ctx.pid = state.get("pid")
        try:
            ctx.started_wall = datetime.fromisoformat(state["started_at"]).timestamp()
        except (KeyError, ValueError):
            ctx.started_wall = None

        if processor is None:
            self._send_with_retry("failed", lambda: self.client.report_failed(
                job_uuid, None, "AGENT_RESTARTED",
                "Agent restarted and no processor exists for this job type"))
            self._clear_state()
            return

        if pid_alive(state.get("pid"), state.get("pid_create_time")):
            logger.info("Payload pid %s is still running — re-adopting job %s",
                        state["pid"], job_uuid)
            with self._lock:
                self._cancel_event = threading.Event()
                self._active = {"uuid": job_uuid, "percent": None,
                                "message": "re-adopted after agent restart"}
                self._thread = threading.Thread(
                    target=self._watch_adopted, args=(ctx, processor),
                    name=f"adopt-{job_uuid[:8]}", daemon=True)
                self._thread.start()
            return

        logger.info("Payload for %s is gone — judging it by its outputs", job_uuid)
        ctx.exit_code = None
        try:
            validation = processor.validate_outputs(ctx)
        except Exception as exc:
            validation = Validation(ok=False, errors=[f"validation crashed: {exc}"])
        if validation.ok:
            if self._send_with_retry("succeeded", lambda: self.client.report_succeeded(
                    ctx.job_uuid, None,
                    message="Recovered after agent restart; outputs validated",
                    outputs=validation.outputs, validation=validation.summary)):
                self._clear_state()
        else:
            artifacts = self._make_failure_bundle(
                ctx, "agent restarted; outputs not valid: " + "; ".join(validation.errors))
            if self._send_with_retry("failed", lambda: self.client.report_failed(
                    ctx.job_uuid, None, "AGENT_RESTARTED",
                    "Agent/machine restarted mid-job and outputs did not validate: "
                    + "; ".join(validation.errors), artifacts)):
                self._clear_state()

    def _watch_adopted(self, ctx: JobContext, processor: Processor) -> None:
        """Watch a re-adopted payload pid (not our child) to completion."""
        try:
            deadline = time.monotonic() + ctx.max_runtime_seconds
            while pid_alive(ctx.pid, None):
                if self._cancel_event.is_set():
                    kill_tree(ctx.pid)  # type: ignore[arg-type]
                    self._send_with_retry("cancelled", lambda: self.client.report_cancelled(
                        ctx.job_uuid, "Cancelled (re-adopted job); process tree killed"))
                    self._clear_state()
                    return
                if time.monotonic() > deadline:
                    kill_tree(ctx.pid)  # type: ignore[arg-type]
                    artifacts = self._make_failure_bundle(ctx, "max runtime exceeded (adopted)")
                    self._send_with_retry("failed", lambda: self.client.report_failed(
                        ctx.job_uuid, None, "TIMEOUT",
                        "Exceeded max runtime (re-adopted after restart)", artifacts))
                    self._clear_state()
                    return
                time.sleep(1.0)
            ctx.exit_code = None  # orphan exit codes are unknowable
            self._finish(ctx, processor)
        except Exception:
            logger.exception("Error watching adopted job %s", ctx.job_uuid)
        finally:
            with self._lock:
                self._active = None
            if self.on_finished:
                try:
                    self.on_finished()
                except Exception:
                    pass

    # --- housekeeping ------------------------------------------------------------

    def cleanup_old_job_dirs(self) -> None:
        """Delete job dirs older than keep_job_dirs_days (skip any containing
        a .keep marker)."""
        cutoff = time.time() - self.cfg.keep_job_dirs_days * 86400
        try:
            entries = list(self.cfg.jobs_dir.iterdir())
        except FileNotFoundError:
            return
        state = self.read_state()
        active_uuid = state.get("job_uuid") if state else None
        for entry in entries:
            try:
                if not entry.is_dir() or entry.name == active_uuid:
                    continue
                if (entry / ".keep").exists():
                    continue
                if entry.stat().st_mtime < cutoff:
                    import shutil
                    shutil.rmtree(entry, ignore_errors=True)
                    logger.info("Cleaned old job dir %s", entry.name)
            except OSError:
                continue
