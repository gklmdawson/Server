"""Probe + selector for the PIX4Dmatic 'Organizations and licenses' dialog.

What probing on the rig established (2026-07-23):

* Each license row IS in the raw UIA tree: a nameless Group with
  auto_id="licenseItem" and an exact bounding rect (81px tall on the 4K/150%
  rig). The tree returns them out of order — the same order instability that
  broke get_license()'s blind dy=200/dy=300 offset clicks — but sorting by
  rect top fixes that. The rows expose NO text, so their content is read by
  OCR'ing each row's screenshot crop (winocr = Windows' built-in OCR engine,
  `py -m pip install winocr`; pytesseract works as a fallback).
* The selection-blue fill marks the ACTIVE license — it does NOT move when a
  row is merely clicked. Clicking a different license makes an 'Apply'
  button materialize in the dialog's button box; Apply is what commits the
  switch. So: "Apply appeared" is the click confirmation, and the blue fill
  is only meaningful before clicking (which license is active now?) and
  after Apply.
* An active license holds its own seat, so the row that is currently active
  can read '0/1 seat(s) available' — a selected row's seat count must NOT
  disqualify it.
* 0-seat rows carry an orange warning triangle. ClearType subpixel fringing
  on the gray subtitle text also produces scattered orange-ish pixels, so
  triangle detection uses local density (a solid 16x16 block), not a global
  count.

Run on the workstation WHILE the dialog is open (profile icon ->
Organizations & licenses). Default mode (no flags) is --rows.

  --rows     read the license list and print one line per row: product,
             seats, active/warning flags, rect, raw OCR text. Read-only.
  --select   pick the right PIX4Dmatic row, click it, confirm via the Apply
             button appearing, click Apply (suppress with --no-apply).
  --close    close the dialog via its X button (Cancel might revert the
             choice, so the X is used). Combine as --select --close.
  --hover    diagnostic: hit-test sweep of the band (ElementFromPoint).
  --dump     diagnostic: raw UIA subtree clipped to the dialog area.
  --pixels   diagnostic: per-slab color counts across the band.
  --keys     diagnostic: arrow-key focus scan. NOTE: moves the selection.

--select exit codes (what the agent / AutomatePix4D branches on):
  0  a PIX4Dmatic license with a seat is active (already was, or clicked +
     applied)
  3  no PIX4Dmatic license is active and every one shows 0 seats
     (route to the device-manager / Reclaim flow, or retry later)
  4  rows could not be read (no licenseItem elements, or OCR never yielded
     a PIX4Dmatic row) — run the diagnostics and read the raw OCR text
  5  rows read fine but the click never registered (no Apply button, no
     highlight) after two attempts — verify on screen

AutomatePix4D.get_license() imports select_available_license /
close_license_dialog from here; set_emitter() routes this module's output
into its logger so payload logs capture the row table.

Known limitation: rows that would need scrolling (more licenses than fit the
list) aren't handled — QML only materializes visible delegates. No org on
our rigs has that many licenses today.
"""

import argparse
import ctypes
import re
import sys
import time

# Declare STA before pywinauto is imported (it checks for this attribute when
# comtypes is already loaded) — silences its COM threading-mode warning
# without changing behavior.
sys.coinit_flags = 2

import comtypes.client

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen.UIAutomationClient import (  # noqa: E402
    CUIAutomation,
    IUIAutomation,
    IUIAutomationLegacyIAccessiblePattern,
    IUIAutomationSelectionItemPattern,
    IUIAutomationValuePattern,
    tagPOINT,
)
from pywinauto import Desktop, mouse  # noqa: E402
from pywinauto.keyboard import send_keys  # noqa: E402

try:
    from PIL import Image, ImageGrab, ImageOps
except ImportError:  # Pillow is required for --rows/--select/--pixels
    Image = ImageGrab = ImageOps = None

_uia = comtypes.client.CreateObject(CUIAutomation._reg_clsid_, interface=IUIAutomation)

