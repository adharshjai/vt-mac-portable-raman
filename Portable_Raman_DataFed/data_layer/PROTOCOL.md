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

Beside this flow, a finished scan session can be uploaded whole, with its dark spectrum and a
checked list of its files, to the team folder on VT ARC, by Globus or SSH (section 5.6).

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
HOME/data_layer/requirements-mac.txt  requirements.txt plus the Globus SDK, for this Mac (section 5.6)
HOME/datafed_python.txt               first line = interpreter for the data layer
HOME/datafed_config.json              data-layer settings (section 5.1)
HOME/data_catalog/                    ledger.sqlite, local_demo/, staging/, sync.lock, review.lock
HOME/data_catalog/arc_uploads/        uploads to ARC, one folder per upload (section 5.6)
HOME/data_catalog/arc_demo/           demo uploads: a folder on this Mac that stands in for ARC (section 5.6)
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

The window runs the `arc-*` commands (section 6) only for the ARC page of its Data tab. The
`status` it runs anyway tells it how many uploads to ARC are unfinished (`arc_unfinished`) and how
many need a person (`arc_attention`); they colour the page's ARC upload button. While that page is
hidden (also before it is first shown) and either count is above 0, the window runs `arc-list
--limit=1` every 30 s and uses only its two counts, so that the colour follows the uploads. The
long work of an upload runs in a worker program of its own, started detached (section 5.6): it
outlives the window, and closing the window stops no upload.

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

**Uploads to ARC: `arc_upload`.** The settings of section 5.6 are a group of their own. It is not
among the defaults above and is not written on first use: a key that is missing takes its default
when it is read (`arc_settings.effective`). `config --set arc_upload.<key>=<value>` saves only when
every `arc_upload` value it sets is valid and it brings no problem that was not there before;
otherwise it is refused and nothing is saved (`arc_settings.require_valid_change`). An invalid
value it does not set never holds it back, so one invalid value never blocks repairing another,
and a `config --set` of core keys alone is judged by the core rules only. The team known-hosts file
is looked at only when the command sets `arc_upload.ssh.known_hosts` (or a group around it). What
is still invalid is listed under `arc_problems`, after a save as well. A value made invalid by
editing the file stops `arc-sessions`, `arc-upload`, `arc-check`, `arc-retry` and `arc-keep-copy`
(kind `config`, reason `settings_invalid`). `arc-list`, `arc-status`, `arc-stop` and `arc-doctor`
still answer, since every record keeps the settings it was made with (`settings_used`): an upload
can always be listed and stopped, `arc-list`'s `ready` then gives `settings_invalid` for every
transport, and `arc-doctor` names the invalid values so that the ARC settings dialog can repair
them. `status`, `sync`, `list`, the review commands and the Studio keep working. When the file
itself cannot be read (not JSON), `arc-list`, `arc-status` and `arc-stop` still answer
(`arc-list` with the defaults as its `settings`); the other `arc-*` commands, `arc-doctor`
included, answer `settings_invalid`. The `arc-*` commands never need valid catalog settings: they
never open the catalog.

