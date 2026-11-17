#pragma once
// The ARC part of the Data tab: a finished scan session goes to the team folder on ARC, by Globus or
// by SSH, or to the demo folder on this Mac that stands in for ARC (data_layer/PROTOCOL.md section 5.6,
// the arc-* commands of section 6). The window only asks and shows: every upload runs in a program of
// the data layer that goes on when the window closes, and each command here is one short process that
// nothing waits for (DataLayerClient). Nothing is started before the page is first shown, and no dialog
// blocks: the acquisition code shares this event loop, so Stop and Laser OFF stay within reach.
// data_panels.h includes this file below the table, the facts box and the words it shares with the
// Data tab (ShareTable, FactsText, DataText). Header-only and without Q_OBJECT (see window_theme.h).

#include "data_layer_client.h"
#include "window_status.h"
#include "window_theme.h"

#include <QBoxLayout>
#include <QButtonGroup>
#include <QCheckBox>
#include <QClipboard>
#include <QComboBox>
#include <QDateTime>
#include <QDesktopServices>
#include <QDialog>
#include <QElapsedTimer>
#include <QGridLayout>
#include <QGuiApplication>
#include <QHash>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QPointer>
#include <QProgressBar>
#include <QPushButton>
#include <QRadioButton>
#include <QRegularExpression>
#include <QScrollArea>
#include <QTimer>
#include <QToolButton>
#include <QUrl>

#include <cmath>
#include <functional>

// How the arc-* answers are worded in the page. The data layer makes the labels and the messages;
// these are the window's own words around them.
namespace ArcText {
inline QString count(qint64 n, const QString& one, const QString& many) { return QString("%1 %2").arg(n).arg(n == 1 ? one : many); }
// 433104 bytes: "433 kB"; decimal units, as the confirmation of an upload says them.
inline QString size(double bytes) {
    if (bytes < 1000) return count(qint64(bytes), "byte", "bytes");
    const qint64 kilo = qRound64(bytes / 1000.0);
    if (kilo < 1000) return QString("%1 kB").arg(kilo);
    if (bytes < 1e9) return QString("%1 MB").arg(bytes / 1e6, 0, 'f', bytes < 1e7 ? 1 : 0);
    return QString("%1 GB").arg(bytes / 1e9, 0, 'f', 1);
}
inline QString sentence(const QJsonObject& reason) {
    const QString text = reason["text"].toString().trimmed(), fix = reason["fix"].toString().trimmed();
    return fix.isEmpty() ? text : text.isEmpty() ? fix : text + " " + fix;
}
// A warning or a note: a sentence, or an object that holds one.
inline QString text(const QJsonValue& value) {
    if (!value.isObject()) return value.toString();
    const QJsonObject object = value.toObject();
    return object["text"].toString(object["message"].toString());
}
inline QString transport(const QJsonObject& item) {
    return QString(item["transport"].toString() == "ssh" ? "SSH" : "Globus") + (item["demo"].toBool() ? " (demo)" : "");
}
// The states in which a program of the data layer works on an upload (section 7.3).
inline bool working(const QString& state) {
    static const QStringList states{"queued", "waiting_for_terminal", "preparing", "signing_in", "checking", "submitting",
                                    "transferring", "verifying", "placing"};
    return states.contains(state);
}
// The states an upload rests in when nothing is left to do: on ARC (checked, or on ARC with a last check
// that failed: "check_failed", which the data layer shows for a stored "verified"), failed or stopped.
inline bool isFinal(const QString& state) {
    return state == "verified" || state == "check_failed" || state == "failed" || state == "stopped";
}
// The newest check of an upload on ARC that could not look (a sign-in that did not happen, ssh that ended before
// it signed in, Globus out of reach): "looked": false. It says nothing about the copy (review REG-01).
inline bool lastCheckNotLooked(const QJsonObject& item) {
    const QJsonValue looked = item["last_check"].toObject()["looked"];
    return looked.isBool() && !looked.toBool();
}
// The newest check of an upload on ARC that looked found the copy missing or different (fix plan I1). The data
// layer shows it as check_failed, also when a later check could not look.
inline bool lastCheckFailed(const QJsonObject& item) {
    const QJsonValue ok = item["last_check"].toObject()["ok"];
    return item["display_state"].toString() == "check_failed" ||
           (item["state"].toString() == "verified" && ok.isBool() && !ok.toBool() && !lastCheckNotLooked(item));
}
// What a check that could not look says, with what to do: "<why>. Check status could not look. <fix>".
inline QString checkNote(const QJsonObject& check) {
    const QString message = check["message"].toString(), fix = check["fix"].toString();
    return fix.isEmpty() ? message : message + " " + fix;
}
// A demo upload that went well is no upload to ARC: it is never shown in the colour of one. An upload on
// ARC whose last check failed is shown in the warn tone (fix plan I1).
inline UiTone tone(const QJsonObject& item) {
    const QString state = item["display_state"].toString(item["state"].toString());
    if (working(state)) return UiTone::Info;
    if (state == "verified" && lastCheckFailed(item)) return UiTone::Warn;
    if (state == "verified") return item["demo"].toBool() ? UiTone::Warn : UiTone::Ok;
    if (state == "failed" || state == "unreadable") return UiTone::Danger;
    if (QStringList{"uncertain", "conflict", "stopped", "not_watched", "interrupted", "not_started", "check_failed"}.contains(state))
        return UiTone::Warn;
    return UiTone::Neutral;
}
// When an upload on ARC was last found as it should be: its newest check when that one was ok, else the
// check made when it was put in place.
inline QString checkedUtc(const QJsonObject& item) {
    const QJsonObject last = item["last_check"].toObject();
    if (last["ok"].toBool() && !last["utc"].toString().isEmpty()) return last["utc"].toString();
    return item["verification"].toObject()["utc"].toString(item["updated_utc"].toString());
}
// Reasons for which the data layer stops before it counts the session folder's spectra (a name it does not
// take, a folder that went away): their number and how the scan ended are not known then, and arc-sessions
// says 0 and "ended (no end mark)". A folder without session.json is counted (arc_session.check).
inline bool notRead(const QJsonObject& why) {
    static const QStringList reasons{"odd_name", "unknown_session"};
    return reasons.contains(why["reason"].toString());
}
// The name a new copy is put under (design I4: the first free one of <folder>, <folder>_copy2 ...):
// the one after the name the copy on ARC has now.
inline QString nextCopyName(const QString& folder, const QString& placedAs) {
    static const QRegularExpression copy("_copy(\\d+)$");
    const QRegularExpressionMatch found = copy.match(placedAs);
    const int number = placedAs.startsWith(folder + "_copy") && found.hasMatch() ? found.captured(1).toInt() + 1 : 2;
    return QString("%1_copy%2").arg(folder).arg(number);
}
// How a scan ended (section 4.3), as the confirmation says it in brackets after the number of spectra.
inline QString end(const QString& end) {
    if (end == "finished") return "finished";
    if (end == "stopped") return "stopped early";
    if (end == "failed") return "ended with an error";
    return "no end mark";
}
inline QString kind(const QString& kind, bool declared) {
    if (kind == "real") return declared ? "measured, as the uploader said" : "measured";
    if (kind == "simulated") return "simulated";
    if (kind == "mixed") return "partly simulated";
    return "simulation not recorded";
}
// What "checked" means for an upload that is on ARC (record "verification").
inline QString check(const QJsonObject& verification, bool demo) {
    const QString method = verification["method"].toString();
    if (method.isEmpty()) return QString();
    const QString prefix = demo ? QString("Demo: ") : QString();
    if (method.startsWith("demo") || (demo && method == "sha256-on-arc")) return "Demo: SHA-256 of every file";
    if (method == "sha256-on-arc") return "SHA-256 of every file computed on ARC";
    if (method == "globus-checksum")
        return prefix + QString("Globus checksums (%1) match this Mac's").arg(verification["algorithm"].toString("MD5").toUpper());
    if (method == "globus-verify") return prefix + "Globus compared every file with this Mac's copy";
    return prefix + method;
}
// Section 5 of the design, as data_layer/raman_catalog/arc_paths.py has it: the folder that ARC's
// Globus collection shows for a cluster path, empty when the path is not inside the collection's top.
inline QString collectionPath(const QString& cluster, const QString& root) {
    if (root == "/") return cluster;
    if (cluster == root) return "/";
    if (!root.isEmpty() && cluster.startsWith(root + "/")) return cluster.mid(root.size());
    return QString();
}
inline bool validFolder(const QString& path, const QString& root) {
    static const QRegularExpression part("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$");
    if (!path.startsWith('/') || path.size() > 200) return false;
    const QStringList parts = path.mid(1).split('/');
    for (const QString& name : parts)
        if (!part.match(name).hasMatch()) return false;
    const QString below = collectionPath(path, root);
    return !below.isEmpty() && below.mid(1).split('/', Qt::SkipEmptyParts).size() >= 2;
}
}

// The Session box. Opening it reads the sessions again: a scan may have ended since.
class ArcSessionBox : public QComboBox {
public:
    std::function<void()> opening;
    void showPopup() override {
        if (opening) opening();
        QComboBox::showPopup();
    }
};

// Recent uploads. An empty table says so, like the provenance box of the Data tab.
class ArcRecentTable : public ShareTable {
public:
    ArcRecentTable() : ShareTable({"Started", "Session", "With", "State", "On ARC"}, {0, 5, 0, 4, 3}) {}
    QString emptyText;

protected:
    void paintEvent(QPaintEvent* event) override {
        ShareTable::paintEvent(event);
        if (rowCount() > 0 || emptyText.isEmpty()) return;
        QPainter painter(viewport());
        painter.setFont(uiFont(this, 12));
        painter.setPen(uiTheme().textDim);
        painter.drawText(viewport()->rect().adjusted(8, 0, -8, 0), Qt::AlignVCenter | Qt::AlignLeft, emptyText);
    }
};

// The recent uploads beside their details where the page has room for both, one above the other where it has not.
class ArcLowerBox : public QWidget {
public:
    static constexpr int kSideBySide = 1000;
    ArcLowerBox(QLayout* recent, QLayout* details) {
        box = new QBoxLayout(QBoxLayout::LeftToRight, this);
        box->setContentsMargins(0, 0, 0, 0);
        box->setSpacing(14);
        box->addLayout(recent, 3);
        box->addLayout(details, 2);
    }
    bool sideBySide() const { return box->direction() == QBoxLayout::LeftToRight; }

protected:
    void resizeEvent(QResizeEvent* event) override {
        QWidget::resizeEvent(event);
        const QBoxLayout::Direction wanted = width() >= kSideBySide ? QBoxLayout::LeftToRight : QBoxLayout::TopToBottom;
        if (box->direction() != wanted) box->setDirection(wanted);
    }

private:
    QBoxLayout* box = nullptr;
};

class ArcPanel : public QObject {
    friend struct ArcUiTest;

public:
    ArcPanel(DataLayerClient* client, QObject* parent) : QObject(parent), dataLayer(client) {
        buildPage();
        poll.setSingleShot(true);
        connect(&poll, &QTimer::timeout, this, [this] { listUploads(); });
        // A session that ended a moment ago may go once it has been quiet long enough (retry_after_seconds).
        sessionsLater.setSingleShot(true);
        connect(&sessionsLater, &QTimer::timeout, this, [this] { if (shown) loadSessions(); });
        // The end of an acquisition counts once it has lasted a moment: the focus monitor pauses between frames.
        becomingIdle.setSingleShot(true);
        connect(&becomingIdle, &QTimer::timeout, this, [this] { applyAcquisitionBusy(false); });
        // While the page is hidden and the toggle has a colour, the counts behind the colour are read again.
        hiddenPoll.setSingleShot(true);
        connect(&hiddenPoll, &QTimer::timeout, this, [this] { listHidden(); });
        applyTheme();
        showAll();
    }

    // Page 1 of the lower area of the Data tab: the ARC page in a scroll area that scrolls vertically only.
    QWidget* page() const { return scroll; }
    // The page has come into view (its toggle is on and the Data tab is in front) or has gone out of it.
    // Shown, it reads the uploads and the sessions and then keeps reading the uploads. Hidden, it asks
    // nothing, except arc-list --limit=1 at the idle rate while the toggle shows uploads that are not
    // finished or need the person: that keeps the toggle's colour right.
    void setShown(bool show) {
        if (show == shown) return;
        shown = show;
        followHidden();
        if (!shown) { poll.stop(); return; }
        everShown = true;
        listUploads();
        loadSessions();
    }
    // A scan, a capture or a laser command is under way: nothing that starts work is offered, the
    // sessions are read without --controller-idle and the uploads at most every busyPollMs. The window
    // says so every 200 ms; the end of an acquisition counts once it has lasted idleAfterMs, so that the
    // pauses of the focus monitor between its frames change nothing.
    void setAcquisitionBusy(bool busy) {
        if (busy) {
            becomingIdle.stop();
            if (!acquisitionBusy) applyAcquisitionBusy(true);
        } else if (acquisitionBusy && !becomingIdle.isActive()) {
            becomingIdle.start(idleAfterMs);
        }
    }
    // A scan or a single capture has ended: its session is offered once the acquisition is over.
    void captureFinished() {
        if (!shown) return;
        sessionsDue = acquisitionBusy;
        if (!acquisitionBusy) loadSessions();
    }
    // The window's own status calls carry the numbers of unfinished uploads and of uploads that need the
    // person; they colour the toggle without a process of their own.
    void statusUpdated(const QJsonObject& status) { noteCounts(status["arc_unfinished"].toInt(), status["arc_attention"].toInt()); }
    // Colours that were put on items, rich text and style sheets are fixed when those are made: make them again.
    void applyTheme() {
        // The window's style sheet leaves progress bars to the base style, whose groove stays white in the
        // dark appearance; this one is drawn in the colours of the appearance in use.
        const UiTheme& theme = uiTheme();
        progressBar->setStyleSheet(QString("QProgressBar { background: %1; border: 1px solid %2; border-radius: 4px; color: %3; "
                                           "text-align: center; font-size: 11px; } QProgressBar::chunk { background: %4; border-radius: 3px; }")
                                       .arg(theme.surfaceAlt.name(), theme.borderStrong.name(), theme.text.name(),
                                            UiTheme::mix(theme.surface, theme.accent, theme.dark ? 0.55 : 0.35).name()));
        styleBadge();
        showUploadRows();
        showDetails();
    }

