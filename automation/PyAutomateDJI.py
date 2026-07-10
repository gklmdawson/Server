import configparser
import functools
import logging
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

_logger = logging.getLogger("dji_lidar")

# Set from --unattended: suppress all dialogs/popups so the script can run
# under the job agent without anything blocking on human input.
UNATTENDED = False

def _configure_logging(log_file_path: str):
    """Append this process's stdout/stderr to the Data-Intake log file."""
    _logger.setLevel(logging.DEBUG)
    _formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(log_file_path, mode="a", encoding="utf-8")
    fh.setFormatter(_formatter)
    _logger.addHandler(fh)

    class _ToLog:
        def __init__(self, level): self._level = level
        def write(self, msg):
            for line in msg.splitlines():
                line = line.strip()
                if line:
                    _logger.log(self._level, line)
        def flush(self): pass

    sys.stdout = _ToLog(logging.INFO)
    sys.stderr = _ToLog(logging.ERROR)
    _logger.info("----- DJI LiDAR automation started -----")

def _check_dpi_150():
    """Exit if Windows UI scaling is not 150% (popup unless --unattended; exit code 2)."""
    dpi = ctypes.windll.user32.GetDpiForSystem()
    if dpi != 144:
        pct = round(dpi / 96 * 100)
        msg = (f"Windows UI scaling is {pct}% (DPI={dpi}).\n"
               "This script requires 150%. Please adjust in Display Settings and re-run.")
        print(f"[error] {msg}", file=sys.stderr)
        if not UNATTENDED:
            ctypes.windll.user32.MessageBoxW(0, msg, "Wrong DPI Scale", 0x30)  # MB_ICONWARNING | MB_OK
        raise SystemExit(2)
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    print("[ok] DPI scaling confirmed at 150%")

# ── Configuration ────────────────────────────────────────────────
WIN_TITLE        = "DJI Terra"
EXE_PATH         = r"C:\Program Files (x86)\DJI Product\DJI Terra\DJI Terra.exe"
LAUNCH_WAIT      = 30

_base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
_cfg = configparser.ConfigParser()
_cfg.read(_base / "DJI_PARAMETERS.ini", encoding="utf-8")
_p = _cfg["parameters"] if "parameters" in _cfg else {}

PROJECT_NAME     = _p.get("project_name",     "")
PROJECT_LOCATION = _p.get("project_location", "")
DATA_SOURCE      = _p.get("data_source",      "")
EPSG_HORIZONTAL  = _p.get("epsg_horizontal",  "")
EPSG_VERTICAL    = _p.get("epsg_vertical",    "")
GCP_PATH         = _p.get("gcp_path",         "")
TLT_GCP_PATH     = ""   # populated at startup from GCP_PATH after filtering for TLT rows
# ─────────────────────────────────────────────────────────────────

def launch_if_needed() -> bool:
    """Start DJI Terra if not already running. Returns True if just launched, False if already running."""
    try:
        Application(backend="uia").connect(
            title=WIN_TITLE, class_name="Chrome_WidgetWin_1", timeout=3
        )
        print("[ok] DJI Terra already running")
        return False
    except (ElementNotFoundError, PWTimeoutError):
        pass

    print("[..] Launching DJI Terra...")
    subprocess.Popen([EXE_PATH])
    for _ in range(LAUNCH_WAIT):
        time.sleep(1)
        try:
            Application(backend="uia").connect(
                title=WIN_TITLE, class_name="Chrome_WidgetWin_1", timeout=1
            )
            print("[ok] DJI Terra window ready")
            return True
        except (ElementNotFoundError, PWTimeoutError):
            pass
    raise TimeoutError(f"DJI Terra window did not appear within {LAUNCH_WAIT}s")

def get_app():
    """Attach to running DJI Terra instance."""
    app = Application(backend="uia").connect(
        title=WIN_TITLE,
        class_name="Chrome_WidgetWin_1"
    )
    return app

def get_main_window(app):
    return app.window(title="DJI Terra", class_name="Chrome_WidgetWin_1")

def _bring_to_front(_win):
    """Restore and focus DJI Terra. Uses FindWindowW for the top-level HWND so we never
    accidentally target a background Chromium subwindow via pywinauto handle resolution."""
    SW_RESTORE  = 9
    SW_MAXIMIZE = 3

    hwnd = ctypes.windll.user32.FindWindowW(None, WIN_TITLE)
    if not hwnd:
        raise RuntimeError(f"FindWindowW could not find '{WIN_TITLE}' — is DJI Terra running?")
    print(f"[..] Terra HWND: {hwnd}")

    ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.5)
    ctypes.windll.user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.3)

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
    time.sleep(0.5)
    print("[ok] DJI Terra brought to front")

