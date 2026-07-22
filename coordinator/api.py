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
import json
import logging
import os
import secrets
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from coordinator import __version__, notify
from coordinator.assign import (
    housekeeping,
    node_has_active_job,
    pick_and_claim_next_job,
    reconcile_reported_jobs,
    waiting_on,
)
from coordinator.config import CoordinatorConfig
from coordinator.db import Job, JobEvent, Node, Project, iso_z, log_event, utcnow
from coordinator.epsg_names import EPSG_NAMES
from coordinator.intake import (
    CLASSIFY_MODELS as INTAKE_CLASSIFY_MODELS,
    SENSORS as INTAKE_SENSORS,
    IntakeValidationError,
    build_job_specs,
)
from shared.schemas import (
    CancelledReport,
    EjectRequest,
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
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
)

logger = logging.getLogger("coordinator.api")
router = APIRouter(prefix="/api/v1")

TERMINAL = {s.value for s in TERMINAL_STATUSES}
ACTIVE = {s.value for s in ACTIVE_STATUSES}  # live on a machine — cancel before deleting


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
    if revived:
        notify.job_recovered(job, "A start report arrived — the job is running.")
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
        notify.job_recovered(job, "A progress report arrived — the job is running.")
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
    # Chain progress alert — silent tick per finished step, a real notification
    # when the whole submission is done (nothing left but SUCCEEDED, ignoring
    # deliberately CANCELLED branches).
    project = job.project
    if project is not None:
        total = len(project.jobs)
        done = sum(1 for j in project.jobs if j.status == JobStatus.SUCCEEDED.value)
        remaining = [j for j in project.jobs
                     if j.status not in (JobStatus.SUCCEEDED.value,
                                         JobStatus.CANCELLED.value)]
        if not remaining:
            notify.project_complete(project, done, finished_at=now)
        else:
            notify.job_succeeded(job, done=done, total=total)
    else:
        notify.job_succeeded(job)
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
    notify.job_failed(job)
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

    chains = [name for name, on in (("photo", body.run_photo_chain),
                                    ("lidar", body.run_lidar_chain)) if on]
    notify.intake_submitted(project, len(created), chains)

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
    """Form-population data: valid sensors, shop defaults, EPSG name lookup, and
    the Cyclone 3DR classification models the LiDAR dropdown offers."""
    defaults = dict(cfg.intake_defaults or {})
    # Always give the form a classification-model list to build its dropdown
    # from; config may override, otherwise fall back to the known 3DR models.
    if not defaults.get("classify_models"):
        defaults["classify_models"] = list(INTAKE_CLASSIFY_MODELS)
    return {
        "sensors": list(INTAKE_SENSORS),
        "defaults": defaults,
        "epsg_names": EPSG_NAMES,
    }


# ---------------------------------------------------------------------------
# Server-side file browser (the web form's Browse buttons)
# ---------------------------------------------------------------------------

MAX_BROWSE_ENTRIES = 5000
# Dotfiles plus the usual NAS housekeeping folders (@eaDir, #recycle, ~$…).
BROWSE_SKIP_PREFIXES = (".", "@", "#", "~$")


