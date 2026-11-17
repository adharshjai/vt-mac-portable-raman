#pragma once
// What the header and the "last event" line show, worked out from a copy of the controller's state
// and from the lines the controller writes to its two logs. Everything here is a pure function of
// its arguments: the display cannot influence an acquisition, and it can be tested without a window.

#include "window_theme.h"

#include <QFileInfo>
#include <QRegularExpression>
#include <QString>
#include <QStringList>

#include <optional>

struct PillState {
    UiTone tone = UiTone::Neutral;
    QString text;
    QString tip;
    bool prominent = false;
};

// Parts of one status are separated by a middle dot. Written as UTF-16 so that the result does not
// depend on the character set a compiler assumes for narrow strings.
inline QString uiJoin(const QStringList& parts) { return parts.join(QStringLiteral(" \u00B7 ")); }

// The points of a line scan: 0, 2, ... 24 mm. The motor program owns the plan; the controller has
// always shown this number ("/13" in the chart title, the tool tip of 1D Scan).
static const int kLineScanPointCount = 13;

// The controller's state as far as the display needs it. MainWindow fills it from its members.
struct ControllerView {
    bool backendRunning = false, captureFault = false;
    bool laserOn = false, laserUncertain = false, laserCommandBusy = false;
    QString powerInForce;                  // LOW / MEDIUM / HIGH; empty while no Laser ON has been confirmed
    bool laserReadBack = false;            // a Laser ON or OFF has been confirmed by the read-back since the start
    bool scanActive = false, paused = false;
    QString scanMode;                      // 2d, 1d-x or 1d-y
    quint64 savedPoints = 0;
    enum class ScanEnd { None, Finished, Stopped, Failed };
    ScanEnd lastScanEnd = ScanEnd::None;   // how the latest scan of this session ended
};

inline PillState backendPillState(const ControllerView& view) {
    if (!view.backendRunning)
        return {UiTone::Danger, "stopped", "The capture backend is not running. Close the controller and start it again.", true};
    if (view.captureFault)
        return {UiTone::Danger, "fault", "A capture failed or timed out. Confirm Laser OFF, then close and restart the controller before a new run.", true};
    return {UiTone::Ok, "running", "The capture backend is running."};
}

inline PillState laserPillState(const ControllerView& view) {
    // While a command is on its way the laser may already be on although nothing is confirmed yet.
    if (view.laserCommandBusy)
        return {UiTone::Warn, "switching...", "A laser command is on its way; the state is not confirmed yet.", true};
    if (view.laserUncertain)
        return {UiTone::Danger, "UNCONFIRMED", "The laser did not confirm its state. Treat it as ON until Laser OFF is confirmed.", true};
    if (view.laserOn)
        return {UiTone::Hazard, view.powerInForce.isEmpty() ? QString("ON") : uiJoin({"ON", view.powerInForce}),
                "Laser ON, confirmed by the read-back.", true};
    // Nothing reads the laser when the controller starts, and after an abnormal end the laser stays as
    // it was. OFF is said only once a read-back has said so. A Laser ON confirmed earlier counts as well:
    // every command since then was read back too, or the state shows as UNCONFIRMED above.
    if (!view.laserReadBack && view.powerInForce.isEmpty())
        return {UiTone::Warn, "not read",
                "Not read back since the controller started; after an abnormal end treat the laser as on "
                "(macos-native/README.md, Safety stop).", true};
    return {UiTone::Neutral, "OFF", "Laser OFF, confirmed by the read-back."};
}

inline PillState scanPillState(const ControllerView& view) {
    // Only a line scan has a size the controller knows; the 2D plan is the motor program's.
    const bool line = view.scanMode != "2d";
    const QString count = line ? QString("%1 of %2").arg(view.savedPoints).arg(kLineScanPointCount) : QString("%1 saved").arg(view.savedPoints);
    const auto with = [&](const QString& word, const QString& joiner) { return line ? word + joiner + count : uiJoin({word, count}); };
    if (view.scanActive && view.paused)
        return {UiTone::Warn, with("paused", " "), "The scan is paused: the motor waits. Resume continues with the same point.", true};
    if (view.scanActive)
        return {UiTone::Info, with("running", " "), "A scan is running. The number counts the points saved so far."};
    switch (view.lastScanEnd) {
    case ControllerView::ScanEnd::Failed:
        return {UiTone::Danger, with("FAILED", " at "), "The latest scan was aborted. The reason is in the scan log and in failure.txt of its folder.", true};
    case ControllerView::ScanEnd::Stopped:
        return {UiTone::Warn, with("stopped", " at "), "The latest scan was stopped before its end. The points saved until then are kept."};
    case ControllerView::ScanEnd::Finished:
        return {UiTone::Ok, uiJoin({"idle", QString("last scan saved %1").arg(view.savedPoints)}), "The latest scan ran to its end."};
    case ControllerView::ScanEnd::None:
        break;
    }
    return {UiTone::Neutral, "idle", "No scan has run since the controller started."};
}

