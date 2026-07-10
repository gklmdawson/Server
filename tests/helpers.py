"""Small wrappers used across the coordinator tests."""
from __future__ import annotations

from typing import Optional


def sync(client, node: str = "NODE-1", caps=("MOCK",), active: Optional[list] = None,
         accepting: bool = True, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.post(f"/api/v1/nodes/{node}/sync", json={
        "agent_version": "test-agent",
        "computer_name": node,
        "current_user": "tester",
        "capabilities": list(caps),
        "active_jobs": active or [],
        "accepting_jobs": accepting,
        "telemetry": {"cpu_percent": 1.0},
    }, headers=headers)
    return r


def make_job(client, job_type: str = "MOCK", **kwargs) -> str:
    r = client.post("/api/v1/jobs", json={"job_type": job_type, **kwargs})
    assert r.status_code == 201, r.text
    return r.json()["job_uuid"]


def get_job(client, job_uuid: str) -> dict:
    r = client.get(f"/api/v1/jobs/{job_uuid}")
    assert r.status_code == 200, r.text
    return r.json()


def report(client, job_uuid: str, kind: str, payload: Optional[dict] = None, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/api/v1/jobs/{job_uuid}/{kind}", json=payload or {}, headers=headers)


def run_to_success(client, node: str, caps, job_uuid: Optional[str] = None) -> str:
    """Sync until assigned (optionally expecting a specific job), then drive it
    started -> succeeded. Returns the completed job uuid."""
    r = sync(client, node=node, caps=caps)
    assert r.status_code == 200, r.text
    assign = r.json()["assign"]
    assert assign is not None, "expected an assignment"
    if job_uuid is not None:
        assert assign["job_uuid"] == job_uuid
    uuid = assign["job_uuid"]
    assert report(client, uuid, "started", {"pid": 1}).status_code == 200
    assert report(client, uuid, "succeeded", {"exit_code": 0}).status_code == 200
    return uuid
