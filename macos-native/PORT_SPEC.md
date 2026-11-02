# Mac-native port of the Portable Raman system — specification

Read this whole file before touching code. It is the contract between the components; the
original Windows sources are the ground truth for behaviour, this file tells you how they map
to macOS. Paths are relative to the project root `Portable_Raman_full_project/`.

## 0. Goal and ground rules

Goal: the Qt controller (`laser_test/`) runs natively on this Apple Silicon Mac and drives the
two hardware programs it launches, replaced by Mac-native equivalents that implement the SAME
file/stdout protocols and the SAME sequences:

- the Thorlabs KDC101 motor program `RamanAutofocus.exe` -> Python program `macos-native/motor/`
- the Hamamatsu capture backend `SampleApl.exe` -> .NET program `macos-native/backend/` that runs
  Hamamatsu's unmodified managed library `wrrusb2a.dll` on top of a small C shim mapping its
  Windows USB calls onto libusb.

Both replacements have a simulation mode so the full pipeline can be exercised on this Mac
without instruments. Real-device paths are implemented but can only be verified with hardware.

Rules for every agent:
- Change only the files of your component (listed in your section). Never modify files under
  `laser_test/build/`, `RamanAutofocus/`, `laser_test/hardware/SampleApl/`, or any data.
- Keep Windows behaviour intact wherever a shared file is edited (`#ifdef _WIN32` / `Q_OS_WIN`).
- Do not decompile, disassemble or inspect the IL of `wrrusb2a.dll`. Only its public API and
  its P/Invoke declarations (already dumped to `macos-native/tools/wrrusb2a_api.txt` and
  `wrrusb2a_refs.txt`) are used.
- Where a line is a direct port, cite the source file:line in a short comment.
- Everything you write must build and its tests must pass on this machine; run them and report
  the exact commands and output in your final answer.
- Tooling available: Python 3.12 at `/opt/anaconda3/bin/python3` (pyserial 3.5 installed),
  .NET SDK 9 at `~/.dotnet/dotnet` (set `DOTNET_ROOT=$HOME/.dotnet`, add to PATH,
  `DOTNET_CLI_TELEMETRY_OPTOUT=1`), clang/Xcode, libusb 1.0 at `/opt/homebrew/opt/libusb`
  (`-I/opt/homebrew/include/libusb-1.0 -L/opt/homebrew/lib -lusb-1.0`), Qt 6.12 at
  `~/Qt/6.12.0/macos`, CMake `~/Qt/Tools/CMake/CMake.app/Contents/bin/cmake`, Ninja
  `~/Qt/Tools/Ninja/ninja`.

## 1. Deploy layout on macOS (`deploy-macos/`, assembled later by `macos-native/build_macos.sh`)

```
deploy-macos/
  laser_test                     Qt controller built as a plain executable (not an .app bundle)
  cnn_1.py, models, dark_measurements/, scan_sessions/   copied from the Windows deploy folder
  handshake_v2/backend/SampleApl     launcher script (bash) -> dotnet SampleApl.dll
  handshake_v2/backend/*.dll, libwinusb_shim.dylib, System.Windows.Forms.dll, wrrusb2a.dll
  handshake_v2/motor/RamanAutofocus  launcher script (bash) -> python3 raman_autofocus.py
  handshake_v2/motor/*.py, motor_config.json
```

The controller launches `handshake_v2/backend/SampleApl` with the working directory set to
`scan_sessions/_backend_<uuid>/` (the backend uses RELATIVE paths `command.txt`,
`command_completed.txt`, `capture_results/`), and launches `handshake_v2/motor/RamanAutofocus`
with arguments `<sessionDir> <sessionId> [1d-x|1d-y]` and the working directory set to the
motor folder. Launcher scripts must `exec` the real program with the same cwd and arguments,
and must work when started from a GUI app (no PATH assumptions: use absolute interpreter
paths filled in at build time; the build script substitutes `@PYTHON@` and `@DOTNET@`).

