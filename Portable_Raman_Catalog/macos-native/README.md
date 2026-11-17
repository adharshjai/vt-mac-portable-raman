# Portable Raman controller, Mac-native

This folder makes the whole system run on macOS (Apple Silicon) without Windows: the Qt
controller, a Mac-native replacement for the Thorlabs motor program, and a Mac-native capture
backend that runs Hamamatsu's own USB library on top of libusb. Both hardware pieces also have a
simulation mode, so the complete pipeline can be exercised on a laptop with nothing plugged in.

Design document: `PORT_SPEC.md`. Everything below runs from this edition's folder,
`Portable_Raman_Catalog/`. The original project around it has a `macos-native/build_macos.sh` of
its own, which builds the original, not this edition.

## Build and deploy

```bash
./macos-native/build_macos.sh          # builds everything and assembles deploy-macos/
./macos-native/build_macos.sh --test   # ... then runs every test suite of the edition (about 15 minutes)
```

When `.venv/` is missing, the build first makes it with `data_layer/setup_env.sh`, which downloads
the data layer's packages from PyPI, the Globus SDK for uploads to ARC included. The build never
installs into an existing `.venv/`: when the Globus SDK is not in it, the build says so (only live
Globus uploads to ARC need it) and goes on; run `./data_layer/setup_env.sh` once from this edition's
folder to add it. When an upload to ARC is running, the build says so before it replaces the data
layer; the upload keeps the code it loaded when it started.

`--test` runs each suite with its output in `.build/test-logs/<suite>.log`, prints PASS or FAIL per
suite and exits 1 if any failed: the data layer, the Studio page, its embedded data and its upload
strip (`studio_arc`), the motor program, the backend (protocol, safety stop, virtual sample), the
simulated hardware check, the controller's handshake, settings and window tests and the tests of
the window's ARC page (`arc_ui_tests`), the simulated runner and the end-to-end run. It sets
`RAMAN_ARC_TEST_GUARD=1`, so no test of the uploads to ARC can sign in to Globus or run `ssh`,
`open` or `caffeinate`.
The hardware check runs in a deploy folder of its own, `.build/test-logs/hardware_check_deploy/`
(copies of `handshake_v2/` and `virtual_sample/`), so that its `scan_sessions/_hwcheck_<time>/`
folder is never left in `deploy-macos/`.
The self-tests that open the USB stack and the vendor library (`shim_selftest`, `SampleApl
--selftest`) are left out while the instruments are attached, and also with
`RAMAN_SKIP_DEVICE_SELFTESTS=1`.

Prerequisites already installed on this Mac: Qt 6.12 (`~/Qt`), Xcode command line tools, libusb
(Homebrew), .NET SDK 9 (`~/.dotnet`), in the Anaconda Python: pyserial, pyusb, pyftdi; the
edition's `.venv` (`data_layer/setup_env.sh`) and Node for the Studio page's tests.

Result: `deploy-macos/` holds the flat layout the controller expects:

```
deploy-macos/laser_test                  the controller (plain executable)
deploy-macos/run_simulated.sh            start it with both backends simulated
deploy-macos/run_hardware.sh             start it against the real instruments
deploy-macos/raman_sim_runner            the controller without a window, always simulated (for the Studio)
deploy-macos/studio.sh                   start the Simulation Studio for this folder
deploy-macos/handshake_v2/backend/       SampleApl (launcher) + SampleApl.dll + wrrusb2a.dll + libwinusb_shim.dylib + System.Windows.Forms.dll
deploy-macos/handshake_v2/motor/         RamanAutofocus (launcher) + raman_autofocus.py + apt_kdc101.py + motor_config.json
deploy-macos/data_layer/                 raman_catalog/ (data layer, uploads to ARC included), requirements.txt, requirements-mac.txt
deploy-macos/models/, virtual_sample/, studio/index.html, catalog_python.txt, raman_python.txt
deploy-macos/cnn_1.py, Portable_Raman_database/, _CNN_2class_r6g_mb/, dark_measurements/, scan_sessions/
```

`dark_measurements/` starts empty: the darks of the original hand-off folder are not copied. They
have no `.json` beside them, so they count as real, and a real session could be paired with one
of them months later (data_layer/PROTOCOL.md sections 1 and 2.5). A dark in `deploy-macos/` with
the name and the content of a hand-off dark and no `.json` (a copy an earlier build made, or a link
to a hand-off dark) is removed by the build, which then names the hand-off folder that keeps them.
It removes nothing when both dark folders are one, and never a file that a hand-off link points to.

