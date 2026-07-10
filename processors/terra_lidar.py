"""TERRA_LIDAR processor — wraps DJI_AUTOMATE_UI.exe (PyAutomateDJI.py).

The payload clicks through project creation and *starts* reconstruction, then
exits — Terra itself keeps working for hours. Completion is detected exactly
the way classify_3dr.py already does it in production: wait for

    <terra_path>/<project_name>_LiDAR/lidars/report/report.md

to appear (the payload types the project name with a _LiDAR suffix).
Validation then requires LAS/LAZ output in lidars/terra_laz/.

Cancelling during the completion wait stops the watch and reports the job
cancelled, but DJI Terra itself keeps reconstructing — closing the app is a
human decision on that workstation.

Job parameters (exactly the args data_intake builds today):
  project_name, project_location (the Terra folder), data_source,
  epsg_h, epsg_v, gcp_path, no_targets
Optional: min_las_mb (default 1), completion_poll_seconds (default 60)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from processors.base import JobContext, Processor, ProcessorError, Progress, Validation
from processors.util import (
    check_payload_exe,
    files_matching,
    missing_params,
    newer_than_start,
    payload_exe,
    tail_last_line,
)

PAYLOAD_KEY = "dji_automate_ui"


class TerraLidarProcessor(Processor):
    job_types = {"TERRA_LIDAR"}
    requires_desktop = True
    version = "1.0"

    def _terra_project_dir(self, ctx: JobContext) -> Path:
        return (Path(ctx.parameters["project_location"])
                / f"{ctx.parameters['project_name']}_LiDAR")

    def _report_path(self, ctx: JobContext) -> Path:
        return self._terra_project_dir(ctx) / "lidars" / "report" / "report.md"

    def _laz_dir(self, ctx: JobContext) -> Path:
        return self._terra_project_dir(ctx) / "lidars" / "terra_laz"

    # --- hooks ----------------------------------------------------------------

    def preflight(self, ctx: JobContext) -> list[str]:
        errors = missing_params(ctx, ["project_name", "project_location", "data_source"])
        errors += check_payload_exe(self.cfg, PAYLOAD_KEY)
        data_source = ctx.parameters.get("data_source", "")
        if data_source and not Path(data_source).is_dir():
            errors.append(f"data_source not reachable: {data_source}")
        location = ctx.parameters.get("project_location", "")
        if location and not Path(location).parent.exists():
            errors.append(f"project_location parent not reachable: {location}")
        gcp = ctx.parameters.get("gcp_path", "")
        if gcp and not ctx.parameters.get("no_targets") and not Path(gcp).is_file():
            errors.append(f"gcp_path not reachable: {gcp}")
        return errors

    def build_command(self, ctx: JobContext) -> list[str]:
        p = ctx.parameters
        cmd = [
            str(payload_exe(self.cfg, PAYLOAD_KEY)),
            "--project-name",     str(p["project_name"]),
            "--project-location", str(p["project_location"]),
            "--data-source",      str(p["data_source"]),
        ]
        if p.get("epsg_h"):
            cmd += ["--epsg-h", str(p["epsg_h"])]
        if p.get("epsg_v"):
            cmd += ["--epsg-v", str(p["epsg_v"])]
        if p.get("gcp_path") and not p.get("no_targets"):
            cmd += ["--gcp-path", str(p["gcp_path"])]
        if p.get("no_targets"):
            cmd += ["--no-targets"]
        cmd += ["--log-file", str(ctx.work_dir / "script.log"), "--unattended"]
        return cmd

    def poll(self, ctx: JobContext, elapsed_seconds: float) -> Optional[Progress]:
        line = tail_last_line(ctx.work_dir / "script.log") or tail_last_line(ctx.log_path)
        if line:
            return Progress(percent=None, stage="terra_setup", message=line)
        return None

    def after_exit(self, ctx: JobContext, cancelled) -> None:
        """The payload exited after clicking Start Reconstruction; now wait for
        Terra's report.md. Bounded by the job's max runtime."""
        report = self._report_path(ctx)
        poll_seconds = float(ctx.parameters.get("completion_poll_seconds", 60))
        start = ctx.started_wall or time.time()
        deadline = start + ctx.max_runtime_seconds

        while not report.is_file():
            if cancelled():
                return  # runner reports cancelled; Terra keeps running (see module doc)
            if time.time() > deadline:
                raise ProcessorError(
                    f"Terra reconstruction incomplete after max runtime "
                    f"({ctx.max_runtime_seconds / 3600:.1f} h): {report} never appeared")
            time.sleep(poll_seconds)

    def validate_outputs(self, ctx: JobContext) -> Validation:
        errors: list[str] = []
        report = self._report_path(ctx)
        if not report.is_file():
            errors.append(f"report.md missing: {report}")
        elif not newer_than_start(report, ctx):
            errors.append(f"report.md predates this job: {report}")

        min_bytes = float(ctx.parameters.get("min_las_mb", 1)) * 1024 * 1024
        laz_files = files_matching(self._laz_dir(ctx), ["*.las", "*.laz"])
        big_enough = [p for p in laz_files if p.stat().st_size >= min_bytes]
        if not big_enough:
            errors.append(
                f"no LAS/LAZ >= {min_bytes / 1024 / 1024:.0f} MB in {self._laz_dir(ctx)} "
                f"(found {len(laz_files)} file(s))")

        if errors:
            return Validation(ok=False, errors=errors)
        return Validation(
            ok=True,
            outputs=[str(p) for p in big_enough] + [str(report)],
            summary={"las_count": len(big_enough),
                     "total_las_bytes": sum(p.stat().st_size for p in big_enough)},
        )
