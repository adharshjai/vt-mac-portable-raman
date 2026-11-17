// Tests of the uploads to ARC in the Data tab (arc_panel.h): when the window asks the data layer about
// them, what the page shows, and what each button and dialog sends. The stand-in data layer
// (fake_data_layer.py) answers the arc-* commands with made-up uploads: nothing is copied, nothing is
// contacted, and no browser, Finder or Terminal window is opened (the page's openUrl hook stands in for
// Finder). Built like ui_tests: the real window code with the two emulators next to this program.
#include "../main.cpp"
#include <QClipboard>
#include <QPointer>
#include <functional>
#include <iostream>
#include <stdexcept>

static void check(bool ok, const QString& message) {
    if (!ok) throw std::runtime_error(message.toStdString());
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

struct HandshakeTest {   // friend of MainWindow: the parts of the window these tests look at
    static DataPanels& panels(MainWindow& w) { return *w.dataPanels; }
    static QTabWidget* tabs(MainWindow& w) { return w.display.tabs; }
    static QTimer* displayTimer(MainWindow& w) { return w.display.timer; }
    static QLineEdit* sampleField(MainWindow& w) { return w.sampleIdEdit; }
    static void applyTheme(MainWindow& w, const UiTheme& theme) { w.applyTheme(theme); }
    static QList<StatusPill*> pills(MainWindow& w) { return {w.display.simulation, w.display.backend, w.display.laser, w.display.scan, w.display.data}; }
    static QList<StatusPill*> wholePills(MainWindow& w) { return {w.display.backend, w.display.laser, w.display.scan}; }
};

struct ArcUiTest {   // friend of ArcPanel and DataPanels
    using H = HandshakeTest;

    // ---- the stand-in data layer in this program's folder
    static QString home() { return QCoreApplication::applicationDirPath(); }
    static inline QList<QUrl> opened;    // what Show in Finder asked to open
    static void resetStandIn() {
        for (const char* name : {"fake_data_layer_state.json", "fake_data_layer_calls.jsonl", "fake_data_layer_control.json",
                                 "fake_studio.pid", "fake_studio_stop"})
            QFile::remove(home() + "/" + name);
        opened.clear();
    }
    static void control(const QByteArray& json) {
        QJsonParseError error;
        const QJsonDocument document = QJsonDocument::fromJson(json, &error);
        check(document.isObject(), "the test's control file: " + error.errorString());
        check(AcquisitionProtocol::publish(home() + "/fake_data_layer_control.json", document.toJson()), "write the stand-in's control file");
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
    static QStringList arcCommands() {
        QStringList names;
        for (const QString& name : commandsCalled())
            if (name.startsWith("arc-") || name == "config") names << name;
        return names;
    }
    // What follows "--home HOME <command>".
    static QStringList optionsOf(const QJsonObject& call) {
        QStringList words;
        for (const QJsonValue& word : call["argv"].toArray()) words << word.toString();
        return words.mid(3);
    }
    static QList<QJsonObject> callsOf(const QString& command) {
        QList<QJsonObject> found;
        for (const QJsonObject& call : calls())
            if (call["command"].toString() == command) found << call;
        return found;
    }
    static QJsonObject lastCall(const QString& command) {
        const QList<QJsonObject> found = callsOf(command);
        if (found.isEmpty()) throw std::runtime_error(("The stand-in was never asked for " + command).toStdString());
        return found.last();
    }
    // arc-upload without --dry-run: an upload made.
    static int uploadsSent() {
        int sent = 0;
        for (const QJsonObject& call : callsOf("arc-upload")) sent += !optionsOf(call).contains("--dry-run");
        return sent;
    }

    // ---- the window
    static ArcPanel& arc(MainWindow& w) { return *H::panels(w).arcPanel; }
    static bool settled(DataPanels& p) {
        return !p.refreshRunning && !p.syncRunning && !p.classifyRunning && !p.offlineRunning && !p.importRunning && !p.provenanceRunning &&
               !p.reviewShowRunning && !p.reviewSetRunning && p.dataLayer->running() == 0;
    }
    static bool arcSettled(ArcPanel& a) {
        return !a.listRunning && !a.sessionsRunning && !a.statusRunning && !a.actionRunning && !a.hiddenListRunning &&
               a.dataLayer->running() == 0;
    }
    static bool quiet(MainWindow& w) { return settled(H::panels(w)) && arcSettled(arc(w)); }
    // Shown, with its first status and lists; Show in Finder only records what it would open.
    static void prepare(MainWindow& w) {
        arc(w).openUrl = [](const QUrl& url) { opened << url; return true; };
        w.show();
        DataPanels& p = H::panels(w);
        check(waitUntil([&]{ return !p.status.isEmpty() && settled(p); }, 20000), "first status and lists");
    }
    // The Data tab in front and its ARC upload button down: the page reads the uploads and the sessions.
    static ArcPanel& openPage(MainWindow& w) {
        DataPanels& p = H::panels(w);
        H::tabs(w)->setCurrentIndex(1);
        if (!p.arcToggle->isChecked()) p.arcToggle->click();
        ArcPanel& a = arc(w);
        check(waitUntil([&]{ return a.loaded() && quiet(w); }, 20000), "the ARC page has read the uploads and the sessions");
        return a;
    }
    // The tests ask for the uploads themselves, one answer at a time.
    static void noPolling(ArcPanel& a) {
        a.activePollMs = a.idlePollMs = a.busyPollMs = 3600 * 1000;
        if (a.poll.isActive()) a.poll.start(a.pollInterval());
        if (a.hiddenPoll.isActive()) a.hiddenPoll.start(a.idlePollMs);
    }
    // The line's own words, in the tone it is drawn in.
    static bool lineIs(ArcPanel& a, const QString& text, UiTone tone) { return a.statusLabel->text() == text && a.statusLabel->tone() == tone; }
    static QString idOf(ArcPanel& a, const QString& created) {
        for (const QJsonValue& value : a.uploads)
            if (value.toObject()["created_utc"].toString() == created) return value.toObject()["upload_id"].toString();
        throw std::runtime_error(("No upload made at " + created).toStdString());
    }
    static void listOnce(MainWindow& w) {
        arc(w).listUploads();
        check(waitUntil([&]{ return quiet(w); }), "arc-list answered");
    }
    static void choose(ArcPanel& a, int index) {
        a.sessionBox->setCurrentIndex(index);
        emit a.sessionBox->activated(index);
    }
    static QPushButton* button(QWidget& parent, const QString& text) {
        for (auto* b : parent.findChildren<QPushButton*>()) if (b->text() == text) return b;
        throw std::runtime_error(("Button not found: " + text).toStdString());
    }
    static QDialog* dialogOf(ArcPanel& a) {
        check(waitUntil([&]{ return a.actionDialog && a.actionDialog->isVisible(); }), "a dialog opens");
        return a.actionDialog;
    }
    static QString dialogText(QDialog* dialog) { return dialog->findChild<QLabel*>("arcDialogText")->text(); }
    static int visibleDialogs(MainWindow& w) {
        int count = 0;
        for (QDialog* dialog : w.findChildren<QDialog*>()) count += dialog->isVisible();
        return count;
    }
    static int rowOf(ArcPanel& a, const QString& id) {
        for (int row = 0; row < a.uploads.size(); ++row)
            if (a.uploads.at(row).toObject()["upload_id"].toString() == id) return row;
        throw std::runtime_error(("Upload not listed: " + id).toStdString());
    }
    static QJsonObject upload(ArcPanel& a, const QString& id) { return a.uploads.at(rowOf(a, id)).toObject(); }
    static void select(MainWindow& w, const QString& id) {
        ArcPanel& a = arc(w);
        a.recentTable->selectRow(rowOf(a, id));
        check(waitUntil([&]{ return a.selectedUpload()["upload_id"].toString() == id && quiet(w); }), "the upload is selected");
    }
    static bool matches(const QString& text, const QString& pattern) {
        return QRegularExpression(QRegularExpression::anchoredPattern(pattern)).match(text).hasMatch();
    }
    static const inline QString kTime = "\\d\\d:\\d\\d:\\d\\d";
    static const inline QString kToggleTip = "Show the uploads of finished sessions to ARC. Press again to show the captures.";
    static const inline QString kLineSession = "1d_x_scan_20261113_101500_123";
    static const inline QString kArcFolder = "/projects/pcl_photonics/raman_globus_upload";
    static const inline QString kGoesAsTheyAre =
        "The files go as they are, with the Sample ID, Operator, Location and Notes they hold; everyone in the ARC project can read them. "
        "Nothing on ARC is deleted or replaced. You can stop the upload at any time.";

    // ---- tests

    static void startsNothingUntilShown() {
        resetStandIn();
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        ArcPanel& a = arc(w);
        check(commandsCalled() == QStringList({"status", "list", "review-list", "review-show"}),
              "at start the window runs exactly status, list, review-list and review-show");
        check(p.arcToggle->text() == "ARC upload" && p.arcToggle->isCheckable() && !p.arcToggle->isChecked() && p.lowerStack->currentIndex() == 0 &&
              p.arcToggle->toolTip() == "Show the uploads of finished sessions to ARC. Press again to show the captures.",
              "the ARC upload button is up at start: the captures are shown first");
        check(p.lowerStack->widget(1) == a.page() && qobject_cast<QScrollArea*>(a.page()) && a.content->objectName() == "arcPage" &&
              static_cast<QScrollArea*>(a.page())->horizontalScrollBarPolicy() == Qt::ScrollBarAlwaysOff, "page 1: the ARC page in a scroll area");
        H::tabs(w)->setCurrentIndex(1);
        spin(400);
        check(arcCommands().isEmpty() && !a.everShown, "the Data tab alone starts nothing for ARC");
        // The button down while another tab is in front: the page is not in view, nothing starts.
        H::tabs(w)->setCurrentIndex(0);
        p.arcToggle->setChecked(true);
        spin(400);
        check(p.lowerStack->currentIndex() == 1 && arcCommands().isEmpty() && !a.shown, "the ARC page behind another tab starts nothing");
        H::tabs(w)->setCurrentIndex(1);
        check(waitUntil([&]{ return a.loaded() && quiet(w); }, 20000), "the Data tab comes to the front with the page down: it reads");
        check(arcCommands().count("arc-list") == 1 && arcCommands().count("arc-sessions") == 1 && arcCommands().size() == 2,
              "then arc-list and arc-sessions, once each");
        check(optionsOf(lastCall("arc-list")) == QStringList({"--limit=12"}) &&
              optionsOf(lastCall("arc-sessions")) == QStringList({"--limit=30", "--controller-idle"}),
              "arc-list --limit=12, arc-sessions --limit=30 --controller-idle while no acquisition runs");
        check(a.poll.isActive() && a.poll.interval() == 30000 && a.active == 0, "no upload is worked on: arc-list again in 30 s");
        // Up again: the captures, and no polling.
        p.arcToggle->click();
        check(p.lowerStack->currentIndex() == 0 && !a.shown && !a.poll.isActive(), "the captures again: the ARC page stops polling");
        a.idlePollMs = 150;
        spin(700);
        check(arcCommands().count("arc-list") == 1, "nothing is asked while the page is hidden");
        p.arcToggle->click();
        check(waitUntil([&]{ return arcCommands().count("arc-list") >= 3 && arcCommands().count("arc-sessions") == 2; }),
              "shown again: read at once, then polled");
        H::tabs(w)->setCurrentIndex(2);
        check(waitUntil([&]{ return quiet(w); }) && !a.shown && !a.poll.isActive(), "another tab in front: the page stops polling");
        const qsizetype lists = arcCommands().count("arc-list");
        spin(600);
        check(arcCommands().count("arc-list") == lists, "and asks nothing");
    }

    static void polling() {
        resetStandIn();
        QJsonArray script{"queued", "preparing"};
        for (int file = 1; file <= 40; ++file) script << QString("transferring:%1/41").arg(file);
        script << "verifying" << "placing" << "verified";
        control(QJsonDocument(QJsonObject{{"arc", QJsonObject{{"script", script}}}}).toJson());
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        ArcPanel& a = openPage(w);
        check(a.poll.interval() == 30000 && a.activePollMs == 2000 && a.idlePollMs == 30000 && a.busyPollMs == 10000, "the intervals of the design");
        a.uploadButton->click();
        check(waitUntil([&]{ return a.active > 0 && uploadsSent() == 1 && quiet(w); }), "a demo upload is worked on");
        check(a.poll.isActive() && a.poll.interval() == 2000, "every 2 s while an upload is worked on");
        H::displayTimer(w)->stop();      // it would tell the panels every 200 ms that no acquisition runs
        p.setAcquisitionBusy(true);
        // The next list is due 10 s after the last answer, not 10 s after the change (a coarse timer: within 5%).
        check(a.poll.isActive() && a.pollInterval() == 10000 && a.poll.remainingTime() <= 10500 &&
                  a.poll.remainingTime() >= 10000 - a.listAnswered.elapsed() - 600,
              QString("at most every 10 s while an acquisition runs: due in %1 ms, %2 ms after the last answer")
                  .arg(a.poll.remainingTime()).arg(a.listAnswered.elapsed()));
        listOnce(w);
        check(a.active > 0 && a.poll.interval() == 10000, "also after the next answer");
        p.setAcquisitionBusy(false);
        check(a.acquisitionBusy && a.becomingIdle.isActive() && a.pollInterval() == 10000,
              "the end of an acquisition counts once it has lasted a moment");
        check(waitUntil([&]{ return !a.acquisitionBusy; }, 5000) && a.pollInterval() == 2000 && a.poll.remainingTime() <= 2100,
              "every 2 s again when the acquisition is over");
        // The cadence itself, with shorter intervals.
        a.activePollMs = 150; a.busyPollMs = 1300; a.idlePollMs = 5000;
        a.poll.start(a.pollInterval());
        qsizetype before = arcCommands().count("arc-list");
        spin(1500);
        check(arcCommands().count("arc-list") - before >= 4, "arc-list follows an upload at the short interval");
        p.setAcquisitionBusy(true);
        check(waitUntil([&]{ return !a.listRunning; }), "the list that was on its way ends");
        before = arcCommands().count("arc-list");
        spin(2000);
        check(arcCommands().count("arc-list") - before <= 2, "during an acquisition no more often than the longer interval");
        p.setAcquisitionBusy(false);
        p.arcToggle->click();
        check(waitUntil([&]{ return !a.listRunning; }), "the list that was on its way ends");
        before = arcCommands().count("arc-list");
        spin(800);
        // The toggle shows the unfinished upload: hidden, only its counts are read again, at the idle rate (5 s here).
        check(arcCommands().count("arc-list") == before && !a.poll.isActive() && a.hiddenPoll.isActive() &&
              a.hiddenPoll.interval() == 5000, "hidden: no list of the page; the counts at the idle rate");
        // One at a time: a request made while one runs is made once afterwards.
        noPolling(a);
        before = arcCommands().count("arc-list");
        p.arcToggle->click();
        check(a.listRunning, "shown: a list on its way at once");
        a.listUploads(); a.listUploads(); a.listUploads();
        check(waitUntil([&]{ return quiet(w); }) && arcCommands().count("arc-list") == before + 2, "three requests during one list: one more list afterwards");
    }

    static void toggleColour() {
        resetStandIn();
        control(R"({"arc_unfinished": 2})");
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        check(p.arcToggle->property("tone") == "warn" && p.arcToggle->toolTip() == "2 uploads to ARC are not finished. Press to see them." &&
              p.arcToggle->styleSheet().contains(uiTheme().warnInk.name()), "unfinished uploads: the button in the warn tone, with the tool tip");
        check(arcCommands().isEmpty(), "from the status the window asks anyway: no process of its own");
        control(R"({"arc_unfinished": 1})");
        p.refresh();
        check(waitUntil([&]{ return settled(p) && p.arcToggle->toolTip() == "1 upload to ARC is not finished. Press to see it."; }), "one");
        control(R"({})");
        p.refresh();
        check(waitUntil([&]{ return settled(p) && !p.arcToggle->property("tone").isValid(); }) && p.arcToggle->styleSheet().isEmpty() &&
              p.arcToggle->toolTip() == "Show the uploads of finished sessions to ARC. Press again to show the captures.", "none: the plain button");
        control(R"({"arc_unfinished": 3})");
        p.sync(false);
        check(waitUntil([&]{ return settled(p) && p.arcToggle->toolTip().startsWith("3 uploads"); }), "the status a sync brings counts as well");
        H::applyTheme(w, UiTheme::darkAppearance());
        check(p.arcToggle->styleSheet().contains(UiTheme::darkAppearance().warnInk.name()), "in the colours of the appearance in use");
        check(arc(w).progressBar->styleSheet().contains(UiTheme::darkAppearance().surfaceAlt.name()) &&
              arc(w).badge->styleSheet().contains(UiTheme::darkAppearance().warnFill.name()), "the ARC page's own colours follow the appearance");
        H::applyTheme(w, UiTheme::light());
        // Uploads that need the person (fix plan I2): the danger tone, before the unfinished ones.
        control(R"({"arc_unfinished": 3, "arc_attention": 1})");
        p.refresh();
        check(waitUntil([&]{ return settled(p) && p.arcToggle->property("tone") == "danger"; }) &&
              p.arcToggle->toolTip() == "1 upload to ARC needs you. Press to see it." &&
              p.arcToggle->styleSheet().contains(uiTheme().dangerInk.name()) && p.arcToggle->styleSheet().contains(uiTheme().dangerFill.name()),
              "an upload that needs the person: the danger tone, with its tool tip: " + p.arcToggle->toolTip());
        control(R"({"arc_attention": 2})");
        p.refresh();
        check(waitUntil([&]{ return settled(p) && p.arcToggle->toolTip() == "2 uploads to ARC need you. Press to see them."; }) &&
              p.arcToggle->property("tone") == "danger", "two");
        H::applyTheme(w, UiTheme::darkAppearance());
        check(p.arcToggle->styleSheet().contains(UiTheme::darkAppearance().dangerInk.name()), "the danger tone of the appearance in use");
        H::applyTheme(w, UiTheme::light());
        control(R"({})");
        p.refresh();
        check(waitUntil([&]{ return settled(p) && !p.arcToggle->property("tone").isValid(); }) && p.arcToggle->toolTip() == kToggleTip,
              "none again: the plain button");
        check(arcCommands().isEmpty(), "still no arc command: the colour came from status alone, and the counts were read again only "
                                       "at the idle rate (30 s)");
    }

    // Fix plan I2 (UI-V01): while the ARC page is hidden and the button shows a colour, arc-list --limit=1 runs
    // at the idle rate, so that an upload that fails meanwhile turns the button to the danger tone.
    static void toggleFollowsUploads() {
        resetStandIn();
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        ArcPanel& a = arc(w);
        check(!p.arcToggle->property("tone").isValid() && !a.hiddenPoll.isActive(), "no upload: no colour, nothing read");
        a.idlePollMs = 300;
        control(R"({"arc": {"uploads": [{"script": ["transferring:3/21", "transferring:4/21", "failed:task_failed"]}]}})");
        p.refresh();
        check(waitUntil([&]{ return settled(p) && p.arcToggle->property("tone") == "warn"; }) &&
              p.arcToggle->toolTip() == "1 upload to ARC is not finished. Press to see it.", "status counts the upload: the warn tone");
        check(a.hiddenPoll.isActive() && !a.everShown, "the page was never shown; the counts are read again while the button has a colour");
        check(waitUntil([&]{ return p.arcToggle->property("tone") == "danger" && quiet(w); }, 10000) &&
              p.arcToggle->toolTip() == "1 upload to ARC needs you. Press to see it.",
              "the upload failed while the captures were shown: the danger tone: " + p.arcToggle->toolTip());
        check(!callsOf("arc-list").isEmpty(), "arc-list ran");
        for (const QJsonObject& call : callsOf("arc-list")) check(optionsOf(call) == QStringList({"--limit=1"}), "only arc-list --limit=1");
        check(callsOf("arc-sessions").isEmpty() && !a.listLoaded && a.uploads.isEmpty() && !a.everShown, "the page itself still reads nothing");
        check(a.hiddenPoll.isActive(), "it goes on while the button has a colour");
        // Shown: the page reads in full and keeps the colour; hidden again: the counts again.
        openPage(w);
        check(p.arcToggle->property("tone") == "danger" && !a.hiddenPoll.isActive() && optionsOf(lastCall("arc-list")) == QStringList({"--limit=12"}),
              "shown: arc-list --limit=12, the danger tone stays");
        p.arcToggle->click();
        check(!a.shown && a.hiddenPoll.isActive(), "hidden again: the counts are followed again");
        // No upload left: no colour, and nothing is read.
        a.idlePollMs = 3600 * 1000;
        a.hiddenPoll.start(a.idlePollMs);
        check(waitUntil([&]{ return quiet(w); }), "no list on its way");
        QFile::remove(home() + "/fake_data_layer_state.json");
        control(R"({})");
        p.refresh();
        check(waitUntil([&]{ return settled(p) && !p.arcToggle->property("tone").isValid(); }) && p.arcToggle->toolTip() == kToggleTip,
              "status says nothing waits: the plain button");
        check(!a.hiddenPoll.isActive(), "no colour: no arc-list");
        a.idlePollMs = 300;
        const qsizetype lists = callsOf("arc-list").size();
        spin(900);
        check(callsOf("arc-list").size() == lists, "and none runs");
    }

    static void demoUpload() {
        resetStandIn();
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        check(a.demoBox->isChecked() && a.globusRadio->isChecked() && a.badge->text() == "DEMO" && a.declareBox->isHidden(),
              "the settings' defaults: demo, Globus");
        check(a.intro->text() == "Copies a finished scan session, with its dark spectrum and a checked list of its files, to the team folder on ARC.",
              "what the page does");
        check(a.sessionBox->count() == 3 && a.sessionBox->currentIndex() == 0, "the newest session that may go is chosen");
        const QString dot = QStringLiteral(" \u00B7 ");
        // A folder without session.json is counted (UI-07: not "0 spectra"); it has no sample.
        check(a.sessionBox->itemText(0) == "single_20261113_103000_456" + dot + "TEST-MB-01" + dot + "1 spectrum" + dot + "finished" + dot + "simulated" &&
              a.sessionBox->itemText(1) == kLineSession + dot + "TEST-PLATE-A" + dot + "13 spectra" + dot + "finished" + dot + "simulated" &&
              a.sessionBox->itemText(2) == "2d_scan_20260924_141500_001" + dot + DataText::none() + dot + "3 spectra" + dot + "ended (no end mark)" + dot +
                                           "simulation not recorded" + dot + "not ready", "the sessions, in words: " + a.sessionBox->itemText(2));
        check(a.sessionBox->itemData(2, Qt::ToolTipRole).toString() ==
                  "2d_scan_20260924_141500_001 has no session.json, so this edition did not record it. Add it with Import scan folder in the Data tab "
                  "(command line: import-session --folder 2d_scan_20260924_141500_001), then upload.", "a session that may not go says why");
        check(a.statusLabel->text() == "Ready for a demo upload: 6 files (61 kB) go to the demo folder on this Mac." &&
              a.statusLabel->tone() == UiTone::Neutral, "ready for a demo upload");
        choose(a, 1);
        const QString demoRoot = QDir::cleanPath(home() + "/data_catalog/arc_demo");
        check(a.statusLabel->text() == "Ready for a demo upload: 21 files (433 kB) go to the demo folder on this Mac.", "the chosen session");
        check(a.folderLabel->text() == demoRoot + kArcFolder + "   (demo, on this Mac)" &&
              a.folderLabel->toolTip() == "Change it in ARC settings. Each session goes into a folder of its own inside it.", "the demo's ARC folder");
        check(a.destinationLabel->text() == demoRoot + kArcFolder + "/simulated/" + kLineSession &&
              a.destinationLabel->toolTip() == "Demo folder on this Mac. It stands in for " + kArcFolder + "/simulated/" + kLineSession + ".",
              "where the demo puts it");
        check(a.uploadButton->isEnabled() && a.uploadButton->toolTip() == "Copy the chosen session to the demo folder on this Mac.", "Upload to ARC");
        check(a.recentTable->rowCount() == 0 && a.recentTable->emptyText == "No uploads yet.", "no uploads yet");

        // Upload: the dry run first, then the upload, without a dialog; a double click makes one upload.
        a.uploadButton->click();
        a.uploadButton->click();
        check(waitUntil([&]{ return uploadsSent() == 1 && quiet(w); }), "the upload is made");
        const QList<QJsonObject> asked = callsOf("arc-upload");
        check(asked.size() == 2 && optionsOf(asked[0]) == QStringList({"--session=" + kLineSession, "--transport=globus", "--demo", "--controller-idle", "--dry-run"}) &&
              optionsOf(asked[1]) == QStringList({"--session=" + kLineSession, "--transport=globus", "--demo", "--controller-idle"}),
              "--dry-run first with --controller-idle, then the same without --dry-run; a double click runs it once");
        check(!a.actionDialog && visibleDialogs(w) == 0, "a demo upload starts without a dialog");
        const QString id = a.trackedUpload;
        check(!id.isEmpty() && a.selectedUpload()["upload_id"].toString() == id && arcCommands().indexOf("arc-list", arcCommands().lastIndexOf("arc-upload")) > 0,
              "the upload is followed and selected; arc-list follows the command");
        // The status line follows the scripted states word for word, each in its tone.
        const QStringList expected{
            "Demo: Preparing the files: copying the session and its dark spectrum and computing a checksum for every file.",
            "Demo: no sign-in. A real upload opens your browser here.",
            "Demo: checking this Mac's files and the demo folder.",
            "Demo: Handing the copy to Globus.",
            "Demo: Copying with Globus: 12 of 21 files (247 kB of 433 kB). Keep Globus Connect Personal running.",
            "Demo: checking the copy in the demo folder.",
            "Demo: putting the copy in its folder in the demo folder.",
            "Demo: 21 files in the demo folder on this Mac, every one checked. Nothing left this Mac."};
        for (int step = 0; step < expected.size(); ++step) {
            if (step > 0) listOnce(w);
            const QJsonObject item = upload(a, id);
            const QString text = a.statusLabel->text();
            check(item["message"].toString() == expected[step], "the stand-in's message at step " + QString::number(step));
            check(text == QString("Demo upload at %1 (nothing leaves this Mac): %2")
                              .arg(DataText::localTime(item["updated_utc"].toString(), "HH:mm:ss"), expected[step]),
                  "the status line at step " + QString::number(step) + ": " + text);
            const bool last = step == expected.size() - 1;
            check(a.statusLabel->tone() == (last ? UiTone::Warn : UiTone::Info), "Info while working; a demo upload that went well is Warn, not Ok");
            if (item["display_state"].toString() == "transferring")
                check(a.progressBar->isVisible() && a.progressBar->text() == "12 of 21 files" && a.progressBar->maximum() == 21, "files in transferring");
            else if (!last)
                check(a.progressBar->isVisible() && a.progressBar->maximum() == 0, "busy without a number before and after transferring");
            else
                check(a.progressBar->isHidden(), "no progress once it is on ARC");
        }
        check(waitUntil([&]{ return quiet(w) && a.sessionBox->itemText(1).endsWith(dot + "in demo folder"); }),
              "the upload became final: the sessions are read again and the session says it is in the demo folder");
        check(callsOf("arc-sessions").size() == 2 && optionsOf(lastCall("arc-status")) == QStringList({"--upload=" + id}),
              "arc-sessions after the final state; arc-status for the selected upload's history");
        const QString facts = a.details->toPlainText();
        for (const QString& part : QStringList{"Upload", id + dot + "Globus (demo)", "Files", "21 files, 433 kB", "Dark", "dark_20261113_101400_001.txt",
                                    "Signed in as", "demo (no account)", "Check", "Demo: Globus checksums (MD5) match this Mac's", "History",
                                    "Demo: Preparing the files"})
            check(facts.contains(part), "the details: " + part);
        check(a.recentTable->item(0, 2)->text() == "Globus (demo)" && a.recentTable->item(0, 3)->text() == "On ARC, checked" &&
              a.recentTable->item(0, 4)->text() == "simulated/" + kLineSession && a.recentTable->item(0, 1)->text() == kLineSession,
              "the row: With, State, On ARC");
        // The buttons of a demo upload on ARC: Check status and Show in Finder.
        check(a.finderButton->isEnabled() && a.checkButton->isEnabled() && !a.copyPathButton->isEnabled() && !a.retryButton->isEnabled() &&
              !a.keepCopyButton->isEnabled() && !a.stopButton->isEnabled(), "the buttons follow the upload's actions");
        a.finderButton->click();
        check(opened == QList<QUrl>{QUrl::fromLocalFile(demoRoot + kArcFolder + "/simulated/" + kLineSession)}, "Show in Finder opens the demo folder");
        // Once more: it is in the demo folder already. The new copy goes beside it.
        a.uploadButton->click();
        QDialog* dialog = dialogOf(a);
        // Review COR-05: a demo upload is in the demo folder on this Mac, so the dialog's title says so too.
        check(dialog->windowTitle() == "Already in the demo folder" && !dialog->isModal(), "the dialog for already_on_arc, not modal: " +
                                                                                              dialog->windowTitle());
        check(matches(dialogText(dialog), "This session is already in the demo folder, checked on \\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d, at " +
                                              QRegularExpression::escape(demoRoot + kArcFolder + "/simulated/" + kLineSession) +
                                              "\\. Upload it again\\? The new copy goes beside it as " + kLineSession +
                                              "_copy2 \\(or the next free name\\)\\."),
              "already on ARC, in the demo's words: " + dialogText(dialog));
        button(*dialog, "Upload again")->click();
        check(waitUntil([&]{ return uploadsSent() == 2 && quiet(w); }), "uploaded again");
        check(optionsOf(callsOf("arc-upload").at(3)) == QStringList({"--session=" + kLineSession, "--transport=globus", "--demo", "--new",
                                                                    "--controller-idle", "--dry-run"}) &&
              optionsOf(lastCall("arc-upload")) == QStringList({"--session=" + kLineSession, "--transport=globus", "--demo", "--new", "--controller-idle"}),
              "--new, with the dry run first");
    }

    static void liveUploads() {
        resetStandIn();
        control(R"({"arc": {"sessions": [
            {"folder": "2d_scan_20261115_090000_001", "scan_mode": "2d", "kind": "real", "points": 8, "end": "stopped", "sample_id": "S-002"},
            {"folder": "1d_x_scan_20261113_101500_123", "kind": "real", "sample_id": "S-001"},
            {"folder": "single_20261113_103000_456", "kind": "simulated"},
            {"folder": "2d_scan_20260924_141500_001", "why_not": null}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        a.demoBox->click();
        check(!a.demoBox->isChecked() && a.badge->text() == "ARC", "live: the badge says ARC");
        check(a.folderLabel->text() == kArcFolder + "   (Globus: /pcl_photonics/raman_globus_upload)", "the ARC folder and its Globus path");
        // This Mac's collection ID is not set: Globus cannot be used yet.
        const QString missing = "Set up first: This Mac's Globus collection ID is not set. Open ARC settings and paste it: Globus Connect Personal (its "
                                "icon in the menu bar) > Settings shows it (command line: config --set arc_upload.globus.mac_collection=<ID>).";
        check(!a.uploadButton->isEnabled() && a.uploadButton->toolTip() == missing && a.statusLabel->text() == missing &&
              a.statusLabel->tone() == UiTone::Warn, "Upload disabled while setup is missing, with the tool tip");
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "sessions": [
            {"folder": "2d_scan_20261115_090000_001", "scan_mode": "2d", "kind": "real", "points": 8, "end": "stopped", "sample_id": "S-002"},
            {"folder": "1d_x_scan_20261113_101500_123", "kind": "real", "sample_id": "S-001"},
            {"folder": "single_20261113_103000_456", "kind": "simulated"},
            {"folder": "2d_scan_20260924_141500_001", "why_not": null}]}})");
        listOnce(w);
        choose(a, 1);
        const QString cluster = kArcFolder + "/" + kLineSession, globus = "/pcl_photonics/raman_globus_upload/" + kLineSession;
        check(a.uploadButton->isEnabled() && a.uploadButton->toolTip() == "Copy the chosen session to ARC. You confirm what goes where first." &&
              a.statusLabel->text() == "Ready: 21 files (433 kB) go to " + cluster + "." && a.statusLabel->tone() == UiTone::Neutral, "ready, live");
        check(a.destinationLabel->text() == cluster &&
              a.destinationLabel->toolTip() == "In ARC's Globus collection: " + globus + ". Both ways of sending reach the same folder.", "To, live Globus");
        // Globus: the confirmation built from the plan; Cancel runs nothing more.
        a.uploadButton->click();
        QPointer<QDialog> dialog = dialogOf(a);
        check(optionsOf(lastCall("arc-upload")) == QStringList({"--session=" + kLineSession, "--transport=globus", "--live", "--controller-idle", "--dry-run"}),
              "the dry run of a live upload");
        check(dialog->windowTitle() == "Upload to ARC" && dialog->windowModality() == Qt::NonModal && !QApplication::activeModalWidget(), "not modal");
        const QString globusText = QStringList{
            "Send session " + kLineSession + " to ARC?", "",
            "13 spectra (finished), 21 files, 433 kB, with the dark spectrum dark_20261113_101400_001.txt and a list of every file with its SHA-256.",
            "To:  " + cluster, "     (in ARC's Globus collection: " + globus + ")",
            "By:  Globus. A web page opens for you to sign in. Keep Globus Connect Personal running, and this Mac awake, until the upload says \"On ARC, checked\".",
            "", kGoesAsTheyAre}.join("\n");
        check(dialogText(dialog) == globusText, "the confirmation for Globus: " + dialogText(dialog));
        check(button(*dialog, "Upload")->isDefault() && button(*dialog, "Cancel"), "Upload (default), Cancel");
        const qsizetype before = arcCommands().size();
        button(*dialog, "Cancel")->click();
        spin(500);
        check(!dialog && uploadsSent() == 0 && arcCommands().size() == before, "Cancel runs nothing more");
        // The list that follows the upload waits a moment here, so that the reply's own words can be read first.
        control(R"({"arc-list": {"mode": "slow", "seconds": 1.0}, "arc": {"ready": {"globus": [], "ssh": []}, "sessions": [
            {"folder": "2d_scan_20261115_090000_001", "scan_mode": "2d", "kind": "real", "points": 8, "end": "stopped", "sample_id": "S-002"},
            {"folder": "1d_x_scan_20261113_101500_123", "kind": "real", "sample_id": "S-001"},
            {"folder": "single_20261113_103000_456", "kind": "simulated"},
            {"folder": "2d_scan_20260924_141500_001", "why_not": null}]}})");
        a.uploadButton->click();
        dialog = dialogOf(a);
        QPushButton* send = button(*dialog, "Upload");
        send->click();
        send->click();
        check(waitUntil([&]{ return uploadsSent() == 1 && !a.actionRunning; }), "Upload sends it");
        check(matches(a.statusLabel->text(), "Upload at " + kTime + ": A web page opens for you to sign in to Globus\\. Come back here when you have signed in\\.") &&
              a.statusLabel->tone() == UiTone::Info, "the reply's words in the status line: " + a.statusLabel->text());
        check(waitUntil([&]{ return quiet(w); }, 20000), "the list that follows");
        spin(300);
        check(uploadsSent() == 1 && optionsOf(lastCall("arc-upload")) == QStringList({"--session=" + kLineSession, "--transport=globus", "--live",
                                                                                     "--controller-idle"}), "once, although Upload was clicked twice");
        const QJsonObject started = upload(a, a.trackedUpload);
        check(a.statusLabel->text() == "Upload at " + DataText::localTime(started["updated_utc"].toString(), "HH:mm:ss") +
                                           ": Preparing the files: copying the session and its dark spectrum and computing a checksum for every file.",
              "then the upload's own state: " + a.statusLabel->text());
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "sessions": [
            {"folder": "2d_scan_20261115_090000_001", "scan_mode": "2d", "kind": "real", "points": 8, "end": "stopped", "sample_id": "S-002"},
            {"folder": "1d_x_scan_20261113_101500_123", "kind": "real", "sample_id": "S-001"},
            {"folder": "single_20261113_103000_456", "kind": "simulated"},
            {"folder": "2d_scan_20260924_141500_001", "why_not": null}]}})");

        // SSH: the confirmation names the account; the To line shows it too.
        bool answered = false;
        a.dataLayer->run({"config", "--set=arc_upload.ssh.username=standin-user"}, [&](const DataLayerReply&) { answered = true; });
        check(waitUntil([&]{ return answered; }), "the VT username of the stand-in");
        listOnce(w);
        a.sshRadio->click();
        choose(a, 0);
        check(a.destinationLabel->text() == "standin-user@datatransfer.arc.vt.edu:" + kArcFolder + "/2d_scan_20261115_090000_001", "To, live SSH");
        a.uploadButton->click();
        dialog = dialogOf(a);
        // ssh's questions in the order it asks them (fix plan I9): the host key first, then the password, then Duo.
        const QString sshText = QStringList{
            "Send session 2d_scan_20261115_090000_001 to ARC?", "",
            "8 spectra (stopped early), 21 files, 433 kB, with the dark spectrum dark_20261113_101400_001.txt and a list of every file with its SHA-256.",
            "To:  " + kArcFolder + "/2d_scan_20261115_090000_001",
            "     (in ARC's Globus collection: /pcl_photonics/raman_globus_upload/2d_scan_20261115_090000_001)",
            "By:  SSH as standin-user@datatransfer.arc.vt.edu. A Terminal window opens; this window never sees what you type there. Not you? "
            "Change the VT username in ARC settings first.",
            "     ssh asks, in this order:",
            "     1. The first time only: ARC's key fingerprint, then \"Are you sure you want to continue connecting (yes/no/[fingerprint])?\". "
            "Type only yes or no here; what you type is shown on screen, so never type your password at this question. Answer yes only if the "
            "fingerprint matches the one in the README (section 14, Host key); otherwise answer no and ask the team.",
            "     2. Your VT password. Typing shows nothing; that is normal. Press Return.",
            "     3. Duo: approve the push on your phone, or type your password followed by a comma and a six-digit passcode at the password "
            "question. Choose Duo Push if a numbered menu appears.",
            "     ssh may print a WARNING that the connection is not using a post-quantum key exchange algorithm. That is expected with ARC "
            "(ARC's FAQ explains it); continue.",
            "     ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN.",
            "", "This scan was stopped early: 8 of 124 spectra.",
            "", kGoesAsTheyAre}.join("\n");
        check(dialogText(dialog) == sshText, "the confirmation for SSH, of a scan stopped early: " + dialogText(dialog));
        check(optionsOf(lastCall("arc-upload")).contains("--transport=ssh"), "sent with SSH");
        button(*dialog, "Cancel")->click();
        check(waitUntil([&]{ return !dialog; }), "closed");

        // A simulated session: refused live until the setting allows it, then it goes into simulated/.
        a.globusRadio->click();
        choose(a, 2);
        check(!a.uploadButton->isEnabled() && a.statusLabel->text().startsWith("This session was made in simulation; simulated sessions are not sent to ARC. "
                                                                                 "Tick Demo to try the upload.") && a.statusLabel->tone() == UiTone::Warn,
              "a simulated session may not go live: why, and what to do");
        // It has no place on ARC: the To row shows none, with no tool tip, for Globus and for SSH (UI-10).
        check(a.selectedSession()["live"].toObject()["cluster_path"].isNull(), "arc-sessions gives no cluster path for it");
        check(a.destinationLabel->text() == DataText::none() && a.destinationLabel->toolTip().isEmpty(),
              "To, live Globus, a session that may not go: " + a.destinationLabel->text() + " | " + a.destinationLabel->toolTip());
        a.sshRadio->click();
        check(a.destinationLabel->text() == DataText::none() && a.destinationLabel->toolTip().isEmpty(),
              "To, live SSH, a session that may not go: " + a.destinationLabel->text());
        a.globusRadio->click();
        answered = false;
        a.dataLayer->run({"config", "--set=arc_upload.include_simulated=true"}, [&](const DataLayerReply&) { answered = true; });
        check(waitUntil([&]{ return answered; }), "simulated sessions allowed");
        a.loadSessions();
        check(waitUntil([&]{ return quiet(w) && a.uploadButton->isEnabled(); }), "now it may go");
        a.uploadButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog).contains("To:  " + kArcFolder + "/simulated/single_20261113_103000_456\n") &&
              dialogText(dialog).contains("\nThis session was made in simulation. It goes into the separate folder simulated/.\n"),
              "the confirmation of a simulated session: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();

        // Simulation not recorded: handled like a simulated one unless "Measured with the real instruments" is ticked.
        answered = false;
        a.dataLayer->run({"config", "--set=arc_upload.include_simulated=false"}, [&](const DataLayerReply&) { answered = true; });
        check(waitUntil([&]{ return answered; }), "simulated sessions refused again");
        a.loadSessions();
        check(waitUntil([&]{ return quiet(w); }), "sessions read");
        choose(a, 3);
        check(!a.declareBox->isHidden() && !a.declareBox->isChecked() && a.declareBox->text() == "Measured with the real instruments",
              "the box is shown for a session of kind unknown");
        check(!a.uploadButton->isEnabled() && a.statusLabel->text().startsWith("It is not recorded whether this session was made in simulation"),
              "until it is ticked, the session is handled like a simulated one");
        a.declareBox->click();
        check(a.uploadButton->isEnabled(), "ticked: it may go");
        a.uploadButton->click();
        dialog = dialogOf(a);
        check(optionsOf(lastCall("arc-upload")) == QStringList({"--session=2d_scan_20260924_141500_001", "--transport=globus", "--live", "--declare-real",
                                                                "--controller-idle", "--dry-run"}), "--declare-real is sent");
        check(dialogText(dialog).contains("\nYou said this session was measured with the real instruments; the upload records that.\n") &&
              dialogText(dialog).contains("3 spectra (no end mark)") && dialogText(dialog).contains("\nThis scan has no end mark: 3 spectra.\n"),
              "the confirmation of a declared session: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();
        choose(a, 1);
        check(a.declareBox->isHidden(), "another session: the box goes");
    }

    static void conflicts() {
        resetStandIn();
        control(R"({"arc": {"upload": "conflict:earlier_upload"}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        choose(a, 1);
        a.uploadButton->click();
        QPointer<QDialog> dialog = dialogOf(a);
        check(dialog->windowTitle() == "Earlier upload", "earlier_upload opens its dialog");
        // A demo upload's files are in the demo folder on this Mac (review COR-05).
        const QString demoRoot = QDir::cleanPath(home() + "/data_catalog/arc_demo");
        check(dialogText(dialog) == "This session has an earlier upload that ended as Failed: Demo: Globus could not finish the copy: the connection to this "
                                    "Mac was lost. Files copied so far are in " + demoRoot + kArcFolder + "/simulated/_incoming/" + kLineSession + "." +
                                    a.trackedUpload + ". Retry that request, or start a new upload with the current ARC settings?",
              "the earlier upload's state and message: " + dialogText(dialog));
        const QString earlier = a.trackedUpload;
        check(button(*dialog, "Retry that request")->isEnabled() && button(*dialog, "Start a new upload") && button(*dialog, "Cancel"), "three choices");
        button(*dialog, "Retry that request")->click();
        check(waitUntil([&]{ return callsOf("arc-retry").size() == 1 && quiet(w); }), "Retry that request");
        check(optionsOf(lastCall("arc-retry")) == QStringList({"--upload=" + earlier}) && uploadsSent() == 0, "arc-retry of the earlier upload, no new upload");
        const QJsonObject retried = upload(a, earlier);
        check(a.trackedUpload == earlier && a.statusLabel->text() == "Demo upload at " + DataText::localTime(retried["updated_utc"].toString(), "HH:mm:ss") +
                                                                          " (nothing leaves this Mac): Demo: Preparing the files: copying the session and its "
                                                                          "dark spectrum and computing a checksum for every file.",
              "the status line follows the retried upload: " + a.statusLabel->text());
        // Another session, and a new upload this time.
        choose(a, 0);
        a.uploadButton->click();
        dialog = dialogOf(a);
        button(*dialog, "Start a new upload")->click();
        check(waitUntil([&]{ return uploadsSent() == 1 && quiet(w); }), "a new upload");
        const QList<QJsonObject> asked = callsOf("arc-upload");
        check(optionsOf(asked.at(asked.size() - 2)) == QStringList({"--session=single_20261113_103000_456", "--transport=globus", "--demo", "--new",
                                                                   "--controller-idle", "--dry-run"}) &&
              optionsOf(asked.last()) == QStringList({"--session=single_20261113_103000_456", "--transport=globus", "--demo", "--new", "--controller-idle"}),
              "--new, after its dry run");
        // Already on ARC: Upload again, or Cancel.
        control(R"({"arc": {"upload": "conflict:already_on_arc"}})");
        a.demoBox->click();
        control(R"({"arc": {"upload": "conflict:already_on_arc", "ready": {"globus": []}, "sessions": [{"folder": "1d_x_scan_20261113_101500_123", "kind": "real"}]}})");
        a.loadSessions();
        listOnce(w);
        check(waitUntil([&]{ return quiet(w) && a.uploadButton->isEnabled(); }), "a live upload may be asked for");
        a.uploadButton->click();
        dialog = dialogOf(a);
        check(dialog->windowTitle() == "Already on ARC" &&
                  matches(dialogText(dialog), "This session is already on ARC, checked on \\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d, at " +
                                                  QRegularExpression::escape(kArcFolder + "/" + kLineSession) +
                                                  "\\. Upload it again\\? The new copy goes beside it as " + kLineSession +
                                                  "_copy2 \\(or the next free name\\)\\."),
              "already_on_arc opens its dialog: " + dialogText(dialog));
        const int sent = uploadsSent();
        button(*dialog, "Cancel")->click();
        spin(400);
        check(uploadsSent() == sent && !dialog, "Cancel: nothing more");
        a.uploadButton->click();
        dialog = dialogOf(a);
        button(*dialog, "Upload again")->click();
        dialog = nullptr;
        check(waitUntil([&]{ return a.actionDialog && a.actionDialog->windowTitle() == "Upload to ARC" && quiet(w); }),
              "a live upload again: the confirmation follows");
        check(dialogText(a.actionDialog).contains("\nIt goes beside the earlier copy as " + kLineSession + "_copy2 (or the next free name).\n"),
              "the confirmation says where the new copy goes: " + dialogText(a.actionDialog));
        button(*a.actionDialog, "Upload")->click();
        check(waitUntil([&]{ return uploadsSent() == sent + 1 && quiet(w); }) && optionsOf(lastCall("arc-upload")).contains("--new"), "uploaded beside it");
    }

    static void actions() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "uploads": [
            {"state": "transferring:3/21", "created_utc": "2026-11-15T10:11:00.000Z"},
            {"state": "not_watched", "created_utc": "2026-11-15T10:10:00.000Z"},
            {"state": "interrupted", "created_utc": "2026-11-15T10:09:00.000Z"},
            {"state": "uncertain", "created_utc": "2026-11-15T10:08:00.000Z"},
            {"state": "conflict", "created_utc": "2026-11-15T10:07:00.000Z"},
            {"state": "failed:task_failed", "created_utc": "2026-11-15T10:06:00.000Z"},
            {"state": "failed:payload_changed", "created_utc": "2026-11-15T10:05:00.000Z"},
            {"state": "stopped", "payload_kept": false, "created_utc": "2026-11-15T10:04:00.000Z"},
            {"state": "verified", "area": "real", "created_utc": "2026-11-15T10:03:00.000Z"},
            {"state": "verified", "demo": true, "created_utc": "2026-11-15T10:02:00.000Z"},
            {"state": "failed:ssh_interrupted", "transport": "ssh", "created_utc": "2026-11-15T10:01:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        check(a.uploads.size() == 11 && a.recentTable->rowCount() == 11, "every upload is listed");
        // The buttons follow the selected upload's actions exactly.
        const QList<QPair<QPushButton*, QString>> buttons{{a.checkButton, "check"}, {a.retryButton, "retry"}, {a.keepCopyButton, "keep_copy"},
                                                          {a.stopButton, "stop"}, {a.copyPathButton, "copy_path"}, {a.finderButton, "show_in_finder"}};
        QSet<QString> seen;
        for (int row = 0; row < a.uploads.size(); ++row) {
            const QJsonObject item = a.uploads.at(row).toObject();
            select(w, item["upload_id"].toString());
            const QJsonArray actions = a.selectedUpload()["actions"].toArray();
            for (const auto& [pressed, action] : buttons)
                check(pressed->isEnabled() == actions.contains(QJsonValue(action)),
                      QString("%1 in state %2: %3").arg(pressed->text(), item["display_state"].toString(), action));
            seen.insert(item["display_state"].toString());
            check(a.recentTable->item(row, 3)->text() == item["display_label"].toString(), "the State column: the display label");
        }
        check(seen == QSet<QString>({"transferring", "not_watched", "interrupted", "uncertain", "conflict", "failed", "stopped", "verified"}),
              "every kind of state was looked at");
        const auto idIn = [&](const QString& state, bool demo = false, const QString& transport = "globus") {
            for (const QJsonValue& value : a.uploads) {
                const QJsonObject item = value.toObject();
                if (item["display_state"].toString() == state && item["demo"].toBool() == demo && item["transport"].toString() == transport)
                    return item["upload_id"].toString();
            }
            throw std::runtime_error(("No upload in state " + state).toStdString());
        };
        // Retry asks first for a live upload, in words that fit what it does.
        QString id = idIn("uncertain");
        select(w, id);
        a.retryButton->click();
        QPointer<QDialog> dialog = dialogOf(a);
        QJsonObject item = upload(a, id);
        check(dialog->windowTitle() == "Retry the same upload" &&
                  dialogText(dialog) == QString("Send the same upload again? Upload %1 of %2 with Globus, to %3. Globus is asked again with the same request "
                                                "number, so it cannot make a second copy. It uses the settings saved with the upload, not the current ARC settings.")
                                            .arg(id, item["session_folder"].toString(), item["destination"].toObject()["final_path"].toString()),
              "Retry asks first: " + dialogText(dialog));
        check(button(*dialog, "Retry")->isDefault() && button(*dialog, "Cancel"), "Retry, Cancel");
        button(*dialog, "Cancel")->click();
        spin(300);
        check(callsOf("arc-retry").isEmpty() && !dialog, "Cancel sends nothing");
        id = idIn("failed");
        select(w, id);
        a.retryButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog).contains(" Globus copies the same files again into the same _incoming folder. "), "a failed Globus copy: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();
        id = idIn("failed", false, "ssh");
        select(w, id);
        a.retryButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog).contains(" with SSH, to ") &&
              dialogText(dialog).contains(" A Terminal window opens for your password and Duo; the files are sent again into the same folder on ARC "
                                          "unless ARC has them all. "),
              "SSH: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();
        // Stop: a Globus transfer nobody watches is cancelled after a sign-in, which the question says.
        id = idIn("not_watched");
        select(w, id);
        a.stopButton->click();
        dialog = dialogOf(a);
        item = upload(a, id);
        check(dialog->windowTitle() == "Stop upload" &&
                  dialogText(dialog) == "Stop this upload? Files already copied stay in " + item["destination"].toObject()["incoming_path"].toString() +
                                            " on ARC; nothing is deleted. A web page opens for you to sign in to Globus; then the transfer is cancelled.",
              "Stop asks first: " + dialogText(dialog));
        check(button(*dialog, "Stop the upload") && button(*dialog, "Keep going"), "Stop the upload, Keep going");
        button(*dialog, "Keep going")->click();
        spin(300);
        check(callsOf("arc-stop").isEmpty(), "Keep going: nothing is sent");
        // One dialog at a time: a new question takes the place of the open one.
        id = idIn("interrupted");
        select(w, id);
        a.retryButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog).contains(" It continues where it stopped. "), "interrupted: " + dialogText(dialog));
        id = idIn("not_watched");
        select(w, id);
        a.checkButton->click();
        check(waitUntil([&]{ return !dialog; }) && visibleDialogs(w) == 1 && a.actionDialog->windowTitle() == "Check status", "one dialog at a time");
        dialog = a.actionDialog;
        check(dialogText(dialog) == "Check this upload on ARC? A web page opens for you to sign in to Globus. Nothing is sent.", "Check asks first");
        check(callsOf("arc-check").isEmpty() && callsOf("arc-retry").isEmpty(), "nothing is sent before the answer");
        button(*dialog, "Check")->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 1 && quiet(w); }) && optionsOf(lastCall("arc-check")) == QStringList({"--upload=" + id}),
              "then arc-check --upload=<id>");
        check(a.trackedUpload == id && matches(a.statusLabel->text(), "Upload at " + kTime + ": .+"), "the status line follows that upload");
        id = idIn("uncertain");
        select(w, id);
        a.retryButton->click();
        button(*dialogOf(a), "Retry")->click();
        check(waitUntil([&]{ return callsOf("arc-retry").size() == 1 && quiet(w); }) && optionsOf(lastCall("arc-retry")) == QStringList({"--upload=" + id}),
              "Retry: arc-retry --upload=<id>");
        // Stop of an upload that is worked on: no sign-in, the program stops by itself.
        id = idIn("transferring");
        select(w, id);
        a.stopButton->click();
        dialog = dialogOf(a);
        item = upload(a, id);
        check(dialogText(dialog) == "Stop this upload? Files already copied stay in " + item["destination"].toObject()["incoming_path"].toString() +
                                        " on ARC; nothing is deleted.", "Stop of an upload that is worked on: " + dialogText(dialog));
        button(*dialog, "Stop the upload")->click();
        check(waitUntil([&]{ return callsOf("arc-stop").size() == 1 && quiet(w); }) && optionsOf(lastCall("arc-stop")) == QStringList({"--upload=" + id}),
              "then arc-stop --upload=<id>");
        // Keep as new copy.
        id = idIn("conflict");
        select(w, id);
        a.keepCopyButton->click();
        dialog = dialogOf(a);
        item = upload(a, id);
        check(dialogText(dialog) == QString("ARC already has a folder named %1 in %2/simulated. Put this upload beside it as %1_copy2 (or the next free name)? "
                                            "Nothing is replaced.").arg(item["session_folder"].toString(), kArcFolder), "Keep as new copy asks first: " + dialogText(dialog));
        check(button(*dialog, "Put it beside") && button(*dialog, "Not now"), "Put it beside, Not now");
        button(*dialog, "Put it beside")->click();
        check(waitUntil([&]{ return callsOf("arc-keep-copy").size() == 1 && quiet(w); }) && optionsOf(lastCall("arc-keep-copy")) == QStringList({"--upload=" + id}),
              "then arc-keep-copy --upload=<id>");
        // Copy ARC path: the folder on ARC; a demo upload is checked without a dialog.
        for (const QJsonValue& value : a.uploads)
            if (value.toObject()["created_utc"].toString() == "2026-11-15T10:03:00.000Z") id = value.toObject()["upload_id"].toString();
        select(w, id);
        check(upload(a, id)["display_state"].toString() == "verified" && upload(a, id)["area"].toString() == "real", "the upload on ARC");
        a.copyPathButton->click();
        check(QGuiApplication::clipboard()->text() == kArcFolder + "/" + kLineSession,
              "Copy ARC path puts the folder's path on ARC on the clipboard: " + QGuiApplication::clipboard()->text());
        id = idIn("verified", true);
        select(w, id);
        a.checkButton->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 2 && quiet(w); }) && optionsOf(lastCall("arc-check")) == QStringList({"--upload=" + id}) &&
              !a.actionDialog, "a demo upload is checked without a dialog");
        a.finderButton->click();
        check(opened.size() == 1 && opened.last() == QUrl::fromLocalFile(upload(a, id)["destination"].toObject()["local_final_path"].toString()), "Show in Finder");
        check(callsOf("arc-upload").isEmpty(), "no upload was made by any of these");
    }

    // Stop names the Globus sign-in only where a program has to sign in to stop the upload (section 8.6): a
    // request whose answer is unknown, a transfer that was still copying, a request that may have been sent. A
    // transfer that had already ended is stopped at once, without one.
    static void stopSignIn() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "uploads": [
            {"state": "not_watched", "created_utc": "2026-11-15T10:10:00.000Z"},
            {"state": "not_watched:verifying", "created_utc": "2026-11-15T10:09:00.000Z"},
            {"state": "uncertain", "created_utc": "2026-11-15T10:08:00.000Z"},
            {"state": "interrupted:submitting", "created_utc": "2026-11-15T10:07:00.000Z"},
            {"state": "interrupted:checking", "created_utc": "2026-11-15T10:06:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        const QString signIn = " A web page opens for you to sign in to Globus; then the transfer is cancelled.";
        const QList<QPair<QString, bool>> cases{{"2026-11-15T10:10:00.000Z", true}, {"2026-11-15T10:09:00.000Z", false},
                                                 {"2026-11-15T10:08:00.000Z", true}, {"2026-11-15T10:07:00.000Z", true},
                                                 {"2026-11-15T10:06:00.000Z", false}};
        for (const auto& [created, asks] : cases) {
            QString id;
            for (const QJsonValue& value : a.uploads)
                if (value.toObject()["created_utc"].toString() == created) id = value.toObject()["upload_id"].toString();
            check(!id.isEmpty(), "the upload made " + created);
            select(w, id);
            const QJsonObject item = upload(a, id);
            a.stopButton->click();
            QPointer<QDialog> dialog = dialogOf(a);
            check(dialog && dialog->windowTitle() == "Stop upload" && dialogText(dialog).endsWith(signIn) == asks,
                  QString("%1 (stored %2): %3").arg(item["display_state"].toString(), item["state"].toString(), dialogText(dialog)));
            button(*dialog, "Keep going")->click();
            check(waitUntil([&]{ return !dialog; }), "Keep going closes the question");
        }
        check(callsOf("arc-stop").isEmpty(), "Keep going sends nothing");
    }

    static void acquisitionAndRefusals() {
        resetStandIn();
        control(R"({"arc": {"uploads": [{"state": "not_watched"}, {"state": "conflict"}],
                    "sessions": [{"folder": "1d_x_scan_20261113_101500_123"},
                                 {"folder": "single_20261113_103000_456", "why_not": "settling", "retry_after_seconds": 42}]}})");
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        H::displayTimer(w)->stop();      // it would tell the panels every 200 ms that no acquisition runs
        // The control file's uploads keep its order: each is a second older than the one before (UI-V05).
        const QDateTime first = QDateTime::fromString(a.uploads.at(0).toObject()["created_utc"].toString(), Qt::ISODateWithMs);
        const QDateTime second = QDateTime::fromString(a.uploads.at(1).toObject()["created_utc"].toString(), Qt::ISODateWithMs);
        check(a.uploads.at(0).toObject()["display_state"].toString() == "not_watched" &&
              a.uploads.at(1).toObject()["display_state"].toString() == "conflict" && first.isValid() && second.msecsTo(first) >= 1000,
              "the stand-in's uploads are listed in the control file's order, a second apart");
        check(a.uploadButton->isEnabled(), "idle: Upload to ARC");
        p.setAcquisitionBusy(true);
        const QString later = "Upload to ARC is available when the scan or capture has ended.";
        check(!a.uploadButton->isEnabled() && a.uploadButton->toolTip() == later && a.statusLabel->text() == later &&
              a.statusLabel->tone() == UiTone::Neutral, "Upload disabled during an acquisition, with the tool tip");
        select(w, a.uploads.at(0).toObject()["upload_id"].toString());
        check(a.selectedUpload()["display_state"].toString() == "not_watched" && !a.checkButton->isEnabled() &&
              a.checkButton->toolTip() == "Available when the scan or capture has ended." && a.stopButton->isEnabled(),
              "what starts work waits for the end of the acquisition; Stop upload does not");
        select(w, a.uploads.at(1).toObject()["upload_id"].toString());
        check(!a.retryButton->isEnabled() && !a.keepCopyButton->isEnabled() && a.retryButton->toolTip() == "Available when the scan or capture has ended." &&
              a.stopButton->isEnabled(), "Retry and Keep as new copy wait as well");
        a.uploadButton->click();
        spin(300);
        check(callsOf("arc-upload").isEmpty(), "nothing is sent while an acquisition runs");
        a.loadSessions();
        check(waitUntil([&]{ return quiet(w); }) && optionsOf(lastCall("arc-sessions")) == QStringList({"--limit=30"}), "no --controller-idle during an acquisition");
        const qsizetype sessionsAsked = callsOf("arc-sessions").size();
        p.captureFinished();
        spin(300);
        check(callsOf("arc-sessions").size() == sessionsAsked, "a capture ended, but the acquisition goes on: the sessions wait");
        p.setAcquisitionBusy(false);
        check(waitUntil([&]{ return callsOf("arc-sessions").size() == sessionsAsked + 1 && quiet(w); }) &&
              optionsOf(lastCall("arc-sessions")) == QStringList({"--limit=30", "--controller-idle"}), "then they are read, with --controller-idle");
        check(a.uploadButton->isEnabled(), "and Upload to ARC is back");
        // A session that may not go yet.
        choose(a, 1);
        const QString settling = "This scan ended less than a minute ago, or is still being written. Try again in 42 s.";
        check(!a.uploadButton->isEnabled() && a.uploadButton->toolTip() == settling && a.statusLabel->text() == settling && a.statusLabel->tone() == UiTone::Warn,
              "a session that may not go: Upload disabled, the reason in its tool tip and in the status line");
        check(a.sessionBox->itemText(1).endsWith(QStringLiteral(" \u00B7 not ready")) && a.sessionBox->itemText(1).contains("ended 18 s ago"), "and in the Session box");
        check(a.sessionsLater.isActive() && a.sessionsLater.interval() == 42500, "the sessions are read again once it has been quiet long enough");
        // A command of the window on its way: no second one.
        choose(a, 0);
        control(R"({"arc-upload": {"mode": "slow", "seconds": 1.5}, "arc": {"uploads": [{"state": "not_watched"}, {"state": "conflict"}]}})");
        a.uploadButton->click();
        check(!a.uploadButton->isEnabled() && a.uploadButton->toolTip() == "Waiting for the answer to the last upload command.", "while it runs");
        a.uploadButton->click();
        check(waitUntil([&]{ return uploadsSent() == 1 && quiet(w); }, 20000) && callsOf("arc-upload").size() == 2, "one dry run, one upload");
        // A refusal says what it is and what to do.
        control(R"({"arc": {"upload": "not_ready:running"}})");
        a.forget();
        a.uploadButton->click();
        check(waitUntil([&]{ return quiet(w) && callsOf("arc-upload").size() == 3; }), "refused");
        check(a.statusLabel->text() == kLineSession + " is still being recorded. Wait until the scan has ended; the chart title then says finished, stopped or failed." &&
              a.statusLabel->tone() == UiTone::Warn, "the refusal and its fix: " + a.statusLabel->text());
        // Terminal could not be opened: the upload is made, its work did not start.
        control(R"({"arc": {"upload": "command:terminal_not_opened"}})");
        a.forget();
        a.uploadButton->click();
        check(waitUntil([&]{ return quiet(w) && uploadsSent() == 2; }), "sent");
        check(a.statusLabel->text() == "Upload failed: Terminal could not be opened: the stand-in was told so. Double-click ssh_upload.command in the "
                                       "upload's folder, or press Retry same request." && a.statusLabel->tone() == UiTone::Danger,
              "a failure in the danger tone: " + a.statusLabel->text());
        check(a.selectedUpload()["upload_id"].toString() == a.trackedUpload && a.selectedUpload()["display_state"].toString() == "failed",
              "the failed upload is selected");
    }

    static void settings() {
        resetStandIn();
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        check(a.settingsButton->text() == "ARC settings..." &&
              a.settingsButton->toolTip() == "Where uploads go, this Mac's Globus collection ID and your VT username.", "ARC settings...");
        control(R"({"arc-doctor": {"mode": "slow", "seconds": 1.0}})");
        a.settingsButton->click();
        QPointer<QDialog> dialog = a.settingsDialog;
        check(dialog && dialog->isVisible() && dialog->windowTitle() == "ARC settings" && dialog->windowModality() == Qt::NonModal, "the dialog, not modal");
        QPushButton* save = button(*dialog, "Save");
        check(!save->isEnabled(), "Save waits for arc-doctor");
        check(waitUntil([&]{ return save->isEnabled() && quiet(w); }) && optionsOf(lastCall("arc-doctor")).isEmpty(), "arc-doctor on open");
        auto* collection = dialog->findChild<QLineEdit*>("arcMacCollection");
        auto* username = dialog->findChild<QLineEdit*>("arcUsername");
        auto* folder = dialog->findChild<QLineEdit*>("arcFolderSetting");
        auto* note = dialog->findChild<QLabel*>("arcFolderNote");
        check(collection->text().isEmpty() && collection->placeholderText() == "paste the ID from Globus Connect Personal" &&
              username->text().isEmpty() && username->placeholderText() == "the part of your VT email before @vt.edu",
              "the VT username and the collection ID are empty by default, with no example value");
        check(folder->text() == kArcFolder && note->text() == "Each session goes into a folder of its own inside it. Globus reaches the same folder as "
                                                              "/pcl_photonics/raman_globus_upload.", "the ARC folder and where Globus reaches it");
        check(dialog->findChild<QLineEdit*>("arcCollectionRoot")->text() == "/projects" &&
              dialog->findChild<QLineEdit*>("arcSshHost")->text() == "datatransfer.arc.vt.edu" &&
              dialog->findChild<QLineEdit*>("arcClientId")->text() == "61338d24-54d5-408f-a10d-66c06b59f6d2" &&
              dialog->findChild<QWidget*>("arcMoreBox")->isHidden(), "More is closed, with the team's values in it");
        const QString checks = dialog->findChild<QLabel*>("arcChecks")->text();
        check(checks.contains("Missing: This Mac&#x27;s Globus collection ID is not set.") || checks.contains("Missing: This Mac's Globus collection ID is not set."),
              "Check setup lists what is missing: " + checks);
        check(checks.contains("OK: The ARC settings are valid."), "and what is fine");
        folder->setText("/projects");
        check(note->text() == "Use a folder under /projects/, such as /projects/pcl_photonics/raman_globus_upload.", "a folder that cannot be used");
        folder->setText("  /projects/pcl_photonics/raman_test ");
        check(note->text() == "Each session goes into a folder of its own inside it. Globus reaches the same folder as /pcl_photonics/raman_test.",
              "the caption follows the typing");
        username->setText(" standin-user ");
        // Refused: nothing is saved, the dialog stays open with the reason.
        control(R"({"arc": {"config_set": "error"}})");
        save->click();
        check(waitUntil([&]{ return callsOf("config").size() == 1 && quiet(w); }), "config --set");
        check(optionsOf(lastCall("config")) == QStringList({"--set=arc_upload.ssh.username=standin-user", "--set=arc_upload.folder=/projects/pcl_photonics/raman_test"}),
              "one config --set with exactly the changed keys, without the spaces around them");
        auto* result = dialog->findChild<QLabel*>("arcSettingsResult");
        check(dialog && dialog->isVisible() && !result->isHidden() && result->property("tone") == "danger" &&
              result->text() == "The ARC settings in catalog_config.json are not valid (arc_upload.folder: use a folder under /projects with two folders below it, "
                                "such as /projects/pcl_photonics/raman_globus_upload)", "a refusal keeps the dialog open, in the danger tone: " + result->text());
        // One at a time.
        a.settingsButton->click();
        int open = 0;
        for (QDialog* each : w.findChildren<QDialog*>()) open += each->isVisible() && each->windowTitle() == "ARC settings";
        check(open == 1 && a.settingsDialog == dialog, "one settings dialog at a time");
        // Saved: the dialog closes; the page reads the uploads and the sessions again.
        control(R"({})");
        dialog->findChild<QCheckBox*>("arcDemoDefault")->click();
        const qsizetype lists = callsOf("arc-list").size(), sessions = callsOf("arc-sessions").size();
        save->click();
        check(waitUntil([&]{ return !dialog && callsOf("arc-list").size() > lists && callsOf("arc-sessions").size() > sessions && quiet(w); }),
              "saved: closed, and read again");
        check(optionsOf(lastCall("config")) == QStringList({"--set=arc_upload.ssh.username=standin-user", "--set=arc_upload.folder=/projects/pcl_photonics/raman_test",
                                                            "--set=arc_upload.demo=false"}), "the changed keys");
        check(!a.demoBox->isChecked() && a.badge->text() == "ARC", "a changed default applies to the page");
        check(a.folderLabel->text() == "/projects/pcl_photonics/raman_test   (Globus: /pcl_photonics/raman_test)", "the new folder is shown");
        // Nothing changed: nothing is sent.
        a.settingsButton->click();
        dialog = a.settingsDialog;
        save = button(*dialog, "Save");
        check(waitUntil([&]{ return save->isEnabled() && quiet(w); }), "arc-doctor answered");
        check(dialog->findChild<QLineEdit*>("arcUsername")->text() == "standin-user", "the saved value is shown");
        const qsizetype configs = callsOf("config").size();
        save->click();
        check(waitUntil([&]{ return !dialog; }) && callsOf("config").size() == configs, "Save without a change sends nothing");
    }

    static void notAvailable() {
        resetStandIn();
        control(R"({"arc": {"absent": true}})");
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        H::tabs(w)->setCurrentIndex(1);
        p.arcToggle->click();
        ArcPanel& a = arc(w);
        check(waitUntil([&]{ return a.absent && quiet(w); }, 20000), "the data layer answers arc-list as an unknown command");
        check(a.statusLabel->text() == "Uploads to ARC are not part of this installation's data layer.", "the page says so");
        int controls = 0;
        for (QWidget* control : a.content->findChildren<QWidget*>()) {
            if (!qobject_cast<QPushButton*>(control) && !qobject_cast<QRadioButton*>(control) && !qobject_cast<QCheckBox*>(control) &&
                !qobject_cast<QComboBox*>(control)) continue;
            check(!control->isEnabled(), "every ARC control is disabled: " + control->objectName());
            ++controls;
        }
        check(controls == 13, QString("all of them: %1").arg(controls));
        check(!a.poll.isActive(), "and nothing is polled");
        const qsizetype asked = arcCommands().size();
        p.arcToggle->click(); p.arcToggle->click();
        spin(500);
        check(arcCommands().size() == asked, "shown again: nothing more is asked");
    }

    // Fix plan I1 and I3 (UI-01, SSH-01, SSH-V03, COR-V02): Check status of an upload on ARC says what it found;
    // a check that failed is shown in the warn tone in the row, the details, the Session box and the line;
    // "checked on" is the newest check that was ok; Stop is offered while a check runs, and ends only the check.
    static void checkResults() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "check": {"lists": 1}, "uploads": [
            {"state": "verified", "area": "real", "created_utc": "2026-11-15T10:03:00.000Z"},
            {"state": "verified", "demo": true, "created_utc": "2026-11-15T10:02:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        const QString live = idOf(a, "2026-11-15T10:03:00.000Z"), inDemo = idOf(a, "2026-11-15T10:02:00.000Z");
        const QString dot = QStringLiteral(" · ");
        check(a.sessionBox->itemText(1).endsWith(dot + "on ARC"), "the session is on ARC: " + a.sessionBox->itemText(1));
        select(w, live);
        a.checkButton->click();
        button(*dialogOf(a), "Check")->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 1 && quiet(w); }), "arc-check");
        // The check program works: the reply's words stand; Stop is offered, for the check only; Check is not.
        check(matches(a.statusLabel->text(), "Upload at " + kTime + ": Checking the upload\\. A web page opens for you to sign in to Globus\\.") &&
              a.statusLabel->tone() == UiTone::Info, "while the check runs: " + a.statusLabel->text());
        check(a.checkRuns(a.selectedUpload()) && a.stopButton->isEnabled() && !a.checkButton->isEnabled() &&
              a.stopButton->toolTip() == "Stop the check of this upload. Nothing is sent or deleted, and the upload stays as it is.",
              "Stop follows the upload's actions while the check runs");
        // It ends: the line says what it found, in the ok tone (I1, UI-01).
        listOnce(w);
        QJsonObject item = upload(a, live);
        QJsonObject last = item["last_check"].toObject();
        check(last["ok"].toBool() && !a.checkRuns(item) && item["display_state"].toString() == "verified", "the check found the copy");
        check(lineIs(a, "Checked at " + DataText::localTime(last["utc"].toString(), "HH:mm") + ": Still on ARC: 21 files with the right sizes.",
                     UiTone::Ok), "the check's result: " + a.statusLabel->text());
        check(a.checkButton->isEnabled() && a.copyPathButton->isEnabled() && !a.stopButton->isEnabled(), "the buttons of an upload on ARC again");
        listOnce(w);
        check(a.statusLabel->text().startsWith("Checked at ") && a.statusLabel->tone() == UiTone::Ok, "it stays");
        // A check that finds the copy gone: check_failed, in the warn tone, the check's words first.
        const QString gone = "The folder /pcl_photonics/raman_globus_upload/" + kLineSession + " is no longer on ARC.";
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "check": {"ok": false, "message": ")" + gone.toUtf8() + R"("}}})");
        a.checkButton->click();
        button(*dialogOf(a), "Check")->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 2 && quiet(w); }), "checked again");
        item = upload(a, live);
        last = item["last_check"].toObject();
        check(item["state"].toString() == "verified" && item["display_state"].toString() == "check_failed" &&
              item["display_label"].toString() == "On ARC, last check failed" && item["message"].toString() == gone &&
              item["actions"].toArray() == QJsonArray({"check", "copy_path"}), "the stand-in shows check_failed as the data layer does");
        check(lineIs(a, "Checked at " + DataText::localTime(last["utc"].toString(), "HH:mm") + ": " + gone, UiTone::Warn),
              "a failed check on the line, in the warn tone: " + a.statusLabel->text());
        QTableWidgetItem* state = a.recentTable->item(rowOf(a, live), 3);
        check(state->text() == "On ARC, last check failed" && state->foreground().color() == uiTheme().warnInk && state->toolTip().contains(gone),
              "the State cell: the label, the warn tone and the check's words: " + state->toolTip());
        check(waitUntil([&]{ return quiet(w) && a.details->toPlainText().contains("Last check failed ("); }) &&
              a.details->toPlainText().indexOf("Last check failed (") < a.details->toPlainText().indexOf("Globus checksums (MD5) match"),
              "the details: the failure first: " + a.details->toPlainText());
        check(waitUntil([&]{ return quiet(w) && a.sessionBox->itemText(1).endsWith(dot + "on ARC, last check failed"); }),
              "the Session box no longer says plainly that it is on ARC: " + a.sessionBox->itemText(1));
        // A data layer that lists such an upload as verified still gets it shown in the warn tone.
        check(ArcText::tone(QJsonObject{{"state", "verified"}, {"display_state", "verified"}, {"last_check", QJsonObject{{"ok", false}}}}) == UiTone::Warn &&
              ArcText::tone(QJsonObject{{"state", "verified"}, {"display_state", "verified"}, {"last_check", QJsonObject{{"ok", true}}}}) == UiTone::Ok,
              "a verified upload whose newest check failed is never in the ok tone");
        // Review REG-01: a check that could not look says nothing about the copy.
        check(ArcText::tone(QJsonObject{{"state", "verified"}, {"display_state", "verified"},
                                        {"last_check", QJsonObject{{"ok", false}, {"looked", false}}}}) == UiTone::Ok &&
              !ArcText::lastCheckFailed(QJsonObject{{"state", "verified"}, {"last_check", QJsonObject{{"ok", false}, {"looked", false}}}}),
              "a check that could not look is no failed check");
        // "Checked on": the newest check that was ok, else the check of the upload itself; never a failed one.
        QJsonObject shown = item;
        shown["verification"] = QJsonObject{{"method", "globus-checksum"}, {"algorithm", "MD5"}, {"utc", "2026-11-15T10:03:00.000Z"}};
        shown["last_check"] = QJsonObject{{"ok", false}, {"utc", "2026-11-15T16:30:00.000Z"}, {"message", gone}};
        QJsonObject destination = shown["destination"].toObject();
        destination["placed_as"] = kLineSession + "_copy2";
        shown["destination"] = destination;
        a.askAgain(a.choice(), shown);
        check(dialogText(dialogOf(a)).contains(", checked on " + DataText::localTime("2026-11-15T10:03:00.000Z", "yyyy-MM-dd HH:mm") + ", at ") &&
              dialogText(dialogOf(a)).endsWith("The new copy goes beside it as " + kLineSession + "_copy3 (or the next free name)."),
              "a failed check is not the date it was checked on; the new copy follows the name it has: " + dialogText(dialogOf(a)));
        shown["last_check"] = QJsonObject{{"ok", true}, {"utc", "2026-11-15T16:40:00.000Z"}, {"message", "Still on ARC."}};
        a.askAgain(a.choice(), shown);
        check(dialogText(dialogOf(a)).contains(", checked on " + DataText::localTime("2026-11-15T16:40:00.000Z", "yyyy-MM-dd HH:mm") + ", at "),
              "an ok check is: " + dialogText(dialogOf(a)));
        button(*a.actionDialog, "Cancel")->click();
        // Stop while a check runs: the check ends, the upload stays as it was, and the line says so again.
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "check": {"lists": 3}}})");
        select(w, live);
        a.checkButton->click();
        button(*dialogOf(a), "Check")->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 3 && quiet(w); }) && a.stopButton->isEnabled(), "a third check runs");
        a.stopButton->click();
        QPointer<QDialog> dialog = dialogOf(a);
        check(dialog->windowTitle() == "Stop upload" &&
              dialogText(dialog) == "Stop the check of this upload? Nothing is sent or deleted, and the upload stays as it is.",
              "Stop asks about the check: " + dialogText(dialog));
        button(*dialog, "Stop the check")->click();
        check(waitUntil([&]{ return callsOf("arc-stop").size() == 1 && quiet(w); }) && optionsOf(lastCall("arc-stop")) == QStringList({"--upload=" + live}),
              "arc-stop --upload=<id>");
        item = upload(a, live);
        check(!a.checkRuns(item) && item["display_state"].toString() == "check_failed" && item["last_check"].toObject() == last,
              "the check ended without a result; the upload is as it was");
        check(lineIs(a, "Upload at " + DataText::localTime(item["updated_utc"].toString(), "HH:mm:ss") + ": " + gone, UiTone::Warn),
              "the line follows the upload again once the check program has ended: " + a.statusLabel->text());
        check(a.checkButton->isEnabled() && !a.stopButton->isEnabled(), "Check status again, no Stop");
        // Review REG-01: a later check that could not look leaves check_failed as it is and says why, with what to do.
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "check": {"ok": false, "looked": false}}})");
        a.checkButton->click();
        button(*dialogOf(a), "Check")->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 4 && quiet(w); }), "a check that could not look");
        item = upload(a, live);
        QJsonObject note = item["last_check"].toObject();
        const QString notLooked = "You did not finish signing in within 10 minutes. Check status could not look.";
        const QString checkAgain = "Press Check status again and sign in when the page opens.";
        check(note["looked"] == QJsonValue(false) && note["fix"].toString() == checkAgain && item["display_state"].toString() == "check_failed" &&
              item["message"].toString() == gone, "the copy found gone is still what is shown: " + item["message"].toString());
        check(lineIs(a, "Checked at " + DataText::localTime(note["utc"].toString(), "HH:mm") + ": " + notLooked + " " + checkAgain, UiTone::Warn),
              "the line says why it could not look, and what to do: " + a.statusLabel->text());
        check(waitUntil([&]{ return quiet(w) && a.details->toPlainText().contains("newest check ("); }) &&
              a.details->toPlainText().contains("A check failed: " + gone) && a.details->toPlainText().contains(checkAgain),
              "the details: the failed check, then the note: " + a.details->toPlainText());
        // A demo upload: checked without a dialog; an ok check of a demo is never in the ok tone.
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}}})");
        select(w, inDemo);
        a.checkButton->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 5 && quiet(w); }) && !a.actionDialog, "a demo upload is checked without a dialog");
        last = upload(a, inDemo)["last_check"].toObject();
        check(lineIs(a, "Checked at " + DataText::localTime(last["utc"].toString(), "HH:mm") +
                        ": Demo: still in the demo folder: 21 files with the right sizes.", UiTone::Warn),
              "the demo's check: " + a.statusLabel->text());
        // Review R2-05: a demo check that finds the copy gone. The details name the demo folder, never ARC, word for word.
        const QString demoGone = "Demo: the folder " + upload(a, inDemo)["destination"].toObject()["local_final_path"].toString() +
                                 " is no longer in the demo folder.";
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "check": {"ok": false, "message": ")" + demoGone.toUtf8() + R"("}}})");
        a.checkButton->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 6 && quiet(w); }), "the demo upload is checked again");
        listOnce(w);
        check(upload(a, inDemo)["display_state"].toString() == "check_failed", "the demo's copy is gone: " + upload(a, inDemo)["display_state"].toString());
        check(waitUntil([&]{ return quiet(w) && a.details->toPlainText().contains("Last check failed ("); }) &&
                  a.details->toPlainText().contains(demoGone + "; when it was put in the demo folder: Demo: Globus checksums (MD5) match this Mac's") &&
                  !a.details->toPlainText().contains("when it was put on ARC") && !a.details->toPlainText().contains(" on ARC"),
              "the details of a demo upload whose check failed: " + a.details->toPlainText());
        check(callsOf("arc-upload").isEmpty(), "nothing was uploaded");
    }

    // Review REG-01: a check that could not look (a sign-in that did not happen, ssh that ended with 255, Globus out of
    // reach) says nothing about the copy. The upload stays On ARC, checked, in the ok tone; the line, the State tool tip
    // and the details say why and what to do; the session is still on ARC.
    static void checkThatCouldNotLook() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "check": {"ok": false, "looked": false}, "uploads": [
            {"state": "verified", "area": "real", "created_utc": "2026-11-15T10:03:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        const QString id = idOf(a, "2026-11-15T10:03:00.000Z");
        select(w, id);
        a.checkButton->click();
        button(*dialogOf(a), "Check")->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 1 && quiet(w); }), "arc-check");
        listOnce(w);
        const QJsonObject item = upload(a, id);
        const QJsonObject note = item["last_check"].toObject();
        const QString why = "You did not finish signing in within 10 minutes. Check status could not look.";
        const QString fix = "Press Check status again and sign in when the page opens.";
        check(note["looked"] == QJsonValue(false) && note["ok"] == QJsonValue(false) && item["display_state"].toString() == "verified",
              "the stand-in: a check that could not look, the upload still on ARC");
        check(lineIs(a, "Checked at " + DataText::localTime(note["utc"].toString(), "HH:mm") + ": " + why + " " + fix, UiTone::Warn),
              "the line: why, and what to do: " + a.statusLabel->text());
        QTableWidgetItem* state = a.recentTable->item(rowOf(a, id), 3);
        check(state->text() == "On ARC, checked" && state->foreground().color() == uiTheme().ink(UiTone::Ok) &&
              state->toolTip().contains(why + " " + fix), "the State cell: On ARC, checked, the ok tone, the note: " + state->toolTip());
        check(waitUntil([&]{ return quiet(w) && a.details->toPlainText().contains("newest check ("); }) &&
              !a.details->toPlainText().contains("Last check failed") && a.details->toPlainText().contains(why + " " + fix),
              "the details: a note, not a failed check: " + a.details->toPlainText());
        check(a.checkButton->isEnabled() && a.copyPathButton->isEnabled(), "Check status again, Copy ARC path");
        check(waitUntil([&]{ return quiet(w) && a.sessionBox->itemText(1).endsWith(QStringLiteral(" \u00B7 on ARC")); }),
              "the session is still on ARC: " + a.sessionBox->itemText(1));
    }

    // A Globus check of an upload whose transfer may still run cancels that transfer when it is stopped, as Stop upload
    // does (the data layer's decision I3 as built): the question and the tool tip say so instead of "the upload stays".
    static void stopOfACheckThatCancels() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "uploads": [
            {"state": "placing", "area": "real", "check_worker": {"lists": 99}, "created_utc": "2026-11-15T10:06:00.000Z"},
            {"state": "uncertain", "demo": true, "check_worker": {"lists": 99}, "created_utc": "2026-11-15T10:05:00.000Z"},
            {"state": "verifying", "area": "real", "check_worker": {"lists": 99}, "created_utc": "2026-11-15T10:04:00.000Z"},
            {"state": "uncertain", "area": "real", "check_worker": {"lists": 99}, "created_utc": "2026-11-15T10:03:00.000Z"},
            {"state": "verified", "area": "real", "check_worker": {"lists": 99}, "created_utc": "2026-11-15T10:02:00.000Z"},
            {"state": "failed", "area": "real", "may_run": true, "check_worker": {"lists": 99}, "created_utc": "2026-11-15T10:01:00.000Z"},
            {"state": "failed", "area": "real", "check_worker": {"lists": 99}, "created_utc": "2026-11-15T10:00:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        const QString uncertain = idOf(a, "2026-11-15T10:03:00.000Z"), onArc = idOf(a, "2026-11-15T10:02:00.000Z");
        const QString failedRuns = idOf(a, "2026-11-15T10:01:00.000Z"), failedDone = idOf(a, "2026-11-15T10:00:00.000Z");
        select(w, uncertain);
        QJsonObject item = upload(a, uncertain);
        check(a.checkRuns(item) && a.checkMayCancel(item) && a.stopButton->isEnabled() && a.stopButton->toolTip() == ArcPanel::kStopCheckCancelsTip,
              "a check of an upload Globus may still copy: Stop cancels the transfer: " + a.stopButton->toolTip());
        a.stopButton->click();
        QPointer<QDialog> dialog = dialogOf(a);
        check(dialog->windowTitle() == "Stop upload" &&
                  dialogText(dialog) == "Stop this upload? The check ends, and the transfer Globus may still be running is cancelled (after the Globus "
                                        "sign-in, if that page is still open). Files already copied stay in " +
                                            item["destination"].toObject()["incoming_path"].toString() + " on ARC; nothing is deleted." &&
                  button(*dialog, "Stop the upload") && button(*dialog, "Keep going"),
              "the question says the transfer is cancelled: " + dialogText(dialog));
        button(*dialog, "Keep going")->click();
        spin(300);
        check(callsOf("arc-stop").isEmpty(), "Keep going: nothing is sent");
        // A check of an upload on ARC only ends.
        select(w, onArc);
        item = upload(a, onArc);
        check(a.checkRuns(item) && !a.checkMayCancel(item) && a.stopButton->toolTip() == ArcPanel::kStopCheckTip,
              "a check of an upload on ARC only ends: " + a.stopButton->toolTip());
        a.stopButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog) == "Stop the check of this upload? Nothing is sent or deleted, and the upload stays as it is.",
              "the check's own question: " + dialogText(dialog));
        button(*dialog, "Keep going")->click();
        // Review REG-03: a failed upload whose transfer Globus may still run (the listing's may_run, whatever the stored
        // state): stopping its check cancels that transfer, and the question and the tool tip say so.
        select(w, failedRuns);
        item = upload(a, failedRuns);
        check(item["may_run"].toBool() && a.checkRuns(item) && a.checkMayCancel(item) && a.stopButton->toolTip() == ArcPanel::kStopCheckCancelsTip,
              "a check of a failed upload whose transfer may run: " + a.stopButton->toolTip());
        a.stopButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog).startsWith("Stop this upload? The check ends, and the transfer Globus may still be running is cancelled") &&
                  button(*dialog, "Stop the upload"),
              "the question says the transfer is cancelled: " + dialogText(dialog));
        button(*dialog, "Keep going")->click();
        // A failed upload whose transfer has ended: its check only ends.
        select(w, failedDone);
        item = upload(a, failedDone);
        check(!item["may_run"].toBool() && a.checkRuns(item) && !a.checkMayCancel(item) && a.stopButton->toolTip() == ArcPanel::kStopCheckTip,
              "a check of a failed upload whose transfer has ended: " + a.stopButton->toolTip());
        // Reviews R2-01 and R2-04: a Globus check that checks or places a copy whose transfer has ended (its task SUCCEEDED):
        // nothing runs on Globus (may_run false, as the data layer lists it), so a stop ends the check and the upload stays as it is.
        for (const QString& created : QStringList{"2026-11-15T10:04:00.000Z", "2026-11-15T10:06:00.000Z"}) {
            const QString id = idOf(a, created);
            select(w, id);
            item = upload(a, id);
            check(!item["may_run"].toBool() && a.checkRuns(item) && !a.checkMayCancel(item) && a.stopButton->toolTip() == ArcPanel::kStopCheckTip,
                  "a check of an upload in " + item["state"].toString() + ": " + a.stopButton->toolTip());
            a.stopButton->click();
            dialog = dialogOf(a);
            check(dialogText(dialog) == "Stop the check of this upload? Nothing is sent or deleted, and the upload stays as it is.",
                  "the check's own question for " + item["state"].toString() + ": " + dialogText(dialog));
            button(*dialog, "Keep going")->click();
        }
        // Review R2-05: a demo upload whose transfer the demo's Globus may still run: the demo's words, exactly (its Stop asks nothing).
        const QString demoRuns = idOf(a, "2026-11-15T10:05:00.000Z");
        select(w, demoRuns);
        item = upload(a, demoRuns);
        check(item["demo"].toBool() && a.checkMayCancel(item) && a.stopButton->toolTip() == ArcPanel::kStopCheckCancelsDemoTip &&
                  ArcPanel::kStopCheckCancelsDemoTip == "Stop this upload: the check ends, and the transfer the demo's Globus may still be running is "
                                                        "cancelled. Files already copied stay in _incoming of the demo folder; nothing is deleted.",
              "the Stop tool tip of a check of a demo upload: " + a.stopButton->toolTip());
        spin(200);
        check(callsOf("arc-stop").isEmpty(), "nothing was stopped");
    }

    // UI-09 and the rest of UI-01: an upload stopped at once is shown in the warn tone, and an upload found
    // waiting for the person says what it waits for, in its tone; both stay right at the next lists.
    static void restingLines() {
        resetStandIn();
        control(R"({"arc": {"uploads": [
            {"state": "conflict", "demo": true, "created_utc": "2026-11-15T10:07:00.000Z"},
            {"state": "conflict", "transport": "ssh", "demo": true, "session": "single_20261113_103000_456",
             "created_utc": "2026-11-15T10:06:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        const QString waiting = idOf(a, "2026-11-15T10:07:00.000Z"), other = idOf(a, "2026-11-15T10:06:00.000Z");
        choose(a, 1);
        check(a.selectedSession()["folder"].toString() == kLineSession, "the session whose upload waits");
        a.uploadButton->click();
        check(waitUntil([&]{ return callsOf("arc-upload").size() == 1 && quiet(w); }) && uploadsSent() == 0, "only the dry run");
        const QJsonObject item = upload(a, waiting);
        const QString expected = "An upload of this session waits for you (started " + DataText::localTime(item["created_utc"].toString(), "HH:mm") +
                                 "): " + item["message"].toString();
        check(a.trackedUpload == waiting && lineIs(a, expected, UiTone::Warn), "the upload found, in its tone: " + a.statusLabel->text());
        listOnce(w);
        listOnce(w);
        check(lineIs(a, expected, UiTone::Warn), "and at the next lists: " + a.statusLabel->text());
        // Stopped at once (nothing runs for it): the final words, in the warn tone, now and later.
        select(w, other);
        a.stopButton->click();
        check(waitUntil([&]{ return callsOf("arc-stop").size() == 1 && quiet(w); }), "arc-stop of a demo upload, without a dialog");
        const QJsonObject stopped = upload(a, other);
        check(stopped["display_state"].toString() == "stopped", "stopped at once");
        check(matches(a.statusLabel->text(), "Demo upload at " + kTime + " \\(nothing leaves this Mac\\): " +
                                                 QRegularExpression::escape(stopped["message"].toString())) &&
              a.statusLabel->tone() == UiTone::Warn, "an upload stopped at once, in the warn tone: " + a.statusLabel->text());
        listOnce(w);
        check(a.statusLabel->tone() == UiTone::Warn && a.statusLabel->text().endsWith(stopped["message"].toString()), "it stays so");
        // Review COR-05: a demo upload's files are in the demo folder on this Mac; the details and the Stop tool tip say so.
        const QString leftover = upload(a, other)["leftover_on_arc"].toString();
        check(leftover.startsWith(QDir::cleanPath(home() + "/data_catalog/arc_demo") + kArcFolder + "/"), "the demo's leftover folder: " + leftover);
        check(waitUntil([&]{ return quiet(w) && a.details->toPlainText().contains("Left in the demo folder"); }) &&
                  a.details->toPlainText().contains("Files copied so far are in " + leftover + "; nothing was deleted.") &&
                  !a.details->toPlainText().contains("Left on ARC"),
              "the details of a demo upload: " + a.details->toPlainText());
        select(w, waiting);
        check(a.stopButton->toolTip() == ArcPanel::kStopDemoTip &&
                  ArcPanel::kStopDemoTip == "Stop this upload. Files already copied stay in _incoming of the demo folder; nothing is deleted.",
              "the Stop tool tip of a demo upload: " + a.stopButton->toolTip());
        // Review COR-05: Retry same request and Check status of a demo upload name the demo folder, with no sign-in.
        check(a.retryButton->isEnabled() && a.retryButton->toolTip() == ArcPanel::kRetryDemoTip &&
                  ArcPanel::kRetryDemoTip == "Continue this upload with the same files and the same place in the demo folder. Globus: the same "
                                             "request number while it is not known whether the demo's Globus got it; after a failed transfer, the "
                                             "files are copied again into the same _incoming folder. SSH: the files are sent again unless the "
                                             "demo folder has them all.",
              "the Retry tool tip of a demo upload: " + a.retryButton->toolTip());
        check(a.checkButton->toolTip() == ArcPanel::kCheckDemoTip &&
                  ArcPanel::kCheckDemoTip == "Look at how this upload stands in the demo folder on this Mac. Nothing is sent, and nothing asks you "
                                             "to sign in.",
              "the Check status tool tip of a demo upload: " + a.checkButton->toolTip());
        for (const QString& tip : {a.retryButton->toolTip(), a.checkButton->toolTip(), a.stopButton->toolTip()})
            check(!tip.contains("ARC") && !tip.contains("Terminal") && !tip.contains("sign in again"), "a demo tool tip names no far end: " + tip);
    }

    // UI-03: the focus monitor tells the window busy, idle, busy... several times a second. The uploads are
    // still read (at the busy interval), and Upload to ARC does not blink.
    static void acquisitionFlips() {
        resetStandIn();
        QJsonArray script{"queued", "preparing"};
        for (int file = 1; file <= 60; ++file) script << QString("transferring:%1/61").arg(file);
        control(QJsonDocument(QJsonObject{{"arc", QJsonObject{{"script", script}}}}).toJson());
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        ArcPanel& a = openPage(w);
        H::displayTimer(w)->stop();      // the test plays the monitor itself
        a.uploadButton->click();
        check(waitUntil([&]{ return uploadsSent() == 1 && a.active > 0 && quiet(w); }), "an upload is worked on");
        a.activePollMs = 200;
        a.busyPollMs = 1000;
        a.idlePollMs = 5000;
        a.poll.start(a.pollInterval());
        const qsizetype before = callsOf("arc-list").size();
        p.setAcquisitionBusy(true);
        check(!a.uploadButton->isEnabled(), "the monitor's first frame: Upload to ARC waits");
        QElapsedTimer clock;
        clock.start();
        bool busy = true, enabled = a.uploadButton->isEnabled();
        int changes = 0;
        while (clock.elapsed() < 6000) {
            // 600 ms frames, 150 ms between them, sampled as the window's 200 ms timer would.
            const bool now = clock.elapsed() % 750 < 600;
            if (now != busy) {
                busy = now;
                p.setAcquisitionBusy(busy);
            }
            QCoreApplication::processEvents();
            QThread::msleep(5);
            if (a.uploadButton->isEnabled() != enabled) {
                enabled = !enabled;
                ++changes;
            }
        }
        const qsizetype lists = callsOf("arc-list").size() - before;
        check(lists >= 3 && lists <= 8, QString("the uploads are read during the monitor, at the busy interval: %1 lists in 6 s").arg(lists));
        check(changes == 0 && !a.uploadButton->isEnabled() && a.uploadButton->toolTip() == "Upload to ARC is available when the scan or capture has ended.",
              QString("Upload to ARC stays disabled, without blinking: %1 changes").arg(changes));
        p.setAcquisitionBusy(false);
        check(waitUntil([&]{ return !a.acquisitionBusy && quiet(w); }, 5000) && a.uploadButton->isEnabled(), "the monitor is off: Upload to ARC is back");
    }

    // UI-04: an upload that no program holds yet but that is about to be worked on (waiting for Terminal) is
    // read at the short interval, and so are the uploads for a while after an action of this window.
    static void pollingSoon() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "uploads": [
            {"state": "waiting_for_terminal", "transport": "ssh", "worker": false, "created_utc": "2026-11-15T10:11:00.000Z"},
            {"state": "verified", "demo": true, "created_utc": "2026-11-15T10:03:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        const QJsonObject waiting = upload(a, idOf(a, "2026-11-15T10:11:00.000Z"));
        check(a.active == 0 && waiting["needs"].toString() == "terminal" && !waiting["worker"].toObject()["alive"].toBool(),
              "no program holds it yet; it waits for Terminal");
        check(a.poll.isActive() && a.poll.interval() == 2000, QString("read every 2 s, not 30 s: %1").arg(a.poll.interval()));
        // Without it: 30 s, except for a while after an action of this window.
        QFile::remove(home() + "/fake_data_layer_state.json");
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "uploads": [
            {"state": "verified", "demo": true, "created_utc": "2026-11-15T10:03:00.000Z"}]}})");
        listOnce(w);
        check(a.uploads.size() == 1 && a.poll.interval() == 30000, QString("nothing about to be worked on: 30 s (%1)").arg(a.poll.interval()));
        a.afterActionMs = 3000;
        select(w, a.uploads.at(0).toObject()["upload_id"].toString());
        a.checkButton->click();
        check(waitUntil([&]{ return callsOf("arc-check").size() == 1 && quiet(w); }), "Check status of the demo upload");
        check(a.active == 0 && a.poll.interval() == 2000 && a.pollInterval() == 2000, "after an action of this window: 2 s");
        spin(3100);
        check(a.pollInterval() == 30000, "a while later: 30 s again");
    }

    // UI-08: a demo SSH upload is counted in bytes in the bar too, as its status line counts them.
    static void sshProgress() {
        resetStandIn();
        control(R"({"arc": {"uploads": [{"state": "transferring:12/21", "transport": "ssh", "demo": true}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        const QJsonObject item = a.selectedUpload();
        check(item["transport"].toString() == "ssh" && item["demo"].toBool() && item["message"].toString() == "Demo: copying over SSH: 57% (0.25 of 0.43 MB).",
              "the demo's SSH line counts bytes: " + item["message"].toString());
        check(a.progressBar->isVisible() && a.progressBar->maximum() == 100 && a.progressBar->value() == 57 && a.progressBar->text() == "57%",
              "the bar too: " + a.progressBar->text());
    }

    // UI-11 and the window side of SEC-03: words of the confirmation and of the questions about an SSH upload.
    static void dialogWords() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []},
            "sessions": [{"folder": "1d_x_scan_20261113_101500_123", "kind": "real", "dark": "missing", "sample_id": "S-001"}],
            "uploads": [
                {"state": "verified", "transport": "ssh", "area": "real", "session": "single_20261113_103000_456",
                 "ssh_username": "standin-user", "created_utc": "2026-11-15T10:05:00.000Z"},
                {"state": "failed:ssh_interrupted", "transport": "ssh", "area": "real", "session": "single_20261113_103000_456",
                 "ssh_username": "standin-user", "created_utc": "2026-11-15T10:04:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        a.demoBox->click();
        check(waitUntil([&]{ return quiet(w) && a.uploadButton->isEnabled(); }), "a live upload may be asked for");
        a.uploadButton->click();
        QPointer<QDialog> dialog = dialogOf(a);
        check(dialogText(dialog).contains("\n13 spectra (finished), 21 files, 433 kB, no dark spectrum, and a list of every file with its SHA-256.\n") &&
              !dialogText(dialog).contains("without a dark spectrum"), "no dark: the list is not missing too: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();
        // An SSH upload signs in with the VT username saved with it: the questions name that account.
        select(w, idOf(a, "2026-11-15T10:05:00.000Z"));
        a.checkButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog) == "Check this upload on ARC? A Terminal window opens for the VT password of standin-user@datatransfer.arc.vt.edu "
                                    "and Duo, once. Nothing is sent.", "Check status names the account: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();
        select(w, idOf(a, "2026-11-15T10:04:00.000Z"));
        a.retryButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog).contains(" A Terminal window opens for the VT password of standin-user@datatransfer.arc.vt.edu and Duo; the files are "
                                          "sent again into the same folder on ARC unless ARC has them all. "), "Retry names the account: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();
        check(callsOf("arc-check").isEmpty() && callsOf("arc-retry").isEmpty() && uploadsSent() == 0, "Cancel sends nothing");
        // Review DOC-V02: Retry same request promises the same request number only while Globus may have the request.
        check(a.retryButton->toolTip() == ArcPanel::kRetryTip && !a.retryButton->toolTip().contains("copied twice") &&
                  a.retryButton->toolTip().contains("after a failed transfer, the files are copied again into the same _incoming folder"),
              "the Retry tool tip: " + a.retryButton->toolTip());
        check(a.checkButton->toolTip() == ArcPanel::kCheckTip && a.checkButton->toolTip().contains("SSH: a Terminal window opens"),
              "the Check status tool tip of a live upload: " + a.checkButton->toolTip());
        // With the team's file of ARC's keys, ssh does not ask about ARC's key: the confirmation leaves that step out (I9).
        bool answered = false;
        a.dataLayer->run({"config", "--set=arc_upload.ssh.username=standin-user", "--set=arc_upload.ssh.known_hosts=/Users/Shared/arc_known_hosts"},
                         [&](const DataLayerReply&) { answered = true; });
        check(waitUntil([&]{ return answered; }), "the team's file is set");
        listOnce(w);
        a.sshRadio->click();
        check(waitUntil([&]{ return quiet(w) && a.uploadButton->isEnabled(); }), "SSH may be used");
        a.uploadButton->click();
        dialog = dialogOf(a);
        check(dialogText(dialog).contains("\n     ssh asks, in this order:\n     1. Your VT password. Typing shows nothing; that is normal. Press Return.\n"
                                          "     2. Duo: ") &&
              dialogText(dialog).contains("\n     ssh checks ARC's key against the team's file /Users/Shared/arc_known_hosts; it does not ask.\n") &&
              !dialogText(dialog).contains("(yes/no/[fingerprint])"), "no question about the key: " + dialogText(dialog));
        button(*dialog, "Cancel")->click();
    }

    // UI-07: a folder the data layer stops at before it counts it (a name it does not take) is listed without
    // the count and the end that arc-sessions gives it (0, "ended (no end mark)").
    static void uncountedSession() {
        resetStandIn();
        control(R"({"arc": {"sessions": [{"folder": "1d_x_scan_20261113_101500_123"},
            {"folder": "copy of scan", "why_not": "odd_name", "points": 124, "end": "finished"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        const QString dot = QStringLiteral(" · ");
        const QJsonObject odd = a.sessions.at(1).toObject();
        check(odd["points"].toInt() == 0 && odd["end_text"].toString() == "ended (no end mark)" &&
              odd["why_not"].toObject()["reason"].toString() == "odd_name", "the stand-in reports it as the data layer does");
        check(a.sessionBox->itemText(1) == "copy of scan" + dot + DataText::none() + dot + "simulation not recorded" + dot + "not ready",
              "no \"0 spectra\" and no end: " + a.sessionBox->itemText(1));
        check(a.sessionBox->itemText(0).contains(dot + "13 spectra" + dot + "finished"), "a counted session keeps both");
    }

    // UI-V02: the Session box lists the newest 30 sessions and says when there are more; its last entry lists them.
    static void moreSessions() {
        resetStandIn();
        QJsonArray specs;
        for (int index = 0; index < 33; ++index)
            specs << QJsonObject{{"folder", QString("single_20261114_%1_%2").arg(235900 - index * 100, 6, 10, QChar('0')).arg(index, 3, 10, QChar('0'))},
                                 {"scan_mode", "single"}, {"points", 1}};
        control(QJsonDocument(QJsonObject{{"arc", QJsonObject{{"sessions", specs}}}}).toJson());
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        check(optionsOf(lastCall("arc-sessions")) == QStringList({"--limit=30", "--controller-idle"}), "the newest 30 are asked for");
        check(a.sessions.size() == 30 && a.sessionsTotal == 33 && a.sessionBox->count() == 31 &&
              a.sessionBox->itemText(30) == "Show more sessions (30 of 33 listed)" &&
              a.sessionBox->itemData(30, Qt::ToolTipRole).toString() == "Only the newest 30 sessions are listed. Choose this to list older ones too.",
              "the box says that the list is cut: " + a.sessionBox->itemText(30));
        choose(a, 5);
        const QString chosen = a.selectedSession()["folder"].toString();
        choose(a, 30);
        check(a.sessionBox->currentIndex() == 5 && a.selectedSession()["folder"].toString() == chosen, "the chosen session stays chosen");
        check(waitUntil([&]{ return quiet(w) && a.sessions.size() == 33; }) &&
              optionsOf(lastCall("arc-sessions")) == QStringList({"--limit=130", "--controller-idle"}), "more are asked for");
        check(a.sessionBox->count() == 33 && a.selectedSession()["folder"].toString() == chosen && a.sessionBox->itemText(32).startsWith(
              specs.at(32).toObject()["folder"].toString()), "all are listed, the oldest last, and nothing says more now");
        check(a.uploadButton->isEnabled() && a.statusLabel->text().startsWith("Ready for a demo upload"), "the chosen one may go");
    }

    // UI-V03: new ARC settings close a live confirmation that names the old folder or account.
    static void settingsCloseConfirmation() {
        resetStandIn();
        control(R"({"arc": {"ready": {"globus": [], "ssh": []}, "sessions": [{"folder": "1d_x_scan_20261113_101500_123", "kind": "real"}]}})");
        MainWindow w;
        prepare(w);
        ArcPanel& a = openPage(w);
        noPolling(a);
        a.demoBox->click();
        check(waitUntil([&]{ return quiet(w) && a.uploadButton->isEnabled(); }), "live");
        a.uploadButton->click();
        QPointer<QDialog> confirmation = dialogOf(a);
        check(confirmation->windowTitle() == "Upload to ARC" && dialogText(confirmation).contains("\nTo:  " + kArcFolder + "/" + kLineSession + "\n"),
              "the confirmation names the folder");
        a.settingsButton->click();
        QPointer<QDialog> settings = a.settingsDialog;
        QPushButton* save = button(*settings, "Save");
        check(waitUntil([&]{ return save->isEnabled() && quiet(w); }), "arc-doctor answered");
        check(confirmation && confirmation->isVisible(), "both are open");
        settings->findChild<QLineEdit*>("arcFolderSetting")->setText("/projects/pcl_photonics/raman_test");
        save->click();
        check(waitUntil([&]{ return !settings && !confirmation && quiet(w); }), "saved: the confirmation was closed with the settings");
        check(lineIs(a, "The ARC settings changed, so the open confirmation was closed. Press Upload to ARC again to see where the session goes now.",
                     UiTone::Info), "the line says why: " + a.statusLabel->text());
        check(uploadsSent() == 0, "nothing was sent");
        a.uploadButton->click();
        confirmation = dialogOf(a);
        check(dialogText(confirmation).contains("\nTo:  /projects/pcl_photonics/raman_test/" + kLineSession + "\n"),
              "the next confirmation names the new folder: " + dialogText(confirmation));
        button(*confirmation, "Cancel")->click();
    }

    // SEC-04: a value the data layer refuses stops every arc-* command; ARC settings shows every setting, opens
    // More when one is refused, and Save corrects it.
    static void settingsRepair() {
        resetStandIn();
        control(R"({"arc": {"settings": {"arc_upload.settle_seconds": 5, "arc_upload.globus.deadline_hours": 0}}})");
        MainWindow w;
        prepare(w);
        DataPanels& p = H::panels(w);
        H::tabs(w)->setCurrentIndex(1);
        p.arcToggle->click();
        ArcPanel& a = arc(w);
        check(waitUntil([&]{ return a.everShown && !a.listProblem.isEmpty() && quiet(w); }, 20000), "arc-list is refused");
        check(a.statusLabel->text() == "The ARC settings are not valid: arc_upload.settle_seconds must be a whole number of seconds from 10 to 3600; "
                                       "arc_upload.globus.deadline_hours must be a whole number of hours from 1 to 168. Correct them in ARC settings "
                                       "(command line: config --set key=value)." && a.statusLabel->tone() == UiTone::Warn,
              "the page says what to correct, and where: " + a.statusLabel->text());
        check(a.settingsButton->isEnabled(), "ARC settings can be opened");
        a.settingsButton->click();
        QPointer<QDialog> dialog = a.settingsDialog;
        QPushButton* save = button(*dialog, "Save");
        check(waitUntil([&]{ return save->isEnabled() && quiet(w); }), "arc-doctor answered");
        auto* settle = dialog->findChild<QLineEdit*>("arcSettleSeconds");
        auto* deadline = dialog->findChild<QLineEdit*>("arcDeadlineHours");
        auto* demoFolder = dialog->findChild<QLineEdit*>("arcDemoFolder");
        check(settle && deadline && demoFolder, "the dialog has a field for each of them");
        check(!dialog->findChild<QWidget*>("arcMoreBox")->isHidden() && dialog->findChild<QToolButton*>("arcMore")->isChecked(),
              "More is open: a value there is refused");
        check(settle->text() == "5" && deadline->text() == "0" && demoFolder->text() == "data_catalog/arc_demo", "the values as they are");
        check(dialog->findChild<QLabel*>("arcChecks")->text().contains("Missing: arc_upload.settle_seconds must be"), "Check setup names them");
        settle->setText(" 60 ");
        save->click();
        check(waitUntil([&]{ return callsOf("config").size() == 1 && quiet(w); }), "config --set");
        auto* result = dialog->findChild<QLabel*>("arcSettingsResult");
        check(optionsOf(lastCall("config")) == QStringList({"--set=arc_upload.settle_seconds=60"}) && dialog && !result->isHidden() &&
              result->text() == "The ARC settings in catalog_config.json are not valid (arc_upload.globus.deadline_hours must be a whole number of hours "
                                "from 1 to 168)", "one value left: refused, nothing saved: " + result->text());
        deadline->setText("24");
        save->click();
        check(waitUntil([&]{ return !dialog && quiet(w); }), "saved: closed");
        check(optionsOf(lastCall("config")) == QStringList({"--set=arc_upload.settle_seconds=60", "--set=arc_upload.globus.deadline_hours=24"}),
              "exactly the changed keys");
        check(waitUntil([&]{ return quiet(w) && a.loaded() && a.listProblem.isEmpty(); }) && a.uploadButton->isEnabled() &&
              a.statusLabel->text().startsWith("Ready for a demo upload"), "the page works again: " + a.statusLabel->text());
        // A demo folder inside scan_sessions is refused with its reason.
        a.settingsButton->click();
        dialog = a.settingsDialog;
        save = button(*dialog, "Save");
        check(waitUntil([&]{ return save->isEnabled() && quiet(w); }), "arc-doctor answered again");
        check(dialog->findChild<QWidget*>("arcMoreBox")->isHidden(), "valid settings: More stays closed");
        dialog->findChild<QLineEdit*>("arcDemoFolder")->setText("scan_sessions/demo");
        save->click();
        check(waitUntil([&]{ return callsOf("config").size() == 3 && quiet(w); }) && dialog &&
              dialog->findChild<QLabel*>("arcSettingsResult")->text().contains("arc_upload.demo_folder must not be inside scan_sessions/"),
              "the demo folder's rule");
        button(*dialog, "Cancel")->click();
    }

    // Nothing is cut off or pushed out of its place; ui_tests.cpp::fits, here for the window with the
    // ARC page. scrolling: a column that is allowed to scroll at this size.
    static void fits(MainWindow& w, const QString& where, const QScrollArea* scrolling = nullptr, const QScrollArea* scrolling2 = nullptr) {
        const auto fail = [&](const QString& what) { throw std::runtime_error((what + " (" + where + ")").toStdString()); };
        for (QWidget* control : w.findChildren<QWidget*>()) {
            if (!control->isVisible()) continue;
            const auto* pushButton = qobject_cast<QPushButton*>(control);
            if (!pushButton && !qobject_cast<QLineEdit*>(control) && !qobject_cast<QComboBox*>(control)) continue;
            if (pushButton && pushButton->fontMetrics().horizontalAdvance(pushButton->text()) + 8 > pushButton->width()) fail("button text cut: " + pushButton->text());
            if ((scrolling && scrolling->isAncestorOf(control)) || (scrolling2 && scrolling2->isAncestorOf(control))) continue;
            const QRect area(control->mapTo(&w, QPoint(0, 0)), control->size());
            for (QWidget* parent = control->parentWidget(); parent && parent != &w; parent = parent->parentWidget())
                if (!QRect(parent->mapTo(&w, QPoint(0, 0)), parent->size()).contains(area))
                    fail("control pushed out of its place: " + (pushButton ? pushButton->text() : control->objectName()));
        }
        const QList<StatusPill*> pills = H::pills(w);
        for (StatusPill* pill : pills) {
            if (!pill->isVisible()) continue;
            if (!pill->parentWidget()->rect().contains(pill->geometry())) fail("status outside the header: " + pill->value());
            if (pill->width() < pill->minimumSizeHint().width()) fail("status squeezed: " + pill->value());
            for (StatusPill* other : pills)
                if (other != pill && other->isVisible() && other->geometry().intersects(pill->geometry())) fail("statuses overlap: " + pill->value());
        }
        for (StatusPill* whole : H::wholePills(w))
            if (whole->width() < whole->sizeHint().width()) fail("status cut: " + whole->value());
    }
    static void layout() {
        resetStandIn();
        control(R"({"arc_unfinished": 2, "arc": {"uploads": [
            {"state": "transferring:3/21", "created_utc": "2026-11-15T10:11:00.000Z"},
            {"state": "conflict", "created_utc": "2026-11-15T10:07:00.000Z"},
            {"state": "verified", "area": "real", "created_utc": "2026-11-15T10:03:00.000Z"}]}})");
        MainWindow w;
        prepare(w);
        QScrollArea* leftColumn = nullptr;
        for (QWidget* parent = H::sampleField(w)->parentWidget(); parent && !leftColumn; parent = parent->parentWidget())
            leftColumn = qobject_cast<QScrollArea*>(parent);
        check(leftColumn, "the left column");
        ArcPanel& a = openPage(w);
        noPolling(a);
        select(w, a.uploads.at(0).toObject()["upload_id"].toString());
        check(a.progressBar->isVisible() && waitUntil([&]{ return a.details->toPlainText().contains("History"); }), "an upload with its progress and history");
        // No two buttons share a text, also with the ARC page: they are found by it. The new ones are there once.
        QStringList texts;
        for (QPushButton* b : w.findChildren<QPushButton*>()) texts << b->text();
        check(QSet<QString>(texts.begin(), texts.end()).size() == texts.size(), "no two buttons share a text: " + texts.join(", "));
        for (const char* text : {"ARC upload", "Upload to ARC", "ARC settings...", "Stop upload", "Check status", "Retry same request", "Keep as new copy",
                                 "Copy ARC path", "Show in Finder", "Stop"})
            check(texts.count(text) == 1, QString("once: ") + text);
        auto* page = static_cast<QScrollArea*>(a.page());
        for (const QSize& size : {QSize(1440, 880), QSize(1180, 720)}) {
            w.resize(size);
            spin(250);
            const QString where = QString("%1 x %2, ARC page").arg(size.width()).arg(size.height());
            check(w.size() == size && a.page()->isVisible(), "the window takes the size: " + where);
            check(leftColumn->verticalScrollBar()->maximum() == 0, "the left column fits without scrolling: " + where);
            if (size.width() == 1440) {
                check(a.lowerBox->sideBySide() && a.content->width() >= ArcLowerBox::kSideBySide, "side by side at 1440 x 880");
                check(page->verticalScrollBar()->maximum() == 0, QString("the page fits without scrolling at 1440 x 880 (%1 px too tall)")
                                                                     .arg(page->verticalScrollBar()->maximum()));
                fits(w, where);
            } else {
                check(!a.lowerBox->sideBySide() && a.content->width() < ArcLowerBox::kSideBySide, "one above the other below 1000 px");
                check(page->verticalScrollBar()->maximum() > 0 && !page->horizontalScrollBar()->isVisible(), "the page scrolls, vertically only");
                fits(w, where, page);
            }
            check(H::tabs(w)->currentWidget()->height() >= 150, "the tab area keeps a usable height");
        }
        // The longest texts: a long status line is shortened, not widened.
        w.resize(1180, 720);
        a.setLine(QString("Upload failed: ") + QString("A very long reason. ").repeated(20), UiTone::Danger);
        spin(200);
        check(w.size() == QSize(1180, 720), "a long status line does not widen the window");
        fits(w, "long status line", page);
        w.resize(1440, 880);
        spin(250);
        fits(w, "long status line, 1440 x 880");
    }
};

