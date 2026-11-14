#pragma once
// Starts the Python data layer as a child process, the way data_layer/PROTOCOL.md section 1
// describes it, and hands the one JSON object it prints to a callback. No call here waits for the
// data layer: each returns at once and the answer arrives through the event loop, so a slow or
// hanging data layer cannot hold up the window or an acquisition. Whatever goes wrong arrives the
// same way, as a reply that says what failed. Header-only and without Q_OBJECT (see window_theme.h).

#include <QCoreApplication>
#include <QDesktopServices>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QObject>
#include <QProcess>
#include <QProcessEnvironment>
#include <QStringList>
#include <QTimer>
#include <QUrl>

#include <functional>
#include <memory>

#ifdef Q_OS_WIN
#include <qt_windows.h>
#else
#include <cerrno>
#include <csignal>
#endif

struct DataLayerReply {
    enum class Failure {
        None,
        NotInstalled,    // HOME/data_layer/raman_datafed is missing: nothing was started
        FailedToStart,   // the interpreter could not be run
        Crashed,
        TimedOut,        // no answer in time; the process was stopped
        Unreadable,      // it ended without printing one JSON object
        Reported         // the data layer itself answered "ok": false
    };
    Failure failure = Failure::None;
    QJsonObject json;    // what the command printed, also when it reported a failure
    QString error;       // one short sentence; empty when the command succeeded
    QString detail;      // more, for a tool tip: the command's own detail, or the end of its log
    int exitCode = -1;

    bool ok() const { return failure == Failure::None; }
    static DataLayerReply failed(Failure failure, const QString& error, const QString& detail = QString()) {
        DataLayerReply reply;
        reply.failure = failure; reply.error = error; reply.detail = detail;
        return reply;
    }
};

class DataLayerClient : public QObject {
public:
    using Done = std::function<void(const DataLayerReply&)>;

    // fallbackInterpreter: the analysis interpreter, the last choice of the lookup in program().
    DataLayerClient(const QString& home, const QString& fallbackInterpreter, QObject* parent = nullptr)
        : QObject(parent), home(home), fallbackInterpreter(fallbackInterpreter) {}

    // A command that is still running when the window closes is ended here. The ledger commits each
    // change on its own, so the next sync continues where this one stopped. The wait is for a killed
    // process to be reaped, not for the data layer.
    ~DataLayerClient() override {
        for (QProcess* process : findChildren<QProcess*>()) {
            process->disconnect(this);
            if (process->state() == QProcess::NotRunning) continue;
            process->kill();
            process->waitForFinished(2000);
        }
    }

    QString homeDirectory() const { return home; }
    QString packageDirectory() const { return home + "/data_layer/raman_datafed"; }
    // Without the package the data features are "not installed" and no process is ever started.
    bool installed() const { return QFileInfo(packageDirectory()).isDir(); }
    int running() const { return active; }

    // RAMAN_DATAFED_PYTHON, else the first line of HOME/datafed_python.txt, else the analysis
    // interpreter. A recorded interpreter that has since disappeared is not replaced by another one:
    // the failure to start names it, which is what the user needs in order to repair the file.
    QString program() const {
        if (!qEnvironmentVariableIsEmpty("RAMAN_DATAFED_PYTHON")) return qEnvironmentVariable("RAMAN_DATAFED_PYTHON");
        QFile recorded(home + "/datafed_python.txt");
        if (recorded.open(QIODevice::ReadOnly | QIODevice::Text)) {
            const QString path = QString::fromUtf8(recorded.readLine()).trimmed();
            if (!path.isEmpty()) return path.contains('/') ? QDir(home).absoluteFilePath(path) : path;
        }
        return fallbackInterpreter;
    }
    QStringList arguments(const QStringList& command) const {
        return QStringList{"-m", "raman_datafed", "--home", home} + command;
    }
    QProcessEnvironment environment() const {
        QProcessEnvironment variables = QProcessEnvironment::systemEnvironment();
        const QString package = home + "/data_layer";
        const QString existing = variables.value("PYTHONPATH");
        variables.insert("PYTHONPATH", existing.isEmpty() ? package : package + QDir::listSeparator() + existing);
        return variables;
    }

