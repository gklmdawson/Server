"""INTAKE processor: folder build, resumable copy, base-data/RINEX handling,
and validation — all on a fake filesystem with a stub Trimble converter."""
import os
import sys
import time

import pytest

from agent.config import AgentConfig
from processors import build_registry
from processors.base import JobContext, Validation
from processors.intake import IntakeProcessor


# --- fixtures -----------------------------------------------------------------

def make_converter(tmp_path):
    """Executable stub for convertToRinex.exe: writes <input>.26o with a
    RINEX-ish header so obs detection and position patching are exercised."""
    conv_py = tmp_path / "conv.py"
    conv_py.write_text(
        "import sys, pathlib\n"
        "t = pathlib.Path(sys.argv[1])\n"
        "obs = t.with_suffix('.26o')\n"
        "obs.write_text('%60s%s\\n%73s%s\\n' % ('', 'APPROX POSITION XYZ', '', 'END OF HEADER'))\n"
    )
    if sys.platform == "win32":
        exe = tmp_path / "conv.bat"
        exe.write_text(f'@"{sys.executable}" "{conv_py}" %1\n')
    else:
        exe = tmp_path / "conv.sh"
        exe.write_text(f'#!/bin/sh\n"{sys.executable}" "{conv_py}" "$1"\n')
        exe.chmod(0o755)
    return exe


@pytest.fixture
def cfg(tmp_path):
    return AgentConfig(
        node_name="INTAKE-01", work_root=str(tmp_path / "work"),
        capabilities=["INTAKE"],
        payload_paths={"convert_to_rinex_exe": str(make_converter(tmp_path))},
    )


def make_sources(tmp_path, flight_name="DJI_202607100915_002"):
    src = tmp_path / "ingest" / flight_name
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(b"a" * 100)
    (src / "b.jpg").write_bytes(b"jpegdata")           # invalid image: EXIF is skipped
    (src / "sub" / "c.bin").write_bytes(b"c" * 50)
    base_dir = tmp_path / "ingest" / "base"
    base_dir.mkdir()
    (base_dir / "1234.T02").write_bytes(b"t02")
    return src, base_dir / "1234.T02"


def make_ctx(tmp_path, params, max_seconds=3600.0):
    work_dir = tmp_path / "jobdir"
    work_dir.mkdir(parents=True, exist_ok=True)
    return JobContext(job_uuid="j1", job_type="INTAKE", parameters=params,
                      work_dir=work_dir, log_path=work_dir / "payload.log",
                      max_runtime_seconds=max_seconds, started_wall=time.time())


def base_params(tmp_path, src, base, sensor="M3E", **extra):
    root = tmp_path / "nas"
    root.mkdir(exist_ok=True)
    return {
        "root_path": str(root), "client": "Brahma", "project": "SilverPeak",
        "date": "10Jul2026", "sensor_type": sensor,
        "source_folders": [str(src)], "base_data_paths": [str(base)],
        "base_data_is_rinex": False, "base_ecef_xyz": None,
        **extra,
    }


def run(proc, ctx, cancelled=lambda: False):
    calls = []
    validation = proc.run_custom(ctx, lambda *a: calls.append(a), cancelled)
    return validation, calls


# --- tests ----------------------------------------------------------------------

def test_registry_includes_intake(cfg):
    registry = build_registry(cfg, ["INTAKE"])
    assert isinstance(registry["INTAKE"], IntakeProcessor)
    assert not registry["INTAKE"].requires_desktop
    assert registry["INTAKE"].custom_execution


