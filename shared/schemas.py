"""Shared enums, constants, and API schemas used by the coordinator, agents,
and the intake submission client.

Job types are plain strings on the wire and in the database — the coordinator
never interprets them, so new processors can be added without touching it.
The constants below are for the components that *do* know about specific
processors (agents, intake, tests).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
ACTIVE_STATUSES = {JobStatus.ASSIGNED, JobStatus.RUNNING}


class ProjectStatus(str, Enum):
    ACTIVE = "ACTIVE"
    QA = "QA"
    ARCHIVED = "ARCHIVED"
    CANCELLED = "CANCELLED"


# Known job types (informational — the coordinator accepts any string).
INTAKE = "INTAKE"
TERRA_PPK = "TERRA_PPK"
TERRA_LIDAR = "TERRA_LIDAR"
PIX4D_MATIC = "PIX4D_MATIC"
CYCLONE_CLASSIFY = "CYCLONE_CLASSIFY"
MOCK = "MOCK"


# ---------------------------------------------------------------------------
# Job event types (job_events.type values written by the coordinator)
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    RETRIED = "RETRIED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    REVIVED = "REVIVED"


# ---------------------------------------------------------------------------
# Agent <-> coordinator sync
# ---------------------------------------------------------------------------

class ActiveJobInfo(BaseModel):
    """The agent's view of a job it is currently running, sent with each sync."""
    job_uuid: str
    progress_percent: Optional[float] = None
    progress_message: str = ""


class SyncRequest(BaseModel):
    agent_version: str = ""
    computer_name: str = ""
    current_user: str = ""
    capabilities: list[str] = Field(default_factory=list)
    active_jobs: list[ActiveJobInfo] = Field(default_factory=list)
    # False while the agent's own preflight fails (locked desktop, app open by a
    # human, NAS unreachable) or it has been paused locally — the node stays
    # visible and heartbeating but is skipped for assignment.
    accepting_jobs: bool = True
    telemetry: dict[str, Any] = Field(default_factory=dict)


class JobAssignment(BaseModel):
    job_uuid: str
    job_type: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    max_runtime_minutes: int = 1440
    project_uuid: str = ""
    project_name: str = ""
    client: str = ""


class SyncResponse(BaseModel):
    node_name: str
    assign: Optional[JobAssignment] = None
    cancel_job_uuids: list[str] = Field(default_factory=list)
    enabled: bool = True
    drain: bool = False
    poll_after_seconds: int = 10


# ---------------------------------------------------------------------------
# Job report payloads (agent -> coordinator, all idempotent)
# ---------------------------------------------------------------------------

class StartedReport(BaseModel):
    pid: Optional[int] = None
    processor_version: str = ""
    agent_version: str = ""
    message: str = ""


class ProgressReport(BaseModel):
    progress_percent: Optional[float] = None
    stage: str = ""
    message: str = ""


class SucceededReport(BaseModel):
    exit_code: Optional[int] = 0
    message: str = ""
    outputs: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)


class FailedReport(BaseModel):
    exit_code: Optional[int] = None
    error_code: str = ""
    error_message: str = ""
    artifacts_path: str = ""


class CancelledReport(BaseModel):
    message: str = ""


# ---------------------------------------------------------------------------
# Intake / admin payloads
# ---------------------------------------------------------------------------

class ChainSpec(BaseModel):
    """One workflow chain to instantiate from a named template.

    `parameters` maps job_type -> parameters_json for that step; parameters are
    opaque to the coordinator and interpreted only by the processor.
    """
    template: str
    parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ProjectCreate(BaseModel):
    name: str
    client: str = ""
    project_number: str = ""
    sensor_type: str = ""
    root_path: str = ""
    date_folder: str = ""
    priority: int = 100
    metadata: dict[str, Any] = Field(default_factory=dict)
    chains: list[ChainSpec] = Field(default_factory=list)


class IntakeSubmit(BaseModel):
    """One flight's intake, submitted from the web form.

    Creates the project, one INTAKE job (copy + RINEX on the intake machine),
    and the selected processing chains gated on it. Paths must be visible to
    the agents (UNC or a share mapped identically on every machine).
    """
    root_path: str                       # projects root, e.g. Z:/Survey/Projects
    client: str
    project: str
    date: str                            # ddMonYYYY, e.g. 10Jul2026
    sensor_type: str                     # M3E | P1 | L2 | L3 | R3Pro | R3ProMobile
    source_folders: list[str]
    base_data_paths: list[str] = Field(default_factory=list)
    base_data_is_rinex: bool = False
    base_ecef_xyz: Optional[list[float]] = None      # corrected base position

    run_photo_chain: bool = False        # TERRA_PPK -> PIX4D_MATIC
    run_lidar_chain: bool = False        # TERRA_LIDAR -> CYCLONE_CLASSIFY
    gcp_path: str = ""                   # targets csv: LiDAR GCP / Pix4D TAT
    epsg_h: str = ""
    epsg_v: str = ""
    no_targets: bool = False
    classify_model: str = ""             # empty -> skip the Cyclone step
    priority: int = 100


class JobCreate(BaseModel):
    job_type: str
    project_uuid: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)  # job uuids
    priority: Optional[int] = None  # default: inherit project priority (or 100)
    max_runtime_minutes: Optional[int] = None  # default: per-type config
    max_retries: int = 0


class NodeCreate(BaseModel):
    node_name: str
    capabilities: list[str] = Field(default_factory=list)


class NodeCapabilityUpdate(BaseModel):
    """Coordinator-side capability policy for a node.

    `enabled=None` clears the restriction (everything the agent declares is
    allowed); a list restricts assignment to declared ∩ enabled. Job types the
    node doesn't declare may be included — they simply have no effect until an
    agent on that machine declares them.
    """
    enabled: Optional[list[str]] = None