# Same table as UIInspect.py
CTRL_NAMES = {
    50000: "Button",      50002: "CheckBox",   50003: "ComboBox",
    50004: "Edit",        50007: "ListItem",   50008: "List",
    50009: "Menu",        50011: "MenuItem",   50013: "RadioButton",
    50018: "Tab",         50019: "TabItem",    50020: "Text",
    50021: "ToolBar",     50023: "Tree",       50024: "TreeItem",
    50025: "Custom",      50026: "Group",      50032: "Window",
    50033: "Pane",        50037: "TitleBar",
}

# UIA pattern ids (stable constants from UIAutomationClient.h)
UIA_ValuePatternId             = 10002
UIA_SelectionItemPatternId     = 10010
UIA_LegacyIAccessiblePatternId = 10018

# OCR parsing. 'PIX4D' tolerates the classic I/l/1 confusions; seat counts
# are plain digits around a slash ('0/1 seat(s) available'; the warning
# triangle tends to OCR as a stray 'A').
PRODUCT_RE = re.compile(r"P[Il1]X4D\s*([A-Za-z]+)", re.I)
SEATS_RE   = re.compile(r"(\d+)\s*/\s*(\d+)")

ROW_AUTO_ID = "licenseItem"

# Peak 16x16 orange density that counts as a real warning triangle. Measured
# expectation: solid icon core ~150-220, ClearType fringe clusters < ~40.
# The per-row warn= score is printed so this can be re-tuned from a real run.
WARN_SCORE_MIN = 100

# Library output goes through _emit so AutomatePix4D can route it into its
# logger (payload --log-file captures logging, not print).
_emit = print


def set_emitter(fn):
    """Route this module's output (row tables, results) through `fn`."""
    global _emit
    _emit = fn


# ── UIA plumbing ──────────────────────────────────────────────────


def _pattern(elem, pattern_id, iface):
    """Best-effort fetch of a UIA pattern interface from an element."""
    try:
        pat = elem.GetCurrentPattern(pattern_id)
        if pat:
            return pat.QueryInterface(iface)
    except Exception:
        pass
    return None


class ElemInfo:
    """Snapshot of one UIA element: identity, rect, and selection state."""

    def __init__(self, elem):
        self.name    = elem.CurrentName or ""
        self.ctrl    = CTRL_NAMES.get(elem.CurrentControlType, f"type:{elem.CurrentControlType}")
        self.cls     = elem.CurrentClassName or ""
        self.auto_id = elem.CurrentAutomationId or ""
        r = elem.CurrentBoundingRectangle
        self.rect = (r.left, r.top, r.right, r.bottom)

        self.selected = None      # SelectionItemPattern.IsSelected, if exposed
        self.legacy_state = None  # LegacyIAccessible state bits, if exposed
        self.value = None         # ValuePattern value, if exposed
        p = _pattern(elem, UIA_SelectionItemPatternId, IUIAutomationSelectionItemPattern)
        if p is not None:
            try:
                self.selected = bool(p.CurrentIsSelected)
            except Exception:
                pass
        p = _pattern(elem, UIA_LegacyIAccessiblePatternId, IUIAutomationLegacyIAccessiblePattern)
        if p is not None:
            try:
                self.legacy_state = p.CurrentState
            except Exception:
                pass
        p = _pattern(elem, UIA_ValuePatternId, IUIAutomationValuePattern)
        if p is not None:
            try:
                self.value = p.CurrentValue
            except Exception:
                pass

    def __str__(self):
        parts = [f'name="{self.name}"', f"ctrl={self.ctrl}", f"rect={self.rect}"]
        if self.cls:
            parts.append(f"class={self.cls}")
        if self.auto_id:
            parts.append(f"auto_id={self.auto_id}")
        if self.selected is not None:
            parts.append(f"selected={self.selected}")
        if self.legacy_state is not None:
            parts.append(f"legacy_state=0x{self.legacy_state:x}")
        if self.value:
            parts.append(f'value="{self.value}"')
        return "  ".join(parts)