def _st_dev(path: str) -> Optional[int]:
    """Device id of the filesystem `path` lives on (None if unreadable).
    A folder whose st_dev differs from its parent's is a mount point — i.e.
    a drive is actually attached there."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


@router.get("/browse")
def browse(request: Request, root: Optional[str] = None, path: str = "",
           cfg: CoordinatorConfig = Depends(get_cfg),
           _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """List a folder under a configured browse root (cfg.browse_roots).

    Without `root`: the configured roots, so the UI knows what to offer.
    With `root` (+ optional slash-separated relative `path`): the folder's
    entries, plus the `display_path` (UNC form) the picker writes into job
    parameters. Browsing never leaves the configured root.

    Roots flagged `mounted_only` (default for `ejectable` roots) are
    removable-media roots like the NAS card reader: the host keeps a
    mount-point folder per USB device it has ever seen, so the top level only
    lists folders with a drive actually mounted on them (st_dev differs from
    the root's) instead of every stale empty mount dir. Deeper levels are
    inside a mounted drive and are never filtered.
    """
    roots = cfg.browse_roots or {}
    eject_on = bool(cfg.eject_spool_dir)
    if root is None:
        # `restartable`: removable-media roots where the Rescan (container
        # restart) button makes sense — needs the same host watcher as eject.
        return {"roots": [
            {"label": label,
             "display": str(spec.get("display") or spec.get("path", "")),
             "ejectable": eject_on and bool(spec.get("ejectable")),
             "restartable": eject_on and bool(spec.get("mounted_only")
                                              or spec.get("ejectable"))}
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

    mounted_only = bool(spec.get("mounted_only", spec.get("ejectable")))
    base_dev = _st_dev(target) if (mounted_only and target == base) else None

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
            if base_dev is not None and is_dir and _st_dev(entry.path) == base_dev:
                continue  # same filesystem as the root -> no drive mounted here
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


def _resolve_root_target(cfg: CoordinatorConfig, root: str, path: str) -> str:
    """Resolve a configured browse root + relative path to a real, jailed
    directory on this coordinator's filesystem (same rules as /browse)."""
    spec = (cfg.browse_roots or {}).get(root)
    if not spec or not spec.get("path"):
        raise HTTPException(status_code=404, detail=f"Unknown browse root '{root}'")
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise HTTPException(status_code=400, detail="path may not contain '..'")
    base = os.path.realpath(str(spec["path"]))
    target = os.path.realpath(os.path.join(base, *parts))
    if target != base and not target.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="path escapes the browse root")
    return target


@router.get("/intake/probe")
def intake_probe(request: Request, root: str, path: str = "", rtk: bool = False,
                 cfg: CoordinatorConfig = Depends(get_cfg),
                 _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """NAS helper: read one representative image in a source folder on the share
    and return sensor / date / GPS / EPSG (H+V) so the web form can pre-fill
    them — all editable. `rtk=true` adds the (slower) exiftool RtkFlag scan.

    The folder is addressed exactly like /browse (a configured root + relative
    path); reading never leaves the root and never mutates anything."""
    from coordinator import probe as probe_mod

    target = _resolve_root_target(cfg, root, path)
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail=f"Not a folder: {path}")
    result = probe_mod.probe_folder(target, cfg.stateplane_shapefile or None)
    if rtk:
        result["rtk"] = probe_mod.rtk_scan(target, cfg.exiftool_path or "exiftool")
    return result


def _device_display_prefix(cfg: CoordinatorConfig, root: str, device: str) -> str:
    """The path a job would use to read `device` under `root` (its display
    form), so we can tell whether an active job is still reading the card."""
    spec = (cfg.browse_roots or {})[root]
    display_root = str(spec.get("display") or spec["path"])
    sep = "/" if display_root.startswith("/") else "\\"
    return f"{display_root.rstrip(sep)}{sep}{device}"


def _active_job_using(session: Session, prefix: str) -> Optional[str]:
    """UUID of an ASSIGNED/RUNNING job whose parameters reference `prefix`
    (the card device path), or None. The substring check is deliberately
    broad — refusing to eject a card a job might touch is the safe direction."""
    needle = prefix.replace("\\", "/").lower()
    rows = session.execute(
        select(Job.uuid, Job.parameters_json).where(Job.status.in_(ACTIVE_STATUSES))
    ).all()
    for uuid, params in rows:
        blob = json.dumps(params or {}).replace("\\", "/").lower()
        if needle in blob:
            return uuid
    return None


