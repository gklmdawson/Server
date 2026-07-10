# Data Intake Job Queue Server

Distributed job queue for geospatial processing workstations: one coordinator
assigns work to dedicated machines running DJI Terra, Pix4Dmatic, and
Cyclone 3DR, so each licensed app lives on its own box and runs visibly in the
logged-in session. Full architecture and phased plan: **[DESIGN.md](DESIGN.md)**.

## Layout

| Path | What it is |
|---|---|
| `coordinator/` | FastAPI coordinator: SQLite job DB, `/sync` assignment, dashboard |
| `agent/` | Workstation agent (Phase 2) |
| `processors/` | Per-application processor modules (Phases 3–5) |
| `automation/` | The GUI-automation payload scripts (run as EXEs on workstations) |
| `shared/` | Enums + API schemas shared by all components |
| `intake/` | Queue client for the Data Intake GUI (Phase 6) |
| `data_intake.py`, `classify_3dr.py` | Current intake GUI (unchanged until Phase 6 cutover) |
| `scripts/` | `fake_agent.py`, install/update scripts |
| `build.py` | PyInstaller builds: `terra`, `ppk`, `pix4d`, `agent`, `coordinator` |

## Coordinator quickstart (dev)

```bash
pip install -e ".[coordinator,dev]"
python -m coordinator.main                # http://127.0.0.1:8443 — dashboard at /
python scripts/fake_agent.py --node TERRA-01 --capabilities TERRA_PPK,TERRA_LIDAR
pytest                                     # 35 tests
```

Create a project with a workflow chain:

```bash
curl -X POST http://127.0.0.1:8443/api/v1/projects -H "Content-Type: application/json" -d '{
  "name": "SilverPeak", "client": "Brahma", "sensor_type": "M3E",
  "chains": [{"template": "photo_ppk", "parameters": {
    "TERRA_PPK":   {"project_name": "...", "data_source": "...", "ppk_path": "..."},
    "PIX4D_MATIC": {"project_name": "...", "project_root": "...", "tat_path": "..."}
  }}]
}'
```

Templates (`photo_ppk`: TERRA_PPK → PIX4D_MATIC; `lidar`: TERRA_LIDAR →
CYCLONE_CLASSIFY) live in the config; see `config/coordinator.example.yaml`.

## Production notes

* Coordinator runs on the Pix4D machine (192.168.35.67), single worker,
  SQLite on its **local disk** — never on the NAS.
* Agents authenticate with per-node bearer tokens: create with
  `POST /api/v1/nodes` (admin), which returns the token exactly once.
* Admin/intake calls send `Authorization: Bearer <admin_token>`
  (`DATA_INTAKE_ADMIN_TOKEN` env var on the coordinator).

## Payload scripts

The automation scripts in `automation/` still run standalone exactly as
before (`DJI_PARAMETERS.ini` fallback, dialogs on errors). Under the agent
they are launched with `--unattended`, which suppresses every dialog so a
failure can never block: errors go to stderr and the exit code (1 =
automation error, 2 = wrong DPI/environment). `AutomatePix4D.py` now takes
`--project-name/--project-root/--epsg-h/--epsg-v/--tat-path` like the Terra
scripts (`--dev`/`--step` keep the old edit-and-run defaults).