def test_full_run_builds_tree_copies_and_converts(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    ctx = make_ctx(tmp_path, base_params(tmp_path, src, base))
    validation, calls = run(IntakeProcessor(cfg), ctx)
    assert validation.ok, validation.errors

    date_dir = tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026"
    for sub in ("BaseData", "Pix4d", "Terra", "PPK", "M3E"):
        assert (date_dir / sub).is_dir()
    flight = date_dir / "M3E" / src.name
    assert (flight / "a.bin").read_bytes() == b"a" * 100
    assert (flight / "sub" / "c.bin").is_file()
    # Converter stub produced an obs; M3E copies it into each flight subfolder.
    assert (date_dir / "BaseData" / "1234.26o").is_file()
    assert (flight.parent / src.name / "1234.obs").is_file() or (flight / "1234.obs").is_file()
    assert validation.summary["files_present"] == validation.summary["files_total"] == 3
    assert calls, "expected progress reports"


def test_folder_tree_built_at_translated_root(tmp_path):
    """With a path_map (the Docker/NAS worker), the whole template tree —
    including the empty Terra/PPK/Pix4d/TerraArchive folders — must land at the
    machine-local root, not the raw UNC."""
    from processors.intake import IntakeCopyProcessor

    local_root = tmp_path / "nas_local"
    unc_root = "\\\\NAS\\3dData"
    cfg = AgentConfig(
        node_name="NAS-COPY", work_root=str(tmp_path / "work"),
        capabilities=["INTAKE_COPY"],
        path_map={unc_root: str(local_root)},
    )
    src, base = make_sources(tmp_path)
    params = {
        "root_path": unc_root, "client": "Brahma", "project": "SilverPeak",
        "date": "10Jul2026", "sensor_type": "L3",
        "source_folders": [str(src)], "base_data_paths": [str(base)],
        "base_data_is_rinex": False, "base_ecef_xyz": None,
    }
    ctx = make_ctx(tmp_path, params)
    validation, _ = run(IntakeCopyProcessor(cfg), ctx)
    assert validation.ok, validation.errors

    date_dir = local_root / "Brahma" / "SilverPeak" / "10Jul2026"
    for sub in ("BaseData", "PPK", "Pix4d", "TerraArchive", "Terra", "L3"):
        assert (date_dir / sub).is_dir(), f"{sub} not created under the translated root"


def test_run_splits_targets_into_tlt_and_tat(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    targets = tmp_path / "uploads" / "all_points.csv"
    targets.parent.mkdir(parents=True, exist_ok=True)
    targets.write_text("p1,1,2,3,TLT\np2,4,5,6,TAT\np3,7,8,9,MISC\np4,1,1,1,tlt\n")
    ctx = make_ctx(tmp_path, base_params(tmp_path, src, base,
                                         targets_upload=str(targets)))
    validation, _ = run(IntakeProcessor(cfg), ctx)
    assert validation.ok, validation.errors

    date_dir = tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026"
    tlt = (date_dir / "SINGLE_TLT.csv").read_text().splitlines()
    tat = (date_dir / "TAT.csv").read_text().splitlines()
    # SINGLE_TLT.csv = TLT only (p1, p4); TAT.csv = TAT + TLT (p1, p2, p4).
    assert [r.split(",")[0] for r in tlt] == ["p1", "p4"]
    assert [r.split(",")[0] for r in tat] == ["p1", "p2", "p4"]


def test_rerun_skips_existing_files(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    ctx = make_ctx(tmp_path, base_params(tmp_path, src, base))
    proc = IntakeProcessor(cfg)
    assert run(proc, ctx)[0].ok

    flight = tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026" / "M3E" / src.name
    (flight / "a.bin").unlink()
    assert run(proc, ctx)[0].ok
    # No dedup duplicates were created for unchanged files.
    assert not list(flight.glob("c_1.bin")) and not list((flight / "sub").glob("c_1.bin"))
    assert (flight / "a.bin").is_file()


def test_validation_fails_on_missing_copy(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    ctx = make_ctx(tmp_path, base_params(tmp_path, src, base))
    proc = IntakeProcessor(cfg)
    assert run(proc, ctx)[0].ok

    flight = tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026" / "M3E" / src.name
    (flight / "sub" / "c.bin").unlink()
    validation = proc.validate_outputs(ctx)
    assert not validation.ok
    assert "1/3" in validation.errors[0]


def test_lidar_sensor_renames_obs_next_to_rtk(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    (src / "flight.rtk").write_bytes(b"rtk")
    ctx = make_ctx(tmp_path, base_params(tmp_path, src, base, sensor="L2"))
    validation, _ = run(IntakeProcessor(cfg), ctx)
    assert validation.ok, validation.errors
    flight = tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026" / "L2" / src.name
    assert (flight / "flight.obs").is_file()
    assert (tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026" / "TerraArchive").is_dir()


def test_r3pro_copies_base_set_into_pos_base(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    ctx = make_ctx(tmp_path, base_params(tmp_path, src, base, sensor="R3Pro"))
    validation, _ = run(IntakeProcessor(cfg), ctx)
    assert validation.ok, validation.errors
    flight = tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026" / "R3Pro" / src.name
    assert (flight / "POS" / "base" / "1234.26o").is_file()


def test_rinex_base_data_skips_converter(tmp_path):
    """base_data_is_rinex=True must not need convertToRinex at all."""
    cfg = AgentConfig(node_name="I", work_root=str(tmp_path / "work"),
                      capabilities=["INTAKE"], payload_paths={})
    src, _ = make_sources(tmp_path)
    rinex_dir = tmp_path / "ingest" / "rinex"
    rinex_dir.mkdir()
    (rinex_dir / "base.26o").write_text("x")
    (rinex_dir / "base.26mix").write_text("nav")
    params = base_params(tmp_path, src, rinex_dir / "base.26o",
                         base_data_is_rinex=True)
    proc = IntakeProcessor(cfg)
    ctx = make_ctx(tmp_path, params)
    assert proc.preflight(ctx) == []
    validation, _ = run(proc, ctx)
    assert validation.ok, validation.errors
    base_folder = tmp_path / "nas" / "Brahma" / "SilverPeak" / "10Jul2026" / "BaseData"
    assert (base_folder / "base.26o").is_file()
    assert (base_folder / "base.26nav").is_file()       # mix renamed to nav


def test_preflight_catches_missing_pieces(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    proc = IntakeProcessor(cfg)

    params = base_params(tmp_path, src, base)
    params["source_folders"] = [str(tmp_path / "nope")]
    assert any("source folder not found" in e
               for e in proc.preflight(make_ctx(tmp_path, params)))

    params = base_params(tmp_path, src, base)
    no_conv = AgentConfig(node_name="I", work_root=str(tmp_path / "w2"),
                          capabilities=["INTAKE"], payload_paths={})
    errors = IntakeProcessor(no_conv).preflight(make_ctx(tmp_path, params))
    assert any("convert_to_rinex_exe" in e for e in errors)


def test_cancel_mid_copy_reports_cancelled(cfg, tmp_path):
    src, base = make_sources(tmp_path)
    ctx = make_ctx(tmp_path, base_params(tmp_path, src, base))
    validation, _ = run(IntakeProcessor(cfg), ctx, cancelled=lambda: True)
    assert not validation.ok
    assert validation.errors == ["cancelled"]
