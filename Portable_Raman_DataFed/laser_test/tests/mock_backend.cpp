// File-protocol emulator only. No SDK imports, USB calls, or laser access.
#include "../acquisition_protocol.h"
#include "../hardware/SampleApl/capture_timing.h"
#include <QElapsedTimer>
#include <QCoreApplication>
#include <QDir>
#include <QThread>
#include <cmath>
#include <iostream>
#ifdef _WIN32
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif
static QString mockMode() {
    QFile settings("mock_mode.txt");
    return settings.open(QIODevice::ReadOnly) ? QString::fromUtf8(settings.readAll()).trimmed() : QString();
}
// Mode spectrum: a curve with a slope and a few peaks instead of the flat 0x0010 row, so that the
// window snapshots (ui_snapshot.cpp) show a chart that looks like one. The values are made up; the
// capture id shifts the heights a little, so that successive points differ.
static QString emulatedSpectrumPixel(int index, const QString& id) {
    static const int peaks[] = {330, 430, 690, 775, 810, 905, 1000};
    const double gain = 0.8 + (qHash(id) % 40) / 100.0;
    double value = 900.0 + 260.0 * std::exp(-index / 900.0);
    for (int peak : peaks) value += gain * (peak % 3 == 0 ? 1500.0 : 900.0) * std::exp(-std::pow((index - peak) / 7.0, 2));
    return QString::number(int(value), 16).toUpper().rightJustified(4, '0') + " ";
}
// The laser menu as the controller drives it: 6 = set enable, 4 = set power, each taking the next
// command as its parameter, and 5 = print the enable state the controller reads back. The state
// exists in this process only. laser_commands.txt records what arrived, for the tests.
// Mode laser_stays_off: the enable command is consumed but has no effect, so Laser ON is not confirmed.
struct EmulatedLaser {
    bool enabled = false;
    QString pendingSetter;
    void handle(const QString& input) {
        if (!pendingSetter.isEmpty()) {
            QFile received("laser_commands.txt");
            if (received.open(QIODevice::Append))
                received.write(((pendingSetter == "6" ? "ENABLE " : "POWER ") + input + "\n").toUtf8());
            if (pendingSetter == "6") enabled = input == "1" && mockMode() != "laser_stays_off";
            pendingSetter.clear();
        } else if (input == "6" || input == "4") {
            pendingSetter = input;
        } else if (input == "5") {
            std::cout << "LD Power Enable : 0x" << (enabled ? "00000001" : "00000000") << std::endl;
        }
    }
};
int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QDir().mkpath("capture_results");
    // The two lines the real backend prints once after device initialisation (data_layer/PROTOCOL.md
    // section 3), with values that cannot be mistaken for an instrument. MOCK_BACKEND_INFO=absent
    // emulates a backend that does not print them, =unavailable a failed module information call.
    const QByteArray info = qgetenv("MOCK_BACKEND_INFO");
    if (info != "absent") {
        std::cout << "CAPTURE_SETTINGS exposure_time_us=1000000 cycle_time_us=1100000" << std::endl;
        std::cout << (info == "unavailable" ? "MODULE_INFO unavailable"
            : "MODULE_INFO model=\"EMULATOR\" serial=\"EMU-0001\" unit=\"EMULATED UNIT 1\" firmware=\"0.00\"") << std::endl;
    }
    EmulatedLaser laser;
    for (;;) {
        QFile command("command.txt");
        if (!command.open(QIODevice::ReadOnly)) { QThread::msleep(5); continue; }
        const QString input = QString::fromUtf8(command.readAll()).trimmed(); command.close(); command.remove();
        AcquisitionProtocol::publish("command_completed.txt", "done");
        if (!input.startsWith("S ") && !input.startsWith("M ")) { laser.handle(input); continue; }
        const QString id = input.mid(2);
        const QString mode = mockMode();
        QFile calls("calls.txt"); calls.open(QIODevice::Append); calls.write((id + "\n").toUtf8()); calls.close();
        QThread::msleep(mode == "late" ? 2200 : 100);
        bool statusTimeout = false;
        if (mode.startsWith("not_ready")) {
            QElapsedTimer timer; timer.start();
            for (;;) {
                const bool ready = mode == "not_ready_then_ready" && timer.elapsed() >= 150;
                const auto decision = captureStatusDecision(ready ? 0 : 1, ready ? 1 : 0,
                    ready ? 0 : -1, 5, timer.elapsed(), 400);
                if (decision == CaptureStatusDecision::Ready) break;
                if (decision != CaptureStatusDecision::Wait) { statusTimeout = true; break; }
                QThread::msleep(20);
            }
        }
        QString result = "CAPTURE " + id;
        if (mode == "fail" || statusTimeout) result += " FAIL -1\n";
        else {
            result += " OK 2048\n";
            const int n = mode == "partial" ? 2047 : 2048;
            for (int i = 0; i < n; ++i) result += mode == "spectrum" ? emulatedSpectrumPixel(i, id) : QString("0010 ");
            result += "\n";
        }
        result += "END " + (mode == "wrong_id" ? "OLD_ID" : id) + "\n";
        if (input.startsWith("M ")) {
            std::cout << "MONITOR_FRAME " << result.replace('\n', ' ').toStdString() << std::endl;
        } else if (mode.startsWith("locked_")) {
            // locked_*: the result file exists but cannot be read for a while.
            const QString path = "capture_results/" + id + ".txt";
            const auto bytes = result.toUtf8();
            const int holdMs = mode == "locked_short" ? 60 : mode == "locked_two" ? 160 : 600;
#ifdef _WIN32
            // Windows: an exclusive share mode denies every reader while the handle is open.
            HANDLE handle = CreateFileW(reinterpret_cast<LPCWSTR>(path.utf16()), GENERIC_WRITE,
                0, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (handle == INVALID_HANDLE_VALUE) return 2;
            DWORD written = 0;
            WriteFile(handle, bytes.constData(), DWORD(bytes.size()), &written, nullptr);
            FlushFileBuffers(handle);
            QThread::msleep(holdMs);
            CloseHandle(handle);
#else
            // POSIX has no mandatory share locks: emulate the denied read with permissions
            // (PORT_SPEC.md section 2). The file is created with mode 000 so the controller
            // never sees a readable window; the descriptor returned by O_CREAT stays writable.
            const QByteArray native = QFile::encodeName(path);
            const int fd = ::open(native.constData(), O_WRONLY | O_CREAT | O_EXCL, 0);
            if (fd < 0) return 2;
            const char* data = bytes.constData();
            qsizetype remaining = bytes.size();
            while (remaining > 0) {
                const ssize_t n = ::write(fd, data, static_cast<size_t>(remaining));
                if (n < 0) { ::close(fd); return 2; }
                data += n; remaining -= n;
            }
            ::fsync(fd);
            ::fchmod(fd, 0);
            ::close(fd);
            QThread::msleep(holdMs);
            ::chmod(native.constData(), 0644);
#endif
        } else AcquisitionProtocol::publish("capture_results/" + id + ".txt", result.toUtf8());
    }
}
