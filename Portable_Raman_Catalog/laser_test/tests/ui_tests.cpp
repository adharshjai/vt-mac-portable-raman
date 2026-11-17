// Tests of the display code and of the Data and Review tabs: the data-layer client, the status
// derivation, and the window with the stand-in data layer (fake_data_layer.py), which the build
// copies to data_layer/raman_catalog/ next to this program. Built like handshake_tests: the real
// window code with the two emulators. No SDK, USB or laser access, and nothing is contacted.
#include "../main.cpp"
#include <QPointer>
#include <QTemporaryDir>
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
static QByteArray readFile(const QString& path) {
    QFile f(path); if (!f.open(QIODevice::ReadOnly)) return {};
    return f.readAll();
}

struct HandshakeTest {   // friend of MainWindow and DataPanels
    using Failure = DataLayerReply::Failure;

    // ---- the stand-in data layer in this program's folder
    static QString home() { return QCoreApplication::applicationDirPath(); }
    static void resetStandIn() {
        for (const char* name : {"fake_data_layer_state.json", "fake_data_layer_calls.jsonl", "fake_data_layer_control.json",
                                 "fake_studio.pid", "fake_studio_stop"})
            QFile::remove(home() + "/" + name);
    }
    static void control(const QJsonObject& behaviour) {
        check(AcquisitionProtocol::publish(home() + "/fake_data_layer_control.json", QJsonDocument(behaviour).toJson()), "write the stand-in's control file");
    }
    static QList<QJsonObject> calls() {
        QList<QJsonObject> entries;
        QFile file(home() + "/fake_data_layer_calls.jsonl");
        if (file.open(QIODevice::ReadOnly))
            for (const QByteArray& line : file.readAll().split('\n'))
                if (!line.trimmed().isEmpty()) entries << QJsonDocument::fromJson(line).object();
        return entries;
    }
    static QStringList commandsCalled() {
        QStringList names;
        for (const QJsonObject& entry : calls()) names << entry["command"].toString();
        return names;
    }
    static QStringList argumentsOf(const QJsonObject& call) {
        QStringList words;
        for (const QJsonValue& word : call["argv"].toArray()) words << word.toString();
        return words;
    }
    static DataLayerReply ask(DataLayerClient& client, const QStringList& command, int timeoutMs = 0) {
        bool answered = false;
        DataLayerReply reply;
        client.run(command, [&](const DataLayerReply& got) { reply = got; answered = true; }, timeoutMs);
        check(!answered, "the answer never arrives inside the call");
        check(waitUntil([&]{ return answered; }, 30000), "every call is answered");
        return reply;
    }
    // A HOME folder that has the package folder, so that it counts as installed.
    static void makeInstalled(const QString& folder) {
        check(QDir().mkpath(folder + "/data_layer/raman_catalog"), "temporary HOME");
    }

    // ---- data_layer_client.h
    static void replyParsing() {
        DataLayerReply reply = DataLayerClient::parse("{\"ok\": true, \"mode\": \"local-demo\", \"pending\": 3}\n", "INFO log line\n", 0);
        check(reply.ok() && reply.failure == Failure::None && reply.error.isEmpty() && reply.exitCode == 0, "one-line object");
        check(reply.json["mode"].toString() == "local-demo" && reply.json["pending"].toInt() == 3, "values of the object");
        reply = DataLayerClient::parse("\n{\n  \"ok\": true,\n  \"items\": [\n    {\"capture_id\": \"a\"}\n  ],\n  \"total\": 1\n}\n\n", "", 0);
        check(reply.ok() && reply.json["items"].toArray().size() == 1 && reply.json["total"].toInt() == 1, "pretty-printed object");
        reply = DataLayerClient::parse("{\"ok\": false, \"error\": \"No such record\", \"detail\": \"d/9 is not in the catalog\"}", "", 1);
        check(!reply.ok() && reply.failure == Failure::Reported && reply.error == "No such record" &&
              reply.detail == "d/9 is not in the catalog" && reply.exitCode == 1 && !reply.json["ok"].toBool(true), "reported failure");
        reply = DataLayerClient::parse("{\"ok\": true, \"offline\": false}", "", 1);
        check(reply.failure == Failure::Reported && reply.error.contains("exit code 1"), "ok with a failing exit code is not a success");
        reply = DataLayerClient::parse("{\"ok\": false}", "", 0);
        check(reply.failure == Failure::Reported && !reply.error.isEmpty(), "exit code 0 without ok is not a success");
        reply = DataLayerClient::parse("{\"mode\": \"local-demo\"}", "", 0);
        check(reply.failure == Failure::Reported, "an object without ok is not a success");
        const QByteArray trace = "Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'raman_catalog'\n";
        reply = DataLayerClient::parse("", trace, 1);
        check(reply.failure == Failure::Unreadable && reply.error.contains("exit code 1") && reply.detail.contains("No module named") &&
              reply.json.isEmpty(), "no output: the end of the log is the detail");
        check(DataLayerClient::parse("[1, 2]", "", 0).failure == Failure::Unreadable, "an array is not the object");
        check(DataLayerClient::parse("{\"ok\": true} {\"ok\": true}", "", 0).failure == Failure::Unreadable, "two objects are not one");
        reply = DataLayerClient::parse("Usage: something else", "", 2);
        check(reply.failure == Failure::Unreadable && reply.detail == "Usage: something else" && reply.exitCode == 2, "text instead of JSON");
        reply = DataLayerClient::parse("", QByteArray(5000, 'x') + "the end", 1);
        check(reply.detail.size() == 600 && reply.detail.endsWith("the end"), "a long log is cut to its end");
    }
    static void interpreterLookup() {
        const QByteArray fromEnvironment = qgetenv("RAMAN_CATALOG_PYTHON");
        const auto restore = qScopeGuard([fromEnvironment]{ qputenv("RAMAN_CATALOG_PYTHON", fromEnvironment); });
        QTemporaryDir folder(home() + "/lookup_XXXXXX");
        check(folder.isValid(), "temporary HOME");
        const DataLayerClient client(folder.path(), "analysis-python");
        qputenv("RAMAN_CATALOG_PYTHON", "/from/the/environment/python");
        check(client.program() == "/from/the/environment/python", "the environment variable comes first");
        qputenv("RAMAN_CATALOG_PYTHON", "");
        check(client.program() == "analysis-python", "an empty variable counts as unset; without the file: the analysis interpreter");
        qunsetenv("RAMAN_CATALOG_PYTHON");
        check(client.program() == "analysis-python", "neither variable nor file: the analysis interpreter");
        check(AcquisitionProtocol::publish(folder.path() + "/catalog_python.txt", "  /recorded/venv/bin/python  \nsecond line\n"), "write file");
        check(client.program() == "/recorded/venv/bin/python", "first line of catalog_python.txt, trimmed");
        qputenv("RAMAN_CATALOG_PYTHON", "/from/the/environment/python");
        check(client.program() == "/from/the/environment/python", "the variable wins over the file");
        qunsetenv("RAMAN_CATALOG_PYTHON");
        check(AcquisitionProtocol::publish(folder.path() + "/catalog_python.txt", "../.venv/bin/python\n"), "write file");
        check(client.program() == QDir(folder.path()).absoluteFilePath("../.venv/bin/python"), "a relative path is relative to HOME");
        check(AcquisitionProtocol::publish(folder.path() + "/catalog_python.txt", "python3\n"), "write file");
        check(client.program() == "python3", "a bare name is looked up on the PATH");
        check(AcquisitionProtocol::publish(folder.path() + "/catalog_python.txt", "\n/second/line/python\n"), "write file");
        check(client.program() == "analysis-python", "an empty first line counts as no recorded interpreter");
        check(client.arguments({"list", "--limit", "5"}) == QStringList({"-m", "raman_catalog", "--home", folder.path(), "list", "--limit", "5"}),
              "-m raman_catalog --home HOME <command> [options]");
    }
    static void launchContract() {
        resetStandIn();
        const QByteArray pythonPath = qgetenv("PYTHONPATH");
        const auto restore = qScopeGuard([pythonPath]{ pythonPath.isNull() ? qunsetenv("PYTHONPATH") : qputenv("PYTHONPATH", pythonPath); });
        DataLayerClient client(home(), "unused-fallback");
        check(client.installed() && client.packageDirectory() == home() + "/data_layer/raman_catalog", "the stand-in counts as installed");
        // This program is started from HOME; the commands must run there wherever the controller was started from.
        const QString startedFrom = QDir::currentPath();
        const auto backToStart = qScopeGuard([startedFrom]{ QDir::setCurrent(startedFrom); });
        check(QDir::setCurrent(QDir::tempPath()) && QFileInfo(QDir::currentPath()).canonicalFilePath() != QFileInfo(home()).canonicalFilePath(),
              "the test runs the commands from another folder");
        qunsetenv("PYTHONPATH");
        DataLayerReply reply = ask(client, {"status"});
        check(reply.ok() && reply.json["mode"].toString() == "local-demo" && reply.json["demo"].toBool() &&
              reply.json["counts"].toObject().contains("needs_review") && reply.json["problems"].isArray(), "status of the stand-in");
        qputenv("PYTHONPATH", "/some/other/packages");
        reply = ask(client, {"list", "--limit", "2"});
        check(reply.ok() && reply.json["items"].toArray().size() == 2 && reply.json["total"].toInt() == 9, "list with an option");
        const QList<QJsonObject> made = calls();
        check(made.size() == 2, "one process per command");
        check(argumentsOf(made[0]) == QStringList({"--home", home(), "status"}) &&
              argumentsOf(made[1]) == QStringList({"--home", home(), "list", "--limit", "2"}), "arguments: --home HOME <command> [options]");
        check(QFileInfo(made[0]["cwd"].toString()).canonicalFilePath() == QFileInfo(home()).canonicalFilePath(), "working directory is HOME");
        check(made[0]["pythonpath"].toString() == home() + "/data_layer", "PYTHONPATH is HOME/data_layer");
        check(made[1]["pythonpath"].toString() == home() + "/data_layer" + QDir::listSeparator() + "/some/other/packages",
              "HOME/data_layer is put in front of an existing PYTHONPATH");
        check(client.running() == 0, "nothing left running");
    }
    static void errorReply() {
        resetStandIn();
        control({{"status", QJsonObject{{"mode", "error"}, {"error", "The catalog rejected the request"}, {"detail", "canned detail"}}}});
        DataLayerClient client(home(), "unused-fallback");
        const DataLayerReply reply = ask(client, {"status"});
        check(!reply.ok() && reply.failure == Failure::Reported && reply.exitCode == 1, "an error reply is a failure, delivered as a reply");
        check(reply.error == "The catalog rejected the request" && reply.detail == "canned detail" && reply.json.contains("ok"), "its text is kept");
        check(ask(client, {"no-such-command"}).failure == Failure::Reported, "an unknown command is reported by the data layer");
        check(ask(client, {"list"}).ok(), "the next command is unaffected");
    }
    static void slowReply() {
        resetStandIn();
        control({{"sync", QJsonObject{{"mode", "slow"}, {"seconds", 1.2}}}});
        DataLayerClient client(home(), "unused-fallback");
        int ticks = 0;
        QTimer ticker;
        QObject::connect(&ticker, &QTimer::timeout, &ticker, [&ticks]{ ++ticks; });
        ticker.start(20);
        bool answered = false;
        DataLayerReply reply;
        QElapsedTimer clock; clock.start();
        client.run({"sync"}, [&](const DataLayerReply& got) { reply = got; answered = true; });
        check(clock.elapsed() < 500 && !answered && client.running() == 1, "the call returns at once while the command runs");
        check(waitUntil([&]{ return answered; }, 20000), "the slow command is answered");
        check(clock.elapsed() >= 1200 && reply.ok() && reply.json["status"].isObject(), "the answer is the slow command's own");
        check(ticks >= 30, "the event loop kept running while the data layer was busy");
        check(client.running() == 0, "nothing left running");

        control({{"sync", QJsonObject{{"mode", "slow"}, {"seconds", 30}}}});
        clock.restart();
        reply = ask(client, {"sync"}, 400);
        check(reply.failure == Failure::TimedOut && reply.error.contains("in time") && reply.detail.contains("sync"), "no answer in time is reported");
        check(clock.elapsed() < 5000 && client.running() == 0, "the command that did not answer was stopped");
    }
    static void brokenReplies() {
        resetStandIn();
        DataLayerClient client(home(), "unused-fallback");
        control({{"status", QJsonObject{{"mode", "garbage"}}}});
        DataLayerReply reply = ask(client, {"status"});
        check(reply.failure == Failure::Unreadable && reply.exitCode == 0 && reply.detail.contains("not the JSON object"), "output that is not JSON");
        control({{"status", QJsonObject{{"mode", "silent"}}}});
        reply = ask(client, {"status"});
        check(reply.failure == Failure::Unreadable && reply.exitCode == 3 && reply.detail.contains("ending without an answer"),
              "no output: exit code and log");
        control({{"status", QJsonObject{{"mode", "crash"}}}});
        reply = ask(client, {"status"});
        check(reply.failure == Failure::Crashed && !reply.error.isEmpty(), "a crash is reported");
        check(client.running() == 0, "nothing left running");
    }
    static void missingPackage() {
        resetStandIn();
        QTemporaryDir folder(home() + "/no_package_XXXXXX");
        check(folder.isValid(), "temporary HOME");
        {
            DataLayerClient client(folder.path(), UI_TEST_PYTHON);
            check(!client.installed(), "no data_layer/raman_catalog: not installed");
            const DataLayerReply reply = ask(client, {"status"});
            check(reply.failure == Failure::NotInstalled && reply.error == "The data layer is not installed" &&
                  reply.detail.contains("raman_catalog") && reply.json.isEmpty(), "not installed is reported as a reply");
            check(client.running() == 0 && calls().isEmpty(), "nothing was started");
        }
        // A package folder without the package in it: Python itself says so, and that reaches the reply.
        makeInstalled(folder.path());
        DataLayerClient client(folder.path(), UI_TEST_PYTHON);
        check(client.installed(), "the folder exists");
        const DataLayerReply reply = ask(client, {"status"});
        check(reply.failure == Failure::Unreadable && reply.exitCode != 0 && reply.detail.contains("raman_catalog"), "an empty package folder");
    }
    static void processFailsToStart() {
        resetStandIn();
        const QByteArray fromEnvironment = qgetenv("RAMAN_CATALOG_PYTHON");
        const auto restore = qScopeGuard([fromEnvironment]{ qputenv("RAMAN_CATALOG_PYTHON", fromEnvironment); });
        qputenv("RAMAN_CATALOG_PYTHON", (home() + "/no_such_interpreter").toUtf8());
        DataLayerClient client(home(), "unused-fallback");
        const DataLayerReply reply = ask(client, {"status"});
        check(reply.failure == Failure::FailedToStart && reply.error == "The data layer could not be started" &&
              reply.detail.contains("no_such_interpreter") && reply.json.isEmpty(), "an interpreter that cannot be run");
        check(client.running() == 0 && calls().isEmpty(), "nothing ran");
    }
    static void clientClosedWhileRunning() {
        resetStandIn();
        control({{"sync", QJsonObject{{"mode", "slow"}, {"seconds", 30}}}});
        bool answered = false;
        auto* client = new DataLayerClient(home(), "unused-fallback");
        client->run({"sync"}, [&](const DataLayerReply&) { answered = true; });
        check(waitUntil([&]{ return commandsCalled().contains("sync"); }), "the command is running");
        QElapsedTimer clock; clock.start();
        delete client;
        check(clock.elapsed() < 2500, "closing does not wait for the data layer");
        spin(300);
        check(!answered, "no answer is delivered to a window that has closed");
    }

