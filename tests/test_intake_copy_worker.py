"""INTAKE_COPY worker: env-driven agent config, UNC->mount path translation,
and a NAS-local copy run through a path map."""
import time

import pytest

from agent.config import AgentConfig, load_config
from processors import build_registry
from processors.intake import IntakeCopyProcessor
from processors.base import JobContext


# --- path translation -------------------------------------------------------

def test_translate_unc_to_mount():
    cfg = AgentConfig(path_map={"\\\\192.168.35.25\\3dData": "/mnt/3dData"})
    assert cfg.translate_path("\\\\192.168.35.25\\3dData\\Brahma\\Peak") == "/mnt/3dData/Brahma/Peak"
    # bare root maps to the mount itself
    assert cfg.translate_path("\\\\192.168.35.25\\3dData") == "/mnt/3dData"


def test_translate_longest_prefix_and_passthrough():
    cfg = AgentConfig(path_map={
        "\\\\NAS\\share": "/mnt/share",
        "\\\\NAS\\share\\sub": "/mnt/special",
    })
    assert cfg.translate_path("\\\\NAS\\share\\sub\\x") == "/mnt/special/x"
    assert cfg.translate_path("\\\\NAS\\share\\other") == "/mnt/share/other"
    # already-local / unmapped paths pass through untouched
    assert cfg.translate_path("/mnt/ingest/DCIM") == "/mnt/ingest/DCIM"
    assert cfg.translate_path("/data/uploads/x/base.T02") == "/data/uploads/x/base.T02"


def test_empty_map_is_noop():
    assert AgentConfig().translate_path("\\\\NAS\\x") == "\\\\NAS\\x"


# --- env-driven config (how the container runs, no YAML) --------------------

def test_load_config_from_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no agent.yaml present
    monkeypatch.setenv("DATA_INTAKE_NODE_NAME", "NAS-COPY")
    monkeypatch.setenv("DATA_INTAKE_COORDINATOR_URL", "http://coordinator:8443")
    monkeypatch.setenv("DATA_INTAKE_CAPABILITIES", "INTAKE_COPY")
    monkeypatch.setenv("DATA_INTAKE_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("DATA_INTAKE_PATH_MAP", '{"\\\\\\\\NAS\\\\3dData": "/mnt/3dData"}')
    monkeypatch.setenv("DATA_INTAKE_NODE_TOKEN", "tok123")

    cfg = load_config()
    assert cfg.node_name == "NAS-COPY"
    assert cfg.capabilities == ["INTAKE_COPY"]
    assert cfg.coordinator_url == "http://coordinator:8443"
    assert cfg.path_map == {"\\\\NAS\\3dData": "/mnt/3dData"}
    assert cfg.token == "tok123"


def test_registry_builds_intake_copy_worker():
    cfg = AgentConfig(node_name="NAS-COPY", capabilities=["INTAKE_COPY"])
    registry = build_registry(cfg, ["INTAKE_COPY"])
    assert isinstance(registry["INTAKE_COPY"], IntakeCopyProcessor)
    assert registry["INTAKE_COPY"].custom_execution
    assert not registry["INTAKE_COPY"].requires_desktop


# --- end-to-end NAS-local copy through the path map -------------------------

def test_copy_worker_resolves_unc_and_copies(tmp_path):
    dest_root = tmp_path / "3dData"
    dest_root.mkdir()
    src = tmp_path / "ingest" / "DJI_flight"
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(b"a" * 100)
    (src / "sub" / "c.bin").write_bytes(b"c" * 50)

    cfg = AgentConfig(
        node_name="NAS-COPY", work_root=str(tmp_path / "work"),
        capabilities=["INTAKE_COPY"],
        path_map={"\\\\NAS\\3dData": str(dest_root)},
    )
    proc = IntakeCopyProcessor(cfg)

    params = {
        "root_path": "\\\\NAS\\3dData",            # UNC as stored in job params
        "client": "Brahma", "project": "SilverPeak",
        "date": "10Jul2026", "sensor_type": "M3E",
        "source_folders": [str(src)],              # local /mnt path (passes through)
        "base_data_paths": [],                      # no base -> no obs required
        "base_data_is_rinex": False, "base_ecef_xyz": None,
    }
    work_dir = tmp_path / "jobdir"
    work_dir.mkdir()
    ctx = JobContext(job_uuid="c1", job_type="INTAKE_COPY", parameters=params,
                     work_dir=work_dir, log_path=work_dir / "p.log",
                     max_runtime_seconds=3600.0, started_wall=time.time())

    validation = proc.run_custom(ctx, lambda *a: None, lambda: False)

    # Copied into the LOCAL destination the UNC mapped to.
    flight = dest_root / "Brahma" / "SilverPeak" / "10Jul2026" / "M3E" / "DJI_flight"
    assert (flight / "a.bin").read_bytes() == b"a" * 100
    assert (flight / "sub" / "c.bin").read_bytes() == b"c" * 50
    assert validation.ok, validation.errors
