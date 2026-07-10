"""Node token and admin token enforcement."""
import pytest
from fastapi.testclient import TestClient

from coordinator.main import create_app
from tests.conftest import make_config
from tests.helpers import make_job, report, sync


@pytest.fixture
def secure_client(tmp_path):
    cfg = make_config(tmp_path, require_agent_tokens=True, admin_token="admin-secret")
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


ADMIN = {"Authorization": "Bearer admin-secret"}


def _create_node(client, name="N1", caps=("MOCK",)) -> str:
    r = client.post("/api/v1/nodes",
                    json={"node_name": name, "capabilities": list(caps)},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["token"]


def test_unknown_node_rejected(secure_client):
    assert sync(secure_client, node="GHOST").status_code == 401


def test_node_token_flow(secure_client):
    token = _create_node(secure_client)
    assert sync(secure_client, node="N1", token=token).status_code == 200
    assert sync(secure_client, node="N1", token="wrong").status_code == 401
    assert sync(secure_client, node="N1").status_code == 401  # missing token


def test_token_rotation_invalidates_old_token(secure_client):
    old = _create_node(secure_client)
    new = _create_node(secure_client)  # same name -> rotates
    assert sync(secure_client, node="N1", token=old).status_code == 401
    assert sync(secure_client, node="N1", token=new).status_code == 200


def test_admin_endpoints_require_token(secure_client):
    assert secure_client.post(
        "/api/v1/jobs", json={"job_type": "MOCK"}
    ).status_code == 401
    r = secure_client.post("/api/v1/jobs", json={"job_type": "MOCK"}, headers=ADMIN)
    assert r.status_code == 201


def test_job_reports_require_assigned_nodes_token(secure_client):
    node_token = _create_node(secure_client, "N1", ("MOCK",))
    intruder_token = _create_node(secure_client, "N2", ("MOCK",))

    r = secure_client.post("/api/v1/jobs", json={"job_type": "MOCK"}, headers=ADMIN)
    uuid = r.json()["job_uuid"]
    assign = sync(secure_client, node="N1", token=node_token).json()["assign"]
    assert assign["job_uuid"] == uuid

    # No token / another node's token -> rejected.
    assert report(secure_client, uuid, "started").status_code == 401
    assert report(secure_client, uuid, "started", token=intruder_token).status_code == 401
    # The assigned node's token works.
    assert report(secure_client, uuid, "started", token=node_token).status_code == 200


def test_open_mode_auto_registers(client):
    # Default test config: require_agent_tokens=False.
    assert sync(client, node="ANY-NODE").status_code == 200
    names = [n["node_name"] for n in client.get("/api/v1/nodes").json()["nodes"]]
    assert "ANY-NODE" in names