    // Called with the number of uploads that are not finished and the number that need the person
    // (status.arc_attention, arc-list's attention) whenever one of them may have changed.
    std::function<void(int unfinished, int attention)> countsChanged;
    // Replaced by the tests, which must not open Finder.
    std::function<bool(const QUrl&)> openUrl = [](const QUrl& url) { return QDesktopServices::openUrl(url); };
    // How often arc-list is asked while the page is shown: while an upload is worked on or about to be,
    // while none is, and at most while an acquisition runs; afterActionMs: how long the short interval
    // holds after an action of this window. idleAfterMs: how long the end of an acquisition must last.
    // The tests shorten them.
    int activePollMs = 2000, idlePollMs = 30000, busyPollMs = 10000, afterActionMs = 120000, idleAfterMs = 1500;

private:
    // The values a choice of session and transport stands for. A dry run and the upload it leads to
    // are asked with the same values, whatever the page shows by then.
    struct Choice {
        QString folder, transport;
        bool demo = true, declared = false;
        QStringList command(bool dryRun, bool newCopy, bool controllerIdle) const {
            QStringList words{"arc-upload", "--session=" + folder, "--transport=" + transport, demo ? "--demo" : "--live"};
            if (declared) words << "--declare-real";
            if (newCopy) words << "--new";
            if (controllerIdle) words << "--controller-idle";
            if (dryRun) words << "--dry-run";
            return words;
        }
    };
    struct Button {
        QString text;
        std::function<void()> act;
        bool primary = false;
        bool enabled = true;
        QString tip;
    };

    static inline const QString kNotAvailable = "Uploads to ARC are not part of this installation's data layer.";
    static inline const QString kAsking = "Asking the data layer about uploads to ARC...";
    static inline const QString kAfterAcquisition = "Upload to ARC is available when the scan or capture has ended.";
    static inline const QString kWorkAfterAcquisition = "Available when the scan or capture has ended.";
    static inline const QString kGoesAsTheyAre =
        "The files go as they are, with the Sample ID, Operator, Location and Notes they hold; everyone in the ARC project can "
        "read them. Nothing on ARC is deleted or replaced. You can stop the upload at any time.";
    // ssh's questions in the Terminal window, in the order ssh asks them (fix plan I9): the words of the
    // Terminal text, arc_ssh.py's HOST_KEY_STEP, PASSWORD_STEP, DUO_STEP, WARNING_TEXT and NETWORK_TEXT
    // (test_arc_shapes.py checks that they stay the same).
    static inline const QString kSshHostKey =
        "The first time only: ARC's key fingerprint, then \"Are you sure you want to continue connecting (yes/no/[fingerprint])?\". "
        "Type only yes or no here; what you type is shown on screen, so never type your password at this question. Answer yes only "
        "if the fingerprint matches the one in the README (section 14, Host key); otherwise answer no and ask the team.";
    static inline const QString kSshPassword = "Your VT password. Typing shows nothing; that is normal. Press Return.";
    static inline const QString kSshDuo =
        "Duo: approve the push on your phone, or type your password followed by a comma and a six-digit passcode at the password "
        "question. Choose Duo Push if a numbered menu appears.";
    static inline const QString kSshWarning =
        "ssh may print a WARNING that the connection is not using a post-quantum key exchange algorithm. That is expected with ARC "
        "(ARC's FAQ explains it); continue.";
    static inline const QString kSshNetwork = "ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN.";
    static inline const QString kMoreSessions = "Show more sessions (%1 of %2 listed)";
    // What Retry same request sends again (review DOC-V02): the same request number only while it is not known
    // whether Globus got it.
    static inline const QString kRetryTip =
        "Continue this upload with the same files and the same place on ARC. Globus: the same request number while it is not "
        "known whether Globus got it; after a failed transfer, the files are copied again into the same _incoming folder. "
        "SSH: the files are sent again unless ARC has them all.";
    static inline const QString kCheckTip =
        "Ask Globus or ARC how this upload stands. Nothing is sent. Globus: you sign in again in the browser. SSH: a Terminal "
        "window opens and you sign in once.";
    // The demo wordings of Retry same request and Check status (review COR-05): a demo upload's copy is in the demo folder
    // on this Mac, and nothing signs in.
    static inline const QString kRetryDemoTip =
        "Continue this upload with the same files and the same place in the demo folder. Globus: the same request number while "
        "it is not known whether the demo's Globus got it; after a failed transfer, the files are copied again into the same "
        "_incoming folder. SSH: the files are sent again unless the demo folder has them all.";
    static inline const QString kCheckDemoTip =
        "Look at how this upload stands in the demo folder on this Mac. Nothing is sent, and nothing asks you to sign in.";
    // What a stop of a check that cancels nothing does (also of a Globus check that would put a finished copy in place:
    // stopped before that, it leaves the upload as it is; review R2-01).
    static inline const QString kStopCheckTip = "Stop the check of this upload. Nothing is sent or deleted, and the upload stays as it is.";
    static inline const QString kStopCheckCancelsTip =
        "Stop this upload: the check ends, and the transfer Globus may still be running is cancelled. Files already copied stay "
        "in _incoming on ARC; nothing is deleted.";
    // The demo wordings of the Stop tool tips (review COR-05): a demo upload's files are in the demo folder on this Mac.
    static inline const QString kStopCheckCancelsDemoTip =
        "Stop this upload: the check ends, and the transfer the demo's Globus may still be running is cancelled. Files already "
        "copied stay in _incoming of the demo folder; nothing is deleted.";
    static inline const QString kStopTip = "Stop this upload. Files already copied stay in _incoming on ARC; nothing is deleted.";
    static inline const QString kStopDemoTip = "Stop this upload. Files already copied stay in _incoming of the demo folder; nothing is deleted.";
    static constexpr int kSessionsStep = 100;

    // ------------------------------------------------------------------ the page

    static QLabel* label(const QString& text, const char* name) {
        auto* made = new QLabel(text);
        made->setObjectName(name);
        return made;
    }
    QPushButton* button(const QString& text, const char* name, const QString& tip) {
        auto* made = new QPushButton(text);
        made->setObjectName(name);
        made->setToolTip(tip);
        return made;
    }

    void buildPage() {
        content = new QWidget;
        content->setObjectName("arcPage");
        auto* layout = new QVBoxLayout(content);
        layout->setContentsMargins(0, 2, 0, 0);
        layout->setSpacing(6);

        // What the page does, and where its settings are.
        badge = label("DEMO", "arcBadge");
        // One line that gives way at narrow sizes (its whole text stays in the tool tip): a wrapping label
        // would make the scroll area size the page by its preferred height, and scroll at the default size.
        intro = new ElidedLabel;
        intro->setObjectName("arcIntro");
        intro->setText("Copies a finished scan session, with its dark spectrum and a checked list of its files, to the team folder on ARC.");
        settingsButton = button("ARC settings...", "arcSettings", "Where uploads go, this Mac's Globus collection ID and your VT username.");
        auto* titleRow = new QHBoxLayout;
        titleRow->setSpacing(8);
        titleRow->addWidget(label("ARC UPLOAD", "paneTitle"));
        titleRow->addWidget(badge);
        titleRow->addWidget(intro, 1);
        titleRow->addWidget(settingsButton);

        // What goes, how, and where to.
        sessionBox = new ArcSessionBox;
        sessionBox->setObjectName("arcSession");
        sessionBox->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
        sessionBox->setMinimumContentsLength(24);
        sessionBox->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        sessionBox->opening = [this] { loadSessions(); };
        globusRadio = new QRadioButton("Globus");
        globusRadio->setObjectName("arcGlobus");
        globusRadio->setToolTip("Sends through Globus. You sign in once in your web browser. Globus Connect Personal must be running on this Mac.");
        sshRadio = new QRadioButton("SSH");
        sshRadio->setObjectName("arcSsh");
        sshRadio->setToolTip("Sends with ssh. A Terminal window opens where you type your VT password and approve Duo, once per upload.");
        auto* transports = new QButtonGroup(content);
        transports->addButton(globusRadio);
        transports->addButton(sshRadio);
        globusRadio->setChecked(true);
        demoBox = new QCheckBox("Demo (no ARC, no account)");
        demoBox->setObjectName("arcDemo");
        demoBox->setToolTip("A folder on this Mac stands in for ARC (data_catalog/arc_demo). Nothing is sent anywhere. Simulated sessions are welcome.");
        demoBox->setChecked(true);
        declareBox = new QCheckBox("Measured with the real instruments");
        declareBox->setObjectName("arcDeclareReal");
        declareBox->setToolTip("This session's files do not say whether it was made in simulation. Tick this only if it was measured with the "
                               "real spectrometer and stage; the upload records that you said so.");
        declareBox->hide();
        auto* sendRow = new QHBoxLayout;
        sendRow->setSpacing(10);
        sendRow->addWidget(globusRadio);
        sendRow->addWidget(sshRadio);
        sendRow->addSpacing(16);
        sendRow->addWidget(demoBox);
        sendRow->addSpacing(16);
        sendRow->addWidget(declareBox);
        sendRow->addStretch(1);
        folderLabel = new ElidedLabel(Qt::ElideMiddle);
        folderLabel->setObjectName("arcFolder");
        destinationLabel = new ElidedLabel(Qt::ElideMiddle);
        destinationLabel->setObjectName("arcDestination");
        auto* form = new QGridLayout;
        form->setHorizontalSpacing(10);
        form->setVerticalSpacing(5);
        form->setColumnStretch(1, 1);
        form->addWidget(label("Session", "fieldLabel"), 0, 0);
        form->addWidget(sessionBox, 0, 1);
        form->addWidget(label("Send with", "fieldLabel"), 1, 0);
        form->addLayout(sendRow, 1, 1);
        form->addWidget(label("ARC folder", "fieldLabel"), 2, 0);
        form->addWidget(folderLabel, 2, 1);
        form->addWidget(label("To", "fieldLabel"), 3, 0);
        form->addWidget(destinationLabel, 3, 1);

        // The action, what it would do or what it did, and how far the upload has come.
        uploadButton = button("Upload to ARC", "arcUpload", QString());
        uploadButton->setProperty("role", "primary");
        statusLabel = new ElidedLabel;
        statusLabel->setObjectName("arcStatus");
        progressBar = new QProgressBar;
        progressBar->setObjectName("arcProgress");
        progressBar->setFixedWidth(190);
        progressBar->hide();
        stopButton = button("Stop upload", "arcStop", kStopTip);
        auto* actionRow = new QHBoxLayout;
        actionRow->setSpacing(10);
        actionRow->addWidget(uploadButton);
        actionRow->addWidget(statusLabel, 1);
        actionRow->addWidget(progressBar);
        actionRow->addWidget(stopButton);

        // Recent uploads and what can be done with the selected one; its details beside them.
        recentTable = new ArcRecentTable;
        recentTable->setObjectName("arcRecent");
        recentTable->setMinimumHeight(74);
        checkButton = button("Check status", "arcCheck", kCheckTip);
        retryButton = button("Retry same request", "arcRetry", kRetryTip);
        keepCopyButton = button("Keep as new copy", "arcKeepCopy", "Put this upload beside the folder that already has its name, as <folder>_copy2.");
        copyPathButton = button("Copy ARC path", "arcCopyPath", "Copy the folder's path on ARC.");
        finderButton = button("Show in Finder", "arcShowFinder", "Open the demo folder on this Mac.");
        auto* rowButtons = new QHBoxLayout;
        rowButtons->setSpacing(6);
        for (QPushButton* each : {checkButton, retryButton, keepCopyButton, copyPathButton, finderButton}) rowButtons->addWidget(each);
        rowButtons->addStretch(1);
        auto* recent = new QVBoxLayout;
        recent->setSpacing(4);
        recent->addWidget(label("RECENT UPLOADS", "paneTitle"));
        recent->addWidget(recentTable, 1);
        recent->addLayout(rowButtons);
        details = new FactsText;
        details->setObjectName("arcDetails");
        details->setStyleSheet("QTextEdit { background: transparent; border: none; font-size: 12px; }");
        auto* detailsBox = new QWidget;
        auto* detailsLayout = new QVBoxLayout(detailsBox);
        detailsLayout->setContentsMargins(0, 0, 4, 0);
        detailsLayout->addWidget(details);
        detailsLayout->addStretch(1);
        detailsScroll = new QScrollArea;
        detailsScroll->setWidget(detailsBox);
        detailsScroll->setWidgetResizable(true);
        detailsScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        detailsScroll->viewport()->setAutoFillBackground(false);
        detailsBox->setAutoFillBackground(false);
        detailsScroll->setMinimumSize(200, 60);
        auto* aside = new QVBoxLayout;
        aside->setSpacing(4);
        aside->addWidget(label("UPLOAD DETAILS", "paneTitle"));
        aside->addWidget(detailsScroll, 1);
        lowerBox = new ArcLowerBox(recent, aside);

        layout->addLayout(titleRow);
        layout->addLayout(form);
        layout->addLayout(actionRow);
        layout->addSpacing(2);
        layout->addWidget(lowerBox, 1);

        scroll = new QScrollArea;
        scroll->setObjectName("arcScroll");
        scroll->setWidget(content);
        scroll->setWidgetResizable(true);
        scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        scroll->viewport()->setAutoFillBackground(false);
        content->setAutoFillBackground(false);

        connect(settingsButton, &QPushButton::clicked, this, [this] { openSettings(); });
        connect(sessionBox, &QComboBox::activated, this, [this](int index) { sessionChosen(index); });
        connect(sessionBox, &QComboBox::currentIndexChanged, this, [this](int index) {
            // The entry that lists more sessions is no session: sessionChosen() answers it.
            if (isMoreEntry(index)) return;
            if (index >= 0 && index < sessions.size()) shownSession = index;
            if (!filling) showAll();
        });
        connect(transports, &QButtonGroup::buttonClicked, this, [this] { modeChosen(); });
        connect(demoBox, &QCheckBox::clicked, this, [this] { modeChosen(); });
        connect(declareBox, &QCheckBox::clicked, this, [this] { forget(); showAll(); });
        connect(uploadButton, &QPushButton::clicked, this, [this] { if (uploadBlocked().isEmpty()) askPlan(choice(), false); });
        connect(stopButton, &QPushButton::clicked, this, [this] { askFirst("stop"); });
        connect(checkButton, &QPushButton::clicked, this, [this] { askFirst("check"); });
        connect(retryButton, &QPushButton::clicked, this, [this] { askFirst("retry"); });
        connect(keepCopyButton, &QPushButton::clicked, this, [this] { askFirst("keep_copy"); });
        connect(copyPathButton, &QPushButton::clicked, this, [this] { copyPath(); });
        connect(finderButton, &QPushButton::clicked, this, [this] { showInFinder(); });
        connect(recentTable, &QTableWidget::itemSelectionChanged, this, [this] { uploadSelected(); });
    }