## Running

- Simulation: `deploy-macos/run_simulated.sh`. Laser ON, Capture Dark, Monitor, 1D and 2D scans all
  work; motors move instantly. The spectra follow the virtual sample in `virtual_sample/`
  (data_layer/PROTOCOL.md section 4); without it they are synthetic (baseline plus a few Gaussian
  peaks). Capture a dark in simulation before a simulated scan: the scan is paired with the newest
  simulated dark, and a real dark is never used for it (Capture Dark writes `dark_<stamp>.json`
  beside each dark to say which kind it is; data_layer/PROTOCOL.md section 2.5).
- Simulation Studio: `deploy-macos/studio.sh` serves the Studio page on 127.0.0.1 and opens it; its
  Play button starts `raman_sim_runner`, which runs Laser ON, Capture Dark, the scan and Laser OFF
  with both helper programs simulated. It does not run while the controller window is open.
- Hardware: `deploy-macos/run_hardware.sh` with the spectrometer and the three KDC101 controllers
  plugged into the Mac. No drivers are needed: the Hamamatsu module is a vendor-specific USB
  device that libusb claims directly, and the KDC101s are reached through libusb with pyftdi
  because their FTDI chips use Thorlabs' custom product id (0403:FAF0) that macOS's own FTDI
  driver does not bind. If a controller is not found, `python3 -c "from pyftdi.ftdi import Ftdi;
  Ftdi.add_custom_product(0x0403,0xFAF0); Ftdi.show_devices()"` lists what pyftdi sees.
- The analysis button runs `cnn_1.py` with the Python named in `RAMAN_PYTHON` (the run scripts set
  it to the Anaconda interpreter, which has torch and scikit-learn).

Scan sessions, dark measurements and logs land inside `deploy-macos/`, next to the executable, as
on Windows. While it is open, the window also holds `../deploy-macos/controller.lock` of the
original project (README.md, "Two locks"); it keeps Operator and Location in the macOS
preferences; and the Open Simulation Studio button writes its server's output to the temporary
folder.

## Safety stop

If the controller goes away without shutting its helper programs down, they stop on their own.

- **Capture backend.** It switches the laser off, reads the state back to confirm it, closes the
  spectrometer and exits. It reacts when the controller process disappears (crash, force quit, Qt
  Creator's Stop button), when it is asked to terminate (SIGTERM, SIGINT, SIGHUP, SIGQUIT), and
  when an exception would end it. SIGTERM is also what the controller sends when its window is
  closed, so on macOS closing the window with the laser still on switches the laser off. If the
  backend is stuck inside a device call, a second thread switches the laser off after one second.
- **Motor program.** It stops the move in progress, commands nothing further and closes the three
  controllers. It reacts to the same events, and it opens nothing if the controller is already gone
  when it starts.

What the backend did is written to `watchdog.log` in its working folder under
`scan_sessions/_backend_*`. The line to look for is
`laser disable rc=0; readback rc=0, LD Power Enable : 0x00000000`. Exit code 5 or 6 means the laser
was confirmed off; 7 means it could not be confirmed, and the log then says
`THE LASER IS NOT CONFIRMED OFF`. If the log is missing or lacks the confirmed line after an
abnormal end, treat the laser as on.

The laser stays as it was, and only unplugging the spectrometer's USB cable switches it off (the
module is USB powered), when:

- the backend itself is killed with `kill -9`, or crashes outside managed code;
- the controller hangs or is suspended without dying (beachball, a debugger breakpoint, Ctrl-Z):
  it is then not gone, and nothing is triggered;
- the spectrometer no longer answers on USB.

For the motor program, `kill -9` leaves a move in progress running to its commanded target; the
controller only does that if the program has not ended 8 s after a cancel.

Verified: in simulation by `backend/tests/test_backend_watchdog.py` and the safety-stop tests in
`motor/tests/test_motor.py`. On the real module, with the laser off throughout, the first version
of the stop was checked on 2026-11-11 (disable accepted, state read back as disabled, device
released). Not verified on hardware: an actual on-to-off transition after a kill, the last-resort
path, and the reworked version. The Windows build does not have this safety stop. Source:
`backend/SampleApl/Watchdog.cs`, `motor/scan_handshake.py`.

## How the Hamamatsu backend works without Windows

Hamamatsu ships the spectrometer protocol inside `wrrusb2a.dll`, a pure .NET library whose only
Windows dependencies are the WinUSB/SetupAPI system calls it makes. The port keeps that library
unmodified and gives it what it expects:

- `backend/lib/wrrusb2a.dll` is a byte-identical copy except for the 2-byte PE "machine" field,
  re-tagged from x64 to ARM64 so the arm64 .NET runtime accepts it (`tools/retag_assembly.py`;
  the IL and metadata are untouched).
- `backend/stubs/System.Windows.Forms` is a tiny stand-in for the .NET Framework assembly the
  library references for two helper windows that are never shown. It carries the same identity
  (name, version 2.0.0.0, ECMA public key token) so the reference binds.
- `backend/shim/libwinusb_shim.dylib` implements the 26 Windows functions the library imports
  (WinUsb_*, SetupDi*, CreateFile/CloseHandle, QueryPerformanceCounter, GetPrivateProfileString,
  device-notification registration) on libusb. `backend/SampleApl` is the C# port of the original
  C++/CLI backend's file protocol and single-spectrum capture logic, and redirects the library's
  imports to the shim.
- No code of the library was decompiled; only its public API and its import table (plain
  metadata, see `tools/wrrusb2a_api.txt`) were used.
- The shim links libusb statically so the deploy folder is self-contained (libusb is LGPL-2.1;
  for an internal lab tool that is fine, but keep the libusb source/notice if you ever
  distribute the folder). `WRRUSB_SHIM_DEBUG=1` traces every call the library makes;
  `WRRUSB_SHIM_FAKE_DEVICES=0661:294c@1-5` fakes an enumeration entry for testing.

## What is verified and what is not

Last full run of the original port (`build_macos.sh --test`, 2026-10-27): shim self-test 98/98,
motor 18/18, backend 8/8 including the real-library self-test, controller suite all PASS,
end-to-end simulated run PASS (laser, dark, monitor, 1D 13 points, 2D 124 points, motor exit 0
both times).

Last full run of the catalog edition (`RAMAN_SKIP_DEVICE_SELFTESTS=1 build_macos.sh --test`,
2026-11-14, after the second round of review fixes, nothing attached): every suite PASS, in 692 s.
Data layer 488 tests, Studio embedded data, engine (32) and page, motor 36, backend protocol 10
(without its self-test class), safety stop 15, virtual sample 18 and 16, simulated hardware check
(in its own deploy folder under `.build/test-logs/`), handshake_tests 47, settings_tests 3,
ui_tests 27, simulated runner 7, end-to-end 1 (laser, dark and its `.json`, monitor, 1D 13 points,
2D 124 points, both sessions naming the dark the run took). The device self-tests were not run in
that session.

Last full run with the uploads to ARC added (2026-11-15, before their review, in a scratch copy of
this folder with a Python environment that has the Globus SDK): every suite PASS, in 792 s. Data
layer 967 tests, Studio upload strip 28, arc_ui_tests 12 PASS lines, the other suites as above
(motor 36 with 1 skipped).

Last full run after the review of the uploads to ARC and the fourth round of its fixes (2026-11-16,
in a scratch copy of this folder as above): every suite PASS, in 921 s. Data layer 1162 tests,
Studio upload strip 37, arc_ui_tests 25 PASS lines, the other suites as above. Every run, and the
counts of each, are in `VERIFICATION.json` at the top of this edition.

These three runs were made before 2026-11-17, when the connection to an external catalog service
and the tests of that connection were taken out of this edition: the data layer has fewer tests
since. What was run after that change is in `VERIFICATION.json` under
`after_removing_the_catalog_service`.

Verified for the original port on 2026-10-27 (above): the controller's own hardware-free test
suite; the motor program's unit tests (sequence identical to the C++ original, 124 points in 2D,
13 in 1D, handshake, cancel, heartbeat); the backend's file-protocol test in simulation; the
backend self-test that loads the real Hamamatsu library through the shim; and the end-to-end
simulated run of the real controller through laser-on, dark, monitor, a 1D scan and a full 2D scan.
For this edition only the simulated suites ran; the shim and backend self-tests were not run.

Not verified, because it needs the instruments on the bench: real USB traffic to the spectrometer
through the shim (endpoint types, timeouts, descriptor details), real motion of the KDC101s
through pyftdi (homing, travel, timing), and the 1D travel-limit check, which on Windows came
from the Kinesis settings database and here comes from `motor_config.json` (defaults assume
25 mm actuators). Treat the first hardware session as a supervised test, exactly as the hand-off
notes demand: laser safety, clearance for the 20 mm Z move, Stop does not switch the laser off.