    // ---- window_status.h: what the header shows, from a copy of the controller's state
    static void statusPills() {
        ControllerView view;
        view.backendRunning = true; view.scanMode = "2d";
        PillState pill = backendPillState(view);
        check(pill.text == "running" && pill.tone == UiTone::Ok && !pill.prominent, "backend running");
        view.captureFault = true;
        pill = backendPillState(view);
        check(pill.text == "fault" && pill.tone == UiTone::Danger && pill.prominent && pill.tip.contains("restart"), "backend fault");
        view.backendRunning = false;
        pill = backendPillState(view);
        check(pill.text == "stopped" && pill.tone == UiTone::Danger && pill.prominent, "backend stopped, whatever else is set");

        pill = laserPillState(view);
        check(pill.text == "not read" && pill.tone == UiTone::Warn && pill.prominent && pill.tip.contains("treat the laser as on") &&
              pill.tip.contains("Safety stop"), "nothing read back since the start: never shown as off");
        view.laserReadBack = true;
        pill = laserPillState(view);
        check(pill.text == "OFF" && pill.tone == UiTone::Neutral && !pill.prominent && pill.tip.contains("read-back"), "laser off, read back");
        view.laserReadBack = false; view.laserOn = true;
        pill = laserPillState(view);
        check(pill.text == "ON" && pill.tone == UiTone::Hazard && pill.prominent, "laser on: the hazard form");
        view.powerInForce = "HIGH";
        check(laserPillState(view).text == uiJoin({"ON", "HIGH"}), "laser on with the level in force");
        view.laserOn = false;
        check(laserPillState(view).text == "OFF", "off after a confirmed Laser ON: every command since then was read back");
        view.laserUncertain = true;
        pill = laserPillState(view);
        check(pill.text == "UNCONFIRMED" && pill.tone == UiTone::Danger && pill.prominent, "laser unconfirmed");
        view.laserCommandBusy = true; view.laserOn = true;
        pill = laserPillState(view);
        check(pill.text == "switching..." && pill.tone == UiTone::Warn && pill.prominent, "a command on its way is never shown as a settled state");
        view = ControllerView();

        view.scanMode = "2d";
        pill = scanPillState(view);
        check(pill.text == "idle" && pill.tone == UiTone::Neutral, "no scan yet");
        view.scanActive = true; view.savedPoints = 3;
        check(scanPillState(view).text == uiJoin({"running", "3 saved"}) && scanPillState(view).tone == UiTone::Info, "2D scan: its size is not known");
        view.scanMode = "1d-x";
        check(scanPillState(view).text == "running 3 of 13", "line scan: n of N");
        view.paused = true;
        pill = scanPillState(view);
        check(pill.text == "paused 3 of 13" && pill.tone == UiTone::Warn && pill.prominent, "paused");
        view.scanActive = false;
        check(scanPillState(view).text == "idle", "the pause flag alone is not a scan");
        view.paused = false; view.savedPoints = 13; view.lastScanEnd = ControllerView::ScanEnd::Finished;
        pill = scanPillState(view);
        check(pill.text == uiJoin({"idle", "last scan saved 13"}) && pill.tone == UiTone::Ok, "finished scan");
        view.savedPoints = 3; view.lastScanEnd = ControllerView::ScanEnd::Stopped;
        pill = scanPillState(view);
        check(pill.text == "stopped at 3 of 13" && pill.tone == UiTone::Warn, "stopped scan");
        view.lastScanEnd = ControllerView::ScanEnd::Failed;
        pill = scanPillState(view);
        check(pill.text == "FAILED at 3 of 13" && pill.tone == UiTone::Danger && pill.prominent, "failed scan");
        view.scanMode = "2d";
        check(scanPillState(view).text == uiJoin({"FAILED", "3 saved"}), "failed 2D scan");
    }
    static void lastEvents() {
        const EventContext line{"1d-x", 3, false}, grid{"2d", 3, false}, cancelled{"2d", 0, true};
        const auto none = [&](const QString& text) { return !eventFromLogLine(text, line).has_value(); };
        const auto is = [](const QString& text, const EventContext& context, UiTone tone, const QString& shown) {
            const auto event = eventFromLogLine(text, context);
            return event && event->tone == tone && event->text == shown;
        };
        const auto tone = [&](const QString& text, UiTone expected) {
            const auto event = eventFromLogLine(text, line);
            return event && event->tone == expected;
        };
        check(none("[2026-11-13 10:15:00.123] [Backend] CAPTURE_SETTINGS exposure_time_us=1000000 cycle_time_us=1100000") &&
              none("[2026-11-13 10:15:00.123] [Backend] Error code 3") && none("[Raman] homing X") && none("[Raman ERROR] Traceback (most recent call last)") &&
              none("[Motor executable] /somewhere/RamanAutofocus"), "what the helper programs print is not an event");
        check(none("prediction: r6g 0.97") && none("") && none("   "), "nor is the output of the analysis script");
        check(none("[2026-11-13 10:15:00.123] [Scan failed] Capture timed out") && none("[2026-11-13 10:15:00.123] [Single capture saved] run=a pixels=2048 elapsed_ms=1100") &&
              none("[2026-11-13 10:15:00.123] [Single capture failed] Backend exited during capture"), "lines the other log repeats one line later");
        check(none("[2026-11-13 10:15:00.123] [Capture discarded from scan] Scan stopped; raw backend result retained: 5f0c1d2e_2"),
              "the capture that was in flight at a stop does not replace the stop itself");
        check(is("[2026-11-13 10:15:02.500] [Point saved] run=5f0c1d2e point=3 pixels=2048 elapsed_ms=1103", line, UiTone::Ok, "Point 3 of 13 saved"), "saved point of a line scan");
        check(is("[2026-11-13 10:15:02.500] [Point saved] run=5f0c1d2e point=41 pixels=2048 elapsed_ms=1103", grid, UiTone::Ok, "Point 41 saved"), "saved point of a 2D scan");
        check(is("[Capture] Saved: /home/scan_sessions/single_20261113_101500_123", line, UiTone::Ok, uiJoin({"Spectrum saved", "single_20261113_101500_123"})), "single capture saved");
        check(is("[Dark] Saved: /home/dark_measurements/dark_20261113_101400_001.txt", line, UiTone::Ok, uiJoin({"Dark spectrum saved", "dark_20261113_101400_001.txt"})), "dark saved");
        check(is("[Handshake v2] Data: /home/scan_sessions/1d_x_scan_20261113_101500_123", line, UiTone::Info, uiJoin({"Scan started", "1d_x_scan_20261113_101500_123"})), "scan started");
        check(is("[2026-11-13 10:15:00.123] [Laser] ON confirmed. Click Start Z scan then 2D scan to begin scanning.", line, UiTone::Hazard, "Laser ON confirmed"), "laser on");
        check(is("[2026-11-13 10:15:00.123] [Laser] OFF confirmed.", line, UiTone::Ok, "Laser OFF confirmed"), "laser off");
        check(tone("[2026-11-13 10:15:00.123] [Laser] State unconfirmed; scanning blocked. Check backend output and confirm Laser OFF before retrying.", UiTone::Danger), "laser unconfirmed");
        check(is("[2026-11-13 10:15:00.123] [Backend process started]", line, UiTone::Info, "Backend started"), "backend started");
        check(is("[Stop] Scan stopped. This does not switch the laser off.", line, UiTone::Warn, "Scan stopped. This does not switch the laser off."), "stop");
        check(is("[Raman exited] code=0, status=Normal", line, UiTone::Ok, uiJoin({"Scan finished", "3 points saved"})), "scan ran to its end");
        check(!eventFromLogLine("[Raman exited] code=4, status=Normal", cancelled).has_value() &&
              !eventFromLogLine("[Raman exited] code=0, status=Normal", cancelled).has_value(), "after Stop or an abort the reason was already given");
        const auto ended = eventFromLogLine("[Raman exited] code=3, status=Normal", line);
        check(ended && ended->tone == UiTone::Danger && ended->text.contains("code 3") && ended->text.contains("stopped"), "motor program ended by itself");
        check(tone("[Raman exited] code=9, status=Crash", UiTone::Danger), "motor program crashed");
        const QString laserAfterAbort = ". Laser not switched off: press Laser OFF, restart the controller.";
        check(is("[Scan aborted] Capture timed out (15000 ms). Motor must not advance. Laser remains under manual control.", line, UiTone::Danger,
                 "Scan aborted: Capture timed out (15000 ms)" + laserAfterAbort) &&
              is("[Scan aborted] Capture timed out (15000 ms); no blind retry and no success ACK. Motor must not advance. Laser remains under manual control.",
                 line, UiTone::Danger, "Scan aborted: Capture timed out (15000 ms)" + laserAfterAbort), "aborted scan: its reason, then what to do about the laser");
        // The backend's reply is the first line of a reason that goes on over several lines of the log.
        check(is("[Scan aborted] Backend capture failed: CAPTURE a02e7964-26cb-4f70-ae1d-8cb45f24e3c3_1 FAIL -1", grid, UiTone::Danger,
                 "Scan aborted: Backend capture failed" + laserAfterAbort) && is("[Scan aborted] ", grid, UiTone::Danger, "Scan aborted" + laserAfterAbort),
              "a raw backend reply is not shown, and the laser instruction is never lost");
        check(tone("[Capture Error] Backend exited during capture. No spectrum saved.", UiTone::Danger), "an error that mentions 'saved' is still an error");
        check(tone("[Scan] Not started: fill in Sample ID first.", UiTone::Warn) && tone("[Dark] Stop Focus Monitor first.", UiTone::Warn) &&
              tone("[Scan] Please wait for the current operation.", UiTone::Warn) && tone("[Close] Wait for the current capture/laser command to finish, then close again.", UiTone::Warn),
              "refusals are cautions");
        check(tone("[Resume] No paused active scan. A failed/aborted scan cannot resume.", UiTone::Warn), "a refused Resume is a caution, not a failure");
        check(tone("[Resume] Continuing the same pending point; existing data preserved.", UiTone::Ok), "resume");
        check(is("[Paused] Incoming STEP will be held.", line, UiTone::Warn, "Scan paused: press Resume to continue.") &&
              tone("[Paused] Already paused.", UiTone::Warn), "pause, and what continues it");
        check(tone("[Focus Monitor] Live acquisition. No motor movement; spectra are not saved.", UiTone::Info), "'not saved' is not a save");
        check(tone("[Focus Monitor Error] Capture timed out (15000 ms)", UiTone::Danger), "monitor error");
        check(tone("[2026-11-13 10:15:00.123] [Capture read] Cannot read capture result (attempt 1/3): denied; file: x.txt", UiTone::Warn) &&
              tone("[2026-11-13 10:15:00.123] [Capture read recovered] abc; attempt 2/3", UiTone::Ok), "a read that is retried, and one that recovered");
        check(tone("[Scan] Capture fault: confirm Laser OFF, close and restart the app before a new run.", UiTone::Danger), "capture fault");
        check(is("[Analysis] Finished (Code: 0)", line, UiTone::Ok, "Analysis finished") &&
              is("[Analysis] Finished (Code: 1)", line, UiTone::Danger, "Analysis ended with code 1") && tone("[Analysis] Starting...", UiTone::Info), "analysis");
        check(is("[Already running]", line, UiTone::Warn, "Already running"), "a line that is only a tag");
        check(tone("[2026-11-13 10:15:00.123] [Timeout waiting for command processing]", UiTone::Danger) &&
              tone("[Error] Could not start: /somewhere/RamanAutofocus", UiTone::Danger), "errors");
    }
    static void simulationBadgeRules() {
        const char* flag = "RAMAN_TEST_SIMULATION_USE";     // never one of the two real variables: those stay at 1
        const auto restore = qScopeGuard([flag]{ qunsetenv(flag); });
        qunsetenv(flag);
        check(simulationUse(flag) == SimulationUse::Real, "unset: the instrument is driven");
        qputenv(flag, "0");
        check(simulationUse(flag) == SimulationUse::Real, "0: the instrument is driven");
        qputenv(flag, "1");
        check(simulationUse(flag) == SimulationUse::Simulated, "1: simulated");
        for (const char* other : {"true", "yes", "2", "", " 1"}) {
            qputenv(flag, other);
            check(simulationUse(flag) == SimulationUse::Unclear, "any other value: simulation not confirmed, the instruments count as live");
        }
        using Use = SimulationUse;
        PillState badge = simulationBadge(Use::Simulated, Use::Simulated);
        check(badge.text == "SIMULATION" && badge.tone == UiTone::Info && badge.prominent, "both simulated");
        check(simulationBadge(Use::Real, Use::Real).text.isEmpty(), "nothing simulated: no badge");
        badge = simulationBadge(Use::Simulated, Use::Real);
        check(badge.text.startsWith("MOTORS ARE REAL") && badge.tone == UiTone::Danger, "only the backend simulated: the motors lead");
        badge = simulationBadge(Use::Real, Use::Simulated);
        check(badge.text.startsWith("LASER IS REAL") && badge.tone == UiTone::Danger, "only the motors simulated: the laser leads");
        for (const auto& pair : {std::pair{Use::Unclear, Use::Simulated}, std::pair{Use::Simulated, Use::Unclear}, std::pair{Use::Unclear, Use::Real},
                                 std::pair{Use::Real, Use::Unclear}, std::pair{Use::Unclear, Use::Unclear}}) {
            badge = simulationBadge(pair.first, pair.second);
            check(badge.text == "SIMULATION NOT CONFIRMED" && badge.tone == UiTone::Danger, "an unclear value never shows as SIMULATION");
        }
    }
    // Readable in both appearances: the contrast ratios of the colour pairs that carry text.
    static double luminance(const QColor& colour) {
        const auto channel = [](double value) { return value <= 0.03928 ? value / 12.92 : std::pow((value + 0.055) / 1.055, 2.4); };
        return 0.2126 * channel(colour.redF()) + 0.7152 * channel(colour.greenF()) + 0.0722 * channel(colour.blueF());
    }
    static double contrast(const QColor& a, const QColor& b) {
        const double first = luminance(a), second = luminance(b);
        return (std::max(first, second) + 0.05) / (std::min(first, second) + 0.05);
    }
    static void themeContrast() {
        for (const UiTheme& theme : {UiTheme::light(), UiTheme::darkAppearance()}) {
            check(contrast(theme.text, theme.surface) >= 7 && contrast(theme.text, theme.window) >= 7 && contrast(theme.text, theme.surfaceAlt) >= 7, "text");
            check(contrast(theme.textDim, theme.surface) >= 4.5 && contrast(theme.textDim, theme.window) >= 4.5 &&
                  contrast(theme.textDim, theme.surfaceAlt) >= 4.5, "secondary text");
            check(contrast(theme.accentText, theme.accent) >= 4.5 && contrast(theme.accentText, theme.accentHover) >= 4.5, "primary buttons");
            check(contrast(theme.accentInk, theme.surface) >= 4.5 && contrast(theme.series, theme.surface) >= 4.5, "accent text and the spectrum line");
            for (UiTone tone : {UiTone::Neutral, UiTone::Info, UiTone::Ok, UiTone::Warn, UiTone::Danger, UiTone::Hazard}) {
                check(contrast(theme.ink(tone), theme.fill(tone)) >= 4.5, "status text on its own fill");
                if (tone != UiTone::Hazard) check(contrast(theme.ink(tone), theme.surface) >= 4.5, "state words on a table row");
            }
            check(contrast(theme.hazardInk, theme.hazard) >= 10, "the laser warning pair");
            check(contrast(theme.borderStrong, theme.surface) >= 3, "the outline of fields and buttons");
            check(!theme.styleSheet().contains('@'), "every colour name of the style sheet is replaced");
        }
        check(!UiTheme::light().dark && UiTheme::darkAppearance().dark, "the two appearances");
    }