    // ------------------------------------------------------------------ what the answers say

    bool loaded() const { return listLoaded && sessionsLoaded; }
    bool usable() const { return everShown && loaded() && !absent; }
    bool demo() const { return demoBox->isChecked(); }
    QString transport() const { return sshRadio->isChecked() ? "ssh" : "globus"; }
    bool declared() const { return !declareBox->isHidden() && declareBox->isChecked(); }
    Choice choice() const { return {selectedSession()["folder"].toString(), transport(), demo(), declared()}; }

    // An ARC setting as arc-list and arc-doctor name them ("arc_upload.ssh.username"); the short and
    // the nested forms are read as well.
    static QJsonValue setting(const QJsonObject& settings, const QString& key) {
        if (settings.contains("arc_upload." + key)) return settings["arc_upload." + key];
        if (settings.contains(key)) return settings[key];
        QJsonValue value = settings["arc_upload"].isObject() ? settings["arc_upload"] : QJsonValue(settings);
        for (const QString& part : key.split('.')) value = value.toObject()[part];
        return value;
    }
    QJsonValue setting(const QString& key) const {
        const QJsonValue value = setting(listSettings, key);
        return value.isUndefined() || value.isNull() ? setting(sessionSettings, key) : value;
    }
    QString arcFolder() const { return sessionSettings["folder"].toString(setting("folder").toString()); }
    QString globusFolder() const {
        const QString given = sessionSettings["globus_folder"].toString();
        return given.isEmpty() ? ArcText::collectionPath(arcFolder(), setting("globus.collection_root").toString("/projects")) : given;
    }
    // The folder on this Mac that stands in for ARC's file system: HOME/<demo_folder>.
    QString demoRoot() const {
        const QString folder = setting("demo_folder").toString("data_catalog/arc_demo");
        return QDir::cleanPath(QDir(dataLayer->homeDirectory()).absoluteFilePath(folder.isEmpty() ? QString("data_catalog/arc_demo") : folder));
    }

    QJsonObject selectedSession() const {
        const int index = sessionBox->currentIndex();
        return index >= 0 && index < sessions.size() ? sessions.at(index).toObject() : QJsonObject();
    }
    // Why a session may not go now, in this mode: what the data layer found (R1 to R6), else the rule for
    // simulated sessions of this mode, unless "Measured with the real instruments" answers it.
    QJsonObject whyNot(const QJsonObject& session, bool declaredReal) const {
        if (session["why_not"].isObject()) return session["why_not"].toObject();
        const QJsonObject rule = session[demo() ? "demo" : "live"].toObject()["why_not"].toObject();
        if (declaredReal && session["kind"].toString() == "unknown" && rule["reason"].toString() == "simulation_unknown") return QJsonObject();
        return rule;
    }
    bool mayGo(const QJsonObject& session) const { return whyNot(session, false).isEmpty(); }
    // What a live transport still lacks on this Mac: the first problem arc-list names.
    QJsonObject setupProblem() const {
        const QJsonObject ready = readiness[transport()].toObject();
        if (ready.isEmpty() || ready["ok"].toBool()) return QJsonObject();
        const QJsonArray problems = ready["problems"].toArray();
        if (!problems.isEmpty()) return problems.first().toObject();
        return QJsonObject{{"text", QString("%1 is not set up on this Mac.").arg(transport() == "ssh" ? "SSH" : "Globus")},
                           {"fix", "Open ARC settings and press Check setup."}};
    }
    // Why Upload to ARC cannot be pressed now; empty when it can. Also its tool tip.
    QString uploadBlocked(bool commandCounts = true) const {
        if (absent) return kNotAvailable;
        if (!usable()) return kAsking;
        if (acquisitionBusy) return kAfterAcquisition;
        const QJsonObject session = selectedSession();
        if (session.isEmpty()) return "No scan session yet.";
        const QJsonObject why = whyNot(session, declared());
        if (!why.isEmpty()) return ArcText::sentence(why);
        if (!demo() && !setupProblem().isEmpty()) return "Set up first: " + ArcText::sentence(setupProblem());
        if (commandCounts && actionRunning) return "Waiting for the answer to the last upload command.";
        return QString();
    }

    QJsonObject uploadById(const QString& id) const {
        for (const QJsonValue& value : uploads)
            if (value.toObject()["upload_id"].toString() == id) return value.toObject();
        return QJsonObject();
    }
    QJsonObject selectedUpload() const {
        const QList<QTableWidgetItem*> selected = recentTable->selectedItems();
        return selected.isEmpty() ? QJsonObject() : uploads.at(selected.first()->row()).toObject();
    }
    static QString displayState(const QJsonObject& item) { return item["display_state"].toString(item["state"].toString()); }
    static bool offers(const QJsonObject& item, const char* action) { return item["actions"].toArray().contains(QJsonValue(QString(action))); }
    // A program checks the upload now (Check status); Stop upload then ends the check only.
    static bool checkRuns(const QJsonObject& item) {
        const QJsonObject worker = item["worker"].toObject();
        return worker["alive"].toBool() && worker["action"].toString() == "check";
    }
    // A Globus check of an upload whose transfer may still run (the listing's "may_run", whatever the stored state:
    // a failed upload's transfer too): stopped, it cancels that transfer as Stop upload does (after the sign-in it
    // waits for); any other check only ends and changes nothing (review REG-03).
    static bool checkMayCancel(const QJsonObject& item) {
        return checkRuns(item) && item["transport"].toString() == "globus" && item["may_run"].toBool();
    }
    // Where the upload is (or goes) on ARC: the final folder, under the name it was put in place with.
    static QString pathOnArc(const QJsonObject& item) {
        const QJsonObject destination = item["destination"].toObject();
        const QString finalPath = destination["final_path"].toString(), placed = destination["placed_as"].toString();
        if (placed.isEmpty() || finalPath.isEmpty()) return finalPath;
        return finalPath.left(finalPath.lastIndexOf('/') + 1) + placed;
    }
    static QString parentOf(const QString& path) {
        const int slash = path.lastIndexOf('/');
        return slash > 0 ? path.left(slash) : path;
    }
    // "Upload at 16:00:05", or for a demo upload "Demo upload at 16:00:05 (nothing leaves this Mac)": the
    // words in front of every result, as DataPanels::actionPrefix has them. The time is when the data layer
    // wrote what the line says.
    static QString linePrefix(bool demo, const QString& utc) {
        const QDateTime at = QDateTime::fromString(utc, Qt::ISODateWithMs);
        const QString time = (at.isValid() ? at.toLocalTime() : QDateTime::currentDateTime()).toString("HH:mm:ss");
        return demo ? QString("Demo upload at %1 (nothing leaves this Mac)").arg(time) : QString("Upload at %1").arg(time);
    }
    static QString itemLine(const QJsonObject& item) {
        if (displayState(item) == "failed") {
            const QJsonObject error = item["error"].toObject();
            const QString message = error["message"].toString(item["message"].toString()), fix = error["fix"].toString();
            return "Upload failed: " + message + (fix.isEmpty() ? QString() : " " + fix);
        }
        return linePrefix(item["demo"].toBool(), item["updated_utc"].toString()) + ": " + item["message"].toString();
    }
    static QString itemKey(const QJsonObject& item) {
        return displayState(item) + "|" + item["message"].toString() + "|" + item["error"].toObject()["fix"].toString();
    }
    // What a check of an upload on ARC found, when the upload this window follows gains a newer one:
    // "Checked at 16:05: Still on ARC: 21 files with the right sizes." (fix plan I1). A check that could not look
    // says why and what to do (review REG-01).
    static QString checkLine(const QJsonObject& check) {
        return QString("Checked at %1: %2").arg(DataText::localTime(check["utc"].toString(), "HH:mm"), ArcText::checkNote(check));
    }
    // Ok for a check that found the upload as it should be (Warn for a demo upload, which is never shown in
    // the colour of an upload to ARC), Warn for one that did not.
    static UiTone checkTone(const QJsonObject& item, const QJsonObject& check) {
        return check["ok"].toBool() && !item["demo"].toBool() ? UiTone::Ok : UiTone::Warn;
    }
    // A refusal of an arc-* command: what it says and what to do, in a tone for its kind.
    static QString refusal(const DataLayerReply& reply) {
        if (reply.json.isEmpty())
            return "Uploads to ARC: " + reply.error + (reply.detail.isEmpty() ? QString() : " (" + reply.detail.simplified() + ")");
        const QString fix = reply.json["fix"].toString();
        return reply.error + (fix.isEmpty() ? QString() : " " + fix);
    }
    static UiTone refusalTone(const DataLayerReply& reply) {
        static const QStringList cautious{"not_ready", "refused", "config", "not_installed", "conflict", "not_found"};
        return cautious.contains(reply.json["kind"].toString()) ? UiTone::Warn : UiTone::Danger;
    }
    // A data layer without the arc-* commands answers them as unknown commands.
    static bool notThere(const DataLayerReply& reply) {
        return reply.failure == DataLayerReply::Failure::Reported && reply.json["kind"].toString() == "usage";
    }

    // ------------------------------------------------------------------ commands

