"""Exercise the GUI with real SDK calls and HTTP mocks, never real Globus tasks."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
from unittest.mock import patch
import globus_sdk
from globus_sdk.transport import RetryConfig
import responses
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QLabel

import app
from bridge import GlobusBridge, ROOT, TRANSFER_API, save_json

SUBMISSION = "c5746e3a-a79b-4cbd-b17e-06a6ef6277a5"
TASK = "922a121c-ad73-499b-afcb-bdf6f47cb98f"


@responses.activate
def main():
    responses.get(TRANSFER_API + "/submission_id", json={"value": SUBMISSION})
    responses.post(TRANSFER_API + "/transfer", json={"task_id": TASK, "code": "Accepted"})
    responses.get(TRANSFER_API + "/task/" + TASK, json={"status": "SUCCEEDED", "task_id": TASK, "files_transferred": 1, "bytes_transferred": 8})
    application = QApplication([])
    application.setStyle("Fusion")
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    application.setFont(QFont("Segoe UI", 10))
    window = app.Window()
    window.centralWidget().layout().insertWidget(0, QLabel("HTTP MOCK TEST ONLY - no login and no real files transferred"))
    window.service.close()
    client = globus_sdk.TransferClient(authorizer=globus_sdk.NullAuthorizer(), retry_config=RetryConfig(max_retries=0))
    window.service = GlobusBridge(record_root=ROOT / "test_outputs/mock_http_gui", client=client)
    window.auto_poll.setChecked(False)
    window.show()
    result, requested = [], [False]
    def inspect():
        if not window.busy and window.remote_state == "SUBMITTED" and not requested[0]:
            requested[0] = True
            window.check_status()
        if not window.busy and window.remote_state == "SUCCEEDED":
            result.append(window.task_id.text())
            window.state.setText("HTTP MOCK TEST SUCCEEDED - not a real Globus transfer")
            window.grab().save(str(ROOT / "test_outputs/mock_gui_preview.png"))
            window.close()
            application.quit()
    timer = QTimer()
    timer.timeout.connect(inspect)
    timer.start(100)
    QTimer.singleShot(15000, application.quit)
    with patch.object(app, "save_json"):
        window.job("submit", lambda: window.service.submit(window.spec()))
        application.exec()
    assert result == [TASK], (result, window.state.text(), window.output.toPlainText())
    save_json(ROOT / "test_outputs/gui_test_report.json", dict(
        passed=True, real_network=False, real_transfer=False,
        requests=[dict(method=call.request.method, url=call.request.url) for call in responses.calls]))
    print("PASS: GUI -> official SDK -> mocked HTTP submit/status -> task display; no real transfer.")


if __name__ == "__main__":
    main()
