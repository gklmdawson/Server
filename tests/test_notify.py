"""ntfy alert publishing: which transitions notify, and what they say.

The capture fixture swaps the module notifier for one whose transport records
payloads instead of hitting the network; `alerts()` drains the worker queue
and returns everything published so far.
"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from coordinator import notify
from coordinator.db import Job, Node, utcnow
from coordinator.notify import (
    PRIORITY_DEFAULT,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    Notifier,
    _duration,
)
from tests.helpers import make_job, report, run_to_success, sync


class _CaptureTransport:
    def __init__(self):
        self.sent = []

    def __call__(self, server, token, payload):
        self.sent.append(payload)


@pytest.fixture
def alerts(app, monkeypatch):
    """Enable alerting for the app under test and return a drain function."""
    transport = _CaptureTransport()
    notifier = Notifier(server="https://ntfy.test", topic="unit-test-topic",
                        transport=transport)
    monkeypatch.setattr(notify, "_notifier", notifier)

    def _drain():
        notifier.flush()
        return transport.sent

    return _drain


def _titled(sent, prefix):
    return [p for p in sent if p["title"].startswith(prefix)]


# ---------------------------------------------------------------------------
# Notifier unit behaviour
# ---------------------------------------------------------------------------

def test_disabled_without_topic():
    transport = _CaptureTransport()
    n = Notifier(topic="", transport=transport)
    n.publish("title", "message")
    n.flush()
    assert transport.sent == []


def test_app_default_is_disabled(app):
    # conftest's config sets no topic, so a test app never tries to publish.
    assert notify.get_notifier().enabled is False


def test_publish_truncates_long_messages():
    transport = _CaptureTransport()
    n = Notifier(topic="t", transport=transport)
    n.publish("title", "x" * 5000)
    n.flush()
    assert len(transport.sent) == 1
    assert len(transport.sent[0]["message"]) == 600


def test_duration_formatting():
    t0 = utcnow()
    assert _duration(None, t0) == ""
    assert _duration(t0, None) == ""
    assert _duration(t0, t0 + timedelta(seconds=45)) == "45s"
    assert _duration(t0, t0 + timedelta(minutes=5)) == "5m"
    assert _duration(t0, t0 + timedelta(seconds=3700)) == "1h 01m"


# ---------------------------------------------------------------------------
# Job lifecycle alerts
# ---------------------------------------------------------------------------

def test_job_failed_alerts_high_priority(client, alerts):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")
    report(client, uuid, "failed", {
        "error_code": "TERRA_TIMEOUT",
        "error_message": "Terra never produced the report",
    })

    failed = _titled(alerts(), "Failed: MOCK")
    assert len(failed) == 1
    assert failed[0]["priority"] == PRIORITY_HIGH
    assert failed[0]["tags"] == ["rotating_light"]
    assert "on N1" in failed[0]["message"]
    assert "TERRA_TIMEOUT" in failed[0]["message"]
    assert "Terra never produced the report" in failed[0]["message"]

    # A duplicate (idempotent retry) report must not alert twice.
    report(client, uuid, "failed", {"error_code": "TERRA_TIMEOUT"})
    assert len(_titled(alerts(), "Failed: MOCK")) == 1


def test_projectless_job_success_is_a_silent_tick(client, alerts):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")
    report(client, uuid, "succeeded", {"exit_code": 0})

    done = _titled(alerts(), "Done: MOCK")
    assert len(done) == 1
    assert done[0]["priority"] == PRIORITY_LOW
    assert "on N1" in done[0]["message"]


def test_chain_progress_then_project_complete(client, alerts):
    r = client.post("/api/v1/projects", json={
        "name": "Ranch", "client": "Acme",
        "chains": [{"template": "mock_chain"}],
    })
    assert r.status_code == 201, r.text

    run_to_success(client, "N1", ("MOCK_A",))
    sent = alerts()
    ticks = _titled(sent, "Done: MOCK_A — Ranch (Acme)")
    assert len(ticks) == 1
    assert ticks[0]["priority"] == PRIORITY_LOW
    assert "1/2 jobs done" in ticks[0]["message"]
    assert not _titled(sent, "Complete:")

    run_to_success(client, "N2", ("MOCK_B",))
    sent = alerts()
    complete = _titled(sent, "Complete: Ranch (Acme)")
    assert len(complete) == 1
    assert complete[0]["priority"] == PRIORITY_DEFAULT
    assert complete[0]["tags"] == ["tada"]
    assert "All 2 jobs succeeded" in complete[0]["message"]
    # The final step reports completion, not another progress tick.
    assert not _titled(sent, "Done: MOCK_B")


def test_cancelled_branch_still_completes_project(client, alerts):
    r = client.post("/api/v1/projects", json={
        "name": "Ranch", "client": "Acme",
        "chains": [{"template": "mock_chain"}],
    })
    jobs = {j["job_type"]: j["job_uuid"] for j in r.json()["jobs"]}

    # Operator kills the dependent step before it runs (no alert for that),
    # then the remaining step finishing completes the project.
    assert client.post(f"/api/v1/jobs/{jobs['MOCK_B']}/cancel").status_code == 200
    run_to_success(client, "N1", ("MOCK_A",))

    sent = alerts()
    assert not _titled(sent, "Failed:")
    complete = _titled(sent, "Complete: Ranch (Acme)")
    assert len(complete) == 1
    assert "Processing succeeded" in complete[0]["message"]


# ---------------------------------------------------------------------------
# Needs-attention / recovery / node presence
# ---------------------------------------------------------------------------

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


def test_lost_node_alerts_then_recovery_alerts(client, db, cfg, alerts):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")

    _backdate_node(db, "N1", cfg.attention_after_seconds + 60)
    _backdate_job(db, uuid, last_progress_at=cfg.attention_after_seconds + 60,
                  started_at=cfg.attention_after_seconds + 60)
    client.get("/api/v1/status")  # any read runs housekeeping

    sent = alerts()
    attention = _titled(sent, "Needs attention: MOCK")
    assert len(attention) == 1
    assert attention[0]["priority"] == PRIORITY_HIGH
    assert "N1" in attention[0]["message"]
    offline = _titled(sent, "Node offline: N1")
    assert len(offline) == 1
    assert offline[0]["priority"] == PRIORITY_DEFAULT

    # Node reconnects still reporting the job -> recovered + back online.
    sync(client, node="N1", caps=("MOCK",),
         active=[{"job_uuid": uuid, "progress_percent": 50}])
    sent = alerts()
    recovered = _titled(sent, "Recovered: MOCK")
    assert len(recovered) == 1
    assert recovered[0]["priority"] == PRIORITY_LOW
    assert len(_titled(sent, "Node back online: N1")) == 1


def test_lease_strikeout_alerts(client, db, cfg, alerts):
    uuid = make_job(client, "MOCK")
    for _ in range(cfg.max_assign_attempts):
        assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is not None
        _backdate_job(db, uuid, assigned_at=cfg.lease_minutes * 60 + 30)
    sync(client, node="N1", caps=("MOCK",))  # housekeeping strikes out

    attention = _titled(alerts(), "Needs attention: MOCK")
    assert len(attention) == 1
    assert "without a start report" in attention[0]["message"]


def test_job_not_reported_alerts(client, db, cfg, alerts):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")

    _backdate_job(db, uuid, last_progress_at=cfg.missing_job_grace_seconds + 60,
                  started_at=cfg.missing_job_grace_seconds + 60)
    sync(client, node="N1", caps=("MOCK",), active=[])

    attention = _titled(alerts(), "Needs attention: MOCK")
    assert len(attention) == 1
    assert "no longer reports" in attention[0]["message"]


def test_first_node_sighting_is_a_silent_baseline(client, alerts):
    sync(client, node="N1", caps=("MOCK",))
    client.get("/api/v1/status")
    sent = alerts()
    assert not _titled(sent, "Node offline")
    assert not _titled(sent, "Node back online")


# ---------------------------------------------------------------------------
# Intake submission
# ---------------------------------------------------------------------------

def test_intake_submission_alerts(client, alerts):
    r = client.post("/api/v1/intake", json={
        "root_path": "Z:/Survey/Projects",
        "client": "Brahma",
        "project": "SilverPeak",
        "date": "10Jul2026",
        "sensor_type": "L3",
        "source_folders": ["D:/ingest/DJI_202607100915_002_SilverPeak"],
        "base_data_paths": ["D:/ingest/base/1234.T02"],
        "run_photo_chain": True,
        "run_lidar_chain": True,
        "gcp_path": "Z:/Survey/Projects/targets.csv",
        "epsg_h": "6341",
        "epsg_v": "8228",
        "classify_model": "Heavy Construction UAV 2.0",
    })
    assert r.status_code == 201, r.text
    job_count = len(r.json()["jobs"])

    submitted = _titled(alerts(), "New intake: SilverPeak (Brahma)")
    assert len(submitted) == 1
    assert submitted[0]["priority"] == PRIORITY_LOW
    assert f"{job_count} jobs queued" in submitted[0]["message"]
    assert "photo, lidar" in submitted[0]["message"]
