# Portable Raman controller, DataFed edition

This folder is a copy of the Portable Raman controller with a data pipeline added. Every saved
spectrum gets a catalog record. A classifier result is saved as its own record, linked to the
spectrum, the model version and the dark spectrum it came from. Uncertain results wait for a
person to review them. The catalog is either a local demo on this Mac or the DataFed service.

Everything here was built and run on this Mac **in simulation only**. No instrument was attached,
and nothing was sent to DataFed or Globus. What was tested, with counts, is in
[VERIFICATION.json](VERIFICATION.json).

1. [What this edition is](#1-what-this-edition-is)
2. [Before you run anything](#2-before-you-run-anything)
3. [Quick start in simulation](#3-quick-start-in-simulation)
4. [Opening it in Qt Creator](#4-opening-it-in-qt-creator)
5. [The data flow in five steps](#5-the-data-flow-in-five-steps)
6. [The Simulation Studio](#6-the-simulation-studio)
7. [Going live with DataFed](#7-going-live-with-datafed)
8. [The classification job on ARC](#8-the-classification-job-on-arc)
9. [Real instruments](#9-real-instruments)
10. [The older platform bridges](#10-the-older-platform-bridges)
11. [Folder map](#11-folder-map)
12. [Troubleshooting](#12-troubleshooting)
13. [What was verified](#13-what-was-verified)

## 1. What this edition is

The original project around this folder (`..`) was not changed. This folder holds copies of the
parts the edition needs: the Qt controller, the Mac-native capture backend and motor program, and
the classifier model. It adds:

- **Who, what, where.** The window has a Sample group. Sample ID, Operator and Location are
  required; Notes is optional. Each field takes at most 200 characters, counted in UTF-16 units as
  `raman_sim_runner` counts them: an emoji counts two, and one that does not fit whole is left out.
  A scan does not start while a required field is empty. Operator and Location are remembered
  between starts.
- **More in every file.** Each scan folder gets a `session.json`, and each `point_NNNN.json` gets a
  `capture` block: the four fields, the laser power level, the instrument, whether the simulators
  were used, and the dark spectrum the session is paired with. Each dark spectrum gets a
  `dark_<stamp>.json` beside it that says whether it was taken in simulation.
- **Capture Spectrum.** A button that saves one spectrum at the current position. It does not move
  the motors.
- **A data layer** (`data_layer/`, Python). It registers each spectrum in the catalog, uploads its
  file, runs the classifier, links each prediction to its sources and keeps the review queue. The
  window shows it in two new tabs, Data and Review.
- **The Simulation Studio** (`studio/`). A web page that shows a scan and the data pipeline as they
  happen, and starts simulated runs.
- **A virtual sample** (`virtual_sample/`). A simulated 25 mm × 25 mm plate with R6G, methylene
  blue and adenine on DI water. Simulated scans give spectra that depend on the stage position,
  the laser power and the focus, built from real measured spectra.
- **raman_sim_runner.** The controller without a window, always simulated. The Studio uses it; you
  can also run it by hand.

The contract between all these pieces is [data_layer/PROTOCOL.md](data_layer/PROTOCOL.md).

**What did not change.** The code paths used with real instruments keep their behaviour: laser on
and off with its read-back, power commands, captures, motor moves, Stop, cancel, failures and the
safety stop. Two additions reach the real instruments' paths. At start-up the capture backend reads
the module's identity (see [Real instruments](#9-real-instruments)). Capture Dark writes
`dark_<stamp>.json` beside each dark; the dark's `.txt` file is written exactly as before, and if
the `.json` cannot be written the dark counts as not saved.

**Two locks.** Each controller writes `controller.lock` in its own deploy folder, so the same
controller cannot run twice. The original controller checks only its own lock,
`../deploy-macos/controller.lock`. So this edition's window takes that lock too, for as long as it
is open, provided `../deploy-macos/` with its `handshake_v2/` folder existed when the window
started:

- While this edition's window is open, the original refuses to start ("Controller already running").
- While the original runs, this edition's window refuses to start ("Original controller running").

The two controllers therefore cannot drive the instruments at the same time. If you build the
original's deploy folder while this window is open, restart this window so that it takes that
lock as well. When a lock file cannot be created at all, for example in a folder without write
permission, the window does not start either and says "Controller lock not available" or "Original
controller lock not available". The lock is released when the window closes. It covers an original
started from `../deploy-macos/` or from Qt Creator, which uses that folder as well; an original
started from any other folder, or the Windows build, is not covered. It was tested with two
processes on stand-in folders, not yet against the original controller itself. `raman_sim_runner`
takes only this edition's lock; it never drives an instrument.

**Darks.** A scan is never paired with a dark of the other kind: a simulated scan uses the newest
simulated dark, a real scan the newest real one. A dark without a `.json` (every dark taken
before this edition) counts as real; one whose `.json` cannot be read is never used. The build
copies none of the original project's dark spectra into this edition
([PROTOCOL.md](data_layer/PROTOCOL.md) section 2.5).

## 2. Before you run anything

Check what is plugged in:

```bash
reg="$(/usr/sbin/ioreg -p IOUSB -l -w0 2>/dev/null)"; printf '%s\n' "$reg" | grep -cE '"idVendor" = 1633|"idProduct" = 64240'
```

It counts the Hamamatsu module (USB vendor 1633) and the Thorlabs KDC101 motor controllers (USB
product 64240). Every simulation run of this edition was made with this check printing 0. If it
prints anything else, unplug the spectrometer and the three motor controllers before you follow the
quick start.

Then, in every Terminal window you use for this edition:

```bash
export RAMAN_BACKEND_SIMULATE=1 RAMAN_MOTOR_SIMULATE=1
```

With both variables at exactly `1`, the capture backend and the motor program run their simulators.
The window then shows **SIMULATION** in its header and in its title. `run_simulated.sh` and
`raman_sim_runner` set the two variables themselves; `run_hardware.sh` clears them.
`hardware_check.sh` ignores them: it simulates only when you give it `--simulate`.

This holds for the macOS build. On Windows the helper programs are the original `SampleApl.exe` and
`RamanAutofocus.exe`, which have no simulator: there a set variable makes the header say **NO
SIMULATOR ON WINDOWS: instruments are real**, and the data is labelled real. The Windows build was
not built or run for this edition.

## 3. Quick start in simulation

Run the commands in Terminal from this folder, `Portable_Raman_DataFed/`, unless they start with
`cd`. Everything they need is installed on this Mac: Qt 6.12.0 in `~/Qt`, the .NET SDK in
`~/.dotnet`, Anaconda Python with pyserial, Node, and this edition's Python environment `.venv/`
(made by `data_layer/setup_env.sh`). The full list is in
[macos-native/README.md](macos-native/README.md). The build also needs Hamamatsu's library
`macos-native/backend/lib/wrrusb2a.dll`, which is not in git
([macos-native/backend/lib/README.md](macos-native/backend/lib/README.md)).

**Build.** This makes the deploy folder `deploy-macos/`, which holds the window, the helper programs,
the data layer and the data:

```bash
./macos-native/build_macos.sh
```

If `.venv/` is missing, as in a fresh copy of this folder, the build first runs
`data_layer/setup_env.sh`, which downloads the data layer's packages from PyPI (about 215 MB).

**Test** (optional, about 11 minutes):

```bash
RAMAN_SKIP_DEVICE_SELFTESTS=1 ./macos-native/build_macos.sh --test
```

It prints one PASS or FAIL line per suite and ends with a line that starts `== every suite passed`.
Each suite's output is in `.build/test-logs/`. `RAMAN_SKIP_DEVICE_SELFTESTS=1` leaves out the two
self-tests that open the USB stack and the vendor library (`shim_selftest`, `SampleApl --selftest`);
they were not run for this edition.

**The window.**

```bash
cd deploy-macos
./run_simulated.sh
```

In the window: fill in Sample ID, Operator and Location. Until a read-back has confirmed the
laser's state, the LASER pill in the header says `not read`. Press Laser ON and wait until the
LASER pill says `ON · HIGH`. The title row of the Capture group names the dark the next scan will
use; in a new deploy folder it says `no simulated dark yet`, and Capture Dark is then the
highlighted button. Press Capture Dark. Then choose X axis in the box beside 1D Scan and press
**1D Scan**, or press the single button **Start Z scan then 2D scan**. When the scan has ended (the
chart title then says how it ended and which point was saved last), open the Data tab: it syncs
by itself after the scan, and then every 30 s while a sync has something to move on. When the new
rows say Uploaded (or after one more Sync now), press Classify now, then open the Review tab.
Press Laser OFF when you are done. The window was not started this way while the edition was
rehearsed; its code ran in the test programs (see [What was verified](#13-what-was-verified)).

**The Simulation Studio**, from `deploy-macos/`:

```bash
./studio.sh
```

It prints its address, opens the page in your browser and runs until you press Ctrl-C. Without a
browser window:

```bash
./studio.sh --no-open --port 8765
```

and open <http://127.0.0.1:8765/> yourself. A run started with the page's Play button does not
start while the controller window is open, because both take the same lock; the page says so.
Close the window first.

**The runner by hand**, from `deploy-macos/` (about 27 s):

```bash
./raman_sim_runner --mode 1d-x --sample-id SIM-1DX --operator "quick start" --location simulation --power HIGH
```

Put your own name after `--operator`. One run is Laser ON, Capture Dark, the scan, Laser OFF. The
modes are `1d-x`, `1d-y`, `2d` (a Z sweep of 4 spectra, then 120 grid points; about 3 minutes) and
`single`. It prints one line per step, each starting with `[runner]`. Exit code: 0 done, 1 a step
failed (also when its `controller.lock` cannot be created), 2 wrong arguments, 3 the lock is held
by another controller, 128 + N stopped by signal N. Unless you add `--keep-stage`, the simulated
stage starts at the virtual sample's start position.

**The data layer by hand**, from `deploy-macos/`. First give it a short name in this Terminal
window. It starts the data layer with the interpreter and package of the deploy folder, as the
window does ([PROTOCOL.md](data_layer/PROTOCOL.md) section 1):

```bash
datalayer() { PYTHONPATH="$PWD/data_layer" "$(head -n 1 datafed_python.txt)" -m raman_datafed --home "$PWD" "$@"; }
```

Then:

```bash
datalayer sync
datalayer sync
datalayer classify
datalayer review-list
datalayer status
```

Each command prints one JSON object. The first `sync` registers the 13 new spectra and starts their
uploads. The second finishes the uploads: the demo catalog's uploads advance one step per call,
like Globus transfers. `classify` then makes 13 predictions. Points 1 to 3 are DI water (background),
4 to 6 R6G, 7 to 9 methylene blue and 10 to 13 adenine. Points 10 and 13 score below the 0.8
threshold and wait for review. To look at one and decide, put a `prediction_id` from `review-list`
(it looks like `d/453`) in place of `<ID>`, without the angle brackets:

```bash
datalayer review-show --prediction <ID>
datalayer review-set --prediction <ID> --decision confirm --reviewer "quick start"
datalayer provenance --record <ID>
datalayer list --limit 13
```

`--decision relabel --label di_water` records another class; `--decision reject` marks the spectrum
as unusable (the window's button for it is Mark unusable). `datalayer -h` lists every command;
[PROTOCOL.md](data_layer/PROTOCOL.md) section 6 describes them.

## 4. Opening it in Qt Creator

Qt Creator is the editor for the window's C++ code. It builds and starts **the window only**. The
helper programs, the data layer and the data stay in `deploy-macos/`, so run
`./macos-native/build_macos.sh` once before you start. These steps are for the macOS build (on
Windows the helper programs have no simulator, see section 2). They were checked against Qt
Creator 20.0.2's settings files, program files and manual on this Mac, but not clicked through.

1. Open Qt Creator (`~/Qt/Qt Creator.app`).
2. **File > Open File or Project...**, choose `Portable_Raman_DataFed/laser_test/CMakeLists.txt`,
   press Open.
3. On the Configure Project page, tick the kit **Qt 6.12.0 for macOS** and press
   **Configure Project**.
4. Before the first Run: click **Projects** in the left bar. Under Qt 6.12.0 for macOS, click
   **Run**. In the **Environment** section (click **Details** if it is folded), press **Add** or
   **Edit** and enter exactly these two lines, without spaces:

   ```
   RAMAN_BACKEND_SIMULATE=1
   RAMAN_MOTOR_SIMULATE=1
   ```

   The summary under Environment must then read "Set RAMAN_BACKEND_SIMULATE to 1" and "Set
   RAMAN_MOTOR_SIMULATE to 1". A line with the name alone sets the variable to an empty value, and
   the helper programs then drive the instruments.
5. Run the instrument check of [section 2](#2-before-you-run-anything); it must print 0. Then press
   **Run**: the green triangle at the bottom left of the window, or Cmd+R.
6. Check the header: it must say **SIMULATION**. If it shows no badge, or MOTORS ARE REAL, LASER IS
   REAL or SIMULATION NOT CONFIRMED, close the window and correct step 4.

Where it looks for the deploy folder: Qt Creator builds `laser_test.app` in a folder under
`laser_test/build/` named after the kit. That folder has no helper programs, so the window uses the
deploy folder that CMake recorded when the project was configured: `RAMAN_DEFAULT_HOME`, which is
`laser_test/../deploy-macos`, this edition's `deploy-macos/` (`laser_test/CMakeLists.txt`,
`ramanHome()` in `laser_test/main.cpp`). A `RAMAN_HOME` variable that names another deploy folder
comes first. The status bar at the bottom of the window shows the folder in use ("Data folder: ...").

Three ways to start the same window:

| how | which build | instruments |
|---|---|---|
| `deploy-macos/run_simulated.sh` | the Release build `build_macos.sh` put in `deploy-macos/` | simulated |
| Qt Creator's Run | a Debug build from the same source | simulated with step 4, **real without it** |
| `deploy-macos/run_hardware.sh` | the same as the first | real |

Qt Creator keeps the step 4 settings in `laser_test/.qtcreator/`, which is not in git: set them
again on another computer. A change you build in Qt Creator does not reach `deploy-macos/laser_test`
or `raman_sim_runner` until you run `build_macos.sh` again.

## 5. The data flow in five steps

1. **Capture.** The controller saves each spectrum on this Mac, with who, what and where.
2. **Register.** The data layer makes one catalog record per spectrum and uploads the raw file. A
   capture made offline waits on this Mac and is registered at the next sync.
3. **Classify.** A job, run on this Mac or on VT ARC, fetches the spectrum, runs the model and saves
   the prediction as a new record.
4. **Link.** The prediction record is "derived from" the raw spectrum, the model version and the
   dark spectrum.
5. **Review.** A person checks the predictions that are uncertain or rejected.

| step | in the window | in the code | command |
|---|---|---|---|
| Capture | Sample group, Laser ON, Capture Dark, 1D Scan or the single button Start Z scan then 2D scan, Capture Spectrum | `laser_test/main.cpp`: `captureMetadata()`, `publishSessionFile()`, `handleScanPoint()`, `captureSingleSpectrum()`, `captureDarkMeasurement()` | `raman_sim_runner` (simulated) |
| Register | Data tab: runs by itself after each scan and every 30 s while a sync has something to move on ([Offline](#7-going-live-with-datafed)); Sync now | `data_layer/raman_datafed/sync.py`, with `metadata.py`, `ledger.py`, `local_catalog.py`, `datafed_catalog.py` | `sync` |
| Classify | Data tab: Classify now (runs on this Mac) | `job.py`, `classifier.py`; on ARC `arc_job.py` and `data_layer/arc/classify_job.slurm` | `classify` |
| Link | Data tab: select a row; PROVENANCE (DERIVED FROM) on the right | `job.py` saves each prediction with its three links; `commands_core.py` reads the chain | `provenance --record ID` |
| Review | Review tab: Confirm (it names the class it records), Relabel with a class, or Mark unusable; the reviewer is the Operator field | `review.py`; in the window `laser_test/data_panels.h` | `review-list`, `review-show`, `review-set` |

The window starts the data layer as a separate process and never waits for it
(`laser_test/data_layer_client.h`), so a slow catalog cannot hold up a scan. Sync now, Classify
now and Import scan folder stay usable during a scan, so that you can look at the earlier points
of a long scan: a point that is still being written is left for a later sync and is never marked
failed for it. Each capture moves through these states in the local ledger,
`deploy-macos/data_catalog/ledger.sqlite`: `captured`, `registered`, `uploading`, `uploaded`,
`classified`, then `accepted` or `needs_review`, then `reviewed`; a step that fails puts it in
`error` until a later sync retries it. The classifier is the PCA-SVM model in
`models/pca_svm_pure_20260922` with a threshold of 0.8. A prediction is `confident` (a material),
`background` (DI water), `low_confidence` (top score below 0.8, needs review) or `rejected`
(spectrum unusable, or no dark of its kind; needs review). The window and the Studio never show a
score below the threshold rounded up: 0.797 reads 0.797, not 0.80.

## 6. The Simulation Studio

**Opening it.** Press **Open Simulation Studio** at the bottom of the window's left column, or run
`./studio.sh` in `deploy-macos/`. Both start a small server for this deploy folder on this Mac only
(<http://127.0.0.1:8765/>, or the next free port up to 8784) and open the page. The server the window
starts ends by itself after 15 minutes without use; `studio.sh` runs until Ctrl-C. You can also open
`studio/index.html` as a file: it then runs in demo mode only.

**DEMO and LIVE.** The badge at the top says which one you see.

- **DEMO** is a re-enactment built into the page, with example data. Nothing leaves the page. Its
  classifier is a stand-in, and the page says so.
- **LIVE** starts by itself when the page was served by the server. It shows what the real code in
  this deploy folder is doing: the newest scan session (also one running in the window), the
  simulated stage, and the data layer's records. Play starts `raman_sim_runner`, which is always
  simulated; Stop ends it. Sync now, Run classifier, Offline and the review buttons call the data
  layer. Pause, Step, Speed and Reset work in DEMO only. The Demo button switches back.

**The three panels.**

1. **Instrument.** A top view of the 25 mm × 25 mm stage travel with the virtual sample, the planned
   path, the captured points and the probe, which shows a ring while the laser exposes. Insets show
   the 1 mm area of the 2D scan and the stage height against the focus plane. Below: the spectrum on
   a Raman shift axis, and a ticker of what the code is doing (STEP, ACK HOLD, the capture command,
   the point file, ACK OK); in LIVE it shows the log of the last run started with Play, while a
   scan in the window logs to the window's Activity tab. The Sample ID, Operator and Location
   fields, the scan type and the laser power are those of the next run. Ground truth splits each
   point: the plate's material on the left, the prediction on the right. It is shown only for the
   spectra of sessions whose `session.json` says that both helper programs simulated, the session
   on show and the 12 newest. The others, such as a session with the real spectrometer, are left
   out of the count of predictions that agree with it ("N not known to be simulated, left out"),
   and their record says which instrument was real.
2. **Data pipeline.** The five steps as lanes over where they happen: the laptop, DataFed with
   Globus, the ARC cluster and the reviewer. Each square is one spectrum. Select one to see its
   record: ids, aliases, metadata, upload task, and the provenance chain from the prediction back to
   the spectrum, the model and the dark. Here are Sync now, Run classifier, the Offline switch, the
   threshold (read-only in LIVE), Find in metadata, Next to review, and Confirm, Reject and Relabel
   with a reviewer name.
3. **The bigger picture.** The loop of an automated experiment: Plan, Move and acquire, Transfer,
   Record, Analyse, Decide, then the next Plan. Each step names the layer the project's platform
   plan gives it, and counts what has happened there: INTERSECT for planning, the Qt controller to
   move and acquire, Globus to transfer, DataFed to record, the VT ARC cluster to analyse, a person
   to decide.

**What DataFed and Globus add.** Switch on **Without DataFed and Globus** in the third panel to see
the same loop with files in folders: copied by hand, no record ids, no search across sessions,
results that do not say which spectrum, dark or model they came from, and no review queue. In LIVE
the local demo catalog gives the same records, links and review queue on this Mac alone: this
edition's data layer makes them with either catalog. DataFed and Globus make them reachable from
ARC and other people, with file transfers that resume. Globus moves each file off the laptop as a
transfer with a task id that can be checked. DataFed gives each spectrum a record that can be
found by its metadata. An analysis that runs somewhere else, such as on ARC, comes back linked to
exactly the inputs it used, and a person sees what waits for a decision. That is what an automated
experiment needs before the next scan can be planned from the last one without anyone collecting
files by hand. In this edition Plan and Decide are still a person, and the
ARC step has not been run.

## 7. Going live with DataFed

Nothing in this section has been run against DataFed or Globus. No account was used, nobody logged
in, and nothing connected to either. Only the settings commands and `doctor` below were tried, in
copies of the deploy folder, and the offline example in the demo catalog. The DataFed adapter
(`data_layer/raman_datafed/datafed_catalog.py`) is tested against stand-ins only.

**What only you can do.** The data layer never logs in, never asks for a password and never runs
`datafed setup`. These steps are yours:

1. **A Globus account.** DataFed accounts are tied to a Globus identity.
2. **A DataFed account.** You sign in at <https://datafed.ornl.gov> with your Globus identity.
   DataFed's
   [getting started page](https://ornl.github.io/DataFed/system/getting_started.html) describes it.
3. **A storage allocation.** DataFed stores record files in a repository allocation, given to you or
   to a DataFed project by the repository's administrators. Note the project id (`p/...`) if the
   records should go into a project, and the repository id (`repo/...`) if you have more than one
   allocation.
4. **Globus Connect Personal** on this Mac. Install it from
   <https://www.globus.org/globus-connect-personal>, sign in and leave it running. It makes this Mac
   a Globus collection; its collection id (a UUID) is in its preferences and on
   <https://app.globus.org>. It has to be allowed to read `deploy-macos/`, for the uploads, and to
   write `deploy-macos/data_catalog/staging/`, where Classify now and the Review tab download files
   in DataFed mode. In its Preferences, list the deploy folder with writing allowed. macOS may ask
   for permission when the folder is inside a protected folder such as Downloads. None of this was
   tried.
5. **`datafed setup`**, from this folder. It asks for your DataFed user ID and a password, once,
   and keeps key files in `~/.datafed`. Keep them private. The password is DataFed's own, not your
   Globus login: the client sends it to DataFed. How to set one in DataFed's web portal is
   described in DataFed's documentation; that was not tried or checked for this text. If `datafed
   setup` says that a newer DataFed client is available, do not upgrade: the data layer is pinned
   to datafed 5.1.0 (`data_layer/requirements.txt`).

   ```bash
   .venv/bin/datafed setup
   ```

6. **The default endpoint**, with the collection id of step 4:

   ```bash
   .venv/bin/datafed ep default set <collection-id>
   ```

**Switching the edition to DataFed.** Close the window and the Studio first. Then, from
`deploy-macos/` with `datalayer` defined ([Quick start](#3-quick-start-in-simulation)):

```bash
datalayer config --set mode=datafed datafed.context=p/<your-project-id>
datalayer doctor
```

Put your project id in place of `<your-project-id>`, or leave out `datafed.context=...` to use your
own DataFed space. Add `datafed.repo_id=<repository-id>` (it looks like `repo/...`) for an
allocation other than your default one. `doctor` checks without logging in: Python 3.11 or newer,
the datafed package, the two key files in `~/.datafed` (only that they exist), the default
endpoint, Globus Connect Personal installed and running, and the settings, including
`datafed.include_simulated` (below). Each failed check says what to do (`fix`);
`"datafed_ready": true` means every check passed. On this Mac at hand-over, `doctor` found Python
and the datafed package ready and listed the key files, the default endpoint and Globus Connect
Personal as missing. Then:

```bash
datalayer doctor --online
datalayer sync
```

`doctor --online` asks DataFed who you are: it is the first command here that contacts DataFed. In
DataFed mode `status` and `sync` contact it too, and so do the window and the Studio, which run
them. Back to the demo catalog: `datalayer config --set mode=local-demo`.

Until the first sync after a switch, the Data tab marks its rows as those of the catalog used
before: the column header reads "State (previous catalog)", the record ids move into the tool
tips, and the DATA pill says "not synced yet". The window runs that sync by itself within 30 s,
and it registers every capture in the catalog now in use.

**Simulated captures stay on this Mac.** At the first sync after the switch, the real captures in
`deploy-macos/scan_sessions/` are registered in DataFed. Captures made in simulation are not: they
stay on this Mac, are not marked failed and use up none of their attempts. `datalayer status` and
`datalayer sync` count them in `held_simulated` and add "N simulated capture(s) stay on this Mac
and are not sent to DataFed: to send them as well, run config --set
datafed.include_simulated=true."; the window's Data tab says "N simulated captures stay on this
Mac: DataFed gets only real captures." To register them as well:

```bash
datalayer config --set datafed.include_simulated=true
```

The next sync picks them up; their records say simulated. `datafed.include_simulated=false` holds
the ones not yet registered back again. The setting takes `true` or `false` only. A session added
with `import-session` for which nobody recorded whether it was simulated is registered either way,
and the local demo catalog registers everything, whatever the setting says. Before hand-over, the
simulated data of this edition's rehearsal and checks was moved out of `deploy-macos/`, so the
deploy folder holds none of it.

**What goes to DataFed.** Everyone who can read the records sees all of this: the members of your
DataFed project, or only you in your own space.

- **Each spectrum:** a record with the whole `point_NNNN.json` file (intensities, positions, times)
  and its metadata: Sample ID, Operator, Location, Notes, laser power, the instrument (model,
  serial number, unit, firmware), the session id and folder name (the folder name holds the local
  start time), point number, position, times, the simulated flag and the software version.
- **Each dark spectrum:** its `.txt` file, whose header holds the local time it was taken, with
  its checksum and the simulated flag.
- **Each model version:** a zip of the four model files, and the content of `meta.json`.
- **Each prediction:** label, scores, outcome and reason, the host name of the computer that
  computed it (on ARC the node and the Slurm job number), and the versions of Python, numpy,
  scikit-learn and joblib.
- **Each review:** the reviewer's name (the Operator field) and comment.
- **The path of every uploaded file on this Mac,** your macOS account name included (for example
  `/Users/<account>/.../deploy-macos/scan_sessions/<folder>/point_0001.json`): DataFed keeps it as
  the record's source.

The collections are under the root alias `raman-portable`, with one collection per scan session
for its spectra and one for its predictions ([PROTOCOL.md](data_layer/PROTOCOL.md) section 5.2).

**Offline.** When DataFed cannot be reached, captures wait on this Mac in the ledger.
`datalayer status` then names the problem (in DataFed mode "DataFed cannot be reached (...)", in
the demo "The catalog cannot be reached.") and adds "N capture(s) wait for the next sync.". The
window's DATA pill shows how many a sync can move on ("N to sync") and, as "N stuck", what no sync
moves by itself: a capture without its Sample ID, Operator or Location (Import scan folder), or a
capture or review that failed 5 times (Sync now). The window syncs every 30 s while a sync has
something to move on (status `sync_work`). With DataFed it looks for predictions made elsewhere,
such as by the ARC job, only every 5 minutes: press Sync now to look at once. Captures held back,
missing their fields or past their 5 attempts do not keep it syncing. The first sync that reaches
DataFed registers and uploads what waited. Uploads are Globus transfers that run on their own; no
command waits for one, and the next sync picks up the result. A review recorded offline is queued
and sent by the next sync; until then the Review tab says it is kept on this Mac, and afterwards
when it was sent. A capture that fails is tried again by later syncs, 5 times; Sync now (`sync
--retry-failed`) tries again after that. You can watch all this in the demo catalog, from `deploy-macos/`:

```bash
datalayer offline --on
./raman_sim_runner --mode single --sample-id SIM-OFFLINE --operator "quick start" --location simulation --power MEDIUM
datalayer sync
datalayer offline --off
datalayer sync
datalayer sync
```

The first sync finds 1 capture and reports that the catalog cannot be reached; after `offline --off`
the next two register and upload it. The window's Go offline button and the Studio's Offline switch
do the same as `offline --on`.

**Time limits.** In DataFed mode, Classify now fetches each file back through Globus, as the ARC job
would, and the Review tab does the same for a file that is not on this Mac. The window stops a
Classify now after 10 minutes and a sync after 3 minutes ("The data layer did not answer in time").
What was done until then is kept: press it again, or run `datalayer classify` in Terminal, which has
no overall limit. For large scans, such as a 2D scan of 124 spectra, use the ARC job. Not verified.

**Not verified, to watch on the first real sync:** the length limit DataFed puts on titles; that
DataFed drops metadata keys set to null; that `root` works as the parent inside a project; how
long a dropped connection takes to be noticed (each command opens its own connection);
the datafed package's check for a newer version on PyPI when it connects; that Globus Connect
Personal may read the deploy folder and write into its `data_catalog/staging/`; how many items a
DataFed collection may hold (predictions go into one collection per scan session, while the
collections of sessions and darks grow by one item per session or per dark); and the number of records your
allocation allows, against two records per spectrum (the spectrum and its prediction).

## 8. The classification job on ARC

[data_layer/arc/README.md](data_layer/arc/README.md) explains the job step by step: an ARC account
and allocation, a `/projects` directory shared through Globus, the files, the Python environment,
DataFed on ARC, one transfer by hand, the batch script `classify_job.slurm`, and how to read its
output. The job takes the spectra waiting in DataFed, classifies them on a compute node and saves
each prediction with its three links; the next sync on this Mac picks the predictions up (the
window looks for them within 5 minutes, Sync now at once). It downloads each spectrum on its own
through Globus: at least 4 s per spectrum, probably 10 to 30 s (not measured). The script's 30
minutes therefore suit about 50 spectra; that README says how to set the number or the time.

Not verified: nothing was run on ARC, under Slurm, against DataFed or with Globus. The job's logic
was tested on this Mac against the demo catalog and an in-memory stand-in for DataFed. The batch
script's shell logic was tried with stand-in commands only. Three things the public documentation
does not settle: whether the ARC Globus collection shows your project directory under the same path
as the cluster, whether DataFed may transfer files to that collection for you, and whether compute
nodes may connect to `datafed.ornl.gov` on port 7512. Cluster, partition and module names were read
from ARC's documentation on 2026-11-13.

## 9. Real instruments

This edition has only been run in simulation. No build, test or run touched an instrument.

1. Read the safety notes first: [macos-native/README.md](macos-native/README.md) (Safety stop; What
   is verified and what is not) and `../INSTALL_AND_RUN_NOTES.txt` of the original project. Stop
   does not switch the laser off.
2. Try the check tool in simulation, from `deploy-macos/`:

   ```bash
   ./hardware_check.sh all --simulate --dark
   ```

3. With the instruments plugged in, run the checks that cannot move a motor or switch the laser on,
   one at a time: `./hardware_check.sh detect` (reads the USB registry only), then
   `./hardware_check.sh motors`, then `./hardware_check.sh spectrometer --dark` (takes one frame only
   after the laser has read back as disabled twice). These were not run for this edition.
4. Then start the window with the instruments: `./run_hardware.sh`. Its header shows no
   SIMULATION badge. Treat this first session as a supervised test: laser safety, clearance for the
   20 mm Z move.

What is different with real instruments in this edition:

- **Capture a real dark before every real scan**, with the optical path blocked. A real scan is
  paired with the newest real dark in `deploy-macos/dark_measurements/` (a dark without a `.json`
  counts as real), and a simulated dark is never used for it. The title row of the Capture group
  names the dark the next scan will use; `no real dark yet` means that it would have none, and the
  classifier would reject its spectra. The window does not ask for a fresh dark. A fresh one also
  covers a simulated dark whose save was cut short: Capture Dark writes the dark's `.txt` before
  its `.json`, and a simulated dark left without its `.json` by a crash, a kill or a power cut in
  between counts as real.
- At start-up the capture backend reads the module's identity with two read-only calls (those of
  its menu item 1) and prints a `MODULE_INFO` line. Only the simulated device has answered them so far.
- The `simulated` flag of every record follows each helper program's own rule for choosing its
  simulator, so a real run is recorded as real ([PROTOCOL.md](data_layer/PROTOCOL.md) section 2.2).
- In DataFed mode, the Operator and Location you type go into every record, and the instrument's
  serial number with them ([What goes to DataFed](#7-going-live-with-datafed)).

## 10. The older platform bridges

The original project also holds three bridges in `../laser_test/`: an INTERSECT service, a Bluesky
plan and a Globus demo. The INTERSECT service and the Bluesky plan start the existing scans of the
Windows controller through a file mailbox and classify the saved points with the same PCA-SVM model
as this edition (identical model files). The Globus demo moves a Globus tutorial file between two
test collections; it never sees scan output. None of the three is part of this edition, and none
starts on this Mac as shipped: their Python packages are not installed and their launchers are
Windows programs. This edition's data layer does the job the Globus demo only sketched: in DataFed
mode every raw file is to reach DataFed through a Globus transfer, with a record, provenance links,
an offline waiting list, a classifier job and a review queue (not yet run against the services). A
review of the bridges drove a scratch copy of the Mac controller through the mailbox in simulation;
for this edition the mailbox code would also have to move into `laser_test/main.cpp`, and the
remote start request would have to carry Sample ID, Operator and Location, which it does not today.

## 11. Folder map

```
Portable_Raman_DataFed/
  README.md, VERIFICATION.json   this file; what was tested
  laser_test/                    the Qt controller and its CMakeLists.txt
    main.cpp                     a 2048-value Raman shift calibration table, then MainWindow and main()
    window_theme.h               the window's look
    window_status.h              its header and status line
    data_layer_client.h          starts the data layer
    data_panels.h                the Data and Review tabs
    tests/                       handshake_tests, settings_tests, ui_tests, ui_snapshot, emulators
  macos-native/                  build_macos.sh, README.md, hardware_check.py
                                 PORT_SPEC.md: the file protocols between the controller and the helpers
    backend/                     capture backend (C#/.NET, Hamamatsu library on a libusb shim) and its tests
    motor/                       motor program (Python, Thorlabs KDC101) and its tests
    sim_runner/                  raman_sim_runner and its tests
    e2e/                         end-to-end test of the simulated pipeline
    tools/                       helper scripts of the Mac port
  data_layer/                    PROTOCOL.md (the contract), requirements.txt, setup_env.sh
    raman_datafed/               the Python package: commands, catalogs, classifier, Studio server
    tests/                       its tests
    arc/                         ARC batch job and its README
  models/pca_svm_pure_20260922/  the classifier (scikit-learn files and meta.json)
  virtual_sample/                the simulated plate, its reference spectra and README
  studio/                        Simulation Studio page and its tests
```

Made on this Mac and not in git: `deploy-macos/`, `build-macos-flat/`, `laser_test/tests/build-macos/`,
`macos-native/sim_runner/build/`, `macos-native/e2e/build/` and the backend's `bin/` and `obj/` (by
the build and the tests), `.build/` (test logs and scratch builds), `.venv/` (by
`data_layer/setup_env.sh`), `laser_test/build/` and `laser_test/.qtcreator/` (by Qt Creator).

The deploy folder, `deploy-macos/`:

```
laser_test, run_simulated.sh, run_hardware.sh   the window and its two start scripts
raman_sim_runner, studio.sh                      the simulated runner; the Studio server for this folder
handshake_v2/backend/, handshake_v2/motor/       the helper programs
data_layer/                                      raman_datafed/, arc/, requirements.txt
datafed_python.txt, raman_python.txt             interpreters: data layer (.venv), analysis (Anaconda)
models/, virtual_sample/, studio/index.html      classifier, simulated plate, Studio page
hardware_check.sh, hardware_check.py             first-contact instrument checks
cnn_1.py, ckpts/, best_raman_1dcnn.pth, ...      copied from the original project for Analyze Latest (CNN)
scan_sessions/, dark_measurements/               your data: one folder per scan; per dark a .txt and its .json
data_catalog/, datafed_config.json               the ledger and demo catalog; the data-layer settings
laser_log.txt                                    the controller's log
set_aside_<date>_<time>/                         data you set aside (Troubleshooting); nothing reads it
```

## 12. Troubleshooting

- **The Capture group says "no simulated dark yet" or "no real dark yet".** The next scan would
  have no dark of its own kind, and the classifier would reject its spectra. Block the optical path
  and press Capture Dark. A dark of the other kind is never used: such a scan has no dark, and its
  predictions are rejected with a reason such as "dark not used: the dark was taken in simulation;
  this spectrum is real". The runner always captures its own dark.
- **status says a dark spectrum file could not be uploaded because it is missing or changed.** Put
  the file back into `dark_measurements/` unchanged and press Sync now (`sync --retry-failed`).
  If it stays missing, after 5 tries (`sync.max_attempts`) the dark's record is marked unavailable,
  and the spectra taken with it are rejected ("the dark spectrum ... was never uploaded") and go to
  the review list. A spectrum is classified once, so these stay rejected even if the file comes
  back later; a person decides in the Review tab.
- **A capture's Error says "... holds a broken character".** Its point file holds half of a
  character, such as an emoji cut in two by a program that wrote the file; the window no longer
  writes one. Such text cannot be stored in a catalog, and Import scan folder keeps the values
  already in the file. Correct the named field in that `point_NNNN.json` with a text editor (the
  broken half shows as an escape such as `\ud83d`); the next sync reads the file again.
- **Classify says spectra were skipped or left for later.** Their uploads have not finished. Sync once
  more (Sync now, or wait 30 s in the window), then classify again.
- **"The data layer did not answer in time" after Classify now or Sync now.** The window stops a
  Classify now after 10 minutes and a sync after 3 minutes; what was done is kept. See [Time
  limits](#7-going-live-with-datafed).
- **The Studio's Play says the run did not start: the controller lock is held.** The window, or another
  run, is open in this deploy folder. Close it or wait for it. When it says instead that
  `controller.lock` cannot be created, the deploy folder is not writable: fix its permissions.
- **"Controller already running" or "Original controller running".** Another window of this edition,
  a runner, or the original controller is open. See [Two locks](#1-what-this-edition-is).
- **"Controller lock not available" or "Original controller lock not available".** The named lock
  file cannot be created, for example because the folder is not writable. The window does not start
  then, because it could not keep the other controller out. Fix the folder's permissions.
- **The header says MOTORS ARE REAL, LASER IS REAL or SIMULATION NOT CONFIRMED.** One simulation
  variable is missing or is not exactly `1`. Close the window and set both.
- **A 1D Y scan after a 1D X scan finds only DI water.** The simulated stage stays where the last scan
  left it (x = 24 mm). Delete `deploy-macos/scan_sessions/_sim_stage.json` to send it back to the
  start position. The runner does this itself unless you add `--keep-stage`.
- **The Data tab says "The data layer is not installed".** `deploy-macos/data_layer/raman_datafed/`
  is missing: run `./macos-native/build_macos.sh`.
- **"The data layer could not be started", or studio.sh says "No data-layer interpreter".** The first
  line of `deploy-macos/datafed_python.txt` must name `.venv/bin/python` of this folder.
  `data_layer/setup_env.sh` makes that environment; it downloads packages from PyPI.
- **The build stops: wrrusb2a.dll is missing.** See
  [macos-native/backend/lib/README.md](macos-native/backend/lib/README.md).
- **Port 8765 is in use.** The Studio server takes the next free port up to 8784. The first line it
  prints names the address.
- **Disk use.** Every start of the window or the runner leaves a `scan_sessions/_backend_<id>` folder
  (about 1.3 MB after a 2D scan), and every hardware check a `_hwcheck_<time>` folder
  (`build_macos.sh --test` keeps its own in `.build/test-logs/hardware_check_deploy/`). Both can be
  deleted while no controller runs.
- **Start with an empty data folder.** Quit the window, the Studio and any runner first. This moves
  every scan, dark, catalog entry and setting of the deploy folder, and the log, into a new folder
  `set_aside_<date>_<time>` inside `deploy-macos/`; the programs make new ones:

  ```bash
  cd deploy-macos
  stamp=$(date +%Y%m%d_%H%M%S); mkdir "set_aside_$stamp"
  mv scan_sessions dark_measurements data_catalog datafed_config.json laser_log.txt "set_aside_$stamp/"
  ```

  `mv` names any of the five that does not exist and moves the others all the same. The new folder
  stays inside `deploy-macos/`, which git ignores; the controller, the data layer, the Studio and
  `cnn_1.py` never read it. Delete it once you are sure you do not need it. Copy a dark back into
  `dark_measurements/` together with its `.json`: a simulated dark without one counts as real.

## 13. What was verified

On this Mac, with nothing attached, both simulation variables at `1` and the local demo catalog:

- The build and every test suite of `build_macos.sh --test`, except the two device self-tests:
  every suite passed, in about 11 minutes, also in the two full runs made after the first round of
  review fixes and in the one made after the second round and the changes that completed it.
- A rehearsal from `deploy-macos/`: a 1D X scan (13 spectra, 2 to review), a single capture made
  offline and registered on reconnect, a 2D scan (124 spectra, 5 to review), reviews online and
  offline, provenance, the Studio server's API, and the Studio page in a browser with the real runner,
  including Play, Stop and a run refused by the lock.
- The window's code through its test programs, offscreen, against a stand-in data layer
  (`laser_test/tests/fake_data_layer.py`). Against the real data layer it ran only through scratch
  drivers: in the rehearsal, before the review fixes, and after them for a sync, Classify now and
  reviews made offline and sent by a later sync, and for a change of catalog settings.
- The pairing of darks by kind and the holding back of simulated captures in DataFed mode, in the
  test programs of the controller, the runner, the end-to-end run and the data layer (DataFed
  against stand-ins only).
- This README's commands, run as written, with `TMPDIR` in a scratch folder,
  `PYTHONDONTWRITEBYTECODE=1` and `DATAFED_SERVER_HOST=127.0.0.1` also set: the instrument check,
  the build, the tests, the runner, the data-layer commands, the offline demo, `hardware_check.sh
  all --simulate --dark`, and `studio.sh --no-open`. The `config --set` and `doctor` commands of
  section 7 and the move of the troubleshooting list were run in scratch copies of
  `deploy-macos/`, so they changed nothing in the deploy folder.
- The checks after each round of review fixes, in scratch copies of `deploy-macos/`: the
  runner's 1D X scan with sync, sync, classify and review-list (13 predictions, points 10 and 13 to
  review); a dark without a `.json`, so of the real kind, dated later than the simulated ones,
  which a new simulated session did not use; `status` in DataFed mode holding the simulated
  captures back, with the DataFed client kept from loading; and the Studio server starting a single
  capture and stopping a 1D scan.

Before hand-over, the simulated data of the rehearsal and checks (scan folders, darks, the demo
catalog with its ledger, and the log) was moved out of `deploy-macos/`; `status` there then
reported an empty local demo catalog.

Not verified: live DataFed, any Globus transfer, the ARC job, real instruments, Windows, the two
device self-tests, the window started through `run_simulated.sh` or Qt Creator, and the second lock
against the original controller. Not run from this README: `run_simulated.sh`, Qt Creator,
`studio.sh` opening a browser, the commands that need your DataFed and Globus accounts, the
hardware checks without `--simulate`, `run_hardware.sh` and `setup_env.sh`.
[VERIFICATION.json](VERIFICATION.json) has the counts, times and commands.
