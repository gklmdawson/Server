"""All coordinator HTTP routes (/api/v1).

Route groups:
  - Agent-facing: /nodes/{name}/sync + the five idempotent job report routes.
  - Intake/admin: projects, jobs, node administration.
  - Dashboard:    /status (the JSON the dashboard polls every 5 s).

Auth:
  - Agents send `Authorization: Bearer <node token>`; enforced when
    cfg.require_agent_tokens is true (nodes auto-register on first sync when
    it is false — dev / bring-up mode).
  - Admin routes check cfg.admin_token when one is configured.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coordinator import __version__
from coordinator.assign import (
    housekeeping,
    node_has_active_job,
    pick_and_claim_next_job,
    reconcile_reported_jobs,
    waiting_on,
)
from coordinator.config import CoordinatorConfig
from coordinator.db import Job, JobEvent, Node, Project, iso_z, log_event, utcnow
from coordinator.intake import (
    SENSORS as INTAKE_SENSORS,
    IntakeValidationError,
    build_job_specs,
)
from shared.schemas import (
    CancelledReport,
    EventType,
    FailedReport,
    IntakeSubmit,
    JobAssignment,
    JobCreate,
    JobStatus,
    NodeCapabilityUpdate,
    NodeCreate,
    ProgressReport,
    ProjectCreate,
    StartedReport,
    SucceededReport,
    SyncRequest,
    SyncResponse,
    TERMINAL_STATUSES,
)

logger = logging.getLogger("coordinator.api")
router = APIRouter(prefix="/api/v1")

TERMINAL = {s.value for s in TERMINAL_STATUSES}


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def get_cfg(request: Request) -> CoordinatorConfig:
    return request.app.state.cfg


def get_session(request: Request) -> Session:
    """Request-scoped session, committed by the middleware in main.py BEFORE
    the response reaches the client. (A yield-dependency commit runs after
    the response is sent on current FastAPI/Starlette — an agent reacting
    immediately to a sync assignment could then read the pre-commit state
    and get a bogus 409.)"""
    session = getattr(request.state, "db_session", None)
    if session is None:
        session = request.app.state.session_factory()
        request.state.db_session = session
    return session


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_admin(request: Request, cfg: CoordinatorConfig = Depends(get_cfg)) -> None:
    if not cfg.admin_token:
        return
    token = _bearer(request) or request.headers.get("x-admin-token", "")
    if not secrets.compare_digest(token, cfg.admin_token):
        raise HTTPException(status_code=401, detail="Admin token required")


def _authenticate_node(session: Session, cfg: CoordinatorConfig,
                       node_name: str, request: Request) -> Node:
    node = session.execute(
        select(Node).where(Node.node_name == node_name)
    ).scalar_one_or_none()
    if cfg.require_agent_tokens:
        if node is None:
            raise HTTPException(status_code=401, detail=f"Unknown node '{node_name}'")
        token = _bearer(request)
        if not node.token_hash or not token or not secrets.compare_digest(
            _hash_token(token), node.token_hash
        ):
            raise HTTPException(status_code=401, detail="Invalid node token")
    elif node is None:
        node = Node(node_name=node_name)
        session.add(node)
        session.flush()
        logger.info("Auto-registered node %s (require_agent_tokens=false)", node_name)
    return node


def _verify_report_auth(session: Session, cfg: CoordinatorConfig,
                        job: Job, request: Request) -> None:
    """Job reports must carry the token of the node the job is assigned to."""
    if not cfg.require_agent_tokens:
        return
    node = session.execute(
        select(Node).where(Node.node_name == job.assigned_node)
    ).scalar_one_or_none()
    token = _bearer(request)
    if node is None or not node.token_hash or not token or not secrets.compare_digest(
        _hash_token(token), node.token_hash
    ):
        raise HTTPException(status_code=401, detail="Invalid node token for this job")


def _get_job(session: Session, job_uuid: str) -> Job:
    job = session.execute(select(Job).where(Job.uuid == job_uuid)).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_uuid} not found")
    return job


def _get_project(session: Session, project_uuid: str) -> Project:
    project = session.execute(
        select(Project).where(Project.uuid == project_uuid)
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project {project_uuid} not found")
    return project


# ---------------------------------------------------------------------------
# Agent: sync
# ---------------------------------------------------------------------------

@router.post("/nodes/{node_name}/sync", response_model=SyncResponse)
def sync(node_name: str, body: SyncRequest, request: Request,
         session: Session = Depends(get_session),
         cfg: CoordinatorConfig = Depends(get_cfg)) -> SyncResponse:
    node = _authenticate_node(session, cfg, node_name, request)
    now = utcnow()

    node.last_sync_at = now
    node.accepting_jobs = body.accepting_jobs
    node.last_telemetry_json = body.telemetry or {}
    if body.agent_version:
        node.agent_version = body.agent_version
    if body.computer_name:
        node.computer_name = body.computer_name
    if body.current_user:
        node.current_user = body.current_user
    if body.capabilities:
        node.capabilities_json = body.capabilities

    housekeeping(session, cfg, now)
    reconcile_reported_jobs(
        session, node, [j.job_uuid for j in body.active_jobs], cfg, now
    )

    # Progress piggybacked on the sync (the agent also posts explicit
    # /progress reports; this keeps last_progress_at fresh between them).
    for aj in body.active_jobs:
        job = session.execute(select(Job).where(Job.uuid == aj.job_uuid)).scalar_one_or_none()
        if job is not None and job.status == JobStatus.RUNNING.value:
            if aj.progress_percent is not None:
                job.progress_percent = aj.progress_percent
            if aj.progress_message:
                job.progress_message = aj.progress_message

    busy = bool(body.active_jobs) or node_has_active_job(session, node.node_name)
    assignment: Optional[JobAssignment] = None
    if node.enabled and not node.draining and body.accepting_jobs and not busy:
        job = pick_and_claim_next_job(session, node, cfg)
        if job is not None:
            project = job.project
            assignment = JobAssignment(
                job_uuid=job.uuid,
                job_type=job.job_type,
                parameters=job.parameters_json or {},
                priority=job.priority,
                max_runtime_minutes=job.max_runtime_minutes,
                project_uuid=project.uuid if project else "",
                project_name=project.name if project else "",
                client=project.client if project else "",
            )
            busy = True

    cancel_uuids = session.execute(
        select(Job.uuid).where(
            Job.assigned_node == node.node_name,
            Job.cancel_requested.is_(True),
            Job.status.in_([
                JobStatus.ASSIGNED.value,
                JobStatus.RUNNING.value,
                JobStatus.NEEDS_ATTENTION.value,
            ]),
        )
    ).scalars().all()

    return SyncResponse(
        node_name=node.node_name,
        assign=assignment,
        cancel_job_uuids=list(cancel_uuids),
        enabled=node.enabled,
        drain=node.draining,
        poll_after_seconds=cfg.poll_busy_seconds if busy else cfg.poll_idle_seconds,
    )


# ---------------------------------------------------------------------------
# Agent: job reports (idempotent)
# ---------------------------------------------------------------------------

def _conflict(job: Job, action: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=f"Cannot report {action} for job {job.uuid} in status {job.status}",
    )


@router.post("/jobs/{job_uuid}/started")
def report_started(job_uuid: str, body: StartedReport, request: Request,
                   session: Session = Depends(get_session),
                   cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    _verify_report_auth(session, cfg, job, request)
    if job.status == JobStatus.RUNNING.value:
        return {"ok": True, "status": job.status, "note": "already running"}
    if job.status not in (JobStatus.ASSIGNED.value, JobStatus.NEEDS_ATTENTION.value):
        raise _conflict(job, "started")
    revived = job.status == JobStatus.NEEDS_ATTENTION.value
    now = utcnow()
    job.status = JobStatus.RUNNING.value
    job.started_at = job.started_at or now
    job.last_progress_at = now
    job.error_code = ""
    job.error_message = ""
    if body.processor_version:
        job.processor_version = body.processor_version
    if body.agent_version:
        job.agent_version = body.agent_version
    log_event(session, job,
              EventType.REVIVED.value if revived else EventType.STARTED.value,
              body.message or (f"pid {body.pid}" if body.pid else ""),
              details={"pid": body.pid} if body.pid else None,
              node_name=job.assigned_node)
    return {"ok": True, "status": job.status}


@router.post("/jobs/{job_uuid}/progress")
def report_progress(job_uuid: str, body: ProgressReport, request: Request,
                    session: Session = Depends(get_session),
                    cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    _verify_report_auth(session, cfg, job, request)
    if job.status in TERMINAL:
        # Late progress after completion is harmless — swallow it so a
        # retrying agent never errors out.
        return {"ok": True, "status": job.status, "note": "job already finished; ignored"}
    if job.status == JobStatus.ASSIGNED.value:
        raise _conflict(job, "progress (no start report yet)")
    revived = job.status == JobStatus.NEEDS_ATTENTION.value
    if revived:
        job.status = JobStatus.RUNNING.value
        job.error_code = ""
        job.error_message = ""
        log_event(session, job, EventType.REVIVED.value,
                  "Progress report received after node loss", node_name=job.assigned_node)
    now = utcnow()
    message_changed = bool(body.message) and body.message != job.progress_message
    if body.progress_percent is not None:
        job.progress_percent = body.progress_percent
    if body.message:
        job.progress_message = body.message
    job.last_progress_at = now
    if message_changed or body.stage:
        log_event(session, job, EventType.PROGRESS.value,
                  body.message or body.stage,
                  details={"percent": body.progress_percent, "stage": body.stage},
                  node_name=job.assigned_node)
    return {"ok": True, "status": job.status}


@router.post("/jobs/{job_uuid}/succeeded")
def report_succeeded(job_uuid: str, body: SucceededReport, request: Request,
                     session: Session = Depends(get_session),
                     cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    _verify_report_auth(session, cfg, job, request)
    if job.status == JobStatus.SUCCEEDED.value:
        return {"ok": True, "status": job.status, "note": "already succeeded"}
    if job.status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.QUEUED.value):
        raise _conflict(job, "succeeded")
    now = utcnow()
    job.status = JobStatus.SUCCEEDED.value
    job.finished_at = now
    job.exit_code = body.exit_code
    job.progress_percent = 100.0
    if body.message:
        job.progress_message = body.message
    job.cancel_requested = False
    log_event(session, job, EventType.SUCCEEDED.value, body.message,
              details={"outputs": body.outputs, "validation": body.validation,
                       "exit_code": body.exit_code},
              node_name=job.assigned_node)
    return {"ok": True, "status": job.status}


@router.post("/jobs/{job_uuid}/failed")
def report_failed(job_uuid: str, body: FailedReport, request: Request,
                  session: Session = Depends(get_session),
                  cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    _verify_report_auth(session, cfg, job, request)
    if job.status == JobStatus.FAILED.value:
        return {"ok": True, "status": job.status, "note": "already failed"}
    if job.status in (JobStatus.SUCCEEDED.value, JobStatus.CANCELLED.value, JobStatus.QUEUED.value):
        raise _conflict(job, "failed")
    now = utcnow()
    job.status = JobStatus.FAILED.value
    job.finished_at = now
    job.exit_code = body.exit_code
    job.error_code = body.error_code or "PROCESSOR_FAILED"
    job.error_message = body.error_message
    log_event(session, job, EventType.FAILED.value,
              body.error_message or body.error_code,
              details={"exit_code": body.exit_code, "error_code": body.error_code,
                       "artifacts_path": body.artifacts_path},
              node_name=job.assigned_node)
    return {"ok": True, "status": job.status}


@router.post("/jobs/{job_uuid}/cancelled")
def report_cancelled(job_uuid: str, body: CancelledReport, request: Request,
                     session: Session = Depends(get_session),
                     cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    _verify_report_auth(session, cfg, job, request)
    if job.status == JobStatus.CANCELLED.value:
        return {"ok": True, "status": job.status, "note": "already cancelled"}
    if job.status in (JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.QUEUED.value):
        raise _conflict(job, "cancelled")
    job.status = JobStatus.CANCELLED.value
    job.finished_at = utcnow()
    job.cancel_requested = False
    log_event(session, job, EventType.CANCELLED.value, body.message,
              node_name=job.assigned_node)
    return {"ok": True, "status": job.status}


# ---------------------------------------------------------------------------
# Projects and jobs (intake / admin)
# ---------------------------------------------------------------------------

def _create_job_row(session: Session, cfg: CoordinatorConfig, *,
                    job_type: str, project: Optional[Project],
                    parameters: dict, depends_on: list[str],
                    priority: Optional[int], max_runtime_minutes: Optional[int],
                    max_retries: int = 0) -> Job:
    job = Job(
        job_type=job_type,
        project_id=project.id if project else None,
        parameters_json=parameters or {},
        depends_on_json=depends_on or [],
        priority=priority if priority is not None else (project.priority if project else 100),
        max_runtime_minutes=(
            max_runtime_minutes if max_runtime_minutes is not None
            else cfg.max_runtime_for(job_type)
        ),
        max_retries=max_retries,
        created_at=utcnow(),
    )
    session.add(job)
    session.flush()
    log_event(session, job, EventType.CREATED.value,
              f"{job_type} created" + (f" for project {project.name}" if project else ""))
    return job


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, request: Request,
                   session: Session = Depends(get_session),
                   cfg: CoordinatorConfig = Depends(get_cfg),
                   _admin: None = Depends(require_admin)) -> dict[str, Any]:
    for chain in body.chains:
        if chain.template not in cfg.templates:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown template '{chain.template}' "
                       f"(available: {', '.join(sorted(cfg.templates))})",
            )
    project = Project(
        name=body.name, client=body.client, project_number=body.project_number,
        sensor_type=body.sensor_type, root_path=body.root_path,
        date_folder=body.date_folder, priority=body.priority,
        metadata_json=body.metadata, created_at=utcnow(),
    )
    session.add(project)
    session.flush()

    created_jobs: list[Job] = []
    for chain in body.chains:
        uuid_by_type: dict[str, str] = {}
        for step in cfg.templates[chain.template]:
            job_type = step["job_type"]
            dep_uuids = [
                uuid_by_type[t] for t in step.get("depends_on", []) if t in uuid_by_type
            ]
            job = _create_job_row(
                session, cfg,
                job_type=job_type, project=project,
                parameters=chain.parameters.get(job_type, {}),
                depends_on=dep_uuids, priority=None, max_runtime_minutes=None,
            )
            uuid_by_type[job_type] = job.uuid
            created_jobs.append(job)

    return {
        "project_uuid": project.uuid,
        "name": project.name,
        "jobs": [
            {"job_uuid": j.uuid, "job_type": j.job_type, "depends_on": j.depends_on_json}
            for j in created_jobs
        ],
    }


@router.post("/intake", status_code=201)
def submit_intake(body: IntakeSubmit, request: Request,
                  session: Session = Depends(get_session),
                  cfg: CoordinatorConfig = Depends(get_cfg),
                  _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """One web-form submission -> project + INTAKE job + selected chains,
    with all job parameters built server-side (coordinator/intake.py)."""
    try:
        specs = build_job_specs(body)
    except IntakeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    project = Project(
        name=body.project.strip(), client=body.client.strip(),
        sensor_type=body.sensor_type, root_path=body.root_path.strip(),
        date_folder=body.date.strip(), priority=body.priority,
        metadata_json={"submitted_by": "web_intake", "date": body.date.strip(),
                       "epsg_h": body.epsg_h, "epsg_v": body.epsg_v},
        created_at=utcnow(),
    )
    session.add(project)
    session.flush()

    created: list[Job] = []
    for spec in specs:
        job = _create_job_row(
            session, cfg,
            job_type=spec["job_type"], project=project,
            parameters=spec["parameters"],
            depends_on=[created[i].uuid for i in spec["depends_on"]],
            priority=None, max_runtime_minutes=None,
        )
        created.append(job)

    return {
        "project_uuid": project.uuid,
        "name": project.name,
        "jobs": [
            {"job_uuid": j.uuid, "job_type": j.job_type, "depends_on": j.depends_on_json}
            for j in created
        ],
    }


@router.get("/intake/options")
def intake_options(request: Request,
                   cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    """Form-population data: valid sensors and shop defaults from config."""
    return {"sensors": list(INTAKE_SENSORS), "defaults": cfg.intake_defaults or {}}


# ---------------------------------------------------------------------------
# Server-side file browser (the web form's Browse buttons)
# ---------------------------------------------------------------------------

MAX_BROWSE_ENTRIES = 5000
# Dotfiles plus the usual NAS housekeeping folders (@eaDir, #recycle, ~$…).
BROWSE_SKIP_PREFIXES = (".", "@", "#", "~$")


@router.get("/browse")
def browse(request: Request, root: Optional[str] = None, path: str = "",
           cfg: CoordinatorConfig = Depends(get_cfg),
           _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """List a folder under a configured browse root (cfg.browse_roots).

    Without `root`: the configured roots, so the UI knows what to offer.
    With `root` (+ optional slash-separated relative `path`): the folder's
    entries, plus the `display_path` (UNC form) the picker writes into job
    parameters. Browsing never leaves the configured root.
    """
    roots = cfg.browse_roots or {}
    if root is None:
        return {"roots": [
            {"label": label, "display": str(spec.get("display") or spec.get("path", ""))}
            for label, spec in roots.items() if spec.get("path")
        ]}

    spec = roots.get(root)
    if not spec or not spec.get("path"):
        raise HTTPException(status_code=404, detail=f"Unknown browse root '{root}'")

    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise HTTPException(status_code=400, detail="path may not contain '..'")
    base = os.path.realpath(str(spec["path"]))
    target = os.path.realpath(os.path.join(base, *parts))
    if target != base and not target.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="path escapes the browse root")
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail=f"Not a folder: {'/'.join(parts)}")

    display_root = str(spec.get("display") or spec["path"])
    if display_root.startswith("/"):
        sep, display_path = "/", str(PurePosixPath(display_root, *parts))
    else:
        sep, display_path = "\\", str(PureWindowsPath(display_root, *parts))
    # PureWindowsPath keeps a trailing sep on a bare UNC share root
    # (\\server\share\); drop it so picker joins don't double up. Keep it for
    # anchors that need it (C:\, /).
    stripped = display_path.rstrip(sep)
    if display_path.endswith(sep) and stripped and not stripped.endswith(":"):
        display_path = stripped

    entries: list[dict[str, Any]] = []
    truncated = False
    with os.scandir(target) as it:
        for entry in it:
            if entry.name.startswith(BROWSE_SKIP_PREFIXES):
                continue
            try:
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
            except OSError:
                continue
            entries.append({"name": entry.name, "dir": is_dir, "size": size})
            if len(entries) >= MAX_BROWSE_ENTRIES:
                truncated = True
                break
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))

    return {
        "root": root,
        "path": "/".join(parts),
        "parent": "/".join(parts[:-1]) if parts else None,
        "display_path": display_path,
        "sep": sep,
        "entries": entries,
        "truncated": truncated,
    }


@router.post("/jobs", status_code=201)
def create_job(body: JobCreate, request: Request,
               session: Session = Depends(get_session),
               cfg: CoordinatorConfig = Depends(get_cfg),
               _admin: None = Depends(require_admin)) -> dict[str, Any]:
    project = _get_project(session, body.project_uuid) if body.project_uuid else None
    if body.depends_on:
        found = session.execute(
            select(func.count(Job.id)).where(Job.uuid.in_(body.depends_on))
        ).scalar_one()
        if found != len(set(body.depends_on)):
            raise HTTPException(status_code=400, detail="Unknown job uuid in depends_on")
    job = _create_job_row(
        session, cfg,
        job_type=body.job_type, project=project, parameters=body.parameters,
        depends_on=body.depends_on, priority=body.priority,
        max_runtime_minutes=body.max_runtime_minutes, max_retries=body.max_retries,
    )
    return {"job_uuid": job.uuid, "job_type": job.job_type, "status": job.status}


def _job_summary(session: Session, job: Job, include_waiting: bool = False) -> dict[str, Any]:
    out = {
        "job_uuid": job.uuid,
        "job_type": job.job_type,
        "status": job.status,
        "priority": job.priority,
        "assigned_node": job.assigned_node,
        "progress_percent": job.progress_percent,
        "progress_message": job.progress_message,
        "cancel_requested": job.cancel_requested,
        "retry_count": job.retry_count,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": iso_z(job.created_at),
        "assigned_at": iso_z(job.assigned_at),
        "started_at": iso_z(job.started_at),
        "last_progress_at": iso_z(job.last_progress_at),
        "finished_at": iso_z(job.finished_at),
        "depends_on": job.depends_on_json or [],
        "project_uuid": job.project.uuid if job.project else None,
        "project_name": job.project.name if job.project else "",
    }
    if include_waiting and job.status == JobStatus.QUEUED.value:
        out["waiting_on"] = waiting_on(session, job)
    return out


@router.get("/jobs")
def list_jobs(request: Request, status: Optional[str] = None, node: Optional[str] = None,
              job_type: Optional[str] = None, project_uuid: Optional[str] = None,
              limit: int = 100,
              session: Session = Depends(get_session)) -> dict[str, Any]:
    q = select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(min(limit, 500))
    if status:
        q = q.where(Job.status == status)
    if node:
        q = q.where(Job.assigned_node == node)
    if job_type:
        q = q.where(Job.job_type == job_type)
    if project_uuid:
        project = _get_project(session, project_uuid)
        q = q.where(Job.project_id == project.id)
    jobs = session.execute(q).scalars().all()
    return {"jobs": [_job_summary(session, j, include_waiting=True) for j in jobs]}


@router.get("/jobs/{job_uuid}")
def get_job(job_uuid: str, request: Request,
            session: Session = Depends(get_session)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    events = session.execute(
        select(JobEvent).where(JobEvent.job_id == job.id)
        .order_by(JobEvent.ts.desc(), JobEvent.id.desc()).limit(200)
    ).scalars().all()
    out = _job_summary(session, job, include_waiting=True)
    out["parameters"] = job.parameters_json or {}
    out["exit_code"] = job.exit_code
    out["max_runtime_minutes"] = job.max_runtime_minutes
    out["events"] = [
        {"ts": iso_z(e.ts), "type": e.type, "message": e.message,
         "details": e.details_json, "node": e.node_name}
        for e in events
    ]
    return out


@router.get("/projects")
def list_projects(request: Request,
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    projects = session.execute(
        select(Project).order_by(Project.created_at.desc()).limit(200)
    ).scalars().all()
    out = []
    for p in projects:
        counts: dict[str, int] = {}
        for j in p.jobs:
            counts[j.status] = counts.get(j.status, 0) + 1
        out.append({
            "project_uuid": p.uuid, "name": p.name, "client": p.client,
            "sensor_type": p.sensor_type, "status": p.status,
            "priority": p.priority, "created_at": iso_z(p.created_at),
            "job_counts": counts,
        })
    return {"projects": out}


@router.get("/projects/{project_uuid}")
def get_project(project_uuid: str, request: Request,
                session: Session = Depends(get_session)) -> dict[str, Any]:
    p = _get_project(session, project_uuid)
    return {
        "project_uuid": p.uuid, "name": p.name, "client": p.client,
        "project_number": p.project_number, "sensor_type": p.sensor_type,
        "root_path": p.root_path, "date_folder": p.date_folder,
        "status": p.status, "priority": p.priority,
        "metadata": p.metadata_json, "created_at": iso_z(p.created_at),
        "jobs": [_job_summary(session, j, include_waiting=True) for j in p.jobs],
    }


# ---------------------------------------------------------------------------
# Admin: job actions
# ---------------------------------------------------------------------------

@router.post("/jobs/{job_uuid}/cancel")
def cancel_job(job_uuid: str, request: Request,
               session: Session = Depends(get_session),
               _admin: None = Depends(require_admin)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    if job.status in TERMINAL:
        raise _conflict(job, "cancel")
    if job.status in (JobStatus.QUEUED.value, JobStatus.NEEDS_ATTENTION.value):
        job.status = JobStatus.CANCELLED.value
        job.finished_at = utcnow()
        job.cancel_requested = False
        log_event(session, job, EventType.CANCELLED.value, "Cancelled before execution")
        return {"ok": True, "status": job.status}
    job.cancel_requested = True
    log_event(session, job, EventType.CANCEL_REQUESTED.value,
              f"Cancel requested; will be delivered to {job.assigned_node} on next sync")
    return {"ok": True, "status": job.status, "cancel_requested": True}


@router.post("/jobs/{job_uuid}/retry")
def retry_job(job_uuid: str, request: Request,
              session: Session = Depends(get_session),
              _admin: None = Depends(require_admin)) -> dict[str, Any]:
    job = _get_job(session, job_uuid)
    if job.status not in (JobStatus.FAILED.value, JobStatus.CANCELLED.value,
                          JobStatus.NEEDS_ATTENTION.value):
        raise _conflict(job, "retry")
    job.status = JobStatus.QUEUED.value
    job.retry_count += 1
    job.assign_attempts = 0
    job.assigned_node = ""
    job.assigned_at = None
    job.started_at = None
    job.last_progress_at = None
    job.finished_at = None
    job.exit_code = None
    job.error_code = ""
    job.error_message = ""
    job.progress_percent = None
    job.progress_message = ""
    job.cancel_requested = False
    log_event(session, job, EventType.RETRIED.value, f"Manual retry #{job.retry_count}")
    return {"ok": True, "status": job.status, "retry_count": job.retry_count}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@router.post("/nodes", status_code=201)
def create_node(body: NodeCreate, request: Request,
                session: Session = Depends(get_session),
                _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Create a node (or rotate an existing node's token). The token is
    returned exactly once — only its hash is stored."""
    token = secrets.token_urlsafe(32)
    node = session.execute(
        select(Node).where(Node.node_name == body.node_name)
    ).scalar_one_or_none()
    rotated = node is not None
    if node is None:
        node = Node(node_name=body.node_name)
        session.add(node)
    node.token_hash = _hash_token(token)
    if body.capabilities:
        node.capabilities_json = body.capabilities
    session.flush()
    return {"node_name": node.node_name, "token": token, "rotated": rotated}


