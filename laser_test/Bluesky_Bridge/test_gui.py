"""Offscreen GUI simulation only; does not open or query hardware Qt."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
import app


def main():
    application = QApplication([])
    application.setStyle("Fusion")
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    application.setFont(QFont("Segoe UI", 10))
    application.setStyleSheet("QWidget{font-family:'Segoe UI';font-size:12px} QPushButton{padding:7px 10px} QGroupBox{font-weight:600;margin-top:8px;padding-top:10px}")
    window = app.Window()
    window.cfg = dict(window.cfg, output_dir=app.ROOT / "test_outputs/gui_runs")
    window.show()
    # Suppress only the GUI's config write, keeping normal run records enabled.
    with patch.object(app, "save_json"):
        window.start()
    result = []
    def inspect():
        if window.worker and not window.worker.is_alive():
            window.pump()
            result.append((window.state.text(), window.table.rowCount()))
            print(window.log_box.toPlainText())
            target = app.ROOT / "test_outputs/gui_simulation.png"
            assert window.grab().save(str(target))
            window.close()
            application.quit()
    timer = QTimer()
    timer.timeout.connect(inspect)
    timer.start(100)
    QTimer.singleShot(30000, application.quit)
    application.exec()
    assert result and result[0][0].startswith("COMPLETED") and result[0][1] == 13, result
    print("PASS: GUI worker, 13 point rows, live plot, completion state; no hardware opened.")


if __name__ == "__main__":
    main()
