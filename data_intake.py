"""
Data Intake Application - Modularized Version
Refactored using DRY (Don't Repeat Yourself) and SRP (Single Responsibility Principle)

Modules:
- Config: Application constants and configuration
- FileOperations: File and folder manipulation utilities
- RinexProcessor: RINEX conversion and file handling
- RTBProcessor: REDToolBox CLI integration
- SensorDetector: EXIF-based sensor detection
- FolderStructureBuilder: Sensor-specific folder structure creation
- ProcessingWorker: Background processing orchestration
- DataIntakeUI: User interface
"""

from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
    QVBoxLayout, QHBoxLayout, QFileDialog, QMessageBox, QScrollArea,
    QSizePolicy, QProgressBar, QDialog, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QRadioButton, QButtonGroup, QToolTip,
)
from PyQt5.QtGui import QFont, QIcon, QPixmap, QCursor
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QEventLoop
from PyQt5.QtMultimedia import QSound
import configparser
import csv
import sys
import os
import shutil
import subprocess
import glob
import logging
import re
import json
import html
import math

from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple, Set, ClassVar

import struct

from classify_3dr import Classify3DRThread, Classify3DRWidget

# Disable PIL decompression bomb warning for large drone images (L3 captures ~100MP)
Image.MAX_IMAGE_PIXELS = None


# =============================================================================
# CONFIG MODULE - Application constants and configuration
# =============================================================================

class Config:
    """Centralized application configuration."""
    
    APP_VERSION = "Data Intake v2.4.4"
    APP_BUILD_DATE = "07/07/2026"
    
    # Paths
    LAST_FOLDER_FILE = os.path.join(
        os.environ.get("APPDATA", os.getcwd()), 
        "DataIntake_last_folder.txt"
    )
    ATX_REPO_DIR = r"Z:\Survey\UT\_Scripts\GMS\_ATX-REPO"
    LOGO_PATH = r"Z:\Survey\UT\_GabeA\PanoSandbox\logo.png"
    LOGO_SMALL_PATH = r"Z:\Survey\UT\_GabeA\PanoSandbox\logo-small.png"
    SOUND_PATH = r"Z:\Survey\UT\_GabeA\PanoSandbox\super_mario.wav"
    # Placeholder path applied when secret password is typed in the window
    SOUND_PATH_SECRET = r"Z:\Survey\UT\_GabeA\PanoSandbox\RickAstley-NeverGonnaGiveYouUp.wav"
    STATEPLANE_SHAPEFILE = r"Z:/Survey/UT/_Scripts/SunrisePhoto/resources/NAD83SPCEPSG.shp"

    # External tools
    CONVERT_TO_RINEX_EXE = r"C:\Program Files (x86)\Trimble\convertToRINEX\convertToRinex.exe"
    RTB_CLI_EXE = r"C:\Program Files\REDToolBox\resources\assets\REDToolBoxCLI\REDToolBoxCLI.exe"
    # ExifTool — REDToolbox bundle under AppData (Phil Harvey binary shipped with REDcatch)
    EXIFTOOL_EXE = os.path.normpath(
        os.path.join(
            os.environ.get("APPDATA", ""),
            "REDcatch GmBH",
            "REDToolbox",
            "exif",
            "exiftool.exe",
        )
    )
    
    # Processing
    SUBPROCESS_TIMEOUT = 1800
    COPY_BUFFER_SIZE = 4 * 1024 * 1024  # 4 MB
    
    # Sensor mappings: EXIF Model -> Internal sensor name
    EXIF_MODEL_TO_SENSOR = {
        "PMA2616": "R3Pro",
        "L2": "L2",
        "L3": "L3",
        "M3E": "M3E",
        "ZenmuseP1": "P1",
    }
    
    DEFAULT_SENSOR_IF_NO_IMAGES = "R3ProMobile"
    
    # File extensions
    IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
    RINEX_EXTENSIONS = ("o", "n", "g", "p", "l", "s", "obs", "rnx", "crx", "mix", "nav")
    BASE_DATA_EXTENSIONS = (".t02", ".t04")
    UNWANTED_PPK_EXTENSIONS = {".LDR", ".DBG", ".LDRT"}
    DJI_AUTOMATE_EXE     = r"Z:\Survey\UT\_Scripts\Automate UI\dist\DJI_AUTOMATE_UI.exe"
    DJI_AUTOMATE_PPK_EXE = r"Z:\Survey\UT\_Scripts\Automate UI\dist\DJI_AUTOMATE_PPK.exe"


class Styles:
    """Centralized UI styles."""
    
    LABEL_PRIMARY = "color: #113e59; background: #eaf6fa; border: 1px solid #ffd457; border-radius: 6px; padding: 5px; margin: 5px;"
    LABEL_SECONDARY = "color: #113e59; background: #fffbe6; border: 1px solid #ffd457; border-radius: 6px; padding: 5px; margin: 5px;"
    LABEL_WARNING = "color: #D32F2F; background: #ffd457; border-radius: 6px; padding: 10px; margin: 10px;"
    LABEL_SUCCESS = "color: #228B22; background: #eaf6fa; border-radius: 6px; padding: 5px; margin: 5px;"
    LABEL_LIST = "background: #f8f8f8; border: 1px solid #ffd457; color: #113e59; border-radius: 6px; padding: 5px; margin: 5px;"
    LABEL_TRANSPARENT = "background: transparent; color: #eaf6fa; padding: 5px; margin: 5px;"
    LABEL_PROGRESS = "color: #113e59; background: #eaf6fa; border: 1px solid #ffd457; border-radius: 6px; padding: 8px; margin: 5px;"
    LABEL_DROP = """
        background-color: #fffbe6;
        border: 2px dashed #ffd457;
        color: #113e59;
        border-radius: 6px;
        padding: 5px;
        margin: 5px;
    """
    
    BUTTON_PRIMARY = "color: #113e59; background: #ffd457; border-radius: 6px; border: 1px solid #eaf6fa; padding: 5px; margin: 5px;"
    BUTTON_SECONDARY = "color: #113e59; background: #eaf6fa; border-radius: 6px; border: 1px solid #ffd457; padding: 5px; margin: 5px;"
    BUTTON_DANGER = "color: #fff; background: #D32F2F; border-radius: 6px; padding: 2px 8px; margin: 5px;"
    BUTTON_MAIN = """
        QPushButton {
            background: #ffd457;
            color: #113e59;
            border-radius: 6px;
            font-weight: bold;
            border: 1px solid #eaf6fa;
            padding: 5px;
            margin: 5px;
        }
        QPushButton:pressed {
            background: #228B22;
            color: #fff;
        }
    """
    
    INPUT = "background: #eaf6fa; border: 1px solid #ffd457; border-radius: 6px; color: #113e59; padding: 5px; margin: 5px;"
    
    PROGRESS_BAR = """
        QProgressBar {
            color: #113e59; 
            background: #eaf6fa; 
            border: 1px solid #ffd457; 
            border-radius: 6px; 
            padding: 2px; 
            margin: 5px;
            text-align: center;
            font-size: 14px;
            font-weight: bold;
        }
        QProgressBar::chunk {
            background: #ffd457;
            border-radius: 4px;
        }
    """
    
    MESSAGEBOX = """
        QMessageBox {
            background: #113e59;
            border: 1px solid #ffd457;
            border-radius: 6px;
            color: #ffffff;
        }
        QMessageBox QLabel {
            color: #eaf6fa;
            font-family: 'Segoe UI';
            font-size: 12pt;
        }
        QPushButton {
            background: #ffd457;
            color: #113e59;
            border-radius: 6px;
            padding: 5px;
            margin: 5px;
        }
        QPushButton:pressed {
            background: #228B22;
            color: #fff;
        }
    """
    
    # Light-theme dialog for EPSG lookup — overrides any inherited dark stylesheet
    DIALOG_EPSG = """
        QDialog {
            background: #f5f5f5;
        }
        QLabel {
            color: #1a1a1a;
            background: transparent;
        }
        QLineEdit {
            background: #ffffff;
            color: #1a1a1a;
            border: 1px solid #aaaaaa;
            border-radius: 4px;
            padding: 4px;
        }
        QTableWidget {
            background: #ffffff;
            color: #1a1a1a;
            gridline-color: #dddddd;
            border: 1px solid #aaaaaa;
        }
        QTableWidget::item:selected {
            background: #1a6496;
            color: #ffffff;
        }
        QHeaderView::section {
            background: #0f4366;
            color: #ffffff;
            padding: 4px;
            border: none;
        }
        QPushButton {
            background: #0f4366;
            color: #ffffff;
            border-radius: 4px;
            padding: 5px 14px;
            margin: 2px;
        }
        QPushButton:pressed {
            background: #228B22;
        }
    """

    # QDialog matching sensor / intake message styling (dark panel, light text, gold buttons)
    DIALOG_SENSOR = """
        QDialog {
            background: #113e59;
            border: 1px solid #ffd457;
            border-radius: 6px;
        }
        QLabel {
            color: #eaf6fa;
            font-family: 'Segoe UI';
            font-size: 11pt;
        }
        QPushButton {
            background: #ffd457;
            color: #113e59;
            border-radius: 6px;
            padding: 8px 16px;
            margin: 4px;
            font-weight: bold;
        }
        QPushButton:pressed {
            background: #228B22;
            color: #fff;
        }
        QScrollArea {
            border: 1px solid #ffd457;
            border-radius: 6px;
            background: #0d3248;
        }
    """
    
    CHECKBOX_MAIN = """
        QCheckBox {
            color: #eaf6fa;
            font-family: 'Segoe UI';
            font-size: 11pt;
            spacing: 10px;
        }
        QCheckBox::indicator {
            width: 18px;
            height: 18px;
            border-radius: 3px;
            border: 1px solid #ffd457;
            background: #eaf6fa;
        }
        QCheckBox::indicator:checked {
            background: #ffd457;
        }
    """
    
    MESSAGEBOX_ERROR = """
        QMessageBox {
            background: #113e59;
            border: 2px solid black;
            border-radius: 6px;
            color: #ffffff;
        }
        QMessageBox QLabel {
            color: #ffffff;
            font-family: 'Segoe UI';
            font-size: 12pt;
        }
        QPushButton {
            background: #ffffff;
            color: #113e59;
            border: 1px solid black;
            border-radius: 6px;
            padding: 5px;
            margin: 5px;
        }
        QPushButton:pressed {
            background: black;
            color: white;
        }
    """


# =============================================================================
# LOGGING MODULE - Logging configuration and utilities
# =============================================================================

def _safe_stream(preferred):
    """Return a writable stream, falling back to __stderr__/__stdout__/devnull."""
    if preferred and hasattr(preferred, "write"):
        return preferred
    if hasattr(sys, "__stderr__") and sys.__stderr__:
        return sys.__stderr__
    if hasattr(sys, "__stdout__") and sys.__stdout__:
        return sys.__stdout__
    return open(os.devnull, "w")


ORIGINAL_STDOUT = _safe_stream(sys.stdout)
ORIGINAL_STDERR = _safe_stream(sys.stderr)
logger = logging.getLogger("data_intake")


def ensure_std_streams():
    """Ensure sys.stdout/sys.stderr are valid writable streams."""
    if sys.stderr is None:
        sys.stderr = ORIGINAL_STDERR
    if sys.stdout is None:
        sys.stdout = ORIGINAL_STDOUT


class StreamToLogger:
    """Redirects stdout/stderr to the configured logger."""

    def __init__(self, logger_instance, level=logging.INFO):
        self.logger = logger_instance
        self.level = level

    def write(self, message):
        message = message.strip()
        if message:
            for line in message.splitlines():
                try:
                    if self.logger.handlers:
                        self.logger.log(self.level, line.strip())
                    else:
                        target = ORIGINAL_STDERR if self.level >= logging.ERROR else ORIGINAL_STDOUT
                        target.write(line + "\n")
                except Exception:
                    try:
                        _safe_stream(None).write(line + "\n")
                    except Exception:
                        pass

    def flush(self):
        pass


def configure_logging(log_file_path: str):
    """Configure logging to write detailed steps and errors to a file."""
    ensure_std_streams()
    logging.raiseExceptions = False

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(_safe_stream(sys.stderr))
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    sys.stdout = StreamToLogger(logger, logging.INFO)
    sys.stderr = StreamToLogger(logger, logging.ERROR)

    logger.info(f"Logging initialized -> {log_file_path}")


def run_subprocess(cmd, **kwargs):
    """
    Unified subprocess launcher.
    Keeps command behavior consistent and suppresses extra console windows on Windows.
    """
    run_args = dict(kwargs)
    if sys.platform == "win32":
        run_args.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
    return subprocess.run(cmd, **run_args)


# =============================================================================
# FILE OPERATIONS MODULE - File and folder manipulation utilities
# =============================================================================

class FileOperations:
    """Utility class for file and folder operations."""
    
    @staticmethod
    def copy_file(source: str, dest: str, buffer_size: int = Config.COPY_BUFFER_SIZE) -> bool:
        """Copy a single file with buffered I/O."""
        try:
            with open(source, "rb") as src_f, open(dest, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f, length=buffer_size)
            shutil.copystat(source, dest, follow_symlinks=True)
            return True
        except Exception as e:
            print(f"Failed to copy {source}: {e}")
            return False
    
    @staticmethod
    def copy_file_with_dedup(source: str, target_folder: str) -> Optional[str]:
        """Copy file to target folder, handling duplicate filenames."""
        file_name = os.path.basename(source)
        base_name, ext = os.path.splitext(file_name)
        dest_file = os.path.join(target_folder, file_name)
        
        counter = 1
        while os.path.exists(dest_file):
            dest_file = os.path.join(target_folder, f"{base_name}_{counter}{ext}")
            counter += 1
        
        if FileOperations.copy_file(source, dest_file):
            return dest_file
        return None
    
    @staticmethod
    def create_folder_structure(base_path: str, structure: Dict) -> None:
        """Recursively create nested folder structure."""
        for name, subdict in structure.items():
            path = os.path.join(base_path, name)
            os.makedirs(path, exist_ok=True)
            if subdict:
                FileOperations.create_folder_structure(path, subdict)
    
    @staticmethod
    def find_first_image(folder: str) -> Optional[str]:
        """Find the first image file in a folder tree."""
        for root_dir, _, files in os.walk(folder):
            image_files = sorted(
                f for f in files if f.lower().endswith(Config.IMAGE_EXTENSIONS)
            )
            if image_files:
                return os.path.join(root_dir, image_files[0])
        return None
    
    @staticmethod
    def find_files_by_glob(pattern: str) -> Optional[str]:
        """Find the latest file matching a glob pattern."""
        try:
            files = sorted(glob.glob(pattern), key=os.path.getmtime)
            return files[-1] if files else None
        except Exception as e:
            print(f"Error finding files with pattern {pattern}: {e}")
            return None
    
    @staticmethod
    def find_files_with_year_suffix(folder_path: str, target_exts: Set[str]) -> Optional[str]:
        """
        Find the newest file whose extension matches target_exts, 
        allowing two-digit year prefixes (e.g., .25o, .24n).
        """
        latest_path = None
        latest_mtime = -1
        
        try:
            for entry in os.scandir(folder_path):
                if not entry.is_file():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                norm_ext = FileOperations.normalize_extension(ext)
                
                if norm_ext in target_exts:
                    mtime = entry.stat().st_mtime
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                        latest_path = entry.path
        except Exception as e:
            print(f"Error scanning {folder_path}: {e}")
        
        return latest_path
    
    @staticmethod
    def normalize_extension(ext: str) -> str:
        """Normalize extension by removing year prefix (e.g., .25o -> .o)."""
        if len(ext) > 3 and ext[1:3].isdigit():
            return "." + ext[3:]
        return ext
    
    @staticmethod
    def delete_files_by_extension(folder: str, extensions: Set[str]) -> None:
        """Delete all files with specified extensions in a folder tree."""
        for root_dir, _, files in os.walk(folder):
            for file in files:
                if os.path.splitext(file)[1].upper() in extensions:
                    try:
                        os.remove(os.path.join(root_dir, file))
                    except Exception as e:
                        print(f"Failed to delete {file}: {e}")
    
    @staticmethod
    def is_rinex_file(path: str) -> bool:
        """Check if file is a RINEX file based on extension."""
        return path.lower().endswith(Config.RINEX_EXTENSIONS)


