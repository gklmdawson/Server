# Job Queue Server — Design (v2.1 — converged; v3 addendum below)

**Repo:** `gklmdawson/Server` · **Branch:** `claude/job-queue-server-design-0rvfqg`
**Status:** implemented through Phase 5 code (coordinator, agent, all four
processors, intake client — 80 tests). Remaining: on-machine deployment and
calibration (see README "On-machine work remaining") and the Phase 6 GUI
cutover. Watch items in §12.

---

## v3 redesign addendum (2026-07-16, branch `claude/architecture-redesign-3x5gvh`)

The v2 queue core below is unchanged — v3 rebuilds the *human half* around
one rule: **browser for every human, Python for every machine.**

1. **React web UI** (`web/`, Vite) served by the coordinator replaces the
   Jinja dashboard and, once trusted, the PyQt5 intake GUI: dashboard,
   project pages, machine controls, and the submit form live at one URL.
   The legacy single-file dashboard remains as a fallback when `web/dist`
   isn't built (PyInstaller EXE without Node still works).
2. **Intake is a job.** New `INTAKE` job type + processor port of
   `ProcessingWorker` (folder tree → resumable copy → base data → Trimble
   RINEX → obs distribution). `POST /api/v1/intake` turns one form into the
   whole job graph server-side (`coordinator/intake.py` owns the §8
   parameter contract); chains gate on the INTAKE job via `depends_on`.
3. **Machines are reconfigurable from the dashboard.** The agent declares
   what a box CAN run; a new coordinator-side policy
   (`enabled_capabilities`, Machines tab toggles) controls what it MAY run:
   effective = declared ∩ enabled. Adding a second Terra box is an agent
   install + token — routing by capability already handles the rest.
4. **Coordinator hosting moves to Docker on the UGREEN DXP4800 Plus**
   (`Dockerfile` + `docker-compose.yml`; SQLite on the NAS volume accessed
   locally — not over SMB). The Windows EXE remains a supported fallback.
5. **Fix:** request DB commits now happen in middleware BEFORE the response
   is sent (FastAPI yield-teardown commits land after it), closing a race
   where a fast agent's `started` report could read pre-commit state and
   get a spurious 409.

Deployment runbook: **DEPLOY.md**. Retirement of `data_intake.py` happens
only after supervised parallel running of the web intake.

## v3.1 addendum (split intake + NAS helper)

Two changes let intake run without a Windows machine touching the bulk data,
and bring back the old GUI's dynamic form:

1. **Intake is split into two jobs.** `INTAKE_COPY` (capability on a NAS-local
   worker) builds the folder tree and copies card → `3dData` as a **local disk
   copy** — no SMB round-trip, no `shutil`-through-Windows. `RINEX_CONVERT`
   (capability on a Windows worker) then runs only the Trimble `convertToRinex`
   step and distributes the obs; it reads/writes just the small BaseData set,
   never the imagery. `build_job_specs` emits `INTAKE_COPY → RINEX_CONVERT →
   chains` (RINEX_CONVERT only when base data is supplied; chains gate on the
   last intake job). The monolithic `INTAKE` processor stays as the
   single-machine / EXE fallback. Processors live in `processors/intake.py`
   (`IntakeCopyProcessor`, `RinexConvertProcessor`, shared `_IntakeBase`).

2. **A read-only NAS helper pre-fills the form** (`coordinator/probe.py`,
   `GET /api/v1/intake/probe`). Running inside the coordinator container with
   the card + `3dData` mounted read-only, it reads one representative image and
   returns **sensor** (EXIF Model), **date**, **GPS**, and **EPSG H+V** — the
   vertical pulled from the *same* State Plane table as the horizontal
   (`STATEPLANE_HV`), never defaulted. An opt-in `rtk=true` runs an exiftool
   RtkFlag coverage scan. EPSG needs the State Plane shapefile at
   `stateplane_shapefile` in config (its `.dbf` sibling read alongside); absent,
   the EPSG fields just come back blank. Small inputs (base data, targets csv,
   base ECEF csv) are **uploaded** (`POST /api/v1/intake/upload`,
   `/intake/parse-ecef`) to the NAS uploads volume rather than addressed by
   path; the bulk imagery is never uploaded. All auto-detected fields remain
   user-editable — the probe only pre-fills them.

