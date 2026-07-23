#!/usr/bin/env python3
"""Seed a fully-populated local demo so every UI element has something to show.

Generates, under a data dir (default ``.devdata`` beside the repo):

  * a sample NAS tree with **real EXIF images** (Model + DateTimeOriginal), so
    the Submit form's folder Browse + auto-detect probe actually fire;
  * sample upload files (Trimble .T04, RINEX .obs, targets csv, base ECEF csv)
    to drop into the base-station / targets fields;
  * a coordinator config wiring browse roots, upload dir and intake defaults;
  * a coordinator DB pre-loaded with machines, projects and jobs in **every
    state** (running, queued, needs-attention, failed, stalled, done) plus a
    couple of standalone queued jobs a fake agent can pick up for live motion.

Usage:
    python scripts/seed_demo.py                 # -> ./.devdata
    python scripts/seed_demo.py --data-dir DIR  # custom location
    python scripts/seed_demo.py --reset         # wipe the DB + generated data

Then (paths printed at the end too):
    python -m coordinator.main --config .devdata/coordinator.yaml
    cd web && npm install && npm run dev         # http://localhost:5173
    # optional live progress on the standalone MOCK jobs:
    python scripts/fake_agent.py --node DEMO-LIVE --capabilities MOCK
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from coordinator.db import (  # noqa: E402
    Job, JobEvent, Node, Project, init_db, make_engine, make_session_factory,
)
from shared.schemas import JobStatus, ProjectStatus  # noqa: E402


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ago(**kw) -> datetime:
    return now_utc() - timedelta(**kw)


# ---------------------------------------------------------------------------
# Sample files on disk (NAS tree with EXIF images + upload samples)
# ---------------------------------------------------------------------------

FLIGHTS = [
    # (client, project, date_folder, subfolder, exif_model, n_images)
    ("Acme", "Route66", "10Jul2026", "images", "M3E", 3),
    ("Globex", "Bridge12", "09Jul2026", "lidars", "L2", 2),
    ("Initech", "Quarry7", "08Jul2026", "images", "ZenmuseP1", 2),
]


def make_exif_jpeg(path: Path, model: str, when: datetime) -> None:
    """A tiny JPEG carrying the EXIF the probe reads: Make/Model (IFD0) and
    DateTimeOriginal (Exif sub-IFD 0x8769 -> 0x9003)."""
    from PIL import Image

    img = Image.new("RGB", (64, 48), (58, 92, 120))
    exif = Image.Exif()
    exif[0x010F] = "DJI"           # Make
    exif[0x0110] = model           # Model  -> EXIF_MODEL_TO_SENSOR
    exif[0x8769] = {0x9003: when.strftime("%Y:%m:%d %H:%M:%S")}  # DateTimeOriginal
    img.save(str(path), "JPEG", exif=exif)


def build_nas(nas_root: Path) -> None:
    for client, project, date_folder, sub, model, n in FLIGHTS:
        folder = nas_root / client / project / date_folder / sub
        folder.mkdir(parents=True, exist_ok=True)
        when = datetime(2026, 7, 10, 8, 15, 0)
        for i in range(1, n + 1):
            make_exif_jpeg(folder / f"DJI_{i:04d}.JPG", model, when)


def build_samples(samples: Path) -> None:
    samples.mkdir(parents=True, exist_ok=True)
    # Trimble raw base (extension is what the UI keys on for Trimble vs RINEX).
    (samples / "base_station.T04").write_bytes(os.urandom(2048))
    # A already-RINEX observation file.
    (samples / "rover_base.25o").write_text(
        "     3.04           OBSERVATION DATA    M (MIXED)           RINEX VERSION / TYPE\n"
        "                                                            END OF HEADER\n",
        encoding="ascii",
    )
    # All-points targets csv (5th column = point type: TLT / TAT / misc).
    rows = ["PointID,Northing,Easting,Elevation,Type"]
    for i in range(1, 7):
        kind = "TLT" if i % 3 == 0 else ("TAT" if i % 2 == 0 else "MISC")
        rows.append(f"P{i},100{i}.11,200{i}.22,{50 + i}.3,{kind}")
    (samples / "targets_all_points.csv").write_text("\n".join(rows) + "\n", encoding="ascii")
    # Corrected base position — the exact header parse_base_ecef_csv requires.
    (samples / "base_ecef.csv").write_text(
        "Point ID,X (ECEF),Y (ECEF),Z (ECEF)\n"
        "BASE1,-1878522.21,-4599428.34,4001432.17\n",
        encoding="ascii",
    )


def write_config(data_dir: Path, nas_root: Path, samples: Path,
                 uploads: Path, db_path: Path) -> Path:
    exiftool = shutil.which("exiftool") or "exiftool"
    cfg = {
        "require_agent_tokens": False,   # fake agents auto-register
        "admin_token": "",               # admin endpoints open (LAN dev)
        "db_path": str(db_path),
        "upload_dir": str(uploads),
        "exiftool_path": exiftool,
        "intake_defaults": {
            "root_path": r"\\192.168.35.25\3dData",
            "epsg_h": "6341",
            "epsg_v": "8228",
            "classify_models": ["Rail corridor v3", "Vegetation v2", "Bare earth"],
        },
        # Two roots so the picker's root switcher (ToggleButtonGroup) shows.
        "browse_roots": {
            "3dData": {"path": str(nas_root), "display": r"\\192.168.35.25\3dData"},
            "samples": {"path": str(samples), "display": str(samples)},
        },
    }
    path = data_dir / "coordinator.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Database seed (nodes / projects / jobs / events across every state)
# ---------------------------------------------------------------------------

def seed_nodes(session) -> None:
    def node(name, caps, *, last_sync, enabled=True, draining=False,
             accepting=True, enabled_caps=None, telemetry=None, user="survey"):
        session.add(Node(
            node_name=name, capabilities_json=caps,
            enabled_capabilities_json=enabled_caps, enabled=enabled,
            draining=draining, accepting_jobs=accepting,
            agent_version="3.0.0", computer_name=name, current_user=user,
            last_sync_at=last_sync, last_telemetry_json=telemetry or {"cpu_percent": 12.0},
        ))

    node("TERRA-01", ["TERRA_PPK", "TERRA_LIDAR"], last_sync=ago(seconds=4))
    # Declares two caps but LiDAR is toggled off in coordinator policy.
    node("TERRA-02", ["TERRA_PPK", "TERRA_LIDAR"], last_sync=ago(seconds=6),
         enabled_caps=["TERRA_PPK"])
    node("PIX4D-01", ["PIX4D_MATIC"], last_sync=ago(seconds=5))
    node("NAS-COPY", ["INTAKE_COPY"], last_sync=ago(seconds=3), user="nasd")
    node("RINEX-01", ["RINEX_CONVERT", "INTAKE_COPY"], last_sync=ago(seconds=7),
         draining=True)
    # Paused by its own preflight (desktop locked) — heartbeating but skipped.
    node("CYCLONE-01", ["CYCLONE_CLASSIFY"], last_sync=ago(seconds=5),
         accepting=False, telemetry={"cpu_percent": 3.0,
                                     "preflight": ["desktop locked", "3DR not installed"]})
    node("OLD-BOX", ["MOCK"], last_sync=ago(minutes=12))           # -> Offline
    node("SPARE-01", ["TERRA_PPK"], last_sync=ago(seconds=8), enabled=False)  # Disabled
    node("DEMO-LIVE", ["MOCK"], last_sync=ago(seconds=9))          # for live fake agent


def add_events(session, job, specs):
    """specs: list of (type, message, when)."""
    for typ, msg, when in specs:
        session.add(JobEvent(job_id=job.id, type=typ, message=msg,
                             node_name=job.assigned_node, ts=when,
                             details_json={}))


def mkproject(session, name, client, sensor, date_folder, priority=100,
              status=ProjectStatus.ACTIVE):
    p = Project(
        name=name, client=client, sensor_type=sensor, date_folder=date_folder,
        project_number=f"{client[:3].upper()}-{name}",
        root_path=fr"\\192.168.35.25\3dData\{client}\{name}",
        priority=priority, status=status.value, created_at=ago(hours=6),
    )
    session.add(p)
    session.flush()
    return p


def mkjob(session, project, job_type, status, *, node="", progress=None,
          msg="", depends_on=None, error_code="", error_message="",
          created=None, assigned=None, started=None, last_progress=None,
          finished=None, priority=100, params=None):
    j = Job(
        project_id=project.id if project else None, job_type=job_type,
        status=status.value, assigned_node=node, progress_percent=progress,
        progress_message=msg, depends_on_json=depends_on or [],
        parameters_json=params or {}, error_code=error_code,
        error_message=error_message, priority=priority,
        created_at=created or ago(hours=5), assigned_at=assigned,
        started_at=started, last_progress_at=last_progress, finished_at=finished,
    )
    session.add(j)
    session.flush()
    return j


def seed_jobs(session) -> None:
    # A — photo chain mid-flight: intake done, PPK running, Pix4D queued on it.
    a = mkproject(session, "Route66", "Acme", "M3E", "10Jul2026", priority=200)
    a_copy = mkjob(session, a, "INTAKE_COPY", JobStatus.SUCCEEDED, node="NAS-COPY",
                   progress=100, created=ago(hours=5), started=ago(hours=5),
                   finished=ago(hours=4, minutes=40),
                   params={"source_folders": [r"\\192.168.35.25\3dData\Acme\Route66\10Jul2026"]})
    a_rin = mkjob(session, a, "RINEX_CONVERT", JobStatus.SUCCEEDED, node="RINEX-01",
                  progress=100, depends_on=[a_copy.uuid], started=ago(hours=4, minutes=35),
                  finished=ago(hours=4, minutes=20))
    a_ppk = mkjob(session, a, "TERRA_PPK", JobStatus.RUNNING, node="TERRA-01",
                  progress=45, msg="solving trajectory (epoch 12/26)",
                  depends_on=[a_rin.uuid], assigned=ago(minutes=20),
                  started=ago(minutes=18), last_progress=ago(seconds=20))
    mkjob(session, a, "PIX4D_MATIC", JobStatus.QUEUED, depends_on=[a_ppk.uuid])
    add_events(session, a_ppk, [
        ("CREATED", "job created from intake", ago(hours=5)),
        ("ASSIGNED", "assigned to TERRA-01", ago(minutes=20)),
        ("STARTED", "pid 4821", ago(minutes=18)),
        ("PROGRESS", "solving trajectory (epoch 12/26)", ago(seconds=20)),
    ])

    # B — LiDAR chain blocked on a failed reconstruction that needs attention.
    b = mkproject(session, "Bridge12", "Globex", "L2", "09Jul2026")
    b_copy = mkjob(session, b, "INTAKE_COPY", JobStatus.SUCCEEDED, node="NAS-COPY",
                   progress=100, finished=ago(hours=3))
    b_lidar = mkjob(session, b, "TERRA_LIDAR", JobStatus.NEEDS_ATTENTION, node="TERRA-01",
                    depends_on=[b_copy.uuid], error_code="TERRA_TIMEOUT",
                    error_message="reconstruction exceeded 240 min runtime cap",
                    assigned=ago(hours=3), started=ago(hours=3),
                    finished=ago(hours=1), progress=62)
    mkjob(session, b, "CYCLONE_CLASSIFY", JobStatus.QUEUED, depends_on=[b_lidar.uuid],
          params={"classify_model": "Rail corridor v3"})
    add_events(session, b_lidar, [
        ("CREATED", "job created from intake", ago(hours=3, minutes=30)),
        ("ASSIGNED", "assigned to TERRA-01", ago(hours=3)),
        ("STARTED", "pid 5120", ago(hours=3)),
        ("FAILED", "runtime cap exceeded", ago(hours=1)),
        ("NEEDS_ATTENTION", "3 lease strikes / hard failure", ago(hours=1)),
    ])

    # C — a completely finished photo project (all SUCCEEDED).
    c = mkproject(session, "Quarry7", "Initech", "P1", "08Jul2026",
                  status=ProjectStatus.QA)
    c_copy = mkjob(session, c, "INTAKE_COPY", JobStatus.SUCCEEDED, node="NAS-COPY",
                   progress=100, finished=ago(hours=8))
    c_rin = mkjob(session, c, "RINEX_CONVERT", JobStatus.SUCCEEDED, node="RINEX-01",
                  progress=100, depends_on=[c_copy.uuid], finished=ago(hours=7, minutes=30))
    c_ppk = mkjob(session, c, "TERRA_PPK", JobStatus.SUCCEEDED, node="TERRA-02",
                  progress=100, depends_on=[c_rin.uuid], finished=ago(hours=6, minutes=30))
    mkjob(session, c, "PIX4D_MATIC", JobStatus.SUCCEEDED, node="PIX4D-01",
          progress=100, depends_on=[c_ppk.uuid], finished=ago(hours=5))

    # D — a bad intake: copy failed outright, plus a cancelled MOCK for variety.
    d = mkproject(session, "Levee3", "Umbrella", "M3E", "07Jul2026")
    mkjob(session, d, "INTAKE_COPY", JobStatus.FAILED, node="NAS-COPY",
          error_code="COPY_SOURCE_MISSING",
          error_message="source folder not found on the share",
          assigned=ago(hours=2), started=ago(hours=2), finished=ago(hours=2), progress=8)
    mkjob(session, d, "MOCK", JobStatus.CANCELLED, node="OLD-BOX",
          finished=ago(hours=2, minutes=10), msg="cancelled by operator")

    # E — a RUNNING job that has gone quiet long enough to flag as stalled.
    e = mkproject(session, "Highway9", "Wayne", "L3", "06Jul2026")
    e_copy = mkjob(session, e, "INTAKE_COPY", JobStatus.SUCCEEDED, node="NAS-COPY",
                   progress=100, finished=ago(hours=4))
    e_lidar = mkjob(session, e, "TERRA_LIDAR", JobStatus.RUNNING, node="TERRA-02",
                    progress=30, msg="densifying point cloud",
                    depends_on=[e_copy.uuid], assigned=ago(minutes=40),
                    started=ago(minutes=38), last_progress=ago(minutes=20))
    mkjob(session, e, "CYCLONE_CLASSIFY", JobStatus.QUEUED, depends_on=[e_lidar.uuid])

    # Standalone queued MOCK jobs (no deps) for a fake agent to run live.
    for i in range(2):
        mkjob(session, None, "MOCK", JobStatus.QUEUED, priority=100,
              created=ago(minutes=5 - i), params={"note": "demo — pick me up with a MOCK agent"})


def seed_db(db_path: Path) -> None:
    engine = make_engine(str(db_path))
    init_db(engine)
    Session = make_session_factory(engine)
    with Session() as session:
        # Fresh every run: clear the four tables (order respects FKs).
        for model in (JobEvent, Job, Project, Node):
            session.query(model).delete()
        session.commit()
        seed_nodes(session)
        seed_jobs(session)
        session.commit()


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a local demo dataset")
    ap.add_argument("--data-dir", default=str(REPO_ROOT / ".devdata"))
    ap.add_argument("--reset", action="store_true",
                    help="delete the data dir before seeding")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if args.reset and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    nas_root = data_dir / "nas"
    samples = data_dir / "samples"
    uploads = data_dir / "uploads"
    db_path = data_dir / "coordinator.db"
    uploads.mkdir(parents=True, exist_ok=True)

    # A stale WAL/db from a previous run would keep old rows around.
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    print("Generating sample NAS tree (with EXIF images)…")
    build_nas(nas_root)
    print("Generating sample upload files…")
    build_samples(samples)
    print("Seeding coordinator database…")
    seed_db(db_path)
    cfg_path = write_config(data_dir, nas_root, samples, uploads, db_path)

    rel = os.path.relpath(cfg_path, Path.cwd())
    print("\nDemo data ready in", data_dir)
    print("  • 9 machines (online/offline/draining/paused/disabled + capability policy)")
    print("  • 5 projects, jobs in every state (running/queued/attention/failed/stalled/done)")
    print("  • sample uploads in", samples)
    print("\nNext:")
    print(f"  python -m coordinator.main --config {rel}")
    print("  cd web && npm install && npm run dev        # http://localhost:5173")
    print("  python scripts/fake_agent.py --node DEMO-LIVE --capabilities MOCK   # live motion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
