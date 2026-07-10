"""Project creation with chain templates and cross-job dependency wiring."""
from tests.helpers import get_job, report, run_to_success, sync


def _create_mock_project(client):
    r = client.post("/api/v1/projects", json={
        "name": "SilverPeak",
        "client": "Brahma",
        "sensor_type": "M3E",
        "root_path": "//UGREEN/Active/Brahma/SilverPeak",
        "date_folder": "10Jul2026",
        "chains": [{
            "template": "mock_chain",
            "parameters": {
                "MOCK_A": {"foo": 1},
                "MOCK_B": {"bar": 2},
            },
        }],
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_project_chain_creates_wired_jobs(client):
    body = _create_mock_project(client)
    jobs = body["jobs"]
    assert [j["job_type"] for j in jobs] == ["MOCK_A", "MOCK_B"]
    a, b = jobs
    assert a["depends_on"] == []
    assert b["depends_on"] == [a["job_uuid"]]

    detail = client.get(f"/api/v1/projects/{body['project_uuid']}").json()
    assert detail["name"] == "SilverPeak"
    assert len(detail["jobs"]) == 2

    job_b = get_job(client, b["job_uuid"])
    assert job_b["waiting_on"] == [a["job_uuid"]]
    assert job_b["project_name"] == "SilverPeak"


def test_chain_executes_in_order(client):
    body = _create_mock_project(client)
    a_uuid = body["jobs"][0]["job_uuid"]
    b_uuid = body["jobs"][1]["job_uuid"]
    caps = ("MOCK_A", "MOCK_B")

    # Only A is eligible first, even though the node can run both.
    run_to_success(client, "N1", caps, job_uuid=a_uuid)
    # A succeeded -> B becomes eligible.
    run_to_success(client, "N1", caps, job_uuid=b_uuid)


def test_chain_parameters_and_assignment_payload(client):
    body = _create_mock_project(client)
    assign = sync(client, node="N1", caps=("MOCK_A",)).json()["assign"]
    assert assign["parameters"] == {"foo": 1}
    assert assign["project_name"] == "SilverPeak"
    assert assign["client"] == "Brahma"


def test_unknown_template_rejected(client):
    r = client.post("/api/v1/projects", json={
        "name": "X", "chains": [{"template": "nope"}],
    })
    assert r.status_code == 400
    assert "nope" in r.json()["detail"]


def test_unknown_dependency_rejected(client):
    r = client.post("/api/v1/jobs", json={
        "job_type": "MOCK", "depends_on": ["not-a-job-uuid"],
    })
    assert r.status_code == 400


def test_failed_parent_blocks_chain_until_retry(client):
    body = _create_mock_project(client)
    a_uuid = body["jobs"][0]["job_uuid"]
    b_uuid = body["jobs"][1]["job_uuid"]
    caps = ("MOCK_A", "MOCK_B")

    assign = sync(client, "N1", caps).json()["assign"]
    assert assign["job_uuid"] == a_uuid
    report(client, a_uuid, "started")
    report(client, a_uuid, "failed", {"error_code": "BOOM"})

    # B stays blocked while A is failed.
    assert sync(client, "N1", caps).json()["assign"] is None

    # Manual retry of A unblocks the chain end-to-end.
    assert client.post(f"/api/v1/jobs/{a_uuid}/retry").status_code == 200
    run_to_success(client, "N1", caps, job_uuid=a_uuid)
    run_to_success(client, "N1", caps, job_uuid=b_uuid)


def test_status_endpoint_shape(client):
    _create_mock_project(client)
    sync(client, node="N1", caps=("MOCK_A",))
    r = client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("server_time", "nodes", "queue", "running", "attention", "recent"):
        assert key in body
    assert body["nodes"][0]["node_name"] == "N1"
    assert body["nodes"][0]["active_job"]["job_type"] == "MOCK_A"