Open items: the `INTAKE_COPY` worker ships as a second Linux container (sketched
in `docker-compose.yml`); the State Plane shapefile must be dropped into the
image; and card→3dData path translation for the NAS worker is a mount/config
concern (see DEPLOY.md).

This is the ChatGPT build spec ("Data Intake Distributed Processing System") reviewed
against the actual code (`data_intake.py`, `classify_3dr.py`, the three automation
scripts), trimmed where it was over-engineered for a 3-workstation shop, and hardened
where the code shows real failure modes. Where this doc and the original spec disagree,
this doc wins.

**Decisions locked in v2** (from discussion 2026-07-10):

* Agent ships as a **PyInstaller EXE** (matches the existing `build.py` workflow).
* **The intake GUI keeps owning data movement** — copy to storage + RINEX conversion
  happen at intake, before any job is queued. Agents do not stage data.
* Production chains: **photo → TERRA_PPK → PIX4D_MATIC** and
  **LiDAR → TERRA_LIDAR → CYCLONE_CLASSIFY** (Terra eligible once intake's
  convert-to-RINEX is done, i.e. at submission time).
* Terra LiDAR completion signal is **known**: `…/<project>_LiDAR/lidars/report/report.md`
  (already used by `classify_3dr.py`).
* Cyclone 3DR is **CLI automation, not GUI** (`3DR.exe --Script=… --scriptAutorun --silent`).
* Coordinator host: **192.168.35.67 — the Pix4D machine** (dual role: coordinator + agent).
* The operator uploads **one all-points targets csv** (TAT + TLT + misc).
  `INTAKE_COPY` splits it in the project folder into `SINGLE_TLT.csv` (TLT rows
  only — the LiDAR/Terra input) and `TAT.csv` (TAT + TLT rows — the Pix4D input);
  misc points are dropped from both. The chains read these prepared files from
  the share (`gcp_path`→`SINGLE_TLT.csv`, `tat_path`→`TAT.csv`). The Terra-LiDAR
  script still re-filters TLT at runtime, so feeding it `SINGLE_TLT.csv` is
  idempotent. Point type lives in column 5 of each row.
* Pix4Dmatic progress/completion comes from **tailing its logs** (rich stage +
  completion logging); the **orthomosaic is the final export** to validate; the
  automation then **saves the project and closes Pix4Dmatic**.
* **Pix4D scratch drive** (agent.yaml `scratch_dir`, PIX4D box only): AV (Sophos)
  scanning of the NAS share slows Pix4D badly, so when set the `PIX4D_MATIC`
  processor stages the run onto local disk — `prepare()` copies `PPK/` (+ the TAT
  csv) to `<scratch_dir>/<project_name>/`, Pix4D runs there, then `after_exit()`
  copies the finished project back to the NAS `project_root`, verifies the ortho
  landed, and deletes the scratch copy (kept on failure for retry/inspection).
  Empty `scratch_dir` = run in place on the NAS. This uses a general pre-launch
  `prepare()` processor hook (runner calls it after preflight, before launch).
* `DJI_PARAMETERS.ini` is standalone-run fallback only — ignored by this system.

---

## 1. Goals and non-negotiables

* One dedicated machine per licensed app — Terra, Pix4Dmatic, Cyclone 3DR each on
  their own box. Routing **is** license binding, and it stops one machine from trying
  to run everything at once.
* Processing apps run visibly in the logged-in interactive desktop session.
* Agents make outbound connections only. No inbound ports, WinRM, PsExec, or remote
  execution on workstations. The coordinator never reaches into a workstation.
* The coordinator is the single source of truth. Agents never touch the database.
* Shared storage (UGREEN NAS) holds project data; the intake GUI puts it there.
* Buildable and maintainable by one internal Python developer. No Redis, Celery,
  RabbitMQ, Docker, or Kubernetes.

---

## 2. What exists today (code-review findings)

