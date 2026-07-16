# Data Intake — Distributed Processing (v3)

One rule: **browser for every human, Python for every machine.**

A single coordinator (Docker container on the UGREEN NAS) runs the job queue,
the REST API, and the React web app — office staff submit flights and watch
progress from any browser, with zero installs. One small Python agent EXE runs
on every Windows machine that does work; **everything is a job**, including
intake itself (copy + RINEX conversion). The pywinauto payloads that click
through DJI Terra / Pix4Dmatic are unchanged — they are the irreplaceable core.

```text
        Office staff (any browser on the LAN — phones included)
                          │  http://<nas>:8443
                          ▼
       COORDINATOR — Docker on the UGREEN DXP4800 Plus
       FastAPI + SQLite + React UI (queue, submit form, dashboard,
       per-machine capability toggles)
                          ▲  outbound HTTP polls (/sync)
        ┌───────────────┬─┴─────────────┬───────────────┐
   INTAKE machine   TERRA box(es)   PIX4D box       CYCLONE box
   agent.exe        agent.exe       agent.exe       agent.exe
   INTAKE jobs:     DJI Terra via   Pix4Dmatic via  3DR.exe CLI
   copy + RINEX     pywinauto       pywinauto       --silent
        └───────────────┴───────────────┴───────────────┘
                     UGREEN NAS shares (project data)
```

Machines are interchangeable by **capability**: each agent declares the job
types its box can run (`agent.yaml`), and the dashboard's Machines tab
controls what it *may* run right now. Two Terra licenses = two agents
declaring `TERRA_PPK`/`TERRA_LIDAR`; the queue drains twice as fast, with no
coordinator changes.

**Install/ops guide: [DEPLOY.md](DEPLOY.md)** · Full architecture &
history: [DESIGN.md](DESIGN.md)

## Layout

| Path | What it is |
|---|---|
| `coordinator/` | FastAPI coordinator: SQLite job DB, `/sync` assignment, `/intake` submission builder, serves the web UI |
| `web/` | React (Vite) web app: dashboard, submit form, projects, machine controls |
| `agent/` | Workstation agent: sync loop, desktop preflight, runner with watchdog + crash recovery |
| `processors/` | `intake` (copy+RINEX), `terra_ppk`, `terra_lidar`, `pix4dmatic`, `cyclone_classify`, `mock` |
| `automation/` | GUI-automation payload scripts (run as EXEs on workstations, unchanged) |
| `shared/` | Enums + API schemas shared by all components |
| `Dockerfile`, `docker-compose.yml` | Coordinator container for the NAS (builds the web UI inside) |
| `scripts/` | `fake_agent.py`, agent install/update PowerShell |
| `build.py` | PyInstaller builds: `agent`, `coordinator`, `terra`, `ppk`, `pix4d` |
| `data_intake.py`, `classify_3dr.py` | Legacy PyQt5 intake GUI + 3DR watcher (kept until the web intake is trusted, then retired) |
| `intake/` | `queue_client.py` — submission client for the legacy GUI's cutover |

## Dev quickstart

```bash
pip install -e ".[coordinator,agent,dev]"
python -m coordinator.main            # http://127.0.0.1:8443
python scripts/fake_agent.py --node TERRA-01 --capabilities TERRA_PPK,TERRA_LIDAR
pytest                                # 103 tests

# Web UI (only needed when changing it — production builds it in Docker):
cd web && npm install
npm run dev                           # http://localhost:5173, proxies /api
npm run build                         # coordinator serves web/dist automatically
```

Without `web/dist` the coordinator falls back to a minimal built-in status
page, so the PyInstaller EXE workflow still works without Node.

## How work flows

1. **Submit** (browser): client/project/date/sensor, source paths, base data,
   EPSG, chains. `POST /api/v1/intake` builds the whole job graph server-side
   (`coordinator/intake.py` — the parameter contract from DESIGN.md §8).
2. **INTAKE job** runs on whichever machine declares the `INTAKE` capability
   and can see the source paths: folder tree → resumable copy → base data →
   Trimble RINEX conversion → obs distribution per sensor type.
3. **Chains** gate on it: `TERRA_PPK → PIX4D_MATIC` and/or
   `TERRA_LIDAR → CYCLONE_CLASSIFY`, routed purely by capability.
4. **Dashboard** shows machines, progress, queue, and an attention panel;
   retry/cancel/drain and per-machine capability toggles are one click
   (admin token required — ⚙ in the header).

## Production notes

* Coordinator state is one SQLite file on the NAS volume mount — accessed
  locally by the container, never over SMB. Back up the `data/` folder.
* Agents authenticate with per-node bearer tokens (`POST /api/v1/nodes`
  returns each token exactly once). Set `DATA_INTAKE_ADMIN_TOKEN` for all
  admin/intake calls.
* Agents make outbound connections only; one job per machine, structurally.
* Completion = output validation, never exit codes alone. Failed GUI jobs
  keep a failure bundle (screenshot + logs) on the workstation for 7 days.
