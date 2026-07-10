# Data Intake Job Queue Server

Distributed job queue for geospatial processing workstations: one coordinator
assigns work to dedicated machines running DJI Terra, Pix4Dmatic, and
Cyclone 3DR, so each licensed app lives on its own box and runs visibly in the
logged-in session. Full architecture and phased plan: **[DESIGN.md](DESIGN.md)**.

## Layout

| Path | What it is |
|---|---|
| `coordinator/` | FastAPI coordinator: SQLite job DB, `/sync` assignment, dashboard |
| `agent/` | Workstation agent: sync loop, desktop preflight, job runner with watchdog + crash recovery |
| `processors/` | `terra_ppk`, `terra_lidar` (report.md watch), `pix4dmatic` (log/ortho watch), `cyclone_classify` (resumable 3DR CLI), `mock` |
| `automation/` | The GUI-automation payload scripts (run as EXEs on workstations) |
| `shared/` | Enums + API schemas shared by all components |
| `intake/` | `queue_client.py` — stdlib-only submission client for the Data Intake GUI |
| `data_intake.py`, `classify_3dr.py` | Current intake GUI (unchanged until Phase 6 cutover) |
| `scripts/` | `fake_agent.py`, install/update scripts |
| `build.py` | PyInstaller builds: `terra`, `ppk`, `pix4d`, `agent`, `coordinator` |

## Coordinator quickstart (dev)

```bash
pip install -e ".[coordinator,agent,dev]"
python -m coordinator.main                # http://127.0.0.1:8443 — dashboard at /
python -m agent.main --config agent.yaml  # real agent (see config/agent.example.yaml)
python scripts/fake_agent.py --node TERRA-01 --capabilities TERRA_PPK,TERRA_LIDAR
pytest                                     # 80 tests
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

## On-machine work remaining (needs the real workstations)

1. Build the EXEs on Windows (`py build.py all`, `py build.py agent`,
   `py build.py coordinator`) and install: coordinator on the Pix4D box,
   agents via `scripts/install_agent.ps1`.
2. Pix4D calibration: confirm Pix4Dmatic's log location + stage/completion
   strings (job params `completion_log_glob` / `completion_pattern` /
   `failure_pattern`), confirm the ortho export path (`ortho_glob`), and add
   the save-project + close-app steps to `automation/AutomatePix4D.py`.
3. First supervised runs of each chain (`photo_ppk`, `lidar`) end-to-end.
4. Phase 6 cutover: swap `DJISequenceThread`/`Classify3DRThread` in
   `data_intake.py` for `intake.queue_client.submit_project()` (the payload
   builder mirrors `_handle_complete()`'s existing values).

## Payload scripts

The automation scripts in `automation/` still run standalone exactly as
before (`DJI_PARAMETERS.ini` fallback, dialogs on errors). Under the agent
they are launched with `--unattended`, which suppresses every dialog so a
failure can never block: errors go to stderr and the exit code (1 =
automation error, 2 = wrong DPI/environment). `AutomatePix4D.py` now takes
`--project-name/--project-root/--epsg-h/--epsg-v/--tat-path` like the Terra
scripts (`--dev`/`--step` keep the old edit-and-run defaults).
