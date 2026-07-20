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

Scratch drive (agent.yaml `scratch_dir`): AV (Sophos) scanning of the NAS
share slows Pix4D badly, so when a scratch dir is configured the processor
stages the run onto local disk:

  prepare()     copies <project_root>/PPK (+ the TAT csv) to
                <scratch_dir>/<project_name>/ and Pix4D runs against that.
  after_exit()  waits for completion on the scratch copy, copies everything
                Pix4D produced (all of the scratch dir except the PPK input)
                back to the NAS project_root, verifies the orthomosaic landed,
                then deletes the scratch dir. Left in place on failure so a
                retry resumes and the operator can inspect it.

Without scratch_dir the run happens in place on the NAS, unchanged.

Once the on-machine save-project + close-app steps are added to
AutomatePix4D.py, the payload itself will block until done and after_exit's
first poll will pass immediately — this processor needs no change for that.

Job parameters: project_name, project_root (the date folder), epsg_h, epsg_v,
tat_path (the targets/TAT csv, used as-is).
Optional: ortho_glob, ortho_min_mb (default 10), completion_poll_seconds (30),
completion_log_glob, completion_pattern, failure_pattern.
"""
from __future__ import annotations

import os
import re
import shutil
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
PPK_DIR = "PPK"


class Pix4dMaticProcessor(Processor):
    job_types = {"PIX4D_MATIC"}
    requires_desktop = True
    version = "1.1"

    # --- roots (NAS = final home, run = where Pix4D actually runs) -------------

    def _nas_root(self, ctx: JobContext) -> Path:
        return Path(ctx.parameters["project_root"])

    def _scratch_root(self, ctx: JobContext) -> Optional[Path]:
        """Per-project scratch dir when a scratch drive is configured, else None
        (run in place on the NAS)."""
        base = (getattr(self.cfg, "scratch_dir", "") or "").strip()
        if not base:
            return None
        return Path(base) / str(ctx.parameters["project_name"])

    def _run_root(self, ctx: JobContext) -> Path:
        """Where Pix4D reads/writes for this run: scratch if staging, else NAS."""
        return self._scratch_root(ctx) or self._nas_root(ctx)

    def _tat_for_run(self, ctx: JobContext) -> str:
        """TAT path Pix4D should read — the staged copy on scratch when present,
        otherwise the original NAS path."""
        tat = str(ctx.parameters.get("tat_path", "") or "")
        scratch = self._scratch_root(ctx)
        if tat and scratch is not None:
            staged = scratch / Path(tat).name
            if staged.is_file():
                return str(staged)
        return tat

    # --- ortho / log completion (parameterized by which root Pix4D used) -------

    def _ortho_candidates(self, ctx: JobContext, root: Path) -> list[Path]:
        glob = ctx.parameters.get("ortho_glob", DEFAULT_ORTHO_GLOB)
        return sorted(p for p in root.glob(glob) if p.is_file())

    def _newest_app_log(self, ctx: JobContext, root: Path) -> Optional[Path]:
        glob = ctx.parameters.get("completion_log_glob", "")
        if not glob:
            return None
        logs = [p for p in root.glob(glob) if p.is_file()]
        return max(logs, key=lambda p: p.stat().st_mtime, default=None)

    def _log_matches(self, ctx: JobContext, root: Path, pattern_key: str) -> bool:
        pattern = ctx.parameters.get(pattern_key, "")
        log = self._newest_app_log(ctx, root)
        if not pattern or log is None:
            return False
        try:
            text = log.read_text(encoding="utf-8", errors="replace")[-131_072:]
        except OSError:
            return False
        return re.search(pattern, text) is not None

    def _fresh_orthos(self, ctx: JobContext, root: Path, min_bytes: float = 0.0) -> list[Path]:
        return [p for p in self._ortho_candidates(ctx, root)
                if newer_than_start(p, ctx) and p.stat().st_size >= min_bytes]

    def _run_looks_complete(self, ctx: JobContext, root: Path) -> bool:
        orthos = self._fresh_orthos(ctx, root)
        if not orthos:
            return False
        if ctx.parameters.get("completion_pattern"):
            return self._log_matches(ctx, root, "completion_pattern")
        # No log pattern configured: require the newest ortho to be size-stable
        # (not still being written).
        newest = max(orthos, key=lambda p: p.stat().st_mtime)
        size1 = newest.stat().st_size
        time.sleep(2)
        return newest.stat().st_size == size1 and size1 > 0

    # --- scratch staging -------------------------------------------------------

    @staticmethod
    def _copy_tree(src: Path, dst: Path, cancelled) -> None:
        """Recursive copy, resumable: a destination file that already exists at
        the same size is skipped (a retried stage picks up where it left off)."""
        for dirpath, _dirnames, filenames in os.walk(src):
            rel = os.path.relpath(dirpath, src)
            target = dst if rel == "." else dst / rel
            target.mkdir(parents=True, exist_ok=True)
            for name in filenames:
                if cancelled():
                    raise ProcessorError("cancelled during copy")
                s = Path(dirpath) / name
                d = target / name
                try:
                    if d.exists() and d.stat().st_size == s.stat().st_size:
                        continue
                    shutil.copy2(s, d)
                except OSError as exc:
                    raise ProcessorError(f"copy failed {s} -> {d}: {exc}")

    def _stage_out(self, ctx: JobContext, scratch: Path, nas: Path, cancelled) -> None:
        """Copy everything Pix4D produced (all of scratch except the PPK input we
        staged in) back to the NAS project folder."""
        nas.mkdir(parents=True, exist_ok=True)
        for entry in scratch.iterdir():
            if entry.name.lower() == PPK_DIR.lower():
                continue
            if entry.is_dir():
                self._copy_tree(entry, nas / entry.name, cancelled)
            else:
                if cancelled():
                    raise ProcessorError("cancelled during copy")
                try:
                    shutil.copy2(entry, nas / entry.name)
                except OSError as exc:
                    raise ProcessorError(f"copy failed {entry} -> {nas / entry.name}: {exc}")

    # --- hooks -----------------------------------------------------------------

    def preflight(self, ctx: JobContext) -> list[str]:
        errors = missing_params(ctx, ["project_name", "project_root", "tat_path"])
        errors += check_payload_exe(self.cfg, PAYLOAD_KEY)
        root = ctx.parameters.get("project_root", "")
        if root:
            if not Path(root).is_dir():
                errors.append(f"project_root not reachable: {root}")
            elif not (Path(root) / PPK_DIR).is_dir():
                errors.append(f"PPK folder missing under project_root: {root}/PPK "
                              "(TERRA_PPK output expected)")
        tat = ctx.parameters.get("tat_path", "")
        if tat and not Path(tat).is_file():
            errors.append(f"tat_path not reachable: {tat}")
        return errors

    def prepare(self, ctx: JobContext, cancelled) -> None:
        """Stage the run onto the scratch drive: copy PPK (+ the TAT csv) local
        so Pix4D never touches the AV-scanned NAS during processing. No-op when
        no scratch_dir is configured."""
        scratch = self._scratch_root(ctx)
        if scratch is None:
            return
        nas = self._nas_root(ctx)
        ppk_src = nas / PPK_DIR
        if not ppk_src.is_dir():
            raise ProcessorError(f"PPK folder not found to stage: {ppk_src}")
        scratch.mkdir(parents=True, exist_ok=True)
        self._copy_tree(ppk_src, scratch / PPK_DIR, cancelled)
        tat = str(ctx.parameters.get("tat_path", "") or "")
        if tat and Path(tat).is_file():
            try:
                shutil.copy2(tat, scratch / Path(tat).name)
            except OSError as exc:
                raise ProcessorError(f"could not stage TAT csv: {exc}")

    def build_command(self, ctx: JobContext) -> list[str]:
        p = ctx.parameters
        cmd = [
            str(payload_exe(self.cfg, PAYLOAD_KEY)),
            "--project-name", str(p["project_name"]),
            "--project-root", str(self._run_root(ctx)),
            "--tat-path",     self._tat_for_run(ctx),
        ]
        if p.get("epsg_h"):
            cmd += ["--epsg-h", str(p["epsg_h"])]
        if p.get("epsg_v"):
            cmd += ["--epsg-v", str(p["epsg_v"])]
        cmd += ["--log-file", str(ctx.work_dir / "script.log"), "--unattended"]
        return cmd

    def poll(self, ctx: JobContext, elapsed_seconds: float) -> Optional[Progress]:
        root = self._run_root(ctx)
        app_log = self._newest_app_log(ctx, root)
        line = (tail_last_line(app_log) if app_log else "") \
            or tail_last_line(ctx.work_dir / "script.log") \
            or tail_last_line(ctx.log_path)
        if line:
            return Progress(percent=None, stage="pix4dmatic", message=line)
        return None

    def after_exit(self, ctx: JobContext, cancelled) -> None:
        """Wait until the run is complete on the run root, then (if staging) copy
        the project back to the NAS, verify the ortho landed, and delete the
        scratch copy. Left in place on failure for retry/inspection."""
        run_root = self._run_root(ctx)
        poll_seconds = float(ctx.parameters.get("completion_poll_seconds", 30))
        start = ctx.started_wall or time.time()
        deadline = start + ctx.max_runtime_seconds

        while not self._run_looks_complete(ctx, run_root):
            if cancelled():
                return
            if self._log_matches(ctx, run_root, "failure_pattern"):
                raise ProcessorError(
                    "Pix4Dmatic log matched the failure pattern "
                    f"({ctx.parameters.get('failure_pattern')})")
            if time.time() > deadline:
                raise ProcessorError(
                    f"Pix4Dmatic run incomplete after max runtime "
                    f"({ctx.max_runtime_seconds / 3600:.1f} h): no fresh orthomosaic "
                    f"matching '{ctx.parameters.get('ortho_glob', DEFAULT_ORTHO_GLOB)}'")
            time.sleep(poll_seconds)

        scratch = self._scratch_root(ctx)
        if scratch is None:
            return  # ran in place on the NAS; nothing to move
        if cancelled():
            return
        nas = self._nas_root(ctx)
        self._stage_out(ctx, scratch, nas, cancelled)
        # Only drop the scratch copy once the deliverable is verified on the NAS.
        if not self._fresh_orthos(ctx, nas):
            raise ProcessorError(
                "Pix4D outputs were copied to the NAS but no orthomosaic matching "
                f"'{ctx.parameters.get('ortho_glob', DEFAULT_ORTHO_GLOB)}' is present "
                f"under {nas} — leaving scratch copy at {scratch}")
        shutil.rmtree(scratch, ignore_errors=True)

    def validate_outputs(self, ctx: JobContext) -> Validation:
        # Validate the final home (the NAS). When staging, after_exit has already
        # copied outputs here; without staging this is the run root.
        nas = self._nas_root(ctx)
        errors: list[str] = []
        min_bytes = float(ctx.parameters.get("ortho_min_mb", 10)) * 1024 * 1024
        orthos = self._fresh_orthos(ctx, nas, min_bytes)
        if not orthos:
            errors.append(
                f"no fresh orthomosaic >= {min_bytes / 1024 / 1024:.0f} MB matching "
                f"'{ctx.parameters.get('ortho_glob', DEFAULT_ORTHO_GLOB)}' under {nas}")
        if ctx.parameters.get("failure_pattern") and \
                self._log_matches(ctx, nas, "failure_pattern"):
            errors.append("Pix4Dmatic log contains the failure pattern")

        if errors:
            return Validation(ok=False, errors=errors)
        return Validation(
            ok=True,
            outputs=[str(p) for p in orthos],
            summary={"ortho_count": len(orthos),
                     "largest_ortho_bytes": max(p.stat().st_size for p in orthos)},
        )
