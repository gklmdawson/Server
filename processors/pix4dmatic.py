"""PIX4D_MATIC processor — wraps PIX4D_AUTOMATE.exe (AutomatePix4D.py).

Today the payload exits right after clicking Start, so after_exit keeps
watching until the run is demonstrably complete. Two signals, both
job-parameter configurable because the exact Pix4Dmatic layout gets pinned
down on the machine during Phase 4 calibration:

  * completion_log_glob + completion_pattern / failure_pattern — Pix4Dmatic
    writes rich stage logging; when configured, the newest matching log file
    is tailed for progress messages, an early failure signal, and completion.
  * ortho_glob (default "Pix4D/**/*ortho*.tif") — the orthomosaic is the
    final export; validation requires it fresh and over ortho_min_mb.

Once the on-machine save-project + close-app steps are added to
AutomatePix4D.py, the payload itself will block until done and after_exit's
first poll will pass immediately — this processor needs no change for that.

Job parameters: project_name, project_root (the date folder), epsg_h, epsg_v,
tat_path (the targets/TAT csv, used as-is).
Optional: ortho_glob, ortho_min_mb (default 10), completion_poll_seconds (30),
completion_log_glob, completion_pattern, failure_pattern.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from processors.base import JobContext, Processor, ProcessorError, Progress, Validation
from processors.util import (
    check_payload_exe,
    missing_params,
    newer_than_start,
    payload_exe,
    tail_last_line,
)

PAYLOAD_KEY = "pix4d_automate"
DEFAULT_ORTHO_GLOB = "Pix4D/**/*ortho*.tif"


class Pix4dMaticProcessor(Processor):
    job_types = {"PIX4D_MATIC"}
    requires_desktop = True
    version = "1.0"

    # --- helpers ---------------------------------------------------------------

    def _project_root(self, ctx: JobContext) -> Path:
        return Path(ctx.parameters["project_root"])

    def _ortho_candidates(self, ctx: JobContext) -> list[Path]:
        glob = ctx.parameters.get("ortho_glob", DEFAULT_ORTHO_GLOB)
        root = self._project_root(ctx)
        return sorted(p for p in root.glob(glob) if p.is_file())

    def _newest_app_log(self, ctx: JobContext) -> Optional[Path]:
        glob = ctx.parameters.get("completion_log_glob", "")
        if not glob:
            return None
        root = self._project_root(ctx)
        logs = [p for p in root.glob(glob) if p.is_file()]
        return max(logs, key=lambda p: p.stat().st_mtime, default=None)

    def _log_matches(self, ctx: JobContext, pattern_key: str) -> bool:
        pattern = ctx.parameters.get(pattern_key, "")
        log = self._newest_app_log(ctx)
        if not pattern or log is None:
            return False
        try:
            text = log.read_text(encoding="utf-8", errors="replace")[-131_072:]
        except OSError:
            return False
        return re.search(pattern, text) is not None

    def _run_looks_complete(self, ctx: JobContext) -> bool:
        orthos = [p for p in self._ortho_candidates(ctx) if newer_than_start(p, ctx)]
        if not orthos:
            return False
        if ctx.parameters.get("completion_pattern"):
            return self._log_matches(ctx, "completion_pattern")
        # No log pattern configured: require the newest ortho to be size-stable
        # (not still being written).
        newest = max(orthos, key=lambda p: p.stat().st_mtime)
        size1 = newest.stat().st_size
        time.sleep(2)
        return newest.stat().st_size == size1 and size1 > 0

    # --- hooks -----------------------------------------------------------------

    def preflight(self, ctx: JobContext) -> list[str]:
        errors = missing_params(ctx, ["project_name", "project_root", "tat_path"])
        errors += check_payload_exe(self.cfg, PAYLOAD_KEY)
        root = ctx.parameters.get("project_root", "")
        if root:
            if not Path(root).is_dir():
                errors.append(f"project_root not reachable: {root}")
            elif not (Path(root) / "PPK").is_dir():
                errors.append(f"PPK folder missing under project_root: {root}/PPK "
                              "(TERRA_PPK output expected)")
        tat = ctx.parameters.get("tat_path", "")
        if tat and not Path(tat).is_file():
            errors.append(f"tat_path not reachable: {tat}")
        return errors

    def build_command(self, ctx: JobContext) -> list[str]:
        p = ctx.parameters
        cmd = [
            str(payload_exe(self.cfg, PAYLOAD_KEY)),
            "--project-name", str(p["project_name"]),
            "--project-root", str(p["project_root"]),
            "--tat-path",     str(p["tat_path"]),
        ]
        if p.get("epsg_h"):
            cmd += ["--epsg-h", str(p["epsg_h"])]
        if p.get("epsg_v"):
            cmd += ["--epsg-v", str(p["epsg_v"])]
        cmd += ["--log-file", str(ctx.work_dir / "script.log"), "--unattended"]
        return cmd

    def poll(self, ctx: JobContext, elapsed_seconds: float) -> Optional[Progress]:
        app_log = self._newest_app_log(ctx)
        line = (tail_last_line(app_log) if app_log else "") \
            or tail_last_line(ctx.work_dir / "script.log") \
            or tail_last_line(ctx.log_path)
        if line:
            return Progress(percent=None, stage="pix4dmatic", message=line)
        return None

    def after_exit(self, ctx: JobContext, cancelled) -> None:
        """Wait until the run is demonstrably complete (ortho present and
        stable, plus the log completion pattern when configured)."""
        poll_seconds = float(ctx.parameters.get("completion_poll_seconds", 30))
        start = ctx.started_wall or time.time()
        deadline = start + ctx.max_runtime_seconds

        while not self._run_looks_complete(ctx):
            if cancelled():
                return
            if self._log_matches(ctx, "failure_pattern"):
                raise ProcessorError(
                    "Pix4Dmatic log matched the failure pattern "
                    f"({ctx.parameters.get('failure_pattern')})")
            if time.time() > deadline:
                raise ProcessorError(
                    f"Pix4Dmatic run incomplete after max runtime "
                    f"({ctx.max_runtime_seconds / 3600:.1f} h): no fresh orthomosaic "
                    f"matching '{ctx.parameters.get('ortho_glob', DEFAULT_ORTHO_GLOB)}'")
            time.sleep(poll_seconds)

    def validate_outputs(self, ctx: JobContext) -> Validation:
        errors: list[str] = []
        min_bytes = float(ctx.parameters.get("ortho_min_mb", 10)) * 1024 * 1024
        orthos = [p for p in self._ortho_candidates(ctx)
                  if newer_than_start(p, ctx) and p.stat().st_size >= min_bytes]
        if not orthos:
            errors.append(
                f"no fresh orthomosaic >= {min_bytes / 1024 / 1024:.0f} MB matching "
                f"'{ctx.parameters.get('ortho_glob', DEFAULT_ORTHO_GLOB)}' under "
                f"{self._project_root(ctx)}")
        if ctx.parameters.get("failure_pattern") and self._log_matches(ctx, "failure_pattern"):
            errors.append("Pix4Dmatic log contains the failure pattern")

        if errors:
            return Validation(ok=False, errors=errors)
        return Validation(
            ok=True,
            outputs=[str(p) for p in orthos],
            summary={"ortho_count": len(orthos),
                     "largest_ortho_bytes": max(p.stat().st_size for p in orthos)},
        )