def _node_summary(node: Node, cfg: CoordinatorConfig, now) -> dict[str, Any]:
    return {
        "node_name": node.node_name,
        "online": node.is_online(now, cfg.offline_after_seconds),
        "enabled": node.enabled,
        "draining": node.draining,
        "accepting_jobs": node.accepting_jobs,
        "capabilities": node.capabilities_json or [],
        "enabled_capabilities": node.enabled_capabilities_json,
        "effective_capabilities": node.effective_capabilities(),
        "agent_version": node.agent_version,
        "computer_name": node.computer_name,
        "current_user": node.current_user,
        "last_sync_at": iso_z(node.last_sync_at),
        "telemetry": node.last_telemetry_json or {},
    }


@router.get("/nodes")
def list_nodes(request: Request, session: Session = Depends(get_session),
               cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    now = utcnow()
    nodes = session.execute(select(Node).order_by(Node.node_name)).scalars().all()
    return {"nodes": [_node_summary(n, cfg, now) for n in nodes]}


def _set_node_flags(session: Session, node_name: str, **flags) -> Node:
    node = session.execute(
        select(Node).where(Node.node_name == node_name)
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_name} not found")
    for key, value in flags.items():
        setattr(node, key, value)
    return node


@router.post("/nodes/{node_name}/enable")
def enable_node(node_name: str, request: Request,
                session: Session = Depends(get_session),
                _admin: None = Depends(require_admin)) -> dict[str, Any]:
    node = _set_node_flags(session, node_name, enabled=True, draining=False)
    return {"ok": True, "node_name": node.node_name, "enabled": True, "draining": False}


@router.post("/nodes/{node_name}/disable")
def disable_node(node_name: str, request: Request,
                 session: Session = Depends(get_session),
                 _admin: None = Depends(require_admin)) -> dict[str, Any]:
    node = _set_node_flags(session, node_name, enabled=False)
    return {"ok": True, "node_name": node.node_name, "enabled": False}


@router.post("/nodes/{node_name}/drain")
def drain_node(node_name: str, request: Request,
               session: Session = Depends(get_session),
               _admin: None = Depends(require_admin)) -> dict[str, Any]:
    node = _set_node_flags(session, node_name, draining=True)
    return {"ok": True, "node_name": node.node_name, "draining": True}


@router.post("/nodes/{node_name}/capabilities")
def set_node_capabilities(node_name: str, body: NodeCapabilityUpdate, request: Request,
                          session: Session = Depends(get_session),
                          _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Set the coordinator-side capability policy for a node (what may be
    assigned). The agent keeps declaring what the machine *can* run; this
    restricts assignment to declared ∩ enabled. enabled=null clears it."""
    node = _set_node_flags(session, node_name,
                           enabled_capabilities_json=body.enabled)
    return {
        "ok": True,
        "node_name": node.node_name,
        "capabilities": node.capabilities_json or [],
        "enabled_capabilities": node.enabled_capabilities_json,
        "effective_capabilities": node.effective_capabilities(),
    }


# ---------------------------------------------------------------------------
# Dashboard status + health
# ---------------------------------------------------------------------------

@router.get("/status")
def status(request: Request, session: Session = Depends(get_session),
           cfg: CoordinatorConfig = Depends(get_cfg)) -> dict[str, Any]:
    now = utcnow()
    housekeeping(session, cfg, now)

    nodes = session.execute(select(Node).order_by(Node.node_name)).scalars().all()
    node_rows = []
    for n in nodes:
        row = _node_summary(n, cfg, now)
        active = session.execute(
            select(Job).where(
                Job.assigned_node == n.node_name,
                Job.status.in_([JobStatus.ASSIGNED.value, JobStatus.RUNNING.value]),
            ).limit(1)
        ).scalar_one_or_none()
        row["active_job"] = _job_summary(session, active) if active else None
        node_rows.append(row)

    queued = session.execute(
        select(Job)
        .where(Job.status.in_([JobStatus.QUEUED.value, JobStatus.ASSIGNED.value]))
        .order_by(Job.priority.desc(), Job.created_at.asc())
        .limit(100)
    ).scalars().all()

    running = session.execute(
        select(Job).where(Job.status == JobStatus.RUNNING.value)
        .order_by(Job.started_at.asc())
    ).scalars().all()
    running_rows = []
    for j in running:
        row = _job_summary(session, j)
        ref = j.last_progress_at or j.started_at
        row["stalled"] = bool(ref and (now - ref).total_seconds() > cfg.stalled_after_seconds)
        running_rows.append(row)

    attention = session.execute(
        select(Job)
        .where(Job.status.in_([JobStatus.NEEDS_ATTENTION.value, JobStatus.FAILED.value]))
        .order_by(Job.finished_at.desc().nulls_last(), Job.created_at.desc())
        .limit(50)
    ).scalars().all()

    recent = session.execute(
        select(Job).where(Job.status.in_(list(TERMINAL)))
        .order_by(Job.finished_at.desc().nulls_last()).limit(15)
    ).scalars().all()

    return {
        "server_time": iso_z(now),
        "version": __version__,
        "nodes": node_rows,
        "queue": [_job_summary(session, j, include_waiting=True) for j in queued],
        "running": running_rows,
        "attention": [_job_summary(session, j) for j in attention],
        "recent": [_job_summary(session, j) for j in recent],
    }


@router.get("/health")
def health(request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    session.execute(select(func.count(Node.id)))
    return {"ok": True, "version": __version__}