    // ---- the window with the emulators and the stand-in data layer
    static void click(MainWindow& w, const QString& text) { button(w, text)->click(); }
    static QPushButton* button(QWidget& w, const QString& text) {
        for (auto* b : w.findChildren<QPushButton*>()) if (b->text() == text) return b;
        throw std::runtime_error(("Button not found: " + text).toStdString());
    }
    static void setMode(MainWindow& w, const QByteArray& mode) {
        check(AcquisitionProtocol::publish(w.laserProcess->workingDirectory() + "/mock_mode.txt", mode), "Set mock mode");
    }
    static void start(MainWindow& w, const QByteArray& mode = "ok") {
        check(w.laserProcess->state() == QProcess::Running, "Mock backend not running");
        setMode(w, mode);
        w.laserOn = true; // Tests only: bypass physical laser controls, never call an SDK.
        click(w, "Start Z scan then 2D scan");
    }
    static void duringCapture(MainWindow& w, const QString& text) {
        auto* timer = new QTimer(&w);
        timer->setInterval(5);
        QObject::connect(timer, &QTimer::timeout, &w, [&w, timer, text]{
            if (w.isCollecting) { timer->stop(); click(w, text); }
        });
        timer->start();
    }
    // Laser ON the way the operator does it: through the emulated laser menu and its read-back.
    static void confirmLaserOn(MainWindow& w) {
        setMode(w, "ok");
        click(w, "Laser ON");
        check(w.laserOn && !w.laserStateUncertain, "Laser ON confirmed by the emulated read-back");
    }
    // The title of the left-column group a control stands in.
    static QString groupOf(QWidget* widget) {
        for (QWidget* parent = widget->parentWidget(); parent; parent = parent->parentWidget())
            if (parent->objectName() == "group") return parent->findChild<QLabel*>("groupTitle")->text();
        return QString();
    }
    static bool settled(DataPanels& p) {
        return !p.refreshRunning && !p.syncRunning && !p.classifyRunning && !p.offlineRunning && !p.importRunning && !p.provenanceRunning &&
               !p.reviewShowRunning && !p.reviewSetRunning && p.dataLayer->running() == 0;
    }
    static bool firstRefresh(DataPanels& p) {
        return waitUntil([&]{ return !p.status.isEmpty() && settled(p); }, 20000);
    }
    static QJsonObject lastCall(const QString& command) {
        const QList<QJsonObject> made = calls();
        for (auto call = made.rbegin(); call != made.rend(); ++call)
            if ((*call)["command"].toString() == command) return *call;
        throw std::runtime_error(("The stand-in was never asked for " + command).toStdString());
    }
    // What follows "--home HOME <command>".
    static QStringList optionsOf(const QString& command) { return argumentsOf(lastCall(command)).mid(3); }
    static int rowOf(DataPanels& p, const QString& sample, int point) {
        for (int row = 0; row < p.captures.size(); ++row) {
            const QJsonObject item = p.captures.at(row).toObject();
            if (item["sample_id"].toString() == sample && item["point_id"].toInt() == point) return row;
        }
        throw std::runtime_error(("Capture not in the list: " + sample).toStdString());
    }
    static QString cellText(DataPanels& p, int row, int column) { return p.captureTable->item(row, column)->text(); }
    static int reviewRowOf(DataPanels& p, const QString& prediction) {
        for (int row = 0; row < p.reviewItems.size(); ++row)
            if (p.reviewItems.at(row).toObject()["prediction_id"].toString() == prediction) return row;
        throw std::runtime_error(("Prediction not in the review list: " + prediction).toStdString());
    }
    static void showReviewItem(DataPanels& p, const QString& prediction) {
        p.reviewTable->selectRow(reviewRowOf(p, prediction));
        check(waitUntil([&]{ return p.reviewShownId == prediction && settled(p); }), "the selected prediction is shown");
    }

    static void windowLayout() {
        resetStandIn();
        MainWindow w;
        QStringList texts;
        for (QPushButton* b : w.findChildren<QPushButton*>()) texts << b->text();
        for (const char* text : {"Laser ON", "Start Z scan then 2D scan", "1D Scan", "Stop", "Pause", "Resume", "Monitor: OFF", "Draw Once",
                                 "Capture Dark", "Analyze Latest (CNN)", "Capture Spectrum"})
            check(texts.count(text) == 1, "every control of the controller is there once, under its own text");
        check(QSet<QString>(texts.begin(), texts.end()).size() == texts.size(), "no two buttons share a text: they are found by it");
        for (QLineEdit* field : {w.sampleIdEdit, w.operatorEdit, w.locationEdit, w.notesEdit}) check(groupOf(field) == "SAMPLE", "the four fields");
        check(groupOf(w.powerCombo) == "LASER" && groupOf(button(w, "Laser ON")) == "LASER", "power and the laser button");
        for (const char* text : {"Start Z scan then 2D scan", "1D Scan", "Stop", "Pause", "Resume"}) check(groupOf(button(w, text)) == "SCAN", "scan controls");
        check(groupOf(w.lineAxisCombo) == "SCAN", "the axis of the line scan");
        for (QPushButton* b : {button(w, "Capture Spectrum"), button(w, "Capture Dark"), w.monitorButton, w.oneShotButton})
            check(groupOf(b) == "CAPTURE", "capture controls");
        check(groupOf(button(w, "Analyze Latest (CNN)")) == "ANALYSIS", "analysis");
        check(groupOf(button(w, "Open Simulation Studio")).isEmpty() && button(w, "Open Simulation Studio")->isEnabled(), "the Studio button, below the groups");
        check(w.display.tabs->count() == 3 && w.display.tabs->tabText(0) == "Activity" && w.display.tabs->tabText(1) == "Data" &&
              w.display.tabs->tabText(2) == "Review", "the three tabs");
        check(w.display.tabs->widget(0)->isAncestorOf(w.laserOutput) && w.display.tabs->widget(0)->isAncestorOf(w.ramanOutput), "Activity holds the two logs");
        check(w.display.tabs->widget(1) == w.dataPanels->dataPage() && w.display.tabs->widget(2) == w.dataPanels->reviewPage(), "Data and Review");
        check(w.display.lastEvent->parentWidget() == w.chartView->parentWidget() && w.display.lastEvent->y() < w.chartView->y() || !w.isVisible(),
              "the last event stands with the chart");
        check(w.size() == QSize(1440, 880) && w.minimumWidth() <= 1180 && w.minimumHeight() <= 720, "default and smallest size");
        check(w.windowTitle() == "Portable Raman Controller (SIMULATION)", "the window title says simulation");
        check(w.display.simulation->value() == "SIMULATION" && !w.display.simulation->isHidden(), "SIMULATION badge: both helpers are simulated here");
        check(w.display.lastEvent->text() == "Backend started", "the first event of every start");
        check(!w.sampleIdEdit->property("missing").toBool(), "filled required field: no mark");
        w.sampleIdEdit->setText("  ");
        check(w.sampleIdEdit->property("missing").toBool() && !w.notesEdit->property("missing").isValid(), "an empty required field is marked");
        spin(400);
        check(!w.display.timer->isActive() && !w.dataPanels->started() && calls().isEmpty(), "a window that is not shown starts no timer and no process");
    }

