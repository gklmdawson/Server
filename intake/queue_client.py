"""Queue submission client for the Data Intake GUI (Phase 6 cutover).

Stdlib-only (urllib) so data_intake.py gains no new dependencies. The GUI
keeps doing everything through copy + RINEX conversion, then — instead of
launching the automation EXEs via DJISequenceThread — calls submit_project()
with the same values it already computes in _handle_complete().

Example (the photo + lidar project with 3DR classification):

    payload = build_project_payload(
        client="Brahma", project="SilverPeak", date="10Jul2026",
        sensor_type="L3", date_folder=date_path,
        ppk=dict(data_source=dji_data_source, terra_path=terra_folder,
                 ppk_path=ppk_folder, epsg_h=epsg_h, epsg_v=epsg_v),
        lidar=dict(data_source=sensor_path, project_location=terra_folder,
                   gcp_path=gcp, epsg_h=epsg_h, epsg_v=epsg_v,
                   no_targets=no_targets),
        pix4d=dict(project_root=date_path, tat_path=gcp,
                   epsg_h=epsg_h, epsg_v=epsg_v),
        classify_model="Heavy Construction UAV 2.0",
    )
    result = submit_project("http://192.168.35.67:8443", admin_token, payload)
    # result["jobs"] -> [{job_uuid, job_type, depends_on}, ...]
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional


class QueueSubmitError(Exception):
    pass


def build_project_payload(
    *,
    client: str,
    project: str,
    date: str,
    date_folder: str = "",
    sensor_type: str = "",
    root_path: str = "",
    priority: int = 100,
    ppk: Optional[dict[str, Any]] = None,
    pix4d: Optional[dict[str, Any]] = None,
    lidar: Optional[dict[str, Any]] = None,
    classify_model: str = "",
) -> dict[str, Any]:
    """Build a POST /api/v1/projects body from what the intake GUI already
    knows. Include `ppk` (+ optional `pix4d`) for the photo chain, `lidar`
    (+ `classify_model` for Cyclone) for the LiDAR chain — either or both.
    """
    project_name = f"{client}_{project}_{date}"
    chains: list[dict[str, Any]] = []

    if ppk is not None:
        parameters: dict[str, dict[str, Any]] = {
            "TERRA_PPK": {"project_name": project_name, **ppk},
        }
        if pix4d is not None:
            parameters["PIX4D_MATIC"] = {"project_name": project_name, **pix4d}
        chains.append({"template": "photo_ppk", "parameters": parameters})

    if lidar is not None:
        parameters = {
            "TERRA_LIDAR": {"project_name": project_name, **lidar},
        }
        if classify_model:
            terra_folder = lidar.get("project_location", "")
            parameters["CYCLONE_CLASSIFY"] = {
                "terra_folder": terra_folder,
                "project_name": f"{project_name}_LiDAR",
                "model_name": classify_model,
            }
        chains.append({"template": "lidar", "parameters": parameters})

    return {
        "name": project,
        "client": client,
        "sensor_type": sensor_type,
        "root_path": root_path,
        "date_folder": date_folder,
        "priority": priority,
        "metadata": {"submitted_by": "data_intake", "date": date},
        "chains": chains,
    }


def submit_project(coordinator_url: str, admin_token: str,
                   payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """POST the payload to the coordinator; returns its JSON response
    (project_uuid + created jobs). Raises QueueSubmitError with a readable
    message on any failure so the GUI can show it in a dialog."""
    url = coordinator_url.rstrip("/") + "/api/v1/projects"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {admin_token}"} if admin_token else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise QueueSubmitError(f"Coordinator rejected the submission "
                               f"({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise QueueSubmitError(
            f"Cannot reach the coordinator at {coordinator_url}: {exc.reason}") from exc
