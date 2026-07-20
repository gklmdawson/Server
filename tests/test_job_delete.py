"""DELETE /jobs/{uuid} and DELETE /projects/{uuid} — remove bad submissions
from the dashboard. A job live on a machine must be cancelled first."""
from tests.helpers import make_job, report, sync


def _intake_body(**overrides):
    body = {
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
    }
    body.update(overrides)
    return body


# --- job delete -----------------------------------------------------------------

def test_delete_leaf_job_removes_only_it(client):
    a = make_job(client, "MOCK")
    b = make_job(client, "MOCK", depends_on=[a])
    r = client.delete(f"/api/v1/jobs/{b}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == [b]
    assert client.get(f"/api/v1/jobs/{b}").status_code == 404
    assert client.get(f"/api/v1/jobs/{a}").status_code == 200


def test_delete_job_cascades_to_dependents(client):
    a = make_job(client, "MOCK")
    b = make_job(client, "MOCK", depends_on=[a])
    c = make_job(client, "MOCK", depends_on=[b])
    r = client.delete(f"/api/v1/jobs/{a}")
    assert r.status_code == 200, r.text
    assert set(r.json()["deleted"]) == {a, b, c}
    for uuid in (a, b, c):
        assert client.get(f"/api/v1/jobs/{uuid}").status_code == 404


def test_delete_running_job_is_refused(client):
    uuid = make_job(client, "MOCK")
    assign = sync(client, node="NODE-1", caps=("MOCK",)).json()["assign"]
    assert assign and assign["job_uuid"] == uuid
    assert report(client, uuid, "started", {"pid": 1}).status_code == 200

    r = client.delete(f"/api/v1/jobs/{uuid}")
    assert r.status_code == 409
    # Still on the dashboard — cancel it first.
    assert client.get(f"/api/v1/jobs/{uuid}").status_code == 200


def test_delete_unknown_job_is_404(client):
    assert client.delete("/api/v1/jobs/does-not-exist").status_code == 404


# --- project delete -------------------------------------------------------------

def test_delete_project_removes_project_and_all_jobs(client):
    resp = client.post("/api/v1/intake", json=_intake_body()).json()
    puid = resp["project_uuid"]
    job_uuids = [j["job_uuid"] for j in resp["jobs"]]
    assert len(job_uuids) >= 4

    r = client.delete(f"/api/v1/projects/{puid}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted_jobs"] == len(job_uuids)
    assert client.get(f"/api/v1/projects/{puid}").status_code == 404
    for uuid in job_uuids:
        assert client.get(f"/api/v1/jobs/{uuid}").status_code == 404


def test_delete_project_refused_while_a_job_runs(client):
    resp = client.post("/api/v1/intake", json=_intake_body()).json()
    puid = resp["project_uuid"]
    intake = next(j for j in resp["jobs"] if j["job_type"] == "INTAKE_COPY")

    assign = sync(client, node="NAS-COPY", caps=("INTAKE_COPY",)).json()["assign"]
    assert assign and assign["job_uuid"] == intake["job_uuid"]
    assert report(client, intake["job_uuid"], "started", {"pid": 1}).status_code == 200

    r = client.delete(f"/api/v1/projects/{puid}")
    assert r.status_code == 409
    assert client.get(f"/api/v1/projects/{puid}").status_code == 200
