"""Recovery semantics: lease reclaim, lost nodes, lost agent state, and
reports arriving from NEEDS_ATTENTION."""
from datetime import timedelta

from sqlalchemy import select

from coordinator.db import Job, Node, utcnow
from tests.helpers import get_job, make_job, report, sync


def _backdate_job(db, uuid, **deltas):
    with db() as session:
        job = session.execute(select(Job).where(Job.uuid == uuid)).scalar_one()
        for field, seconds in deltas.items():
            current = getattr(job, field)
            if current is not None:
                setattr(job, field, current - timedelta(seconds=seconds))
        session.commit()


def _backdate_node(db, node_name, seconds):
    with db() as session:
        node = session.execute(
            select(Node).where(Node.node_name == node_name)
        ).scalar_one()
        node.last_sync_at = utcnow() - timedelta(seconds=seconds)
        session.commit()


def test_lease_reclaim_and_reassign(client, db, cfg):
    uuid = make_job(client, "MOCK")
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"]["job_uuid"] == uuid

    # Agent never confirms start; lease expires -> requeued and immediately
    # reclaimable on the next sync (attempt 2).
    _backdate_job(db, uuid, assigned_at=cfg.lease_minutes * 60 + 30)
    assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
    assert assign is not None and assign["job_uuid"] == uuid

    events = [e["type"] for e in get_job(client, uuid)["events"]]
    assert "LEASE_EXPIRED" in events


def test_lease_strikeout_goes_to_needs_attention(client, db, cfg):
    uuid = make_job(client, "MOCK")
    for _ in range(cfg.max_assign_attempts):
        assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
        assert assign is not None and assign["job_uuid"] == uuid
        _backdate_job(db, uuid, assigned_at=cfg.lease_minutes * 60 + 30)

    # Next sync: housekeeping sees the third expired lease and strikes out.
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is None
    job = get_job(client, uuid)
    assert job["status"] == "NEEDS_ATTENTION"
    assert job["error_code"] == "LEASE_EXPIRED"


def test_lost_node_flags_job_then_agent_revives_it(client, db, cfg):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")

    # Node goes dark mid-job.
    _backdate_node(db, "N1", cfg.attention_after_seconds + 60)
    _backdate_job(db, uuid, last_progress_at=cfg.attention_after_seconds + 60,
                  started_at=cfg.attention_after_seconds + 60)

    client.get("/api/v1/status")  # any read runs housekeeping
    job = get_job(client, uuid)
    assert job["status"] == "NEEDS_ATTENTION"
    assert job["error_code"] == "NODE_LOST"

    # Agent reconnects still reporting the job -> revived, never duplicated.
    resp = sync(client, node="N1", caps=("MOCK",),
                active=[{"job_uuid": uuid, "progress_percent": 70,
                         "progress_message": "still going"}]).json()
    assert resp["assign"] is None  # busy with the revived job
    job = get_job(client, uuid)
    assert job["status"] == "RUNNING"
    assert job["progress_percent"] == 70

    assert report(client, uuid, "succeeded").status_code == 200


def test_agent_lost_local_state_flags_job(client, db, cfg):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")

    # Agent restarts with no memory of the job: syncs with an empty active
    # list. Inside the grace window nothing happens...
    resp = sync(client, node="N1", caps=("MOCK",), active=[]).json()
    assert get_job(client, uuid)["status"] == "RUNNING"
    assert resp["assign"] is None  # coordinator still counts the job as active

    # ...past the grace window the job is flagged for a human.
    _backdate_job(db, uuid, last_progress_at=cfg.missing_job_grace_seconds + 60,
                  started_at=cfg.missing_job_grace_seconds + 60)
    sync(client, node="N1", caps=("MOCK",), active=[])
    job = get_job(client, uuid)
    assert job["status"] == "NEEDS_ATTENTION"
    assert job["error_code"] == "JOB_NOT_REPORTED"


def test_terminal_report_accepted_from_needs_attention(client, db, cfg):
    """Agent recovers after a reboot, validates outputs on disk, and reports
    success for a job the coordinator had flagged."""
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")
    _backdate_node(db, "N1", cfg.attention_after_seconds + 60)
    _backdate_job(db, uuid, last_progress_at=cfg.attention_after_seconds + 60,
                  started_at=cfg.attention_after_seconds + 60)
    client.get("/api/v1/status")
    assert get_job(client, uuid)["status"] == "NEEDS_ATTENTION"

    assert report(client, uuid, "succeeded",
                  {"exit_code": 0, "message": "validated after reboot"}).status_code == 200
    assert get_job(client, uuid)["status"] == "SUCCEEDED"


def test_succeeded_from_assigned_when_start_report_was_lost(client):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    # started report lost in a network blip; terminal report still lands.
    assert report(client, uuid, "succeeded", {"exit_code": 0}).status_code == 200
    assert get_job(client, uuid)["status"] == "SUCCEEDED"