## 2. Controller changes (component "controller", files: `laser_test/main.cpp`,
`laser_test/CMakeLists.txt`, `laser_test/tests/CMakeLists.txt`, `laser_test/tests/mock_backend.cpp`,
`laser_test/tests/mock_motor.cpp`, `laser_test/hardware/motor/scan_handshake.h`)

main.cpp (already uses app-relative paths):
1. Backend executable name: `.../handshake_v2/backend/SampleApl.exe` on Windows, `.../SampleApl`
   elsewhere (`#ifdef Q_OS_WIN`).
2. Motor executable: `ramanDir + "\\RamanAutofocus.exe"` on Windows; `ramanDir + "/RamanAutofocus"`
   elsewhere.
3. Analysis interpreter: `"python"` on Windows; elsewhere the value of environment variable
   `RAMAN_PYTHON` if set, else `"python3"`.
4. Nothing else changes. Keep the `HANDSHAKE_TESTING` blocks exactly as they are.

CMakeLists.txt: add `option(RAMAN_FLAT_LAYOUT "Build a plain executable instead of an .app bundle" OFF)`;
when ON, set `MACOSX_BUNDLE FALSE` for the target (the deploy folder is flat so the helper
folders sit next to the executable).

Tests (`laser_test/tests/`): make them build and pass on macOS with Qt 6.12:
- `CMakeLists.txt`: `/utf-8` only for MSVC; `handshake_tests` needs
  `target_compile_definitions(... HANDSHAKE_TESTING)` (already) and on macOS must not be a bundle.
  `motor_path_test` stays Windows-only (`if(WIN32)`), it compiles the Windows C++ motor source.
- `mock_backend.cpp`: the `locked_*` modes emulate a result file that exists but cannot be read
  for a while. On POSIX emulate with permissions: create the file with mode 000 after writing the
  content (open/write/fchmod(0)/close), sleep the same durations (60/160/600 ms), then
  `chmod 0644`. Keep the Windows code under `#ifdef _WIN32`.
- `mock_motor.cpp`: replace `Sleep` with a portable sleep.
- `hardware/motor/scan_handshake.h` is included by the mocks and by the Windows motor program:
  make `publish` (tmp file + atomic rename), `cancelled` (file exists) and the sleep portable
  (`#ifdef _WIN32` keeps MoveFileExA/GetFileAttributesA/Sleep; else `rename(2)`, `stat`, `usleep`).
- The test binary looks for `handshake_v2/backend/SampleApl` and `handshake_v2/motor/RamanAutofocus`
  next to itself (no `.exe` on macOS) — that is what the OUTPUT_NAME settings already produce once
  main.cpp change 1–2 are in.
Build and run: `cmake -S laser_test/tests -B laser_test/tests/build-macos -G Ninja
-DCMAKE_MAKE_PROGRAM=~/Qt/Tools/Ninja/ninja -DCMAKE_PREFIX_PATH=~/Qt/6.12.0/macos && cmake --build ...
&& (cd laser_test/tests/build-macos && ./handshake_tests)`. All `PASS` lines of
`handshake_tests.cpp` must print and the exit code must be 0. Also rebuild the controller itself:
`cmake --build build-macos-qt6.12` must still succeed.

## 3. Motor program (component "motor", directory `macos-native/motor/`)

Files:
- `raman_autofocus.py` — the program; a faithful port of
  `RamanAutofocus/RamanAutofocus/RamanAutofocusTest.cpp` (822 lines; read ALL of it).
- `apt_kdc101.py` — Thorlabs APT protocol over pyserial + a `KDC101` class with the operations the
  program needs; plus `SimulatedTransport` used when env `RAMAN_MOTOR_SIMULATE=1`.
- `scan_handshake.py` — port of `RamanAutofocus/RamanAutofocus/scan_handshake.h` and
  `line_scan_plan.h` (same semantics, same timings).