    // An upload is worked on, or is about to be: a program holds it, a listed upload is in a working state
    // (a program is starting) or waits for Terminal or the browser, or this window acted a moment ago.
    bool moving() const {
        if (active > 0 || (actedAt.isValid() && actedAt.elapsed() < afterActionMs)) return true;
        for (const QJsonValue& value : uploads) {
            const QJsonObject item = value.toObject();
            const QString needs = item["needs"].toString();
            if (ArcText::working(displayState(item)) || needs == "terminal" || needs == "browser") return true;
        }
        return false;
    }
    int pollInterval() const {
        const int interval = moving() ? activePollMs : idlePollMs;
        return acquisitionBusy ? std::max(interval, busyPollMs) : interval;
    }
    // The next list is due pollInterval() after the last answer: a change of the interval moves it, a
    // repeated change does not push it ever further.
    void reschedule() {
        if (!poll.isActive()) return;
        const qint64 since = listAnswered.isValid() ? listAnswered.elapsed() : 0;
        poll.start(int(std::max<qint64>(0, pollInterval() - since)));
    }
    void applyAcquisitionBusy(bool busy) {
        if (busy == acquisitionBusy) return;
        acquisitionBusy = busy;
        reschedule();
        if (!busy && sessionsDue) {
            sessionsDue = false;
            if (shown) loadSessions();
        }
        showAll();
    }
    // The uploads, newest first. One at a time: a request made while one runs is made once afterwards.
    void listUploads() {
        if (absent || !everShown) return;
        if (listRunning) { listAgain = true; return; }
        listRunning = true;
        poll.stop();
        dataLayer->run({"arc-list", "--limit=12"}, [this](const DataLayerReply& reply) {
            listRunning = false;
            listAnswered.start();
            if (reply.ok()) {
                listProblem.clear();
                showUploads(reply.json);
            } else if (notThere(reply)) {
                showAbsent();
            } else {
                listProblem = refusal(reply);
                listProblemTone = refusalTone(reply);
                showAll();
            }
            if (listAgain) { listAgain = false; listUploads(); }
            else if (shown && !absent) poll.start(pollInterval());
        });
    }
    // The page is hidden while the toggle shows uploads that are not finished or need the person: only the
    // counts are read again, at the idle rate, so that the toggle's colour follows the uploads (I2).
    void followHidden() {
        if (shown || absent || (std::max(unfinished, 0) == 0 && attention == 0)) {
            hiddenPoll.stop();
            return;
        }
        if (!hiddenPoll.isActive() && !hiddenListRunning) hiddenPoll.start(idlePollMs);
    }
    void listHidden() {
        if (shown || absent || hiddenListRunning) return;
        hiddenListRunning = true;
        dataLayer->run({"arc-list", "--limit=1"}, [this](const DataLayerReply& reply) {
            hiddenListRunning = false;
            if (reply.ok()) noteCounts(reply.json["unfinished"].toInt(std::max(unfinished, 0)), reply.json["attention"].toInt());
            else if (notThere(reply)) showAbsent();
            followHidden();
        });
    }
    void loadSessions() {
        if (absent || !everShown) return;
        if (sessionsRunning) { sessionsAgain = true; return; }
        sessionsRunning = true;
        QStringList command{"arc-sessions", "--limit=" + QString::number(sessionsLimit)};
        if (!acquisitionBusy) command << "--controller-idle";
        dataLayer->run(command, [this](const DataLayerReply& reply) {
            sessionsRunning = false;
            if (reply.ok()) {
                sessionsProblem.clear();
                showSessions(reply.json);
            } else if (notThere(reply)) {
                showAbsent();
            } else {
                sessionsProblem = refusal(reply);
                sessionsProblemTone = refusalTone(reply);
                showAll();
            }
            if (sessionsAgain) { sessionsAgain = false; loadSessions(); }
        });
    }
    // arc-status of the selected upload, for its history. The newest request wins.
    void wantStatus(const QString& id) {
        statusWanted = id;
        if (id.isEmpty() || !everShown || absent) { statusShown = QJsonObject(); showDetails(); return; }
        if (statusRunning) { statusAgain = true; return; }
        statusRunning = true;
        dataLayer->run({"arc-status", "--upload=" + id}, [this, id](const DataLayerReply& reply) {
            statusRunning = false;
            if (statusAgain || id != statusWanted) {
                statusAgain = false;
                if (!statusWanted.isEmpty()) wantStatus(statusWanted);
                return;
            }
            statusShown = reply.ok() ? reply.json["upload"].toObject() : QJsonObject();
            statusFailure = reply.ok() ? QString() : refusal(reply);
            showDetails();
        });
    }

    // Upload to ARC: a dry run first, which says what would go where; a live upload is confirmed in a
    // dialog, a demo upload starts at once. newCopy: the person chose a new copy beside an earlier one.
    void askPlan(const Choice& asked, bool newCopy) {
        if (actionRunning || absent || asked.folder.isEmpty()) return;
        if (acquisitionBusy) { setLine(kAfterAcquisition, UiTone::Neutral); return; }
        startAction();
        dataLayer->run(asked.command(true, newCopy, !acquisitionBusy), [this, asked, newCopy](const DataLayerReply& reply) {
            endAction();
            if (!reply.ok()) { uploadRefused(reply, asked); return; }
            const QJsonValue existing = reply.json["existing"];
            if (existing.isObject()) { showExisting(existing.toObject()); return; }
            if (existing.toBool() && reply.json["upload"].isObject()) { showExisting(reply.json["upload"].toObject()); return; }
            if (asked.demo) sendUpload(asked, newCopy);
            else confirmUpload(asked, newCopy, reply.json["plan"].toObject());
        });
    }
    void sendUpload(const Choice& asked, bool newCopy) {
        if (actionRunning) return;
        if (acquisitionBusy) { setLine(kAfterAcquisition, UiTone::Neutral); return; }
        startAction();
        dataLayer->run(asked.command(false, newCopy, !acquisitionBusy), [this, asked](const DataLayerReply& reply) {
            endAction();
            const QJsonObject upload = reply.json["upload"].toObject();
            if (reply.ok()) actedAt.start();
            if (reply.ok() && reply.json["existing"].toBool()) {
                showExisting(upload);
            } else if (reply.ok()) {
                follow(upload);
                showStarted(upload, reply.json["message"].toString(), true);
            } else {
                uploadRefused(reply, asked);
            }
            listUploads();
        });
    }
    void uploadRefused(const DataLayerReply& reply, const Choice& asked) {
        const QString reason = reply.json["reason"].toString();
        const QJsonObject upload = reply.json["upload"].toObject();
        if (reply.json["kind"].toString() == "conflict" && reason == "earlier_upload" && !upload.isEmpty()) { askEarlier(asked, upload); return; }
        if (reply.json["kind"].toString() == "conflict" && reason == "already_on_arc" && !upload.isEmpty()) { askAgain(asked, upload); return; }
        if (!upload.isEmpty() && reason != "in_progress" && reason != "another_live_upload" && reason != "busy") {
            // The upload was made, but its work could not be started (Terminal did not open).
            follow(upload);
            setLine("Upload failed: " + refusal(reply), UiTone::Danger);
            listUploads();
            return;
        }
        setLine(refusal(reply), refusalTone(reply));
        const int later = reply.json["retry_after_seconds"].toInt();
        if (later > 0) sessionsLater.start(std::min(later, 600) * 1000 + 500);
        if (reply.json["kind"].toString() == "not_ready" || reply.json["kind"].toString() == "refused") loadSessions();
    }

    // Check status, Retry same request, Stop upload, Keep as new copy, for the selected upload. A live
    // upload asks first; a demo upload, which sends nothing anywhere, does not.
    void askFirst(const QString& action) {
        const QJsonObject item = selectedUpload();
        if (item.isEmpty() || actionRunning || !offers(item, action.toUtf8().constData())) return;
        if (acquisitionBusy && action != "stop") return;
        const QString id = item["upload_id"].toString();
        const QString command = action == "keep_copy" ? QString("arc-keep-copy") : "arc-" + action;
        const auto send = [this, command, id] { runAction(command, id); };
        if (item["demo"].toBool()) { send(); return; }
        const bool ssh = item["transport"].toString() == "ssh";
        const QJsonObject destination = item["destination"].toObject();
        // A live SSH upload signs in with the VT username saved with it, whatever ARC settings say now.
        const QString account = destination["ssh_target"].toString().section(':', 0, 0);
        if (action == "retry") {
            openDialog("Retry the same upload",
                       QString("Send the same upload again? Upload %1 of %2 with %3, to %4. %5It uses the settings saved with the upload, "
                               "not the current ARC settings.")
                           .arg(id, item["session_folder"].toString(), ssh ? "SSH" : "Globus", destination["final_path"].toString(),
                                retrySentence(item)),
                       {{"Retry", send, true}, {"Cancel", nullptr}});
        } else if (action == "check") {
            openDialog("Check status",
                       QString("Check this upload on ARC? %1 Nothing is sent.")
                           .arg(!ssh ? QString("A web page opens for you to sign in to Globus.")
                                : account.isEmpty() ? QString("A Terminal window opens for your VT password and Duo, once.")
                                                    : QString("A Terminal window opens for the VT password of %1 and Duo, once.").arg(account)),
                       {{"Check", send, true}, {"Cancel", nullptr}});
        } else if (action == "stop" && checkMayCancel(item)) {
            const QString incoming = destination["incoming_path"].toString();
            openDialog("Stop upload",
                       QString("Stop this upload? The check ends, and the transfer Globus may still be running is cancelled (after the "
                               "Globus sign-in, if that page is still open). Files already copied stay in %1 on ARC; nothing is deleted.")
                           .arg(incoming.isEmpty() ? QString("_incoming") : incoming),
                       {{"Stop the upload", send, true}, {"Keep going", nullptr}});
        } else if (action == "stop" && checkRuns(item)) {
            // Stopping it ends the check: nothing is sent or deleted, and the upload stays as it was, also when the check was
            // checking or placing the copy of a Globus transfer that had ended (review R2-01).
            openDialog("Stop upload", "Stop the check of this upload? Nothing is sent or deleted, and the upload stays as it is.",
                       {{"Stop the check", send, true}, {"Keep going", nullptr}});
        } else if (action == "stop") {
            // A Globus transfer or request nobody watches is cancelled by a program that signs in first (section 8.6):
            // a request whose answer is unknown, a transfer that was still copying, or a request the program may
            // have sent before it stopped. A transfer that had already ended stops at once, without a sign-in.
            const QString incoming = destination["incoming_path"].toString();
            const QString shown = displayState(item), stored = item["state"].toString();
            const bool unwatched = !ssh && !item["worker"].toObject()["alive"].toBool() &&
                                   (shown == "uncertain" || (shown == "not_watched" && stored == "transferring") ||
                                    (shown == "interrupted" && stored == "submitting"));
            openDialog("Stop upload",
                       QString("Stop this upload? Files already copied stay in %1 on ARC; nothing is deleted.%2")
                           .arg(incoming.isEmpty() ? QString("_incoming") : incoming,
                                unwatched ? QString(" A web page opens for you to sign in to Globus; then the transfer is cancelled.") : QString()),
                       {{"Stop the upload", send, true}, {"Keep going", nullptr}});
        } else {
            const QString folder = item["session_folder"].toString();
            openDialog("Keep as new copy",
                       QString("ARC already has a folder named %1 in %2. Put this upload beside it as %1_copy2 (or the next free name)? "
                               "Nothing is replaced.").arg(folder, parentOf(destination["final_path"].toString())),
                       {{"Put it beside", send, true}, {"Not now", nullptr}});
        }
    }
    // What Retry same request does for this upload (section 7.7).
    static QString retrySentence(const QJsonObject& item) {
        const QString state = displayState(item), reason = item["error"].toObject()["reason"].toString();
        const QString account = item["destination"].toObject()["ssh_target"].toString().section(':', 0, 0);
        if (item["transport"].toString() == "ssh" && !account.isEmpty())
            return QString("A Terminal window opens for the VT password of %1 and Duo; the files are sent again into the same folder on ARC "
                           "unless ARC has them all. ")
                .arg(account);
        if (item["transport"].toString() == "ssh")
            return "A Terminal window opens for your password and Duo; the files are sent again into the same folder on ARC unless ARC has "
                   "them all. ";
        if (state == "uncertain") return "Globus is asked again with the same request number, so it cannot make a second copy. ";
        if (state == "failed" && (reason == "task_failed" || reason == "verify_mismatch"))
            return "Globus copies the same files again into the same _incoming folder. ";
        if (state == "conflict") return QString();
        return "It continues where it stopped. ";
    }
    void runAction(const QString& command, const QString& id) {
        if (actionRunning) return;
        if (acquisitionBusy && command != "arc-stop") { setLine(kAfterAcquisition, UiTone::Neutral); return; }
        startAction();
        const QJsonObject before = uploadById(id);
        dataLayer->run({command, "--upload=" + id}, [this, id, command, before](const DataLayerReply& reply) {
            endAction();
            QJsonObject upload = reply.json["upload"].toObject();
            if (upload.isEmpty()) upload = before;
            // A check whose result is in the reply already is a newer one than the upload had before.
            follow(upload, before.isEmpty() ? upload : before);
            trackedUpload = id;
            if (!reply.ok()) {
                setLine(refusal(reply), refusalTone(reply));
            } else {
                actedAt.start();
                // An upload stopped at once is done; otherwise the reply's words stand until the upload moves
                // on, or until the program the command started has ended (actionPending).
                const bool doneNow = reply.json["stopped_now"].toBool();
                if (command == "arc-stop" && reply.json["message"].toString().isEmpty() && !reply.json["stopped_now"].toBool(true)) {
                    actionPending = true;
                    setLine(linePrefix(upload["demo"].toBool(), QString()) + ": Stopping: the upload program stops within a second.", UiTone::Info);
                } else {
                    showStarted(upload, reply.json["message"].toString(), !doneNow);
                }
            }
            listUploads();
        });
    }
    void startAction() {
        actionRunning = true;
        showAll();
    }
    void endAction() {
        actionRunning = false;
        showAll();
    }

    void copyPath() {
        const QJsonObject item = selectedUpload();
        if (!offers(item, "copy_path")) return;
        QGuiApplication::clipboard()->setText(pathOnArc(item));
    }
    void showInFinder() {
        const QJsonObject item = selectedUpload();
        if (!offers(item, "show_in_finder")) return;
        const QString folder = item["destination"].toObject()["local_final_path"].toString();
        if (folder.isEmpty() || !openUrl(QUrl::fromLocalFile(folder)))
            setLine("The demo folder could not be opened: " + QDir::toNativeSeparators(folder), UiTone::Warn);
    }

    // ------------------------------------------------------------------ dialogs