// Whether a helper program simulates, as far as the header may claim it. The header must never say
// SIMULATION while an instrument is driven, and it is decided once, while the motor program reads
// its motor_config.json again at every scan; so only the variable set to 1 counts as simulated and
// every value but 0 and 1 is "unclear". The saved "simulated" label applies each helper's complete
// rule when a capture starts (main.cpp, data_layer/PROTOCOL.md section 2.2).
enum class SimulationUse { Real, Simulated, Unclear };
inline SimulationUse simulationUse(const char* variable) {
    if (!qEnvironmentVariableIsSet(variable)) return SimulationUse::Real;
    const QByteArray value = qgetenv(variable);
    return value == "1" ? SimulationUse::Simulated : value == "0" ? SimulationUse::Real : SimulationUse::Unclear;
}

// The badge next to the program name; an empty text means none. With only one helper simulated the
// badge leads with what is real, because that is the part that can hurt.
inline PillState simulationBadge(SimulationUse backend, SimulationUse motor) {
    if (backend == SimulationUse::Unclear || motor == SimulationUse::Unclear)
        return {UiTone::Danger, "SIMULATION NOT CONFIRMED",
                "A simulation variable is set to a value the helper programs do not act on (they simulate for the value 1 only). "
                "Treat the instruments as live.", true};
    if (backend == SimulationUse::Simulated && motor == SimulationUse::Simulated)
        return {UiTone::Info, "SIMULATION", "Both helper programs run their simulators. No instrument is driven.", true};
    if (backend == SimulationUse::Simulated)
        return {UiTone::Danger, "MOTORS ARE REAL (capture simulated)",
                "Only the capture backend runs its simulator. The motor program is not simulated: the stages move.", true};
    if (motor == SimulationUse::Simulated)
        return {UiTone::Danger, "LASER IS REAL (motors simulated)",
                "Only the motor program runs its simulator. The capture backend is not simulated: the laser and the spectrometer are driven.", true};
    return {};
}

struct UiEvent {
    UiTone tone = UiTone::Neutral;
    QString text;
};

// What the controller knew when it wrote the line.
struct EventContext {
    QString scanMode;
    quint64 savedPoints = 0;
    bool scanCancelled = false;
};

