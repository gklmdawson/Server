"""TERRA_PPK processor — wraps DJI_AUTOMATE_PPK.exe (DJIAutomatePPKV2.py).

The payload runs the whole PPK flow itself (Terra visible-light project →
PPK calculation → POS.txt export → EXIF/XMP embed into <ppk_path>) and exits
when done, so completion is simply process exit; validation checks the PPK
folder for POS.txt and the embedded images that Pix4Dmatic will import.

Job parameters (exactly the args data_intake builds today):
  project_name, data_source, terra_path, ppk_path, epsg_h, epsg_v
Optional: min_embedded_images (default 1)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from processors.base import JobContext, Processor, Progress, Validation
from processors.util import (
    check_payload_exe,
    files_matching,
    missing_params,
    newer_than_start,
    payload_exe,
    tail_last_line,
)

PAYLOAD_KEY = "dji_automate_ppk"


class TerraPpkProcessor(Processor):
    job_types = {"TERRA_PPK"}
    requires_desktop = True
    version = "1.0"

    def preflight(self, ctx: JobContext) -> list[str]:
        errors = missing_params(ctx, ["project_name", "data_source",
                                      "terra_path", "ppk_path"])
        errors += check_payload_exe(self.cfg, PAYLOAD_KEY)
        data_source = ctx.parameters.get("data_source", "")
        if data_source and not Path(data_source).is_dir():
            errors.append(f"data_source not reachable: {data_source}")
        ppk_path = ctx.parameters.get("ppk_path", "")
        if ppk_path and not Path(ppk_path).parent.exists():
            errors.append(f"ppk_path parent not reachable: {ppk_path}")
        return errors

    def build_command(self, ctx: JobContext) -> list[str]:
        p = ctx.parameters
        cmd = [
            str(payload_exe(self.cfg, PAYLOAD_KEY)),
            "--project-name", str(p["project_name"]),
            "--data-source",  str(p["data_source"]),
            "--terra-path",   str(p["terra_path"]),
            "--ppk-path",     str(p["ppk_path"]),
        ]
        if p.get("epsg_h"):
            cmd += ["--epsg-h", str(p["epsg_h"])]
        if p.get("epsg_v"):
            cmd += ["--epsg-v", str(p["epsg_v"])]
        cmd += ["--log-file", str(ctx.work_dir / "script.log"), "--unattended"]
        return cmd

    def poll(self, ctx: JobContext, elapsed_seconds: float) -> Optional[Progress]:
        # Stage-only progress: the payload's own log line is the honest signal.
        line = tail_last_line(ctx.work_dir / "script.log") or tail_last_line(ctx.log_path)
        if line:
            return Progress(percent=None, stage="terra_ppk", message=line)
        return None

    def validate_outputs(self, ctx: JobContext) -> Validation:
        ppk_path = Path(ctx.parameters.get("ppk_path", ""))
        errors: list[str] = []

        pos = ppk_path / "POS.txt"
        if not pos.is_file() or pos.stat().st_size == 0:
            errors.append(f"POS.txt missing or empty: {pos}")
        elif not newer_than_start(pos, ctx):
            errors.append(f"POS.txt predates this job: {pos}")

        min_images = int(ctx.parameters.get("min_embedded_images", 1))
        images = files_matching(ppk_path, ["*.jpg", "*.jpeg", "*.JPG", "*.JPEG"])
        if len(images) < min_images:
            errors.append(
                f"expected >= {min_images} embedded image(s) under {ppk_path}, "
                f"found {len(images)}")

        if errors:
            return Validation(ok=False, errors=errors)
        return Validation(
            ok=True,
            outputs=[str(pos), str(ppk_path)],
            summary={"embedded_images": len(images),
                     "pos_size_bytes": pos.stat().st_size},
        )
