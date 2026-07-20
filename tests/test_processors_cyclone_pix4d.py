"""Cyclone and Pix4Dmatic processor tests. Cyclone runs against a fake
3DR.exe (a tiny script honoring the real CLI contract); Pix4D against a fake
project tree with configurable ortho/log signals."""
import os
import stat
import sys
import threading
import time

import pytest

from agent.config import AgentConfig
from agent.runner import JobRunner
from processors import build_registry
from processors.base import JobContext, ProcessorError
from processors.cyclone_classify import CycloneClassifyProcessor
from processors.pix4dmatic import Pix4dMaticProcessor
from tests.test_agent_runner import FakeClient

FAKE_3DR = f"""#!{sys.executable}
import re, sys, pathlib
param = [a for a in sys.argv if a.startswith("--scriptParam=")][0]
input_file = re.search(r"inputFile='([^']+)'", param).group(1)
las = pathlib.Path(input_file)
calls = pathlib.Path(__file__).parent / "calls.txt"
with open(calls, "a") as fh:
    fh.write(las.name + "\\n")
if "bad" not in las.name:
    las.with_suffix(".3dr").write_text("classified")
"""


@pytest.fixture
def cyclone_env(tmp_path):
    exe = tmp_path / "fake3dr.py"
    exe.write_text(FAKE_3DR)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    script = tmp_path / "ClassifyLAZ.js"
    script.write_text("// js")
    cfg = AgentConfig(node_name="C", work_root=str(tmp_path / "work"),
                      capabilities=["CYCLONE_CLASSIFY"],
                      payload_paths={"cyclone_3dr_exe": str(exe),
                                     "cyclone_classify_script": str(script)})
    cfg.ensure_dirs()

    terra = tmp_path / "date" / "Terra"
    project = terra / "Job_LiDAR"
    (project / "lidars" / "report").mkdir(parents=True)
    (project / "lidars" / "report" / "report.md").write_text("# done")
    (project / "lidars" / "terra_laz").mkdir(parents=True)

    params = {"terra_folder": str(terra), "project_name": "Job_LiDAR",
              "model_name": "Heavy Construction UAV 2.0",
              "poll_seconds": 0.05, "per_file_timeout_hours": 0.01}
    return cfg, terra, project / "lidars" / "terra_laz", params, exe


def _ctx(tmp_path, params, max_seconds=120.0):
    work = tmp_path / "jobdir"
    work.mkdir(parents=True, exist_ok=True)
    return JobContext(job_uuid="j1", job_type="CYCLONE_CLASSIFY",
                      parameters=params, work_dir=work,
                      log_path=work / "payload.log",
                      max_runtime_seconds=max_seconds,
                      started_wall=time.time())


def _calls(exe):
    calls = exe.parent / "calls.txt"
    return calls.read_text().splitlines() if calls.exists() else []


# --- Cyclone ---------------------------------------------------------------

def test_cyclone_select_files_merged_rule(cyclone_env, tmp_path):
    cfg, terra, laz_dir, params, exe = cyclone_env
    proc = CycloneClassifyProcessor(cfg)
    (laz_dir / "tile_1.laz").write_bytes(b"L" * 100)
    (laz_dir / "tile_2.laz").write_bytes(b"L" * 100)
    (laz_dir / "cloud_merged.laz").write_bytes(b"L" * 100)

    # Small merged cloud -> classify only the merged file.
    ctx = _ctx(tmp_path, params)
    assert [p.name for p in proc.select_files(ctx)] == ["cloud_merged.laz"]

    # Merged over the threshold -> tiles only.
    ctx.parameters["merged_threshold_gb"] = 1e-9
    names = [p.name for p in proc.select_files(ctx)]
    assert "cloud_merged.laz" not in names and len(names) == 2


