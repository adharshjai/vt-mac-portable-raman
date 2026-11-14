// Pictures of the controller window, for looking at its layout without a display: the window in
// several states, in the light and in the dark appearance, at the default size and at the smallest
// supported one, saved as PNG files.
//
//   ./ui_snapshot [output folder]        (default: snapshots/ next to this program)
//
// It is built like handshake_tests: the real window code with the two emulators next to this
// program, plus the stand-in data layer (fake_data_layer.py) in data_layer/. No SDK, USB or laser
// access, and nothing is contacted. Everything shown is emulated or made up.
#include "../main.cpp"
#include <functional>
#include <iostream>
#include <stdexcept>

static void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
static bool waitUntil(const std::function<bool()>& done, int timeout = 10000) {
    QElapsedTimer timer; timer.start();
    while (!done() && timer.elapsed() < timeout) {
        QCoreApplication::processEvents(); QThread::msleep(5);
    }
    return done();
}
static void spin(int ms) {
    QElapsedTimer t; t.start(); waitUntil([&]{return t.elapsed() >= ms;}, ms + 1000);
}

struct HandshakeTest {   // friend of MainWindow and DataPanels
    static void click(MainWindow& w, const QString& text) {
        for (auto* b : w.findChildren<QPushButton*>()) if (b->text() == text) { b->click(); return; }
        throw std::runtime_error(("Button not found: " + text).toStdString());
    }
    static void setMode(MainWindow& w, const QByteArray& mode) {
        check(AcquisitionProtocol::publish(w.laserProcess->workingDirectory() + "/mock_mode.txt", mode), "Set mock mode");
    }
    static void save(QWidget& widget, const QString& path) {
        if (!widget.grab().save(path)) throw std::runtime_error(("Cannot write " + path).toStdString());
        std::cout << path.toStdString() << std::endl;
    }
    // One state of the window, at both sizes.
    static void save(MainWindow& w, const QString& dir, const QString& appearance, const QString& state) {
        for (const QSize& size : {QSize(1440, 880), QSize(1180, 720)}) {
            w.resize(size);
            spin(250);   // the layout settles
            // The status display follows on a timer, and the chart lays out its new title on a posted event:
            // a point saved just now would otherwise stand in the event line only.
            w.refreshStatusDisplay();
            QCoreApplication::sendPostedEvents(nullptr, QEvent::LayoutRequest);
            save(w, QString("%1/%2_%3x%4_%5.png").arg(dir, appearance).arg(size.width()).arg(size.height()).arg(state));
        }
    }
    static void selectCapture(DataPanels& panels, const QString& sample, int point) {
        for (int row = 0; row < panels.captures.size(); ++row) {
            const QJsonObject item = panels.captures.at(row).toObject();
            if (item["sample_id"].toString() == sample && item["point_id"].toInt() == point) { panels.captureTable->selectRow(row); return; }
        }
        throw std::runtime_error("Capture not in the list");
    }