- `motor_config.json` — serial numbers (X 27266528, Y 27266445, Z 27266446), steps per mm (34700
  each), travel limits in steps for the 1D limit check (default 0..867500 = 25 mm, see below),
  optional explicit serial-port path per axis.
- `RamanAutofocus` — launcher: `#!/bin/bash` → `exec "@PYTHON@" "$(dirname "$0")/raman_autofocus.py" "$@"`
  (the build script replaces `@PYTHON@`; default to `python3` if not replaced). `chmod +x`.
- `tests/test_motor.py` — unittest; run with `python3 -m unittest discover -s macos-native/motor/tests`.

Behaviour (all from RamanAutofocusTest.cpp; keep names, constants, log text, exit codes):
- Arguments: `<sessionDir> <sessionId> [1d-x|1d-y]`; wrong count → message
  "Start this version from the updated Qt controller." exit 2; invalid mode → "Invalid scan mode."
  exit 2; non-ASCII in args → "Handshake path must be ASCII." exit 2; sessionDir missing → exit 2.
- Device list / open X, Y, Z in order (exit 1 with the same messages on failure, closing what was
  opened); start polling (200 ms) is a no-op for us except the 100 ms INIT_DELAY sleep.
- 1D branch: limits via config (the Windows build asks Kinesis's settings database; document the
  difference), `lineScanPositions()` = 0,2,...,24 mm × 34700; reject with the same message if out of
  limits; home the selected axis with `homeAxisAndConfirm`; for each target: move absolute, wait
  MoveCompleted, sleep 200 ms, verify |pos − target| ≤ 100 else abort; print
  `[1D] X at <mm> mm`; `triggerAndWaitQt()`; result 0; cleanup.
- 2D branch: backlash 0 on X, Y, Z; home X, Y, Z (robust homing: settle 500 ms, up to 10 reads
  100 ms apart until the change ≤ 5 steps, retry once if |pos| > 1000, fail otherwise); Z to
  20.0 mm then back to Z_OFFSET_AFTER_HOME = 3.8 mm; Z scan: move to offset, then 4 points at
  offset + 0.1/0.2/0.3/0.4 mm, trigger at each; then Z to offset + 0.4 − 0.2 = 4.0 mm; X and Y to
  2.0 mm (no trigger); then the 120 handwritten `scanY`/`scanX` calls EXACTLY as listed in the C++
  main (transcribe them, do not generate them; `scanX/scanY` use RELATIVE moves of ±0.1 mm per
  step as in the source and trigger after every step). Total triggers in a full 2D run: 124.
- `triggerAndWaitQt()`: `++pointId`; publish `request.txt` = `STEP <sessionId> <pointId> <posX> <posY> <posZ>\n`
  using the CURRENT positions of the three serials; wait for the ACK exactly as `waitForAck`
  (poll `response.txt` every 20 ms; line `ACK <run> <point> <OK|FAIL|PAUSED|HOLD> <heartbeat>`;
  OK → true; FAIL → false; PAUSED/HOLD with a changed heartbeat refresh the 30 s contact
  timeout; `cancel.txt` present → false). Print `[Point N] saved, advance` or
  `[Point N] FAILED/cancelled, do not advance`. Additionally publish `advanced.txt` = `<pointId>`
  after an OK (the test mocks do this; it is harmless and useful for tests).
- `waitForMessage`: poll for the expected device event, give up after 120 s or when cancelled;
  on failure send StopImmediate and print "[Error] Movement cancelled or timed out; scan aborted."
- Cleanup always: stop polling, close X, Y, Z; print "All device connections closed." and
  "Scan complete." or "Scan aborted; no further point acquired."; exit code = scanResult.
- stdout must be line-buffered/flushed (the controller shows it live).

APT protocol facts (Thorlabs "APT Communications Protocol", KDC101):
- Serial port: the KDC101's FTDI chip appears on macOS as `/dev/cu.usbserial-<serial>` with the
  Thorlabs serial number as the FTDI serial; 115200 baud, 8N1, RTS/CTS flow control
  (`rtscts=True`), set RTS high; discover with `serial.tools.list_ports` matching `serial_number`.