// The event a log line stands for, if it is one. Both logs are read: the laser log (lines start with
// a time stamp) and the scan log. Lines the helper programs or the analysis script printed are detail
// for the logs, not events of the controller, and give nothing.
inline std::optional<UiEvent> eventFromLogLine(QString line, const EventContext& context) {
    static const QRegularExpression stamp("^\\[\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}\\.\\d{3}\\] ");
    static const QRegularExpression tagged("^\\[([^\\]]+)\\]\\s*(.*)$");
    line = line.trimmed();
    const auto stamped = stamp.match(line);
    if (stamped.hasMatch()) line = line.mid(stamped.capturedLength());
    const auto parts = tagged.match(line);
    if (!parts.hasMatch()) return std::nullopt;
    const QString tag = parts.captured(1), message = parts.captured(2).trimmed();
    if (QStringList{"Backend", "Backend ERROR", "Raman", "Raman ERROR", "Motor executable"}.contains(tag)) return std::nullopt;
    // The scan log reports the same capture one line later, with the folder.
    if (tag == "Single capture saved" || tag == "Single capture failed" || tag == "Scan failed") return std::nullopt;
    // A detail of a stop that has just been reported; it must not take the place of "the laser stays on".
    if (tag == "Capture discarded from scan") return std::nullopt;

    const auto fileName = [](const QString& path) { return QFileInfo(path.trimmed()).fileName(); };
    if (tag == "Point saved") {
        const QString point = QRegularExpression("point=(\\d+)").match(message).captured(1);
        return UiEvent{UiTone::Ok, context.scanMode == "2d" ? QString("Point %1 saved").arg(point)
                                                             : QString("Point %1 of %2 saved").arg(point).arg(kLineScanPointCount)};
    }
    if (tag == "Capture" && message.startsWith("Saved: ")) return UiEvent{UiTone::Ok, uiJoin({"Spectrum saved", fileName(message.mid(7))})};
    if (tag == "Dark" && message.startsWith("Saved: ")) return UiEvent{UiTone::Ok, uiJoin({"Dark spectrum saved", fileName(message.mid(7))})};
    if (tag == "Handshake v2" && message.startsWith("Data: ")) return UiEvent{UiTone::Info, uiJoin({"Scan started", fileName(message.mid(6))})};
    if (tag == "Laser" && message.startsWith("ON confirmed")) return UiEvent{UiTone::Hazard, "Laser ON confirmed"};
    if (tag == "Laser" && message.startsWith("OFF confirmed")) return UiEvent{UiTone::Ok, "Laser OFF confirmed"};
    if (tag == "Backend process started") return UiEvent{UiTone::Info, "Backend started"};
    if (tag == "Stop") return UiEvent{UiTone::Warn, message};
    // The reason can be a raw reply of the backend (capture id, status code) that goes on over several
    // lines; its first clause names the failure, and the whole of it is in the scan log and failure.txt.
    // What has to be read here is that an abort leaves the laser as it was.
    if (tag == "Scan aborted") {
        static const QRegularExpression clauseEnd("[:;]|\\.(\\s|$)");
        const QString reason = message.section(clauseEnd, 0, 0).trimmed();
        return UiEvent{UiTone::Danger, (reason.isEmpty() ? QString("Scan aborted") : "Scan aborted: " + reason) +
                                           ". Laser not switched off: press Laser OFF, restart the controller."};
    }
    if (tag == "Paused" && message.startsWith("Incoming STEP will be held")) return UiEvent{UiTone::Warn, "Scan paused: press Resume to continue."};
    if (tag == "Raman exited") {
        // After Stop or an abort the controller ended the motor program itself and has already said why.
        if (context.scanCancelled) return std::nullopt;
        const auto exit = QRegularExpression("code=(-?\\d+), status=(\\w+)").match(message);
        if (exit.captured(1) == "0" && exit.captured(2) == "Normal")
            return UiEvent{UiTone::Ok, uiJoin({"Scan finished", QString("%1 points saved").arg(context.savedPoints)})};
        return UiEvent{UiTone::Danger, QString("Motor program ended by itself (code %1, %2). The scan has stopped; see the scan log.")
                                           .arg(exit.captured(1), exit.captured(2).toLower())};
    }
    if (tag == "Analysis" && message.startsWith("Finished")) {
        const QString code = QRegularExpression("Code: (-?\\d+)").match(message).captured(1);
        return code == "0" ? UiEvent{UiTone::Ok, "Analysis finished"} : UiEvent{UiTone::Danger, QString("Analysis ended with code %1").arg(code)};
    }

    const QString text = message.isEmpty() ? tag : tag + ": " + message;
    static const QRegularExpression retrying("^Capture read$|No paused active scan", QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression bad("error|fail|abort|fault|unconfirmed|timed out|timeout|could not|cannot|crash",
                                        QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression caution("not started|stop|wait|requires|paused|warning|already|turn on|discarded|not running",
                                            QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression good("(?<!not )saved|confirmed|finished|continuing|recovered", QRegularExpression::CaseInsensitiveOption);
    if (retrying.match(tag).hasMatch() || retrying.match(message).hasMatch()) return UiEvent{UiTone::Warn, text};
    if (bad.match(text).hasMatch()) return UiEvent{UiTone::Danger, text};
    if (caution.match(text).hasMatch()) return UiEvent{UiTone::Warn, text};
    if (good.match(text).hasMatch()) return UiEvent{UiTone::Ok, text};
    return UiEvent{UiTone::Info, text};
}