| File | Role | State |
|---|---|---|
| `data_intake.py` | PyQt5 intake GUI v2.4.4: sensor detect (EXIF), folder structure build, source copy, base-data copy, **convert-to-RINEX** (Trimble CLI), GCP/EPSG entry, then launches the automation EXEs sequentially (`DJISequenceThread`) and finally `Classify3DRThread` | The orchestrator to be replaced by the queue — its *data prep* stays, its *launch/watch* logic moves to the coordinator+agents |
| `PyAutomateDJI.py` (`DJI_AUTOMATE_UI.exe`) | Terra LiDAR reconstruction via pywinauto | Parameterized via CLI args; script exits after clicking *Start Reconstruction* |
| `DJIAutomatePPKV2.py` (`DJI_AUTOMATE_PPK.exe`) | Terra PPK: visible-light project → PPK calc → export `POS.txt` → EXIF/XMP embed into `<date>/PPK` | Parameterized via CLI args; runs to completion itself |
| `AutomatePix4D.py` | Pix4Dmatic: import `<date>/PPK` → CRS → templates → targets → start | **Params still hardcoded** (`TODO: will be passed in from data_intake.py`) — needs argparse |
| `classify_3dr.py` | Waits for Terra's `report.md`, then runs `ClassifyLAZ.js` on LAZ files via `3DR.exe` CLI; output-file watch + size-stability instead of trusting exit; business-hours retry | Becomes the `cyclone_classify` processor almost verbatim |
| `embed_ppk_metadata.py` | EXIF/XMP writer used by PPK script | Library, fine as-is |
| `UIInspect.py`, `build.py` | Dev tools (coordinate inspector; PyInstaller build + commit menu) | Keep; `build.py` gains agent/coordinator build targets |

**The current end-to-end flow (single machine):**

```text
GUI: pick folders/client/project/sensor/GCP/EPSG
 → ProcessingWorker: detect date → build folder tree → copy source data
   → copy base data → convert to RINEX (Trimble convertToRinex.exe)
 → _handle_complete: build EXE command list
   → DJISequenceThread: run DJI_AUTOMATE_PPK.exe, then DJI_AUTOMATE_UI.exe (serially)
 → Classify3DRThread: wait for lidars/report/report.md → 3DR.exe per LAZ file
```

The queue server replaces everything after `_handle_complete` with "POST jobs to
coordinator"; the machines running each stage change, the stages themselves don't.

Findings that shape the design:

1. **The GUI-automation contract includes the desktop itself.** Clicks are pixel
   offsets from UIA anchors, calibrated at 150% DPI on a specific resolution.
   → The agent must *preflight the desktop* (DPI, resolution, session unlocked)
   before accepting a **GUI** job (Terra, Pix4D) and reject with a specific error
   otherwise. Cyclone jobs are CLI (`--silent`) and skip the desktop preflight.
2. **Terra LiDAR completion detection now has a proven signal**:
   `<terra_folder>/<project>_LiDAR/lidars/report/report.md` appears when
   reconstruction finishes; LAZ output lands in `lidars/terra_laz/`. The
   `terra_lidar` processor watches for it exactly as `classify_3dr.py` does today.
3. **Error handling currently blocks forever.** `_show_error_dialog()` runs a tkinter
   `mainloop()`, and the DPI check / missing-args warnings use `MessageBoxW`.
   → Add `--unattended` to all three automation scripts: no dialogs, errors to
   stderr + nonzero exit. Hand-run behavior unchanged.
4. **Subprocess output must never inherit an undrained pipe.** `classify_3dr.py`
   documents a real 3DR.exe deadlock: a child inheriting a pipe nobody reads hangs
   forever once the ~64KB buffer fills. → The agent runner always redirects child
   stdout/stderr to a log file.
5. **One job per machine is structural, not policy.** Two pywinauto automations fight
   over foreground focus. `max_parallel_jobs` is always 1.
6. **`AutomatePix4D.py` needs parameterizing** (argparse, same pattern as the Terra
   scripts) before it can be queued — and it currently ends at clicking *Start*, so
   Phase 4 adds save-project + close-app steps for after processing completes.
7. **The business-hours logic in `classify_3dr.py` becomes queue semantics.** Today:
   if classification fails 8am–5pm MST (human likely using 3DR), sleep until 5pm and
   retry. In the queue: the agent's preflight rejects a Cyclone job while `3DR.exe`
   is already running (human at the machine), so the job simply stays QUEUED and is
   retried on a later sync — no clock math. An optional per-node "quiet hours"
   config window stays available if wanted.

---

## 3. Decisions vs. the original spec

