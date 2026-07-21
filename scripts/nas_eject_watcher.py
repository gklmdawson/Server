#!/usr/bin/env python3
"""Host-side media-eject watcher for the Data Intake coordinator.

The coordinator runs in a Docker container and can't unmount the NAS host's
SD/USB cards itself (a umount inside the container only affects the container's
mount namespace). So the web UI's Eject button spools a request file; THIS
script — running on the NAS host as root — does the real umount and writes the
result back. See DEPLOY.md "Media eject".

Protocol (a shared spool dir both the container and host see, e.g. a subdir of
the coordinator's ./data volume):

    <spool>/requests/<id>.json   {"id","device","requested_at"}   (container writes)
    <spool>/results/<id>.json    {"id","ok","message"}            (this writes back)

`device` is a single path segment (e.g. "sda1"); this script resolves it under
--usb-base and refuses anything that isn't an actual mountpoint beneath it, so
the container can never ask it to unmount something off the card base.

Run it as a systemd service (scripts/data-intake-eject.service). Stdlib only —
no pip installs on the NAS.

    sudo python3 nas_eject_watcher.py \
        --spool /volume1/docker/data-intake/data/eject \
        --usb-base /mnt/@usb
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [eject-watcher] {msg}", flush=True)


def is_mountpoint(path: Path) -> bool:
    try:
        return path.is_mount()
    except OSError:
        return False


def valid_device(device: str) -> bool:
    return bool(device) and "/" not in device and "\\" not in device \
        and device not in (".", "..") and "\x00" not in device


def do_umount(target: Path, power_off: bool) -> tuple[bool, str]:
    """umount the card; optionally spin the whole disk down. Returns (ok, msg)."""
    if not is_mountpoint(target):
        # Already gone (or never mounted here) — treat as success so a
        # double-click doesn't look like a failure.
        return True, f"{target.name} is not mounted — safe to remove."

    proc = subprocess.run(["umount", str(target)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "busy" in err.lower():
            return False, (f"{target.name} is busy — another process on the NAS "
                           "still has it open. Close it and try again.")
        return False, f"umount failed: {err or f'exit {proc.returncode}'}"

    msg = f"{target.name} unmounted — safe to remove."
    if power_off:
        # Best-effort spin-down of the backing device (udisks, if present).
        try:
            subprocess.run(["udisksctl", "power-off", "-b", str(target)],
                           capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    return True, msg


def handle(req_path: Path, results_dir: Path, usb_base: Path,
           power_off: bool) -> None:
    try:
        data = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"skipping unreadable request {req_path.name}: {exc}")
        req_path.unlink(missing_ok=True)
        return

    req_id = str(data.get("id") or req_path.stem)
    device = str(data.get("device") or "")

    if not valid_device(device):
        ok, msg = False, f"invalid device name: {device!r}"
    else:
        target = (usb_base / device).resolve()
        # Jail: the resolved target must sit directly under usb_base.
        if target.parent != usb_base.resolve():
            ok, msg = False, f"device {device!r} is not under {usb_base}"
        else:
            ok, msg = do_umount(target, power_off)

    log(f"{'OK' if ok else 'FAIL'} device={device}: {msg}")
    result = {"id": req_id, "ok": ok, "message": msg}
    tmp = results_dir / f"{req_id}.json.tmp"
    tmp.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp, results_dir / f"{req_id}.json")
    req_path.unlink(missing_ok=True)


def sweep_stale_results(results_dir: Path, max_age: float = 300.0) -> None:
    """Drop result files the container never picked up (it crashed / timed out)
    so they don't accumulate."""
    now = time.time()
    for r in results_dir.glob("*.json"):
        try:
            if now - r.stat().st_mtime > max_age:
                r.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Data Intake host eject watcher")
    ap.add_argument("--spool", required=True,
                    help="shared spool dir (matches coordinator eject_spool_dir "
                         "on the host, e.g. /volume1/docker/data-intake/data/eject)")
    ap.add_argument("--usb-base", default="/mnt/@usb",
                    help="where the NAS mounts cards (default: /mnt/@usb)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between spool scans (default: 1)")
    ap.add_argument("--power-off", action="store_true",
                    help="also udisksctl power-off the device after umount")
    args = ap.parse_args()

    spool = Path(args.spool)
    usb_base = Path(args.usb_base)
    requests_dir = spool / "requests"
    results_dir = spool / "results"
    requests_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    if os.name == "posix" and os.geteuid() != 0:
        log("WARNING: not running as root — umount will likely fail")

    log(f"watching {requests_dir} (usb base {usb_base})")
    while True:
        try:
            for req in sorted(requests_dir.glob("*.json")):
                handle(req, results_dir, usb_base, args.power_off)
            sweep_stale_results(results_dir)
        except Exception as exc:  # never let the loop die
            log(f"loop error: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
