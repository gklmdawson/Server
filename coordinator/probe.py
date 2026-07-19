"""NAS helper: read-only metadata extraction off the data share.

This is the server-side port of data_intake.py's dynamic-form logic. It runs
INSIDE the coordinator container, reading the source flight folder directly on
the NAS (the card share and 3dData are mounted read-only), so the web form can
pre-fill sensor / date / EPSG the way the old PyQt GUI did — no upload of the
bulk imagery, and no Windows.

Everything here is read-only and pure Python:
  * sensor  — EXIF ``Model`` of the first image  -> EXIF_MODEL_TO_SENSOR
  * date    — EXIF ``DateTimeOriginal``           -> ddMonYYYY
  * epsg    — image GPS -> State Plane shapefile point-in-polygon -> (H, V)
  * rtk     — optional exiftool scan of RtkFlag across the flight JPEGs

Pillow is imported lazily so importing this module never hard-depends on it
(the endpoint reports a clean error if it is missing). The State Plane
shapefile is not bundled — point ``stateplane_shapefile`` in the coordinator
config at a ``.shp`` (its ``.dbf`` sibling is read alongside); when it is
absent the EPSG fields simply come back empty and the operator types them.
"""
from __future__ import annotations

import csv
import json
import os
import struct
import subprocess
from datetime import datetime
from typing import Any, Optional

# EXIF camera Model -> our internal sensor name (from data_intake.Config).
EXIF_MODEL_TO_SENSOR = {
    "PMA2616": "R3Pro",
    "L2": "L2",
    "L3": "L3",
    "M3E": "M3E",
    "ZenmuseP1": "P1",
}
DEFAULT_SENSOR_IF_NO_IMAGES = "R3ProMobile"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")

# Horizontal EPSG -> vertical EPSG, derived from data_intake's _SPCZONE table.
# Most NAD83 State Plane zones pair with 6360 (NAVD88 height, ftUS); the twelve
# below use 8228. The vertical is looked up from the SAME table as the
# horizontal — it is never defaulted.
STATEPLANE_HV: dict[str, str] = {
    "6394": "6360", "6395": "6360", "6396": "6360", "6397": "6360",
    "6398": "6360", "6399": "6360", "6400": "6360", "6401": "6360",
    "6402": "6360", "6403": "6360", "6405": "8228", "6407": "8228",
    "6409": "8228", "6411": "6360", "6413": "6360", "6416": "6360",
    "6418": "6360", "6420": "6360", "6422": "6360", "6424": "6360",
    "6426": "6360", "6428": "6360", "6430": "6360", "6432": "6360",
    "6434": "6360", "6436": "6360", "6438": "6360", "6441": "6360",
    "6443": "6360", "6445": "6360", "6447": "6360", "6449": "6360",
    "6451": "6360", "6453": "6360", "6455": "6360", "6457": "6360",
    "6459": "6360", "6461": "6360", "6463": "6360", "6465": "6360",
    "6467": "6360", "6469": "6360", "6471": "6360", "6475": "6360",
    "6477": "6360", "6479": "6360", "6484": "6360", "6486": "6360",
    "6488": "6360", "6490": "6360", "6492": "6360", "6494": "8228",
    "6496": "8228", "6499": "8228", "6501": "6360", "6503": "6360",
    "6505": "6360", "6507": "6360", "6510": "6360", "6511": "6360",
    "6512": "6360", "6513": "6360", "6515": "8228", "6519": "6360",
    "6521": "6360", "6523": "6360", "6525": "6360", "6527": "6360",
    "6529": "6360", "6531": "6360", "6533": "6360", "6535": "6360",
    "6537": "6360", "6539": "6360", "6541": "6360", "6543": "6360",
    "6545": "8228", "6547": "8228", "6549": "6360", "6551": "6360",
    "6553": "6360", "6555": "6360", "6559": "8228", "6561": "8228",
    "6563": "6360", "6565": "6360", "6568": "6360", "6570": "8228",
    "6572": "6360", "6574": "6360", "6576": "6360", "6578": "6360",
    "6582": "6360", "6584": "6360", "6586": "6360", "6588": "6360",
    "6590": "6360", "6593": "6360", "6595": "6360", "6597": "6360",
    "6599": "6360", "6601": "6360", "6603": "6360", "6605": "6360",
    "6607": "6360", "6609": "6360", "6612": "6360", "6614": "6360",
    "6616": "6360", "6618": "6360", "6625": "6360", "6626": "6360",
    "6627": "6360", "6628": "6360", "6629": "6360", "6630": "6360",
    "6631": "6360", "6632": "6360", "6880": "6360", "9748": "6360",
    "9749": "6360",
}