def _collect(root_elem, keep, max_nodes=8000, max_depth=25):
    """Raw-view walk of a UIA subtree, returning ElemInfos matching `keep`.
    The raw view matters: pywinauto's control view filters out the nameless
    licenseItem Groups entirely."""
    walker = _uia.RawViewWalker
    out = []
    visited = [0]

    def _walk(elem, depth):
        if depth > max_depth or visited[0] > max_nodes:
            return
        visited[0] += 1
        try:
            info = ElemInfo(elem)
            if keep(info):
                out.append(info)
        except Exception:
            pass
        try:
            child = walker.GetFirstChildElement(elem)
        except Exception:
            return
        while child is not None:
            _walk(child, depth + 1)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

    _walk(root_elem, 0)
    return out


def _drill_down(elem, x, y, depth=0):
    """Recurse into raw-view children to the deepest element containing (x, y).
    Same approach as UIInspect.py."""
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


def element_at(x, y):
    """Hit-test (x, y) and return an ElemInfo for the deepest element there."""
    try:
        pt = tagPOINT()
        pt.x, pt.y = x, y
        elem = _uia.ElementFromPoint(pt)
        elem = _drill_down(elem, x, y)
        return ElemInfo(elem)
    except Exception:
        return None


def focused_element():
    """ElemInfo for whatever currently has UIA focus (used by --keys)."""
    try:
        return ElemInfo(_uia.GetFocusedElement())
    except Exception:
        return None


# ── Locating the dialog ───────────────────────────────────────────


