"""INTAKE processor — the intake GUI's copy + RINEX pipeline as a queue job.

This is ProcessingWorker._process_data from data_intake.py, running on
whichever machine can see the source data (capability INTAKE in that agent's
config). The web form only collects decisions; this job moves the bytes:

    build folder tree -> copy each source into <date>/<sensor>/<name>/ ->
    copy base data into <date>/BaseData -> convert T02/T04 to RINEX (Trimble
    CLI) or rename provided RINEX -> distribute the obs file per sensor type.

Resumable by design: copy_tree skips destination files that already exist
with the same size, so a crash/retry finishes the remainder instead of
duplicating (see intake_ops). Completion is judged by validate_outputs —
every source file accounted for at the destination, plus an obs file in
BaseData whenever base data was supplied.

Parameters (built by POST /api/v1/intake):
    root_path, client, project, date (ddMonYYYY), sensor_type,
    source_folders [..], base_data_paths [..], base_data_is_rinex (bool),
    base_ecef_xyz ([x,y,z] or null)

Agent config: payload_paths.convert_to_rinex_exe — required only when base
data needs conversion (T02/T04 supplied).
"""
from __future__ import annotations

import os
import time
from typing import Optional

import processors.intake_ops as ops
from processors.base import JobContext, Processor, ProcessorError, Validation
from processors.util import missing_params

CONVERTER_KEY = "convert_to_rinex_exe"

R3PRO_SENSORS = ("R3Pro", "R3ProMobile")
LIDAR_SENSORS = ("L2", "L3")


