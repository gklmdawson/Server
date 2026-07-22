# Deploying Data Intake v3

Zero-to-running guide for this build (branch `claude/architecture-redesign-3x5gvh`).

Two things get installed, in this order:

| # | What | Where | How |
|---|---|---|---|
| 1 | **Coordinator** — job queue + API + web UI (+ optional `INTAKE_COPY` worker) | UGREEN DXP4800 Plus NAS | Docker container(s) |
| 2 | **Agent** — one per processing machine | Each Windows workstation | `DataIntakeAgent.exe` + Task Scheduler |

Everything else is a browser: staff use `http://<nas-ip>:8443` — no installs
on office PCs, ever.

```text
     Office browsers ──────────► NAS :8443 (Docker: queue + web UI + SQLite
                                    ▲          + NAS helper + INTAKE_COPY worker)
                                    │ outbound polls only
     WIN-RINEX   TERRA-01 (…-02)  PIX4D-01   CYCLONE-01     ← agent each
     Trimble CLI DJI Terra        Pix4Dmatic  3DR CLI
                                    ▼
                          NAS shares (project data)
```

The copy runs on the NAS (`INTAKE_COPY`), leaving only Trimble RINEX on Windows
(`WIN-RINEX`) — Part 1.6. To skip the split, give one Windows agent the
`INTAKE` capability (copy + RINEX together) and ignore the copy worker.

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

### 1.6 Split intake — NAS copy worker + NAS helper (optional)

By default the monolithic `INTAKE` job (copy + RINEX) runs on a Windows agent.
The split runs the **copy on the NAS** (no SMB round-trip) and leaves only the
Trimble RINEX step to Windows, and turns on the **NAS helper** that pre-fills
the web form from the flight images.

1. **Mounts + config.** The `intake-copy` service and the card/uploads mounts
   are already in `docker-compose.yml` — adjust the USB path (UGOS uses
   `/mnt/@usb`; check yours with `lsblk -o NAME,LABEL,MOUNTPOINT`)
   to where your NAS mounts the card. In `data/coordinator.yaml` add the card
   as a browse root and (optionally) the State Plane shapefile for EPSG:

   ```yaml
   browse_roots:
     3dData: { path: /mnt/3dData, display: \\192.168.35.25\3dData }
     ingest: { path: /mnt/ingest, display: /mnt/ingest, mounted_only: true }
   stateplane_shapefile: /app/coordinator/resources/NAD83SPCEPSG.shp
   ```

   `mounted_only: true` keeps the ingest listing honest: the host keeps a
   mount-point folder for every USB device it has ever seen, so without it
   Browse shows a pile of stale empty folders. With it, only devices with a
   drive actually mounted appear (roots marked `ejectable` get this
   automatically).

   For EPSG auto-detect, drop `NAD83SPCEPSG.shp` **and** its `.dbf` into
   `coordinator/resources/` before `docker compose build` (absent, the EPSG
   fields just stay blank). exiftool for the RTK scan is already in the image.

2. **Provision the copy worker's node token** (like any agent):

   ```bash
   curl -s -X POST http://<nas-ip>:8443/api/v1/nodes \
     -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
     -d '{"node_name":"NAS-COPY","capabilities":["INTAKE_COPY"]}'
   ```

   Put the returned token in `.env` as `DATA_INTAKE_COPY_TOKEN`, then start the
   worker (it's behind a compose profile so a plain `up` runs only the
   coordinator):

   ```bash
   sudo docker compose --profile intake-copy up -d --build
   ```

   The worker registers as **NAS-COPY** and shows up on the dashboard.

3. **Path map.** The worker rewrites the projects-root UNC in job parameters to
   its mount via `DATA_INTAKE_PATH_MAP` (set in compose, e.g.
   `{"\\192.168.35.25\3dData":"/mnt/3dData"}`). Source folders are already
   local `/mnt/ingest` paths and uploaded base data is `/data/uploads` — only
   3dData needs mapping. Match the server IP to yours.

4. **RINEX worker on Windows.** Provision a second node for the conversion half
   and install the agent on the box that has `convertToRinex.exe` (can be the
   Terra box):

   ```
   {"node_name":"WIN-RINEX","capabilities":["RINEX_CONVERT"]}
   ```

   Its `agent.yaml` sets `payload_paths.convert_to_rinex_exe`. A submission now
   runs `INTAKE_COPY` (NAS) → `RINEX_CONVERT` (Windows) → the chains.

To stay on the single-machine model instead, give one Windows agent the
`INTAKE` capability and skip this section — both paths remain supported.

### 1.7 Eject cards & restart containers from the web UI (optional)

