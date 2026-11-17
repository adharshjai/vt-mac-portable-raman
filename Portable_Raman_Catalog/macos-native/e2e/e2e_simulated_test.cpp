// End-to-end test of the Mac-native pipeline in simulation: the REAL Qt controller code
// (production timeouts) drives the Python motor program and the .NET capture backend, both in
// their simulation modes, through the same files and processes used with hardware.
// Expects handshake_v2/{backend/SampleApl, motor/RamanAutofocus} next to this executable.
#define main controller_main          // keep the production main() out of this program
#include "../../laser_test/main.cpp"
#undef main
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>
#include <functional>
#include <iostream>
#include <stdexcept>

static void check(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }
static bool waitUntil(const std::function<bool()>& done, int timeoutMs) {
    QElapsedTimer t; t.start();
    while (!done() && t.elapsed() < timeoutMs) { QCoreApplication::processEvents(); QThread::msleep(10); }
    return done();
}
static QByteArray readFile(const QString& path) { QFile f(path); return f.open(QIODevice::ReadOnly) ? f.readAll() : QByteArray(); }

struct HandshakeTest {   // friend of MainWindow (see main.cpp)
    static void click(MainWindow& w, const QString& text) {
        for (auto* b : w.findChildren<QPushButton*>()) if (b->text() == text) { b->click(); return; }
        throw std::runtime_error(("button not found: " + text).toStdString());
    }
    static QJsonObject point(const QString& dir, int i) {
        const auto bytes = readFile(dir + QString("/point_%1.json").arg(i, 4, 10, QChar('0')));
        check(!bytes.isEmpty(), ("missing point file " + QString::number(i)).toStdString());
        return QJsonDocument::fromJson(bytes).object();
    }
    // session.json and the capture block of every point (data_layer/PROTOCOL.md section 2). A backend
    // that does not print the report lines of section 3 leaves the integration time and the instrument null.
    // dark: the simulated dark this run captured, which every simulated session of it is paired with.
    static void checkCaptureMetadata(MainWindow& w, const QString& mode, int points, const QString& dark) {
        const auto session = QJsonDocument::fromJson(readFile(w.sessionDir + "/session.json")).object();
        check(session["schema"].toString() == "raman-session/1" && session["session_id"].toString() == w.sessionId &&
              session["folder"].toString() == QFileInfo(w.sessionDir).fileName() && session["scan_mode"].toString() == mode,
              "session.json identifies the session");
        check(session["sample_id"].toString() == "E2E-SAMPLE" && session["operator"].toString() == "e2e simulated test" &&
              session["location"].toString() == "simulation" && session["notes"].toString().isEmpty(), "session.json holds the operator fields");
        check(session["laser_power_level"].toString() == "HIGH", "session.json holds the level sent with the confirmed Laser ON");
        check(session["simulated"].toObject() == QJsonObject{{"backend", true}, {"motor", true}}, "both simulators are labelled");
        check(session["dark_file"].toString() == dark, "session.json names the simulated dark of this run");
        check(session.contains("integration_time_us") &&
              (session["integration_time_us"].isNull() || session["integration_time_us"].toInteger() > 0), "integration time reported or null");
        check(session.contains("instrument") && (session["instrument"].isNull() || session["instrument"].isObject()),
              "instrument identity reported or null");
        check(session["software"].toObject()["name"].toString() == "portable-raman-controller-catalog", "software name");
        QJsonObject capture = session;
        for (const char* key : {"session_id", "folder", "scan_mode", "started_utc"}) capture.remove(key);
        capture["schema"] = "raman-capture/1";
        for (int i = 1; i <= points; ++i)
            check(point(w.sessionDir, i)["capture"].toObject() == capture, "capture block of point " + std::to_string(i));
    }
    static void run() {
        MainWindow w;
        // Required before a scan starts (data_layer/PROTOCOL.md section 2.1); this program remembers none of them.
        w.sampleIdEdit->setText("E2E-SAMPLE"); w.operatorEdit->setText("e2e simulated test"); w.locationEdit->setText("simulation");
        check(waitUntil([&]{ return w.laserProcess->state() == QProcess::Running; }, 15000), "backend process starts");
        QThread::msleep(1500); QCoreApplication::processEvents();   // let the backend initialise (prints its menu)

        std::cout << "[e2e] Laser ON through the backend (commands 6/1, 4/3, 5 readback)" << std::endl;
        click(w, "Laser ON");
        check(w.laserOn, "laser ON confirmed by the backend readback");

        std::cout << "[e2e] Capture Dark" << std::endl;
        const QString darks = QCoreApplication::applicationDirPath() + "/dark_measurements";
        const QStringList before = QDir(darks).entryList({"dark_*.txt"}, QDir::Files);
        click(w, "Capture Dark");
        check(waitUntil([&]{ return !w.isCollecting; }, 30000), "dark capture completes");
        QStringList added = QDir(darks).entryList({"dark_*.txt"}, QDir::Files);
        for (const QString& name : before) added.removeAll(name);
        check(added.size() == 1, "dark file saved");
        const QString dark = added.first();
        // Lead decision D1: beside the .txt, a .json that says the dark was taken in simulation.
        const auto record = QJsonDocument::fromJson(readFile(darks + "/" + dark.chopped(4) + ".json")).object();
        check(record["schema"].toString() == "raman-dark-description/1" && record["dark_file"].toString() == dark &&
              record["simulated"].toObject() == QJsonObject{{"backend", true}, {"motor", true}} &&
              record["instrument"].toObject()["serial"].toString() == "SIMULATED", "the dark's .json: simulated, the simulated device");

        std::cout << "[e2e] Focus monitor: 2 preview frames then OFF" << std::endl;
        w.monitorButton->click();
        check(waitUntil([&]{ return w.monitorFrameCount >= 2; }, 30000), "monitor acquires preview frames");
        w.monitorButton->click();
        check(waitUntil([&]{ return !w.isCollecting && !w.monitorActive; }, 30000), "monitor stops");

        std::cout << "[e2e] 1D X scan (13 points)" << std::endl;
        w.lineAxisCombo->setCurrentIndex(w.lineAxisCombo->findData("1d-x"));
        click(w, "1D Scan");
        check(waitUntil([&]{ return w.lastCompletedPoint == 13 && !w.scanActive; }, 180000), "1D X: 13 points complete");
        const auto first = point(w.sessionDir, 1);
        for (int i = 1; i <= 13; ++i) {
            const auto p = point(w.sessionDir, i);
            check(p["scan_mode"].toString() == "1d-x", "1D mode recorded");
            check(p["intensity"].toArray().size() == 2048, "1D full spectrum");
            check(p["controller_x_steps"].toInt() == (i - 1) * 69400, "1D X coordinate");
            check(p["controller_y_steps"].toInt() == first["controller_y_steps"].toInt(), "1D Y unchanged");
            check(p["controller_z_steps"].toInt() == first["controller_z_steps"].toInt(), "1D Z unchanged");
        }
        check(w.ramanProcess->exitCode() == 0, "motor program exits 0 after 1D");
        checkCaptureMetadata(w, "1d-x", 13, dark);

        std::cout << "[e2e] 2D scan: Z scan (4) + 120 XY points, ~3-4 minutes" << std::endl;
        click(w, "Start Z scan then 2D scan");
        check(waitUntil([&]{ return w.lastCompletedPoint == 124 && !w.scanActive; }, 900000), "2D: 124 points complete");
        const int zScan[4] = {135330, 138800, 142270, 145740};   // 3.8 mm offset + 0.1..0.4 mm at 34700 steps/mm
        for (int i = 1; i <= 124; ++i) {
            const auto p = point(w.sessionDir, i);
            check(p["intensity"].toArray().size() == 2048, "2D full spectrum");
            check(p["scan_mode"].toString() == "2d", "2D mode recorded");
            if (i <= 4) {
                check(p["controller_x_steps"].toInt() == 0 && p["controller_y_steps"].toInt() == 0, "Z scan at X=Y=home");
                check(p["controller_z_steps"].toInt() == zScan[i - 1], "Z scan position");
            } else {
                check(p["controller_z_steps"].toInt() == 138800, "XY points at the fixed working height (4.0 mm)");
            }
        }
        const auto p5 = point(w.sessionDir, 5), p124 = point(w.sessionDir, 124);
        check(p5["controller_x_steps"].toInt() == 69400 && p5["controller_y_steps"].toInt() == 72870, "first XY point X=2.0 mm, Y=2.1 mm");
        check(p124["controller_x_steps"].toInt() == 104100 && p124["controller_y_steps"].toInt() == 104100, "last XY point X=3.0 mm, Y=3.0 mm");
        check(readFile(w.sessionDir + "/laser_log_combined.txt").count("Image Data") == 124, "combined TXT holds all 124 frames");
        check(w.ramanProcess->exitCode() == 0, "motor program exits 0 after 2D");
        checkCaptureMetadata(w, "2d", 124, dark);

        std::cout << "[e2e] Laser OFF" << std::endl;
        click(w, "Laser OFF");
        check(!w.laserOn, "laser OFF confirmed");
        check(w.laserProcess->state() == QProcess::Running, "backend still alive at the end");
    }
};

int main(int argc, char** argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    qputenv("RAMAN_BACKEND_SIMULATE", "1");
    qputenv("RAMAN_MOTOR_SIMULATE", "1");
    QApplication app(argc, argv);
    try { HandshakeTest::run(); std::cout << "PASS e2e simulated pipeline: laser, dark, monitor, 1D (13), 2D (124)" << std::endl; return 0; }
    catch (const std::exception& ex) { std::cerr << "FAIL " << ex.what() << std::endl; return 1; }
}
