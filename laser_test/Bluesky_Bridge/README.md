# Bluesky Raman Bridge

This demo uses Bluesky to run an experiment with the existing portable Raman
and XYZ motor controller. Qt still runs the complete scan and talks to the
device programs. The existing PCA-SVM model can analyze each saved point.

**This is a separate project. The original device code, Qt executable,
INTERSECT service, models, and old measurements are not changed.**

## Open the demo

Double-click **BlueskyBridge.exe** or **Start_Bluesky.cmd** in this folder.
Opening the bridge does not open Qt, move a motor, or turn on a laser.
The default mode is **Simulation (no hardware)**.

This EXE is a small launcher. Keep it in this folder with `app.py` and `.venv`.
It is not a stand-alone package for a new computer.

### 1. Try the simulation first

1. Leave Data source on **Simulation (no hardware)**.
2. Enter a Sample ID.
3. Click **Run experiment**.
4. Watch 13 synthetic spectra and the point table.
5. Click **Open results folder** to see the saved run.

Simulation labels say `SIMULATED`. No model or instrument is used.
Synthetic motor values are examples, not the real travel path.

### 2. Replay saved Raman data

1. Select **Saved-data replay**.
2. Click **Browse scan folder** and choose one scan folder containing
   `point_0001.json`, `point_0002.json`, and so on.
3. Choose the correct Dark TXT file if PCA-SVM analysis is enabled.
4. Click **Run experiment**.

This reads existing data. It does not acquire new spectra or change the old files.
Replay uses the positions and scan mode in the saved point records. The Scan mode
selector is used for simulation and new hardware scans, not to change old data.
Use the dark spectrum that matches the original acquisition settings. An old
scan replay with the current dark file checks software execution, not accuracy.

### 3. Run the physical Raman system

1. Finish any existing scan. Do not submit scans from the INTERSECT launcher
   and this bridge at the same time.
2. Click **Open hardware Qt**, or use the existing bridge Qt window if it is
   already open. This button opens the real device program.
3. In local Qt, prepare the connected instruments, laser, and dark spectrum.
   Enable **Enable remote scans (existing travel paths)**.
4. In Bluesky Bridge, click **Check Qt status**. Check the dark file selection.
5. Select **Hardware: existing Qt scan**, Sample ID, and Scan mode.
6. Click **Run experiment** to submit one real scan.
7. Wait for **COMPLETED** and open the results folder.

`1d-x` and `1d-y` use the existing Qt path: 0 to 24 mm, 2 mm spacing,
13 points, including the existing homing behavior. `2d` uses the current Qt
Z/XY procedure. This bridge does not change range, speed, step size, laser
power, or any device settings.

**Stop this run** cancels the current owned scan. It does not turn off the
laser. Use the local Qt laser controls. Closing this bridge during a run
requests a stop and waits for stop handling; it does not close Qt.
If a command or cancellation is uncertain, inspect local Qt and use its Stop
control. A failed or timed-out start is not automatically resubmitted.
After a crash, old commands are not automatically resumed. Check local Qt
before starting another experiment.

## How it works

```
Bluesky Plan / RunEngine
          |
          v
Python whole-scan adapter
          |
          v
Existing local JSON mailbox (intersect_bridge/build_qt/bin/ipc)
          |
          v
Existing Qt controller -> Raman and motor
          |
          v
Saved point JSON -> existing PCA-SVM -> Bluesky records and GUI
```

This demo does not use the INTERSECT network transport, RabbitMQ, or Docker.
It reuses the existing local Qt interface and inference helper files from
the sibling `intersect_bridge` folder. Do not rename or remove that folder
without updating this project's `config.json`.

**Scope:** one Bluesky trigger starts a complete Qt scan. Qt controls each
motor move and capture inside the scan. Bluesky collects each saved point
as an Event and records experiment metadata. This is not yet an independent
Bluesky motor/detector driver or a point-by-point Bluesky scan.
Pause/resume, remote web access, and automatic laser control are not added.