So crews can safely remove a card without opening the NAS UI, the web
picker can show an **Eject** button next to each device under the `ingest`
root — and a **⟳ Rescan cards** button that restarts the data-intake
containers for the rare card whose mount never propagates into them. The
coordinator container can't `umount` the host's card itself (a umount
inside the container only affects the container's namespace), and it
certainly can't `docker restart` itself, so a tiny **host-side watcher**
does the real work; the container just spools it a request over the shared
`/data` volume. The restart request carries no arguments — what actually
runs is fixed in the watcher's own `--restart-cmd`, so the container can
never ask the host to run anything else.

1. **Turn it on in `data/coordinator.yaml`:** mark the ingest root ejectable
   and set the spool dir (as the *container* sees it):

   ```yaml
   browse_roots:
     3dData: { path: /mnt/3dData, display: \\192.168.35.25\3dData }
     ingest: { path: /mnt/ingest, display: /mnt/ingest, ejectable: true }
   eject_spool_dir: /data/eject
   ```

   `sudo docker compose restart` — the Eject buttons now appear, but they
   return a "watcher not running" error until step 2.

2. **Install the host watcher** (runs on the NAS itself, as root, so it can
   unmount). `--spool` is the same folder as the *host* sees it — the host
   side of the `./data` bind mount:

   ```bash
   sudo cp scripts/nas_eject_watcher.py /usr/local/bin/
   sudo cp scripts/data-intake-eject.service /etc/systemd/system/
   # edit ExecStart in the unit if your spool / USB base differ, then:
   sudo systemctl daemon-reload
   sudo systemctl enable --now data-intake-eject
   systemctl status data-intake-eject      # should be active (running)
   ```

   The default `ExecStart` uses
   `--spool /volume1/docker/data-intake/data/eject --usb-base /mnt/@usb
   --restart-cmd "docker restart data-intake-coordinator data-intake-copy"`;
   match `--spool` to wherever your compose folder's `data/eject` lands on
   the host, `--usb-base` to where UGOS mounts cards (Part 1.6), and
   `--restart-cmd` to the containers you actually run (drop the flag to
   disable web-triggered restarts).

Now in **Submit → Browse → ingest**, each card shows **⏏ Eject**; clicking
it flushes and unmounts on the NAS and reports "safe to remove". The button
refuses while an `INTAKE_COPY` job is still reading that card. The restart
lives in two places: **⟳ Rescan cards** right in the picker's empty state
(the moment a plugged-in card isn't showing), and **⟳ Restart intake
containers** on the **Machines** tab (the permanent, discoverable home).
Both wait out the ~10–20 s gap and refresh themselves, and both are
refused while any `INTAKE_COPY` job is running, so a copy can never be
killed mid-write. Nothing here changes the container's privileges — the
unmount and the restart both happen entirely in the host watcher.

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

The `pip install` line is only needed the first time — and again whenever
the dependency list in `pyproject.toml` changes (as it did when the
system-tray mode added `pystray`); re-running it when nothing changed is
harmless. `py build.py agent` is the actual build and is needed after any
code change.

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

Or the same call from a plain **Command Prompt** (`curl` ships with
Windows 10/11) — one line, and the `\"` escapes inside `-d` are required
for cmd to pass the JSON quotes through:

```cmd
curl -X POST http://<nas-ip>:8443/api/v1/nodes -H "Authorization: Bearer YOUR_ADMIN_TOKEN" -H "Content-Type: application/json" -d "{\"node_name\":\"TERRA-01\",\"capabilities\":[\"TERRA_PPK\",\"TERRA_LIDAR\"]}"
```

An all-Windows-capabilities box (see `config/agent-all.example.yaml`)
declares every type and gets trimmed per-machine on the Machines tab:

```cmd
curl -X POST http://<nas-ip>:8443/api/v1/nodes -H "Authorization: Bearer YOUR_ADMIN_TOKEN" -H "Content-Type: application/json" -d "{\"node_name\":\"WIN-01\",\"capabilities\":[\"RINEX_CONVERT\",\"TERRA_PPK\",\"TERRA_LIDAR\",\"PIX4D_MATIC\",\"CYCLONE_CLASSIFY\"]}"
```

The response includes the node's **token — shown exactly once**; save it for
step 2.3. Repeat for `WIN-RINEX` (`RINEX_CONVERT`), `PIX4D-01`, `CYCLONE-01`, …
(or `INTAKE-01` with `INTAKE` on the single-machine model). The NAS `INTAKE_COPY`
worker is provisioned the same way — see Part 1.6.

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
   (No admin rights on the box? See 2.4 below.)
4. Log the processing account in (auto-logon recommended). The machine shows
   up on the **Machines** tab within seconds.

**The agent lives in the system tray.** On Windows it starts minimized to a
tray icon (navy square with a yellow band) instead of an open console
window. Double-click the icon for the **status window** — node, current
state, running job with progress, last sync, and a live log tail. Closing
that window only hides it back to the tray; the worker keeps running.
The tray menu also offers:

* **Open dashboard** — the coordinator web UI in a browser.
* **Open logs folder** — the agent's `logs\` directory.
* **Sync now** — poke the coordinator immediately instead of waiting out
  the poll interval.
* **Pause new jobs** — a local drain switch: the running job finishes but
  nothing new is taken, and the dashboard shows the node **Paused** with
  the reason until you resume.
* **Exit agent** — the only way to stop it; quitting is always this
  deliberate menu action, never a stray click on an X.

`DataIntakeAgent.exe --no-tray` runs the old plain console loop instead
(it is also the automatic fallback if the tray cannot start).

**Entering the token without editing files (recommended):** run

```
DataIntakeAgent.exe --setup
```

This opens a small window to enter the **coordinator URL** and **node token**,
**Test connection** (it does a live sync and shows connected / token-rejected /
unreachable), and **Save**. The values are written to `agent_setup.json` in the
work root and take precedence over the YAML — so token entry needs no `setx`,
no admin, and re-pasting a rotated token is a five-second fix. Token resolution
order is: explicit `token` in YAML → `agent_setup.json` / `token_file` → the
`DATA_INTAKE_NODE_TOKEN` env var.

**Agent updates later:** build the new EXE to the dist share, run
`scripts\update_agent.ps1` on each box. The dashboard shows every node's
agent version, so stragglers are visible.

### 2.4 Installing without admin rights

`install_agent.ps1` needs an elevated PowerShell (it writes to Program
Files and registers a Scheduled Task). On a box where you can't elevate,
everything still works from the user profile — no admin, no `setx`, no
environment variables:

1. **Make a user-owned folder**, e.g. `C:\Users\<you>\DataIntakeAgent`,
   and copy in `DataIntakeAgent.exe` plus `agent.yaml` (start from
   `config/agent.example.yaml`, or `config/agent-all.example.yaml` for an
   all-capabilities box).
2. **Edit `agent.yaml`:** set `node_name` (must match how the node was
   provisioned in 2.2), `coordinator_url: http://<nas-ip>:8443`, and point
   the work root inside the same folder so every write stays user-owned:

   ```yaml
   work_root: C:/Users/<you>/DataIntakeAgent/work
   ```
3. **Enter the token:** run `DataIntakeAgent.exe --setup`, paste the
   coordinator URL and this node's token, **Save & Test** until it shows
   green. The values land in `agent_setup.json` inside the work root —
   a plain user file, which is exactly why no admin is needed.
4. **Auto-start at logon:** press `Win+R`, run `shell:startup`, and in the
   folder that opens right-click → New → Shortcut with the target:

   ```text
   "C:\Users\<you>\DataIntakeAgent\DataIntakeAgent.exe" --config "C:\Users\<you>\DataIntakeAgent\agent.yaml"
   ```

   Log out and back in (or double-click the shortcut once) — the tray icon
   appears and the node shows up on the **Machines** tab.

Two trade-offs versus the admin install: the Startup shortcut does not
auto-restart the agent if it crashes (the Scheduled Task does — use
**Exit agent** → relaunch the shortcut after an update), and anything under
your user profile is per-user, so if a different account will run the
processing apps, do these steps as *that* account.

---

## Part 3 — First submission (checklist)

1. Make the flight data visible to the workers: plug the card into the NAS
   (split intake reads `/mnt/ingest` directly) or copy it where the intake
   machine can see it. The browser sends *paths* for the bulk data.
2. Web app → **Submit**: client, project, date (`16Jul2026` style). Pick the
   **source folder** — the NAS helper reads one image and pre-fills sensor,
   date and EPSG (all editable). **Drop** the base data, targets csv and base
   ECEF csv (these upload); set the chains → **Queue it**.
3. Watch the project page. With the split: `INTAKE_COPY` (NAS: folder tree →
   copy) → `RINEX_CONVERT` (Windows) → the chains fire machine by machine
   (`TERRA_PPK → PIX4D_MATIC`, `TERRA_LIDAR → CYCLONE_CLASSIFY`). On the
   single-machine model it's one `INTAKE` job instead of the first two.
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

**Retire a machine for good** — stop its agent first (tray **Exit agent**,
then remove the Scheduled Task or Startup shortcut) so it can't phone home,
then on the **Machines** tab click **Remove** on that node's card. Remove
only appears once a node is **offline or disabled**, and the coordinator
refuses it while the node still has a running job. If you delete a node
whose agent is still alive, it re-registers on its next sync (token-less
mode) or is rejected with 401 (token mode) — hence stopping the agent
first. `DELETE /api/v1/nodes/<name>` is the API equivalent.

**Get alerts on your phone (ntfy)** — the coordinator can push alerts
through [ntfy.sh](https://ntfy.sh): set `DATA_INTAKE_NTFY_TOPIC` in `.env`
(`.env.example` ships a generated topic; regenerate your own with
`python -c "import secrets; print('data-intake-' + secrets.token_urlsafe(24))"`),
`docker compose up -d`, then install the ntfy app (iOS/Android, or use
https://ntfy.sh in a browser) and subscribe to that exact topic — no account
needed. The topic name is the only credential: anyone who knows it can read
the alerts, so treat it like a password. What you'll get: **loud** —
job failed, job needs attention (node lost / lease expired); **normal** —
project fully processed, node offline; **silent** — each chain step done
(with x/y progress), job recovered, node back online, new intake submitted.
Alerts are fire-and-forget: if ntfy is unreachable nothing blocks or
retries, so the queue never waits on the internet. Self-hosting ntfy?
Point `DATA_INTAKE_NTFY_SERVER` (and `DATA_INTAKE_NTFY_TOKEN` if your
topic needs auth) at it.

**Move the coordinator** — stop the container, copy the whole
`data-intake/` folder (including `data/`) to the new host,
`docker compose up -d --build`, repoint each agent's `coordinator_url`
(unnecessary if you used a DNS name).

**Provisioning is idempotent** — re-POSTing `/api/v1/nodes` with an existing
`node_name` updates its capabilities but leaves the token alone (returns
`token: null`), so a repeated setup call can't silently break a working node.
**Rotate a token** only on purpose: `POST /api/v1/nodes?rotate=true` with the
same `node_name` issues a new token (the old one stops working) — then update
the box via the agent's `--setup` window (or re-run `install_agent.ps1`). A
token never expires on its own; only an explicit rotation changes it. For the
admin token, edit `.env` and `docker compose up -d`.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Machine shows **Offline** | Is the agent running in the logged-in session on that box (tray icon present)? `coordinator_url` correct? Token saved (`--setup`) or env set (re-run install script)? |
| Machine shows **Paused** with "paused from the agent's tray menu" | Someone flipped **Pause new jobs** in the tray — untick it there (or restart the agent) to resume. |
| Machine shows **Paused** | Its own preflight is failing — the reason is on its card (locked desktop, wrong DPI/resolution, app already open by a person, NAS unreachable). |
| Job stuck **Queued** | Is some *online* machine's toggle for that job type on (Machines tab)? Are the job's "waiting on" dependencies finished? |
| Web actions return 401 | Set the admin token via ⚙ — it must match `DATA_INTAKE_ADMIN_TOKEN` in `.env`. |
| `INTAKE_COPY` fails `source folder not found` | The worker must see that path: on the NAS copy worker it's the `/mnt/ingest` mount (and `path_map` must map the projects-root UNC to `/mnt/3dData`); on a single-machine `INTAKE` agent it's a share/mapping for the processing account. |
| Card plugged into the NAS but not in Browse | The `ingest` root must be in `browse_roots` (Part 1.6), and the card must be visible inside the container (`sudo docker exec data-intake-coordinator ls /mnt/ingest`). The compose mount uses `rslave` propagation so hot-plugged cards appear live; on older compose files (or if the host mount tree isn't shared) a `sudo docker compose restart` after inserting the card is the fallback. With `mounted_only`/`ejectable` set, a device folder only appears once the host has actually *mounted* the card there — if `ls` inside the container shows files on the card but Browse hides it, the container is seeing a stale copy of the folder, not the mount: restart the containers (the picker's **⟳ Rescan cards** button does this from the browser when the Part 1.7 watcher is installed). |
| Eject button says the watcher didn't respond | The host watcher isn't running or points at the wrong spool. `systemctl status data-intake-eject`, and confirm its `--spool` is the host path of the container's `eject_spool_dir` (Part 1.7). |
| Eject says the card is busy | Something on the NAS still has a file open on it (a copy job, a shell `cd`'d into it, the file manager). Close it and retry. |
| `git clone` says repository not found | The repo is private — see Part 0 (ZIP download or `gh auth login`). |
| Container won't start | `sudo docker compose logs` — usually a missing `.env` / `DATA_INTAKE_ADMIN_TOKEN` line. |
