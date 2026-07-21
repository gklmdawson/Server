"""Windows system-tray mode: the agent parks in the tray, not a console.

The agent used to be a bare console window on the worker's desktop — one
stray click on its X killed the worker mid-shift. In tray mode the process
lives as a tray icon; a small status window (state, current job, last sync,
live log tail) opens from the icon, and CLOSING that window only hides it
back to the tray — the agent keeps running. Exiting is an explicit
tray-menu action.

Threading: the sync loop runs in a background thread, tkinter owns the main
thread, and pystray runs its own message pump thread. pystray menu handlers
never touch tk directly — they enqueue actions the tk tick consumes (tkinter
is not thread-safe). Everything here is optional: agent/main.py falls back
to the plain console loop when --no-tray is passed or these imports fail.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import webbrowser
from typing import Any, Callable, Optional

logger = logging.getLogger("agent.tray")

# Sunrise palette (matches the web UI).
NAVY = "#113e59"
YELLOW = "#ffd457"
GRAY = "#a3a5a8"
GOOD = "#006f67"
RED = "#d2342e"
INK2 = "#4c6e75"

LOG_TAIL_BYTES = 16_384
UI_TICK_MS = 250          # queue pump (tray thread -> tk thread)
STATUS_TICK_MS = 1000     # status labels / tooltip refresh
LOG_TICK_MS = 2000        # log tail refresh


def _icon_image(paused: bool):
    """Navy rounded square with the Sunrise yellow band; gray band = paused."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=NAVY)
    d.rectangle((14, 40, 50, 50), fill=GRAY if paused else YELLOW)
    return img


