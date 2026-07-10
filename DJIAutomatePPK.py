import configparser
import subprocess
import sys
import time
import ctypes
from pathlib import Path
from pywinauto import Application, Desktop
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PWTimeoutError
from pywinauto.keyboard import send_keys
from pywinauto import timings as _t

_t.Timings.window_find_retry       = 0.5
_t.Timings.after_click_wait        = 0.0
_t.Timings.after_clickinput_wait   = 0.0
_t.Timings.after_sendkeys_key_wait = 0.0

def _check_dpi_150():
    """Exit with a popup warning if Windows UI scaling is not set to 150%."""
    dpi = ctypes.windll.user32.GetDpiForSystem()
    if dpi != 144:
        pct = round(dpi / 96 * 100)
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Windows UI scaling is {pct}% (DPI={dpi}).\nThis script requires 150%. Please adjust in Display Settings and re-run.",
            "Wrong DPI Scale",
            0x30
        )
        raise SystemExit(1)
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    print("[ok] DPI scaling confirmed at 150%")

# ── Configuration ────────────────────────────────────────────────
WIN_TITLE   = "DJI Terra"
EXE_PATH    = r"C:\Program Files (x86)\DJI Product\DJI Terra\DJI Terra.exe"
LAUNCH_WAIT = 30

_base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
_cfg = configparser.ConfigParser()
_cfg.read(_base / "DJI_PARAMETERS.ini", encoding="utf-8")
if "parameters" not in _cfg:
    raise FileNotFoundError(
        f"DJI PARAMETERS.ini not found or missing [parameters] section — expected at: {_base / 'DJI PARAMETERS.ini'}"
    )
_p = _cfg["parameters"]

PROJECT_NAME     = _p["project_name"]
PROJECT_LOCATION = _p["project_location"]
DATA_SOURCE      = _p["data_source"]
EPSG_HORIZONTAL  = _p["epsg_horizontal"]
EPSG_VERTICAL    = _p["epsg_vertical"]
GCP_PATH         = _p.get("gcp_path", "")
BASE_DATA        = r"\\192.168.35.25\3dData\RockSprings\BitterCreek\05Apr2026\BaseData\00020950.OBS"
TERRA_PATH      = r"\\192.168.35.25\3dData\RockSprings\BitterCreek\05Apr2026\Terra"
# ─────────────────────────────────────────────────────────────────

def launch_if_needed() -> bool:
    """Launch DJI Terra if not already running. Returns True if just launched."""
    try:
        Application(backend="uia").connect(
            title=WIN_TITLE, class_name="Chrome_WidgetWin_1", timeout=1
        )
        print("[ok] DJI Terra already running")
        return False
    except (ElementNotFoundError, PWTimeoutError):
        pass

    print("[..] Launching DJI Terra...")
    subprocess.Popen([EXE_PATH])
    return True

def get_app():
    app = Application(backend="uia").connect(
        title=WIN_TITLE, class_name="Chrome_WidgetWin_1", timeout=10
    )
    return app

def get_main_window(app):
    return app.window(title="DJI Terra", class_name="Chrome_WidgetWin_1")

def _bring_to_front(_win):
    """Restore and focus DJI Terra via FindWindowW (avoids background Chromium subwindow)."""
    SW_RESTORE  = 9
    SW_MAXIMIZE = 3

    hwnd = ctypes.windll.user32.FindWindowW(None, WIN_TITLE)
    if not hwnd:
        raise RuntimeError(f"FindWindowW could not find '{WIN_TITLE}' — is DJI Terra running?")
    print(f"[..] Terra HWND: {hwnd}")

    ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
    ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)

    fg_hwnd = ctypes.windll.user32.GetForegroundWindow()
    fg_tid  = ctypes.windll.user32.GetWindowThreadProcessId(fg_hwnd, None)
    my_tid  = ctypes.windll.kernel32.GetCurrentThreadId()
    tgt_tid = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
    if fg_tid != my_tid:
        ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, True)
    ctypes.windll.user32.AttachThreadInput(tgt_tid, fg_tid, True)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    ctypes.windll.user32.BringWindowToTop(hwnd)
    ctypes.windll.user32.AttachThreadInput(tgt_tid, fg_tid, False)
    if fg_tid != my_tid:
        ctypes.windll.user32.AttachThreadInput(my_tid, fg_tid, False)
    time.sleep(0.1)
    print("[ok] DJI Terra brought to front")

# ── Steps ────────────────────────────────────────────────────────