- Message header 6 bytes little-endian: `msgid(u16) param1(u8) param2(u8) dest(u8) source(u8)`; for
  messages with a data packet the bytes 2–3 hold the data length (u16) and `dest |= 0x80`.
  dest = 0x50 (generic USB device), source = 0x01 (host). Single-channel K-cube: channel 1.
- Messages used: `MGMSG_HW_NO_FLASH_PROGRAMMING 0x0018` (send once after opening),
  `MGMSG_MOD_SET_CHANENABLESTATE 0x0210` (param1 = chan 1, param2 = 1 enable),
  `MGMSG_HW_REQ_INFO 0x0005` → `MGMSG_HW_GET_INFO 0x0006` (84-byte data; serial number = first 4
  bytes LE, model = next 8 chars) to verify the serial, `MGMSG_MOT_REQ_POSCOUNTER 0x0411` (param1 = 1)
  → `MGMSG_MOT_GET_POSCOUNTER 0x0412` (data: chan u16, position i32), `MGMSG_MOT_MOVE_HOME 0x0443`
  (param1 = 1) → `MGMSG_MOT_MOVE_HOMED 0x0444`, `MGMSG_MOT_MOVE_ABSOLUTE 0x0453` long form (data:
  chan u16, position i32) → `MGMSG_MOT_MOVE_COMPLETED 0x0464`, `MGMSG_MOT_MOVE_RELATIVE 0x0448`
  long form (data: chan u16, distance i32) → `0x0464`, `MGMSG_MOT_SET_GENMOVEPARAMS 0x043A`
  (data: chan u16, backlash i32), `MGMSG_MOT_MOVE_STOP 0x0465` (param1 = 1, param2 = 1 immediate)
  → `MGMSG_MOT_MOVE_STOPPED 0x0466`. MOVE_COMPLETED/HOMED/STOPPED carry a 14-byte status data
  packet; just consume it. Do not enable automatic status updates, so no ACK messages are needed.
- Positions are encoder counts; the program converts mm with its own 34700 counts/mm constants.
- The reader must tolerate unsolicited messages (parse by header, read the data length when
  `dest & 0x80`) and must not block forever: use short serial timeouts in a loop.
- `SimulatedTransport`: emulates one KDC101 per serial: homing takes ~0.3 s then position 0 and
  HOMED; absolute/relative moves update the position after a delay proportional to distance
  (cap 0.2 s) and emit MOVE_COMPLETED; POSCOUNTER answers; GET_INFO answers with the serial;
  backlash/enable/no-flash accepted silently. Env `RAMAN_MOTOR_SIMULATE=1` selects it; the
  simulator must also work without any real serial port present.

Transport on macOS — IMPORTANT (added after the hardware check): the KDC101's FTDI chip uses
Thorlabs' custom product id (VID 0x0403, PID 0xFAF0). Apple's built-in FTDI driver only binds the
standard FTDI product ids, so on macOS NO `/dev/cu.usbserial-*` device appears for a KDC101.
Therefore `apt_kdc101.py` must offer two real transports behind one interface:
- `PyFtdiTransport` (DEFAULT on macOS): uses `pyftdi` (installed in /opt/anaconda3, with pyusb
  and libusb) — register the custom id once with `Ftdi.add_custom_product(0x0403, 0xFAF0, 'APT')`,
  then open with `pyftdi.serialext.serial_for_url('ftdi://0x0403:0xfaf0:<serial>/1', baudrate=115200,
  bytesize=8, parity='N', stopbits=1, rtscts=True, timeout=...)`, purge RX/TX, set RTS. Device
  discovery: `Ftdi.list_devices()` / `UsbTools.find_all([(0x0403, 0xFAF0)])` matching the USB
  serial-number string to the configured serial.