    // Runs "<command> [options]" and calls done exactly once, from the event loop, never from inside this call.
    void run(const QStringList& command, const Done& done, int timeoutMs = 0) {
        if (!installed()) {
            const DataLayerReply reply = DataLayerReply::failed(DataLayerReply::Failure::NotInstalled,
                "The data layer is not installed", "There is no folder " + QDir::toNativeSeparators(packageDirectory()) + ".");
            QTimer::singleShot(0, this, [done, reply] { if (done) done(reply); });
            return;
        }
        const int limit = timeoutMs > 0 ? timeoutMs : defaultTimeoutMs(command.value(0));
        auto* process = new QProcess(this);
        process->setProgram(program());
        process->setArguments(arguments(command));
        process->setProcessEnvironment(environment());
        process->setWorkingDirectory(home);
        // Nothing may ever wait for input the window cannot give (a library asking for a password).
        process->setStandardInputFile(QProcess::nullDevice());
        ++active;
        auto answered = std::make_shared<bool>(false);
        auto timedOut = std::make_shared<bool>(false);
        const auto answer = [this, process, answered, done](const DataLayerReply& reply) {
            if (*answered) return;
            *answered = true;
            --active;
            process->deleteLater();
            if (done) done(reply);
        };
        connect(process, &QProcess::errorOccurred, this, [this, process, answer](QProcess::ProcessError error) {
            if (error != QProcess::FailedToStart) return;      // a crash is reported by finished()
            const DataLayerReply reply = DataLayerReply::failed(DataLayerReply::Failure::FailedToStart,
                "The data layer could not be started", QDir::toNativeSeparators(process->program()) + ": " + process->errorString());
            QTimer::singleShot(0, this, [answer, reply] { answer(reply); });
        });
        connect(process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
                [process, answer, timedOut, command, limit](int code, QProcess::ExitStatus status) {
            const QByteArray output = process->readAllStandardOutput(), log = process->readAllStandardError();
            if (*timedOut)
                answer(DataLayerReply::failed(DataLayerReply::Failure::TimedOut, "The data layer did not answer in time",
                                              QString("%1 was stopped after %2 s.").arg(command.value(0)).arg(limit / 1000.0, 0, 'g', 3)));
            else if (status == QProcess::CrashExit)
                answer(DataLayerReply::failed(DataLayerReply::Failure::Crashed, "The data layer stopped unexpectedly", logEnd(log)));
            else
                answer(parse(output, log, code));
        });
        auto* watchdog = new QTimer(process);
        watchdog->setSingleShot(true);
        connect(watchdog, &QTimer::timeout, process, [process, timedOut] { *timedOut = true; process->kill(); });
        watchdog->start(limit);
        process->start();
    }

    // A catalog on the network, or the classifier over a long scan, takes its time; everything else is local and quick.
    static int defaultTimeoutMs(const QString& command) {
        if (command == "classify") return 600000;
        if (command == "sync" || command == "import-session") return 180000;
        return 60000;
    }

    // The contract of section 6: exactly one JSON object on stdout, and exit code 0 only with "ok": true.
    static DataLayerReply parse(const QByteArray& output, const QByteArray& log, int exitCode) {
        DataLayerReply reply;
        reply.exitCode = exitCode;
        const QJsonDocument document = QJsonDocument::fromJson(output.trimmed());
        if (!document.isObject()) {
            reply.failure = DataLayerReply::Failure::Unreadable;
            reply.error = QString("The data layer gave no readable answer (exit code %1)").arg(exitCode);
            reply.detail = log.trimmed().isEmpty() ? QString::fromUtf8(output.trimmed().left(600)) : logEnd(log);
            return reply;
        }
        reply.json = document.object();
        if (reply.json.value("ok").toBool() && exitCode == 0) return reply;
        reply.failure = DataLayerReply::Failure::Reported;
        reply.error = reply.json.value("error").toString();
        if (reply.error.isEmpty()) reply.error = QString("The data layer reported a failure (exit code %1)").arg(exitCode);
        reply.detail = reply.json.value("detail").toString();
        return reply;
    }

private:
    static QString logEnd(const QByteArray& log) { return QString::fromUtf8(log.trimmed().right(600)); }

    const QString home, fallbackInterpreter;
    int active = 0;
};

// The Simulation Studio server ("studio" of section 6). It is started detached, so that it stays
// available after the controller has closed; its one output line, which names the address, is
// therefore read from a file instead of a pipe. Nobody is left to stop a detached server, so it is
// told to end by itself once the Studio has not been used for a while.
class StudioLauncher : public QObject {
public:
    // ok with the address, or not ok with the reason.
    using Done = std::function<void(bool ok, const QString& text)>;

    explicit StudioLauncher(DataLayerClient* client, QObject* parent = nullptr) : QObject(parent), client(client) {
        poll.setInterval(100);
        connect(&poll, &QTimer::timeout, this, [this] { readAnswer(); });
    }

    // Replaced by the tests, which must not start a browser.
    std::function<bool(const QUrl&)> openUrl = [](const QUrl& url) { return QDesktopServices::openUrl(url); };
    int answerTimeoutMs = 20000;
    int idleExitSeconds = 900;