# =============================================================================
# COORDINATE UTILITIES - GPS extraction and State Plane zone lookup
# =============================================================================

def _ecef_to_geodetic(x: float, y: float, z: float) -> Tuple[float, float]:
    """Convert ECEF (meters) to (lat_deg, lon_deg) using iterative WGS84."""
    a = 6378137.0
    e2 = 0.00669437999014
    lon = math.degrees(math.atan2(y, x))
    p = math.sqrt(x ** 2 + y ** 2)
    lat = math.degrees(math.atan2(z, p * (1 - e2)))
    for _ in range(10):
        sin_lat = math.sin(math.radians(lat))
        N = a / math.sqrt(1 - e2 * sin_lat ** 2)
        lat = math.degrees(math.atan2(z + e2 * N * sin_lat, p))
    return lat, lon


def _gps_from_image(image_path: str) -> Optional[Tuple[float, float]]:
    """Extract (lat, lon) decimal degrees from image GPS EXIF. Returns None if unavailable."""
    try:
        with Image.open(image_path) as img:
            exif_raw = img._getexif()
        if not exif_raw:
            return None
        gps = exif_raw.get(34853)  # 34853 = GPSInfo IFD tag
        if not gps or 2 not in gps or 4 not in gps:
            return None

        def _rat(v):
            # (numerator, denominator) tuple — old Pillow rational format
            if isinstance(v, tuple) and len(v) == 2:
                return v[0] / v[1]
            # IFDRational, plain int/float, or anything with __float__
            return float(v)

        def _scalar(vals):
            """Return a single decimal-degree value from whatever EXIF hands back."""
            # Standard DMS: 3-element sequence (degrees, minutes, seconds)
            if hasattr(vals, '__len__') and len(vals) == 3:
                return _rat(vals[0]) + _rat(vals[1]) / 60.0 + _rat(vals[2]) / 3600.0
            # Single-element sequence (decimal degrees in a wrapper)
            if hasattr(vals, '__len__') and len(vals) == 1:
                return _rat(vals[0])
            # Already a scalar rational or float
            return _rat(vals)

        def _to_deg(vals, ref):
            deg = _scalar(vals)
            return -deg if str(ref).upper() in ("S", "W") else deg

        return _to_deg(gps[2], gps.get(1, "N")), _to_deg(gps[4], gps.get(3, "E"))
    except Exception as e:
        print(f"GPS EXIF read failed for {image_path}: {e}")
        return None


_BASE_ECEF_EXPECTED_HEADERS: Tuple[str, ...] = ("Point ID", "X (ECEF)", "Y (ECEF)", "Z (ECEF)")