- `PySerialTransport`: for an explicit `port` path in `motor_config.json` (e.g. when FTDI's own VCP
  driver is installed, or on Linux where `/dev/ttyUSB*` appears).
Selection: config `transport` = `auto` (default: explicit port → pyserial; else pyftdi), `pyftdi`,
`serial`, or `simulate`; env `RAMAN_MOTOR_SIMULATE=1` overrides to the simulator. Tests must cover
the transport selection logic and the message layer; the pyftdi path itself can only be verified
with hardware (say so in known_gaps).

Tests (`tests/test_motor.py`), all against the simulator with a fake Qt responder thread that
answers every `request.txt` with `ACK <run> <id> OK <n>` (like `tests/motor_path_test.cpp`):
- 1D X and 1D Y: 13 points, selected axis homed once, moves at i×69400, the other axis and Z
  untouched (start them at 69400/72870/131860 like the stub), exit 0.
- 1D with a 20 mm limit (max 694000): rejected before any motion, non-zero exit.
- 2D: 124 points, Z held constant at the working height during XY, exit 0; the sequence of
  (axis, direction) steps equals the sequence parsed from the C++ `main` (parse
  `RamanAutofocusTest.cpp` in the test to derive the expected list; assert 120 XY calls).
- Cancel: `cancel.txt` during a run → program exits non-zero without advancing.
- Heartbeat: an ACK that stays `PAUSED` with a frozen heartbeat times out (use a shortened
  timeout hook for the test); a changing heartbeat keeps waiting; OK resumes.
- Codec round-trips for every message above.

## 4. Capture backend (component "backend", directory `macos-native/backend/`)

Existing inputs: `lib/wrrusb2a.dll` (copy of Hamamatsu's library with the PE machine field
re-tagged to ARM64 by `tools/retag_assembly.py`; the IL is untouched), the stand-in
`stubs/System.Windows.Forms/` (built: `bin/Release/net9.0/System.Windows.Forms.dll`, identity
`System.Windows.Forms, Version=2.0.0.0, PublicKeyToken=b77a5c561934e089`), the API dump
`../tools/wrrusb2a_api.txt`, the references dump `../tools/wrrusb2a_refs.txt`.

Files to create:
- `SampleApl/SampleApl.csproj` (net9.0, OutputType Exe, `AllowUnsafeBlocks`, reference
  `..\lib\wrrusb2a.dll` and `..\stubs\System.Windows.Forms\bin\Release\net9.0\System.Windows.Forms.dll`
  with `Private=true` so both are copied to the output; `InvariantGlobalization` false).
- `SampleApl/Program.cs` — port of `laser_test/hardware/SampleApl/SampleApl.cpp` main loop,
  `CheckCommandFile`, `PublishCaptureResult`, `InitializeDevice`, `UninitializeDevice`,
  `GetDeivceInformation` (menu 1), `GetCalibrationValue` (2), `GetLDPower` (3), `SetLDPower` (4),
  `GetLDPowerEnable` (5), `SetLDPowerEnable` (6), `GetLDTemperature` (7), `GetLDBoardTemperature` (8),
  `StartMeasureForSingleSpectrum` (S / M), `DisplayData`, `OnProcessExit`, `Q`. Menu items F/G/H/R/T/C
  may print "not available in this build" (they are interactive/continuous modes the controller
  never uses). Console text must match the original strings exactly where the controller parses
  them: `LD Power Enable : 0x{0:x08}`, `Error code 0x{0:x04}: ...`, `MONITOR_FRAME ` + result on one
  line, `Image Data ` + 2048 `{0:X04} ` values, `[Capture handshake v2.2] ...`, `[Capture waiting]`,
  `[Capture ready] ...`, `[Capture failed] ...`, `End of measurement`, `[Backend process started]`
  is printed by the controller, not you. Flush stdout after every line.
