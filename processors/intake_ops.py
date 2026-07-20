"""Qt-free port of data_intake.py's file/RINEX machinery for the INTAKE job.

Source of truth: FileOperations, FolderStructureBuilder, RinexProcessor and
ProcessingWorker in data_intake.py (v2.4.4). Behavior is kept 1:1 except:

  * Progress/status go through plain callables instead of Qt signals.
  * The Trimble converter path comes from agent config (payload_paths),
    not a hardcoded C:\\ path.
  * copy_tree() is RESUMABLE: a destination file that already exists with the
    same size is skipped (a retried/recovered intake picks up where it left
    off). A same-name file with a DIFFERENT size still gets the GUI's
    dedup-suffix copy (never overwrite data), so nothing is ever lost.
"""
from __future__ import annotations

import csv
import glob
import os
import shutil
import subprocess
from datetime import datetime
from typing import Callable, Optional

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
RINEX_EXTENSIONS = ("o", "n", "g", "p", "l", "s", "obs", "rnx", "crx", "mix", "nav")
BASE_DATA_EXTENSIONS = (".t02", ".t04", ".t0b")
COPY_BUFFER_SIZE = 4 * 1024 * 1024
SUBPROCESS_TIMEOUT = 1800

# Folder templates — identical to FolderStructureBuilder in data_intake.py.
LIDAR_TEMPLATE = {"BaseData": {}, "PPK": {}, "Pix4d": {}, "TerraArchive": {}, "Terra": {}}
STANDARD_TEMPLATE = {"BaseData": {}, "Pix4d": {}, "Terra": {}, "PPK": {}}

StatusFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Folder structure
# ---------------------------------------------------------------------------

def build_structure(client: str, project: str, date: str, sensor: str) -> dict:
    if sensor in ("L2", "L3"):
        inner = {**LIDAR_TEMPLATE, sensor: {}}
    else:
        inner = {**STANDARD_TEMPLATE, sensor: {}}
    return {client: {project: {date: inner}}}


def create_folder_structure(base_path: str, structure: dict) -> None:
    for name, subdict in structure.items():
        path = os.path.join(base_path, name)
        os.makedirs(path, exist_ok=True)
        if subdict:
            create_folder_structure(path, subdict)


def sensor_folder_path(base: str, client: str, project: str, date: str, sensor: str) -> str:
    return os.path.join(base, client, project, date, sensor)


def date_folder_path(base: str, client: str, project: str, date: str) -> str:
    return os.path.join(base, client, project, date)


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------

