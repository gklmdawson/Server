"""Assignment routing: capabilities, priority/FIFO, one job per node, deps,
node flags."""
from tests.helpers import get_job, make_job, report, sync


def test_sync_registers_node_and_returns_no_work(client):
    r = sync(client, node="TERRA-01", caps=("TERRA_PPK",))
    assert r.status_code == 200
    body = r.json()
    assert body["assign"] is None
    assert body["poll_after_seconds"] > 0

    nodes = client.get("/api/v1/nodes").json()["nodes"]
    assert [n["node_name"] for n in nodes] == ["TERRA-01"]
    assert nodes[0]["online"] is True
    assert nodes[0]["capabilities"] == ["TERRA_PPK"]


def test_capability_routing(client):
    make_job(client, "PIX4D_MATIC")
    mock_uuid = make_job(client, "MOCK")

    r = sync(client, node="MOCK-01", caps=("MOCK",))
    assign = r.json()["assign"]
    assert assign is not None
    assert assign["job_uuid"] == mock_uuid
    assert assign["job_type"] == "MOCK"

    # No second MOCK job exists; the PIX4D job must not leak to this node.
    report(client, mock_uuid, "started")
    report(client, mock_uuid, "succeeded")
    assert sync(client, node="MOCK-01", caps=("MOCK",)).json()["assign"] is None


def test_priority_then_fifo(client):
    low_old = make_job(client, "MOCK", priority=100)
    low_new = make_job(client, "MOCK", priority=100)
    high = make_job(client, "MOCK", priority=200)

    order = []
    for _ in range(3):
        assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
        order.append(assign["job_uuid"])
        report(client, assign["job_uuid"], "started")
        report(client, assign["job_uuid"], "succeeded")
    assert order == [high, low_old, low_new]


def test_one_job_per_node(client):
    make_job(client, "MOCK")
    make_job(client, "MOCK")

    first = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
    assert first is not None
    # Node already holds an ASSIGNED job -> nothing new, even though the agent
    # didn't echo it back in active_jobs.
    second = sync(client, node="N1", caps=("MOCK",)).json()
    assert second["assign"] is None
    assert second["poll_after_seconds"] > 0


def test_two_nodes_split_the_queue(client):
    a = make_job(client, "MOCK")
    b = make_job(client, "MOCK")
    got1 = sync(client, node="N1", caps=("MOCK",)).json()["assign"]["job_uuid"]
    got2 = sync(client, node="N2", caps=("MOCK",)).json()["assign"]["job_uuid"]
    assert {got1, got2} == {a, b}


def test_depends_on_gates_assignment(client):
    parent = make_job(client, "MOCK")
    child = make_job(client, "MOCK", depends_on=[parent])

    assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
    assert assign["job_uuid"] == parent
    assert get_job(client, child)["waiting_on"] == [parent]

    report(client, parent, "started")
    report(client, parent, "succeeded")

    assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
    assert assign["job_uuid"] == child


def test_failed_dependency_keeps_child_queued(client):
    parent = make_job(client, "MOCK")
    child = make_job(client, "MOCK", depends_on=[parent])

    assign = sync(client, node="N1", caps=("MOCK",)).json()["assign"]
    assert assign["job_uuid"] == parent
    report(client, parent, "started")
    report(client, parent, "failed", {"error_code": "BOOM"})

    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is None
    assert get_job(client, child)["status"] == "QUEUED"


def test_disabled_drained_paused_nodes_get_nothing(client):
    make_job(client, "MOCK")
    sync(client, node="N1", caps=("MOCK",), accepting=False)  # register first

    # agent-side pause
    assert sync(client, node="N1", caps=("MOCK",), accepting=False).json()["assign"] is None

    # admin disable
    client.post("/api/v1/nodes/N1/disable")
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is None

    # enabled but draining
    client.post("/api/v1/nodes/N1/enable")
    client.post("/api/v1/nodes/N1/drain")
    r = sync(client, node="N1", caps=("MOCK",)).json()
    assert r["assign"] is None
    assert r["drain"] is True

    # enable clears draining
    client.post("/api/v1/nodes/N1/enable")
    assert sync(client, node="N1", caps=("MOCK",)).json()["assign"] is not None
