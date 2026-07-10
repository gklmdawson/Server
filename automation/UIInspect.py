import ctypes
import threading
import time
import tkinter as tk

import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation, tagPOINT

_uia = comtypes.client.CreateObject(CUIAutomation._reg_clsid_, interface=IUIAutomation)

CTRL_NAMES = {
    50000: "Button",      50002: "CheckBox",   50003: "ComboBox",
    50004: "Edit",        50007: "ListItem",   50008: "List",
    50009: "Menu",        50011: "MenuItem",   50013: "RadioButton",
    50018: "Tab",         50019: "TabItem",    50020: "Text",
    50021: "ToolBar",     50023: "Tree",       50024: "TreeItem",
    50025: "Custom",      50026: "Group",      50032: "Window",
    50033: "Pane",        50037: "TitleBar",
}

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _drill_down(elem, x, y, depth=0):
    """Recurse into UIA children to find the deepest element containing (x, y)."""
    if depth > 12:
        return elem
    walker = _uia.RawViewWalker
    try:
        child = walker.GetFirstChildElement(elem)
    except Exception:
        return elem
    while child is not None:
        try:
            r = child.CurrentBoundingRectangle
            if r.left <= x <= r.right and r.top <= y <= r.bottom:
                return _drill_down(child, x, y, depth + 1)
        except Exception:
            pass
        try:
            child = walker.GetNextSiblingElement(child)
        except Exception:
            break
    return elem


def _window_title_for_pid(pid):
    """Return the visible top-level window title for a given PID."""
    result = ctypes.create_unicode_buffer(256)
    found  = [None]

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)

    def _cb(hwnd, _):
        p = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and ctypes.windll.user32.IsWindowVisible(hwnd):
            ctypes.windll.user32.GetWindowTextW(hwnd, result, 256)
            if result.value:
                found[0] = result.value
                return False
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] or f"PID {pid}"


def element_at_cursor():
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    try:
        com_pt = tagPOINT()
        com_pt.x, com_pt.y = pt.x, pt.y
        elem = _uia.ElementFromPoint(com_pt)
        elem = _drill_down(elem, pt.x, pt.y)
        name    = elem.CurrentName or ""
        ctrl    = CTRL_NAMES.get(elem.CurrentControlType, f"type:{elem.CurrentControlType}")
        cls     = elem.CurrentClassName or ""
        auto_id = elem.CurrentAutomationId or ""
        pid     = elem.CurrentProcessId
        return pt.x, pt.y, name, ctrl, cls, auto_id, pid
    except Exception:
        return pt.x, pt.y, None, None, None, None, None


# ── State ─────────────────────────────────────────────────────────
_running  = threading.Event()
_picking  = threading.Event()
_last_key = [None]
_target   = {"pid": None, "title": "All Windows"}
_gui_update_cb  = [None]   # refreshes target label
_gui_start_cb   = [None]   # triggers Start from keyboard
_gui_pause_cb   = [None]   # triggers Pause from keyboard

VK_BRACKET_OPEN  = 0xDB   # [
VK_BRACKET_CLOSE = 0xDD   # ]
_prev_keys = {"start": False, "stop": False}


def _loop():
    while True:
        # ── Keyboard shortcuts: [ = start, ] = stop ───────────────
        open_down  = bool(ctypes.windll.user32.GetAsyncKeyState(VK_BRACKET_OPEN)  & 0x8000)
        close_down = bool(ctypes.windll.user32.GetAsyncKeyState(VK_BRACKET_CLOSE) & 0x8000)

        if open_down and not _prev_keys["start"] and not _running.is_set():
            if _gui_start_cb[0]:
                _gui_start_cb[0]()
        if close_down and not _prev_keys["stop"] and _running.is_set():
            if _gui_pause_cb[0]:
                _gui_pause_cb[0]()

        _prev_keys["start"] = open_down
        _prev_keys["stop"]  = close_down

        if _running.is_set() or _picking.is_set():
            x, y, name, ctrl, cls, auto_id, pid = element_at_cursor()

            # Picking mode: left mouse button confirms the target window
            if _picking.is_set() and pid is not None:
                if ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000:
                    _target["pid"]   = pid
                    _target["title"] = _window_title_for_pid(pid)
                    _picking.clear()
                    _last_key[0] = None
                    if _gui_update_cb[0]:
                        _gui_update_cb[0]()

            # Normal inspection
            if _running.is_set() and name is not None:
                if _target["pid"] is None or pid == _target["pid"]:
                    key = (name, ctrl, cls, auto_id)
                    if key != _last_key[0]:
                        _last_key[0] = key
                        parts = [f"({x},{y})"]
                        if name:    parts.append(f'name="{name}"')
                        if ctrl:    parts.append(f"ctrl={ctrl}")
                        if cls:     parts.append(f"class={cls}")
                        if auto_id: parts.append(f"auto_id={auto_id}")
                        print("  ".join(parts))
        time.sleep(0.25)


