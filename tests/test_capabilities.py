"""Coordinator-side capability policy (dashboard toggles).

The agent declares what a machine CAN run; POST /nodes/{name}/capabilities
restricts what the coordinator MAY assign to it (declared ∩ enabled).
"""
from tests.helpers import make_job, report, sync


def _set_enabled(client, node, enabled):
    return client.post(f"/api/v1/nodes/{node}/capabilities", json={"enabled": enabled})


def test_restriction_blocks_assignment_of_disallowed_type(client):
    sync(client, node="TERRA-01", caps=("TERRA_PPK", "TERRA_LIDAR"))
    make_job(client, "TERRA_LIDAR")

    r = _set_enabled(client, "TERRA-01", ["TERRA_PPK"])
    assert r.status_code == 200
    assert r.json()["effective_capabilities"] == ["TERRA_PPK"]

    # LiDAR job is queued but this node may only take PPK now.
    assert sync(client, node="TERRA-01", caps=("TERRA_PPK", "TERRA_LIDAR")).json()["assign"] is None

    ppk = make_job(client, "TERRA_PPK")
    assign = sync(client, node="TERRA-01", caps=("TERRA_PPK", "TERRA_LIDAR")).json()["assign"]
    assert assign["job_uuid"] == ppk


def test_clearing_restriction_restores_declared_set(client):
    sync(client, node="N1", caps=("MOCK",))
    job = make_job(client, "MOCK")

    _set_enabled(client, "N1", [])
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is None

    r = _set_enabled(client, "N1", None)
    assert r.json()["effective_capabilities"] == ["MOCK"]
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"]["job_uuid"] == job


def test_enabling_undeclared_type_has_no_effect_until_declared(client):
    sync(client, node="N1", caps=("MOCK",))
    make_job(client, "TERRA_PPK")

    # Allowed list may name types the agent doesn't declare — effective stays
    # bounded by the declaration, so nothing is assigned...
    _set_enabled(client, "N1", ["MOCK", "TERRA_PPK"])
    nodes = client.get("/api/v1/nodes").json()["nodes"]
    assert nodes[0]["effective_capabilities"] == ["MOCK"]
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is None

    # ...until the agent starts declaring it (Terra installed on the box).
    assign = sync(client, node="N1", caps=("MOCK", "TERRA_PPK")).json()["assign"]
    assert assign is not None
    assert assign["job_type"] == "TERRA_PPK"


def test_two_nodes_same_capability_split_work(client):
    """Two Terra boxes drain the same queue without double assignment."""
    a = make_job(client, "TERRA_PPK")
    b = make_job(client, "TERRA_PPK")
    got1 = sync(client, node="TERRA-01", caps=("TERRA_PPK",)).json()["assign"]["job_uuid"]
    got2 = sync(client, node="TERRA-02", caps=("TERRA_PPK",)).json()["assign"]["job_uuid"]
    assert {got1, got2} == {a, b}
    for uuid in (got1, got2):
        report(client, uuid, "started")
        report(client, uuid, "succeeded")
    assert sync(client, node="TERRA-01", caps=("TERRA_PPK",)).json()["assign"] is None


def test_node_summary_reports_policy(client):
    sync(client, node="N1", caps=("MOCK", "TERRA_PPK"))
    _set_enabled(client, "N1", ["TERRA_PPK"])
    node = client.get("/api/v1/nodes").json()["nodes"][0]
    assert node["capabilities"] == ["MOCK", "TERRA_PPK"]
    assert node["enabled_capabilities"] == ["TERRA_PPK"]
    assert node["effective_capabilities"] == ["TERRA_PPK"]


def test_unknown_node_404(client):
    assert _set_enabled(client, "NOPE", ["MOCK"]).status_code == 404