def step_01_create_project(dlg):
    btn = dlg.child_window(title="Create Project", control_type="Button")
    btn.wait("enabled visible", timeout=10)
    btn.invoke()
    print("[ok] Clicked 'Create Project'")

def step_02_select_visible_light(dlg):
    elem = dlg.child_window(title="Visible Light", control_type="Text")
    elem.wait("visible", timeout=10)
    elem.click_input()
    print("[ok] Clicked 'Visible Light'")

def step_03_enter_project_name(dlg):
    from pywinauto import mouse
    label = dlg.child_window(title="Project Name", control_type="Text")
    label.wait("visible", timeout=10)
    r = label.rectangle()
    field_x = r.left + (r.right - r.left) // 2
    field_y = r.bottom + 25
    mouse.click(button='left', coords=(field_x, field_y))
    time.sleep(0.1)
    send_keys("^a{DELETE}")
    send_keys(PROJECT_NAME, with_spaces=True)
    print(f"[ok] Entered project name '{PROJECT_NAME}'")

def step_04_set_project_location(dlg):
    from pywinauto import mouse
    label = dlg.child_window(title="Storage Location", control_type="Text")
    label.wait("visible", timeout=10)
    lr = label.rectangle()
    mouse.click(button='left', coords=(lr.right + 36, lr.bottom + 25))

    file_dlg = _wait_for_select_folder_dialog("Select Folder")
    if file_dlg is None:
        raise RuntimeError("'Select Folder' dialog did not appear within 10s")

    _send_path_to_browse_dialog(file_dlg, PROJECT_LOCATION)
    send_keys("{ENTER}")

    btn = file_dlg.child_window(title="Select Folder", control_type="Button")
    btn.wait("enabled", timeout=5)
    btn.click_input()
    print(f"[ok] Set project location '{PROJECT_LOCATION}'")

def step_05_click_ok(dlg):
    btn = dlg.child_window(title="OK", control_type="Hyperlink")
    btn.wait("visible", timeout=10)
    btn.click_input()
    print("[ok] Clicked 'OK'")

def step_06_select_data_source(dlg):
    btn = dlg.child_window(title="Select Folder", control_type="Button", found_index=0)
    btn.wait("visible enabled", timeout=10)
    btn.click_input()

    file_dlg = _wait_for_select_folder_dialog("Select Folder")
    if file_dlg is None:
        raise RuntimeError("'Select Folder' dialog did not appear within 10s")

    _send_path_to_browse_dialog(file_dlg, DATA_SOURCE)
    send_keys("{ENTER}")

    confirm = file_dlg.child_window(title="Select Folder", control_type="Button")
    confirm.wait("enabled", timeout=5)
    confirm.click_input()
    print(f"[ok] Selected data source '{DATA_SOURCE}'")
# ── PPK-specific steps ───────────────────────────────────────────

def _wait_for_progress_done(dlg, timeout=300):
    """Poll until no Text element containing '%' is visible (import complete)."""
    print("[..] Waiting for import to finish...")
    elapsed = 0
    while elapsed < timeout:
        time.sleep(1)
        elapsed += 1
        try:
            if not dlg.child_window(title_re=r".*%.*", control_type="Text").exists(timeout=0.2):
                print(f"[ok] Import complete ({elapsed}s)")
                return
        except Exception:
            return
    raise TimeoutError(f"Progress indicator still visible after {timeout}s")

def step_07_click_photo_pos_arrow(dlg):
    from pywinauto import mouse
    _wait_for_progress_done(dlg)
    # Anchor: (2243,228) Text "Photo POS" → target: (2284,231) ICON-ArrowDown → dx=+41, dy=+3
    label = dlg.child_window(title="Photo POS", control_type="Text")
    label.wait("visible", timeout=10)
    r = label.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 41, cy + 3))
    print("[ok] Clicked 'Photo POS' arrow")

def step_07_click_photo_PPK(dlg):
    from pywinauto import mouse
    _wait_for_progress_done(dlg)
    # Anchor: (2243,228) Text "Photo POS" → target: (2284,231) ICON-ArrowDown → dx=+41, dy=+3
    label = dlg.child_window(title="When signal is poor or lost during data collection, PPK can be used to obtain photo POS data", control_type="Text")
    label.wait("visible", timeout=10)
    r = label.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 41, cy - 40))
    print("[ok] Clicked 'Local PPK'")

