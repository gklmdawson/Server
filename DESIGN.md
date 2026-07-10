# Job Queue Server — Reviewed Design (v1 draft, for discussion)

**Repo:** `gklmdawson/Server` · **Branch:** `claude/job-queue-server-design-0rvfqg`
**Status:** proposal — nothing here is built yet; this doc is the working spec to iterate on.

This is the ChatGPT build spec ("Data Intake Distributed Processing System") reviewed
against the actual code in this repo, trimmed where it was over-engineered for a
3-workstation shop, and hardened where the existing scripts show real failure modes.
Where this doc and the original spec disagree, this doc wins.

---

## 1. Goals and non-negotiables

Unchanged from the original spec:

* One dedicated machine per licensed app — Terra on TERRA-01, Pix4Dmatic on PIX4D-01,
  Cyclone 3DR on CYCLONE-01. Routing **is** license binding.
* Processing apps run visibly in the logged-in interactive desktop session.
* Agents make outbound connections only. No inbound ports, WinRM, PsExec, or remote
  execution on workstations. The coordinator never reaches into a workstation.
* The coordinator is the single source of truth. Agents never touch the database.
* NAS (UGREEN) holds source data and final outputs; local NVMe is scratch.
* Buildable and maintainable by one internal Python developer. No Redis, Celery,
  RabbitMQ, Docker, or Kubernetes.

---

## 2. What exists today (code-review findings)

| File | Role | State |
|---|---|---|
| `PyAutomateDJI.py` | Terra LiDAR reconstruction (create project → EPSG → GCP/TLT import → start reconstruction) | Parameterized via CLI args + `DJI_PARAMETERS.ini`; built to EXE |
| `DJIAutomatePPKV2.py` | Terra PPK (visible-light project → PPK calc → export POS.txt → EXIF/XMP embed) | Parameterized via CLI args; built to EXE; supersedes `DJIAutomatePPK.py` |
| `AutomatePix4D.py` | Pix4Dmatic (import `<root>/PPK` → CRS → templates → targets → start) | **Params still hardcoded** (`TODO: will be passed in from data_intake.py`) |
| `embed_ppk_metadata.py` | EXIF/XMP writer used by PPK script | Library, fine as-is |
| `UIInspect.py` | Dev tool for measuring UIA coordinates | Keep as dev tool |
| `build.py` | PyInstaller builds + interactive commit/tag/push | Keep for standalone EXE workflow |

Findings that shape the design:

1. **The GUI automation contract includes the desktop itself.** Clicks are pixel
   offsets from UIA anchors, calibrated at 150% DPI on a specific resolution.
   A locked screen, RDP resize, or scaling change silently breaks everything.
   → The agent must *preflight the desktop* (DPI, resolution, session unlocked,
   foreground available) before accepting a GUI job, and reject with a specific
   error otherwise.
