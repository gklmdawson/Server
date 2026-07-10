"""Job state machine: happy path, idempotent reports, rejected transitions,
cancel and retry flows."""
from tests.helpers import get_job, make_job, report, sync


def test_happy_path_with_events(client):
    uuid = make_job(client, "MOCK")
    assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
    assert assign["job_uuid"] == uuid
    assert get_job(client, uuid)["status"] == "ASSIGNED"

    assert report(client, uuid, "started", {"pid": 4242}).status_code == 200
    job = get_job(client, uuid)
    assert job["status"] == "RUNNING"
    assert job["started_at"] is not None

    assert report(client, uuid, "progress",
                  {"progress_percent": 40, "stage": "stage1", "message": "working"}
                  ).status_code == 200
    job = get_job(client, uuid)
    assert job["progress_percent"] == 40
    assert job["progress_message"] == "working"

    assert report(client, uuid, "succeeded",
                  {"exit_code": 0, "outputs": ["//nas/out.las"],
                   "validation": {"files": 1}}).status_code == 200
    job = get_job(client, uuid)
    assert job["status"] == "SUCCEEDED"
    assert job["finished_at"] is not None
    assert job["progress_percent"] == 100

    types = [e["type"] for e in job["events"]]
    for expected in ("CREATED", "ASSIGNED", "STARTED", "PROGRESS", "SUCCEEDED"):
        assert expected in types


def test_reports_are_idempotent(client):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    assert report(client, uuid, "started").status_code == 200
    assert report(client, uuid, "started").status_code == 200  # repeat OK
    assert report(client, uuid, "succeeded").status_code == 200
    r = report(client, uuid, "succeeded")  # repeat OK
    assert r.status_code == 200
    assert r.json()["note"] == "already succeeded"
    # Late progress after completion is swallowed, not an error.
    r = report(client, uuid, "progress", {"progress_percent": 99})
    assert r.status_code == 200
    assert get_job(client, uuid)["status"] == "SUCCEEDED"


def test_invalid_transitions_rejected(client):
    uuid = make_job(client, "MOCK")
    # QUEUED -> RUNNING is not allowed without assignment.
    assert report(client, uuid, "started").status_code == 409
    assert report(client, uuid, "succeeded").status_code == 409

    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")
    report(client, uuid, "failed", {"error_code": "X", "error_message": "boom"})
    # A failed job cannot later be reported succeeded (or vice versa).
    assert report(client, uuid, "succeeded").status_code == 409
    assert report(client, uuid, "failed").status_code == 200  # idempotent repeat


def test_cancel_queued_job(client):
    uuid = make_job(client, "MOCK")
    r = client.post(f"/api/v1/jobs/{uuid}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is None


def test_cancel_running_job_via_sync(client):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")

    r = client.post(f"/api/v1/jobs/{uuid}/cancel")
    assert r.status_code == 200
    assert r.json()["cancel_requested"] is True
    assert get_job(client, uuid)["status"] == "RUNNING"  # still running until agent acts

    resp = sync(client, node="N1", caps=("MOCK",),
                active=[{"job_uuid": uuid, "progress_percent": 50}]).json()
    assert uuid in resp["cancel_job_uuids"]

    assert report(client, uuid, "cancelled", {"message": "killed"}).status_code == 200
    assert get_job(client, uuid)["status"] == "CANCELLED"


def test_cancel_terminal_job_rejected(client):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")
    report(client, uuid, "succeeded")
    assert client.post(f"/api/v1/jobs/{uuid}/cancel").status_code == 409


def test_retry_resets_job(client):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")
    report(client, uuid, "failed", {"error_code": "X", "error_message": "boom"})

    r = client.post(f"/api/v1/jobs/{uuid}/retry")
    assert r.status_code == 200
    job = get_job(client, uuid)
    assert job["status"] == "QUEUED"
    assert job["retry_count"] == 1
    assert job["assigned_node"] == ""
    assert job["error_message"] == ""

    # And it is assignable again.
    assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
    assert assign["job_uuid"] == uuid


def test_retry_of_running_job_rejected(client):
    uuid = make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",))
    report(client, uuid, "started")
    assert client.post(f"/api/v1/jobs/{uuid}/retry").status_code == 409
