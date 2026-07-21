"""Media eject over a file spool (container -> host watcher -> back).

The coordinator runs in a Docker container; the SD/USB card is mounted on the
NAS *host*. A `umount` inside the container only touches the container's mount
namespace, so it can't actually make the card safe to pull. Instead the
coordinator drops a request file in a shared spool directory and a tiny
host-side watcher (scripts/nas_eject_watcher.py, running as root) does the real
umount and writes back a result. This module is the container half: request
validation and the write-then-poll-for-result handshake.

The container only ever names the DEVICE leaf (e.g. "sda1", a direct child of
the ingest mount) — never a host path. The watcher resolves that against its
own configured USB base, so the container needs no host-path knowledge and
can't ask it to unmount anything outside the card base.
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


def write_request(spool_dir: str, device: str) -> str:
    """Atomically drop an eject request; returns its id."""
    device = validate_device(device)
    spool = Path(spool_dir)
    req_dir = _requests_dir(spool)
    _results_dir(spool).mkdir(parents=True, exist_ok=True)
    req_dir.mkdir(parents=True, exist_ok=True)

    req_id = uuid.uuid4().hex
    payload = {"id": req_id, "device": device, "requested_at": time.time()}
    tmp = req_dir / f"{req_id}.json.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, req_dir / f"{req_id}.json")
    return req_id


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
        message="Eject requested, but the host eject watcher hasn't responded. "
                "Is scripts/nas_eject_watcher.py running on the NAS?")


def eject(spool_dir: str, device: str, timeout: float) -> EjectResult:
    req_id = write_request(spool_dir, device)
    return poll_result(spool_dir, req_id, timeout)