def _parse_base_ecef_csv(path: str) -> Tuple[float, float, float]:
    """Parse a corrected base position CSV and return (X, Y, Z) in metres.

    Required format (BOM is tolerated):
        Point ID,X (ECEF),Y (ECEF),Z (ECEF)
        <name>,<X_m>,<Y_m>,<Z_m>

    Raises ValueError with a user-readable message on any format mismatch.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            try:
                headers = tuple(h.strip() for h in next(reader))
            except StopIteration:
                raise ValueError("CSV file is empty.")
            if headers != _BASE_ECEF_EXPECTED_HEADERS:
                raise ValueError(
                    f"Column headers do not match expected format.\n\n"
                    f"  Found:    {list(headers)}\n"
                    f"  Expected: {list(_BASE_ECEF_EXPECTED_HEADERS)}"
                )
            try:
                row = next(reader)
            except StopIteration:
                raise ValueError("CSV has column headers but no data rows.")
            if len(row) < 4:
                raise ValueError(
                    f"Data row has {len(row)} column(s); expected 4 "
                    f"(Point ID, X, Y, Z)."
                )
            try:
                x, y, z = float(row[1].strip()), float(row[2].strip()), float(row[3].strip())
            except ValueError:
                raise ValueError(
                    f"X / Y / Z values must be numeric.\n"
                    f"  Got: {row[1:4]}"
                )
            return x, y, z
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read CSV: {exc}") from exc


def _gps_from_rinex(rinex_path: str) -> Optional[Tuple[float, float]]:
    """Parse APPROX POSITION XYZ from a RINEX obs file header; return (lat, lon)."""
    try:
        with open(rinex_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "APPROX POSITION XYZ" in line:
                    parts = line.split()
                    return _ecef_to_geodetic(float(parts[0]), float(parts[1]), float(parts[2]))
                if "END OF HEADER" in line:
                    break
    except Exception as e:
        print(f"RINEX coordinate read failed for {rinex_path}: {e}")
    return None


def _gps_from_t02(t02_path: str) -> Optional[Tuple[float, float]]:
    """Convert a T02/T04 to RINEX in a temp dir and extract APPROX POSITION XYZ."""
    import tempfile
    if not os.path.isfile(Config.CONVERT_TO_RINEX_EXE):
        print(f"convertToRINEX.exe not found — cannot extract coords from T02")
        return None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_src = os.path.join(tmp, os.path.basename(t02_path))
            shutil.copy2(t02_path, tmp_src)
            run_subprocess(
                [Config.CONVERT_TO_RINEX_EXE, tmp_src],
                capture_output=True,
                timeout=120,
            )
            for root, _, files in os.walk(tmp):
                for fname in files:
                    norm_ext = FileOperations.normalize_extension(
                        os.path.splitext(fname)[1].lower()
                    )
                    if norm_ext in (".o", ".obs"):
                        result = _gps_from_rinex(os.path.join(root, fname))
                        if result:
                            return result
    except Exception as e:
        print(f"T02 GPS extraction failed for {t02_path}: {e}")
    return None


def _shp_polygons(shp_path: str) -> List[List[List[Tuple[float, float]]]]:
    """Parse polygon rings from a .shp binary (shape types 5/15/25).
    Returns one list-of-rings per record; empty list for non-polygon records."""
    result = []
    with open(shp_path, "rb") as fh:
        fh.read(100)  # file header
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                break
            rec_bytes = struct.unpack(">i", hdr[4:])[0] * 2  # content length in 16-bit words
            data = fh.read(rec_bytes)
            if len(data) < 4:
                break
            stype = struct.unpack("<i", data[:4])[0]
            if stype not in (5, 15, 25, 31):  # Polygon / PolygonZ / PolygonM / MultiPatch
                result.append([])
                continue
            n_parts, n_pts = struct.unpack("<ii", data[36:44])
            parts = list(struct.unpack(f"<{n_parts}i", data[44:44 + 4 * n_parts]))
            # MultiPatch (31) has a PartTypes int32 array after Parts before Points
            base = 44 + 4 * n_parts + (4 * n_parts if stype == 31 else 0)
            flat = struct.unpack(f"<{n_pts * 2}d", data[base:base + n_pts * 16])
            pts = [(flat[i * 2], flat[i * 2 + 1]) for i in range(n_pts)]
            parts.append(n_pts)  # sentinel for slicing
            result.append([pts[parts[i]:parts[i + 1]] for i in range(n_parts)])
    return result


def _dbf_rows(dbf_path: str, want: Set[str]) -> List[Optional[Dict[str, str]]]:
    """Read selected field values from a .dbf file."""
    rows: List[Optional[Dict[str, str]]] = []
    with open(dbf_path, "rb") as fh:
        fh.read(4)                                          # version + date
        n_recs = struct.unpack("<I", fh.read(4))[0]
        hdr_sz = struct.unpack("<H", fh.read(2))[0]
        rec_sz = struct.unpack("<H", fh.read(2))[0]
        fh.read(20)                                         # reserved
        fields: List[Tuple[str, int]] = []
        while True:
            desc = fh.read(32)
            if not desc or desc[0] in (0x0D, 0x1A):        # terminator / EOF
                break
            name = desc[:11].rstrip(b"\x00").decode("ascii", errors="replace")
            fields.append((name, desc[16]))                 # (field_name, field_length)
        fh.seek(hdr_sz)
        for _ in range(n_recs):
            raw = fh.read(rec_sz)
            if not raw:
                break
            if raw[0] == 0x2A:                              # deleted record
                rows.append(None)
                continue
            d: Dict[str, str] = {}
            off = 1                                         # skip deletion flag
            for name, length in fields:
                val = raw[off:off + length].decode("ascii", errors="replace").strip()
                if name in want:
                    d[name] = val
                off += length
            rows.append(d)
    return rows


def _ray_cast(px: float, py: float, ring: List[Tuple[float, float]]) -> bool:
    """Even-odd ray casting: True if point (px, py) is inside the closed ring."""
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > py) != (yj > py):
            if px < (xj - xi) * (py - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _epsg_from_latlon(lat: float, lon: float) -> Optional[Tuple[str, str]]:
    """Query the State Plane shapefile with no third-party dependencies.
    Handles multipart polygons and holes via even-odd rule across all rings."""
    shp_path = Config.STATEPLANE_SHAPEFILE
    dbf_path = os.path.splitext(shp_path)[0] + ".dbf"
    if not os.path.isfile(shp_path) or not os.path.isfile(dbf_path):
        return None
    try:
        polys = _shp_polygons(shp_path)
        attrs = _dbf_rows(dbf_path, {"EPSG", "ZONENAME"})
        for rings, attr in zip(polys, attrs):
            if not attr or not rings:
                continue
            hits = sum(1 for ring in rings if _ray_cast(lon, lat, ring))
            if hits % 2 == 1:
                return str(int(float(attr["EPSG"]))), attr["ZONENAME"]
    except Exception as e:
        print(f"State Plane shapefile lookup failed: {e}")
    return None


# =============================================================================
# SENSOR DETECTION MODULE - EXIF-based sensor detection
# =============================================================================

@dataclass
class SensorDetectionResult:
    """Result of sensor detection."""
    sensor_choice: Optional[str]
    image_path: Optional[str]
    exif_model: Optional[str]


class SensorDetector:
    """Detects sensor type from image EXIF metadata."""
    
    @staticmethod
    def get_camera_model(image_path: str) -> Optional[str]:
        """Extract the camera model from EXIF metadata."""
        try:
            with Image.open(image_path) as image:
                exif_data = image._getexif()
            if not exif_data:
                return None
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == "Model":
                    return str(value).strip()
        except Exception as e:
            print(f"Error reading EXIF model from {image_path}: {e}")
        return None
    
    @staticmethod
    def get_image_date(image_path: str) -> Optional[str]:
        """Extract date from image EXIF data in DDMmmYYYY format."""
        try:
            with Image.open(image_path) as image:
                exif_data = image._getexif()
            if exif_data:
                for tag_id, value in exif_data.items():
                    tag = TAGS.get(tag_id, tag_id)
                    if tag == "DateTimeOriginal":
                        date_str = value.split(" ")[0]
                        year, month, day = date_str.split(":")
                        month_name = datetime.strptime(month, "%m").strftime("%b")
                        return f"{day}{month_name}{year}"
        except Exception as e:
            print(f"Error reading EXIF date from {image_path}: {e}")
        return None
    
    @staticmethod
    def detect_from_folders(folders: List[str]) -> SensorDetectionResult:
        """Detect sensor type from the first image found in folders."""
        for folder in folders:
            image_path = FileOperations.find_first_image(folder)
            if image_path:
                exif_model = SensorDetector.get_camera_model(image_path)
                if exif_model:
                    sensor = Config.EXIF_MODEL_TO_SENSOR.get(exif_model)
                    return SensorDetectionResult(sensor, image_path, exif_model)
                return SensorDetectionResult(None, image_path, None)
        
        # No images found - default to R3ProMobile
        return SensorDetectionResult(Config.DEFAULT_SENSOR_IF_NO_IMAGES, None, None)


@dataclass
class RtkFlagScanResult:
    """ExifTool scan of DJI Rtk Flag across flight JPEGs."""
    total_photos: int
    values: List[float]
    exiftool_error: Optional[str]

    # DJI Rtk Flag value commonly used for full RTK / fixed solution in reports
    RTK_FLAG_TARGET: ClassVar[float] = 50.0
    _RTK_FLAG_EQ_TOL: ClassVar[float] = 1e-6

    def count_rtk_flag_equal_50(self) -> int:
        """How many decoded flags match 50 (within float tolerance)."""
        t = self.RTK_FLAG_TARGET
        tol = self._RTK_FLAG_EQ_TOL
        return sum(1 for v in self.values if abs(float(v) - t) <= tol)

    def pct_rtk_flag_50_of_all_images(self) -> Optional[float]:
        """Percentage of all JPEGs scanned whose Rtk Flag is 50 (missing flag ≠ 50)."""
        if self.total_photos <= 0:
            return None
        return 100.0 * self.count_rtk_flag_equal_50() / self.total_photos

    @property
    def min_max_text(self) -> str:
        """Explicit minimum and maximum Rtk Flag (not a spread / difference)."""
        if not self.values:
            return "—"
        lo, hi = min(self.values), max(self.values)
        return f"min {lo}, max {hi}"


class FlightPhotoExifAnalyzer:
    """Batch Rtk Flag extraction via ExifTool for dropped flight folders."""

    @staticmethod
    def resolve_exiftool_path() -> Optional[str]:
        red_local = os.path.normpath(
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "REDcatch GmBH",
                "REDToolbox",
                "exif",
                "exiftool.exe",
            )
        )
        candidates = [
            Config.EXIFTOOL_EXE,
            red_local,
            r"C:\Program Files\exiftool\exiftool.exe",
            shutil.which("exiftool"),
            shutil.which("exiftool.exe"),
        ]
        for p in candidates:
            if p and os.path.isfile(p):
                return p
        return None

    @staticmethod
    def _parse_rtk_flag_value(raw: object) -> Optional[float]:
        if raw is None:
            return None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        s = str(raw).strip()
        if not s or s.lower() in ("none", "null", "n/a", ""):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _rtk_flag_from_record(rec: Dict) -> Optional[float]:
        """Read Rtk Flag from one ExifTool -json object (handles grouped tag names)."""
        direct_keys = (
            "RtkFlag",
            "Rtk Flag",
            "DJI:RtkFlag",
            "MakerNotes:RtkFlag",
        )
        for k in direct_keys:
            if k in rec:
                v = FlightPhotoExifAnalyzer._parse_rtk_flag_value(rec.get(k))
                if v is not None:
                    return v
        for k, v in rec.items():
            if k == "SourceFile":
                continue
            compact = k.replace(" ", "").replace(":", "").lower()
            if "rtkflag" in compact:
                parsed = FlightPhotoExifAnalyzer._parse_rtk_flag_value(v)
                if parsed is not None:
                    return parsed
        return None

    @staticmethod
    def _norm_flight_root(path: str) -> str:
        return os.path.normpath(os.path.abspath(path))

    @staticmethod
    def _which_flight_folder(source_file: str, folder_norms: List[str]) -> Optional[str]:
        """Longest matching flight root (normalized) for a JPEG path."""
        sn = os.path.normcase(os.path.normpath(source_file))
        best: Optional[str] = None
        best_len = -1
        for fn in folder_norms:
            c = os.path.normcase(fn)
            if sn == c or sn.startswith(c + os.sep):
                if len(c) > best_len:
                    best = fn
                    best_len = len(c)
        return best

    @staticmethod
    def _exiftool_rtk_records(folders: List[str]) -> Tuple[List[Dict], Optional[str]]:
        """Run ExifTool once; return (record dicts, error message or None)."""
        if not folders:
            return [], None

        exiftool = FlightPhotoExifAnalyzer.resolve_exiftool_path()
        if not exiftool:
            return (
                [],
                "ExifTool was not found. Install ExifTool or set Config.EXIFTOOL_EXE.",
            )

        cmd = [
            exiftool,
            "-json",
            "-n",
            "-RtkFlag",
            "-ext",
            "jpg",
            "-ext",
            "jpeg",
            "-r",
        ]
        for d in folders:
            if os.path.isdir(d):
                cmd.append(d)

        if len(cmd) <= 9:
            return [], "No valid folders to scan."

        try:
            run_args = dict(
                capture_output=True,
                text=True,
                timeout=Config.SUBPROCESS_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
            proc = run_subprocess(cmd, **run_args)
        except subprocess.TimeoutExpired:
            return [], "ExifTool timed out while reading flight photos."
        except Exception as e:
            return [], f"ExifTool failed: {e}"

        if proc.returncode != 0 and not (proc.stdout or "").strip():
            err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            return [], err

        try:
            records = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as e:
            return [], f"Could not parse ExifTool output: {e}"

        if not isinstance(records, list):
            records = [records]
        out: List[Dict] = [r for r in records if isinstance(r, dict)]
        return out, None

    @staticmethod
    def _rtk_scan_result_from_records(sub_records: List[Dict], exiftool_error: Optional[str]) -> RtkFlagScanResult:
        values: List[float] = []
        for rec in sub_records:
            v = FlightPhotoExifAnalyzer._rtk_flag_from_record(rec)
            if v is not None:
                values.append(v)
        return RtkFlagScanResult(len(sub_records), values, exiftool_error)

    def scan_rtk_flags_per_folder(
        folders: List[str],
    ) -> Tuple[RtkFlagScanResult, Dict[str, RtkFlagScanResult]]:
        """
        One ExifTool run; split JPEG records by flight folder (normalized paths as keys).
        Aggregate is the union of all flights.
        """
        if not folders:
            return RtkFlagScanResult(0, [], None), {}

        records, err = FlightPhotoExifAnalyzer._exiftool_rtk_records(folders)
        folder_norms = [
            FlightPhotoExifAnalyzer._norm_flight_root(d)
            for d in folders
            if os.path.isdir(d)
        ]
        per: Dict[str, List[Dict]] = {fn: [] for fn in folder_norms}

        if err:
            empty_per = {
                fn: RtkFlagScanResult(0, [], err) for fn in folder_norms
            }
            return RtkFlagScanResult(0, [], err), empty_per

        for rec in records:
            src = rec.get("SourceFile")
            if not src or not isinstance(src, str):
                continue
            w = FlightPhotoExifAnalyzer._which_flight_folder(src, folder_norms)
            if w:
                per[w].append(rec)

        per_results: Dict[str, RtkFlagScanResult] = {
            fn: FlightPhotoExifAnalyzer._rtk_scan_result_from_records(sub, None)
            for fn, sub in per.items()
        }
        agg = FlightPhotoExifAnalyzer._rtk_scan_result_from_records(records, None)
        return agg, per_results


# =============================================================================
# FOLDER STRUCTURE MODULE - Sensor-specific folder structure creation
# =============================================================================

class FolderStructureBuilder:
    """Builds folder structures based on sensor type."""
    
    # Template for LiDAR sensors (L2, L3)
    LIDAR_TEMPLATE = {
        "BaseData": {},
        "PPK": {},
        "Pix4d": {},
        "TerraArchive": {},
        "Terra": {},
    }
    
    # Template for standard sensors (M3E, P1, R3Pro)
    STANDARD_TEMPLATE = {
        "BaseData": {},
        "Pix4D": {},
        "Terra": {},
        "PPK": {},
    }
    
    @classmethod
    def build(cls, client: str, project: str, date: str, sensor: str) -> Dict:
        """Build folder structure for the given sensor type."""
        if sensor in ("L2", "L3"):
            inner = {**cls.LIDAR_TEMPLATE, sensor: {}}
        else:
            inner = {**cls.STANDARD_TEMPLATE, sensor: {}}
        
        return {client: {project: {date: inner}}}
    
    @staticmethod
    def get_sensor_folder_path(base: str, client: str, project: str, date: str, sensor: str) -> str:
        """Get the path to the sensor folder."""
        return os.path.join(base, client, project, date, sensor)
    
    @staticmethod
    def get_date_folder_path(base: str, client: str, project: str, date: str) -> str:
        """Get the path to the date folder."""
        return os.path.join(base, client, project, date)


# =============================================================================
# RINEX PROCESSOR MODULE - RINEX conversion and file handling
# =============================================================================

class RinexProcessor:
    """Handles RINEX conversion and related file operations."""
    
    @staticmethod
    def collect_rinex_files(base_data_path: str) -> List[str]:
        """Collect all RINEX companion files for a given base data file."""
        base_dir = os.path.dirname(base_data_path)
        base_prefix = os.path.splitext(os.path.basename(base_data_path))[0]
        files_to_copy = []
        
        try:
            for file in os.listdir(base_dir):
                if file.startswith(base_prefix) and file.lower().endswith(Config.RINEX_EXTENSIONS):
                    files_to_copy.append(os.path.join(base_dir, file))
        except FileNotFoundError:
            pass
        
        return files_to_copy or [base_data_path]
    
    @staticmethod
    def rename_mix_to_nav(folder_path: str) -> None:
        """Rename *.mix files to .nav in the provided folder."""
        try:
            mix_patterns = [
                os.path.join(folder_path, "*.*mix"),
                os.path.join(folder_path, "*.mix")
            ]
            for pattern in mix_patterns:
                for mix_file in glob.glob(pattern):
                    if not mix_file.lower().endswith("mix"):
                        continue
                    nav_file = mix_file[:-3] + "nav"
                    if not os.path.exists(nav_file):
                        os.rename(mix_file, nav_file)
                        print(f"Renamed {os.path.basename(mix_file)} to {os.path.basename(nav_file)}")
        except Exception as e:
            print(f"Failed to rename mix files in {folder_path}: {e}")
    
    @staticmethod
    def batch_convert(
        folder_path: str,
        base_ecef_xyz: Optional[Tuple[float, float, float]] = None,
    ) -> None:
        """Convert T02/T04 files to RINEX format.

        If base_ecef_xyz is provided the corrected ECEF position (X, Y, Z in
        metres) is written into the APPROX POSITION XYZ field of every
        converted RINEX observation file via the convertToRINEX -x/-y/-z flags.
        """
        print(f"Starting RINEX conversion in folder: {folder_path}")

        if not os.path.isdir(folder_path):
            print("Error: Folder path does not exist.")
            return

        t_files = [
            f for f in os.listdir(folder_path)
            if f.lower().endswith((".t02", ".t04", "t0b"))
        ]

        if not t_files:
            print("No .T02 or .T04 files found.")
            return

        if not os.path.isfile(Config.CONVERT_TO_RINEX_EXE):
            print(f"Error: {Config.CONVERT_TO_RINEX_EXE} not found.")
            return

        for file_name in t_files:
            try:
                file_path = os.path.join(folder_path, file_name)

                command = [Config.CONVERT_TO_RINEX_EXE, file_path]
                if base_ecef_xyz is not None:
                    x, y, z = base_ecef_xyz
                    command.append(f"/xy:{x},{y},{z}")
                    print(f"Base position override: X={x}  Y={y}  Z={z}")
                print(f"Command: {' '.join(command)}")
                run_subprocess(command, check=True, timeout=Config.SUBPROCESS_TIMEOUT)

                if base_ecef_xyz is not None:
                    RinexProcessor.patch_approx_position(folder_path, x, y, z)

                RinexProcessor.rename_mix_to_nav(folder_path)
                print(f"Successfully converted {file_name}")
            except Exception as e:
                print(f"Error converting {file_name}: {e}")
    
    @staticmethod
    def patch_approx_position(folder_path: str, x: float, y: float, z: float) -> None:
        """Overwrite the APPROX POSITION XYZ line in every RINEX obs file in folder_path."""
        for fname in os.listdir(folder_path):
            fpath = os.path.join(folder_path, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            # RINEX obs files end in a digit + O (e.g. .26O) or .obs / .rnx
            if not (ext.endswith("o") or ext in (".obs", ".rnx")):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
                patched = False
                for i, line in enumerate(lines):
                    if "APPROX POSITION XYZ" in line:
                        lines[i] = f"{x:14.4f}{y:14.4f}{z:14.4f}                  APPROX POSITION XYZ\n"
                        patched = True
                        break
                    if "END OF HEADER" in line:
                        break
                if patched:
                    with open(fpath, "w", encoding="utf-8") as fh:
                        fh.writelines(lines)
                    print(f"Patched APPROX POSITION XYZ in {fname}")
                else:
                    print(f"No APPROX POSITION XYZ found in {fname} — skipped")
            except Exception as e:
                print(f"Failed to patch {fname}: {e}")

    @staticmethod
    def collect_nav_files(folder_path: str) -> List[str]:
        """Collect all NAV files in a folder."""
        nav_exts = {".n", ".nav"}
        nav_files = []
        
        try:
            for entry in os.scandir(folder_path):
                if not entry.is_file():
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                norm_ext = FileOperations.normalize_extension(ext)
                if norm_ext in nav_exts and entry.name.lower() != "merged.nav":
                    nav_files.append(entry.path)
        except Exception as e:
            print(f"Error collecting NAV files: {e}")
        
        return sorted(set(nav_files), key=os.path.getmtime)
    
    @staticmethod
    def merge_nav_files(nav_files: List[str], output_dir: str) -> Optional[str]:
        """Merge multiple NAV files using REDToolBoxCLI."""
        if not nav_files:
            return None
        if len(nav_files) == 1:
            return nav_files[0]
        
        merged_path = os.path.join(output_dir, "merged.nav")
        cmd = [
            Config.RTB_CLI_EXE, "merge-nav",
            "--log-level", "normal",
            "--output-dir", output_dir,
            "--merged-file-name", "merged.nav",
        ]
        for nav in nav_files:
            cmd.extend(["--nav-input-file", nav])
        
        print(f"Merging NAV files -> {merged_path}")
        try:
            run_subprocess(cmd, check=True, timeout=Config.SUBPROCESS_TIMEOUT)
            if os.path.exists(merged_path):
                return merged_path
        except Exception as e:
            print(f"NAV merge failed: {e}")
        
        return nav_files[-1]
    
    @staticmethod
    def extract_antenna_filename(rinex_obs_path: str) -> Optional[str]:
        """Extract antenna descriptor from RINEX obs file header."""
        if not rinex_obs_path or not os.path.isfile(rinex_obs_path):
            return None
        try:
            with open(rinex_obs_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "ANT # / TYPE" in line:
                        parts = [p for p in line.split() if p]
                        if len(parts) >= 2:
                            return f"{parts[0]}_{parts[1]}.atx"
                        break
        except Exception as e:
            print(f"Failed to extract antenna info: {e}")
        return None
    
    @staticmethod
    def rename_for_sensor(rinex_file_path: str, sensor_folder: str) -> None:
        """
        Copy and rename the RINEX obs file into every flight subfolder that contains a .rtk file.
        Used for L2, L3, M3E, and P1 sensors.
        """
        file_to_copy = os.path.basename(rinex_file_path)
        
        for subdir, _, files in os.walk(sensor_folder):
            rtk_file = next(
                (f for f in files if f.lower().endswith(".rtk")), 
                None
            )
            if rtk_file:
                rtk_base = os.path.splitext(rtk_file)[0]
                dest_path = os.path.join(subdir, file_to_copy)
                
                if not os.path.exists(dest_path):
                    print(f"Copying '{file_to_copy}' to '{subdir}'")
                    shutil.copy(rinex_file_path, dest_path)
                
                new_path = os.path.join(subdir, f"{rtk_base}.obs")
                print(f"Renaming to '{rtk_base}.obs'")
                shutil.move(dest_path, new_path)
            else:
                print(f"No .rtk file found in '{subdir}'. Skipping.")
        
        print("RINEX rename completed!")




# =============================================================================
# EXIF RTK FLAG SCAN THREAD (keep ExifTool off the GUI thread — avoids Windows "not responding")
# =============================================================================

class DJISequenceThread(QThread):
    """Runs DJI automation EXEs sequentially in a background thread so the UI stays responsive."""

    sequence_complete = pyqtSignal()
    status_update     = pyqtSignal(str)

    def __init__(self, cmds: list, parent=None):
        super().__init__(parent)
        self._cmds = cmds  # ordered list of argument lists

    def run(self):
        total = len(self._cmds)
        for i, cmd in enumerate(self._cmds, 1):
            name = os.path.splitext(os.path.basename(cmd[0]))[0]
            self.status_update.emit(f"[{i}/{total}] Launching {name}...")
            result = subprocess.run(cmd)
            if result.returncode != 0:
                self.status_update.emit(f"[{i}/{total}] {name} failed (exit {result.returncode})")
                break
            self.status_update.emit(f"[{i}/{total}] {name} complete")
        self.sequence_complete.emit()


class ExifRtkScanThread(QThread):
    """Runs FlightPhotoExifAnalyzer.scan_rtk_flags_per_folder in a worker thread."""

    scan_finished = pyqtSignal(int, object, object)  # generation, aggregate, per_folder dict

    def __init__(self, folders: List[str], generation: int):
        super().__init__()
        self._folders = list(folders)
        self._generation = generation

    def run(self):
        agg, per = FlightPhotoExifAnalyzer.scan_rtk_flags_per_folder(self._folders)
        self.scan_finished.emit(self._generation, agg, per)


class T02CoordThread(QThread):
    """Converts a T02/T04 to RINEX in a temp dir and extracts GPS coordinates off the GUI thread."""

    result_ready = pyqtSignal(object)  # emits (lat, lon) tuple or None

    def __init__(self, t02_path: str):
        super().__init__()
        self._path = t02_path

    def run(self):
        self.result_ready.emit(_gps_from_t02(self._path))


class EpsgDetectThread(QThread):
    """Runs GPS extraction + State Plane shapefile lookup off the GUI thread.

    Emits (lat, lon, epsg_code, zone_name, source_desc) on full success,
    (lat, lon, None, None, source_desc) when coords found but no zone matched,
    or None when GPS coordinates could not be extracted at all.
    """

    result_ready = pyqtSignal(object)

    def __init__(self, use_image: bool, candidates: list, folders: list):
        super().__init__()
        self._use_image = use_image
        self._candidates = candidates
        self._folders = folders

    def run(self):
        coords = None
        source_desc = ""
        if self._use_image:
            for folder in self._folders:
                img_path = FileOperations.find_first_image(folder)
                if img_path:
                    coords = _gps_from_image(img_path)
                    if coords:
                        source_desc = os.path.basename(img_path)
                    break
        else:
            for candidate in self._candidates:
                c = None
                if FileOperations.is_rinex_file(candidate):
                    c = _gps_from_rinex(candidate)
                elif candidate.lower().endswith(Config.BASE_DATA_EXTENSIONS):
                    c = _gps_from_t02(candidate)
                if c:
                    coords = c
                    source_desc = os.path.basename(candidate)
                    break
        if not coords:
            self.result_ready.emit(None)
            return
        lat, lon = coords
        epsg = _epsg_from_latlon(lat, lon)
        if epsg:
            self.result_ready.emit((lat, lon, epsg[0], epsg[1], source_desc))
        else:
            self.result_ready.emit((lat, lon, None, None, source_desc))


# =============================================================================
# PROCESSING WORKER MODULE - Background processing orchestration
# =============================================================================

class ProcessingWorker(QThread):
    """Background worker thread for data processing."""
    
    # Signals
    progress_update = pyqtSignal(int)
    status_update = pyqtSignal(str)
    file_copy_progress = pyqtSignal(int, int, str)
    error_occurred = pyqtSignal(str)
    processing_complete = pyqtSignal(str, str, str, str, object, int, str)
    
    def __init__(
        self, selected_folder: str, data_source_folders: List[str],
        base_data_paths: List[str], client: str, project: str,
        sensor_choice: str, base_data_is_rinex: bool = False,
        base_ecef_xyz: Optional[Tuple[float, float, float]] = None,
    ):
        super().__init__()
        self.selected_folder = selected_folder
        self.data_source_folders = data_source_folders
        self.base_data_paths = base_data_paths
        self.client = client
        self.project = project
        self.sensor_choice = sensor_choice
        self.base_data_is_rinex = base_data_is_rinex
        self.base_ecef_xyz = base_ecef_xyz
        self.should_stop = False
        self.log_file_path = None
    
    def stop_processing(self):
        self.should_stop = True
    
    def run(self):
        try:
            logger.info("Processing thread started")
            self._process_data()
        except Exception as e:
            logger.exception("Critical error during processing")
            self.error_occurred.emit(f"Critical error: {str(e)}")
    
    def _process_data(self):
        """Main processing logic."""
        try:
            self.status_update.emit("Starting data processing...")
            
            # Determine date from first image
            date_curr = self._get_date_from_images()
            
            # Setup logging
            self._setup_logging(date_curr)
            
            # Create folder structure
            self.status_update.emit("Creating folder structure...")
            structure = FolderStructureBuilder.build(
                self.client, self.project, date_curr, self.sensor_choice
            )
            FileOperations.create_folder_structure(self.selected_folder, structure)
            
            sensor_folder = FolderStructureBuilder.get_sensor_folder_path(
                self.selected_folder, self.client, self.project, date_curr, self.sensor_choice
            )
            date_folder = FolderStructureBuilder.get_date_folder_path(
                self.selected_folder, self.client, self.project, date_curr
            )
            
            # Copy source data
            self.status_update.emit("Copying data source files...")
            files_copied, first_image = self._copy_source_data(sensor_folder)
            
            if self.should_stop:
                return
            
            # Sensor-specific processing
            self._process_sensor_specific(sensor_folder, date_folder)
            
            # Emit completion
            self.processing_complete.emit(
                self.client, self.project, date_curr,
                sensor_folder, first_image, files_copied, date_folder
            )
            logger.info("Processing complete")
            
        except Exception as e:
            logger.exception("Error during processing")
            self.error_occurred.emit(f"Error: {str(e)}")
    
    def _get_date_from_images(self) -> str:
        """Get date from first image or use current date."""
        for folder in self.data_source_folders:
            if self.should_stop:
                break
            image = FileOperations.find_first_image(folder)
            if image:
                date = SensorDetector.get_image_date(image)
                if date:
                    return date
        return datetime.now().strftime("%d%b%Y")
    
    def _setup_logging(self, date_curr: str):
        """Setup logging for this processing run."""
        log_dir = os.path.join(
            self.selected_folder, self.client, self.project, date_curr
        )
        os.makedirs(log_dir, exist_ok=True)
        self.log_file_path = os.path.join(
            log_dir, f"{self.client}_{self.project}_{date_curr}_intake.log"
        )
        configure_logging(self.log_file_path)
        logger.info("----- New data intake run started -----")
        logger.info(f"Client: {self.client}, Project: {self.project}, Sensor: {self.sensor_choice}")
    
    def _copy_source_data(self, sensor_folder: str) -> Tuple[int, Optional[str]]:
        """Copy source data to sensor folder."""
        files_copied = 0
        first_image = None
        
        total_files = sum(
            len(files)
            for folder in self.data_source_folders
            for _, _, files in os.walk(folder)
        )
        
        current_file = 0
        for source_folder in self.data_source_folders:
            if self.should_stop:
                break
            
            folder_name = os.path.basename(source_folder)
            
            # Create directory structure
            for root_dir, _, _ in os.walk(source_folder):
                rel_path = os.path.relpath(root_dir, source_folder)
                target = os.path.join(sensor_folder, folder_name, rel_path)
                os.makedirs(target, exist_ok=True)
            
            # Copy files
            for root_dir, _, files in os.walk(source_folder):
                for file in files:
                    if self.should_stop:
                        break
                    
                    source_file = os.path.join(root_dir, file)
                    rel_path = os.path.relpath(root_dir, source_folder)
                    target_folder = os.path.join(sensor_folder, folder_name, rel_path)
                    
                    if FileOperations.copy_file_with_dedup(source_file, target_folder):
                        files_copied += 1
                        current_file += 1
                        
                        if not first_image and file.lower().endswith(Config.IMAGE_EXTENSIONS):
                            first_image = source_file
                        
                        self.file_copy_progress.emit(current_file, total_files, folder_name)
        
        return files_copied, first_image
    
    def _process_sensor_specific(self, sensor_folder: str, date_folder: str):
        """Handle sensor-specific processing."""
        base_folder = os.path.join(date_folder, "BaseData")
        
        if self.sensor_choice in ("L2", "L3"):
            self._process_lidar_sensor(sensor_folder, base_folder)
        elif self.sensor_choice in ("R3Pro", "R3ProMobile"):
            self._process_r3pro_sensor(sensor_folder, date_folder, base_folder)
        else:
            self._process_standard_sensor(sensor_folder, base_folder)
    
    def _process_lidar_sensor(self, sensor_folder: str, base_folder: str):
        """Process L2/L3 LiDAR sensors."""
        self._copy_base_data([base_folder])
        self._convert_or_rename_rinex(base_folder)
        rinex_file = self._find_rinex_obs(base_folder)
        if rinex_file:
            RinexProcessor.rename_for_sensor(rinex_file, sensor_folder)

        
    
    def _process_r3pro_sensor(self, sensor_folder: str, date_folder: str, base_folder: str):
        """Process R3Pro sensors."""
        self.status_update.emit("Processing R3Pro base data...")
        os.makedirs(base_folder, exist_ok=True)
        
        self._copy_base_data([base_folder])
        self._convert_or_rename_rinex(base_folder)
        
        # Copy to each subfolder's POS/base
        for subfolder_name in os.listdir(sensor_folder):
            subfolder_path = os.path.join(sensor_folder, subfolder_name)
            if os.path.isdir(subfolder_path):
                target = os.path.join(subfolder_path, "POS", "base")
                os.makedirs(target, exist_ok=True)
                for file_name in os.listdir(base_folder):
                    src = os.path.join(base_folder, file_name)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(target, file_name))
    
    def _process_standard_sensor(self, sensor_folder: str, base_folder: str):
        """Process standard sensors (M3E, P1)."""
        self._copy_base_data([base_folder])
        self._convert_or_rename_rinex(base_folder)
        rinex_file = self._find_rinex_obs(base_folder)
        if not rinex_file:
            logger.warning("No RINEX obs file found in BaseData after conversion — base file will not be copied to flight folders.")
            return
        # Standardise to .obs and copy into every flight subfolder.
        # M3E/P1 folders contain images, not .rtk files, so rename_for_sensor cannot be used.
        obs_name = os.path.splitext(os.path.basename(rinex_file))[0] + ".obs"
        for subfolder_name in os.listdir(sensor_folder):
            subfolder_path = os.path.join(sensor_folder, subfolder_name)
            if os.path.isdir(subfolder_path):
                shutil.copy2(rinex_file, os.path.join(subfolder_path, obs_name))
                print(f"[ok] Copied obs to '{subfolder_path}'")

    
    def _copy_base_data(self, targets: List[str]):
        """Copy base data files to target locations."""
        valid_sources = [p for p in self.base_data_paths if os.path.isfile(p)]
        if not valid_sources:
            logger.error("No valid base data files found.")
            return
        
        for target in targets:
            if self.should_stop:
                break
            os.makedirs(target, exist_ok=True)
            
            for source in valid_sources:
                files = (RinexProcessor.collect_rinex_files(source) 
                        if self.base_data_is_rinex else [source])
                for file_path in files:
                    FileOperations.copy_file(
                        file_path, 
                        os.path.join(target, os.path.basename(file_path))
                    )
            
            if self.base_data_is_rinex:
                RinexProcessor.rename_mix_to_nav(target)
    
    def _convert_or_rename_rinex(self, base_folder: str):
        """Convert to RINEX or rename mix files."""
        if self.base_data_is_rinex:
            self.status_update.emit("RINEX provided; skipping conversion.")
            RinexProcessor.rename_mix_to_nav(base_folder)
        else:
            self.status_update.emit("Converting to RINEX format...")
            RinexProcessor.batch_convert(base_folder, self.base_ecef_xyz)
    
    def _copy_to_ppk(self, sensor_folder: str, ppk_folder: str):
        """Copy sensor folder contents to PPK folder."""
        for item in os.listdir(sensor_folder):
            if self.should_stop:
                break
            src = os.path.join(sensor_folder, item)
            dst = os.path.join(ppk_folder, item)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
    
    def _cleanup_ppk_folder(self, ppk_folder: str, date_folder: str):
        """Clean up PPK folder and run conversions."""
        # Remove unwanted files
        FileOperations.delete_files_by_extension(ppk_folder, Config.UNWANTED_PPK_EXTENSIONS)
        
        # Handle RINEX conversion
        base_data_date = os.path.join(date_folder, "BaseData")
        base_data_ppk = os.path.join(ppk_folder, "BaseData")
        os.makedirs(base_data_ppk, exist_ok=True)
        
        self._convert_or_rename_rinex(base_data_date)
        
        # Sync to PPK/BaseData
        self._sync_base_data(base_data_date, base_data_ppk)
        
        # Find RINEX and run processing
        rinex_file = self._find_rinex_obs(base_data_ppk)
        if rinex_file:
            sensor_folder = os.path.join(date_folder, self.sensor_choice)
            RinexProcessor.rename_for_sensor(rinex_file, sensor_folder)

    
    def _sync_base_data(self, source: str, target: str):
        """Sync base data from source to target folder."""
        try:
            # Clear target
            for f in os.listdir(target):
                fp = os.path.join(target, f)
                if os.path.isfile(fp):
                    os.remove(fp)
                else:
                    shutil.rmtree(fp, ignore_errors=True)
            # Copy files
            for f in os.listdir(source):
                shutil.copy2(os.path.join(source, f), os.path.join(target, f))
        except Exception as e:
            print(f"Failed to sync base data: {e}")
    
    def _find_rinex_obs(self, folder: str) -> Optional[str]:
        """Find RINEX observation file in folder."""
        for root_dir, _, files in os.walk(folder):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                norm_ext = FileOperations.normalize_extension(ext)
                if norm_ext in (".o", ".obs"):
                    return os.path.join(root_dir, file)
        return None
    


# =============================================================================
# UI MODULE - User Interface
# =============================================================================

class EPSGLookupDialog(QDialog):
    """Quick EPSG lookup from US State Plane Coordinate zone table."""

    _SPCZONE = [
        ("AK_1","Alaska Zone 1","6394","6360"),("AK_2","Alaska Zone 2","6395","6360"),
        ("AK_3","Alaska Zone 3","6396","6360"),("AK_4","Alaska Zone 4","6397","6360"),
        ("AK_5","Alaska Zone 5","6398","6360"),("AK_6","Alaska Zone 6","6399","6360"),
        ("AK_7","Alaska Zone 7","6400","6360"),("AK_8","Alaska Zone 8","6401","6360"),
        ("AK_9","Alaska Zone 9","6402","6360"),("AK_10","Alaska Zone 10","6403","6360"),
        ("AZ_C","Arizona Central","6405","8228"),("AZ_E","Arizona East","6407","8228"),
        ("AZ_W","Arizona West","6409","8228"),("AR_N","Arkansas North","6411","6360"),
        ("AR_S","Arkansas South","6413","6360"),("CA_1","California Zone 1","6416","6360"),
        ("CA_2","California Zone 2","6418","6360"),("CA_3","California Zone 3","6420","6360"),
        ("CA_4","California Zone 4","6422","6360"),("CA_5","California Zone 5","6424","6360"),
        ("CA_6","California Zone 6","6426","6360"),("CO_C","Colorado Central","6428","6360"),
        ("CO_N","Colorado North","6430","6360"),("CO_S","Colorado South","6432","6360"),
        ("CT","Connecticut","6434","6360"),("DE","Delaware","6436","6360"),
        ("FL_E","Florida East","6438","6360"),("FL_N","Florida North","6441","6360"),
        ("FL_W","Florida West","6443","6360"),("GA_E","Georgia East","6445","6360"),
        ("GA_W","Georgia West","6447","6360"),("ID_C","Idaho Central","6449","6360"),
        ("ID_E","Idaho East","6451","6360"),("ID_W","Idaho West","6453","6360"),
        ("IL_E","Illinois East","6455","6360"),("IL_W","Illinois West","6457","6360"),
        ("IN_E","Indiana East","6459","6360"),("IN_W","Indiana West","6461","6360"),
        ("IA_N","Iowa North","6463","6360"),("IA_S","Iowa South","6465","6360"),
        ("KS_N","Kansas North","6467","6360"),("KS_S","Kansas South","6469","6360"),
        ("KY_N","Kentucky North","6471","6360"),("KY_S","Kentucky South","6475","6360"),
        ("LA_N","Louisiana North","6477","6360"),("LA_S","Louisiana South","6479","6360"),
        ("ME_E","Maine East","6484","6360"),("ME_W","Maine West","6486","6360"),
        ("MD","Maryland","6488","6360"),("MA_I","Massachusetts Island","6490","6360"),
        ("MA_M","Massachusetts Mainland","6492","6360"),("MI_C","Michigan Central","6494","8228"),
        ("MI_N","Michigan North","6496","8228"),("MI_S","Michigan South","6499","8228"),
        ("MN_C","Minnesota Central","6501","6360"),("MN_N","Minnesota North","6503","6360"),
        ("MN_S","Minnesota South","6505","6360"),("MS_E","Mississippi East","6507","6360"),
        ("MS_W","Mississippi West","6510","6360"),("MO_C","Missouri Central","6511","6360"),
        ("MO_E","Missouri East","6512","6360"),("MO_W","Missouri West","6513","6360"),
        ("MT","Montana","6515","8228"),("NV_C","Nevada Central","6519","6360"),
        ("NV_E","Nevada East","6521","6360"),("NV_W","Nevada West","6523","6360"),
        ("NH","New Hampshire","6525","6360"),("NJ","New Jersey","6527","6360"),
        ("NM_C","New Mexico Central","6529","6360"),("NM_E","New Mexico East","6531","6360"),
        ("NM_W","New Mexico West","6533","6360"),("NY_C","New York Central","6535","6360"),
        ("NY_E","New York East","6537","6360"),("NY_LI","New York Long Island","6539","6360"),
        ("NY_W","New York West","6541","6360"),("NC","North Carolina","6543","6360"),
        ("ND_N","North Dakota North","6545","8228"),("ND_S","North Dakota South","6547","8228"),
        ("OH_N","Ohio North","6549","6360"),("OH_S","Ohio South","6551","6360"),
        ("OK_N","Oklahoma North","6553","6360"),("OK_S","Oklahoma South","6555","6360"),
        ("OR_N","Oregon North","6559","8228"),("OR_S","Oregon South","6561","8228"),
        ("PA_N","Pennsylvania North","6563","6360"),("PA_S","Pennsylvania South","6565","6360"),
        ("RI","Rhode Island","6568","6360"),("SC","South Carolina","6570","8228"),
        ("SD_N","South Dakota North","6572","6360"),("SD_S","South Dakota South","6574","6360"),
        ("TN","Tennessee","6576","6360"),("TX_C","Texas Central","6578","6360"),
        ("TX_N","Texas North","6582","6360"),("TX_NC","Texas North Central","6584","6360"),
        ("TX_S","Texas South","6586","6360"),("TX_SC","Texas South Central","6588","6360"),
        ("VT","Vermont","6590","6360"),("VA_N","Virginia North","6593","6360"),
        ("VA_S","Virginia South","6595","6360"),("WA_N","Washington North","6597","6360"),
        ("WA_S","Washington South","6599","6360"),("WV_N","West Virginia North","6601","6360"),
        ("WV_S","West Virginia South","6603","6360"),("WI_C","Wisconsin Central","6605","6360"),
        ("WI_N","Wisconsin North","6607","6360"),("WI_S","Wisconsin South","6609","6360"),
        ("WY_E","Wyoming East","6612","6360"),("WY_EC","Wyoming East Central","6614","6360"),
        ("WY_W","Wyoming West","6616","6360"),("WY_WC","Wyoming West Central","6618","6360"),
        ("UT_C","Utah Central","6625","6360"),("UT_N","Utah North","6626","6360"),
        ("UT_S","Utah South","6627","6360"),("HI_1","Hawaii Zone 1","6628","6360"),
        ("HI_2","Hawaii Zone 2","6629","6360"),("HI_3","Hawaii Zone 3","6630","6360"),
        ("HI_4","Hawaii Zone 4","6631","6360"),("HI_5","Hawaii Zone 5","6632","6360"),
        ("NE","Nebraska","6880","6360"),("AL_E","Alabama East","9748","6360"),
        ("AL_W","Alabama West","9749","6360"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_h_code = None
        self.selected_v_code = None
        self.setWindowTitle("EPSG Lookup")
        self.resize(520, 460)
        self.setStyleSheet(Styles.DIALOG_EPSG)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Type to filter by zone or name...")
        self._filter.setFont(QFont("Segoe UI", 10))
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Zone", "Zone Name", "EPSG (H)", "Vertical"])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept_selection)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("Select")
        ok_btn.setFont(QFont("Segoe UI", 10))
        ok_btn.clicked.connect(self._accept_selection)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFont(QFont("Segoe UI", 10))
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self._populate(self._SPCZONE)

    def _populate(self, rows):
        self._table.setRowCount(0)
        for zone, name, epsg, vert in rows:
            r = self._table.rowCount()
            self._table.insertRow(r)
            for c, val in enumerate((zone, name, epsg, vert)):
                item = QTableWidgetItem(val)
                item.setTextAlignment(Qt.AlignCenter)
                self._table.setItem(r, c, item)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

    def _apply_filter(self, text):
        terms = text.lower().split()
        filtered = [
            row for row in self._SPCZONE
            if all(t in " ".join(row).lower() for t in terms)
        ]
        self._populate(filtered)

    def _accept_selection(self):
        row = self._table.currentRow()
        if row < 0:
            return
        self.selected_h_code = self._table.item(row, 2).text()
        self.selected_v_code = self._table.item(row, 3).text()
        self.accept()


class DataIntakeUI(QMainWindow):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        # Data state
        self.selected_folder = ""
        self.data_source_folders = []
        self.base_data_paths = []
        self.base_data_is_rinex = False
        self.processing_worker = None
        self.current_progress_bar = None
        self.current_progress_label = None
        self.log_file_path = None
        self._per_folder_rtk_scan: Dict[str, RtkFlagScanResult] = {}  # norm path -> last ExifTool slice
        self._rtk_scan_gen = 0
        self._rtk_scan_thread = None  # ExifRtkScanThread when a flight-folder scan is running
        
        # Pre-initialize widget references to None (prevents AttributeError during init)
        self.folder_display = None
        self.base_file_drop = None
        self.base_file_label = None
        self.base_drop_busy = None
        self.drop_label = None
        self.data_drop_busy = None
        self.data_source_list = None
        self.client_label = None
        self.client_input = None
        self.project_label = None
        self.project_input = None
        self.ready_label = None
        self.start_btn = None
        self.dji_terra_toggle = None
        self.dji_ppk_toggle = None
        self._dji_box = None
        self._dji_details = None
        self._dji_thread = None
        self._classify_3dr_widget = None
        self._classify_3dr_thread = None
        self._pending_3dr_terra_folder = None
        self._pending_3dr_project_name = None
        self._scroll_content = None
        self.gcp_path_input = None
        self.epsg_h_input = None
        self.epsg_v_input = None
        self._gcp_section = None
        self._no_targets_check = None
        self._manual_base_check = None
        self._manual_base_csv_path: Optional[str] = None
        self._manual_base_csv_label = None
        self._manual_base_csv_browse_btn = None

        self._secret_password = "gabeisagenius"
        self._key_buffer = ""
        self._setup_window()
        self._show_startup_popup()
        self._init_ui()
        self._load_last_folder()
    
    def _setup_window(self):
        """Configure main window."""
        self.setWindowTitle("Data Intake")
        self._set_window_geometry()
        self.setStyleSheet("background-color: #113e59; color: #113e59;")
        
        if os.path.exists(Config.LOGO_SMALL_PATH):
            self.setWindowIcon(QIcon(Config.LOGO_SMALL_PATH))
    
    def keyPressEvent(self, event):
        """Secret password: typing 'gabeisagenius' anywhere in the window switches SOUND_PATH to placeholder."""
        text = event.text()
        if text and len(text) == 1 and text.isprintable():
            self._key_buffer = (self._key_buffer + text.lower())[-len(self._secret_password):]
            if self._key_buffer == self._secret_password:
                Config.SOUND_PATH = Config.SOUND_PATH_SECRET
                self._key_buffer = ""
                self._play_alternative_sound_8_seconds()
                self._show_password_accepted_popup()
        super().keyPressEvent(event)

    def _play_alternative_sound_8_seconds(self):
        """Play the alternative (secret) audio clip and stop after 8 seconds."""
        try:
            self._secret_sound = QSound(Config.SOUND_PATH_SECRET)
            self._secret_sound.play()
            QTimer.singleShot(8000, self._secret_sound.stop)
        except Exception:
            pass

    def _show_password_accepted_popup(self):
        """Show a small separate popup window when the secret password is accepted."""
        popup = QDialog(self)
        popup.setWindowTitle("")
        popup.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        popup.setAttribute(Qt.WA_TranslucentBackground, False)
        layout = QVBoxLayout(popup)
        label = QLabel("YES YOU ARE A GENIUS")
        label.setFont(QFont("Segoe UI", 10))
        label.setStyleSheet("padding: 12px 20px; background: #113e59; color: #eaf6fa; border-radius: 6px;")
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(label)
        popup.adjustSize()
        # Center popup relative to main window, slightly above center
        geo = self.geometry()
        top_left = self.mapToGlobal(self.rect().topLeft())
        popup.move(top_left.x() + (geo.width() - popup.width()) // 2,
                  top_left.y() + (geo.height() - popup.height()) // 2 - 80)
        QTimer.singleShot(2000, popup.close)
        popup.exec_()

    def _set_window_geometry(self):
        """Set window size based on screen."""
        screen = QApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            width  = max(900, min(1100, int(avail.width() * 0.9)))
            height = avail.height() - 40
            x = avail.x() + (avail.width() - width) // 2
            y = avail.y() + 40
            self.setGeometry(x, y, width, height)
        else:
            self.setGeometry(50, 50, 1100, 900)
    
    def _show_startup_popup(self):
        """Show startup warning dialog."""
        popup = QDialog(self)
        popup.setWindowTitle("Data Intake Warning")
        popup.setGeometry(400, 250, 400, 180)
        
        layout = QVBoxLayout(popup)
        label = QLabel(
            "Before using this app, ensure you have:\n\n"
            "• GCP file\n• Base file\n• Drone data"
        )
        label.setFont(QFont("Segoe UI Bold", 14, QFont.Bold))
        label.setStyleSheet(Styles.LABEL_WARNING)
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        
        close_btn = QPushButton("OK")
        close_btn.setFont(QFont("Segoe UI", 12, QFont.Bold))
        close_btn.setStyleSheet(Styles.BUTTON_SECONDARY)
        close_btn.clicked.connect(popup.accept)
        layout.addWidget(close_btn)
        
        popup.exec_()
    
    def _init_ui(self):
        """Initialize user interface components."""
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        main_layout = QVBoxLayout(self.central_widget)
        
        # Version header
        self._add_version_header(main_layout)
        
        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self._scroll_content = scroll_content
        scroll.setWidget(scroll_content)
        self.scroll_layout = QVBoxLayout(scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignLeft)
        main_layout.addWidget(scroll)
        
        # Add UI sections
        self._add_logo()
        self._add_folder_selection()
        self._add_base_file_section()
        self._add_data_source_section()
        self._add_input_fields()
        self._add_dji_settings_section()
        self._add_action_buttons()
    
    def _add_version_header(self, layout):
        """Add version banner."""
        header = QHBoxLayout()
        header.setAlignment(Qt.AlignLeft)
        
        version_label = QLabel(f"{Config.APP_VERSION}  |  {Config.APP_BUILD_DATE}")
        version_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        version_label.setStyleSheet("color: #eaf6fa; background: transparent; padding: 4px;")
        header.addWidget(version_label)
        header.addStretch()
        
        layout.addLayout(header)
    
    def _add_logo(self):
        """Add logo image."""
        self.logo_label = QLabel()
        self.logo_label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        
        try:
            pixmap = QPixmap(Config.LOGO_PATH)
            if not pixmap.isNull():
                self.logo_label.setPixmap(
                    pixmap.scaled(550, 550, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
        except Exception:
            pass
        
        self.scroll_layout.addWidget(self.logo_label, alignment=Qt.AlignHCenter | Qt.AlignTop)
    
    def _add_folder_selection(self):
        """Add folder selection section."""
        layout = QHBoxLayout()
        layout.setAlignment(Qt.AlignHCenter)
        
        label = QLabel("Selected folder:")
        label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        label.setStyleSheet(Styles.LABEL_PRIMARY)
        label.setMinimumWidth(180)
        layout.addWidget(label)
        
        self.folder_display = QLabel("Path to 3D data folder")
        self.folder_display.setFont(QFont("Segoe UI", 12))
        self.folder_display.setStyleSheet(Styles.LABEL_SECONDARY)
        self.folder_display.setMinimumWidth(420)
        layout.addWidget(self.folder_display)
        
        clear_btn = self._create_button("Clear", Styles.BUTTON_DANGER, 10)
        clear_btn.clicked.connect(self._clear_folder)
        layout.addWidget(clear_btn)
        
        choose_btn = self._create_button("Choose Folder", Styles.BUTTON_PRIMARY, 12, bold=True)
        choose_btn.setMinimumWidth(130)
        choose_btn.clicked.connect(self._choose_folder)
        layout.addWidget(choose_btn)
        
        self.scroll_layout.addLayout(layout)
    
    def _add_base_file_section(self):
        """Add base file drop section."""
        layout = QHBoxLayout()
        layout.setAlignment(Qt.AlignHCenter)
        
        self.base_file_drop = QLabel("Drop Base Data file(s) here or click to select")
        self.base_file_drop.setFont(QFont("Segoe UI", 12))
        self.base_file_drop.setStyleSheet(Styles.LABEL_DROP)
        self.base_file_drop.setFixedHeight(110)
        self.base_file_drop.setAcceptDrops(True)
        self.base_file_drop.setWordWrap(True)
        self.base_file_drop.mousePressEvent = self._choose_base_file
        self.base_file_drop.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.base_file_drop, stretch=1)
        
        self.base_file_label = QLabel("Base data file not selected")
        self.base_file_label.setFont(QFont("Segoe UI", 12))
        self.base_file_label.setStyleSheet(
            "color: #113e59; background: #eaf6fa; border: none; border-radius: 6px; padding: 5px; margin: 5px;"
        )
        self.base_file_label.setFixedHeight(110)
        self.base_file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.base_file_label, stretch=1)
        
        clear_btn = self._create_button("Clear", Styles.BUTTON_DANGER, 10)
        clear_btn.clicked.connect(self._clear_base_file)
        layout.addWidget(clear_btn)
        
        self.scroll_layout.addLayout(layout)
        
        # Loading indicator
        self.base_drop_busy = self._create_progress_bar("Processing base file drop...")
        self.scroll_layout.addWidget(self.base_drop_busy)

        # Manual base processing row
        manual_row = QHBoxLayout()
        manual_row.setAlignment(Qt.AlignLeft)

        self._manual_base_check = QCheckBox("Manual base processing")
        self._manual_base_check.setFont(QFont("Segoe UI", 11))
        self._manual_base_check.setStyleSheet(
            "QCheckBox { color: #eaf6fa; background: transparent; border: none; } "
            "QCheckBox::indicator { width: 16px; height: 16px; }"
        )
        self._manual_base_check.toggled.connect(self._on_manual_base_toggled)
        manual_row.addWidget(self._manual_base_check)

        self._manual_base_csv_label = QLabel("No corrected base position CSV selected")
        self._manual_base_csv_label.setFont(QFont("Segoe UI", 11))
        self._manual_base_csv_label.setStyleSheet(
            "color: #eaf6fa; background: transparent; border: none; padding: 2px;"
        )
        self._manual_base_csv_label.hide()
        manual_row.addWidget(self._manual_base_csv_label, stretch=1)

        self._manual_base_csv_browse_btn = self._create_button("Browse CSV", Styles.BUTTON_PRIMARY, 10)
        self._manual_base_csv_browse_btn.clicked.connect(self._choose_base_ecef_csv)
        self._manual_base_csv_browse_btn.hide()
        manual_row.addWidget(self._manual_base_csv_browse_btn)

        self.scroll_layout.addLayout(manual_row)

        self.base_file_drop.installEventFilter(self)
    
    def _add_data_source_section(self):
        """Add data source drop section."""
        self.drop_label = QLabel("Drop Source Data Folders Here")
        self.drop_label.setFont(QFont("Segoe UI", 12))
        self.drop_label.setStyleSheet(Styles.LABEL_DROP)
        self.drop_label.setFixedHeight(128)
        self.drop_label.setAcceptDrops(True)
        self.drop_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.scroll_layout.addWidget(self.drop_label)
        self.drop_label.installEventFilter(self)
        
        # Loading indicator
        self.data_drop_busy = self._create_progress_bar("Processing source folder drop...")
        self.scroll_layout.addWidget(self.data_drop_busy)
        
        # Folder list
        self.data_source_list = QLabel("No data source folders selected.")
        self.data_source_list.setFont(QFont("Segoe UI", 11))
        self.data_source_list.setStyleSheet(Styles.LABEL_LIST)
        self.data_source_list.setWordWrap(True)
        self.data_source_list.hide()
        self.scroll_layout.addWidget(self.data_source_list)
        
        clear_btn = self._create_button("Clear drone data sources", Styles.BUTTON_DANGER, 10)
        clear_btn.clicked.connect(self._clear_data_sources)
        self.scroll_layout.addWidget(clear_btn)
        self.scroll_layout.setAlignment(clear_btn, Qt.AlignHCenter)
    
    def _add_input_fields(self):
        """Add client/project input fields."""
        layout = QHBoxLayout()
        layout.setAlignment(Qt.AlignHCenter)
        
        # Client
        self.client_label = QLabel("Client:")
        self.client_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.client_label.setStyleSheet(Styles.LABEL_TRANSPARENT)
        self.client_label.hide()
        layout.addWidget(self.client_label)
        
        self.client_input = QLineEdit()
        self.client_input.setFont(QFont("Segoe UI", 12))
        self.client_input.setStyleSheet(Styles.INPUT)
        self.client_input.setFixedWidth(180)
        self.client_input.hide()
        self.client_input.textChanged.connect(self._update_ready_state)
        layout.addWidget(self.client_input)
        
        # Project
        self.project_label = QLabel("Project:")
        self.project_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.project_label.setStyleSheet(Styles.LABEL_TRANSPARENT)
        self.project_label.hide()
        layout.addWidget(self.project_label)
        
        self.project_input = QLineEdit()
        self.project_input.setFont(QFont("Segoe UI", 12))
        self.project_input.setStyleSheet(Styles.INPUT)
        self.project_input.setFixedWidth(180)
        self.project_input.hide()
        self.project_input.textChanged.connect(self._update_ready_state)
        layout.addWidget(self.project_input)
        
        self.scroll_layout.addLayout(layout)

    def _add_dji_settings_section(self):
        """Add DJI Terra parameter fields inside a fixed-height box."""
        box = QWidget()
        box.setStyleSheet(
            "QWidget { background: #fffbe6; border: 2px solid #ffd457;"
            " border-radius: 6px; margin: 5px; }"
            " QLabel { border: none; margin: 0; }"
            " QLineEdit { border: 1px solid #ffd457; border-radius: 4px;"
            " background: #eaf6fa; color: #113e59; padding: 3px; margin: 2px; }"
            " QPushButton { border: 1px solid #ffd457; border-radius: 4px;"
            " background: #eaf6fa; color: #113e59; padding: 3px 8px; margin: 2px; }"
            " QCheckBox { border: none; color: #eaf6fa; margin: 2px; }"
        )
        inner = QVBoxLayout(box)
        inner.setContentsMargins(10, 6, 10, 6)
        inner.setSpacing(4)

        # Header
        header = QLabel("DJI Terra Parameters")
        header.setFont(QFont("Segoe UI", 12, QFont.Bold))
        header.setStyleSheet("color: #113e59; background: transparent;")
        header.setAlignment(Qt.AlignHCenter)
        inner.addWidget(header)

        # Toggles
        toggle_row = QHBoxLayout()
        toggle_row.setAlignment(Qt.AlignHCenter)
        self.dji_terra_toggle = QCheckBox("DJI Automate — LiDAR reconstruction")
        self.dji_terra_toggle.setFont(QFont("Segoe UI", 11))
        self.dji_terra_toggle.setStyleSheet("color: #113e59;")
        toggle_row.addWidget(self.dji_terra_toggle)
        inner.addLayout(toggle_row)
        self.dji_terra_toggle.stateChanged.connect(
            lambda state: self._gcp_section.setVisible(bool(state))
        )

        toggle_row2 = QHBoxLayout()
        toggle_row2.setAlignment(Qt.AlignHCenter)
        self.dji_ppk_toggle = QCheckBox("DJI Automate PPK — Visible light PPK")
        self.dji_ppk_toggle.setFont(QFont("Segoe UI", 11))
        self.dji_ppk_toggle.setStyleSheet("color: #113e59;")
        toggle_row2.addWidget(self.dji_ppk_toggle)
        inner.addLayout(toggle_row2)

        # GCP section — only visible when LiDAR toggle is checked
        self._gcp_section = QWidget()
        self._gcp_section.setStyleSheet("background: transparent; border: none;")
        self._gcp_section.setVisible(False)
        gcp_vbox = QVBoxLayout(self._gcp_section)
        gcp_vbox.setContentsMargins(0, 0, 0, 0)
        gcp_vbox.setSpacing(4)

        # "No Targets" toggle row
        no_target_row = QHBoxLayout()
        no_target_row.setAlignment(Qt.AlignHCenter)
        self._no_targets_check = QCheckBox("No Targets (run without GCP file)")
        self._no_targets_check.setFont(QFont("Segoe UI", 10))
        self._no_targets_check.setStyleSheet("color: #113e59; border: none;")
        no_target_row.addWidget(self._no_targets_check)
        gcp_vbox.addLayout(no_target_row)

        # GCP file row — hidden when No Targets is checked
        self._gcp_file_row = QWidget()
        self._gcp_file_row.setStyleSheet("background: transparent; border: none;")
        gcp_row = QHBoxLayout(self._gcp_file_row)
        gcp_row.setContentsMargins(0, 0, 0, 0)
        gcp_row.setAlignment(Qt.AlignHCenter)
        gcp_label = QLabel("GCP File:")
        gcp_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        gcp_label.setStyleSheet("color: #113e59; background: transparent;")
        gcp_row.addWidget(gcp_label)
        self.gcp_path_input = QLineEdit()
        self.gcp_path_input.setFont(QFont("Segoe UI", 10))
        self.gcp_path_input.setMinimumWidth(350)
        self.gcp_path_input.setPlaceholderText("Select GCP .csv file or drop here...")
        self.gcp_path_input.setReadOnly(True)
        self.gcp_path_input.setAcceptDrops(True)
        self.gcp_path_input.installEventFilter(self)
        gcp_row.addWidget(self.gcp_path_input)
        browse_btn = QPushButton("Browse...")
        browse_btn.setFont(QFont("Segoe UI", 10))
        browse_btn.clicked.connect(self._browse_gcp_file)
        gcp_row.addWidget(browse_btn)
        gcp_vbox.addWidget(self._gcp_file_row)

        self._no_targets_check.stateChanged.connect(
            lambda state: self._gcp_file_row.setVisible(not bool(state))
        )

        inner.addWidget(self._gcp_section)

        # EPSG row — label+input pairs are sub-grouped so the label sits flush against its field
        epsg_row = QHBoxLayout()
        epsg_row.setAlignment(Qt.AlignHCenter)
        epsg_row.setSpacing(6)

        epsg_h_pair = QHBoxLayout()
        epsg_h_pair.setSpacing(3)
        epsg_h_label = QLabel("EPSG Horizontal:")
        epsg_h_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        epsg_h_label.setStyleSheet("color: #113e59; background: transparent;")
        epsg_h_pair.addWidget(epsg_h_label)
        self.epsg_h_input = QLineEdit()
        self.epsg_h_input.setFont(QFont("Segoe UI", 11))
        self.epsg_h_input.setFixedWidth(100)
        self.epsg_h_input.setPlaceholderText("e.g. 6625")
        self.epsg_h_input.textChanged.connect(self._update_ready_state)
        epsg_h_pair.addWidget(self.epsg_h_input)
        epsg_row.addLayout(epsg_h_pair)

        epsg_h_search = QPushButton("\U0001f50d")
        epsg_h_search.setFixedWidth(40)
        epsg_h_search.setFont(QFont("Segoe UI", 9))
        epsg_h_search.clicked.connect(self._open_epsg_lookup)
        epsg_row.addWidget(epsg_h_search)
        epsg_row.addWidget(self._make_help_btn("Search EPSG horizontal codes"))

        epsg_detect_btn = QPushButton("Auto-Detect")
        epsg_detect_btn.setFont(QFont("Segoe UI", 9))
        epsg_detect_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        epsg_detect_btn.setContentsMargins(8, 0, 8, 0)
        epsg_detect_btn.clicked.connect(self._auto_detect_epsg)
        epsg_row.addWidget(epsg_detect_btn)
        epsg_row.addWidget(self._make_help_btn("Auto-detect State Plane zone from the selected GPS source"))

        self.epsg_src_image = QRadioButton("Image")
        self.epsg_src_image.setFont(QFont("Segoe UI", 9))
        self.epsg_src_image.setChecked(True)
        epsg_row.addWidget(self.epsg_src_image)
        self.epsg_src_base = QRadioButton("Base File")
        self.epsg_src_base.setFont(QFont("Segoe UI", 9))
        epsg_row.addWidget(self.epsg_src_base)
        self._epsg_src_group = QButtonGroup(self)
        self._epsg_src_group.addButton(self.epsg_src_image)
        self._epsg_src_group.addButton(self.epsg_src_base)

        epsg_v_pair = QHBoxLayout()
        epsg_v_pair.setSpacing(3)
        epsg_v_label = QLabel("EPSG Vertical:")
        epsg_v_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        epsg_v_label.setStyleSheet("color: #113e59; background: transparent;")
        epsg_v_pair.addWidget(epsg_v_label)
        self.epsg_v_input = QLineEdit()
        self.epsg_v_input.setFont(QFont("Segoe UI", 11))
        self.epsg_v_input.setFixedWidth(100)
        self.epsg_v_input.setText("6360")
        epsg_v_pair.addWidget(self.epsg_v_input)
        epsg_row.addLayout(epsg_v_pair)
        inner.addLayout(epsg_row)

        # 3DR auto-classification row — hidden until DJI Terra toggle is on
        classify_row = QHBoxLayout()
        classify_row.setAlignment(Qt.AlignHCenter)
        self._classify_3dr_widget = Classify3DRWidget()
        self._classify_3dr_widget.setVisible(False)
        classify_row.addWidget(self._classify_3dr_widget)
        inner.addLayout(classify_row)
        self.dji_terra_toggle.stateChanged.connect(
            lambda state: self._classify_3dr_widget.setVisible(bool(state))
        )

        self.scroll_layout.addWidget(box)

        # Pre-populate from DJI PARAMETERS.ini if it exists
        ini_path = os.path.normpath(os.path.join(
            os.path.dirname(Config.DJI_AUTOMATE_EXE), "..", "DJI PARAMETERS.ini"
        ))
        if os.path.isfile(ini_path):
            _cfg = configparser.ConfigParser()
            _cfg.read(ini_path, encoding="utf-8")
            if "parameters" in _cfg:
                p = _cfg["parameters"]
                if p.get("gcp_path"):
                    self.gcp_path_input.setText(p["gcp_path"])
                if p.get("epsg_horizontal"):
                    self.epsg_h_input.setText(p["epsg_horizontal"])
                if p.get("epsg_vertical"):
                    self.epsg_v_input.setText(p["epsg_vertical"])

    def _browse_gcp_file(self):
        """Open file dialog to select a GCP CSV file."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select GCP File", "", "CSV Files (*.csv);;All Files (*)"
        )
        if path:
            self.gcp_path_input.setText(os.path.normpath(path))

    def _on_gcp_drop(self, event):
        """Handle a file drop onto the GCP path field."""
        urls = event.mimeData().urls()
        if not urls:
            return
        path = os.path.normpath(urls[0].toLocalFile())
        if os.path.isfile(path):
            self.gcp_path_input.setText(path)

    def _add_action_buttons(self):
        """Add action buttons."""
        self.ready_label = QLabel("All fields selected - ready to create folders.")
        self.ready_label.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.ready_label.setStyleSheet(Styles.LABEL_SUCCESS)
        self.ready_label.setFixedHeight(60)
        self.ready_label.hide()
        self.scroll_layout.addWidget(self.ready_label)
        
        self.start_btn = QPushButton("Start data intake processes")
        self.start_btn.setFont(QFont("Segoe UI", 13, QFont.Bold))
        self.start_btn.setStyleSheet(Styles.BUTTON_MAIN)
        self.start_btn.clicked.connect(self._start_processing)
        self.start_btn.hide()
        self.scroll_layout.addWidget(self.start_btn)
        self.scroll_layout.setAlignment(self.start_btn, Qt.AlignHCenter)
    
    def _create_button(self, text: str, style: str, size: int, bold: bool = False) -> QPushButton:
        """Create a styled button."""
        btn = QPushButton(text)
        weight = QFont.Bold if bold else QFont.Normal
        btn.setFont(QFont("Segoe UI", size, weight))
        btn.setStyleSheet(style)
        return btn
    
    @staticmethod
    def _make_help_btn(tooltip_text: str) -> QPushButton:
        btn = QPushButton("?")
        btn.setFont(QFont("Segoe UI", 7, QFont.Bold))
        btn.setFixedSize(16, 16)
        btn.setStyleSheet(
            "QPushButton { color: #113e59; background: #ffd457; border-radius: 8px;"
            " border: none; padding: 0; font-weight: bold; }"
            " QPushButton:pressed { background: #eaf6fa; }"
        )
        btn.setCursor(Qt.ArrowCursor)
        btn.clicked.connect(lambda: QToolTip.showText(QCursor.pos(), tooltip_text))
        return btn

    def _create_progress_bar(self, text: str) -> QProgressBar:
        """Create a hidden progress bar."""
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(True)
        bar.setFormat(text)
        bar.setStyleSheet(Styles.PROGRESS_BAR)
        bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        bar.setFixedHeight(28)
        bar.hide()
        return bar
    
    def eventFilter(self, source, event):
        """Handle drag and drop events."""
        if event.type() == event.DragEnter and event.mimeData().hasUrls():
            event.accept()
            return True
        
        if event.type() == event.Drop:
            if source == self.drop_label:
                self._on_data_drop(event)
                return True
            elif source == self.base_file_drop:
                self._on_base_file_drop(event)
                return True
            elif source == self.gcp_path_input:
                self._on_gcp_drop(event)
                return True
        
        return super().eventFilter(source, event)
    
    def _choose_folder(self):
        """Open folder selection dialog."""
        folder = QFileDialog.getExistingDirectory(self, "Select 3dData Folder", "E:/Data")
        if folder:
            # Normalize path for Windows (fixes forward slashes in UNC paths)
            folder = os.path.normpath(folder)
            self.selected_folder = folder
            self.folder_display.setText(folder)
            self._save_last_folder(folder)
        else:
            self._show_message(QMessageBox.Warning, "Folder Required", "Please select a folder.")
    
    def _choose_base_file(self, event):
        """Open base file selection dialog."""
        file_filter = (
            "Base Data Files (*.T02 *.T04 *.t02 *.t04 *.??o *.??n *.??g "
            "*.o *.n *.g *.rnx *.obs *.crx *.mix *.nav);;All Files (*)"
        )
        files, _ = QFileDialog.getOpenFileNames(self, "Select Base Data file(s)", "", file_filter)
        if files:
            self._add_base_files(files)
    
    def _on_base_file_drop(self, event):
        """Handle base file drop."""
        self._show_loading(self.base_drop_busy)
        try:
            files = [
                url.toLocalFile() for url in event.mimeData().urls()
                if os.path.isfile(url.toLocalFile()) and 
                   (url.toLocalFile().lower().endswith(Config.BASE_DATA_EXTENSIONS) or 
                    FileOperations.is_rinex_file(url.toLocalFile()))
            ]
            if files:
                self._add_base_files(files)
        finally:
            self._hide_loading(self.base_drop_busy)
    
    def _add_base_files(self, files: List[str]):
        """Add base data files."""
        added = False
        for file in files:
            if (os.path.isfile(file) and 
                file not in self.base_data_paths and
                (file.lower().endswith(Config.BASE_DATA_EXTENSIONS) or 
                 FileOperations.is_rinex_file(file))):
                self.base_data_paths.append(file)
                added = True
        
        if added:
            self.base_data_is_rinex = all(
                FileOperations.is_rinex_file(f) for f in self.base_data_paths
            )
            names = [os.path.basename(f) for f in self.base_data_paths]
            self.base_file_label.setText(f"Selected ({len(names)}): {', '.join(names)}")
            self._show_input_fields()
            self._update_ready_state()
    
    def _clear_folder(self):
        """Clear the output folder selection."""
        self.selected_folder = ""
        if self.folder_display:
            self.folder_display.setText("Path to 3D data folder")

    def _clear_base_file(self):
        """Clear base file selection."""
        self.base_data_paths = []
        self.base_data_is_rinex = False
        if self.base_file_label:
            self.base_file_label.setText("Base data file not selected")
        self._update_ready_state()

    def _on_manual_base_toggled(self, checked: bool):
        """Show/hide the CSV picker row when the manual base checkbox changes."""
        if self._manual_base_csv_label:
            self._manual_base_csv_label.setVisible(checked)
        if self._manual_base_csv_browse_btn:
            self._manual_base_csv_browse_btn.setVisible(checked)
        if not checked:
            self._manual_base_csv_path = None
            if self._manual_base_csv_label:
                self._manual_base_csv_label.setText("No corrected base position CSV selected")
                self._manual_base_csv_label.setStyleSheet(
                    "color: #eaf6fa; background: transparent; border: none; padding: 2px;"
                )

    def _choose_base_ecef_csv(self):
        """Open a file dialog for the corrected base position CSV and validate its format."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Corrected Base Position CSV", "", "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            _parse_base_ecef_csv(path)
        except ValueError as exc:
            self._show_message(
                QMessageBox.Critical, "Invalid Base Position CSV",
                f"The selected file does not match the required format:\n\n{exc}\n\n"
                f"Expected columns: {', '.join(_BASE_ECEF_EXPECTED_HEADERS)}"
            )
            self._manual_base_csv_path = None
            if self._manual_base_csv_label:
                self._manual_base_csv_label.setText("No corrected base position CSV selected")
                self._manual_base_csv_label.setStyleSheet(
                    "color: #eaf6fa; background: transparent; border: none; padding: 2px;"
                )
            return
        self._manual_base_csv_path = path
        if self._manual_base_csv_label:
            self._manual_base_csv_label.setText(os.path.basename(path))
            self._manual_base_csv_label.setStyleSheet(
                "color: #5cdb95; background: transparent; border: none; padding: 2px;"
            )

    def _open_epsg_lookup(self):
        dlg = EPSGLookupDialog(self)
        if dlg.exec_() == QDialog.Accepted and dlg.selected_h_code:
            self.epsg_h_input.setText(dlg.selected_h_code)
            if dlg.selected_v_code:
                self.epsg_v_input.setText(dlg.selected_v_code)

    def _run_t02_with_loading_popup(self, candidate: str) -> Optional[Tuple[float, float]]:
        """Run T02/T04 → RINEX conversion in a background thread behind a loading dialog."""
        result_holder: list = [None]

        dlg = QDialog(self)
        dlg.setWindowTitle("Converting Base File")
        dlg.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        dlg.setModal(True)
        dlg.setStyleSheet(Styles.DIALOG_SENSOR)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)

        msg = QLabel(
            f"Converting base file to RINEX…\n\n"
            f"{os.path.basename(candidate)}\n\n"
            "This may take up to 30 seconds."
        )
        msg.setFont(QFont("Segoe UI", 11))
        msg.setAlignment(Qt.AlignCenter)
        layout.addWidget(msg)

        bar = QProgressBar()
        bar.setRange(0, 0)  # indeterminate spinner
        bar.setTextVisible(False)
        bar.setFixedHeight(18)
        bar.setStyleSheet(Styles.PROGRESS_BAR)
        layout.addWidget(bar)

        dlg.adjustSize()
        dlg.show()
        QApplication.processEvents()

        loop = QEventLoop()
        thread = T02CoordThread(candidate)

        def _on_done(res):
            result_holder[0] = res
            loop.quit()

        thread.result_ready.connect(_on_done)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        loop.exec_()

        dlg.close()
        return result_holder[0]

    def _auto_detect_epsg(self):
        """Auto-detect State Plane EPSG from GPS in a drone image or RINEX base file."""
        if not os.path.isfile(Config.STATEPLANE_SHAPEFILE):
            self._show_message(
                QMessageBox.Warning, "Shapefile Not Found",
                f"State Plane shapefile not found:\n{Config.STATEPLANE_SHAPEFILE}"
            )
            return

        use_image = self.epsg_src_image.isChecked()

        if use_image:
            if not self.data_source_folders:
                self._show_message(QMessageBox.Warning, "No Source Data",
                                   "Drop drone source folders first.")
                return
        else:
            if not self.base_data_paths:
                self._show_message(QMessageBox.Warning, "No Base File",
                                   "Select a base data file first.")
                return

        # Loading dialog — mirrors _run_t02_with_loading_popup
        result_holder: list = [None]

        dlg = QDialog(self)
        dlg.setWindowTitle("Detecting EPSG")
        dlg.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        dlg.setModal(True)
        dlg.setStyleSheet(Styles.DIALOG_SENSOR)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)

        src_label = "drone image GPS EXIF" if use_image else "base file coordinates"
        lbl = QLabel(f"Detecting State Plane zone\nfrom {src_label}…\n\nThis may take a moment.")
        lbl.setFont(QFont("Segoe UI", 11))
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)

        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        bar.setFixedHeight(18)
        bar.setStyleSheet(Styles.PROGRESS_BAR)
        layout.addWidget(bar)

        dlg.adjustSize()
        dlg.show()
        QApplication.processEvents()

        loop = QEventLoop()
        thread = EpsgDetectThread(
            use_image,
            list(self.base_data_paths),
            list(self.data_source_folders),
        )

        def _on_done(res):
            result_holder[0] = res
            loop.quit()

        thread.result_ready.connect(_on_done)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        loop.exec_()

        dlg.close()

        detection = result_holder[0]
        if detection is None:
            if use_image:
                self._show_message(
                    QMessageBox.Warning, "No GPS in Image",
                    "Could not read GPS EXIF from any image in the selected source folders."
                )
            else:
                self._show_message(
                    QMessageBox.Warning, "No Coordinates Found",
                    "Could not extract GPS coordinates from the base file.\n\n"
                    "• RINEX obs files require APPROX POSITION XYZ in the header\n"
                    "• T02/T04 requires convertToRINEX.exe and may take ~30 seconds\n\n"
                    "Try switching to the Image source."
                )
            return

        lat, lon, epsg_code, zone_name, source_desc = detection
        if epsg_code is None:
            self._show_message(
                QMessageBox.Warning, "Zone Not Found",
                f"No US State Plane zone found for:\n"
                f"Lat {lat:.5f}°   Lon {lon:.5f}°\n\n"
                "Coordinates may be outside State Plane coverage."
            )
            return

        self.epsg_h_input.setText(epsg_code)

        # Look up the matching vertical EPSG from the zone table and apply it,
        # mirroring what the manual EPSG lookup dialog does on accept.
        v_code = next(
            (row[3] for row in EPSGLookupDialog._SPCZONE if row[2] == epsg_code),
            None
        )
        if v_code and self.epsg_v_input:
            self.epsg_v_input.setText(v_code)

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle("Zone Detected")
        v_line = f"EPSG-V:  {v_code}" if v_code else "EPSG-V:  (not found — check manually)"
        msg.setText(
            f"Source:  {source_desc}\n"
            f"Coords:  {lat:.5f}°N   {lon:.5f}°E\n\n"
            f"Zone:    {zone_name}\n"
            f"EPSG-H:  {epsg_code}\n"
            f"{v_line}"
        )
        msg.setStyleSheet(Styles.MESSAGEBOX)
        msg.exec_()

    def _on_data_drop(self, event):
        """Handle data source folder drop."""
        try:
            for url in event.mimeData().urls():
                path = url.toLocalFile()
                if os.path.isdir(path) and path not in self.data_source_folders:
                    self.data_source_folders.append(path)
            
            if not self.data_source_folders:
                self._show_message(QMessageBox.Warning, "No Folders", "No valid folders dropped.")
                return
            
            self.drop_label.setText(f"{len(self.data_source_folders)} folder(s) selected:")
            self.data_source_list.setText("\n".join(self.data_source_folders))
            self.data_source_list.show()
            self._prune_flight_correction_state()
            self._update_ready_state()
            
            self._show_loading(self.data_drop_busy)
            self.data_drop_busy.setFormat("Scanning flight photos (ExifTool — Rtk Flag)...")
            self._start_rtk_flag_scan_async()
        except Exception as e:
            self._show_message(QMessageBox.Critical, "Error", str(e))
            self.data_source_folders = []
            self._hide_loading(self.data_drop_busy)
    
    def _start_rtk_flag_scan_async(self) -> None:
        """Run ExifTool scan off the GUI thread (prevents Windows not-responding / crash dialogs)."""
        self._rtk_scan_gen += 1
        gen = self._rtk_scan_gen
        folders = list(self.data_source_folders)
        thread = ExifRtkScanThread(folders, gen)
        self._rtk_scan_thread = thread
        thread.scan_finished.connect(self._on_exif_rtk_scan_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()
    
    def _norm_flight_folder(self, path: str) -> str:
        return FlightPhotoExifAnalyzer._norm_flight_root(path)

    def _prune_flight_correction_state(self) -> None:
        """Drop cached Exif slices for flight folders no longer in the selection."""
        keys = {self._norm_flight_folder(p) for p in self.data_source_folders}
        self._per_folder_rtk_scan = {
            k: v for k, v in self._per_folder_rtk_scan.items() if k in keys
        }

    def _on_exif_rtk_scan_finished(
        self, generation: int, scan: RtkFlagScanResult, per_folder: object
    ) -> None:
        if generation != self._rtk_scan_gen:
            return
        self.data_drop_busy.setFormat("Processing source folder drop...")
        self._hide_loading(self.data_drop_busy)
        per_map = per_folder if isinstance(per_folder, dict) else {}
        self._per_folder_rtk_scan = {
            self._norm_flight_folder(k): v
            for k, v in per_map.items()
            if isinstance(v, RtkFlagScanResult)
        }
        self._prune_flight_correction_state()
        self._show_combined_rtk_flag_results_dialog(scan, per_map)
        self._update_ready_state()
    
    def _clear_data_sources(self):
        """Clear data source selection."""
        self._rtk_scan_gen += 1
        self._hide_loading(self.data_drop_busy)
        self.data_source_folders = []
        if self.data_source_list:
            self.data_source_list.setText("No data source folders selected.")
            self.data_source_list.hide()
        if self.drop_label:
            self.drop_label.setText("Drop Source Data Folders Here")
        self._per_folder_rtk_scan = {}
        self._update_ready_state()
    
    def _show_input_fields(self):
        """Show client/project input fields."""
        self.client_label.show()
        self.client_input.show()
        self.project_label.show()
        self.project_input.show()
    
    def _update_ready_state(self):
        """Update UI based on readiness to process."""
        # Safety check - widgets may not exist during init
        if not all([self.ready_label, self.start_btn, self.client_input, self.project_input, self.epsg_h_input]):
            return

        # Use all() to get a proper boolean (not the last truthy value)
        ready = all([
            self.client_input.text().strip(),
            self.project_input.text().strip(),
            self.data_source_folders,
            self.base_data_paths,
            self.epsg_h_input.text().strip(),
        ])
        self.ready_label.setVisible(ready)
        self.start_btn.setVisible(ready)
    
    def _show_loading(self, bar: QProgressBar):
        """Show loading indicator."""
        bar.setRange(0, 0)
        bar.show()
        QApplication.processEvents()
    
    def _hide_loading(self, bar: QProgressBar):
        """Hide loading indicator."""
        bar.hide()
        bar.setRange(0, 100)
    
    @staticmethod
    def _html_rtk_scan_metrics(scan: RtkFlagScanResult) -> str:
        parts: List[str] = []
        if scan.exiftool_error:
            parts.append(
                f"<span style='color:#ffd457;'><b>ExifTool:</b> "
                f"{html.escape(scan.exiftool_error, quote=True)}</span>"
            )
        parts.append(f"JPEGs scanned: {scan.total_photos}")
        parts.append(f"Photos with Rtk Flag value: {len(scan.values)}")
        n50 = scan.count_rtk_flag_equal_50()
        pct = scan.pct_rtk_flag_50_of_all_images()
        if pct is not None:
            parts.append(
                f"Rtk Flag = 50: {n50} of {scan.total_photos} images ({pct:.1f}%)"
            )
        else:
            parts.append("Rtk Flag = 50: — (no JPEGs scanned)")
        parts.append(f"Min / max Rtk Flag: {scan.min_max_text}")
        return "<br/>".join(parts)

    def _show_combined_rtk_flag_results_dialog(
        self,
        aggregate: RtkFlagScanResult,
        per_map: Dict[str, RtkFlagScanResult],
    ) -> None:
        """
        One dialog after ExifTool: combined summary + per-flight list (no PPK buttons).
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("Rtk Flag — flight photos")
        dlg.setModal(True)
        dlg.setStyleSheet(Styles.DIALOG_SENSOR)
        layout = QVBoxLayout(dlg)
        sum_lbl = QLabel(
            "<b>All flights (combined)</b><br/><br/>"
            + self._html_rtk_scan_metrics(aggregate)
        )
        sum_lbl.setWordWrap(True)
        sum_lbl.setTextFormat(Qt.RichText)
        layout.addWidget(sum_lbl)
        by_lbl = QLabel("<b>By flight folder</b>")
        by_lbl.setTextFormat(Qt.RichText)
        layout.addWidget(by_lbl)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner_l = QVBoxLayout(inner)
        for p in self.data_source_folders:
            fk = self._norm_flight_folder(p)
            sub = per_map.get(fk)
            if sub is None or not isinstance(sub, RtkFlagScanResult):
                sub = RtkFlagScanResult(0, [], aggregate.exiftool_error)
            esc_p = html.escape(p, quote=True)
            block = (
                f"<b>{esc_p}</b><br/><br/>"
                + self._html_rtk_scan_metrics(sub)
            )
            sep = QLabel(
                "<hr style='border:0;border-top:1px solid #ffd457;margin:8px 0;'/>"
                + block
            )
            sep.setWordWrap(True)
            sep.setTextFormat(Qt.RichText)
            inner_l.addWidget(sep)
        inner_l.addStretch()
        scroll.setWidget(inner)
        scroll.setMinimumHeight(240)
        layout.addWidget(scroll)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn, alignment=Qt.AlignHCenter)
        dlg.exec_()

    def _dji_auto_active(self) -> bool:
        """True when either DJI automation checkbox is checked (autonomous run in progress)."""
        return (
            bool(self.dji_terra_toggle and self.dji_terra_toggle.isChecked()) or
            bool(self.dji_ppk_toggle   and self.dji_ppk_toggle.isChecked())
        )

    def _show_message(self, icon, title: str, text: str):
        """Show a message dialog. Suppressed for non-errors during DJI autonomous runs."""
        if self._dji_auto_active() and icon != QMessageBox.Critical:
            print(f"[suppressed popup] {title}: {text}")
            return
        msg = QMessageBox(self)
        msg.setIcon(icon)
        msg.setWindowTitle(title)
        msg.setText(text)
        msg.setStyleSheet(Styles.MESSAGEBOX)
        msg.exec_()
    
    def _save_last_folder(self, path: str):
        """Save last used folder."""
        try:
            os.makedirs(os.path.dirname(Config.LAST_FOLDER_FILE), exist_ok=True)
            with open(Config.LAST_FOLDER_FILE, "w", encoding="utf-8") as f:
                f.write(path)
        except Exception as e:
            print(f"Could not save last folder: {e}")
    
    def _load_last_folder(self):
        """Load last used folder."""
        try:
            if os.path.isfile(Config.LAST_FOLDER_FILE):
                with open(Config.LAST_FOLDER_FILE, "r", encoding="utf-8") as f:
                    folder = f.read().strip()
                if folder:
                    # Normalize path for Windows
                    folder = os.path.normpath(folder)
                    if os.path.isdir(folder):
                        self.selected_folder = folder
                        self.folder_display.setText(folder)
        except Exception as e:
            print(f"Could not load last folder: {e}")
    
    def _start_processing(self):
        """Start data processing."""
        # Validate inputs
        if not self.selected_folder:
            self._show_message(QMessageBox.Warning, "No Folder", "Please choose a folder.")
            return
        if not self.data_source_folders:
            self._show_message(QMessageBox.Warning, "No Data", "Please select data source folders.")
            return
        if not self.base_data_paths:
            self._show_message(QMessageBox.Warning, "No Base File", "Please select base data files.")
            return
        
        client = self.client_input.text().strip()
        project = self.project_input.text().strip()
        if not client or not project:
            self._show_message(QMessageBox.Warning, "Missing Info", "Enter client and project names.")
            return

        if not (self.epsg_h_input and self.epsg_h_input.text().strip()):
            self._show_message(
                QMessageBox.Warning, "Coordinate System Required",
                "Please select a coordinate system (EPSG Horizontal) before starting intake.\n\n"
                "Use the \U0001f50d button to look up your State Plane zone."
            )
            return

        no_targets = bool(self._no_targets_check and self._no_targets_check.isChecked())
        if (self.dji_terra_toggle and self.dji_terra_toggle.isChecked()
                and not no_targets
                and self.gcp_path_input and not self.gcp_path_input.text().strip()):
            self._show_message(
                QMessageBox.Warning, "GCP File Required",
                "A GCP .csv file is required when 'DJI Automate — LiDAR reconstruction' is enabled.\n\n"
                "Check 'No Targets' to run without a GCP file, or select a GCP file."
            )
            return
        
        # Detect sensor
        result = SensorDetector.detect_from_folders(self.data_source_folders)
        if not result.sensor_choice:
            error_msg = "Could not determine sensor from image metadata."
            if result.exif_model:
                error_msg += f"\n\nEXIF Model: '{result.exif_model}' (not supported)"
                error_msg += f"\n\nSupported: {list(Config.EXIF_MODEL_TO_SENSOR.keys())}"
            if result.image_path:
                error_msg += f"\n\nImage: {result.image_path}"
            self._show_message(QMessageBox.Critical, "Sensor Not Detected", error_msg)
            return

        # Validate manual base position CSV if enabled
        base_ecef_xyz: Optional[Tuple[float, float, float]] = None
        if self._manual_base_check and self._manual_base_check.isChecked():
            if not self._manual_base_csv_path:
                self._show_message(
                    QMessageBox.Warning, "Base Position CSV Required",
                    "Manual base processing is enabled but no CSV file has been selected.\n\n"
                    "Click 'Browse CSV' to choose your corrected base position file."
                )
                return
            try:
                base_ecef_xyz = _parse_base_ecef_csv(self._manual_base_csv_path)
            except ValueError as exc:
                self._show_message(
                    QMessageBox.Critical, "Invalid Base Position CSV", str(exc)
                )
                return

        # Start worker
        self.start_btn.setEnabled(False)
        self.start_btn.setText("Processing...")

        self.processing_worker = ProcessingWorker(
            self.selected_folder, self.data_source_folders, self.base_data_paths,
            client, project, result.sensor_choice, self.base_data_is_rinex,
            base_ecef_xyz=base_ecef_xyz,
        )
        
        self.processing_worker.file_copy_progress.connect(self._update_progress)
        self.processing_worker.status_update.connect(self._update_status)
        self.processing_worker.error_occurred.connect(self._handle_error)
        self.processing_worker.processing_complete.connect(self._handle_complete)
        self.processing_worker.finished.connect(self._cleanup_worker)
        
        self.processing_worker.start()
    
    def _update_progress(self, current: int, total: int, name: str):
        """Update progress bar."""
        if not self.current_progress_bar:
            self._setup_progress_bar(total)
        
        if self.current_progress_bar:
            self.current_progress_bar.setMaximum(total)
            self.current_progress_bar.setValue(current)
        
        if self.current_progress_label:
            self.current_progress_label.setText(f"Processing: {name} ({current}/{total})")
        
        QApplication.processEvents()
    
    def _update_status(self, message: str):
        """Update status display."""
        logger.info(message)
        if self.current_progress_label:
            self.current_progress_label.setText(message)
            QApplication.processEvents()
    
    def _setup_progress_bar(self, maximum: int):
        """Create progress bar widgets."""
        self.current_progress_bar = QProgressBar()
        self.current_progress_bar.setMaximum(maximum)
        self.current_progress_bar.setTextVisible(True)
        self.current_progress_bar.setAlignment(Qt.AlignCenter)
        self.current_progress_bar.setFixedHeight(35)
        self.current_progress_bar.setStyleSheet(Styles.PROGRESS_BAR)
        
        self.current_progress_label = QLabel("Processing...")
        self.current_progress_label.setFont(QFont("Segoe UI", 14, QFont.Bold))
        self.current_progress_label.setStyleSheet(Styles.LABEL_PROGRESS)
        
        layout = self.central_widget.layout()
        layout.addWidget(self.current_progress_label)
        layout.addWidget(self.current_progress_bar)
    
    def _cleanup_progress_bar(self):
        """Remove progress bar widgets."""
        if self.current_progress_bar and self.current_progress_label:
            layout = self.central_widget.layout()
            self.current_progress_label.hide()
            self.current_progress_bar.hide()
            layout.removeWidget(self.current_progress_label)
            layout.removeWidget(self.current_progress_bar)
            self.current_progress_label.deleteLater()
            self.current_progress_bar.deleteLater()
            self.current_progress_label = None
            self.current_progress_bar = None
    
    def _handle_error(self, message: str):
        """Handle processing error."""
        logger.error(f"Processing error: {message}")
        self._cleanup_progress_bar()
        
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Critical)
        msg.setWindowTitle("Processing Error")
        msg.setText(f"Error:\n\n{message}")
        msg.setStyleSheet(Styles.MESSAGEBOX_ERROR)
        msg.exec_()
        
        self._reset_ui()
    
    def _handle_complete(self, client, project, date, sensor_path, image, files, date_path):
        """Handle processing completion."""
        logger.info("Processing finished successfully")
        self._cleanup_progress_bar()
        
        project_path = os.path.join(self.selected_folder, client, project)
        msg_text = (
            f"Created folders under:\n{sensor_path}\n\n"
            f"Files processed: {files}\n"
            f"Folders processed: {len(self.data_source_folders)}\n"
            f"Project path: {project_path}"
        )
        
        # Open PDFs
        for root, _, files_list in os.walk(date_path):
            for file in files_list:
                if file.lower().endswith('.pdf'):
                    try:
                        os.startfile(os.path.join(root, file))
                    except Exception as e:
                        print(f"Could not open PDF: {e}")
        
        # Play sound
        try:
            QSound.play(Config.SOUND_PATH)
        except Exception:
            pass
        
        # Show completion message (skipped during DJI autonomous runs)
        if not self._dji_auto_active():
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("Process Completed")
            msg.setText(msg_text)
            msg.setStyleSheet(Styles.MESSAGEBOX)
            msg.setStandardButtons(QMessageBox.Ok)
            msg.exec_()
        else:
            print(f"[autonomous] intake complete — {msg_text}")
        
        # Open project folder after dialog closes
        # Normalize path for Windows (fixes mixed slashes in UNC paths)
        project_path = os.path.normpath(project_path)
        print(f"Opening project folder: {project_path}")
        if os.path.isdir(project_path):
            try:
                os.startfile(project_path)
            except Exception as e:
                print(f"Could not open folder: {e}")
        else:
            print(f"Project path does not exist: {project_path}")
        
        self._reset_ui()

        terra_folder = os.path.join(date_path, "Terra")
        ppk_folder   = os.path.join(date_path, "PPK")
        gcp    = self.gcp_path_input.text().strip() if self.gcp_path_input else ""
        epsg_h = self.epsg_h_input.text().strip()   if self.epsg_h_input   else ""
        epsg_v = self.epsg_v_input.text().strip()   if self.epsg_v_input   else ""

        # Resolve the actual DJI flight subfolder — intake copies each source as
        # sensor_path/<folder_name>/, so pass that specific subfolder to DJI Terra
        try:
            _subs = sorted([
                os.path.join(sensor_path, d) for d in os.listdir(sensor_path)
                if os.path.isdir(os.path.join(sensor_path, d))
            ])
            dji_data_source = _subs[0] if len(_subs) == 1 else sensor_path
        except Exception:
            dji_data_source = sensor_path

        # Build DJI command sequence — DJIAutomatePPKV2 (PPK) always before PyAutomateDJI (terra)
        log_path = (
            self.processing_worker.log_file_path
            if self.processing_worker and self.processing_worker.log_file_path
            else None
        )
        dji_cmds = []
        if self.dji_ppk_toggle and self.dji_ppk_toggle.isChecked():
            cmd = [
                Config.DJI_AUTOMATE_PPK_EXE,
                "--project-name",     f"{client}_{project}_{date}",
                "--project-location", ppk_folder,
                "--data-source",      dji_data_source,
                "--terra-path",       terra_folder,
                "--ppk-path",         ppk_folder,
            ]
            if epsg_h:    cmd.extend(["--epsg-h",    epsg_h])
            if epsg_v:    cmd.extend(["--epsg-v",    epsg_v])
            if log_path:  cmd.extend(["--log-file",  log_path])
            dji_cmds.append(cmd)

        if self.dji_terra_toggle and self.dji_terra_toggle.isChecked():
            cmd = [
                Config.DJI_AUTOMATE_EXE,
                "--project-name",     f"{client}_{project}_{date}",
                "--project-location", terra_folder,
                "--data-source",      sensor_path,
            ]
            if gcp:       cmd.extend(["--gcp-path",  gcp])
            if epsg_h:    cmd.extend(["--epsg-h",    epsg_h])
            if epsg_v:    cmd.extend(["--epsg-v",    epsg_v])
            if log_path:  cmd.extend(["--log-file",  log_path])
            if self._no_targets_check and self._no_targets_check.isChecked():
                cmd.append("--no-targets")
            dji_cmds.append(cmd)

        if dji_cmds:
            self._pending_3dr_terra_folder = terra_folder
            self._pending_3dr_project_name = f"{client}_{project}_{date}_LiDAR"
            self._setup_progress_bar(len(dji_cmds))
            self._update_status("Starting DJI automation sequence...")
            self._dji_thread = DJISequenceThread(dji_cmds, self)
            self._dji_thread.status_update.connect(self._update_status)
            self._dji_thread.sequence_complete.connect(self._on_dji_sequence_complete)
            self._dji_thread.start()

    def _on_dji_sequence_complete(self):
        """Called when DJI EXE sequence finishes. Starts 3DR classification if enabled."""
        widget        = self._classify_3dr_widget
        terra         = self._pending_3dr_terra_folder
        project_name  = self._pending_3dr_project_name
        if widget and widget.is_enabled and widget.selected_model and terra and project_name:
            model = widget.selected_model
            self._update_status(f"[3DR] Starting auto-classification — model: {model}")
            self._classify_3dr_thread = Classify3DRThread(terra, model, project_name=project_name, parent=self)
            self._classify_3dr_thread.status_update.connect(self._update_status)
            self._classify_3dr_thread.classification_complete.connect(
                lambda *_: self._play_completion_sound()
            )
            self._classify_3dr_thread.finished.connect(
                self._classify_3dr_thread.deleteLater
            )
            self._classify_3dr_thread.start()
        else:
            self._play_completion_sound()

    def _play_completion_sound(self):
        self._cleanup_progress_bar()
        try:
            QSound.play(Config.SOUND_PATH)
        except Exception:
            pass

    def _cleanup_worker(self):
        """Clean up worker thread."""
        if self.processing_worker:
            self.processing_worker.deleteLater()
            self.processing_worker = None
    
    def _reset_ui(self):
        """Reset UI after processing."""
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Start data intake processes")


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def main():
    """Application entry point."""
    app = QApplication(sys.argv)
    window = DataIntakeUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