    // Not modal, opened with show(), one at a time: a new question takes the place of one still open.
    QDialog* openDialog(const QString& title, const QString& text, const QList<Button>& buttons) {
        if (actionDialog) actionDialog->close();
        auto* dialog = new QDialog(content->window());
        actionDialog = dialog;
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setWindowModality(Qt::NonModal);
        dialog->setWindowTitle(title);
        auto* layout = new QVBoxLayout(dialog);
        layout->setContentsMargins(16, 16, 16, 16);
        layout->setSpacing(14);
        auto* words = new QLabel(text);
        words->setObjectName("arcDialogText");
        words->setTextFormat(Qt::PlainText);
        words->setWordWrap(true);
        words->setTextInteractionFlags(Qt::TextSelectableByMouse);
        words->setMinimumWidth(520);
        layout->addWidget(words);
        auto* row = new QHBoxLayout;
        row->addStretch(1);
        QPushButton* primary = nullptr;
        QList<QPushButton*> others;
        for (const Button& each : buttons) {
            // Only the primary answer is the default: Return never picks another one because it has the focus.
            auto* made = new QPushButton(each.text, dialog);
            made->setAutoDefault(each.primary);
            made->setEnabled(each.enabled);
            if (!each.tip.isEmpty()) made->setToolTip(each.tip);
            const std::function<void()> act = each.act;
            connect(made, &QPushButton::clicked, this, [dialog, act] {
                dialog->close();
                if (act) act();
            });
            if (each.primary) primary = made;
            else others << made;
        }
        for (QPushButton* other : others) row->addWidget(other);
        if (primary) {
            primary->setProperty("role", "primary");
            primary->setDefault(true);
            row->addWidget(primary);
        }
        layout->addLayout(row);
        dialog->show();
        return dialog;
    }
    // The confirmation of a live upload, built from the dry run's plan (section 14.5).
    // The confirmation names a destination and an account from the settings of its dry run: new settings
    // close it (openSettings), so that Upload never sends where the dialog does not say.
    void confirmUpload(const Choice& asked, bool newCopy, const QJsonObject& plan) {
        confirmation = openDialog("Upload to ARC", confirmationText(asked, plan),
                                  {{"Upload", [this, asked, newCopy] { sendUpload(asked, newCopy); }, true}, {"Cancel", nullptr}});
    }
    QString confirmationText(const Choice& asked, const QJsonObject& plan) const {
        const QJsonObject session = plan["session"].toObject(), destination = plan["destination"].toObject();
        const bool ssh = plan["transport"].toString(asked.transport) == "ssh";
        QStringList sent;
        for (const QJsonValue& dark : plan["darks"].toArray())
            if (dark.toObject()["sent"].toBool()) sent << dark.toObject()["file"].toString();
        // "no dark spectrum, and a list ...": never "without a dark spectrum and a list", which reads as if the list were missing too.
        const QString dark = sent.isEmpty() ? QString("no dark spectrum,")
                           : sent.size() == 1 ? "with the dark spectrum " + sent.first() : "with the dark spectra " + sent.join(" and ");
        const QString cluster = destination["cluster_path"].toString();
        QStringList lines{QString("Send session %1 to ARC?").arg(session["folder"].toString(asked.folder)), QString()};
        lines << QString("%1 (%2), %3, %4, %5 and a list of every file with its SHA-256.")
                     .arg(ArcText::count(session["points"].toInt(), "spectrum", "spectra"), ArcText::end(session["end"].toString()),
                          ArcText::count(plan["files"].toInt(), "file", "files"), ArcText::size(plan["bytes"].toDouble()), dark);
        lines << "To:  " + cluster;
        // Both ways of sending reach the same folder; SSH changes only how it gets there.
        if (!destination["globus_path"].toString().isEmpty())
            lines << "     (in ARC's Globus collection: " + destination["globus_path"].toString() + ")";
        if (ssh) {
            QString account = destination["ssh_target"].toString().section(':', 0, 0);
            if (account.isEmpty())
                account = setting("ssh.username").toString() + "@" + setting("ssh.host").toString("datatransfer.arc.vt.edu");
            lines << QString("By:  SSH as %1. A Terminal window opens; this window never sees what you type there. Not you? Change the VT "
                             "username in ARC settings first.").arg(account);
            // ssh asks about ARC's key only while it does not know it: not with the team's file of ARC's keys.
            const QString keys = setting("ssh.known_hosts").toString();
            QStringList steps;
            if (keys.isEmpty()) steps << kSshHostKey;
            steps << kSshPassword << kSshDuo;
            lines << "     ssh asks, in this order:";
            for (int step = 0; step < steps.size(); ++step) lines << QString("     %1. %2").arg(step + 1).arg(steps.at(step));
            if (!keys.isEmpty()) lines << QString("     ssh checks ARC's key against the team's file %1; it does not ask.").arg(keys);
            lines << "     " + kSshWarning << "     " + kSshNetwork;
        } else {
            lines << "By:  Globus. A web page opens for you to sign in. Keep Globus Connect Personal running, and this Mac awake, until "
                     "the upload says \"On ARC, checked\".";
        }
        QStringList more;
        for (const QJsonValue& note : plan["notes"].toArray()) more << ArcText::text(note);
        for (const QJsonValue& warning : plan["warnings"].toArray()) more << ArcText::text(warning);
        more.removeAll(QString());
        if (!more.isEmpty()) lines << QString() << more;
        lines << QString() << kGoesAsTheyAre;
        return lines.join("\n");
    }
    // conflict / earlier_upload: the session has an upload that failed or was stopped.
    void askEarlier(const Choice& asked, const QJsonObject& upload) {
        follow(upload);
        const bool canRetry = offers(upload, "retry");
        const QString id = upload["upload_id"].toString();
        openDialog("Earlier upload",
                   QString("This session has an earlier upload that ended as %1: %2 Retry that request, or start a new upload with the current "
                           "ARC settings?").arg(upload["display_label"].toString(), upload["message"].toString()),
                   {{"Retry that request", [this, id] { runAction("arc-retry", id); }, true, canRetry,
                     canRetry ? QString() : QString("The files of that upload are no longer kept, or it cannot be continued.")},
                    {"Start a new upload", [this, asked] { askPlan(asked, true); }},
                    {"Cancel", nullptr}});
    }
    // conflict / already_on_arc: the session is on ARC (or, for a demo upload, in the demo folder).
    // "Checked on" is when the copy was last found as it should be: the newest check that was ok, else the
    // check made when it was put in place; a check that failed is never that date.
    void askAgain(const Choice& asked, const QJsonObject& upload) {
        follow(upload);
        const QString when = DataText::localTime(ArcText::checkedUtc(upload), "yyyy-MM-dd HH:mm");
        const bool inDemo = upload["demo"].toBool();
        const QString where = inDemo ? upload["destination"].toObject()["local_final_path"].toString(pathOnArc(upload)) : pathOnArc(upload);
        const QString folder = upload["session_folder"].toString(asked.folder);
        const QString placed = upload["destination"].toObject()["placed_as"].toString(folder);
        // A demo upload is in the demo folder on this Mac, not on ARC (review COR-05).
        openDialog(inDemo ? "Already in the demo folder" : "Already on ARC",
                   QString("This session is already %1, checked on %2, at %3. Upload it again? The new copy goes beside it as %4 (or the next "
                           "free name).").arg(inDemo ? "in the demo folder" : "on ARC", when, where, ArcText::nextCopyName(folder, placed)),
                   {{"Upload again", [this, asked] { askPlan(asked, true); }, true}, {"Cancel", nullptr}});
    }

