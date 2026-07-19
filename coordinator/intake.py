"""Server-side intake submission: one form -> split intake jobs + chains.

The intake step is split: INTAKE_COPY (NAS-local folder tree + bulk copy) runs
first, then RINEX_CONVERT (Windows Trimble conversion) when base data is
supplied; the processing chains gate on whichever intake job is last.


This is the single source of truth for the parameter building the intake GUI
does in `_handle_complete()` today (and `intake/queue_client.py` mirrors).
The web form sends decisions (client/project/date/sensor/paths/EPSG); this
module turns them into the job rows, so path conventions live in exactly one
place — see DESIGN.md §8 for the parameter contract per job type.

Paths are composed with PureWindowsPath: agents run on Windows, while the
coordinator may run on Linux (Docker on the NAS), so os.path.join would build
the wrong separators here.
"""
from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any

from shared.schemas import IntakeSubmit

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

SENSORS = ("M3E", "P1", "L2", "L3", "R3Pro", "R3ProMobile")

# Cyclone 3DR classification models offered by the LiDAR chain's model dropdown.
# Kept in sync with classify_3dr.CLASSIFICATION_MODELS, but duplicated here so
# the coordinator never has to import that PyQt-dependent GUI module. Config's
# intake_defaults.classify_models overrides this when present.
CLASSIFY_MODELS = (
    "BLK Mobile Filter People 2.0",
    "Heavy Construction UAV 2.0",
    "Indoor 2.2",
    "Indoor Construction Site 1.3",
    "Outdoor TLS 2.1",
    "Plant 2.0",
    "Road 1.0",
)

PHOTO_TEMPLATE = "photo_ppk"
LIDAR_TEMPLATE = "lidar"


class IntakeValidationError(ValueError):
    pass


def _w(*parts: str) -> str:
    """Join as a Windows path (backslashes), UNC-safe."""
    return str(PureWindowsPath(*parts))


def validate(body: IntakeSubmit) -> None:
    if not body.root_path.strip():
        raise IntakeValidationError("root_path is required")
    if not body.client.strip() or not body.project.strip():
        raise IntakeValidationError("client and project are required")
    date = body.date.strip()
    if not (len(date) == 9 and date[:2].isdigit() and date[5:].isdigit()
            and date[2:5].capitalize() in MONTHS):
        raise IntakeValidationError(
            f"date must be ddMonYYYY (e.g. 10Jul2026), got {date!r}")
    if body.sensor_type not in SENSORS:
        raise IntakeValidationError(
            f"sensor_type must be one of {', '.join(SENSORS)}")
    if not [s for s in body.source_folders if s.strip()]:
        raise IntakeValidationError("at least one source folder is required")
    if body.base_ecef_xyz is not None and len(body.base_ecef_xyz) != 3:
        raise IntakeValidationError("base_ecef_xyz must be [x, y, z]")
    if body.run_lidar_chain and body.sensor_type not in ("L2", "L3"):
        raise IntakeValidationError(
            "the LiDAR chain needs an L2/L3 sensor submission")
    if (body.run_photo_chain or body.run_lidar_chain) and not body.base_data_paths:
        raise IntakeValidationError(
            "processing chains need base data (PPK requires a base observation)")


def build_job_specs(body: IntakeSubmit) -> list[dict[str, Any]]:
    """The ordered job specs for one submission. Each spec:
    {job_type, parameters, depends_on: [indices into this list]} — the API
    layer swaps indices for the created jobs' uuids."""
    validate(body)

    root = body.root_path.strip()
    client, project, date = body.client.strip(), body.project.strip(), body.date.strip()
    sensor = body.sensor_type
    sources = [s.strip() for s in body.source_folders if s.strip()]

    date_path = _w(root, client, project, date)
    sensor_path = _w(date_path, sensor)
    terra_folder = _w(date_path, "Terra")
    ppk_folder = _w(date_path, "PPK")
    project_name = f"{client}_{project}_{date}"

    # Mirror _handle_complete: with exactly one flight folder DJI Terra gets
    # that subfolder, otherwise the sensor folder. Intake copies each source
    # as <sensor>/<basename>/, so the subfolder set is knowable at submit time.
    names = sorted({PureWindowsPath(s.replace("\\", "/")).name for s in sources})
    dji_data_source = _w(sensor_path, names[0]) if len(names) == 1 else sensor_path

    intake_params = {
        "root_path": root,
        "client": client,
        "project": project,
        "date": date,
        "sensor_type": sensor,
        "source_folders": sources,
        "base_data_paths": [b.strip() for b in body.base_data_paths if b.strip()],
        "base_data_is_rinex": body.base_data_is_rinex,
        "base_ecef_xyz": body.base_ecef_xyz,
    }

    # Split intake: the NAS-local copy (INTAKE_COPY) runs first; the Windows
    # RINEX conversion (RINEX_CONVERT) follows only when there is base data to
    # convert. Processing chains gate on whichever intake step is last.
    specs: list[dict[str, Any]] = [{
        "job_type": "INTAKE_COPY",
        "parameters": intake_params,
        "depends_on": [],
    }]
    if intake_params["base_data_paths"]:
        specs.append({
            "job_type": "RINEX_CONVERT",
            "parameters": intake_params,
            "depends_on": [0],
        })
    intake_gate = len(specs) - 1  # index of the last intake job

    if body.run_photo_chain:
        specs.append({
            "job_type": "TERRA_PPK",
            "parameters": {
                "project_name": project_name,
                "project_location": ppk_folder,
                "data_source": dji_data_source,
                "terra_path": terra_folder,
                "ppk_path": ppk_folder,
                "epsg_h": body.epsg_h,
                "epsg_v": body.epsg_v,
            },
            "depends_on": [intake_gate],
        })
        specs.append({
            "job_type": "PIX4D_MATIC",
            "parameters": {
                "project_name": project_name,
                "project_root": date_path,
                "tat_path": body.gcp_path,
                "epsg_h": body.epsg_h,
                "epsg_v": body.epsg_v,
            },
            "depends_on": [len(specs) - 1],
        })

    if body.run_lidar_chain:
        specs.append({
            "job_type": "TERRA_LIDAR",
            "parameters": {
                "project_name": project_name,
                "project_location": terra_folder,
                "data_source": sensor_path,
                "gcp_path": body.gcp_path,
                "epsg_h": body.epsg_h,
                "epsg_v": body.epsg_v,
                "no_targets": body.no_targets,
            },
            "depends_on": [intake_gate],
        })
        if body.classify_model:
            specs.append({
                "job_type": "CYCLONE_CLASSIFY",
                "parameters": {
                    "terra_folder": terra_folder,
                    "project_name": f"{project_name}_LiDAR",
                    "model_name": body.classify_model,
                },
                "depends_on": [len(specs) - 1],
            })

    return specs