| Area | Original spec | This design | Why |
|---|---|---|---|
| Scheduler | Scoring system (+100 preferred, +30 idle, …) | Routing by capability; FIFO within priority | One machine per app — there is no choice to optimize. |
| Assignment | Background scheduler loop, `ASSIGNING` state | Assign synchronously inside the agent's poll request | No background races, no reconciliation, two fewer states. |
| Node comms | `register` + `heartbeat` + `next-job` endpoints | **One `/sync` endpoint.** First sync = registration (upsert). Every sync = heartbeat + telemetry + job request + cancel delivery. | One loop to keep alive, one endpoint to debug. |
| Node status | 7-value stored enum | `enabled` + `draining` booleans; online/busy **derived** from `last_sync_at` at read time | Stored status drifts and needs a janitor. Derived status is always correct. |
| Job states | 13 states | **7**: `QUEUED, ASSIGNED, RUNNING, SUCCEEDED, FAILED, CANCELLED, NEEDS_ATTENTION` (+ `cancel_requested` flag) | `BLOCKED` is derived (unmet deps); `PAUSED/COMPLETING/ASSIGNING/STARTING` add transitions without information. |
| Workflows | Workflow-definition tables, versions, engine | `depends_on` (list of job ids) on the job row; chain templates in coordinator config | Both production flows are 2-step linear chains. Dependency gating is a query condition, not an engine. |
| Data staging | Agent-managed scratch, NAS↔local copies | **Intake GUI copies data before submitting**; jobs carry UNC paths; agents work in place | Decided. Keeps agents dumb. `scratch_path` stays in the job model as an escape hatch if in-place NAS processing proves slow (§12.3). |
| Telemetry | Time-series table + retention | Latest snapshot JSON on the node row; job events in `job_events` | Dashboard needs *now*, not history. |
| Tray app | PySide6/PyQt tray application | Agent is a visible console window; the web dashboard is the monitoring surface | The agent must be interactive anyway; console output matches how the scripts already communicate. |
| Live dashboard | WebSocket / SSE | 5-second fetch polling | Three nodes, LAN. |
| Notifications | Notification service | Deferred; dashboard "attention" panel; webhook later | |
| Agent deploy | PyInstaller EXE | **PyInstaller EXE — decided.** `build.py` gains `agent` (and `coordinator`) targets; EXEs distributed from the NAS dist folder like today; `update_agent.ps1` copies new EXE + restarts the task | Matches the shop's existing workflow. |
| Security | Tokens + RBAC + CSRF + HTTPS | Per-node bearer token + LAN bind now; HTTPS/roles later if exposure changes | Matches actual threat model. |
| Database | SQLite designed for Postgres migration | SQLite WAL on the coordinator's **local disk — never a network share** (SQLite over SMB corrupts). Plain SQLAlchemy keeps Postgres possible | 3 nodes at 10s polls ≈ 0.3 writes/sec. |
| Repo layout | 8 top-level packages incl. `services/` | 6 packages, no service layer; intake GUI is cut over in place | Small codebase; indirection costs more than it buys. |

Kept from the spec unchanged: coordinator authority; poll-only agents; Task Scheduler
at-logon deployment (not a service — Session 0 can't touch the desktop); UNC paths
over mapped drives; **output validation gates completion, never exit code alone**;
structured logs; failure artifacts including screenshots; mock processor first.

---

## 4. Architecture

```text
                       ┌─────────────────────────────────────────┐
   INTAKE MACHINE      │  COORDINATOR — 192.168.35.67            │
   data_intake.py      │  (co-hosted on the Pix4D machine)       │
   copies data +       │  FastAPI + Uvicorn (single worker)      │
   converts RINEX,  ──▶│  SQLite (WAL, local disk)               │
   then POSTs          │  Dashboard (Jinja2 + 5s fetch polling)  │
   project + jobs      │  Assignment runs inside /sync           │
                       └───────────────▲─────────────────────────┘
                                       │ HTTP :8443 (LAN only; agents connect out)
                ┌──────────────────────┼──────────────────────┐
                │                      │                      │
          TERRA node             PIX4D node             CYCLONE node
          agent EXE in           agent EXE in           agent EXE in
          logged-in session      logged-in session      logged-in session
          DJI Terra +            Pix4Dmatic +           Cyclone 3DR
          DJI_AUTOMATE_*.exe     AutomatePix4D          (CLI, --silent)
                │                      │                      │
                └──────────────────────┼──────────────────────┘
                                       ▼
                          Shared storage (UGREEN NAS)
                 <root>\<client>\<project>\<date>\{Sensor, BaseData,
                                    PPK, Terra, Pix4D, …}
```

The coordinator lives on the Pix4D machine, which also runs its own agent —
coordinator and agent are separate processes and don't special-case each other (the
coordinator is featherweight next to a Pix4D run). If that box reboots, agents
elsewhere just back off and reconnect (§7); nothing is lost — all state is in SQLite.