def step_07_click_photo_settings(dlg):
    from pywinauto import mouse
    # Anchor: (2253,267) Text "Camera Info" → target: (2529,223) ICON-Set → dx=+276, dy=-44
    anchor = dlg.child_window(title="Camera Info", control_type="Text")
    anchor.wait("visible", timeout=10)
    r = anchor.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 420, cy - 70))
    print("[ok] Clicked PPK settings")

def step_07_start_calculation(dlg):
    from pywinauto import mouse
    btn = dlg.child_window(title="Start Calculation", control_type="Button")
    btn.wait("enabled visible", timeout=10)
    btn.invoke()
    print("[ok] Clicked 'Start Calculation'")
    label = dlg.child_window(title=r"Export", control_type="Button")
    label.wait("enabled visible", timeout=5000)
    label.click_input()
    file_dlg = _wait_for_select_folder_dialog("Save As")
    if file_dlg is None:
        raise RuntimeError("'Save As' dialog did not appear within 10s")

    # Type folder path into address bar (top), navigate there
    _send_path_to_browse_dialog(file_dlg, TERRA_PATH)
    send_keys("{ENTER}")
    time.sleep(0.5)
    # Type filename into the filename field (bottom)
    filename_edit = file_dlg.child_window(title="File name:", control_type="Edit")
    filename_edit.wait("visible enabled", timeout=5)
    filename_edit.set_edit_text("POS.txt")
    send_keys("{ENTER}")
    print("[ok] Saved POS.txt to Terra folder")


# ── Helpers ──────────────────────────────────────────────────────


def _wait_for_select_folder_dialog(title):
    for _ in range(100):
        time.sleep(0.1)
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            try:
                return Desktop(backend="uia").window(handle=hwnd)
            except Exception:
                pass
    return None

def _send_path_to_browse_dialog(file_dlg, path):
    WM_SETTEXT = 0x000C
    hwnd = file_dlg.handle
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.2)

    edit_hwnd = ctypes.windll.user32.FindWindowExW(hwnd, None, "Edit", None)
    if not edit_hwnd:
        combo_ex = ctypes.windll.user32.FindWindowExW(hwnd, None, "ComboBoxEx32", None)
        if combo_ex:
            combo = ctypes.windll.user32.FindWindowExW(combo_ex, None, "ComboBox", None)
            if combo:
                edit_hwnd = ctypes.windll.user32.FindWindowExW(combo, None, "Edit", None)

    if edit_hwnd:
        ctypes.windll.user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, path)
    else:
        send_keys("^a{DEL}")
        send_keys(path, with_spaces=True, pause=0.05)

# ── Main ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import traceback

    _parser = argparse.ArgumentParser()
    _parser.add_argument("--project-name",     default=None)
    _parser.add_argument("--project-location", default=None)
    _parser.add_argument("--data-source",      default=None)
    _parser.add_argument("--epsg-h",           default=None)
    _parser.add_argument("--epsg-v",           default=None)
    _parser.add_argument("--gcp-path",         default=None)
    _args = _parser.parse_args()

    if _args.project_name:     PROJECT_NAME     = _args.project_name
    if _args.project_location: PROJECT_LOCATION = _args.project_location
    if _args.data_source:      DATA_SOURCE      = _args.data_source
    if _args.epsg_h:           EPSG_HORIZONTAL  = _args.epsg_h
    if _args.epsg_v:           EPSG_VERTICAL    = _args.epsg_v
    if _args.gcp_path:         GCP_PATH         = _args.gcp_path

    try:
        _check_dpi_150()
        just_launched = launch_if_needed()
        if just_launched:
            print("[..] Waiting 50s for DJI Terra home screen...")
            time.sleep(50)
        print("[..] Connecting to DJI Terra...")
        app  = get_app()
        print("[ok] Connected")
        main = get_main_window(app)
        _bring_to_front(main)
        main.wait("ready", timeout=5)
        print("[ok] Window ready")
    except Exception:
        traceback.print_exc()
        input("Press Enter to exit...")
        raise SystemExit(1)

    try:
        step_01_create_project(main)
        step_02_select_visible_light(main)
        step_03_enter_project_name(main)
        step_04_set_project_location(main)
        step_05_click_ok(main)
        step_06_select_data_source(main)
        step_07_click_photo_pos_arrow(main)
        step_07_click_photo_PPK(main)
        step_07_click_photo_settings(main)
        step_07_start_calculation(main)
        # TODO: call PPK steps here
        print("[ok] Automation complete")
    except Exception:
        traceback.print_exc()
        input("Press Enter to exit...")
        raise SystemExit(1)

    input("Press Enter to close...")
