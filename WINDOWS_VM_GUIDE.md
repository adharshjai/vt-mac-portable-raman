# Running the Portable Raman project on this Mac through a Windows 11 VM

Goal: build and run the whole system (Qt controller, Hamamatsu capture backend,
Thorlabs motor program, Python analysis) with the real spectrometer and KDC101
stages, on an Apple Silicon MacBook, inside **VMware Fusion** running **Windows 11
Arm**. Windows 11 Arm runs the x64 toolchain and x64 programs through its built-in
emulation; the only components that must be Arm-native are kernel drivers.

Companion files, all in this folder:

- `windows-vm/check_prereqs.ps1` — run inside Windows; reports what is installed, what is missing, and whether the instruments are visible.
- `windows-vm/build_all.cmd` — run inside Windows; builds all three programs and deploys them in the layout the controller expects.
- `windows-vm/apply_path_fixes.py` — already applied here; replaces the author's absolute paths in `laser_test/main.cpp` and `cnn_1.py` with paths next to the executable.
- `windows-vm/Set-RamanDeviceInterfaceGuid.ps1`, `windows-vm/winusb2.0wrraman_arm64.inf` — Hamamatsu driver setup for Windows on Arm.
- `INSTALL_AND_RUN_NOTES.txt` — the collaborator's hand-off notes (translated). Read section 6 on safety before any hardware run.

---

## 1. VMware Fusion

1. Download VMware Fusion 13.6 or newer (free for personal use) from Broadcom's support portal with your Broadcom account, and install it.
2. Fusion 13.5+ can fetch Windows itself: **File > New > Get Windows from Microsoft**. It downloads the Windows 11 Arm image and creates the VM with TPM and Secure Boot already configured. Pick *Windows 11 Pro* if offered (Home works too).
3. Before the first boot, open the VM's **Settings**:
   - *Processors & Memory*: 4 cores, 8 GB RAM minimum (16 GB if you will train models).
   - *Hard Disk*: 100 GB. Visual Studio, Qt and Windows need roughly 50 GB together.
   - *USB & Bluetooth*: USB compatibility **USB 3.1**; set *When a USB device is plugged in* to **Ask what to do**, so you can send each instrument to Windows.
4. Boot and complete Windows setup. Activation is optional.
5. Install **VMware Tools** (*Virtual Machine > Install VMware Tools*) if the Get-Windows flow did not already. Tools give you shared folders, clipboard and resolution scaling.
6. Share this project folder: *Settings > Sharing > Enable Shared Folders*, add `Portable_Raman_full_project`. In Windows it appears under `\\vmware-host\Shared Folders\`.

## 2. Put the project on the Windows disk

Copy the whole `Portable_Raman_full_project` folder from the shared folder to a short
local path, for example `C:\Raman\Portable_Raman_full_project`. Build and run from the
local copy, never from the shared folder (file locking and speed). Keep `laser_test`
and `RamanAutofocus` side by side, as the hand-off notes require.

## 3. Install the toolchain (inside Windows)

All of these are x64 installers and run fine on Windows 11 Arm.

**Visual Studio 2022 Community** (visualstudio.microsoft.com):
- Workload *Desktop development with C++*.
- Individual components, searched by name in the installer:
  - *MSVC v143 - VS 2022 C++ x64/x86 build tools (Latest)* — on an Arm host this is the x64 cross compiler, not selected by default.
  - *C++/CLI support for v143 build tools (Latest)* — required by the Hamamatsu backend.
  - *.NET Framework 4.7.2 targeting pack* — required by the Hamamatsu backend.
  - *Windows 11 SDK* (any 10.0.22621 or newer).

**Qt** (Qt Online Installer, free Qt account):
- *Qt 6.x* (6.8 LTS or newer) > tick **MSVC 2022 64-bit**.
- Under *Additional Libraries* tick **Qt Charts** (the controller will not configure without it).
- Under *Build Tools* tick **CMake** and **Ninja**. Qt Creator is included and optional.

**Python 3.11 or 3.12, x64 build** (python.org): tick *Add python.exe to PATH*. The
controller's *Analyze Latest* button runs `python cnn_1.py`. Then, from the project folder:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r Python_dependencies\core-analysis.requirements.txt
```

Use the x64 build, not the Arm build, so the PyTorch and scikit-learn wheels install
without trouble (they run under emulation; training is slow, inference is fine).

**Thorlabs Kinesis** (thorlabs.com > Kinesis software, 64-bit): install to the default
`C:\Program Files\Thorlabs\Kinesis`. The build uses its headers, `.lib` and DLLs.

**FTDI driver for Windows on Arm**: the KDC101 controllers contain an FTDI USB chip.
Kinesis bundles only the x64 FTDI driver, which will not install on Arm Windows.
Download the **ARM64** CDM driver (2.12.36.4A or newer) from FTDI's driver page
(D2XX/VCP, pick the ARM64 package, it is not on Windows Update) and install it before
plugging the controllers in.

Run `windows-vm\check_prereqs.ps1` in PowerShell to confirm all of the above:

```
powershell -ExecutionPolicy Bypass -File windows-vm\check_prereqs.ps1
```

## 4. Attach the instruments and install drivers

Plug each instrument into the Mac and, when Fusion asks, send it to the Windows VM
(or use *Virtual Machine > USB & Bluetooth > Connect <device>*).

**Hamamatsu Raman module (`USB\VID_0661&PID_294C`)** — use Windows' own signed WinUSB driver, then register the interface GUID the Hamamatsu library searches for:

