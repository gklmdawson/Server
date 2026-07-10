import configparser
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

_logger = logging.getLogger("dji_ppk")

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
    _logger.info("----- DJI PPK automation started -----")

def _check_dpi_150():
    """Exit if Windows UI scaling is not 150% (popup unless --unattended; exit code 2)."""
    dpi = ctypes.windll.user32.GetDpiForSystem()
    if dpi != 144:
        pct = round(dpi / 96 * 100)
        msg = (f"Windows UI scaling is {pct}% (DPI={dpi}).\n"
               "This script requires 150%. Please adjust in Display Settings and re-run.")
        print(f"[error] {msg}", file=sys.stderr)
        if not UNATTENDED:
            ctypes.windll.user32.MessageBoxW(0, msg, "Wrong DPI Scale", 0x30)
        raise SystemExit(2)
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    print("[ok] DPI scaling confirmed at 150%")

# ── Configuration ────────────────────────────────────────────────
WIN_TITLE   = "DJI Terra"
EXE_PATH    = r"C:\Program Files (x86)\DJI Product\DJI Terra\DJI Terra.exe"
LAUNCH_WAIT = 30

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
BASE_DATA        = _p.get("base_data",  "")
TERRA_PATH       = _p.get("terra_path", "")
PPK_PATH         = _p.get("ppk_path",   "")
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
        title=WIN_TITLE, class_name="Chrome_WidgetWin_1", timeout=900
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

def close_terra():
    """Send WM_CLOSE to DJI Terra's top-level window."""
    WM_CLOSE = 0x0010
    hwnd = ctypes.windll.user32.FindWindowW(None, WIN_TITLE)
    if hwnd:
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        print("[ok] DJI Terra close signal sent")
    else:
        print("[warn] DJI Terra window not found — may have already closed")

# ── Steps ────────────────────────────────────────────────────────

def step_01_create_project(dlg):
    btn = dlg.child_window(title="Create Project", control_type="Button")
    btn.wait("enabled visible", timeout=900)
    btn.invoke()
    print("[ok] Clicked 'Create Project'")

def step_02_select_visible_light(dlg):
    elem = dlg.child_window(title="Visible Light", control_type="Text")
    elem.wait("visible", timeout=900)
    elem.click_input()
    print("[ok] Clicked 'Visible Light'")

def step_03_enter_project_name(dlg):
    from pywinauto import mouse
    label = dlg.child_window(title="Project Name", control_type="Text")
    label.wait("visible", timeout=900)
    r = label.rectangle()
    field_x = r.left + (r.right - r.left) // 2
    field_y = r.bottom + 25
    mouse.click(button='left', coords=(field_x, field_y))
    time.sleep(0.1)
    send_keys("^a{DELETE}")
    send_keys(PROJECT_NAME + "_PPK", with_spaces=True)
    print(f"[ok] Entered project name '{PROJECT_NAME}_PPK'")

def step_04_set_project_location(dlg):
    from pywinauto import mouse
    label = dlg.child_window(title="Storage Location", control_type="Text")
    label.wait("visible", timeout=900)
    lr = label.rectangle()
    mouse.click(button='left', coords=(lr.right + 36, lr.bottom + 25))

    file_dlg = _wait_for_select_folder_dialog("Select Folder")
    if file_dlg is None:
        raise RuntimeError("'Select Folder' dialog did not appear within 900s")

    _send_path_to_browse_dialog(file_dlg, PROJECT_LOCATION)
    send_keys("{ENTER}")

    btn = file_dlg.child_window(title="Select Folder", control_type="Button")
    btn.wait("enabled", timeout=900)
    btn.click_input()
    print(f"[ok] Set project location '{PROJECT_LOCATION}'")

def step_05_click_ok(dlg):
    btn = dlg.child_window(title="OK", control_type="Hyperlink")
    btn.wait("visible", timeout=900)
    btn.click_input()
    print("[ok] Clicked 'OK'")

