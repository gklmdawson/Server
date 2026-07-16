# Deploying Data Intake v3

Zero-to-running guide for this build (branch `claude/architecture-redesign-3x5gvh`).

Two things get installed, in this order:

| # | What | Where | How |
|---|---|---|---|
| 1 | **Coordinator** — job queue + API + web UI | UGREEN DXP4800 Plus NAS | Docker container |
| 2 | **Agent** — one per processing machine | Each Windows workstation | `DataIntakeAgent.exe` + Task Scheduler |

Everything else is a browser: staff use `http://<nas-ip>:8443` — no installs
on office PCs, ever.

```text
     Office browsers ──────────► NAS :8443 (Docker: queue + web UI + SQLite)
                                    ▲ outbound polls only
     INTAKE-01   TERRA-01 (…-02)  PIX4D-01   CYCLONE-01     ← agent.exe each
     copy+RINEX  DJI Terra        Pix4Dmatic  3DR CLI
                                    ▼
                          NAS shares (project data)
```

---

## Part 0 — Get the code

The repo (`gklmdawson/Server`) is **private**, so anonymous `git clone` says
"repository not found". Either:

* **ZIP (no git needed):** logged into GitHub in a browser, open the repo,
  switch the branch dropdown to `claude/architecture-redesign-3x5gvh`, then
  **Code → Download ZIP** and extract. The ZIP is the entire project —
  backend, `web/` frontend, Docker files, agent scripts.
* **GitHub CLI:** `gh auth login`, then
  `gh repo clone gklmdawson/Server && cd Server &&
  git checkout claude/architecture-redesign-3x5gvh`.

You'll place this same folder on the NAS (Part 1) and use its `scripts/` +
`config/` on the workstations (Part 2).

---

## Part 1 — Coordinator on the UGREEN NAS

The DXP4800 Plus is x86-64 and runs Docker natively; the container is
featherweight next to file serving.

### 1.1 One-time NAS prep

1. **Pin the NAS's address**: DHCP reservation or static IP, so
   `http://<nas-ip>:8443` never moves. Even better, give it a DNS/hosts name
   (e.g. `intake-server`) — then agents and bookmarks survive IP changes.
2. UGOS **App Center → install Docker** (UGREEN's container app).
3. **Enable SSH** (Control Panel → Terminal/SSH) for the setup commands; you
   can disable it again afterwards.

### 1.2 Copy the project to the NAS

Put the folder from Part 0 at a path like:

```text
/volume1/docker/data-intake/     ← Dockerfile at the top level
```

Copying over the network share or with the UGOS file manager is fine — the
Docker build doesn't care how the files arrived (handy since the repo is
private and the NAS has no GitHub login).

### 1.3 Configure and start

SSH in (`ssh <admin-user>@<nas-ip>`):

```bash
cd /volume1/docker/data-intake
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # generate a token
nano .env                      # paste it as DATA_INTAKE_ADMIN_TOKEN

sudo docker compose up -d --build
```

The first build takes a few minutes — it compiles the React UI *inside* the
container, so no machine in the shop ever needs Node. Verify:

* `http://<nas-ip>:8443` → the web app (Dashboard / Projects / Submit / Machines)
* `http://<nas-ip>:8443/health` → `{"ok": true, …}`

In the web app click **⚙** (top right) and paste the admin token — that
browser can now submit flights and manage machines. Do this on each browser
that needs more than read-only viewing.

> **GUI alternative:** UGREEN's Docker app can run compose projects — point
> its Project/Compose screen at the folder from 1.2 and set
> `DATA_INTAKE_ADMIN_TOKEN` there instead of using `.env` over SSH.

### 1.4 Where state lives

Everything the coordinator knows is in `data/` next to the compose file:

```text
/volume1/docker/data-intake/data/coordinator.db      ← the queue (SQLite + WAL)
/volume1/docker/data-intake/data/coordinator.yaml    ← optional settings
```

That's the NAS's own filesystem accessed locally by the container — the
"never SQLite over SMB" rule is satisfied. For settings (chain templates,
web-form defaults like your projects root / EPSG / 3DR models, timeouts),
start from `config/coordinator.example.yaml`, save it as
`data/coordinator.yaml`, and `sudo docker compose restart`.

**Back up `data/`** — RAID survives a dead disk, not a deleted database.
Point UGOS's backup app (or any nightly copy) at the folder.

### 1.5 Updating the coordinator

```bash
cd /volume1/docker/data-intake
# copy the new files over (or git pull if the NAS has credentials)
sudo docker compose up -d --build
```

