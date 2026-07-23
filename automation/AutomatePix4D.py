import argparse
import configparser
import inspect
import logging
import msvcrt
import os
import subprocess
import sys
import threading
import time
import ctypes
from pathlib import Path
from pywinauto import Application, Desktop
from pywinauto.findwindows import ElementNotFoundError
from pywinauto.timings import TimeoutError as PWTimeoutError
from pywinauto.keyboard import send_keys
from pywinauto import timings as _t
from pywinauto import mouse

import license_probe

_t.Timings.window_find_retry       = 0.5
_t.Timings.after_click_wait        = 0.0
_t.Timings.after_clickinput_wait   = 0.0
_t.Timings.after_sendkeys_key_wait = 0.0

_logger = logging.getLogger("pix4d")

PIX4D_EXE = r"C:\Program Files\Pix4Dmatic\PIX4Dmatic.exe"

# Populated from CLI args in main(). --dev (or --step) runs fall back to
# _DEV_DEFAULTS below so the original edit-and-run workflow still works.
PROJECT_NAME = ""
PROJECT_ROOT = ""
EPSG_CODE_H = ""
EPSG_CODE_V = ""
PROJECT_TAT = ""

_DEV_DEFAULTS = {
    "project_name": "SilverPeak",
    "project_root": r"D:\3dData\Brahma\SilverPeak\11Jun2026",
    "epsg_h":       "32611",
    "epsg_v":       "EPSG:8228",
    "tat_path":     r"D:\3dData\Brahma\SilverPeak\11Jun2026\SINGLE_TLT.csv",
}

# Set from --unattended: suppress all dialogs/popups so the script can run
# under the job agent without anything blocking on human input.
UNATTENDED = False

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")  # matches data_intake.py's Config.IMAGE_EXTENSIONS


def _count_images(root: str) -> int:
    """Recursively count image files (IMAGE_EXTENSIONS) under root."""
    count = 0
    for _dirpath, _dirs, filenames in os.walk(root):
        count += sum(1 for f in filenames if f.lower().endswith(IMAGE_EXTENSIONS))
    return count


def _wait_for_select_folder_dialog(title: str, timeout: int = 10):
    """Wait for a folder/file browse dialog using FindWindowW (bypasses UIA traversal issues)."""
    for _ in range(timeout * 2):
        time.sleep(0.5)
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
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
    _logger.info("DPI scaling confirmed at 150%")