    static void statusDisplay() {
        resetStandIn();
        MainWindow w;
        w.show();
        check(w.display.timer->isActive() && w.dataPanels->started(), "the display and the data layer start when the window is shown");
        // The screen of the offscreen platform is smaller than the default size of the window.
        const QSize room = w.screen()->availableGeometry().size();
        check(room.width() < 1440 && room.height() < 880 && room.height() > w.minimumHeight(), "the test screen is the small one this check is about");
        check(w.width() == w.minimumWidth() && w.frameGeometry().height() <= room.height() && w.height() > room.height() - 40,
              "a screen with less room than the default size gets a window that fits on it, down to the smallest size");
        check(w.display.backend->value() == "running" && w.display.backend->tone() == UiTone::Ok, "backend running");
        // Nothing has read the laser back yet: a laser left on by an abnormal end must not read as off.
        check(w.display.laser->value() == "not read" && w.display.laser->tone() == UiTone::Warn && w.display.laser->prominent() &&
              w.display.laser->toolTip().contains("treat the laser as on"), "laser not read back since the start: not shown as off");
        check(!w.display.hazard->isActive() && !w.display.laserGroup->property("alert").toBool(), "and no warning stripes");
        check(w.display.scan->value() == "idle", "no scan");
        setMode(w, "ok");
        w.powerCombo->setCurrentIndex(1);
        click(w, "Laser ON");
        w.refreshStatusDisplay();
        check(w.display.laser->value() == uiJoin({"ON", "MEDIUM"}) && w.display.laser->tone() == UiTone::Hazard && w.display.laser->prominent() &&
              w.display.hazard->isActive() && w.display.laserGroup->property("alert").toBool() && w.display.laserButton->property("laser") == "on",
              "laser on: pill, stripes, group and button");
        check(w.display.lastEvent->text() == "Laser ON confirmed" && w.display.lastEvent->tone() == UiTone::Hazard, "laser on is the last event");
        w.powerCombo->setCurrentIndex(2);
        w.refreshStatusDisplay();
        check(w.display.laser->value() == uiJoin({"ON", "MEDIUM"}), "the pill shows the level in force, not the Power box");
        w.laserCommandBusy = true;
        w.refreshStatusDisplay();
        check(w.display.laser->value() == "switching..." && w.display.hazard->isActive(), "a laser command on its way");
        w.laserCommandBusy = false;
        click(w, "Laser OFF");
        w.refreshStatusDisplay();
        check(w.display.laser->value() == "OFF" && w.display.laser->tone() == UiTone::Neutral && !w.display.hazard->isActive() &&
              w.display.laserButton->property("laser") == "off" && w.display.lastEvent->text() == "Laser OFF confirmed" &&
              w.display.lastEvent->tone() == UiTone::Ok, "confirmed off: read back");
        setMode(w, "laser_stays_off");
        click(w, "Laser ON");
        w.refreshStatusDisplay();
        check(w.display.laser->value() == "UNCONFIRMED" && w.display.laser->tone() == UiTone::Danger && w.display.laser->prominent() &&
              w.display.hazard->isActive() && w.display.laserGroup->property("alert").toBool(), "unconfirmed laser: shown as a hazard");
        check(w.display.laserButton->text() == "Confirm Laser OFF" && w.display.laserButton->property("laser") == "unconfirmed", "the laser button says what to do");
        check(w.display.lastEvent->tone() == UiTone::Danger && w.display.lastEvent->text().startsWith("Laser: State unconfirmed"), "and the last event says why");
        click(w, "Confirm Laser OFF");
        w.refreshStatusDisplay();
        check(w.display.laser->value() == "OFF" && !w.display.hazard->isActive() && w.display.laserButton->property("laser") == "off" &&
              w.display.lastEvent->text() == "Laser OFF confirmed" && w.display.lastEvent->tone() == UiTone::Ok, "confirmed off after an unconfirmed command");
        w.backendCaptureFault = true;
        w.refreshStatusDisplay();
        check(w.display.backend->value() == "fault" && w.display.backend->tone() == UiTone::Danger && w.display.backend->prominent(), "capture fault");
        w.backendCaptureFault = false;
        w.laserProcess->kill();
        check(w.laserProcess->waitForFinished(3000), "emulator stopped");
        check(waitUntil([&]{ return w.display.backend->value() == "stopped"; }), "the display notices by itself that the backend has gone");
    }

    static void scanDisplay() {
        resetStandIn();
        {
            MainWindow w;
            w.show();
            confirmLaserOn(w);
            w.lineAxisCombo->setCurrentIndex(w.lineAxisCombo->findData("1d-x"));
            click(w, "1D Scan");
            QStringList pills, events;
            check(waitUntil([&]{
                if (!pills.contains(w.display.scan->value())) pills << w.display.scan->value();
                if (!events.contains(w.display.lastEvent->text())) events << w.display.lastEvent->text();
                return w.lastCompletedPoint == 13 && !w.scanActive;
            }, 30000), "line scan completes while the display runs");
            check(pills.filter(QRegularExpression("^running \\d+ of 13$")).size() >= 3, "the scan pill counts the saved points");
            check(events.filter(QRegularExpression("^Point \\d+ of 13 saved$")).size() >= 3, "every saved point is an event");
            check(events.filter(QRegularExpression("^1D Scan: 1d-x: home selected axis")).size() == 1, "the start of a line scan says which axis homes");
            w.refreshStatusDisplay();
            check(w.display.scan->value() == uiJoin({"idle", "last scan saved 13"}) && w.display.scan->tone() == UiTone::Ok, "finished scan");
            check(w.display.lastEvent->text() == uiJoin({"Scan finished", "13 points saved"}) && w.display.lastEvent->tone() == UiTone::Ok, "finished scan event");
            check(!w.display.stopButton->property("urgent").toBool(), "Stop is calm again");
            click(w, "Stop");
            w.refreshStatusDisplay();
            check(QFile::exists(w.sessionDir + "/cancel.txt") && w.display.scan->value() == uiJoin({"idle", "last scan saved 13"}),
                  "a Stop pressed afterwards does not make a finished scan a stopped one");
        }
        {
            MainWindow w;
            w.show();
            start(w);
            check(waitUntil([&]{ return w.display.scan->value().startsWith("running"); }), "running");
            check(w.display.stopButton->property("urgent").toBool(), "Stop stands out while a scan runs");
            duringCapture(w, "Stop");
            check(waitUntil([&]{ return !w.scanActive && !w.isCollecting; }), "stop completes");
            w.refreshStatusDisplay();
            check(w.lastCompletedPoint < 3 && w.display.scan->value() == uiJoin({"stopped", QString("%1 saved").arg(w.lastCompletedPoint)}) &&
                  w.display.scan->tone() == UiTone::Warn, "stopped scan, with the points saved until then");
            check(w.display.lastEvent->text() == "Scan stopped. This does not switch the laser off." && w.display.lastEvent->tone() == UiTone::Warn,
                  "Stop reminds that the laser stays on");
        }
        {
            MainWindow w;
            w.show();
            start(w, "fail");
            check(waitUntil([&]{ return !w.scanActive && !w.isCollecting; }), "failure stops the scan");
            w.refreshStatusDisplay();
            check(w.display.scan->value() == uiJoin({"FAILED", "0 saved"}) && w.display.scan->tone() == UiTone::Danger && w.display.scan->prominent() &&
                  w.display.backend->value() == "fault", "failed scan and faulty backend");
            check(w.display.lastEvent->tone() == UiTone::Danger && w.display.lastEvent->text().startsWith("Scan aborted: "), "the reason stays the last event");
        }
        {
            MainWindow w;
            w.show();
            start(w);
            duringCapture(w, "Pause");
            check(waitUntil([&]{ return w.paused && w.lastCompletedPoint == 1 && !w.isCollecting; }), "paused in the first exposure");
            w.refreshStatusDisplay();
            check(w.display.scan->value() == uiJoin({"paused", "1 saved"}) && w.display.scan->tone() == UiTone::Warn &&
                  w.display.pauseButton->property("engaged").toBool(), "paused scan");
            click(w, "Resume");
            check(waitUntil([&]{ return w.lastCompletedPoint == 3 && !w.scanActive; }), "resume completes");
            w.refreshStatusDisplay();
            check(!w.display.pauseButton->property("engaged").toBool(), "Pause is released");
        }
    }