class IntakeProcessor(Processor):
    job_types = {"INTAKE"}
    requires_desktop = False
    custom_execution = True
    version = "1.0"

    # --- parameter helpers ---------------------------------------------------

    @staticmethod
    def _sources(ctx: JobContext) -> list[str]:
        return [str(s) for s in (ctx.parameters.get("source_folders") or []) if str(s).strip()]

    @staticmethod
    def _base_paths(ctx: JobContext) -> list[str]:
        return [str(s) for s in (ctx.parameters.get("base_data_paths") or []) if str(s).strip()]

    @staticmethod
    def _base_ecef(ctx: JobContext) -> Optional[tuple[float, float, float]]:
        raw = ctx.parameters.get("base_ecef_xyz")
        if not raw:
            return None
        try:
            x, y, z = (float(v) for v in raw)
            return (x, y, z)
        except (TypeError, ValueError):
            raise ProcessorError(f"base_ecef_xyz must be [x, y, z] numbers, got {raw!r}")

    def _paths(self, ctx: JobContext) -> dict[str, str]:
        p = ctx.parameters
        root, client = str(p["root_path"]), str(p["client"])
        project, date = str(p["project"]), str(p["date"])
        sensor = str(p["sensor_type"])
        date_folder = ops.date_folder_path(root, client, project, date)
        return {
            "date_folder": date_folder,
            "sensor_folder": ops.sensor_folder_path(root, client, project, date, sensor),
            "base_folder": os.path.join(date_folder, "BaseData"),
        }

    def _converter_exe(self) -> str:
        return (getattr(self.cfg, "payload_paths", {}) or {}).get(CONVERTER_KEY, "")

    def _needs_converter(self, ctx: JobContext) -> bool:
        return bool(self._base_paths(ctx)) and not bool(ctx.parameters.get("base_data_is_rinex"))

    # --- hooks -----------------------------------------------------------------

    def preflight(self, ctx: JobContext) -> list[str]:
        errors = missing_params(ctx, ["root_path", "client", "project", "date", "sensor_type"])
        sources = self._sources(ctx)
        if not sources:
            errors.append("missing job parameter: source_folders")
        for src in sources:
            if not os.path.isdir(src):
                errors.append(f"source folder not found (agent must be able to see it): {src}")
        for base in self._base_paths(ctx):
            if not os.path.isfile(base):
                errors.append(f"base data file not found: {base}")
        root = str(ctx.parameters.get("root_path", ""))
        if root and not os.path.isdir(root):
            errors.append(f"projects root not reachable: {root}")
        if self._needs_converter(ctx):
            exe = self._converter_exe()
            if not exe:
                errors.append(f"agent config payload_paths.{CONVERTER_KEY} is not set "
                              "(needed to convert T02/T04 base data)")
            elif not os.path.isfile(exe):
                errors.append(f"convertToRinex not found: {exe}")
        try:
            self._base_ecef(ctx)
        except ProcessorError as exc:
            errors.append(str(exc))
        return errors

    def run_custom(self, ctx: JobContext, progress, cancelled) -> Validation:
        p = ctx.parameters
        paths = self._paths(ctx)
        deadline = (ctx.started_wall or time.time()) + ctx.max_runtime_seconds

        def tick() -> bool:
            """Cancel probe for the copy loops; raises past the deadline."""
            if time.time() > deadline:
                raise ProcessorError(
                    "max runtime exceeded (completed files are kept; retry resumes)")
            return cancelled()

        def status(message: str) -> None:
            progress(None, "intake", message)

        progress(1, "intake", "Creating folder structure…")
        structure = ops.build_structure(str(p["client"]), str(p["project"]),
                                        str(p["date"]), str(p["sensor_type"]))
        ops.create_folder_structure(str(p["root_path"]), structure)

        # --- copy source data (bulk of the runtime) ---
        sources = self._sources(ctx)
        total = max(ops.count_files(sources), 1)
        done = {"n": 0}
        first_image: Optional[str] = None

        for source in sources:
            name = os.path.basename(os.path.normpath(source))

            def on_file(fname: str, _name=name) -> None:
                done["n"] += 1
                if done["n"] % 25 == 0 or done["n"] == total:
                    progress(5 + done["n"] / total * 70, "intake",
                             f"Copying {_name}: {done['n']}/{total} files")

            copied, skipped, image = ops.copy_tree(
                source, paths["sensor_folder"], on_file=on_file, cancelled=tick)
            first_image = first_image or image
            status(f"{name}: {copied} copied, {skipped} already present")
            if cancelled():
                return Validation(ok=False, errors=["cancelled"])

        # --- flight-date sanity check (warn only, like the GUI's auto-detect) ---
        exif_date = ops.get_image_date(first_image) if first_image else None
        if exif_date and exif_date != str(p["date"]):
            status(f"WARNING: EXIF flight date {exif_date} != submitted date {p['date']} "
                   "— folder tree uses the submitted date")

        # --- base data + RINEX ---
        base_paths = self._base_paths(ctx)
        if base_paths:
            progress(78, "intake", "Copying base data…")
            self._process_base_data(ctx, paths, status)
        if cancelled():
            return Validation(ok=False, errors=["cancelled"])

        progress(97, "intake", "Validating outputs…")
        validation = self.validate_outputs(ctx)
        if exif_date:
            validation.summary["exif_date"] = exif_date
            validation.summary["date_matches_exif"] = exif_date == str(p["date"])
        return validation

    # --- sensor-specific base handling (ProcessingWorker._process_sensor_specific) ---

    def _process_base_data(self, ctx: JobContext, paths: dict[str, str],
                           status) -> None:
        p = ctx.parameters
        sensor = str(p["sensor_type"])
        is_rinex = bool(p.get("base_data_is_rinex"))
        base_folder = paths["base_folder"]

        copied = ops.copy_base_data(self._base_paths(ctx), base_folder, is_rinex, status)
        status(f"Base data: {copied} file(s) in {base_folder}")

        if is_rinex:
            ops.rename_mix_to_nav(base_folder, status)
        else:
            status("Converting base data to RINEX…")
            ops.batch_convert(base_folder, self._converter_exe(),
                              self._base_ecef(ctx), status)

        if sensor in R3PRO_SENSORS:
            # Copy the (converted) base set into every flight's POS/base.
            for subfolder_name in os.listdir(paths["sensor_folder"]):
                subfolder_path = os.path.join(paths["sensor_folder"], subfolder_name)
                if not os.path.isdir(subfolder_path):
                    continue
                target = os.path.join(subfolder_path, "POS", "base")
                os.makedirs(target, exist_ok=True)
                for file_name in os.listdir(base_folder):
                    src = os.path.join(base_folder, file_name)
                    if os.path.isfile(src):
                        ops.copy_file(src, os.path.join(target, file_name))
                status(f"Base set copied to {target}")
            return

        rinex_file = ops.find_rinex_obs(base_folder)
        if not rinex_file:
            # Validation decides whether this fails the job; keep going so the
            # copy work is never wasted.
            status("WARNING: no RINEX obs file found in BaseData after conversion")
            return

        if sensor in LIDAR_SENSORS:
            ops.rename_for_sensor(rinex_file, paths["sensor_folder"], status)
        else:
            # M3E/P1: standardise to .obs and copy into every flight subfolder.
            obs_name = os.path.splitext(os.path.basename(rinex_file))[0] + ".obs"
            for subfolder_name in os.listdir(paths["sensor_folder"]):
                subfolder_path = os.path.join(paths["sensor_folder"], subfolder_name)
                if os.path.isdir(subfolder_path):
                    ops.copy_file(rinex_file, os.path.join(subfolder_path, obs_name))
                    status(f"Copied {obs_name} to {subfolder_path}")

    # --- validation (also the crash-recovery judge) -------------------------------

    def validate_outputs(self, ctx: JobContext) -> Validation:
        paths = self._paths(ctx)
        errors: list[str] = []
        outputs: list[str] = []

        if not os.path.isdir(paths["sensor_folder"]):
            return Validation(ok=False, errors=[
                f"sensor folder was never created: {paths['sensor_folder']}"])
        outputs.append(paths["sensor_folder"])

        total = present = 0
        for source in self._sources(ctx):
            if not os.path.isdir(source):
                errors.append(f"source folder no longer reachable for validation: {source}")
                continue
            folder_name = os.path.basename(os.path.normpath(source))
            for root_dir, _, files in os.walk(source):
                rel_path = os.path.relpath(root_dir, source)
                target_folder = os.path.join(paths["sensor_folder"], folder_name, rel_path)
                for file in files:
                    total += 1
                    if ops.dest_has_copy(os.path.join(root_dir, file), target_folder):
                        present += 1
        if present < total:
            errors.append(f"{total - present}/{total} source file(s) not copied yet")

        base_paths = self._base_paths(ctx)
        if base_paths:
            base_folder = paths["base_folder"]
            if not os.path.isdir(base_folder) or not any(os.scandir(base_folder)):
                errors.append(f"BaseData folder is empty: {base_folder}")
            elif ops.find_rinex_obs(base_folder) is None:
                errors.append("no RINEX obs file in BaseData "
                              "(conversion failed or wrong base files?)")
            else:
                outputs.append(base_folder)

        return Validation(
            ok=not errors, outputs=outputs, errors=errors,
            summary={"files_total": total, "files_present": present,
                     "base_files": len(base_paths)},
        )
