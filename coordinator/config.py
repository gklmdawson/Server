"""Coordinator configuration.

Loaded from a YAML file (see config/coordinator.example.yaml); every field has
a sane default so the coordinator also starts with no config at all for
development. Secrets (admin token) can come from the environment instead of
the file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    # Photo workflow (M3E/P1): PPK on the Terra box feeds Pix4Dmatic via the
    # NAS <date>/PPK folder.
    "photo_ppk": [
        {"job_type": "TERRA_PPK"},
        {"job_type": "PIX4D_MATIC", "depends_on": ["TERRA_PPK"]},
    ],
    # LiDAR workflow (L2/L3): Terra reconstruction, then Cyclone 3DR
    # classification gated on lidars/report/report.md output.
    "lidar": [
        {"job_type": "TERRA_LIDAR"},
        {"job_type": "CYCLONE_CLASSIFY", "depends_on": ["TERRA_LIDAR"]},
    ],
}


@dataclass
class CoordinatorConfig:
    host: str = "0.0.0.0"
    port: int = 8443
    # SQLite file on the coordinator's LOCAL disk — never a network share.
    db_path: str = "data/coordinator.db"

    # Auth. require_agent_tokens=False lets unknown nodes auto-register on
    # first sync (dev / initial bring-up). admin_token empty = admin endpoints
    # open (LAN dev) — set it in production.
    require_agent_tokens: bool = True
    admin_token: str = ""

    # Scheduling / housekeeping knobs (all driven lazily, no background loop).
    lease_minutes: int = 5              # ASSIGNED with no started-report -> back to QUEUED
    max_assign_attempts: int = 3        # lease strikes before NEEDS_ATTENTION
    offline_after_seconds: int = 90     # node considered offline (derived, not stored)
    attention_after_seconds: int = 600  # RUNNING on an offline node this long -> NEEDS_ATTENTION
    missing_job_grace_seconds: int = 120  # RUNNING job absent from agent sync this long -> NEEDS_ATTENTION
    stalled_after_seconds: int = 900    # display-only "stalled" flag on the dashboard

    poll_idle_seconds: int = 10
    poll_busy_seconds: int = 30

    default_max_runtime_minutes: int = 1440
    job_max_runtime_minutes: dict[str, int] = field(default_factory=dict)

    templates: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: dict(DEFAULT_TEMPLATES))

    # Defaults shown by the web intake form (GET /api/v1/intake/options):
    # e.g. {root_path: "Z:/Survey/Projects", classify_models: ["…"],
    #       epsg_h: "6341", epsg_v: "8228"}. Free-form — the form reads what
    #       it knows and ignores the rest.
    intake_defaults: dict[str, Any] = field(default_factory=dict)

    # Server-side file browser for the web form's Browse buttons
    # (GET /api/v1/browse). Each root: label -> {path, display}. `path` is
    # where THIS coordinator process sees the share (e.g. the Docker volume
    # mount); `display` is how agents/jobs address the same location (the UNC
    # path written into job parameters). Empty = Browse buttons hidden.
    #   browse_roots:
    #     3dData:
    #       path: /mnt/3dData
    #       display: \\192.168.35.25\3dData
    #     ingest:                       # the card share, mounted read-only
    #       path: /mnt/ingest
    #       display: /mnt/ingest        # local: only NAS containers read sources
    browse_roots: dict[str, dict[str, str]] = field(default_factory=dict)

    # NAS helper (GET /api/v1/intake/probe): where the coordinator process can
    # read the State Plane shapefile for EPSG auto-detect (its .dbf sibling is
    # read alongside), and the exiftool command for the RTK coverage scan.
    # Empty shapefile path = EPSG fields just come back blank for manual entry.
    stateplane_shapefile: str = ""
    exiftool_path: str = "exiftool"

    # Small-file uploads (POST /api/v1/intake/upload): base data + targets csv
    # the operator drops in the browser land here, on a volume the INTAKE_COPY
    # worker also mounts. Must be writable by the coordinator process.
    upload_dir: str = "data/uploads"
    max_upload_bytes: int = 64 * 1024 * 1024  # 64 MB — base/csv are tiny

    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CoordinatorConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        cfg = cls()
        for key, value in raw.items():
            if hasattr(cfg, key) and value is not None:
                setattr(cfg, key, value)
        cfg.apply_env()
        return cfg

    def apply_env(self) -> None:
        """Environment overrides (set by Docker; win over the YAML so the DB
        always lands on the container volume)."""
        env_admin = os.environ.get("DATA_INTAKE_ADMIN_TOKEN")
        if env_admin:
            self.admin_token = env_admin
        env_db = os.environ.get("DATA_INTAKE_DB_PATH")
        if env_db:
            self.db_path = env_db
        # Uploads must land on the mounted /data volume (not the container's
        # ephemeral /app/data), and at a path the INTAKE_COPY worker also mounts.
        env_uploads = os.environ.get("DATA_INTAKE_UPLOAD_DIR")
        if env_uploads:
            self.upload_dir = env_uploads
        env_host = os.environ.get("DATA_INTAKE_HOST")
        if env_host:
            self.host = env_host
        env_port = os.environ.get("DATA_INTAKE_PORT")
        if env_port:
            self.port = int(env_port)

    def max_runtime_for(self, job_type: str) -> int:
        return int(self.job_max_runtime_minutes.get(job_type, self.default_max_runtime_minutes))


def load_config(path: Optional[str] = None) -> CoordinatorConfig:
    """Load config from `path`, $DATA_INTAKE_COORDINATOR_CONFIG, or defaults."""
    path = path or os.environ.get("DATA_INTAKE_COORDINATOR_CONFIG")
    if path and Path(path).is_file():
        return CoordinatorConfig.from_yaml(path)
    cfg = CoordinatorConfig()
    cfg.apply_env()
    return cfg