2. **Completion detection does not exist yet for Terra LiDAR.** `PyAutomateDJI.py`
   ends right after clicking *Start Reconstruction* ("Next steps will go here...").
   Watching the run to completion and validating outputs is **new work**, not a
   refactor. (The PPK script *does* run to completion — it waits for `POS.txt` and
   finishes the embed — so it's the easiest first integration.)
3. **Error handling currently blocks forever.** `_show_error_dialog()` runs a tkinter
   `mainloop()`, and the DPI check / missing-args warnings use `MessageBoxW`. With
   nobody at the desk, a failed job would hang indefinitely.
   → Add an `--unattended` flag to all three scripts: no dialogs, errors go to
   stderr + nonzero exit code. (Small, surgical edits; interactive behavior
   unchanged when run by hand.)
4. **One job per machine is structural, not a policy.** Two pywinauto automations
   would fight over foreground focus. `max_parallel_jobs` is always 1 for GUI
   processors — which deletes most of the scheduling complexity in the spec.
5. **The intake GUI lives outside this repo.** The scripts are launched *by*
   `data_intake.py` (Sunrise-Intake) with CLI args. That arg list is already the
   de-facto job-parameters schema — the coordinator should adopt it as-is.
6. **There is already a real cross-machine workflow.** Pix4Dmatic imports
   `<project>/PPK` — the *output* of the Terra PPK job. Once apps live on dedicated
   machines, that hand-off must go through the NAS, and the PIX4D job must wait on
   the PPK job. This is the concrete case that justifies job dependencies (and
   nothing heavier).

---

## 3. Decisions vs. the original spec

What changes, and why:

| Area | Original spec | This design | Why |
|---|---|---|---|
| Scheduler | Scoring system (+100 preferred, +30 idle, …) | Routing by capability; FIFO within priority | With one machine per app there is no choice to optimize. A scoring engine tunes a decision that doesn't exist. |
| Assignment | Background scheduler loop, `ASSIGNING` state | Assign synchronously inside the agent's poll request | No background races, no reconciliation between loop and API, two fewer states. |
| Node comms | `register` + `heartbeat` + `next-job` endpoints, two agent loops | **One `/sync` endpoint.** First sync = registration (upsert). Every sync = heartbeat + telemetry + job request + cancel delivery. | One loop to keep alive, one endpoint to debug. Cancel arrives for free on the next sync. |
| Node status | 7-value stored enum (ONLINE…DISABLED) | `enabled` + `draining` booleans (admin intent) — online/busy **derived** from `last_sync_at` and active jobs at read time | Stored status drifts and needs a janitor process. Derived status is always correct. |
| Job states | 13 states | **7**: `QUEUED, ASSIGNED, RUNNING, SUCCEEDED, FAILED, CANCELLED, NEEDS_ATTENTION` (+ `cancel_requested` flag) | `BLOCKED` is derived (unmet deps), `PAUSED`/`COMPLETING`/`ASSIGNING`/`STARTING` add transitions without adding information. |
| Workflows | Workflow-definition tables, versions, engine | `depends_on` (list of job ids) on the job row; intake templates (in config) create job chains | Today's flows are linear chains. Dependency gating is a query condition (`all parents SUCCEEDED`), not an engine. DAG-ready if ever needed. |
| Telemetry | Time-series table + retention policy | Latest snapshot JSON on the node row; job-scoped events in `job_events` | The dashboard needs *now*, not history. Add a table later if wanted — nothing depends on it. |
| Tray app | PySide6/PyQt tray application | Agent runs as a visible console window; the web dashboard is the monitoring surface | The agent must be interactive anyway; a console shows exactly what the scripts already print. A Qt tray + PyInstaller is a sub-project — revisit after MVP. |
| Live dashboard | WebSocket / SSE | Plain 5-second fetch polling | Three nodes, LAN. Zero extra moving parts. |
| Notifications | Notification service | Deferred; dashboard "attention" panel covers MVP; a webhook (email/Teams) is a later 20-line add | |
| Agent deploy | PyInstaller EXE | Proposal: git checkout + venv + PowerShell bootstrap; update = `git pull` + restart task | Pixel offsets get retuned often — iteration speed on the workstation matters more than a single file. (`build.py` EXE flow stays for the standalone tools.) Open question §12. |
| Security | Tokens + RBAC + CSRF + HTTPS | Per-node bearer token + bind to LAN interface now. HTTPS/roles when exposure changes | Matches the actual threat model (private LAN, 3 known machines). Token check is ~20 lines; keep it. |
| Database | SQLite designed for Postgres migration | SQLite in WAL mode, **on the coordinator's local disk — never on the NAS** (SQLite over SMB corrupts). Plain SQLAlchemy models keep Postgres possible | 3 nodes polling at 10s is ~0.3 writes/sec. SQLite will never be the bottleneck here. |
| Repo layout | 8 top-level packages incl. `services/`, `intake/` | 6 packages, no service layer, intake GUI stays in its own repo and calls the API | Small codebase; indirection costs more than it buys. |

Kept from the spec unchanged (it got these right): coordinator authority; poll-only
agents; Task Scheduler at-logon deployment (not a service — Session 0 can't touch the
desktop); UNC paths over mapped drives; scratch retention windows + `.keep` marker;
**output validation gates completion, never the exit code alone**; structured logs;
failure artifacts including screenshots; phased build starting with a mock processor.

---

## 4. Architecture

```text
            ┌────────────────────────────────────────────┐
            │  COORDINATOR (Windows NUC, always on)      │
            │  FastAPI + Uvicorn (single worker)         │
            │  SQLite (WAL, local disk)                  │
            │  Dashboard (Jinja2 + fetch polling)        │
            │  Assignment logic (runs inside /sync)      │
            └───────────────────▲────────────────────────┘
                                │ HTTP :8443 (LAN only, outbound from agents)
        ┌───────────────────────┼───────────────────────┐
        │                       │                       │
  TERRA-01 agent          PIX4D-01 agent          CYCLONE-01 agent
  (console app in         (console app in         (console app in
   logged-in session)      logged-in session)      logged-in session)
   DJI Terra + scripts     Pix4Dmatic + script     Cyclone 3DR (phase 6)
   D:\Scratch              D:\Scratch              D:\Scratch
        │                       │                       │
        └───────────────────────┼───────────────────────┘
                                ▼
                    \\UGREEN  (NVMe NAS, 10 GbE)
                    Incoming / Active / Deliverables / ArchiveQueue
```

Proposed repo layout (this repo):

```text
Server/
├── coordinator/
│   ├── main.py            # FastAPI app + startup
│   ├── api.py             # all routes (split only when it hurts)
│   ├── db.py              # SQLAlchemy models + session
│   ├── assign.py          # eligibility + pick-next-job (called from /sync)
│   ├── config.py
│   └── dashboard/         # templates/ + static/
├── agent/
│   ├── main.py            # sync loop → run job → report
│   ├── preflight.py       # desktop/NAS/scratch/app checks
│   ├── runner.py          # subprocess launch, watchdog, state file, failure bundle
│   ├── scratch.py         # staging + retention cleanup
│   └── config.py
├── processors/
│   ├── base.py            # interface + SubprocessProcessor
│   ├── terra_ppk.py
│   ├── terra_lidar.py
│   └── pix4dmatic.py
├── automation/            # existing scripts move here UNCHANGED (+ --unattended flag)
│   ├── PyAutomateDJI.py
│   ├── DJIAutomatePPKV2.py
│   ├── AutomatePix4D.py
│   └── embed_ppk_metadata.py
├── shared/
│   └── schemas.py         # pydantic models + enums shared by both sides
├── scripts/               # install_agent.ps1, install_coordinator.ps1, update.ps1
├── tests/
└── pyproject.toml
```

---

## 5. Data model (4 tables)

**projects** — `id, uuid, client, project_number, name, sensor_type, source_path,
active_path, output_path, priority, status, metadata_json, created_at, updated_at`.
Status stays coarse: `ACTIVE, QA, ARCHIVED, CANCELLED` — per-stage detail lives on jobs;
"anything failed?" is derived from its jobs.

**jobs** — `id, uuid, project_id, job_type, status, priority, depends_on_json,
parameters_json, assigned_node, scratch_path, cancel_requested, retry_count,
max_retries (default 0), max_runtime_minutes, created_at, assigned_at, started_at,
last_progress_at, finished_at, exit_code, error_message, progress_percent,
progress_message, processor_version, agent_version`.

* `parameters_json` is **opaque to the coordinator** — only the processor on the agent
  interprets it. This is the key future-proofing rule: adding Cyclone/Global Mapper/TBC
  never touches coordinator code, only a new processor module + a capability string.
* Parameters for the existing scripts are exactly their current CLI args
  (`project_name, terra_path, data_source, epsg_h, epsg_v, gcp_path, ppk_path,
  no_targets, …`).

**nodes** — `id, node_name (unique), token_hash, capabilities_json, enabled, draining,
agent_version, computer_name, current_user, last_sync_at, last_telemetry_json,
created_at, updated_at`. Online = `last_sync_at` within 90s, computed at read time.

**job_events** — `id, job_id, ts, type, message, details_json`. Append-only audit trail
(assigned, started, progress, validation, failure artifacts path, retries, scheduler
decisions).

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

* **All timestamps are stamped by the coordinator.** Workstation clocks are not trusted.
* **Lease reclaim:** `ASSIGNED` with no `started` report within 5 min → back to `QUEUED`
  (3 strikes → `NEEDS_ATTENTION`).
* **A vanished node never auto-fails a job** — the app may still be running. Job goes
  `NEEDS_ATTENTION`; when the agent reconnects it reconciles from its local state file.
* **Retries default to 0 for GUI jobs.** Blind re-runs of GUI automation can create
  half-built duplicate projects. Retry is a human button on the dashboard (which is
  cheap because job creation is idempotent and scratch is per-job).
* Eligibility for assignment = `status=QUEUED` ∧ all `depends_on` jobs `SUCCEEDED`
  ∧ `job_type ∈ node.capabilities` ∧ node enabled ∧ not draining ∧ node has no active
  job. Order by `priority desc, created_at asc`. Assignment is one atomic
  `UPDATE … WHERE status='QUEUED'`.

---

## 6. API (~14 routes, `/api/v1`)

Agent-facing (bearer token per node):

```text
POST /nodes/{node_name}/sync        # THE endpoint: registration, heartbeat,
                                    # telemetry, job request, cancel delivery
POST /jobs/{id}/started             # idempotent
POST /jobs/{id}/progress            # percent/stage/message → job + event log
POST /jobs/{id}/succeeded           # includes validation summary; idempotent
POST /jobs/{id}/failed              # error code/message + artifacts path; idempotent
```

`/sync` request: agent version, capabilities, active job ids + progress snapshot,
telemetry (cpu/ram/gpu/scratch-free/NAS-ok), desktop-preflight status.
`/sync` response: `{assign: job|null, cancel_job_ids: [], drain: bool,
poll_after_seconds: n}`.

Intake/admin-facing:

```text
POST /projects                      # create project + job chain from a template
POST /jobs                          # create a single job
GET  /projects /projects/{id} /jobs /jobs/{id} /nodes
POST /jobs/{id}/retry  /jobs/{id}/cancel
POST /nodes/{name}/drain /nodes/{name}/enable /nodes/{name}/disable
GET  /health
```

Dashboard: server-rendered pages + a JSON status endpoint polled every 5s.
Panels: nodes (online/job/progress/telemetry), queue, active jobs, **attention**
(failed, needs-attention, stalled = no progress in N min, offline nodes, low scratch).

---

## 7. Agent

Startup: load config → logging → read local state file → reconcile any interrupted job
(process still alive? watch it. dead? report outcome from evidence) → sync loop.

**Preflight before accepting any GUI job** (reject with specific error code):

1. Desktop: session unlocked, expected resolution, DPI 150%, foreground obtainable.
2. NAS source path reachable; output path writable.
3. Scratch root: exists, writable, ≥ configured free space.
4. Required app installed (exe exists) and **not already running** (a leftover Terra
   instance would receive our clicks in the wrong state).
5. No other active job.

**Execution:** stage inputs (robocopy NAS → scratch when the job asks for it) → write
local state file (atomic: temp + `os.replace`) → launch script via `subprocess.Popen`
with `--unattended --log-file …` → watchdog loop (process alive, progress sources,
`max_runtime_minutes` kill via `taskkill /T`, cancel check each sync) → on exit:
processor validates outputs → robocopy results scratch → NAS (write to `_partial`
suffix, rename when complete) → report succeeded/failed with evidence.

**On failure:** capture screenshot + agent log tail + script log + job params into
`scratch/<job>/_failure/`, referenced in the failure report. This is the single
highest-value debugging feature for GUI automation — keep it from day one.

**Scratch retention** (from spec, unchanged): success 24h, failed 7d, cancelled 3d,
`.keep` marker protects. Cleanup runs hourly, only after coordinator has acknowledged
the terminal report.

**Recovery matrix:**

| Event | Behavior |
|---|---|
| Coordinator down | Agent keeps running/watching current job, buffers progress, exponential backoff (poll caps at 60s), reports when back |
| Agent crash / reboot mid-job | State file names the job + PID; on restart: PID alive → resume watching; dead → validate what's on disk, report success or failure with evidence |
| NAS blip | Retry stage/copy with backoff; only fail after N attempts; never delete scratch on copy failure |
| Windows Update reboot | Same as reboot; Task Scheduler relaunches agent at logon (auto-logon on processing account, per spec §13) |

---

## 8. Processors

```python
class Processor:
    job_types: set[str]                     # e.g. {"TERRA_PPK"}
    def preflight(self, job) -> list[str]           # job-specific checks (beyond agent's)
    def build_command(self, job) -> list[str]       # argv for the existing script/EXE
    def watch(self, proc, job, report) -> None      # progress + completion detection
    def validate_outputs(self, job) -> Validation   # files exist/size/mtime/count
```

`SubprocessProcessor` is the default base — all three apps are driven by launching the
existing scripts with args. **The automation scripts are not refactored into importable
modules for MVP** — they move into `automation/` unchanged (plus `--unattended`).
Refactor later only if a reason appears.

| Processor | Completion signal | Validation | Notes |
|---|---|---|---|
| `terra_ppk` | Script already runs to completion (waits for POS.txt, embeds EXIF) → exit code | `POS.txt` exists; embedded image count > 0 in `<root>/PPK` | **Integrate first** — cleanest story |
| `terra_lidar` | New watcher: script exits after starting reconstruction → watch Terra process + output dir for `.las`/export products + quiescence | `.las` present, > min size, mtime > job start | Needs a calibration session on TERRA-01 to identify the reliable "done" signal |
| `pix4dmatic` | Watch project dir (report/outputs) after Start | Expected exports exist, > min size | Prereq: parameterize `AutomatePix4D.py` (replace hardcoded constants with argparse, same pattern as Terra scripts) |

Capability strings = job types (`TERRA_PPK`, `TERRA_LIDAR`, `PIX4D_MATIC`,
`CYCLONE_REGISTER`, …), declared in each agent's config. Adding a processor = new
module + capability string in one agent's config. Coordinator code untouched.

---

## 9. Resilience rules (operational safeguards)

Spec's list, trimmed and extended with what the code review surfaced:

* One active job per workstation, structurally.
* No arbitrary commands via API — `parameters_json` feeds an allowlisted argv builder
  in the processor; paths are validated against configured roots.
* Job completion requires output validation, never exit code alone.
* No scratch deletion before validation + coordinator ack + retention window.
* No duplicate assignment (atomic conditional UPDATE; single uvicorn worker).
* No auto-fail on missing heartbeat; `NEEDS_ATTENTION` + reconcile instead.
* Agent report endpoints are idempotent (safe to retry over a flaky network).
* All scripts run `--unattended` under the agent — no dialog can ever block a job.
* Desktop preflight before GUI jobs (DPI/resolution/lock state).
* SQLite lives on the coordinator's local disk; nightly file backup to the NAS.
* Retries default 0 for GUI processors; retry is a human action.
* NAS writes land in `_partial` then atomic rename; robocopy for bulk copies.
* Server-side timestamps everywhere.

---

## 10. Security & deployment

* Per-node bearer token (random, hashed in DB), delivered once at install into the
  agent machine's env/DPAPI store. Admin/dashboard behind the LAN + a simple shared
  admin token initially; HTTPS via reverse proxy when/if exposure grows.
* Coordinator: NUC, `uvicorn` single worker under Task Scheduler at-boot (or NSSM),
  one inbound firewall rule TCP 8443 restricted to the LAN subnet.
* Agents: Task Scheduler at-logon of the processing account (`ProcessingSvc`-style,
  auto-logon), *run only when logged on*, restart on failure, 30s delay — per spec §24.
* Agent install/update: `scripts/install_agent.ps1` (venv, config, task registration),
  `scripts/update.ps1` (`git pull` + `pip sync` + restart task). No PyInstaller needed
  for the agent (open question §12).

---

## 11. Phased plan

Each phase ends demonstrable; nothing depends on a later phase.

**Phase 0 — Repo restructure + script hardening (small)**
Move scripts to `automation/` unchanged; add `--unattended` to all three; add argparse
to `AutomatePix4D.py`; `pyproject.toml` with pinned deps.
✓ Scripts still run by hand exactly as before.

**Phase 1 — Coordinator core**
DB models, `/sync` with assignment, job report endpoints, project/job creation,
minimal dashboard, node tokens. Fake-agent script for testing.
✓ Fake agent registers, gets a job, reports it done; dashboard shows it.

**Phase 2 — Real agent + mock processor**
Sync loop, preflight, runner + watchdog + state file, scratch manager, failure bundle,
recovery-from-state-file. Mock processor (sleeps, writes a file).
✓ Kill the agent mid-job and reboot the box: job recovers or lands NEEDS_ATTENTION
with evidence — never duplicated, never silently lost.

**Phase 3 — Terra on TERRA-01**
`terra_ppk` first, then `terra_lidar` (includes the completion-watcher discovery
session on the real machine).
✓ PPK job submitted at the coordinator runs visibly on TERRA-01, outputs land on NAS,
validated, job completes without a human touching the box.

**Phase 4 — Pix4D on PIX4D-01 + the real chain**
`pix4dmatic` processor; intake template creating `TERRA_PPK → PIX4D_MATIC` with
`depends_on`; NAS hand-off of the PPK folder.
✓ One submission drives both machines in sequence.

**Phase 5 — Intake integration + ops polish**
Data Intake GUI posts projects/jobs to the coordinator (small client in the intake
repo) instead of launching EXEs; dashboard retry/cancel/drain controls; install
scripts; DB backup task.
✓ Office staff never RDP into processing machines for normal work.

**Phase 6 — Cyclone 3DR + extras**
`cyclone` processor (there is an existing `gklmdawson/Cyclone3DR` repo to review),
priorities UI, webhook notifications, telemetry history if still wanted.

---

## 12. Open questions (blocking Phase 1 start)

1. **Where does the intake GUI live and how do we integrate?** `Sunrise-Intake`
   (0riginalUsername) appears to be it; there is also an older `gklmdawson/DataIntake`
   repo. Preferred: the GUI keeps its screens and swaps "launch EXE" for "POST to
   coordinator". Needs that repo added to a session to wire up.
2. **Agent deployment:** git+venv (fast iteration, proposed) vs PyInstaller EXE
   (no Python install on workstations, but slow to iterate)?
3. **Which chains at MVP?** Photo→PPK→Pix4D confirmed? LiDAR→Terra standalone?
   Where does Cyclone 3DR sit in the pipeline (registration after Terra)?
4. **Data staging ownership:** today `DJI_PARAMETERS.ini` points at local `C:\3Ddata`
   paths — someone copies from the NAS first. Should the agent own NAS→scratch staging
   (proposed), or does intake pre-stage to the target machine?
5. **Coordinator host:** confirm the Windows NUC (always-on) and its name/IP.
6. **Cyclone 3DR automation:** does `gklmdawson/Cyclone3DR` already automate it
   (3DR script engine?), or is that greenfield?
