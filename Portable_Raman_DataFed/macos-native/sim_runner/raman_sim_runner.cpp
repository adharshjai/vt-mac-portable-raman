// raman_sim_runner: the controller of laser_test/main.cpp without its window, for the Simulation
// Studio (data_layer/PROTOCOL.md section 7) and for runs by hand from a deploy folder:
//
//   raman_sim_runner --mode 1d-x|1d-y|2d|single --sample-id S --operator O --location L
//                    --power LOW|MEDIUM|HIGH [--keep-stage]
//
// One run is Laser ON, Capture Dark, the scan or the single capture, Laser OFF. Each step is a click
// on the window's own control, so it takes the controller's code path with its production timeouts.
// Both helper programs always run their simulators: this program cannot reach an instrument.
// Exit codes: 0 done, 1 a step failed, 2 wrong arguments, 3 the controller lock of this folder is
// held, 128 + N stopped by signal N (SIGTERM, SIGINT or SIGHUP) once the laser was switched off.
#define main controller_main          // the window program's main() is not part of this one
#include "../../laser_test/main.cpp"
#undef main
#include <QCommandLineParser>
#include <QLoggingCategory>
#include <csignal>
#include <cstdio>
#include <functional>

namespace {

// The waits of a run look at this flag; a signal handler may do no more than set it.
volatile std::sig_atomic_t stopSignal = 0;
void noteStop(int number) { stopSignal = number; }

const char* signalName(int number) {
    return number == SIGTERM ? "SIGTERM" : number == SIGINT ? "SIGINT" : number == SIGHUP ? "SIGHUP" : "a signal";
}

// One line per step on stdout, flushed at once: the Studio shows the lines while the run goes on.
void say(const QString& text) {
    const QByteArray line = ("[runner] " + text + "\n").toUtf8();
    std::fwrite(line.constData(), 1, size_t(line.size()), stdout);
    std::fflush(stdout);
}

struct Request {
    QString mode, sampleId, operatorName, location, power;
    bool keepStage = false;
};

const QStringList kModes{"1d-x", "1d-y", "2d", "single"};
const QStringList kPowers{"LOW", "MEDIUM", "HIGH"};   // the order of the controller's Power box

QString fileText(const QString& path) {
    QFile file(path);
    return file.open(QIODevice::ReadOnly) ? QString::fromUtf8(file.readAll()).trimmed() : QString();
}

}  // namespace

struct SimulatedRunner {   // friend of MainWindow (laser_test/main.cpp)
    MainWindow& w;
    const Request& request;

    static bool stopping() { return stopSignal != 0; }

    // Lets the controller work until `done` holds; false on a stop request or after the timeout.
    static bool waitFor(const std::function<bool()>& done, int timeoutMs) {
        QElapsedTimer elapsed; elapsed.start();
        while (!done()) {
            if (stopping() || elapsed.elapsed() > timeoutMs) return false;
            QCoreApplication::processEvents(QEventLoop::AllEvents, 10);
            QThread::msleep(10);
        }
        return true;
    }

    bool click(const QString& text) {
        for (QPushButton* candidate : w.findChildren<QPushButton*>())
            if (candidate->text() == text) { candidate->click(); return true; }
        say("The controller has no button \"" + text + "\"");
        return false;
    }

    // What the controller said last in one of its logs: the reason for a refused or failed step.
    static QString lastLine(const QTextEdit* log) {
        static const QRegularExpression stamp("^\\[\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d:\\d\\d\\.\\d{3}\\] ");
        return log->document()->lastBlock().text().trimmed().remove(stamp);
    }

