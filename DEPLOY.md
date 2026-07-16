# Deploying the Data Intake system

Two moving parts, in install order:

| # | What | Where | How |
|---|---|---|---|
| 1 | **Coordinator** (queue + API + web UI) | UGREEN DXP4800 Plus NAS | Docker container |
| 2 | **Agent** (one per processing machine) | Each Windows workstation | `DataIntakeAgent.exe` + Task Scheduler |

Everything else is a browser: staff open `http://<nas-ip>:8443`.

---

## Part 1 — Coordinator on the UGREEN NAS (Docker)

The DXP4800 Plus is x86-64 and runs Docker natively, so the standard image
works. The container is tiny (idle FastAPI + SQLite) — it will never be
noticed next to file serving.

### 1.1 One-time NAS prep

1. **Reserve the NAS's IP** (or set a static one) in your router/DHCP so
   `http://<nas-ip>:8443` never moves. Better: also add a DNS/hosts entry like
   `intake-server` so agents and bookmarks survive any future IP change.
2. In UGOS **App Center**, install **Docker** (UGREEN's container app).
3. Enable **SSH** (Control Panel → Terminal/SSH) — needed once for setup;
   you can turn it back off afterwards.

### 1.2 Put the code on the NAS

Create a shared folder for containers if you don't have one (e.g. `docker`),
then copy this repository into it — via the UGOS file manager, a network
share, or git. You want a path like:

```text
/volume1/docker/data-intake/          <- this repo (Dockerfile at top level)
```

### 1.3 Configure and start

SSH in (`ssh <your-admin-user>@<nas-ip>`) and run:

```bash
cd /volume1/docker/data-intake
cp .env.example .env
# Edit .env: set DATA_INTAKE_ADMIN_TOKEN to a long random string, e.g.:
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # or any generator
nano .env

sudo docker compose up -d --build
```

First build takes a few minutes (it compiles the React UI inside the
container — no machine in the shop ever needs Node). Then:

* `http://<nas-ip>:8443` → the web app (Dashboard/Projects/Submit/Machines)
* `http://<nas-ip>:8443/health` → `{"ok": true, ...}`

Open the web app, click **⚙** in the header, and paste the admin token —
that browser can now submit and manage machines.

> **GUI alternative:** UGREEN's Docker app can also run compose projects —
> point its Project/Compose screen at the folder from 1.2 and set the
> `DATA_INTAKE_ADMIN_TOKEN` variable there instead of using SSH.

### 1.4 What lives where

All coordinator state is in `data/` next to the compose file
(`/volume1/docker/data-intake/data/coordinator.db` + WAL sidecars). That's a
local-filesystem path inside the NAS — the "never SQLite over SMB" rule is
satisfied. Optional advanced settings (chain templates, web-form defaults,
timeouts) go in `data/coordinator.yaml` — start from
`config/coordinator.example.yaml`, then `sudo docker compose restart`.

**Back up the `data/` folder** — RAID protects against a dead disk, not a
deleted database. Point UGOS's backup app (or any nightly copy) at it.

### 1.5 Updating the coordinator

```bash
cd /volume1/docker/data-intake
git pull            # or copy the new files over
sudo docker compose up -d --build
```

Jobs and machines are untouched — state is in `data/`, and agents just
reconnect when the container comes back (they keep running their current job
while it's down).

---

## Part 2 — Agents on the Windows machines

Each processing machine runs one `DataIntakeAgent.exe` in the logged-in
desktop session (GUI automation needs a real desktop — this is why it's a
Task Scheduler at-logon job, never a Windows service).

### 2.1 Build the EXE once

On any Windows box with Python 3.10+:

```bat
pip install -e ".[agent,dev]"
py build.py agent
```

Copy `dist\DataIntakeAgent.exe` to the NAS dist share (same place the
DJI_AUTOMATE EXEs live).

### 2.2 Provision a node token (per machine)

From any machine (PowerShell), one call per node — pick real names:

```powershell
$tok = "YOUR_ADMIN_TOKEN"
Invoke-RestMethod -Method Post -Uri http://<nas-ip>:8443/api/v1/nodes `
  -Headers @{Authorization="Bearer $tok"} -ContentType application/json `
  -Body '{"node_name":"TERRA-01","capabilities":["TERRA_PPK","TERRA_LIDAR"]}'
```

The response contains the node's **token — shown exactly once**; save it for
the next step. Repeat for `INTAKE-01`, `PIX4D-01`, `CYCLONE-01`, …

### 2.3 Install on each machine

1. Copy `DataIntakeAgent.exe` + `config/agent.example.yaml` (renamed
   `agent.yaml`) into e.g. `C:\DataIntakeAgent\`.
2. Edit `agent.yaml`: `node_name`, `coordinator_url: http://<nas-ip>:8443`,
   and the `capabilities` this box declares:

   | Machine | capabilities | payload_paths needed |
   |---|---|---|
   | Intake (sees cards/ingest share) | `INTAKE` | `convert_to_rinex_exe` |
   | Terra box(es) | `TERRA_PPK, TERRA_LIDAR` | `dji_automate_ppk`, `dji_automate_ui` |
   | Pix4D box | `PIX4D_MATIC` | `pix4d_automate` |
   | Cyclone box | `CYCLONE_CLASSIFY` | `cyclone_3dr_exe`, `cyclone_classify_script` |

   One box may declare several types (it still runs one job at a time).
3. Run `scripts\install_agent.ps1` as admin — it stores the node token for
   the processing account and registers the at-logon Scheduled Task.
4. Log the processing account in (auto-logon recommended). The machine
   appears on the dashboard's **Machines** tab within seconds.

Agent updates later: build the new EXE to the dist share, run
`scripts\update_agent.ps1` on each box (stop task → copy → start task). The
dashboard shows each node's agent version.

---

## Part 3 — First submission (checklist)

1. Copy the flight card(s) to a path the **intake machine** can see (its
   ingest share or local disk) — the browser sends *paths*, not files.
2. Web app → **Submit**: client, project, date (`10Jul2026` style), sensor,
   source folder path(s), base data path(s), EPSG, chains. **Queue it.**
3. Watch the project page: INTAKE runs first (copy + RINEX), then the chains
   light up machine by machine. Failures land in the dashboard's attention
   panel with a Retry button and a failure bundle on the workstation.

---

## Part 4 — Day-2 operations

**Add a second Terra machine** — install DJI Terra + license, then Part 2
with `node_name: TERRA-02` and the same `TERRA_*` capabilities. Done: both
boxes now pull from the same queue; nothing else changes.

**Change what a machine does** — Machines tab → tick/untick job types. This
is the coordinator-side policy (effective = declared ∩ enabled), instant and
reversible. To *add* a type a box never declared, install the app there and
add it to that machine's `agent.yaml` capabilities + payload paths.

**Take a machine for manual work** — Machines tab → **Drain** (finishes its
current job, takes nothing new). **Enable** when you're done. The Cyclone
"human is using 3DR" case is automatic — the agent pauses itself while
3DR.exe is running.

**Move the coordinator** — stop the container, copy the whole
`data-intake/` folder (incl. `data/`) to the new host, `docker compose up
-d --build`, and repoint `coordinator_url` in each agent's `agent.yaml`
(skip this if you used a DNS name).

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Machine shows **Offline** | Agent running on that box? (console window in the logged-in session) `coordinator_url` right? Token env var set (re-run install script)? |
| Machine **Paused** | The agent's own preflight is failing — the reason is shown on its card (locked desktop, wrong DPI/resolution, app already open, NAS unreachable). |
| Job stuck **Queued** | Does any *online* machine have that capability *enabled* (Machines tab)? Are its `waiting on` dependencies finished? |
| Web actions fail with 401 | Set the admin token via ⚙ (matches `DATA_INTAKE_ADMIN_TOKEN` in `.env`). |
| INTAKE fails `source folder not found` | The *intake machine* must see that exact path — use a share/drive mapping that exists for the processing account. |
| Container won't start | `sudo docker compose logs`; commonest cause is a missing `.env` / admin token line. |