Queue state is untouched (it's in `data/`), and agents tolerate the restart —
they keep watching their running jobs and reconnect with backoff.

---

## Part 2 — Agents on the Windows machines

Each processing machine runs one `DataIntakeAgent.exe` in the logged-in
desktop session (GUI automation needs a real desktop — that's why it's a
Task Scheduler at-logon task, never a Windows service).

### 2.1 Build the EXE once

On any Windows box with Python 3.10+, from the project folder:

```bat
pip install -e ".[agent,dev]"
py build.py agent
```

Copy `dist\DataIntakeAgent.exe` to the NAS dist share (same place the
`DJI_AUTOMATE_*.exe` payloads live).

### 2.2 Provision a node token (one per machine)

From any PC (PowerShell):

```powershell
$tok = "YOUR_ADMIN_TOKEN"
Invoke-RestMethod -Method Post -Uri http://<nas-ip>:8443/api/v1/nodes `
  -Headers @{Authorization="Bearer $tok"} -ContentType application/json `
  -Body '{"node_name":"TERRA-01","capabilities":["TERRA_PPK","TERRA_LIDAR"]}'
```

The response includes the node's **token — shown exactly once**; save it for
step 2.3. Repeat for `INTAKE-01`, `PIX4D-01`, `CYCLONE-01`, ….

### 2.3 Install on each machine

1. Copy `DataIntakeAgent.exe` and `config/agent.example.yaml` (renamed to
   `agent.yaml`) into e.g. `C:\DataIntakeAgent\`.
2. Edit `agent.yaml`: `node_name`, `coordinator_url: http://<nas-ip>:8443`,
   and what this box declares:

   | Machine | `capabilities` | `payload_paths` needed |
   |---|---|---|
   | Intake (sees cards / ingest share) | `INTAKE` | `convert_to_rinex_exe` |
   | Terra box(es) | `TERRA_PPK, TERRA_LIDAR` | `dji_automate_ppk`, `dji_automate_ui` |
   | Pix4D box | `PIX4D_MATIC` | `pix4d_automate` |
   | Cyclone box | `CYCLONE_CLASSIFY` | `cyclone_3dr_exe`, `cyclone_classify_script` |

   A box may declare several types (it still runs one job at a time); what
   it's *allowed* to run day-to-day is toggled on the dashboard, not here.
3. Run `scripts\install_agent.ps1` as admin — it stores the node token for
   the processing account and registers the at-logon Scheduled Task.
4. Log the processing account in (auto-logon recommended). The machine shows
   up on the **Machines** tab within seconds.

**Agent updates later:** build the new EXE to the dist share, run
`scripts\update_agent.ps1` on each box. The dashboard shows every node's
agent version, so stragglers are visible.

---

## Part 3 — First submission (checklist)

1. Copy the flight card(s) to a path the **intake machine** can see (its
   ingest share or local disk). The browser sends *paths*, not files.
2. Web app → **Submit**: client, project, date (`16Jul2026` style), sensor,
   source folder path(s), base data path(s), EPSG, chains → **Queue it**.
3. Watch the project page: INTAKE runs first (folder tree → copy → RINEX),
   then the chains fire machine by machine
   (`TERRA_PPK → PIX4D_MATIC`, `TERRA_LIDAR → CYCLONE_CLASSIFY`).
   Failures land in the dashboard's attention panel with a Retry button and
   a failure bundle (screenshot + logs) on the workstation.

The old PyQt5 intake GUI keeps working unchanged throughout — run both in
parallel until you trust the web form.

---

## Part 4 — Day-2 operations

**Add a second Terra machine** — install DJI Terra (+ license) on the new
box, then Part 2 with `node_name: TERRA-02` and the same `TERRA_*`
capabilities. That's the whole procedure: routing is by capability, so both
boxes immediately share the Terra queue.

**Change what a machine does** — **Machines** tab → tick/untick job types
(effective = declared ∩ enabled). Instant, reversible, no RDP. To add a type
a box has never declared, install the app there and extend that machine's
`agent.yaml`.

**Take a machine for manual work** — **Drain** (finishes the current job,
takes nothing new), then **Enable** when done. The Cyclone box handles the
"human is using 3DR" case automatically — its agent pauses itself while
`3DR.exe` is running.

**Move the coordinator** — stop the container, copy the whole
`data-intake/` folder (including `data/`) to the new host,
`docker compose up -d --build`, repoint each agent's `coordinator_url`
(unnecessary if you used a DNS name).

**Rotate a token** — re-POST `/api/v1/nodes` with the same `node_name` (new
token, old one dies); re-run `install_agent.ps1` with it. For the admin
token, edit `.env` and `docker compose up -d`.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Machine shows **Offline** | Is the agent console running in the logged-in session on that box? `coordinator_url` correct? Token env set (re-run install script)? |
| Machine shows **Paused** | Its own preflight is failing — the reason is on its card (locked desktop, wrong DPI/resolution, app already open by a person, NAS unreachable). |
| Job stuck **Queued** | Is some *online* machine's toggle for that job type on (Machines tab)? Are the job's "waiting on" dependencies finished? |
| Web actions return 401 | Set the admin token via ⚙ — it must match `DATA_INTAKE_ADMIN_TOKEN` in `.env`. |
| INTAKE fails `source folder not found` | The *intake machine* must see that exact path — use a share/mapping that exists for the processing account. |
| `git clone` says repository not found | The repo is private — see Part 0 (ZIP download or `gh auth login`). |
| Container won't start | `sudo docker compose logs` — usually a missing `.env` / `DATA_INTAKE_ADMIN_TOKEN` line. |