    // ARC settings: filled from arc-doctor, saved with one config --set of exactly the changed keys.
    struct SettingField {
        QString key;
        QLineEdit* edit = nullptr;
        QCheckBox* box = nullptr;
    };
    // A setting as its field shows it and config --set takes it back: true/false, 60, text; a value of
    // another type (an invalid one) in the same words, so that an untouched field is never sent.
    static QString settingText(const QJsonValue& value) {
        if (value.isBool()) return value.toBool() ? "true" : "false";
        if (value.isDouble()) return QString::number(value.toDouble());
        return value.isString() ? value.toString() : QString();
    }
    QDialog* openSettings() {
        if (settingsDialog) {
            settingsDialog->raise();
            settingsDialog->activateWindow();
            return settingsDialog;
        }
        auto* dialog = new QDialog(content->window());
        settingsDialog = dialog;
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setWindowModality(Qt::NonModal);
        dialog->setWindowTitle("ARC settings");
        auto* form = new QGridLayout(dialog);
        form->setContentsMargins(16, 16, 16, 16);
        form->setHorizontalSpacing(10);
        form->setVerticalSpacing(6);
        form->setColumnStretch(1, 1);
        int row = 0;
        const auto caption = [](const QString& text) {
            auto* made = new QLabel(text);
            made->setObjectName("caption");
            made->setWordWrap(true);
            return made;
        };
        const auto addField = [&](QGridLayout* grid, int& at, const QString& name, QWidget* field, QLabel* note = nullptr) {
            auto* title = new QLabel(name);
            title->setObjectName("fieldLabel");
            grid->addWidget(title, at, 0);
            grid->addWidget(field, at++, 1);
            if (note) grid->addWidget(note, at++, 1);
        };
        auto fields = std::make_shared<QList<SettingField>>();
        const auto lineEdit = [&](const QString& key, const char* name, const QString& placeholder = QString()) {
            auto* edit = new QLineEdit;
            edit->setObjectName(name);
            edit->setPlaceholderText(placeholder);
            edit->setMinimumWidth(380);
            fields->append({key, edit, nullptr});
            return edit;
        };
        const auto checkBox = [&](const QString& key, const char* name, const QString& text) {
            auto* box = new QCheckBox(text);
            box->setObjectName(name);
            fields->append({key, nullptr, box});
            return box;
        };
        QLineEdit* macCollection = lineEdit("globus.mac_collection", "arcMacCollection", "paste the ID from Globus Connect Personal");
        addField(form, row, "This Mac's Globus collection ID", macCollection,
                 caption("Needed for Globus only. Globus Connect Personal shows it under Settings (its icon in the menu bar); app.globus.org "
                         "shows it under Collections. It is an ID, not a password."));
        QLineEdit* username = lineEdit("ssh.username", "arcUsername", "the part of your VT email before @vt.edu");
        addField(form, row, "Your VT username", username,
                 caption("Needed for SSH only. Your password is never kept: you type it in Terminal each time."));
        QLineEdit* folder = lineEdit("folder", "arcFolderSetting");
        QLabel* folderNote = caption(QString());
        folderNote->setObjectName("arcFolderNote");
        addField(form, row, "ARC folder (on the cluster)", folder, folderNote);
        auto* globus = new QRadioButton("Globus");
        auto* ssh = new QRadioButton("SSH");
        globus->setObjectName("arcUsuallyGlobus");
        ssh->setObjectName("arcUsuallySsh");
        auto* usually = new QButtonGroup(dialog);
        usually->addButton(globus);
        usually->addButton(ssh);
        globus->setChecked(true);
        auto* usuallyRow = new QHBoxLayout;
        usuallyRow->addWidget(globus);
        usuallyRow->addWidget(ssh);
        usuallyRow->addStretch(1);
        auto* usuallyBox = new QWidget;
        usuallyBox->setLayout(usuallyRow);
        usuallyRow->setContentsMargins(0, 0, 0, 0);
        addField(form, row, "Usually send with", usuallyBox);
        form->addWidget(checkBox("include_simulated", "arcIncludeSimulated", "Also send simulated sessions (into the separate folder simulated/)"), row++, 1);
        form->addWidget(checkBox("demo", "arcDemoDefault", "Demo by default (no ARC, no account)"), row++, 1);
        form->addWidget(checkBox("globus.force_login", "arcForceLogin", "Ask for the Globus sign-in every time (a Mac that several people use)"), row++, 1);

        // More: what the team sets once.
        auto* more = new QToolButton;
        more->setObjectName("arcMore");
        more->setText("More");
        more->setCheckable(true);
        more->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
        more->setArrowType(Qt::RightArrow);
        more->setAutoRaise(true);
        auto* moreBox = new QWidget;
        moreBox->setObjectName("arcMoreBox");
        auto* moreForm = new QGridLayout(moreBox);
        moreForm->setContentsMargins(0, 0, 0, 0);
        moreForm->setHorizontalSpacing(10);
        moreForm->setVerticalSpacing(6);
        moreForm->setColumnStretch(1, 1);
        int moreRow = 0;
        addField(moreForm, moreRow, "ARC's Globus collection ID", lineEdit("globus.arc_collection", "arcArcCollection"));
        QLineEdit* root = lineEdit("globus.collection_root", "arcCollectionRoot");
        addField(moreForm, moreRow, "Folder the collection shows as its top", root);
        addField(moreForm, moreRow, "Globus app ID", lineEdit("globus.client_id", "arcClientId"),
                 caption("The default is the Globus SDK's public example app, for trying this out. For regular use, register an app for the "
                         "team at app.globus.org (Settings, Developers) and enter its ID here."));
        addField(moreForm, moreRow, "SSH computer", lineEdit("ssh.host", "arcSshHost"));
        addField(moreForm, moreRow, "Team file of ARC's SSH keys", lineEdit("ssh.known_hosts", "arcKnownHosts"),
                 caption("A known-hosts file with ARC's keys that the team checked with ARC. Empty: ssh asks you to confirm ARC's key the "
                         "first time."));
        // Every other ARC setting, so that Save can correct each value the data layer refuses.
        addField(moreForm, moreRow, "Wait after a scan ends (seconds)", lineEdit("settle_seconds", "arcSettleSeconds"),
                 caption("A session may go once nothing has written into it for this long: 10 to 3600 seconds, 60 by default."));
        addField(moreForm, moreRow, "Time Globus may take (hours)", lineEdit("globus.deadline_hours", "arcDeadlineHours"),
                 caption("Globus gives up an upload that has not finished by then: 1 to 168 hours, 24 by default."));
        addField(moreForm, moreRow, "Demo folder (on this Mac)", lineEdit("demo_folder", "arcDemoFolder"),
                 caption("Where Demo puts its copies: a folder inside the controller's folder, such as data_catalog/arc_demo (the default), "
                         "or a full path. Not inside scan_sessions, dark_measurements or data_catalog/arc_uploads."));
        moreBox->hide();
        connect(more, &QToolButton::toggled, dialog, [more, moreBox, dialog](bool open) {
            more->setArrowType(open ? Qt::DownArrow : Qt::RightArrow);
            moreBox->setVisible(open);
            dialog->adjustSize();
        });
        form->addWidget(more, row++, 0, 1, 2, Qt::AlignLeft);
        form->addWidget(moreBox, row++, 0, 1, 2);

        // The checks of arc-doctor, and the result of Save.
        auto* checkSetup = new QPushButton("Check setup", dialog);
        checkSetup->setAutoDefault(false);
        checkSetup->setToolTip("Look again at what each way of sending needs on this Mac.");
        auto* checks = new QLabel;
        checks->setObjectName("arcChecks");
        checks->setWordWrap(true);
        checks->setTextFormat(Qt::RichText);
        auto* result = new QLabel;
        result->setObjectName("arcSettingsResult");
        result->setWordWrap(true);
        result->hide();
        form->addWidget(checkSetup, row, 0, Qt::AlignTop | Qt::AlignLeft);
        form->addWidget(checks, row++, 1);
        form->addWidget(result, row++, 0, 1, 2);
        auto* save = new QPushButton("Save", dialog);
        save->setProperty("role", "primary");
        save->setDefault(true);
        save->setEnabled(false);
        auto* cancel = new QPushButton("Cancel", dialog);
        cancel->setAutoDefault(false);
        auto* buttons = new QHBoxLayout;
        buttons->addStretch(1);
        buttons->addWidget(cancel);
        buttons->addWidget(save);
        form->addLayout(buttons, row++, 0, 1, 2);

        // What arc-doctor said the settings are: Save sends only what differs from them.
        auto known = std::make_shared<QJsonObject>();
        const auto valueOf = [](const SettingField& field) {
            return field.box ? QString(field.box->isChecked() ? "true" : "false") : field.edit->text().trimmed();
        };
        const auto knownValue = [known](const QString& key) { return settingText(setting(*known, key)); };
        const auto describeFolder = [folder, root, folderNote] {
            const QString path = folder->text().trimmed(), top = root->text().trimmed().isEmpty() ? QString("/projects") : root->text().trimmed();
            folderNote->setText(ArcText::validFolder(path, top)
                                    ? QString("Each session goes into a folder of its own inside it. Globus reaches the same folder as %1.")
                                          .arg(ArcText::collectionPath(path, top))
                                    : QString("Use a folder under /projects/, such as /projects/pcl_photonics/raman_globus_upload."));
            setStyleProperty(folderNote, "tone", ArcText::validFolder(path, top) ? QVariant() : QVariant("warn"));
        };
        connect(folder, &QLineEdit::textChanged, dialog, describeFolder);
        connect(root, &QLineEdit::textChanged, dialog, describeFolder);
        const auto doctor = [this, dialog, checks, save, fields, known, globus, ssh, describeFolder, more](bool fill) {
            checks->setText("Checking the setup...");
            dataLayer->run({"arc-doctor"}, [dialog = QPointer<QDialog>(dialog), checks, save, fields, known, globus, ssh, describeFolder,
                                            more, fill](const DataLayerReply& reply) {
                if (!dialog) return;
                if (!reply.ok()) {
                    checks->setText(QString("<span style='color:%1;'>%2</span>").arg(uiTheme().dangerInk.name(), refusal(reply).toHtmlEscaped()));
                    return;
                }
                QStringList lines;
                bool valid = true;
                for (const QJsonValue& value : reply.json["checks"].toArray()) {
                    const QJsonObject check = value.toObject();
                    const QString detail = check["detail"].toString().toHtmlEscaped();
                    if (check["name"].toString() == "settings_valid" && !check["ok"].toBool()) valid = false;
                    lines << (check["ok"].toBool() ? "OK: " + detail
                                                   : QString("<span style='color:%1;'>Missing: %2 %3</span>")
                                                         .arg(uiTheme().warnInk.name(), detail, check["fix"].toString().toHtmlEscaped()));
                }
                checks->setText(lines.join("<br>"));
                if (!fill) return;
                *known = reply.json["settings"].toObject();
                for (const SettingField& field : *fields) {
                    const QJsonValue value = setting(*known, field.key);
                    if (field.box) field.box->setChecked(value.toBool());
                    else field.edit->setText(settingText(value));
                }
                // A value the data layer refuses may be one of the team's settings: they are all shown then.
                if (!valid) more->setChecked(true);
                (setting(*known, "transport").toString() == "ssh" ? ssh : globus)->setChecked(true);
                describeFolder();
                save->setEnabled(true);
            });
        };
        connect(checkSetup, &QPushButton::clicked, dialog, [doctor] { doctor(false); });
        connect(cancel, &QPushButton::clicked, dialog, &QDialog::reject);
        connect(save, &QPushButton::clicked, this, [this, dialog, save, result, fields, valueOf, knownValue, ssh] {
            // Exactly the keys that differ from what arc-doctor said, in one command: all are saved or none.
            QStringList command{"config"};
            QHash<QString, QString> changed;
            for (const SettingField& field : *fields)
                if (valueOf(field) != knownValue(field.key)) changed.insert(field.key, valueOf(field));
            const QString usually = ssh->isChecked() ? "ssh" : "globus";
            if (usually != knownValue("transport")) changed.insert("transport", usually);
            if (changed.isEmpty()) { dialog->accept(); return; }
            for (const SettingField& field : *fields)
                if (changed.contains(field.key)) command << "--set=arc_upload." + field.key + "=" + changed.value(field.key);
            if (changed.contains("transport")) command << "--set=arc_upload.transport=" + usually;
            save->setEnabled(false);
            result->hide();
            dataLayer->run(command, [this, dialog = QPointer<QDialog>(dialog), save, result, changed](const DataLayerReply& reply) {
                if (!reply.ok()) {
                    if (!dialog) return;
                    // Nothing was saved: the dialog stays open with what was refused, in the danger tone.
                    result->setText(reply.error + (reply.detail.isEmpty() ? QString() : " (" + reply.detail.simplified() + ")"));
                    setStyleProperty(result, "tone", "danger");
                    result->show();
                    save->setEnabled(true);
                    return;
                }
                // A changed default applies to the page at once; everything else is read again.
                if (changed.contains("demo")) demoBox->setChecked(changed.value("demo") == "true");
                if (changed.contains("transport")) (changed.value("transport") == "ssh" ? sshRadio : globusRadio)->setChecked(true);
                forget();
                // An open confirmation names the folder and the account of the old settings, while its Upload
                // would send with the new ones: it is closed, and a new dry run makes the next one.
                if (confirmation && confirmation->isVisible()) {
                    confirmation->close();
                    setLine("The ARC settings changed, so the open confirmation was closed. Press Upload to ARC again to see where the "
                            "session goes now.", UiTone::Info);
                }
                if (dialog) dialog->accept();
                listUploads();
                loadSessions();
            });
        });
        dialog->show();
        doctor(true);
        return dialog;
    }

    // ------------------------------------------------------------------ showing

    void showAbsent() {
        absent = true;
        poll.stop();
        hiddenPoll.stop();
        showAll();
    }
    // The numbers behind the toggle's colour: uploads that are not finished, and uploads that need the person.
    void noteCounts(int unfinishedNow, int attentionNow) {
        if (unfinishedNow == unfinished && attentionNow == attention) return;
        unfinished = unfinishedNow;
        attention = attentionNow;
        if (countsChanged) countsChanged(unfinished, attention);
        followHidden();
    }
    void showUploads(const QJsonObject& answer) {
        listLoaded = true;
        uploads = answer["items"].toArray();
        active = answer["active"].toInt();
        readiness = answer["ready"].toObject();
        listSettings = answer["settings"].toObject();
        applyDefaults(setting(listSettings, "demo"), setting(listSettings, "transport"));
        if (answer["unfinished"].isDouble()) noteCounts(answer["unfinished"].toInt(), answer["attention"].toInt());
        // An upload that has just become final changes what the sessions say ("on ARC").
        bool becameFinal = false;
        QHash<QString, QString> states;
        for (const QJsonValue& value : uploads) {
            const QJsonObject item = value.toObject();
            const QString id = item["upload_id"].toString(), state = displayState(item);
            states.insert(id, state);
            if (lastStates.contains(id) && ArcText::isFinal(state) && lastStates.value(id) != state) becameFinal = true;
        }
        lastStates = states;
        showUploadRows();
        if (!trackedUpload.isEmpty()) {
            const QJsonObject item = uploadById(trackedUpload);
            if (!item.isEmpty()) followTracked(item);
        }
        if (becameFinal) loadSessions();
        showAll();
    }
    // The status line follows the upload of this window: a check newer than the one it had says what it
    // found, a new state or message takes the line, and the words of an action stand until the upload moves
    // on or the program the action started has ended (its record was written since and nobody holds it).
    void followTracked(const QJsonObject& item) {
        const QJsonObject check = item["last_check"].toObject();
        const QString checked = check["utc"].toString(), key = itemKey(item);
        const bool ended = actionPending && !item["worker"].toObject()["alive"].toBool() && item["updated_utc"].toString() != trackedUpdated;
        if (!checked.isEmpty() && checked != trackedCheck) {
            trackedCheck = checked;
            trackedKey = key;
            actionPending = false;
            setLine(checkLine(check), checkTone(item, check));
        } else if (key != trackedKey || ended) {
            trackedKey = key;
            actionPending = false;
            setLine(itemLine(item), ArcText::tone(item));
        }
    }
    void showUploadRows() {
        const QString keep = wantSelected.isEmpty() ? selectedUpload()["upload_id"].toString() : wantSelected;
        int keptRow = -1;
        {
            const QSignalBlocker quiet(recentTable);
            recentTable->clearSelection();
            recentTable->setRowCount(int(uploads.size()));
            const QFont stateFont = uiFont(recentTable, 12, QFont::Bold);
            for (int row = 0; row < uploads.size(); ++row) {
                const QJsonObject item = uploads.at(row).toObject();
                const QString created = item["created_utc"].toString();
                recentTable->setItem(row, 0, cell(DataText::localTime(created, "MM-dd HH:mm"), DataText::localTime(created, "yyyy-MM-dd HH:mm:ss")));
                recentTable->setItem(row, 1, cell(item["session_folder"].toString()));
                recentTable->setItem(row, 2, cell(ArcText::transport(item)));
                QStringList tip{item["message"].toString()};
                // An upload on ARC whose newest check failed: what that check found. A newest check that could not look:
                // why, and what to do (review REG-01).
                const QJsonObject check = item["last_check"].toObject();
                if ((ArcText::lastCheckFailed(item) || ArcText::lastCheckNotLooked(item)) && !check["message"].toString().isEmpty() &&
                    !tip.contains(check["message"].toString()))
                    tip << "Last check, " + DataText::localTime(check["utc"].toString(), "yyyy-MM-dd HH:mm") + ": " + ArcText::checkNote(check);
                if (!item["error"].toObject()["fix"].toString().isEmpty()) tip << item["error"].toObject()["fix"].toString();
                if (!item["identity"].toString().isEmpty()) tip << "Signed in to Globus as " + item["identity"].toString();
                auto* state = cell(item["display_label"].toString(item["state_label"].toString(displayState(item))), tip.join("\n"));
                const UiTone tone = ArcText::tone(item);
                state->setForeground(tone == UiTone::Neutral ? uiTheme().textDim : uiTheme().ink(tone));
                state->setFont(stateFont);
                recentTable->setItem(row, 3, state);
                const QString placed = item["destination"].toObject()["placed_as"].toString();
                const QString onArc = placed.isEmpty() ? DataText::none() : (item["area"].toString() == "simulated" ? "simulated/" : "") + placed;
                recentTable->setItem(row, 4, cell(onArc, placed.isEmpty() ? QString() : pathOnArc(item)));
                if (!keep.isEmpty() && item["upload_id"].toString() == keep) keptRow = row;
            }
            // The upload this window works with is selected; otherwise the newest.
            if (keptRow < 0 && !uploads.isEmpty()) keptRow = 0;
            if (keptRow >= 0) recentTable->selectRow(keptRow);
        }
        if (keptRow >= 0 && uploads.at(keptRow).toObject()["upload_id"].toString() == wantSelected) wantSelected.clear();
        recentTable->emptyText = listLoaded ? QString("No uploads yet.") : QString();
        recentTable->fitColumns();
        recentTable->viewport()->update();
        uploadSelected();
    }
    static QTableWidgetItem* cell(const QString& text, const QString& tip = QString()) {
        auto* item = new QTableWidgetItem(text);
        item->setToolTip(tip.isEmpty() ? text : tip);
        return item;
    }
    void uploadSelected() {
        const QJsonObject item = selectedUpload();
        const QString id = item["upload_id"].toString();
        // The history is asked for again when the upload has moved on since it was read.
        const QString key = id + "|" + item["updated_utc"].toString();
        if (key != statusKey) {
            statusKey = key;
            if (id != statusShown["upload_id"].toString()) statusShown = QJsonObject();
            wantStatus(id);
        }
        showDetails();
        showAll();
    }

