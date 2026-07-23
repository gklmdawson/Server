"""Probe + selector for the PIX4Dmatic 'Organizations and licenses' dialog.

What the first probe run on the rig established (2026-07-23):

* Each license row IS in the raw UIA tree: a nameless Group with
  auto_id="licenseItem", class QQuickRectangle_QML_109, with an exact
  bounding rect (81px tall on the 4K/150% rig). The tree even returns them
  out of order — the same order instability that breaks get_license()'s
  blind dy=200/dy=300 offset clicks — but sorting by rect top fixes that.
* The rows expose NO text: hover/hit-test scans only ever return the popup
  container, and the row Groups have no accessible name. So UIA gives exact,
  order-proof click targets but cannot say which row is which product.
* Pixel signals are reliable: the selected row is a solid selection-blue
  fill, and 0-seat rows carry an orange warning triangle in the subtitle.

So the reader works like this: enumerate licenseItem rects via UIA, grab one
screenshot of the row band, OCR each row crop to get its product + seat
counts ('PIX4Dmatic, One-time charge' / '0/1 seat(s) available'), and use the
blue/orange pixel signals as selected-state, as a warning cross-check, and as
post-click verification. OCR engine: winocr (Windows' built-in OCR — no
external binary; `py -m pip install winocr`), falling back to pytesseract if
that's what's installed.

Run on the workstation WHILE the dialog is open (profile icon ->
Organizations & licenses). Default mode (no flags) is --rows.

  --rows     read the license list and print one line per row: product,
             seats, selected/warning flags, rect, raw OCR text. Read-only.
  --select   pick the first PIX4Dmatic row with seats available, click it,
             verify the row turned selection-blue, click 'Apply' if such a
             button exists (suppress with --no-apply). The dialog is left
             open — close it with its X button; Cancel may revert the choice.
  --hover    diagnostic: hit-test sweep of the band (ElementFromPoint).
  --dump     diagnostic: raw UIA subtree clipped to the dialog area.
  --pixels   diagnostic: per-slab color counts across the band.
  --keys     diagnostic: arrow-key focus scan. NOTE: moves the selection.

--select exit codes (for the agent / AutomatePix4D integration):
  0  an available PIX4Dmatic license was clicked (or was already selected)
  3  rows were readable but every PIX4Dmatic license shows 0 seats
     (route to the existing device-manager / Reclaim flow, or retry later)
  4  rows could not be read (no licenseItem elements, or OCR never yielded
     a PIX4Dmatic row) — run the diagnostics and read the raw OCR text

Once --select is proven on the rig, get_license() can replace its blind
offset clicks with:

    from license_probe import select_available_license
    status = select_available_license(self.win)   # "SELECTED"/"NO_SEATS"/"SCAN_FAILED"

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
# are plain digits around a slash ('0/1 seat(s) available').
PRODUCT_RE = re.compile(r"P[Il1]X4D\s*([A-Za-z]+)", re.I)
SEATS_RE   = re.compile(r"(\d+)\s*/\s*(\d+)")

ROW_AUTO_ID = "licenseItem"


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
    existing click anchor); clamp bottom/right to real dialog geometry."""
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


def _count(img, pred, step=2):
    px = img.load()
    n = 0
    for y in range(0, img.height, step):
        for x in range(0, img.width, step):
            if pred(px[x, y]):
                n += 1
    return n


def _ratio(img, pred, step=3):
    px = img.load()
    n = total = 0
    for y in range(0, img.height, step):
        for x in range(0, img.width, step):
            total += 1
            if pred(px[x, y]):
                n += 1
    return (n / total) if total else 0.0


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
            print(f"  [ocr] winocr failed ({exc}) — trying pytesseract")
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
        self.selected = False     # selection-blue fill detected
        self.warning = False      # orange warning triangle detected

    @property
    def click_point(self):
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def __str__(self):
        if self.avail is None:
            seats = "?/?"
        else:
            seats = f"{self.avail}/{self.total if self.total is not None else '?'}"
        flags = ",".join(f for f, on in (("SELECTED", self.selected), ("WARN", self.warning)) if on)
        return (f"row{self.index}  {str(self.product):12s} seats={seats:4s} "
                f"[{flags:13s}] rect={self.rect}  ocr={self.text!r}")


