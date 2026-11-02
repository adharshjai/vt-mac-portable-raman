"""English Qt desktop panel. Opening the panel never starts a device."""
import argparse
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

os.environ["QT_API"] = "pyside6"
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parent / ".cache/matplotlib"))
from PySide6.QtCore import QLockFile, QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

from bridge import ROOT, device_settings, qt_client, run_experiment, save_json, settings


class QueueLog(logging.Handler):
    def __init__(self, events):
        super().__init__(logging.ERROR)
        self.events = events

    def emit(self, record):
        self.events.put(("log", self.format(record)))


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg, self.events = settings(), queue.Queue()
        self.cancel, self.worker = threading.Event(), None
        self.output, self.close_pending = None, False
        self.app_lock = QLockFile(str(ROOT / ".app.lock"))
        if not self.app_lock.tryLock(0):
            raise RuntimeError("Bluesky Raman Bridge is already open")
        self.setWindowTitle("Bluesky Raman Bridge")
        self.resize(1180, 870)
        self.setMinimumSize(980, 750)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 18, 20, 16)
        title = QLabel("Bluesky Raman Bridge")
        title.setStyleSheet("font-size:25px;font-weight:700;color:#16365c")
        layout.addWidget(title)
        layout.addWidget(QLabel("Bluesky plans and records the experiment. Qt executes the existing scan path."))

        options = QGroupBox("Experiment")
        grid = QGridLayout(options)
        self.sample = QLineEdit("raman-demo-001")
        self.sample.setMaxLength(160)
        self.mode = QComboBox()
        self.mode.addItems(["1d-x", "1d-y", "2d"])
        self.mode.setCurrentText(self.cfg.get("default_scan_mode", "1d-y"))
        self.kind = QComboBox()
        self.kind.addItems(["Simulation (no hardware)", "Saved-data replay", "Hardware: existing Qt scan"])
        grid.addWidget(QLabel("Sample ID"), 0, 0)
        grid.addWidget(self.sample, 0, 1)
        grid.addWidget(QLabel("Scan mode"), 0, 2)
        grid.addWidget(self.mode, 0, 3)
        grid.addWidget(QLabel("Data source"), 0, 4)
        grid.addWidget(self.kind, 0, 5)
        self.replay_folder = QLineEdit()
        browse = QPushButton("Browse scan folder")
        browse.clicked.connect(self.browse_replay)
        grid.addWidget(QLabel("Replay folder"), 1, 0)
        grid.addWidget(self.replay_folder, 1, 1, 1, 4)
        grid.addWidget(browse, 1, 5)
        self.dark = QLineEdit(str(device_settings(self.cfg)["dark_file"]))
        self.dark.setCursorPosition(0)
        choose_dark = QPushButton("Choose dark file")
        choose_dark.clicked.connect(self.browse_dark)
        grid.addWidget(QLabel("Dark TXT file"), 2, 0)
        grid.addWidget(self.dark, 2, 1, 1, 4)
        grid.addWidget(choose_dark, 2, 5)
        self.analysis = QCheckBox("Analyze with existing PCA-SVM (replay / hardware only)")
        self.analysis.setChecked(True)
        grid.addWidget(self.analysis, 3, 0, 1, 6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(5, 1)
        layout.addWidget(options)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("Run experiment")
        self.start_button.setStyleSheet("background:#176b8a;color:white;font-weight:600;padding:9px 14px")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("Stop this run")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        check = QPushButton("Check Qt status")
        check.clicked.connect(self.check_status)
        self.qt_button = QPushButton("Open hardware Qt")
        self.qt_button.clicked.connect(self.open_qt)
        results = QPushButton("Open results folder")
        results.clicked.connect(self.open_output)
        for button in (self.start_button, self.stop_button, check, self.qt_button, results):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.state = QLabel("Ready. Simulation is selected. No device has been started.")
        self.state.setWordWrap(True)
        self.state.setStyleSheet("background:#e9f1fa;padding:10px;font-weight:600;color:#16365c;border-radius:5px")
        layout.addWidget(self.state)
        note = QLabel("Hardware uses the Qt travel settings. Stop does not switch off the laser; use local Qt laser control.")
        note.setWordWrap(True)
        layout.addWidget(note)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.figure = Figure(figsize=(8, 2.7), dpi=100)
        self.axes = self.figure.add_subplot(111)
        self.line, = self.axes.plot([], [], color="#1b7b9c", linewidth=1)
        self.axes.set_xlabel("Detector pixel (raw spectrum)")
        self.axes.set_ylabel("Intensity (counts)")
        self.axes.set_title("No spectrum yet")
        self.figure.tight_layout()
        self.canvas = FigureCanvasQTAgg(self.figure)
        splitter.addWidget(self.canvas)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Point", "X (reported steps)", "Y (reported steps)", "Z (reported steps)", "Class label", "Top class score"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        splitter.addWidget(self.table)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(1500)
        self.log_box.setStyleSheet("font-family:Consolas;font-size:11px")
        splitter.addWidget(self.log_box)
        splitter.setSizes([320, 130, 100])
        layout.addWidget(splitter, 1)
        self.log_handler = QueueLog(self.events)
        logging.getLogger("bluesky").addHandler(self.log_handler)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump)
        self.timer.start(100)

    def log(self, text):
        self.log_box.appendPlainText(str(text))

    def browse_replay(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a scan folder containing point_*.json", str(device_settings(self.cfg)["scan_root"]))
        if folder:
            self.replay_folder.setText(folder)
            self.kind.setCurrentText("Saved-data replay")

    def browse_dark(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose the dark spectrum for this experiment", str(Path(self.dark.text()).parent), "Dark spectrum (*.txt);;All files (*)")
        if path:
            self.dark.setText(path)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        kind = {"Simulation (no hardware)": "simulation", "Saved-data replay": "replay", "Hardware: existing Qt scan": "hardware"}[self.kind.currentText()]
        sample, mode = self.sample.text().strip(), self.mode.currentText()
        folder, analyze = self.replay_folder.text().strip(), self.analysis.isChecked()
        if not sample:
            QMessageBox.warning(self, "Sample ID required", "Enter a sample ID first.")
            return
        if kind == "replay" and (not folder or not Path(folder).is_dir()):
            QMessageBox.warning(self, "Scan folder required", "Choose a folder containing point_*.json files.")
            return
        cfg = dict(self.cfg, dark_file_override=self.dark.text().strip())
        if kind != "simulation" and analyze and not Path(cfg["dark_file_override"]).is_file():
            QMessageBox.warning(self, "Dark file missing", "Choose a valid dark TXT file, or turn off analysis.")
            return
        try:
            save_json(ROOT / "config.json", cfg)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot save settings", str(exc))
            return
        self.cfg = cfg
        self.cancel = threading.Event()
        self.table.setRowCount(0)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.qt_button.setEnabled(False)
        self.state.setText(f"Running {kind} | Sample: {sample}")
        self.close_pending = False
        if kind == "hardware":
            self.log("Submitting a real Qt scan. Local Qt must be ready and remote scans enabled.")
        def work():
            try:
                run_experiment(kind, sample, mode, folder, analyze, cfg, self.cancel,
                               lambda name, value: self.events.put((name, value)))
            except Exception as exc:
                self.events.put(("startup_error", f"{type(exc).__name__}: {exc}"))
        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()

    def stop(self):
        self.cancel.set()
        self.stop_button.setEnabled(False)
        self.state.setText("Stopping this run. Waiting for the current operation and Qt response...")
        self.log("Stop requested. Laser power is unchanged.")

    def check_status(self):
        cfg = dict(self.cfg)
        def read():
            try:
                self.events.put(("log", "Qt status: " + json.dumps(qt_client(cfg).status())))
            except Exception as exc:
                self.events.put(("log", f"Qt not available: {exc}"))
        threading.Thread(target=read, daemon=True).start()

    def open_qt(self):
        try:
            try:
                qt_client(self.cfg).status()
            except (RuntimeError, FileNotFoundError):
                pass
            else:
                self.log("Qt is already running. Use the existing Qt window.")
                return
            folder = self.cfg["intersect_bridge_dir"]
            subprocess.Popen(["cmd.exe", "/d", "/c", str(folder / "start_qt.cmd")], cwd=str(folder),
                             creationflags=subprocess.CREATE_NO_WINDOW)
            self.log("Hardware Qt launch requested. Check the real device window, then Check Qt status.")
        except Exception as exc:
            QMessageBox.warning(self, "Qt launch failed", str(exc))

    def open_output(self):
        path = Path(self.output) if self.output else self.cfg["output_dir"]
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def idle(self):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.qt_button.setEnabled(True)

    def pump(self):
        try:
            while True:
                name, value = self.events.get_nowait()
                if name == "log":
                    self.log(value)
                elif name == "output":
                    self.output = value
                    self.log("Output folder: " + value)
                elif name == "point":
                    raw = value["spectrum"]
                    self.line.set_data(range(len(raw)), raw)
                    self.axes.relim()
                    self.axes.autoscale_view()
                    self.axes.set_title(f"Point {value['point_id']} | {value['label']}")
                    self.canvas.draw_idle()
                    scores = json.loads(value["scores_json"])
                    score = f"{max(scores.values()):.3f}" if scores else "--"
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    for col, text in enumerate((value["point_id"], value["controller_x_steps"], value["controller_y_steps"], value["controller_z_steps"], value["label"], score)):
                        self.table.setItem(row, col, QTableWidgetItem(str(text)))
                    self.table.scrollToBottom()
                    self.log(f"Point {value['point_id']} recorded | {value['label']}")
                elif name == "finished":
                    self.state.setText(f"{value['state'].upper()} | {value['recorded_points']} points | {value['data_source']}")
                    self.log("Run UID: " + str(value["bluesky_run_uid"]))
                    self.log("Summary: " + str(Path(value["output_directory"]) / "summary.json"))
                    if value["error"]:
                        self.log(value["error"])
                    if value.get("cleanup_error"):
                        self.log("Stop handling issue: " + value["cleanup_error"])
                    self.idle()
                elif name == "startup_error":
                    self.state.setText("Could not start. See the log.")
                    self.log(value)
                    self.idle()
        except queue.Empty:
            pass
        if self.close_pending and (not self.worker or not self.worker.is_alive()):
            self.close()

    def closeEvent(self, event):
        if self.worker and self.worker.is_alive():
            self.close_pending = True
            self.stop()
            self.log("Window will close after stop handling finishes. Use local Qt if hardware needs attention.")
            event.ignore()
        else:
            self.timer.stop()
            logging.getLogger("bluesky").removeHandler(self.log_handler)
            self.app_lock.unlock()
            event.accept()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-image", help="Render this window only; no hardware is started")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    # Also render correctly on offscreen Qt platforms used for GUI checks.
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet("QWidget{font-family:'Segoe UI';font-size:12px} QPushButton{padding:7px 10px} QGroupBox{font-weight:600;margin-top:8px;padding-top:10px}")
    try:
        window = Window()
    except Exception as exc:
        QMessageBox.critical(None, "Bluesky Bridge", str(exc))
        return 1
    window.show()
    if args.preview_image:
        def preview():
            Path(args.preview_image).parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(args.preview_image)
            window.close()
        QTimer.singleShot(300, preview)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