def step_06_select_data_source(dlg):
    btn = dlg.child_window(title="Select Folder", control_type="Button", found_index=0)
    btn.wait("visible enabled", timeout=900)
    btn.click_input()

    file_dlg = _wait_for_select_folder_dialog("Select Folder")
    if file_dlg is None:
        raise RuntimeError("'Select Folder' dialog did not appear within 10s")

    _send_path_to_browse_dialog(file_dlg, DATA_SOURCE)
    send_keys("{ENTER}")

    confirm = file_dlg.child_window(title="Select Folder", control_type="Button")
    confirm.wait("enabled", timeout=900)
    confirm.click_input()
    print(f"[ok] Selected data source '{DATA_SOURCE}'")
# ── PPK-specific steps ───────────────────────────────────────────

def _wait_for_progress_done(dlg, timeout=900):
    """Poll until no Text element containing '%' is visible (import complete)."""
    time.sleep(1.5)
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
    label.wait("visible", timeout=900)
    r = label.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 39, cy + 1))
    time.sleep(1.5)
    print("[ok] Clicked 'Photo POS' arrow")

def step_07_click_photo_PPK(dlg):
    from pywinauto import mouse
    _wait_for_progress_done(dlg)
    # Anchor: (2243,228) Text "Photo POS" → target: (2284,231) ICON-ArrowDown → dx=+41, dy=+3
    label = dlg.child_window(title="When signal is poor or lost during data collection, PPK can be used to obtain photo POS data", control_type="Text")
    if not label.exists(timeout=900):
        # Dropdown didn't open or element title changed — print visible Text elements to aid diagnosis
        texts = [c.window_text() for c in dlg.descendants(control_type="Text") if c.is_visible()]
        print(f"[diag] Visible Text elements: {texts}")
        raise RuntimeError("PPK dropdown option not found — check [diag] output above for actual element titles")
    r = label.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 41, cy - 40))
    print("[ok] Clicked 'Local PPK'")

def step_07_click_photo_settings(dlg):
    from pywinauto import mouse
    # Anchor: (2253,267) Text "Camera Info" → target: (2529,223) ICON-Set → dx=+276, dy=-44
    anchor = dlg.child_window(title="Camera Info", control_type="Text")
    anchor.wait("visible", timeout=900)
    r = anchor.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 420, cy - 70))
    print("[ok] Clicked PPK settings")

def step_07_start_calculation(dlg):
    from pywinauto import mouse
    btn = dlg.child_window(title="Start Calculation", control_type="Button")
    btn.wait("enabled visible", timeout=900)
    btn.invoke()
    print("[ok] Clicked 'Start Calculation'")
    label = dlg.child_window(title=r"Export", control_type="Button")
    label.wait("enabled visible", timeout=5000)
    label.click_input()
    file_dlg = _wait_for_select_folder_dialog("Save As")
    if file_dlg is None:
        raise RuntimeError("'Save As' dialog did not appear within 900s")

    # Set the full destination path directly in the "File name:" field.
    # Windows Save As dialogs accept a full path there — no address-bar navigation needed.
    full_path = str(Path(PPK_PATH) / "POS.txt")
    WM_SETTEXT = 0x000C
    filename_edit = file_dlg.child_window(title="File name:", control_type="Edit")
    filename_edit.wait("visible enabled", timeout=900)
    edit_hwnd = filename_edit.handle
    ctypes.windll.user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, full_path)
    time.sleep(0.2)
    send_keys("{ENTER}")
    print(f"[ok] Saved POS.txt to {full_path}")




def step_08_click_above_vertical_accuracy(dlg):
    time.sleep(1)
    from pywinauto import mouse
    # Anchor: (1675,464) Group "Vertical Accuracy" → target 40px above center
    anchor = dlg.child_window(title="Preview POS Calculation Results", control_type="Text")
    anchor.wait("visible", timeout=900)
    r = anchor.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx + 600, cy))
    print("[ok] Clicked above 'Vertical Accuracy'")


def step_09_click_arrow_left(dlg):
    btn = dlg.child_window(title="ICON-ArrowLeft", control_type="Button")
    btn.wait("enabled visible", timeout=900)
    btn.invoke()
    print("[ok] Clicked 'ICON-ArrowLeft'")

def step_11_click_delete(dlg):
    btn = dlg.child_window(title="ICON-Delete", control_type="Button")
    btn.wait("enabled visible", timeout=900)
    btn.invoke()
    print("[ok] Clicked 'ICON-Delete'")

