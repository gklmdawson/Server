"""Machine/desktop preflight checks.

GUI-automation payloads click at pixel offsets calibrated for a specific
desktop: 150% DPI, a known resolution, and an unlocked interactive session.
These checks run BEFORE the agent requests work — while any of them fail the
agent syncs with accepting_jobs=false (visible on the dashboard with the
reasons in telemetry) instead of taking a job it would butcher.

Everything Windows-specific is behind sys.platform guards so the agent logic
stays testable on any OS.
"""
from __future__ import annotations

import ctypes
import sys
from typing import Optional

import psutil

if sys.platform == "win32":
    # Must happen before any GetDpiForSystem()/GetSystemMetrics() call below —
    # a DPI-unaware process is virtualized to 96 (100%) by Windows regardless
    # of the real display scaling. The automation payloads (DJIAutomatePPK.py
    # etc.) get this for free via pywinauto's import-time side effect; the
    # agent never imports pywinauto, so it has to declare it itself.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def desktop_errors(expected_resolution: list[int], require_dpi_150: bool) -> list[str]:
    """Errors preventing GUI automation right now (empty = good to go)."""
    if sys.platform != "win32":
        return []
    errors: list[str] = []

    # Session must be unlocked for foreground clicks to land.
    DESKTOP_SWITCHDESKTOP = 0x0100
    hdesk = ctypes.windll.user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if hdesk:
        ctypes.windll.user32.CloseDesktop(hdesk)
    else:
        errors.append("desktop locked or not interactive")

    if require_dpi_150:
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            if dpi != 144:
                errors.append(f"DPI is {round(dpi / 96 * 100)}% (need 150%)")
        except Exception:
            errors.append("could not read system DPI")

    if expected_resolution:
        width = ctypes.windll.user32.GetSystemMetrics(0)
        height = ctypes.windll.user32.GetSystemMetrics(1)
        if [width, height] != list(expected_resolution):
            errors.append(
                f"resolution {width}x{height} (expected "
                f"{expected_resolution[0]}x{expected_resolution[1]})"
            )
    return errors


def process_running(image_names: list[str]) -> Optional[str]:
    """Return the first of `image_names` found running (case-insensitive),
    e.g. to keep Cyclone jobs queued while a person has 3DR.exe open."""
    wanted = {n.lower() for n in image_names}
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in wanted:
            return name
    return None


def basic_telemetry(work_root: str) -> dict:
    vm = psutil.virtual_memory()
    out = {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "memory_percent": vm.percent,
        "memory_total_gb": round(vm.total / 1024**3, 1),
        "platform": sys.platform,
    }
    try:
        du = psutil.disk_usage(work_root)
        out["work_disk_free_gb"] = round(du.free / 1024**3, 1)
    except Exception:
        pass
    return out