    static void dataTab() {
        resetStandIn();
        MainWindow w;
        w.show();
        DataPanels& p = *w.dataPanels;
        check(firstRefresh(p), "first status and lists");
        check(commandsCalled() == QStringList({"status", "list", "review-list", "review-show"}),
              "status, the rows of both tabs, the first prediction that waits; nothing else, and no sync");
        check(!p.catalogBadge->isHidden() && p.catalogBadge->text() == "DEMO", "the demo catalog is marked DEMO");
        check(p.catalogLabel->text().contains("demo", Qt::CaseInsensitive) && p.catalogLabel->text().contains("nothing is sent anywhere"), "and named in plain words");
        // The capture in error lacks its fields: it is counted once, as a problem, and not as one a sync can move.
        check(p.summaryLabel->text() == uiJoin({"9 captures", "2 not uploaded yet", "1 to classify", "3 to review", "1 with a problem", "no sync yet"}), "counts");
        check(!p.problemsLabel->isHidden() && p.problemsLabel->text().contains("cannot be registered as their files are") &&
              p.problemsLabel->tone() == UiTone::Warn, "problems");
        check(w.display.data->value() == uiJoin({"demo", "2 to sync", "1 stuck"}) && w.display.data->tone() == UiTone::Warn &&
              w.display.data->toolTip().contains("1 stuck: no sync moves it on by itself") &&
              w.display.tabs->tabText(2) == "Review (3)",
              "header: catalog, what a sync has still to move and what no sync can move; what waits for a review is the tab's count");
        QStringList headers;
        for (int column = 0; column < p.captureTable->columnCount(); ++column) headers << p.captureTable->horizontalHeaderItem(column)->text();
        check(headers == QStringList({"Time", "Sample", "Point", "State (demo)", "Record", "Prediction", "Review"}) && p.captureTable->rowCount() == 9, "the table");
        int row = rowOf(p, "TEST-PLATE-A", 2);
        check(cellText(p, row, 1) == "TEST-PLATE-A" && !p.captureTable->item(row, 1)->icon().isNull() && cellText(p, row, 2) == "2" &&
              cellText(p, row, 3) == "Needs review" && p.captureTable->item(row, 3)->foreground().color() == uiTheme().warnInk &&
              cellText(p, row, 4) == "d/102" && cellText(p, row, 5) == "r6g? 0.61 (uncertain)" && cellText(p, row, 6) == "to review", "a row that waits for review");
        check(p.captureTable->item(row, 5)->toolTip() == "Model: " + uiJoin({"UNKNOWN (r6g 0.61)", "low confidence"}), "the model's own label in the tool tip");
        row = rowOf(p, "TEST-PLATE-A", 1);
        check(cellText(p, row, 3) == "Accepted" && p.captureTable->item(row, 3)->foreground().color() == uiTheme().okInk &&
              cellText(p, row, 5) == "r6g 0.97" && cellText(p, row, 6) == DataText::none(), "an accepted row");
        // Background and uncertain are both UNKNOWN to the model; the words tell them apart, the score comes early.
        check(cellText(p, rowOf(p, "TEST-PLATE-A", 4), 5) == "di_water 0.93 (background)" && cellText(p, rowOf(p, "TEST-PLATE-A", 3), 5) == "unusable",
              "an accepted background spectrum, and one the classifier rejected");
        check(cellText(p, rowOf(p, "TEST-MB-01", 1), 2) == "single", "a single capture");
        const int errorRow = rowOf(p, QString(), 3);
        check(cellText(p, errorRow, 3) == "Error" && p.captureTable->item(errorRow, 3)->foreground().color() == uiTheme().dangerInk &&
              cellText(p, errorRow, 1) == DataText::none() && p.captureTable->item(errorRow, 1)->icon().isNull() &&
              p.captureTable->item(errorRow, 3)->toolTip().contains("no capture block"), "a row with a problem; not known to be simulated: no SIM mark");

        // Details and provenance of the selected row.
        p.captureTable->selectRow(rowOf(p, "TEST-PLATE-A", 2));
        check(waitUntil([&]{ return settled(p) && p.captureProvenance->rowCount() == 4; }), "provenance of the selected capture");
        const QString facts = p.captureFacts->toPlainText();
        for (const char* part : {"TEST-PLATE-A", "simulated", "1d_x_scan_20261113_101500_123", "d/102", "UNKNOWN (r6g 0.61)", "low confidence", "to review"})
            check(facts.contains(part), "details of the selected capture, with the model's own label");
        check(optionsOf("provenance") == QStringList({"--record", "d/102"}), "provenance is asked for the raw record");
        QTreeWidgetItem* derived = p.captureProvenance->topLevelItem(0);
        check(p.captureProvenance->topLevelItemCount() == 1 && derived->text(0) == "Prediction" && derived->text(1) == "d/202" &&
              derived->text(2) == "r6g? 0.61 (uncertain)" && derived->childCount() == 3, "the prediction, with what it is derived from below it");
        check(derived->child(0)->text(0) == "Spectrum" && derived->child(0)->text(1) == "d/102" && derived->child(0)->font(0).bold() &&
              derived->child(1)->text(0) == "Model" && derived->child(1)->text(2) == "pca_svm_pure_20260922" &&
              derived->child(2)->text(0) == "Dark" && derived->child(2)->text(2) == "20261113_101400_001", "spectrum (the selected one), model, dark");
        p.captureTable->selectRow(rowOf(p, "TEST-PLATE-A", 6));
        check(waitUntil([&]{ return settled(p) && p.captureProvenance->rowCount() == 1; }), "a record without a prediction");
        check(p.provenanceNote->text() == "Nothing has been derived from this record yet." && !p.provenanceNote->isHidden(), "said in words");
        const int provenanceCalls = commandsCalled().count("provenance");
        p.captureTable->selectRow(errorRow);
        spin(300);
        check(p.provenanceNote->text().startsWith("Not registered in the demo catalog yet") && p.captureProvenance->isHidden() &&
              commandsCalled().count("provenance") == provenanceCalls, "no record: nothing to ask for");

        // Sync now.
        p.syncButton->click();
        check(!p.syncButton->isEnabled(), "one sync at a time");
        check(waitUntil([&]{ return settled(p) && commandsCalled().contains("sync"); }), "sync");
        check(p.syncButton->isEnabled() && commandsCalled().mid(commandsCalled().lastIndexOf("sync")) == QStringList({"sync", "list", "review-list"}),
              "a sync brings its own status; the rows are read again");
        check(optionsOf("sync") == QStringList({"--retry-failed"}), "pressing Sync now also retries what had failed too often");
        check(p.activityLabel->text().startsWith("Demo sync at ") && p.activityLabel->text().contains("(nothing leaves this Mac): 1 registered, 1 upload started, 1 upload finished.") &&
              p.activityLabel->tone() == UiTone::Neutral, "the result is worded as a demo");
        check(w.display.data->value() == uiJoin({"demo", "1 to sync", "1 stuck"}) && p.summaryLabel->text().contains("last sync ") &&
              cellText(p, rowOf(p, "TEST-PLATE-A", 7), 3) == "Uploading" && cellText(p, rowOf(p, "TEST-PLATE-A", 6), 3) == "Uploaded", "the rows moved on");
        check(p.selectedCapture()["capture_id"].toString() == p.captures.at(errorRow).toObject()["capture_id"].toString(), "the selection survives a refresh");

        // Classify now, then the sync that confirms the uploads classify could not see confirmed.
        p.classifyButton->click();
        check(waitUntil([&]{ return settled(p) && commandsCalled().count("sync") == 2; }), "classify, then sync");
        check(commandsCalled().indexOf("classify") < commandsCalled().lastIndexOf("sync") && optionsOf("sync").isEmpty(),
              "in that order; a sync that follows by itself does not retry what was given up");
        check(cellText(p, rowOf(p, "TEST-PLATE-A", 5), 3) == "Accepted" && cellText(p, rowOf(p, "TEST-PLATE-A", 5), 5) == "adenine 0.91" &&
              cellText(p, rowOf(p, "TEST-PLATE-A", 6), 5) == "methylene_blue 0.88", "new predictions in the table");
        check(p.activityLabel->text().startsWith("Demo classify at ") &&
              p.activityLabel->text().endsWith("(nothing leaves this Mac): 2 processed, 2 prediction(s) made, 1 left for later (runner: local).") &&
              p.activityLabel->tone() == UiTone::Neutral, "the result of classify stays in view after the sync that follows it");

        // Offline switch of the demo.
        check(!p.offlineButton->isHidden() && p.offlineButton->text() == "Go offline", "the demo has an offline switch");
        p.offlineButton->click();
        check(waitUntil([&]{ return settled(p) && p.catalogOffline(); }), "offline");
        check(optionsOf("offline") == QStringList({"--on"}) && p.offlineButton->text() == "Go online" && p.catalogLabel->text().contains("switched offline for the demo"), "offline shown");
        check(w.display.data->value().contains("offline") && w.display.data->tone() == UiTone::Warn, "and in the header");
        p.syncButton->click();
        check(waitUntil([&]{ return settled(p) && commandsCalled().count("sync") == 3; }), "sync while offline");
        check(p.activityLabel->text().contains("the demo catalog is switched offline") && p.activityLabel->tone() == UiTone::Warn, "an offline sync says that captures wait");
        p.offlineButton->click();
        check(waitUntil([&]{ return settled(p) && !p.catalogOffline(); }), "online again");
        check(optionsOf("offline") == QStringList({"--off"}) && p.offlineButton->text() == "Go offline", "online shown");

        // Import of a folder the original controller recorded: refused without the three fields, then supplied.
        const QString oldFolder = home() + "/scan_sessions/2d_scan_20260924_141500_001";
        p.importFolder(oldFolder, QString(), "someone", QString());
        check(waitUntil([&]{ return settled(p) && commandsCalled().contains("import-session"); }), "import without the fields");
        check(p.activityLabel->text() == "Import failed: 2d_scan_20260924_141500_001 lacks fields that have to be supplied: --sample-id, --location "
                                         "(Run import-session again with those options.)" && p.activityLabel->tone() == UiTone::Danger,
              "the refusal names what is missing");
        p.importFolder(oldFolder, "S-OLD", "  earlier operator ", "Lab 7");
        check(waitUntil([&]{ return settled(p) && commandsCalled().count("import-session") == 2; }), "import");
        check(optionsOf("import-session") == QStringList({"--folder=" + oldFolder, "--sample-id=S-OLD", "--operator=earlier operator", "--location=Lab 7"}),
              "the folder and the supplied fields, trimmed");
        check(p.activityLabel->text() == "Imported 2d_scan_20260924_141500_001: 1 capture(s) added to the list, 0 skipped.", "import result");
        check(cellText(p, rowOf(p, "S-OLD", 3), 3) == "Captured" && p.problemsLabel->isHidden() && !w.display.data->value().contains("stuck"),
              "the capture is usable now; nothing needs a person any more");
        QPointer<QDialog> dialog = p.openImportDialog();
        check(dialog && dialog->findChild<QLineEdit*>("importFolder")->text().isEmpty(), "the import dialog opens without blocking");
        check(dialog->isVisible() && dialog->windowModality() == Qt::NonModal && !QApplication::activeModalWidget(),
              "not modal: during a scan, Stop and Laser OFF of the window stay within reach");
        QDialog* again = p.openImportDialog();
        int importDialogs = 0;
        for (const QDialog* open : w.findChildren<QDialog*>()) importDialogs += open->windowTitle() == "Import scan folder";
        check(again == dialog.data() && importDialogs == 1, "one import dialog at a time");
        button(*dialog, "Import")->click();
        spin(100);
        check(dialog && commandsCalled().count("import-session") == 2, "nothing is imported without a folder");
        dialog->findChild<QLineEdit*>("importFolder")->setText("/somewhere else/2d_scan_20260101_000000_000");
        dialog->findChild<QLineEdit*>("importSample")->setText("-S 2");
        button(*dialog, "Import")->click();
        check(waitUntil([&]{ return settled(p) && commandsCalled().count("import-session") == 3 && !dialog; }), "import from the dialog, which closes");
        check(optionsOf("import-session") == QStringList({"--folder=/somewhere else/2d_scan_20260101_000000_000", "--sample-id=-S 2"}),
              "empty fields are not sent; a value that begins with a dash stays a value");
        check(p.activityLabel->text() == "Imported 2d_scan_20260101_000000_000 (copied into scan_sessions): 2 capture(s) added to the list, 0 skipped.",
              "a folder from elsewhere is said to have been copied");

        // A slow command holds nothing up: the details of another row arrive while it runs.
        control({{"sync", QJsonObject{{"mode", "slow"}, {"seconds", 2.0}}}});
        p.syncButton->click();
        p.captureTable->selectRow(rowOf(p, "TEST-PLATE-A", 1));
        check(waitUntil([&]{ return p.captureProvenance->rowCount() == 4 && p.captureProvenance->topLevelItem(0)->text(1) == "d/201"; }), "details during a slow sync");
        check(p.syncRunning && !p.syncButton->isEnabled(), "the sync is still on its way");
        check(waitUntil([&]{ return settled(p); }), "and ends");

        // The data layer fails: said in the tab and in the header, and the tab recovers.
        control({{"status", QJsonObject{{"mode", "error"}, {"error", "The catalog rejected the request"}, {"detail", "canned detail"}}}});
        p.refresh();
        check(waitUntil([&]{ return settled(p) && !p.statusProblem.isEmpty(); }), "failed status");
        check(p.catalogLabel->text() == "The data layer did not give its status: The catalog rejected the request (canned detail)" &&
              p.catalogLabel->property("tone") == "danger" && p.catalogBadge->isHidden(), "the failure in words");
        check(w.display.data->value() == "unavailable" && w.display.data->tone() == UiTone::Danger, "and in the header");
        control({});
        p.refresh();
        check(waitUntil([&]{ return settled(p) && p.statusProblem.isEmpty(); }), "status again");
        check(p.catalogBadge->text() == "DEMO" && !p.catalogBadge->isHidden() && !p.offlineButton->isHidden() &&
              p.captureTable->horizontalHeaderItem(3)->text() == "State (demo)" && w.display.data->value().startsWith("demo"), "the demo catalog again, with its offline switch");
        p.syncButton->click();
        check(waitUntil([&]{ return settled(p); }) && p.activityLabel->text().startsWith("Demo sync at "), "a sync is called a demo sync");

        // The settings name another catalog than the one the rows describe, until the next sync: the
        // states, records and predictions of the catalog used before are not passed off as this one's.
        // The periodic sync would end that at any moment (periodicSync tests it); here the test syncs.
        p.autoSync.stop();
        control({{"catalog_changed", true}});
        p.refresh();
        check(waitUntil([&]{ return settled(p) && p.catalogChanged(); }), "rows of the catalog used before");
        QTableWidgetItem* stateHeader = p.captureTable->horizontalHeaderItem(3);
        check(stateHeader->text() == "State (previous catalog)" && stateHeader->toolTip().contains("The next sync registers every capture in the demo catalog."),
              "the states are said to be the previous catalog's");
        row = rowOf(p, "TEST-PLATE-A", 1);
        check(cellText(p, row, 4) == DataText::none() && p.captureTable->item(row, 4)->toolTip().startsWith("d/101 in the catalog used before"),
              "no record id of the previous catalog in the table; it is in the tool tip");
        check(p.summaryLabel->text().startsWith(uiJoin({QString("%1 captures").arg(p.captures.size()), "the next sync registers them in the demo catalog"})) &&
              !p.summaryLabel->text().contains("to review") && !p.summaryLabel->text().contains("not uploaded"),
              "the summary says what the next sync does and counts none of the previous catalog's states");
        check(w.display.data->value() == uiJoin({"demo", "not synced yet"}) && w.display.data->tone() == UiTone::Info && p.periodicSyncDue(),
              "the header does not say synced; the periodic sync is due");
        const qsizetype provenanceAsked = commandsCalled().count("provenance");
        p.captureTable->selectRow(row);
        check(waitUntil([&]{ return settled(p) && p.provenanceNote->text().startsWith("Registered in the catalog used before"); }) &&
              commandsCalled().count("provenance") == provenanceAsked && p.captureFacts->toPlainText().contains("in the catalog used before: d/101") &&
              p.captureFacts->toPlainText().contains("in the catalog used before: r6g 0.97"), "the catalog is not asked about a record id of the previous catalog");
        p.sync(false);
        check(waitUntil([&]{ return settled(p) && !p.catalogChanged(); }), "the sync binds the rows to the catalog in use");
        row = rowOf(p, "TEST-PLATE-A", 1);
        check(p.captureTable->horizontalHeaderItem(3)->text() == "State (demo)" && cellText(p, row, 4) == "d/101" &&
              waitUntil([&]{ return settled(p) && commandsCalled().count("provenance") == provenanceAsked + 1; }) &&
              p.captureFacts->toPlainText().contains("d/101") && !p.captureFacts->toPlainText().contains("catalog used before"),
              "then the rows are this catalog's again and the provenance is asked for");
    }

