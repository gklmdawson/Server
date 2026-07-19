"""Agent setup window (`DataIntakeAgent.exe --setup`).

A tiny local helper so an operator can point the box at the coordinator and
paste its node token WITHOUT editing YAML or juggling environment variables —
the pain that env-var/setx setup created. It writes the values to the agent's
local settings file (config.save_local_settings) and can run a live status
check against the coordinator so the operator sees green/red immediately.

This is a *setup/status* tool only — job monitoring stays in the browser
dashboard. Tkinter (stdlib, bundled by PyInstaller) keeps it dependency-free;
it is imported lazily so this module stays importable on headless/CI machines.
"""
from __future__ import annotations

from typing import Optional

from agent.config import AgentConfig


def check_connection(coordinator_url: str, node_name: str, token: str,
                     capabilities: Optional[list[str]] = None,
                     timeout: float = 8.0) -> tuple[bool, str]:
    """Do a real (accepting_jobs=false) sync and report the outcome as
    (ok, message) — so the window and tests share one code path.

    accepting_jobs is false so this probe never pulls a job; capabilities are
    sent as declared so the probe does not blank the node's capability list."""
    from agent import __version__ as AGENT_VERSION
    from agent.client import CoordinatorClient, ReportConflict
    from shared.schemas import SyncRequest

    url = (coordinator_url or "").strip()
    node = (node_name or "").strip()
    tok = (token or "").strip()
    if not url:
        return False, "Enter the coordinator URL (e.g. http://192.168.35.25:8443)."
    if not node:
        return False, "Enter the node name (must match how it was provisioned)."
    if not tok:
        return False, "Enter the node token."

    client = CoordinatorClient(url, tok, timeout=timeout)
    try:
        req = SyncRequest(agent_version=AGENT_VERSION,
                          capabilities=capabilities or [],
                          accepting_jobs=False)
        resp = client.sync(node, req)
        state = "enabled" if resp.enabled else "disabled on the dashboard"
        return True, f"Connected — '{node}' is registered ({state})."
    except ReportConflict as exc:
        if exc.status_code == 401:
            return False, ("Token rejected (401). Check the token is current "
                           "and the node name matches what was provisioned.")
        return False, f"Coordinator refused the sync: {exc.detail}"
    except Exception as exc:  # network / DNS / TLS
        return False, f"Cannot reach {url}: {exc}"
    finally:
        client.close()


def run_setup(cfg: AgentConfig, config_path: Optional[str] = None) -> None:
    """Open the setup window, prefilled from `cfg`. Blocks until closed."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Data Intake Agent — Setup")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    pad = {"padx": 10, "pady": 6}
    frm = ttk.Frame(root, padding=14)
    frm.grid(sticky="nsew")

    ttk.Label(frm, text="Coordinator URL").grid(row=0, column=0, sticky="w", **pad)
    url_var = tk.StringVar(value=cfg.coordinator_url or "http://192.168.35.25:8443")
    ttk.Entry(frm, textvariable=url_var, width=48).grid(row=0, column=1, **pad)

    ttk.Label(frm, text="Node name").grid(row=1, column=0, sticky="w", **pad)
    node_var = tk.StringVar(value=cfg.node_name)
    ttk.Entry(frm, textvariable=node_var, width=48).grid(row=1, column=1, **pad)

    ttk.Label(frm, text="Node token").grid(row=2, column=0, sticky="w", **pad)
    token_var = tk.StringVar(value=cfg.token)
    ttk.Entry(frm, textvariable=token_var, width=48).grid(row=2, column=1, **pad)

    caps = ", ".join(cfg.capabilities) or "(none declared)"
    ttk.Label(frm, text=f"Capabilities: {caps}", foreground="#4c6e75").grid(
        row=3, column=0, columnspan=2, sticky="w", padx=10)

    status = tk.StringVar(value="Enter the details, then Test or Save.")
    status_lbl = ttk.Label(frm, textvariable=status, wraplength=430,
                           foreground="#4c6e75")
    status_lbl.grid(row=4, column=0, columnspan=2, sticky="w", **pad)

    def set_status(ok: Optional[bool], msg: str) -> None:
        status.set(msg)
        status_lbl.configure(
            foreground={True: "#006f67", False: "#d2342e"}.get(ok, "#4c6e75"))
        root.update_idletasks()

    def do_test() -> None:
        set_status(None, "Testing…")
        ok, msg = check_connection(url_var.get(), node_var.get(),
                                   token_var.get(), cfg.capabilities)
        set_status(ok, msg)

    def do_save() -> None:
        cfg.save_local_settings(url_var.get(), node_var.get(), token_var.get())
        set_status(None, f"Saved to {cfg.settings_file}")

    def do_save_test() -> None:
        do_save()
        do_test()

    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=2, sticky="e", padx=6, pady=4)
    ttk.Button(btns, text="Test connection", command=do_test).grid(row=0, column=0, padx=4)
    ttk.Button(btns, text="Save", command=do_save).grid(row=0, column=1, padx=4)
    ttk.Button(btns, text="Save & Test", command=do_save_test).grid(row=0, column=2, padx=4)
    ttk.Button(btns, text="Close", command=root.destroy).grid(row=0, column=3, padx=4)

    root.mainloop()
