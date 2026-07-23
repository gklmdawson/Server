"""Probe + selector for the PIX4Dmatic 'Organizations and licenses' dialog.

The license rows in this dialog are custom-painted Qt items: they don't show up
in a normal UIA tree walk (same lazy-accessibility behavior as the
'Select folder...' button noted in AutomatePix4D.click_select_folder), and the
row ORDER varies between runs, so get_license()'s blind offset clicks
(dy=200/dy=300 from the dialog title) can land on the wrong license. This
script answers, on the real rig, "what CAN we actually read out of that
dialog?" — and, once reading works, "click the PIX4Dmatic license that still
has seats, or report that none does".

Run it on the workstation WHILE the dialog is open (profile icon ->
Organizations & licenses). Modes combine freely; with no flags it runs the
read-only trio --hover --dump --pixels:

  --hover    sweep the mouse down the license list, hit-testing with UIA
             ElementFromPoint at each step (the mouse move is deliberate —
             hover is what forces Qt to materialize lazy accessibles) and
             print every named element: name, control type, rect, patterns.
             If titles like 'PIX4Dmatic, One-time charge' and subtitles like
             '0/1 seat(s) available' appear here, --select will work.
  --dump     walk the RAW UIA subtree of the PIX4Dmatic window and print
             everything intersecting the dialog area. Runs after --hover on
             purpose: lazily-created accessibles are then already alive.
             (pywinauto's control view filters elements; the raw walker
             doesn't, so this can reveal rows the normal walk hides.)
  --pixels   screenshot the list band and print per-slab counts of
             selection-blue / warning-orange / text-white pixels (needs
             Pillow). Calibration data for a pixel fallback + for verifying a
             click landed (selected row turns blue; 0-seat rows carry the
             orange warning triangle).
  --keys     click the first-row area, then arrow-key down the list printing
             the UIA *focused* element after each press — focus, like hover,
             forces Qt to create the accessible, so this can read rows even
             when hit-testing can't. NOTE: this MOVES the selection; Cancel
             the dialog afterwards if you don't want to keep it.
  --select   do it for real: read the rows via the hit-test scan, pick the
             first PIX4Dmatic license with seats available, click it, click
             'Apply' if an Apply button exists (suppress with --no-apply),
             then verify and report.

--select exit codes (so the agent / AutomatePix4D can branch on the outcome):
  0  an available PIX4Dmatic license was found and clicked
  3  rows were readable but every PIX4Dmatic license shows 0 seats
     (route to the existing device-manager / Reclaim flow, or retry later)
  4  rows were NOT readable via UIA hit-testing (use the probe output to
     pick a fallback — OCR of the same band is the guaranteed one)

Once --select is proven on the rig, get_license() can replace its blind
offset clicks with:

    from license_probe import select_available_license
    status = select_available_license(self.win)   # "SELECTED"/"NO_SEATS"/"SCAN_FAILED"
"""

import argparse
import ctypes
import re
import sys
import time

# Declare STA before pywinauto is imported (it checks for this attribute when
# comtypes is already loaded) — silences its 'Revert to STA COM threading
# mode' warning without changing behavior.
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
    from PIL import ImageGrab
except ImportError:  # Pillow is in the agent/dev extras; probe still works without it
    ImageGrab = None

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

# Row text parsing. Titles look like 'PIX4Dmatic, One-time charge' /
# 'PIX4Dsurvey, One-time charge' / 'Discovery'; seat subtitles look like
# '0/1 seat(s) available' or '1/1 seat(s) available'. Some Qt items expose
# title+subtitle as ONE name, so both regexes are always run on every name.
PRODUCT_RE = re.compile(r"\bPIX4D([A-Za-z]+)", re.I)
SEATS_RE   = re.compile(r"(\d+)\s*/\s*(\d+)\s*seat", re.I)


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


def _drill_down(elem, x, y, depth=0):
    """Recurse into raw-view children to the deepest element containing (x, y).
    Same approach as UIInspect.py — ElementFromPoint alone often stops at a
    container in Qt apps."""
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


class Band:
    """The screen region the license rows live in, derived from live rects —
    never from hardcoded pixels — so it holds at any resolution/DPI."""

    def __init__(self, anchor_rect, bottom, x_columns):
        self.anchor = anchor_rect          # 'Organizations and licenses' title rect
        self.top = anchor_rect.bottom + 10
        self.bottom = bottom
        self.x_columns = x_columns         # x positions to hit-test down
        self.left = anchor_rect.left - 20
        self.right = anchor_rect.left + 820


