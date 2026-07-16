"""Job assignment and lazy housekeeping.

There is no background scheduler loop. Assignment happens synchronously inside
an agent's /sync request, and housekeeping (lease reclaim, offline-node
flagging) runs lazily at the start of sync and dashboard reads. Node
online/offline and job "blocked on dependencies" are derived, never stored.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from coordinator.config import CoordinatorConfig
from coordinator.db import Job, Node, log_event, utcnow
from shared.schemas import EventType, JobStatus

logger = logging.getLogger("coordinator.assign")


def deps_satisfied(session: Session, job: Job) -> bool:
    """True when every job uuid in depends_on_json is SUCCEEDED.

    A missing dependency row counts as unsatisfied — the job simply never
    becomes eligible, which is visible on the dashboard as waiting-on.
    """
    dep_uuids = list(job.depends_on_json or [])
    if not dep_uuids:
        return True
    rows = session.execute(
        select(Job.uuid, Job.status).where(Job.uuid.in_(dep_uuids))
    ).all()
    found = {u: s for u, s in rows}
    return all(found.get(u) == JobStatus.SUCCEEDED.value for u in dep_uuids)


def waiting_on(session: Session, job: Job) -> list[str]:
    """Uuids of dependencies not yet SUCCEEDED (for display)."""
    dep_uuids = list(job.depends_on_json or [])
    if not dep_uuids:
        return []
    rows = session.execute(
        select(Job.uuid, Job.status).where(Job.uuid.in_(dep_uuids))
    ).all()
    found = {u: s for u, s in rows}
    return [u for u in dep_uuids if found.get(u) != JobStatus.SUCCEEDED.value]


def node_has_active_job(session: Session, node_name: str) -> bool:
    row = session.execute(
        select(Job.id)
        .where(
            Job.assigned_node == node_name,
            Job.status.in_([JobStatus.ASSIGNED.value, JobStatus.RUNNING.value]),
        )
        .limit(1)
    ).first()
    return row is not None


def pick_and_claim_next_job(session: Session, node: Node, cfg: CoordinatorConfig) -> Optional[Job]:
    """Pick the best eligible QUEUED job for this node and atomically claim it.

    Eligibility: capability match, dependencies SUCCEEDED, ordered by priority
    (higher first) then FIFO. The claim is a conditional UPDATE so a job can
    never be assigned twice even if two syncs race.
    """
    capabilities = node.effective_capabilities()
    if not capabilities:
        return None

    candidates: Sequence[Job] = session.execute(
        select(Job)
        .where(Job.status == JobStatus.QUEUED.value, Job.job_type.in_(capabilities))
        .order_by(Job.priority.desc(), Job.created_at.asc(), Job.id.asc())
    ).scalars().all()

    now = utcnow()
    for job in candidates:
        if not deps_satisfied(session, job):
            continue
        claimed = session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == JobStatus.QUEUED.value)
            .values(
                status=JobStatus.ASSIGNED.value,
                assigned_node=node.node_name,
                assigned_at=now,
                assign_attempts=Job.assign_attempts + 1,
            )
        )
        if claimed.rowcount == 1:
            session.refresh(job)
            log_event(session, job, EventType.ASSIGNED.value,
                      f"Assigned to {node.node_name} (capability {job.job_type}, "
                      f"priority {job.priority}, attempt {job.assign_attempts})",
                      node_name=node.node_name)
            logger.info("Assigned job %s (%s) to %s", job.uuid, job.job_type, node.node_name)
            return job
    return None


def housekeeping(session: Session, cfg: CoordinatorConfig, now: Optional[datetime] = None) -> None:
    """Lazy state repair, called at the top of sync and dashboard reads.

    1. Lease reclaim: ASSIGNED jobs whose agent never confirmed start within
       lease_minutes go back to QUEUED (NEEDS_ATTENTION after max strikes).
    2. Lost-node flagging: RUNNING jobs whose node has not synced for
       attention_after_seconds go to NEEDS_ATTENTION. Never auto-failed — the
       app may still be running; the reconnecting agent reconciles (see
       report handlers, which accept reports from NEEDS_ATTENTION).
    """
    now = now or utcnow()

    lease_cutoff = now - timedelta(minutes=cfg.lease_minutes)
    expired = session.execute(
        select(Job).where(
            Job.status == JobStatus.ASSIGNED.value,
            Job.assigned_at.is_not(None),
            Job.assigned_at < lease_cutoff,
        )
    ).scalars().all()
    for job in expired:
        node_name = job.assigned_node
        if job.assign_attempts >= cfg.max_assign_attempts:
            job.status = JobStatus.NEEDS_ATTENTION.value
            job.error_code = "LEASE_EXPIRED"
            job.error_message = (
                f"Assigned {job.assign_attempts} times without a start report; "
                "check the node's agent."
            )
            log_event(session, job, EventType.NEEDS_ATTENTION.value,
                      job.error_message, node_name=node_name)
        else:
            job.status = JobStatus.QUEUED.value
            job.assigned_node = ""
            job.assigned_at = None
            log_event(session, job, EventType.LEASE_EXPIRED.value,
                      f"No start report from {node_name} within {cfg.lease_minutes} min; requeued",
                      node_name=node_name)

    attention_cutoff = now - timedelta(seconds=cfg.attention_after_seconds)
    running = session.execute(
        select(Job).where(Job.status == JobStatus.RUNNING.value)
    ).scalars().all()
    if running:
        nodes = {
            n.node_name: n
            for n in session.execute(select(Node)).scalars().all()
        }
        for job in running:
            node = nodes.get(job.assigned_node)
            last_seen = node.last_sync_at if node else None
            if last_seen is None or last_seen < attention_cutoff:
                job.status = JobStatus.NEEDS_ATTENTION.value
                job.error_code = "NODE_LOST"
                job.error_message = (
                    f"Node {job.assigned_node or '?'} stopped syncing while this job was running. "
                    "The application may still be running on it."
                )
                log_event(session, job, EventType.NEEDS_ATTENTION.value,
                          job.error_message, node_name=job.assigned_node)


def reconcile_reported_jobs(session: Session, node: Node, reported_uuids: list[str],
                            cfg: CoordinatorConfig, now: Optional[datetime] = None) -> None:
    """Cross-check the agent's reported active jobs against coordinator state.

    - A NEEDS_ATTENTION job the agent still reports as active is revived to
      RUNNING (the node was lost and came back).
    - A RUNNING job the agent no longer reports (and whose terminal report
      never arrived) is flagged NEEDS_ATTENTION after a grace period — the
      agent may have crashed and lost its local state.
    """
    now = now or utcnow()
    reported = set(reported_uuids)

    coordinator_view = session.execute(
        select(Job).where(
            Job.assigned_node == node.node_name,
            Job.status.in_([JobStatus.RUNNING.value, JobStatus.NEEDS_ATTENTION.value]),
        )
    ).scalars().all()

    for job in coordinator_view:
        if job.uuid in reported:
            if job.status == JobStatus.NEEDS_ATTENTION.value:
                job.status = JobStatus.RUNNING.value
                job.error_code = ""
                job.error_message = ""
                log_event(session, job, EventType.REVIVED.value,
                          f"{node.node_name} reconnected and still reports this job active",
                          node_name=node.node_name)
            job.last_progress_at = now
        elif job.status == JobStatus.RUNNING.value:
            started = job.started_at or job.assigned_at or now
            reference = job.last_progress_at or started
            if (now - reference).total_seconds() > cfg.missing_job_grace_seconds:
                job.status = JobStatus.NEEDS_ATTENTION.value
                job.error_code = "JOB_NOT_REPORTED"
                job.error_message = (
                    f"{node.node_name} is syncing but no longer reports this job "
                    "and no terminal report arrived."
                )
                log_event(session, job, EventType.NEEDS_ATTENTION.value,
                          job.error_message, node_name=node.node_name)