## Files and results

| File | Purpose |
|---|---|
| `app.py` | English Qt desktop panel |
| `bridge.py` | Bluesky plan, adapters, CLI, and experiment records |
| `config.json` | Sibling project location, output folder, timeout, optional dark override |
| `requirements.txt` | Tested direct package versions |
| `requirements-lock.txt` | Exact installed environment versions |
| `Start_Bluesky.cmd` | Open the panel using the local Python environment |
| `Setup_Bluesky.cmd` | Create/install an environment on another computer |
| `launcher.py`, `Build_Launcher.cmd` | Build the small Windows EXE launcher |
| `test_bridge.py` | No-hardware tests |

Each new run is saved in `Bluesky_Bridge/runs/<source>_<time>_<unique_id>/`:

- `documents.jsonl`: Bluesky Start, Descriptor, Event, and Stop documents.
  Each Event includes the 2048 raw intensity values, source file reference,
  reported controller positions, session ID, and class result.
- `manifest.json`: sample ID, source type, model/dark hashes, and software versions.
- `point_0001.json`, etc.: per-point analysis and references.
- `summary.json`: run UID, Qt session ID, point count, outcome, errors, and labels.
- Hardware runs also save start/cancel request and reply information when available.

Raw hardware files remain in `intersect_bridge/build_qt/bin/scan_sessions/`.
New analysis records go to this project's `runs/`, not the old results directory.
The program does not copy broker passwords into its experiment records.

The plot uses detector pixel number and raw counts. Reported controller steps
are not converted to millimeters. Classification scores are not accuracy.
`UNKNOWN` and `REJECTED` are valid model outputs, not software failures.

## Configuration

The bridge reads `ipc_dir`, `scan_root`, `model_dir`, and `dark_file` from
`../intersect_bridge/config.json`. It does not read or connect to the broker.
Choosing a dark file in this GUI saves `dark_file_override` only in this project's
config. It does not change the INTERSECT project's config.
The model is loaded once at the start of each analyzed run. No training is done.

## Command line (optional)

From this folder in PowerShell:

```powershell
.\.venv\Scripts\python.exe bridge.py check
.\.venv\Scripts\python.exe bridge.py simulation --sample-id demo-001
.\.venv\Scripts\python.exe bridge.py status
.\.venv\Scripts\python.exe bridge.py replay --session-dir "PATH_TO_ONE_SCAN_FOLDER" --sample-id replay-001
```

The following command submits a **real physical scan** after local Qt is ready:

```powershell
.\.venv\Scripts\python.exe bridge.py hardware --mode 1d-y --sample-id sample-001 --execute-existing-qt-scan
```

Use Ctrl+C to request stop in the CLI. Cancellation is cooperative and may wait
for the current mailbox call or inference operation.

## Setup and tests

This machine uses Python 3.12 in an independent `.venv`. On another computer,
install Python 3.12, prepare the sibling Qt project and trusted model/device
files, edit the local paths, and run `Setup_Bluesky.cmd`.
For a matching full environment, install `requirements-lock.txt` instead.

```powershell
.\.venv\Scripts\python.exe -m unittest test_bridge -v
```

`test_gui.py` checks the desktop panel with synthetic data. `test_mock_qt.py`
copies only the existing SDK-free mock Qt/backend/motor binaries into this
project's `test_outputs` and checks the full file interface. It does not use
the real instrument executables.

All automated tests use synthetic data, saved spectra, or a fake mailbox.
They never open the hardware Qt program. Real instrument validation must be
done by the operator using the hardware steps above.

## Official references

- [Bluesky tutorial](https://blueskyproject.io/bluesky/v1.15.1/tutorial.html)
- [Bluesky hardware interfaces](https://blueskyproject.io/bluesky/v1.15.1/hardware.html)
- [Bluesky Event Model](https://blueskyproject.io/event-model/main/explanations/data-model.html)