def read_license_rows(win):
    """Build the row model: rects from UIA (licenseItem), content from
    per-row OCR, selected/warning from pixel signals."""
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
        row.warning = _count(subtitle, _is_orange) >= 6

        # Exclude the right edge (selected-row checkmark) from the OCR crop.
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
    """Pick the first `product` license with seats available and click it.
    Returns 'SELECTED', 'NO_SEATS', or 'SCAN_FAILED' (see module docstring
    for the CLI exit-code mapping). Importable from AutomatePix4D."""
    rows = read_license_rows(win)
    print(f"\n=== SELECT: rows read ({len(rows)}) ===")
    for row in rows:
        print(f"  {row}")

    if not rows:
        print("RESULT: SCAN_FAILED — no licenseItem elements found (dialog not open?).")
        return "SCAN_FAILED"

    matic = [r for r in rows if r.product and r.product.lower() == product.lower()]
    if not matic:
        print(f"RESULT: SCAN_FAILED — OCR never produced a {product} row; "
              "check the ocr= text above.")
        return "SCAN_FAILED"

    candidates = [r for r in matic if (r.avail or 0) > 0]
    if not candidates:
        unknown = [r.index for r in matic if r.avail is None]
        note = f" (row(s) {unknown} had unreadable seat counts)" if unknown else ""
        print(f"RESULT: NO_SEATS — every {product} license shows 0 seats available{note}.")
        return "NO_SEATS"

    target = candidates[0]
    if target.selected:
        print(f"RESULT: SELECTED — row{target.index} ({target.avail}/{target.total}) "
              "was already the active selection.")
        return "SELECTED"

    print(f"Clicking row{target.index} ({target.avail}/{target.total}) at {target.click_point}")
    mouse.click(coords=target.click_point)

    verified = False
    for _ in range(2):
        time.sleep(0.7)
        if _row_selected_now(target.rect):
            verified = True
            break
    if verified:
        print("  verified: row shows the selection-blue fill.")
    else:
        print("  WARNING: row did not turn selection-blue — verify on screen.")

    if apply:
        try:
            apply_btn = win.child_window(title="Apply", control_type="Button")
            if apply_btn.exists(timeout=1):
                apply_btn.click_input()
                print("  clicked 'Apply'.")
            else:
                print("  no 'Apply' button — selection applies on click; dialog left "
                      "open (close via its X; Cancel may revert the choice).")
        except Exception as exc:
            print(f"  Apply lookup failed: {exc}")

    print("RESULT: SELECTED")
    return "SELECTED"


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
    print("\n=== PIXEL BANDS (blue=selected row, orange=0-seat warning, white=text) ===")
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
    parser.add_argument("--product", default="PIX4Dmatic", help="product to select (default PIX4Dmatic)")
    parser.add_argument("--hover",  action="store_true", help="diagnostic: hit-test sweep")
    parser.add_argument("--dump",   action="store_true", help="diagnostic: raw UIA subtree dump")
    parser.add_argument("--pixels", action="store_true", help="diagnostic: color counts per slab")
    parser.add_argument("--keys",   action="store_true", help="diagnostic: arrow-key focus scan (MOVES selection)")
    args = parser.parse_args()

    if not (args.rows or args.select or args.hover or args.dump or args.pixels or args.keys):
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

    if args.select:
        status = select_available_license(win, product=args.product, apply=not args.no_apply)
        sys.exit({"SELECTED": 0, "NO_SEATS": 3, "SCAN_FAILED": 4}[status])


if __name__ == "__main__":
    main()