int main(int argc, char** argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    // Only the emulators next to this program are started; both variables keep even real helper
    // programs simulated. The guard keeps a real data layer from any live upload path (design 9.5).
    qputenv("RAMAN_BACKEND_SIMULATE", "1");
    qputenv("RAMAN_MOTOR_SIMULATE", "1");
    qputenv("RAMAN_ARC_TEST_GUARD", "1");
    qputenv("RAMAN_CATALOG_PYTHON", UI_TEST_PYTHON);
    QApplication app(argc, argv);
    useUiBaseStyle();
    struct Test { const char* name; void (*run)(); };
    const Test tests[] = {
        {"start / exactly status, list, review-list, review-show / nothing until the page is shown / hidden: no polling", ArcUiTest::startsNothingUntilShown},
        {"polling / 2 s while active, 30 s otherwise, at most 10 s during an acquisition, hidden: only the toggle's counts, one at a time", ArcUiTest::polling},
        {"toggle / warn tone from status.arc_unfinished, danger tone from status.arc_attention, no process of its own", ArcUiTest::toggleColour},
        {"toggle follows / hidden page: arc-list --limit=1 at the idle rate while the button has a colour; a failed upload turns it danger",
         ArcUiTest::toggleFollowsUploads},
        {"demo upload / dry run, then the upload without a dialog, once / status line word for word / Show in Finder / already there", ArcUiTest::demoUpload},
        {"live upload / setup missing / confirmation texts for Globus, SSH, simulated, stopped early, declared real / Cancel", ArcUiTest::liveUploads},
        {"conflicts / earlier upload: retry or new / already on ARC: upload again", ArcUiTest::conflicts},
        {"actions / buttons follow actions exactly / Retry, Check, Stop, Keep as new copy ask first / copy path / Finder", ArcUiTest::actions},
        {"stop / the Globus sign-in is named only where a program signs in to stop the upload", ArcUiTest::stopSignIn},
        {"acquisition and refusals / Upload disabled with its tool tips / Stop stays / sessions after the capture / refusals", ArcUiTest::acquisitionAndRefusals},
        {"ARC settings / arc-doctor / empty VT username and collection ID / one config --set of the changed keys / refusal", ArcUiTest::settings},
        {"not available / a data layer without the arc-* commands", ArcUiTest::notAvailable},
        {"check results / Checked at HH:MM / check_failed in the warn tone / checked on the newest ok check / Stop of a check",
         ArcUiTest::checkResults},
        {"stop of a check / a Globus check of a transfer that may run cancels it, and the question says so", ArcUiTest::stopOfACheckThatCancels},
        {"check that could not look / a note with what to do; the upload stays on ARC", ArcUiTest::checkThatCouldNotLook},
        {"resting lines / stopped at once in the warn tone / an upload that waits says so, also at the next lists", ArcUiTest::restingLines},
        {"acquisition flips / the focus monitor: uploads still read, Upload to ARC does not blink", ArcUiTest::acquisitionFlips},
        {"polling soon / waiting for Terminal and after an action: the short interval", ArcUiTest::pollingSoon},
        {"ssh progress / a demo SSH upload in bytes, as its line", ArcUiTest::sshProgress},
        {"dialog words / no dark spectrum / the VT account of an SSH upload in Check and Retry / the team's file of ARC's keys", ArcUiTest::dialogWords},
        {"uncounted session / a folder the data layer did not count: no 0 spectra, no end", ArcUiTest::uncountedSession},
        {"more sessions / 30 of N listed, the last entry lists more", ArcUiTest::moreSessions},
        {"settings close confirmation / a live confirmation closes when ARC settings change", ArcUiTest::settingsCloseConfirmation},
        {"settings repair / every value has a field; More opens for a refused one; Save corrects it", ArcUiTest::settingsRepair},
        {"layout / nothing cut at 1440 x 880 / scrolls at 1180 x 720 / no two buttons share a text", ArcUiTest::layout},
    };
    // ./arc_ui_tests [part of a test name]: only the tests whose name contains it.
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