    qint64 serverPid() const { return pid; }
    bool starting() const { return poll.isActive(); }

    // Opens the Studio page. The server this controller started earlier is used again while it lives.
    void open(const Done& done) {
        if (!client->installed()) { done(false, "The data layer is not installed; the Simulation Studio is part of it."); return; }
        if (starting()) { done(false, "The Simulation Studio is still starting."); return; }
        if (pid > 0 && url.isValid() && processAlive(pid)) { show(done); return; }
        const QString stem = QDir::tempPath() + QString("/raman_studio_%1_%2").arg(QCoreApplication::applicationPid()).arg(++launches);
        outputPath = stem + ".out"; logPath = stem + ".log";
        QFile::remove(outputPath); QFile::remove(logPath);
        QProcess server;
        server.setProgram(client->program());
        // The page is opened from here, once, when the address is known.
        server.setArguments(client->arguments({"studio", "--no-open", "--idle-exit", QString::number(idleExitSeconds)}));
        server.setProcessEnvironment(client->environment());
        server.setWorkingDirectory(client->homeDirectory());
        server.setStandardInputFile(QProcess::nullDevice());
        server.setStandardOutputFile(outputPath);
        server.setStandardErrorFile(logPath);
        pid = 0; url = QUrl();
        if (!server.startDetached(&pid)) {
            pid = 0;
            done(false, "The Simulation Studio could not be started: " + QDir::toNativeSeparators(server.program()));
            return;
        }
        waitingFor = done;
        waited.start();
        poll.start();
    }

    static bool processAlive(qint64 processId) {
#ifdef Q_OS_WIN
        const HANDLE handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, DWORD(processId));
        if (!handle) return false;
        DWORD code = 0;
        const bool alive = GetExitCodeProcess(handle, &code) && code == STILL_ACTIVE;
        CloseHandle(handle);
        return alive;
#else
        return ::kill(pid_t(processId), 0) == 0 || errno == EPERM;
#endif
    }

private:
    void show(const Done& done) {
        if (openUrl(url)) done(true, url.toString());
        else done(false, "The Simulation Studio runs at " + url.toString() + ", but the page could not be opened in a browser.");
    }

    void readAnswer() {
        QFile output(outputPath);
        const QByteArray line = output.open(QIODevice::ReadOnly) ? output.readLine() : QByteArray();
        if (line.endsWith('\n')) {
            const QJsonObject answer = QJsonDocument::fromJson(line.trimmed()).object();
            const QUrl named(answer.value("url").toString());
            if (answer.value("ok").toBool() && named.isValid() && !named.host().isEmpty()) {
                url = named;
                finish();
                show(takeDone());
                return;
            }
            QString reason = answer.value("error").toString();
            if (reason.isEmpty()) reason = "its first output line is not the address";
            const QString more = answer.value("detail").toString();
            giveUp("The Simulation Studio did not start: " + reason + (more.isEmpty() ? QString() : " (" + more + ")"));
        } else if (!processAlive(pid)) {
            QFile log(logPath);
            const QString last = log.open(QIODevice::ReadOnly) ? QString::fromUtf8(log.readAll().trimmed().right(300)) : QString();
            giveUp("The Simulation Studio ended before it gave its address" + (last.isEmpty() ? QString(".") : ": " + last));
        } else if (waited.elapsed() > answerTimeoutMs) {
            stopServer();
            giveUp(QString("The Simulation Studio gave no address within %1 s and was stopped.").arg(answerTimeoutMs / 1000));
        }
    }
    // The two files were needed for the start only. A server that runs keeps writing its log into
    // the removed file, which goes away with it.
    void finish() {
        poll.stop();
        QFile::remove(outputPath);
        QFile::remove(logPath);
    }
    Done takeDone() {
        Done done = waitingFor;
        waitingFor = nullptr;
        return done;
    }
    void giveUp(const QString& reason) {
        finish();
        pid = 0; url = QUrl();
        takeDone()(false, reason);
    }
    void stopServer() {
#ifdef Q_OS_WIN
        if (const HANDLE handle = OpenProcess(PROCESS_TERMINATE, FALSE, DWORD(pid))) { TerminateProcess(handle, 1); CloseHandle(handle); }
#else
        ::kill(pid_t(pid), SIGTERM);
#endif
    }

    DataLayerClient* client = nullptr;
    QTimer poll;
    QElapsedTimer waited;
    Done waitingFor;
    QString outputPath, logPath;
    qint64 pid = 0;
    QUrl url;
    int launches = 0;
};
