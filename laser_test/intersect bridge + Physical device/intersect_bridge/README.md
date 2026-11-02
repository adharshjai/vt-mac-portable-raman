# INTERSECT bridge for Portable Raman / Qt / motor / PCA-SVM

This is a standalone new project. The existing `laser_test/main.cpp`, CMake, device source code, original EXE, models and historical data are all left unmodified.
`ORNL/intersect-architecture` is the architecture documentation; what is actually used here is `intersect-sdk==0.9.3`.

## Implemented scope

- The INTERSECT client queries status, starts the existing `1d-x` / `1d-y` / `2d` scans, cancels scans, and reads SVM results page by page.
- A standalone bridge version is generated from the original Qt source code; the original buttons and scan logic keep working.
- Qt and Python communicate through a local JSON file mailbox. No vendor driver needs to be modified, and no network library needs to be added to Qt.
- Qt's point-by-point acquisition, atomic saving and motor ACK keep their original order; the SVM analysis runs in the background in Python.
- The pure-class PCA-SVM loads the existing scaler, PCA and SVM; training inside the service is forbidden.
- Request ID de-duplication, start deadline, Qt launch instance ID, and a record written to disk before execution. When an execution result is unclear, the action is not repeated automatically.

In the first version, laser on/off, dark-spectrum acquisition and enabling remote scans remain local manual operations. Arbitrary XYZ moves and remote laser power control are not exposed.
`1d-x` / `1d-y` are still the original program's post-homing 0–24 mm, 2 mm-step, 13-point scan; `2d` keeps the original Z/XY path.
The client cannot specify a new range or step size. Stopping a scan still does not turn off the laser.

## File layout

| File | Purpose |
|---|---|
| `service.py` | INTERSECT service and request models |
| `client.py` | Client run on this machine or on another computer |
| `controller.py` | Scan-session binding, background inference and result paging |
| `qt_client.py` | Local file-mailbox client |
| `svm_predictor.py` | Pure-class preprocessing and inference, no training code |
| `prepare_qt.py` / `qt_bridge_members.inc` | Generates the extended Qt copy, checks replacement anchors |
| `build_qt.cmd` / `start_qt.cmd` | Build/start the standalone Qt version |
| `config.json` | Settings for the current machine; contains the account name and password, should not be committed to the repository |
| `config.example.json` | Configuration template |
| `compose.yaml` / `enabled_plugins` | Local RabbitMQ MQTT5 message broker configuration |
| `replay.py` | Replay of historical point-by-point JSON, does not start any device |
| `test_bridge.py` / `test_mock_integration.py` | Inference and hardware-free mock tests |

Generated files go in `generated/`, build outputs in `build_qt/bin/`; the mock versions go in `generated_mock/` and `build_mock/bin/` respectively.
New scans go in `build_qt/bin/scan_sessions/`; results go in `runtime/results/<session_id>/`.
Historical replay output goes in `runtime/replay.json`. These are all new files inside this directory and do not overwrite the original scans.

## Preparation already completed on the current machine

The newly added `.venv` has the SDK and inference dependencies installed. The device and mock versions of Qt are built with the local Qt 6.6.3 / MSVC2019.
At runtime the device version reads the original `handshake_v2/backend/SampleApl.exe` and keeps the original motor EXE path under Downloads.
The device version uses the same `controller.lock` as the original Qt; the two cannot occupy the instrument at the same time.
The hashes of the original source files used to generate the Qt copy are recorded in `generated/source_manifest.json`.

Local SDK service/schema validation, replay of the real historical 13-point scan, preprocessing consistency and mock Qt integration testing can run without connecting the hardware.
Full message-broker communication and real-hardware integration must be verified after an actual start; mock results must not be taken as real-instrument results.

## Start-up: verify on the same computer first

Enter this directory in PowerShell:

```powershell
Set-Location 'C:\Users\Jeremy666\OneDrive\Documents\laser_test\intersect_bridge'
```

### 1. Configure the model and dark spectrum

Edit `config.json`:

- `model_dir`: directory of the saved trusted model. The first version uses the pure branch of `_PCA_SVM_r6g20260922_results`.
- `dark_file`: the full dark spectrum applicable to this experiment. **The template is pre-filled with the historical dark spectrum from 2026-09-21, which is only for historical replay; before acquiring on the real instrument, replace it with a dark spectrum that matches your measurement settings.**
- `intersect.brokers[0].password`: replace `CHANGE_ME` so that it matches the message broker password.
- Currently only small in-message JSON is used and MinIO is not configured; the SDK message that MINIO cannot be supported is expected.

The service loads the model and dark spectrum once at start-up. Restart the service after replacing them. Each bound session records the model/dark-spectrum SHA256, so a new version is not mixed into an old session.
The model score is not an accuracy rate; background and low scores return UNKNOWN, and low-quality/saturated spectra return REJECTED.

First run the checks that do not connect to the device:

```powershell
.\.venv\Scripts\python.exe service.py --check
.\.venv\Scripts\python.exe replay.py '..\build\Desktop_Qt_6_6_3_MSVC2019_64bit-Debug\scan_sessions\1d_y_scan_20260921_152840_289'
```

### 2. Start the message broker

Docker was not found in PATH when this machine was checked. If an accessible MQTT5 message broker already exists, fill in its address directly; otherwise set up Docker Desktop first, and after it starts run:

```powershell
$env:RAMAN_BROKER_PASSWORD = 'enter the password matching config.json'
docker compose up -d
```

Compose exposes 1883 to this machine only by default. Do not keep the example password.

### 3. Open the bridge version of Qt

Close the original Qt control window first, then run:

```powershell
.\start_qt.cmd
```

This starts the real Raman backend just like the original Qt; it was not executed during this development test.
Confirm the device status locally, turn on the laser and prepare the dark spectrum. When remote scans are needed, tick the newly added `Enable remote scans (existing travel paths)`.
This checkbox is off by default and must be re-enabled after Qt restarts.

### 4. Start the Python service

Open another PowerShell and enter this directory:

```powershell
.\.venv\Scripts\python.exe service.py
```

Keep the service running. Terminating the Python service does not automatically stop Qt's local scan, nor does it automatically turn off the laser; this is the intended behavior of the first version.
When the network is interrupted, the local scan can keep saving; query the status and results after it recovers. Do not unconditionally resend a new start task.

### 5. Operate with the client

Open another PowerShell:

```powershell
.\.venv\Scripts\python.exe client.py status
.\.venv\Scripts\python.exe client.py start-scan --mode 1d-y --request-id experiment-001
```

A successful start returns a `session_id`. The start request only waits for Qt to accept the task, not for the whole scan to complete.
Below, replace `SESSION_ID` with the session ID actually returned:

```powershell
.\.venv\Scripts\python.exe client.py status
.\.venv\Scripts\python.exe client.py results SESSION_ID
.\.venv\Scripts\python.exe client.py cancel-scan SESSION_ID --request-id cancel-001
```

Each page holds at most 50 points; for the next page pass the `next_after_point` from the previous response:

```powershell
.\.venv\Scripts\python.exe client.py results SESSION_ID --after-point 50
```

Repeating the same start request must reuse the same `--request-id` and parameters. The program returns the existing response and does not start the scan again.
If a request times out, query the status first, or retry with the original request_id. Unexecuted commands left over from before a Qt restart are not executed in the new instance.
When the acquisition status is completed, the SVM may still be analyzing; check `analyzed_points`, `last_completed_point` and `analysis_error`.
The first version automatically analyzes scans that were successfully submitted and bound through INTERSECT. Scans started independently with the local buttons can be analyzed with `replay.py`.

## Another computer

Only `client.py`, `common.py` and one configuration file need to be copied, and the same version of `intersect-sdk` installed; Qt, the models and the vendor DLLs do not need to be copied.
The client actually uses only the `intersect` section of the configuration; the other local path fields can be deleted.
The hierarchy on both computers must be identical, and the brokers address must point to the same message broker.
If the Compose setup here is used, change the port binding from `127.0.0.1` to the LAN address designated for the device computer, and restrict the firewall scope to the experiment network.
The current example is plaintext MQTT on the experiment LAN; across untrusted networks, configure TLS or connect through a controlled VPN.

## Rebuild and test

```powershell
.\build_qt.cmd
.\build_qt.cmd --mock
.\.venv\Scripts\python.exe -m unittest test_bridge -v
.\.venv\Scripts\python.exe test_mock_integration.py
```

The mock test only runs the vendor-SDK-free test program under `build_mock`. The real device version is not started automatically.
The generator only reads the original source code; if a change in the original source structure makes an anchor non-unique, it refuses to generate, and the generator must be checked and updated.
For deployment on a new computer, a separate environment can be created: `python -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -r requirements.txt`.
The Qt/Visual Studio paths inside `build_qt.cmd` are specific to this computer and must be adjusted when changing computers.

## Known limitations

- The first version connects to a pure-class SVM, not CNN/Transformer, and does not do mixture identification or adaptive point selection.
- The existing scan paths, acquisition timeouts and local laser control behavior are kept.
- The Qt file mailbox is only a same-machine bridge; its directory should not be used as a shared command channel across computers; use INTERSECT across computers.
- If the status heartbeat exceeds 10 seconds, offline/uncertain is reported; this status does not mean the physical motor has stopped.
- A usable model and dark spectrum must be configured before the INTERSECT service starts; the service refuses to start when the model is unavailable.
- Real operation of the device version, the message broker container and communication between the two computers still need on-site verification.

Official references:
- https://github.com/ORNL/intersect-architecture
- https://github.com/INTERSECT-SDK/python-sdk
- https://intersect-python-sdk.readthedocs.io/en/latest/examples/hello-world.html
