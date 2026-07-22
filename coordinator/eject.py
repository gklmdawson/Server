"""Host actions over a file spool (container -> host watcher -> back).

The coordinator runs in a Docker container; some things only the NAS *host*
can do. A `umount` inside the container only touches the container's mount
namespace, so it can't actually make a card safe to pull — and the container
obviously can't `docker restart` itself. Instead the coordinator drops a
request file in a shared spool directory and a tiny host-side watcher
(scripts/nas_eject_watcher.py, running as root) does the real work and writes
back a result. This module is the container half: request validation and the
write-then-poll-for-result handshake.

Two actions:
  * eject:   safely unmount one card. The container only ever names the
             DEVICE leaf (e.g. "sda1") — never a host path. The watcher
             resolves that against its own configured USB base, so the
             container can't ask it to unmount anything outside the card base.
  * restart: restart the data-intake containers (the fix for a hot-plugged
             card that never propagated into the container's mount
             namespace). The request carries no arguments — the command run
             is fixed on the host side (the watcher's --restart-cmd), so the
             container can't ask the host to run anything else.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class EjectError(Exception):
    """Bad request (caller's fault) — maps to HTTP 400."""


@dataclass
class EjectResult:
    ok: bool
    message: str
    pending: bool = False   # request written but the watcher hadn't answered yet


def validate_device(device: str) -> str:
    """A device is a single path segment (the card's mount leaf). Reject
    anything that could escape the card base on the host side."""
    d = (device or "").strip()
    if not d or "/" in d or "\\" in d or d in (".", "..") or "\x00" in d:
        raise EjectError(f"invalid device name: {device!r}")
    return d


def _requests_dir(spool: Path) -> Path:
    return spool / "requests"


def _results_dir(spool: Path) -> Path:
    return spool / "results"


def _write_spool_request(spool_dir: str, payload: dict) -> str:
    """Atomically drop a request file; returns its id."""
    spool = Path(spool_dir)
    req_dir = _requests_dir(spool)
    _results_dir(spool).mkdir(parents=True, exist_ok=True)
    req_dir.mkdir(parents=True, exist_ok=True)

    req_id = uuid.uuid4().hex
    payload = {"id": req_id, "requested_at": time.time(), **payload}
    tmp = req_dir / f"{req_id}.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, req_dir / f"{req_id}.json")
    return req_id


def write_request(spool_dir: str, device: str) -> str:
    """Drop an eject request; returns its id."""
    device = validate_device(device)
    return _write_spool_request(spool_dir, {"action": "eject", "device": device})


def write_restart_request(spool_dir: str) -> str:
    """Drop a container-restart request; returns its id. No arguments cross
    the boundary — the watcher's --restart-cmd decides what actually runs."""
    return _write_spool_request(spool_dir, {"action": "restart"})


def poll_result(spool_dir: str, req_id: str, timeout: float,
                interval: float = 0.25) -> EjectResult:
    """Wait up to `timeout` for the watcher's result file, consuming it.
    Returns pending=True if the watcher hasn't answered in time (the request
    stays queued — the watcher will still process it when it comes up)."""
    result_path = _results_dir(Path(spool_dir)) / f"{req_id}.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(interval)
            continue
        try:
            result_path.unlink()
        except OSError:
            pass
        return EjectResult(ok=bool(data.get("ok")),
                           message=str(data.get("message", "")))
    return EjectResult(
        ok=False, pending=True,
        message="Request spooled, but the host watcher hasn't responded. "
                "Is scripts/nas_eject_watcher.py running on the NAS?")


def eject(spool_dir: str, device: str, timeout: float) -> EjectResult:
    req_id = write_request(spool_dir, device)
    return poll_result(spool_dir, req_id, timeout)


def restart(spool_dir: str, timeout: float) -> EjectResult:
    """Ask the host to restart the data-intake containers. The watcher
    acknowledges BEFORE running the command (so this response can still reach
    the browser), then restarts a beat later."""
    req_id = write_restart_request(spool_dir)
    return poll_result(spool_dir, req_id, timeout)
