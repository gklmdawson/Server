"""Processor interface and shared types.

The split of responsibilities with the agent's JobRunner:

  Runner owns   : local state file, launching the payload with output
                  redirected to a log file, cancel/timeout enforcement,
                  failure bundles, all reporting to the coordinator.
  Processor owns: readiness (is this machine currently able to run my jobs
                  at all — e.g. app already open by a human), job preflight
                  (paths in the parameters exist), the payload command line,
                  progress polling, post-exit completion detection (e.g.
                  waiting for Terra's report.md), and output validation.

Job completion is decided by validate_outputs(), never by exit code alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Optional


class ProcessorError(Exception):
    pass


@dataclass
class JobContext:
    """Everything a processor needs to know about the job being run."""
    job_uuid: str
    job_type: str
    parameters: dict[str, Any]
    work_dir: Path                      # per-job dir under the agent work root
    log_path: Path                      # payload stdout/stderr log file
    max_runtime_seconds: float = 24 * 3600.0
    pid: Optional[int] = None
    exit_code: Optional[int] = None     # None when unknown (recovered orphan)
    started_wall: Optional[float] = None  # time.time() at launch (validators
                                          # use it for output-freshness checks)


@dataclass
class Progress:
    percent: Optional[float] = None
    stage: str = ""
    message: str = ""


@dataclass
class Validation:
    ok: bool
    outputs: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class Processor:
    """Base class. Subclasses set job_types and implement build_command +
    validate_outputs; the other hooks have safe defaults."""

    job_types: ClassVar[set[str]] = set()
    requires_desktop: ClassVar[bool] = False   # GUI automation payloads only
    version: ClassVar[str] = "0"

    def __init__(self, agent_cfg) -> None:
        self.cfg = agent_cfg

    def ready(self) -> list[str]:
        """Machine-level readiness, checked BEFORE requesting work. A non-empty
        list pauses assignment (accepting_jobs=false) rather than failing jobs
        — e.g. 'PIX4Dmatic already running (in use by a person)'."""
        return []

    def preflight(self, ctx: JobContext) -> list[str]:
        """Job-level checks AFTER assignment (parameter paths exist, etc.).
        A non-empty list fails the job with PREFLIGHT_FAILED — these problems
        don't fix themselves and a human should see them."""
        return []

    def build_command(self, ctx: JobContext) -> list[str]:
        raise NotImplementedError

    def poll(self, ctx: JobContext, elapsed_seconds: float) -> Optional[Progress]:
        """Called every watchdog tick while the payload runs. Return None when
        there is nothing (new) to say."""
        return None

    def after_exit(self, ctx: JobContext, cancelled: "CancelCheck") -> None:
        """Post-exit completion wait (e.g. Terra LiDAR's report.md watch).
        Must poll `cancelled()` regularly and return promptly when it's true.
        Raise ProcessorError to fail the job."""
        return None

    def validate_outputs(self, ctx: JobContext) -> Validation:
        raise NotImplementedError


# Signature for the cancel probe passed into after_exit.
CancelCheck = Any  # Callable[[], bool] — kept loose for dataclass simplicity
