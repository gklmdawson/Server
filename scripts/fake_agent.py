#!/usr/bin/env python3
"""Simulated workstation agent for exercising the coordinator without Windows.

Registers (first sync), polls for work, "runs" assigned jobs by sleeping while
emitting progress, then reports success/failure. Honors cancel requests.

Examples:
  python scripts/fake_agent.py --node TERRA-01 --capabilities TERRA_PPK,TERRA_LIDAR
  python scripts/fake_agent.py --node PIX4D-01 --capabilities PIX4D_MATIC --duration 20
  python scripts/fake_agent.py --node BAD-01 --capabilities MOCK --fail
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Fake data-intake agent")
    parser.add_argument("--coordinator", default="http://127.0.0.1:8443")
    parser.add_argument("--node", required=True)
    parser.add_argument("--capabilities", default="MOCK",
                        help="comma-separated job types this fake node handles")
    parser.add_argument("--token", default="", help="node bearer token (if tokens required)")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="seconds each fake job 'runs'")
    parser.add_argument("--fail", action="store_true", help="report jobs as failed")
    parser.add_argument("--once", action="store_true", help="exit after finishing one job")
    args = parser.parse_args()

    caps = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    base = args.coordinator.rstrip("/") + "/api/v1"
    client = httpx.Client(headers=headers, timeout=10.0)

    def sync(active: list[dict]) -> dict:
        r = client.post(f"{base}/nodes/{args.node}/sync", json={
            "agent_version": "fake-0.1",
            "computer_name": args.node,
            "current_user": "fake",
            "capabilities": caps,
            "active_jobs": active,
            "accepting_jobs": True,
            "telemetry": {"cpu_percent": 7.0, "fake": True},
        })
        r.raise_for_status()
        return r.json()

    def report(job_uuid: str, kind: str, payload: dict) -> None:
        r = client.post(f"{base}/jobs/{job_uuid}/{kind}", json=payload)
        r.raise_for_status()

    print(f"[fake-agent] {args.node} caps={caps} -> {args.coordinator}")
    while True:
        try:
            resp = sync([])
        except Exception as exc:
            print(f"[fake-agent] sync failed: {exc}; retrying in 5s")
            time.sleep(5)
            continue

        job = resp.get("assign")
        if not job:
            time.sleep(min(resp.get("poll_after_seconds", 5), 5))
            continue

        uuid = job["job_uuid"]
        print(f"[fake-agent] got {job['job_type']} job {uuid[:8]} "
              f"(project {job.get('project_name') or '—'})")
        report(uuid, "started", {"pid": 12345, "agent_version": "fake-0.1",
                                 "processor_version": "fake"})

        cancelled = False
        steps = max(int(args.duration), 1)
        for i in range(steps):
            time.sleep(args.duration / steps)
            pct = (i + 1) / steps * 100
            report(uuid, "progress", {
                "progress_percent": pct,
                "stage": "fake_processing",
                "message": f"fake step {i + 1}/{steps}",
            })
            mid = sync([{"job_uuid": uuid, "progress_percent": pct,
                         "progress_message": f"fake step {i + 1}/{steps}"}])
            if uuid in mid.get("cancel_job_uuids", []):
                print(f"[fake-agent] cancel requested for {uuid[:8]}")
                report(uuid, "cancelled", {"message": "killed by fake agent"})
                cancelled = True
                break

        if not cancelled:
            if args.fail:
                report(uuid, "failed", {
                    "exit_code": 1, "error_code": "FAKE_FAILURE",
                    "error_message": "fake agent was asked to fail",
                })
                print(f"[fake-agent] reported FAILED for {uuid[:8]}")
            else:
                report(uuid, "succeeded", {
                    "exit_code": 0, "message": "fake job complete",
                    "outputs": [f"//UGREEN/fake/{uuid}.out"],
                    "validation": {"checked": True},
                })
                print(f"[fake-agent] reported SUCCEEDED for {uuid[:8]}")

        if args.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