def copy_file(source: str, dest: str, buffer_size: int = COPY_BUFFER_SIZE,
              on_status: StatusFn = _noop) -> bool:
    try:
        with open(source, "rb") as src_f, open(dest, "wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f, length=buffer_size)
        shutil.copystat(source, dest, follow_symlinks=True)
        return True
    except Exception as exc:
        on_status(f"COPY FAILED {source}: {exc}")
        return False


def _dedup_dest(target_folder: str, file_name: str) -> str:
    base_name, ext = os.path.splitext(file_name)
    dest = os.path.join(target_folder, file_name)
    counter = 1
    while os.path.exists(dest):
        dest = os.path.join(target_folder, f"{base_name}_{counter}{ext}")
        counter += 1
    return dest


def dest_has_copy(source_file: str, target_folder: str) -> bool:
    """True when target_folder already holds a same-size copy of source_file
    (under its own name or any dedup-suffixed variant)."""
    try:
        src_size = os.path.getsize(source_file)
    except OSError:
        return False
    file_name = os.path.basename(source_file)
    base_name, ext = os.path.splitext(file_name)
    candidates = [os.path.join(target_folder, file_name)]
    candidates += glob.glob(os.path.join(glob.escape(target_folder), f"{glob.escape(base_name)}_*{ext}"))
    for cand in candidates:
        try:
            if os.path.isfile(cand) and os.path.getsize(cand) == src_size:
                return True
        except OSError:
            continue
    return False


def count_files(folders: list[str]) -> int:
    return sum(len(files) for folder in folders for _, _, files in os.walk(folder))


def copy_tree(
    source_folder: str,
    dest_root: str,
    *,
    on_file: Callable[[str], None] = _noop,
    cancelled: Callable[[], bool] = lambda: False,
    on_status: StatusFn = _noop,
) -> tuple[int, int, Optional[str]]:
    """Copy source_folder into dest_root/<basename(source_folder)>/…,
    mirroring ProcessingWorker._copy_source_data for one source.

    Per-file copy failures are reported through on_status (with the path and
    the OS error) and do not stop the tree walk — validate_outputs is the
    final judge of completeness.

    Returns (files_copied, files_skipped_as_present, first_image_path).
    """
    folder_name = os.path.basename(os.path.normpath(source_folder))
    copied = skipped = 0
    first_image: Optional[str] = None

    for root_dir, _, _ in os.walk(source_folder):
        rel_path = os.path.relpath(root_dir, source_folder)
        os.makedirs(os.path.join(dest_root, folder_name, rel_path), exist_ok=True)

    for root_dir, _, files in os.walk(source_folder):
        for file in files:
            if cancelled():
                return copied, skipped, first_image
            source_file = os.path.join(root_dir, file)
            rel_path = os.path.relpath(root_dir, source_folder)
            target_folder = os.path.join(dest_root, folder_name, rel_path)

            if first_image is None and file.lower().endswith(IMAGE_EXTENSIONS):
                first_image = source_file

            if dest_has_copy(source_file, target_folder):
                skipped += 1
                on_file(file)
                continue
            if copy_file(source_file, _dedup_dest(target_folder, file),
                         on_status=on_status):
                copied += 1
                on_file(file)
    return copied, skipped, first_image


def find_first_image(folder: str) -> Optional[str]:
    for root_dir, _, files in os.walk(folder):
        image_files = sorted(f for f in files if f.lower().endswith(IMAGE_EXTENSIONS))
        if image_files:
            return os.path.join(root_dir, image_files[0])
    return None


# ---------------------------------------------------------------------------
# EXIF (optional — Pillow is already an agent dependency)
# ---------------------------------------------------------------------------

def get_image_date(image_path: str) -> Optional[str]:
    """EXIF DateTimeOriginal as ddMonYYYY (e.g. 10Jul2026), like the GUI."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        with Image.open(image_path) as image:
            exif_data = image._getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                if TAGS.get(tag_id, tag_id) == "DateTimeOriginal":
                    date_str = str(value).split(" ")[0]
                    year, month, day = date_str.split(":")
                    month_name = datetime.strptime(month, "%m").strftime("%b")
                    return f"{day}{month_name}{year}"
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# RINEX
# ---------------------------------------------------------------------------

def normalize_extension(ext: str) -> str:
    """Strip two-digit year prefixes: .25o -> .o"""
    if len(ext) > 3 and ext[1:3].isdigit():
        return "." + ext[3:]
    return ext


def is_rinex_file(path: str) -> bool:
    return path.lower().endswith(RINEX_EXTENSIONS)


def collect_rinex_files(base_data_path: str) -> list[str]:
    """All RINEX companion files sharing the base file's prefix."""
    base_dir = os.path.dirname(base_data_path)
    base_prefix = os.path.splitext(os.path.basename(base_data_path))[0]
    files_to_copy: list[str] = []
    try:
        for file in os.listdir(base_dir):
            if file.startswith(base_prefix) and file.lower().endswith(RINEX_EXTENSIONS):
                files_to_copy.append(os.path.join(base_dir, file))
    except FileNotFoundError:
        pass
    return files_to_copy or [base_data_path]


def rename_mix_to_nav(folder_path: str, on_status: StatusFn = _noop) -> None:
    try:
        for pattern in (os.path.join(folder_path, "*.*mix"),
                        os.path.join(folder_path, "*.mix")):
            for mix_file in glob.glob(pattern):
                if not mix_file.lower().endswith("mix"):
                    continue
                nav_file = mix_file[:-3] + "nav"
                if not os.path.exists(nav_file):
                    os.rename(mix_file, nav_file)
                    on_status(f"Renamed {os.path.basename(mix_file)} -> {os.path.basename(nav_file)}")
    except Exception as exc:
        on_status(f"Failed to rename mix files in {folder_path}: {exc}")


def patch_approx_position(folder_path: str, x: float, y: float, z: float,
                          on_status: StatusFn = _noop) -> None:
    for fname in os.listdir(folder_path):
        fpath = os.path.join(folder_path, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
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
                on_status(f"Patched APPROX POSITION XYZ in {fname}")
        except Exception as exc:
            on_status(f"Failed to patch {fname}: {exc}")


def batch_convert(
    folder_path: str,
    converter_exe: str,
    base_ecef_xyz: Optional[tuple[float, float, float]] = None,
    on_status: StatusFn = _noop,
    timeout: float = SUBPROCESS_TIMEOUT,
) -> int:
    """Convert every T02/T04/T0B in folder_path to RINEX with the Trimble CLI.
    Returns the number of files converted without error. Raises RuntimeError
    when there are T-files but no usable converter."""
    if not os.path.isdir(folder_path):
        raise RuntimeError(f"BaseData folder does not exist: {folder_path}")

    t_files = [f for f in os.listdir(folder_path)
               if f.lower().endswith(BASE_DATA_EXTENSIONS)]
    if not t_files:
        on_status("No .T02/.T04 files found — nothing to convert")
        return 0
    if not converter_exe or not os.path.isfile(converter_exe):
        raise RuntimeError(
            f"convertToRinex.exe not found ({converter_exe!r}) — set "
            "payload_paths.convert_to_rinex_exe in the agent config")

    converted = 0
    for file_name in t_files:
        try:
            command = [converter_exe, os.path.join(folder_path, file_name)]
            if base_ecef_xyz is not None:
                x, y, z = base_ecef_xyz
                command.append(f"/xy:{x},{y},{z}")
            on_status(f"Converting {file_name} to RINEX…")
            subprocess.run(command, check=True, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL)
            if base_ecef_xyz is not None:
                patch_approx_position(folder_path, x, y, z, on_status)
            rename_mix_to_nav(folder_path, on_status)
            converted += 1
            on_status(f"Converted {file_name}")
        except Exception as exc:
            on_status(f"Error converting {file_name}: {exc}")
    return converted


def find_rinex_obs(folder: str) -> Optional[str]:
    for root_dir, _, files in os.walk(folder):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if normalize_extension(ext) in (".o", ".obs"):
                return os.path.join(root_dir, file)
    return None


def rename_for_sensor(rinex_file_path: str, sensor_folder: str,
                      on_status: StatusFn = _noop) -> None:
    """Copy the obs file as <rtk-base>.obs into every flight subfolder that
    contains a .rtk file (L2/L3)."""
    file_to_copy = os.path.basename(rinex_file_path)
    for subdir, _, files in os.walk(sensor_folder):
        rtk_file = next((f for f in files if f.lower().endswith(".rtk")), None)
        if not rtk_file:
            continue
        rtk_base = os.path.splitext(rtk_file)[0]
        dest_path = os.path.join(subdir, file_to_copy)
        if not os.path.exists(dest_path):
            on_status(f"Copying {file_to_copy} -> {subdir}")
            shutil.copy(rinex_file_path, dest_path)
        shutil.move(dest_path, os.path.join(subdir, f"{rtk_base}.obs"))
        on_status(f"Renamed to {rtk_base}.obs in {subdir}")


def copy_base_data(
    base_data_paths: list[str],
    target: str,
    base_data_is_rinex: bool,
    on_status: StatusFn = _noop,
) -> int:
    """Copy base files (plus RINEX companions when base_data_is_rinex) into
    target; mirrors ProcessingWorker._copy_base_data for one target."""
    valid_sources = [p for p in base_data_paths if os.path.isfile(p)]
    if not valid_sources:
        return 0
    os.makedirs(target, exist_ok=True)
    copied = 0
    for source in valid_sources:
        files = collect_rinex_files(source) if base_data_is_rinex else [source]
        for file_path in files:
            if copy_file(file_path, os.path.join(target, os.path.basename(file_path)),
                         on_status=on_status):
                copied += 1
    if base_data_is_rinex:
        rename_mix_to_nav(target, on_status)
    return copied


# ---------------------------------------------------------------------------
# Targets CSV split (all-points csv -> SINGLE_TLT.csv + TAT.csv)
# ---------------------------------------------------------------------------

# The point-type lives in the 5th column (index 4) of each target row — the
# same convention the Terra-LiDAR automation reads (PyAutomateDJI).
_TARGET_TYPE_COL = 4


def _target_type(row: list[str]) -> str:
    return row[_TARGET_TYPE_COL].strip().upper() if len(row) > _TARGET_TYPE_COL else ""


def split_targets_csv(src: str, dest_folder: str,
                      on_status: StatusFn = _noop) -> dict:
    """Split an all-points targets csv into two files in dest_folder:

      * SINGLE_TLT.csv — rows whose type (col 5) is TLT (the LiDAR chain input).
      * TAT.csv        — rows whose type is TAT or TLT (the Pix4D chain input).

    Misc/other point types are dropped from both. Returns a summary dict with
    the written paths and the row counts. Mirrors the TLT rule the Terra-LiDAR
    automation applies at runtime, but also produces the combined TAT file so
    both chains read a prepared file from the project folder."""
    tlt_rows: list[list[str]] = []
    tat_rows: list[list[str]] = []
    total = 0
    with open(src, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or not any(c.strip() for c in row):
                continue
            total += 1
            kind = _target_type(row)
            if kind == "TLT":
                tlt_rows.append(row)
                tat_rows.append(row)
            elif kind == "TAT":
                tat_rows.append(row)

    os.makedirs(dest_folder, exist_ok=True)
    tlt_path = os.path.join(dest_folder, "SINGLE_TLT.csv")
    tat_path = os.path.join(dest_folder, "TAT.csv")
    with open(tlt_path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(tlt_rows)
    with open(tat_path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(tat_rows)

    on_status(
        f"Targets: {len(tlt_rows)} TLT -> SINGLE_TLT.csv, "
        f"{len(tat_rows)} TAT+TLT -> TAT.csv (of {total} point(s))")
    if not tlt_rows:
        on_status("WARNING: no TLT rows found in the targets csv (SINGLE_TLT.csv is empty)")
    return {
        "tlt_path": tlt_path, "tat_path": tat_path,
        "tlt_count": len(tlt_rows), "tat_count": len(tat_rows), "total_rows": total,
    }