1. Device Manager > *Other devices* > the unknown device (check *Details > Hardware Ids*).
2. *Update driver > Browse my computer > Let me pick > Universal Serial Bus devices > WinUsb Device*.
3. PowerShell **as Administrator**, in the `windows-vm` folder:
   `powershell -ExecutionPolicy Bypass -File .\Set-RamanDeviceInterfaceGuid.ps1`
4. Unplug, replug, send to the VM again.

The alternative unsigned INF (`winusb2.0wrraman_arm64.inf`) only works with driver signature enforcement disabled; use it only if the registry method fails.

**Thorlabs KDC101 controllers**: with the ARM64 FTDI driver installed, each controller
appears as a *USB Serial Converter*. Open the Kinesis application; it must list three
KDC101 units with these serial numbers, which are hard-coded in
`RamanAutofocus/RamanAutofocus/RamanAutofocusTest.cpp`:

| Axis | Serial number |
|---|---|
| X | 27266528 |
| Y | 27266445 |
| Z | 27266446 |

If Kinesis does not see them, the FTDI driver is the first thing to check (see
Troubleshooting). Close Kinesis before running the controller; only one program can
own the controllers.

## 5. Build everything

From a normal Command Prompt in the project folder:

```
windows-vm\build_all.cmd
```

It locates Visual Studio, builds the Qt controller (`laser_test\build-win-x64`), the
motor program (`RamanAutofocus\build-win-x64`, target `RamanAutofocus` only, never the
`KDC101Test`/`LibTest` programs), and the Hamamatsu backend (MSBuild, Release x64,
retargeted to the v143 toolset and compiled with the x64-hosted tools, since C++/CLI is
not available from the Arm-hosted compiler), then deploys into
`laser_test\build\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug\`:

```
laser_test.exe  + Qt DLLs (windeployqt)
cnn_1.py, models, dark_measurements\, scan_sessions\   (already there from the hand-off)
handshake_v2\backend\SampleApl.exe + wrrusb2a.dll
handshake_v2\motor\RamanAutofocus.exe + Thorlabs.MotionControl.*.dll
```

That folder is used because it already holds the analysis script, trained models and
measurement history the controller expects next to its exe. If your Qt or Kinesis
installs are elsewhere, set `QT_DIR` and `KINESIS_DIR` before running the script, e.g.
`set QT_DIR=C:\Qt\6.9.0\msvc2022_64`.

To work in **Qt Creator** instead: open `laser_test\CMakeLists.txt`, choose the
*Desktop Qt 6.x MSVC2022 64bit* kit, let it create a new build directory, build, and
copy `laser_test.exe` into the deploy folder above; the backends still come from
`build_all.cmd`.

The path fixes already applied (`windows-vm/apply_path_fixes.py`, originals kept as
`*.orig`): the controller now looks for `handshake_v2\motor`, `cnn_1.py` and
`laser_log.txt` next to its own exe in every build, and `cnn_1.py` uses its own folder
as `DATA_ROOT`. Nothing else in the controller logic was changed.

## 6. First run, in order

Follow section 6 of `INSTALL_AND_RUN_NOTES.txt`. In short:

1. Run `check_prereqs.ps1` once more with both instruments attached: it must show WinUSB bound with the GUID set, and three FTDI devices.
2. Start `laser_test.exe` from the deploy folder. The log should show `[Backend process started]`. If it shows *Could not start backend*, see Troubleshooting.
3. Try **Capture Dark** (no laser, no motion) to prove the spectrometer path.
4. **Laser ON** only with laser safety in place; **Monitor** gives a live focus preview.
5. Before any scan, check the physical travel: the 2D sequence homes Z, moves to 20 mm, then returns to the configured offset. Stop does not switch the laser off.

## 7. Troubleshooting

- **`build_all.cmd` says Qt Charts is missing**: add *Qt Charts* for your kit in the Qt Maintenance Tool.
- **MSBuild error about toolset v142 or .NET 4.7.2**: install the two Visual Studio components listed in section 3. The script already passes `PlatformToolset=v143`.
- **C4819 encoding warnings**: gone; the Japanese Shift-JIS comments were translated to ASCII.
- **Controller: "Could not start backend"**: `handshake_v2\backend\SampleApl.exe` is missing, or .NET Framework is disabled. Run `SampleApl.exe` from a Command Prompt to see its own error.
- **Backend runs but never reports a frame**: the Hamamatsu device is not under WinUSB with the GUID, or it is attached to macOS instead of the VM. Re-run `Set-RamanDeviceInterfaceGuid.ps1`, replug, reattach.
- **Motor program: device not found**: FTDI ARM64 driver not installed, controllers still attached to macOS, or serial numbers differ from the table above. Verify in Kinesis first, then close Kinesis.
- **`Thorlabs.MotionControl.DeviceManager.dll` not found at runtime**: add `C:\Program Files\Thorlabs\Kinesis` to the system PATH, or copy the remaining Kinesis DLLs into `handshake_v2\motor`.
- **Analyze Latest: Script not found**: `cnn_1.py` must sit next to `laser_test.exe` (it does in the deploy folder) and `python` must be on PATH with the packages installed.
- **USB device keeps going to the Mac**: Fusion *Settings > USB & Bluetooth*, set the plug-in action to connect to this VM.

## 8. Known risks specific to Windows on Arm

- FTDI's ARM64 driver is the only unofficial link in the chain. One report on Windows 11 24H2 says it did not work in a Parallels VM; most reports are fine. If the KDC101s never enumerate, test the FTDI driver with a single controller first, and consider a native Windows PC as the fallback for motor runs.
- Thorlabs does not list Windows on Arm as supported for Kinesis; it runs as an x64 application under emulation.
- USB passthrough timing under virtualization is good enough for this handshake (seconds-scale timeouts), but a spectrometer stuck in a capture is easier to recover by unplugging and reattaching than on bare metal.