    // The capture backend has initialised its device once it reports it (data_layer/PROTOCOL.md
    // section 3). Only the simulated device is accepted; with any other nothing is switched on.
    bool backendReady() {
        const auto reported = [this] {
            return w.instrumentInfo.isObject() || w.laserProcess->state() != QProcess::Running ||
                   !w.laserOutput->document()->find("MODULE_INFO unavailable").isNull();
        };
        if (!waitFor(reported, 30000)) {
            if (!stopping()) say("Capture backend did not report its device within 30 s; nothing was switched on");
            return false;
        }
        const QJsonObject device = w.instrumentInfo.toObject();
        if (w.laserProcess->state() != QProcess::Running || device.value("serial").toString() != "SIMULATED") {
            say("Capture backend did not report the simulated device; nothing was switched on (" + lastLine(w.laserOutput) + ")");
            return false;
        }
        say(QString("Capture backend ready: simulated device %1, serial %2").arg(device.value("model").toString(), device.value("serial").toString()));
        const QTextCursor ignored = w.laserOutput->document()->find("virtual sample ignored");
        if (!ignored.isNull()) say("Capture backend: " + ignored.block().text().section("[Backend] ", 1));
        return true;
    }

    bool switchLaserOn() {
        say("Laser ON at power " + request.power);
        w.display.laserButton->click();
        if (!w.laserOn || w.laserStateUncertain || !w.powerInForce) { say("Laser ON was not confirmed: " + lastLine(w.laserOutput)); return false; }
        say("Laser ON confirmed (" + w.powerLevelString(*w.powerInForce) + ")");
        return true;
    }

    // session.json names the newest dark of the capture backend's kind, here simulated (lead decision
    // D1), and the data layer subtracts it from every spectrum of the session. The dark taken here has
    // to be that one, as the window chooses it: a simulated dark dated later would take its place.
    bool captureDark() {
        const QString folder = ramanHome() + "/dark_measurements";
        const QStringList before = QDir(folder).entryList({"dark_*.txt"}, QDir::Files);
        say("Capture Dark");
        if (!click("Capture Dark")) return false;
        QStringList added = QDir(folder).entryList({"dark_*.txt"}, QDir::Files);
        for (const QString& name : before) added.removeAll(name);
        if (added.size() != 1) { say("Dark not saved: " + lastLine(w.ramanOutput)); return false; }
        const QJsonValue paired = w.darkForNextCapture();
        if (paired.toString() != added.first()) {
            say("Dark saved as " + added.first() + ", but the spectra of this run would be paired with " +
                (paired.isString() ? "dark_measurements/" + paired.toString() : QString("no dark")) +
                ": session.json takes the simulated dark with the latest time in its name. Move later simulated darks away, then run again.");
            return false;
        }
        say("Dark saved: dark_measurements/" + added.first());
        return true;
    }

    bool scan() {
        const bool line = request.mode != "2d";
        if (line) w.lineAxisCombo->setCurrentIndex(w.lineAxisCombo->findData(request.mode));
        const QString earlierSession = w.sessionId;
        say(line ? QString("1D scan along %1, 13 points").arg(request.mode == "1d-x" ? "X" : "Y") : QString("Z scan then 2D scan"));
        if (!click(line ? "1D Scan" : "Start Z scan then 2D scan")) return false;
        if (w.sessionId == earlierSession || !w.scanActive) { say("Scan not started: " + lastLine(w.ramanOutput)); return false; }
        const QString folder = "scan_sessions/" + QFileInfo(w.sessionDir).fileName();
        say("Scan started: " + folder);
        quint64 reported = 0;
        const auto ended = [&] {
            while (reported < w.lastCompletedPoint) {
                ++reported;
                say(line ? QString("Point %1 of 13 saved").arg(reported) : QString("Point %1 saved").arg(reported));
            }
            return !w.scanActive && w.ramanProcess->state() == QProcess::NotRunning;
        };
        if (!waitFor(ended, line ? 10 * 60000 : 60 * 60000)) {
            say(stopping() ? "Stop requested: stopping the scan as the Stop button does" : "Scan did not end in time: stopping it as the Stop button does");
            click("Stop");
            say(QString("Scan stopped: %1 points saved in %2").arg(w.lastCompletedPoint).arg(folder));
            return false;
        }
        const QString failure = fileText(w.sessionDir + "/failure.txt");
        if (!failure.isEmpty()) { say("Scan aborted: " + failure); return false; }
        if (w.ramanProcess->exitStatus() != QProcess::NormalExit || w.ramanProcess->exitCode() != 0) {
            say(QString("Motor program ended with code %1: %2").arg(w.ramanProcess->exitCode()).arg(lastLine(w.ramanOutput)));
            return false;
        }
        say(QString("Scan finished: %1 points saved in %2").arg(w.lastCompletedPoint).arg(folder));
        return true;
    }

