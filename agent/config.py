"""Agent configuration (YAML next to the EXE; see config/agent.example.yaml).

The node token comes from an environment variable by default so it never has
to sit in the YAML file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class AgentConfig:
    node_name: str = ""
    coordinator_url: str = "http://127.0.0.1:8443"
    token: str = ""                       # discouraged; prefer token_env
    token_env: str = "DATA_INTAKE_NODE_TOKEN"
    capabilities: list[str] = field(default_factory=list)
    work_root: str = "C:/ProgramData/DataIntakeAgent"
    request_timeout_seconds: float = 15.0
    # Desktop preflight for GUI processors: [] disables the resolution check.
    expected_resolution: list[int] = field(default_factory=list)
    require_dpi_150: bool = True
    # Payload locations etc., read by processors (keys are processor-defined).
    payload_paths: dict[str, str] = field(default_factory=dict)
    keep_job_dirs_days: int = 7
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        cfg = cls()
        for key, value in raw.items():
            if hasattr(cfg, key) and value is not None:
                setattr(cfg, key, value)
        cfg.resolve_token()
        return cfg

    def resolve_token(self) -> None:
        if not self.token and self.token_env:
            self.token = os.environ.get(self.token_env, "")

    # --- derived paths -----------------------------------------------------

    @property
    def work_root_path(self) -> Path:
        return Path(self.work_root)

    @property
    def state_file(self) -> Path:
        return self.work_root_path / "state" / "current_job.json"

    @property
    def jobs_dir(self) -> Path:
        return self.work_root_path / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.work_root_path / "logs"

    def ensure_dirs(self) -> None:
        for p in (self.state_file.parent, self.jobs_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_config(path: Optional[str] = None) -> AgentConfig:
    """Load from `path`, $DATA_INTAKE_AGENT_CONFIG, or agent.yaml next to the
    executable / current directory."""
    candidates = [path, os.environ.get("DATA_INTAKE_AGENT_CONFIG"),
                  "agent.yaml", "config/agent.yaml"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return AgentConfig.from_yaml(candidate)
    raise FileNotFoundError(
        "No agent config found (looked for agent.yaml; pass --config or set "
        "DATA_INTAKE_AGENT_CONFIG)"
    )