RTK_FLAG_TARGET = 50.0            # DJI Rtk Flag value for a fixed solution
_RTK_FLAG_EQ_TOL = 1e-6


# ---------------------------------------------------------------------------
# Images: sensor, date, GPS  (Pillow, imported lazily)
# ---------------------------------------------------------------------------

def find_first_image(folder: str) -> Optional[str]:
    """First image file found walking `folder` (sorted for determinism)."""
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(IMAGE_EXTENSIONS):
                return os.path.join(root, name)
    return None


def count_images(folder: str, cap: int = 5000) -> int:
    """Best-effort image count, capped so a huge card folder stays cheap."""
    n = 0
    for _root, _dirs, files in os.walk(folder):
        for name in files:
            if name.lower().endswith(IMAGE_EXTENSIONS):
                n += 1
                if n >= cap:
                    return n
    return n


def _exif(image_path: str) -> Optional[dict]:
    from PIL import Image  # lazy: keeps module import Pillow-free
    with Image.open(image_path) as img:
        return img._getexif()


def camera_model(image_path: str) -> Optional[str]:
    """EXIF `Model` tag (0x0110), stripped."""
    try:
        exif = _exif(image_path)
    except Exception:
        return None
    if not exif:
        return None
    value = exif.get(0x0110)
    return str(value).strip() if value is not None else None


def image_date(image_path: str) -> Optional[str]:
    """EXIF `DateTimeOriginal` (0x9003) as ddMonYYYY, e.g. 10Jul2026."""
    try:
        exif = _exif(image_path)
    except Exception:
        return None
    if not exif:
        return None
    value = exif.get(0x9003)
    if not value:
        return None
    try:
        date_str = str(value).split(" ")[0]
        year, month, day = date_str.split(":")
        month_name = datetime.strptime(month, "%m").strftime("%b")
        return f"{day}{month_name}{year}"
    except Exception:
        return None