def test_cyclone_preflight_requires_report(cyclone_env, tmp_path):
    cfg, terra, laz_dir, params, exe = cyclone_env
    proc = CycloneClassifyProcessor(cfg)
    assert proc.preflight(_ctx(tmp_path, params)) == []
    (terra / "Job_LiDAR" / "lidars" / "report" / "report.md").unlink()
    errors = proc.preflight(_ctx(tmp_path, params))
    assert any("report.md" in e for e in errors)


def test_cyclone_ready_when_3dr_not_running(cyclone_env):
    cfg, *_ = cyclone_env
    assert CycloneClassifyProcessor(cfg).ready() == []


def test_cyclone_classifies_all_files(cyclone_env, tmp_path):
    cfg, terra, laz_dir, params, exe = cyclone_env
    proc = CycloneClassifyProcessor(cfg)
    (laz_dir / "tile_1.laz").write_bytes(b"L" * 100)
    (laz_dir / "tile_2.laz").write_bytes(b"L" * 100)

    progress_msgs = []
    v = proc.run_custom(_ctx(tmp_path, params),
                        lambda pct, stage, msg: progress_msgs.append(msg),
                        cancelled=lambda: False)
    assert v.ok, v.errors
    assert (laz_dir / "tile_1.3dr").is_file()
    assert (laz_dir / "tile_2.3dr").is_file()
    assert v.summary == {"classified": 2, "total": 2}
    assert any("classifying" in m for m in progress_msgs)


def test_cyclone_resume_skips_done_files(cyclone_env, tmp_path):
    cfg, terra, laz_dir, params, exe = cyclone_env
    proc = CycloneClassifyProcessor(cfg)
    (laz_dir / "tile_1.laz").write_bytes(b"L" * 100)
    (laz_dir / "tile_2.laz").write_bytes(b"L" * 100)
    time.sleep(0.02)
    (laz_dir / "tile_1.3dr").write_text("already classified")  # fresh output

    v = proc.run_custom(_ctx(tmp_path, params), lambda *a: None, lambda: False)
    assert v.ok
    assert _calls(exe) == ["tile_2.laz"], "already-classified file must be skipped"


def test_cyclone_partial_failure_lists_files(cyclone_env, tmp_path):
    cfg, terra, laz_dir, params, exe = cyclone_env
    proc = CycloneClassifyProcessor(cfg)
    (laz_dir / "tile_good.laz").write_bytes(b"L" * 100)
    (laz_dir / "bad_tile.laz").write_bytes(b"L" * 100)

    v = proc.run_custom(_ctx(tmp_path, params), lambda *a: None, lambda: False)
    assert not v.ok
    assert "bad_tile" in v.errors[0]
    assert (laz_dir / "tile_good.3dr").is_file(), "good file's output is kept"
    assert v.summary["classified"] == 1


def test_cyclone_cancel_stops_early(cyclone_env, tmp_path):
    cfg, terra, laz_dir, params, exe = cyclone_env
    proc = CycloneClassifyProcessor(cfg)
    (laz_dir / "tile_1.laz").write_bytes(b"L" * 100)
    v = proc.run_custom(_ctx(tmp_path, params), lambda *a: None, lambda: True)
    assert not v.ok and v.errors == ["cancelled"]


def test_cyclone_through_runner_custom_path(cyclone_env, tmp_path):
    cfg, terra, laz_dir, params, exe = cyclone_env
    (laz_dir / "tile_1.laz").write_bytes(b"L" * 100)
    client = FakeClient()
    registry = build_registry(cfg, ["CYCLONE_CLASSIFY"])
    runner = JobRunner(cfg, client, registry)
    ctx = runner._make_context("job-cy", "CYCLONE_CLASSIFY", params, 60)
    runner._execute(ctx, registry["CYCLONE_CLASSIFY"])

    kinds = client.kinds()
    assert kinds[0] == "started" and kinds[-1] == "succeeded"
    assert not cfg.state_file.exists()


# --- Pix4Dmatic ---------------------------------------------------------------