def _dialog_band(win):
    """Anchor on the dialog title (already known to be in the UIA tree — it's
    get_license()'s existing click anchor) and clamp the bottom to the
    'Go to device manager' button when present."""
    anchor = win.child_window(title="Organizations and licenses", control_type="Text")
    anchor.wait("visible", timeout=10)
    a = anchor.rectangle()

    bottom = a.bottom + 700
    try:
        btn = win.child_window(title="Go to device manager", control_type="Button")
        if btn.exists(timeout=1):
            bottom = btn.rectangle().top - 8
    except Exception:
        pass

    # Three sweep columns: near the row's left edge (icons/containers), over
    # the title/subtitle text, and mid-row. Which one lands on readable text
    # depends on how Qt carved up the item — sweeping all three costs ~5s.
    x_columns = (a.left + 40, a.left + 130, a.left + 300)
    return Band(a, bottom, x_columns)


# ── Modes ─────────────────────────────────────────────────────────


def scan_named_elements(band, hover=True, step=8, settle=0.04):
    """Sweep the band, hit-testing each point; return deduped ElemInfos that
    carry a name, sorted top-to-bottom. hover=True moves the real cursor so
    hover-materialized accessibles (the click_select_folder phenomenon) get
    created before the hit-test."""
    seen = {}
    for x in band.x_columns:
        for y in range(band.top, band.bottom, step):
            if hover:
                mouse.move(coords=(x, y))
                time.sleep(settle)
            info = element_at(x, y)
            if info and info.name:
                seen.setdefault((info.name, info.rect), info)
    return sorted(seen.values(), key=lambda i: (i.rect[1], i.rect[0]))


def mode_hover(band):
    print(f"\n=== HOVER SCAN  (band y {band.top}..{band.bottom}, columns {band.x_columns}) ===")
    infos = scan_named_elements(band)
    if not infos:
        print("No named elements found in the band — hit-testing can't see the rows.")
        return infos
    for info in infos:
        tags = []
        if PRODUCT_RE.search(info.name):
            tags.append("PRODUCT")
        if SEATS_RE.search(info.name):
            tags.append("SEATS")
        prefix = f"[{','.join(tags)}] " if tags else ""
        print(f"  {prefix}{info}")
    return infos


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


def _is_blue(p):
    r, g, b = p[:3]
    return b > 120 and b > r + 30 and g > r          # PIX4D selection highlight

def _is_orange(p):
    r, g, b = p[:3]
    return r > 190 and 110 < g < 200 and b < 110     # warning triangle on 0-seat rows

def _is_white(p):
    return min(p[:3]) > 200                          # row title text