def gps_from_image(image_path: str) -> Optional[tuple[float, float]]:
    """(lat, lon) decimal degrees from GPS EXIF, or None."""
    try:
        exif = _exif(image_path)
    except Exception:
        return None
    if not exif:
        return None
    gps = exif.get(34853)  # GPSInfo IFD
    if not gps or 2 not in gps or 4 not in gps:
        return None

    def _rat(v):
        if isinstance(v, tuple) and len(v) == 2:
            return v[0] / v[1]
        return float(v)

    def _scalar(vals):
        if hasattr(vals, "__len__") and len(vals) == 3:
            return _rat(vals[0]) + _rat(vals[1]) / 60.0 + _rat(vals[2]) / 3600.0
        if hasattr(vals, "__len__") and len(vals) == 1:
            return _rat(vals[0])
        return _rat(vals)

    def _to_deg(vals, ref):
        deg = _scalar(vals)
        return -deg if str(ref).upper() in ("S", "W") else deg

    try:
        return _to_deg(gps[2], gps.get(1, "N")), _to_deg(gps[4], gps.get(3, "E"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# EPSG: State Plane shapefile point-in-polygon  (no pyproj / GDAL)
# ---------------------------------------------------------------------------

def _shp_polygons(shp_path: str) -> list[list[list[tuple[float, float]]]]:
    """Polygon rings per record from a .shp (types 5/15/25/31)."""
    result: list[list[list[tuple[float, float]]]] = []
    with open(shp_path, "rb") as fh:
        fh.read(100)  # file header
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                break
            rec_bytes = struct.unpack(">i", hdr[4:])[0] * 2
            data = fh.read(rec_bytes)
            if len(data) < 4:
                break
            stype = struct.unpack("<i", data[:4])[0]
            if stype not in (5, 15, 25, 31):
                result.append([])
                continue
            n_parts, n_pts = struct.unpack("<ii", data[36:44])
            parts = list(struct.unpack(f"<{n_parts}i", data[44:44 + 4 * n_parts]))
            base = 44 + 4 * n_parts + (4 * n_parts if stype == 31 else 0)
            flat = struct.unpack(f"<{n_pts * 2}d", data[base:base + n_pts * 16])
            pts = [(flat[i * 2], flat[i * 2 + 1]) for i in range(n_pts)]
            parts.append(n_pts)
            result.append([pts[parts[i]:parts[i + 1]] for i in range(n_parts)])
    return result


def _dbf_rows(dbf_path: str, want: set[str]) -> list[Optional[dict[str, str]]]:
    """Selected field values from a .dbf file (one dict per record)."""
    rows: list[Optional[dict[str, str]]] = []
    with open(dbf_path, "rb") as fh:
        fh.read(4)
        n_recs = struct.unpack("<I", fh.read(4))[0]
        hdr_sz = struct.unpack("<H", fh.read(2))[0]
        rec_sz = struct.unpack("<H", fh.read(2))[0]
        fh.read(20)
        fields: list[tuple[str, int]] = []
        while True:
            desc = fh.read(32)
            if not desc or desc[0] in (0x0D, 0x1A):
                break
            name = desc[:11].rstrip(b"\x00").decode("ascii", errors="replace")
            fields.append((name, desc[16]))
        fh.seek(hdr_sz)
        for _ in range(n_recs):
            raw = fh.read(rec_sz)
            if not raw:
                break
            if raw[0] == 0x2A:  # deleted
                rows.append(None)
                continue
            d: dict[str, str] = {}
            off = 1
            for name, length in fields:
                val = raw[off:off + length].decode("ascii", errors="replace").strip()
                if name in want:
                    d[name] = val
                off += length
            rows.append(d)
    return rows


def _ray_cast(px: float, py: float, ring: list[tuple[float, float]]) -> bool:
    """Even-odd ray casting: True if (px, py) is inside the closed ring."""
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > py) != (yj > py):
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def epsg_from_latlon(lat: float, lon: float, shp_path: str) -> Optional[tuple[str, str]]:
    """(EPSG_H, EPSG_V) for the State Plane zone containing (lat, lon).

    Horizontal comes from the shapefile; vertical from STATEPLANE_HV keyed on
    that horizontal code. Returns None when no zone matches or the shapefile is
    absent/unreadable."""
    dbf_path = os.path.splitext(shp_path)[0] + ".dbf"
    if not (os.path.isfile(shp_path) and os.path.isfile(dbf_path)):
        return None
    try:
        polys = _shp_polygons(shp_path)
        attrs = _dbf_rows(dbf_path, {"EPSG", "ZONENAME"})
        for rings, attr in zip(polys, attrs):
            if not attr or not rings:
                continue
            hits = sum(1 for ring in rings if _ray_cast(lon, lat, ring))
            if hits % 2 == 1:
                epsg_h = str(int(float(attr["EPSG"])))
                return epsg_h, STATEPLANE_HV.get(epsg_h, "")
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Base ECEF CSV  (corrected base position -> X, Y, Z metres)
# ---------------------------------------------------------------------------

_BASE_ECEF_EXPECTED_HEADERS = ("Point ID", "X (ECEF)", "Y (ECEF)", "Z (ECEF)")


def parse_base_ecef_csv(path: str) -> tuple[float, float, float]:
    """Parse a corrected-base-position CSV -> (X, Y, Z) metres.

    Format (BOM tolerated):
        Point ID,X (ECEF),Y (ECEF),Z (ECEF)
        <name>,<X_m>,<Y_m>,<Z_m>
    Raises ValueError with a user-readable message on any mismatch."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            headers = tuple(h.strip() for h in next(reader))
        except StopIteration:
            raise ValueError("CSV file is empty.")
        if headers != _BASE_ECEF_EXPECTED_HEADERS:
            raise ValueError(
                "Base ECEF CSV header must be exactly: "
                + ",".join(_BASE_ECEF_EXPECTED_HEADERS))
        try:
            row = next(reader)
        except StopIteration:
            raise ValueError("Base ECEF CSV has no data row.")
        try:
            x, y, z = float(row[1]), float(row[2]), float(row[3])
        except (IndexError, ValueError):
            raise ValueError("Base ECEF CSV X/Y/Z must be numeric metres.")
    return x, y, z


# ---------------------------------------------------------------------------
# Targets CSV  (keep only the TLT rows -> SINGLE_TLT.csv)
# ---------------------------------------------------------------------------

def extract_tlt_rows(src: str, dest: str) -> tuple[int, int]:
    """Filter a targets/GCP CSV down to its TLT rows and write them to `dest`.

    A TLT row is one whose 5th column (index 4) equals "TLT" (case-insensitive),
    matching the extraction the Terra-LiDAR automation does at runtime
    (PyAutomateDJI._extract_tlt_csv). Returns (tlt_count, total_rows). Raises
    ValueError when the source has no TLT rows."""
    total = 0
    tlt_rows: list[list[str]] = []
    with open(src, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or not any(c.strip() for c in row):
                continue
            total += 1
            if len(row) >= 5 and row[4].strip().upper() == "TLT":
                tlt_rows.append(row)
    if not tlt_rows:
        raise ValueError("no TLT rows (column 5 == 'TLT') found in the targets csv")
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(tlt_rows)
    return len(tlt_rows), total


# ---------------------------------------------------------------------------
# RTK coverage  (opt-in exiftool scan)
# ---------------------------------------------------------------------------

def _parse_rtk_flag_value(raw: object) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s or s.lower() in ("none", "null", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _rtk_flag_from_record(rec: dict) -> Optional[float]:
    for k in ("RtkFlag", "Rtk Flag", "DJI:RtkFlag", "MakerNotes:RtkFlag"):
        if k in rec:
            v = _parse_rtk_flag_value(rec.get(k))
            if v is not None:
                return v
    for k, v in rec.items():
        if k == "SourceFile":
            continue
        if "rtkflag" in k.replace(" ", "").replace(":", "").lower():
            parsed = _parse_rtk_flag_value(v)
            if parsed is not None:
                return parsed
    return None


def rtk_scan(folder: str, exiftool: str = "exiftool",
             timeout: int = 1800) -> dict[str, Any]:
    """Scan JPEG RtkFlag under `folder`. Returns
    {total_photos, rtk_values, fixed_count, fixed_pct, error}.

    `exiftool` may be a bare command on PATH (Linux container:
    ``libimage-exiftool-perl``) or an absolute path."""
    if not os.path.isdir(folder):
        return {"total_photos": 0, "fixed_count": 0, "fixed_pct": None,
                "error": f"not a folder: {folder}"}
    cmd = [exiftool, "-json", "-n", "-RtkFlag",
           "-ext", "jpg", "-ext", "jpeg", "-r", folder]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"total_photos": 0, "fixed_count": 0, "fixed_pct": None,
                "error": "exiftool not found (install libimage-exiftool-perl)"}
    except subprocess.TimeoutExpired:
        return {"total_photos": 0, "fixed_count": 0, "fixed_pct": None,
                "error": "exiftool timed out"}
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return {"total_photos": 0, "fixed_count": 0, "fixed_pct": None,
                "error": (proc.stderr or "").strip() or f"exit {proc.returncode}"}
    try:
        records = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {"total_photos": 0, "fixed_count": 0, "fixed_pct": None,
                "error": f"could not parse exiftool output: {exc}"}
    if not isinstance(records, list):
        records = [records]
    values = [v for v in (_rtk_flag_from_record(r) for r in records
                          if isinstance(r, dict)) if v is not None]
    fixed = sum(1 for v in values if abs(v - RTK_FLAG_TARGET) <= _RTK_FLAG_EQ_TOL)
    total = len(records)
    return {
        "total_photos": total,
        "fixed_count": fixed,
        "fixed_pct": (100.0 * fixed / total) if total else None,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Top-level probe used by the /intake/probe endpoint
# ---------------------------------------------------------------------------

def probe_folder(abs_path: str, shp_path: Optional[str] = None) -> dict[str, Any]:
    """Cheap metadata from one representative image in `abs_path`.

    Reads a single image (sensor/date/GPS) and, if GPS + a shapefile are
    available, resolves EPSG H/V. Never raises for missing metadata — the
    fields simply come back null so the form leaves them blank/editable."""
    result: dict[str, Any] = {
        "sensor": None, "exif_model": None, "date": None,
        "gps": None, "epsg_h": None, "epsg_v": None,
        "image_count": 0, "first_image": None,
    }
    if not os.path.isdir(abs_path):
        result["error"] = f"not a folder: {abs_path}"
        return result

    image = find_first_image(abs_path)
    result["first_image"] = image
    if image is None:
        result["sensor"] = DEFAULT_SENSOR_IF_NO_IMAGES
        return result

    result["image_count"] = count_images(abs_path)
    model = camera_model(image)
    result["exif_model"] = model
    result["sensor"] = EXIF_MODEL_TO_SENSOR.get(model) if model else None
    result["date"] = image_date(image)

    gps = gps_from_image(image)
    if gps:
        result["gps"] = {"lat": gps[0], "lon": gps[1]}
        if shp_path:
            hv = epsg_from_latlon(gps[0], gps[1], shp_path)
            if hv:
                result["epsg_h"], result["epsg_v"] = hv
    return result
