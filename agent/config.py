"""Agent configuration (YAML next to the EXE; see config/agent.example.yaml).

The node token comes from an environment variable by default so it never has
to sit in the YAML file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class AgentConfig:
    node_name: str = ""
    coordinator_url: str = "http://127.0.0.1:8443"
    token: str = ""                       # discouraged; prefer token_file/env
    # The node token is resolved in this order: explicit `token` above, then
    # `token_file` (written by the --setup window), then the `token_env` var.
    token_file: str = ""                  # default: <work_root>/node_token
    token_env: str = "DATA_INTAKE_NODE_TOKEN"
    capabilities: list[str] = field(default_factory=list)
    work_root: str = "C:/ProgramData/DataIntakeAgent"
    request_timeout_seconds: float = 15.0
    # Desktop preflight for GUI processors: [] disables the resolution check.
    expected_resolution: list[int] = field(default_factory=list)
    require_dpi_150: bool = True
    # Payload locations etc., read by processors (keys are processor-defined).
    payload_paths: dict[str, str] = field(default_factory=dict)
    # Rewrite job-parameter paths from their canonical (UNC) form to this
    # machine's local view — how the NAS-local INTAKE_COPY worker turns
    # \\NAS\3dData into /mnt/3dData. Longest matching prefix wins;
    # case-insensitive; slashes normalized. Empty (Windows agents) = no-op.
    path_map: dict[str, str] = field(default_factory=dict)
    keep_job_dirs_days: int = 7
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        cfg = cls()
        for key, value in raw.items():
            if hasattr(cfg, key) and value is not None:
                setattr(cfg, key, value)
        cfg.apply_env()
        return cfg

    def apply_env(self) -> None:
        """Environment overrides — how the container is configured without a
        baked YAML file. Env wins over YAML so one image serves any node."""
        env = os.environ
        if v := env.get("DATA_INTAKE_NODE_NAME"):
            self.node_name = v
        if v := env.get("DATA_INTAKE_COORDINATOR_URL"):
            self.coordinator_url = v
        if v := env.get("DATA_INTAKE_CAPABILITIES"):
            self.capabilities = [c.strip() for c in v.split(",") if c.strip()]
        if v := env.get("DATA_INTAKE_WORK_ROOT"):
            self.work_root = v
        if v := env.get("DATA_INTAKE_PATH_MAP"):
            try:
                self.path_map = json.loads(v)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"DATA_INTAKE_PATH_MAP is not valid JSON: {exc}")
        if v := env.get("DATA_INTAKE_LOG_LEVEL"):
            self.log_level = v
        self.resolve_token()

    def resolve_token(self) -> None:
        """Fill self.token from token_file then token_env if not already set."""
        if not self.token:
            path = self.token_file or str(self.work_root_path / "node_token")
            try:
                self.token = Path(path).read_text(encoding="utf-8").strip()
            except OSError:
                pass
        if not self.token and self.token_env:
            self.token = os.environ.get(self.token_env, "")

    # --- UI-managed local settings (the --setup window) ---------------------

    @property
    def settings_file(self) -> Path:
        """Where the --setup window persists coordinator_url / node_name / token.
        Lives in the work root so a non-admin user owns it."""
        return self.work_root_path / "agent_setup.json"

    def apply_local_settings(self) -> None:
        """Overlay values the operator saved in the --setup window. These win
        over the YAML (the operator set them at runtime); env still wins over
        these, and containers never have this file."""
        p = self.settings_file
        if not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("coordinator_url"):
            self.coordinator_url = str(data["coordinator_url"]).strip()
        if data.get("node_name"):
            self.node_name = str(data["node_name"]).strip()
        if data.get("token"):
            self.token = str(data["token"]).strip()

    def save_local_settings(self, coordinator_url: str, node_name: str,
                            token: str) -> Path:
        """Persist the setup fields to settings_file (creating the work root)."""
        self.work_root_path.mkdir(parents=True, exist_ok=True)
        payload = {
            "coordinator_url": coordinator_url.strip(),
            "node_name": node_name.strip(),
            "token": token.strip(),
        }
        self.settings_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Reflect immediately in this instance.
        self.coordinator_url = payload["coordinator_url"] or self.coordinator_url
        self.node_name = payload["node_name"] or self.node_name
        self.token = payload["token"] or self.token
        return self.settings_file

    # --- path translation ---------------------------------------------------

    def translate_path(self, p: str) -> str:
        """Rewrite `p` through path_map (longest prefix, case-insensitive).
        Returns `p` unchanged when nothing matches or the map is empty."""
        if not p or not self.path_map:
            return p
        norm = p.replace("\\", "/")
        low = norm.lower()
        for src in sorted(self.path_map, key=len, reverse=True):
            s = src.replace("\\", "/").rstrip("/")
            sl = s.lower()
            if low == sl or low.startswith(sl + "/"):
                rest = norm[len(s):].lstrip("/")
                dst = self.path_map[src]
                return os.path.join(dst, *rest.split("/")) if rest else dst
        return p

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
    executable / current directory. With none of those present, fall back to a
    fully environment-driven config (how the container runs — no YAML baked in);
    DATA_INTAKE_NODE_NAME et al. must then be set."""
    candidates = [path, os.environ.get("DATA_INTAKE_AGENT_CONFIG"),
                  "agent.yaml", "config/agent.yaml"]
    cfg = None
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            cfg = AgentConfig.from_yaml(candidate)
            break
    if cfg is None:
        cfg = AgentConfig()
        cfg.apply_env()
    # Overlay what the operator saved in the --setup window, then (re)resolve
    # the token so token_file / token_env still fill in when unset.
    cfg.apply_local_settings()
    cfg.resolve_token()
    return cfg