def mode_pixels(band, slab=6):
    print("\n=== PIXEL BANDS (blue=selected row, orange=0-seat warning, white=text) ===")
    if ImageGrab is None:
        print("Pillow not installed — skipping (pip install Pillow).")
        return
    bbox = (band.left, band.anchor.top - 20, band.right, band.bottom + 40)
    img = ImageGrab.grab(bbox=bbox).convert("RGB")
    px = img.load()
    w, h = img.size
    for y0 in range(0, h, slab):
        blue = orange = white = 0
        for y in range(y0, min(y0 + slab, h)):
            for x in range(0, w, 2):        # sample every other column, plenty for counts
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
    start = ((a.left + a.right) // 2, (a.top + a.bottom) // 2 + 200)  # get_license's first-row offset
    print(f"  clicking first-row area {start} to focus the list...")
    mouse.click(coords=start)
    time.sleep(0.5)
    info = focused_element()
    print(f"  focused after click: {info}")
    for i in range(presses):
        send_keys("{DOWN}")
        time.sleep(0.2)
        info = focused_element()
        print(f"  after DOWN x{i + 1}: {info}")


# ── Row model + selection ─────────────────────────────────────────


class LicenseRow:
    def __init__(self, product, rect):
        self.product = product      # e.g. 'PIX4Dmatic', 'PIX4Dsurvey'
        self.rect = rect
        self.avail = None
        self.total = None
        self.raw = []               # source element names, for logging

    @property
    def click_point(self):
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def __str__(self):
        seats = f"{self.avail}/{self.total}" if self.avail is not None else "?/?"
        return f"{self.product:12s} seats={seats:5s} rect={self.rect} raw={self.raw!r}"


def read_license_rows(win):
    """Build the row model from a hit-test scan: pair each product title with
    its seat count — from the same element's name when Qt exposes the row as
    one item, else from the nearest seats-text just below the title."""
    band = _dialog_band(win)
    infos = scan_named_elements(band)

    rows = []
    seat_texts = []   # (rect, avail, total) for standalone '0/1 seat(s) available' texts
    for info in infos:
        prod = PRODUCT_RE.search(info.name)
        seats = SEATS_RE.search(info.name)
        if prod:
            row = LicenseRow("PIX4D" + prod.group(1), info.rect)
            row.raw.append(info.name)
            if seats:
                row.avail, row.total = int(seats.group(1)), int(seats.group(2))
            rows.append(row)
        elif seats:
            seat_texts.append((info.rect, int(seats.group(1)), int(seats.group(2))))

    # Attach standalone seat texts to the title directly above them.
    for rect, avail, total in seat_texts:
        best = None
        for row in rows:
            if row.avail is not None:
                continue
            gap = rect[1] - row.rect[3]           # seats top below title bottom
            if -8 <= gap <= 45 and (best is None or gap < best[0]):
                best = (gap, row)
        if best:
            best[1].avail, best[1].total = avail, total
            best[1].raw.append(f"{avail}/{total} seat(s)")

    # Same row often comes back twice (e.g. row container + title text) —
    # collapse entries whose tops are within 12px, keeping the richer one.
    rows.sort(key=lambda r: r.rect[1])
    merged = []
    for row in rows:
        if merged and abs(row.rect[1] - merged[-1].rect[1]) < 12 and row.product == merged[-1].product:
            keep, other = merged[-1], row
            if keep.avail is None and other.avail is not None:
                merged[-1] = other
            merged[-1].raw = list(dict.fromkeys(keep.raw + other.raw))
        else:
            merged.append(row)
    return merged


def select_available_license(win, product="PIX4Dmatic", apply=True):
    """Pick the first `product` license with seats available and click it.
    Returns 'SELECTED', 'NO_SEATS', or 'SCAN_FAILED'. Import this from
    AutomatePix4D.get_license() once the probe proves the scan works."""
    rows = read_license_rows(win)
    print(f"\n=== SELECT: rows read ({len(rows)}) ===")
    for row in rows:
        print(f"  {row}")

    if not any(row.product.lower() == product.lower() for row in rows):
        print(f"RESULT: SCAN_FAILED — no {product} rows readable via UIA hit-testing.")
        return "SCAN_FAILED"

    candidates = [r for r in rows
                  if r.product.lower() == product.lower() and (r.avail or 0) > 0]
    if not candidates:
        print(f"RESULT: NO_SEATS — every {product} license shows 0 seats available.")
        return "NO_SEATS"

    target = candidates[0]
    print(f"Clicking: {target} at {target.click_point}")
    mouse.click(coords=target.click_point)
    time.sleep(0.6)

    # Verify best-effort: re-hit-test the row and report any selection state
    # UIA exposes; pixel-blue at the row is the fallback signal (see --pixels).
    check = element_at(*target.click_point)
    print(f"  post-click element: {check}")
    if check is not None and check.selected is False:
        print("  WARNING: element reports selected=False — verify manually / check pixels.")

    if apply:
        try:
            apply_btn = win.child_window(title="Apply", control_type="Button")
            if apply_btn.exists(timeout=1):
                apply_btn.click_input()
                print("  clicked 'Apply'.")
            else:
                print("  no 'Apply' button — selection appears to apply on click.")
        except Exception as exc:
            print(f"  Apply lookup failed: {exc}")

    print("RESULT: SELECTED")
    return "SELECTED"


# ── CLI ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Probe/select licenses in PIX4Dmatic's 'Organizations and licenses' dialog "
                    "(open the dialog first, then run this)")
    parser.add_argument("--hover",  action="store_true", help="hit-test sweep of the license list")
    parser.add_argument("--dump",   action="store_true", help="raw UIA subtree dump (dialog area)")
    parser.add_argument("--pixels", action="store_true", help="per-slab color counts of the list band")
    parser.add_argument("--keys",   action="store_true", help="arrow-key focus scan (MOVES selection)")
    parser.add_argument("--select", action="store_true", help="click the available PIX4Dmatic license")
    parser.add_argument("--no-apply", action="store_true", help="with --select: don't click Apply")
    parser.add_argument("--product", default="PIX4Dmatic", help="product to select (default PIX4Dmatic)")
    args = parser.parse_args()

    if not (args.hover or args.dump or args.pixels or args.keys or args.select):
        args.hover = args.dump = args.pixels = True   # read-only default

    ctypes.windll.shcore.SetProcessDpiAwareness(1)    # physical pixels, like AutomatePix4D
    win = _find_pix4d_window()
    band = _dialog_band(win)
    a = band.anchor  # pywinauto RECT: attribute access only, it isn't iterable
    print(f"Anchor 'Organizations and licenses' rect: ({a.left}, {a.top}, {a.right}, {a.bottom})")

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
