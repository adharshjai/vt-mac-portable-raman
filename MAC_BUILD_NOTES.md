# Building and running the Portable Raman project on this Mac

Written 2026-10-27 after unpacking `Portable_Raman_full_project_20261027.zip` on an
Apple Silicon MacBook (macOS 27, Xcode 27.1, Qt 6.12.0 macOS kit in `~/Qt`).

## What builds on macOS

**The Qt controller (`laser_test/`) builds cleanly.** It uses only Qt Widgets and
Qt Charts, both present in the Qt 6.12 kit. A finished build is in
`build-macos-qt6.12/laser_test.app` (Debug, Ninja). Three compiler warnings about
ignored `QFile::open` return values are harmless.

To rebuild or work on it in Qt Creator:

1. Open `laser_test/CMakeLists.txt` in Qt Creator.
2. Choose the *Desktop Qt 6.12.0* kit and let it create a new build directory
   (do not reuse the Windows `build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug` folder).
3. Build. Or from a terminal:

```bash
~/Qt/Tools/CMake/CMake.app/Contents/bin/cmake -S laser_test -B build-macos-qt6.12 -G Ninja -DCMAKE_MAKE_PROGRAM=~/Qt/Tools/Ninja/ninja -DCMAKE_PREFIX_PATH=~/Qt/6.12.0/macos -DCMAKE_BUILD_TYPE=Debug && ~/Qt/Tools/CMake/CMake.app/Contents/bin/cmake --build build-macos-qt6.12
```

**The Python analysis code** (PCA-SVM, CNN, Transformer scripts and notebooks) is
plain Python and runs on macOS. The Anaconda Python at `/opt/anaconda3` already has
numpy, scipy, scikit-learn, matplotlib and torch. Every script still contains the
original author's absolute Windows paths (`C:\Users\Jeremy666\...`), which must be
changed before running, as the hand-off notes say.

## UPDATE: the backends now run on macOS too

Since this note was written, both hardware programs were ported: see `macos-native/README.md`
and build with `macos-native/build_macos.sh`. The section below describes the original
Windows-only state and is kept for reference.

## What did NOT run on macOS (original state)

The controller is only the front end. At runtime it launches two Windows programs
next to its own executable and talks to them through files and stdout:

| Backend | Location expected by the controller | Why it is Windows-only |
|---|---|---|
| `SampleApl.exe` (Hamamatsu capture backend, `laser_test/hardware/SampleApl`) | `<app dir>/handshake_v2/backend/SampleApl.exe` | C++/CLI on .NET Framework 4.7.2, calls Hamamatsu's `wrrusb2a.dll`, which needs the WinUSB driver |
| `RamanAutofocus.exe` (Thorlabs motor control, `RamanAutofocus/`) | `<app dir>/handshake_v2/motor/RamanAutofocus.exe` | Links the Thorlabs Kinesis `KCube.DCServo` DLLs (Windows only) |

So on the Mac the GUI opens, but Laser ON, scans, focus monitor and dark capture all
stop with "Backend is not running". Driving the real spectrometer and KDC101 motors
still needs Windows. The practical options are:

1. **Windows 11 Arm VM with USB passthrough** (the same route as for Hamamatsu's own
   RamSpecView; see `../C13560-0274 64bit/Mac-setup/README-Run-on-Mac.md`). Build the
   whole project there with Qt 6.6/6.12 MSVC, Visual Studio C++/CLI and Kinesis.
2. **Port the two backends.** The Hamamatsu side would need the USB protocol
   reimplemented on libusb; the Thorlabs side could use the open APT serial protocol
   (KDC101 shows up as a USB serial port). That is a real engineering project and
   needs the hardware on the bench to test.

The author's hard-coded Windows paths in `laser_test/main.cpp` (`kPythonScriptPath`,
`ramanDir`, `mappingLogPath`) and in `cnn_1.py` (`DATA_ROOT`) have been replaced by
paths next to the executable/script with `windows-vm/apply_path_fixes.py`; the originals
are kept as `main.cpp.orig` and `cnn_1.py.orig`. See `WINDOWS_VM_GUIDE.md` for the
Windows build. Note that on macOS `applicationDirPath()` points inside
the `.app` bundle (`laser_test.app/Contents/MacOS`), so `scan_sessions/`,
`dark_measurements/`, `laser_log.txt` and `controller.lock` would be created there.

## Hardware-free test suite

`laser_test/tests` (handshake_tests, motor_path_test, serialization_cli_test) compiles
the real controller against fake backends. It is not buildable on macOS as shipped:
`mock_backend.cpp` includes `<windows.h>` and the harness launches `.exe` emulators.
Porting the two small mocks would be a modest job if you want the tests on the Mac.

## Not translated

PNG figures and the prebuilt `.exe` files in the package may still contain Chinese
text in images or embedded strings; those cannot be edited and are rebuilt from the
(now English) sources anyway.
