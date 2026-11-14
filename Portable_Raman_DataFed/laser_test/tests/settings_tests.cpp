// Operator and Location are remembered only by the controller as it is really built (without
// HANDSHAKE_TESTING), so this program compiles main.cpp that way. The settings store is redirected
// to a throw-away INI file in this program's own folder: the user's preferences are never read or
// written. Only the capture emulator next to this program is started; no SDK, USB or laser access.
#define main controller_main          // keep the production main() out of this program
#include "../main.cpp"
#undef main
#include <iostream>
#include <stdexcept>

static void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
static QStringList entries(const QString& dir) {
    return QDir(dir).entryList(QDir::AllEntries | QDir::Hidden | QDir::NoDotAndDotDot);
}
struct HandshakeTest {   // friend of MainWindow (see main.cpp)
    static void click(MainWindow& w, const QString& text) {
        for (auto* b : w.findChildren<QPushButton*>()) if (b->text() == text) { b->click(); return; }
        throw std::runtime_error("Button not found");
    }
    static bool fieldsEmpty(MainWindow& w) {
        return w.sampleIdEdit->text().isEmpty() && w.operatorEdit->text().isEmpty() &&
               w.locationEdit->text().isEmpty() && w.notesEdit->text().isEmpty();
    }
    // A harness that builds the window itself, as the end-to-end test does: main() has not named the organisation.
    static void withoutOrganisation(const QString& store) {
        check(QCoreApplication::organizationName().isEmpty() && !MainWindow::remembersOperatorFields(), "no organisation name yet");
        {
            MainWindow w;
            check(fieldsEmpty(w), "the controller starts with empty fields");
            w.laserOn = true;   // Tests only: bypass physical laser controls, never call an SDK.
            for (const QString& button : {QString("Start Z scan then 2D scan"), QString("1D Scan")}) {
                click(w, button);
                check(w.ramanOutput->toPlainText().endsWith("[Scan] Not started: fill in Sample ID, Operator, Location first.") &&
                      w.sessionId.isEmpty() && !w.scanActive && w.ramanProcess->state() == QProcess::NotRunning,
                      "a freshly started controller does not scan before the fields are filled in");
            }
            click(w, "Capture Spectrum");
            check(w.ramanOutput->toPlainText().endsWith("[Capture] Not started: fill in Sample ID, Operator, Location first.") &&
                  w.singleCaptureDir.isEmpty(), "nor does it capture a single spectrum");
            w.operatorEdit->setText("harness operator"); w.locationEdit->setText("harness place");
        }
        check(entries(store).isEmpty(), "nothing is stored without the organisation name");
        MainWindow w;
        check(fieldsEmpty(w), "and nothing is loaded");
    }
    // As in the controller started through main(), which names the organisation and the application.
    // The names here are this test's own: even an unredirected store could not be the real controller's.
    static void withOrganisation(const QString& store) {
        QCoreApplication::setOrganizationName("PortableRamanSettingsTest");
        QCoreApplication::setApplicationName("settings-tests");
        check(MainWindow::remembersOperatorFields(), "organisation named: fields are remembered");
        {
            MainWindow w;
            check(fieldsEmpty(w), "first start: nothing to remember yet");
            w.sampleIdEdit->setText("S-9"); w.operatorEdit->setText("  Operator Nine ");
            w.locationEdit->setText("Lab 9"); w.notesEdit->setText("a note");
        }
        check(!entries(store).isEmpty(), "stored in the redirected settings store");
        {
            MainWindow w;
            check(w.operatorEdit->text() == "Operator Nine" && w.locationEdit->text() == "Lab 9", "Operator and Location remembered, trimmed");
            check(w.sampleIdEdit->text().isEmpty() && w.notesEdit->text().isEmpty(), "Sample ID and Notes are not remembered");
            w.operatorEdit->setText("Operator Ten");
            w.locationEdit->clear();
        }
        MainWindow w;
        check(w.operatorEdit->text() == "Operator Ten" && w.locationEdit->text().isEmpty(), "the latest value is remembered, an emptied field too");
    }
};
int main(int argc, char** argv) {
    qputenv("QT_QPA_PLATFORM", "offscreen");
    qputenv("RAMAN_BACKEND_SIMULATE", "1");
    qputenv("RAMAN_MOTOR_SIMULATE", "1");
    QApplication app(argc, argv);
    const QString store = QCoreApplication::applicationDirPath() + "/settings_test_store";
    QDir(store).removeRecursively();
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, store);
    try {
        check(ramanHome() == QCoreApplication::applicationDirPath(), "must run next to the emulators (handshake_v2/ in this folder)");
        HandshakeTest::withoutOrganisation(store); std::cout << "PASS no organisation name / empty fields / guard / nothing stored or loaded\n";
        HandshakeTest::withOrganisation(store); std::cout << "PASS Operator and Location remembered / Sample ID and Notes not\n";
        return 0;
    } catch (const std::exception& ex) {
        std::cerr << "FAIL " << ex.what() << std::endl; return 1;
    }
}