    static void run(const QString& dir, bool dark) {
        const QString home = QCoreApplication::applicationDirPath();
        const QString appearance = dark ? "dark" : "light";
        // Both appearances start from the same canned catalog.
        for (const char* name : {"fake_data_layer_state.json", "fake_data_layer_calls.jsonl", "fake_data_layer_control.json"})
            QFile::remove(home + "/" + name);
        MainWindow w;
        w.applyTheme(dark ? UiTheme::darkAppearance() : UiTheme::light());
        w.show();
        DataPanels& panels = *w.dataPanels;
        check(waitUntil([&]{ return !panels.status.isEmpty() && !panels.refreshRunning; }), "first status and list of the stand-in data layer");
        save(w, dir, appearance, "01_idle");

        setMode(w, "laser_stays_off");
        click(w, "Laser ON");
        check(w.laserStateUncertain, "the emulated laser did not confirm");
        save(w, dir, appearance, "02_laser_unconfirmed");
        click(w, "Confirm Laser OFF");

        setMode(w, "spectrum");
        click(w, "Laser ON");
        check(w.laserOn, "emulated Laser ON confirmed");
        save(w, dir, appearance, "03_laser_on");

        w.lineAxisCombo->setCurrentIndex(w.lineAxisCombo->findData("1d-x"));
        click(w, "1D Scan");
        check(waitUntil([&]{ return w.lastCompletedPoint >= 3; }), "line scan running");
        save(w, dir, appearance, "04_scan_running");
        click(w, "Pause");
        check(waitUntil([&]{ return w.paused && !w.isCollecting; }), "scan paused");
        save(w, dir, appearance, "05_scan_paused");
        click(w, "Resume");
        check(waitUntil([&]{ return w.lastCompletedPoint == 13 && !w.scanActive; }, 30000), "line scan finished");
        check(waitUntil([&]{ return !panels.syncRunning && !panels.refreshRunning && !panels.status["last_sync_utc"].toString().isEmpty(); }),
              "sync after the scan");
        save(w, dir, appearance, "06_scan_finished");

        w.display.tabs->setCurrentIndex(1);
        selectCapture(panels, "TEST-PLATE-A", 2);
        check(waitUntil([&]{ return !panels.provenanceRunning && panels.captureProvenance->rowCount() == 4; }), "provenance of the selected capture");
        save(w, dir, appearance, "07_data_tab");

        w.display.tabs->setCurrentIndex(2);
        check(waitUntil([&]{ return !panels.reviewShownId.isEmpty() && !panels.reviewShowRunning; }), "review item shown");
        panels.commentEdit->setText("peak at 612 is there, but weak");
        save(w, dir, appearance, "08_review_tab");
        panels.commentEdit->clear();

        QDialog* importDialog = panels.openImportDialog();
        importDialog->findChild<QLineEdit*>("importFolder")->setText(home + "/scan_sessions/2d_scan_20260924_141500_001");
        spin(100);
        save(*importDialog, QString("%1/%2_import_dialog.png").arg(dir, appearance));
        importDialog->reject();

        // A failed capture: the scan aborts and the backend is marked faulty.
        w.display.tabs->setCurrentIndex(0);
        setMode(w, "fail");
        click(w, "Start Z scan then 2D scan");
        check(waitUntil([&]{ return !w.scanActive && !w.isCollecting && w.backendCaptureFault; }), "scan aborted");
        save(w, dir, appearance, "09_scan_failed");

        // A review recorded while the demo catalog is offline, and the same tab once a sync has sent it.
        const auto settled = [&]{
            return !panels.refreshRunning && !panels.syncRunning && !panels.offlineRunning && !panels.reviewSetRunning &&
                   !panels.reviewShowRunning && panels.dataLayer->running() == 0;
        };
        w.display.tabs->setCurrentIndex(2);
        panels.setOffline(true);
        check(waitUntil([&]{ return settled() && panels.catalogOffline() && !panels.reviewShownId.isEmpty(); }), "demo catalog offline");
        panels.confirmButton->click();
        check(waitUntil([&]{ return settled() && panels.reviewResult->text().contains("Kept on this Mac"); }), "review kept on this Mac");
        save(w, dir, appearance, "10_review_kept");
        panels.setOffline(false);
        check(waitUntil([&]{ return settled() && !panels.catalogOffline(); }), "demo catalog online");
        panels.syncButton->click();
        check(waitUntil([&]{ return settled() && panels.reviewResult->text().contains("Recorded in the demo catalog"); }), "review sent by the sync");
        save(w, dir, appearance, "11_review_sent");

        // The settings name DataFed while the rows are still those of the demo catalog, until the next sync.
        w.display.tabs->setCurrentIndex(1);
        panels.autoSync.stop();
        check(AcquisitionProtocol::publish(home + "/fake_data_layer_control.json", R"({"catalog": "datafed", "catalog_changed": true})"),
              "tell the stand-in that the catalog changed");
        panels.refresh();
        check(waitUntil([&]{ return settled() && panels.catalogChanged(); }), "rows of the catalog used before");
        selectCapture(panels, "TEST-PLATE-A", 2);
        check(waitUntil([&]{ return settled() && !panels.provenanceNote->text().isEmpty(); }), "details of a capture of the catalog used before");
        save(w, dir, appearance, "12_catalog_changed");

        // The two tabs of a copy without the data layer, as they would stand in the tab area.
        QLineEdit nobody;
        DataPanels absent(home + "/no_such_home", "python3", &nobody, nullptr);
        QWidget frame;
        frame.setObjectName("appRoot");
        frame.setPalette(uiTheme().palette());
        frame.setStyleSheet(uiTheme().styleSheet());
        auto* layout = new QVBoxLayout(&frame);
        layout->addWidget(absent.dataPage());
        delete absent.reviewPage();
        frame.resize(1100, 300);
        save(frame, QString("%1/%2_not_installed.png").arg(dir, appearance));
    }
};

int main(int argc, char** argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    // Only the emulators next to this program are started; both variables keep even real helper
    // programs simulated, and they are what the SIMULATION badge of the header reads.
    qputenv("RAMAN_BACKEND_SIMULATE", "1");
    qputenv("RAMAN_MOTOR_SIMULATE", "1");
    qputenv("RAMAN_DATAFED_PYTHON", UI_TEST_PYTHON);
    QApplication app(argc, argv);
    useUiBaseStyle();
    try {
        check(ramanHome() == QCoreApplication::applicationDirPath(), "must run next to the emulators (handshake_v2/ in this folder)");
        const QString dir = argc > 1 ? QDir(QString::fromLocal8Bit(argv[1])).absolutePath() : QCoreApplication::applicationDirPath() + "/snapshots";
        check(QDir().mkpath(dir), "cannot create the output folder");
        HandshakeTest::run(dir, false);
        HandshakeTest::run(dir, true);
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "FAIL " << ex.what() << std::endl; return 1;
    }
}
