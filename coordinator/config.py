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
        env_admin = os.environ.get("DATA_INTAKE_ADMIN_TOKEN")
        if env_admin:
            self.admin_token = env_admin

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