- `SampleApl/CaptureTiming.cs` — port of `capture_timing.h` (`firstCaptureStatusDelayMs`,
  `firstAvailableCaptureIndex`, `captureStatusDecision`).
- `SampleApl/IWrrUsb2.cs` — interface with exactly the 30 `USB2_*` operations SampleApl uses, with
  the DLL's signatures (`long` ids, `ref long`, `ref ushort[]`, `double[]`, `short[] list, ref ushort number`,
  `ref CModuleInformation`...; see the dump). `RealDevice` wraps `WRRUSB2_DLL.WRRUSB2`.
- `SampleApl/SimulatedDevice.cs` — used when `--simulate` or env `RAMAN_BACKEND_SIMULATE=1`:
  one device id 0; set* return 0; image size 1×2048; `captureStart` records the time;
  `getCaptureStatus` returns 1 (pending) until exposure (1,000,000 µs) + 100 ms have elapsed, then 0
  with frame=1,index=0; `getImageHeaderData` fills a synthetic spectrum (baseline ~800 counts, a few
  Gaussian Raman peaks, small noise, values < 65535) and the header; LD power/enable are stored and
  read back; module info strings `Hamamatsu`, `C13560-0274`, `SIMULATED`, firmware `1.00`; spectro
  unit `SIM`; calibration coefficients fixed numbers; temperatures 25.
- `SampleApl/Native.cs` — at startup: `Encoding.RegisterProvider(CodePagesEncodingProvider.Instance)`
  (the DLL uses Shift-JIS); `AssemblyLoadContext.Default.Resolving` → load `System.Windows.Forms.dll`
  from the app folder; `NativeLibrary.SetDllImportResolver` for the `wrrusb2a` assembly mapping
  library names `winusb.dll`, `setupapi.dll`, `kernel32.dll`, `kernel32`, `user32.dll`, `shell32.dll`
  to `libwinusb_shim.dylib` in the app folder (one dylib implements all of them).
- `SampleApl/Program.cs --selftest`: without hardware, load the DLL for real: create `WRRUSB2`,
  call `USB2_initialize()` and print the return code (any value is fine, it must not throw),
  `USB2_getModuleConnectionList` (expect 0 devices or an error code), then create
  `WRRUSB2_DLL.USBRemovableCheck`, call `SetDevicePath("selftest")` and `IsRemoveDevice()` (this
  reaches the user32 P/Invoke through the shim; the kernel32/shell32/winusb entry points are not
  reachable without hardware, so the self-test re-declares them with the DLL's exact signatures in
  ShimProbes.cs and calls them directly), then exit 0 and print `SELFTEST OK`.
  If the shim is missing, print a clear message and exit 3.
- `SampleApl` launcher script next to the csproj: `exec "@DOTNET@" "$(dirname "$0")/SampleApl.dll" "$@"`.
- Build: `dotnet publish -c Release -o bin/publish` (framework-dependent, no single-file). The
  deploy step copies `bin/publish/*` plus the shim dylib into `handshake_v2/backend/`.
- A Python protocol test `tests/test_backend_protocol.py` that starts the backend in simulation
  in a temp dir and drives it through the file protocol like the controller does: `6` then `1`
  (enable), `4` then `2` (power), `5` (expect stdout `LD Power Enable : 0x00000001`), `S abc123`
  (expect `capture_results/abc123.txt` = `CAPTURE abc123 OK 2048\n<2048 hex>\nEND abc123\n` within
  ~2 s), `M mon1` (expect a `MONITOR_FRAME CAPTURE mon1 OK 2048 ... END mon1` stdout line), `Q` (exit 0).
  Also run `--selftest` and assert `SELFTEST OK`.

## 5. Windows-API shim (component "shim", directory `macos-native/backend/shim/`)

`winusb_shim.c` + `Makefile` producing `libwinusb_shim.dylib` (arm64, links `-lusb-1.0`), and
`shim_selftest.c` producing `shim_selftest` that calls the enumeration functions and prints the
devices found (0 without hardware) — both must build with no warnings under `-Wall -Wextra`.

