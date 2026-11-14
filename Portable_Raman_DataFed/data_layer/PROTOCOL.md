# Portable Raman, DataFed edition: interface contract

This file is the single source of truth for how the pieces of this edition talk to each other.
If code and this file disagree, fix one of them in the same change.

Pieces: the Qt controller (`laser_test/`), the two helper programs (`macos-native/`), the Python
data layer (`data_layer/raman_datafed/`), the virtual sample used in simulation
(`virtual_sample/`), and the Simulation Studio (`studio/`).

The flow being implemented:

1. **Capture**: the controller saves each spectrum locally with its metadata.
2. **Register**: the data layer creates one catalog record per spectrum and uploads the raw file.
   Offline captures wait in a local ledger and are registered on the next sync.
3. **Classify**: a job (on VT ARC, or run locally) pulls the spectrum, runs the model, and saves
   the prediction as a new record.
4. **Link**: the prediction record is "derived from" the raw spectrum, the model version, and
   the dark spectrum.
5. **Review**: a person checks the low-confidence predictions.

Two catalogs implement the same interface: `local-demo` (files on this Mac, nothing leaves it,
always labelled as a demo) and `datafed` (the real DataFed service through the `datafed` Python
package; needs the user's own DataFed account, storage allocation, and Globus Connect Personal).

## 1. Folders

`HOME` is the folder the controller runs from (`ramanHome()` in `laser_test/main.cpp`), normally
`deploy-macos/` built by `macos-native/build_macos.sh`.

```
HOME/laser_test                       controller executable
HOME/handshake_v2/{backend,motor}/    helper programs
HOME/scan_sessions/<folder>/          session.json, point_NNNN.json, request.txt, response.txt ...
HOME/scan_sessions/_sim_stage.json    simulation only: simulated stage position (section 4)
HOME/dark_measurements/dark_*.txt     dark spectra
HOME/dark_measurements/dark_*.json    beside each dark taken by this edition: simulated or real (section 2.5)
HOME/data_layer/raman_datafed/        deployed copy of the Python package
HOME/data_layer/arc/                  ARC job files
HOME/data_layer/requirements.txt      the package's dependencies (for the ARC environment)
HOME/datafed_python.txt               first line = interpreter for the data layer
HOME/datafed_config.json              data-layer settings (section 5.1)
HOME/data_catalog/                    ledger.sqlite, local_demo/, staging/, sync.lock, review.lock
HOME/models/<name>/                   classifier model files
HOME/virtual_sample/                  sample.json, reference_spectra.json (simulation only)
HOME/studio/index.html                Simulation Studio page
HOME/raman_sim_runner                 headless controller, always simulated (section 7)
HOME/studio.sh                        starts the Studio server for HOME (section 7)
```

`HOME/dark_measurements/` of a deploy holds only darks captured there: the build does not copy the
darks of the original hand-off folder. They have no `.json`, so they count as real (section 2.5),
and a real session could otherwise be paired with a dark months old. A dark there with the name
and the content of a hand-off dark and no `.json` (a copy an earlier build made, or a link to a
hand-off dark) is removed by the build; the hand-off folder keeps its own, and nothing is removed
when both folders are one.

The controller starts the data layer as a child process and never blocks on it:

```
program : RAMAN_DATAFED_PYTHON (env) | first line of HOME/datafed_python.txt | the analysis interpreter
args    : -m raman_datafed --home HOME <command> [options]
env     : PYTHONPATH = HOME/data_layer (prepended)
cwd     : HOME
```

If `HOME/data_layer/raman_datafed/` does not exist the controller shows the data features as
"not installed" and starts no process.

The controller window starts all this only once it is shown: `status`, `list` and `review-list`
then; a plain `sync` when a scan or single capture has ended, after its own `classify`, and every
30 s while no scan runs and the status reports `sync_work` above 0 or `catalog_changed` (section
6), or uploaded captures wait for a prediction made elsewhere (for those, with DataFed, only once
the last sync is 5 minutes old); `sync --retry-failed` from its Sync now button. Sync now,
Classify now and Import scan folder also work while a scan runs, so that earlier points of a long
scan can be looked at; what a point or `session.json` still being written means for them is in
section 5.4. A controller that is never shown (`raman_sim_runner`, the tests) starts no data-layer
process, so nothing is synced behind the Studio's back. The Studio button starts `studio --no-open
--idle-exit 900` detached.

## 2. Files the controller writes

Existing files and keys keep their exact meaning. Everything below is additive.

### 2.1 Operator fields

The controller has four text fields: **Sample ID**, **Operator**, **Location** (all required,
trimmed, non-empty) and **Notes** (optional), each at most 200 characters, counted in UTF-16 units
as `raman_sim_runner` counts them: an emoji counts two. A character that does not fit whole is left
out, whether it is typed, pasted or a remembered value, so a field never holds half of a character
(a lone UTF-16 surrogate) and `session.json` and the capture blocks never contain a `\ud800` to
`\udfff` escape. A remembered value longer than the limit is shown cut at a whole character, and
captures record what the field shows. The Studio's server counts the same limit in code points
(section 7). A scan or single capture does not start while a required field is empty. Operator and
Location are remembered between runs (`QSettings`). The values are copied at the moment the scan
starts and do not change during that scan.

`laser_power_level` is the level that was sent with the last confirmed Laser ON
(`LOW` | `MEDIUM` | `HIGH`), not the current position of the Power box.

### 2.2 `HOME/scan_sessions/<folder>/session.json`

Written atomically once, when a scan or single capture starts, before any point exists.

```json
{
  "schema": "raman-session/1",
  "session_id": "<uuid>",
  "folder": "1d_x_scan_20261113_101500_123",
  "scan_mode": "1d-x",
  "started_utc": "2026-11-13T15:15:00.123Z",
  "sample_id": "S-001", "operator": "A. Operator", "location": "Lab 123", "notes": "",
  "laser_power_level": "HIGH",
  "integration_time_us": 1000000,
  "instrument": {"model": "C15471", "serial": "823OK002", "unit": "...", "firmware": "1.03"},
  "simulated": {"backend": true, "motor": true},
  "dark_file": "dark_20261113_101400_001.txt",
  "software": {"name": "portable-raman-controller-datafed", "version": "1.0.0"}
}
```

`scan_mode` is `1d-x`, `1d-y`, `2d` or `single`. `integration_time_us` and `instrument` are
`null` when the backend did not report them (section 3). `simulated.backend` and `simulated.motor`
apply the rule by which each helper program chooses its simulator to the environment it is started
with: the backend simulates for `RAMAN_BACKEND_SIMULATE` exactly `1`; the motor program for
`RAMAN_MOTOR_SIMULATE` `1` once leading and trailing blanks are removed, or for `"transport":
"simulate"` (or `true`) in the `motor_config.json` it reads (`RAMAN_MOTOR_CONFIG`, else the one in
`handshake_v2/motor/`). The controller reads that file only when both Python and Qt can (section
2.5), the motor program with Python alone: a hand-edited `motor_config.json` that selects
`simulate` and also holds what only Qt refuses (`NaN`, `Infinity`, a number beyond the range of a
double, nesting deeper than 1024) gives `simulated.motor` false while the motor program simulates.
A Windows build starts the original `SampleApl.exe` and `RamanAutofocus.exe`, which have no
simulator: there both are always `false`.

`dark_file` is the file name (no folder) of the newest dark in `HOME/dark_measurements/` at that
moment whose kind (section 2.5) is the session's: simulated when `simulated.backend` is true, real
when it is false. It is `null` when there is no dark of that kind; a dark of the other kind, or of
unknown kind, is never named. Newest is the latest time stamp in the name
(`dark_yyyyMMdd_HHmmss_zzz`), the way `cnn_1.py` orders dated files; the modification time decides
only when no name of that kind is dated (copied darks, such as the original hand-off's, all share
one modification time).

### 2.3 `point_NNNN.json`

All existing keys stay. One key is added:

```json
"capture": {
  "schema": "raman-capture/1",
  "sample_id": "...", "operator": "...", "location": "...", "notes": "...",
  "laser_power_level": "HIGH",
  "integration_time_us": 1000000,
  "instrument": {...} ,
  "simulated": {"backend": true, "motor": true},
  "dark_file": "dark_....txt",
  "software": {"name": "portable-raman-controller-datafed", "version": "1.0.0"}
}
```

`simulated` and `dark_file` follow the rules of section 2.2.

### 2.4 Single capture

Button **Capture Spectrum**: one saved spectrum at the current position, no motor program.
Folder `single_<yyyyMMdd_HHmmss_zzz>`, `scan_mode` = `single`, exactly one `point_0001.json` with
`point_id` 1 and `controller_x_steps`, `controller_y_steps`, `controller_z_steps` = `null`
(the controller does not know the position without the motor program). Same guards as Capture
Dark, plus: laser ON confirmed. It uses the same capture call as every other capture.
`capture_id` is `single_` + the session id. A single capture that fails leaves `session.json` and
`failure.txt` (the reason, as a failed scan does) and no point file.

### 2.5 Dark spectra: `HOME/dark_measurements/dark_<stamp>.json`

A spectrum taken in simulation is never paired with a real dark, nor a real spectrum with a
simulated dark. Capture Dark saves `dark_<stamp>.txt` exactly as before (other tools read it) and
then, with the same atomic publish as `session.json`, `dark_<stamp>.json` beside it. `<stamp>`
includes any `_N` suffix the `.txt` name got.

```json
{
  "schema": "raman-dark-description/1",
  "dark_file": "dark_20261113_101400_001.txt",
  "captured_utc": "2026-11-13T15:14:00.456Z",
  "simulated": {"backend": true, "motor": true},
  "instrument": {"model": "C13560-0274", "serial": "SIMULATED", "unit": "SIM", "firmware": "1.00"},
  "software": {"name": "portable-raman-controller-datafed", "version": "1.0.0"}
}
```

`captured_utc` is UTC with milliseconds. `simulated` follows the rule of `session.json` (section
2.2) and `instrument` is what `session.json` gets. When the `.json` cannot be written, the `.txt`
is removed and the dark counts as not saved (`[Dark Error] Save failed.`); a `.txt` left without
its `.json` would count as real. Should the removal fail as well, the log says to delete the file
by hand. A controller stopped between the two writes (a crash, a kill, a power cut) leaves a
simulated dark without its `.json`, and that dark then counts as real. A real dark captured
before every real scan keeps such a dark from being the newest real one.

The kind of a dark, read the same way by the controller (`darkKind()` in `laser_test/main.cpp`)
and by the data layer (`dark_kind()` in `metadata.py`):

| kind | when |
|---|---|
| simulated | its `.json` has `simulated.backend` true |
| real | its `.json` has `simulated.backend` false, or it has no `.json`: every dark taken before this edition, the hand-off darks included |
| unknown | its `.json` cannot be read (below; also a link to nothing, a folder, or a value that is not an object), or `simulated.backend` is missing or not `true`/`false` |

A `.json` can be read only when both Python's `json` module and Qt's JSON parser read it, so that
both readers give every dark the same kind. The controller also refuses what only Python refuses:
a byte order mark, text that is not UTF-8, a raw control character in a string, an escape that
JSON does not have (`\q`, `\x41`, `\'`) and a number whose point lacks a digit on either side
(`1.`, `.5`, `-.5`, `1.e5`). The data layer also refuses what only Qt refuses: `NaN`, `Infinity`
and `-Infinity`, a number beyond the range of a double (`1e400`, or a whole number too large for
one), a number that reads as 0 although a digit of it is not (`1e-400`), and nesting deeper than
1024 arrays and objects.

A dark of unknown kind is never paired. `session.json` names only a dark of the session's kind
(section 2.2), and the data layer checks the kinds again when it registers a capture (section 5.3).

This file's schema id is `raman-dark-description/1`. The catalog metadata of a dark record
(section 5.3) has its own, `raman-dark/1`, and other keys. Neither reader checks the id of this
file: the kind comes from `simulated.backend` alone.

## 3. Backend lines

The capture backend prints two extra lines on stdout once, after a successful device
initialisation and before the command loop. They change no command and no reply.

```
CAPTURE_SETTINGS exposure_time_us=1000000 cycle_time_us=<n>
MODULE_INFO model="C15471" serial="823OK002" unit="..." firmware="1.03"
```

`MODULE_INFO unavailable` is printed when the read-only information call fails, or is skipped
because a safety stop is already pending at start-up; that is never fatal. Values are stripped of
NUL padding, control characters and double quotes. The controller reads the lines in
its existing stdout line loop. Missing lines (older backend, test emulator) leave the fields `null`.
With the current capture constants `<n>` is 1100000. The simulated device reports
`model="C13560-0274" serial="SIMULATED" unit="SIM" firmware="1.00"`.

## 4. Virtual sample (simulation only)

Real code paths are not affected. Only the two simulators use this.

`virtual_sample/sample.json` describes a virtual 25 mm x 25 mm plate: materials, regions, focus
height, laser power scaling. `virtual_sample/reference_spectra.json` holds one dark spectrum and
one dark-subtracted signal spectrum per material, each 2048 values, derived from real measured
spectra of the classifier's training data (`sources` names the files; `virtual_sample/README.md`):

```json
{"schema": "raman-reference-spectra/1", "source": "...", "sources": {"r6g": {...}, ...}, "pixel_count": 2048,
 "dark": [..2048..], "signals": {"r6g": [..], "methylene_blue": [..], "adenine": [..], "di_water": [..]}}
```

Simulated spectrum at stage position (x, y, z) in mm:

```
raw[i] = dark[i] + laser_on * power_scale[level] * focus(z) * sum_m( w_m(x, y) * signal_m[i] ) + noise
focus(z) = 1 / (1 + ((z - z_focus_mm) / half_width_mm)^2)
```

clipped to 0..65534. A capture whose id starts with `dark_` returns `dark[i] + noise` (the
operator has blocked the optical path). `laser_on` is the simulated laser-enable state.
`level` follows the simulated LD power value the controller sends: 1 = LOW, 2 = MEDIUM, 3 = HIGH;
any other value (no power command yet) gives no laser signal. The position is read from the stage
file when the frame is read out; without a usable stage file it is `start_position_mm`.
The vendor library interface carries no capture id, so the backend's command loop announces each
`S` / `M` command to the simulated device (`SimulatedDevice.BeginCapture`) before the capture; the
real device never receives that call.

Region weights `w_m(x, y)`, with `b` = the blend width for that shape and `clamp` to 0..1:

- `frame` (`inner_mm`, `outer_mm`), `d = max(x, y)`:
  `clamp((d - inner + b/2) / b) * clamp((outer + b/2 - d) / b)`
- `circle` (`center_mm`, `radius_mm`), `r` = distance to the centre: `clamp((radius + b/2 - r) / b)`

Per material take the largest weight of its regions. If the sum over materials exceeds 1,
divide by the sum. The background material gets `1 - sum`.

Stage position: the motor simulator atomically replaces `HOME/scan_sessions/_sim_stage.json`

```json
{"x_steps": 86750, "y_steps": 86750, "z_steps": 138800, "steps_per_mm": 34700, "moving": false,
 "updated_utc": "2026-11-13T15:15:01.250Z"}
```

when the simulated axes are opened, at the end of every simulated move (homing and stops
included), and at about 10 Hz while a simulated move is in progress.
A new motor process starts from the positions in that file, else from `start_position_mm`; an
explicit `RAMAN_MOTOR_SIM_START` entry (existing test hook) still overrides the start of its axis.
The stage therefore stays where the last simulated scan left it (after a 1D X scan: x = 24 mm)
until the file is deleted.

How each simulator finds the files:

| | sample folder | stage file |
|---|---|---|
| backend simulator | `RAMAN_SIM_SAMPLE_DIR`, else `<cwd>/../../virtual_sample` | `RAMAN_SIM_STAGE_FILE`, else `<cwd>/../_sim_stage.json` |
| motor simulator | `RAMAN_SIM_SAMPLE_DIR`, else `<session dir>/../../virtual_sample` | `RAMAN_SIM_STAGE_FILE`, else `<session dir>/../_sim_stage.json` |

When the sample files are missing both simulators behave exactly as before (synthetic spectrum,
previous start positions, no stage file), so every existing test keeps its meaning. With a
usable sample each simulator prints one `[sim] ...` line on stdout that names the folder; sample
files that exist but cannot be used are reported with a `[sim] ... ignored` line and then treated
as missing.

## 5. Data layer

### 5.1 Settings: `HOME/datafed_config.json`

Created with these defaults on first use. Unknown keys are kept.

```json
{
  "mode": "local-demo",
  "datafed": {"context": "", "root_alias": "raman-portable", "repo_id": "", "include_simulated": false},
  "review": {"confidence_threshold": null},
  "classifier": {"model_dir": "models/pca_svm_pure_20260922"},
  "sync": {"max_attempts": 5}
}
```

`mode` is `local-demo` or `datafed`. `datafed.context` is a DataFed project id (`p/...`) or empty
for the user's own space. `datafed.repo_id` is the id of a repository allocation (`repo/...`), empty
for the default one. `review.confidence_threshold` = `null` means "use the model's own threshold"
(0.8 for the shipped model). Relative paths are relative to HOME.

`datafed.include_simulated` takes `true` or `false` only (`config --set
datafed.include_simulated=true`; any other text, `False` included, is refused and nothing is
saved). While it is `false`, a sync in `datafed` mode does not register a capture made in
simulation (its `simulated` is true: either helper simulated). Such a capture is left where it is
on this Mac (`captured`, or `error` at `register` after a failed attempt made while the setting was
`true`), is not marked failed and spends no attempt; `status` and `sync` count it in
`held_simulated` (section 6), and the status's `problems` then hold the one sentence that names the
setting: "N simulated capture(s) stay on this Mac and are not sent to DataFed: to send them as
well, run config --set datafed.include_simulated=true." The window's Data tab shows the count in a
line of its own instead of that sentence. One registered before goes on to its upload. The first sync after
the setting is `true` registers the others. A capture
whose `simulated` is `null` (an imported session) is registered either way. The setting is not
part of the catalog binding, so changing it registers nothing again. The local demo registers
everything, whatever it says.

The data layer never logs in, never stores a password, and never runs `datafed setup`. In
`datafed` mode it uses the credentials the user installed themselves; if there are none it
reports that, with the command the user has to run.

### 5.2 Catalog layout

All aliases are lowercase `[a-z0-9._-]`, deterministic, and at most 40 characters, so that a
retried creation finds the earlier record instead of making a duplicate. `<root>` is
`datafed.root_alias`. `h12(text)` = first 12 hex characters of SHA-256 of the UTF-8 text.

| Item | Alias | Parent collection |
|---|---|---|
| root collection | `<root>` | user or project root |
| sessions collection | `<root>-sessions` | `<root>` |
| one collection per scan session | `rsess-` + h12(session_id) | `<root>-sessions` |
| darks collection | `<root>-darks` | `<root>` |
| models collection | `<root>-models` | `<root>` |
| predictions collection | `<root>-predictions` | `<root>` |
| one collection per scan session's predictions | `rpred-` + h12(session_id) | `<root>-predictions` |
| queue: waiting for the classifier | `<root>-to-classify` | `<root>` |
| queue: waiting for a person | `<root>-needs-review` | `<root>` |
| raw spectrum record | `rs-` + h12(capture_id) | its session collection, also linked in `<root>-to-classify` |
| dark record | `rdark-` + h12(sha256 of the dark file) | `<root>-darks` |
| model version record | `rmodel-` + first 12 hex of the model hash | `<root>-models` |
| prediction record | `rp-` + h12(capture_id + ":" + model hash) | its session's `rpred-` collection, also linked in `<root>-needs-review` when it needs review |

A DataFed collection is understood to hold a limited number of items (not checked against a live
server), so predictions go into one collection per scan session; the job makes it when it saves
the first prediction of that session, titled `Predictions <sample_id> - <session_folder>` (with
` [simulated]` for a simulated session). A prediction of a capture without a session id, and every
prediction saved before these collections existed, is in `<root>-predictions` itself. The other
collections still grow by one item per dark spectrum or per session.

The model hash is SHA-256 over the model files in the fixed order
`pure_scaler_minmax.joblib`, `pure_pca_minmax.joblib`, `pure_svm_minmax.joblib`, `meta.json`.

Raw data attached to records: raw spectrum = the `point_NNNN.json` file itself; dark = the dark
`.txt` file (not its `.json`); model = a zip of the four model files; prediction = none (metadata
only). Uploads start from the file's absolute path on this Mac, to which the `datafed` package adds
the default Globus endpoint; DataFed keeps that path, account name included, as the record's
`source`, which everyone who can read the record sees.

### 5.3 Record metadata

Raw spectrum (`type` = `raman.raw_spectrum`):

```json
{"type": "raman.raw_spectrum", "schema": "raman-capture/1",
 "capture_id": "...", "session_id": "...", "session_folder": "...", "point_id": 3, "scan_mode": "1d-x",
 "sample_id": "...", "operator": "...", "location": "...", "notes": "...",
 "timestamp_utc": "<completed_utc>", "started_utc": "...", "capture_elapsed_ms": 1100,
 "integration_time_us": 1000000, "laser_power_level": "HIGH",
 "position_steps": {"x": 0, "y": 0, "z": 0}, "position_mm": {"x": 0.0, "y": 0.0, "z": 0.0},
 "pixel_count": 2048, "instrument": {...}, "simulated": true,
 "dark": {"file": "dark_....txt", "sha256": "...", "record": "<dark record id>", "kind": "simulated"},
 "raw_file": {"name": "point_0003.json", "sha256": "...", "bytes": 12345},
 "software": {...}}
```

`position_*` are `null` for single captures. `simulated` is true when either simulator was used.
A session completed by `import-session` carries `"imported": true` in its capture blocks and raw
records; there `laser_power_level`, `simulated`, `software` and `scan_mode` may be `null` (nobody
recorded them at the time).

`dark` is `null` when the capture names no dark. `dark.kind` is that of section 2.5 (`real`,
`simulated` or `unknown`); like `sha256` it is `null` when the dark file is not on this Mac. A capture is
paired with its dark only when the dark's kind equals the capture's `simulated.backend` (simulated
for true, real for false); a capture whose `simulated` is `null` (an imported session) is paired
with its dark whatever its kind. Otherwise `dark.record` is `null`, `dark.reason` says why (the
texts are those of the prediction's `reason` below), no dark record is made for it and nothing is
derived from it: the capture counts as having no dark.

Registration refuses metadata the catalog would refuse: when the JSON of the metadata, with ASCII
escapes as the DataFed adapter sends it, takes more than 102,400 - 256 bytes, the capture goes to
`error` / `metadata` with a message that names the longest of the four text fields. Titles are
shortened to 80 UTF-16 code units (an emoji counts 2): the free text in front is shortened and its
end kept whole. When that end (the session folder, in the title of a session's collection or of
its predictions' collection) leaves no room for the text in front, the two are shortened together.
A ` [simulated]` at the end is always kept, also when a catalog fits a whole title again.

Text holding half of a character (a lone UTF-16 surrogate, as an emoji cut in two leaves it) has
no UTF-8 form, so neither the ledger nor a catalog can store it. In the capture block, or in any
other key of the point, it puts the capture in `error` / `metadata` with `<point> cannot be
registered: <key> holds a broken character` (a key whose own name is broken is called "a key").
A broken `capture_id` or `session_id` gets no row at all (section 5.4). Whole numbers are 64-bit,
as the ledger stores them: a `point_id` beyond that range gets no row, `controller_*_steps` beyond
it give `position_*` null, and an `integration_time_us` beyond it is a problem of the capture
block.

Dark (`type` = `raman.dark_spectrum`): `{"type": "raman.dark_spectrum", "schema": "raman-dark/1",
"file": "dark_....txt", "sha256": "...", "bytes": 12345, "simulated": true}`. `simulated` is
`null` for a dark of unknown kind, and the title of a simulated dark ends with ` [simulated]`. Its
file is uploaded on its own; the raw records that name it do not wait for that upload. When sync
gives up uploading a dark whose file is missing or changed (the last of `sync.max_attempts`
attempts), it sets `unavailable` (the reason) in the record; the classifier then rejects the
spectra taken with that dark instead of waiting for its file. The file found again and uploaded
removes the mark.

Model (`type` = `raman.model`): `{"type": "raman.model", "schema": "raman-model/1", "name":
"pca_svm_pure_20260922", "sha256": "<model hash>", "files": [{"name", "sha256", "bytes"}, ...],
"meta": {<content of meta.json>}}`, `files` in the order of the model hash. `sync` creates the
record of the model the settings name and uploads its zip without waiting for it.

Prediction (`type` = `raman.prediction`), provenance `der` -> raw spectrum, model, dark (the dark
link is omitted when there is no dark):

```json
{"type": "raman.prediction", "capture_id": "...", "sample_id": "...", "point_id": 3,
 "raw_record": "<id>", "model": {"record": "<id>", "name": "pca_svm_pure_20260922", "sha256": "..."},
 "dark": {"record": "<id>", "sha256": "..."},
 "label": "r6g", "top_class": "r6g", "confidence": 0.93, "scores": {"r6g": 0.93, "...": 0.02},
 "outcome": "confident", "reason": "", "threshold": 0.8, "needs_review": false, "simulated": true,
 "job": {"runner": "local", "host": "...", "slurm_job_id": null, "started_utc": "...", "finished_utc": "..."},
 "review": {"status": "not_required", "label": null, "reviewer": null, "time_utc": null, "comment": ""}}
```

`dark` is `null` when the raw record names no dark record. `job.runner` is `local` or `arc`,
`job.finished_utc` is when this prediction was computed, and `job` also has `software` (the
versions of python, numpy, scikit-learn and joblib that computed it). A `rejected` prediction has
`top_class` and `confidence` `null` and `scores` `{}`.

`reason` of a spectrum rejected for its dark:

- `dark not used: the dark was taken in simulation; this spectrum is real`, or `dark not used: the
  dark was taken with the real spectrometer; this spectrum is simulated`;
- `dark not used: it is not known whether the dark was taken in simulation (<file>.json cannot be
  read | <file>.json does not say whether the backend simulated | a dark named <file>.json has no
  room for a description beside it); this spectrum is real` (or `simulated`); the third is a dark
  whose own name ends in `.json`;
- in these three cases `dark` is `null` and there is no link to the dark;
- `the dark spectrum <file> was never uploaded: <why>`, when its record says `unavailable`; here
  `dark` is `{record, sha256}` and the link is made.

`outcome` and what it means for review:

| outcome | when | label | needs_review |
|---|---|---|---|
| `confident` | top class is a material and its score >= threshold | the class | no |
| `background` | top class is `di_water` and its score >= threshold | `UNKNOWN` | no |
| `low_confidence` | top score < threshold | `UNKNOWN` | yes |
| `rejected` | spectrum unusable (not 2048 px, saturated, no dark or none of its kind, low raw signal) | `REJECTED` | yes |

`review.status`: `not_required`, `pending`, `confirmed`, `relabelled`, `rejected`. A review merges
`review` and sets `needs_review` false on the prediction record and unlinks it from
`<root>-needs-review`. The model's own `label`, `scores` and `outcome` are never overwritten.
A classification run unlinks the raw record from `<root>-to-classify` once its prediction is saved:
`sync` looks for the predictions of spectra that left that queue (and of every uploaded spectrum
once per 15 minutes, should one have stayed in it). Likewise it lists `<root>-needs-review` once
per sync and reads a prediction that waits for a person only once it has left that queue (every
one at the 15-minute check).

### 5.4 Local ledger states

One row per capture in `HOME/data_catalog/ledger.sqlite`.

```
captured -> registered -> uploading -> uploaded -> classified -> (needs_review -> reviewed | accepted)
```

`error` is a side state with `error_stage` (`metadata`, `register`, `upload`) and a message; a
later sync retries up to `sync.max_attempts`. `captured` items whose point file lacks a valid
`capture` block are `error` / `metadata` until the missing fields are supplied (`import-session`).
`accepted` = classified and no review needed. In `datafed` mode with `datafed.include_simulated`
false, a simulated capture that is not registered stays where it is (section 5.1).

During a scan the controller publishes each point and `session.json` under a temporary name and
renames it when complete, and the data layer reads only the final names. A point file or
`session.json` that cannot be read yet gets no row, or stays `error` / `metadata`, which every
sync reads again: it never spends an attempt and is never given up. So `sync`, `classify` and
`import-session` may run during a scan.

A file that cannot be read never ends a sync; it is reported on its own and the other files go
on. A session folder that cannot be listed gives the errors entry `<folder> cannot be read: <OS
error>`. A `session.json` or point that cannot be used at all (not JSON, nested too deeply for
Python, a `capture_id` or `session_id` that is missing or holds a broken character, a `point_id`
that is not a 64-bit whole number) gives an entry that starts with the folder and names the file,
for example `<folder>: point_0003.json: capture_id holds a broken character` or `<folder>:
session.json cannot be read as JSON`. These entries have `capture_id` null and no row; the status
lists them among its `problems` as the last sync found them. A point whose file, or whose dark,
cannot be read when it is found waits in `error` / `metadata` (`<point> cannot be registered: <OS
error>`). A capture with a broken character waits there until its point file is corrected:
`import-session` keeps the values already in the files.

### 5.5 Catalog interface (Python)

```
class Catalog:                          # raman_datafed/catalog.py
    name: "local-demo" | "datafed";  label: str (shown in the UI)
    whoami() -> {"online": bool, "user": str|None, "context": str, "endpoint": str|None, "demo": bool}
        # never raises: unreachable or missing credentials are reported, with a "problem" text
    ensure_collection(alias, title, description="", parent=None) -> Collection          # idempotent
    create_record(alias, title, metadata, parent, deps=(), description="", tags=(), identity_key=None) -> Record
        # idempotent: alias exists and metadata[identity_key] matches -> the existing record;
        # alias exists with a different identity -> CatalogConflict
        # deps: (relation, id or alias) pairs, relation "der" | "comp" | "ver"
    get_record(id_or_alias) -> Record     # deps in BOTH directions: Dep(relation, id, alias, direction "out"|"in")
    update_record(id_or_alias, metadata=None (merged), deps_add=(), title=None) -> Record
        # nested objects merge; a key set to null may come back missing: read updated metadata with defaults
    link(collection, record); unlink(collection, record)                                 # idempotent
    list_collection(collection) -> [RecordSummary(id, alias, title)]                     # every page
    put_data(record, local_path) -> Transfer(task_id, status, message)                   # returns at once
    transfer_status(task_id) -> Transfer        # status: queued | running | succeeded | failed
    get_data(record, dest_dir) -> Path          # blocks until the file is there
Errors: CatalogOffline (unreachable, nothing changed, retry later; in DataFed mode also a client
that cannot connect at all: "DataFed cannot be used from this Mac", and an interpreter without the
datafed package: "The datafed Python package is not available in this interpreter"),
CatalogAuthRequired (the user must set up credentials), CatalogConflict, CatalogNotFound,
CatalogError (base).
```

A CatalogOffline ends a sync without spending an attempt of any capture: `sync` reports `online`
false, and a command that fails on it reports `"kind": "offline"`. The missing package's detail is
`<import error>; create the data layer's environment with data_layer/setup_env.sh`, and `status`
names it as `The datafed Python package is not available in this interpreter (<that detail>).`

`local-demo` keeps everything under `HOME/data_catalog/local_demo/`. A file
`HOME/data_catalog/local_demo/OFFLINE` makes every call but `whoami` raise `CatalogOffline`, which is how the
demo and the tests exercise "captured offline, registered on reconnect". Its uploads are
asynchronous like Globus: `put_data` returns `queued`; each later `transfer_status` call while
online advances it `queued -> running -> succeeded`.

## 6. Command line

`python -m raman_datafed --home HOME <command> [options]` prints exactly one JSON object on
stdout (UTF-8, one line or pretty, nothing else), logs to stderr, and exits 0 only when
`"ok": true`. Failure: `{"ok": false, "error": "<short>", "detail": "<longer, optional>"}`, plus
`"kind"`: `offline`, `auth_required`, `not_found`, `conflict`, `config`, `metadata`, `command`,
`usage` (wrong arguments; exit code 2), `interrupted` (Ctrl-C; exit code 130), `catalog` or
`internal`. `-h` gives `{"ok": true, "help": "..."}`. Commands never wait for a Globus transfer to
finish; the next `sync` picks the result up.

| command | does | main output keys |
|---|---|---|
| `status` | nothing but report | `mode`, `mode_label`, `demo`, `online`, `user`, `context`, `endpoint`, `counts` (one key per state), `total`, `pending` (captures not yet uploaded, without the held ones), `held_simulated` (simulated captures that `datafed` mode keeps on this Mac, section 5.1; always 0 in the demo), `sync_work` (what a plain `sync` can move on now: captures and dark spectra to register or upload, failed ones below `sync.max_attempts`, reviews recorded offline; not the held captures, those lacking fields, or those past the limit), `catalog_changed` (true while the rows, with their states, record ids and predictions, still belong to the catalog used before the settings changed; the next sync registers every capture in the catalog in use. False when there is no capture), `reviews_queued`, `last_sync_utc`, `problems` (list of strings, below) |
| `sync [--retry-failed]` | find new points, register, upload, poll transfers, pick up predictions and reviews; `--retry-failed` also retries what reached `sync.max_attempts` | `busy` (another sync is running; nothing was done and `online` is `null`), `online`, `ingested`, `registered`, `uploads_started`, `uploads_done`, `predictions_found`, `reviews_found`, `reviews_sent` (reviews written to the catalog: a queued review that a later one already in the catalog supersedes leaves the queue unwritten), `held_simulated` (as in `status`), `errors` (list of `{capture_id, stage, message}`), `status` (same object as `status`) |
| `list [--session ID] [--state S] [--limit N]` | rows for the Data tab, newest first | `items`, `total` |
| `classify [--limit N]` | run the classifier job here against the active catalog; unlike the other commands it waits for the downloads of the files it classifies | `runner`, `processed` (spectra that left the queue with a prediction), `predictions` (prediction records this run created), `skipped` (left in the queue: a file is not uploaded yet), `remaining` (not looked at), `errors`, `items` (`{capture_id, raw_record, prediction_record, label, top_class, confidence, outcome, needs_review, created}`), `model` |
| `review-list` | predictions of this HOME's captures waiting for a person | `items`, `online` (`null`: the catalog did not have to be asked; `false`: the items come from the ledger alone and their `scores`, `reason`, `threshold`, `model` are `null`) |
| `review-show --prediction ID` | one prediction in full; the spectrum is read from this Mac and only fetched from the catalog (waiting for the download) when it is not here | `item`, `spectrum` (`raman_shift`, `intensity`, `dark`, `processed`: index-aligned lists of 2048; `processed` is `null` for the first `processed_start` = 90 pixels, which the model does not see; `dark` or `processed` is `null` when there is none; `source`), `provenance` (the records the prediction is derived from: nodes as in `provenance`, plus `relation` and `label`), `classes`, `online`, `problems`. Only the dark the classifier used is shown: online the one the prediction names (the file on this Mac only when its SHA-256 matches); offline the capture's dark when the kinds allow it (section 5.3). Without one, `dark` and `processed` are `null` and `problems` says why, for example "The classifier used no dark spectrum for this prediction, so none is shown." |
| `review-set --prediction ID --decision confirm\|relabel\|reject [--label CLASS] --reviewer NAME [--comment TEXT]` | record a review (queued locally when offline). `--label` goes with `relabel` only: one of the model's classes, or `unknown` (stored as `UNKNOWN`). `confirm` stores the prediction's `top_class` as the review label (`REJECTED` for a rejected one), `reject` none. It waits, without a time limit, for `HOME/data_catalog/review.lock`, which a sync holds while it sends one queued review (the sync makes its first catalog request, which connects, before it takes the lock), and takes the review's time inside it; a review already in the catalog that was made later is kept, and `item` then shows that one | `item`, `queued` |
| `provenance --record ID` | the provenance chain around a record | `nodes` (`{id, alias, title, type, role: start\|source\|derived, info}`), `edges` (`{from, to, relation, label}`: `from` is derived from `to`), `truncated` |
| `import-session --folder DIR [--sample-id S --operator O --location L] [--notes N --laser-power LEVEL --simulated yes\|no --dark-file FILE]` | add an existing scan folder (copied under `scan_sessions/` unless it is there), supplying missing fields; values already in the files win. `--dark-file` also copies the dark's `.json` from beside it, first; when that `.json` exists, or is a link, but is not a file that can be read (a link to nothing, a folder, an unreadable file), nothing is imported: kind `command`, "The description <name> beside the dark cannot be read; nothing was imported", because the dark copied without it would count as real. A point holding a broken character outside its capture block (section 5.3) is not completed: an `errors` entry `<file>: <key> holds a broken character` | `ingested`, `skipped`, `errors`, `folder`, `session_id`, `copied` |
| `config [--set key=value ...]` | show or change settings (dotted keys) | `config`, `path`, `problems` and `warnings` when there are any |
| `doctor [--online]` | check the DataFed prerequisites without logging in; `--online` (datafed mode only) also asks DataFed who the user is. The settings check names `datafed.include_simulated` and what it does | `checks` (list of `{name, ok, detail, fix}`), `online_checked`, `datafed_ready`, `mode`, `include_simulated` |
| `offline --on\|--off` | local-demo only: simulate losing the connection | `offline` |
| `studio [--port N] [--no-open] [--idle-exit SECONDS]` | serve the Simulation Studio on 127.0.0.1, on port N (default 8765) or the next free one, until interrupted (SIGINT, SIGTERM, SIGHUP); with `--idle-exit` also until nobody has used it for that long (no request, no run of its own) | prints `{"ok": true, "url": "http://127.0.0.1:N/"}` then keeps running |

While `catalog_changed` is true, `pending`, `held_simulated` and `sync_work` count as the next sync
will see them: `held_simulated` every simulated capture that sync will hold back, `pending` all
captures less those (the ones lacking fields included), and `sync_work` the captures it will
register plus the reviews queued for the catalog in use, with no dark spectra. `counts` and
`total` still describe the rows as stored.

The status `problems`, in this order, each only when it applies (N and M are numbers):

1. What the catalog reports, such as `DataFed cannot be reached (...).`, `DataFed cannot be used
   from this Mac (connect: ...).`, the missing package (section 5.5), or missing credentials or
   default endpoint; `The catalog cannot be reached.` when it cannot be reached and says no more
   (the demo). While it cannot be reached and captures wait, ` N capture(s) wait for the next
   sync.` follows.
2. `The catalog settings changed: the next sync registers every capture in <label>.`, with
   `, except the simulated ones, which stay on this Mac` before the full stop while simulated
   captures are held back (section 5.1). `<label>` is the catalog's `mode_label`.
3. `N capture(s) cannot be registered as their files are (their Error rows say why). Where Sample
   ID, Operator or Location is missing, import the session folder again with these fields (Data
   tab: Import scan folder; command line: import-session --folder FOLDER).`
4. `N capture(s) failed and will be tried again by the next sync.`
5. `N capture(s) failed M times and are no longer retried: fix the cause, then run sync
   --retry-failed.` (M is `sync.max_attempts`)
6. In `datafed` mode: `N upload(s) have made no progress for more than 10 minutes: check that
   Globus Connect Personal is running on this Mac (see doctor).`
7. `N dark spectrum file(s) could not be uploaded (the last: <reason>); Sync now tries again.`
8. `N review(s) could not be sent: <the last error>`
9. The held-back sentence of section 5.1.
10. The errors without a capture id that the last sync found (section 5.4).

While `catalog_changed` is true, 4 to 7 are left out: they concern the catalog used before.

`list` item:

```json
{"capture_id": "...", "session_id": "...", "session_folder": "...", "point_id": 3, "scan_mode": "1d-x",
 "sample_id": "...", "operator": "...", "timestamp_utc": "...", "simulated": true,
 "position_mm": {"x": 4.0, "y": 2.5, "z": 4.0},
 "state": "needs_review", "state_label": "Needs review",
 "record_id": "d/12", "record_alias": "rs-...", "upload": {"task_id": "task/3", "status": "succeeded"},
 "prediction": {"record_id": "d/40", "label": "UNKNOWN", "top_class": "r6g", "confidence": 0.61,
                "outcome": "low_confidence", "needs_review": true},
 "review": {"status": "pending", "label": null, "reviewer": null, "time_utc": null},
 "error": null}
```

`prediction` and `review` are `null` until they exist. An item also has `point_file`
(`point_0003.json`); `error` is `{"stage", "message", "attempts"}` in state `error`; `review` has
`"queued": true` while a review recorded offline waits to be sent. `review-list` items are the same shape
plus `prediction_id`, `scores`, `reason`, `threshold`, `model` (`name`, `sha256`, `record`). The text
of an item shows U+FFFD in place of half a character (section 5.3), and a value that the ledger
cannot hold (an object, a list, a whole number beyond 64 bits) is `null`.

## 7. Simulation Studio

`studio/index.html` is one self-contained page. Opened as a file (or published as a web page) it
runs its built-in demo engine. Served by `python -m raman_datafed studio` it switches to live mode
and shows what the real code is doing. The server listens on 127.0.0.1 only.

| endpoint | returns |
|---|---|
| `GET /` | `studio/index.html` |
| `GET /api/ping` | `{"ok": true, "home": "...", "mode": "local-demo", "simulated_stack_available": true}` |
| `GET /api/sample` | `sample.json` plus `reference_spectra` reduced to 256 points per curve |
| `GET /api/scan` | newest session: `session` (session.json), `active`, `stage` (`x_mm`, `y_mm`, `z_mm`, `moving`), `request` (last STEP: `point_id`, `x_mm`, `y_mm`, `z_mm`), `ack` (last ACK status), `points` (list of `{point_id, x_mm, y_mm, z_mm, completed_utc, capture_id, spectrum}` with `spectrum` reduced to 256 values), `log_tail` |
| `GET /api/pipeline` | `status` + `items` (as `list`) + `review` (as `review-list`) |
| `GET /api/provenance?record=ID` | as `provenance` |
| `POST /api/sync`, `/api/classify` | as the commands |
| `POST /api/review` | body `{prediction, decision, label, reviewer, comment}`, as `review-set` |
| `POST /api/offline` | body `{"offline": true}`, local-demo only |
| `POST /api/run` | body `{"mode": "1d-x"\|"1d-y"\|"2d"\|"single", "sample_id", "operator", "location", "power": "LOW"\|"MEDIUM"\|"HIGH"}`: starts `HOME/raman_sim_runner`; `{"ok": true, "pid": N}` or an error |
| `POST /api/stop` | stops a run started by `/api/run` |

Every answer but the page is a JSON object with `"ok"`. A failure is `{"ok": false, "error", "detail"}`
with an HTTP status of 400 or above; where a command failed it is the command's failure object. A
command that is not installed answers `"error": "not available"`. The server answers only requests
whose `Host` is `127.0.0.1:<port>` or `localhost:<port>`, refuses a POST whose `Origin` is not that
same origin, and reads a POST body only as a JSON object (`Content-Type: application/json`, at most 64 kB).
Any other request under `/api/` that the browser marks as coming from another page
(`Sec-Fetch-Site` other than `same-origin` or `none`) gets 403 `{"ok": false, "error": "The API
answers only the Studio page this server serves"}`; a request without that header (curl, scripts)
and the page itself are answered.

Beyond the table:

- `GET /api/scan` also returns `folder`, `sessions` (the 12 newest: `folder`, `session_id`,
  `scan_mode`, `started_utc`, `sample_id`, and `simulated`: `session.json`'s `{"backend",
  "motor"}`, `null` when it does not give both as true or false; the page shows ground truth only
  for the spectra of a session whose two flags are true, the one on show or one of these) and `run`
  (the run started by `/api/run`, else `null`: `pid`, `running`, `exit_code`, `started_utc`,
  `finished_utc`, `stop_requested`, `simulated` and the fields it was started with). `?session=<folder>` shows another session. Newest is the latest
  `started_utc`, else the folder's modification time. `active`: `request.txt` or `response.txt` of
  the newest session changed in the last 5 s and there is no `cancel.txt`, or the run of this server
  is alive. `ack` is `null` until the controller has answered the STEP in `request`. `stage` also has
  `updated_utc` and `"simulated": true`, and its `moving` is false once the stage file has not been
  rewritten for 2 s. A spectrum is reduced by the maximum of every 8 pixels.
- `GET /api/ping` adds `problem` when the settings cannot be used. `GET /api/sample` has the keys of
  `sample.json` at the top level and `reference_spectra` beside them (`null` when that file is unusable).
- `GET /api/pipeline` also returns `total` and `as_of_utc`, takes `?limit=N` (default 500) and
  `?session=ID`, and has `review_problem` (with an empty `review`) when the review list could not be had.
- `POST /api/sync` takes `{"retry_failed": true}`, `POST /api/classify` `{"limit": N}`.
- `POST /api/run` passes `sample_id`, `operator` and `location` as single arguments: at most 200
  characters each, counted in code points, no control characters, no half characters, no
  bidirectional controls (U+202A to U+202E, U+2066 to U+2069), not starting with `-`. One run at a
  time. `raman_sim_runner` counts the 200 in UTF-16 units, as the window does (section 2.1), and
  refuses a longer text with exit code 2: a text of more than 100 emoji passes the server and still
  does not start a run. The page's own fields take at most 60 units. The text fields of `POST
  /api/review` follow the same character rules (its comment may hold line breaks); a refused field
  answers 400.
- `POST /api/stop` sends SIGTERM to the runner, then, after 5 s, SIGKILL to the runner's whole
  process group (the runner and its helper programs), and answers `{"ok": true, "stopped": true,
  "pid", "exit_code", "forced"}`; a negative `exit_code` is the number of the signal. It is a
  failure when no run of this server is in progress.

`raman_sim_runner` is the real controller code without a window. It forces
`RAMAN_BACKEND_SIMULATE=1` and `RAMAN_MOTOR_SIMULATE=1` in its own environment before anything
else, so nothing started from the Studio can reach an instrument. Sequence: Laser ON, Capture
Dark, the requested scan or single capture, Laser OFF, exit. It takes the same controller lock as
the window, so it does not run while the controller window is open.

```
raman_sim_runner --mode 1d-x|1d-y|2d|single --sample-id S --operator O --location L
                 --power LOW|MEDIUM|HIGH [--keep-stage]
```

Its HOME is the folder it sits in (`RAMAN_HOME`, `RAMAN_SIM_SAMPLE_DIR`, `RAMAN_SIM_STAGE_FILE`
and `RAMAN_MOTOR_SIM_START` are removed from its environment). It switches the laser on only after
the backend has reported the simulated device (`serial="SIMULATED"`, section 3), and it scans only
when the dark it has just captured is the newest simulated one (section 2.5), so that
`session.json` pairs the run with it. Unless `--keep-stage` is given it deletes
`scan_sessions/_sim_stage.json` first, so the run starts at the virtual sample's
`start_position_mm`. It prints one line per step, each starting with `[runner] `. SIGTERM, SIGINT
or SIGHUP stop the scan as the Stop button does, switch the laser off and end both helper
programs. Exit code: 0 done, 1 a step failed (also when `controller.lock` cannot be created, for
example without permission), 2 wrong arguments, 3 the controller lock is held by another
controller, 128 + N after signal N.
