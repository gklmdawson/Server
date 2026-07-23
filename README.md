# Data Intake — Distributed Processing (v3)

One rule: **browser for every human, Python for every machine.**

A single coordinator (Docker container on the UGREEN NAS) runs the job queue,
the REST API, and the React web app — office staff submit flights and watch
progress from any browser, with zero installs. One small Python agent runs on
every machine that does work; **everything is a job**, including intake. Intake
is split so the bulk copy runs **NAS-local** (a container beside the
coordinator, no SMB round-trip) and only the Trimble RINEX step lands on
Windows — a capability worker like the others. The pywinauto payloads that click
through DJI Terra / Pix4Dmatic are unchanged — they are the irreplaceable core.

```text
        Office staff (any browser on the LAN — phones included)
                          │  http://<nas>:8443
                          ▼
       COORDINATOR — Docker on the UGREEN DXP4800 Plus
       FastAPI + SQLite + React UI (queue, submit form, dashboard,
       per-machine capability toggles) + NAS helper (EXIF probe, uploads)
       ├─ INTAKE_COPY worker (container on the NAS): folder tree + card→3dData
       ▲  outbound HTTP polls (/sync)
        ┌───────────────┬─┴─────────────┬───────────────┬───────────────┐
   RINEX box        TERRA box(es)   PIX4D box       CYCLONE box
   agent            agent           agent           agent
   RINEX_CONVERT:   DJI Terra via   Pix4Dmatic via  3DR.exe CLI
   Trimble CLI      pywinauto       pywinauto       --silent
        └───────────────┴───────────────┴───────────────┴───────────────┘
                     UGREEN NAS shares (project data)
```

The single-machine model — one Windows agent with the `INTAKE` capability doing
copy + RINEX together — remains a supported fallback.

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
| `coordinator/` | FastAPI coordinator: SQLite job DB, `/sync` assignment, `/intake` submission builder, `probe.py` NAS helper (EXIF sensor/date/EPSG + uploads), serves the web UI |
| `web/` | React (Vite) web app: dashboard, submit form (auto-detect + drag-drop uploads), projects, machine controls |
| `agent/` | Agent: sync loop, desktop preflight, runner with watchdog + crash recovery; env-driven + UNC→mount `path_map` so it also runs as the NAS `INTAKE_COPY` worker |
| `processors/` | `intake` / `intake_copy` / `rinex_convert`, `terra_ppk`, `terra_lidar`, `pix4dmatic`, `cyclone_classify`, `mock` |
| `automation/` | GUI-automation payload scripts (run as EXEs on workstations, unchanged) |
| `shared/` | Enums + API schemas shared by all components |
| `Dockerfile`, `docker-compose.yml` | NAS containers: coordinator (builds the web UI inside) + the `INTAKE_COPY` worker |
| `scripts/` | `fake_agent.py`, agent install/update PowerShell |
| `build.py` | PyInstaller builds: `agent`, `coordinator`, `terra`, `ppk`, `pix4d` |
| `data_intake.py`, `classify_3dr.py` | Legacy PyQt5 intake GUI + 3DR watcher (kept until the web intake is trusted, then retired) |
| `intake/` | `queue_client.py` — submission client for the legacy GUI's cutover |

## Dev quickstart

```bash
pip install -e ".[coordinator,agent,dev]"
printf 'require_agent_tokens: false\n' > config/dev.yaml   # fake agents auto-register
python -m coordinator.main --config config/dev.yaml        # http://127.0.0.1:8443
python scripts/fake_agent.py --node TERRA-01 --capabilities TERRA_PPK,TERRA_LIDAR
pytest                                # 211 tests

# Web UI (only needed when changing it — production builds it in Docker):
cd web && npm install
npm run dev                           # http://localhost:5173, proxies /api
npm run build                         # coordinator serves web/dist automatically
```

Without a config, `require_agent_tokens` defaults to true and
`fake_agent.py` gets 401s — either use the dev config above or provision a
real node token (DEPLOY.md §2.2). No admin token set = admin endpoints and
the web submit form are open, which is what you want locally. To exercise
the dashboard, enqueue a throwaway job the fake agent will pick up:

```bash
curl -X POST http://127.0.0.1:8443/api/v1/jobs \
  -H "Content-Type: application/json" -d '{"job_type": "TERRA_PPK"}'
```

### Full demo dataset (test every UI element)

For a one-shot local playground with realistic content in every panel —
machines in each state, jobs running/queued/failed/stalled/done, a sample
NAS tree with real EXIF images (so Submit's folder Browse + auto-detect
fire), and sample upload files — run the seeder, then point the coordinator
at the config it writes:

```bash
python scripts/seed_demo.py                     # writes ./.devdata (gitignored)
python -m coordinator.main --config .devdata/coordinator.yaml
cd web && npm run dev                            # http://localhost:5173
# optional: watch a job run live end-to-end
python scripts/fake_agent.py --node DEMO-LIVE --capabilities MOCK
```

Sample files to drag into the Submit form live in `.devdata/samples/`
(Trimble `.T04`, RINEX `.25o`, an all-points targets csv, a base ECEF csv).
Re-run with `--reset` to wipe and regenerate.

Without `web/dist` the coordinator falls back to a minimal built-in status
page, so the PyInstaller EXE workflow still works without Node.

## How work flows

1. **Submit** (browser): client/project/date, then pick the source folder —
   the NAS helper (`GET /api/v1/intake/probe`) reads one image on the share and
   pre-fills sensor, date and EPSG H+V (all editable). Small inputs (base data,
   targets csv, base ECEF csv) are drag-drop **uploaded**; bulk data stays a
   path. `POST /api/v1/intake` builds the job graph server-side
   (`coordinator/intake.py` — the parameter contract from DESIGN.md §8).
2. **Split intake**: `INTAKE_COPY` (NAS-local folder tree + resumable copy)
   runs first; `RINEX_CONVERT` (Trimble conversion + obs distribution on
   Windows) follows when base data is supplied. The same job carries UNC paths;
   the copy worker rewrites them to its mounts via `path_map`. (The monolithic
   `INTAKE` capability still does both on one machine.)
3. **Chains** gate on the last intake job: `TERRA_PPK → PIX4D_MATIC` and/or
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
* Optional phone alerts via [ntfy.sh](https://ntfy.sh): set
  `DATA_INTAKE_NTFY_TOPIC` in `.env` and subscribe to the topic in the ntfy
  app — failures and lost nodes page loudly, chain progress ticks silently
  (see DEPLOY.md "Get alerts on your phone").
