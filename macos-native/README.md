# Portable Raman controller, Mac-native

This folder makes the whole system run on macOS (Apple Silicon) without Windows: the Qt
controller, a Mac-native replacement for the Thorlabs motor program, and a Mac-native capture
backend that runs Hamamatsu's own USB library on top of libusb. Both hardware pieces also have a
simulation mode, so the complete pipeline can be exercised on a laptop with nothing plugged in.

Design document: `PORT_SPEC.md`. Everything below assumes the project root
`Portable_Raman_full_project/`.

## Build and deploy

```bash
./macos-native/build_macos.sh          # builds everything and assembles deploy-macos/
./macos-native/build_macos.sh --test   # ... then runs every test suite, including the simulated end-to-end run
```

Prerequisites already installed on this Mac: Qt 6.12 (`~/Qt`), Xcode command line tools, libusb
(Homebrew), .NET SDK 9 (`~/.dotnet`), and in the Anaconda Python: pyserial, pyusb, pyftdi.

Result: `deploy-macos/` holds the flat layout the controller expects:

```
deploy-macos/laser_test                  the controller (plain executable)
deploy-macos/run_simulated.sh            start it with both backends simulated
deploy-macos/run_hardware.sh             start it against the real instruments
deploy-macos/handshake_v2/backend/       SampleApl (launcher) + SampleApl.dll + wrrusb2a.dll + libwinusb_shim.dylib + System.Windows.Forms.dll
deploy-macos/handshake_v2/motor/         RamanAutofocus (launcher) + raman_autofocus.py + apt_kdc101.py + motor_config.json
deploy-macos/cnn_1.py, Portable_Raman_database/, _CNN_2class_r6g_mb/, dark_measurements/, scan_sessions/
```

## Running

- Simulation: `deploy-macos/run_simulated.sh`. Laser ON, Capture Dark, Monitor, 1D and 2D scans all
  work; spectra are synthetic (baseline plus a few Gaussian peaks), motors move instantly.
- Hardware: `deploy-macos/run_hardware.sh` with the spectrometer and the three KDC101 controllers
  plugged into the Mac. No drivers are needed: the Hamamatsu module is a vendor-specific USB
  device that libusb claims directly, and the KDC101s are reached through libusb with pyftdi
  because their FTDI chips use Thorlabs' custom product id (0403:FAF0) that macOS's own FTDI
  driver does not bind. If a controller is not found, `python3 -c "from pyftdi.ftdi import Ftdi;
  Ftdi.add_custom_product(0x0403,0xFAF0); Ftdi.show_devices()"` lists what pyftdi sees.
- The analysis button runs `cnn_1.py` with the Python named in `RAMAN_PYTHON` (the run scripts set
  it to the Anaconda interpreter, which has torch and scikit-learn).

Everything the controller writes (scan sessions, dark measurements, logs) lands inside
`deploy-macos/`, next to the executable, as on Windows.

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

Last full run (`build_macos.sh --test`, 2026-10-27): shim self-test 98/98, motor 18/18, backend 8/8
including the real-library self-test, controller suite all PASS, end-to-end simulated run PASS
(laser, dark, monitor, 1D 13 points, 2D 124 points, motor exit 0 both times).

Verified on this Mac (see `build_macos.sh --test`): the controller's own hardware-free test
suite; the motor program's unit tests (sequence identical to the C++ original, 124 points in 2D,
13 in 1D, handshake, cancel, heartbeat); the backend's file-protocol test in simulation; the
backend self-test that loads the real Hamamatsu library through the shim; and the end-to-end
simulated run of the real controller through laser-on, dark, monitor, a 1D scan and a full 2D scan.

Not verified, because it needs the instruments on the bench: real USB traffic to the spectrometer
through the shim (endpoint types, timeouts, descriptor details), real motion of the KDC101s
through pyftdi (homing, travel, timing), and the 1D travel-limit check, which on Windows came
from the Kinesis settings database and here comes from `motor_config.json` (defaults assume
25 mm actuators). Treat the first hardware session as a supervised test, exactly as the hand-off
notes demand: laser safety, clearance for the 20 mm Z move, Stop does not switch the laser off.