    void showSessions(const QJsonObject& answer) {
        sessionsLoaded = true;
        sessions = answer["items"].toArray();
        // arc-sessions lists the newest sessionsLimit; "total" counts them all.
        sessionsTotal = std::max(int(sessions.size()), answer["total"].toInt(int(sessions.size())));
        sessionSettings = answer["settings"].toObject();
        applyDefaults(sessionSettings["demo"], sessionSettings["transport"]);
        fillSessionBox();
        // A session that ended a moment ago is read again once it has been quiet long enough.
        int later = 0;
        for (const QJsonValue& value : sessions) {
            const QJsonObject session = value.toObject();
            if (session["why_not"].toObject()["reason"].toString() == "settling" && session["retry_after_seconds"].toInt() > 0)
                later = later == 0 ? session["retry_after_seconds"].toInt() : std::min(later, session["retry_after_seconds"].toInt());
        }
        if (later > 0 && !sessionsLater.isActive()) sessionsLater.start(std::min(later, 600) * 1000 + 500);
        showAll();
    }
    // The starting values of Demo and Send with: the settings' defaults, once.
    void applyDefaults(const QJsonValue& demoDefault, const QJsonValue& transportDefault) {
        if (defaultsApplied || !demoDefault.isBool()) return;
        defaultsApplied = true;
        demoBox->setChecked(demoDefault.toBool());
        (transportDefault.toString() == "ssh" ? sshRadio : globusRadio)->setChecked(true);
    }
    // The newest session that may go in this mode, else the newest; one the person chose stays chosen.
    int preselected() const {
        for (int index = 0; index < sessions.size(); ++index)
            if (mayGo(sessions.at(index).toObject())) return index;
        return sessions.isEmpty() ? -1 : 0;
    }
    // The last entry of the Session box when arc-sessions has more sessions than it listed: choosing it
    // lists kSessionsStep more (older ones). It is no session.
    bool isMoreEntry(int index) const { return sessionsTotal > sessions.size() && index == int(sessions.size()) && index > 0; }
    void fillSessionBox() {
        const QString keep = selectedSession()["folder"].toString();
        QStringList folders;
        for (const QJsonValue& value : sessions) folders << value.toObject()["folder"].toString();
        QStringList entries = folders;
        if (sessionsTotal > folders.size() && !folders.isEmpty()) entries << kMoreSessions.arg(folders.size()).arg(sessionsTotal);
        filling = true;
        if (entries != shownFolders) {
            sessionBox->clear();
            if (folders.isEmpty()) sessionBox->addItem("No scan session yet");
            for (const QString& folder : folders) sessionBox->addItem(folder, folder);
            if (entries.size() > folders.size()) {
                sessionBox->addItem(entries.last());
                sessionBox->setItemData(int(folders.size()), QString("Only the newest %1 sessions are listed. Choose this to list older ones too.")
                                                                 .arg(folders.size()), Qt::ToolTipRole);
            }
            shownFolders = entries;
            int index = chosenByHand ? folders.indexOf(keep) : -1;
            if (index < 0) {
                chosenByHand = false;
                index = preselected();
            }
            sessionBox->setCurrentIndex(std::max(index, 0));
        } else if (!chosenByHand) {
            sessionBox->setCurrentIndex(std::max(preselected(), 0));
        }
        filling = false;
        nameSessions();
    }
    // Each item says what the session is and whether it may go: the folder, the sample, "13 spectra", how it
    // ended, then whether it is simulated, on ARC or in the demo folder, or not ready, joined by middle dots.
    // A folder the data layer did not count (ArcText::notRead) has no count and no end: arc-sessions says 0.
    void nameSessions() {
        for (int index = 0; index < sessions.size() && index < sessionBox->count(); ++index) {
            const QJsonObject session = sessions.at(index).toObject();
            const bool read = !ArcText::notRead(session["why_not"].toObject());
            QStringList parts{session["folder"].toString(), DataText::sample(session)};
            if (read) parts << ArcText::count(session["points"].toInt(), "spectrum", "spectra");
            if (read && !session["end_text"].toString().isEmpty()) parts << session["end_text"].toString();
            QString text = uiJoin(parts);
            if (session["kind"].toString() == "simulated") text += QStringLiteral(" \u00B7 simulated");
            else if (session["kind"].toString() == "unknown") text += QStringLiteral(" \u00B7 simulation not recorded");
            const QJsonObject uploaded = session["uploads"].toObject();
            const QString live = displayState(uploaded["live"].toObject()), inDemo = displayState(uploaded["demo"].toObject());
            if (session["why_not"].isObject()) text += QStringLiteral(" \u00B7 not ready");
            else if (live == "verified") text += QStringLiteral(" \u00B7 on ARC");
            else if (live == "check_failed") text += QStringLiteral(" \u00B7 on ARC, last check failed");
            else if (inDemo == "verified") text += QStringLiteral(" \u00B7 in demo folder");
            else if (inDemo == "check_failed") text += QStringLiteral(" \u00B7 in demo folder, last check failed");
            const QJsonObject why = whyNot(session, index == sessionBox->currentIndex() && declared());
            const QString path = demo() ? session["demo"].toObject()["demo_path"].toString() : session["live"].toObject()["cluster_path"].toString();
            sessionBox->setItemText(index, text);
            sessionBox->setItemData(index, why.isEmpty() ? path : ArcText::sentence(why), Qt::ToolTipRole);
        }
        sessionBox->setToolTip(sessionBox->currentData(Qt::ToolTipRole).toString());
    }
    void sessionChosen(int index) {
        if (isMoreEntry(index)) {
            // Back to the session shown before; the list grows by the older sessions.
            filling = true;
            sessionBox->setCurrentIndex(std::clamp(shownSession, 0, std::max(0, int(sessions.size()) - 1)));
            filling = false;
            sessionsLimit = std::max(sessionsLimit, int(sessions.size())) + kSessionsStep;
            loadSessions();
            showAll();
            return;
        }
        chosenByHand = true;
        forget();
        declareBox->setChecked(false);
        showAll();
    }
    void modeChosen() {
        forget();
        if (!chosenByHand && !sessions.isEmpty()) {
            filling = true;
            sessionBox->setCurrentIndex(std::max(preselected(), 0));
            filling = false;
        }
        showAll();
    }

    // The person chose another session, transport or mode: the status line speaks of that choice now.
    // The upload it followed stays in Recent uploads.
    void forget() {
        trackedUpload.clear();
        trackedKey.clear();
        trackedLine.clear();
        trackedCheck.clear();
        trackedUpdated.clear();
        actionPending = false;
    }
    // The upload this window started or acted on: the status line follows it from now on. `before`: the
    // upload as it was before the action; a check newer than its newest one is what the action found.
    void follow(const QJsonObject& upload, const QJsonObject& before = QJsonObject()) {
        if (upload.isEmpty()) return;
        trackedUpload = upload["upload_id"].toString();
        trackedKey = itemKey(upload);
        trackedCheck = (before.isEmpty() ? upload : before)["last_check"].toObject()["utc"].toString();
        trackedUpdated = upload["updated_utc"].toString();
        actionPending = false;
        wantSelected = trackedUpload;
        if (!uploadById(trackedUpload).isEmpty()) showUploadRows();
    }
    // The words of the reply to an action (upload, retry, check, stop, keep as new copy). While the program
    // the action started works (pending), they are about that work: Info. An upload the action left at rest
    // (stopped at once) is shown in its own tone; a failed one in the danger tone.
    void showStarted(const QJsonObject& upload, const QString& message, bool pending) {
        actionPending = pending;
        if (message.isEmpty()) { setLine(itemLine(upload), ArcText::tone(upload)); return; }
        const QString state = displayState(upload);
        const UiTone tone = state == "failed" ? UiTone::Danger : pending || ArcText::working(state) ? UiTone::Info : ArcText::tone(upload);
        setLine(linePrefix(upload["demo"].toBool(), QString()) + ": " + message, tone);
    }
    // arc-upload found an upload of this session that is not finished. One that is worked on is followed
    // as it goes on; one that waits for the person (a decision, Check status, Retry same request) says what
    // it waits for, in its own tone: its line then changes when the upload does.
    void showExisting(const QJsonObject& upload) {
        follow(upload);
        const QString started = DataText::localTime(upload["created_utc"].toString(), "HH:mm");
        if (ArcText::working(displayState(upload)))
            setLine(QString("An upload of this session is already under way (started %1). It is selected under Recent uploads.").arg(started),
                    UiTone::Info);
        else
            setLine(QString("An upload of this session waits for you (started %1): %2").arg(started, upload["message"].toString()),
                    ArcText::tone(upload));
        listUploads();
    }
    void setLine(const QString& text, UiTone tone) {
        trackedLine = text;
        trackedTone = tone;
        showLine();
    }
    // What the status line says: the upload of this window while there is one, else what Upload to ARC
    // would do now, or why it cannot.
    void showLine() {
        if (absent) { statusLabel->setText(kNotAvailable, UiTone::Warn); return; }
        if (!trackedLine.isEmpty()) { statusLabel->setText(trackedLine, trackedTone); return; }
        if (!listProblem.isEmpty()) { statusLabel->setText(listProblem, listProblemTone); return; }
        if (!sessionsProblem.isEmpty()) { statusLabel->setText(sessionsProblem, sessionsProblemTone); return; }
        const QString blocked = uploadBlocked(false);
        if (!blocked.isEmpty()) {
            const bool calm = blocked == kAsking || blocked == kAfterAcquisition || selectedSession().isEmpty();
            statusLabel->setText(blocked, calm ? UiTone::Neutral : UiTone::Warn);
            return;
        }
        const QJsonObject session = selectedSession();
        const QString files = ArcText::count(session["files"].toInt(), "file", "files"), bytes = ArcText::size(session["bytes"].toDouble());
        statusLabel->setText(demo() ? QString("Ready for a demo upload: %1 (%2) go to the demo folder on this Mac.").arg(files, bytes)
                                    : QString("Ready: %1 (%2) go to %3.").arg(files, bytes, session["live"].toObject()["cluster_path"].toString()),
                             UiTone::Neutral);
    }

