# Portable Raman controller, catalog edition

This folder is a copy of the Portable Raman controller with a data pipeline added. Every saved
spectrum gets a catalog record. A classifier result is saved as its own record, linked to the
spectrum, the model version and the dark spectrum it came from. Uncertain results wait for a
person to review them. The catalog is kept on this Mac, in the deploy folder. A
finished scan session can also be uploaded whole to the team folder on ARC, by Globus or SSH;
besides the demo, this was tried live once each way with simulated scans
([section 14](#14-uploading-sessions-to-arc)).

Everything here was built and run on this Mac **in simulation**, apart from one start of
`run_hardware.sh` with no instrument attached. No instrument was attached. The catalog sends
nothing anywhere; two simulated scan sessions went to ARC, one over SSH and one with Globus
([section 13](#13-what-was-verified)). What was tested, with counts, is in
[VERIFICATION.json](VERIFICATION.json).

1. [What this edition is](#1-what-this-edition-is)
2. [Before you run anything](#2-before-you-run-anything)
3. [Quick start in simulation](#3-quick-start-in-simulation)
4. [Opening it in Qt Creator](#4-opening-it-in-qt-creator)
5. [The data flow in five steps](#5-the-data-flow-in-five-steps)
6. [The Simulation Studio](#6-the-simulation-studio)
7. [The catalog on this Mac](#7-the-catalog-on-this-mac)
8. [Offline, retries and time limits](#8-offline-retries-and-time-limits)
9. [Real instruments](#9-real-instruments)
10. [The older platform bridges](#10-the-older-platform-bridges)
11. [Folder map](#11-folder-map)
12. [Troubleshooting](#12-troubleshooting)
13. [What was verified](#13-what-was-verified)
14. [Uploading sessions to ARC](#14-uploading-sessions-to-arc)

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
- **Uploads to ARC.** A finished scan session can go to the team folder on VT ARC by Globus or SSH:
  a copy of the session folder, its dark spectrum, a manifest and a list of checksums, checked on
  ARC. The Data tab's **ARC upload** button shows it. Tried live once each way with simulated
  scans; everything else in the demo only ([section 14](#14-uploading-sessions-to-arc)).

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

Run the commands in Terminal from this folder, `Portable_Raman_Catalog/`, unless they start with
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
`data_layer/setup_env.sh`, which downloads the data layer's packages from PyPI (about 230 MB),
the Globus SDK for uploads to ARC included. An existing `.venv/` is left as it is: if the build
says that the Globus SDK is not in `.venv`, run `./data_layer/setup_env.sh` once from this folder
to add it. Only live Globus uploads need it.

**Test** (optional, about 15 minutes):

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
datalayer() { PYTHONPATH="$PWD/data_layer" "$(head -n 1 catalog_python.txt)" -m raman_catalog --home "$PWD" "$@"; }
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

**Try an upload to ARC (demo).** Nothing leaves this Mac: a folder in `deploy-macos/` stands in for
ARC. After a scan has ended, in the window's Data tab press **ARC upload**. Leave **Demo (no ARC, no
account)** ticked, choose the session, and press **Upload to ARC**. Under Recent uploads the State
goes from Waiting to start to **On ARC, checked** in a few seconds. Press **Show in Finder** to see
the copy. The same from the command line, with `datalayer` defined as above (put a folder name from
the first command's `items` in place of `<folder>`):

```bash
datalayer arc-sessions
datalayer arc-upload --session <folder> --demo
datalayer arc-list
```

The copy lands in `deploy-macos/data_catalog/arc_demo/projects/pcl_photonics/raman_globus_upload/`,
in `simulated/` for a simulated session. [Section 14](#14-uploading-sessions-to-arc) has the real
uploads.

## 4. Opening it in Qt Creator

Qt Creator is the editor for the window's C++ code. It builds and starts **the window only**. The
helper programs, the data layer and the data stay in `deploy-macos/`, so run
`./macos-native/build_macos.sh` once before you start. These steps are for the macOS build (on
Windows the helper programs have no simulator, see section 2). They were checked against Qt
Creator 20.0.2's settings files, program files and manual on this Mac, but not clicked through.

1. Open Qt Creator (`~/Qt/Qt Creator.app`).
2. **File > Open File or Project...**, choose `Portable_Raman_Catalog/laser_test/CMakeLists.txt`,
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
3. **Classify.** A job, run on this Mac, fetches the spectrum from the catalog, runs the model and
   saves the prediction as a new record.
4. **Link.** The prediction record is "derived from" the raw spectrum, the model version and the
   dark spectrum.
5. **Review.** A person checks the predictions that are uncertain or rejected.

| step | in the window | in the code | command |
|---|---|---|---|
| Capture | Sample group, Laser ON, Capture Dark, 1D Scan or the single button Start Z scan then 2D scan, Capture Spectrum | `laser_test/main.cpp`: `captureMetadata()`, `publishSessionFile()`, `handleScanPoint()`, `captureSingleSpectrum()`, `captureDarkMeasurement()` | `raman_sim_runner` (simulated) |
| Register | Data tab: runs by itself after each scan and every 30 s while a sync has something to move on ([Offline](#8-offline-retries-and-time-limits)); Sync now | `data_layer/raman_catalog/sync.py`, with `metadata.py`, `ledger.py`, `catalog.py`, `local_catalog.py` | `sync` |
| Classify | Data tab: Classify now (runs on this Mac) | `job.py`, `classifier.py` | `classify` |
| Link | Data tab: select a row; PROVENANCE (DERIVED FROM) on the right | `job.py` saves each prediction with its three links; `commands_core.py` reads the chain | `provenance --record ID` |
| Review | Review tab: Confirm (it names the class it records), Relabel with a class, or Mark unusable; the reviewer is the Operator field | `review.py`; in the window `laser_test/data_panels.h` | `review-list`, `review-show`, `review-set` |
| Archive: the session, its dark spectrum and a checked manifest go to the team folder on ARC ([section 14](#14-uploading-sessions-to-arc)) | Data tab: ARC upload, then Upload to ARC | `arc_commands.py`, `arc_worker.py`, `arc_globus.py`, `arc_ssh.py`, `arc_remote.sh`; in the window `laser_test/arc_panel.h` | `arc-sessions`, `arc-upload`, `arc-list` |

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
2. **Data pipeline.** The five steps as lanes over where they happen. Each square is one spectrum. Select one to see its
   record: ids, aliases, metadata, upload task, and the provenance chain from the prediction back to
   the spectrum, the model and the dark. Here are Sync now, Run classifier, the Offline switch, the
   threshold (read-only in LIVE), Find in metadata, Next to review, and Confirm, Reject and Relabel
   with a reviewer name. Below the lanes, the **Upload to ARC** strip draws each upload as a parcel
   that goes from the laptop through Globus or over SSH to ARC, waits in `_incoming` while it is
   checked, and is renamed into place. In DEMO every scan that ends is uploaded to the demo ARC
   folder (switch off Upload after each scan to stop that); Drop the connection shows what happens
   when the connection breaks, and Retry same request finishes the upload. In LIVE the strip shows
   this deploy folder's uploads, the window's included, and its Upload to ARC (demo) button starts
   demo uploads only: uploads to ARC itself are made in the window. The window's uploads to ARC
   itself are drawn read-only; demo uploads, the window's included, can be stopped, retried and
   checked from the strip, since an upload's record does not say where it was started. An upload
   whose last Check status did not find its copy is drawn as not checked. A Check status that could
   not look (a sign-in that did not happen, for example) changes nothing: the line says why and what
   to do.
3. **The bigger picture.** The loop of an automated experiment: Plan, Move and acquire, Transfer,
   Record, Analyse, Decide, then the next Plan. Each step names the layer the project's platform
   plan gives it, and counts what has happened there.

**What the catalog adds.** The switch in the third panel shows the same loop with files in
folders and no catalog: copied by hand, no record ids, no search across sessions, results that do
not say which spectrum, dark or model they came from, and no review queue. In LIVE the catalog
gives records, links and a review queue on this Mac: each spectrum has a record that can be found
by its metadata, a prediction is linked to exactly the inputs it used, and a person sees what
waits for a decision. In this edition Plan and Decide are still a person.

## 7. The catalog on this Mac

The catalog of this edition is a folder of plain files in the deploy folder,
`deploy-macos/data_catalog/local_demo/`. The window, the Studio and the commands call it the local
demo catalog. Nothing in it is sent anywhere, there is no account and nothing to sign in to. The
only part of this edition that sends data off this Mac is an upload to ARC
([section 14](#14-uploading-sessions-to-arc)).

**What the catalog holds.** Everyone who can read the deploy folder can read all of this.

- **Each spectrum:** a record with a copy of the whole `point_NNNN.json` file (intensities,
  positions, times) and its metadata: Sample ID, Operator, Location, Notes, laser power, the
  instrument (model, serial number, unit, firmware), the session id and folder name (the folder
  name holds the local start time), point number, position, times, the simulated flag and the
  software version.
- **Each dark spectrum:** its `.txt` file, whose header holds the local time it was taken, with
  its checksum and the simulated flag.
- **Each model version:** a zip of the four model files, and the content of `meta.json`.
- **Each prediction:** label, scores, outcome and reason, the host name of the computer that
  computed it, and the versions of Python, numpy, scikit-learn and joblib.
- **Each review:** the reviewer's name (the Operator field) and comment.

Simulated and real captures are both registered; a simulated one says so in its record and in its
title. The collections are under the root alias `raman-portable`, with one collection per scan
session for its spectra and one for its predictions ([PROTOCOL.md](data_layer/PROTOCOL.md) section
5.2). Beside the catalog, `deploy-macos/data_catalog/ledger.sqlite` keeps the state of every
capture. Deleting `data_catalog/local_demo/` and `ledger.sqlite` starts the catalog from empty; the
next sync registers every capture in `scan_sessions/` again.

**Settings.** They are in `deploy-macos/catalog_config.json`, written with its defaults on first
use ([PROTOCOL.md](data_layer/PROTOCOL.md) section 5.1 lists the keys). From `deploy-macos/` with
`datalayer` defined ([Quick start](#3-quick-start-in-simulation)):

```bash
datalayer config
datalayer config --set review.confidence_threshold=0.7
datalayer doctor
```

`config` shows the settings and `config --set key=value` changes one or more. `doctor` checks that
Python is 3.11 or newer and that the settings are valid; each failed check says what to do (`fix`),
and `"ready": true` means every check passed. The checks for uploads to ARC are `arc-doctor`'s
([section 14](#14-uploading-sessions-to-arc)).

**A deploy folder from the earlier edition.** The earlier edition, which used an external catalog
service, kept its settings in another file of the deploy folder, and this edition does not read
it. To keep the ARC settings of such a folder, copy the `arc_upload`, `review`, `classifier` and
`sync` groups of the old settings file into `catalog_config.json`, and put the old `root_alias`
under `"catalog"`. The old file, the old interpreter note and the old package folder under
`data_layer/` can then be deleted; the build does not remove them. The ledger and the catalog
folder are used as they are.

**Another root alias.** Close the window and the Studio first, then set `catalog.root_alias` to
another name. The catalog then starts a second tree of collections under that name; the first one
stays where it is. Until the first sync after the change, the Data tab marks its rows as those of
the catalog used before: the column header reads "State (previous catalog)", the record ids move
into the tool tips, and the DATA pill says "not synced yet". The window runs that sync by itself
within 30 s, and it registers every capture under the root alias now in use.

## 8. Offline, retries and time limits

**Offline.** The catalog is on this Mac, so it is always within reach. To show what a catalog out
of reach looks like, it has a switch: while it is on, every call to the catalog fails as if the
connection were lost, and captures wait on this Mac in the ledger. `datalayer status` then says
"The catalog cannot be reached." and adds "N capture(s) wait for the next sync.".
The window's DATA pill shows how many a sync can move on ("N to sync") and, as "N stuck", what no
sync moves by itself: a capture without its Sample ID, Operator or Location (Import scan folder),
or a capture or review that failed 5 times (Sync now). The window syncs every 30 s while a sync
has something to move on (status `sync_work`). Captures missing their fields or past their 5
attempts do not keep it syncing. The first sync after the switch is off registers and uploads what
waited. An upload into the catalog finishes on its own; no command waits for one, and the next
sync picks up the result. A review recorded offline is queued and sent by the next sync; until
then the Review tab says it is kept on this Mac, and afterwards when it was recorded. From
`deploy-macos/`:

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

**Retries.** A capture that fails is tried again by later syncs, 5 times (`sync.max_attempts`);
Sync now (`sync --retry-failed`) tries again after that.

**Time limits.** The window stops a Classify now after 10 minutes and a sync after 3 minutes ("The
data layer did not answer in time"). What was done until then is kept: press it again, or run
`datalayer classify` in Terminal, which has no overall limit.

## 9. Real instruments

This edition has run only in simulation, apart from one start of `run_hardware.sh` with no
instrument attached (the capture backend stopped with "Error code 0x0003: Dll Can not be
Initialized"). No build, test or run touched an instrument.

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
- The Operator and Location you type go into every record, and the instrument's serial number
  with them ([What the catalog holds](#7-the-catalog-on-this-mac)); an upload to ARC carries them
  too ([section 14](#14-uploading-sessions-to-arc)).

## 10. The older platform bridges

The original project also holds three bridges in `../laser_test/`: an INTERSECT service, a Bluesky
plan and a Globus demo. The INTERSECT service and the Bluesky plan start the existing scans of the
Windows controller through a file mailbox and classify the saved points with the same PCA-SVM model
as this edition (identical model files). The Globus demo moves a Globus tutorial file between two
test collections; it never sees scan output. None of the three is part of this edition, and none
starts on this Mac as shipped: their Python packages are not installed and their launchers are
Windows programs. This edition's data layer does more than the Globus demo sketched: every raw
file gets a record in the catalog on this Mac, with provenance links, an offline waiting list, a
classifier job and a review queue, and a finished session can go to ARC with Globus
([section 14](#14-uploading-sessions-to-arc)). A
review of the bridges drove a scratch copy of the Mac controller through the mailbox in simulation;
for this edition the mailbox code would also have to move into `laser_test/main.cpp`, and the
remote start request would have to carry Sample ID, Operator and Location, which it does not today.

The team has also used a separate tool, run by hand, that copies scan folders to the team folder on
ARC with Globus or SSH. The Data tab's ARC upload does that job inside this edition and adds what
the tool left to the person: the session's dark spectrum goes with it; simulated sessions stay apart
(in `simulated/`, and only when allowed); only sessions that have ended go, and the manifest says
whether the scan finished, stopped early or failed; SSH asks for the password and Duo once per
attempt; every file is checked on ARC before the folder gets its name; nothing on ARC is replaced;
and every upload keeps a record on this Mac
([section 14](#14-uploading-sessions-to-arc)).

## 11. Folder map

```
Portable_Raman_Catalog/
  README.md, VERIFICATION.json   this file; what was tested
  laser_test/                    the Qt controller and its CMakeLists.txt
    main.cpp                     a 2048-value Raman shift calibration table, then MainWindow and main()
    window_theme.h               the window's look
    window_status.h              its header and status line
    data_layer_client.h          starts the data layer
    data_panels.h                the Data and Review tabs
    arc_panel.h                  the ARC page of the Data tab (uploads to ARC)
    tests/                       handshake_tests, settings_tests, ui_tests, arc_ui_tests, ui_snapshot, emulators
  macos-native/                  build_macos.sh, README.md, hardware_check.py
                                 PORT_SPEC.md: the file protocols between the controller and the helpers
    backend/                     capture backend (C#/.NET, Hamamatsu library on a libusb shim) and its tests
    motor/                       motor program (Python, Thorlabs KDC101) and its tests
    sim_runner/                  raman_sim_runner and its tests
    e2e/                         end-to-end test of the simulated pipeline
    tools/                       helper scripts of the Mac port
  data_layer/                    PROTOCOL.md (the contract), requirements.txt, requirements-mac.txt, setup_env.sh
    raman_catalog/               the Python package: commands, catalogs, classifier, Studio server,
                                 uploads to ARC (arc_*.py, arc_remote.sh)
    tests/                       its tests
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
data_layer/                                      raman_catalog/, requirements.txt, requirements-mac.txt
catalog_python.txt, raman_python.txt             interpreters: data layer (.venv), analysis (Anaconda)
models/, virtual_sample/, studio/index.html      classifier, simulated plate, Studio page
hardware_check.sh, hardware_check.py             first-contact instrument checks
cnn_1.py, ckpts/, best_raman_1dcnn.pth, ...      copied from the original project for Analyze Latest (CNN)
scan_sessions/, dark_measurements/               your data: one folder per scan; per dark a .txt and its .json
data_catalog/, catalog_config.json               the ledger and demo catalog; the data-layer settings
data_catalog/arc_uploads/                        one folder per upload to ARC: its record, log and copy
data_catalog/arc_demo/                           the demo's stand-in for ARC
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
  limits](#8-offline-retries-and-time-limits).
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
- **The Data tab says "The data layer is not installed".** `deploy-macos/data_layer/raman_catalog/`
  is missing: run `./macos-native/build_macos.sh`.
- **"The data layer could not be started", or studio.sh says "No data-layer interpreter".** The first
  line of `deploy-macos/catalog_python.txt` must name `.venv/bin/python` of this folder.
  `data_layer/setup_env.sh` makes that environment; it downloads packages from PyPI.
- **The build stops: wrrusb2a.dll is missing.** See
  [macos-native/backend/lib/README.md](macos-native/backend/lib/README.md).
- **Port 8765 is in use.** The Studio server takes the next free port up to 8784. The first line it
  prints names the address.
- **Disk use.** Every start of the window or the runner leaves a `scan_sessions/_backend_<id>` folder
  (about 1.3 MB after a 2D scan), and every hardware check a `_hwcheck_<time>` folder
  (`build_macos.sh --test` keeps its own in `.build/test-logs/hardware_check_deploy/`). Both can be
  deleted while no controller runs.
- **Start with an empty data folder.** Quit the window, the Studio and any runner first, and wait
  until no upload to ARC is working (the ARC page of the Data tab shows none). This moves every scan,
  dark, catalog entry, upload record and setting of the deploy folder, and the log, into a new folder
  `set_aside_<date>_<time>` inside `deploy-macos/`; the programs make new ones:

  ```bash
  cd deploy-macos
  stamp=$(date +%Y%m%d_%H%M%S); mkdir "set_aside_$stamp"
  mv scan_sessions dark_measurements data_catalog catalog_config.json laser_log.txt "set_aside_$stamp/"
  ```

  `mv` names any of the five that does not exist and moves the others all the same. The new folder
  stays inside `deploy-macos/`, which git ignores; the controller, the data layer, the Studio and
  `cnn_1.py` never read it. Copy a dark back into `dark_measurements/` together with its `.json`: a
  simulated dark without one counts as real. Delete the folder once you are sure you do not need
  it. The copies that uploads to ARC keep (`data_catalog/arc_uploads/<upload id>/payload/`) are
  read-only files, and `rm` asks about each of them, so give them write permission first (put the
  folder's name in place of `set_aside_<stamp>`):

  ```bash
  chmod -R u+w set_aside_<stamp>
  rm -r set_aside_<stamp>
  ```

Uploads to ARC ([section 14](#14-uploading-sessions-to-arc)). The State column under Recent uploads
and the line above it say what happened and what to do; these are the cases that need more:

- **"The Globus part of the data layer is not installed (the globus-sdk package)".** In Terminal, in
  `Portable_Raman_Catalog/` (from `deploy-macos/`, `cd ..` first), run `./data_layer/setup_env.sh`
  once; it downloads the Globus SDK from PyPI. Or send with SSH.
- **"Globus Connect Personal on this Mac is not running or not connected".** Open it from the menu
  bar and check that it says Connected, keep the Mac awake, then press Retry same request. Globus
  copies from this Mac only while Globus Connect Personal runs.
- **"Globus cannot read the upload copy on this Mac".** Globus Connect Personal may not read the
  deploy folder. In its Preferences, under Access, add the deploy folder to the list (leave Sharable
  off). The deploy folder is inside
  Downloads, so macOS must let Globus Connect Personal read it too: when macOS asks, allow it; if
  you refused, turn it on in System Settings > Privacy & Security > Files and Folders; if Globus
  Connect Personal is not listed there, add it under Full Disk Access. Then press Retry same
  request.
- **"The Globus collection in ARC settings belongs to another Globus account".** Only the Globus
  account that set up Globus Connect Personal on this Mac can send from it. Sign in with that
  account (turn on Ask for the Globus sign-in every time in ARC settings), or send with SSH; then
  start a new upload. If the collection ID in ARC settings is not this Mac's own, copy it again.
- **"Globus sees other files in this Mac's folder than the upload holds".** The collection ID in ARC
  settings is not this Mac's, or Globus Connect Personal shares another folder under the same path.
  Copy the ID again from Globus Connect Personal (Preferences, Info tab: Endpoint ID) into ARC
  settings and start a new upload.
- **"Your Globus account may not write into ... on ARC".** You are not in the ARC project that owns
  the ARC folder, or a folder there was made by someone else without group write: its owner makes it
  shared (For the team lead, once, in [section 14](#14-uploading-sessions-to-arc)). If the path it
  names is the top of ARC's collection, the collection shows the project folder elsewhere: step 7 of
  "First time, Globus" there checks it.
- **Terminal says "Operation not permitted", or the upload says Not started.** macOS asks once
  whether Terminal may open files in the Downloads folder: allow it. If it was refused, turn on
  Terminal's Downloads Folder in System Settings > Privacy & Security > Files and Folders. Then
  press Retry same request, or double-click `ssh_upload.command` in the upload's folder
  (`deploy-macos/data_catalog/arc_uploads/<upload id>/`), which also works when no Terminal window
  opened.
- **"Signing in to ARC did not work (ssh ended with code 255)".** The Terminal window shows ssh's own
  reason above that line. Check the VT username in ARC settings, your password and Duo, that you have
  an ARC account in the project that owns the ARC folder, and the network: ARC refuses VT Open WiFi;
  use eduroam, a wired VT connection or the VT VPN. Then press Retry same request. Nothing was
  changed on ARC.
- **ssh prints "WARNING: connection is not using a post-quantum key exchange algorithm".** That is
  expected with ARC (ARC's FAQ explains it): continue.
- **ssh says "REMOTE HOST IDENTIFICATION HAS CHANGED".** Stop there and ask the team: someone may
  be answering in ARC's place. Do not delete the line from `known_hosts` to get past it. Nothing was
  sent.
- **"ARC's transfer host did not run the upload commands", or "ARC lacks a tool the upload needs".**
  Use a login node instead: in ARC settings, under More, set SSH computer to
  `tinkercliffs1.arc.vt.edu` (or run `datalayer config --set
  arc_upload.ssh.host=tinkercliffs1.arc.vt.edu`). Then start a new upload. `/projects` is the same
  on every ARC computer.
- **"Your VT account may not write into ... on ARC: that shared folder is not group-writable".** A
  folder that all uploads share (the ARC folder, `simulated/` or an `_incoming/` in them) was made
  without group write, for example by the team's earlier upload tool or by hand. Its owner runs
  `chmod 3770` on it on ARC, as the message says (For the team lead, once, in
  [section 14](#14-uploading-sessions-to-arc)). Then press Retry same request. Nothing was sent.
- **"Name already on ARC".** ARC already has a folder with the session's name in the ARC folder, and
  nothing replaces it. Press Keep as new copy to put this upload beside it as `<folder>_copy2` (or
  the next free name), or move the other folder on ARC and press Retry same request.
- **"On ARC, last check failed".** Check status looked at the copy of an upload that was put on ARC
  and found it missing or different (the State tool tip says what it found). Press Check status
  again; to send the session again, press Upload to ARC: when no copy of the session is known to be
  on ARC any more, the new upload takes the session's own name.
- **"Check status could not look."** The check did not get to the copy: you did not finish the
  Globus sign-in, the VT password or Duo did not work (ssh ended with code 255), Globus or ARC could
  not be reached, or the upload program met an error. Nothing was changed, and the upload stays
  **On ARC, checked**: the State tool tip, the details and the line above Recent uploads say why and
  what to do, for example "Press Check status again and sign in when the page opens." Upload to ARC
  still says the session is on ARC. Some causes stay until someone clears them, so another check
  meets them again; their texts say to ask the team. "Your Globus identity may not list ... on ARC",
  or "Your VT account may not read ... on ARC": your account may no longer read the project folder,
  or a folder in the copy such as its `dark/` (for example after a change of the ARC project's
  members). "ARC refused a folder on the way to the copy: ... is a link": a folder on the path was
  replaced by a link. "The check cannot run on ...": the SSH computer saved with the upload lacks a
  tool or runs no commands, and every check of that upload uses it, so ask the team to fix that
  computer. A demo upload's checks run on this Mac and look in the demo folder saved with the
  upload, so choosing another demo folder does not help them: make the folder named readable again,
  or put a moved folder back in place of the link that replaced it.
- **"Not sure Globus got it".** The request reached Globus or not; nobody can tell yet. Press Retry
  same request: it asks again with the same request number, and Globus does not make a second copy
  from it. Check status asks Globus without sending.
- **A folder left in `_incoming` on ARC.** A stopped or failed upload leaves the files it copied in
  `<ARC folder>/_incoming/<session folder>.<upload id>/`, and the window names that folder; nothing
  is deleted for you. Retry same request uses it. To remove it by hand, sign in to ARC and remove
  only that one folder, for example `rm -r '/projects/pcl_photonics/raman_globus_upload/_incoming/<session folder>.<upload id>'`
  with the names from the window, or delete it in the Globus web app (<https://app.globus.org>, File
  Manager). Never remove the `_incoming` folder itself while another upload may be using it.

## 13. What was verified

The runs in this section were made before 2026-11-17. On that day the connection to an external
catalog service, which this edition had as a second catalog and never used, was taken out of the
code, together with a classification job for the ARC cluster that depended on it. What was run
after that change is in [VERIFICATION.json](VERIFICATION.json) under `after_removing_the_catalog_service`.

On this Mac, with nothing attached, both simulation variables at `1` and the local demo catalog:

- The build and every test suite of `build_macos.sh --test`, except the two device self-tests:
  every suite passed, in about 11 minutes, also in the two full runs made after the first round of
  review fixes and in the one made after the second round and the changes that completed it. With
  the uploads to ARC added, every suite passed again in each of the three full runs that followed,
  in about 13 minutes each, and in the two full runs made after their review and its fixes and the
  ones made after the second, the third and the fourth round of those fixes, in about 15 minutes
  each.
- A rehearsal from `deploy-macos/`: a 1D X scan (13 spectra, 2 to review), a single capture made
  offline and registered on reconnect, a 2D scan (124 spectra, 5 to review), reviews online and
  offline, provenance, the Studio server's API, and the Studio page in a browser with the real runner,
  including Play, Stop and a run refused by the lock.
- The window's code through its test programs, offscreen, against a stand-in data layer
  (`laser_test/tests/fake_data_layer.py`). Against the real data layer it ran only through scratch
  drivers: in the rehearsal, before the review fixes, and after them for a sync, Classify now and
  reviews made offline and sent by a later sync, and for a change of catalog settings.
- The pairing of darks by kind, in the test programs of the controller, the runner, the end-to-end
  run and the data layer.
- This README's commands, run as written before the uploads to ARC were added, with `TMPDIR` in a
  scratch folder, `PYTHONDONTWRITEBYTECODE=1` also set: the
  instrument check, the build, the tests, the runner, the data-layer commands, the offline demo,
  `hardware_check.sh all --simulate --dark`, and `studio.sh --no-open`. The move of the
  troubleshooting list was run in scratch copies of `deploy-macos/`, so it changed nothing in the
  deploy folder. The `config` and `doctor` commands of section 7 were not run as written here.
- The command-line examples for uploads to ARC of sections 3, 12 and 14, run as written with the
  `datalayer` function of section 3, on 2026-11-15, after the review fixes, in a scratch folder laid
  out like `deploy-macos/` with three test sessions, both simulation variables at 1 and
  `RAMAN_ARC_TEST_GUARD=1`: `arc-sessions`; a demo `arc-upload` (**On ARC, checked**); `arc-list`;
  `arc-status`; the `config --set` of the collection ID, the VT username (the tests' made-up one)
  and the SSH computer; `arc-doctor`; `arc-upload --live --dry-run` with Globus and with SSH (a dry
  run starts nothing and sends nothing); and the move of the troubleshooting list, with `chmod -R
  u+w` and `rm -r` of a set-aside folder that held a kept upload copy.
- The checks after each round of review fixes, in scratch copies of `deploy-macos/`: the
  runner's 1D X scan with sync, sync, classify and review-list (13 predictions, points 10 and 13 to
  review); a dark without a `.json`, so of the real kind, dated later than the simulated ones,
  which a new simulated session did not use; and the Studio server starting a single
  capture and stopping a 1D scan.
- Uploads to ARC ([section 14](#14-uploading-sessions-to-arc)), in demo mode and against stand-ins
  only, in the test suites: both transports end to end in the demo; each crash-and-restart case of
  [PROTOCOL.md](data_layer/PROTOCOL.md) section 5.6 with real processes; the Globus steps against a
  mocked Globus SDK; the SSH steps against a stand-in ssh that runs the real remote script with
  `/bin/bash` on this Mac; the ARC page of the window offscreen against the stand-in data layer; the
  Studio's upload strip.
  The answers of the window's stand-in data layer, of the Studio's engine and server, and the
  examples and tables of [PROTOCOL.md](data_layer/PROTOCOL.md) are checked in the suite against
  those of the real commands (`data_layer/tests/test_arc_shapes.py` and `test_arc_contract.py`).
- Uploads to ARC from `deploy-macos/`, in demo mode and from the command line: two scans made with
  `raman_sim_runner`, then `arc-sessions`, one demo upload with Globus and one with SSH, each **On
  ARC, checked** about 5 seconds after `arc-upload`. In the demo folder each copy held the session's
  files, its dark spectrum in `dark/`, the manifest and `SHA256SUMS`, which `shasum -a 256 -c
  SHA256SUMS` confirmed, and `_incoming/` was empty. The run data was then moved out of
  `deploy-macos/`.
- The same after the review fixes, with three scans: a demo upload over SSH and one with Globus,
  each **On ARC, checked**. The Globus copy was then deleted from the demo folder, and Check
  status showed **On ARC, last check failed**, with one upload that needs you in `arc-list` and
  `status`. Uploaded again, the session went under its own name. A folder of the third session's
  name made by hand gave **Name already on ARC**, and Keep as new copy put the upload beside it as
  `<folder>_copy2`. The shared folders a demo SSH run made were group-writable and sticky, and
  3770 in a folder whose group this account is in; the upload's own folders were 0750 and its
  files 0640. The run data was then moved out of `deploy-macos/`.
- The same after the second round of review fixes, with three scans. Two checks could not look:
  Globus could not list the demo copy, and the remote script refused a folder that was a link.
  Both uploads stayed **On ARC, checked**, and each check said why and what to do. `arc-list` and
  `status` counted no upload that needs you, and a new upload of the session was still refused as
  already on ARC. A check that found the copy gone gave **On ARC, last check failed**. Uploaded
  again, the session went under its own name. After **Name already on ARC**, Keep as new copy put
  the upload beside a folder made by hand, as `<folder>_copy2`. The run data was then moved out of
  `deploy-macos/`.
- The same after the third round of review fixes, with three scans. Four checks could not look:
  Globus could not list the demo copy, the remote script refused a folder that was a link, and it
  could not read the SSH copy or the folder that holds it. Each check said why in its own words
  and what to do, and both uploads stayed **On ARC, checked**. `arc-list` and `status` counted no
  upload that needs you, and a new upload of each session was still refused as already on ARC. A
  check that found the copy gone gave **On ARC, last check failed**. Uploaded again, the session
  went under its own name. After **Name already on ARC**, Keep as new copy put the upload beside a
  folder made by hand, as `<folder>_copy2`. No message, history line, fix or check of these demo
  uploads said "on ARC"; only the state names did. The run data was then moved out of
  `deploy-macos/`.
- After the fourth round of review fixes, nothing was run from `deploy-macos/`. The cases that
  round fixed ran in the test suites, and again from the command line with the upload program in a
  scratch copy of the data layer, in the demo and against the stand-ins: a stopped Globus check, the
  demo fixes of a check that could not look, a folder in an SSH copy that this account may not
  read, and an SSH computer that cannot run a check.

Before hand-over, the simulated data of the rehearsal and checks (scan folders, darks, the demo
catalog with its ledger, and the log) was moved out of `deploy-macos/`; `status` there then
reported an empty local demo catalog.

After the hand-over, on 2026-11-16, one upload each way went to ARC live, from the window's ARC
page in `deploy-macos/` (the window started with `run_simulated.sh`, simulated 1D scans, signed in
with a VT account). Over SSH, 23 files went into the `simulated/` folder of the default ARC folder,
with ARC's SHA-256 of every file matching this Mac's; that copy was later moved by hand into a test
folder on ARC and checked there. With Globus, 22 files went into the `simulated/` folder of that
test folder, with Globus's SHA1 checksums matching. Section 14 says what that settled.

Not verified: uploads to ARC beyond those
two (a live Check status, Stop or Retry same request among them), real instruments
(`run_hardware.sh` was started once, with none attached), Windows, the two device self-tests, the
window started from Qt Creator, and the second lock against the original controller. Not run from
this README: Qt Creator, `studio.sh` opening a browser, the commands that send to ARC from the command line (a live `arc-upload` among them), the steps in the Globus
web app and on ARC (section 14: the collection check, the consent removal, `chmod 3770` and
`ls -ld`, `rm -r` of a folder in `_incoming`), the Files and Folders and Full Disk Access steps of
section 14, the hardware checks without `--simulate`, and `setup_env.sh` itself (its package
install ran, to add the Globus SDK to `.venv`).
[VERIFICATION.json](VERIFICATION.json) has the counts, times and commands.

## 14. Uploading sessions to ARC

The Data tab's **ARC upload** button opens a page for copying a finished scan session to the team's
folder on VT ARC. It sends a copy of the session folder with its dark spectrum and a list of every
file with its SHA-256, checks the copy on ARC, and only then gives it the session's name there. It
sends with Globus (the default) or SSH. The **Demo** box, ticked by default, sends to a folder on
this Mac instead, so you can try every step without an account. How it works in detail is in
[PROTOCOL.md](data_layer/PROTOCOL.md) section 5.6.

**Tried live once each way (2026-11-16): one simulated scan over SSH and one with Globus, both On
ARC, checked.** Apart from those two uploads and the first-time steps they needed, everything here
ran in the demo and the tests only. What the two uploads settled, and what is not known yet, is at
the end of this section.

**What goes, and where.** The ARC folder is `/projects/pcl_photonics/raman_globus_upload` unless
you change it in ARC settings. Each session gets a folder of its own in it, named like the session:

```
/projects/pcl_photonics/raman_globus_upload/
  1d_x_scan_20261113_101500_123/      session.json, point_0001.json ... and the other files of the session
    dark/                             the dark spectrum the session is paired with (.txt and .json)
    raman_upload_manifest.json        every file with its size and SHA-256, how the scan ended, simulated or not
    SHA256SUMS                        on ARC, "sha256sum -c SHA256SUMS" in this folder checks every file
  1d_x_scan_20261113_101500_123_copy2/  a new copy (Keep as new copy, or Upload again) when the name was taken
  _incoming/                          uploads that are being copied, or that stopped part way
  simulated/                          simulated sessions, only when you allow them
```

- An upload is first copied into `_incoming/<session folder>.<upload id>/` and checked there. Only
  when every file matches does one rename give it the session's name, so nobody on ARC sees a
  half-copied session under that name. Over SSH, ARC computes the SHA-256 of every file and this
  Mac compares them; with Globus, Globus checks every file after copying it, and the checksums it
  reports are compared with this Mac's (SHA1 from this Mac to ARC; MD5, SHA-256, SHA-512 and
  Adler-32 would do too).
- **Nothing on ARC is deleted, replaced or overwritten.** When the name is taken, the upload waits as
  "Name already on ARC": press **Keep as new copy** to put it beside the other folder as
  `<folder>_copy2` (or the next free name), or move the other folder on ARC and press **Retry same
  request**. A new copy always takes the first free name of `<folder>`, `<folder>_copy2`,
  `<folder>_copy3` and so on.
- Only sessions that have ended go: no program has the session open, and nothing in it changed for
  60 seconds. The window knows when no scan runs, so right after a scan it does not wait. A scan
  that was stopped early or ended with an error may go too; the confirmation and the manifest say so,
  for example "This scan was stopped early: 8 of 124 spectra."
- Simulated sessions are not sent to ARC unless you turn on **Also send simulated sessions** in ARC
  settings; they then go into `simulated/`. A session whose files do not say whether it was
  simulated (for example one added with Import scan folder) is handled like a simulated one, unless you tick
  **Measured with the real instruments**; the manifest records that you said so. The demo takes
  simulated sessions, into its own `simulated/` folder.
- Both ways of sending reach the same folder. ARC's Globus collection shows `/projects/<dir>` as
  `/<dir>`, so the page shows the Globus path beside the ARC folder, for example
  `/pcl_photonics/raman_globus_upload`.

**First time, Globus.**

1. An ARC account, and membership in the ARC project that owns the ARC folder (its owner adds you
   in ColdFront). The owner must have switched on "Share via Globus" for the project.
2. Install Globus Connect Personal (<https://www.globus.org/globus-connect-personal>), log in with
   your Virginia Tech identity, and give the collection a name. The Globus account that sets it up
   is the only one that can send from this Mac with Globus ("On a Mac that several people use",
   below).
3. In Globus Connect Personal's Preferences (its icon in the menu bar), under Access: remove your
   home folder, add the deploy folder (`Portable_Raman_Catalog/deploy-macos`) to the list, and
   leave Sharable off.
4. The deploy folder is inside Downloads, which macOS protects. When Globus Connect Personal first
   reads it, at the latest during the first upload, macOS may ask whether it may access files in
   your Downloads folder: allow it. If you refused, turn Globus Connect Personal on in System
   Settings > Privacy & Security > Files and Folders. If it is not listed there, add it under Full
   Disk Access instead.
5. Copy the collection ID into ARC settings: **This Mac's Globus collection ID**. Globus Connect
   Personal shows it on the Info tab of its Preferences, as Endpoint ID; so does
   <https://app.globus.org> under Collections, and the file `~/.globusonline/lta/client-id.txt`
   holds it. It is an ID, not a password. From the command line: `datalayer config --set
   arc_upload.globus.mac_collection=<ID>`.
6. Live Globus uploads need the Globus SDK in `.venv`. If the build said it is missing, run
   `./data_layer/setup_env.sh` once in `Portable_Raman_Catalog/` (from `deploy-macos/`, `cd ..`
   first); it downloads it from PyPI.
7. Check once where ARC's collection shows the ARC folder. At <https://app.globus.org>, open File
   Manager, search the collections for "Virginia Tech ARC Globus Projects Directories" and open it.
   The project folder `/projects/pcl_photonics` must be listed at its top as `/pcl_photonics`: the
   collection shows the cluster folder `/projects` as `/`. If it shows as `/projects/pcl_photonics`
   instead, set **Folder the collection shows as its top** (ARC settings, under More) to `/`; if it
   is not there at all, ask the project's owner about "Share via Globus" (step 1).
8. The first upload: untick Demo, leave Globus chosen, press **Upload to ARC**, read the
   confirmation and press **Upload**. A web page opens: sign in with Virginia Tech. A second page
   may ask you to allow access to ARC: allow it (Check status and Retry same request may ask for
   it right after their sign-in too). Come back to the window, and keep Globus Connect Personal
   running and the Mac awake until the upload says **On ARC, checked**.

**First time, SSH.**

1. An ARC account, and membership in the ARC project that owns the ARC folder, as for Globus (step
   1 above). Without an ARC account the sign-in fails (ssh ends with code 255); outside the
   project, ARC does not let you write into its folder.
2. Enter your VT username in ARC settings: **Your VT username**, the part of your VT email before
   @vt.edu. From the command line: `datalayer config --set arc_upload.ssh.username=<name>`. Your
   password is never kept: you type it in Terminal each time.
3. The network: ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN. Off
   campus, connect to the VT VPN first.
4. Untick Demo, choose SSH, press **Upload to ARC**. Check that the confirmation's "By:" line ("By:
   SSH as `<name>`@datatransfer.arc.vt.edu. ...") names your VT username, then press **Upload**. A
   Terminal window opens.
5. macOS may ask whether Terminal may access files in your Downloads folder: allow it.
6. The Terminal window lists ssh's questions in the order ssh asks them, in these words:
   1. The first time only: ARC's key fingerprint, then "Are you sure you want to continue
      connecting (yes/no/[fingerprint])?". Type only yes or no here; what you type is shown on
      screen, so never type your password at this question. Answer yes only if the fingerprint
      matches the one under Host key below; otherwise answer no and ask the team.
   2. Your VT password. Typing shows nothing; that is normal. Press Return.
   3. Duo: approve the push on your phone, or type your password followed by a comma and a
      six-digit passcode at the password question. Choose Duo Push if a numbered menu appears.

   That is the only sign-in of this attempt. With the team's file of ARC's keys, ssh does not ask
   the first question.
7. ssh may print a WARNING that the connection is not using a post-quantum key exchange algorithm.
   That is expected with ARC (ARC's FAQ explains it); continue.
8. At the end Terminal says "Done: all 21 files are on ARC and match this Mac's copy." (with your
   number of files) and the path. Press Return to close the window. What you typed while the
   upload ran is dropped first, so a Return pressed during the upload does not close it.

**Host key.** The fingerprint of `datatransfer.arc.vt.edu` is `<filled in by the team lead, from
ARC>`. Until the team lead has filled it in, ask before you answer yes. With the team's file of
ARC's keys (**Team file of ARC's SSH keys** in ARC settings), ssh checks the key itself and does not
ask. If ssh warns that the REMOTE HOST IDENTIFICATION HAS CHANGED, stop: nothing is sent. Ask the
team.

**Every day.**

- After a scan, open the Data tab and press **ARC upload**. The newest session that may go is
  chosen. The Session box lists the newest 30; when there are more, its last entry, Show more
  sessions, lists older ones. Check Send with and the Demo box, press **Upload to ARC** and confirm.
- Recent uploads shows each upload: Waiting to start, Preparing the files, Signing in, Checking
  ARC, Handing over to Globus, Copying with Globus (or Copying over SSH), Checking the copy on ARC,
  Putting the copy in place, and **On ARC, checked**. While an upload is not finished, the ARC upload
  button has the warning colour, and its tool tip says how many are not finished. When an upload of
  the last 7 days needs you (it failed, waits for your decision, is not sure, was interrupted, did
  not start, is not being watched, or its last check failed), the button has the danger colour
  instead: "1 upload to ARC needs you. Press to see it." The colour follows the uploads also while
  the ARC page is closed.
- You can close the window: a Globus upload goes on in its own program, an SSH upload in its
  Terminal window. When you open the page again, Recent uploads shows how it ended.
- **Stop upload** stops it. Files already copied stay in `_incoming` on ARC; nothing is deleted.
  Stop upload is there whenever a program works on the upload, Check status included. Stopping a
  check ends it and leaves the upload as it was, also when Globus had finished the copy before the
  check started and the check was checking it or putting it in place. A check that started while
  Globus might still be copying for the upload (also for an upload that failed while its transfer
  still ran) is stopped like any other upload, and the Stop question says so: the copy is
  cancelled, or, when Globus has finished it in the meantime, the upload is Stopped with the
  complete copy in `_incoming`, where Check status or Retry same request puts it in place.
- **Retry same request** continues an upload with the same files and the same place on ARC. It uses
  the settings saved with the upload, not the current ones. With Globus, while it is not known
  whether Globus got the request, it asks again with the same request number, so Globus makes no
  second copy; after a transfer that failed it makes a new request, which copies every file again
  into the same `_incoming` folder. Over SSH you sign in again in a new Terminal window, and the
  files are sent again into the same folder on ARC, unless ARC has them all already.
- **Check status** asks Globus or ARC how an upload stands, without sending anything. It needs a new
  sign-in: the browser for Globus, Terminal for SSH. The line above Recent uploads then says what it
  found ("Checked at 16:05: ..."), and so does the Terminal window of an SSH check. When it looks and
  does not find the copy of an upload that is on ARC as it was sent, that upload shows **On ARC, last
  check failed** in the warning colour, with what the check found ([Troubleshooting](#12-troubleshooting)).
  A check that could not look (a sign-in that did not happen, ARC or Globus out of reach) changes
  nothing: it says why and what to do, and the upload stays on ARC. So does a check that may not read
  the folder, or that cannot run on the SSH computer; where another check would meet the same
  problem, what to do is to ask the team.
- A Globus upload is watched for 90 minutes after you signed in. A longer one shows "Not being
  watched": Globus keeps copying; press Check status later. Keep this Mac awake until the copy is
  done: Globus copies from this Mac only while Globus Connect Personal runs.
- From the command line, with `datalayer` defined as in [section 3](#3-quick-start-in-simulation):
  `datalayer arc-sessions`, `datalayer arc-upload --session <folder> --live`, `datalayer arc-list`
  and `datalayer arc-status --upload=<upload id>`. Add `--transport=ssh` to send with SSH, and
  `--dry-run` to see what would go where without starting anything. A live SSH upload from the
  command line opens Terminal the same way.

**On a Mac that several people use.**

- With Globus, only the Globus account that set up Globus Connect Personal on this Mac can send from
  it: a Globus Connect Personal collection serves one account (Globus's FAQ says so). Everyone else
  sends with SSH, or uses a macOS account of their own with their own Globus Connect Personal. An
  upload signed in as another account ends with "The Globus collection in ARC settings belongs to
  another Globus account".
- Once you have signed in, the tool tip of the upload's State under Recent uploads, and **Signed in
  as** in its details, name the Globus account that sends the files (the line above Recent uploads
  says it only while the upload checks ARC). If it is not the one you meant, press Stop upload.
- Turn on **Ask for the Globus sign-in every time** in ARC settings, so the browser asks for the
  Virginia Tech sign-in even when it still knows the last person.
- Log out at <https://app.globus.org> when you are done.
- For SSH, check the VT username in the confirmation; change it in ARC settings first if it is not
  yours. Retry same request and Check status sign in with the VT username saved with the upload, not
  the one in ARC settings now: their questions, and the Terminal window, name it
  (`<name>@datatransfer.arc.vt.edu`). If it is not yours, type no password: start a new upload with
  your own username instead.

**What goes to ARC and who sees it.**

- `session.json` with Sample ID, Operator, Location, Notes, the laser power and the instrument's
  serial number; every point file (the spectrum, the positions and times, and the same fields); the
  dark spectrum and its description; the manifest; `SHA256SUMS`. The files go as they are, with one
  exception: for a session added with Import scan folder, `session.json` says which folder it was
  imported from, and the copy sent keeps only that folder's name, not this Mac's path to it (the
  manifest's `adjusted` says so; the file on this Mac stays as it is).
- Over SSH, everyone in the ARC project can read them: the files get mode 0640 and the session's
  folders 0750, and the shared folders an upload makes (the ARC folder, `_incoming`, `simulated`
  and `simulated/_incoming`) get 3770, so every member of the project can upload into them, and a
  folder you add there can be renamed or deleted only by you and the shared folder's owner. Over
  Globus, folders and files get what ARC's collection sets (this program sets nothing there), so
  who can read them depends on ARC (item 4 below).
- Not in the files: paths on this Mac, your macOS account name, your VT username, your Globus name.
  On ARC, as every file there, they belong to the account that sent them.
- Globus keeps the transfer's source path, with your macOS account name in it, in your Globus
  activity (<https://app.globus.org>, Activity).
- On this Mac every upload keeps its record in `deploy-macos/data_catalog/arc_uploads/<upload id>/`:
  its state, the steps it took, its log, and the copy until it is on ARC (read-only files, in
  folders you can delete). Never a password or a token. git ignores the deploy folder.

**ARC settings** (the ARC settings... button of the page): This Mac's Globus collection ID, Your VT
username, ARC folder (on the cluster), Usually send with, Also send simulated sessions, Demo by
default, and Ask for the Globus sign-in every time; under More: ARC's Globus collection ID, the
folder the collection shows as its top (`/projects`), the Globus app ID, the SSH computer, the team
file of ARC's SSH keys, Wait after a scan ends (seconds), Time Globus may take (hours) and Demo
folder (on this Mac). More opens by itself when a value there is not valid. **Check setup** says
what each way of sending still needs. The ARC folder must be a folder inside a project directory
under `/projects`, such as `/projects/pcl_photonics/raman_globus_upload`; its Globus path follows by
itself. The demo folder must be new, empty or one the demo made itself, and outside
`scan_sessions`, `dark_measurements` and `data_catalog/arc_uploads`. A value that is not valid
stops new uploads, retries and checks, but the uploads are still listed and can be stopped. The
command line takes the same values with `datalayer config --set arc_upload.<key>=<value>`
([PROTOCOL.md](data_layer/PROTOCOL.md) section 5.1 lists the keys), and `datalayer arc-doctor` runs
the same checks.

**For the team lead, once.**

- Register an app for the team at <https://app.globus.org> (Settings, Developers) as a thick client
  (a native app), and enter its ID as **Globus app ID** (`arc_upload.globus.client_id`). Until then
  the sign-in uses the Globus SDK's public example app, and Check setup says so.
- Then remove the consent you gave the example app: open <https://app.globus.org/settings/identities>,
  press Manage Your Consents, and press the X beside that app's consent.
- Make the four shared folders on ARC writable for the whole project, before the first upload by
  anyone. Every member's upload adds a folder of its own in them, and a shared folder without group
  write lets only its owner upload. The team's earlier upload tool may have made the ARC folder
  already, and Globus makes folders with what ARC's collection sets. Sign in to an ARC login node,
  such as `tinkercliffs1.arc.vt.edu`, and run:

  ```bash
  d=/projects/pcl_photonics/raman_globus_upload   # the ARC folder of ARC settings
  mkdir -p "$d/_incoming" "$d/simulated/_incoming"
  chmod 3770 "$d" "$d/_incoming" "$d/simulated" "$d/simulated/_incoming"
  ls -ld "$d" "$d/_incoming" "$d/simulated" "$d/simulated/_incoming"
  ```

  Each line `ls -ld` prints must start with `drwxrws--T` (the project group may write, its new
  folders belong to that group, and a member's folder in it can be renamed or deleted only by that
  member and the shared folder's owner) and name the project's group. Only a folder's owner may run
  `chmod` (and `chgrp`, for a folder of another group) on it: if it answers "Operation not
  permitted", ask whoever made that folder to run the `chmod` line. An SSH upload makes a missing
  shared folder this way itself, and an upload that cannot write into one says which folder and
  what to run.
- Ask ARC for the host-key fingerprints of `datatransfer.arc.vt.edu`. Fill them in under Host key
  above, put the keys in a known-hosts file, and set **Team file of ARC's SSH keys**
  (`arc_upload.ssh.known_hosts`) to it: ssh then checks ARC's key itself and asks nothing.
- If SSH uploads end with "ARC's transfer host did not run the upload commands", set the SSH
  computer to a login node, `tinkercliffs1.arc.vt.edu`.

**Settled by the two live uploads** (2026-11-16, one each way, simulated scans):

- Globus: the first upload opened two pages, the sign-in and then one to allow access to ARC's
  collection. Globus reported SHA1 checksums from this Mac to ARC, and every file matched. Globus
  gave a user name for the VT identity. Globus Connect Personal (3.3.2) showed the upload copy
  under its absolute path inside Downloads. The copy went to `/pcl_photonics/...` in ARC's Projects
  collection (`8e804f3a-5bac-4b6d-9836-bcd5b9c133ec`), as the default **Folder the collection shows
  as its top** (`/projects`) has it.
- SSH: `datatransfer.arc.vt.edu` ran the upload's commands (bash, base64, chmod, tar, sha256sum,
  `mv -T`). ssh asked about ARC's key first: the key went into `~/.ssh/known_hosts` 3 s after ssh
  started. The VT password and Duo took the next 36 s; Terminal's screen was not captured, so their
  order was not seen. In a later ssh session to the same host, ssh's warning about post-quantum key
  exchange came before the password question, and no Duo question appeared on screen. The ARC
  folder, its `simulated/` folder and that folder's `_incoming/`, which the upload made, got
  `drwxrws--T` (3770) and the project's group; the session's folder got `drwxr-s---`. Terminal
  opened the upload's `ssh_upload.command`.

**Not known yet**:

1. Globus: whether Check status and Retry same request ask for the consent right after their
   sign-in, and whether Globus names the owner of a Globus Connect Personal collection, so that a
   sign-in with another account is reported as such.
2. That the collection's `/pcl_photonics` is `/projects/pcl_photonics` on ARC: the Globus upload
   was not looked at from ARC's side. Step 7 of "First time, Globus" checks the collection's top.
3. Globus Connect Personal on macOS: whether macOS asks about the Downloads folder (Files and
   Folders, or Full Disk Access; it was not noted in the live upload), and whether it shows hidden
   files.
4. The order of the password and Duo questions of an SSH upload (the order above follows ARC's
   FAQ), ARC's host key fingerprints (the key was accepted at ssh's question, not compared with a
   list), the modes ARC gives the files of an SSH upload, and the modes Globus gives the folders and
   files it makes.
5. Terminal's one-time question about the Downloads folder (it was not noted in the live upload).