Repo layout (this repo):

```text
Server/
├── coordinator/
│   ├── main.py            # FastAPI app + startup
│   ├── api.py             # all routes
│   ├── db.py              # SQLAlchemy models + session
│   ├── assign.py          # eligibility + pick-next-job (called from /sync)
│   ├── templates.py       # chain templates (photo_ppk, lidar) → job rows
│   ├── config.py
│   └── dashboard/
├── agent/
│   ├── main.py            # sync loop → run job → report
│   ├── client.py          # coordinator HTTP client (retrying, idempotent reports)
│   ├── preflight.py       # desktop / NAS / app checks (per job type)
│   ├── runner.py          # subprocess launch (output → log file), watchdog,
│   │                      # local state file, failure bundle, crash recovery
│   └── config.py
├── processors/
│   ├── base.py            # interface + SubprocessProcessor
│   ├── terra_ppk.py
│   ├── terra_lidar.py
│   ├── pix4dmatic.py
│   └── cyclone_classify.py
├── automation/            # existing payloads, moved UNCHANGED (+ --unattended)
│   ├── PyAutomateDJI.py
│   ├── DJIAutomatePPKV2.py
│   ├── AutomatePix4D.py
│   └── embed_ppk_metadata.py
├── intake/
│   └── queue_client.py    # small client data_intake.py calls to submit jobs
├── data_intake.py         # GUI (cutover in Phase 6: DJISequenceThread → queue_client)
├── classify_3dr.py        # source for cyclone_classify port; retired at cutover
├── shared/
│   └── schemas.py         # pydantic models + enums shared by both sides
├── scripts/               # install_coordinator.ps1, install_agent.ps1, update_agent.ps1
├── tests/
├── build.py               # + agent / coordinator PyInstaller targets
└── pyproject.toml
```

---

## 5. Data model (4 tables)

**projects** — `id, uuid, client, project_number, name, sensor_type, date_folder,
root_path, priority, status, metadata_json, created_at, updated_at`.
Status stays coarse (`ACTIVE, QA, ARCHIVED, CANCELLED`); "anything failed?" derives
from jobs.

**jobs** — `id, uuid, project_id, job_type, status, priority, depends_on_json,
parameters_json, assigned_node, scratch_path (unused for now, see §12.3),
cancel_requested, retry_count, max_retries (default 0), max_runtime_minutes,
created_at, assigned_at, started_at, last_progress_at, finished_at, exit_code,
error_message, progress_percent, progress_message, processor_version, agent_version`.

* `parameters_json` is **opaque to the coordinator** — only the processor interprets
  it. Adding Cyclone/Global Mapper/TBC never touches coordinator code: new processor
  module + capability string in one agent's config.
* Parameters are exactly what `data_intake._handle_complete()` builds today (§8).

**nodes** — `id, node_name (unique), token_hash, capabilities_json, enabled, draining,
agent_version, computer_name, current_user, last_sync_at, last_telemetry_json,
created_at, updated_at`. Online = `last_sync_at` within 90s, computed at read time.

**job_events** — `id, job_id, ts, type, message, details_json`. Append-only audit
trail (assigned, started, progress, validation, failure-artifact path, retries).

**Job state machine**

```text
QUEUED → ASSIGNED → RUNNING → SUCCEEDED
                        ├──→ FAILED          (launch/validation/timeout/crash)
                        └──→ CANCELLED       (cancel_requested honored by agent)
ASSIGNED → QUEUED                            (lease expired: agent never confirmed start)
RUNNING/ASSIGNED → NEEDS_ATTENTION           (node lost mid-job; human decides)
NEEDS_ATTENTION/FAILED → QUEUED              (manual retry from dashboard)
```

Rules:

* **All timestamps stamped by the coordinator.** Workstation clocks are not trusted.
* **Lease reclaim:** `ASSIGNED` with no `started` report within 5 min → back to
  `QUEUED` (3 strikes → `NEEDS_ATTENTION`).
* **A vanished node never auto-fails a job** — the app may still be running. Job goes
  `NEEDS_ATTENTION`; the reconnecting agent reconciles from its local state file.
* **Retries default to 0 for GUI jobs** (blind reruns create half-built duplicate
  projects). Retry is a human button on the dashboard. Cyclone (CLI, idempotent per
  file) may set `max_retries=1`.
