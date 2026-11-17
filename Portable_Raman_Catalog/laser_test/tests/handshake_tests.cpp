#include "../main.cpp"
#include "../hardware/SampleApl/capture_timing.h"
#include "../hardware/motor/scan_handshake.h"
#include "../hardware/motor/line_scan_plan.h"
#include <QTemporaryDir>
#include <QTimeZone>
#include <functional>
#include <iostream>
#include <stdexcept>
#ifndef Q_OS_WIN
#include <sys/stat.h>
#endif

static void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
static QByteArray readFile(const QString& path) {
    QFile f(path); if (!f.open(QIODevice::ReadOnly)) return {};
    return f.readAll();
}
static bool waitUntil(const std::function<bool()>& done, int timeout = 4000) {
    QElapsedTimer timer; timer.start();
    while (!done() && timer.elapsed() < timeout) {
        QCoreApplication::processEvents(); QThread::msleep(5);
    }
    return done();
}
static void spin(int ms) {
    QElapsedTimer t; t.start(); waitUntil([&]{return t.elapsed() >= ms;}, ms + 1000);
}
struct HandshakeTest {
    static void darkStorage() {
        MainWindow w;
        const QVector<int> pixels(2048, 4321);
        const QString first = w.saveDarkMeasurementToTxt(pixels);
        const QString second = w.saveDarkMeasurementToTxt(pixels);
        check(!first.isEmpty() && !second.isEmpty() && first != second, "dark saves distinct files");
        check(QFileInfo(first).absolutePath() == QCoreApplication::applicationDirPath() + "/dark_measurements", "dark subfolder");
        const auto content = readFile(first);
        check(content.contains("# Points: 2048") && content.contains("index\traman_shift_cm-1\tintensity"), "dark original format retained");
        check(content.count('\n') == 2052, "complete dark rows");
    }
    static void lineScan(const QString& mode) {
        MainWindow w;
        w.laserOn = true;
        AcquisitionProtocol::publish(w.laserProcess->workingDirectory() + "/mock_mode.txt", "ok");
        w.lineAxisCombo->setCurrentIndex(w.lineAxisCombo->findData(mode));
        click(w, "1D Scan");
        check(waitUntil([&]{return w.lastCompletedPoint == 13 && !w.scanActive;}, 20000), "13-point line scan completes");
        check(readFile(w.sessionDir + "/advanced.txt") == "13", "13 motor acknowledgements");
        check(QFileInfo(w.sessionDir).fileName().startsWith(mode == "1d-x" ? "1d_x_scan_" : "1d_y_scan_"), "readable scan directory");
        const auto combined = readFile(w.sessionDir + "/laser_log_combined.txt");
        QList<QByteArray> frames;
        for (const auto& line : combined.split('\n'))
            if (line.startsWith("Image Data")) frames.append(line);
        check(frames.size() == 13, "combined TXT contains all frames");
        check(QDir(w.sessionDir).entryList({"point_*.txt"}, QDir::Files).isEmpty(), "no individual TXT files");
        for (int i = 1; i <= 13; ++i) {
            const auto p = QJsonDocument::fromJson(readFile(w.sessionDir + QString("/point_%1.json").arg(i,4,10,QChar('0')))).object();
            check(p["scan_mode"].toString() == mode, "correct recorded mode");
            QVector<int> txtPixels;
            check(w.parseImageDataLineToInts(QString::fromUtf8(frames[i-1]), txtPixels), "combined TXT frame readable");
            check(txtPixels.size() == 2048, "TXT complete frame");
            for (int pixel = 0; pixel < txtPixels.size(); ++pixel)
                check(txtPixels[pixel] == p["intensity"].toArray()[pixel].toInt(), "TXT and JSON values identical");
            check(p["controller_x_steps"].toInt() == (mode == "1d-x" ? (i-1)*69400 : 69400), "X coordinate");
            check(p["controller_y_steps"].toInt() == (mode == "1d-y" ? (i-1)*69400 : 72870), "Y coordinate");
            check(p["controller_z_steps"].toInt() == 131860, "Z held fixed");
        }
        check(w.lineAxisCombo->isEnabled(), "axis selector restored after scan");
        check(combined.count("Image Data") == 13 && combined.count("Stop Capture") == 13, "combined TXT contains all 13 frames");
        for (int i = 1; i <= 13; ++i)
            check(combined.contains(QString("# Point: %1\n").arg(i).toUtf8()), "combined retains point identifiers");
    }
    static void monitor(bool fail = false) {
        MainWindow w;
        w.monitorButton->click();
        check(!w.monitorActive, "monitor requires explicit Laser ON");
        w.laserOn = true;
        const QString dir = w.laserProcess->workingDirectory();
        AcquisitionProtocol::publish(dir + "/mock_mode.txt", fail ? "fail" : "ok");
        w.monitorButton->click();
        if (fail) {
            check(waitUntil([&]{return !w.monitorActive && !w.isCollecting;}), "preview error stops monitor");
            check(w.backendCaptureFault && w.monitorFrameCount == 0, "bad preview not displayed");
            return;
        }
        click(w, "Start Z scan then 2D scan");
        click(w, "Capture Dark");
        check(w.sessionId.isEmpty(), "monitor blocks motor scan");
        check(waitUntil([&]{return w.monitorFrameCount >= 3;}), "monitor continuously acquires");
        w.monitorButton->click();
        check(waitUntil([&]{return !w.isCollecting;}), "preview drains before another operation");
        const int count = readFile(dir + "/calls.txt").count('\n');
        spin(250);
        check(readFile(dir + "/calls.txt").count('\n') == count, "OFF stops new captures");
        check(w.rtSeries->count() == 2048, "monitor displays complete frame");
        check(w.ramanProcess->state() == QProcess::NotRunning, "monitor never starts motor");
        check(QDir(dir + "/capture_results").entryList({"*.txt"}, QDir::Files).isEmpty(), "no preview spectral files");
        check(!readFile(w.laserTxtFile.fileName()).contains("MONITOR_FRAME"), "preview pixels excluded from log");
        start(w);
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "normal scan still works after monitor");
    }
    static void duringCapture(MainWindow& w, const QString& button) {
        auto* timer = new QTimer(&w);
        timer->setInterval(5);
        QObject::connect(timer, &QTimer::timeout, &w, [&w, timer, button]{
            if (w.isCollecting) { timer->stop(); click(w, button); }
        });
        timer->start();
    }
    static void click(MainWindow& w, const QString& text) {
        for (auto* b : w.findChildren<QPushButton*>()) if (b->text() == text) { b->click(); return; }
        throw std::runtime_error("Button not found");
    }
    static void start(MainWindow& w, const QByteArray& mode = "ok") {
        check(w.laserProcess->state() == QProcess::Running, "Mock backend not running");
        check(AcquisitionProtocol::publish(w.laserProcess->workingDirectory() + "/mock_mode.txt", mode), "Set mock mode");
        w.laserOn = true; // Tests only: bypass physical laser controls, never call an SDK.
        click(w, "Start Z scan then 2D scan");
    }
    static void success(const QByteArray& mode = "ok") {
        MainWindow w; start(w, mode);
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "3 points must complete");
        check(readFile(w.sessionDir + "/advanced.txt") == "3", "motor advances after saved point");
        check(readFile(w.laserProcess->workingDirectory() + "/calls.txt").count('\n') == 3, "duplicates must not recapture");
        check(readFile(w.sessionDir + "/laser_log_combined.txt").count("Image Data") == 3, "combined TXT has no duplicate frames");
        const auto p = QJsonDocument::fromJson(readFile(w.sessionDir + "/point_0001.json")).object();
        check(p["intensity"].toArray().size() == 2048 && p["controller_z_steps"].toInt() == 131860, "saved spectrum and coordinates");
    }
    static void pause() {
        MainWindow w; start(w);
        duringCapture(w, "Pause");
        check(waitUntil([&]{return w.lastCompletedPoint == 1 && !w.isCollecting;}), "in-flight point saved during pause");
        spin(150);
        check(readFile(w.sessionDir + "/advanced.txt").isEmpty(), "no advance during pause");
        const auto logSize = QFileInfo(w.laserTxtFile.fileName()).size();
        click(w, "Resume");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "resume completes");
        check(QFileInfo(w.laserTxtFile.fileName()).size() >= logSize, "resume does not clear logs");
        check(readFile(w.laserProcess->workingDirectory() + "/calls.txt").count('\n') == 3, "paused point not reacquired");
    }
    static void failure(const QByteArray& mode) {
        MainWindow w; start(w, mode);
        check(waitUntil([&]{return !w.scanActive && !w.isCollecting;}), "failure must stop scan");
        check(w.lastCompletedPoint == 0 && w.backendCaptureFault, "failed data not accepted");
        check(w.ramanProcess->exitStatus() == QProcess::NormalExit && w.ramanProcess->exitCode() == 4,
              "allow delayed motor cleanup without force-kill");
        check(!QFile::exists(w.sessionDir + "/point_0001.json"), "failure must not save success record");
        check(readFile(w.sessionDir + "/advanced.txt").isEmpty(), "motor must not advance after failure");
        const auto run = w.sessionId;
        click(w, "Start Z scan then 2D scan");
        check(w.sessionId == run, "backend fault blocks new scan");
        if (mode == "late") {
            spin(1000);
            check(w.lastCompletedPoint == 0, "late result cannot be accepted after timeout");
        }
    }
    static void stop() {
        MainWindow w; start(w);
        duringCapture(w, "Stop");
        check(waitUntil([&]{return !w.scanActive && !w.isCollecting;}), "stop completes");
        check(readFile(w.sessionDir + "/advanced.txt").isEmpty(), "no advance after Stop");
        check(w.lastCompletedPoint == 0, "in-flight cancelled capture not attributed to a scan point");
    }
    static void darkIsolation() {
        MainWindow w; start(w);
        duringCapture(w, "Capture Dark");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "scan survives dark-button click");
        check(readFile(w.laserProcess->workingDirectory() + "/calls.txt").count('\n') == 3, "dark cannot overwrite scan capture command");
    }

    // ---- Capture metadata: data_layer/PROTOCOL.md sections 2 and 3 ----
    static QJsonObject json(const QString& path) { return QJsonDocument::fromJson(readFile(path)).object(); }
    static QJsonObject point(const QString& dir, int id) { return json(dir + QString("/point_%1.json").arg(id,4,10,QChar('0'))); }
    static QStringList lines(const QString& path) { return QString::fromUtf8(readFile(path)).split('\n', Qt::SkipEmptyParts); }
    static QStringList laserCommands(MainWindow& w) { return lines(w.laserProcess->workingDirectory() + "/laser_commands.txt"); }
    static QStringList captureCalls(MainWindow& w) { return lines(w.laserProcess->workingDirectory() + "/calls.txt"); }
    static QString scanLog(MainWindow& w) { return w.ramanOutput->toPlainText(); }
    static bool oneLineAdded(MainWindow& w, const QString& before, const QString& line) {
        return scanLog(w) == (before.isEmpty() ? line : before + "\n" + line);
    }
    static QStringList sessionFolders() {
        QStringList folders;
        const QDir root(QCoreApplication::applicationDirPath() + "/scan_sessions");
        for (const QString& name : root.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name))
            if (!name.startsWith("_backend_")) folders << name;
        return folders;
    }
    static void setMode(MainWindow& w, const QByteArray& mode) {
        check(AcquisitionProtocol::publish(w.laserProcess->workingDirectory() + "/mock_mode.txt", mode), "Set mock mode");
    }
    static bool fieldsEditable(MainWindow& w) {
        return !w.sampleIdEdit->isReadOnly() && !w.operatorEdit->isReadOnly() && !w.locationEdit->isReadOnly() && !w.notesEdit->isReadOnly();
    }
    static bool fieldsLocked(MainWindow& w) {
        return w.sampleIdEdit->isReadOnly() && w.operatorEdit->isReadOnly() && w.locationEdit->isReadOnly() && w.notesEdit->isReadOnly();
    }
    // Laser ON the way the operator does it: through the emulated laser menu and its read-back.
    static void confirmLaserOn(MainWindow& w) {
        check(w.laserProcess->state() == QProcess::Running, "Mock backend not running");
        click(w, "Laser ON");
        check(w.laserOn && !w.laserStateUncertain, "Laser ON confirmed by the emulated read-back");
    }
    static QJsonObject emulatedInstrument() {
        return {{"model", "EMULATOR"}, {"serial", "EMU-0001"}, {"unit", "EMULATED UNIT 1"}, {"firmware", "0.00"}};
    }
    static QJsonObject captureBlock(const QString& sample, const QString& op, const QString& location, const QString& notes,
                                    const QJsonValue& level, const QJsonValue& integration, const QJsonValue& instrument,
                                    const QJsonValue& dark) {
        return {{"schema", "raman-capture/1"}, {"sample_id", sample}, {"operator", op}, {"location", location},
                {"notes", notes}, {"laser_power_level", level}, {"integration_time_us", integration},
                {"instrument", instrument}, {"simulated", simulatedHere()},
                {"dark_file", dark},
                {"software", QJsonObject{{"name", "portable-raman-controller-catalog"}, {"version", "1.0.0"}}}};
    }
    // main() below sets both variables to 1; only the helper programs of macos-native/ act on them.
    static QJsonObject simulatedHere() { return {{"backend", kHelpersHaveSimulators}, {"motor", kHelpersHaveSimulators}}; }
    // session.json = the capture fields under the session schema, plus the four session keys.
    static QJsonObject sessionFor(QJsonObject capture, const QString& id, const QString& dir, const QString& mode, const QJsonValue& started) {
        capture["schema"] = "raman-session/1"; capture["session_id"] = id;
        capture["folder"] = QFileInfo(dir).fileName(); capture["scan_mode"] = mode; capture["started_utc"] = started;
        return capture;
    }
    static QJsonObject captureOf(QJsonObject session) {
        for (const char* key : {"session_id", "folder", "scan_mode", "started_utc"}) session.remove(key);
        session["schema"] = "raman-capture/1";
        return session;
    }
    static QStringList pointKeys() {
        return {"capture", "capture_elapsed_ms", "capture_id", "completed_utc", "controller_x_steps", "controller_y_steps",
                "controller_z_steps", "intensity", "pixel_count", "point_id", "protocol", "scan_mode", "session_id",
                "started_utc", "status"};
    }
    static bool utcStamp(const QJsonValue& value, const QDateTime& notBefore) {
        const QString text = value.toString();
        const QDateTime time = QDateTime::fromString(text, Qt::ISODateWithMs);
        return QRegularExpression("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$").match(text).hasMatch()
            && time.isValid() && time >= notBefore && time <= QDateTime::currentDateTimeUtc();
    }
    static QDateTime nowUtcMs() {   // truncated like the recorded stamps
        return QDateTime::fromMSecsSinceEpoch(QDateTime::currentMSecsSinceEpoch(), QTimeZone::UTC);
    }
    // A dark saved the way Capture Dark saves it, .txt and .json, without a capture command.
    static QString newestDark(MainWindow& w) {
        const QString path = w.saveDark(QVector<int>(2048, 7), QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
        check(!path.isEmpty() && QFile::exists(darkRecordPath(path)), "dark saved with its .json");
        return QFileInfo(path).fileName();
    }
    static QString darkFolder() { return QCoreApplication::applicationDirPath() + "/dark_measurements"; }
    static bool primary(const QPushButton* button) { return button->property("role").toString() == "primary"; }

    static void backendReport() {
        const QString logPath = QCoreApplication::applicationDirPath() + "/laser_log.txt";
        const qint64 logStart = QFileInfo(logPath).size();
        MainWindow w;
        check(w.integrationTimeUs.isNull() && w.instrumentInfo.isNull(), "nothing known before the backend reports it");
        check(waitUntil([&]{return w.integrationTimeUs.isDouble() && w.instrumentInfo.isObject();}), "backend report lines read");
        check(w.integrationTimeUs.toInteger() == 1000000, "integration time from CAPTURE_SETTINGS");
        check(w.instrumentInfo.toObject() == emulatedInstrument(), "instrument identity from MODULE_INFO, quoted values with spaces");
        const QByteArray logged = readFile(logPath).mid(logStart);
        check(logged.contains("[Backend] CAPTURE_SETTINGS exposure_time_us=1000000 cycle_time_us=1100000") &&
              logged.contains("[Backend] MODULE_INFO model=\"EMULATOR\" serial=\"EMU-0001\""), "report lines still go to the log");
        // Line forms the real backend may print (the loop hands over one trimmed line at a time).
        w.readBackendReport("CAPTURE_SETTINGS cycle_time_us=600000 exposure_time_us=500000");
        check(w.integrationTimeUs.toInteger() == 500000, "exposure found wherever it stands in the line");
        w.readBackendReport("CAPTURE_SETTINGS exposure_time_us=soon");
        w.readBackendReport("CAPTURE_SETTINGS next_exposure_time_us=7");
        w.readBackendReport("[Debug] CAPTURE_SETTINGS exposure_time_us=7");
        check(w.integrationTimeUs.toInteger() == 500000, "unreadable or foreign lines change nothing");
        w.readBackendReport("MODULE_INFO model=\"C15471\" serial=\"823OK002\" unit=\"\" firmware=\"1.03\" extra=\"serial=\"");
        check(w.instrumentInfo.toObject() == QJsonObject{{"model", "C15471"}, {"serial", "823OK002"}, {"unit", ""}, {"firmware", "1.03"}},
              "the four identity values, an empty one included, nothing else");
        w.readBackendReport("MODULE_INFO unavailable");
        check(w.instrumentInfo.toObject()["model"].toString() == "C15471", "an unavailable report does not invent or erase values");
    }
    // A backend that does not print the lines (older build), or whose module information call failed.
    static void backendReportMissing(const QByteArray& emulate) {
        qputenv("MOCK_BACKEND_INFO", emulate);
        const auto restore = qScopeGuard([]{ qunsetenv("MOCK_BACKEND_INFO"); });
        MainWindow w;
        confirmLaserOn(w);   // The read-back arrives after everything printed at start-up, so nothing is still on its way.
        const QJsonValue integration = emulate == "absent" ? QJsonValue() : QJsonValue(1000000);
        check(w.integrationTimeUs == integration && w.instrumentInfo.isNull(), "missing report leaves the value null");
        const QString dark = newestDark(w);
        setMode(w, "ok");
        click(w, "Start Z scan then 2D scan");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "scan works without the report lines");
        const QJsonObject session = json(w.sessionDir + "/session.json");
        check(session.contains("integration_time_us") && session.contains("instrument"), "keys present, values null");
        const QJsonObject capture = captureBlock("TEST-SAMPLE", "handshake tests", "test bench", "", "HIGH", integration, QJsonValue(), dark);
        check(session == sessionFor(capture, w.sessionId, w.sessionDir, "2d", session["started_utc"]), "session.json with null values");
        for (int i = 1; i <= 3; ++i) check(point(w.sessionDir, i)["capture"].toObject() == capture, "capture block with null values");
    }
    static void sessionFile() {
        MainWindow w;
        confirmLaserOn(w);
        check(w.integrationTimeUs.isDouble() && w.instrumentInfo.isObject(), "report lines precede the laser read-back");
        for (QLineEdit* field : {w.sampleIdEdit, w.operatorEdit, w.locationEdit, w.notesEdit}) {
            field->setText(QString(201, 'x'));
            check(field->text().size() == 200, "a field takes at most 200 characters, as the Studio and the runner do");
        }
        w.sampleIdEdit->setText("  S-001 "); w.operatorEdit->setText("Operator One");
        w.locationEdit->setText("Lab 123"); w.notesEdit->setText(" first run ");
        const QJsonObject capture = captureBlock("S-001", "Operator One", "Lab 123", "first run", "HIGH", 1000000,
                                                 emulatedInstrument(), newestDark(w));
        setMode(w, "ok");
        const QDateTime before = nowUtcMs();
        click(w, "Start Z scan then 2D scan");
        check(w.scanActive, "scan started");
        const QJsonObject session = json(w.sessionDir + "/session.json");
        check(QDir(w.sessionDir).entryList({"point_*"}, QDir::Files).isEmpty(), "session.json is there before any point");
        check(utcStamp(session["started_utc"], before), "started_utc is a UTC stamp with milliseconds");
        check(session == sessionFor(capture, w.sessionId, w.sessionDir, "2d", session["started_utc"]), "session.json content");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "3 points must complete");
        for (int i = 1; i <= 3; ++i) {
            const QJsonObject p = point(w.sessionDir, i);
            check(p.keys() == pointKeys(), "existing point keys kept, exactly one key added");
            check(p["capture"].toObject() == capture, "capture block in every point");
            check(p["session_id"].toString() == w.sessionId && p["point_id"].toInt() == i && p["scan_mode"].toString() == "2d" &&
                  p["protocol"].toInt() == 2 && p["status"].toString() == "OK" &&
                  p["capture_id"].toString() == w.sessionId + "_" + QString::number(i), "existing keys keep their values");
            check(utcStamp(p["started_utc"], before) && utcStamp(p["completed_utc"], before), "point stamps unchanged in form");
        }
        check(json(w.sessionDir + "/session.json") == session, "session.json is written once");
    }
    // The limit of 200 counts UTF-16 units, and a character outside the Basic Multilingual Plane (an
    // emoji) takes two. However a text reaches a field, the field never keeps half of such a character,
    // so session.json and the points never carry one (it would be written as \ud83d, text that the data
    // layer cannot store).
    static void wholeCharacters() {
        const QString emoji = QString::fromUtf8("\xF0\x9F\x94\xAC");   // U+1F52C
        const QString high(QChar(0xD83D)), low(QChar(0xDD2C));         // its two halves on their own
        MainWindow w;
        confirmLaserOn(w);
        setMode(w, "ok");
        struct Field { QLineEdit* edit; char letter; const char* key; };
        const QList<Field> fields{{w.sampleIdEdit, 's', "sample_id"}, {w.operatorEdit, 'o', "operator"},
                                  {w.locationEdit, 'l', "location"}, {w.notesEdit, 'n', "notes"}};
        for (const Field& field : fields) {
            QLineEdit* edit = field.edit;
            edit->setText(QString(199, field.letter) + emoji + "tail");
            check(edit->text() == QString(199, field.letter), "a text longer than the field: cut before the emoji, not through it");
            edit->insert(emoji);
            check(edit->text() == QString(199, field.letter), "an emoji typed or pasted with one unit of room left: not inserted");
            edit->setText(QString(100, field.letter));
            edit->setCursorPosition(50);
            edit->insert(high + "x" + low);
            check(edit->text() == QString(50, field.letter) + "x" + QString(50, field.letter) && edit->cursorPosition() == 51,
                  "half characters pasted on their own are left out");
            edit->setText(QString(197, field.letter));
            edit->setCursorPosition(100);
            edit->insert(emoji + emoji);
            check(edit->text() == QString(100, field.letter) + emoji + QString(97, field.letter) && edit->cursorPosition() == 102,
                  "a paste in the middle that crosses the limit: whole characters only");
        }
        click(w, "Capture Spectrum");
        check(QFile::exists(w.singleCaptureDir + "/point_0001.json"), "single capture saved");
        for (const QString& file : {QString("/session.json"), QString("/point_0001.json")}) {
            const QByteArray bytes = readFile(w.singleCaptureDir + file);
            check(!bytes.toLower().contains("\\ud") && bytes.count(emoji.toUtf8()) == 4, "each emoji whole, as UTF-8, and no half character");
        }
        const QJsonObject session = json(w.singleCaptureDir + "/session.json");
        for (const Field& field : fields) check(session[field.key].toString() == field.edit->text(), "recorded as the field shows it");
    }
    static void lineScanSession() {
        MainWindow w;
        confirmLaserOn(w);
        setMode(w, "ok");
        w.lineAxisCombo->setCurrentIndex(w.lineAxisCombo->findData("1d-y"));
        click(w, "1D Scan");
        const QJsonObject session = json(w.sessionDir + "/session.json");
        check(session["scan_mode"].toString() == "1d-y" && session["folder"].toString().startsWith("1d_y_scan_") &&
              session["folder"].toString() == QFileInfo(w.sessionDir).fileName(), "line scan session.json");
        check(waitUntil([&]{return w.lastCompletedPoint == 13 && !w.scanActive;}, 20000), "13-point line scan completes");
        for (int i = 1; i <= 13; ++i) {
            const QJsonObject p = point(w.sessionDir, i);
            check(p.keys() == pointKeys() && p["capture"].toObject() == captureOf(session), "capture block in every line point");
        }
    }
    static void requiredFields() {
        MainWindow w;
        setMode(w, "ok");
        w.laserOn = true;
        const QStringList folders = sessionFolders();
        const auto nothingHappened = [&](const QString& before, const QString& line) {
            check(oneLineAdded(w, before, line), "one clear line names the missing fields");
            check(w.sessionId.isEmpty() && w.sessionDir.isEmpty() && w.singleCaptureDir.isEmpty() && !w.scanActive &&
                  w.scanCancelled && !w.isCollecting && w.currentScanMode == "2d" && w.lineAxisCombo->isEnabled() &&
                  fieldsEditable(w), "no state change");
            check(w.ramanProcess->state() == QProcess::NotRunning && captureCalls(w).isEmpty(), "no motor program, no capture");
            check(sessionFolders() == folders, "no folder");
        };
        struct Field { QLineEdit* edit; QString name; };
        const QList<Field> required{{w.sampleIdEdit, "Sample ID"}, {w.operatorEdit, "Operator"}, {w.locationEdit, "Location"}};
        for (const Field& field : required) {
            const QString kept = field.edit->text();
            field.edit->setText("   ");   // blanks only count as empty
            QString before = scanLog(w);
            click(w, "Start Z scan then 2D scan");
            nothingHappened(before, "[Scan] Not started: fill in " + field.name + " first.");
            before = scanLog(w);
            click(w, "1D Scan");
            nothingHappened(before, "[Scan] Not started: fill in " + field.name + " first.");
            before = scanLog(w);
            click(w, "Capture Spectrum");
            nothingHappened(before, "[Capture] Not started: fill in " + field.name + " first.");
            field.edit->setText(kept);
        }
        for (const Field& field : required) field.edit->clear();
        QString before = scanLog(w);
        click(w, "Start Z scan then 2D scan");
        nothingHappened(before, "[Scan] Not started: fill in Sample ID, Operator, Location first.");
        w.monitorButton->click();   // The guard comes first: even with the monitor running only the fields are named.
        check(waitUntil([&]{return w.monitorFrameCount >= 1;}), "monitor running");
        before = scanLog(w);
        click(w, "Start Z scan then 2D scan");
        check(oneLineAdded(w, before, "[Scan] Not started: fill in Sample ID, Operator, Location first."), "field guard is the first check");
        w.monitorButton->click();
        check(waitUntil([&]{return !w.isCollecting;}), "preview drains");
        w.sampleIdEdit->setText("S-2"); w.operatorEdit->setText("Operator Two"); w.locationEdit->setText("Lab 2");
        check(w.notesEdit->text().isEmpty(), "Notes is optional");
        click(w, "Start Z scan then 2D scan");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "scan starts once the required fields are filled");
        check(json(w.sessionDir + "/session.json")["sample_id"].toString() == "S-2", "filled value recorded");
    }
    static void fieldLock() {
        {
            MainWindow w;
            check(fieldsEditable(w), "fields editable before a scan");
            bool lockedInExposure = false;
            auto* timer = new QTimer(&w);
            timer->setInterval(5);
            QObject::connect(timer, &QTimer::timeout, &w, [&w, timer, &lockedInExposure]{
                if (!w.isCollecting) return;
                timer->stop();
                lockedInExposure = fieldsLocked(w);
                // What a program could still do to a read-only field must not reach the running scan.
                w.sampleIdEdit->setText("CHANGED"); w.operatorEdit->setText("CHANGED");
                w.locationEdit->setText("CHANGED"); w.notesEdit->setText("CHANGED");
                w.powerCombo->setCurrentIndex(0);
            });
            timer->start();
            start(w);
            check(w.scanActive && fieldsLocked(w), "fields read-only from the moment the scan starts");
            check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "3 points must complete");
            check(lockedInExposure, "fields read-only during the scan");
            check(fieldsEditable(w), "fields editable again when the scan ends");
            const QJsonObject session = json(w.sessionDir + "/session.json");
            check(session["sample_id"].toString() == "TEST-SAMPLE" && session["operator"].toString() == "handshake tests" &&
                  session["location"].toString() == "test bench" && session["notes"].toString().isEmpty(), "values taken at the scan start");
            for (int i = 1; i <= 3; ++i)
                check(point(w.sessionDir, i)["capture"].toObject() == captureOf(session), "a change during the scan reaches no point");
        }
        {
            MainWindow w; start(w);
            duringCapture(w, "Stop");
            check(waitUntil([&]{return !w.scanActive && !w.isCollecting;}), "stop completes");
            check(fieldsEditable(w), "fields editable after Stop");
        }
        {
            MainWindow w; start(w, "fail");
            check(waitUntil([&]{return !w.scanActive && !w.isCollecting;}), "failure must stop scan");
            check(fieldsEditable(w), "fields editable after a failed scan");
        }
    }
    static void powerInForce() {
        MainWindow w;
        check(!w.powerInForce.has_value(), "no level in force before a confirmed Laser ON");
        setMode(w, "laser_stays_off");
        w.powerCombo->setCurrentIndex(1);
        click(w, "Laser ON");
        check(!w.laserOn && w.laserStateUncertain, "read-back OFF: Laser ON is not confirmed");
        check(laserCommands(w) == QStringList({"ENABLE 1", "POWER 2"}), "Medium was sent");
        check(!w.powerInForce.has_value(), "a level that was sent but not confirmed is not in force");
        QString before = scanLog(w);
        click(w, "Capture Spectrum");
        check(oneLineAdded(w, before, "[Capture] Turn on the laser and confirm its status first.") && w.singleCaptureDir.isEmpty(),
              "no single capture while the laser state is unconfirmed");
        click(w, "Confirm Laser OFF");
        check(!w.laserOn && !w.laserStateUncertain, "Laser OFF confirmed");
        w.refreshStatusDisplay();
        check(w.display.laser->value() == "OFF", "the LASER pill says OFF once it is read back, also with no Laser ON confirmed before");
        setMode(w, "ok");
        w.powerCombo->setCurrentIndex(0);
        click(w, "Laser ON");
        check(w.laserOn && laserCommands(w).mid(3) == QStringList({"ENABLE 1", "POWER 1"}), "Low was sent with the confirmed Laser ON");
        check(w.powerInForce == MainWindow::PowerLevel::Low, "the sent level is in force once Laser ON is confirmed");
        w.powerCombo->setCurrentIndex(2);   // The Power box moves afterwards: nothing is sent, the laser stays at Low.
        check(w.selectedPower == MainWindow::PowerLevel::High && laserCommands(w).size() == 5, "moving the Power box sends nothing");
        check(w.powerInForce == MainWindow::PowerLevel::Low, "moving the Power box does not change the level in force");
        click(w, "Start Z scan then 2D scan");
        check(json(w.sessionDir + "/session.json")["laser_power_level"].toString() == "LOW", "session.json records the level in force");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "3 points must complete");
        for (int i = 1; i <= 3; ++i)
            check(point(w.sessionDir, i)["capture"].toObject()["laser_power_level"].toString() == "LOW", "points record the level in force");
        click(w, "Capture Spectrum");
        check(json(w.singleCaptureDir + "/session.json")["laser_power_level"].toString() == "LOW" &&
              point(w.singleCaptureDir, 1)["capture"].toObject()["laser_power_level"].toString() == "LOW", "single capture records the level in force");
        click(w, "Laser OFF");
        check(!w.laserOn, "Laser OFF confirmed");
        click(w, "Laser ON");
        check(w.laserOn && laserCommands(w).mid(6) == QStringList({"ENABLE 1", "POWER 3"}) &&
              w.powerInForce == MainWindow::PowerLevel::High, "the next confirmed Laser ON puts its own level in force");
        click(w, "Start Z scan then 2D scan");
        check(json(w.sessionDir + "/session.json")["laser_power_level"].toString() == "HIGH", "new scan, new level");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "3 points must complete");
    }
    static void singleCapture() {
        MainWindow w;
        confirmLaserOn(w);
        setMode(w, "ok");
        w.notesEdit->setText("single shot");
        const QString dark = newestDark(w);
        const QStringList folders = sessionFolders();
        bool lockedInExposure = false;
        auto* timer = new QTimer(&w);
        timer->setInterval(5);
        QObject::connect(timer, &QTimer::timeout, &w, [&w, timer, &lockedInExposure]{
            if (w.isCollecting) { timer->stop(); lockedInExposure = fieldsLocked(w); }
        });
        timer->start();
        const QDateTime before = nowUtcMs();
        click(w, "Capture Spectrum");
        check(!w.isCollecting && lockedInExposure && fieldsEditable(w), "fields read-only during the capture only");
        const QString dir = w.singleCaptureDir;
        const QString folder = QFileInfo(dir).fileName();
        check(QRegularExpression("^single_\\d{8}_\\d{6}_\\d{3}$").match(folder).hasMatch(), "single_<stamp> folder");
        check(sessionFolders().size() == folders.size() + 1 && sessionFolders().contains(folder), "exactly one new folder");
        check(QDir(dir).entryList(QDir::Files | QDir::Hidden, QDir::Name) == QStringList({"point_0001.json", "session.json"}),
              "one point and session.json: no motor, acknowledge or temporary files");
        const QJsonObject session = json(dir + "/session.json");
        const QString id = session["session_id"].toString();
        const QJsonObject capture = captureBlock("TEST-SAMPLE", "handshake tests", "test bench", "single shot", "HIGH", 1000000,
                                                 emulatedInstrument(), dark);
        check(!QUuid::fromString(id).isNull() && utcStamp(session["started_utc"], before), "session id and start time");
        check(session == sessionFor(capture, id, dir, "single", session["started_utc"]), "single capture session.json");
        const QJsonObject p = point(dir, 1);
        check(p.keys() == pointKeys(), "same keys as a scan point");
        check(p["scan_mode"].toString() == "single" && p["point_id"].toInt() == 1 && p["protocol"].toInt() == 2 &&
              p["status"].toString() == "OK" && p["session_id"].toString() == id && p["capture_id"].toString() == "single_" + id,
              "single point identity");
        check(p["controller_x_steps"].isNull() && p["controller_y_steps"].isNull() && p["controller_z_steps"].isNull(),
              "position unknown without the motor program: null");
        check(p["pixel_count"].toInt() == 2048 && p["intensity"].toArray().size() == 2048 && p["intensity"].toArray()[5].toInt() == 16,
              "complete spectrum saved");
        check(utcStamp(p["started_utc"], before) && utcStamp(p["completed_utc"], before), "capture stamps");
        check(p["capture"].toObject() == capture, "capture block in the single point");
        check(w.rtSeries->count() == 2048, "spectrum shown");
        check(scanLog(w).endsWith("[Capture] Saved: " + dir), "folder logged");
        check(captureCalls(w) == QStringList({"single_" + id}), "one capture command with the single capture id");
        check(w.sessionId.isEmpty() && w.sessionDir.isEmpty() && !w.scanActive && w.lastCompletedPoint == 0 &&
              w.ramanProcess->state() == QProcess::NotRunning && w.ramanProcess->processId() == 0, "scan state untouched, motor never started");
        click(w, "Capture Spectrum");
        check(w.singleCaptureDir != dir && json(w.singleCaptureDir + "/session.json")["session_id"].toString() != id &&
              captureCalls(w).size() == 2, "every single capture has its own folder and ids");
        click(w, "Start Z scan then 2D scan");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "normal scan still works after a single capture");
        check(QDir(w.singleCaptureDir).entryList(QDir::Files, QDir::Name) == QStringList({"point_0001.json", "session.json"}),
              "a later scan and its stop leave the single capture folder alone");
    }
    static void singleCaptureGuards() {
        MainWindow w;
        setMode(w, "ok");
        const QStringList folders = sessionFolders();
        const auto refused = [&](const QString& line) {
            const QString before = scanLog(w);
            click(w, "Capture Spectrum");
            check(oneLineAdded(w, before, line), "single capture refusal reported");
            check(sessionFolders() == folders && w.singleCaptureDir.isEmpty() && captureCalls(w).isEmpty() && fieldsEditable(w),
                  "refused single capture leaves nothing");
        };
        refused("[Capture] Turn on the laser and confirm its status first.");
        w.laserOn = true;
        w.backendCaptureFault = true;
        refused("[Capture] Capture fault: restart after confirming Laser OFF.");
        w.backendCaptureFault = false;
        w.laserCommandBusy = true;
        refused("[Capture] Stop the scan and wait for acquisition to finish first.");
        w.laserCommandBusy = false;
        w.monitorButton->click();
        check(waitUntil([&]{return w.monitorFrameCount >= 1;}), "monitor running");
        const QString before = scanLog(w);
        click(w, "Capture Spectrum");
        check(oneLineAdded(w, before, "[Capture] Stop Focus Monitor first.") && w.singleCaptureDir.isEmpty(), "monitor blocks single capture");
        w.monitorButton->click();
        check(waitUntil([&]{return !w.isCollecting;}), "preview drains");
        check(QDir(w.laserProcess->workingDirectory() + "/capture_results").entryList({"*.txt"}, QDir::Files).isEmpty(), "nothing was saved");
        const qsizetype previews = captureCalls(w).size();
        start(w);
        duringCapture(w, "Capture Spectrum");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "scan survives a Capture Spectrum click");
        check(captureCalls(w).size() == previews + 3 && w.singleCaptureDir.isEmpty(), "single capture cannot overwrite a scan capture command");
        MainWindow gone;   // A backend that has gone away is noticed before a session folder is made.
        gone.laserOn = true;
        gone.laserProcess->kill();
        check(gone.laserProcess->waitForFinished(3000), "emulator stopped");
        const QStringList existing = sessionFolders();
        click(gone, "Capture Spectrum");
        check(scanLog(gone) == "[Capture] Laser backend is not running." && sessionFolders() == existing && gone.singleCaptureDir.isEmpty(),
              "no single capture and no folder without the backend");
    }
    static void singleCaptureFailure(const QByteArray& mode) {
        MainWindow w;
        confirmLaserOn(w);
        setMode(w, mode);
        click(w, "Capture Spectrum");
        const QString dir = w.singleCaptureDir;
        check(!w.isCollecting && w.backendCaptureFault && fieldsEditable(w), "failed single capture ends as a capture fault");
        check(QDir(dir).entryList(QDir::Files | QDir::Hidden, QDir::Name) == QStringList({"failure.txt", "session.json"}),
              "no point, nothing half-written");
        check(!readFile(dir + "/failure.txt").isEmpty() && scanLog(w).contains("[Capture Error] " + QString::fromUtf8(readFile(dir + "/failure.txt"))),
              "failure reported with its reason");
        check(w.rtSeries->count() == 0, "no spectrum shown for a failed capture");
        if (mode == "late") {
            spin(1000);
            check(QDir(dir).entryList({"point_*"}, QDir::Files | QDir::Hidden).isEmpty(), "late result cannot be accepted after timeout");
        }
        const QStringList folders = sessionFolders();
        const QString before = scanLog(w);
        click(w, "Capture Spectrum");
        check(oneLineAdded(w, before, "[Capture] Capture fault: restart after confirming Laser OFF.") && sessionFolders() == folders,
              "capture fault blocks the next single capture");
    }
