"""CYCLONE_CLASSIFY processor — port of classify_3dr.Classify3DRThread.

Runs Cyclone 3DR's ClassifyLAZ.js on each LAS/LAZ file that Terra produced:

    3DR.exe --Script=<ClassifyLAZ.js> --scriptAutorun --silent
            --scriptParam=var inputFile='<file>'; var modelName='<model>';

Behavior carried over from the GUI-thread original:
  * Completion per file = the output .3dr appears and its size is stable for
    two consecutive polls — Exit(0) from the JS is unreliable, so a clean
    process exit only counts if the .3dr exists. 3DR is terminated after the
    output stabilizes.
  * cloud_merged.laz under 8 GB -> classify only the merged cloud; at or over
    8 GB -> classify the tiles and skip the merged file.
  * Output is redirected to a log file, never an inherited pipe (the original
    deadlocked on a full pipe buffer — see its DEVNULL comment).

Deliberately changed for the queue:
  * No report.md wait — this job depends_on TERRA_LIDAR, which already
    validated report.md before succeeding. Preflight double-checks it.
  * No business-hours clock: ready() reports "3DR.exe already running" while
    a person uses Cyclone, which keeps the job QUEUED until the app is free.
  * Resumable: a fresh .3dr next to its LAZ counts as done and is skipped, so
    a retry after a partial failure only redoes the missing files.

Job parameters: terra_folder, project_name (WITH the _LiDAR suffix, matching
what intake passes to Classify3DRThread today), model_name.
Optional: per_file_timeout_hours (6), poll_seconds (10),
merged_threshold_gb (8), skip_existing (true).
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

from processors.base import JobContext, Processor, ProcessorError, Progress, Validation
from processors.util import check_payload_exe, missing_params, payload_exe

EXE_KEY = "cyclone_3dr_exe"
SCRIPT_KEY = "cyclone_classify_script"


class CycloneClassifyProcessor(Processor):
    job_types = {"CYCLONE_CLASSIFY"}
    requires_desktop = False        # CLI --silent; no pixel clicking
    custom_execution = True         # one 3DR.exe launch per LAZ file
    version = "1.0"

    # --- paths / selection ----------------------------------------------------

    def _laz_root(self, ctx: JobContext) -> Path:
        return (Path(ctx.parameters["terra_folder"])
                / ctx.parameters["project_name"] / "lidars" / "terra_laz")

    def _report_path(self, ctx: JobContext) -> Path:
        return (Path(ctx.parameters["terra_folder"])
                / ctx.parameters["project_name"] / "lidars" / "report" / "report.md")

    def select_files(self, ctx: JobContext) -> list[Path]:
        """All LAS/LAZ under terra_laz, with the merged-cloud size rule."""
        root = self._laz_root(ctx)
        files = sorted(p for p in root.rglob("*") if p.suffix.lower() in (".las", ".laz"))
        threshold = float(ctx.parameters.get("merged_threshold_gb", 8)) * 1024**3
        merged = next((p for p in files if p.name.lower() == "cloud_merged.laz"), None)
        if merged is not None:
            if merged.stat().st_size < threshold:
                return [merged]
            return [p for p in files if p != merged]
        return files

    @staticmethod
    def _output_for(las: Path) -> Path:
        return las.with_suffix(".3dr")

    def _already_done(self, las: Path) -> bool:
        out = self._output_for(las)
        try:
            return out.is_file() and out.stat().st_size > 0 \
                and out.stat().st_mtime >= las.stat().st_mtime
        except OSError:
            return False

    # --- hooks -----------------------------------------------------------------

    def ready(self) -> list[str]:
        from agent.preflight import process_running
        running = process_running(["3DR.exe"])
        if running:
            return [f"Cyclone 3DR is in use ({running} running) — waiting for it to close"]
        return []

    def preflight(self, ctx: JobContext) -> list[str]:
        errors = missing_params(ctx, ["terra_folder", "project_name", "model_name"])
        errors += check_payload_exe(self.cfg, EXE_KEY)
        script = (self.cfg.payload_paths or {}).get(SCRIPT_KEY, "")
        if not script:
            errors.append(f"agent config payload_paths.{SCRIPT_KEY} is not set")
        elif not Path(script).is_file():
            errors.append(f"classification script not found: {script}")
        if not errors:
            if not self._report_path(ctx).is_file():
                errors.append(f"Terra report.md missing: {self._report_path(ctx)}")
            elif not self._laz_root(ctx).is_dir():
                errors.append(f"terra_laz folder missing: {self._laz_root(ctx)}")
        return errors

    def build_file_command(self, ctx: JobContext, las: Path) -> list[str]:
        js_path = str(las).replace("\\", "\\\\")
        param = f"var inputFile='{js_path}'; var modelName='{ctx.parameters['model_name']}';"
        return [
            str(payload_exe(self.cfg, EXE_KEY)),
            f"--Script={self.cfg.payload_paths[SCRIPT_KEY]}",
            "--scriptAutorun",
            "--silent",
            f"--scriptParam={param}",
        ]

    def _classify_one(self, ctx: JobContext, las: Path, cancelled,
                      log_file) -> bool:
        """Run 3DR on one file; True on success. Mirrors the original
        _classify_one including the size-stability wait."""
        output = self._output_for(las)
        poll = float(ctx.parameters.get("poll_seconds", 10))
        deadline = time.monotonic() + float(
            ctx.parameters.get("per_file_timeout_hours", 6)) * 3600

        from agent.runner import kill_tree  # local import avoids a cycle
        proc = subprocess.Popen(self.build_file_command(ctx, las),
                                stdout=log_file, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        try:
            while time.monotonic() < deadline:
                if cancelled():
                    kill_tree(proc.pid)
                    return False
                if proc.poll() is not None:
                    # Exit(0) worked — trust it only if the output exists.
                    return output.is_file() and output.stat().st_size > 0

                if output.is_file():
                    prev_size, stable = -1, 0
                    while time.monotonic() < deadline and not cancelled():
                        time.sleep(poll)
                        try:
                            size = output.stat().st_size
                        except OSError:
                            size = -1
                        if size == prev_size and size > 0:
                            stable += 1
                            if stable >= 2:      # ~2 polls unchanged = written out
                                kill_tree(proc.pid)
                                try:
                                    proc.wait(timeout=30)
                                except subprocess.TimeoutExpired:
                                    pass
                                return True
                        else:
                            stable = 0
                        prev_size = size
                    kill_tree(proc.pid)
                    return not cancelled() and output.is_file() and output.stat().st_size > 0

                time.sleep(poll)

            kill_tree(proc.pid)   # per-file timeout
            return False
        finally:
            if proc.poll() is None:
                kill_tree(proc.pid)

    def run_custom(self, ctx: JobContext, progress, cancelled) -> Validation:
        files = self.select_files(ctx)
        if not files:
            raise ProcessorError(f"no LAS/LAZ files found under {self._laz_root(ctx)}")

        overall_deadline = (ctx.started_wall or time.time()) + ctx.max_runtime_seconds
        skip_existing = bool(ctx.parameters.get("skip_existing", True))
        total = len(files)
        failed: list[str] = []

        with open(ctx.log_path, "ab") as log_file:
            for index, las in enumerate(files, 1):
                if cancelled():
                    return Validation(ok=False, errors=["cancelled"])
                if time.time() > overall_deadline:
                    raise ProcessorError(
                        f"max runtime exceeded after {index - 1}/{total} files "
                        "(completed files are kept; retry resumes the rest)")
                if skip_existing and self._already_done(las):
                    progress(index / total * 100, "cyclone",
                             f"[{index}/{total}] {las.name} already classified — skipped")
                    continue
                progress((index - 1) / total * 100, "cyclone",
                         f"[{index}/{total}] classifying {las.name}")
                if self._classify_one(ctx, las, cancelled, log_file):
                    progress(index / total * 100, "cyclone",
                             f"[{index}/{total}] {las.name} done")
                else:
                    if cancelled():
                        return Validation(ok=False, errors=["cancelled"])
                    failed.append(las.name)
                    progress(index / total * 100, "cyclone",
                             f"[{index}/{total}] {las.name} FAILED")

        return self.validate_outputs(ctx)

    def validate_outputs(self, ctx: JobContext) -> Validation:
        files = self.select_files(ctx)
        if not files:
            return Validation(ok=False, errors=[
                f"no LAS/LAZ files found under {self._laz_root(ctx)}"])
        missing = [las.name for las in files if not self._already_done(las)]
        done = [str(self._output_for(las)) for las in files if self._already_done(las)]
        if missing:
            return Validation(
                ok=False, outputs=done,
                errors=[f"{len(missing)}/{len(files)} file(s) not classified: "
                        + ", ".join(missing[:10])],
                summary={"classified": len(done), "total": len(files)})
        return Validation(ok=True, outputs=done,
                          summary={"classified": len(done), "total": len(files)})