* Eligibility = `status=QUEUED` ∧ all `depends_on` `SUCCEEDED` ∧ `job_type ∈
  node.capabilities` ∧ node enabled ∧ not draining ∧ no active job on node.
  Order by `priority desc, created_at asc`. Assignment is one atomic
  `UPDATE … WHERE status='QUEUED'`.

---

## 6. API (~14 routes, `/api/v1`)

Agent-facing (bearer token per node):

```text
POST /nodes/{node_name}/sync        # registration + heartbeat + telemetry +
                                    # job request + cancel delivery, all in one
POST /jobs/{id}/started             # idempotent
POST /jobs/{id}/progress            # percent/stage/message → job + event log
POST /jobs/{id}/succeeded           # includes validation summary; idempotent
POST /jobs/{id}/failed              # error code/message + artifacts path; idempotent
POST /jobs/{id}/cancelled           # agent confirms a delivered cancel; idempotent
```

`/sync` request: agent version, capabilities, active job ids + progress snapshot,
telemetry (cpu/ram/gpu/scratch-free/NAS-ok), desktop-preflight status.
`/sync` response: `{assign: job|null, cancel_job_ids: [], drain: bool,
poll_after_seconds: n}`.

Intake/admin-facing:

```text
POST /projects                      # create project + job chain from a template
POST /jobs                          # create a single job
POST /nodes                         # provision a node; returns its token once
GET  /projects /projects/{id} /jobs /jobs/{id} /nodes
POST /jobs/{id}/retry  /jobs/{id}/cancel
POST /nodes/{name}/drain /nodes/{name}/enable /nodes/{name}/disable
GET  /health
```

Dashboard: server-rendered pages + a JSON status endpoint polled every 5s.
Panels: nodes (online/job/progress/telemetry), queue, active jobs, **attention**
(failed, needs-attention, stalled = no progress in N min, offline nodes).

---

## 7. Agent

Startup: load config → logging → read local state file → reconcile any interrupted
job (process still alive? resume watching. dead? report outcome from evidence) →
sync loop.

**Preflight before accepting a job** (reject with a specific error code; job stays
queued or goes to attention depending on the error):

1. *GUI job types only* (Terra, Pix4D): session unlocked, expected resolution,
   DPI 150%, foreground obtainable.
2. Required app installed, and **not already running** — a leftover Terra instance
   would receive clicks in the wrong state; a human using 3DR means the Cyclone job
   politely stays queued.
3. Job's UNC paths reachable/writable.
4. No other active job.

**Execution:** write local state file (atomic: temp + `os.replace`) → launch
payload via `subprocess.Popen` with `--unattended --log-file …`, stdout/stderr
redirected to a per-job log file (never an inherited pipe — the 3DR deadlock
lesson) → watchdog loop (process alive, progress sources, `max_runtime_minutes`
kill via `taskkill /T`, cancel check each sync) → processor-specific completion
watch (§8) → processor validates outputs → report succeeded/failed with evidence.

**On failure:** capture screenshot + agent log tail + payload log + job params into
a per-job `_failure/` folder (local work dir), path referenced in the failure
report. Highest-value debugging feature for GUI automation — in from day one.