    static void reviewTab() {
        resetStandIn();
        MainWindow w;
        w.show();
        DataPanels& p = *w.dataPanels;
        check(firstRefresh(p) && waitUntil([&]{ return !p.reviewShownId.isEmpty() && settled(p); }), "the review list and its first item");
        check(p.reviewTable->rowCount() == 3 && p.reviewListTitle->text() == "WAITING FOR REVIEW (3)" && w.display.tabs->tabText(2) == "Review (3)", "what waits");
        check(p.reviewShownId == "d/208" && optionsOf("review-show") == QStringList({"--prediction", "d/208"}) && p.reviewStack->currentIndex() == 1,
              "the newest one is selected and shown");
        check(p.reviewSeries->count() == 2048 - 90 && p.reviewSeries->at(0).x() > 200 && p.reviewAxisX->titleText().startsWith("Raman shift") &&
              p.spectrumTitle->text() == "WHAT THE CLASSIFIER SAW" && p.reviewChartView->chart()->title() == uiJoin({"TEST-MB-01 (simulated)", "single"}),
              "the spectrum the classifier saw: without the first pixels, which the model does not see");
        check(p.scoreBars->classes() == QStringList({"methylene_blue", "r6g", "di_water", "adenine"}) && p.scoresTitle->text() == "SCORES (THRESHOLD 0.80)", "scores, highest first");
        check(p.reviewReason->text() == "<b>Low pure-class confidence.</b> The top score, 0.74 for methylene_blue, is below the threshold 0.80.",
              "why it needs review: the classifier's reason, then its numbers");
        check(p.reviewProvenance->rowCount() == 4 && p.reviewProvenance->topLevelItem(0)->text(1) == "d/208" && p.reviewProvenance->topLevelItem(0)->font(0).bold() &&
              p.reviewProvenance->topLevelItem(0)->child(0)->text(1) == "d/108", "the provenance chain of the prediction");
        QStringList classes;
        for (int i = 0; i < p.relabelCombo->count(); ++i) classes << p.relabelCombo->itemText(i);
        check(classes == QStringList({"r6g", "methylene_blue", "adenine", "di_water", "unknown"}) && p.relabelCombo->currentText() == "methylene_blue",
              "the classes of review-show, and unknown");
        check(p.reviewProblems->isHidden(), "nothing the data layer could not show");
        check(p.reviewerLabel->text() == "Reviewer: handshake tests" && p.confirmButton->isEnabled() && p.relabelButton->isEnabled() && p.rejectButton->isEnabled(),
              "the reviewer is the Operator field");
        check(p.confirmButton->text() == "Confirm methylene_blue" && p.confirmButton->toolTip() == "Record methylene_blue as the class of this spectrum." &&
              p.rejectButton->text() == "Mark unusable", "the buttons say what they record: Confirm stores the top class (PROTOCOL.md section 6)");

        w.operatorEdit->setText("   ");
        check(!p.confirmButton->isEnabled() && !p.relabelButton->isEnabled() && !p.rejectButton->isEnabled() && p.reviewerLabel->text().contains("fill in Operator"),
              "no review without a reviewer");
        p.submitReview("confirm");
        spin(200);
        check(!commandsCalled().contains("review-set"), "not even when asked directly");
        w.operatorEdit->setText(" Reviewer One ");

        showReviewItem(p, "d/203");
        check(p.reviewReason->text() == "<b>Rejected by the classifier.</b> Low raw signal." && p.scoreBars->classes().isEmpty(),
              "a spectrum the classifier rejected");
        check(p.spectrumTitle->text() == "RAW SPECTRUM (NOT CLASSIFIED)" && p.reviewSeries->count() == 2048 &&
              p.reviewAxisY->titleText() == "Intensity",
              "no model was given it, although review-show has a curve for it: the raw spectrum is shown and called raw");
        check(p.confirmButton->text() == "Confirm: unusable" && p.confirmButton->isEnabled() && !p.rejectButton->isEnabled() &&
              p.rejectButton->toolTip().contains("Confirm records"), "rejected by the classifier: Confirm records unusable, and Mark unusable, which would do the same, is off");
        p.submitReview("reject");
        spin(200);
        check(!commandsCalled().contains("review-set"), "not even when asked directly");
        check(p.reviewAxisY->tickInterval() >= 1 && p.reviewAxisY->tickInterval() == std::floor(p.reviewAxisY->tickInterval()),
              "counts are marked at whole numbers");
        {   // A flat spectrum spans a few counts only: no two marks may carry the same label.
            QChart chart;
            auto* axis = new QValueAxis;
            chart.addAxis(axis, Qt::AlignLeft);
            axis->setLabelFormat("%.0f");
            useRoundTicks(&chart, axis, Qt::Vertical);
            axis->setRange(15, 17);
            check(axis->tickInterval() == 1, "whole numbers apart where the labels show no decimals");
            axis->setLabelFormat("%.2f");
            axis->setRange(0, 1);
            check(axis->tickInterval() < 1, "fractions where the labels show them");
        }
        const auto reasonOf = [](const QString& outcome, const QJsonValue& reason, const QJsonValue& threshold) {
            return DataPanels::reviewReasonText(QJsonObject{{"prediction", QJsonObject{{"outcome", outcome}, {"top_class", "r6g"}, {"confidence", 0.61}}},
                                                            {"reason", reason}, {"threshold", threshold}});
        };
        check(reasonOf("rejected", "no dark spectrum", 0.8) == "<b>Rejected by the classifier.</b> No dark spectrum." &&
              reasonOf("rejected", QJsonValue(), 0.8) == "<b>Rejected by the classifier.</b> The spectrum could not be used.",
              "the classifier's phrase as a sentence, or a plain one without it");
        check(reasonOf("low_confidence", QJsonValue(), QJsonValue()) == "<b>Low confidence.</b> The top score, 0.61 for r6g, is below the threshold.",
              "offline neither the reason nor the threshold is known");
        // Droplet rims of a 2D scan score just under the threshold: rounded to two places they would equal it.
        const auto belowOf = [](double confidence) {
            return DataPanels::reviewReasonText(QJsonObject{{"prediction", QJsonObject{{"outcome", "low_confidence"}, {"top_class", "methylene_blue"}, {"confidence", confidence}}},
                                                            {"reason", "Low pure-class confidence"}, {"threshold", 0.8}});
        };
        check(belowOf(0.797) == "<b>Low pure-class confidence.</b> The top score, 0.797 for methylene_blue, is below the threshold 0.80." &&
              belowOf(0.79996).contains(" 0.799 for") && belowOf(0.74).contains(" 0.74 for"), "a score just under the threshold is never shown equal to it");
        check(DataText::score(0.7951, 0.8) == "0.795" && DataText::score(0.8, 0.8) == "0.80" && DataText::score(0.95, 0.8) == "0.95" &&
              DataText::score(0.79, 0.8) == "0.79", "two places wherever they tell the score from the threshold");
        // Where no threshold is at hand (list items, the review list offline) the outcome says that the score is below it.
        const QJsonObject uncertain{{"outcome", "low_confidence"}, {"label", "UNKNOWN"}, {"top_class", "r6g"}, {"confidence", 0.797}};
        check(DataText::prediction(uncertain) == "r6g? 0.797 (uncertain)" && DataText::modelLabel(uncertain) == "UNKNOWN (r6g 0.797)" &&
              DataText::predictionCell(QJsonObject{{"prediction", uncertain}, {"review", QJsonObject{{"status", "relabelled"}, {"label", "adenine"}}}}) ==
                  "adenine, relabelled (model: r6g 0.797)" &&
              DataPanels::reviewReasonText(QJsonObject{{"prediction", uncertain}, {"reason", QJsonValue()}, {"threshold", QJsonValue()}}) ==
                  "<b>Low confidence.</b> The top score, 0.797 for r6g, is below the threshold.",
              "a low-confidence score is never rounded up to a threshold the text cannot name");
        check(DataText::lowScore(0.7999) == "0.799" && DataText::lowScore(0.72556) == "0.725" && DataText::lowScore(0.72) == "0.72" &&
              DataText::lowScore(0.6149) == "0.61" && DataText::prediction(QJsonObject{{"outcome", "confident"}, {"label", "r6g"}, {"top_class", "r6g"},
                                                                                     {"confidence", 0.8049}}) == "r6g 0.80",
              "a third place only where two would round up; a score at or above the threshold keeps two places");

        showReviewItem(p, "d/202");
        control({{"review-set", QJsonObject{{"mode", "error"}, {"error", "The record is locked"}}}});
        p.rejectButton->click();
        check(waitUntil([&]{ return settled(p) && p.reviewResult->tone() == UiTone::Danger; }), "a failed review");
        check(optionsOf("review-set").mid(0, 4) == QStringList({"--prediction", "d/202", "--decision", "reject"}) &&
              p.reviewResult->text().startsWith("The review was not recorded: The record is locked") && p.reviewTable->rowCount() == 3 &&
              p.reviewShownId == "d/202" && p.rejectButton->isEnabled(), "Mark unusable asks for reject; the failure is said, and the item stays");
        control({});

        showReviewItem(p, "d/208");
        p.commentEdit->setText("-weak, but the peaks are there");
        p.confirmButton->click();
        check(!p.confirmButton->isEnabled(), "one review at a time");
        check(waitUntil([&]{ return settled(p) && p.reviewItems.size() == 2; }), "confirmed; the list is read again");
        check(optionsOf("review-set") == QStringList({"--prediction", "d/208", "--decision", "confirm", "--reviewer=Reviewer One",
                                                     "--comment=-weak, but the peaks are there"}), "decision, reviewer and comment as given");
        check(p.reviewResult->tone() == UiTone::Ok && p.reviewResult->text() == "Recorded in the demo catalog: d/208 confirmed as methylene_blue." &&
              p.commentEdit->text().isEmpty(), "the result names the class that was recorded");
        check(w.display.tabs->tabText(2) == "Review (2)" && p.reviewListTitle->text() == "WAITING FOR REVIEW (2)", "one less waits");
        check(cellText(p, rowOf(p, "TEST-MB-01", 1), 3) == "Reviewed" && cellText(p, rowOf(p, "TEST-MB-01", 1), 6) == "confirmed: methylene_blue" &&
              cellText(p, rowOf(p, "TEST-MB-01", 1), 5) == "methylene_blue 0.74, confirmed", "and the Data tab shows the review, with the class");
        check(waitUntil([&]{ return p.reviewShownId == "d/203" && settled(p); }), "the next one is shown");

        showReviewItem(p, "d/202");
        check(p.relabelCombo->currentText() == "r6g", "the top class is offered first");
        p.relabelCombo->setCurrentText("adenine");
        p.relabelButton->click();
        check(waitUntil([&]{ return settled(p) && p.reviewItems.size() == 1; }), "relabelled");
        check(optionsOf("review-set") == QStringList({"--prediction", "d/202", "--decision", "relabel", "--label", "adenine", "--reviewer=Reviewer One"}),
              "the chosen class; no comment when none was written");
        check(cellText(p, rowOf(p, "TEST-PLATE-A", 2), 6) == "relabelled: adenine" &&
              cellText(p, rowOf(p, "TEST-PLATE-A", 2), 5) == "adenine, relabelled (model: r6g 0.61)", "the new label leads in the Data tab, the model's answer follows");
        check(p.reviewResult->text() == "Recorded in the demo catalog: d/202 relabelled as adenine.", "the result names the new class");

        // Offline: what only the catalog knows is missing, and that is said; the review is kept on this Mac.
        p.setOffline(true);
        check(waitUntil([&]{ return settled(p) && p.catalogOffline(); }), "offline");
        p.reviewShownId.clear();
        p.reviewSelectionChanged();
        check(waitUntil([&]{ return p.reviewShownId == "d/203" && settled(p); }), "the item is read again, offline");
        check(!p.reviewProblems->isHidden() && p.reviewProblems->text().contains("cannot be reached") && p.scoresTitle->text() == "SCORES" &&
              p.scoreBars->classes().isEmpty() && p.reviewProvenance->rowCount() == 0 && p.reviewSeries->count() == 2048,
              "offline: no scores and no provenance, the spectrum from this Mac, and the reason why");
        const qsizetype shownBefore = commandsCalled().count("review-show");
        p.setOffline(false);
        check(waitUntil([&]{ return settled(p) && !p.catalogOffline() && p.reviewProvenance->rowCount() == 4; }),
              "online again: the item shown without the catalog is asked for again");
        check(p.reviewShownId == "d/203" && p.reviewProblems->isHidden() && p.scoresTitle->text() == "SCORES (THRESHOLD 0.80)" &&
              commandsCalled().count("review-show") == shownBefore + 1, "with what only the catalog knows, and once");
        p.refresh();
        check(waitUntil([&]{ return settled(p); }) && commandsCalled().count("review-show") == shownBefore + 1,
              "an item shown with the catalog is not asked for again by a refresh");
        p.setOffline(true);
        check(waitUntil([&]{ return settled(p) && p.catalogOffline(); }), "offline again");
        // Read again while the catalog is away, as the next item is after a review made offline: the
        // last review then empties a list whose item was shown without the catalog.
        p.reviewShownId.clear();
        p.reviewSelectionChanged();
        check(waitUntil([&]{ return p.reviewShownId == "d/203" && settled(p); }) && p.reviewShownWithoutCatalog, "the last item, shown without the catalog");

        p.confirmButton->click();
        check(waitUntil([&]{ return settled(p) && p.reviewItems.isEmpty(); }), "confirmed unusable while offline");
        check(p.reviewResult->text() == "Kept on this Mac until the demo catalog is online: d/203 confirmed as unusable.",
              "kept, not sent; in the words of the demo");
        check(p.reviewStack->currentIndex() == 0 && p.reviewEmpty->text().endsWith("Nothing is waiting for review.") &&
              p.reviewEmpty->text().contains("d/203 confirmed as unusable") && w.display.tabs->tabText(2) == "Review",
              "the list is empty; the last result stays readable");
        check(cellText(p, rowOf(p, "TEST-PLATE-A", 3), 6) == "confirmed: unusable (not sent yet)" && cellText(p, rowOf(p, "TEST-PLATE-A", 3), 5) == "unusable, confirmed",
              "the Data tab says that the review has not been sent");
        check(p.status["pending"].toInt() == 3 && w.display.data->value() == uiJoin({"demo", "offline", "3 to sync", "1 stuck"}) &&
              p.summaryLabel->text().contains("1 review kept on this Mac"),
              "a review that waits counts as one to sync, next to the two captures a sync can move; the one without its fields needs a person");
        check(!p.confirmButton->isEnabled() && p.reviewProvenance->rowCount() == 0, "nothing to act on");

        // Back online, a sync sends the review: from then on the tab does not say that it is kept on this Mac (D4).
        p.setOffline(false);
        check(waitUntil([&]{ return settled(p) && !p.catalogOffline(); }), "online again");
        check(p.reviewResult->text().contains("Kept on this Mac"), "until a sync has sent it, the review is still said to be kept");
        check(p.reviewStack->currentIndex() == 0 && p.reviewEmpty->text().contains("Kept on this Mac") && !p.reviewEmpty->text().contains("could not be shown"),
              "an empty list is not an item shown without the catalog that has to be asked for again");
        p.syncButton->click();
        check(waitUntil([&]{ return settled(p) && commandsCalled().contains("sync") && p.status["reviews_queued"].toInt() == 0; }), "the sync sends it");
        const QString sentAt = DataText::localTime(p.status["last_sync_utc"].toString(), "HH:mm");
        check(p.activityLabel->text().contains("1 review sent"), "the sync result counts it");
        check(p.reviewResult->text() == "Recorded in the demo catalog at " + sentAt + ": d/203 confirmed as unusable." &&
              p.reviewStack->currentIndex() == 0 && !p.reviewEmpty->text().contains("Kept on this Mac") &&
              p.reviewEmpty->text().contains("Recorded in the demo catalog at " + sentAt), "the Review tab no longer says that the review is kept on this Mac");
        check(cellText(p, rowOf(p, "TEST-PLATE-A", 3), 6) == "confirmed: unusable" && !p.summaryLabel->text().contains("kept on this Mac"), "nor does the Data tab");
        const qsizetype shownSoFar = commandsCalled().count("review-show");
        p.refresh();
        check(waitUntil([&]{ return settled(p); }) && commandsCalled().count("review-show") == shownSoFar &&
              p.reviewEmpty->text().contains("Recorded in the demo catalog at " + sentAt), "a later refresh asks for no item and keeps the line");
        for (const QJsonObject& call : calls())
            if (call["command"].toString() == "review-show")
                check(!argumentsOf(call).mid(3).contains(QString()), "no prediction is asked for without its id");
    }