@pytest.fixture
def pix4d_env(tmp_path):
    exe = tmp_path / "PIX4D_AUTOMATE.exe"
    exe.write_bytes(b"exe")
    cfg = AgentConfig(node_name="P", work_root=str(tmp_path / "work"),
                      capabilities=["PIX4D_MATIC"],
                      payload_paths={"pix4d_automate": str(exe)})
    root = tmp_path / "date"
    (root / "PPK").mkdir(parents=True)
    (root / "Pix4D").mkdir(parents=True)
    tat = tmp_path / "targets.csv"
    tat.write_text("p1,1,2,3\n")
    params = {"project_name": "Job", "project_root": str(root),
              "tat_path": str(tat), "epsg_h": "6523", "epsg_v": "6360",
              "completion_poll_seconds": 0.05, "ortho_min_mb": 0.00001}
    return cfg, root, params


def test_pix4d_build_command(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env
    proc = Pix4dMaticProcessor(cfg)
    cmd = proc.build_command(_ctx(tmp_path, params))
    assert cmd[0].endswith("PIX4D_AUTOMATE.exe")
    assert cmd[cmd.index("--tat-path") + 1].endswith("targets.csv")
    assert "--unattended" in cmd


def test_pix4d_preflight_requires_ppk_folder(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env
    proc = Pix4dMaticProcessor(cfg)
    assert proc.preflight(_ctx(tmp_path, params)) == []
    import shutil
    shutil.rmtree(root / "PPK")
    errors = proc.preflight(_ctx(tmp_path, params))
    assert any("PPK folder missing" in e for e in errors)


def test_pix4d_after_exit_completes_when_ortho_appears(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env
    proc = Pix4dMaticProcessor(cfg)
    ctx = _ctx(tmp_path, params, max_seconds=60)
    ortho = root / "Pix4D" / "job_ortho.tif"

    def write_soon():
        time.sleep(0.3)
        ortho.write_bytes(b"T" * 2048)

    threading.Thread(target=write_soon, daemon=True).start()
    proc.after_exit(ctx, cancelled=lambda: False)  # returns once ortho is stable
    assert ortho.is_file()


def test_pix4d_after_exit_times_out_without_ortho(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env
    proc = Pix4dMaticProcessor(cfg)
    ctx = _ctx(tmp_path, params, max_seconds=0.3)
    with pytest.raises(ProcessorError, match="no fresh orthomosaic"):
        proc.after_exit(ctx, cancelled=lambda: False)


def test_pix4d_after_exit_fails_fast_on_log_failure_pattern(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env
    params.update(completion_log_glob="Pix4D/*.log",
                  failure_pattern="processing failed")
    (root / "Pix4D" / "app.log").write_text("[10:00] ERROR processing failed\n")
    proc = Pix4dMaticProcessor(cfg)
    ctx = _ctx(tmp_path, params, max_seconds=60)
    with pytest.raises(ProcessorError, match="failure pattern"):
        proc.after_exit(ctx, cancelled=lambda: False)


def test_pix4d_completion_pattern_short_circuits(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env
    params.update(completion_log_glob="Pix4D/*.log",
                  completion_pattern=r"Processing finished")
    (root / "Pix4D" / "job_ortho.tif").write_bytes(b"T" * 2048)
    (root / "Pix4D" / "app.log").write_text("[11:00] Processing finished OK\n")
    proc = Pix4dMaticProcessor(cfg)
    start = time.monotonic()
    proc.after_exit(_ctx(tmp_path, params, max_seconds=60), cancelled=lambda: False)
    assert time.monotonic() - start < 1.5, "log completion should skip the stability wait"


def test_pix4d_validation(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env
    proc = Pix4dMaticProcessor(cfg)
    ctx = _ctx(tmp_path, params)

    v = proc.validate_outputs(ctx)
    assert not v.ok

    (root / "Pix4D" / "job_ortho.tif").write_bytes(b"T" * 2048)
    v = proc.validate_outputs(ctx)
    assert v.ok and v.summary["ortho_count"] == 1

    ctx.started_wall = time.time() + 3600  # ortho older than job start -> stale
    v = proc.validate_outputs(ctx)
    assert not v.ok


# --- Pix4Dmatic scratch-drive staging -----------------------------------------

@pytest.fixture
def pix4d_scratch_env(tmp_path):
    exe = tmp_path / "PIX4D_AUTOMATE.exe"
    exe.write_bytes(b"exe")
    scratch = tmp_path / "scratch"
    cfg = AgentConfig(node_name="P", work_root=str(tmp_path / "work"),
                      capabilities=["PIX4D_MATIC"],
                      payload_paths={"pix4d_automate": str(exe)},
                      scratch_dir=str(scratch))
    nas = tmp_path / "nas" / "date"
    (nas / "PPK").mkdir(parents=True)
    (nas / "PPK" / "img.jpg").write_bytes(b"jpeg" * 100)
    (nas / "PPK" / "POS.txt").write_text("pos")
    tat = tmp_path / "TAT.csv"
    tat.write_text("p1,1,2,3,TAT\n")
    params = {"project_name": "Job", "project_root": str(nas),
              "tat_path": str(tat), "completion_poll_seconds": 0.05,
              "ortho_min_mb": 0.00001}
    return cfg, nas, scratch / "Job", params


def test_pix4d_prepare_stages_ppk_and_tat_to_scratch(pix4d_scratch_env, tmp_path):
    cfg, nas, scratch_job, params = pix4d_scratch_env
    proc = Pix4dMaticProcessor(cfg)
    proc.prepare(_ctx(tmp_path, params), cancelled=lambda: False)

    assert (scratch_job / "PPK" / "img.jpg").is_file()
    assert (scratch_job / "PPK" / "POS.txt").is_file()
    assert (scratch_job / "TAT.csv").is_file()


def test_pix4d_build_command_points_at_scratch(pix4d_scratch_env, tmp_path):
    cfg, nas, scratch_job, params = pix4d_scratch_env
    proc = Pix4dMaticProcessor(cfg)
    ctx = _ctx(tmp_path, params)
    proc.prepare(ctx, cancelled=lambda: False)
    cmd = proc.build_command(ctx)
    assert cmd[cmd.index("--project-root") + 1] == str(scratch_job)
    # TAT was staged, so the command reads the local copy, not the NAS path.
    assert cmd[cmd.index("--tat-path") + 1] == str(scratch_job / "TAT.csv")


def test_pix4d_after_exit_copies_project_to_nas_and_clears_scratch(pix4d_scratch_env, tmp_path):
    cfg, nas, scratch_job, params = pix4d_scratch_env
    proc = Pix4dMaticProcessor(cfg)
    ctx = _ctx(tmp_path, params, max_seconds=60)
    proc.prepare(ctx, cancelled=lambda: False)

    # Simulate Pix4D producing the project + ortho on the scratch drive.
    (scratch_job / "Pix4D").mkdir(parents=True)
    (scratch_job / "Pix4D" / "job_ortho.tif").write_bytes(b"T" * 4096)
    (scratch_job / "Job.p4d").write_text("project")

    proc.after_exit(ctx, cancelled=lambda: False)

    # Project copied back to the NAS…
    assert (nas / "Pix4D" / "job_ortho.tif").is_file()
    assert (nas / "Job.p4d").is_file()
    # …the PPK input is NOT copied back (it already lives on the NAS)…
    assert not (nas / "PPK" / "PPK").exists()
    # …and the scratch copy is gone.
    assert not scratch_job.exists()

    assert proc.validate_outputs(ctx).ok


def test_pix4d_prepare_noop_without_scratch_dir(pix4d_env, tmp_path):
    cfg, root, params = pix4d_env  # this cfg has no scratch_dir
    proc = Pix4dMaticProcessor(cfg)
    # Should not raise and should not create any scratch tree.
    proc.prepare(_ctx(tmp_path, params), cancelled=lambda: False)
    assert proc._scratch_root(_ctx(tmp_path, params)) is None