| key | default | accepted | use |
|---|---|---|---|
| `arc_upload.demo` | `true` | JSON `true` or `false` only (`"False"`, `yes`, `1` are refused) | starting value of the window's Demo box; what `arc-upload` does without `--demo` or `--live` |
| `arc_upload.transport` (T) | `"globus"` | `"globus"`, `"ssh"` | the transport used without `--transport` |
| `arc_upload.folder` (T) | `"/projects/pcl_photonics/raman_globus_upload"` | the rules of section 5.6 (ARC folder) | the ARC folder |
| `arc_upload.include_simulated` | `false` | JSON `true` or `false` only | live uploads of simulated sessions, into `simulated/` |
| `arc_upload.settle_seconds` | `60` | a whole number from 10 to 3600 | how long a session must be quiet before it may go |
| `arc_upload.demo_folder` (T) | `"data_catalog/arc_demo"` | not empty; relative to HOME or absolute (a `~` at the start is your home folder); not HOME itself or a folder that holds it (such as `.`, `..` or `/`); not inside `scan_sessions/`, `dark_measurements/` or `data_catalog/arc_uploads/`; these folders are compared without letter case, as the Mac's disk compares them | the demo's stand-in for ARC; the folder must be new, empty or made by the demo (section 5.6, Demo) |
| `arc_upload.globus.mac_collection` (T) | `""` | `""` or a UUID (stored lowercase), not ARC's collection | this Mac's Globus Connect Personal collection; the user fills it in |
| `arc_upload.globus.arc_collection` (T) | `"8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"` | a UUID | ARC's Projects collection |
| `arc_upload.globus.collection_root` (T) | `"/projects"` | `"/"` or an absolute path of parts `[A-Za-z0-9][A-Za-z0-9._-]*`; the ARC folder must be inside it | the cluster folder ARC's collection shows as its top `/` |
| `arc_upload.globus.client_id` (T) | `"61338d24-54d5-408f-a10d-66c06b59f6d2"` | a UUID | the app the Globus sign-in runs under: the Globus SDK's public example app until the team registers its own |
| `arc_upload.globus.deadline_hours` | `24` | a whole number from 1 to 168 | the Globus transfer's deadline |
| `arc_upload.globus.force_login` | `false` | JSON `true` or `false` only | ask for the sign-in every time (a Mac that several people use) |
| `arc_upload.ssh.username` (T) | `""` | `""` or `^[a-z][a-z0-9_-]{1,31}$`, not `root` | the VT username; empty by default, and no text gives an example |
| `arc_upload.ssh.host` (T) | `"datatransfer.arc.vt.edu"` | a lowercase host name ending in `.arc.vt.edu` | the SSH computer |
| `arc_upload.ssh.known_hosts` (T) | `""` | `""` (the user's own `~/.ssh/known_hosts`) or an absolute path of letters, digits and `. _ / + @ , -` only (`^/[A-Za-z0-9._/+@,-]+$`: ssh splits the value at spaces and expands `%` and `${`) to a plain file, not a link, owned by this user and not writable by group or others | a team-checked file of ARC's SSH host keys |

(T): a text key, kept as text when typed (`1234` stays `"1234"` and is then judged by its rule).
Every problem sentence names its key. An empty `mac_collection` or `username` is valid; a live
upload that needs it is refused (kind `config`, reasons `mac_collection_missing`,
`username_missing`). To use another collection, set `arc_upload.globus.arc_collection` and
`arc_upload.globus.collection_root` together in one `config --set`. Every rule above takes the whole
value: a value that ends in a line break is refused. The same holds for session folder names (rule R1)
and upload ids everywhere.

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

**The ARC job's downloads.** `DataFedCatalog(context="", repo_id="", api=None,
download_timeout_sec=600, collection_root=None)`. The datafed client puts its endpoint in front of a
local path, and ARC's Projects collection shows the cluster folder `/projects/<dir>` as `/<dir>`, so
a download into `/projects/<dir>/raman/work/...` would be asked for in a folder the collection does
not have. With `collection_root` set (the ARC job's `--collection-root`, `/projects` in
`classify_job.slurm`), `get_data` asks instead for the full Globus path `<endpoint>/<path of the
download folder below collection_root>` (`arc_paths.relative_below`: both paths resolved first),
which the client passes on unchanged. `<endpoint>` is the client's current endpoint, else its
default, and must be a collection ID (a UUID); otherwise `CatalogAuthRequired`: "No Globus endpoint
is set for DataFed on ARC. On an ARC login node, run 'datafed ep default set <ID of ARC's Projects
collection>' (data_layer/arc/README.md step 5)." A download folder outside `collection_root` is
refused before anything is asked (kind `command`: "The download folder `<x>` is not inside
`<root>`, the part of the cluster that ARC's Globus collection shows"). The file is still looked for
in the local folder. A download still unfinished at the time limit then has the detail "Transfer
`<task>` into `<target>` is still running. Look at its error at app.globus.org > Activity, and
cancel it there if it is stuck." Without `collection_root` (the instrument computer) nothing
changes.

### 5.6 Uploads to ARC

A finished scan session can go to the team's folder on VT ARC: a checked copy of the session
folder, the dark spectrum it is paired with, a manifest and a checksum list. The data layer does
every transfer, by Globus (the default) or SSH, or in demo mode into a folder on this Mac. The
window and the Studio only run the `arc-*` commands (section 6) and show what they answer. Nothing
on ARC is ever deleted, replaced or overwritten. None of this has been run against ARC, Globus or
ssh: only the demo and the tests ran.

Words: an **upload** is one request to put one session on ARC; it has a record and an id `u` + 12
lowercase hex digits (`^u[0-9a-f]{12}$`). An **attempt** is one try: one Globus task or one SSH
connection, numbered from 1. The **payload** is the snapshot of the session that an upload sends
("the upload copy on this Mac"). The **ARC folder** is the setting `arc_upload.folder`, a cluster
path. The **area** is `real` or `simulated`; the **base** is `<ARC folder>` or
`<ARC folder>/simulated`. The **final folder** is `<base>/<session folder>` (or
`<base>/<session folder>_copyN`), the **incoming folder** `<base>/_incoming/<session folder>.<upload id>`.
The **worker** is the upload program, `python -m raman_datafed.arc_worker`. The **request number** is
Globus's `submission_id`.

**Which sessions.** `HOME/scan_sessions/<name>` where `metadata.is_session_folder` is true, the name
does not start with `.` or `_`, and the entry is a real folder (not a link). Newest first by
`session.json`'s `started_utc`, else the folder's modification time. A session may go when all of
these hold; the first that fails gives the refusal (kind, `reason`):

| rule | holds when | otherwise |
|---|---|---|
| R1 name | the folder name matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and is not `simulated` in any case | `refused` / `odd_name` |
| R2 session.json | `metadata.read_session` reads it | `metadata` / `no_session_json` (missing), `unreadable_session` |
| R3 points | at least one `point_NNNN.json` | `not_ready` / `no_points` |
| R4 nothing has it open | no line of `/bin/ps -axo command=` holds the session id or the folder's absolute path (`ps` failing skips this, with the warning `ps_failed`) | `not_ready` / `running` |
| R5 quiet | the newest modification time (`lstat`) in the folder is at least `arc_upload.settle_seconds` old, unless controller-idle is in effect; up to 1 day in the future counts as "just now" | `not_ready` / `settling` (with `retry_after_seconds`), `future_time` |
| R6 contents | at most 4 levels deep, plain files and folders only; no folder `dark` directly in it; every name valid UTF-8 without control characters or a backslash | `refused` / `link`, `special_file`, `name_clash`, `odd_name` |
| R7 kind | the policy below | `refused` / `simulated_not_allowed`, `simulation_unknown`, `mixed`, `declare_real_not_unknown` |

Why 60 s is enough: while a scan runs the controller rewrites `response.txt` every 50 ms, a capture
holds that at most 15 s, and the motor program ends a scan after 30 s without an answer.
**Controller-idle** is in effect when the caller passes `--controller-idle`, `HOME/controller.lock`
exists and its first line is a process id that is alive. The window passes it only while it holds
the lock and no acquisition runs; otherwise the flag is ignored and the reply says
`"controller_idle_used": false`. `arc-sessions` judges R1 to R6 and the kind from `session.json`
alone; `arc-upload` and the worker judge everything, every point's capture block included. The
spectra, the files, the bytes and how the scan ended are counted before rule R2 is judged, so a
folder without `session.json` (one this edition did not record, until it is imported) is listed
with them.

**How the scan ended** (`end`). Planned points: `single` 1, `1d-x` 13, `1d-y` 13, `2d` 124 (4 Z
points and 120 grid points).

| `end` | when | the window's words |
|---|---|---|
| `failed` | `failure.txt` exists | ended with an error |
| `finished` | points >= planned (a `cancel.txt` written later by Stop or by closing the window does not count) | finished |
| `stopped` | `cancel.txt` exists and points < planned | stopped early (`<N>` of `<planned>`) |
| `ended` | none of these, or no planned count for the scan mode | ended (no end mark) |

Sessions that ended in any way may go; the manifest and the confirmation say how they ended.

**Measured or simulated** (`kind`). `session.json`'s `simulated` and, for `arc-upload` and the
worker, every point's `capture.simulated` are sources: two booleans give `real` (both false) or
`simulated` (either true); a missing or null value gives nothing. `mixed`: the sources give both;
`simulated`: some give `simulated`, none `real`; `real`: some give `real`, none `simulated`;
`unknown`: none gives anything (an imported session recorded without the flag).

| kind | live upload | demo upload |
|---|---|---|
| `real` | area `real` | area `real` |
| `simulated` | refused (`simulated_not_allowed`) unless `arc_upload.include_simulated` is true; then area `simulated` | area `simulated` |
| `unknown` | like `simulated` (`simulation_unknown`); with `--declare-real` like `real`, and the manifest says `"kind_declared_by_uploader": true` | the same |
| `mixed` | refused (`mixed`) | refused |

`--declare-real` for a session whose kind is recorded is refused (`declare_real_not_unknown`).

**Dark spectra.** The distinct `dark_file` names of `session.json` and of the capture blocks. For
each, in `HOME/dark_measurements/`: a missing `.txt` is not sent (warning `dark_missing`); a dark
that `metadata.unpaired_reason` does not pair with the session is not sent (warning
`dark_not_paired`, with that reason). The pairing is the catalog's own rule: it uses the
`simulated.backend` flag the session's files record (`true` or `false`; `false` for a session of kind
`unknown` declared real; `None` when the files record none and nothing was declared), so a session
whose motor alone was simulated is paired with a real dark, as the catalog pairs it;
otherwise the `.txt` and, when it exists, its `.json` go into `dark/` of the upload. A warning never
stops an upload; it is in the manifest (`darks[].sent`, `darks[].reason`), the record and the
confirmation.

**The snapshot** (state `preparing`, by the worker):

1. Free space: twice the session's size plus 50 MB must stay free, else `failed` / `no_space`.
2. Left out, and listed in the manifest's `excluded` with the reason: names starting with `.`,
   `Icon\r`, `Thumbs.db`, `desktop.ini`, an earlier `raman_upload_manifest.json` and `SHA256SUMS`,
   and the controller's temporary publish files `<known>.<6 letters or digits>` (`<known>` one of
   `session.json`, `point_NNNN.json`, `request.txt`, `response.txt`, `cancel.txt`, `failure.txt`,
   `advanced.txt`, `laser_log_combined.txt`, `command.txt`); such a file younger than
   `settle_seconds` makes the session `settling` unless controller-idle is in effect.
3. Each file is copied into `payload.tmp-<random>/<session folder>/` (darks into `dark/`), read in
   1 MiB blocks while SHA-256 and MD5 are computed, written and `fsync`ed. The session folder is
   opened once (`O_DIRECTORY | O_NOFOLLOW`) and must be the folder the listing found (device and
   inode); each of its files is opened through it, one name at a time with `O_NOFOLLOW`, so a link
   anywhere on the way is never followed, and the opened file must be the very file the listing
   found (device, inode, size, `st_mtime_ns`). The darks are opened by their path and must match
   the `lstat` taken at the start of the attempt, and again at its end. Size, `st_mtime_ns` or
   `st_ino` differing between before, after and a new `stat` means a file changed. Each copy gets
   the source's times.
4. The folder is listed again and must equal the first listing, the folder itself included.
5. A change in 3 or 4: wait 2 s and start again, at most 3 times, then `failed` /
   `changed_during_copy`.
6. `raman_upload_manifest.json` and `SHA256SUMS` are written into the copy.
7. Files 0400 and folders 0700: the copy cannot be changed by mistake, and its owner can still
   delete a kept copy, since its folders are writable. The folder is renamed to `payload` and the
   record written.

`session.json` of an imported session holds `imported.source_folder`, the absolute path it was
imported from, with this Mac's account name in it. The snapshot's copy keeps only the last name of
that path (of this Mac's path, a `~` path or a Windows path), written as `import-session` writes
the file (`indent=2`, `ensure_ascii=False`); the manifest's `adjusted` says so, and the file on this
Mac is not changed. Any other value is copied as it is.

Copies of the manifest and `SHA256SUMS` stay in the record folder. The payload is kept in every
state but `verified`, because Retry same request sends exactly it again; a retry first checks it
against its own `SHA256SUMS` (`failed` / `payload_changed`). At the end of an upload the worker
writes `verified` first, with the payload still kept; only then does it delete the payload and
compare the session folder with the manifest, in a second write, so a crash in between leaves a
`verified` upload, and the next worker of that upload (a Check status) deletes the payload it
finds. The comparison: only a `cancel.txt` added gives the note "cancel.txt was written after the
upload (closing the window or pressing Stop writes it); the scan data on ARC is complete."; any
other difference gives the warning "The session folder on this Mac changed after it was copied for
this upload (`<files>`). ARC has the copy from `<time>`." (a `session.json` the snapshot adjusted
is compared as it was sent).

**Manifest** `raman_upload_manifest.json`, written as `json.dumps(value, indent=2,
ensure_ascii=False) + "\n"`, so that the line `"upload_id": "<id>",` appears exactly like that (the
remote script looks for it):

```json
{
  "schema": "raman-arc-upload-manifest/1",
  "upload_id": "u3f9a0c1d2e4b",
  "made_utc": "2026-11-15T16:00:03.412Z",
  "transport": "globus",
  "demo": false,
  "session": {
    "folder": "1d_x_scan_20261113_101500_123",
    "session_id": "5f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f",
    "scan_mode": "1d-x",
    "started_utc": "2026-11-13T15:15:00.123Z",
    "sample_id": "S-001",
    "points": 13,
    "points_planned": 13,
    "end": "finished",
    "kind": "real",
    "kind_declared_by_uploader": false,
    "simulated": {"backend": false, "motor": false}
  },
  "darks": [
    {"file": "dark_20261113_101400_001.txt", "kind": "real", "sent": true, "reason": null,
     "path": "dark/dark_20261113_101400_001.txt", "description": "dark/dark_20261113_101400_001.json"}
  ],
  "files": [
    {"path": "advanced.txt", "bytes": 2, "sha256": "<64 hex>"},
    {"path": "dark/dark_20261113_101400_001.json", "bytes": 512, "sha256": "<64 hex>"},
    {"path": "dark/dark_20261113_101400_001.txt", "bytes": 21034, "sha256": "<64 hex>"},
    {"path": "point_0001.json", "bytes": 29852, "sha256": "<64 hex>"}
  ],
  "totals": {"files": 21, "bytes": 433104},
  "excluded": [{"path": ".DS_Store", "reason": "a hidden file"}],
  "adjusted": [],
  "destination": {"arc_folder": "/projects/pcl_photonics/raman_globus_upload", "area": "real"},
  "software": {
    "controller": {"name": "portable-raman-controller-datafed", "version": "1.0.0"},
    "data_layer": {"name": "raman_datafed", "version": "<raman_datafed.__version__>"}
  }
}
```

`files`: every file of the upload except the manifest and `SHA256SUMS`, sorted bytewise by path,
lowercase hex; `totals` counts every file sent, those two included. `adjusted`: what the snapshot
changed in a file it sends, `[{"path": "session.json", "what": "imported.source_folder shortened to
the folder name"}]` for an imported session (above), else `[]`; `files` gives the size and SHA-256
of the file as it was sent. `simulated` is `session.json`'s object or `null`;
`software.controller` its `software` or `null`. Never in the manifest: an absolute path, the Mac's
account or host name, the VT username, the Globus identity, this Mac's collection ID, the record
folder. Operator, Location and Notes stay where the controller wrote them. The SHA-256 of a point
file equals `raw_file.sha256` of its raw record (section 5.3), so a copy on ARC and a catalog
record can be matched. `SHA256SUMS`: one line `<64 hex>  <path>` (two spaces) per sent
file, the manifest included and itself left out, sorted bytewise by path, so that
`sha256sum -c SHA256SUMS` works in the folder on ARC.

**Layout on ARC**, the same for both transports and the demo:

```
<ARC folder>/                                   default /projects/pcl_photonics/raman_globus_upload
  <session folder>/                             a complete, checked upload (area real)
     session.json, point_NNNN.json, request.txt, response.txt, advanced.txt,
     laser_log_combined.txt, cancel.txt, failure.txt   (those the session has)
     dark/dark_<stamp>.txt, dark/dark_<stamp>.json
     raman_upload_manifest.json
     SHA256SUMS
  <session folder>_copy2/                       a new copy, when the session's own name was taken
  _incoming/<session folder>.<upload id>/       an upload in progress, or left by one that stopped or failed
  simulated/<session folder>/                   area simulated (only when allowed, or in the demo)
  simulated/_incoming/<session folder>.<upload id>/
```

1. An upload writes only into its own incoming folder. It makes the ARC folder, `_incoming/`,
   `simulated/` and `simulated/_incoming/` when they are missing.
2. The incoming folder becomes the final folder by one rename, which fails when the name exists
   (Globus `operation_rename` answers `Exists`; over SSH a check, then `mv -T` on GNU). Nobody on
   ARC sees a half-copied session under a session name.
3. Nothing on ARC is deleted, replaced or overwritten: `delete_destination_extra` is always false;
   no delete task, no `operation_delete`, no remote `rm`. The one exception: a retry of the same
   upload rewrites files in its own incoming folder with the same content.
4. A taken final name is the state `conflict`, found while checking (nothing sent) or at placing
   (the copy complete and checked in `_incoming`). The person chooses Keep as new copy, or moves
   the other folder on ARC and presses Retry same request.
5. A new copy (Keep as new copy, or an upload made with `--new` while a copy of the session is on
   ARC) is put under the first free name of `<session folder>`, `<session folder>_copy2` ...
   `_copy99`, the session's own name first. Globus, SSH (`upload-copy` mode) and both demos follow
   this rule.
6. A stopped or failed upload's incoming folder stays on ARC; the record and the window name it
   ("Files copied so far are in `<path>` on ARC; nothing was deleted.").
7. Permissions. The ARC folder, `_incoming/`, `simulated/` and `simulated/_incoming/` are shared by
   the project. Each of them that an SSH run makes gets mode 3770 (group rwx, setgid, sticky;
   `ls -ld` shows `drwxrws--T`): every member of the project group can add an incoming folder and
   rename it into place, and a member's folder in it can be renamed or deleted only by that member
   (and by the shared folder's owner). A folder that
   exists already is never changed; README section 14 has the one-time step that makes the four
   shared folders this way. Folders above the ARC folder that a run makes, the upload's own folders
   and its files get `umask 027`: 0750 and 0640 (on ARC the upload's folders also keep the setgid
   bit they inherit, so their files belong to the project group). Over SSH, an `_incoming/` or
   base folder this account may not write into ends the upload before anything is received
   (`shared_folder_not_writable`, whose fix names `chmod 3770 <folder>`). Folders and files that
   Globus makes get what ARC's collection sets: this program sets no mode over Globus.

**The ARC folder and the Globus path rule** (`raman_datafed/arc_paths.py`). `arc_upload.folder` is
the path that `ls` on ARC shows. ARC's Projects collection (`arc_upload.globus.arc_collection`) shows
the cluster folder `arc_upload.globus.collection_root` (`/projects`) as its top `/`. Every Globus
path of an upload (final, incoming, `simulated/`) is computed from its cluster path with one
function, so both transports land in one folder:

```
collection_path(cluster_path, root):
    root == "/"                       -> cluster_path unchanged
    cluster_path == root              -> "/"
    cluster_path starts with root+"/" -> "/" + the rest      (/projects/pcl_photonics/x -> /pcl_photonics/x)
    otherwise                         -> ValueError("<path> is not inside <root>")
```

`check_cluster_folder(path, root)` returns the problems as sentences, none when the folder is valid:
absolute; every part matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` (so no `.`, `..`, leading `-` or
`.`, no empty part, no `/` at the end); at most 200 characters; inside `root` with at least two parts
below it (`/projects/<dir>/<folder>`): `/projects` and `/projects/<dir>` alone are refused. There is
no second "Globus folder" setting. The ARC job uses the same module (`relative_below`, section 5.5).
In the demo, `demo_local_path(demo_root, cluster_path)` = `demo_root / cluster_path.lstrip("/")`.

**Records.**

```
HOME/data_catalog/arc_uploads/                 0700
  requests.lock                                flock, held while arc-upload looks for earlier uploads and creates one
  <upload id>/                                 0700, one folder per upload
    record.json                                0600, the record; replaced atomically and flushed to the drive
    record.lock                                flock for every read-modify-write of record.json
    worker.lock                                flock held by the process that works on this upload, as long as it does
    events.jsonl                               0600, one line per step, append-only
    payload/<session folder>/...               the snapshot: files 0400, folders 0700; deleted after "verified"
    raman_upload_manifest.json, SHA256SUMS     0600 copies, kept after the payload is deleted
    payload_hashes.json                        0600, the size, SHA-256 and MD5 of every payload file, from the snapshot
    globus_request.json                        0600, Globus: the exact body of the current attempt, flushed to the drive before every POST
    ssh_upload.command                         0700, live SSH: the script Terminal runs for the current attempt
    worker.log                                 0600, the worker's own lines
HOME/data_catalog/arc_demo/                    0700 with the marker file .raman_arc_demo when the demo made it: the demo's stand-in for ARC
```

"Flushed to the drive" (`arc_records.durable_sync`): `fcntl(F_FULLFSYNC)` of the file and then of
its folder, falling back to `fsync` where that is missing or refused. On macOS `fsync` only hands
the data to the drive, whose cache can still lose it in a power cut, and a request number must be
on disk before the request that carries it is posted. Other writes use `fsync`.

A record folder is made as `.creating-<random>` with a complete `record.json` and then renamed, so
no reader sees a folder without a record; readers ignore names starting with `.`, and `arc-upload`
removes a `.creating-*` folder older than 10 minutes (left by a crash) while it holds `requests.lock`. `record.json`
(schema `raman-arc-upload/1`):

```json
{
  "schema": "raman-arc-upload/1",
  "upload_id": "u3f9a0c1d2e4b",
  "created_utc": "2026-11-15T16:00:02.103Z",
  "updated_utc": "2026-11-15T16:02:41.877Z",
  "transport": "globus",
  "demo": false,
  "state": "verified",
  "state_since_utc": "2026-11-15T16:02:41.877Z",
  "message": "On ARC: 21 files in /projects/pcl_photonics/raman_globus_upload/1d_x_scan_20261113_101500_123. Globus checked every file, and its MD5 checksums match this Mac's copy.",
  "error": null,
  "warnings": [],
  "notes": [],
  "resume": null,
  "conflict_at": null,
  "session": {"folder": "1d_x_scan_20261113_101500_123", "session_id": "5f0c1d2e-...", "scan_mode": "1d-x",
              "started_utc": "2026-11-13T15:15:00.123Z", "sample_id": "S-001", "points": 13,
              "points_planned": 13, "end": "finished", "kind": "real", "kind_declared_by_uploader": false},
  "destination": {
    "key": "live:/projects/pcl_photonics/raman_globus_upload|real",
    "arc_folder": "/projects/pcl_photonics/raman_globus_upload",
    "area": "real",
    "base": "/projects/pcl_photonics/raman_globus_upload",
    "name": "1d_x_scan_20261113_101500_123",
    "final_path": "/projects/pcl_photonics/raman_globus_upload/1d_x_scan_20261113_101500_123",
    "incoming_path": "/projects/pcl_photonics/raman_globus_upload/_incoming/1d_x_scan_20261113_101500_123.u3f9a0c1d2e4b",
    "globus_base": "/pcl_photonics/raman_globus_upload",
    "globus_final_path": "/pcl_photonics/raman_globus_upload/1d_x_scan_20261113_101500_123",
    "globus_incoming_path": "/pcl_photonics/raman_globus_upload/_incoming/1d_x_scan_20261113_101500_123.u3f9a0c1d2e4b",
    "demo_root": null,
    "new_copy": false,
    "placed_as": "1d_x_scan_20261113_101500_123",
    "local_final_path": null
  },
  "settings_used": {"folder": "/projects/pcl_photonics/raman_globus_upload", "collection_root": "/projects",
                    "mac_collection": "<this Mac's collection ID>", "arc_collection": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
                    "client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2", "deadline_hours": 24, "force_login": false,
                    "ssh_username": null, "ssh_host": null, "ssh_known_hosts": null,
                    "include_simulated": false, "settle_seconds": 60},
  "payload": {"files": 21, "bytes": 433104, "manifest_sha256": "<64 hex>", "kept": false},
  "progress": {"files": 21, "files_done": 21, "bytes": 433104, "bytes_done": 433104},
  "requests": {"stop_utc": null},
  "worker": null,
  "globus": {
    "identity": "<the Globus user name shown at sign-in>",
    "attempt": 1,
    "submission_id": "0b9e2d4c-...",
    "request_saved_utc": "2026-11-15T16:00:09.551Z",
    "label": "Raman upload u3f9a0c1d2e4b a1 1d_x_scan_20261113_101500_123",
    "posts": [{"utc": "2026-11-15T16:00:09.602Z", "outcome": "accepted"}],
    "task_id": "6f1d2c3b-...",
    "task": {"status": "SUCCEEDED", "nice_status": null, "nice_status_short_description": null, "files": 21,
             "files_transferred": 21, "files_skipped": 0, "bytes_transferred": 433104, "faults": 0,
             "fatal_error": null, "deadline": "2026-11-16T16:00:09+00:00", "completion_time": "2026-11-15T16:02:30+00:00",
             "is_paused": false, "checked_utc": "2026-11-15T16:02:31.004Z"},
    "earlier_attempts": []
  },
  "ssh": null,
  "verification": {"method": "globus-checksum", "algorithm": "MD5", "files": 21, "utc": "2026-11-15T16:02:39.110Z"},
  "checks": [],
  "demo_fault": null,
  "controller_idle_used": false
}
```

- `destination.key`: `live:<ARC folder>|<area>` or `demo:<demo root>|<area>`; with the session id it
  names "the same session to the same place".
- Demo records: `demo` true, `destination.demo_root` set, the `globus_*` paths are the demo
  collection's, and `destination.local_final_path` is the folder on this Mac.
- SSH records: `globus` null, the `globus_*` paths null, and `"ssh": {"attempt", "host", "username",
  "known_hosts", "terminal_opened_utc", "runs": [{"attempt", "action", "mode", "started_utc",
  "ended_utc", "exit_code", "outcome"}], "sent"}`. A run is written when it starts and completed when
  it ends: `action` is `send` or `check`, `mode` `upload`, `upload-copy` or `check`, and `outcome` one
  word: `placed`, `already`, `conflict`, `staged_complete`, `staged_partial`, `missing`, `elsewhere`,
  `fail_<word>` (the word of `RAMAN FAIL`), `stopped`, `cut`, `no_reply`, `stream_failed` or
  `not_started`. `sent` is true once a run printed `RAMAN STEP receive` or a later line: from then on
  something of the upload may be on ARC (Check status, `leftover_on_arc`). `may_be_placed` is true
  once a run reached the script's rename (`STEP place`) or found this upload's copy in place, and is
  set again at the end of every run from what it found: while it is true the copy may be in its
  final folder, so `leftover_on_arc` is `null`. `terminal_open_failed_utc` is set when `open` could
  not start Terminal for the current attempt, and cleared by the next one that does. A demo SSH
  upload also has `demo_fault_used` once its teaching fault was shown.
- Globus records: `globus` also has `rename` (`{"from", "to", "name", "utc"}`, written before each
  rename is asked for), `verification` (kept between `verifying` and `placing`; copied to the top-level
  `verification` at `verified`) and, in the demo, `demo_fault_used`. An entry of `earlier_attempts` has
  `attempt`, `submission_id`, `task_id`, `status`, `fatal_error` (`code` and `description` only),
  `label` and `posts`.
- `destination.placed_as`: the name the copy got on ARC; with a `_copyN` name, `final_path`,
  `globus_final_path` and `local_final_path` name that folder too, so Copy ARC path and Show in
  Finder find it.
- `controller_idle_used`: whether `arc-upload` used `--controller-idle`; the upload program then
  skips rule R5 when it checks the session again. `settings_used` also keeps `include_simulated` and
  `settle_seconds`: the worker checks the session again with the settings saved when the upload was
  made, never with newer ones.
- `warnings` and `notes` are lists of sentences.
- `worker`: `{"pid", "action": "send"|"check"|"stop", "started_utc", "terminal": bool}`. A command
  that starts a worker (`arc-upload`, `arc-retry`, `arc-keep-copy`, `arc-check`, and `arc-stop` when
  it starts a stop worker) first writes the worker it expects, with `pid` and `started_utc` null, so
  the listing knows what a worker does from the moment it takes `worker.lock`. The worker checks that
  it is its turn and writes its own `pid` and `started_utc` in one step under `record.lock`, and
  sets `worker` to `null` at its end, whatever the end. Whether a worker is alive comes from
  `worker.lock` alone: readers use `worker` only while that lock is held.
- `error`: `{"kind", "reason", "message", "fix", "retryable"}`. Its `kind` is `upload` for a result of
  the table below (`internal` for `internal`), or the kind of the refusal of section 6 that the upload
  program met when it checked the session again (for example `not_ready` when the session is being
  written again; such an error is retryable only when its kind is `not_ready`). In a demo upload the
  record's `message` starts with `Demo: `; the `message` inside `error` does not.
- `resume`: `{"at": "<state>"}`, set by `arc-retry` and `arc-keep-copy`, cleared when reached.
  `conflict_at`: `"checking"` or `"placing"` while in `conflict`. `checks`: `[{"utc", "ok",
  "looked", "message", "fix"}]`, the results of Check status on a `verified` upload, newest last.
  `"looked": true` for a check that looked at the copy: `ok` says whether it found it as it was sent,
  and `fix` is `null`. `"looked": false` (with `"ok": false`) for a check that could not look: a
  sign-in that did not happen, ssh that ended before it signed in or before the remote script ended,
  Globus out of reach or a listing it gave only in part, a refusal or an error of this program. Its
  `message` says why and ends "Check status could not look."; its `fix` says what to do (table
  below). An entry without `looked` counts as one that looked. While the newest entry that looked
  has `"ok": false`, the listing shows the upload as `check_failed` (below); a check that could not
  look says nothing about the copy: it never makes an upload `check_failed`, never counts for
  attention and never lets `arc-upload` go ahead without `--new`.
- Only these fields of Globus's task are kept. Never kept anywhere: tokens, sign-in codes and URLs,
  the PKCE verifier, passwords, Duo answers, ssh output other than the parsed `RAMAN` lines,
  environment variables.
- A record of a newer schema is listed read-only ("made by a newer version"); a record that cannot
  be read, or whose keys do not have the types above (a hand edit, a partial restore: "record.json
  of `<id>` does not have the shape of a record"), is listed with state `unreadable` and its error,
  and is not counted by `status`. Nothing acts on either, and neither hides the other uploads.

**States.** The data layer makes the labels; the window and the Studio show them and make none of
their own.

| state | `state_label` | who works on it | kind |
|---|---|---|---|
| `queued` | Waiting to start | a detached worker, once started | working |
| `waiting_for_terminal` | Waiting for Terminal | the Terminal worker, once started | working |
| `preparing` | Preparing the files | the worker | working |
| `signing_in` | Signing in | the worker (Globus: the browser; SSH: password and Duo in Terminal; demo: a pause) | working |
| `checking` | Checking ARC | the worker | working |
| `submitting` | Handing over to Globus | the worker | working |
| `transferring` | Copying with Globus / Copying over SSH | the worker | working |
| `verifying` | Checking the copy on ARC | the worker | working |
| `placing` | Putting the copy in place | the worker | working |
| `uncertain` | Not sure Globus got it | nobody: it waits for the person | waiting |
| `conflict` | Name already on ARC | nobody: it waits for the person | waiting |
| `verified` | On ARC, checked | nobody | final |
| `failed` | Failed | nobody | final until retried |
| `stopped` | Stopped | nobody | final until retried |

`transferring` is "Copying with Globus" for Globus and "Copying over SSH" for SSH. Demo uploads use
the same labels; their messages start with `Demo: `.

**Transitions** (`arc_records.TRANSITIONS`; any other raises). Globus, live and demo: `queued ->
preparing -> signing_in -> checking -> submitting -> transferring -> verifying -> placing ->
verified`. SSH live: `queued -> waiting_for_terminal -> preparing -> signing_in -> checking ->
transferring -> verifying -> placing -> verified`; SSH demo the same without
`waiting_for_terminal`. After `signing_in`, both may move forward over steps that are done (SSH: the
remote script decides what is left to do; Globus: an upload resumed at `resume.at`). Also:

| from | to | when | written by |
|---|---|---|---|
| (none) | `queued` | the record was created | `arc-upload` |
| `queued` | `waiting_for_terminal` | live SSH: `open` started Terminal for the current attempt | `arc-upload`, `arc-retry`, `arc-check`, `arc-keep-copy` |
| `checking` | `conflict` | Globus: the final name exists and `new_copy` is false; nothing was sent | worker |
| `placing` | `conflict` | SSH `CONFLICT`, or the Globus rename answered `Exists`; the copy is complete and checked in `_incoming` | worker |
| `submitting` | `uncertain` | three POSTs without a usable answer; or a retry whose sign-in or consent did not happen while Globus may hold its request (through `submitting`, with no event: nothing was posted) | worker |
| `uncertain` | `transferring` | a check found the task by its label | check worker |
| `verifying` | `verified` | SSH `DONE already` with matching digests | worker |
| any working state | `failed` | an error; `error` filled | worker |
| any working state | `stopped` | a person's stop handled (`requests.stop_utc`, or Ctrl-C in Terminal); a signal that only ends the process writes no state (Workers, below) | worker |
| `queued`, `waiting_for_terminal`, `conflict`, or a working state with no worker and nothing running remotely | `stopped` | `arc-stop` | `arc-stop` |
| `uncertain`, or a Globus working state with no worker and a request number or task id | `stopped` | a stop worker settled the request and cancelled any task | stop worker |
| `uncertain`, `conflict`, `failed`, `stopped`, a working state with no worker | `queued` (detached) or `waiting_for_terminal` (live SSH), with `resume` | Retry same request, Keep as new copy | `arc-retry`, `arc-keep-copy` |
| `transferring`, `verifying`, `placing` (no worker), `failed`, `stopped`, `uncertain` | a later state on the Globus path | a check worker found the task running or finished | check worker |
| `verified` | `verified` | a check; its result is added to `checks` | check worker |

**When nobody works on an upload, and a failed check.** A worker is alive while it holds
`worker.lock` (tested with `flock(LOCK_EX | LOCK_NB)`, given back at once). `arc-list` and
`arc-status` add `display_state` and `display_label`; they never write a record. The first row
that applies gives them:

| condition | `display_state` | `display_label` | message |
|---|---|---|---|
| a `verified` upload whose newest `checks` entry that looked has `ok: false`, also while a new check works on it, and after later checks that could not look | `check_failed` | On ARC, last check failed | that entry's message |
| a worker holds `worker.lock` | the stored state | the `state_label` | the record's `message` |
| a Globus working state `transferring`, `verifying` or `placing` with a task id, no worker | `not_watched` | Not being watched | Globus keeps copying, but this Mac stopped watching. Press Check status to look; it asks for a Globus sign-in. Keep this Mac awake until the copy is done. (Demo: Globus keeps copying, but this Mac stopped watching. Press Check status to look.) |
| `queued` or `waiting_for_terminal` for more than 120 s, no worker; at once for a live SSH upload in `waiting_for_terminal` whose `ssh.terminal_open_failed_utc` is set | `not_started` | Not started | Live SSH: The Terminal window did not start the upload. If it says "Operation not permitted", allow Terminal to use the Downloads folder: System Settings > Privacy & Security > Files and Folders. Then press Retry same request. If no Terminal window opens, double-click ssh_upload.command in the upload's folder. Others: The upload program did not start. Press Retry same request. |
| any other working state, no worker | `interrupted` | Interrupted | The upload program stopped (for example when this Mac restarted). Retry same request continues where it stopped. |
| `uncertain`, `conflict`, `verified`, `failed`, `stopped` | the stored state | the `state_label` | the record's `message` |
| a `record.json` that cannot be read (its `state` is `unreadable` too) | `unreadable` | Cannot be read | why it cannot be read |

**Uploads that need a person** (`status`'s `arc_attention`, `arc-list`'s `attention`): uploads, live
and demo, whose display state is `failed`, `conflict`, `uncertain`, `interrupted`, `not_started`,
`not_watched` or `check_failed` and whose `updated_utc` is less than 7 days old
(`arc_records.ATTENTION_DAYS`). `worker.lock` is looked at only for records in a working state. The
window colours its ARC upload button from this count (danger) and from `arc_unfinished` (warning).

**Locks and writes.** Every write of `record.json`, by any process, is a read-modify-write under
`record.lock` (blocking, 5 s, then `internal`), written to a temporary file beside it that is
renamed into place, and flushed to the drive, the file and then the record folder
(`arc_records.write_record`), so a command that sets `requests.stop_utc` and a worker that moves the
state never lose each other's change, and a step written before something is sent is still there
after a power cut. `worker.lock` is taken with `flock(LOCK_EX | LOCK_NB)` by the one process that
works on the upload (3 tries over 2 s; taken: the worker exits 3, and a stop worker first writes
`requests.stop_utc`, so that the worker holding the upload stops); it goes away with the process,
whatever ends it, and a process id in the record is signalled only while the lock is held.
`requests.lock` (blocking, 10 s) is held by `arc-upload` while it looks for earlier uploads and
creates the record, so two clicks, or the window and the Studio, make one upload.

**Actions.** Every listing item has `actions`; the window and the Studio enable their buttons from it
and nothing else.

| display state | `actions` |
|---|---|
| any, while a worker is alive (a send, a check or a stop worker) | `stop`, and for a `verified` upload also `copy_path` (live) or `show_in_finder` (demo); nothing else starts while a worker works |
| a working state whose worker is starting (shown as the stored state) | `stop` |
| `not_watched` | `check`, `stop` |
| `interrupted`, `not_started` | `retry` when the payload is available; `check` when the upload is checkable; `stop` |
| `uncertain` | `retry` when the payload is available; `check`, `stop` |
| `conflict` | `keep_copy` and `retry` while the payload is there; `stop` (stop gives up; the checked copy stays in `_incoming`) |
| `failed` | `retry` when `error.retryable` and the payload is available; `check` when the upload is checkable |
| `stopped` | `retry` when the payload is available; `check` when the upload is checkable |
| `verified`, `check_failed` | `check`, and `copy_path` (live) or `show_in_finder` (demo) |

The payload is available when no snapshot was made yet (the next worker makes it), or when the
record says it is kept and `payload/` is in the record folder. An upload is checkable when something
of it may be on ARC (a Globus task id, of this or an earlier attempt, or an SSH run that got past
`STEP receive`), or when Globus has its request number (the check looks the transfer up by its
label). Stop is offered whenever a worker works on the upload, a Check status included: `arc-stop`
then sets `requests.stop_utc`, and the worker ends what it does. A stopped SSH check changes
nothing: it ends ssh with SIGTERM. A Globus check is stopped as the listing's `may_run` was when the
check started; the window's Stop question followed `may_run` then. A Globus check that started with
`may_run` false (nothing of the upload could run on Globus any more: `verified`, `conflict`, or an
upload whose task had ended or that had no request Globus may hold) changes nothing either: it
stops waiting for its sign-in at once, or, while it checks or places the copy of the transfer that
had ended (`verifying`, `placing`), it ends before its next step. Each stopped check leaves the
stored state, message, error and `checks` as they were, and the history gets "The check was not
finished: it was stopped." The one exception: an upload that such a check found in `transferring`
or `uncertain` (its task had ended) and had moved on to `verifying` when the stop came stays at
`verifying`, shown as not watched, since no allowed state change leads back. A Globus check that
started with `may_run` true (its task was not known to have ended, or Globus might hold its request)
is stopped as Stop upload stops, to the end of the check: it waits for its sign-in and then cancels
the transfer. When that transfer has ended meanwhile and the check checks or places the copy,
nothing is cancelled and the upload ends `stopped` with the copy complete in `_incoming` ("The copy
had finished before the stop arrived. ...", or "Stopped while the copy was being put in place ..."
once a rename was asked for), as its Stop question said. Once such a check has written that the
task ended, the listing's `may_run` is false and the window asks the Stop question of a check that
changes nothing, but the stop still ends the upload `stopped`. Check workers never hold back another
upload: the refusal `another_live_upload` ignores them.

**Retry same request** never uses the current settings: it is the same request (`settings_used`,
the same payload and, for Globus, the same body while it is unanswered). Where it continues
(`arc_records.retry_target`):

| situation | `resume.at` |
|---|---|
| payload missing (the record says it is not kept, or `payload/` is gone) | refused: `command` / `payload_missing` |
| live SSH, any state that may be retried | none: a new attempt `ssh.attempt + 1`, state `waiting_for_terminal`; the remote script finds what is left: a copy already in place, or complete in `_incoming`, is only checked; otherwise every file is sent again, into the same incoming folder |
| demo SSH | none: state `queued`; the script finds what is left |
| Globus, `uncertain`, or a request number without a task id (interrupted in `submitting`, or a request Globus refused) | `submitting`: the same body and request number. A saved request that Globus refused at every post, or whose deadline has passed, is never sent again: the worker looks the transfer up by its label and makes a new attempt (event "The saved request is not sent again: Globus refused it, or its deadline has passed.") |
| Globus, a task id whose last status is ACTIVE or INACTIVE | `transferring`: watch it |
| Globus, a task id whose last status is SUCCEEDED | `verifying`: check the copy, then put it in place |
| Globus, the last task FAILED, or the check of the copy failed | `checking`: a new attempt; the old one moves to `earlier_attempts`; a new request number only after the old task is confirmed final. A transfer of the earlier request that Globus found by its label and that had SUCCEEDED is checked instead of copied again |
| Globus, `conflict` at `placing`, or interrupted in `placing` | `placing`: the rename is tried again |
| Globus, `conflict` at `checking`, or nothing sent yet | `checking` |

`arc-keep-copy` sets `destination.new_copy` and `resume.at` `placing` (conflict at `placing`) or
`checking` (Globus, demo Globus); over SSH it starts a new attempt in mode `upload-copy`. The copy
then takes the first free name of rule 5 of the layout above.

**Events.** `events.jsonl` holds one `{"utc", "state", "text"}` per line, appended with one `write`
and never rewritten; a last line without a newline is ignored. The texts are plain words, for
example "Saved request number 0b9e2d4c before sending it to Globus.", and never hold a token, a
sign-in URL, a password or a Duo answer. `arc-status` returns the last 30.

**Workers.**

```
python -m raman_datafed.arc_worker --home HOME --upload ID --action send|check|stop [--terminal --attempt N]
```

Like `python -m raman_datafed.arc_job` it is a program of its own, not a command of section 6: the
window never runs it and it prints no JSON object. Exit codes: 0 the upload is `verified` (also after
a check that found it so), 1 it ended `failed`, `stopped`, `conflict`, `uncertain` or not watched,
or a check did not find the copy, or was stopped or its window closed, 2 wrong arguments (or a
record folder that is a link: "There is no upload `<id>` on this Mac."), 3 not its turn
(`worker.lock` held, an older `--attempt`, or a state the action does not take), 97 a test fault,
130 Ctrl-C in its Terminal window, and 128 + N when signal N ended it without a final state (143
SIGTERM; 129 SIGHUP and 130 SIGINT of a detached worker). `send` takes `queued` (detached) or
`waiting_for_terminal` (`--terminal`, with `--attempt` equal to `ssh.attempt`); `check` takes the
states whose `actions` hold `check` when no worker runs, and `stop` (Globus only, with a request
number or task id) those whose `actions` hold `stop`.

Globus uploads and every demo upload run in a detached worker (`arc_worker.spawn`):
`subprocess.Popen([sys.executable, "-m", "raman_datafed.arc_worker", "--home", HOME, "--upload", ID,
"--action", ACTION], cwd=HOME, env=<the environment with the data layer's folder in front of
PYTHONPATH, without PYTHONSTARTUP and PYTHONINSPECT>, stdin, stdout and stderr /dev/null,
start_new_session=True, close_fds=True)`. It runs in a session of its own and holds no pipe of its
caller: closing the window or quitting the Studio does not end it, and the command that started it
returns at once. The long-lived Studio server reaps the workers it started at the start of every
`arc-*` command (`arc_worker.reap()`).

A live SSH upload runs the same program in a Terminal window. `arc-upload`, `arc-retry`, `arc-check`
and `arc-keep-copy` write `<record>/ssh_upload.command` (mode 0700, new for each attempt; every value
checked and written with `shlex.quote`) and run `["/usr/bin/open", "-a", "Terminal",
"<record>/ssh_upload.command"]` (no shell, 15 s limit):

```bash
#!/bin/bash
# Portable Raman: upload 1d_x_scan_20261113_101500_123 to ARC over SSH (upload u3f9a0c1d2e4b, attempt 2).
# Written by the data layer for this one attempt. It runs the data layer, which runs /usr/bin/ssh.
# Your VT password and Duo go to ssh only. This file holds no password, and nothing here saves one.
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
unset PYTHONSTARTUP PYTHONINSPECT PYTHONHOME BASH_ENV ENV SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY
export PYTHONPATH='<HOME>/data_layer'
cd '<HOME>' || exit 1
'<interpreter>' -m raman_datafed.arc_worker --home '<HOME>' --upload u3f9a0c1d2e4b --action send --terminal --attempt 2
status=$?
echo
read -r -p "Press Return to close this window. " _
exit $status
```

`<interpreter>` is `sys.executable` of the command that wrote the file, not resolved (a venv's
`bin/python` is a link). Check mode writes the same file with `--action check`. `open` goes through
Launch Services like a double-click in Finder and needs no Automation permission; the file stays in
the record folder, where a person can read it or double-click it. A Terminal window of an older
attempt prints "This Terminal window belongs to an earlier attempt of this upload; use the newest
one, or press Retry same request in the controller." and exits 3. When `open` fails, the record
stays as it is (a send stays `waiting_for_terminal`), so a double-click on `ssh_upload.command`,
or a Terminal that opens late, still runs that attempt; `ssh.terminal_open_failed_utc` is set, the
event "Terminal could not be opened: `<why>`." is added, the listing shows the upload as
`not_started` at once, and the command answers `command` / `terminal_not_opened` with `upload`.

Inside a worker: it takes `worker.lock`, reads the record again, checks the action and the state,
writes `worker`, and gives everything back at its end. Before its first step it imports every
module it may need (`arc_globus`, `arc_ssh`, `arc_demo`) and reads `arc_remote.sh`, so that a new
install of the data layer while it runs (`build_macos.sh`) cannot mix two versions in one run: a
running upload keeps the code it started with. It writes the record after every step and at most
once a second for progress, and reads `requests.stop_utc` every second (a stop worker does not: it
is the stop). While it sends from this Mac (live Globus and SSH) it runs `/usr/bin/caffeinate -i -w
<its pid>`. It imports no instrument code, and `globus_sdk` only inside the live Globus adapter.

Signals. A person's stop is `requests.stop_utc` (`arc-stop`, the Stop upload button) or, in a
Terminal worker, Ctrl-C (SIGINT) and closing the window (SIGHUP); only a person's stop may cancel
a Globus task. SIGTERM (a logout, a restart, a shutdown), and SIGHUP or SIGINT sent to a detached
worker, end the worker at once: nothing is stopped or cancelled and no final state is written;
only `worker` is cleared, the event "The upload program was ended by the system (`<signal>`) before
it finished. Nothing was stopped or cancelled." is added, and the exit code is 128 + N. An upload
in a working state is then shown as not watched or interrupted, and Check status or Retry same
request goes on from there; any other upload stays as it was. A Terminal send worker whose window
was closed ends `failed` / `terminal_closed` (unless it ended `verified` or in `conflict`); after
Ctrl-C a `failed` outcome of a working state becomes `stopped` (exit 130). A stopped Check status
leaves the stored state, message and error as they were, also a Globus check that was checking or
placing the copy of a transfer that had ended when the check started (a Globus check that started
while the transfer could still run is stopped as Stop upload stops: Actions, above). A stopped SSH
check, a stopped Globus check that started with `may_run` false, and an SSH check whose Terminal
window is closed also add the event "The check was not finished: it was stopped." (or "its
Terminal window was closed."), and an SSH check prints in its Terminal window "The check was
stopped. Nothing was changed."

A stop that comes as a send worker starts (after Retry same request) is acted on right after the
snapshot step, except for a Globus upload whose transfer may still run (its task is not known to
have ended, or Globus may hold its request): that worker signs in, looks the transfer up and
cancels it. A person's stop that comes while a send or check worker of a Globus upload ends
(signing out of Globus takes up to a few seconds), after the worker decided to leave a transfer
running and without having tried to stop it, is not dropped: the worker keeps `requests.stop_utc`,
writes the stop worker it expects into `worker`, adds the event "Stop requested as the upload
program ended; a worker cancels the transfer." and, once it has let go of the upload, starts a stop
worker, as `arc-stop` does for an upload that no program works on.

Logging. The worker logs to `worker.log` (1 MB, one old file kept) through a filter that drops any
line holding, in any letter case, `code=`, `state=`, `access_token`, `refresh_token`, `id_token`,
`Authorization`, `Bearer`, `code_verifier`, `client_secret` or `password`, or one of the whole
words `token`, `tokens`, `verifier`, `secret` and `passcode`, and that shows a run of 24 or more
characters shaped like a token or a code as `[hidden]` (a UUID and a session folder name stay). An
unexpected exception is logged as its type and its `file:line:function` frames, without its text,
and the record gets `failed` / `internal` with the type, which Retry same request may continue.

The end. The worker writes its outcome in one read-modify-write; a `verified` upload is written
first and its payload deleted afterwards (the snapshot, above). A Terminal worker then drops what
was typed in the window while it ran (`tcflush` of its input), so that a Return pressed during the
upload does not answer "Press Return to close this window", and prints its last lines: for Check
status the check's own message, followed, when it looked and did not find the copy, by "This upload
stays listed as on ARC, with its last check failed. Press Check status to look again, or Upload to
ARC to send the session again.", and, when it could not look, by its fix (for ssh that ended with
code 255 the check's own wording of the `ssh_signin_failed` fix); otherwise the upload's message,
and the fix of a failure. The history (`events.jsonl`, `arc-status`'s `events`) gets the check's
message, and for a check that could not look its fix after it.

A check that could not look (`"looked": false`) gets the fix of its reason. Retry same request is
not offered for an upload on ARC, so these name Check status. A check has reasons of its own where
another check would meet the same problem, so their fixes say who can clear it:
`folder_not_readable` (Globus answers 403 when it lists the copy, or the remote script prints `FAIL
access` in a check), `folder_refused` (`FAIL prepare` in a check: a folder on the way to the copy
is a link), and `remote_tools` and `ssh_host_refused_commands` (the check cannot run on the SSH
computer saved with the upload). A demo upload gets the `<reason>_demo` row where there is one. Every
check of a demo upload runs on this Mac and looks in the demo folder saved with the upload, never in
the one ARC settings name now, so no fix sends the person to another demo folder:

| reason of a check that could not look | fix |
|---|---|
| `signin_timeout` | Press Check status again and sign in when the page opens. |
| `no_browser` | Use the controller on the Mac's own screen, not over a remote connection. Then press Check status again. |
| `signin_failed` | Press Check status again and sign in with your Virginia Tech identity. |
| `consent` | Press Check status again and allow access to the ARC collection when Globus asks. |
| `globus_error` | Check that this Mac is online, then press Check status again. If it happens again, send this message to the team. |
| `ssh_signin_failed` | Check your password and Duo, and that you have an ARC account in the project that owns the ARC folder. ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN. Then press Check status again. If ssh says the host key changed, stop and ask the team: do not continue. |
| `internal` | Press Check status again. If it happens again, send the record folder's worker.log to the team. |
| `folder_not_readable` | Ask the team to check that you are still in the ARC project and may read its folder on ARC. Then press Check status again. |
| `folder_not_readable_demo` | Make that folder and the folders in it readable again for this Mac's account. Then press Check status again. |
| `folder_refused` | Nothing was changed. Ask the team to look at that folder on ARC, then press Check status again. |
| `folder_refused_demo` | Nothing was changed. Put the moved folder back in place of the link. Then press Check status again. |
| `remote_tools` | Nothing was changed. Ask the team to fix that computer: every check of this upload runs on it. Then press Check status again. |
| `remote_tools_demo` | Nothing was changed. Ask the team to fix this Mac: every check of this upload runs on it. Then press Check status again. |
| `ssh_host_refused_commands` | Nothing was changed. Ask the team to fix that computer: every check of this upload runs on it. Then press Check status again. |
| `ssh_host_refused_commands_demo` | Nothing was changed. Ask the team to fix this Mac: every check of this upload runs on it. Then press Check status again. |
| any other | Nothing was changed. Press Check status to look again. |

Test hooks: with `RAMAN_ARC_TEST_GUARD=1` (set by every ARC test and by `build_macos.sh --test`) the
data layer refuses to build the live Globus adapter, to run `/usr/bin/ssh`, `/usr/bin/open` or
`/usr/bin/caffeinate`, and to spawn anything but `raman_datafed.arc_worker`. `arc_worker.fault(name)`
ends the process with `os._exit(97)` only when `RAMAN_ARC_TEST_HOOKS=1` and `RAMAN_ARC_FAULT=<name>`
are both set. `RAMAN_ARC_DEMO_PACE` is the demo's seconds per file (default 0.1; tests 0). The fault
names, by the step they end: `create_before_rename` and `upload_before_spawn` (in `arc-upload`),
`snapshot_mid_copy`, `snapshot_before_record`, `check_mid`, `submit_before_post`, `submit_after_post`,
`poll_mid_transfer`, `verify_mid`, `place_after_rename`, `ssh_after_done` and
`verified_before_delete` (in the upload program).

The data layer's folder is the folder that holds the running `raman_datafed` package: `HOME/data_layer`
when deployed. The Terminal script's `PYTHONPATH` names the same folder.

**Globus** (`globus-sdk==4.10.0`, imported only by `arc_globus.LiveGlobus`). Each worker signs in
through the browser: tokens in that worker's memory only, no refresh tokens; a fixed consent label, so
the Mac's host name is not sent; the sign-in's request line is never printed or logged; a sign-in
longer than 10 minutes ends the worker (`signin_timeout`). The person who signed in is shown
("Signed in to Globus as `<name>`."); `arc_upload.globus.force_login` asks for the sign-in every
time. At its end the worker revokes every token it was given, those of a consent sign-in included,
with the app's own Globus Auth client: one try per token, at most 5 s each, and no more tries once
Globus Auth could not be reached; then it forgets them and closes its clients. A worker watches a
transfer for at most 90 minutes after its sign-in; after that, or when Globus asks for a new
sign-in while nobody may be at the screen, the upload is `not_watched` and Check status signs in
again. A Stop that arrives just as the watch ends is acted on before the tokens are revoked.

Consent. Globus may ask for ARC's collection consent (a second page in the browser). The worker
lets it ask only while the person who started it can be expected at the screen: within 10 minutes
of the end of its first sign-in, never while a transfer is watched, and for a send worker only
while it checks or submits. So a check worker, and a send worker that resumes at `transferring`,
`verifying` or `placing`, look at the ARC folder right after signing in, and Globus asks for the
consent then. One consent sign-in per worker; a second request is `failed` / `consent`.

Before it signs in, the worker checks the record's values again against what `arc-upload`
computes: the session folder name, the upload id, the collection and app IDs (UUIDs, this Mac's
collection not ARC's), the collection root and the deadline, the ARC folder and every path derived
from it, the name and `placed_as`, and that the upload copy lies inside the record folder. A value
that does not pass ends the upload as `failed` / `internal` (`RecordValueError`) before anything is
contacted; a check or stop worker of an upload in a state that is not a working state keeps it.

A sign-in or a consent that did not happen (`signin_timeout`, `no_browser`, `signin_failed`,
`consent`) asked nothing of Globus, so the upload keeps what Globus may hold. A check or stop
worker keeps the state and puts "Check status could not look." or "The upload was not stopped."
after the reason (a `verified` upload gets a `checks` entry with `"looked": false` and its fix
instead, and stays "On ARC, checked"). A send worker whose transfer may still run leaves it `transferring` (shown as not
watched), with "This Mac is not watching the transfer; Globus may still be copying. Press Check
status to look."; one whose request Globus may hold ends `uncertain`; any other ends `failed` with
the reason, and when files of the upload may be on ARC its message says where they are ("Nothing
more was sent; files copied earlier stay in `<incoming>` on ARC.", or "... the copy Globus finished
is complete in `<incoming>` on ARC."; a demo upload names the demo folder's path without "on ARC")
instead of "Nothing was sent."

While `checking`, the worker asks Globus for this Mac's collection and lists the payload through
it, page by page with `limit=1000`; the listing must equal the payload exactly (relative paths,
types, sizes, no links, nothing more). A Globus Connect Personal collection serves only the Globus
account that set it up: `get_endpoint` answering 403, or a refused listing of a collection whose
`owner_id` is not the Globus ID of the signed-in identity, is `failed` / `mac_collection_not_yours`;
404 is `mac_collection_unknown`, and any other refused listing `source_not_visible`. The worker
then makes the ARC folders it needs with `operation_mkdir` (`Exists` counts as done) and looks at
the final path (present and no new copy: `conflict`, nothing sent). The request body is built by
the data layer as a plain dict, so what is saved is what is sent:

```json
{
  "DATA_TYPE": "transfer",
  "submission_id": "<from get_submission_id()>",
  "source_endpoint": "<this Mac's collection ID>",
  "destination_endpoint": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
  "label": "Raman upload u3f9a0c1d2e4b a1 1d_x_scan_20261113_101500_123",
  "verify_checksum": true,
  "encrypt_data": true,
  "preserve_timestamp": true,
  "delete_destination_extra": false,
  "fail_on_quota_errors": true,
  "skip_source_errors": false,
  "deadline": "2026-11-16T16:00:09+00:00",
  "notify_on_succeeded": false,
  "notify_on_failed": true,
  "DATA": [{"DATA_TYPE": "transfer_item",
            "source_path": "<HOME>/data_catalog/arc_uploads/u3f9a0c1d2e4b/payload/1d_x_scan_20261113_101500_123/",
            "destination_path": "/pcl_photonics/raman_globus_upload/_incoming/1d_x_scan_20261113_101500_123.u3f9a0c1d2e4b/",
            "recursive": true}]
}
```

One recursive item; no `sync_level`, so every attempt copies every file; no `notify_on_inactive`.
The label holds the upload id and the attempt (`a1`), at most 128 characters of `[A-Za-z0-9._ -]`,
the session folder cut to fit, so a task is found by its exact label. The source path is the
payload's absolute, resolved path.

**Exactly once** (`arc_globus.GlobusFlow.submit`): before the first POST of an attempt the worker
takes a request number (`get_submission_id`), writes `globus_request.json` (flushed to the drive,
the file and the folder) and then the record with `submission_id`, `request_saved_utc`, `attempt`
and `label`. Every later send of that attempt sends exactly that body, checked against a body built
again from the record and against the payload's `SHA256SUMS` (otherwise `failed` /
`payload_changed`). The POST goes without the Globus SDK's own resends, so every POST is one the
worker counts. Globus answers a request number it has seen with `Duplicate` and the task of the
first request, so a resend never makes a second task. A network error, a time-out, HTTP 5xx, 408 or
429, or a reply without a task id is resent after 10 s and after 30 s; a third unknown outcome is
`uncertain` ("It is not known whether Globus received the request. Retry same request asks again
with the same request number; Globus cannot make a second copy from it."). HTTP 409, or any other
4xx after an earlier POST of the request had no answer or when an earlier worker saved the request:
the task is looked for by its label (one: adopted; none: `failed` / `request_conflict` or
`refused_by_globus`; several: `internal`). Any other 4xx is a definite refusal, never `uncertain`:
`failed` / `refused_by_globus`. A request that Globus refused at every POST, or whose deadline has
passed, is never sent again: Retry same request makes a new attempt (Retry same request, above). A
new attempt after a FAILED task moves the old request number, task id and error to
`earlier_attempts` and takes a new request number only after `get_task` confirmed the old task is
final: one upload never has two active tasks.

While `transferring`, `get_task` every 5 s for the first minute, every 15 s up to 10 minutes, then
every 60 s; INACTIVE is not final. While `verifying`, the payload is hashed again, and
`task_successful_transfers` must list every file exactly once with its size; where Globus gives a
checksum, the same algorithm computed here must give the same value (`verification.method`
`globus-checksum`), else `globus-verify` ("Globus compared every file with this Mac's copy after
copying it"); a listing of the incoming folder must hold exactly the expected names and sizes. Any
difference is `failed` / `verify_mismatch`, and the copy is not put in place. When Globus no longer
keeps the list of the files it copied (404, for a transfer that ended long ago) and shows the task
as `SUCCEEDED`, the copy is checked by that listing of the incoming folder alone, with the method
`globus-verify` (event "Globus no longer keeps the list of the files it copied, so the copy is
checked by listing it on ARC."); a task this identity may not see is `task_not_visible`. `placing`
renames the incoming folder to the final name with `operation_rename` (`Exists`: `conflict`; for a
new copy the next name of rule 5 of the layout; `NotFound` after a crash: the final folder's listing
is compared with the payload).

**Stop and Check status (Globus).** A stop worker signs in, settles a request whose reply was lost
(it looks the transfer up by its label), cancels the task and waits up to 2 minutes for it to end;
a cancel that gets no answer is asked for again at each look. When the task has not ended by then
the state stays as it is, with "Globus has not ended the copy yet. Press Stop upload again in a
minute."; when the cancel never reached Globus, or Globus could not be asked, with "The stop did
not reach Globus, so the copy may still be running. Press Stop upload again." A stop while the
request's answers were lost (it ends `uncertain`) says "It is not known whether Globus received the
request, so the stop could not cancel anything. Press Stop upload again: it looks for the transfer
and cancels it." `arc-stop` needs no stop worker, and so no sign-in, when nothing can run on
Globus any more: a `conflict`, or an upload whose task last showed `SUCCEEDED` or `FAILED`. Such an
upload stops at once with "The copy had finished before the stop arrived. It is complete in
`<incoming>` on ARC but was not put in place. Retry same request or Check status checks it and puts
it in place." (the task had succeeded), "Stopped while the copy was being put in place; it may
already be in its folder on ARC. Press Check status to see." (a rename had been asked for:
`globus.rename`), "Stopped before anything was sent." or "Stopped. Files already copied stay in
`<incoming>` on ARC; nothing was deleted." A demo upload says the same without "on ARC" (the copy is
in the demo folder: `arc_records.message(key, demo=True)`). An SSH upload that no program works on,
in `placing` or with `ssh.may_be_placed`, stops at once with the "Stopped while the copy was being
put in place" text, since the remote script may have renamed the copy into its folder already. Check status watches the task, and checks
and places the copy where that is left to do: a `stopped` or `failed` upload whose task succeeded is
checked and put in place. A task Globus does not show to the signed-in identity (403, 404) is
`failed` / `task_not_visible`. For a `verified` upload it lists the final folder and adds an entry
to `checks`: "Still on ARC: `<N>` files with the right sizes." (`ok: true`), "The folder on ARC
differs from this upload: `<n>` differ, for example `<files>`." or "The folder `<path>` is no longer
on ARC." (`ok: false`; a demo upload says "in the demo folder" instead of "on ARC"). A listing
Globus gives only in part ("Globus listed only part of the folder on ARC."), a listing Globus
refuses for this identity (403: "Your Globus identity may not list `<path>` on ARC."; demo: "Globus
may not list `<path>` in the demo folder.", `folder_not_readable`), a sign-in that did not happen,
and a network failure or an answer this program does not expect (`globus_error`) are a check that
could not look (`"looked": false`, with its fix). A watch that gets no answer from Globus for 10 minutes ends with "This Mac
could not reach Globus for a while and stopped watching. Globus keeps copying. Press Check status to
look. Keep this Mac awake until the copy is done." (the upload is then `not_watched`). While a stop
waits, no answer is a reason to look again. Outside a watch and a stop, a network failure, or an
answer this program does not expect, is `failed` / `globus_error`.

**SSH.** One attempt is one `/usr/bin/ssh` connection that runs one remote script: one password and
one Duo per attempt. The command line (an argument list, no shell):

```
/usr/bin/ssh -T -e none
  -o BatchMode=no -o ControlMaster=no -o ControlPath=none
  -o StrictHostKeyChecking=ask                    (default)
     | -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<arc_upload.ssh.known_hosts> -o GlobalKnownHostsFile=/dev/null
  -o ForwardAgent=no -o ForwardX11=no -o ClearAllForwardings=yes -o PermitLocalCommand=no
  -o RequestTTY=no -o RemoteCommand=none
  -o IgnoreUnknown=* -o StdinNull=no -o SessionType=default -o ForkAfterAuthentication=no -o ChannelTimeout=none
  -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=4
  -l <username> -- <host>
  bash -c "$(printf %s '<arc_remote.sh as base64>' | base64 --decode)" raman_arc_upload <MODE> '<base>' '<folder>' '<upload id>' '<name>'
```

`-o` options on the command line win over `~/.ssh/config`, so a person's config cannot give the
remote script an empty stdin (`StdinNull`), open no session (`SessionType`), fork away
(`ForkAfterAuthentication`) or close the session while ARC checks a big copy (`ChannelTimeout`).
`IgnoreUnknown=*` comes first, so an ssh too old to know one of these options ignores it, and a
person's own `IgnoreUnknown` keeps working (`arc_ssh.PINNED_OPTIONS`). Host keys: by default the
person's own `~/.ssh/known_hosts` and `StrictHostKeyChecking=ask`, so the first connection shows
ARC's key and asks; with `arc_upload.ssh.known_hosts` a team-checked file and
`StrictHostKeyChecking=yes`. `accept-new` and `no` are never used, and the data layer never edits a
known-hosts file. ssh reads the password, Duo and any host-key question from the terminal, never
from the worker's pipes. The worker streams a tar of the payload on stdin (PAX format, the manifest
first, then folders and files sorted by path, relative names, uid and gid 0 with empty names, modes
0640 and 0750); check mode sends nothing. The stream stops, and ssh's stdin is closed, as soon as
the script prints a `RAMAN` line other than `STEP prepare` or `STEP receive`: after it the script
reads no more files.

The Terminal window of a live SSH attempt first says what is sent where, then the questions ssh
asks, in the order it asks them (`arc_ssh.terminal_text`; the window's SSH confirmation uses the
same words): only while ARC's key is not known yet (no team file), the host-key question ("The
first time only: ARC's key fingerprint, then "Are you sure you want to continue connecting
(yes/no/[fingerprint])?". Type only yes or no here; what you type is shown on screen, so never type
your password at this question. ..."); then "Your VT password. Typing shows nothing; that is
normal. Press Return."; then "Duo: approve the push on your phone, or type your password followed
by a comma and a six-digit passcode at the password question. Choose Duo Push if a numbered menu
appears." With the team file it says "ssh checks ARC's key against the team's file `<path>`; it does
not ask." instead of the first question. Then: "ssh may print a WARNING that the connection is not
using a post-quantum key exchange algorithm. That is expected with ARC (ARC's FAQ explains it);
continue.", "What you type goes to ssh only: this program does not see, keep or log it.", "ARC
refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN.", the line about REMOTE HOST
IDENTIFICATION HAS CHANGED, and how to stop (Ctrl-C). No `WarnWeakCrypto` option is passed: older
OpenSSH refuses options it does not know.

The remote script, `raman_datafed/arc_remote.sh`, is sent with every upload, so ARC needs nothing
installed beforehand (`bash`, `base64`, `mkdir`, `chmod`, `find`, `sort`, `sed`, `tar`, and
`sha256sum` or `shasum`). Arguments `MODE BASE FOLDER UPLOAD_ID NAME`: `MODE` is `upload`,
`upload-copy` or `check`; `NAME` is the final name, the session folder or the name an earlier run
placed it under. It reads stdin to its end, writes only lines starting `RAMAN ` on stdout, never
deletes anything, and writes only into this upload's incoming folder, the shared folders it makes
(rule 7 of the layout) and the one rename. A `FAIL` line comes before the rest of stdin is read, so
this Mac stops sending at once. Its steps:

1. The ARC folder (`BASE` without `/simulated`) and `BASE` must not be links (every mode). Mode
   `check` then names a folder it must look into but may not read (`FAIL access <folder>`): the
   nearest folder at or above `BASE` that exists must let this account in (`-x`), and `BASE` and
   `BASE/NAME` must be readable (`-r` and `-x`), so that a copy the check cannot see is never
   reported missing. A copy of this upload already in place, found by the manifest's upload id under
   `NAME` or `<FOLDER>_copyN`, is only checked: in mode `check` the copy and every folder in it must
   be readable first (`-r` and `-x`; else `FAIL access <that folder>`, since `find` leaves out the
   files of a folder it cannot enter and the copy would look changed); then `STEP verify`, its
   digests, then `DONE already` (or, when it no longer matches its list, `FAIL verify`).
2. Mode `check` reports what is there and ends; `BASE/_incoming` and this upload's folder in it
   must be readable too, and so must every folder in this upload's folder before it is compared
   (`FAIL access <folder>`).
3. `STEP prepare`; the folders above the ARC folder and the shared folders are made where missing;
   a shared folder in which one must be made (the ARC folder for `simulated/`, `BASE` for
   `_incoming/`) must be writable for this account, else `FAIL access <that folder>`;
   `BASE/_incoming` must not be a link, and `BASE/_incoming` and `BASE` must be writable for this
   account (`FAIL access <folder>`); the incoming folder must not be a link. When it already holds
   the complete copy, `STEP verify` follows at once; otherwise `STEP receive`, the tar stream, and
   `STEP verify`.
4. `STEP place`, and one rename: to `NAME`, or in mode `upload-copy` to the first free name of
   `<FOLDER>`, `<FOLDER>_copy2` ... `<FOLDER>_copy99`.

| line | meaning |
|---|---|
| `RAMAN STEP prepare` | signed in; making `<base>/_incoming` and the incoming folder |
| `RAMAN STEP receive` | receiving the tar stream |
| `RAMAN STEP verify` | comparing every file with `SHA256SUMS` |
| `RAMAN DIGESTS-BEGIN`, `RAMAN DIGEST <hex>  <path>`, `RAMAN DIGESTS-END` | the SHA-256 of every file, computed on ARC |
| `RAMAN STEP place` | renaming into place |
| `RAMAN DONE placed <path>` | placed now |
| `RAMAN DONE already <path>` | an earlier run of this upload placed it (found by the manifest's upload id, under `NAME` or `<FOLDER>_copyN`) |
| `RAMAN CONFLICT <path>` | the name is taken by a folder this upload did not make; the copy is complete and checked in `_incoming` (exit 24) |
| `RAMAN STAGED complete <path>`, `RAMAN STAGED partial <path>`, `RAMAN MISSING <path>`, `RAMAN ELSEWHERE <path>` | check mode's results |
| `RAMAN FAIL <word> <text>` | `<word>`: `tools`, `prepare`, `access`, `receive`, `verify`, `place` (`FAIL access <folder>`: a folder this account may not write into, or in a check may not read) |

Exit codes: 0 done, 20 tools missing, 21 a folder cannot be made or written, or in a check read (or
is a link), 22 receiving failed, 23 the files do not match their list, 24 the name is taken, 25 the
rename failed.
On this Mac, `STEP prepare` moves the record to `checking`, `STEP receive` to `transferring`,
`STEP verify` to `verifying`, `STEP place` to `placing`. After `DONE` the digests must equal the
payload's `SHA256SUMS` and the manifest's own digest exactly (`verified`, `verification.method`
`sha256-on-arc`), else `failed` / `verify_mismatch`; `FAIL tools` is `remote_tools`, `FAIL access`
`shared_folder_not_writable` (it may name `BASE`, `BASE/_incoming`, or, in the simulated area, the
ARC folder above `simulated/`; the fix names that folder), `FAIL verify` of step 1 (this upload's placed copy was changed on
ARC) `placed_changed`, which names that copy, records it as `placed_as` and is not retryable,
another `FAIL verify` `verify_mismatch`, another `FAIL` `remote_failed`. Exit 255 with no `RAMAN`
line is `ssh_signin_failed`; another exit with no `RAMAN` line `ssh_host_refused_commands`; `RAMAN`
lines without `DONE`, `CONFLICT` or `FAIL` `ssh_interrupted`. Check status runs mode `check`; Retry
same request and Keep as new copy start a new attempt, in mode `upload-copy` when
`destination.new_copy` is true. Before ssh starts, the upload program checks every value it takes
from the record again (folder names, base, user, host, the team file); a value that does not pass
ends the upload before anything runs.

A verified SSH upload says "On ARC: `<N>` files in `<path>`. ARC computed the SHA-256 of every file,
and each matches this Mac's copy."; its Terminal window ends with "Done: all `<N>` files are on ARC and
match this Mac's copy." Check status keeps the upload's state and reports in its message (an upload
that failed or stopped) or in `checks` (a `verified` one): `STAGED complete` "The copy is complete in
_incoming on ARC but not in place yet. Retry same request puts it in place.", `STAGED partial` "Part
of the copy is in _incoming on ARC. Retry same request finishes it.", `MISSING` "Nothing of this upload
is on ARC.", `ELSEWHERE` "ARC has a folder with this name that this upload did not make.", and for a
copy in place "Still on ARC: `<N>` files in `<path>`, every one matching this Mac's list (SHA-256)."
(demo: "Demo: still in the demo folder: `<N>` files, every one matching this Mac's list (SHA-256).") or
"A copy of this upload is on ARC (`<path>`), but it does not match this Mac's list (for example
`<file>`)." These looked at the copy. A check that ssh or the script ended before it could look
adds "Check status could not look." to its message, and for a `verified` upload is an entry with
`"looked": false` and its fix. Its texts: ssh that ended with code 255 (`ssh_signin_failed`) gives the
sign-in text; the others are the check's own. A connection that ended: "The SSH connection ended
before the check was done."; `FAIL access` (a folder the check may not read, also a folder in a
copy it compares; `folder_not_readable`, never `MISSING`, `ELSEWHERE` or a copy that does not match):
"Your VT account may not read `<folder>` on ARC."; `FAIL prepare` (`folder_refused`): "ARC refused a
folder on the way to the copy: `<folder>` is a link."; `FAIL tools` (`remote_tools`): "The check
cannot run on `<host>`: `<text>`."; a host that did not run it (`ssh_host_refused_commands`): "The
check cannot run on `<host>`: that computer did not run its commands." `<host>` is the SSH computer
saved with the upload, which every check of it uses, so the fixes of the last two ask the team to
fix that computer (a demo upload's: this Mac, which runs its checks). A demo upload's texts name the demo
folder instead of ARC ("Part of the copy is in _incoming of the demo folder. ...", "Nothing of this
upload is in the demo folder.", "A copy of this upload is in the demo folder (`<path>`), ...", "This
Mac's account may not read `<folder>` in the demo folder.", "The demo refused a folder on the way to
the copy: ...", "The check cannot run on this Mac: ..."). In
its Terminal window, Check status ends with what it found. When ARC put a copy in
place whose digests differ from this Mac's list, the upload is `failed` / `verify_mismatch`, keeps
`placed_as` and gets the warning "ARC put this copy in place as `<path>`, but it does not match this
Mac's list of files. Do not use that folder; start a new upload." A stop request wins over a `FAIL`
line; a `DONE` or `CONFLICT` line read before it is recorded as it is, since that is what is on ARC.
A send stopped by Stop upload or Ctrl-C after the script may have reached its rename (`STEP place`,
a copy found in place, or the digests of the copy in `_incoming` reported) ends `stopped` with
"Stopped while the copy was being put in place; it may already be in its folder on ARC. Press Check
status to see.", since sshd lets the script run on to its next write; `ssh.may_be_placed` then
keeps `leftover_on_arc` empty.

**Demo** (`--demo`; the window's Demo box, whose default is `arc_upload.demo`). No network, no
account, no browser, no Terminal: the same worker, records, states and checks, with stand-ins for
the two far ends. The demo root `HOME/<arc_upload.demo_folder>` stands in for ARC's file system
(`projects/...`), and its `.demo_globus/` keeps the demo Globus service's state. Both demo
transports get it from one helper, `arc_records.ensure_demo_root`: a missing demo root is made with
mode 0700 and the marker file `.raman_arc_demo`; one that exists must be a real folder (not a link)
of this user that this user may write, and either empty (it then gets the marker) or holding the
marker, and its mode is never changed. Anything else is refused before anything runs, `config` /
`demo_folder_unwritable` with the problem, for example "it already holds other files; choose an
empty folder or a new one", "it is a link; name the folder itself", "it is not a folder" or "it
belongs to another user". `arc-upload --demo` (dry runs included), `arc-list`'s `ready.demo` and
`arc-doctor`'s `demo_folder_writable` apply the same rule without changing anything. Demo Globus
(`arc_demo.DemoGlobus`) has two collections, `00000000-0000-4000-8000-0000000000a1` (this Mac:
absolute paths) and `00000000-0000-4000-8000-0000000000a2` (ARC: its `/X` is
`demo_local_path(root, collection_root + "/X")`). Its sign-in gives the identity `demo (no account)`
with "Demo: no sign-in. A real upload opens your browser here."; it keeps its tasks and the request
numbers it has seen on disk, answers a known request number with `Duplicate` and the same task, and
answers with Globus's codes (`ClientError.NotFound` 404, `Exists` 409,
`ExternalError.MkdirFailed.Exists` 502). Demo SSH (`arc_ssh.LocalBashRunner`) runs the real remote
script with `/bin/bash` against the demo root, with the same tar stream, in the detached worker
("Demo: no Terminal and no password. A real SSH upload asks for your VT password and Duo in a
Terminal window here."). This Mac's tar (bsdtar) writes some accented letters in file names in
another form than they were sent, so the demo's check of such a copy would fail: before anything is
sent, demo SSH refuses a snapshot path that it would change (`failed` / `demo_ssh_name`; live SSH
uploads, which ARC's GNU tar receives, keep such names). Teaching faults, demo only, once per upload
(`--demo-fault`, kept in `demo_fault`): `lose_submit_reply` (Globus accepts the request and the
reply is lost; the resend gets `Duplicate`, one task), `fail_task_once` (the task fails halfway;
Retry same request copies again into the same `_incoming`), `drop_connection` (SSH: the local
script is ended after half the stream, `ssh_interrupted`; Retry same request finishes the upload).
Every demo message starts with `Demo:`, and the demo's stop texts name the demo folder, not ARC;
the result reads "Demo: 21 files in the demo folder on this Mac, every one checked. Nothing left
this Mac." A demo upload never counts as "on ARC" for a live upload: their `destination.key`
differs. The demo Globus service keeps `.demo_globus/submissions.json` (request number to task id)
and `.demo_globus/tasks/<task id>.json`; it is polled every 0.5 s at the default pace, and its
pauses (the sign-in, the resend delays) scale with `RAMAN_ARC_DEMO_PACE`, so that 0 gives none.
Both demo transports land in `<demo root>/projects/<ARC folder>[/simulated]/<folder>`.

**Results of the upload program** (`error.reason`; the record's `error` carries `message`, `fix`
and `retryable`):

| reason | message | fix | retryable |
|---|---|---|---|
| `no_space` | This Mac has not enough free disk space to prepare the upload (needs `<N>` MB). Nothing was sent. | Free some space, then press Retry same request. | yes |
| `changed_during_copy` | The session's files kept changing while they were copied. Nothing was sent. | Wait until nothing writes into the session, then press Retry same request. | yes |
| `payload_changed` | The files kept for this upload were changed on this Mac, so the same request cannot be sent. | Start a new upload. | no |
| `session_changed` | This session's files changed after the upload was made: `<what>`. Nothing was sent. | Press Upload to ARC to start a new upload of the session as it is now. | no |
| `signin_timeout` | You did not finish signing in within 10 minutes. Nothing was sent. | Press Retry same request and sign in when the page opens. | yes |
| `no_browser` | No web browser could be opened for the Globus sign-in. | Use the controller on the Mac's own screen, not over a remote connection, or send with SSH. | yes |
| `signin_failed` | Signing in to Globus did not work: `<message>`. | Press Retry same request and sign in with your Virginia Tech identity. | yes |
| `consent` | Globus asked for a permission that was not given. Nothing was sent. | Press Retry same request and allow access to the ARC collection when Globus asks. | yes |
| `mac_collection_unknown` | Globus does not know the collection ID in ARC settings. | Copy it again from Globus Connect Personal > Settings into ARC settings, then start a new upload. | no |
| `mac_collection_not_yours` | The Globus collection in ARC settings belongs to another Globus account, so `<identity>` cannot send from it. | Only the Globus account that set up Globus Connect Personal on this Mac can send from it. Sign in with that account (turn on "Ask for the Globus sign-in every time" in ARC settings), then start a new upload; or send with SSH. Check also that the ID in ARC settings is this Mac's own (Globus Connect Personal > Settings). | no |
| `gcp_not_connected` | Globus Connect Personal on this Mac is not running or not connected (or the Mac slept). | Open it from the menu bar and check that it says Connected, then press Retry same request. | yes |
| `source_not_visible` | Globus cannot read the upload copy on this Mac (`<path>`). | In Globus Connect Personal > Settings > Access, add the deploy folder to the list (leave Sharable off). In System Settings > Privacy & Security > Files and Folders, allow Globus Connect Personal to read Downloads (Full Disk Access when it is not listed there). Then press Retry same request. | yes |
| `source_mismatch` | Globus sees other files in this Mac's folder than the upload holds (`<n>` differ, for example `<file>`). | Check that the collection ID in ARC settings is this Mac's own, then start a new upload. | no |
| `listing_truncated` | Globus listed only part of the upload copy; nothing was sent. | Press Retry same request; if it happens again, send with SSH. | yes |
| `arc_folder_not_dir` | The ARC folder `<path>` is a file on ARC. | Choose another ARC folder in ARC settings, then start a new upload. | no |
| `destination_permission` | Your Globus account may not write into `<Globus path>` on ARC. | Ask the owner of the project folder to add you on ARC, sign in with your VT identity, then press Retry same request. | yes |
| `refused_by_globus` | Globus refused the request: `<code>`: `<message>`. Nothing was copied. | If ARC's project folder is full, ask its owner. Otherwise press Retry same request; if it repeats, send this message to the team. | yes |
| `request_conflict` | Globus is still running another transfer of these files to the same folder. | Wait until that transfer has ended (app.globus.org > Activity shows it), then press Retry same request. | yes |
| `task_failed` | Globus could not finish the copy: `<description>`. Files copied so far are in `<incoming>` on ARC. | Press Retry same request: the same files go into the same place. | yes |
| `task_not_visible` | Globus does not show this transfer to `<identity>` (another Globus identity started it, or Globus no longer keeps it). | Press Check status and sign in with the Globus identity that started this upload (sign out at app.globus.org first if the browser signs you in as someone else). If Globus no longer keeps the transfer, start a new upload. | yes |
| `verify_mismatch` | The copy on ARC does not match this Mac's list of files (for example `<file>`). It was not put in place. | Press Retry same request: the files are copied again. | yes |
| `ssh_signin_failed` | Signing in to ARC did not work (ssh ended with code 255). The Terminal window shows ssh's own reason. Nothing was changed on ARC. | Check your VT username in ARC settings, your password and Duo, and that you have an ARC account in the project that owns the ARC folder. ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN. Then press Retry same request. If ssh says the host key changed, stop and ask the team: do not continue. | yes |
| `ssh_host_refused_commands` | ARC's transfer host did not run the upload commands. | Use a login node instead: ARC settings > More > SSH computer: tinkercliffs1.arc.vt.edu (command line: config --set arc_upload.ssh.host=tinkercliffs1.arc.vt.edu), then start a new upload. | no |
| `ssh_interrupted` | The SSH connection ended during the upload. Files sent so far are in `<incoming>` on ARC. | Press Retry same request: all files are sent again, into the same folder on ARC. | yes |
| `terminal_closed` | The Terminal window was closed before the upload finished. | Press Retry same request. | yes |
| `remote_tools` | ARC lacks a tool the upload needs: `<text>`. | Use a login node instead: ARC settings > More > SSH computer: tinkercliffs1.arc.vt.edu (command line: config --set arc_upload.ssh.host=tinkercliffs1.arc.vt.edu), then start a new upload. | no |
| `remote_failed` | ARC could not `<make the folder / receive the files / rename the folder>`: `<text>`. | Check the space and your permission in the project folder, then press Retry same request. | yes |
| `shared_folder_not_writable` | Your VT account may not write into `<path>` on ARC: that shared folder is not group-writable. | Ask the folder's owner to run chmod 3770 `<path>` on ARC (README section 14), or to add you to the ARC project if you are not in it. Then press Retry same request. | yes |
| `placed_changed` | This upload's copy on ARC (`<path>`) was changed after it was put in place: it no longer matches this Mac's list of files (for example `<file>`). | This program changed nothing on ARC. Look at that folder there; to send the session again, start a new upload (command line: --new). | no |
| `demo_ssh_name` | This Mac's tar changes accented letters in file names, so demo SSH cannot send `<name>`: its check would fail. Nothing was sent. | Try the demo with Globus. A live upload over SSH keeps such names as they are. | no |
| `terminal_not_opened` | Terminal could not be opened (`<reason>`). | Press Retry same request, or double-click ssh_upload.command in the upload's folder. | yes |
| `internal` | Something went wrong in the upload program (`<type>`). The record and the log are in `<record folder>`. | Press Retry same request. If it happens again, send the record folder's worker.log to the team. | yes |
| `globus_error` | Globus could not be reached, or gave an answer this program does not expect (`<what>`). | Check that this Mac is online, then press Retry same request. If it happens again, send this message to the team. | yes |

The record of an upload made before these texts may still hold `failed` / `terminal_not_opened`;
an upload whose Terminal does not open now stays waiting for it (Workers, above).

Messages of the working states, for example: `queued` "Waiting to start."; `waiting_for_terminal`
"Waiting for the Terminal window to start the upload."; `preparing` "Preparing
the files: copying the session and its dark spectrum and computing a checksum for every file.";
`signing_in` (Globus) "A web page has opened for the Globus sign-in. Sign in there; nothing is sent
before that.", (SSH) "Sign in to ARC in the Terminal window. The first time, ssh asks yes or no
about ARC's key before the password: never type your password there. Then type your VT password and
approve Duo."; `checking` (Globus) "Signed in to Globus as `<name>`. Checking this Mac's files and
the ARC folder. If the browser asks to allow access to ARC, allow it."; `submitting` "Handing the
copy to Globus."; `transferring` "Copying with Globus: 12 of 21 files (310 kB of 433 kB). Keep
Globus Connect Personal running." or "Copying over SSH: 40% (0.25 of 0.6 MB)."; `verifying`
"Checking the copy on ARC."; `placing` "Putting the copy in its folder on ARC."; `verified` "On ARC:
21 files in `<path>`. `<how it was checked>`"; `uncertain` "Not sure Globus received the request.
Press Retry same request: Globus will not start a second transfer."; `conflict` "Your copy is
complete and checked in `<incoming>` on ARC. Keep it as a new copy, or move the other folder on ARC
and press Retry same request." (at `placing`) or "ARC already has a folder named `<folder>` in `<ARC
folder>`. Nothing was sent." (at `checking`); `stopped` "Stopped. Files already copied stay in
`<incoming>` on ARC; nothing was deleted." (demo: "Demo: stopped. Files already copied stay in
`<incoming>`; nothing was deleted.", with the demo folder's path).

A demo upload's copy is in the demo folder on this Mac, so every text above that says where it is
names the demo folder (or its path), never ARC (review COR-05): `checking` (Globus) "Demo: checking
this Mac's files and the demo folder.", `verifying` "Demo: checking the copy in the demo folder.",
`placing` "Demo: putting the copy in its folder in the demo folder.", `conflict` "Demo: your copy is
complete and checked in `<incoming>`. Keep it as a new copy, or move the other folder and press Retry
same request." or "Demo: the demo folder already has a folder named `<folder>` in `<path>`. Nothing
was sent.", Check status "Demo: the folder in the demo folder differs from this upload: ..." and
"Demo: the folder `<path>` is no longer in the demo folder.", and the notes after the upload ("The
demo folder has the copy from ...", "the scan data in the demo folder is complete"). These results
have a demo wording in `error` too (`arc_records.DEMO_RESULTS`; the fix is the result's own where
none is given):

| reason | demo message | demo fix |
|---|---|---|
| `task_failed` | Globus could not finish the copy: `<description>`. Files copied so far are in `<incoming>`. | (its own) |
| `verify_mismatch` | The copy in the demo folder does not match this Mac's list of files (for example `<file>`). It was not put in place. | (its own) |
| `ssh_interrupted` | The SSH connection ended during the upload. Files sent so far are in `<incoming>`. | Press Retry same request: all files are sent again, into the same folder. |
| `remote_failed` | The demo could not `<what>` in the demo folder: `<text>`. | Check the free space and the permissions of the demo folder, then press Retry same request. |
| `shared_folder_not_writable` | This Mac's account may not write into `<path>`, a folder of the demo folder. | Make it writable again (in Terminal: chmod u+w `<path>`). Then press Retry same request: it uses the demo folder saved with the upload. |
| `placed_changed` | This upload's copy in the demo folder (`<path>`) was changed after it was put in place: it no longer matches this Mac's list of files (for example `<file>`). | This program changed nothing in the demo folder. Look at that folder there; to send the session again, start a new upload (command line: --new). |
| `destination_permission` | This Mac's account may not write into `<path>` in the demo folder. | Check the permissions of the demo folder, then press Retry same request. |
| `arc_folder_not_dir` | `<path>` in the demo folder is a file, where the ARC folder must be a folder. | (its own) |

## 6. Command line

`python -m raman_datafed --home HOME <command> [options]` prints exactly one JSON object on
stdout (UTF-8, one line or pretty, nothing else), logs to stderr, and exits 0 only when
`"ok": true`. Failure: `{"ok": false, "error": "<short>", "detail": "<longer, optional>"}`, plus
`"kind"`: `offline`, `auth_required`, `not_found`, `conflict`, `config`, `metadata`, `command`,
`usage` (wrong arguments; exit code 2), `interrupted` (Ctrl-C; exit code 130), `catalog` or
`internal`, and for the `arc-*` commands also `not_ready` (the session cannot go yet), `refused` (a
rule says no) and `not_installed` (a part the transport needs is missing). A failure of an `arc-*`
command always has two more keys, `"reason"` (a stable code) and `"fix"` (one sentence: what to do
next, naming the window's control, with the command line in brackets where it differs); some add
`retry_after_seconds` or `upload`. The other commands' failures keep their four keys. `-h` gives
`{"ok": true, "help": "..."}`. Commands never wait for a Globus transfer to finish; the next `sync`
picks the result up. The `arc-*` commands do not wait for a sign-in, a Terminal or a transfer
either: the long work of an upload runs in its worker (section 5.6).

| command | does | main output keys |
|---|---|---|
| `status` | nothing but report | `mode`, `mode_label`, `demo`, `online`, `user`, `context`, `endpoint`, `counts` (one key per state), `total`, `pending` (captures not yet uploaded, without the held ones), `held_simulated` (simulated captures that `datafed` mode keeps on this Mac, section 5.1; always 0 in the demo), `sync_work` (what a plain `sync` can move on now: captures and dark spectra to register or upload, failed ones below `sync.max_attempts`, reviews recorded offline; not the held captures, those lacking fields, or those past the limit), `catalog_changed` (true while the rows, with their states, record ids and predictions, still belong to the catalog used before the settings changed; the next sync registers every capture in the catalog in use. False when there is no capture), `reviews_queued`, `last_sync_utc`, `problems` (list of strings, below), `arc_unfinished` (uploads to ARC in a working state, `uncertain` or `conflict`, read from the records' `schema` and `state` only; 0 when the ARC part is missing or cannot be read), `arc_attention` (uploads to ARC that need a person, section 5.6: failed, in conflict, not sure, interrupted, not started, not watched or with their last check failed, changed in the last 7 days; 0 when the ARC part is missing or anything fails) |
| `sync [--retry-failed]` | find new points, register, upload, poll transfers, pick up predictions and reviews; `--retry-failed` also retries what reached `sync.max_attempts` | `busy` (another sync is running; nothing was done and `online` is `null`), `online`, `ingested`, `registered`, `uploads_started`, `uploads_done`, `predictions_found`, `reviews_found`, `reviews_sent` (reviews written to the catalog: a queued review that a later one already in the catalog supersedes leaves the queue unwritten), `held_simulated` (as in `status`), `errors` (list of `{capture_id, stage, message}`), `status` (same object as `status`) |
| `list [--session ID] [--state S] [--limit N]` | rows for the Data tab, newest first | `items`, `total` |
| `classify [--limit N]` | run the classifier job here against the active catalog; unlike the other commands it waits for the downloads of the files it classifies | `runner`, `processed` (spectra that left the queue with a prediction), `predictions` (prediction records this run created), `skipped` (left in the queue: a file is not uploaded yet), `remaining` (not looked at), `errors`, `items` (`{capture_id, raw_record, prediction_record, label, top_class, confidence, outcome, needs_review, created}`), `model` |
| `review-list` | predictions of this HOME's captures waiting for a person | `items`, `online` (`null`: the catalog did not have to be asked; `false`: the items come from the ledger alone and their `scores`, `reason`, `threshold`, `model` are `null`) |
| `review-show --prediction ID` | one prediction in full; the spectrum is read from this Mac and only fetched from the catalog (waiting for the download) when it is not here | `item`, `spectrum` (`raman_shift`, `intensity`, `dark`, `processed`: index-aligned lists of 2048; `processed` is `null` for the first `processed_start` = 90 pixels, which the model does not see; `dark` or `processed` is `null` when there is none; `source`), `provenance` (the records the prediction is derived from: nodes as in `provenance`, plus `relation` and `label`), `classes`, `online`, `problems`. Only the dark the classifier used is shown: online the one the prediction names (the file on this Mac only when its SHA-256 matches); offline the capture's dark when the kinds allow it (section 5.3). Without one, `dark` and `processed` are `null` and `problems` says why, for example "The classifier used no dark spectrum for this prediction, so none is shown." |
| `review-set --prediction ID --decision confirm\|relabel\|reject [--label CLASS] --reviewer NAME [--comment TEXT]` | record a review (queued locally when offline). `--label` goes with `relabel` only: one of the model's classes, or `unknown` (stored as `UNKNOWN`). `confirm` stores the prediction's `top_class` as the review label (`REJECTED` for a rejected one), `reject` none. It waits, without a time limit, for `HOME/data_catalog/review.lock`, which a sync holds while it sends one queued review (the sync makes its first catalog request, which connects, before it takes the lock), and takes the review's time inside it; a review already in the catalog that was made later is kept, and `item` then shows that one | `item`, `queued` |
| `provenance --record ID` | the provenance chain around a record | `nodes` (`{id, alias, title, type, role: start\|source\|derived, info}`), `edges` (`{from, to, relation, label}`: `from` is derived from `to`), `truncated` |
| `import-session --folder DIR [--sample-id S --operator O --location L] [--notes N --laser-power LEVEL --simulated yes\|no --dark-file FILE]` | add an existing scan folder (copied under `scan_sessions/` unless it is there), supplying missing fields; values already in the files win. `--dark-file` also copies the dark's `.json` from beside it, first; when that `.json` exists, or is a link, but is not a file that can be read (a link to nothing, a folder, an unreadable file), nothing is imported: kind `command`, "The description <name> beside the dark cannot be read; nothing was imported", because the dark copied without it would count as real. A point holding a broken character outside its capture block (section 5.3) is not completed: an `errors` entry `<file>: <key> holds a broken character` | `ingested`, `skipped`, `errors`, `folder`, `session_id`, `copied` |
| `config [--set key=value ...]` | show or change settings (dotted keys); `arc_upload.*` values it sets are checked by the rules of section 5.1 as well (each value it sets must be valid, and it may bring no new problem), and a refusal saves nothing | `config`, `path`, `problems` and `warnings` when there are any, `arc_problems` when the file holds ARC values that are not valid (also after a save) |
| `doctor [--online]` | check the DataFed prerequisites without logging in; `--online` (datafed mode only) also asks DataFed who the user is. The settings check names `datafed.include_simulated` and what it does | `checks` (list of `{name, ok, detail, fix}`), `online_checked`, `datafed_ready`, `mode`, `include_simulated` |
| `offline --on\|--off` | local-demo only: simulate losing the connection | `offline` |
| `studio [--port N] [--no-open] [--idle-exit SECONDS]` | serve the Simulation Studio on 127.0.0.1, on port N (default 8765) or the next free one, until interrupted (SIGINT, SIGTERM, SIGHUP); with `--idle-exit` also until nobody has used it for that long (no request, no run of its own) | prints `{"ok": true, "url": "http://127.0.0.1:N/"}` then keeps running |
| `arc-sessions [--limit=N] [--controller-idle]` | the session folders that could go to ARC (section 5.6), newest first, at most N (default 30), each with whether it may go now; reads files only | `items` (below), `total`, `controller_idle_used`, `settings` (`demo`, `transport`, `folder`, `globus_folder`, `include_simulated`, `settle_seconds`) |
| `arc-upload --session=F [--transport=globus\|ssh] [--demo\|--live] [--declare-real] [--new] [--dry-run] [--controller-idle] [--demo-fault=NAME]` | check the session and the transport, look for an earlier upload of the session to the same place, create the upload and start its worker (detached, or for live SSH in a Terminal window) | `upload` (an item, below), `existing`, `worker` (`detached` or `terminal`), `message`; with `--dry-run` instead `dry_run`, `plan` and `existing`, and nothing is created |
| `arc-list [--limit=N] [--session=F]` | the uploads, newest first (default 12; every one in a working state, `uncertain` or `conflict` is always listed); it answers whatever the ARC settings are | `items`, `total`, `active` (uploads whose worker is alive), `unfinished` (in a working state, `uncertain` or `conflict`), `attention` (uploads that need a person, as `status`'s `arc_attention`, counted over every upload, not only those listed), `ready` (`globus`, `ssh`, `demo`: `{"ok", "problems": [{"reason", "text", "fix"}]}`, from the checks of `arc-doctor` that need no network; with invalid ARC settings each is `{"ok": false}` with the `settings_invalid` problem), `settings` (every effective `arc_upload` value, as flat dotted keys, invalid ones as they are; the defaults when the settings file cannot be read), `as_of_utc` |
| `arc-status --upload=ID` | one upload in full | `upload`: an item plus `events` (the last 30), `manifest` (`files`, `bytes`, `darks`: the manifest's entries), `attempts` (Globus: the earlier attempts and the current one; SSH: `ssh.runs`), `record_folder`, `session` (as the record keeps it: `folder`, `session_id`, `scan_mode`, `started_utc`, `sample_id`, `points`, `points_planned`, `end`, `kind`, `kind_declared_by_uploader`) |
| `arc-check --upload=ID [--demo-only]` | ask how the upload stands; it never sends a file: a check worker (Globus, demo), or a Terminal window in check mode (live SSH: a new attempt, `ssh.attempt` + 1, while the upload keeps its state), with one sign-in (a Globus check may ask for ARC's consent right after it). A check of a `verified` upload that finds the copy missing or different keeps `verified`, adds `{"ok": false, "looked": true, ...}` to `checks` (the listing then shows `check_failed`), and its worker exits 1; one that cannot look adds `{"ok": false, "looked": false, ..., "fix"}`, which leaves the upload "On ARC, checked", and also exits 1 | `upload`, `worker`, `message` |
| `arc-retry --upload=ID [--demo-only]` | Retry same request (section 5.6) | `upload`, `worker`, `message`, and `same_submission` true when the saved Globus body is to be sent again (the worker makes a new attempt instead when Globus refused that request at every post, or its deadline has passed) |
| `arc-stop --upload=ID [--demo-only]` | stop the upload: at once when no worker runs and nothing can run on ARC or Globus any more (a `conflict`, or a Globus task that last showed `SUCCEEDED` or `FAILED`); with a worker alive (a send, a check or a stop worker), it sets `requests.stop_utc` and answers "The upload program stops the upload within a second." (a live Globus upload: "The upload program stops the upload. If Globus may still be copying it, the transfer is cancelled first, after the Globus sign-in if that page is still open."; a check ends at once and changes nothing, also while it checks or places the copy of a Globus transfer that had ended when the check started, except a Globus check that started while the transfer could still run (the listing's `may_run` then), which is stopped as Stop upload stops: it finishes its sign-in and cancels the transfer, or, when the transfer has ended meanwhile, ends the upload `stopped` with the copy complete in `_incoming`); for a Globus task or request nobody watches, it sets `requests.stop_utc` and starts a stop worker that signs in, settles the request and cancels the task. It answers whatever the ARC settings are | `upload`, `stopped_now`, `worker` (`detached` for a stop worker, else `null`), `message` |
| `arc-keep-copy --upload=ID [--demo-only]` | in `conflict` only, while the payload is kept: put the upload beside the folder that has its name, under the first free name of `<folder>`, `<folder>_copy2` ... `_copy99` (the session's own name is taken in a conflict, so this is normally `<folder>_copy2`), and start the work as `arc-retry` does | `upload`, `worker`, `message` |
| `arc-doctor` | check, without the network, what each transport needs, with the effective settings; it also answers when ARC values are not valid (`settings_valid` is then false and `settings` shows the values as they are), so that the ARC settings dialog can repair them | `checks` (list of `{name, ok, detail, fix}`), `ready` (`globus`, `ssh`, `demo`: true or false), `settings` (flat dotted keys) |

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

**The `arc-*` commands** (uploads to ARC, section 5.6) come from `cli.FEATURE_MODULES`
(`raman_datafed.arc_commands`), loaded after `COMMAND_MODULES` and left out when the module is
missing; such a data layer answers them with kind `usage`. They use no network, never import
`globus_sdk`, never wait for a sign-in, a Terminal or a transfer, and answer in well under a second;
`arc-upload` for a live SSH upload waits at most 15 s for `open` to return. Callers pass values as
`--name=value` (the window and the Studio do), so that a value is never read as an option; session
names are checked by rule R1 and upload ids by `^u[0-9a-f]{12}$`. Each first reaps the workers its
own process started. The upload program, `python -m raman_datafed.arc_worker`, is not a command of
this section: like `python -m raman_datafed.arc_job` it is a program of its own (section 5.6).

`arc-sessions` item:

```json
{"folder": "1d_x_scan_20261113_101500_123", "session_id": "5f0c...", "scan_mode": "1d-x",
 "started_utc": "2026-11-13T15:15:00.123Z", "sample_id": "S-001",
 "points": 13, "points_planned": 13, "end": "finished", "end_text": "finished",
 "kind": "real", "files": 21, "bytes": 433104,
 "darks": [{"file": "dark_20261113_101400_001.txt", "on_this_mac": true, "kind": "real", "sent": true, "reason": null}],
 "ready": true, "retry_after_seconds": 0,
 "why_not": null,
 "live": {"allowed": true, "why_not": null, "area": "real",
          "cluster_path": "/projects/pcl_photonics/raman_globus_upload/1d_x_scan_20261113_101500_123",
          "globus_path": "/pcl_photonics/raman_globus_upload/1d_x_scan_20261113_101500_123"},
 "demo": {"allowed": true, "why_not": null, "area": "real",
          "demo_path": "<HOME>/data_catalog/arc_demo/projects/pcl_photonics/raman_globus_upload/1d_x_scan_20261113_101500_123"},
 "uploads": {"live": null,
             "demo": {"upload_id": "u3f9a0c1d2e4b", "state": "verified", "display_state": "verified",
                      "display_label": "On ARC, checked", "transport": "globus", "updated_utc": "...",
                      "final_path": "/projects/..."}},
 "warnings": []}
```

`why_not` (rules R1 to R6) is `{"reason", "text", "fix"}` or `null`; `live.why_not` and
`demo.why_not` give the kind policy (`simulated_not_allowed`, `simulation_unknown`) or `null`.
`kind` comes from `session.json` alone (`real`, `simulated`, `unknown`). `files` counts the files
that would be sent, the manifest and `SHA256SUMS` included; `bytes` the session's and the darks'
bytes. `end_text` holds the window's words for `end` (section 5.6), or `still running` or `ended
<N> s ago` while rule R4 or R5 fails. `arc-sessions` fails only for invalid ARC settings or a missing
HOME.

`arc-upload` checks, in this order, and the first that fails answers: the ARC settings are valid;
`F` is a session folder of section 5.6; rules R1 to R7 with every point read; for a live upload the
transport's setup (`mac_collection_missing`, `username_missing`, `known_hosts_unreadable`,
`globus_sdk_missing` found with `importlib.util.find_spec`, without an import, `ssh_missing`,
`terminal_missing`, `not_macos`) and no live send or stop worker of another upload alive
(`another_live_upload`; a Check status never holds an upload back); for a demo upload, dry runs
included, the demo folder rule of section 5.6 (`config` / `demo_folder_unwritable`, the same check
as `arc-list`'s `ready.demo`), checked before anything is made. Then the earlier uploads with this
session id and `destination.key`, oldest to newest (under `requests.lock`, except for a dry run):

1. One in a working state, `uncertain` or `conflict` makes no new upload but answers `"ok": true`,
   `"existing": true` with that `upload`, `"worker": null` and a `message` ("An upload of this
   session is already under way (`<display label>`).") (with `--new`: `conflict` / `in_progress`,
   with `upload`).
2. Without `--new`, when the newest of the ended uploads (`verified`, `failed`, `stopped`) is
   `failed` or `stopped`: `conflict` / `earlier_upload`, with `upload`.
3. Without `--new`, when it is `verified` and an upload of the session is on ARC (a `verified` one
   whose newest check that looked found the copy, or that has no such check yet; a check that could
   not look changes nothing): `conflict` / `already_on_arc` with the newest such upload, `(checked
   <date>)` being the time of its newest check when that one found the copy, else of its
   verification, as `YYYY-MM-DD HH:MM UTC`. A demo upload, and a folder that
   holds the same session as another folder (a copy made with `cp -R`), get their own wording (the
   variants below the refusals). When no upload of the session is on ARC any more (each `verified`
   one shows `check_failed`), a new upload goes ahead without `--new`, under the session's own
   name, with `new_copy` false.
4. Otherwise a new upload. `destination.new_copy` is true when an upload of the session is on ARC
   (only with `--new`); the copy then takes the first free name of rule 5 of the layout, and the
   confirmation's note says "It goes beside the earlier copy, under the first free name of
   `<folder>`, `<folder>_copy2`, `<folder>_copy3` and so on."

`--dry-run` stops before 4 with `{"ok": true, "dry_run": true, "plan": {...}, "existing": <item or
null>}` (with an upload under way: its plan and that upload, which a real upload would show
instead); `plan` holds `session`, `transport`, `demo`, `files`, `bytes`, `darks`, `excluded`,
`warnings`, `notes` (sentences for the confirmation), `area`, `new_copy` and `destination`
(`cluster_path`, `globus_path`, `demo_path`, and `ssh_target` `"<user>@<host>:<cluster path>"` or
`null`). Otherwise the record is created (`queued`, with the expected `worker`) and the work
started: a detached worker for Globus and every demo upload; for live SSH `ssh_upload.command` and
`open` (state `waiting_for_terminal`; when `open` fails the record stays `waiting_for_terminal`, as
section 5.6 says, and the command fails with `command` / `terminal_not_opened`, carrying `upload`).
`message`: Globus live "A web page opens for you to sign in to Globus. Come back here when you have
signed in."; SSH live "A Terminal window opens. Sign in there once as `<username>`: the VT password
of that account, then Duo." (the VT username the upload keeps in `settings_used`); demo "Demo upload
started. Nothing leaves this Mac." `arc-retry` and `arc-keep-copy` answer the same messages, and
`arc-check` "A web page opens for you to sign in to Globus. Nothing is sent.", "A Terminal window
opens. Sign in there once as `<username>`: the VT password of that account, then Duo. Nothing is
sent." or "Demo: checking the upload. Nothing leaves this Mac." `--demo` and `--live` override
`arc_upload.demo` for this upload and `--transport` overrides `arc_upload.transport`.
`--demo-fault` takes `lose_submit_reply`, `fail_task_once` or `drop_connection` and is `usage`
without demo. Wrong arguments, unknown options included, are `usage` / `usage`, with `fix`.

Upload item (`items` of `arc-list`, and `upload` in the other replies):

```json
{"upload_id": "u3f9a0c1d2e4b", "created_utc": "...", "updated_utc": "...",
 "session_folder": "1d_x_scan_20261113_101500_123", "session_id": "5f0c...", "sample_id": "S-001",
 "kind": "real", "area": "real", "transport": "globus", "demo": false,
 "state": "transferring", "state_label": "Copying with Globus",
 "display_state": "transferring", "display_label": "Copying with Globus", "final": false,
 "message": "Copying with Globus: 12 of 21 files (310 kB of 433 kB). Keep Globus Connect Personal running.",
 "progress": {"files": 21, "files_done": 12, "bytes": 433104, "bytes_done": 310000},
 "destination": {"final_path": "/projects/...", "globus_final_path": "/pcl_photonics/...",
                 "incoming_path": "/projects/.../_incoming/...", "placed_as": null,
                 "ssh_target": null, "local_final_path": null},
 "worker": {"alive": true, "action": "send", "terminal": false},
 "may_run": true,
 "identity": "<Globus user name or null>",
 "needs": null,
 "actions": ["stop"],
 "error": null, "warnings": [], "notes": [],
 "verification": null,
 "last_check": null,
 "leftover_on_arc": null}
```

`needs`: `"browser"` (a Globus sign-in is under way), `"terminal"` (SSH waits for Terminal or the
password, also while Check status of a live SSH upload runs in its Terminal window), `"decision"`
(`conflict` or `uncertain`), else `null`. `actions` follows the table of section 5.6.
`leftover_on_arc`: the incoming path of a `failed` or `stopped` upload that sent something (`null`
while `ssh.may_be_placed` is true). `last_check`: the newest entry of `checks`, also one of a check
that could not look (`"looked": false`, with its `fix`). `may_run`: true while Globus may still run
a transfer of the upload (its task is not known to have ended, or Globus may hold its request): a
stop cancels it, also the stop of a running Check status, whatever the stored state (a `failed`
upload's transfer too); false once its task has ended, and for SSH (`arc_records.globus_may_run`).
A running Check status is stopped as `may_run` was when it started: one that started with `may_run`
true is stopped as Stop upload stops, also after it has written that the task ended; one that
started with `may_run` false only ends (section 5.6, Actions). `worker.action`
and `worker.terminal` are given only while the worker is alive. `destination.ssh_target` is
`"<user>@<host>:<final path>"` of a live SSH upload, with the VT username the upload keeps (Retry
same request and Check status sign in with it, whatever ARC settings say now).

`arc-check`, `arc-retry` and `arc-keep-copy` refuse while a worker holds the upload (`conflict` /
`busy`; while it works in a Terminal window, the `terminal` wording below), and all four refuse an
action that is not in the item's `actions` (`command` / `not_retryable`, `not_checkable`,
`nothing_to_stop`, `no_conflict`). When Retry same request or Keep as new copy is missing from the
actions only because the payload folder is gone, `arc-retry` and `arc-keep-copy` answer `command` /
`payload_missing` instead. `--demo-only`, which the Studio passes, refuses a live upload (`refused` /
`not_demo`). `arc-stop` answers "Stopped before anything was sent." or "Stopped. Files already
copied stay in `<incoming>` on ARC; nothing was deleted." when it stops at once (`"stopped_now":
true`), or for Globus one of the texts of section 5.6 (Stop and Check status); a demo upload's texts
name the demo folder instead of ARC. Each of the four checks again, under `record.lock`, that the
action is still one of the upload's before it writes anything. `arc-check`, `arc-retry` and
`arc-keep-copy` need valid ARC settings (`settings_invalid`); `arc-stop` does not. The checks of
`arc-doctor`:
`settings_valid`, `macos`, `globus_sdk_installed`, `mac_collection_set`,
`globus_connect_personal_installed`, `globus_connect_personal_running`, `client_id_is_registered`
(not ok, as a warning, while the Globus SDK's example app is in use: "Sign-in uses the Globus SDK's
public example app. For regular use, register an app for the team and set
arc_upload.globus.client_id."), `ssh_username_set`, `ssh_program`, `terminal_app`, `open_program`,
`known_hosts_file`, `demo_folder_writable` ("The demo folder `<root>` can be used.", "The demo folder
`<root>` cannot be used: `<problem>`.", or, when `arc_upload.demo_folder` itself is not valid, "The
demo folder cannot be used: arc_upload.demo_folder is not valid (see settings_valid)."),
`remote_script_present`.

Refusals of the `arc-*` commands (kind / `reason`: `error` and `fix`):

| kind / reason | error | fix |
|---|---|---|
| `not_found` / `unknown_session` | There is no session folder `<folder>` in scan_sessions. | Choose a session from the list (command line: arc-sessions). |
| `not_found` / `unknown_upload` | There is no upload `<id>` on this Mac. | Choose an upload under Recent uploads (command line: arc-list). |
| `metadata` / `no_session_json` | `<folder>` has no session.json, so this edition did not record it. | Add it with Import scan folder in the Data tab (command line: import-session --folder `<folder>`), then upload. |
| `metadata` / `unreadable_session` | session.json of `<folder>` cannot be read: `<why>`. | Nothing was sent. Check that file. |
| `not_ready` / `no_points` | `<folder>` has no saved spectrum. | There is nothing to upload. |
| `not_ready` / `running` | `<folder>` is still being recorded. | Wait until the scan has ended; the chart title then says finished, stopped or failed. |
| `not_ready` / `settling` | This scan ended less than a minute ago, or is still being written. | Try again in `<N>` s. (`retry_after_seconds`) |
| `not_ready` / `future_time` | A file of this session has a date in the future. | Check this Mac's clock. |
| `refused` / `odd_name` | This session holds a file (or has a name) that cannot be sent safely: `<name>`. | Rename it, then upload. |
| `refused` / `link` | This session folder holds a link (`<name>`); only plain files are sent. | Replace it with a plain file or move it out of the folder. |
| `refused` / `special_file` | `<name>` in this session is not a plain file. | Move it out of the folder. |
| `refused` / `name_clash` | `<folder>` has a folder of its own named dark, where the upload puts the dark spectra. | Rename that folder, then upload. |
| `refused` / `simulated_not_allowed` | This session was made in simulation; simulated sessions are not sent to ARC. | Tick Demo to try the upload. To send simulated sessions to ARC's simulated/ folder, turn on "Also send simulated sessions" in ARC settings (command line: config --set arc_upload.include_simulated=true). |
| `refused` / `simulation_unknown` | It is not recorded whether this session was made in simulation, so it is handled like a simulated one. | If it was measured with the real instruments, tick "Measured with the real instruments" (command line: --declare-real). Otherwise turn on "Also send simulated sessions" in ARC settings. |
| `refused` / `mixed` | Some points of this session were made in simulation and some were not, so it is not uploaded. | Check the session; nothing was sent. |
| `refused` / `declare_real_not_unknown` | This session records whether it was simulated; "measured with the real instruments" is only for sessions that do not. | Upload it without that choice. |
| `refused` / `not_demo` | The Studio acts on demo uploads only. | Uploads to ARC are handled in the controller window. |
| `conflict` / `in_progress` | This session is being uploaded already. | Stop that upload first, or wait for it. |
| `conflict` / `earlier_upload` | This session has an earlier upload that ended as `<label>`. | Press Retry same request on it, or start a new upload (command line: --new). |
| `conflict` / `already_on_arc` | `<folder>` is already on ARC: `<path>` (checked `<date>`). | To see that it is still there, select it under Recent uploads and press Check status. To upload it again beside it: --new. |
| `conflict` / `another_live_upload` | Another upload to ARC is running (`<folder>`). | Wait for it, or press Stop upload on it. |
| `conflict` / `busy` | The upload program is working on this upload right now. | Wait a moment, or press Stop upload. |
| `config` / `settings_invalid` | The ARC settings are not valid: `<problems>`. | Correct them in ARC settings (command line: config --set key=value). |
| `config` / `mac_collection_missing` | This Mac's Globus collection ID is not set. | Open ARC settings and paste it: Globus Connect Personal (its icon in the menu bar) > Settings shows it (command line: config --set arc_upload.globus.mac_collection=`<ID>`). |
| `config` / `username_missing` | Your VT username is not set. | Open ARC settings and enter it: the part of your VT email before @vt.edu (command line: config --set arc_upload.ssh.username=`<name>`). |
| `config` / `known_hosts_unreadable` | The team file of ARC's SSH keys cannot be used: `<path>`. | Correct the setting, or empty it to use your own known_hosts. |
| `not_installed` / `globus_sdk_missing` | The Globus part of the data layer is not installed (the globus-sdk package). | In Terminal, in Portable_Raman_DataFed/ (from deploy-macos/, cd .. first), run ./data_layer/setup_env.sh once, or send with SSH. |
| `not_installed` / `ssh_missing` | /usr/bin/ssh is missing on this Mac. | Send with Globus; macOS normally has ssh, so ask for help. |
| `not_installed` / `terminal_missing` | Terminal or /usr/bin/open is missing on this Mac. | Send with Globus. |
| `not_installed` / `not_macos` | Uploads to ARC need macOS in this edition. | Use Demo to try the steps. |
| `command` / `not_retryable`, `not_checkable`, `nothing_to_stop`, `no_conflict` | This upload cannot be `<retried / checked / stopped / kept as a new copy>` while it is `<label>`. | (The button is disabled.) |
| `command` / `payload_missing` | The files of this upload are no longer kept on this Mac. | Press Upload to ARC to start a new upload. |
| `command` / `terminal_not_opened` | Terminal could not be opened: `<open's message>`. | Double-click ssh_upload.command in the upload's folder, or press Retry same request. |
| `command` / `home_missing` | The HOME folder `<home>` does not exist. | Run the command with --home set to the controller's folder. |
| `config` / `demo_folder_unwritable` | The demo folder `<path>` cannot be used: `<problem>`. | Choose an empty or new demo folder (command line: config --set arc_upload.demo_folder=`<folder>`). |
| `usage` / `usage` | what was wrong with the arguments | Run the command with -h to see its options. |
| `internal` / `internal` | what went wrong | Try again; if it happens again, send this message to the team. |
| `interrupted` / `interrupted` | Interrupted | Run the command again. |

Some refusals have a second wording for a case their usual text does not fit
(`arc_records.REFUSAL_VARIANTS`); the kind and the reason stay the same:

| reason (case) | error | fix |
|---|---|---|
| `already_on_arc` (a demo upload) | Demo: `<folder>` is already in the demo folder: `<path>` (checked `<date>`). | To see that it is still there, press Check status on it. To upload it again beside it, press Upload again in the controller window (command line: --new). |
| `already_on_arc` (another folder holds the same session, such as a copy made with `cp -R`) | `<folder>` holds the same session as `<other>`, which is already on ARC: `<path>` (checked `<date>`). | To see that it is still there, select it under Recent uploads and press Check status. To upload it again beside it: --new. |
| `already_on_arc` (the same, demo) | Demo: `<folder>` holds the same session as `<other>`, which is already in the demo folder: `<path>` (checked `<date>`). | To see that it is still there, press Check status on it. To upload it again beside it, press Upload again in the controller window (command line: --new). |
| `busy` (the worker runs in a Terminal window) | The upload program is working on this upload right now, in a Terminal window. | Finish or close that Terminal window, or press Stop upload. |
| `terminal_not_opened` (Check status) | Terminal could not be opened: `<open's message>`. | Double-click ssh_upload.command in the upload's folder, or press Check status again. |

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
| `GET /api/arc` | `arc-list --limit=12` alone (it never runs `arc-sessions`, which reads every session folder and lists the running programs): `{"ok": true, "uploads": <items>, "active": n, "unfinished": n, "settings": {"demo", "transport", "folder", "include_simulated", "settle_seconds"}}`, the five settings taken from `arc-list`'s flat `arc_upload.<name>` values (`null` when one is missing; the VT username and the collection IDs are not passed on); a failed command answers with its failure object |
| `POST /api/arc/upload` | body `{"session": "<folder>", "transport": "globus"\|"ssh", "fault"?: "lose_submit_reply"\|"fail_task_once"\|"drop_connection"}`, the fault one of its transport's (Globus `lose_submit_reply`, `fail_task_once`; SSH `drop_connection`; another answers 400 "fault with transport `<t>` must be one of: `<list>`" before anything runs): `arc-upload --session=<folder> --transport=<transport> --demo [--demo-fault=<fault>]`, always with `--demo`, never with `--live` or `--controller-idle` |
| `POST /api/arc/retry`, `/api/arc/stop`, `/api/arc/check`, `/api/arc/keep-copy` | body `{"upload": "u<12 hex>"}`: the command with `--upload=<id> --demo-only`; a live upload answers 403 with the command's failure (`refused` / `not_demo`) |

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
- **Uploads to ARC** (section 5.6): the Studio starts demo uploads only, and acts only on demo
  uploads; uploads to ARC itself are made in the controller window. Its page draws live uploads,
  which are made in the window, read-only. It acts on every demo upload, one started in the window
  included: a record does not say where its upload was started. An upload on ARC whose last Check
  status failed (`check_failed`) is drawn as not checked; one whose newest check could not look
  (`"looked": false`) stays drawn as checked, and its line and tool tip give that check's message and
  fix. When Upload to ARC (demo) is refused with `already_on_arc`, the page's line quotes the time of
  the newest check when that check found the copy (`ok` true), else of the verification, as the
  refusal itself does. The page's own demo engine says "the demo ARC folder" where its copy is, never
  "on ARC", and its items give `may_run` as `arc-list` does (true while its stand-in for Globus may
  run the transfer: submitting and copying). The page reads `GET /api/arc` every second
  while an upload works, waits for Terminal or a Globus sign-in (`needs`), and for 120 reads (2
  minutes) after one of its buttons; otherwise every 5 s. The fields of the `/api/arc/*` bodies:
  `session` a string, not empty, at most 200 code points, without `/`, not starting with `_` or
  `.`, and, as every text field of the server, without spaces around it, control characters, line
  separators or bidirectional controls; `transport` `globus` or `ssh`; `fault` one of the three
  names and one of its transport's (`null` is refused: leave the key out); `upload` matching
  `^u[0-9a-f]{12}$`; any other key answers 400. The `arc-*` commands run one at a time, outside
  the catalog's turn, so that a sync does not hold up the strip. The Host, Origin, Sec-Fetch-Site,
  Content-Type and 64 kB rules above apply to them as well.
  The HTTP status of a failed command follows its kind; `not_ready` gives 409, `refused` 403 and
  `not_installed` 501. A data layer without the `arc-*` commands answers 501 `"not available"` on
  each of these endpoints. `GET /api/ping` is unchanged.

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