    static void syncAfterCaptures() {
        resetStandIn();
        MainWindow w;
        w.show();
        DataPanels& p = *w.dataPanels;
        check(firstRefresh(p), "first status and lists");
        check(!commandsCalled().contains("sync"), "showing the window reads the status; it does not sync");
        confirmLaserOn(w);
        click(w, "Start Z scan then 2D scan");
        bool duringScan = false;
        check(waitUntil([&]{
            if (w.scanActive && commandsCalled().contains("sync")) duringScan = true;
            return w.lastCompletedPoint == 3 && !w.scanActive;
        }), "scan completes with the data layer installed");
        check(!duringScan, "no sync while the scan runs");
        check(waitUntil([&]{ return commandsCalled().count("sync") == 1 && settled(p); }), "one sync when the scan has ended");
        check(optionsOf("sync").isEmpty(), "a sync that starts by itself is a plain one");
        click(w, "Capture Spectrum");
        check(QFile::exists(w.singleCaptureDir + "/point_0001.json"), "single capture saved");
        check(waitUntil([&]{ return commandsCalled().count("sync") == 2 && settled(p); }), "one sync after a single capture");
        start(w);
        duringCapture(w, "Stop");
        check(waitUntil([&]{ return !w.scanActive && !w.isCollecting; }), "stop completes");
        check(waitUntil([&]{ return commandsCalled().count("sync") == 3 && settled(p); }), "a stopped scan is synced too: its saved points count");
        spin(700);
        check(commandsCalled().count("sync") == 3, "and nothing syncs in between");
        const QString logs = w.ramanOutput->toPlainText() + w.laserOutput->toPlainText();
        check(!logs.contains("sync", Qt::CaseInsensitive) && !logs.contains("demo", Qt::CaseInsensitive) && !logs.contains("data layer", Qt::CaseInsensitive),
              "the data layer writes nothing into the controller's two logs");
        check(QDir(w.singleCaptureDir).entryList(QDir::Files | QDir::Hidden, QDir::Name) == QStringList({"point_0001.json", "session.json"}),
              "and nothing into a session folder");
    }

    // The periodic sync is due all the time here (short interval, captures waiting): none may start
    // between the start of a scan and its end.
    static void noSyncDuringScan() {
        resetStandIn();
        MainWindow w;
        w.show();
        DataPanels& p = *w.dataPanels;
        check(firstRefresh(p), "first status and lists");
        p.autoSync.setInterval(100);
        check(waitUntil([&]{ return commandsCalled().count("sync") >= 2; }), "while nothing is acquired the periodic sync runs");
        confirmLaserOn(w);
        w.lineAxisCombo->setCurrentIndex(w.lineAxisCombo->findData("1d-y"));
        click(w, "1D Scan");
        w.refreshStatusDisplay();
        check(w.scanActive && p.acquisitionBusy, "the panels know that a scan runs");
        check(waitUntil([&]{ return !p.syncRunning; }), "a sync that had started before the scan ends");
        const qsizetype before = commandsCalled().count("sync");
        qsizetype during = before;
        check(waitUntil([&]{
            if (w.scanActive) during = commandsCalled().count("sync");
            return w.lastCompletedPoint == 13 && !w.scanActive;
        }, 30000), "line scan completes");
        check(during == before, "no sync started while the scan ran, although one was due all the time");
        check(waitUntil([&]{ return commandsCalled().count("sync") > before; }), "the sync that was due starts when the scan has ended");
    }

    // Every 30 s while captures wait and no scan runs; here with a short interval.
    static void periodicSync() {
        resetStandIn();
        QLineEdit reviewer;
        auto* panels = new DataPanels(home(), "unused-fallback", &reviewer, nullptr);
        QWidget* dataPage = panels->dataPage();
        QWidget* reviewPage = panels->reviewPage();
        const auto cleanUp = qScopeGuard([&]{ delete panels; delete dataPage; delete reviewPage; });
        DataPanels& p = *panels;
        check(p.autoSync.interval() == 30000, "the interval of the periodic sync");
        p.autoSync.setInterval(150);
        spin(500);
        check(calls().isEmpty() && !p.autoSync.isActive(), "nothing runs before start()");
        p.setAcquisitionBusy(true);
        p.start();
        // Of the three captures that wait, one lacks its fields: only import-session can move it.
        check(firstRefresh(p) && p.waiting() == 3 && p.syncWork() == 2 && p.toClassify() == 1, "captures wait");
        spin(700);
        check(!commandsCalled().contains("sync") && p.syncDue, "no sync starts by itself while a scan, a capture or a laser command is under way");
        p.setAcquisitionBusy(false);
        check(p.syncRunning && !p.syncDue, "the sync that was due starts as soon as the acquisition is over");
        check(waitUntil([&]{ return commandsCalled().count("sync") >= 2; }), "periodic sync while captures wait");
        p.setAcquisitionBusy(true);
        p.captureFinished();
        check(p.syncDue, "a finished capture waits too while the next acquisition is already under way");
        p.setAcquisitionBusy(false);
        check(waitUntil([&]{ return p.syncWork() == 0 && settled(p); }, 20000), "what a sync can move on has been moved");
        p.classify();
        check(waitUntil([&]{ return p.toClassify() == 0 && settled(p); }, 20000), "and classified");
        qsizetype syncs = commandsCalled().count("sync");
        spin(900);
        check(p.waiting() == 1 && commandsCalled().count("sync") == syncs,
              "a capture that waits for import-session alone starts no periodic sync: no sync can move it");
        // Supply the fields of the one capture that cannot register; the syncs then empty the queue.
        p.importFolder(home() + "/scan_sessions/2d_scan_20260924_141500_001", "S-OLD", "operator", "lab");
        check(waitUntil([&]{ return p.waiting() == 0 && settled(p); }, 20000), "everything is registered");
        check(p.pillState().text == uiJoin({"demo", "synced"}) && p.pillState().tone == UiTone::Ok && p.toClassify() > 0,
              "nothing waits on this Mac any more; the classifier has not run yet");
        syncs = commandsCalled().count("sync");
        check(waitUntil([&]{ return commandsCalled().count("sync") >= syncs + 2; }), "the periodic sync goes on in the demo: predictions may arrive");
        // The settings name another catalog than the one the rows describe: what the rows count says
        // nothing about it, so one periodic sync binds them to it.
        p.autoSync.stop();
        control({{"catalog_changed", true}});
        p.refresh();
        check(waitUntil([&]{ return settled(p) && p.catalogChanged(); }) && p.syncWork() == 0 && p.periodicSyncDue(), "the catalog settings changed");
        syncs = commandsCalled().count("sync");
        p.autoSync.start();
        check(waitUntil([&]{ return commandsCalled().count("sync") >= syncs + 1 && settled(p) && !p.catalogChanged(); }),
              "a periodic sync binds the rows to the catalog in use");
        control({});
        p.refresh();
        check(waitUntil([&]{ return settled(p) && !p.catalogChanged(); }), "the rows are this catalog's");
        p.classify();
        check(waitUntil([&]{ return p.toClassify() == 0 && p.waiting() == 0 && settled(p); }, 20000), "everything is classified");
        spin(300);
        syncs = commandsCalled().count("sync");
        spin(900);
        check(commandsCalled().count("sync") == syncs, "no periodic sync when a sync has nothing left to move on");
        // The data layer stops answering: its status is asked for again at the same interval.
        control({{"status", QJsonObject{{"mode", "error"}}}});
        p.refresh();
        check(waitUntil([&]{ return !p.statusProblem.isEmpty() && settled(p); }), "failed status");
        const qsizetype asked = commandsCalled().count("status");
        check(waitUntil([&]{ return commandsCalled().count("status") >= asked + 2; }), "the status is asked for again");
        control({});
        check(waitUntil([&]{ return p.statusProblem.isEmpty() && settled(p); }), "and the tab recovers by itself");
    }

    static void headlessWindow() {
        resetStandIn();
        MainWindow w;     // never shown: the way the acquisition tests and a headless run use the class
        start(w);
        check(waitUntil([&]{ return w.lastCompletedPoint == 3 && !w.scanActive; }), "scan completes");
        spin(700);
        check(calls().isEmpty() && !w.dataPanels->started() && w.dataPanels->dataLayer->running() == 0 && !w.display.timer->isActive(),
              "a window that is never shown starts no data-layer process, also after a scan");
        check(w.display.data->value() == "checking..." && w.display.lastEvent->text() == uiJoin({"Scan finished", "3 points saved"}),
              "its event line still follows the logs; its data status stays as it began");
    }

    static void studioFromTheWindow() {
        resetStandIn();
        MainWindow w;
        w.show();
        DataPanels& p = *w.dataPanels;
        check(firstRefresh(p), "first status and lists");
        QList<QUrl> opened;
        p.studio->openUrl = [&opened](const QUrl& url) { opened << url; return true; };      // no browser in a test
        QPushButton* studio = button(w, "Open Simulation Studio");
        {   // The server must run in HOME wherever the controller was started from.
            const QString startedFrom = QDir::currentPath();
            const auto backToStart = qScopeGuard([startedFrom]{ QDir::setCurrent(startedFrom); });
            check(QDir::setCurrent(QDir::tempPath()), "another working folder for the moment of the start");
            studio->click();
        }
        check(!studio->isEnabled() && p.studio->starting(), "the button waits for the server's address");
        check(waitUntil([&]{ return opened.size() == 1; }, 20000), "the page is opened once the server has named its address");
        check(opened[0] == QUrl("http://127.0.0.1:8765/") && studio->isEnabled() && studio->toolTip().contains("http://127.0.0.1:8765/") &&
              w.statusBar()->currentMessage() == "Simulation Studio opened: http://127.0.0.1:8765/", "the address from the first output line");
        const QJsonObject call = lastCall("studio");
        check(argumentsOf(call) == QStringList({"--home", home(), "studio", "--no-open", "--idle-exit", "900"}) &&
              QFileInfo(call["cwd"].toString()).canonicalFilePath() == QFileInfo(home()).canonicalFilePath() &&
              call["pythonpath"].toString().startsWith(home() + "/data_layer"),
              "started like every data-layer command; the page is opened from here only; the server ends by itself when it is no longer used");
        const qint64 server = p.studio->serverPid();
        check(server > 0 && StudioLauncher::processAlive(server) && readFile(home() + "/fake_studio.pid").trimmed().toLongLong() == server,
              "the server is a process of its own and keeps running");
        studio->click();
        check(waitUntil([&]{ return opened.size() == 2; }) && commandsCalled().count("studio") == 1 && p.studio->serverPid() == server,
              "pressing again opens the page of the running server");
        // The server has gone away since: the next press starts a new one.
        check(AcquisitionProtocol::publish(home() + "/fake_studio_stop", "stop"), "tell the stand-in server to end");
        check(waitUntil([&]{ return !StudioLauncher::processAlive(server); }), "server ended");
        QFile::remove(home() + "/fake_studio_stop");
        studio->click();
        check(waitUntil([&]{ return opened.size() == 3; }, 20000) && commandsCalled().count("studio") == 2 && p.studio->serverPid() != server, "a new server");
        const qint64 second = p.studio->serverPid();
        check(AcquisitionProtocol::publish(home() + "/fake_studio_stop", "stop") && waitUntil([&]{ return !StudioLauncher::processAlive(second); }), "second server ended");
    }

