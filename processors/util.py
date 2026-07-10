"""Helpers shared by the real processors."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from processors.base import JobContext


def missing_params(ctx: JobContext, required: list[str]) -> list[str]:
    return [f"missing job parameter: {name}"
            for name in required if not str(ctx.parameters.get(name, "")).strip()]


def payload_exe(cfg, key: str) -> Optional[Path]:
    """Resolve a payload executable from agent config payload_paths."""
    raw = (getattr(cfg, "payload_paths", {}) or {}).get(key, "")
    return Path(raw) if raw else None


def check_payload_exe(cfg, key: str) -> list[str]:
    exe = payload_exe(cfg, key)
    if exe is None:
        return [f"agent config payload_paths.{key} is not set"]
    if not exe.is_file():
        return [f"payload not found: {exe} (payload_paths.{key})"]
    return []


def tail_last_line(path: Path, max_bytes: int = 16_384) -> str:
    """Last non-empty line of a log file ('' when unavailable) — used as an
    honest stage-only progress message when the app exposes no percentage."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if line:
            return line[:300]
    return ""


def newer_than_start(path: Path, ctx: JobContext, slack_seconds: float = 120.0) -> bool:
    """True when `path` was modified after the job started (with slack for
    clock fuzz). When the start time is unknown (recovered job), pass."""
    if ctx.started_wall is None:
        return True
    try:
        return path.stat().st_mtime >= ctx.started_wall - slack_seconds
    except OSError:
        return False


def files_matching(root: Path, patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    for pattern in patterns:
        out.extend(p for p in root.rglob(pattern) if p.is_file())
    return sorted(set(out))
