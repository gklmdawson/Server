"""Terra processor tests: command construction, preflight, the report.md
completion watch, and output validation — all against a fake filesystem."""
import time

import pytest

from agent.config import AgentConfig
from processors import build_registry
from processors.base import JobContext, ProcessorError
from processors.terra_lidar import TerraLidarProcessor
from processors.terra_ppk import TerraPpkProcessor


@pytest.fixture
def cfg(tmp_path):
    ppk_exe = tmp_path / "DJI_AUTOMATE_PPK.exe"
    ui_exe = tmp_path / "DJI_AUTOMATE_UI.exe"
    ppk_exe.write_bytes(b"exe")
    ui_exe.write_bytes(b"exe")
    return AgentConfig(
        node_name="T", work_root=str(tmp_path / "work"),
        capabilities=["TERRA_PPK", "TERRA_LIDAR"],
        payload_paths={"dji_automate_ppk": str(ppk_exe),
                       "dji_automate_ui": str(ui_exe)},
    )


def make_ctx(tmp_path, params, max_seconds=3600.0):
    work_dir = tmp_path / "jobdir"
    work_dir.mkdir(parents=True, exist_ok=True)
    return JobContext(job_uuid="j1", job_type="X", parameters=params,
                      work_dir=work_dir, log_path=work_dir / "payload.log",
                      max_runtime_seconds=max_seconds,
                      started_wall=time.time())


def _ppk_params(tmp_path, **extra):
    src = tmp_path / "flight"
    src.mkdir(exist_ok=True)
    (tmp_path / "date").mkdir(exist_ok=True)
    return {
        "project_name": "Brahma_SilverPeak_10Jul2026",
        "data_source": str(src),
        "terra_path": str(tmp_path / "date" / "Terra"),
        "ppk_path": str(tmp_path / "date" / "PPK"),
        "epsg_h": "6523", "epsg_v": "6360",
        **extra,
    }


def test_registry_includes_terra_processors(cfg):
    registry = build_registry(cfg, ["TERRA_PPK", "TERRA_LIDAR"])
    assert isinstance(registry["TERRA_PPK"], TerraPpkProcessor)
    assert isinstance(registry["TERRA_LIDAR"], TerraLidarProcessor)
    assert registry["TERRA_PPK"].requires_desktop


# --- TERRA_PPK ----------------------------------------------------------------

def test_ppk_build_command(cfg, tmp_path):
    proc = TerraPpkProcessor(cfg)
    ctx = make_ctx(tmp_path, _ppk_params(tmp_path))
    cmd = proc.build_command(ctx)
    assert cmd[0].endswith("DJI_AUTOMATE_PPK.exe")
    assert "--project-name" in cmd and "--ppk-path" in cmd
    assert cmd[cmd.index("--epsg-h") + 1] == "6523"
    assert "--unattended" in cmd
    assert "--log-file" in cmd


def test_ppk_build_command_omits_empty_epsg(cfg, tmp_path):
    proc = TerraPpkProcessor(cfg)
    ctx = make_ctx(tmp_path, _ppk_params(tmp_path, epsg_h="", epsg_v=""))
    cmd = proc.build_command(ctx)
    assert "--epsg-h" not in cmd and "--epsg-v" not in cmd


def test_ppk_preflight(cfg, tmp_path):
    proc = TerraPpkProcessor(cfg)
    assert proc.preflight(make_ctx(tmp_path, _ppk_params(tmp_path))) == []

    bad = _ppk_params(tmp_path, data_source=str(tmp_path / "nope"))
    errors = proc.preflight(make_ctx(tmp_path, bad))
    assert any("data_source" in e for e in errors)

    errors = proc.preflight(make_ctx(tmp_path, {}))
    assert any("project_name" in e for e in errors)

    cfg.payload_paths["dji_automate_ppk"] = str(tmp_path / "missing.exe")
    errors = proc.preflight(make_ctx(tmp_path, _ppk_params(tmp_path)))
    assert any("payload not found" in e for e in errors)


def test_ppk_validation(cfg, tmp_path):
    proc = TerraPpkProcessor(cfg)
    params = _ppk_params(tmp_path)
    ctx = make_ctx(tmp_path, params)
    ppk = tmp_path / "date" / "PPK"
    (ppk / "Flight1").mkdir(parents=True)

    # Nothing there yet -> both checks fail.
    v = proc.validate_outputs(ctx)
    assert not v.ok and len(v.errors) == 2

    (ppk / "POS.txt").write_text("photo,lat,lon\n")
    (ppk / "Flight1" / "img_0001.JPG").write_bytes(b"j" * 10)
    (ppk / "Flight1" / "img_0002.jpg").write_bytes(b"j" * 10)
    v = proc.validate_outputs(ctx)
    assert v.ok
    assert v.summary["embedded_images"] == 2

    # Stale POS.txt (older than job start) is rejected.
    ctx.started_wall = time.time() + 3600
    v = proc.validate_outputs(ctx)
    assert not v.ok and any("predates" in e for e in v.errors)