The managed declarations are in `../../tools/wrrusb2a_api.txt` (search `PINVOKE`). .NET on
macOS marshals them as follows, so the C prototypes are (all exported with C linkage, default
visibility, no name decoration; `BOOL` = `int32_t`, `HANDLE` = `intptr_t`):

```c
// kernel32
intptr_t CreateFile(const char* path_utf8, uint32_t access, int32_t share, void* sec, int32_t disposition, int32_t flags, int32_t template_);
int32_t  CloseHandle(intptr_t h);
int32_t  QueryPerformanceCounter(int64_t* out);     // clock_gettime(CLOCK_MONOTONIC) in ns
int32_t  QueryPerformanceFrequency(int64_t* out);   // 1000000000
int32_t  GetPrivateProfileString(const char* app, const char* key, const char* def, char* out, uint64_t size, const char* file); // copy def (UTF-8), return its length
// user32 / shell32
intptr_t RegisterDeviceNotification(intptr_t recipient, void* filter, int32_t flags); // return a non-zero fake handle
int32_t  UnregisterDeviceNotification(intptr_t h);                                   // TRUE
uint32_t SHChangeNotifyRegister(intptr_t hwnd, int32_t sources, int32_t events, uint32_t msg, int32_t count, void* entry); // export ALSO under the literal symbol names "#2" and "#4" (ordinal entry points; use __asm__ labels) ; return 1
int32_t  SHChangeNotifyUnregister(uint8_t h);                                          // TRUE
// setupapi (enumeration of Hamamatsu devices: idVendor 0x0661, any product id; env WRRUSB_SHIM_VID overrides)
int32_t  SetupDiCreateDeviceInfoList(const GUID* g, int32_t hwnd);
intptr_t SetupDiGetClassDevs(const GUID* g, void* enumerator, void* hwnd, int32_t flags); // snapshot of matching devices; never -1
int32_t  SetupDiEnumDeviceInterfaces(intptr_t set, void* devinfo, const GUID* g, int32_t index, SP_DEVICE_INTERFACE_DATA* out); // cbSize=32, Flags=1, Reserved=index; FALSE + errno ERROR_NO_MORE_ITEMS(259) past the end
int32_t  SetupDiGetDeviceInterfaceDetail(intptr_t set, SP_DEVICE_INTERFACE_DATA* d, void* detail, int32_t size, int32_t* required, void* devinfo);
         // detail==NULL or size too small: *required = 4 + strlen(path)+1, errno = ERROR_INSUFFICIENT_BUFFER(122), return FALSE.
         // else: write the UTF-8 path at offset 4 (cbSize field occupies 0..3, do not overwrite it), return TRUE.
int32_t  SetupDiDestroyDeviceInfoList(intptr_t set);
// winusb
int32_t  WinUsb_Initialize(intptr_t fileHandle, intptr_t* ifaceHandle);
int32_t  WinUsb_Free(intptr_t h);
int32_t  WinUsb_GetDescriptor(intptr_t h, uint8_t type, uint8_t index, uint16_t lang, USB_DEVICE_DESCRIPTOR* buf, uint32_t len, uint32_t* transferred);
int32_t  WinUsb_QueryInterfaceSettings(intptr_t h, uint8_t alt, USB_INTERFACE_DESCRIPTOR* out);   // 9 bytes
int32_t  WinUsb_QueryPipe(intptr_t h, uint8_t alt, uint8_t pipeIndex, WINUSB_PIPE_INFORMATION* out); // {int32 PipeType; uint8 PipeId; uint16 MaximumPacketSize @6; uint8 Interval @8} size 12; FALSE + errno 259 past the end
int32_t  WinUsb_QueryDeviceInformation(intptr_t h, uint32_t type, uint32_t* len, uint8_t* buf);   // type 1 = DEVICE_SPEED: write 1 byte, 3 high / 2 full / 1 low, *len = 1
int32_t  WinUsb_SetPipePolicy(intptr_t h, uint8_t pipe, uint32_t policy, uint32_t valueLen, const void* value); // PIPE_TRANSFER_TIMEOUT(3)=uint32 ms stored per pipe (0 = infinite); others accepted and ignored
int32_t  WinUsb_ReadPipe(intptr_t h, uint8_t pipe, uint8_t* buf, uint32_t len, uint32_t* transferred, void* overlapped);
int32_t  WinUsb_WritePipe(intptr_t h, uint8_t pipe, const uint8_t* buf, uint32_t len, uint32_t* transferred, void* overlapped);
int32_t  WinUsb_ControlTransfer(intptr_t h, WINUSB_SETUP_PACKET setup /* by value, 8 bytes packed */, uint8_t* buf, uint32_t len, uint32_t* transferred, void* overlapped);
int32_t  WinUsb_ResetPipe(intptr_t h, uint8_t pipe);  // libusb_clear_halt
```

