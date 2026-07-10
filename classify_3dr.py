"""
classify_3dr.py - Cyclone 3DR automatic point cloud classification

Invoked after the DJI Terra EXE completes. Finds LAZ/LAS files in the
Terra output folder and runs ClassifyLAZ.js on each one via Cyclone 3DR.

Retry logic: if any file fails on the first attempt and the current time
is between 8 AM – 5 PM MST (business hours when 3DR may be in use), the
thread sleeps until 5 PM MST and retries those files once more. Outside
business hours a single attempt is made and failures are reported as-is.
"""

import datetime
import os
import subprocess
import time
from typing import List

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QWidget


# ---------------------------------------------------------------------------
# Paths — update if the installation differs
# ---------------------------------------------------------------------------

CYCLONE_3DR_EXE   = r"C:\Program Files\Leica Geosystems\Cyclone 3DR\3DR.exe"
CLASSIFY_SCRIPT   = r"Z:\Survey\UT\_Scripts\3DR\LAZ-Classify\ClassifyLAZ.js"
GET_MODELS_SCRIPT = r"Z:\Survey\UT\_Scripts\3DR\LAZ-Classify\GetModels.js"

# ---------------------------------------------------------------------------
# Available classification models (as shown in Cyclone 3DR)
# ---------------------------------------------------------------------------

CLASSIFICATION_MODELS: List[str] = [
    "BLK Mobile Filter People 2.0",
    "Heavy Construction UAV 2.0",
    "Indoor 2.2",
    "Indoor Construction Site 1.3",
    "Outdoor TLS 2.1",
    "Plant 2.0",
    "Road 1.0",
]


# ---------------------------------------------------------------------------
# Business-hours helpers (MST = UTC-7, fixed offset)
# ---------------------------------------------------------------------------

def _now_mst() -> datetime.datetime:
    return datetime.datetime.utcnow() - datetime.timedelta(hours=7)


def _is_business_hours_mst() -> bool:
    """True while current MST time is between 08:00 and 17:00 (8 AM – 5 PM)."""
    hour = _now_mst().hour
    return 8 <= hour < 17


def _seconds_until_5pm_mst() -> float:
    """Seconds from now until 17:00 MST today (0 if already past 5 PM)."""
    now = _now_mst()
    end = now.replace(hour=17, minute=0, second=0, microsecond=0)
    return max(0.0, (end - now).total_seconds())


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def find_laz_files(root: str) -> List[str]:
    """Recursively find all .las/.laz files under root."""
    results = []
    for dirpath, _, filenames in os.walk(root):
        for f in filenames:
            if f.lower().endswith((".las", ".laz")):
                results.append(os.path.join(dirpath, f))
    return results


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------


