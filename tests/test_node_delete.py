"""DELETE /nodes/{name}: remove a machine, but only once it's safely idle
(offline or disabled, and with no assigned/running job)."""
from tests.helpers import make_job, sync


def _node_names(client) -> list[str]:
    return [n["node_name"] for n in client.get("/api/v1/nodes").json()["nodes"]]


def test_delete_disabled_node_succeeds(client):
    sync(client, node="OLD-01", caps=("MOCK",))          # online + enabled
    assert client.post("/api/v1/nodes/OLD-01/disable").status_code == 200

    r = client.delete("/api/v1/nodes/OLD-01")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "node_name": "OLD-01", "deleted": True}
    assert "OLD-01" not in _node_names(client)


def test_delete_online_enabled_node_blocked(client):
    sync(client, node="LIVE-01", caps=("MOCK",))          # online + enabled
    r = client.delete("/api/v1/nodes/LIVE-01")
    assert r.status_code == 409
    assert "LIVE-01" in _node_names(client)               # still there


def test_delete_missing_node_404(client):
    assert client.delete("/api/v1/nodes/GHOST").status_code == 404


def test_delete_node_with_active_job_blocked(client):
    sync(client, node="BUSY-01", caps=("MOCK",))
    job = make_job(client, "MOCK")
    assign = sync(client, node="BUSY-01", caps=("MOCK",)).json()["assign"]
    assert assign["job_uuid"] == job                      # node now has the job

    # Even disabled, a node mid-job can't be removed.
    assert client.post("/api/v1/nodes/BUSY-01/disable").status_code == 200
    r = client.delete("/api/v1/nodes/BUSY-01")
    assert r.status_code == 409
    assert "BUSY-01" in _node_names(client)