Semantics: `CreateFile(path)` opens the libusb device named by the path produced by the
enumeration (`\\?\usb#vid_0661&pid_XXXX#bus-addr#{88dc0d16-f5f0-4836-b6d0-89fd7c3495f3}`),
returns a handle ≥ 0x10000 (never a small number, so a stray `close()` on it is harmless) or
`-1` with errno `ERROR_FILE_NOT_FOUND` (2). `WinUsb_Initialize` claims interface 0 (detach kernel
driver if libusb reports one) and returns a separate interface handle. Pipe reads/writes use bulk
or interrupt transfers according to the endpoint descriptor, with the per-pipe timeout policy
(default when unset: env `WRRUSB_SHIM_DEFAULT_TIMEOUT_MS`, else 5000). On libusb timeout return
FALSE with errno `ERROR_SEM_TIMEOUT` (121); on other errors FALSE with errno
`ERROR_GEN_FAILURE` (31); on success TRUE with `*transferred`. `WinUsb_Free` releases the
interface; `CloseHandle` closes the device. All `errno` values are what the managed side reads
through `Marshal.GetLastWin32Error()` on Unix. Env `WRRUSB_SHIM_DEBUG=1` logs every call to stderr.
Struct layouts (little-endian, natural alignment unless noted): `GUID` 16 bytes (u32,u16,u16,u8[8]);
`SP_DEVICE_INTERFACE_DATA` {int32 cbSize; GUID; int32 Flags; intptr Reserved} = 32 bytes;
`USB_DEVICE_DESCRIPTOR` 18 bytes packed as in the USB spec (the managed struct is Sequential with
default packing: uint16 fields at offsets 2, 8, 10, 12 — identical to the USB layout);
`USB_INTERFACE_DESCRIPTOR` 9 bytes; `WINUSB_SETUP_PACKET` 8 bytes packed (`#pragma pack(1)`).

## 6. Review criteria (used by the reviewers; implementers should self-check against them)

- Motor: every constant, message string, exit code, timing and the 124-point sequence match
  `RamanAutofocusTest.cpp`; the handshake matches `scan_handshake.h`; APT framing bytes are correct
  (header order, data-length/0x80 convention, dest/source); serial settings; no blocking forever.
- Backend: command/response strings byte-identical to `SampleApl.cpp`; capture algorithm identical
  (initial delay, 50 ms poll, 8 s deadline, rc=1 handling, stop/release in `finally`, result codes);
  DLL calls use the exact signatures; simulation mode never touches the DLL; the resolver maps
  every library name the DLL imports; code pages registered before any DLL call.
- Shim: prototypes match the managed declarations under Unix marshaling (UTF-8 strings for
  CharSet.Auto, `ref` → pointer, arrays → pointer, struct by value for the setup packet); struct
  sizes/offsets; errno codes; no undefined behaviour; handles never collide with file descriptors.
- Controller/tests: Windows behaviour unchanged; `handshake_tests` passes on macOS.