@router.post("/intake/eject")
def intake_eject(request: Request, body: EjectRequest,
                 cfg: CoordinatorConfig = Depends(get_cfg),
                 session: Session = Depends(get_session),
                 _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Safely unmount a removable card on the NAS host. The coordinator can't
    umount across the container boundary, so it spools the request to the
    host-side watcher (scripts/nas_eject_watcher.py) and waits for its result."""
    from coordinator import eject as eject_mod

    if not cfg.eject_spool_dir:
        raise HTTPException(status_code=404, detail="Media eject is not configured")
    spec = (cfg.browse_roots or {}).get(body.root)
    if not spec or not spec.get("path"):
        raise HTTPException(status_code=404, detail=f"Unknown browse root '{body.root}'")
    if not spec.get("ejectable"):
        raise HTTPException(status_code=400,
                            detail=f"Root '{body.root}' is not ejectable")
    try:
        device = eject_mod.validate_device(body.device)
    except eject_mod.EjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # The device must be a real top-level folder under the mount (a card),
    # not an arbitrary name.
    base = os.path.realpath(str(spec["path"]))
    target = os.path.realpath(os.path.join(base, device))
    if os.path.dirname(target) != base or not os.path.isdir(target):
        raise HTTPException(status_code=404, detail=f"No such device: {device}")

    busy = _active_job_using(session, _device_display_prefix(cfg, body.root, device))
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"A running job ({busy[:8]}) is still reading this card — "
                   "wait for it to finish before ejecting")

    result = eject_mod.eject(cfg.eject_spool_dir, device, cfg.eject_timeout_seconds)
    if result.pending:
        raise HTTPException(status_code=504, detail=result.message)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message
                            or "the host could not eject the card")
    logger.info("Ejected device %s under root %s", device, body.root)
    return {"ok": True, "device": device, "message": result.message or "Card ejected — safe to remove."}


@router.post("/intake/restart")
def intake_restart(request: Request,
                   cfg: CoordinatorConfig = Depends(get_cfg),
                   session: Session = Depends(get_session),
                   _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Restart the data-intake containers via the host watcher — the fix for a
    hot-plugged card that never propagated into the container's mount
    namespace. Uses the same spool as eject; the watcher acknowledges BEFORE
    running the restart (this process dies with it), so this response reaches
    the browser and the UI then polls /health until the coordinator is back."""
    from coordinator import eject as eject_mod

    if not cfg.eject_spool_dir:
        raise HTTPException(status_code=404,
                            detail="Container restart is not configured "
                                   "(set eject_spool_dir and run the host watcher)")
    # The restart kills the NAS-local copy worker too — never yank a card copy
    # out from under it. Jobs on the Windows boxes only lose a sync or two.
    busy = session.execute(
        select(Job.uuid).where(
            Job.status.in_(ACTIVE),
            Job.job_type == "INTAKE_COPY",
        ).limit(1)
    ).scalar_one_or_none()
    if busy:
        raise HTTPException(
            status_code=409,
            detail=f"An INTAKE_COPY job ({busy[:8]}) is running in the NAS "
                   "container — restarting now would kill the copy. Wait for "
                   "it to finish (or cancel it) first.")

    result = eject_mod.restart(cfg.eject_spool_dir, cfg.eject_timeout_seconds)
    if result.pending:
        raise HTTPException(status_code=504, detail=result.message)
    if not result.ok:
        raise HTTPException(status_code=409,
                            detail=result.message or "the host watcher refused the restart")
    logger.info("Container restart requested via the web UI")
    return {"ok": True, "message": result.message or "Restarting containers…"}


@router.post("/intake/upload", status_code=201)
async def intake_upload(request: Request, file: UploadFile = File(...),
                        cfg: CoordinatorConfig = Depends(get_cfg),
                        _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Store a small dropped file (base data / targets csv) on the NAS uploads
    volume and return the path the INTAKE_COPY worker reads. The bulk imagery
    is never uploaded — only these tiny inputs the operator has locally."""
    safe_name = os.path.basename(file.filename or "upload.bin").replace("\\", "_")
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    token = secrets.token_hex(8)
    dest_dir = os.path.join(cfg.upload_dir, token)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, safe_name)

    size = 0
    limit = cfg.max_upload_bytes
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                os.remove(dest)
                raise HTTPException(status_code=413,
                                    detail=f"file exceeds {limit} byte upload limit")
            out.write(chunk)

    return {"name": safe_name, "size": size, "stored_path": os.path.abspath(dest),
            "token": token}


@router.post("/intake/parse-ecef")
async def intake_parse_ecef(request: Request, file: UploadFile = File(...),
                            cfg: CoordinatorConfig = Depends(get_cfg),
                            _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Parse a corrected-base-position CSV (Point ID,X,Y,Z ECEF) into [x,y,z]
    metres so the form can pre-fill the ECEF field. The file itself is not
    kept — only the three numbers are returned."""
    from coordinator import probe as probe_mod
    import tempfile

    raw = await file.read(cfg.max_upload_bytes + 1)
    if len(raw) > cfg.max_upload_bytes:
        raise HTTPException(status_code=413, detail="file too large")
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        x, y, z = probe_mod.parse_base_ecef_csv(tmp_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        os.remove(tmp_path)
    return {"ecef": [x, y, z]}


@router.post("/intake/targets-summary")
def intake_targets_summary(request: Request, body: dict[str, Any],
                           cfg: CoordinatorConfig = Depends(get_cfg),
                           _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Preview how an uploaded all-points targets csv splits by point type.

    Given the `stored_path` returned by /intake/upload, report the TLT count and
    the TAT+TLT count (column 5 == "TLT"/"TAT"). The actual SINGLE_TLT.csv and
    TAT.csv are written into the project folder by INTAKE_COPY at run time; this
    is a read-only preview so the operator can confirm the upload before submit."""
    from coordinator import probe as probe_mod

    stored_path = str(body.get("stored_path") or "").strip()
    if not stored_path:
        raise HTTPException(status_code=400, detail="stored_path is required")
    # Jail: only files under the uploads volume may be read.
    upload_base = os.path.realpath(cfg.upload_dir)
    real = os.path.realpath(stored_path)
    if real != upload_base and not real.startswith(upload_base + os.sep):
        raise HTTPException(status_code=400, detail="stored_path escapes the uploads dir")
    if not os.path.isfile(real):
        raise HTTPException(status_code=404, detail="uploaded file not found")

    try:
        return probe_mod.summarize_targets(real)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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


@router.delete("/projects/{project_uuid}")
def delete_project(project_uuid: str, request: Request,
                   session: Session = Depends(get_session),
                   _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Remove a whole submission — the project and all its jobs/events — for a
    bad intake. Refused while any job is live on a machine (ASSIGNED/RUNNING);
    cancel it first so no agent is left working on a forgotten job."""
    project = _get_project(session, project_uuid)
    jobs = list(project.jobs)
    live = [j for j in jobs if j.status in ACTIVE]
    if live:
        names = ", ".join(f"{j.job_type} ({j.status})" for j in live)
        raise HTTPException(
            status_code=409,
            detail=f"Cancel the running job(s) first, then delete the project: {names}")
    # Core deletes (events -> jobs -> project) so the ORM never tries to null
    # the project FK on rows we've already removed.
    pid = project.id
    _delete_jobs(session, jobs)
    session.execute(delete(Project).where(Project.id == pid))
    return {"ok": True, "deleted_jobs": len(jobs)}


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


def _dependents_closure(session: Session, job: Job) -> list[Job]:
    """The job plus every job that (transitively) depends on it — a dependent
    can never run once its dependency is gone, so deleting one deletes the tail
    of the chain with it. Scoped to the job's project (deps live in one
    submission); falls back to all jobs for a project-less job."""
    if job.project_id is not None:
        scope = list(session.execute(
            select(Job).where(Job.project_id == job.project_id)).scalars().all())
    else:
        scope = list(session.execute(select(Job)).scalars().all())
    to_delete = {job.uuid}
    changed = True
    while changed:
        changed = False
        for j in scope:
            if j.uuid not in to_delete and any(
                dep in to_delete for dep in (j.depends_on_json or [])
            ):
                to_delete.add(j.uuid)
                changed = True
    return [j for j in scope if j.uuid in to_delete]


def _delete_jobs(session: Session, jobs: list[Job]) -> None:
    """Delete jobs and their events (FK-safe: events first)."""
    ids = [j.id for j in jobs]
    session.execute(delete(JobEvent).where(JobEvent.job_id.in_(ids)))
    session.execute(delete(Job).where(Job.id.in_(ids)))


@router.delete("/jobs/{job_uuid}")
def delete_job(job_uuid: str, request: Request,
               session: Session = Depends(get_session),
               _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Remove a job from the dashboard, along with any jobs that depend on it.

    A job that is live on a machine (ASSIGNED/RUNNING) must be cancelled first —
    deleting it here would leave the agent working on a job the coordinator has
    forgotten. Everything else (QUEUED / FAILED / CANCELLED / SUCCEEDED /
    NEEDS_ATTENTION) can be deleted."""
    job = _get_job(session, job_uuid)
    victims = _dependents_closure(session, job)
    live = [j for j in victims if j.status in ACTIVE]
    if live:
        names = ", ".join(f"{j.uuid} ({j.status})" for j in live)
        raise HTTPException(
            status_code=409,
            detail=f"Cancel the running job(s) first, then delete: {names}")
    deleted = [j.uuid for j in victims]
    _delete_jobs(session, victims)
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

@router.post("/nodes", status_code=201)
def create_node(body: NodeCreate, request: Request, rotate: bool = False,
                session: Session = Depends(get_session),
                _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Create a node and issue its token (returned exactly once — only its hash
    is stored, so a token can never be read back).

    Re-POSTing an existing node is idempotent by default: it updates the
    declared capabilities but leaves the token untouched (`token: null`), so a
    repeated provisioning call can't silently invalidate a working node. Pass
    `?rotate=true` to deliberately issue a NEW token (the old one stops working).
    """
    node = session.execute(
        select(Node).where(Node.node_name == body.node_name)
    ).scalar_one_or_none()
    created = node is None
    if node is None:
        node = Node(node_name=body.node_name)
        session.add(node)

    if body.capabilities:
        node.capabilities_json = body.capabilities

    # Issue a token only for a brand-new node, or an explicit rotation.
    if created or rotate:
        token = secrets.token_urlsafe(32)
        node.token_hash = _hash_token(token)
        session.flush()
        return {"node_name": node.node_name, "token": token,
                "rotated": not created, "created": created}

    session.flush()
    return {"node_name": node.node_name, "token": None, "rotated": False,
            "created": False,
            "detail": "node already exists; token unchanged. "
                      "Pass ?rotate=true to issue a new token."}


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


@router.delete("/nodes/{node_name}")
def delete_node(node_name: str, request: Request,
                session: Session = Depends(get_session),
                cfg: CoordinatorConfig = Depends(get_cfg),
                _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Remove a node record. Only allowed once the node is safely idle — it
    must be offline or disabled AND have no assigned/running job — so we never
    delete a machine mid-work. Note: if that machine's agent is still running,
    it re-registers on its next sync (token-less mode) or is rejected with 401
    (token mode) — stop the agent before deleting for the removal to stick."""
    now = utcnow()
    node = session.execute(
        select(Node).where(Node.node_name == node_name)
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_name} not found")

    online = node.is_online(now, cfg.offline_after_seconds)
    if online and node.enabled:
        raise HTTPException(
            status_code=409,
            detail="Node is online and enabled — disable it (or stop its agent) "
                   "before removing it.")
    if node_has_active_job(session, node_name):
        raise HTTPException(
            status_code=409,
            detail="Node still has an assigned or running job — wait for it to "
                   "finish (or cancel it) before removing the node.")

    session.delete(node)
    logger.info("Deleted node %s", node_name)
    return {"ok": True, "node_name": node_name, "deleted": True}


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
