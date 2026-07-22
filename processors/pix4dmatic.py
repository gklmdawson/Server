"""PIX4D_MATIC processor — wraps PIX4D_AUTOMATE.exe (AutomatePix4D.py).

Today the payload exits right after clicking Start, so after_exit keeps
watching until the run is demonstrably complete. Two signals, both
job-parameter configurable because the exact Pix4Dmatic layout gets pinned
down on the machine during Phase 4 calibration:

  * completion_log_glob + completion_pattern / failure_pattern — Pix4Dmatic
    writes rich stage logging; when configured, the newest matching log file
    is tailed for progress messages, an early failure signal, and completion.
  * ortho_glob (default "Pix4[dD]/**/exports/*ortho*.tif*") — the orthomosaic in
    the project's exports folder is the final export; validation requires it
    fresh and over ortho_min_mb.

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

Save + close: the import payload exits right after clicking Start and can't tell
when Pix4Dmatic has finished — after_exit is what detects completion (the ortho
export landing). So once the run looks complete, after_exit runs the
save-and-close payload (AutomatePix4D --save-close) to save the project and close
the app before results are staged back and the machine is freed. The exe is
payload_paths.pix4d_save_close if set, else payload_paths.pix4d_automate (the
same PIX4D_AUTOMATE.exe) — so this needs no extra config on a box that already
runs the import. It's best-effort and logged step by step: a launch error,
timeout, or non-zero exit is written to the job log but doesn't fail the job (the
ortho already exists). To test just this step against a manually-opened project,
run  PIX4D_AUTOMATE.exe --save-close  (add --no-close to save without closing).

Job parameters: project_name, project_root (the date folder), epsg_h, epsg_v,
tat_path (the targets/TAT csv, used as-is).
Optional: ortho_glob, ortho_min_mb (default 10), completion_poll_seconds (30),
completion_log_glob, completion_pattern, failure_pattern.
Agent config (optional): payload_paths.pix4d_save_close overrides which exe runs
the save + close (defaults to pix4d_automate).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
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
# Save-and-close payload, run once the ortho export proves the run is complete
# (AutomatePix4D --save-close). Normally the same PIX4D_AUTOMATE.exe; kept a
# separate payload_paths key so the step is opt-in per machine and stays off
# where it isn't configured. See automation/AutomatePix4D.py save_and_close().
SAVE_CLOSE_KEY = "pix4d_save_close"
SAVE_CLOSE_TIMEOUT_SECONDS = 300
# Pix4Dmatic exports to <project_root>/Pix4D/<project>/exports/<name>-orthomosaic.tiff
# (AutomatePix4D sets the project Path to <project_root>/Pix4D). Require the
# exports folder (the final deliverable, not an intermediate ortho), accept
# .tif or .tiff, and match either folder case (Pix4D / Pix4d).
DEFAULT_ORTHO_GLOB = "Pix4[dD]/**/exports/*ortho*.tif*"
PPK_DIR = "PPK"
# The project-output subfolder AutomatePix4D types into the Path field; intake
# pre-creates it on the NAS, so the scratch run needs it created too. Intake
# names it "Pix4d" for every sensor type.
PIX4D_OUT_DIR = "Pix4d"


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

    # --- on-machine save + close ----------------------------------------------

    @staticmethod
    def _note(ctx: JobContext, msg: str) -> None:
        """Append an informational line to the job log — after_exit has no
        progress channel of its own, and these save/close notes are worth
        keeping next to the payload output an operator reads."""
        try:
            with open(ctx.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[pix4dmatic] {msg}\n")
        except OSError:
            pass

    def _save_close_exe(self) -> tuple[Optional[Path], str]:
        """The exe to run for save+close, and which config key it came from.

        Prefer an explicit payload_paths.pix4d_save_close, but FALL BACK to the
        import payload payload_paths.pix4d_automate — it's the same
        PIX4D_AUTOMATE.exe, so save/close works on any box that can run the
        import without needing a second config key. (The missing-key silent skip
        is exactly what left Pix4Dmatic open after a completed run.)"""
        exe = payload_exe(self.cfg, SAVE_CLOSE_KEY)
        if exe is not None:
            return exe, SAVE_CLOSE_KEY
        return payload_exe(self.cfg, PAYLOAD_KEY), PAYLOAD_KEY

    def _save_and_close(self, ctx: JobContext, cancelled) -> None:
        """Save the Pix4D project and close the app on this machine now that the
        run is complete. Runs the save-close payload (AutomatePix4D --save-close),
        preferring payload_paths.pix4d_save_close and falling back to
        payload_paths.pix4d_automate. Best-effort and heavily logged: a launch
        error, timeout, or non-zero exit is written to the job log but does NOT
        fail the job, since the orthomosaic deliverable already exists on disk."""
        if cancelled():
            self._note(ctx, "save/close SKIPPED: job was cancelled.")
            return
        exe, key = self._save_close_exe()
        if exe is None or not exe.is_file():
            self._note(ctx, f"save/close SKIPPED: no payload exe found — set "
                            f"payload_paths.{PAYLOAD_KEY} (or .{SAVE_CLOSE_KEY}). "
                            f"Pix4Dmatic left open. (resolved: {exe!r})")
            return

        from agent.runner import NO_WINDOW, kill_tree  # local import avoids a cycle
        save_close_log = ctx.work_dir / "save_close.log"
        cmd = [str(exe), "--save-close", "--unattended", "--log-file", str(save_close_log)]
        self._note(ctx, f"save/close START: run complete → {exe.name} --save-close "
                        f"(payload_paths.{key}); step-by-step detail in {save_close_log}")
        deadline = time.time() + SAVE_CLOSE_TIMEOUT_SECONDS
        outcome = "ok"
        try:
            with open(ctx.log_path, "ab") as log_file:
                proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
                self._note(ctx, f"save/close: launched PID {proc.pid}, waiting up to "
                                f"{SAVE_CLOSE_TIMEOUT_SECONDS}s for it to finish.")
                while proc.poll() is None:
                    if cancelled():
                        kill_tree(proc.pid)
                        outcome = "cancelled"
                        break
                    if time.time() > deadline:
                        kill_tree(proc.pid)
                        outcome = "timeout"
                        break
                    time.sleep(1)
        except OSError as exc:
            self._note(ctx, f"save/close FAILED to launch {exe}: {exc} — "
                            "Pix4Dmatic left open.")
            return
        if outcome == "cancelled":
            self._note(ctx, "save/close cancelled mid-run; Pix4Dmatic may still be open.")
            return
        if outcome == "timeout":
            self._note(ctx, f"save/close TIMED OUT after {SAVE_CLOSE_TIMEOUT_SECONDS}s; "
                            f"Pix4Dmatic may still be open. See {save_close_log}.")
        elif proc.returncode not in (0, None):
            self._note(ctx, f"save/close payload EXITED {proc.returncode}; Pix4Dmatic "
                            f"may still be open. See {save_close_log} for the reason.")
        else:
            self._note(ctx, "save/close DONE: project saved and Pix4Dmatic closed.")

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
            self._note(ctx, "no scratch_dir configured — Pix4Dmatic will read images "
                            "directly from the NAS (images are NOT staged to a local "
                            "drive). Set scratch_dir to process off local disk.")
            return
        nas = self._nas_root(ctx)
        ppk_src = nas / PPK_DIR
        if not ppk_src.is_dir():
            raise ProcessorError(f"PPK folder not found to stage: {ppk_src}")
        scratch.mkdir(parents=True, exist_ok=True)
        self._copy_tree(ppk_src, scratch / PPK_DIR, cancelled)
        self._note(ctx, f"staged PPK images to local scratch for processing: "
                        f"{scratch / PPK_DIR} (originals stay on the NAS at {ppk_src})")
        # AutomatePix4D types <project_root>/Pix4D into the project Path field and
        # relies on that folder existing (intake makes it on the NAS); create it
        # on the scratch root too so the run behaves the same.
        (scratch / PIX4D_OUT_DIR).mkdir(exist_ok=True)
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

        # Run is complete: save the project and close Pix4Dmatic on this machine
        # before results are staged back / the box is freed. Done here (not in
        # the payload) because the import payload has long since exited — this
        # processor is what knows the run finished.
        self._save_and_close(ctx, cancelled)

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
        # Delete the local copy (staged images + working files). Pix4Dmatic was
        # closed above, so its file locks are released and the removal should
        # succeed; verify it and surface a warning if anything remains, rather
        # than silently leaving images on the local drive.
        shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            self._note(ctx, f"WARNING: local scratch copy not fully removed: {scratch} "
                            "— staged images may still be on the local drive.")
        else:
            self._note(ctx, f"removed local scratch copy (staged images deleted): {scratch}")

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
