"""English Globus desktop demo. Launching this panel makes no network requests."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import queue
import sys
import threading

from PySide6.QtCore import QLockFile, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from bridge import (ROOT, GlobusBridge, SubmissionUncertain, TransferSpec,
                    read_json, safe_error, save_json, tutorial_spec, uuid_text)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = read_json(ROOT / "config.json")
        self.service = GlobusBridge(self.cfg["client_id"])
        self.events, self.busy = queue.Queue(), False
        self.record_path, self.tracked_task = None, None
        self.remote_state = ""
        self.app_lock = QLockFile(str(ROOT / ".app.lock"))
        if not self.app_lock.tryLock(0):
            raise RuntimeError("Globus Bridge is already open")
        self.setWindowTitle("Globus Raman Data Bridge")
        self.resize(1150, 860)
        self.setMinimumSize(980, 740)
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(22, 18, 22, 18)
        title = QLabel("Globus Raman Data Bridge")
        title.setStyleSheet("font-size:25px;font-weight:700;color:#16365c")
        layout.addWidget(title)
        layout.addWidget(QLabel("Official Globus SDK / Transfer API. This app moves files; it does not control the laser or motor."))

        auth = QGroupBox("1. Globus login")
        auth_grid = QGridLayout(auth)
        self.client_id = QLineEdit(self.cfg["client_id"])
        auth_grid.addWidget(QLabel("Native client ID"), 0, 0)
        auth_grid.addWidget(self.client_id, 0, 1)
        self.login_button = QPushButton("Log in with Globus")
        self.login_button.clicked.connect(lambda: self.job("login", lambda: self.service.login()))
        self.logout_button = QPushButton("Log out")
        self.logout_button.clicked.connect(lambda: self.job("logout", lambda: self.service.logout()))
        auth_grid.addWidget(self.login_button, 0, 2)
        auth_grid.addWidget(self.logout_button, 0, 3)
        auth_grid.addWidget(QLabel("Default: official SDK tutorial client (demo only). Login opens your browser. Tokens stay in memory for this window."), 1, 0, 1, 4)
        layout.addWidget(auth)

        transfer = QGroupBox("2. Choose what to transfer")
        grid = QGridLayout(transfer)
        self.mode = QComboBox()
        self.mode.addItems(["Official tutorial data", "Custom collections"])
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.new_target = QPushButton("New tutorial target")
        self.new_target.clicked.connect(self.load_tutorial)
        grid.addWidget(QLabel("Demo mode"), 0, 0)
        grid.addWidget(self.mode, 0, 1)
        grid.addWidget(self.new_target, 0, 2)
        self.source_id, self.destination_id = QLineEdit(), QLineEdit()
        self.source_path, self.destination_path = QLineEdit(), QLineEdit()
        grid.addWidget(QLabel("Source collection UUID"), 1, 0)
        grid.addWidget(self.source_id, 1, 1, 1, 2)
        grid.addWidget(QLabel("Source collection path"), 2, 0)
        grid.addWidget(self.source_path, 2, 1, 1, 2)
        grid.addWidget(QLabel("Destination collection UUID"), 3, 0)
        grid.addWidget(self.destination_id, 3, 1, 1, 2)
        grid.addWidget(QLabel("Destination collection path"), 4, 0)
        grid.addWidget(self.destination_path, 4, 1, 1, 2)
        self.recursive = QCheckBox("Transfer a whole directory (recursive)")
        self.label = QLineEdit("Raman Globus official tutorial demo")
        self.label.setMaxLength(128)
        grid.addWidget(self.recursive, 5, 1, 1, 2)
        grid.addWidget(QLabel("Task label"), 6, 0)
        grid.addWidget(self.label, 6, 1, 1, 2)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 1)
        layout.addWidget(transfer)

        actions = QHBoxLayout()
        self.preview_button = QPushButton("Offline API preview")
        self.preview_button.clicked.connect(self.preview)
        self.list_button = QPushButton("List source folder")
        self.list_button.clicked.connect(self.list_source)
        self.destination_button = QPushButton("List destination folder")
        self.destination_button.clicked.connect(self.list_destination)
        self.submit_button = QPushButton("Submit REAL transfer")
        self.submit_button.setStyleSheet("background:#176b8a;color:white;font-weight:600;padding:9px 12px")
        self.submit_button.clicked.connect(self.submit)
        self.folder_button = QPushButton("Open local records")
        self.folder_button.clicked.connect(self.open_records)
        for button in (self.preview_button, self.list_button, self.destination_button, self.submit_button, self.folder_button):
            actions.addWidget(button)
        layout.addLayout(actions)

        tracking = QGroupBox("3. Track the Globus task")
        track_grid = QGridLayout(tracking)
        self.task_id = QLineEdit()
        self.task_id.setPlaceholderText("A real task ID appears here after successful submission")
        track_grid.addWidget(QLabel("Task ID"), 0, 0)
        track_grid.addWidget(self.task_id, 0, 1, 1, 4)
        self.status_button = QPushButton("Check status")
        self.status_button.clicked.connect(self.check_status)
        self.events_button = QPushButton("Show task events")
        self.events_button.clicked.connect(self.task_events)
        self.cancel_button = QPushButton("Cancel task")
        self.cancel_button.clicked.connect(self.cancel_task)
        self.load_button = QPushButton("Load saved task")
        self.load_button.clicked.connect(self.load_record)
        self.retry_button = QPushButton("Retry SAME submission")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.retry)
        for col, button in enumerate((self.status_button, self.events_button, self.cancel_button, self.load_button, self.retry_button)):
            track_grid.addWidget(button, 1, col)
        self.auto_poll = QCheckBox("Refresh active task status every 3 seconds")
        self.auto_poll.setChecked(True)
        track_grid.addWidget(self.auto_poll, 2, 0, 1, 5)
        layout.addWidget(tracking)
        self.state = QLabel("READY | No login or transfer yet. Offline preview does not send files.")
        self.state.setWordWrap(True)
        self.state.setStyleSheet("background:#e9f1fa;padding:10px;color:#16365c;font-weight:600;border-radius:5px")
        layout.addWidget(self.state)
        note = QLabel("Official tutorial mode copies a public test file between Globus-hosted collections. Closing this app does not cancel a submitted remote task.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(1600)
        self.output.setStyleSheet("font-family:Consolas;font-size:11px")
        layout.addWidget(self.output, 1)
        self.load_tutorial()
        self.mode_changed()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.pump)
        self.timer.start(100)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_active)
        self.poll_timer.start(3000)
        self.network_buttons = (self.login_button, self.logout_button, self.list_button,
                                self.destination_button,
                                self.submit_button, self.status_button, self.events_button,
                                self.cancel_button, self.load_button)

    def mode_changed(self, *_):
        tutorial = self.mode.currentIndex() == 0
        for field in (self.source_id, self.destination_id, self.source_path, self.destination_path):
            field.setReadOnly(tutorial)
        self.recursive.setEnabled(not tutorial)
        self.new_target.setEnabled(tutorial)
        if tutorial:
            self.load_tutorial()

    def load_tutorial(self):
        spec = tutorial_spec()
        self.source_id.setText(spec.source_collection)
        self.destination_id.setText(spec.destination_collection)
        self.source_path.setText(spec.source_path)
        self.destination_path.setText(spec.destination_path)
        self.recursive.setChecked(False)
        for field in (self.source_id, self.destination_id, self.source_path, self.destination_path):
            field.setCursorPosition(0)

    def spec(self):
        spec = TransferSpec(self.source_id.text(), self.destination_id.text(), self.source_path.text(),
                            self.destination_path.text(), self.recursive.isChecked(), self.label.text()).validated()
        if self.mode.currentIndex() == 0 and not spec.official_tutorial_only:
            raise ValueError("Tutorial mode must use the official tutorial source file and collections")
        return spec

    def prepare_service(self):
        client_id = uuid_text(self.client_id.text())
        if self.service.client_id != client_id:
            self.service.close()
            self.service = GlobusBridge(client_id)
        self.cfg["client_id"] = client_id
        save_json(ROOT / "config.json", self.cfg)

    def job(self, action, function):
        if self.busy:
            return
        try:
            self.prepare_service()
        except Exception as exc:
            self.show_error(exc)
            return
        self.busy = True
        for button in self.network_buttons:
            button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.state.setText(f"WORKING | {action}. Complete login/consent in the browser if prompted.")
        def work():
            try:
                self.events.put((action, function(), None))
            except Exception as exc:
                self.events.put((action, None, exc))
        # Remote transfers are server tasks and do not depend on this worker staying alive.
        threading.Thread(target=work, daemon=True).start()

    def show_error(self, exc):
        text = safe_error(exc)
        self.state.setText("ERROR | " + text)
        self.output.appendPlainText(text)

    def preview(self):
        try:
            path, result = self.service.preview(self.spec())
            self.output.setPlainText(json.dumps(result, indent=2))
            self.state.setText("OFFLINE PREVIEW | No network call, no task ID, no files transferred.")
            self.output.appendPlainText("\nSaved preview: " + str(path))
        except Exception as exc:
            self.show_error(exc)

    def list_source(self):
        try:
            spec = self.spec()
            path = spec.source_path if spec.recursive else str(PurePosixPath(spec.source_path).parent)
        except Exception as exc:
            self.show_error(exc)
            return
        self.job("list", lambda: self.service.list_directory(spec.source_collection, path))

    def submit(self):
        try:
            spec = self.spec()
        except Exception as exc:
            self.show_error(exc)
            return
        if self.record_path:
            record = GlobusBridge.load_record(self.record_path)
            if record.get("state") in ("SUBMITTING", "SUBMISSION_UNCERTAIN"):
                self.show_error(ValueError("Resolve the uncertain saved request before creating a new one"))
                return
        review = ("Submit this real Globus task?\n\n"
                  f"Source collection: {spec.source_collection}\n{spec.source_path}\n\n"
                  f"Destination collection: {spec.destination_collection}\n{spec.destination_path}\n\n"
                  "An existing destination file can be replaced. No instrument command is sent.")
        if QMessageBox.question(self, "Review transfer", review) != QMessageBox.StandardButton.Yes:
            return
        self.job("submit", lambda: self.service.submit(spec))

    def list_destination(self):
        try:
            spec = self.spec()
            path = spec.destination_path if spec.recursive else str(PurePosixPath(spec.destination_path).parent)
        except Exception as exc:
            self.show_error(exc)
            return
        self.job("list destination", lambda: self.service.list_directory(spec.destination_collection, path))

    def current_task(self):
        task_id = uuid_text(self.task_id.text())
        path = self.record_path if task_id == self.tracked_task else None
        return task_id, path

    def check_status(self):
        try:
            task_id, path = self.current_task()
        except Exception as exc:
            self.show_error(exc)
            return
        self.job("status", lambda: self.service.task_status(task_id, path))

    def task_events(self):
        try:
            task_id, _ = self.current_task()
        except Exception as exc:
            self.show_error(exc)
            return
        self.job("events", lambda: self.service.events(task_id))

    def cancel_task(self):
        try:
            task_id, path = self.current_task()
        except Exception as exc:
            self.show_error(exc)
            return
        if QMessageBox.question(self, "Cancel remote task", f"Request cancellation of task {task_id}?") == QMessageBox.StandardButton.Yes:
            self.job("cancel", lambda: self.service.cancel(task_id, path))

    def retry(self):
        if self.record_path:
            path = self.record_path
            self.job("submit", lambda: self.service.retry_saved(path))

    def accept_record(self, path, record):
        self.record_path = Path(path)
        self.tracked_task = record.get("task_id")
        self.task_id.setText(self.tracked_task or "")
        self.remote_state = record["state"]
        self.cfg["last_record"] = str(path)
        save_json(ROOT / "config.json", self.cfg)
        self.retry_button.setEnabled(record["state"] in ("SUBMITTING", "SUBMISSION_UNCERTAIN"))

    def load_record(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a live Globus record.json", str(ROOT / "records"), "Transfer record (record.json)")
        if path:
            try:
                record = GlobusBridge.load_record(path)
                self.accept_record(path, record)
                self.state.setText("SAVED RECORD | " + record["state"] + ". Check status for current server state.")
                self.output.setPlainText(json.dumps(record, indent=2))
            except Exception as exc:
                self.show_error(exc)

    def poll_active(self):
        if (not self.busy and self.auto_poll.isChecked() and self.tracked_task
                and self.task_id.text() == self.tracked_task
                and self.remote_state in ("SUBMITTED", "ACTIVE", "CANCEL_REQUESTED")):
            self.check_status()

    def open_records(self):
        (ROOT / "records").mkdir(exist_ok=True)
        os.startfile(str(ROOT / "records"))

    def pump(self):
        try:
            while True:
                action, value, error = self.events.get_nowait()
                self.busy = False
                for button in self.network_buttons:
                    button.setEnabled(True)
                if error:
                    self.auto_poll.setChecked(False)
                    if isinstance(error, SubmissionUncertain):
                        self.accept_record(error.record_path, GlobusBridge.load_record(error.record_path))
                    self.show_error(error)
                    continue
                if action == "submit":
                    path, record = value
                    self.accept_record(path, record)
                    self.state.setText("SUBMITTED | A real Globus task ID was returned. Waiting for server status.")
                    self.output.setPlainText(json.dumps(record, indent=2))
                elif action == "status":
                    self.remote_state = value["status"]
                    self.state.setText(f"GLOBUS {value['status']} | {value.get('files_transferred', 0)} files transferred | {value.get('bytes_transferred', 0)} bytes")
                    self.output.setPlainText(json.dumps(value, indent=2))
                elif action == "cancel":
                    self.remote_state = "CANCEL_REQUESTED"
                    self.state.setText("CANCEL REQUESTED | Check status to confirm the server task has ended.")
                    self.output.setPlainText(json.dumps(value, indent=2))
                else:
                    if action == "logout":
                        self.remote_state = ""
                    self.state.setText("LOGGED IN | Globus authorization completed." if action == "login" else "LOGGED OUT" if action == "logout" else "LIVE API RESPONSE | " + action)
                    self.output.setPlainText(json.dumps(value, indent=2))
        except queue.Empty:
            pass

    def closeEvent(self, event):
        self.timer.stop()
        self.poll_timer.stop()
        self.app_lock.unlock()
        if not self.busy:
            self.service.close()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-image", help="Render the offline preview only; no login or network")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
        app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet("QWidget{font-family:'Segoe UI';font-size:12px} QPushButton{padding:7px 10px} QGroupBox{font-weight:600;margin-top:8px;padding-top:10px}")
    try:
        window = Window()
    except Exception as exc:
        QMessageBox.critical(None, "Globus Bridge", safe_error(exc))
        return 1
    window.show()
    if args.preview_image:
        window.preview()
        def capture():
            Path(args.preview_image).parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(args.preview_image)
            window.close()
        QTimer.singleShot(300, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