    bool captureSingle() {
        const QString earlierCapture = w.singleCaptureDir;
        say("Capture Spectrum at the current stage position");
        if (!click("Capture Spectrum")) return false;
        if (w.singleCaptureDir == earlierCapture) { say("Capture not started: " + lastLine(w.ramanOutput)); return false; }
        const QString folder = "scan_sessions/" + QFileInfo(w.singleCaptureDir).fileName();
        if (!QFile::exists(w.singleCaptureDir + "/point_0001.json")) {
            say("Capture failed: " + fileText(w.singleCaptureDir + "/failure.txt")); return false;
        }
        say("Spectrum saved: " + folder);
        return true;
    }

    // The last step of every run, also after a failure or a stop. An unconfirmed state is sent
    // again, as the window's "Confirm Laser OFF" button does.
    bool switchLaserOff() {
        if (!w.laserOn && !w.laserStateUncertain) return true;
        for (int attempt = 1; attempt <= 3 && w.laserProcess->state() == QProcess::Running; ++attempt) {
            say(attempt == 1 ? "Laser OFF" : "Laser OFF not confirmed: sending it again");
            w.display.laserButton->click();
            if (!w.laserOn && !w.laserStateUncertain) { say("Laser OFF confirmed"); return true; }
        }
        say("Laser OFF was NOT confirmed: " + lastLine(w.laserOutput));
        return false;
    }

    int run() {
        w.sampleIdEdit->setText(request.sampleId); w.operatorEdit->setText(request.operatorName); w.locationEdit->setText(request.location);
        w.powerCombo->setCurrentIndex(int(kPowers.indexOf(request.power)));
        const bool measured = backendReady() && !stopping() && switchLaserOn() && !stopping() && captureDark() && !stopping() &&
                              (request.mode == "single" ? captureSingle() : scan());
        const bool off = switchLaserOff();
        return measured && off ? 0 : 1;
    }
};