**Local work dir** (`C:\ProgramData\DataIntakeAgent\`): agent config, logs, job
state file, failure bundles. Failure bundles retained 7 days. (No data staging —
project data lives on the NAS and is the GUI's responsibility.)

**Recovery matrix:**

| Event | Behavior |
|---|---|
| Coordinator down | Keep running/watching current job, buffer progress, exponential backoff (poll caps at 60s), report when back |
| Agent crash / reboot mid-job | State file names the job + PID; on restart: PID alive → resume watching; dead → validate what's on disk, report success or failure with evidence |
| NAS blip | Retry with backoff; only fail after N attempts |
| Windows Update reboot | Same as reboot; Task Scheduler relaunches agent at logon (auto-logon processing account) |

---

## 8. Processors

```python
class Processor:
    job_types: set[str]
    def preflight(self, job) -> list[str]           # job-specific checks
    def build_command(self, job) -> list[str]       # argv for the payload
    def watch(self, proc, job, report) -> None      # progress + completion detection
    def validate_outputs(self, job) -> Validation   # files exist/size/mtime/count
```

`SubprocessProcessor` is the shared base. **The automation scripts are not refactored
into importable modules** — they move to `automation/` unchanged (plus
`--unattended`) and keep shipping as the same EXEs.

| Processor | Payload | Completion signal | Validation |
|---|---|---|---|
| `terra_ppk` | `DJI_AUTOMATE_PPK.exe` | Script runs to completion itself (waits for `POS.txt`, embeds EXIF) → exit code | `POS.txt` exists; embedded image count > 0 under `<date>/PPK` |
| `terra_lidar` | `DJI_AUTOMATE_UI.exe` | EXE exits after *Start Reconstruction*; agent then watches for `<terra>/<project>_LiDAR/lidars/report/report.md` (poll ~60s, per `classify_3dr.py`) | `report.md` present; `.las/.laz` in `lidars/terra_laz/`, > min size, mtime > job start |
| `pix4dmatic` | `AutomatePix4D.py` (parameterized in Phase 0; save + close steps added in Phase 4) | **Tail Pix4Dmatic's log** (watchdog-style file watch) — it logs each processing stage and completion; stage lines feed progress updates | Log reports completion; **orthomosaic** (the final export) exists, > min size, mtime > job start. Automation then saves the project and closes Pix4Dmatic |
| `cyclone_classify` | `3DR.exe --Script=ClassifyLAZ.js --scriptAutorun --silent --scriptParam=…` per LAZ file | Output `.3dr` file appears + size stable ~20s (port of `classify_3dr._classify_one`, incl. 6h per-file timeout and the ≥8GB merged-vs-tiles rule) | One `.3dr` per input LAZ |

**Job parameters** (exactly what `data_intake._handle_complete()` builds today):

```text
TERRA_PPK:        project_name, project_location=<date>/PPK, data_source=<flight dir>,
                  terra_path=<date>/Terra, ppk_path=<date>/PPK, epsg_h, epsg_v
TERRA_LIDAR:      project_name, project_location=<date>/Terra, data_source=<sensor dir>,
                  gcp_path, epsg_h, epsg_v, no_targets
PIX4D_MATIC:      project_name, project_root=<date>, epsg_h, epsg_v,
                  tat_path (the targets/TAT csv from intake, used as-is)
CYCLONE_CLASSIFY: terra_folder=<date>/Terra, project_name=<name>_LiDAR, model_name
```

Note on targets files: the Terra LiDAR script extracts TLT rows from the targets
CSV into `SINGLE_TLT.csv` for its own GCP import — that file is LiDAR-only.
Pix4D imports the TAT csv directly; no extraction happens outside the LiDAR script.

**Chain templates** (coordinator config; intake picks one per submission):

```yaml
templates:
  photo_ppk:           # M3E / P1 — after intake copy + RINEX convert
    - job_type: TERRA_PPK
    - job_type: PIX4D_MATIC
      depends_on: [TERRA_PPK]
  lidar:               # L2 / L3 — after intake copy + RINEX convert
    - job_type: TERRA_LIDAR
    - job_type: CYCLONE_CLASSIFY
      depends_on: [TERRA_LIDAR]
```

A project may carry both chains (both toggles checked today). The per-node
single-job rule serializes whatever lands on the same machine; `depends_on` orders
the rest. Capability strings = job types, declared in each agent's config.

---

## 9. Resilience rules (operational safeguards)

* One active job per workstation, structurally.
* No arbitrary commands via API — `parameters_json` feeds an allowlisted argv
  builder in the processor; paths validated against configured roots.
* Completion requires output validation, never exit code alone (Terra LiDAR's EXE
  exiting is the *start* of the wait, not the end).
* Child processes never inherit pipes; output goes to per-job log files.
* No duplicate assignment (atomic conditional UPDATE; single uvicorn worker).
* No auto-fail on missing heartbeat; `NEEDS_ATTENTION` + reconcile instead.
* Agent report endpoints idempotent (safe to retry over a flaky network).
* All payloads run `--unattended` under the agent — no dialog can block a job.
* Desktop preflight before GUI jobs (DPI / resolution / lock state).
* "App already running" preflight keeps jobs queued instead of failing them
  (covers the human-using-3DR case without clock rules).
* SQLite on the coordinator's local disk; nightly file backup to the NAS.
* Retries default 0 for GUI processors; retry is a human action.
* Server-side timestamps everywhere.

---

## 10. Security & deployment

* Per-node bearer token (random, hashed in DB), installed once per machine.
  Dashboard on the LAN, simple shared admin token initially; HTTPS via reverse
  proxy later if exposure grows.
* **Coordinator** on 192.168.35.67 (the Pix4D machine): `coordinator.exe` (PyInstaller) under Task
  Scheduler at-boot (or NSSM), single worker, one inbound firewall rule TCP 8443
  scoped to the LAN subnet. SQLite + logs under `C:\ProgramData\DataIntakeCoordinator\`.
* **Agents**: `agent.exe` (PyInstaller) via Task Scheduler at-logon of the
  processing account (auto-logon), *run only when logged on*, restart on failure,
  30s delay. Config YAML next to the EXE.
* **Build & update**: `build.py agent` / `build.py coordinator` produce EXEs into
  `dist/`, copied to the NAS dist share (same pattern as `DJI_AUTOMATE_UI.exe`
  today). `scripts/update_agent.ps1` on each box: stop task → copy new EXE from
  the share → start task. The dashboard shows each node's `agent_version`, so
  stale agents are visible at a glance.

---

## 11. Phased plan

Migration safety: `data_intake.py` keeps working exactly as it does today until
Phase 6 — the queue is built and proven alongside it, then the GUI's launch step is
swapped. No disruption window.

**Phase 0 — Repo restructure + payload hardening (small)**
Move automation scripts to `automation/` unchanged; add `--unattended` to all three;
add argparse to `AutomatePix4D.py`; `pyproject.toml`; `build.py` targets for
agent/coordinator.
✓ Scripts still run by hand and from today's GUI exactly as before.

**Phase 1 — Coordinator core**
DB models, `/sync` with assignment, job report endpoints, project/job creation with
chain templates, minimal dashboard, node tokens. Fake-agent script for testing.
✓ Fake agent registers, receives a chained job set in order, reports; dashboard
shows it all.

**Phase 2 — Real agent + mock processor**
Sync loop, preflight, runner + watchdog + state file, failure bundle, recovery.
PyInstaller `agent.exe` built and installed via script on one box.
✓ Kill the agent mid-job and reboot the box: the job recovers or lands
NEEDS_ATTENTION with evidence — never duplicated, never silently lost.

**Phase 3 — Terra node live**
`terra_ppk` first (script already runs to completion), then `terra_lidar` with the
`report.md` watcher.
✓ A PPK job submitted at the coordinator runs visibly on the Terra machine,
outputs validated on the NAS, job completes hands-off.

**Phase 4 — Pix4D node + photo chain**
`pix4dmatic` processor on the parameterized script: log-tail progress/completion,
ortho validation, and new save-project + close-app automation steps (script
currently ends at *Start*); pin down the Pix4Dmatic log path + stage strings on
the machine; `photo_ppk` template end-to-end (PPK on Terra box → Pix4Dmatic on
Pix4D box via the NAS PPK folder).
✓ One submission drives both machines in sequence.

**Phase 5 — Cyclone node + LiDAR chain**
`cyclone_classify` processor (port `Classify3DRThread`); `lidar` template
end-to-end (Terra LiDAR → classification on the Cyclone box).
✓ report.md-gated classification runs on its own machine; human-in-use preflight
keeps jobs queued instead of failing.

**Phase 6 — Intake cutover + ops polish**
`intake/queue_client.py`; `data_intake.py` swaps `DJISequenceThread` +
`Classify3DRThread` for a single "submit to queue" call (keeps copy + RINEX);
dashboard retry/cancel/drain controls; nightly DB backup; docs.
✓ Office staff submit from the intake GUI and watch the dashboard; nobody RDPs
into processing machines for normal work.

---

## 12. Watch items (nothing blocks execution)

1. **In-place NAS processing performance**: Terra/Pix4D project folders live on the
   UGREEN share (GUI copies there; agents work in place — decided). If a real job
   shows SMB slowness/instability, the fallback is agent-side scratch staging; the
   job model keeps `scratch_path` so this can be added per job type without schema
   changes. Watch during Phases 3–4.
2. **Pix4Dmatic log specifics**: log tailing is the decided progress/completion
   source; the exact log path and stage strings get pinned down on the Pix4D
   machine during Phase 4.

Resolved 2026-07-10: coordinator host = the Pix4D machine (192.168.35.67);
`SINGLE_TLT.csv` is Terra-LiDAR-only and Pix4D consumes the TAT csv directly;
Pix4D completion = log-reported completion + orthomosaic present, then the
automation saves the project and closes Pix4Dmatic.