    static void studioFailures() {
        resetStandIn();
        DataLayerClient client(home(), "unused-fallback");
        StudioLauncher launcher(&client);
        bool pageOpens = true;
        launcher.openUrl = [&pageOpens](const QUrl&) { return pageOpens; };
        qint64 server = 0;
        const auto open = [&]() {
            bool answered = false, ok = false;
            QString text;
            launcher.open([&](bool opened, const QString& said) { answered = true; ok = opened; text = said; });
            check(waitUntil([&]{ if (launcher.serverPid() > 0) server = launcher.serverPid(); return answered; }, 30000), "the launcher always answers");
            return qMakePair(ok, text);
        };
        control({{"studio", QJsonObject{{"mode", "error"}, {"error", "The Studio page is missing"}, {"detail", "studio/index.html"}}}});
        auto result = open();
        check(!result.first && result.second == "The Simulation Studio did not start: The Studio page is missing (studio/index.html)" && launcher.serverPid() == 0,
              "a server that reports why it cannot start");
        control({{"studio", QJsonObject{{"mode", "silent"}}}});
        result = open();
        check(!result.first && result.second.startsWith("The Simulation Studio ended before it gave its address") &&
              result.second.contains("ending without an answer"), "a server that ends without an address: the end of its log");
        control({{"studio", QJsonObject{{"mode", "slow"}, {"seconds", 30}}}});
        launcher.answerTimeoutMs = 700;
        bool second = false;
        QString secondText;
        QTimer::singleShot(200, &launcher, [&]{ launcher.open([&](bool ok, const QString& said) { second = !ok; secondText = said; }); });
        result = open();
        check(second && secondText == "The Simulation Studio is still starting.", "a second press while the first is still starting");
        check(!result.first && result.second.contains("gave no address within") && waitUntil([&]{ return server > 0 && !StudioLauncher::processAlive(server); }),
              "a server that never answers is stopped again");
        launcher.answerTimeoutMs = 20000;
        control({});
        pageOpens = false;
        result = open();
        check(!result.first && result.second == "The Simulation Studio runs at http://127.0.0.1:8765/, but the page could not be opened in a browser." &&
              StudioLauncher::processAlive(launcher.serverPid()), "the browser does not open: the address is still given");
        check(AcquisitionProtocol::publish(home() + "/fake_studio_stop", "stop") && waitUntil([&]{ return !StudioLauncher::processAlive(launcher.serverPid()); }), "server ended");
        QTemporaryDir folder(home() + "/no_package_XXXXXX");
        DataLayerClient absent(folder.path(), UI_TEST_PYTHON);
        StudioLauncher none(&absent);
        bool said = false;
        none.open([&](bool ok, const QString& text) { said = !ok && text.contains("not installed"); });
        check(said && commandsCalled().count("studio") == 4, "without the data layer nothing is started");
    }

    // Nothing is cut off or pushed out of its place. scrolling: a column that is allowed to scroll at
    // this size; what stands in it may lie outside the part in view.
    static void fits(MainWindow& w, const QString& where, const QScrollArea* scrolling = nullptr) {
        const auto fail = [&](const QString& what) { throw std::runtime_error((what + " (" + where + ")").toStdString()); };
        for (QWidget* control : w.findChildren<QWidget*>()) {
            if (!control->isVisible()) continue;
            const auto* pushButton = qobject_cast<QPushButton*>(control);
            if (!pushButton && !qobject_cast<QLineEdit*>(control) && !qobject_cast<QComboBox*>(control)) continue;
            if (pushButton && pushButton->fontMetrics().horizontalAdvance(pushButton->text()) + 8 > pushButton->width()) fail("button text cut: " + pushButton->text());
            if (scrolling && scrolling->isAncestorOf(control)) continue;
            const QRect area(control->mapTo(&w, QPoint(0, 0)), control->size());
            for (QWidget* parent = control->parentWidget(); parent && parent != &w; parent = parent->parentWidget())
                if (!QRect(parent->mapTo(&w, QPoint(0, 0)), parent->size()).contains(area))
                    fail("control pushed out of its place: " + (pushButton ? pushButton->text() : control->objectName()));
        }
        const QList<StatusPill*> pills{w.display.simulation, w.display.backend, w.display.laser, w.display.scan, w.display.data};
        for (StatusPill* pill : pills) {
            if (!pill->isVisible()) continue;
            if (!pill->parentWidget()->rect().contains(pill->geometry())) fail("status outside the header: " + pill->value());
            if (pill->width() < pill->minimumSizeHint().width()) fail("status squeezed: " + pill->value());
            for (StatusPill* other : pills)
                if (other != pill && other->isVisible() && other->geometry().intersects(pill->geometry())) fail("statuses overlap: " + pill->value());
        }
        for (StatusPill* whole : {w.display.backend, w.display.laser, w.display.scan})
            if (whole->width() < whole->sizeHint().width()) fail("status cut: " + whole->value());
    }
    static void nothingClipped() {
        resetStandIn();
        MainWindow w;
        w.show();
        DataPanels& p = *w.dataPanels;
        check(firstRefresh(p) && waitUntil([&]{ return !p.reviewShownId.isEmpty() && settled(p); }), "tabs filled");
        QScrollArea* leftColumn = nullptr;
        for (QWidget* parent = w.sampleIdEdit->parentWidget(); parent && !leftColumn; parent = parent->parentWidget())
            leftColumn = qobject_cast<QScrollArea*>(parent);
        check(leftColumn, "the left column");
        // The longest texts the controls ever carry; methylene_blue is the longest class of the model.
        w.display.laserButton->setText("Confirm Laser OFF");
        w.monitorButton->setText("Monitor: ON (Focus)");
        p.confirmButton->setText("Confirm methylene_blue");
        for (const QSize& size : {QSize(1440, 880), QSize(1180, 720)}) {
            w.resize(size);
            for (int tab = 0; tab < 3; ++tab) {
                w.display.tabs->setCurrentIndex(tab);
                spin(200);
                const QString where = QString("%1 x %2, tab %3").arg(size.width()).arg(size.height()).arg(w.display.tabs->tabText(tab));
                check(w.size() == size, "the window takes the size");
                check(leftColumn->verticalScrollBar()->maximum() == 0, "the left column fits without scrolling");
                fits(w, where);
                check(w.chartView->height() >= 180 && w.display.tabs->currentWidget()->height() >= 150, "the chart and the tab area both keep a usable height");
            }
        }
        // Below the smallest supported size, down to the smallest the window allows: the left column
        // scrolls instead of being cut off, and everything else still has its place.
        w.resize(w.minimumSize());
        for (int tab = 0; tab < 3; ++tab) {
            w.display.tabs->setCurrentIndex(tab);
            spin(200);
            check(w.size() == w.minimumSize() && w.minimumSize() == QSize(1080, 660), "the smallest size the window allows");
            check(leftColumn->verticalScrollBar()->maximum() > 0 && !leftColumn->horizontalScrollBar()->isVisible(), "the left column scrolls");
            fits(w, QString("smallest size, tab %1").arg(w.display.tabs->tabText(tab)), leftColumn);
        }
        w.resize(1180, 720);
        // The worst the header has to carry at once, at the smallest supported size.
        w.display.timer->stop();
        w.display.simulation->setState(UiTone::Danger, "LASER IS REAL (motors simulated)", QString(), true);
        w.display.backend->setState(UiTone::Danger, "stopped", QString(), true);
        w.display.laser->setState(UiTone::Danger, "UNCONFIRMED", QString(), true);
        w.display.scan->setState(UiTone::Danger, "FAILED at 12 of 13", QString(), true);
        w.display.data->setState(UiTone::Warn, uiJoin({"demo", "offline", "128 to sync", "12 stuck"}));
        spin(200);
        check(w.size() == QSize(1180, 720), "the longest header texts do not widen the window");
        fits(w, "longest header texts");
        check(w.display.simulation->toolTip().startsWith("LASER IS REAL (motors simulated)") && w.display.data->toolTip().contains(uiJoin({"128 to sync", "12 stuck"})),
              "a status that had to give way keeps its whole text in the tool tip");
    }

    static void logsFollowTheirEnd() {
        MainWindow w;
        w.show();
        w.resize(1180, 720);
        spin(100);
        QScrollBar* bar = w.laserOutput->verticalScrollBar();
        for (int i = 0; i < 200; ++i) w.laserOutput->append(QString("[Backend] line %1").arg(i));
        spin(50);
        check(bar->maximum() > 0 && bar->value() == bar->maximum(), "the log shows its newest line");
        w.display.tabs->setCurrentIndex(1);
        for (int i = 0; i < 50; ++i) w.laserOutput->append(QString("[Backend] while the Data tab was open %1").arg(i));
        w.display.tabs->setCurrentIndex(0);
        spin(100);
        check(bar->value() == bar->maximum(), "also after lines arrived while another tab was open");
        bar->setValue(0);
        w.laserOutput->append("[Backend] one more");
        spin(50);
        check(bar->value() == 0 && bar->maximum() > 0, "a log the operator scrolled back in stays where it is");
        check(w.display.lastEvent->text() == "Backend started", "and none of these backend lines is an event");
    }

    static void appearances() {
        resetStandIn();
        MainWindow w;
        w.show();
        DataPanels& p = *w.dataPanels;
        check(firstRefresh(p), "first status and lists");
        const int row = rowOf(p, "TEST-PLATE-A", 2);
        for (const UiTheme& theme : {UiTheme::darkAppearance(), UiTheme::light()}) {
            w.applyTheme(theme);
            spin(50);
            check(uiTheme().dark == theme.dark && w.palette().color(QPalette::Window) == theme.window, "the appearance in use");
            check(w.sampleIdEdit->palette().color(QPalette::Text) == theme.text && w.laserOutput->palette().color(QPalette::Text) == theme.text,
                  "the palette reaches the widgets, also after an earlier appearance");
            check(w.rtChart->backgroundBrush().color() == theme.surface && w.rtSeries->pen().color() == theme.series &&
                  w.chartView->backgroundBrush().color() == theme.surface, "the chart");
            check(p.captureTable->item(row, 3)->foreground().color() == theme.warnInk && p.reviewChartView->chart()->backgroundBrush().color() == theme.surface,
                  "colours that were set on items are set again");
            check(w.styleSheet() == theme.styleSheet(), "the style sheet");
        }
    }
};

int main(int argc, char** argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    // Only the emulators next to this program are started; both variables keep even real helper
    // programs simulated, and they are what the SIMULATION badge of the header reads.
    qputenv("RAMAN_BACKEND_SIMULATE", "1");
    qputenv("RAMAN_MOTOR_SIMULATE", "1");
    qputenv("RAMAN_CATALOG_PYTHON", UI_TEST_PYTHON);
    QApplication app(argc, argv);
    useUiBaseStyle();
    struct Test { const char* name; void (*run)(); };
    const Test tests[] = {
        {"client / one JSON object / reported failure / unreadable output", HandshakeTest::replyParsing},
        {"client / interpreter lookup: variable, catalog_python.txt, analysis interpreter", HandshakeTest::interpreterLookup},
        {"client / arguments, working directory, PYTHONPATH", HandshakeTest::launchContract},
        {"client / error reply", HandshakeTest::errorReply},
        {"client / slow reply does not block / no answer in time", HandshakeTest::slowReply},
        {"client / garbage, no output, crash", HandshakeTest::brokenReplies},
        {"client / package missing: nothing started / empty package folder", HandshakeTest::missingPackage},
        {"client / interpreter cannot be started", HandshakeTest::processFailsToStart},
        {"client / closed while a command runs", HandshakeTest::clientClosedWhileRunning},
        {"status / backend, laser and scan pills", HandshakeTest::statusPills},
        {"status / last event from the two logs", HandshakeTest::lastEvents},
        {"status / simulation badge / unclear values", HandshakeTest::simulationBadgeRules},
        {"theme / contrast in the light and the dark appearance", HandshakeTest::themeContrast},
        {"window / every control once, in its group / tabs / sizes / nothing started unshown", HandshakeTest::windowLayout},
        {"window / backend and laser status / warning stripes / last event", HandshakeTest::statusDisplay},
        {"window / scan status: running n of N, finished, stopped, failed, paused", HandshakeTest::scanDisplay},
        {"Data tab / demo wording / table / details and provenance / sync, classify, offline, import / failures", HandshakeTest::dataTab},
        {"Review tab / spectrum, scores, reason, provenance / confirm, relabel, reject / reviewer", HandshakeTest::reviewTab},
        {"sync by itself / after a scan, a single capture, a stopped scan / not during an acquisition", HandshakeTest::syncAfterCaptures},
        {"sync by itself / none starts between the start and the end of a scan", HandshakeTest::noSyncDuringScan},
        {"sync by itself / periodically while something waits and no acquisition runs / status asked again", HandshakeTest::periodicSync},
        {"window never shown / no data-layer process", HandshakeTest::headlessWindow},
        {"Simulation Studio / detached server / address from its first line / reused", HandshakeTest::studioFromTheWindow},
        {"Simulation Studio / start failures reported", HandshakeTest::studioFailures},
        {"sizes / nothing cut at 1440 x 880 and at 1180 x 720 / longest header texts / smallest size scrolls", HandshakeTest::nothingClipped},
        {"logs / newest line stays in view / scrolled-back log stays put", HandshakeTest::logsFollowTheirEnd},
        {"appearances / dark and light applied to a running window", HandshakeTest::appearances},
    };
    // ./ui_tests [part of a test name]: only the tests whose name contains it.
    const QString wanted = argc > 1 ? QString::fromLocal8Bit(argv[1]) : QString();
    try {
        check(ramanHome() == QCoreApplication::applicationDirPath(), "must run next to the emulators (handshake_v2/ in this folder)");
        int ran = 0;
        for (const Test& test : tests) {
            if (!QString(test.name).contains(wanted, Qt::CaseInsensitive)) continue;
            test.run();
            std::cout << "PASS " << test.name << std::endl;
            ++ran;
        }
        check(ran > 0, "no test has that name");
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "FAIL " << ex.what() << std::endl; return 1;
    }
}