int main(int argc, char** argv) {
    // First of all: both helper programs simulate, whatever environment this program was started in.
    // They inherit its environment (MainWindow hands it on) and select their simulators for exactly 1.
    qputenv("RAMAN_BACKEND_SIMULATE", "1");
    qputenv("RAMAN_MOTOR_SIMULATE", "1");
    // The deploy folder this program sits in is its HOME, whatever RAMAN_HOME names; the simulators
    // find the virtual sample and keep the stage file there, where the Studio reads them, and start
    // from the virtual sample's own start position.
    for (const char* elsewhere : {"RAMAN_HOME", "RAMAN_SIM_SAMPLE_DIR", "RAMAN_SIM_STAGE_FILE", "RAMAN_MOTOR_SIM_START"}) qunsetenv(elsewhere);
    // No window is ever shown or drawn, so which font would stand in for a missing one does not matter.
    qputenv("QT_QPA_PLATFORM", "offscreen");
    QLoggingCategory::setFilterRules("qt.qpa.fonts=false");
    struct sigaction stop = {};
    stop.sa_handler = noteStop;
    stop.sa_flags = SA_RESTART;
    sigemptyset(&stop.sa_mask);
    for (int number : {SIGTERM, SIGINT, SIGHUP}) sigaction(number, &stop, nullptr);
    QApplication app(argc, argv);

    QCommandLineParser parser;
    parser.setApplicationDescription("The Portable Raman controller without its window, for the Simulation Studio. One run: Laser ON, "
                                     "Capture Dark, the scan or single capture, Laser OFF. Both helper programs always simulate.");
    const QCommandLineOption helpOption = parser.addHelpOption();
    const QCommandLineOption modeOption("mode", "1d-x, 1d-y, 2d or single.", "mode");
    const QCommandLineOption sampleOption("sample-id", "Sample ID (required).", "text");
    const QCommandLineOption operatorOption("operator", "Operator (required).", "text");
    const QCommandLineOption locationOption("location", "Location (required).", "text");
    const QCommandLineOption powerOption("power", "Laser power: LOW, MEDIUM or HIGH.", "level");
    const QCommandLineOption keepOption("keep-stage", "Start where the last simulated run left the stage, not at the virtual sample's start position.");
    parser.addOptions({modeOption, sampleOption, operatorOption, locationOption, powerOption, keepOption});
    const auto usage = [](const QString& problem) {
        std::fprintf(stderr, "raman_sim_runner: %s (--help lists the options)\n", problem.toUtf8().constData());
        return 2;
    };
    if (!parser.parse(app.arguments())) return usage(parser.errorText());
    if (parser.isSet(helpOption)) { std::fputs(parser.helpText().toUtf8().constData(), stdout); return 0; }
    if (!parser.positionalArguments().isEmpty()) return usage("unexpected argument " + parser.positionalArguments().first());
    Request request;
    request.mode = parser.value(modeOption);
    request.power = parser.value(powerOption);
    request.keepStage = parser.isSet(keepOption);
    if (!kModes.contains(request.mode)) return usage("--mode must be 1d-x, 1d-y, 2d or single");
    if (!kPowers.contains(request.power)) return usage("--power must be LOW, MEDIUM or HIGH");
    // The limits of the Studio's /api/run, so that a run by hand records what the Studio could.
    const QList<std::pair<const QCommandLineOption*, QString*>> texts{
        {&sampleOption, &request.sampleId}, {&operatorOption, &request.operatorName}, {&locationOption, &request.location}};
    for (const auto& [option, value] : texts) {
        *value = parser.value(*option).trimmed();
        const QString name = "--" + option->names().first();
        if (value->isEmpty()) return usage(name + " is required");
        if (value->size() > 200) return usage(name + " is longer than 200 characters");
        if (std::any_of(value->cbegin(), value->cend(), [](QChar c) { return c.category() == QChar::Other_Control; }))
            return usage(name + " contains a control character");
    }

    const QString home = ramanHome();
    if (!QFileInfo(home + "/handshake_v2").isDir()) {
        say("Not started: " + home + " is not a deploy folder (no handshake_v2/ with the helper programs next to this program)");
        return 1;
    }
    QLockFile controllerLock(home + "/controller.lock");
    if (!controllerLock.tryLock(100)) {
        // Exit code 3 tells the Studio that the window or another run is open; a lock file that cannot
        // be created says nothing about that.
        if (controllerLock.error() != QLockFile::LockFailedError) {
            say("Not started: " + home + "/controller.lock cannot be created (" +
                (controllerLock.error() == QLockFile::PermissionError ? "no permission" : "write error") +
                "), so this run could not keep the controller window and other runs out of this folder");
            return 1;
        }
        say("Not started: " + home + "/controller.lock is held; the controller window or another simulated run is open in this folder");
        return 3;
    }
    say(QString("Simulated run in %1: %2, laser power %3, sample %4 (both helper programs simulate; no instrument is used)")
            .arg(home, request.mode, request.power, request.sampleId));
    const QString stageFile = home + "/scan_sessions/_sim_stage.json";
    if (request.keepStage) {
        say("Simulated stage kept where the last simulated run left it");
    } else if (QFile::exists(stageFile) && !QFile::remove(stageFile)) {
        say("Not started: " + stageFile + " cannot be removed, so the simulated stage cannot go back to its start position");
        return 1;
    } else {
        say("Simulated stage set back to the start position of the virtual sample");
    }
    if (!QFile::exists(home + "/virtual_sample/sample.json") || !QFile::exists(home + "/virtual_sample/reference_spectra.json"))
        say("No virtual sample in " + home + "/virtual_sample: the simulators fall back to a synthetic spectrum that the classifier does not know");

    int code = 1;
    {
        MainWindow window;
        code = SimulatedRunner{window, request}.run();
    }   // the window's destructor ends both helper programs and waits for them
    say("Helper programs ended");
    if (stopSignal) {
        say(QString("Stopped on request (%1)").arg(signalName(stopSignal)));
        return 128 + stopSignal;
    }
    say(code == 0 ? "Done" : "Failed; the controller's laser log is laser_log.txt in " + home);
    return code;
}