def _with_retry(max_retries=2, delay=1.5):
    """Retry a step on pywinauto timeout or element-not-found, up to max_retries extra attempts."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1 + max_retries):
                try:
                    return fn(*args, **kwargs)
                except (PWTimeoutError, ElementNotFoundError) as exc:
                    if attempt < max_retries:
                        print(f"[retry {attempt + 1}/{max_retries}] {fn.__name__} — {exc!r}")
                        time.sleep(delay)
                    else:
                        raise
        return wrapper
    return decorator


@_with_retry()
def step_01_create_project(dlg):
    btn = dlg.child_window(title="Create Project", control_type="Button")
    btn.wait("enabled visible", timeout=10)
    btn.invoke()
    print("[ok] Clicked 'Create Project'")

@_with_retry()
def step_02_select_lidar(dlg):
    elem = dlg.child_window(title="LiDAR Point Cloud", control_type="Text")
    elem.wait("visible", timeout=10)
    elem.click_input()
    print("[ok] Clicked 'LiDAR Point Cloud'")

@_with_retry()
def step_03_enter_project_name(dlg):
    from pywinauto import mouse
    # The Edit field is not in the UIA tree — click just below the 'Project Name' label.
    label = dlg.child_window(title="Project Name", control_type="Text")
    label.wait("visible", timeout=10)
    r = label.rectangle()
    # Edit field sits directly below the label (~25px gap based on accessibility coords)
    field_x = r.left + (r.right - r.left) // 2
    field_y = r.bottom + 25
    mouse.click(button='left', coords=(field_x, field_y))
    time.sleep(0.3)
    send_keys("^a")
    send_keys("{DELETE}")
    send_keys(PROJECT_NAME + "_LiDAR", with_spaces=True)
    print(f"[ok] Entered project name '{PROJECT_NAME}_LiDAR'")

@_with_retry()
def step_04_set_project_location(dlg):
    # 'ICON Folder' is not in the UIA traversal tree — click via offset from the label.
    # Offsets derived from physical pixel coords (Accessibility Insights) / ~1.5x DPI scale.
    from pywinauto import mouse
    label = dlg.child_window(title="Storage Location", control_type="Text")
    label.wait("visible", timeout=10)
    lr = label.rectangle()
    icon_x = lr.right + 36
    icon_y = lr.bottom + 25
    mouse.click(button='left', coords=(icon_x, icon_y))

    file_dlg = _wait_for_select_folder_dialog("Select Folder")
    if file_dlg is None:
        raise RuntimeError("'Select Folder' dialog did not appear within 10s")

    _send_path_to_browse_dialog(file_dlg, PROJECT_LOCATION)
    send_keys("{ENTER}")


    # Click the confirm button
    btn = file_dlg.child_window(title="Select Folder", control_type="Button")
    btn.wait("enabled", timeout=5)
    btn.click_input()
    print(f"[ok] Set project location '{PROJECT_LOCATION}'")

@_with_retry()
def step_05_click_ok(dlg):
    btn = dlg.child_window(title="OK", control_type="Hyperlink")
    btn.wait("visible", timeout=10)
    btn.click_input()
    print("[ok] Clicked 'OK'")

@_with_retry()
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

def step_07_wait_for_import(dlg):
    # Wait for progress bar to appear (confirms import started)
    print("[..] Waiting for import to start...")
    for _ in range(20):
        time.sleep(0.5)
        if _import_progress_visible(dlg):
            print("[..] Import in progress...")
            break

    # Wait indefinitely until progress bar disappears
    elapsed = 0
    while True:
        time.sleep(1)
        elapsed += 1
        if not _import_progress_visible(dlg):
            print(f"[ok] Import complete ({elapsed}s)")
            return


@_with_retry()
def step_08_click_horizontal_datum(dlg):
    from pywinauto import mouse
    # "Horizontal Datum Settings" not in UIA tree — offset from "Start Reconstruction" anchor
    # Offsets from UIInspect: SR=(2317,841), HD=(2048,888) → dx=-269, dy=+47
    btn = dlg.child_window(title="Start Reconstruction", control_type="Button")
    btn.wait("visible", timeout=15)
    r = btn.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx - 345, cy + 47))
    print("[ok] Clicked 'Horizontal Datum Settings'")

@_with_retry()
def step_09_enter_epsg(dlg):
    combo = dlg.child_window(title="Horizontal Datum Settings", control_type="ComboBox")
    combo.wait("visible", timeout=15)
    combo.click_input()
    time.sleep(0.5)
    send_keys("^a")
    send_keys(EPSG_HORIZONTAL, with_spaces=True)
    print(f"[ok] Entered EPSG code '{EPSG_HORIZONTAL}'")

@_with_retry()
def step_10_select_epsg_result(dlg):
    from pywinauto import mouse
    # Click the search result Group that appears below the "Horizontal Datum Settings" label
    # UIInspect anchor: (781,401) Text, target: (781,511) Group → dy=+110
    label = dlg.child_window(title="Horizontal Datum Settings", control_type="Text")
    label.wait("visible", timeout=10)
    r = label.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx, cy + 175))
    print("[ok] Selected EPSG result")

@_with_retry()
def step_11_enter_vertical_epsg(dlg):
    combo = dlg.child_window(title="Vertical Datum Settings", control_type="ComboBox")
    combo.wait("visible", timeout=15)
    combo.click_input()
    time.sleep(0.5)
    send_keys("^a")
    send_keys(EPSG_VERTICAL, with_spaces=True)
    print(f"[ok] Entered vertical EPSG code '{EPSG_VERTICAL}'")

@_with_retry()
def step_12_select_vertical_result(dlg):

    from pywinauto import mouse
    # UIInspect anchor: (1581,479) Text, target: (1568,585) → dx=-13, dy=+106
    label = dlg.child_window(title="Vertical Datum Settings", control_type="Text")
    label.wait("visible", timeout=10)
    r = label.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx - 13, cy + 160))
    print("[ok] Selected vertical EPSG result")


@_with_retry()
def step_13_click_ok(dlg):
    btn = dlg.child_window(title="OK", control_type="Button")
    btn.wait("visible enabled", timeout=10)
    btn.invoke()
    print("[ok] Clicked 'OK'")

@_with_retry()
def step_14_16_click_template_fields(dlg):
    from pywinauto import mouse
    # Edit field not in UIA tree — offset from "Template" Text anchor
    # UIInspect: anchor=(1559,449), target=(1704,472) → dx=+145, dy=+23
    label = dlg.child_window(title="Template", control_type="Text")
    label.wait("visible", timeout=10)
    r = label.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 145, cy + 40))
    print("[ok] Clicked 'Template' field")
    mouse.click(button='left', coords=(cx + 145, cy + 165))
    print("[ok] Clicked below 'Template' field")
    mouse.click(button='left', coords=(cx + 145, cy + 130))
    time.sleep(1.0)  # wait for DJI Terra UI to update before step_17 looks for "Not Set"
    print("[ok] Clicked below 'Template' field")

@_with_retry()
def step_17_click_arrow_right(dlg):
    label = dlg.child_window(title="Not Set", control_type="Text")
    label.wait("visible", timeout=10)
    label.click_input()
    print("[ok] Clicked 'Not Set'")

@_with_retry()
def step_18_click_import_gcp(dlg):
    btn = dlg.child_window(title="Import GCP", control_type="Button")
    btn.wait("visible enabled", timeout=10)
    btn.invoke()
    print("[ok] Clicked 'Import GCP'")

@_with_retry()
def step_19_select_gcp_path(_dlg):
    file_dlg = _wait_for_select_folder_dialog("Open")
    if file_dlg is None:
        raise RuntimeError("GCP file dialog did not appear within 10s")

    _send_path_to_browse_dialog(file_dlg, TLT_GCP_PATH)
    send_keys("{ENTER}")
    time.sleep(0.5)
    print(f"[ok] Selected TLT target CSV '{TLT_GCP_PATH}'")

@_with_retry()
def step_20_29_click_gcp_entries(dlg):
    from pywinauto import mouse
    btn = dlg.child_window(title="Cancel", control_type="Button")
    btn.wait("visible", timeout=10)
    r = btn.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    time.sleep(1)
    # pt1
    # Anchor: button 'Cancel' [l=1548,t=1363,r=1737,b=1412] → center (1642,1387)
    # Target: Group (411,771) → dx=-1231, dy=-616
    mouse.click(button='left', coords=(cx - 950, cy - 265))
    print("[ok] Clicked GCP entry group")
    time.sleep(1)
    # pt2
    # Anchor: button 'Cancel' [l=1548,t=1363,r=1737,b=1412] → center (1642,1387)
    # Target: Group (411,771) → dx=-1231, dy=-616

    mouse.click(button='left', coords=(cx - 950, cy - 185))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt3
   
    mouse.click(button='left', coords=(cx - 950, cy - 185))
    time.sleep(2)
    mouse.click(button='left', coords=(cx - 950, cy - 105))
    print("[ok] Scrolled GCP list")
    time.sleep(2)
    # pt4
    mouse.click(button='left', coords=(cx - 655, cy - 545))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt5
    mouse.click(button='left', coords=(cx - 655, cy - 500))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt6
    mouse.click(button='left', coords=(cx - 555, cy - 545))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt7
    mouse.click(button='left', coords=(cx - 555, cy - 415))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt8
    mouse.click(button='left', coords=(cx - 355, cy - 545))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt9
    mouse.click(button='left', coords=(cx - 355, cy - 465))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt10
    mouse.click(button='left', coords=(cx - 115, cy - 545))
    print("[ok] Clicked GCP entry group")
    time.sleep(2)
    # pt11
    mouse.click(button='left', coords=(cx - 115, cy - 355))
    print("[ok] Clicked GCP entry group")
    time.sleep(1)
    btn = dlg.child_window(title="Import", control_type="Button")
    btn.wait("visible enabled", timeout=10)
    btn.invoke()
    print("[ok] Clicked 'Import'")

@_with_retry()
def step_30_start_reconstruction(dlg):
    # GCP flow exposes two "Start Reconstruction" buttons (the live one is index 1);
    # the no-targets flow leaves only one (index 0). Try index 1 first, then fall back.
    last_err = None
    for idx in (1, 0):
        try:
            btn = dlg.child_window(title="Start Reconstruction", control_type="Button", found_index=idx)
            btn.wait("visible enabled", timeout=10)
            btn.invoke()
            print(f"[ok] Clicked 'Start Reconstruction' (index {idx})")
            return
        except Exception as e:
            last_err = e
    raise RuntimeError(f"'Start Reconstruction' button not found at index 1 or 0: {last_err}")

@_with_retry()
def step_31_click_ok_checklist(dlg):
    # OK button on Parameters Checklist dialog after Start Reconstruction
    # BoundingRectangle [l=1726,t=1345,r=1846,b=1394]
    btn = dlg.child_window(title="OK", control_type="Button")
    btn.wait("visible enabled", timeout=10)
    btn.invoke()
    print("[ok] Clicked 'OK' (Parameters Checklist)")






def _wait_for_select_folder_dialog(type):
    """Wait for the 'Select Folder' dialog using FindWindowW (bypasses UIA traversal issues)."""
    import ctypes
    for _ in range(10 * 2):
        time.sleep(0.5)
        hwnd = ctypes.windll.user32.FindWindowW(None, type)
        if hwnd:
            try:
                return Desktop(backend="uia").window(handle=hwnd)
            except Exception:
                pass
    return None

def _send_path_to_browse_dialog(file_dlg, path):
    """Set path in a file dialog Edit control via WM_SETTEXT.
    Handles both BrowseForFolder (direct Edit child) and standard Open dialogs
    (Edit nested inside ComboBoxEx32 → ComboBox → Edit)."""
    WM_SETTEXT = 0x000C
    hwnd = file_dlg.handle
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    send_keys("a")  # activates BrowseForFolder's hidden edit box
    time.sleep(0.4)

    # BrowseForFolder: Edit is a direct child
    edit_hwnd = ctypes.windll.user32.FindWindowExW(hwnd, None, "Edit", None)

    # Standard Open dialog: Edit is nested inside ComboBoxEx32 → ComboBox → Edit
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
        send_keys(path, with_spaces=True, pause=0.1)

def _extract_tlt_csv(src: str) -> str:
    """Read the GCP CSV, keep only rows where column E == 'TLT', write to SINGLE_TLT.csv in the project folder."""
    import csv
    tlt_rows = []
    with open(src, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if len(row) >= 5 and row[4].strip().upper() == "TLT":
                tlt_rows.append(row)

    if not tlt_rows:
        raise ValueError(f"No rows with 'TLT' in column E found in: {src}")

    out_path = Path(PROJECT_LOCATION).parent / "SINGLE_TLT.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(tlt_rows)

    print(f"[ok] Extracted {len(tlt_rows)} TLT row(s) → {out_path}")
    return str(out_path)

def _import_progress_visible(dlg):
    """Returns True if the 'Importing data. Do not exit DJI Terra' text element is present."""
    try:
        return dlg.child_window(
            title="Importing data. Do not exit DJI Terra", control_type="Text"
        ).exists(timeout=0.2)
    except Exception:
        return False

# ── Error dialog ─────────────────────────────────────────────────

def _show_error_dialog(title: str, error_text: str) -> None:
    """Error dialog with scrollable traceback and an Export to .txt button.
    Suppressed under --unattended so a failure can never block on human input."""
    if UNATTENDED:
        print(f"[error] {title} (dialog suppressed by --unattended)", file=sys.stderr)
        return
    import tkinter as tk
    from tkinter import filedialog, scrolledtext

    root = tk.Tk()
    root.title(title)
    root.geometry("680x420")
    root.resizable(True, True)
    root.attributes("-topmost", True)

    frame = tk.Frame(root, padx=10, pady=8)
    frame.pack(fill=tk.BOTH, expand=True)

    tk.Label(frame, text="An error occurred:", font=("Segoe UI", 10, "bold"), anchor="w").pack(fill=tk.X)

    txt = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Consolas", 9))
    txt.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
    txt.insert(tk.END, error_text)
    txt.config(state=tk.DISABLED)

    btn_row = tk.Frame(frame)
    btn_row.pack(fill=tk.X)

    def _export():
        path = filedialog.asksaveasfilename(
            parent=root,
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="DJI_Automate_Error.txt",
            title="Save error log",
        )
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(error_text)

    tk.Button(btn_row, text="Export to .txt", width=14, command=_export).pack(side=tk.LEFT)
    tk.Button(btn_row, text="Close", width=10, command=root.destroy).pack(side=tk.RIGHT)

    root.mainloop()

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
    _parser.add_argument("--no-targets",       action="store_true")
    _parser.add_argument("--log-file",         default=None)
    _parser.add_argument("--unattended",       action="store_true",
                         help="suppress all dialogs/popups; errors go to stderr + exit code")
    _args = _parser.parse_args()

    UNATTENDED = _args.unattended

    if _args.log_file:
        _configure_logging(_args.log_file)

    if _args.project_name:     PROJECT_NAME      = _args.project_name
    if _args.project_location: PROJECT_LOCATION  = _args.project_location
    if _args.data_source:      DATA_SOURCE       = _args.data_source
    if _args.epsg_h:           EPSG_HORIZONTAL   = _args.epsg_h
    if _args.epsg_v:           EPSG_VERTICAL     = _args.epsg_v
    if _args.gcp_path:         GCP_PATH          = _args.gcp_path
    no_targets = bool(_args.no_targets)

    if no_targets:
        print("[info] --no-targets set - skipping GCP/TLT import")
    elif GCP_PATH:
        TLT_GCP_PATH = _extract_tlt_csv(GCP_PATH)
    else:
        print("[warn] No GCP path provided — TLT extraction skipped")

    try:
        _check_dpi_150()
        just_launched = launch_if_needed()
        if just_launched:
            time.sleep(50)  # allow home screen to fully render after fresh launch
        app  = get_app()
        main = get_main_window(app)
        _bring_to_front(main)
        main.wait("ready", timeout=15)
        print("[ok] Window ready")
    except Exception:
        traceback.print_exc()
        _show_error_dialog("DJI Automate — Startup Error", traceback.format_exc())
        raise SystemExit(1)

    try:
        step_01_create_project(main)
        step_02_select_lidar(main)
        step_03_enter_project_name(main)
        step_04_set_project_location(main)
        step_05_click_ok(main)
        step_06_select_data_source(main)
        step_07_wait_for_import(main)
        step_08_click_horizontal_datum(main)
        step_09_enter_epsg(main)
        step_10_select_epsg_result(main)
        step_11_enter_vertical_epsg(main)
        step_12_select_vertical_result(main)
        step_13_click_ok(main)
        step_14_16_click_template_fields(main)
        if not no_targets:
            step_17_click_arrow_right(main)
            step_18_click_import_gcp(main)
            step_19_select_gcp_path(main)
            step_20_29_click_gcp_entries(main)
        else:
            print("[info] No targets - skipping GCP import (steps 17-29)")
        step_30_start_reconstruction(main)
        step_31_click_ok_checklist(main)
        # Next steps will go here...
    except Exception:
        traceback.print_exc()
        _show_error_dialog("DJI Automate — Error", traceback.format_exc())
        raise SystemExit(1)