def step_12_click_ok(dlg):
    link = dlg.child_window(title="OK", control_type="Hyperlink")
    link.wait("enabled visible", timeout=900)
    link.invoke()
    print("[ok] Clicked 'OK'")

def step_10_click_below_search(dlg):
    from pywinauto import mouse
    # Anchor: edit 'Search' [l=36,t=243,r=468,b=279] → click 100px below center
    anchor = dlg.child_window(title="Search", control_type="Edit")
    anchor.wait("visible", timeout=900)
    r = anchor.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    mouse.click(button='left', coords=(cx, cy + 100))
    print("[ok] Clicked 100px below 'Search'")


def embed_ppk_from_pos_txt():
    import csv
    import shutil
    import sys as _sys
    import piexif
    # When frozen by PyInstaller, bundled files land in sys._MEIPASS
    _embed_dir = Path(_sys._MEIPASS) if getattr(_sys, "frozen", False) else _base
    _sys.path.insert(0, str(_embed_dir))
    from embed_ppk_metadata import embed_metadata, resolve_image_path

    pos_txt = Path(PPK_PATH) / "POS.txt"
    print(f"[..] PPK_PATH = {PPK_PATH!r}")
    print(f"[..] Looking for POS.txt at: {pos_txt.resolve()}")
    if pos_txt.parent.exists():
        contents = list(pos_txt.parent.iterdir())
        print(f"[..] Directory contents ({len(contents)} items): {[f.name for f in contents]}")
    else:
        print(f"[warn] Directory does not exist: {pos_txt.parent}")
    for _ in range(900):
        if pos_txt.exists():
            break
        time.sleep(1)
    else:
        raise FileNotFoundError(f"POS.txt did not appear within 900s at: {pos_txt.resolve()}")
    print("[ok] POS.txt found")

    ppk_dir = Path(PPK_PATH)
    print(f"[..] PPK output folder: {ppk_dir}")
    ppk_dir.mkdir(parents=True, exist_ok=True)

    # DJI Terra uses varying column names — map them to canonical keys
    _ALIASES = {
        "photo name":              "photo",
        "#photo name":             "photo",
        "latitude(°)":             "lat",
        "latitude":                "lat",
        "longitude(°)":            "lon",
        "longitude":               "lon",
        "ellipsoidal height(m)":   "alt",
        "altitude":                "alt",
        "height(m)":               "alt",
        "horizontal accuracy(m)":  "h_acc",
        "horizontal accuracy":     "h_acc",
        "vertical accuracy(m)":    "v_acc",
        "vertical accuracy":       "v_acc",
    }

    with open(pos_txt, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        col = {_ALIASES[h.strip().lower()]: h
               for h in reader.fieldnames
               if h.strip().lower() in _ALIASES}
        missing = {"photo", "lat", "lon", "alt"} - col.keys()
        if missing:
            raise ValueError(f"POS.txt missing required columns: {missing}. Found: {reader.fieldnames}")
        rows = list(reader)

    ok = skipped = errors = 0
    total = len(rows)
    print(f"[..] PPK folder : {ppk_dir}")
    print(f"[..] Source     : {DATA_SOURCE}")
    print(f"[..] Embedding PPK metadata into {total} images...")

    for i, row in enumerate(rows, 1):
        name = row[col["photo"]].strip()
        try:
            lat   = float(row[col["lat"]])
            lon   = float(row[col["lon"]])
            alt   = float(row[col["alt"]])
            h_acc = float(row[col["h_acc"]]) if "h_acc" in col else 0.0
            v_acc = float(row[col["v_acc"]]) if "v_acc" in col else 0.0
        except (KeyError, ValueError) as e:
            print(f"  [SKIP] {name}: {e}")
            skipped += 1
            continue

        src = resolve_image_path(name, DATA_SOURCE)
        if src is None:
            print(f"  [MISS] {name}")
            skipped += 1
            continue

        segments = [s for s in name.replace("\\", "/").split("/") if s]
        flight_name = segments[-2] if len(segments) >= 2 else "unknown"
        flight_dir  = ppk_dir / flight_name
        flight_dir.mkdir(parents=True, exist_ok=True)
        dest = str(flight_dir / Path(src).name)
        print(f"  [{i}/{total}] copy: {src}")
        print(f"           → {dest}")
        try:
            shutil.copy2(src, dest)
            embed_metadata(image_path=dest, lat=lat, lon=lon, alt=alt,
                           h_acc=h_acc, v_acc=v_acc, gps_status="RTKFix")
            print(f"  [OK]   {Path(src).name}")
            ok += 1
        except Exception as e:
            print(f"  [ERR]  {Path(src).name}: {e}")
            errors += 1

    print(f"[ok] PPK embed complete: {ok}/{total} copied, {skipped} skipped, {errors} errors")

# ── Helpers ──────────────────────────────────────────────────────


def _wait_for_select_folder_dialog(title):
    for _ in range(9000):
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
    # Alt+D focuses the address bar — ensures the caller's send_keys("{ENTER}") navigates
    # rather than going to the focused tree view (which ignores the typed path)
    send_keys("%d")
    time.sleep(0.15)

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
            initialfile="DJI_PPK_Error.txt",
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
    _parser.add_argument("--terra-path",       default=None)
    _parser.add_argument("--ppk-path",         default=None)
    _parser.add_argument("--log-file",         default=None)
    _parser.add_argument("--unattended",       action="store_true",
                         help="suppress all dialogs/popups; errors go to stderr + exit code")
    _args = _parser.parse_args()

    UNATTENDED = _args.unattended

    if _args.log_file:
        _configure_logging(_args.log_file)

    if _args.project_name:     PROJECT_NAME     = _args.project_name
    if _args.project_location: PROJECT_LOCATION = _args.project_location
    if _args.data_source:      DATA_SOURCE      = _args.data_source
    if _args.epsg_h:           EPSG_HORIZONTAL  = _args.epsg_h
    if _args.epsg_v:           EPSG_VERTICAL    = _args.epsg_v
    if _args.gcp_path:         GCP_PATH         = _args.gcp_path
    if _args.terra_path:
        TERRA_PATH       = _args.terra_path
        PROJECT_LOCATION = _args.terra_path   # terra path drives the DJI Terra project location
    if _args.ppk_path:         PPK_PATH         = _args.ppk_path

    print(f"[cfg] PROJECT_NAME     = {PROJECT_NAME!r}")
    print(f"[cfg] PROJECT_LOCATION = {PROJECT_LOCATION!r}")
    print(f"[cfg] DATA_SOURCE      = {DATA_SOURCE!r}")
    print(f"[cfg] TERRA_PATH       = {TERRA_PATH!r}")
    print(f"[cfg] PPK_PATH         = {PPK_PATH!r}")

    # Warn if launched without Data-Intake (falling back to INI values)
    _missing = [n for n, v in [
        ("--project-name", _args.project_name),
        ("--terra-path",   _args.terra_path),   # drives project location
        ("--data-source",  _args.data_source),
        ("--ppk-path",     _args.ppk_path),
    ] if not v]
    if _missing:
        _warn = (
            "WARNING: The following arguments were not passed from Data-Intake:\n\n"
            + "\n".join(f"  {m}" for m in _missing)
            + "\n\nFalling back to DJI_PARAMETERS.ini values.\n"
              "Launch from Data-Intake for automatic project configuration."
        )
        print(f"[warn] {_warn}")
        if not UNATTENDED:
            ctypes.windll.user32.MessageBoxW(
                0, _warn, "Not Launched from Data-Intake", 0x30  # MB_ICONWARNING | MB_OK
            )

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
        main.wait("ready", timeout=900)
        print("[ok] Window ready")
    except Exception:
        traceback.print_exc()
        _show_error_dialog("DJI PPK — Startup Error", traceback.format_exc())
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
        step_08_click_above_vertical_accuracy(main)
        step_09_click_arrow_left(main)
        step_10_click_below_search(main)
        embed_ppk_from_pos_txt()
        print("[ok] Automation complete")
    except Exception:
        traceback.print_exc()
        _show_error_dialog("DJI PPK — Error", traceback.format_exc())
        raise SystemExit(1)