def _find_pix4d_window(timeout: int = 60):
    """Wait for PIX4Dmatic main window to appear and return it as a WindowSpecification
    (not a raw UIAWrapper) so callers can use .child_window() etc."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            wins = Desktop(backend="uia").windows(title="PIX4Dmatic")
            if wins:
                hwnd = wins[0].handle
                app = Application(backend="uia").connect(handle=hwnd)
                return app.window(handle=hwnd)
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("PIX4Dmatic window did not appear within timeout.")


class Pix4DAutomation:
    """Drives the PIX4Dmatic UI.

    self.win is the pywinauto WindowSpecification for the PIX4Dmatic main window.
    It's resolved once in launch() and then reused by every step method — both as
    the root for child_window() anchor lookups and, in bring_to_front(), for its
    .handle — so it doesn't need to be re-found or passed around as an argument.
    self.proc is the Popen handle if this instance launched PIX4Dmatic itself
    (None if it attached to an already-running instance), used by dev-mode's
    'q' kill switch.
    """

    # Step number -> method name. Run one in isolation with:
    #   python AutomatePix4D.py --dev --step 6
    # (--dev also enables the 'q' kill switch). Omit --step to run every
    # step in order via run_all(). To add a step, just add an entry here
    # and define the matching method — run_step()/run_all() dispatch to it
    # automatically via getattr(self, name)().
    STEPS = {
        1: "check_license",
        2: "click_select_folder",
        3: "select_ppk_folder_path",
        4: "enter_project_name",
        5: "enter_path_name",
        6: "enter_epsg",
        7: "start_import",
        8: "open_settings",
        9: "open_templates",
        10: "fix_camera",
        11: "insert_targets",
        12: "start_processing",
    }

    def __init__(self):
        self.win = None
        self.proc = None

    def launch(self):
        """Step 1 — Launch PIX4Dmatic if it isn't already running."""
        try:
            self.win = _find_pix4d_window(timeout=3)
            _logger.info("PIX4Dmatic already running — skipping launch.")
            return
        except RuntimeError:
            pass

        _logger.info("Launching PIX4Dmatic...")
        self.proc = subprocess.Popen([PIX4D_EXE])
        _logger.info("Waiting for PIX4Dmatic window...")
        self.win = _find_pix4d_window(timeout=60)
        _logger.info("PIX4Dmatic window found.")

    def bring_to_front(self):
        """Restore and focus PIX4Dmatic. Uses the window's HWND directly so we never
        accidentally target a background subwindow via pywinauto handle resolution."""
        SW_RESTORE  = 9
        SW_MAXIMIZE = 3

        hwnd = self.win.handle
        _logger.info(f"PIX4Dmatic HWND: {hwnd}")

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
        _logger.info("PIX4Dmatic brought to front")

    def _click_offset_from_anchor(self, anchor_title, anchor_control_type, dx, dy, timeout=30):
        """Click at a (dx, dy) pixel offset from the center of an anchor element.
        Use this when the real target isn't in the UIA tree (e.g. hover-only or
        non-traversable controls) but a nearby stable element is."""
        caller = inspect.currentframe().f_back
        origin = f"{os.path.basename(caller.f_code.co_filename)}:{caller.f_lineno}"

        _logger.info(f"[{origin}] Looking for '{anchor_title}' anchor...")
        anchor = self.win.child_window(title=anchor_title, control_type=anchor_control_type)
        anchor.wait("visible", timeout=timeout)
        r = anchor.rectangle()
        cx = (r.left + r.right) // 2
        cy = (r.top + r.bottom) // 2
        target = (cx + dx, cy + dy)
        mouse.click(coords=target)
        _logger.info(f"[{origin}] Clicked {target} (offset from '{anchor_title}')")
        return target

    def _button_push(self, button_title, button_control_type="Button"):
        """Click the 'Apply' button."""
        apply_btn = self.win.child_window(title=button_title, control_type="Button")
        apply_btn.wait("visible", timeout=10)
        apply_btn.click_input()

    def _click_screen_edge(self, anchor_title, anchor_control_type, dy, side="left", dx=0, margin=10, timeout=30):
        """Click near a fixed screen edge or the screen's horizontal center
        (not an anchor-relative x) at the anchor's vertical position + dy.
        `side` picks "left", "right", or "center"; dx shifts from that base
        point (positive = toward screen center on "left"/"right", toward the
        right edge on "center"). Use this instead of _click_offset_from_anchor
        when the target always sits at a fixed screen position regardless of
        window position."""
        if side not in ("left", "right", "center"):
            raise ValueError(f"side must be 'left', 'right', or 'center', got {side!r}")

        caller = inspect.currentframe().f_back
        origin = f"{os.path.basename(caller.f_code.co_filename)}:{caller.f_lineno}"

        _logger.info(f"[{origin}] Looking for '{anchor_title}' anchor...")
        anchor = self.win.child_window(title=anchor_title, control_type=anchor_control_type)
        anchor.wait("visible", timeout=timeout)
        r = anchor.rectangle()
        cy = (r.top + r.bottom) // 2

        screen_width = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
        if side == "left":
            x = margin + dx
        elif side == "right":
            x = screen_width - margin - dx
        else:  # center
            x = screen_width // 2 + dx

        target = (x, cy + dy)
        mouse.click(coords=target)
        _logger.info(f"[{origin}] Clicked {target} (screen-{side}, offset from '{anchor_title}')")
        return target

    def _read_focused_value(self):
        """Read the text value of whatever control currently has UIA focus, via its
        ValuePattern. Returns None if the focused element doesn't expose one."""
        from pywinauto.uia_defines import IUIA, NoPatternInterfaceError
        from pywinauto.uia_element_info import UIAElementInfo
        from pywinauto.controls.uiawrapper import UIAWrapper

        raw_elem = IUIA().get_focused_element()
        focused = UIAWrapper(UIAElementInfo(raw_elem))
        try:
            return focused.iface_value.CurrentValue
        except NoPatternInterfaceError:
            return None

    def _tab(self, count: int = 1):
        """Press Tab `count` times to move focus, then log what ended up
        focused (via UIA read-back) so hop counts can be sanity-checked/tuned
        instead of clicking at hardcoded pixel offsets."""
        send_keys("{TAB}" * count)
        time.sleep(0.2)
        _logger.info(f"Tabbed x{count} -> focused value: {self._read_focused_value()!r}")

    @staticmethod
    def _set_clipboard_text(text: str):
        """Put `text` on the Windows clipboard as CF_UNICODETEXT.

        GlobalAlloc/GlobalLock return pointers — restype must be set to
        c_void_p or ctypes truncates them to 32-bit on 64-bit Python, which
        silently corrupts the handle and makes the whole write a no-op.
        """
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        data = text.encode("utf-16-le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            _logger.warning("GlobalAlloc failed — clipboard text not set.")
            return
        ptr = kernel32.GlobalLock(h_mem)
        if not ptr:
            _logger.warning("GlobalLock failed — clipboard text not set.")
            return
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h_mem)

        if not user32.OpenClipboard(0):
            _logger.warning("OpenClipboard failed — clipboard text not set.")
            return
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                _logger.warning("SetClipboardData failed — clipboard text not set.")
        finally:
            user32.CloseClipboard()

    def _enter_text(self, text: str, paste: bool = False):
        """Put `text` into whatever field currently has focus — either by
        typing via send_keys, or (paste=True) by putting it on the clipboard
        and pasting with Ctrl+V."""
        if paste:
            self._set_clipboard_text(text)
            send_keys("^v")
        else:
            send_keys(text, with_spaces=True)

    def _type_and_verify(self, text, field_label, on_failure=None, paste=False):
        """Enter `text` into whatever field currently has focus (relies on the
        field auto-highlighting its existing text on click, same as before),
        then read the focused control's value back via UIA and log whether it
        matches. If verification fails: delete what was typed, optionally run
        `on_failure` (e.g. to fix up UI state first), then retry once.
        paste=True enters the text via clipboard copy/paste instead of typing."""
        self._enter_text(text, paste=paste)
        time.sleep(0.2)
        actual = self._read_focused_value()
        if actual == text:
            _logger.info(f"Verified '{field_label}' = '{text}'")
            return True

        _logger.warning(f"Could not verify '{field_label}': expected '{text}', read back {actual!r}")
        send_keys(f"{{BACKSPACE {len(text)}}}")  # delete exactly what we just typed, not the whole field

        if on_failure is not None:
            on_failure()
            time.sleep(0.5)  # let the recovery click (e.g. checkbox) land before retyping

        self._enter_text(text, paste=paste)
        time.sleep(0.2)
        actual = self._read_focused_value()
        if actual == text:
            _logger.info(f"Verified '{field_label}' = '{text}' after retry")
            return True
        _logger.warning(f"Still could not verify '{field_label}' after retry: expected '{text}', read back {actual!r}")
        return False

    def get_license(self):
        """Open the Organizations & licenses dialog and make sure a PIX4Dmatic
        license with a seat is active.

        Row order in the dialog varies between runs, so the old fixed-offset
        clicks (dy=200/dy=300 from the title) picked whatever license happened
        to sit at that offset. license_probe reads the actual rows instead —
        UIA licenseItem rects for geometry, OCR per row for product/seats —
        and clicks the right one, confirming via the Apply button that
        materializes when a different license is chosen. Outcomes:

          SELECTED — a PIX4Dmatic license is active (already was, or clicked +
              applied); the dialog is closed via its X (Cancel could revert).
          NO_SEATS — no active PIX4Dmatic license and every one shows 0
              seats: run the device-manager Reclaim flow (same clicks as the
              old code) to free this machine's stale seat.
          SCAN_FAILED / CLICK_UNCONFIRMED — raise, so the run fails loudly
              with the row readout in the log instead of activating a random
              license the way a blind click would.
        """
        license_probe.set_emitter(_logger.info)

        self.win.child_window(title="S", control_type="CheckBox").click_input()
        self.win.child_window(title="Organizations  licenses", control_type="MenuItem").click_input()
        time.sleep(5)

        status = license_probe.select_available_license(self.win)
        _logger.info(f"License selection: {status}")

        if status == "SELECTED":
            if not license_probe.close_license_dialog(self.win):
                _logger.warning("License dialog did not close after selection.")
            return status

        if status == "NO_SEATS":
            self._reclaim_seat()
            return status

        raise RuntimeError(
            f"License selection failed ({status}) — see the row readout above; "
            "run automation\\license_probe.py with the dialog open to diagnose.")

    def _reclaim_seat(self):
        """No PIX4Dmatic seats available anywhere: open the device manager and
        reclaim this machine's seat (same click sequence the old flow used —
        the Reclaim button when it's in the UIA tree, offset clicks from the
        'Device manager' title otherwise)."""
        device_btn = self.win.child_window(title="Go to device manager", control_type="Button")
        device_btn.wait("visible", timeout=10)
        device_btn.click_input()
        time.sleep(2.5)
        self._click_offset_from_anchor("Device manager", "Text", dx=0, dy=150)
        reclaim_btn = self.win.child_window(title="Reclaim", control_type="Button")
        if reclaim_btn.exists():
            time.sleep(.5)
            reclaim_btn.click_input()
        else:
            self._click_offset_from_anchor("Device manager", "Text", dx=0, dy=185)

    def check_license(self):
        """Step 1 — ensure a usable PIX4Dmatic license is active. Only opens
        the Organizations dialog when the app shows a license nag (the 'Go to
        Organizations  Licenses' popup or the 'Start trial' screen)."""
        _logger.info("Checking license status...")
        popup = self.win.child_window(title="Go to Organizations  Licenses", control_type="Button")
        trial_btn = self.win.child_window(title="Start trial", control_type="Button")

        if popup.exists():
            send_keys("{ESC}")
            self.get_license()
        elif trial_btn.exists():
            _logger.warning("Found 'Start trial' button, proceeding with license activation.")
            self.get_license()
        else:
            _logger.info("No license nag detected — license is present.")

    
    def click_select_folder(self, max_attempts: int = 5):
        """Step 2 — Click the 'Select folder...' button.
        Not present in the UIA tree until the mouse hovers over it, so we anchor off the
        'Start trial' button instead (measured: anchor (3552,91) -> target center (770,616)).
        The click itself is blind (offset from an anchor, not the real element), so it can
        miss — keep re-clicking until the 'Select Folder' browse dialog actually appears
        (the same check step 3 relies on) instead of assuming one click landed."""
        target = None
        for attempt in range(1, max_attempts + 1):
            target = self._click_offset_from_anchor("PIX4Dcloud", "Button", dx=-2900, dy=525)
            _logger.info(f"'Select folder...' clicked (attempt {attempt}/{max_attempts}).")
            if _wait_for_select_folder_dialog("Select Folder", timeout=3) is not None:
                return target
            _logger.warning(f"'Select Folder' dialog not detected after attempt {attempt} — retrying click.")
        _logger.warning(f"'Select Folder' dialog still not detected after {max_attempts} attempts.")
        return target

    def select_ppk_folder_path(self):
        """Step 3 — Enter the PPK project folder path into the browse dialog.
        Same append behavior as enter_path_name: <PROJECT_ROOT>\\PPK, no separate
        PPK path variable needed."""
        ppk_path = str(Path(PROJECT_ROOT) / "PPK")
        _logger.info("Waiting for folder browse dialog...")
        file_dlg = _wait_for_select_folder_dialog("Select Folder")
        if file_dlg is None:
            raise RuntimeError("'Select Folder' dialog did not appear within 10s")

        _send_path_to_browse_dialog(file_dlg, ppk_path)
        send_keys("{ENTER}")

        btn = file_dlg.child_window(title="Select Folder", control_type="Button")
        btn.wait("enabled", timeout=5)
        btn.click_input()
        _logger.info(f"Set PPK folder path '{ppk_path}'")

    def enter_project_name(self):
        """Step 4 — Click the Project name field (edit box not in UIA tree — offset
        from the 'Project name' label, 25px down and 1px right) and type PROJECT_NAME,
        then verify it landed via UIA read-back."""
        self._click_offset_from_anchor("Project name", "Text", dx=8, dy=1)
        self._type_and_verify(PROJECT_NAME, "Project name")

    def enter_path_name(self):
        """Step 5 — Click the Path field (offset from the 'Path' label, same as
        Project name) and type <PROJECT_ROOT>\\Pix4d, then verify it landed via
        UIA read-back. Data intake already creates that 'Pix4d' subfolder under
        the project root (same name for every sensor type)."""
        pix4d_path = str(Path(PROJECT_ROOT) / "Pix4d")
        self._click_offset_from_anchor("Path", "Text", dx=8, dy=28)
        self._type_and_verify(pix4d_path, "Path")

    def _enter_epsg_fields(self):
        """EPSG horizontal + vertical entry. Extracted out of enter_epsg so it
        can be run twice — this search-combo UI is flaky enough that a single
        pass sometimes doesn't take."""
        self._click_offset_from_anchor("Known CRS", "RadioButton", dx=0, dy=85)
        time.sleep(0.5)
        self._type_and_verify(EPSG_CODE_H, "EPSG", paste=True)
        time.sleep(0.5)
        self._click_offset_from_anchor("Known CRS", "RadioButton", dx=0, dy=145)
        time.sleep(0.5)
        self._click_offset_from_anchor("Geoid", "Text", dx=0, dy=-85)
        time.sleep(0.5)
        self._enter_text(EPSG_CODE_V)  # typed, not pasted — this search combo only
        # filters/opens its dropdown on individual keystrokes; a bulk Ctrl+V paste
        # delivers the whole string in one shot and the dropdown just closes on it
        time.sleep(1)

    def _enter_epsg_fieldsactual(self, click_default_crs):
        """EPSG horizontal + vertical entry. Extracted out of enter_epsg so it
        can be run twice — this search-combo UI is flaky enough that a single
        pass sometimes doesn't take."""
        self._click_offset_from_anchor("Known CRS", "RadioButton", dx=0, dy=85)
        time.sleep(0.5)
        self._type_and_verify(EPSG_CODE_H, "EPSG", paste=True)
        time.sleep(0.5)
        self._click_offset_from_anchor("Known CRS", "RadioButton", dx=0, dy=145)
        time.sleep(0.5)
        self._click_offset_from_anchor("Geoid", "Text", dx=0, dy=-85)
        time.sleep(0.5)
        self._enter_text(EPSG_CODE_V)  # typed, not pasted — this search combo only
        # filters/opens its dropdown on individual keystrokes; a bulk Ctrl+V paste
        # delivers the whole string in one shot and the dropdown just closes on it
        time.sleep(1)
        self._click_offset_from_anchor("Geoid", "Text", dx=0, dy=-45)
        time.sleep(0.5)
        self._click_offset_from_anchor("Geoid", "Text", dx=0, dy=30)
        self._type_and_verify('GEOID18', "EPSG")
        self._click_offset_from_anchor("Geoid", "Text", dx=0, dy=75)
        click_default_crs()

    def enter_epsg(self):
        """Step 6 — Click the EPSG field (offset from the 'Path' label) and type
        EPSG_CODE, verifying it landed via UIA read-back. If verification fails,
        click the 'Default CRS' checkbox (a real UIA element, no anchor offset
        needed) as a recovery step before retrying the type once."""
        crs_checkbox_fail = False

        # def _click_default_crs_fail():
        #     nonlocal crs_checkbox_fail
        #     crs_checkbox_fail = True
        #     checkbox = self.win.child_window(title="Default CRS", control_type="CheckBox")
        #     checkbox.wait("visible", timeout=10)
        #     checkbox.click_input()
        #     _logger.info("Clicked 'Default CRS' checkbox with fail.")
        #     # clicking the checkbox steals focus — re-click the EPSG field so the
        #     # retype in _type_and_verify lands on the right target, not the checkbox
        #     self._click_offset_from_anchor("Path", "Text", dx=8, dy=365)

        def _click_default_crs():
            checkbox = self.win.child_window(title="Default CRS", control_type="CheckBox")
            checkbox.wait("visible", timeout=10)
            checkbox.click_input()
            _logger.info("Clicked 'Default CRS' checkbox.")

        self._click_offset_from_anchor("Path", "Text", dx=8, dy=195)
        _click_default_crs()

        self._enter_epsg_fields()
        time.sleep(3)
        self._enter_epsg_fieldsactual(_click_default_crs)

        



        if crs_checkbox_fail:
            _click_default_crs()

        time.sleep(1)
    
    def start_import(self):
        """Step 7 — Click the 'Start import' button (offset from the 'Path' label)."""
        checkbox = self.win.child_window(title="Start", control_type="Button")
        checkbox.wait("visible", timeout=10)
        checkbox.click_input()
        _logger.info("'Start import' clicked.")

    def open_settings(self):
        """Step 8 - Click the 'Settings' button (far-left edge of the screen,
        at the '2D' checkbox's height + offset)."""
        self._click_screen_edge("2D", "CheckBox", dy=85)

    def open_templates(self):
        """Step 9 - Click the 'Template' fields. Waits first — sleep duration
        scales with the number of images in <PROJECT_ROOT>/PPK (count / 250
        seconds), since more images means PIX4D needs longer before these
        fields are ready to click."""
        ppk_folder = Path(PROJECT_ROOT) / "PPK"
        image_count = _count_images(str(ppk_folder))
        wait_secs = image_count / 250
        _logger.info(
            f"Found {image_count} image(s) in '{ppk_folder}' — waiting {wait_secs:.1f}s before opening templates."
        )
        time.sleep(wait_secs)

        self._click_screen_edge("2D", "CheckBox", dy=98, dx=100)
        self._click_screen_edge("2D", "CheckBox", dy=120, dx=100)
    
    def fix_camera(self):
        """Step 10 - Clicks camera field, and selects ellipsoidal height"""
        self._click_screen_edge("2D", "CheckBox", dy=1929, side="right", dx=15)
        time.sleep(0.5)
        self._click_offset_from_anchor("Known CRS", "RadioButton", dx=0, dy=190)
        time.sleep(0.5)
        self._type_and_verify('ellipsoidal', "Vertical coordinate reference system")
        time.sleep(0.5)
        self._click_offset_from_anchor("Known CRS", "RadioButton", dx=0, dy=240)
        time.sleep(0.5)
        self._button_push("Apply")

    def insert_targets(self):
        """Step 11 - Inserts target file path into the camera field. Same
        browse-dialog prompt sequence as select_ppk_folder_path: click to open
        the dialog, wait for it, send PROJECT_TAT, confirm."""
        self._click_screen_edge("2D", "CheckBox", dy=1890, side="center", dx=-40)
        time.sleep(0.5)
        self._button_push("Select from disk")

        _logger.info("Waiting for folder browse dialog...")
        file_dlg = _wait_for_select_folder_dialog("Open")
        if file_dlg is None:
            raise RuntimeError("'Select Folder' dialog did not appear within 10s")

        _send_path_to_browse_dialog(file_dlg, PROJECT_TAT)
        send_keys("{ENTER}")
        # Enter alone submits and closes this Open dialog (unlike the folder
        # browser used for PPK) — no separate confirm button to click after.
        _logger.info(f"Set target path '{PROJECT_TAT}'")
        time.sleep(0.5)
        self._click_offset_from_anchor("Column format", "Text", dx=0, dy=50)
        self._click_offset_from_anchor("Column format", "Text", dx=0, dy=110)
        self._button_push("Apply")
        

    def start_processing(self):
        """Step 12 - Click the 'Start processing' button """
        self._button_push("Start")

    # --- end-of-run save + close (run via --save-close, not as a numbered step) ---
    #
    # These are a SEPARATE phase from the numbered import/processing steps above.
    # AutomatePix4D can't tell when Pix4Dmatic has finished processing — after
    # start_processing() the payload exits and Pix4D keeps working on its own.
    # The agent (processors/pix4dmatic.py) is what watches for completion (the
    # orthomosaic export landing on disk); once it sees that, it re-invokes this
    # exe with --save-close, which reuses the same window-focus + pywinauto
    # connect the import flow uses to save the project and shut Pix4D down.

    def connect(self):
        """Attach to an already-running PIX4Dmatic without launching a new one.
        Used by --save-close: processing is already done, so there is always a
        live instance to save — if there isn't, there is nothing to do and
        _find_pix4d_window raises."""
        _logger.info("[save-close] connect(): looking for a running PIX4Dmatic window...")
        self.win = _find_pix4d_window(timeout=10)
        _logger.info(f"[save-close] connect(): attached to PIX4Dmatic (HWND {self.win.handle}).")

    def save_and_close(self, no_close: bool = False):
        """--save-close entry point: focus the window, save the project, then
        (unless no_close) close the app. Focus + connect are the 'core' bits
        reused from the import flow (bring_to_front / _find_pix4d_window).
        no_close=True (from --no-close) saves but leaves Pix4D open — handy for
        running the save test repeatedly without reopening the project."""
        _logger.info("[save-close] ===== SAVE + CLOSE START =====")
        self.bring_to_front()
        _logger.info("[save-close] window brought to front; saving project...")
        self.save_project()
        if no_close:
            _logger.info("[save-close] --no-close set: leaving PIX4Dmatic open (save only).")
            _logger.info("[save-close] ===== SAVE ONLY DONE =====")
            return
        _logger.info("[save-close] closing PIX4Dmatic...")
        self.close_pix4d()
        _logger.info("[save-close] ===== SAVE + CLOSE DONE =====")

    def _confirm_save_dialog(self, timeout: int = 3):
        """Best-effort: if a save prompt appeared (either a 'Save changes
        before closing?' message box or a 'Save As' browse dialog), click the
        affirmative button that keeps the work. The project Path was already
        set in enter_path_name(), so a plain Ctrl+S usually saves silently and
        this is a no-op — it only acts when a dialog is actually present."""
        for title in ("Save", "Yes", "Save As"):
            try:
                btn = self.win.child_window(title=title, control_type="Button")
                if btn.exists(timeout=timeout / 3):
                    _logger.info(f"Confirming save dialog via '{title}' button.")
                    btn.click_input()
                    time.sleep(0.5)
                    return
            except Exception:
                continue

    def save_project(self):
        """Save the project. Prefer a real 'Save' button in the UIA tree; if it
        isn't present, fall back to the Ctrl+S hotkey. Either way, confirm any
        save/overwrite dialog that pops up so work isn't lost."""
        self.bring_to_front()
        time.sleep(0.5)
        save_btn = self.win.child_window(title="Save", control_type="Button")
        if save_btn.exists():
            _logger.info("[save-close] save_project(): 'Save' button found in the UIA tree — clicking it.")
            save_btn.click_input()
        else:
            _logger.info("[save-close] save_project(): no 'Save' button in the UIA tree — sending Ctrl+S hotkey.")
            send_keys("^s")
        time.sleep(1)
        self._confirm_save_dialog()
        _logger.info("[save-close] save_project(): save issued (button or Ctrl+S) and any dialog confirmed.")

    def close_pix4d(self):
        """Close PIX4Dmatic. Posts WM_CLOSE to the main window for a clean
        shutdown, then confirms any 'save changes?' prompt so nothing is lost.
        Only called after the agent has confirmed processing is complete."""
        hwnd = self.win.handle
        WM_CLOSE = 0x0010
        _logger.info(f"[save-close] close_pix4d(): posting WM_CLOSE to PIX4Dmatic (HWND {hwnd})...")
        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(2)
        # A 'save changes before closing?' prompt may still appear — keep the work.
        self._confirm_save_dialog()
        # If this instance launched PIX4Dmatic, give it a moment to exit cleanly.
        if self.proc is not None:
            try:
                self.proc.wait(timeout=30)
                _logger.info("[save-close] close_pix4d(): PIX4Dmatic process exited cleanly.")
            except Exception:
                _logger.info("[save-close] close_pix4d(): PIX4Dmatic still shutting down after close request.")
        else:
            # Attached to a pre-existing instance (the normal agent case): confirm
            # the window is actually gone so the log says whether close worked.
            time.sleep(1)
            try:
                gone = not self.win.exists()
            except Exception:
                gone = True
            if gone:
                _logger.info("[save-close] close_pix4d(): PIX4Dmatic window is gone — close confirmed.")
            else:
                _logger.warning("[save-close] close_pix4d(): PIX4Dmatic window still present after WM_CLOSE "
                                "(a dialog may be blocking, or the title/handle changed).")

    # def open_templates(self):
    #     """Step 10 - Click the 'Settings' button (far-left edge of the screen,
    #     at the '2D' checkbox's height + offset)."""
    #     self._click_screen_left("2D", "CheckBox", dy=85)

    def run_step(self, step_num: int):
        name = self.STEPS[step_num]
        _logger.info(f"Running only step {step_num} ({name})")
        getattr(self, name)()

    def run_steps(self, step_nums):
        for step_num in step_nums:
            self.run_step(step_num)

    def run_all(self):
        for step_num in sorted(self.STEPS):
            getattr(self, self.STEPS[step_num])()