    void styleBadge() {
        const UiTheme& theme = uiTheme();
        const bool inDemo = demo();
        badge->setText(inDemo ? "DEMO" : "ARC");
        const QColor fill = inDemo ? theme.warnFill : theme.infoFill, ink = inDemo ? theme.warnInk : theme.infoInk;
        badge->setStyleSheet(QString("QLabel { background: %1; color: %2; border: 1px solid %2; border-radius: 4px; padding: 1px 6px; "
                                     "font-size: 11px; font-weight: 700; }").arg(fill.name(), ink.name()));
    }
    // The ARC folder and the exact place the chosen session goes to, for this mode and transport.
    void showDestination() {
        const QJsonObject session = selectedSession();
        if (demo()) {
            folderLabel->setText(QDir::toNativeSeparators(QDir::cleanPath(demoRoot() + arcFolder())) + "   (demo, on this Mac)");
        } else {
            const QString globus = globusFolder();
            folderLabel->setText(arcFolder() + (globus.isEmpty() ? QString() : "   (Globus: " + globus + ")"));
        }
        folderLabel->setToolTip("Change it in ARC settings. Each session goes into a folder of its own inside it.");
        const QJsonObject live = session["live"].toObject();
        const QString cluster = live["cluster_path"].toString();
        // No path for this mode: the session may not go this way (the status line says why).
        const bool nowhere = demo() ? session["demo"].toObject()["demo_path"].toString().isEmpty() : cluster.isEmpty();
        if (session.isEmpty() || nowhere) {
            destinationLabel->setText(DataText::none());
            destinationLabel->setToolTip(QString());
        } else if (demo()) {
            const QString local = session["demo"].toObject()["demo_path"].toString(), root = demoRoot();
            const QString standsFor = local.startsWith(root + "/") ? local.mid(root.size()) : cluster;
            destinationLabel->setText(QDir::toNativeSeparators(local));
            destinationLabel->setToolTip(QString("Demo folder on this Mac. It stands in for %1.").arg(standsFor));
        } else if (transport() == "ssh") {
            const QString user = setting("ssh.username").toString(), host = setting("ssh.host").toString("datatransfer.arc.vt.edu");
            destinationLabel->setText((user.isEmpty() ? host : user + "@" + host) + ":" + cluster);
        } else {
            destinationLabel->setText(cluster);
            destinationLabel->setToolTip(QString("In ARC's Globus collection: %1. Both ways of sending reach the same folder.").arg(live["globus_path"].toString()));
        }
    }
    // The progress of the selected upload, from queued to placing: a count of files while Globus copies
    // (also in the demo), a percentage of the bytes while SSH does (also in the demo, whose status line
    // counts bytes too), otherwise busy without a number.
    void showProgress() {
        const QJsonObject item = selectedUpload();
        const QString state = displayState(item);
        const bool working = ArcText::working(state) && !item.isEmpty();
        progressBar->setVisible(working);
        if (!working) return;
        const QJsonObject progress = item["progress"].toObject();
        if (state != "transferring") {
            progressBar->setRange(0, 0);
            progressBar->setTextVisible(false);
        } else if (item["transport"].toString() == "ssh") {
            const double bytes = progress["bytes"].toDouble(), done = progress["bytes_done"].toDouble();
            const int percent = bytes > 0 ? std::clamp(int(std::floor(100.0 * done / bytes)), 0, 100) : 0;
            progressBar->setRange(0, 100);
            progressBar->setValue(percent);
            progressBar->setFormat(QString("%1%").arg(percent));
            progressBar->setTextVisible(true);
        } else {
            const int files = progress["files"].toInt(), done = progress["files_done"].toInt();
            progressBar->setRange(0, std::max(1, files));
            progressBar->setValue(std::clamp(done, 0, std::max(1, files)));
            progressBar->setFormat(QString("%1 of %2 files").arg(done).arg(files));
            progressBar->setTextVisible(true);
        }
    }
    void showControls() {
        const bool ready = usable();
        sessionBox->setEnabled(ready && !sessions.isEmpty());
        for (QAbstractButton* each : {static_cast<QAbstractButton*>(globusRadio), static_cast<QAbstractButton*>(sshRadio),
                                      static_cast<QAbstractButton*>(demoBox), static_cast<QAbstractButton*>(declareBox)})
            each->setEnabled(ready);
        declareBox->setVisible(ready && selectedSession()["kind"].toString() == "unknown");
        settingsButton->setEnabled(everShown && !absent);
        const QString blocked = uploadBlocked();
        uploadButton->setEnabled(blocked.isEmpty());
        uploadButton->setToolTip(!blocked.isEmpty() ? blocked
                                 : demo() ? QString("Copy the chosen session to the demo folder on this Mac.")
                                          : QString("Copy the chosen session to ARC. You confirm what goes where first."));
        // The buttons of the selected upload follow its "actions" and nothing else. What starts work
        // waits for the end of an acquisition; Stop upload does not.
        const QJsonObject item = selectedUpload();
        const bool demoUpload = item["demo"].toBool();
        const auto allowed = [&](const char* action) { return ready && !actionRunning && offers(item, action); };
        const struct { QPushButton* button; const char* action; QString tip; } work[] = {
            {checkButton, "check", demoUpload ? kCheckDemoTip : kCheckTip},
            {retryButton, "retry", demoUpload ? kRetryDemoTip : kRetryTip},
            {keepCopyButton, "keep_copy", QString("Put this upload beside the folder that already has its name, as %1_copy2.")
                                              .arg(item["session_folder"].toString("<folder>"))},
        };
        for (const auto& each : work) {
            each.button->setEnabled(allowed(each.action) && !acquisitionBusy);
            each.button->setToolTip(acquisitionBusy && allowed(each.action) ? kWorkAfterAcquisition : each.tip);
        }
        // Stop follows "actions" too: the data layer offers it whenever a program works on the upload, a
        // check of an upload that is on ARC, failed or stopped included (fix plan I3).
        stopButton->setEnabled(allowed("stop"));
        stopButton->setToolTip(checkMayCancel(item) ? (demoUpload ? kStopCheckCancelsDemoTip : kStopCheckCancelsTip)
                               : checkRuns(item)    ? kStopCheckTip
                                                    : (demoUpload ? kStopDemoTip : kStopTip));
        copyPathButton->setEnabled(allowed("copy_path"));
        finderButton->setEnabled(allowed("show_in_finder"));
    }
    void showAll() {
        styleBadge();
        nameSessions();
        showDestination();
        showProgress();
        showControls();
        showLine();
    }

    QString factsTable(const QList<QPair<QString, QString>>& rows) const {
        QString html;
        for (const auto& row : rows) {
            if (row.second.isEmpty()) continue;
            html += QString("<tr><td style='color:%1;padding-right:10px;white-space:nowrap;'>%2</td><td>%3</td></tr>")
                        .arg(uiTheme().textDim.name(), row.first, row.second);
        }
        return "<table cellspacing='0' cellpadding='0'>" + html + "</table>";
    }
    // The selected upload in full: what went, where, how it was checked, and its history.
    void showDetails() {
        const QJsonObject listed = selectedUpload();
        if (listed.isEmpty()) {
            details->setFacts(QString("<span style='color:%1;'>%2</span>").arg(uiTheme().textDim.name(),
                listLoaded && uploads.isEmpty() ? QString("No uploads yet. Choose a session and press Upload to ARC.")
                                                : QString("Select an upload to see its details and its history.")));
            return;
        }
        const bool fresh = statusShown["upload_id"].toString() == listed["upload_id"].toString();
        QJsonObject item = listed;
        if (fresh)
            for (auto it = statusShown.begin(); it != statusShown.end(); ++it) item.insert(it.key(), it.value());
        const auto escaped = [](const QString& text) { return text.toHtmlEscaped(); };
        const QJsonObject destination = item["destination"].toObject(), progress = item["progress"].toObject();
        const QJsonObject manifest = item["manifest"].toObject(), session = item["session"].toObject();
        const bool demoUpload = item["demo"].toBool(), ssh = item["transport"].toString() == "ssh";
        QStringList about{DataText::sample(item)};
        if (session["points"].isDouble()) about << ArcText::count(session["points"].toInt(), "spectrum", "spectra");
        if (!session["end"].toString().isEmpty()) about << ArcText::end(session["end"].toString());
        about << ArcText::kind(item["kind"].toString(session["kind"].toString()), session["kind_declared_by_uploader"].toBool());
        const int files = manifest["files"].isDouble() ? manifest["files"].toInt() : progress["files"].toInt();
        const double bytes = manifest["bytes"].isDouble() ? manifest["bytes"].toDouble() : progress["bytes"].toDouble();
        QStringList darks;
        for (const QJsonValue& value : manifest["darks"].toArray()) {
            const QJsonObject dark = value.toObject();
            darks << dark["file"].toString() + (dark["sent"].toBool(true) ? QString() : " (not sent: " + dark["reason"].toString("not paired") + ")");
        }
        QString to = demoUpload ? destination["local_final_path"].toString() : ssh && !destination["ssh_target"].toString().isEmpty()
                                                                                    ? destination["ssh_target"].toString() : pathOnArc(item);
        if (to.isEmpty()) to = destination["final_path"].toString();
        QStringList state{item["display_label"].toString(displayState(item))};
        if (!item["message"].toString().isEmpty()) state << item["message"].toString();
        if (!item["error"].toObject()["fix"].toString().isEmpty()) state << item["error"].toObject()["fix"].toString();
        QStringList check;
        const QString verified = ArcText::check(item["verification"].toObject(), demoUpload);
        const QJsonObject last = item["last_check"].toObject();
        const QString lastWhen = DataText::localTime(last["utc"].toString(), "yyyy-MM-dd HH:mm");
        if (!last.isEmpty() && ArcText::lastCheckFailed(item)) {
            // What the newest check found comes first: the copy on ARC is not, or no longer, as it was put there.
            if (ArcText::lastCheckNotLooked(item))
                check << "A check failed: " + item["message"].toString() << "newest check (" + lastWhen + "): " + ArcText::checkNote(last);
            else
                check << "Last check failed (" + lastWhen + "): " + last["message"].toString();
            if (!verified.isEmpty()) check << (demoUpload ? "when it was put in the demo folder: " : "when it was put on ARC: ") + verified;
        } else {
            if (!verified.isEmpty()) check << verified;
            // A check that could not look is a note with what to do; it says nothing about the copy (review REG-01).
            if (!last.isEmpty())
                check << (ArcText::lastCheckNotLooked(item) ? "newest check (" + lastWhen + "): " + ArcText::checkNote(last)
                                                            : "checked again " + lastWhen + ": " + last["message"].toString());
        }
        QStringList warnings, notes, history;
        for (const QJsonValue& value : item["warnings"].toArray()) warnings << ArcText::text(value);
        for (const QJsonValue& value : item["notes"].toArray()) notes << ArcText::text(value);
        const QJsonArray events = item["events"].toArray();
        for (int i = std::max(0, int(events.size()) - 30); i < events.size(); ++i) {
            const QJsonObject event = events.at(i).toObject();
            history << escaped(DataText::localTime(event["utc"].toString(), "HH:mm:ss") + "  " + event["text"].toString());
        }
        const QString leftover = item["leftover_on_arc"].toString();
        QList<QPair<QString, QString>> rows{
            {"Upload", escaped(uiJoin({item["upload_id"].toString(), ArcText::transport(item)}))},
            {"Session", escaped(item["session_folder"].toString() + " (" + about.join(", ") + ")")},
            {"Files", files > 0 ? escaped(ArcText::count(files, "file", "files") + ", " + ArcText::size(bytes)) : QString()},
            {"Dark", escaped(darks.join(", "))},
            {"To", escaped(to)},
            {"Globus path", ssh ? QString() : escaped(destination["globus_final_path"].toString())},
            {"Signed in as", escaped(item["identity"].toString())},
            {"State", escaped(state.join(" "))},
            {"Check", escaped(check.join("; "))},
            {"Warnings", escaped(warnings.join(" "))},
            {"Notes", escaped(notes.join(" "))},
            // A demo upload's files are in the demo folder on this Mac (review COR-05).
            {demoUpload ? "Left in the demo folder" : "Left on ARC",
             leftover.isEmpty() ? QString()
                                : escaped(QString(demoUpload ? "Files copied so far are in %1; nothing was deleted." : "Files copied so far are in %1 on ARC; "
                                                                                                                     "nothing was deleted.").arg(leftover))},
            {"History", history.join("<br>")},
        };
        if (!fresh && !statusFailure.isEmpty() && statusWanted == listed["upload_id"].toString()) rows << qMakePair(QString("History"), escaped(statusFailure));
        details->setFacts(factsTable(rows));
    }

    DataLayerClient* dataLayer = nullptr;
    QTimer poll, sessionsLater, becomingIdle, hiddenPoll;
    QElapsedTimer listAnswered;           // since the last answer of arc-list: the next one is due pollInterval() after it
    QElapsedTimer actedAt;                // since this window's last action that started work
    bool shown = false, everShown = false, acquisitionBusy = false, absent = false;
    bool listLoaded = false, sessionsLoaded = false, defaultsApplied = false;
    bool listRunning = false, listAgain = false, sessionsRunning = false, sessionsAgain = false, sessionsDue = false;
    bool hiddenListRunning = false;       // arc-list --limit=1 of the hidden page, for the toggle's colour
    bool actionRunning = false;           // an arc command of a button or a dialog is on its way
    bool statusRunning = false, statusAgain = false;
    bool filling = false, chosenByHand = false;
    QJsonArray uploads, sessions;         // the items of arc-list and arc-sessions
    QJsonObject readiness, listSettings, sessionSettings;
    int active = 0, unfinished = -1, attention = 0;
    int sessionsLimit = 30, sessionsTotal = 0, shownSession = 0;   // arc-sessions --limit, its "total", the session shown
    QHash<QString, QString> lastStates;   // display state of each listed upload, to see one become final
    QStringList shownFolders;             // the entries of the Session box, in its order
    QString trackedUpload, trackedKey;    // the upload this window started or acted on last, and what was shown of it
    QString trackedCheck, trackedUpdated; // its newest check before the action, and its updated_utc in the action's reply
    bool actionPending = false;           // the program an action of this window started has not been seen to end
    QString trackedLine;                  // what the status line says about it, until the person chooses something else
    UiTone trackedTone = UiTone::Neutral;
    QString wantSelected;                 // an upload to select once it is listed
    QString listProblem, sessionsProblem;
    UiTone listProblemTone = UiTone::Danger, sessionsProblemTone = UiTone::Danger;
    QString statusWanted, statusKey, statusFailure;
    QJsonObject statusShown;              // arc-status of the selected upload
    QPointer<QDialog> actionDialog, settingsDialog, confirmation;

    QScrollArea *scroll = nullptr, *detailsScroll = nullptr;
    QWidget* content = nullptr;
    ArcLowerBox* lowerBox = nullptr;
    QLabel* badge = nullptr;
    ElidedLabel* intro = nullptr;
    QPushButton *settingsButton = nullptr, *uploadButton = nullptr, *stopButton = nullptr;
    QPushButton *checkButton = nullptr, *retryButton = nullptr, *keepCopyButton = nullptr, *copyPathButton = nullptr, *finderButton = nullptr;
    ArcSessionBox* sessionBox = nullptr;
    QRadioButton *globusRadio = nullptr, *sshRadio = nullptr;
    QCheckBox *demoBox = nullptr, *declareBox = nullptr;
    ElidedLabel *folderLabel = nullptr, *destinationLabel = nullptr, *statusLabel = nullptr;
    QProgressBar* progressBar = nullptr;
    ArcRecentTable* recentTable = nullptr;
    FactsText* details = nullptr;
};