def _tail(path, max_bytes: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return "(no log output yet)"
    if size > max_bytes:
        data = data[data.find("\n") + 1:]
    return data


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    s = max(0, time.time() - ts)
    return f"{s:.0f}s ago" if s < 90 else f"{s / 60:.0f}m ago"


def state_text(snap: dict[str, Any]) -> tuple[str, str]:
    """One-line agent state + a display color, from Agent.status_snapshot()."""
    job = snap.get("job")
    if job:
        line = f"Running {job.get('job_type') or 'job'} {job.get('uuid', '')[:8]}"
        if job.get("percent") is not None:
            line += f" — {job['percent']:.0f}%"
        if job.get("message"):
            line += f" — {job['message']}"
        return line, GOOD
    if snap.get("paused"):
        return "Paused — not taking new jobs (resume from the tray menu)", RED
    if snap.get("preflight"):
        return "Paused by preflight: " + "; ".join(snap["preflight"]), RED
    return "Idle — accepting jobs", INK2


def sync_text(snap: dict[str, Any]) -> tuple[str, str]:
    if snap.get("last_sync_ok") is None:
        return "Connecting to the coordinator…", INK2
    if snap["last_sync_ok"]:
        return f"Last sync OK {_ago(snap.get('last_sync_at'))}", GOOD
    return (f"Sync failing ({snap.get('last_sync_error') or 'unknown'}) — "
            "retrying with backoff", RED)


def run_with_tray(agent, cfg) -> bool:
    """Run the agent under a tray icon + status window; blocks until exit.
    Returns False WITHOUT starting the loop when the tray stack is
    unavailable, so the caller can fall back to the console loop."""
    try:
        import pystray
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        logger.warning("System tray unavailable (%s) — console mode", exc)
        return False

    ui_calls: "queue.Queue[Callable[[], None]]" = queue.Queue()

    # --- agent loop in the background -------------------------------------
    worker_crashed = threading.Event()

    def loop() -> None:
        try:
            agent.run()
        except BaseException:
            logger.exception("Agent loop crashed")
            worker_crashed.set()

    worker = threading.Thread(target=loop, name="agent-loop", daemon=True)

    # --- status window ------------------------------------------------------
    root = tk.Tk()
    root.title(f"Data Intake Agent — {cfg.node_name}")
    root.geometry("760x460")
    root.minsize(560, 320)

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)

    head = ttk.Label(frm, text=f"{cfg.node_name}  ·  {cfg.coordinator_url}",
                     font=("Segoe UI", 11, "bold"), foreground=NAVY)
    head.pack(anchor="w")
    caps = ", ".join(cfg.capabilities) or "(none declared)"
    ttk.Label(frm, text=f"Capabilities: {caps}",
              foreground=INK2).pack(anchor="w", pady=(2, 6))

    state_var = tk.StringVar(value="Starting…")
    state_lbl = ttk.Label(frm, textvariable=state_var, foreground=INK2,
                          wraplength=700)
    state_lbl.pack(anchor="w")
    sync_var = tk.StringVar(value="")
    sync_lbl = ttk.Label(frm, textvariable=sync_var, foreground=INK2)
    sync_lbl.pack(anchor="w", pady=(0, 8))

    log_box = tk.Text(frm, height=14, state="disabled", wrap="none",
                      font=("Consolas", 9), background="#0c1922",
                      foreground="#c4cdd3", relief="flat", padx=8, pady=6)
    log_box.pack(fill="both", expand=True)

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(10, 0))

    def open_dashboard() -> None:
        webbrowser.open(cfg.coordinator_url)

    def open_logs() -> None:
        try:
            os.startfile(str(cfg.logs_dir))  # type: ignore[attr-defined]
        except OSError:
            pass

    pause_btn_var = tk.StringVar(value="Pause new jobs")

    def toggle_pause() -> None:
        agent.paused = not agent.paused
        agent.wake_event.set()  # sync soon so the dashboard shows it
        refresh_pause_ui()

    ttk.Button(btns, text="Open dashboard", command=open_dashboard).pack(
        side="left", padx=(0, 6))
    ttk.Button(btns, text="Open logs folder", command=open_logs).pack(
        side="left", padx=(0, 6))
    ttk.Button(btns, textvariable=pause_btn_var, command=toggle_pause).pack(
        side="left", padx=(0, 6))
    ttk.Button(btns, text="Hide to tray", command=lambda: root.withdraw()).pack(
        side="right")

    # Closing the window hides it — it never stops the agent.
    root.protocol("WM_DELETE_WINDOW", root.withdraw)

    def show_window() -> None:
        root.deiconify()
        root.lift()
        try:
            root.focus_force()
        except tk.TclError:
            pass

    # --- tray icon ----------------------------------------------------------
    exiting = threading.Event()

    def do_exit(icon=None, item=None) -> None:
        # pystray thread: hand everything to the tk thread via the queue.
        exiting.set()
        ui_calls.put(root.destroy)

    tray = pystray.Icon(
        "data-intake-agent",
        _icon_image(False),
        f"Data Intake Agent — {cfg.node_name}",
        menu=pystray.Menu(
            pystray.MenuItem("Open status window",
                             lambda icon, item: ui_calls.put(show_window),
                             default=True),
            pystray.MenuItem("Open dashboard",
                             lambda icon, item: open_dashboard()),
            pystray.MenuItem("Open logs folder",
                             lambda icon, item: open_logs()),
            pystray.MenuItem("Sync now",
                             lambda icon, item: agent.wake_event.set()),
            pystray.MenuItem("Pause new jobs",
                             lambda icon, item: ui_calls.put(toggle_pause),
                             checked=lambda item: bool(agent.paused)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit agent", do_exit),
        ),
    )

    def refresh_pause_ui() -> None:
        pause_btn_var.set("Resume jobs" if agent.paused else "Pause new jobs")
        tray.icon = _icon_image(agent.paused)
        tray.update_menu()

    # --- periodic refresh (tk thread) --------------------------------------
    def pump_ui_calls() -> None:
        try:
            while True:
                ui_calls.get_nowait()()
        except queue.Empty:
            pass
        if not exiting.is_set():
            root.after(UI_TICK_MS, pump_ui_calls)

    def refresh_status() -> None:
        if exiting.is_set():
            return
        if not worker.is_alive():
            # Loop ended: crashed (exit 1 so Task Scheduler restarts us) or
            # stopped on request; either way bring the whole app down.
            do_exit()
            return
        snap = agent.status_snapshot()
        text, color = state_text(snap)
        state_var.set(text)
        state_lbl.configure(foreground=color)
        text, color = sync_text(snap)
        sync_var.set(text)
        sync_lbl.configure(foreground=color)
        tray.title = f"Data Intake Agent — {cfg.node_name}: {state_var.get()}"[:127]
        root.after(STATUS_TICK_MS, refresh_status)

    last_log = [""]

    def refresh_log() -> None:
        if exiting.is_set():
            return
        text = _tail(cfg.logs_dir / "agent.log")
        if text != last_log[0]:
            last_log[0] = text
            log_box.configure(state="normal")
            log_box.delete("1.0", "end")
            log_box.insert("1.0", text)
            log_box.configure(state="disabled")
            log_box.see("end")
        root.after(LOG_TICK_MS, refresh_log)

    # --- go -----------------------------------------------------------------
    worker.start()
    threading.Thread(target=tray.run, name="tray-icon", daemon=True).start()

    root.withdraw()  # start minimized to the tray
    try:
        tray.notify("Running in the background — right-click the tray icon "
                    "for status and options.", f"Data Intake Agent — {cfg.node_name}")
    except Exception:
        pass

    root.after(UI_TICK_MS, pump_ui_calls)
    root.after(STATUS_TICK_MS, refresh_status)
    root.after(LOG_TICK_MS, refresh_log)
    try:
        root.mainloop()
    finally:
        agent.request_stop()
        try:
            tray.stop()
        except Exception:
            pass
        worker.join(timeout=10)
    if worker_crashed.is_set():
        raise SystemExit(1)  # non-zero so Task Scheduler's restart kicks in
    return True