def _watch_for_quit(automation: Pix4DAutomation):
    """Dev mode: press 'q' at any time to kill this script (and PIX4Dmatic)."""
    while True:
        if msvcrt.kbhit() and msvcrt.getch().lower() == b"q":
            _logger.info("'q' pressed — exiting dev session.")
            if automation.proc is not None and automation.proc.poll() is None:
                automation.proc.kill()
            os._exit(0)
        time.sleep(0.1)


def _parse_step_list(raw: str) -> list:
    parts = raw.split(",")
    if not all(p.isdigit() and int(p) in Pix4DAutomation.STEPS for p in parts):
        valid = ", ".join(f"{n} ({name})" for n, name in sorted(Pix4DAutomation.STEPS.items()))
        raise SystemExit(f"--step must be a comma-separated list of: {valid}")
    return [int(p) for p in parts]


def main():
    global PROJECT_NAME, PROJECT_ROOT, EPSG_CODE_H, EPSG_CODE_V, PROJECT_TAT, PIX4D_EXE, UNATTENDED

    parser = argparse.ArgumentParser(description="PIX4Dmatic automation")
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--project-root", default=None,
                        help="date folder: images imported from <root>\\PPK, project created in <root>\\Pix4d")
    parser.add_argument("--epsg-h",       default=None)
    parser.add_argument("--epsg-v",       default=None)
    parser.add_argument("--tat-path",     default=None, help="targets (TAT) csv, imported as-is")
    parser.add_argument("--exe-path",     default=None, help="PIX4Dmatic.exe location override")
    parser.add_argument("--log-file",     default=None)
    parser.add_argument("--unattended",   action="store_true",
                        help="suppress all dialogs/popups; errors go to stderr + exit code")
    parser.add_argument("--dev",          action="store_true",
                        help="dev mode: 'q' kill switch, missing args fall back to _DEV_DEFAULTS")
    parser.add_argument("--step",         default=None,
                        help="comma-separated step numbers to run in isolation (also uses _DEV_DEFAULTS)")
    parser.add_argument("--save-close",   action="store_true",
                        help="save the open project and close PIX4Dmatic, then exit "
                             "(no import/processing; the agent runs this once it detects "
                             "processing is complete). Requires no project arguments. "
                             "TEST IT standalone: open Pix4D on a finished project and run "
                             "AutomatePix4D.py --save-close.")
    parser.add_argument("--no-close",     action="store_true",
                        help="with --save-close: save the project but DON'T close "
                             "Pix4D — lets you re-run the save test without reopening it.")
    args = parser.parse_args()

    UNATTENDED = args.unattended
    if args.exe_path:
        PIX4D_EXE = args.exe_path

    # --save-close is a standalone phase run against an already-open project, so
    # it needs none of the project arguments the import flow does.
    if not args.save_close:
        values = {
            "project_name": args.project_name,
            "project_root": args.project_root,
            "epsg_h":       args.epsg_h,
            "epsg_v":       args.epsg_v,
            "tat_path":     args.tat_path,
        }
        if args.dev or args.step:
            values = {k: (v if v is not None else _DEV_DEFAULTS[k]) for k, v in values.items()}
        missing = [f"--{k.replace('_', '-')}" for k, v in values.items() if v is None]
        if missing:
            raise SystemExit(
                f"Missing required arguments: {', '.join(missing)} (or pass --dev to use _DEV_DEFAULTS)"
            )
    else:
        values = {k: (getattr(args, k) or "") for k in
                  ("project_name", "project_root", "epsg_h", "epsg_v")}
        values["tat_path"] = args.tat_path or ""

    PROJECT_NAME = values["project_name"]
    PROJECT_ROOT = values["project_root"]
    EPSG_CODE_H  = values["epsg_h"]
    EPSG_CODE_V  = values["epsg_v"]
    PROJECT_TAT  = values["tat_path"]

    only_steps = _parse_step_list(args.step) if args.step else None
    if only_steps is not None:
        names = ", ".join(f"{n} ({Pix4DAutomation.STEPS[n]})" for n in only_steps)
        print(f"[--step] Running only step(s): {names}")

    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, mode="a", encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    _logger.info(
        f"[cfg] PROJECT_NAME={PROJECT_NAME!r} PROJECT_ROOT={PROJECT_ROOT!r} "
        f"EPSG_H={EPSG_CODE_H!r} EPSG_V={EPSG_CODE_V!r} TAT={PROJECT_TAT!r}"
    )

    automation = Pix4DAutomation()
    if args.dev:
        _logger.info("Dev mode enabled — press 'q' at any time to quit.")
        threading.Thread(target=_watch_for_quit, args=(automation,), daemon=True).start()

    if args.save_close:
        # Save-and-close phase: attach to the already-running instance (never
        # launch a fresh one) and save + close. No DPI check — this uses the
        # window handle, the Ctrl+S hotkey and a by-title Save button, none of
        # which depend on the 150% offset calibration the import clicks need.
        _logger.info(f"Running --save-close (no_close={args.no_close}): "
                     "saving the open PIX4Dmatic project"
                     f"{' (leaving it open)' if args.no_close else ' and closing it'}.")
        try:
            automation.connect()
            automation.save_and_close(no_close=args.no_close)
            _logger.info("[save-close] finished OK.")
        except Exception:
            _logger.exception("[save-close] FAILED — see traceback above.")
            raise
        return

    _check_dpi_150()
    automation.launch()
    automation.bring_to_front()
    time.sleep(2)

    if only_steps is not None:
        automation.run_steps(only_steps)
    else:
        automation.run_all()


if __name__ == "__main__":
    main()