def _find_pix4d_window(timeout=10):
    """Same lookup AutomatePix4D uses: first top-level 'PIX4Dmatic' window,
    wrapped as a WindowSpecification so child_window() works."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            wins = Desktop(backend="uia").windows(title="PIX4Dmatic")
            if wins:
                return Desktop(backend="uia").window(handle=wins[0].handle)
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit("PIX4Dmatic window not found — is it running?")


def _find_dialog_popup(win):
    """The SelectLicenseDialog popup container (QQuickPopupItem), or None."""
    root = _uia.ElementFromHandle(win.handle)
    hits = _collect(root, lambda i: "SelectLicenseDialog" in i.auto_id
                    and i.cls == "QQuickPopupItem")
    return hits[0] if hits else None


def _row_elements(win):
    """The licenseItem row rects, sorted top-to-bottom (the raw tree returns
    them in arbitrary order)."""
    root = _uia.ElementFromHandle(win.handle)
    rows = _collect(root, lambda i: i.auto_id == ROW_AUTO_ID)
    rows.sort(key=lambda i: i.rect[1])
    return rows


class Band:
    """Screen region the license rows live in, for the diagnostic sweeps —
    derived from live rects, never hardcoded pixels."""

    def __init__(self, anchor_rect, bottom, x_columns, right):
        self.anchor = anchor_rect          # 'Organizations and licenses' title rect
        self.top = anchor_rect.bottom + 10
        self.bottom = bottom
        self.x_columns = x_columns
        self.left = anchor_rect.left - 20
        self.right = right


def _dialog_band(win):
    """Anchor on the dialog title (a known UIA element — it's get_license()'s
    old click anchor); clamp bottom/right to real dialog geometry."""
    anchor = win.child_window(title="Organizations and licenses", control_type="Text")
    anchor.wait("visible", timeout=10)
    a = anchor.rectangle()

    bottom = a.bottom + 700
    right = a.left + 820
    try:
        btn = win.child_window(title="Go to device manager", control_type="Button")
        if btn.exists(timeout=1):
            bottom = btn.rectangle().top - 8
    except Exception:
        pass
    popup = _find_dialog_popup(win)
    if popup is not None:
        right = popup.rect[2] - 6          # stay inside the dialog: pixels beyond
        bottom = min(bottom, popup.rect[3] - 6)  # its edge are the dimmed page behind

    x_columns = (a.left + 40, a.left + 130, a.left + 300)
    return Band(a, bottom, x_columns, right)


# ── Pixel classifiers ─────────────────────────────────────────────


def _is_blue(p):
    r, g, b = p[:3]
    return b > 120 and b > r + 30 and g > r          # PIX4D selection highlight

def _is_orange(p):
    r, g, b = p[:3]
    return r > 190 and 110 < g < 200 and b < 110     # warning triangle on 0-seat rows

def _is_white(p):
    return min(p[:3]) > 200                          # row title text


def _ratio(img, pred, step=3):
    px = img.load()
    n = total = 0
    for y in range(0, img.height, step):
        for x in range(0, img.width, step):
            total += 1
            if pred(px[x, y]):
                n += 1
    return (n / total) if total else 0.0


def _warning_score(subtitle_img):
    """Peak orange count over any 16x16 window (step-1 scan via an 8px cell
    grid). The ⚠ icon is a solid orange block; ClearType fringing on light
    text only yields scattered orange-ish pixels, so density separates them
    where a global count could not."""
    px = subtitle_img.load()
    cw = ch = 8
    cols  = max(1, subtitle_img.width // cw)
    rows_ = max(1, subtitle_img.height // ch)
    cells = [[0] * cols for _ in range(rows_)]
    for y in range(rows_ * ch):
        for x in range(cols * cw):
            if _is_orange(px[x, y]):
                cells[y // ch][x // cw] += 1
    best = 0
    for cy in range(rows_):
        for cx in range(cols):
            s = cells[cy][cx]
            if cx + 1 < cols:
                s += cells[cy][cx + 1]
            if cy + 1 < rows_:
                s += cells[cy + 1][cx]
                if cx + 1 < cols:
                    s += cells[cy + 1][cx + 1]
            best = max(best, s)
    return best


# ── OCR ───────────────────────────────────────────────────────────


def _resample():
    return getattr(Image, "Resampling", Image).LANCZOS


def _ocr_text(img):
    """OCR one row crop. Prefers winocr (Windows' built-in OCR engine, no
    external binary); falls back to pytesseract when that's what's installed.
    White-on-dark and white-on-selection-blue both read fine once upscaled."""
    up = img.resize((img.width * 3, img.height * 3), _resample())
    try:
        import winocr
    except ImportError:
        winocr = None
    if winocr is not None:
        try:
            res = winocr.recognize_pil_sync(up.convert("RGB"), "en")
            if isinstance(res, dict):
                return res.get("text", "") or ""
            return getattr(res, "text", "") or ""
        except Exception as exc:
            _emit(f"  [ocr] winocr failed ({exc}) — trying pytesseract")
    try:
        import pytesseract
    except ImportError:
        raise SystemExit(
            "No OCR engine available. Install one on this machine:\n"
            "  py -m pip install winocr        (uses Windows' built-in OCR — preferred)\n"
            "  py -m pip install pytesseract   (also needs the Tesseract program installed)")
    prepped = ImageOps.autocontrast(ImageOps.invert(up.convert("L")))
    return pytesseract.image_to_string(prepped, config="--psm 6")


# ── Row model ─────────────────────────────────────────────────────


class LicenseRow:
    def __init__(self, index, rect):
        self.index = index
        self.rect = rect          # screen coords (l, t, r, b)
        self.text = ""            # raw OCR of the row crop
        self.product = None       # 'PIX4Dmatic' / 'PIX4Dsurvey' / 'Discovery' / None
        self.avail = None         # seats available, None = unreadable
        self.total = None
        self.selected = False     # selection-blue fill = this is the ACTIVE license
        self.warning = False      # solid orange warning triangle detected
        self.warn_score = 0       # raw density score behind `warning`, for tuning

    @property
    def click_point(self):
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def __str__(self):
        if self.avail is None:
            seats = "?/?"
        else:
            seats = f"{self.avail}/{self.total if self.total is not None else '?'}"
        flags = ",".join(f for f, on in (("ACTIVE", self.selected), ("WARN", self.warning)) if on)
        return (f"row{self.index}  {str(self.product):12s} seats={seats:4s} "
                f"[{flags:11s}] warn={self.warn_score:3d} rect={self.rect}  ocr={self.text!r}")


def read_license_rows(win):
    """Build the row model: rects from UIA (licenseItem), content from
    per-row OCR, active/warning state from pixel signals."""
    if ImageGrab is None:
        raise SystemExit("Pillow is required for row reading: py -m pip install Pillow")
    infos = _row_elements(win)
    if not infos:
        return []

    l = min(i.rect[0] for i in infos) - 4
    t = min(i.rect[1] for i in infos) - 4
    r = max(i.rect[2] for i in infos) + 4
    b = max(i.rect[3] for i in infos) + 4
    shot = ImageGrab.grab(bbox=(l, t, r, b)).convert("RGB")

    rows = []
    for idx, info in enumerate(infos, 1):
        rl, rt, rr, rb = info.rect
        row_img = shot.crop((rl - l, rt - t, rr - l, rb - t))
        h = row_img.height
        row = LicenseRow(idx, info.rect)

        row.selected = _ratio(row_img, _is_blue) > 0.2
        subtitle = row_img.crop((10, h // 2, min(360, row_img.width), max(h // 2 + 1, h - 4)))
        row.warn_score = _warning_score(subtitle)
        row.warning = row.warn_score >= WARN_SCORE_MIN

        # Exclude the right edge (active-row checkmark) from the OCR crop.
        ocr_crop = row_img.crop((8, 2, max(9, row_img.width - 80), h - 2))
        row.text = " ".join(_ocr_text(ocr_crop).split())

        m = PRODUCT_RE.search(row.text)
        if m:
            token = m.group(1).lower()
            if token.startswith("mat"):
                row.product = "PIX4Dmatic"
            elif token.startswith("sur"):
                row.product = "PIX4Dsurvey"
            else:
                row.product = "PIX4D" + m.group(1)
        elif re.search(r"discover", row.text, re.I):
            row.product = "Discovery"

        m = SEATS_RE.search(row.text)
        if m:
            row.avail, row.total = int(m.group(1)), int(m.group(2))
        elif row.warning:
            row.avail = 0     # triangle present but digits unreadable: 0 seats
        rows.append(row)
    return rows


# ── Selection ─────────────────────────────────────────────────────


def _row_selected_now(rect):
    img = ImageGrab.grab(bbox=rect).convert("RGB")
    return _ratio(img, _is_blue) > 0.2


def select_available_license(win, product="PIX4Dmatic", apply=True):
    """Ensure a `product` license with a seat is selected. Returns
    'SELECTED', 'NO_SEATS', 'SCAN_FAILED', or 'CLICK_UNCONFIRMED' (see module
    docstring for the CLI exit-code mapping). Importable from AutomatePix4D.

    Click confirmation is the 'Apply' button materializing — the blue fill
    marks the ACTIVE license and does not move until Apply commits."""
    rows = read_license_rows(win)
    _emit(f"=== SELECT: rows read ({len(rows)}) ===")
    for row in rows:
        _emit(f"  {row}")

    if not rows:
        _emit("RESULT: SCAN_FAILED — no licenseItem elements found (dialog not open?).")
        return "SCAN_FAILED"

    matic = [r for r in rows if r.product and r.product.lower() == product.lower()]
    if not matic:
        _emit(f"RESULT: SCAN_FAILED — OCR never produced a {product} row; "
              "check the ocr= text above.")
        return "SCAN_FAILED"

    active = [r for r in matic if r.selected]
    if active:
        _emit(f"RESULT: SELECTED — row{active[0].index} is already the active {product} "
              "license (an active license holds its own seat, so 0/1 there is fine).")
        return "SELECTED"

    candidates = [r for r in matic if (r.avail or 0) > 0]
    if not candidates:
        unknown = [r.index for r in matic if r.avail is None]
        note = f" (row(s) {unknown} had unreadable seat counts)" if unknown else ""
        _emit(f"RESULT: NO_SEATS — no active {product} license and every one "
              f"shows 0 seats available{note}.")
        return "NO_SEATS"

    target = candidates[0]
    apply_btn = win.child_window(title="Apply", control_type="Button")
    confirmed = False
    for attempt in (1, 2):
        retry = " (retry)" if attempt == 2 else ""
        _emit(f"Clicking row{target.index} ({target.avail}/{target.total}) "
              f"at {target.click_point}{retry}")
        mouse.click(coords=target.click_point)
        time.sleep(0.6)
        if apply_btn.exists(timeout=1) or _row_selected_now(target.rect):
            confirmed = True
            break

    if not confirmed:
        _emit("RESULT: CLICK_UNCONFIRMED — no Apply button and no highlight after "
              "two clicks; verify on screen.")
        return "CLICK_UNCONFIRMED"

    if apply and apply_btn.exists(timeout=1):
        apply_btn.click_input()
        _emit("  clicked 'Apply'.")
        time.sleep(1.0)
        if _find_dialog_popup(win) is None:
            _emit("  dialog closed itself after Apply.")
        elif _row_selected_now(target.rect):
            _emit("  verified: the clicked row now shows as the active license.")
        else:
            _emit("  note: dialog still open and the row isn't highlighted yet — "
                  "re-run --rows to re-read if unsure.")

    _emit("RESULT: SELECTED")
    return "SELECTED"


def close_license_dialog(win, timeout=5):
    """Close the dialog via its X button (top-right, class ButtonIconBigNoBg*
    inside SelectLicenseDialog — matched by prefix because the _QMLTYPE_nn
    suffix changes between app builds). Cancel is deliberately avoided: it
    may revert the selection. Falls back to ESC. True when the popup is gone."""
    if _find_dialog_popup(win) is None:
        return True
    root = _uia.ElementFromHandle(win.handle)
    xs = _collect(root, lambda i: i.ctrl == "Button"
                  and i.cls.startswith("ButtonIconBigNoBg")
                  and "SelectLicenseDialog" in i.auto_id)
    if xs:
        l, t, r, b = xs[0].rect
        mouse.click(coords=((l + r) // 2, (t + b) // 2))
        _emit("  clicked the dialog's X button.")
    else:
        send_keys("{ESC}")
        _emit("  X button not found — sent ESC.")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        if _find_dialog_popup(win) is None:
            return True
    _emit("  WARNING: license dialog still open.")
    return False


# ── Diagnostic modes ──────────────────────────────────────────────


def mode_rows(win):
    print("\n=== LICENSE ROWS (UIA rects + OCR + pixel signals) ===")
    rows = read_license_rows(win)
    if not rows:
        print("  no licenseItem elements found — is the dialog open?")
    for row in rows:
        print(f"  {row}")
    return rows


def mode_hover(band):
    print(f"\n=== HOVER SCAN  (band y {band.top}..{band.bottom}, columns {band.x_columns}) ===")
    seen = {}
    for x in band.x_columns:
        for y in range(band.top, band.bottom, 8):
            mouse.move(coords=(x, y))
            time.sleep(0.04)
            info = element_at(x, y)
            if info and (info.name or info.auto_id):
                seen.setdefault((info.name, info.auto_id, info.rect), info)
    if not seen:
        print("  nothing with a name or automation id under the sweep points.")
    for info in sorted(seen.values(), key=lambda i: (i.rect[1], i.rect[0])):
        tag = "[ROW] " if info.auto_id == ROW_AUTO_ID else ""
        print(f"  {tag}{info}")


def mode_dump(win, band, max_nodes=4000):
    print("\n=== RAW UIA SUBTREE (clipped to the dialog area) ===")
    clip = (band.left - 200, band.anchor.top - 140, band.right + 200, band.bottom + 180)
    root = _uia.ElementFromHandle(win.handle)
    walker = _uia.RawViewWalker
    visited = [0]

    def _intersects(rect):
        return not (rect[2] < clip[0] or rect[0] > clip[2] or rect[3] < clip[1] or rect[1] > clip[3])

    def _walk(elem, depth):
        if depth > 20 or visited[0] > max_nodes:
            return
        visited[0] += 1
        try:
            info = ElemInfo(elem)
            if (info.name or info.auto_id) and _intersects(info.rect):
                print(f"  {'. ' * depth}{info}")
        except Exception:
            pass
        try:
            child = walker.GetFirstChildElement(elem)
        except Exception:
            return
        while child is not None:
            _walk(child, depth + 1)
            try:
                child = walker.GetNextSiblingElement(child)
            except Exception:
                break

    _walk(root, 0)
    print(f"  (visited {visited[0]} nodes)")


def mode_pixels(band, slab=6):
    print("\n=== PIXEL BANDS (blue=active row, orange=0-seat warning, white=text) ===")
    if ImageGrab is None:
        print("Pillow not installed — skipping (py -m pip install Pillow).")
        return
    bbox = (band.left, band.anchor.top - 20, band.right, band.bottom + 40)
    img = ImageGrab.grab(bbox=bbox).convert("RGB")
    px = img.load()
    w, h = img.size
    for y0 in range(0, h, slab):
        blue = orange = white = 0
        for y in range(y0, min(y0 + slab, h)):
            for x in range(0, w, 2):
                p = px[x, y]
                if _is_blue(p):
                    blue += 1
                elif _is_orange(p):
                    orange += 1
                elif _is_white(p):
                    white += 1
        if blue or orange or white:
            abs_y = bbox[1] + y0
            print(f"  y={abs_y} (anchor{abs_y - band.anchor.bottom:+d})  "
                  f"blue={blue:4d}  orange={orange:3d}  white={white:4d}")


def mode_keys(band, presses=12):
    print("\n=== KEYBOARD SCAN (WARNING: moves the selection — Cancel the dialog after) ===")
    a = band.anchor
    start = ((a.left + a.right) // 2, (a.top + a.bottom) // 2 + 200)
    print(f"  clicking first-row area {start} to focus the list...")
    mouse.click(coords=start)
    time.sleep(0.5)
    print(f"  focused after click: {focused_element()}")
    for i in range(presses):
        send_keys("{DOWN}")
        time.sleep(0.2)
        print(f"  after DOWN x{i + 1}: {focused_element()}")


# ── CLI ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Probe/select licenses in PIX4Dmatic's 'Organizations and licenses' dialog "
                    "(open the dialog first, then run this)")
    parser.add_argument("--rows",   action="store_true", help="read + print the license rows (default)")
    parser.add_argument("--select", action="store_true", help="click the available PIX4Dmatic license")
    parser.add_argument("--no-apply", action="store_true", help="with --select: don't click Apply")
    parser.add_argument("--close",  action="store_true", help="close the dialog via its X button")
    parser.add_argument("--product", default="PIX4Dmatic", help="product to select (default PIX4Dmatic)")
    parser.add_argument("--hover",  action="store_true", help="diagnostic: hit-test sweep")
    parser.add_argument("--dump",   action="store_true", help="diagnostic: raw UIA subtree dump")
    parser.add_argument("--pixels", action="store_true", help="diagnostic: color counts per slab")
    parser.add_argument("--keys",   action="store_true", help="diagnostic: arrow-key focus scan (MOVES selection)")
    args = parser.parse_args()

    if not (args.rows or args.select or args.close or args.hover
            or args.dump or args.pixels or args.keys):
        args.rows = True

    ctypes.windll.shcore.SetProcessDpiAwareness(1)    # physical pixels, like AutomatePix4D
    win = _find_pix4d_window()
    band = _dialog_band(win)
    a = band.anchor  # pywinauto RECT: attribute access only, it isn't iterable
    print(f"Anchor 'Organizations and licenses' rect: ({a.left}, {a.top}, {a.right}, {a.bottom})")

    if args.rows:
        mode_rows(win)
    if args.hover:
        mode_hover(band)
    if args.dump:
        mode_dump(win, band)
    if args.pixels:
        mode_pixels(band)
    if args.keys:
        mode_keys(band)

    exit_code = None
    if args.select:
        status = select_available_license(win, product=args.product, apply=not args.no_apply)
        exit_code = {"SELECTED": 0, "NO_SEATS": 3, "SCAN_FAILED": 4, "CLICK_UNCONFIRMED": 5}[status]
    if args.close:
        ok = close_license_dialog(win)
        print(f"close: {'dialog closed' if ok else 'dialog STILL OPEN'}")
    if exit_code is not None:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