class Classify3DRThread(QThread):
    """Classifies every LAZ/LAS file in terra_folder using Cyclone 3DR scripts.

    First pass: attempt each file once.
    If any fail and current time is 8 AM – 5 PM MST, wait until 5 PM then
    retry those files once. Outside business hours no retry is attempted.
    """

    status_update           = pyqtSignal(str)
    classification_complete = pyqtSignal(int, int)   # (succeeded, total)

    def __init__(self, terra_folder: str, model_name: str, project_name: str = "", parent=None):
        super().__init__(parent)
        self._terra_folder = terra_folder
        self._model_name   = model_name
        self._project_name = project_name

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_one(self, path: str) -> bool:
        """Run ClassifyLAZ.js on a single file. Returns True on success.

        Completion is detected by watching for the output .3dr file rather
        than waiting for 3DR.exe to exit — Exit(0) in the JS is unreliable
        and the process can stay open indefinitely.
        """
        js_path     = path.replace("\\", "\\\\")
        param       = f"var inputFile='{js_path}'; var modelName='{self._model_name}';"
        output_3dr  = os.path.splitext(path)[0] + ".3dr"
        poll_secs   = 10
        max_wait    = 21600  # 6 hours

        # IMPORTANT: use DEVNULL, not inherited stdout/stderr (or PIPE without a
        # reader). If 3DR.exe inherits a pipe handle from this process's own
        # ancestry that nobody is actively draining, it deadlocks the instant
        # it writes enough output to fill the ~64KB pipe buffer — no crash, no
        # exit, it just sits there forever. Large files are more likely to hit
        # this since they produce more internal logging.
        proc = subprocess.Popen(
            [CYCLONE_3DR_EXE,
             f"--Script={CLASSIFY_SCRIPT}",
             "--scriptAutorun",
             "--silent",
             f"--scriptParam={param}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        heartbeat_secs = 300  # log a liveness line every 5 min while nothing else is happening
        next_heartbeat = heartbeat_secs

        try:
            elapsed = 0
            while elapsed < max_wait:
                # Process exited on its own (Exit(0) worked)
                if proc.poll() is not None:
                    return os.path.isfile(output_3dr)

                if elapsed >= next_heartbeat:
                    self.status_update.emit(
                        f"[3DR] Still waiting on {os.path.basename(path)} "
                        f"({elapsed // 60}m elapsed, 3DR still running)"
                    )
                    next_heartbeat += heartbeat_secs

                # Output file detected — wait for it to finish writing before closing 3DR
                if os.path.isfile(output_3dr):
                    self.status_update.emit(
                        f"[3DR] Output detected, waiting for file to finish writing: {os.path.basename(output_3dr)}"
                    )
                    prev_size = -1
                    stable_checks = 0
                    while elapsed < max_wait:
                        time.sleep(poll_secs)
                        elapsed += poll_secs
                        try:
                            curr_size = os.path.getsize(output_3dr)
                        except OSError:
                            curr_size = -1
                        if curr_size == prev_size and curr_size > 0:
                            stable_checks += 1
                            if stable_checks >= 2:  # stable for 2 consecutive checks (~20s)
                                break
                        else:
                            stable_checks = 0
                        prev_size = curr_size
                        self.status_update.emit(
                            f"[3DR] Writing... {curr_size // (1024*1024)} MB"
                        )
                    proc.terminate()
                    proc.wait(timeout=15)
                    return True

                time.sleep(poll_secs)
                elapsed += poll_secs

            # Timed out
            self.status_update.emit(f"[3DR] Timed out: {os.path.basename(path)}")
            proc.terminate()
            proc.wait(timeout=15)
        except Exception as exc:
            self.status_update.emit(f"[3DR] Error on {os.path.basename(path)}: {exc}")
            try:
                proc.terminate()
            except Exception:
                pass

        return False

    # ------------------------------------------------------------------
    # Thread entry point
    # ------------------------------------------------------------------

    def run(self):

        report_file = os.path.join(self._terra_folder, self._project_name, "lidars", "report", "report.md")
        self.status_update.emit(f"[3DR] Waiting for: {report_file}")
        while not os.path.isfile(report_file):
            self.status_update.emit("[3DR] LiDAR not yet complete — checking again in 3 min...")
            time.sleep(180)
        self.status_update.emit("[3DR] report.md detected — LiDAR complete, scanning for LAZ files...")

        laz_scan_root = os.path.join(self._terra_folder, self._project_name, "lidars", "terra_laz")
        self.status_update.emit(f"[3DR] Scanning: {laz_scan_root}")
        laz_files = find_laz_files(laz_scan_root)

        if not laz_files:
            self.status_update.emit("[3DR] No LAZ/LAS files found in terra_laz folder.")
            self.classification_complete.emit(0, 0)
            return

        _8GB = 8 * 1024 ** 3
        merged = next((p for p in laz_files if os.path.basename(p).lower() == "cloud_merged.laz"), None)
        if merged is not None:
            merged_size = os.path.getsize(merged)
            merged_gb   = merged_size / 1024 ** 3
            if merged_size < _8GB:
                self.status_update.emit(
                    f"[3DR] cloud_merged.laz is {merged_gb:.2f} GB (<8 GB) — classifying merged only."
                )
                laz_files = [merged]
            else:
                self.status_update.emit(
                    f"[3DR] cloud_merged.laz is {merged_gb:.2f} GB (≥8 GB) — skipping merged, classifying tiles only."
                )
                laz_files = [p for p in laz_files if p != merged]

        total = len(laz_files)
        if total == 0:
            self.status_update.emit("[3DR] No files to classify after size filter.")
            self.classification_complete.emit(0, 0)
            return

        # --- First pass ---
        succeeded = 0
        failed: List[str] = []

        for i, path in enumerate(laz_files, 1):
            self.status_update.emit(
                f"[3DR] Classifying [{i}/{total}]: {os.path.basename(path)}"
            )
            if self._classify_one(path):
                succeeded += 1
            else:
                self.status_update.emit(
                    f"[3DR] Failed (first attempt): {os.path.basename(path)}"
                )
                failed.append(path)

        # --- Retry after business hours if needed ---
        if failed:
            if _is_business_hours_mst():
                wait_secs = _seconds_until_5pm_mst()
                hrs  = int(wait_secs // 3600)
                mins = int((wait_secs % 3600) // 60)
                self.status_update.emit(
                    f"[3DR] {len(failed)} file(s) failed during business hours "
                    f"(8 AM – 5 PM MST). Waiting {hrs}h {mins}m until 5 PM MST to retry..."
                )
                time.sleep(wait_secs)
                self.status_update.emit(
                    f"[3DR] Retrying {len(failed)} file(s) after business hours..."
                )
                retry_failed: List[str] = []
                for path in failed:
                    self.status_update.emit(
                        f"[3DR] Retry: {os.path.basename(path)}"
                    )
                    if self._classify_one(path):
                        succeeded += 1
                    else:
                        self.status_update.emit(
                            f"[3DR] Still failed after retry: {os.path.basename(path)}"
                        )
                        retry_failed.append(path)
                failed = retry_failed
            else:
                self.status_update.emit(
                    f"[3DR] {len(failed)} file(s) failed. "
                    "Outside business hours — no retry scheduled."
                )

        self.status_update.emit(
            f"[3DR] Classification complete: {succeeded}/{total} files processed."
        )
        self.classification_complete.emit(succeeded, total)


# ---------------------------------------------------------------------------
# UI widget
# ---------------------------------------------------------------------------

class Classify3DRWidget(QWidget):
    """
    Compact row: [checkbox] [label] [model combo]

    Meant to live inside the DJI Terra Parameters box.
    Models are populated from the static CLASSIFICATION_MODELS list — 3DR is
    never launched to build the dropdown.
    Call is_enabled / selected_model to read state before launching the thread.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(6)

        self.enabled_check = QCheckBox("Auto-classify with Cyclone 3DR after Terra")
        self.enabled_check.setFont(QFont("Segoe UI", 10))
        self.enabled_check.setStyleSheet("color: #113e59; border: none;")
        layout.addWidget(self.enabled_check)

        model_label = QLabel("Model:")
        model_label.setFont(QFont("Segoe UI", 10))
        model_label.setStyleSheet("color: #113e59; background: transparent; border: none;")
        layout.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.setFont(QFont("Segoe UI", 10))
        self.model_combo.setMinimumWidth(220)
        self.model_combo.setEnabled(False)
        self.model_combo.addItems(CLASSIFICATION_MODELS)
        layout.addWidget(self.model_combo)

        layout.addStretch()

        self.enabled_check.stateChanged.connect(
            lambda state: self.model_combo.setEnabled(bool(state))
        )

    @property
    def is_enabled(self) -> bool:
        return self.enabled_check.isChecked()

    @property
    def selected_model(self) -> str:
        return self.model_combo.currentText() if self.is_enabled else ""


# ---------------------------------------------------------------------------
# Dev mode — run directly: python classify_3dr.py
# ---------------------------------------------------------------------------

# vvv  Edit these three lines before running  vvv
_DEV_TERRA_FOLDER   = r"D:\3dData\test"
_DEV_PROJECT_NAME   = "LiDAR Point Cloud Project(1)"
_DEV_MODEL_NAME     = "Heavy Construction UAV 2.0"
# ^^^  Edit these three lines before running  ^^^

if __name__ == "__main__":
    import sys
    import threading
    import msvcrt
    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv)

    print("[DEV] classify_3dr.py — dev mode")
    print(f"[DEV] Terra folder  : {_DEV_TERRA_FOLDER}")
    print(f"[DEV] Project name  : {_DEV_PROJECT_NAME}")
    print(f"[DEV] Model         : {_DEV_MODEL_NAME}")
    print("[DEV] Press Q to quit at any time.")
    print("-" * 60)

    def _watch_for_quit():
        while True:
            key = msvcrt.getch()
            if key.lower() == b"q":
                print("\n[DEV] Q pressed — exiting.")
                app.quit()
                break

    quit_watcher = threading.Thread(target=_watch_for_quit, daemon=True)
    quit_watcher.start()

    thread = Classify3DRThread(_DEV_TERRA_FOLDER, _DEV_MODEL_NAME, project_name=_DEV_PROJECT_NAME)
    thread.status_update.connect(print)
    thread.classification_complete.connect(
        lambda ok, total: (
            print(f"\n[DEV] Done — {ok}/{total} files classified successfully."),
            app.quit(),
        )
    )
    thread.start()

    sys.exit(app.exec_())
