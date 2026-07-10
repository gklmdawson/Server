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