# ── Always-on-top helpers ─────────────────────────────────────────
HWND_TOPMOST    = -1
HWND_NOTOPMOST  = -2
SWP_NOMOVE_SIZE = 0x0003

_console_hwnd = ctypes.windll.kernel32.GetConsoleWindow()

def _set_topmost(hwnd, on: bool):
    flag = HWND_TOPMOST if on else HWND_NOTOPMOST
    ctypes.windll.user32.SetWindowPos(hwnd, flag, 0, 0, 0, 0, SWP_NOMOVE_SIZE)


# ── GUI ───────────────────────────────────────────────────────────
def main():
    root = tk.Tk()
    root.title("UI Inspect")
    root.resizable(False, False)

    _on_top = [True]

    def _apply_topmost():
        on = _on_top[0]
        root.attributes("-topmost", on)
        _set_topmost(_console_hwnd, on)
        btn_top.config(text="On Top: ON" if on else "On Top: OFF",
                       relief=tk.SUNKEN if on else tk.RAISED)

    root.attributes("-topmost", True)
    _set_topmost(_console_hwnd, True)

    status     = tk.StringVar(value="Paused")
    target_lbl = tk.StringVar(value="Target: All Windows")

    def _refresh_target_label():
        t = _target["title"]
        short = t if len(t) <= 22 else t[:20] + ".."
        target_lbl.set(f"Target: {short}")
        if _picking.is_set():
            status.set("Click target window...")
        elif _running.is_set():
            status.set("Running")

    _gui_update_cb[0] = lambda: root.after(0, _refresh_target_label)

    def start():
        _running.set()
        status.set("Running")
        btn_start.config(state=tk.DISABLED)
        btn_pause.config(state=tk.NORMAL)

    def pause():
        _running.clear()
        _picking.clear()
        status.set("Paused")
        btn_start.config(state=tk.NORMAL)
        btn_pause.config(state=tk.DISABLED)

    _gui_start_cb[0] = lambda: root.after(0, start)
    _gui_pause_cb[0] = lambda: root.after(0, pause)

    def pick_window():
        _picking.set()
        status.set("Click target window...")

    def clear_target():
        _target["pid"]   = None
        _target["title"] = "All Windows"
        _last_key[0] = None
        _refresh_target_label()

    def toggle_topmost():
        _on_top[0] = not _on_top[0]
        _apply_topmost()

    def quit_app():
        _running.clear()
        root.destroy()

    tk.Label(root, textvariable=status,     width=22).grid(row=0, column=0, columnspan=2, pady=(8, 2))
    tk.Label(root, textvariable=target_lbl, width=22, fg="gray").grid(row=1, column=0, columnspan=2)

    btn_start = tk.Button(root, text="Start  [",    width=9, command=start)
    btn_pause = tk.Button(root, text="Pause  ]",    width=9, command=pause, state=tk.DISABLED)
    btn_pick  = tk.Button(root, text="Pick Window", width=12, command=pick_window)
    btn_clear = tk.Button(root, text="Clear",       width=6,  command=clear_target)
    btn_top   = tk.Button(root, text="On Top: ON",  width=12, command=toggle_topmost, relief=tk.SUNKEN)
    btn_quit  = tk.Button(root, text="Quit",        width=9,  command=quit_app)

    btn_start.grid(row=2, column=0, padx=8, pady=4)
    btn_pause.grid(row=2, column=1, padx=8, pady=4)
    btn_pick.grid( row=3, column=0, padx=8, pady=2)
    btn_clear.grid(row=3, column=1, padx=8, pady=2)
    btn_top.grid(  row=4, column=0, columnspan=2, pady=2)
    btn_quit.grid( row=5, column=0, columnspan=2, pady=(2, 8))

    _apply_topmost()

    threading.Thread(target=_loop, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    main()
