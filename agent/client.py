"""HTTP client for the coordinator API.

All report methods are safe to retry — the coordinator's report endpoints are
idempotent. `http` can be injected (tests pass a FastAPI TestClient, which is
an httpx.Client) so the whole agent can run against an in-process coordinator.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import httpx

from shared.schemas import SyncRequest, SyncResponse

logger = logging.getLogger("agent.client")


class ReportConflict(Exception):
    """Coordinator refused the report (409) — server-side state wins."""
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class CoordinatorClient:
    def __init__(self, base_url: str, token: str = "",
                 timeout: float = 15.0, http: Optional[httpx.Client] = None):
        self._base = base_url.rstrip("/")
        self._own_http = http is None
        # One request at a time: the sync loop and the job-runner thread share
        # this client, and individual calls are all small and fast.
        self._lock = threading.Lock()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if http is None:
            self.http = httpx.Client(timeout=timeout, headers=headers)
        else:
            self.http = http
            self._headers = headers

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/api/v1{path}"
        kwargs: dict[str, Any] = {"json": payload}
        if not self._own_http:
            kwargs["headers"] = getattr(self, "_headers", {})
        with self._lock:
            r = self.http.post(url, **kwargs)
        if r.status_code == 409:
            raise ReportConflict(409, r.json().get("detail", r.text))
        if r.status_code == 401:
            raise ReportConflict(401, r.json().get("detail", "unauthorized"))
        r.raise_for_status()
        return r.json()

    # --- sync ---------------------------------------------------------------

    def sync(self, node_name: str, request: SyncRequest) -> SyncResponse:
        data = self._post(f"/nodes/{node_name}/sync", request.model_dump())
        return SyncResponse.model_validate(data)

    # --- job reports ----------------------------------------------------------

    def report_started(self, job_uuid: str, pid: Optional[int] = None,
                       processor_version: str = "", agent_version: str = "",
                       message: str = "") -> None:
        self._post(f"/jobs/{job_uuid}/started", {
            "pid": pid, "processor_version": processor_version,
            "agent_version": agent_version, "message": message,
        })

    def report_progress(self, job_uuid: str, percent: Optional[float],
                        stage: str = "", message: str = "") -> None:
        self._post(f"/jobs/{job_uuid}/progress", {
            "progress_percent": percent, "stage": stage, "message": message,
        })

    def report_succeeded(self, job_uuid: str, exit_code: Optional[int],
                         message: str = "", outputs: Optional[list[str]] = None,
                         validation: Optional[dict[str, Any]] = None) -> None:
        self._post(f"/jobs/{job_uuid}/succeeded", {
            "exit_code": exit_code, "message": message,
            "outputs": outputs or [], "validation": validation or {},
        })

    def report_failed(self, job_uuid: str, exit_code: Optional[int],
                      error_code: str, error_message: str,
                      artifacts_path: str = "") -> None:
        self._post(f"/jobs/{job_uuid}/failed", {
            "exit_code": exit_code, "error_code": error_code,
            "error_message": error_message, "artifacts_path": artifacts_path,
        })

    def report_cancelled(self, job_uuid: str, message: str = "") -> None:
        self._post(f"/jobs/{job_uuid}/cancelled", {"message": message})

    def close(self) -> None:
        if self._own_http:
            self.http.close()