#ifndef Q_OS_WIN
    // A session folder that cannot take session.json: the scan / single capture must not start.
    static void sessionFileFailure() {
        MainWindow w;
        setMode(w, "ok");
        w.laserOn = true;
        const QStringList folders = sessionFolders();
        for (const QString& button : {QString("Start Z scan then 2D scan"), QString("Capture Spectrum")}) {
            const QString before = scanLog(w);
            const mode_t previous = ::umask(0222);   // folders created now are not writable
            click(w, button);
            ::umask(previous);
            check(oneLineAdded(w, before, button == "Capture Spectrum" ? "[Capture] Cannot create session.json."
                                                                        : "[Scan] Cannot create session.json."), "session.json failure reported");
            check(!w.scanActive && !w.isCollecting && w.ramanProcess->state() == QProcess::NotRunning && captureCalls(w).isEmpty() &&
                  fieldsEditable(w), "nothing starts without session.json");
            check(sessionFolders() == folders, "no folder left behind");
        }
        click(w, "Start Z scan then 2D scan");
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "scan works again once the folder is writable");
    }
#endif
    static void writeFile(const QString& path, const QByteArray& content) {
        QFile file(path);
        check(file.open(QIODevice::WriteOnly | QIODevice::Truncate) && file.write(content) == content.size() && file.flush(), "write file");
    }
    static QByteArray darkRecordOf(bool backend) {
        return QJsonDocument(QJsonObject{{"schema", "raman-dark-description/1"}, {"simulated", QJsonObject{{"backend", backend}, {"motor", backend}}}}).toJson();
    }
    static void newestDarkFile() {
        QTemporaryDir folder(QCoreApplication::applicationDirPath() + "/dark_pick_XXXXXX");
        check(folder.isValid(), "temporary dark folder");
        const auto touch = [&](const QString& name, int ageSeconds) {
            QFile file(folder.path() + "/" + name);
            check(file.open(QIODevice::WriteOnly) && file.write("x") == 1 && file.flush(), "write file");
            check(file.setFileTime(QDateTime::currentDateTime().addSecs(-ageSeconds), QFileDevice::FileModificationTime), "set file time");
        };
        const auto record = [&](const QString& name, const QByteArray& content) { writeFile(darkRecordPath(folder.path() + "/" + name), content); };
        const auto real = [&] { return MainWindow::newestDarkFile(folder.path(), false); };
        const auto simulated = [&] { return MainWindow::newestDarkFile(folder.path(), true); };
        for (bool kind : {false, true}) {
            check(MainWindow::newestDarkFile(folder.path(), kind).isNull(), "no dark file: null");
            check(MainWindow::newestDarkFile(folder.path() + "/missing", kind).isNull(), "no dark folder: null");
        }
        touch("dark_reference.txt", 50); touch("dark_other.txt", 60); touch("notes.txt", 0); touch(".DS_Store", 0);
        check(real().toString() == "dark_reference.txt", "undated names: most recently modified dark_*.txt");
        check(simulated().isNull(), "a dark without a .json is real: a simulated session gets none");
        record("dark_other.txt", darkRecordOf(true));
        check(simulated().toString() == "dark_other.txt" && real().toString() == "dark_reference.txt", "undated names: by modification time, each kind for itself");
        // Dated names decide by their own time, as after a copy that gave every file one modification time.
        touch("dark_20260921_151124_857.txt", 10); touch("dark_20260924_141627_841.txt", 10); touch("dark_20260922_124418_305.txt", 10);
        check(real().toString() == "dark_20260924_141627_841.txt", "newest by the time in the name");
        check(simulated().toString() == "dark_other.txt", "no dated simulated dark: the newest undated one");
        touch("dark_20260921_151124_857.txt", 0);
        check(real().toString() == "dark_20260924_141627_841.txt", "a later modification time does not win");
        touch("dark_20260924_141627_841_1.txt", 30);
        check(real().toString() == "dark_20260924_141627_841_1.txt", "same-millisecond suffix is newer");
        record("dark_20260924_141627_841_1.txt", darkRecordOf(true));
        check(simulated().toString() == "dark_20260924_141627_841_1.txt" && real().toString() == "dark_20260924_141627_841.txt",
              "a later dark of the other kind is passed over");
        record("dark_20260924_141627_841.txt", darkRecordOf(false));
        check(real().toString() == "dark_20260924_141627_841.txt", "a .json that says real: real");
        record("dark_20260924_141627_841.txt", "{\"simulated\": {\"motor\": false}}");
        check(real().toString() == "dark_20260922_124418_305.txt" && simulated().toString() == "dark_20260924_141627_841_1.txt",
              "a dark of unknown kind is paired with neither kind");
        check(darkRecordPath("/home/dark_measurements/dark_20261113_101400_001_1.txt") == "/home/dark_measurements/dark_20261113_101400_001_1.json",
              "the .json of a dark: its name with the suffix, .json for .txt");
    }
    // Lead decision D1: what tells a dark's kind. Only a .json that Python's json module reads, holding
    // an object whose simulated.backend is true or false, gives one; a dark without a .json is real.
    static void darkKinds() {
        QTemporaryDir folder(QCoreApplication::applicationDirPath() + "/dark_kind_XXXXXX");
        check(folder.isValid(), "temporary dark folder");
        int next = 0;
        const auto kindOf = [&](const std::optional<QByteArray>& record) {
            const QString txt = folder.path() + QString("/dark_20261113_101400_%1.txt").arg(++next, 3, 10, QChar('0'));
            writeFile(txt, "x");
            if (record) writeFile(darkRecordPath(txt), *record);
            return darkKind(txt);
        };
        check(kindOf(std::nullopt) == DarkKind::Real, "no .json: real");
        check(kindOf(darkRecordOf(false)) == DarkKind::Real, "simulated.backend false: real");
        check(kindOf(darkRecordOf(true)) == DarkKind::Simulated, "simulated.backend true: simulated");
        check(kindOf("{\"simulated\": {\"backend\": true, \"motor\": false}}") == DarkKind::Simulated, "the backend decides, not the motor");
        const QList<QByteArray> unreadable{
            "", "not json", "[]", "{}", "{\"simulated\": true}", "{\"simulated\": {\"motor\": true}}",
            "{\"simulated\": {\"backend\": \"true\"}}", "{\"simulated\": {\"backend\": 1}}", "{\"simulated\": {\"backend\": null}}",
            "\xEF\xBB\xBF{\"simulated\": {\"backend\": true}}",                    // Python's json module refuses a byte order mark,
            "{\"simulated\": {\"backend\": true}, \"note\": \"a\tb\"}",            // a raw tab inside a string,
            "{\"simulated\": {\"backend\": true}, \"note\": \"\xff\"}",            // a file that is not UTF-8,
            "{\"simulated\": {\"backend\": true}, \"x\": 1.}", "{\"simulated\": {\"backend\": true}, \"x\": .5}",
            "{\"simulated\": {\"backend\": true}, \"x\": -.5}", "{\"simulated\": {\"backend\": true}, \"x\": 1.e5}",   // a point without a digit on one side,
            "{\"simulated\": {\"backend\": true}, \"note\": \"\\q\"}", "{\"simulated\": {\"backend\": true}, \"note\": \"\\x41\"}",
            "{\"simulated\": {\"backend\": true}, \"note\": \"\\'\"}",             // and an escape JSON does not have, all of which Qt takes
        };
        for (const QByteArray& record : unreadable) check(kindOf(record) == DarkKind::Unknown, "a .json without a readable simulated.backend: unknown");
        check(kindOf("{\"x\": [1.5e-3, -0.25, 10.0E+2], \"note\": \"a.b .5 \\\"\\\\q\\/\\b\\f\\n\\r\\t\\u00e9\", \"simulated\": {\"backend\": true}}")
                  == DarkKind::Simulated, "numbers with a point, points in a string and the escapes JSON has: read");
        check(kindOf("{\"simulated\": {\"backend\": true}, \"simulated\": {\"backend\": false}}") == DarkKind::Real, "the last key counts, as in Python");
        check(QDir(folder.path()).mkdir("dark_20261113_101700_000.json") && darkKind(folder.path() + "/dark_20261113_101700_000.txt") == DarkKind::Unknown,
              "a folder named like the .json: unknown");
#ifndef Q_OS_WIN
        // POSIX: on Windows the permissions do not keep the owner from reading, and QFile::link makes a shortcut.
        const QString locked = folder.path() + "/dark_20261113_101500_000.txt";
        writeFile(locked, "x"); writeFile(darkRecordPath(locked), darkRecordOf(true));
        check(QFile::setPermissions(darkRecordPath(locked), QFileDevice::Permissions()), "make the .json unreadable");
        const auto readable = qScopeGuard([&]{ QFile::setPermissions(darkRecordPath(locked), QFileDevice::ReadOwner | QFileDevice::WriteOwner); });
        check(darkKind(locked) == DarkKind::Unknown, "a .json that cannot be opened: unknown");
        const QString linked = folder.path() + "/dark_20261113_101600_000.txt";
        writeFile(linked, "x");
        check(QFile::link(folder.path() + "/nowhere.json", darkRecordPath(linked)), "a .json that is a link to nothing");
        check(darkKind(linked) == DarkKind::Unknown, "a .json that leads nowhere: unknown, not missing");
#endif
    }
    // The labels apply each helper program's own rule to the environment the program starts with.
    static void simulatedLabel() {
        QProcessEnvironment backend;
        check(!backendSimulates(backend), "backend: variable unset, not simulated");
        for (const char* other : {"0", "", "true", "yes", "2", " 1", "1 "}) {
            backend.insert("RAMAN_BACKEND_SIMULATE", other);
            check(!backendSimulates(backend), "backend: only exactly 1 simulates");
        }
        backend.insert("RAMAN_BACKEND_SIMULATE", "1");
        check(backendSimulates(backend) == kHelpersHaveSimulators, "backend: 1 simulates, where the backend has a simulator");

        QTemporaryDir motorDir(QCoreApplication::applicationDirPath() + "/motor_rule_XXXXXX");
        const auto configure = [&](const QByteArray& content, const QString& name) {
            QFile file(motorDir.path() + "/" + name);
            check(file.open(QIODevice::WriteOnly | QIODevice::Truncate) && file.write(content) == content.size(), "write a motor configuration");
        };
        QProcessEnvironment motor;
        check(!motorSimulates(motor, motorDir.path()), "motor: no variable, no configuration");
        for (const char* other : {"0", "", "true", "yes", "1.0"}) {
            motor.insert("RAMAN_MOTOR_SIMULATE", other);
            check(!motorSimulates(motor, motorDir.path()), "motor: a value other than 1 does not simulate");
        }
        for (const char* one : {"1", " 1", "1\n", "\t1 ", "\x1f" "1"}) {
            motor.insert("RAMAN_MOTOR_SIMULATE", one);
            check(motorSimulates(motor, motorDir.path()) == kHelpersHaveSimulators, "motor: 1 with blanks around it simulates (Python's strip)");
        }
        motor.insert("RAMAN_MOTOR_SIMULATE", "0");
        const QList<QPair<QByteArray, bool>> configurations{
            {"{\"transport\": \"simulate\"}", true}, {"{\"transport\": \" Simulate \"}", true}, {"{\"transport\": true}", true},
            {"{\"transport\": \"auto\"}", false}, {"{\"transport\": \"pyftdi\"}", false}, {"{\"transport\": false}", false},
            {"{\"transport\": null}", false}, {"{}", false}, {"not json", false}, {"[\"simulate\"]", false},
            {"{\"transport\": \"simulate\", \"transport\": \"auto\"}", false},     // the last one counts, as in Python
            {"\xEF\xBB\xBF{\"transport\": \"simulate\"}", false},                    // Python does not read a byte order mark,
            {"{\"_comment\": \"a\tb\", \"transport\": \"simulate\"}", false},        // a raw tab inside a string,
            {"{\"transport\": \"simulate\", \"note\": \"\xff\"}", false},             // a file that is not UTF-8,
            {"{\"transport\": \"simulate\", \"speed\": 1.}", false},                  // a point without a digit after it,
            {"{\"transport\": \"simulate\", \"note\": \"\\q\"}", false},             // or an escape JSON does not have
        };
        for (const auto& [content, simulates] : configurations) {
            configure(content, "motor_config.json");
            check(motorSimulates(motor, motorDir.path()) == (simulates && kHelpersHaveSimulators), "motor: transport of motor_config.json as Python reads it");
        }
        configure("{\"transport\": \"auto\"}", "motor_config.json");
        configure("{\"transport\": \"simulate\"}", "other.json");
        motor.insert("RAMAN_MOTOR_CONFIG", "other.json");
        check(motorSimulates(motor, motorDir.path()) == kHelpersHaveSimulators, "motor: RAMAN_MOTOR_CONFIG relative to the motor folder");
        motor.insert("RAMAN_MOTOR_CONFIG", motorDir.path() + "/motor_config.json");
        check(!motorSimulates(motor, motorDir.path()), "motor: RAMAN_MOTOR_CONFIG as an absolute path");
        // This program starts the emulators with both variables at 1 (main below), so its captures say simulated.
        MainWindow w;
        check(w.captureMetadata()["simulated"].toObject() == simulatedHere(), "this test run is labelled simulated");
    }
    // The header badge where the helper programs have no simulator (a Windows build): never SIMULATION,
    // and a warning when a simulation variable is set anyway. Elsewhere it is window_status.h's badge.
    static void simulationHeader() {
        check(simulationHeaderBadge(true).text == simulationBadge(simulationUse("RAMAN_BACKEND_SIMULATE"), simulationUse("RAMAN_MOTOR_SIMULATE")).text,
              "with simulators: the badge of window_status.h");
        const PillState warning = simulationHeaderBadge(false);
        check(warning.text == "NO SIMULATOR ON WINDOWS: instruments are real" && warning.tone == UiTone::Danger && warning.prominent &&
              warning.tip.contains("driven"), "both variables set, no simulator: the instruments are real");
        {
            const auto restore = qScopeGuard([]{ qputenv("RAMAN_BACKEND_SIMULATE", "1"); qputenv("RAMAN_MOTOR_SIMULATE", "1"); });
            qunsetenv("RAMAN_BACKEND_SIMULATE"); qunsetenv("RAMAN_MOTOR_SIMULATE");
            check(simulationHeaderBadge(false).text.isEmpty(), "no variable, no simulator: no badge, as for any real run");
            qputenv("RAMAN_MOTOR_SIMULATE", "0");
            check(simulationHeaderBadge(false).text == warning.text, "any variable set: the warning");
        }
        check(qgetenv("RAMAN_BACKEND_SIMULATE") == "1" && qgetenv("RAMAN_MOTOR_SIMULATE") == "1", "both variables back at 1");
        MainWindow w;
        check(w.display.simulation->value() == simulationHeaderBadge(kHelpersHaveSimulators).text &&
              w.windowTitle().endsWith("(SIMULATION)") == kHelpersHaveSimulators, "the header and the title of this build");
    }
    static void nothingRemembered() {
        check(!MainWindow::remembersOperatorFields(), "the test build has no settings store");
        {
            MainWindow w;
            w.sampleIdEdit->setText("other sample"); w.operatorEdit->setText("other operator");
            w.locationEdit->setText("other place"); w.notesEdit->setText("other notes");
        }
        MainWindow w;
        check(w.sampleIdEdit->text() == "TEST-SAMPLE" && w.operatorEdit->text() == "handshake tests" &&
              w.locationEdit->text() == "test bench" && w.notesEdit->text().isEmpty(), "every test window starts with the test values");
    }
    // This test folder has no data_layer/raman_catalog (the tests of the Data and Review tabs run
    // from ui_home/, see ui_tests.cpp): the tabs say so, and no data-layer process is ever started.
    static void dataLayerNotInstalled() {
        check(!QFileInfo::exists(QCoreApplication::applicationDirPath() + "/data_layer/raman_catalog"), "this folder has no data layer");
        MainWindow w;
        w.show();
        spin(500);
        DataPanels& panels = *w.dataPanels;
        check(!panels.installed() && !panels.started() && panels.dataLayer->running() == 0, "not installed: nothing is started when the window is shown");
        for (QWidget* page : {panels.dataPage(), panels.reviewPage()}) {
            const QLabel* title = page->findChild<QLabel*>("emptyTitle");
            check(title && title->text() == "The data layer is not installed" && page->findChildren<QPushButton*>().isEmpty() &&
                  page->findChildren<QTableWidget*>().isEmpty(), "both tabs say that the data layer is not installed, and offer nothing");
        }
        check(w.display.tabs->widget(1) == panels.dataPage() && w.display.tabs->widget(2) == panels.reviewPage() &&
              w.display.tabs->tabText(2) == "Review" && w.display.data->value() == "not installed", "the tabs and the header");
        QPushButton* studio = nullptr;
        for (auto* b : w.findChildren<QPushButton*>()) if (b->text() == "Open Simulation Studio") studio = b;
        check(studio && !studio->isEnabled() && studio->toolTip().contains("not installed"), "the Studio button is off: the Studio is part of the data layer");
        bool refused = false;
        panels.openStudio([&refused](bool ok, const QString& why) { refused = !ok && why.contains("not installed"); });
        check(refused, "and asking for it anyway starts nothing");
        start(w);
        check(waitUntil([&]{return w.lastCompletedPoint == 3 && !w.scanActive;}), "the scan works as ever");
        spin(500);
        check(!panels.started() && panels.dataLayer->running() == 0 && panels.dataLayer->findChildren<QProcess*>().isEmpty(),
              "no data-layer process after a scan either");
        check(w.display.scan->value().startsWith("idle") && w.display.lastEvent->text().startsWith("Scan finished"), "the status display runs without the data layer");
    }
    static void originalControllerLock() {
        QTemporaryDir project(QCoreApplication::applicationDirPath() + "/lock_layout_XXXXXX");
        check(project.isValid(), "temporary project layout");
        const QString original = project.path() + "/deploy-macos";                      // the original controller's folder
        const QString home = project.path() + "/Portable_Raman_Catalog/deploy-macos";   // this edition's folder
        check(QDir().mkpath(original) && QDir().mkpath(home), "project layout");
        {
            OriginalControllerLock lock;
            check(lock.acquire(home) && !QFile::exists(original + "/controller.lock"), "a deploy-macos without helper programs is not locked");
        }
        check(QDir().mkpath(original + "/handshake_v2"), "original deploy folder");
        {
            // main() names the application; the lock must hold whatever the name is.
            QCoreApplication::setApplicationName(kSoftwareName);
            const auto restore = qScopeGuard([]{ QCoreApplication::setApplicationName(QString()); });
            OriginalControllerLock lock;
            check(lock.acquire(home) && QFile::exists(original + "/controller.lock"), "the edition holds the original controller's lock");
            QLockFile originalController(original + "/controller.lock");   // what the original's main() does
            check(!originalController.tryLock(100), "the original controller cannot start while the edition runs");
            originalController.setStaleLockTime(1);
            spin(30);
            check(!originalController.tryLock(100) && QFile::exists(original + "/controller.lock"), "a held lock is not taken over as stale");
            OriginalControllerLock second;
            check(!second.acquire(home), "nor can a second holder take it");
        }
        check(!QFile::exists(original + "/controller.lock"), "the lock is released when the edition closes");
        {
            QLockFile originalController(original + "/controller.lock");
            check(originalController.tryLock(100), "the original controller starts");
            OriginalControllerLock lock;
            check(!lock.acquire(home), "the edition does not start while the original controller runs");
            originalController.unlock();
            check(lock.acquire(home), "and starts once the original controller has closed");
        }
        OriginalControllerLock standalone;
        check(standalone.acquire(project.path()) && QDir(project.path()).entryList({"controller.lock"}, QDir::Files).isEmpty(),
              "outside the original project there is nothing to lock");
#ifndef Q_OS_WIN
        {
            // A folder in which the lock file cannot be created (POSIX permissions): refused, and not
            // taken for a running original.
            check(QFile::setPermissions(original, QFileDevice::ReadOwner | QFileDevice::ExeOwner), "the original's folder made read-only");
            const auto writable = qScopeGuard([&]{ QFile::setPermissions(original, QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner); });
            OriginalControllerLock lock;
            const QString file = QFileInfo(original).canonicalFilePath() + "/controller.lock";
            check(!lock.acquire(home) && lock.error() == QLockFile::PermissionError && lock.filePath() == file && !QFile::exists(file),
                  "a lock file that cannot be created: not acquired, with the reason");
            const LockRefusal refusal = lockRefusal(true, lock.error(), lock.filePath());
            check(refusal.title == "Original controller lock not available" &&
                  refusal.text == "Cannot create " + file + " (no permission to create it): this edition cannot keep the original controller closed while it runs.",
                  "the refusal names the file and the reason, not a running original");
        }
#endif
        const LockRefusal held = lockRefusal(true, QLockFile::LockFailedError, original + "/controller.lock");
        check(held.title == "Original controller running" && held.text.startsWith("The original Raman controller is running"), "a held lock: the original runs");
        const LockRefusal own = lockRefusal(false, QLockFile::LockFailedError, home + "/controller.lock");
        check(own.title == "Controller already running" && own.text == "Close the other Raman controller before starting this one.",
              "this controller's own lock held: the message of the original program");
        const LockRefusal ownUnwritable = lockRefusal(false, QLockFile::UnknownError, home + "/controller.lock");
        check(ownUnwritable.title == "Controller lock not available" &&
              ownUnwritable.text.startsWith("Cannot create " + home + "/controller.lock (it could not be written): "), "this controller's own lock file not writable");
    }

    // ---- Darks (lead decision D1): every dark has its kind, and is paired with spectra of that kind only ----
    // Capture Dark: dark_<stamp>.txt exactly as the controller has always written it, and beside it
    // dark_<stamp>.json with the kind of the dark.
    static void darkRecord() {
        MainWindow w;
        check(waitUntil([&]{ return w.instrumentInfo.isObject(); }), "backend report lines read");
        setMode(w, "ok");
        const QStringList before = QDir(darkFolder()).entryList({"dark_*"}, QDir::Files);
        const QDateTime clicked = nowUtcMs();
        click(w, "Capture Dark");
        QStringList added = QDir(darkFolder()).entryList({"dark_*"}, QDir::Files, QDir::Name);
        for (const QString& name : before) added.removeAll(name);
        check(added.size() == 2 && added[1].endsWith(".txt") && added[0] == added[1].chopped(4) + ".json", "one dark: its .txt and its .json");
        const QString txt = darkFolder() + "/" + added[1];
        check(QRegularExpression("^dark_\\d{8}_\\d{6}_\\d{3}\\.txt$").match(added[1]).hasMatch(), "dark_<stamp>.txt");
        check(scanLog(w).endsWith("[Dark] Saved: " + txt) && captureCalls(w).size() == 1, "one capture, saved and logged as before");
        // The .txt byte for byte, built here independently of the controller's writer: other tools read it.
        const QByteArray bytes = readFile(txt);
        const QByteArray stamp = bytes.split('\n').value(1).mid(13);   // after "# Timestamp: "
        check(QRegularExpression("^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}\\.\\d{3}$").match(QString::fromUtf8(stamp)).hasMatch(), "local time stamp line");
        QByteArray expected = "# Dark Measurement\n# Timestamp: " + stamp + "\n# Points: 2048\nindex\traman_shift_cm-1\tintensity\n";
        for (int i = 0; i < 2048; ++i) expected += QByteArray::number(i) + '\t' + QByteArray::number(kRamanShift[i], 'f', 8) + "\t16\n";
        check(bytes == expected, "the .txt byte for byte as before");
        const QJsonObject record = json(darkFolder() + "/" + added[0]);
        check(record.keys() == QStringList({"captured_utc", "dark_file", "instrument", "schema", "simulated", "software"}), "the keys of the .json");
        check(record["schema"].toString() == "raman-dark-description/1" && record["dark_file"].toString() == added[1], "its schema and its .txt");
        check(utcStamp(record["captured_utc"], clicked), "captured_utc: UTC with milliseconds");
        const QJsonObject session = w.captureMetadata();
        check(record["simulated"].toObject() == simulatedHere() && record["simulated"] == session["simulated"], "simulated by the rule of session.json");
        check(record["instrument"].toObject() == emulatedInstrument() && record["instrument"] == session["instrument"], "the instrument session.json gets");
        check(record["software"].toObject() == QJsonObject{{"name", "portable-raman-controller-catalog"}, {"version", "1.0.0"}}, "software");
        check(darkKind(txt) == DarkKind::Simulated && session["dark_file"].toString() == added[1], "a simulated dark: the next simulated scan is paired with it");
    }
    // A dark whose .json cannot be written is not saved: its .txt is removed, and the dark is reported
    // as not saved, as for any failed save. Every name the .json can get while the click runs is
    // taken by a folder of that name.
    static void darkRecordFailure() {
        MainWindow w;
        setMode(w, "ok");
        check(QDir().mkpath(darkFolder()), "dark folder");
        const QStringList before = QDir(darkFolder()).entryList(QDir::Files);
        const int windowMs = 4000;
        const QDateTime start = QDateTime::currentDateTime();
        QStringList blocked;
        for (int ms = 0; ms < windowMs; ++ms) blocked << "dark_" + start.addMSecs(ms).toString("yyyyMMdd_HHmmss_zzz") + ".json";
        const auto unblock = qScopeGuard([&]{ for (const QString& name : blocked) QDir(darkFolder()).rmdir(name); });
        for (const QString& name : blocked) check(QDir(darkFolder()).mkdir(name), "block a name of the .json");
        click(w, "Capture Dark");
        check(QDateTime::currentDateTime() < start.addMSecs(windowMs), "the dark was saved while every name of its .json was blocked");
        check(scanLog(w).endsWith("[Dark] Acquiring a NEW spectrum. Ensure the optical path is blocked / covered.\n[Dark Error] Save failed."),
              "reported as not saved, as any failed save");
        check(QDir(darkFolder()).entryList(QDir::Files) == before, "no .txt is left without its .json");
        check(captureCalls(w).size() == 1 && !w.isCollecting && !w.backendCaptureFault, "one capture; the capture itself did not fail");
    }
    // A session is paired with the newest dark of its own kind, never with one of the other kind or of
    // unknown kind, however much later it is dated. This program's backend simulates; a session with a
    // real backend is the same window with RAMAN_BACKEND_SIMULATE taken out of the environment its
    // label is read from (the running emulator is not restarted, and no file is written for it).
    static void darkPairing() {
        MainWindow w;
        confirmLaserOn(w);
        setMode(w, "ok");
        const QString simulated = newestDark(w);
        QStringList made;
        const auto removeMade = qScopeGuard([&]{ for (const QString& path : made) QFile::remove(path); });
        const auto later = [&](int last, const std::optional<QByteArray>& record) {
            const QString name = QString("dark_29991231_235959_%1.txt").arg(last);
            writeFile(darkFolder() + "/" + name, "x"); made << darkFolder() + "/" + name;
            if (record) { writeFile(darkRecordPath(darkFolder() + "/" + name), *record); made << darkRecordPath(darkFolder() + "/" + name); }
            return name;
        };
        const QString realWithout = later(900, std::nullopt);
        const QString realWith = later(901, darkRecordOf(false));
        for (int last = 910; last < 914; ++last) later(last, last % 2 ? QByteArray("not json") : QByteArray("{\"simulated\": {\"motor\": true}}"));
        check(w.captureMetadata()["dark_file"].toString() == simulated, "a simulated session: the newest simulated dark, not a later real or unknown one");
        click(w, "Start Z scan then 2D scan");
        check(json(w.sessionDir + "/session.json")["dark_file"].toString() == simulated, "session.json of a simulated scan");
        check(waitUntil([&]{ return w.lastCompletedPoint == 3 && !w.scanActive; }), "3 points must complete");
        check(point(w.sessionDir, 3)["capture"].toObject()["dark_file"].toString() == simulated, "and every point of it");
        click(w, "Capture Spectrum");
        check(json(w.singleCaptureDir + "/session.json")["dark_file"].toString() == simulated, "session.json of a simulated single capture");

        QProcessEnvironment realBackend = QProcessEnvironment::systemEnvironment();
        realBackend.remove("RAMAN_BACKEND_SIMULATE");
        w.laserProcess->setProcessEnvironment(realBackend);
        const auto realSession = [&w] {
            const QJsonObject capture = w.captureMetadata();
            check(capture["simulated"].toObject()["backend"] == false, "a session with a real backend");
            return capture["dark_file"];
        };
        // The Capture group names the dark of the backend's kind, as session.json does.
        const auto captionNames = [&w](const QString& dark) {
            w.refreshStatusDisplay();
            return w.display.nextDark->text() == nextDarkCaption(dark, false, QDate::currentDate()).text &&
                   w.display.nextDark->tone() == UiTone::Neutral && w.display.nextDark->toolTip().contains(dark);
        };
        check(realSession().toString() == realWith, "a real session: the newest real dark, whose .json says real");
        check(captionNames(realWith), "the Capture group of a real session names its real dark");
        QFile::remove(darkRecordPath(darkFolder() + "/" + realWith)); QFile::remove(darkFolder() + "/" + realWith);
        check(realSession().toString() == realWithout && captionNames(realWithout), "a dark without a .json is real");
        const QString simulatedLater = later(999, darkRecordOf(true));
        check(realSession().toString() == realWithout && captionNames(realWithout), "a real session never names a simulated dark, however late");
        w.laserProcess->setProcessEnvironment(QProcessEnvironment::systemEnvironment());
        check(w.captureMetadata()["dark_file"].toString() == simulatedLater, "and the simulated session takes the later simulated one");
    }

    // ---- Display only: the next step and the dark of the next scan (main.cpp refreshStatusDisplay) ----
    static void nextDarkCaptions() {
        const QDate day(2026, 11, 13);
        PillState caption = nextDarkCaption(QJsonValue(), true, day);
        check(caption.text == "no simulated dark yet" && caption.tone == UiTone::Warn && caption.tip.contains("Capture Dark"), "no dark: a warning that says what to do");
        check(nextDarkCaption(QJsonValue(), false, day).text == "no real dark yet", "the kind of the backend");
        caption = nextDarkCaption("dark_20261113_220920_781.txt", true, day);
        check(caption.text == "simulated dark of 22:09:20" && caption.tone == UiTone::Neutral &&
              caption.tip.contains("dark_measurements/dark_20261113_220920_781.txt"), "a dark of today: its time; the file in the tool tip");
        check(nextDarkCaption("dark_20261112_080500_002_1.txt", false, day).text == "real dark of 12 Nov 08:05", "an older dark: its day and time");
        check(nextDarkCaption("dark_reference.txt", false, day).text == "real dark dark_reference.txt", "a name without a time: the name");
    }
    // The brightest button is the next step, and the Capture group says which dark the next scan is
    // paired with. The dark folder is set aside for this test, so that the window starts without darks.
    static void nextStep() {
        const QString aside = darkFolder() + "_aside";
        check(!QFileInfo::exists(aside), "dark_measurements_aside, left by an interrupted run: move it back to dark_measurements by hand");
        check(!QFileInfo::exists(darkFolder()) || QDir().rename(darkFolder(), aside), "dark folder set aside");
        const auto restore = qScopeGuard([&]{ QDir(darkFolder()).removeRecursively(); if (QFileInfo::exists(aside)) QDir().rename(aside, darkFolder()); });
        MainWindow w;
        setMode(w, "ok");
        const auto scansPrimary = [&w] { return primary(w.display.zScanButton) && primary(w.display.lineScanButton) && primary(w.display.captureButton); };
        const auto scansPlain = [&w] { return !primary(w.display.zScanButton) && !primary(w.display.lineScanButton) && !primary(w.display.captureButton); };
        // A session with a real backend: the same window with RAMAN_BACKEND_SIMULATE taken out of the
        // environment its labels are read from (darkPairing); the running emulator is not restarted.
        const QProcessEnvironment simulatedBackend = w.laserProcess->processEnvironment();
        const auto withRealBackend = [&w, &simulatedBackend](const std::function<void()>& checks) {
            QProcessEnvironment realBackend = simulatedBackend;
            realBackend.remove("RAMAN_BACKEND_SIMULATE");
            w.laserProcess->setProcessEnvironment(realBackend);
            w.refreshStatusDisplay();
            checks();
            w.laserProcess->setProcessEnvironment(simulatedBackend);
            w.refreshStatusDisplay();
        };
        w.refreshStatusDisplay();
        check(primary(w.display.laserButton) && !primary(w.display.darkButton) && scansPlain(), "laser off: Laser ON is the next step");
        check(w.display.nextDark->text() == "no simulated dark yet" && w.display.nextDark->tone() == UiTone::Warn, "no dark: said as a warning");
        confirmLaserOn(w);
        withRealBackend([&w, &scansPlain] {
            check(w.display.nextDark->text() == "no real dark yet" && w.display.nextDark->tone() == UiTone::Warn &&
                  primary(w.display.darkButton) && scansPlain(), "a real session without a real dark: said, and Capture Dark is the next step");
        });
        QDir().mkpath(darkFolder());
        const QString real = "dark_29991231_235959_999.txt";
        writeFile(darkFolder() + "/" + real, "x");   // real: no .json
        withRealBackend([&w, &real, &scansPrimary] {
            check(w.display.nextDark->text() == nextDarkCaption(real, false, QDate::currentDate()).text && w.display.nextDark->tone() == UiTone::Neutral &&
                  !primary(w.display.darkButton) && scansPrimary(), "a real session with a real dark: named, and the scans are the next step");
        });
        check(!primary(w.display.laserButton) && primary(w.display.darkButton) && scansPlain(), "laser on, no dark of its kind: Capture Dark");
        check(w.display.nextDark->text() == "no simulated dark yet", "a real dark does not count for a simulated scan");
        click(w, "Capture Dark");
        w.refreshStatusDisplay();
        const QString taken = w.darkForNextCapture().toString();
        check(!taken.isEmpty() && w.display.nextDark->text() == nextDarkCaption(taken, true, QDate::currentDate()).text &&
              w.display.nextDark->tone() == UiTone::Neutral && w.display.nextDark->toolTip().contains(taken), "the dark just taken is named");
        check(!primary(w.display.darkButton) && scansPrimary(), "with a dark: the scans and Capture Spectrum");
        w.sampleIdEdit->clear();
        w.refreshStatusDisplay();
        check(scansPlain(), "a required field empty: no scan is the next step");
        w.sampleIdEdit->setText("TEST-SAMPLE");
        click(w, "Start Z scan then 2D scan");
        w.refreshStatusDisplay();
        check(w.scanActive && scansPlain() && !primary(w.display.darkButton) && !primary(w.display.laserButton), "a scan runs: nothing else is the next step");
        check(waitUntil([&]{ return w.lastCompletedPoint == 3 && !w.scanActive; }), "3 points must complete");
        check(w.rtChart->title() == "2d - saved point 3 (LIVE)", "LIVE while the scan runs");
        w.refreshStatusDisplay();
        check(w.rtChart->title() == "2d - last saved point 3 (scan finished)" && scansPrimary(), "the ended scan is not shown as live");
        w.laserStateUncertain = true;
        w.refreshStatusDisplay();
        check(scansPlain() && !primary(w.display.darkButton) && !primary(w.display.laserButton), "laser state unconfirmed: nothing is the next step");
        w.laserStateUncertain = false;
        start(w, "fail");
        check(waitUntil([&]{ return !w.scanActive && !w.isCollecting; }), "failure must stop scan");
        w.refreshStatusDisplay();
        check(w.rtChart->title() == "2d scan failed - no point saved; the curve shown is not from this scan", "a scan without a point says so");
        check(scansPlain() && !primary(w.display.darkButton), "capture fault: no scan is the next step");
    }
};
static void protocolTests() {
    const auto positions = lineScanPositions();
    check(positions.size() == 13 && positions.front() == 0 && positions.back() == 832800, "0..24 mm inclusive");
    check(lineScanWithinLimits(positions, 0, 867500), "25 mm stage accepts plan");
    check(!lineScanWithinLimits(positions, 0, 694000), "20 mm stage rejects plan");
    check(!lineScanWithinLimits(positions, 0, 0), "unknown limits reject plan");
    using D = CaptureStatusDecision;
    check(captureStatusDecision(1, 0, -1, 5, 1250, 8000) == D::Wait, "rc1 means keep waiting for this status API");
    check(captureStatusDecision(1, 1, 0, 5, 1500, 8000) == D::Wait, "rc1 never accepts stale outputs");
    check(captureStatusDecision(0, 1, 0, 5, 2000, 8000) == D::Ready, "ready after pending polls");
    check(captureStatusDecision(1, 0, -1, 5, 8000, 8000) == D::Timeout, "persistent pending times out");
    check(captureStatusDecision(0, 1, 0, 5, 8001, 8000) == D::Timeout, "late success rejected");
    check(captureStatusDecision(3, 0, -1, 5, 1300, 8000) == D::Error, "access error must not retry");
    check(captureStatusDecision(41, 0, -1, 5, 1300, 8000) == D::Error, "capture error must not retry");
    check(captureStatusDecision(0, 6, 0, 5, 1300, 8000) == D::Error, "invalid metadata not success");
    check(firstCaptureStatusDelayMs(1000000, 1100000) == 1250, "restore known-working initial wait");
    check(firstCaptureStatusDelayMs(2000000, 2100000) == 2250, "wait scales with longer exposure/cycle");
    check(firstCaptureStatusDelayMs(2500001, 1100000) == 2651, "round exposure up to full milliseconds");
    check(firstAvailableCaptureIndex(1, 0, 5) == 0, "first complete frame");
    check(firstAvailableCaptureIndex(2, 1, 5) == 0, "read first frame when multiple ready");
    check(firstAvailableCaptureIndex(5, 4, 5) == 0, "full legacy buffer");
    check(firstAvailableCaptureIndex(3, 0, 5) == 3, "wrapped buffer index");
    check(firstAvailableCaptureIndex(0, 0, 5) == -1, "reject no frame");
    check(firstAvailableCaptureIndex(6, 0, 5) == -1, "reject invalid frame count");
    using namespace AcquisitionProtocol;
    Point point;
    check(parsePoint("STEP run 1 2 3 4", "run", point), "valid point");
    check(!parsePoint("STEP old 1 2 3 4", "run", point), "wrong session");
    check(!parsePoint("STEP run 1 2 3", "run", point), "partial point");
    QVector<int> values; QString error;
    check(parseSpectrum("CAPTURE c OK 2\n0001 FFFF\nEND c", "c", 2, values, error), "valid spectrum");
    check(!parseSpectrum("CAPTURE c OK 2\n0001\nEND c", "c", 2, values, error), "partial spectrum");
    check(!parseSpectrum("CAPTURE c OK 2\n0001 ERROR\nEND c", "c", 2, values, error), "nonhex spectrum");
    check(!parseSpectrum("CAPTURE old OK 2\n0001 0002\nEND old", "c", 2, values, error), "old spectrum");
    QTemporaryDir tmp;
    check(!publish(tmp.path() + "/missing/path.txt", "x"), "write failure detected");
    const auto dir = tmp.path().toStdString();
    check(ScanHandshake::publish(dir + "/response.txt", "ACK old 1 OK 1"), "publish stale ack");
    check(!ScanHandshake::waitForAck(dir, "run", 1, 100), "stale session must not advance");
    ScanHandshake::publish(dir + "/response.txt", "ACK run 2 OK 2");
    check(!ScanHandshake::waitForAck(dir, "run", 1, 100), "wrong point must not advance");
    ScanHandshake::publish(dir + "/response.txt", "ACK run 1 PAUSED 3");
    check(!ScanHandshake::waitForAck(dir, "run", 1, 100), "frozen heartbeat must time out");
    ScanHandshake::publish(dir + "/response.txt", "ACK run 1 FAIL 4");
    check(!ScanHandshake::waitForAck(dir, "run", 1, 100), "explicit failure");
    ScanHandshake::publish(dir + "/response.txt", "ACK run 1 OK 5");
    check(ScanHandshake::waitForAck(dir, "run", 1, 100), "matching saved ack advances");
    ScanHandshake::publish(dir + "/cancel.txt", "STOP");
    check(!ScanHandshake::waitForAck(dir, "run", 1, 100), "cancel wins over saved ack");
}
int main(int argc, char** argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    // Only the emulators next to this program are started. Setting both variables keeps the helper
    // programs simulated even if real ones were ever placed there, and fixes the "simulated" labels
    // the metadata tests expect.
    qputenv("RAMAN_BACKEND_SIMULATE", "1");
    qputenv("RAMAN_MOTOR_SIMULATE", "1");
    QApplication app(argc, argv);
    try {
        check(ramanHome() == QCoreApplication::applicationDirPath(), "must run next to the emulators (handshake_v2/ in this folder)");
        protocolTests(); std::cout << "PASS protocol validation / stale ACK / cancellation / heartbeat\n";
        HandshakeTest::darkStorage(); std::cout << "PASS dark subfolder / unique names / complete TXT\n";
        HandshakeTest::monitor(); std::cout << "PASS live preview / no files / mode exclusion / return to scan\n";
        HandshakeTest::monitor(true); std::cout << "PASS preview failure stops loop\n";
        HandshakeTest::success(); std::cout << "PASS 3-point integration / duplicate request\n";
        HandshakeTest::success("locked_short"); std::cout << "PASS transient file lock / same capture command\n";
        HandshakeTest::success("locked_two"); std::cout << "PASS two failed reads / third read recovers\n";
        HandshakeTest::failure("locked_long"); std::cout << "PASS persistent file lock / stop without advancing\n";
        HandshakeTest::lineScan("1d-x"); std::cout << "PASS 1D X / 13 saved points / live display\n";
        HandshakeTest::lineScan("1d-y"); std::cout << "PASS 1D Y / 13 saved points / live display\n";
        HandshakeTest::success("not_ready_then_ready"); std::cout << "PASS repeated rc1 then ready / one capture command per point\n";
        HandshakeTest::pause(); std::cout << "PASS pause in exposure / resume / retained log\n";
        for (auto mode : {"fail", "partial", "wrong_id", "late", "not_ready_timeout"}) {
            HandshakeTest::failure(mode); std::cout << "PASS failure scenario: " << mode << '\n';
        }
        HandshakeTest::stop(); std::cout << "PASS stop during exposure\n";
        HandshakeTest::darkIsolation(); std::cout << "PASS dark/scan mutual exclusion\n";
        HandshakeTest::simulatedLabel(); std::cout << "PASS simulated label / each helper program's own rule\n";
        HandshakeTest::simulationHeader(); std::cout << "PASS header badge / never SIMULATION without simulators (Windows)\n";
        HandshakeTest::newestDarkFile(); std::cout << "PASS newest dark file of a kind / time in the name / null without one\n";
        HandshakeTest::darkKinds(); std::cout << "PASS dark kind / no .json is real / a .json that cannot be read is unknown\n";
        HandshakeTest::darkRecord(); std::cout << "PASS Capture Dark / .txt byte for byte / .json beside it\n";
        HandshakeTest::darkRecordFailure(); std::cout << "PASS .json cannot be written / dark not saved / no .txt left\n";
        HandshakeTest::darkPairing(); std::cout << "PASS simulated session never names a real dark / real never a simulated one\n";
        HandshakeTest::nextDarkCaptions(); std::cout << "PASS dark of the next scan / kind and time / warning without one\n";
        HandshakeTest::nextStep(); std::cout << "PASS next step / brightest button / Capture group names the dark / chart title after a scan\n";
        HandshakeTest::nothingRemembered(); std::cout << "PASS test build / test values / nothing remembered\n";
        HandshakeTest::originalControllerLock(); std::cout << "PASS original controller lock / both directions / not stale / file cannot be created\n";
        HandshakeTest::backendReport(); std::cout << "PASS backend report lines / parsed / still logged\n";
        HandshakeTest::backendReportMissing("absent"); std::cout << "PASS backend without report lines / null values\n";
        HandshakeTest::backendReportMissing("unavailable"); std::cout << "PASS module information unavailable / null instrument\n";
        HandshakeTest::sessionFile(); std::cout << "PASS session.json content / capture block in every point\n";
        HandshakeTest::wholeCharacters(); std::cout << "PASS fields keep whole characters / no half emoji in session.json or a point\n";
        HandshakeTest::lineScanSession(); std::cout << "PASS 1D session.json / capture block in 13 points\n";
        HandshakeTest::requiredFields(); std::cout << "PASS required fields guard / nothing happens\n";
        HandshakeTest::fieldLock(); std::cout << "PASS fields read-only during a scan / values taken at the start\n";
        HandshakeTest::powerInForce(); std::cout << "PASS power level in force / Power box moved afterwards\n";
        HandshakeTest::singleCapture(); std::cout << "PASS single capture / one point / null position / no motor\n";
        HandshakeTest::singleCaptureGuards(); std::cout << "PASS single capture guards\n";
        for (auto mode : {"fail", "partial", "wrong_id", "late"}) {
            HandshakeTest::singleCaptureFailure(mode); std::cout << "PASS single capture failure: " << mode << '\n';
        }
#ifndef Q_OS_WIN
        HandshakeTest::sessionFileFailure(); std::cout << "PASS session.json cannot be written / nothing starts\n";
#endif
        HandshakeTest::dataLayerNotInstalled(); std::cout << "PASS data layer not installed / tabs say so / nothing started\n";
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "FAIL " << ex.what() << std::endl; return 1;
    }
}
