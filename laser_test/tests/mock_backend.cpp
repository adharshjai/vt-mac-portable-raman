// File-protocol emulator only. No SDK imports, USB calls, or laser access.
#include "../acquisition_protocol.h"
#include "../hardware/SampleApl/capture_timing.h"
#include <QElapsedTimer>
#include <QCoreApplication>
#include <QDir>
#include <QThread>
#include <iostream>
#ifdef _WIN32
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif
int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    QDir().mkpath("capture_results");
    for (;;) {
        QFile command("command.txt");
        if (!command.open(QIODevice::ReadOnly)) { QThread::msleep(5); continue; }
        const QString input = QString::fromUtf8(command.readAll()).trimmed(); command.close(); command.remove();
        AcquisitionProtocol::publish("command_completed.txt", "done");
        if (!input.startsWith("S ") && !input.startsWith("M ")) continue;
        const QString id = input.mid(2);
        QFile settings("mock_mode.txt"); settings.open(QIODevice::ReadOnly);
        const QString mode = QString::fromUtf8(settings.readAll()).trimmed(); settings.close();
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
            for (int i = 0; i < n; ++i) result += "0010 ";
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