# --- TERRA_LIDAR ----------------------------------------------------------------

def _lidar_params(tmp_path, **extra):
    src = tmp_path / "L3data"
    src.mkdir(exist_ok=True)
    terra = tmp_path / "date" / "Terra"
    terra.parent.mkdir(parents=True, exist_ok=True)
    gcp = tmp_path / "targets.csv"
    gcp.write_text("p1,1,2,3,TLT\n")
    return {
        "project_name": "Brahma_SilverPeak_10Jul2026",
        "project_location": str(terra),
        "data_source": str(src),
        "epsg_h": "6523", "epsg_v": "6360",
        "gcp_path": str(gcp),
        **extra,
    }


def test_lidar_build_command_with_gcp(cfg, tmp_path):
    proc = TerraLidarProcessor(cfg)
    cmd = proc.build_command(make_ctx(tmp_path, _lidar_params(tmp_path)))
    assert cmd[0].endswith("DJI_AUTOMATE_UI.exe")
    assert "--gcp-path" in cmd and "--no-targets" not in cmd
    assert "--unattended" in cmd


def test_lidar_build_command_no_targets(cfg, tmp_path):
    proc = TerraLidarProcessor(cfg)
    params = _lidar_params(tmp_path, no_targets=True)
    cmd = proc.build_command(make_ctx(tmp_path, params))
    assert "--no-targets" in cmd and "--gcp-path" not in cmd


def test_lidar_after_exit_returns_when_report_exists(cfg, tmp_path):
    proc = TerraLidarProcessor(cfg)
    params = _lidar_params(tmp_path, completion_poll_seconds=0.05)
    ctx = make_ctx(tmp_path, params)
    report = proc._report_path(ctx)
    report.parent.mkdir(parents=True)
    report.write_text("# done")
    proc.after_exit(ctx, cancelled=lambda: False)  # returns without raising


def test_lidar_after_exit_appears_later(cfg, tmp_path):
    import threading
    proc = TerraLidarProcessor(cfg)
    params = _lidar_params(tmp_path, completion_poll_seconds=0.05)
    ctx = make_ctx(tmp_path, params, max_seconds=30)
    report = proc._report_path(ctx)

    def write_soon():
        time.sleep(0.3)
        report.parent.mkdir(parents=True)
        report.write_text("# done")

    threading.Thread(target=write_soon, daemon=True).start()
    start = time.monotonic()
    proc.after_exit(ctx, cancelled=lambda: False)
    assert 0.2 < time.monotonic() - start < 10


def test_lidar_after_exit_times_out(cfg, tmp_path):
    proc = TerraLidarProcessor(cfg)
    params = _lidar_params(tmp_path, completion_poll_seconds=0.05)
    ctx = make_ctx(tmp_path, params, max_seconds=0.3)
    with pytest.raises(ProcessorError, match="report.md never appeared"):
        proc.after_exit(ctx, cancelled=lambda: False)


def test_lidar_after_exit_honors_cancel(cfg, tmp_path):
    proc = TerraLidarProcessor(cfg)
    params = _lidar_params(tmp_path, completion_poll_seconds=0.05)
    ctx = make_ctx(tmp_path, params, max_seconds=3600)
    start = time.monotonic()
    proc.after_exit(ctx, cancelled=lambda: True)
    assert time.monotonic() - start < 5


def test_lidar_validation(cfg, tmp_path):
    proc = TerraLidarProcessor(cfg)
    params = _lidar_params(tmp_path, min_las_mb=0.0001)
    ctx = make_ctx(tmp_path, params)

    v = proc.validate_outputs(ctx)
    assert not v.ok

    project = proc._terra_project_dir(ctx)
    (project / "lidars" / "report").mkdir(parents=True)
    (project / "lidars" / "report" / "report.md").write_text("# report")
    laz_dir = project / "lidars" / "terra_laz"
    laz_dir.mkdir(parents=True)
    (laz_dir / "cloud_merged.laz").write_bytes(b"L" * 4096)

    v = proc.validate_outputs(ctx)
    assert v.ok
    assert v.summary["las_count"] == 1
    assert any("report.md" in o for o in v.outputs)

    # A too-small LAS with a real minimum fails.
    ctx.parameters["min_las_mb"] = 1
    v = proc.validate_outputs(ctx)
    assert not v.ok and any("LAS/LAZ" in e for e in v.errors)
