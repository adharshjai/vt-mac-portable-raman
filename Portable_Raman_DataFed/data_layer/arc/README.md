# Classification job on Virginia Tech ARC

`classify_job.slurm` runs the classifier of this edition as a Slurm batch job. It does on a
cluster what the `classify` command does on the instrument computer: it takes the spectra that
wait in the DataFed collection `<root>-to-classify`, classifies them, and saves each prediction
as a DataFed record that is "derived from" the raw spectrum, the model version and the dark
spectrum.

> **This job has not been run on ARC.** The job logic (`raman_datafed/job.py`,
> `raman_datafed/arc_job.py`) is tested on a Mac against the local demo catalog and against an
> in-memory stand-in for the DataFed server. Nothing in this folder has been tried
> on a cluster, against the real DataFed service, or with Globus. The cluster names, partitions,
> module names and the Globus collection below are taken from the public ARC and DataFed
> documentation as read on 2026-11-13 (links at the end). What could not be confirmed from the
> documentation is listed under [Not verified](#not-verified); expect to adjust something on the
> first run.

Values in `<angle brackets>` are yours to fill in. Nothing here contains an account name, a
project name or a collection ID of yours: you look those up yourself.

## What you need (you do these yourself)

The job never logs in anywhere and never asks for a password. Everything that needs your
identity is done by you, once, before the first run.

### 1. An ARC account and an allocation

- An ARC account (<https://arc.vt.edu/account>). Off campus you need the Virginia Tech VPN, and
  logging in asks for Duo.
- A compute allocation. Allocations are requested by a PI (faculty) in ColdFront,
  <https://coldfront.arc.vt.edu>; students are added to the project by their PI.
- The **Slurm account name** of that allocation. ARC's documentation: it is shown in ColdFront as
  `slurm_account_name`, it is not your PID, and the command `quota` on a login node lists your
  active accounts. It goes into the `#SBATCH --account=` line of `classify_job.slurm`.

### 2. A project directory that Globus can reach

DataFed moves every file with Globus. On ARC the documented Globus collection is
**"Virginia Tech ARC Globus Projects Directories"**. It reaches `/projects` directories only, and
only those whose owner has switched on **"Share via Globus"** for the project in ColdFront
(check the box, click Update). Your home directory is not reachable through it.

So the job's files and its work folder live under `/projects/<your-project-directory>/`, and the
owner of that project has to switch "Share via Globus" on.

### 3. The files on ARC

Copy the folder `data_layer` (it holds `raman_datafed/`, `requirements.txt` and this `arc/`
folder), from this edition's source folder or from the deploy folder next to the controller, to
`/projects/<your-project-directory>/raman/`. From the instrument computer, with the transfer host
ARC documents:

```
scp -r data_layer <your-pid>@datatransfer.arc.vt.edu:/projects/<your-project-directory>/raman/
```

The model files do not have to be copied. The instrument computer publishes them as a DataFed
record with its `sync`, and the job fetches them from there and checks their hash before loading
them. (Loading a model file runs code stored in it. Use a DataFed project in which only people
you trust can write.)

### 4. A Python environment, made once on a compute node

ARC asks that an environment is built in an interactive job on the node type it will run on,
and never inside a batch job. The job needs Python 3.11 or newer, because the pinned numpy and
scikit-learn do; the model files are only loaded with the scikit-learn version that saved them.

On a login node of the cluster you will use (`tinkercliffs1.arc.vt.edu`,
`tinkercliffs2.arc.vt.edu`, or `owl1.arc.vt.edu` to `owl3.arc.vt.edu`; both clusters have the CPU
partition `normal_q`):

```
interact --account=<your-slurm-account> --partition=normal_q
module reset
module spider Python/3
module load Python/3.12.3-GCCcore-13.3.0
python -m venv ~/venvs/raman-datafed
source ~/venvs/raman-datafed/bin/activate
pip install --upgrade pip
pip install -r /projects/<your-project-directory>/raman/data_layer/requirements.txt
exit
```

`Python/3.12.3-GCCcore-13.3.0` is the module ARC's documentation uses in its examples;
`module spider Python/3` shows what the cluster has today. If you load another one, put the same
name into `PYTHON_MODULE` in `classify_job.slurm`. Stay on one cluster: an environment built on
TinkerCliffs is not meant to be used on Owl.

Optional check of the environment, which needs no DataFed. It needs `data_layer/tests/`, which
only the edition's source folder has; the copy in the deploy folder has none. For this check,
copy `data_layer` from the source folder, and the edition's `models` folder to
`/projects/<your-project-directory>/raman/models`. Then, before the `exit` above, run

```
cd /projects/<your-project-directory>/raman/data_layer
python -m unittest discover -s tests -p "test_classifier.py"
```

These tests load the model and classify three real spectra that ship with the tests. They pass
when the scores agree with the expected ones to six decimals, and they are skipped, with the
reason, when the model cannot be loaded. (They have been run on the Mac only.)

### 5. DataFed on ARC

You need what the instrument computer already uses: a DataFed account
(<https://datafed.ornl.gov>), your Globus identity linked to it, and membership in the same
DataFed project with an allocation. Then, on an ARC login node, with the environment of step 4
active:

1. `datafed setup` and enter your DataFed user ID and password when it asks. These are the
   DataFed credentials, not the Virginia Tech ones and not your Globus login: the client sends
   the password to DataFed itself. How to set that password in DataFed's web portal is described
   in DataFed's documentation; that was not tried or checked for this text. It stores key files in
   `~/.datafed`; keep them private. The job reads those files. If it says that a newer DataFed
   client is available, do not upgrade: `requirements.txt` pins datafed 5.1.0.
2. Find the ID of ARC's collection: in the Globus web app (<https://app.globus.org>) search the
   collections for "Virginia Tech ARC Globus Projects Directories" and open its overview, which
   shows the UUID. DataFed's documentation says a collection has to be activated before DataFed
   can use it: open it in the Globus web app, sign in when it asks, and open your project
   directory. It also says that such an activation runs out after a few days, so this may have
   to be repeated before a later job.
3. `datafed ep default set <uuid-of-the-arc-collection>`
4. Check:

```
datafed user who
datafed ep default get
datafed ls -X <p/your-datafed-project> <root>-to-classify
```

   The first prints your DataFed user ID, the second the collection ID of step 3, the third the
   spectra that wait. `<root>` is `datafed.root_alias` of the instrument computer's settings,
   `raman-portable` unless it was changed. Leave `-X <p/...>` out when the instrument computer
   uses your own DataFed space instead of a project.

### 6. One transfer by hand

Before the first job, fetch one waiting spectrum by hand into the job's work folder. This shows
whether DataFed, Globus and the ARC collection work together for you, which this edition could
not test:

```
mkdir -p /projects/<your-project-directory>/raman/work/try
datafed data get -X <p/your-datafed-project> --wait <alias-from-the-listing> /projects/<your-project-directory>/raman/work/try
ls /projects/<your-project-directory>/raman/work/try
```

If the file arrives, the job has what it needs. If the transfer fails or never finishes, the job
will fail the same way; see [Not verified](#not-verified).

## Fill in the batch script

Open `classify_job.slurm` and set, at the top:

| line | value |
|---|---|
| `#SBATCH --account=` | your Slurm account name (step 1) |
| `RAMAN_DIR` | `/projects/<your-project-directory>/raman` (step 3) |
| `VENV_DIR`, `PYTHON_MODULE` | as made in step 4; the defaults match the commands above |
| `ROOT_ALIAS` | `datafed.root_alias` of the instrument computer |
| `CONTEXT` | `datafed.context` of the instrument computer (`p/...`), or `""` for your own space |
| `CONFIDENCE_THRESHOLD` | `review.confidence_threshold` of the instrument computer when it is not `null` |
| `REPO_ID` | `datafed.repo_id` of the instrument computer when it is not empty |

On the instrument computer the settings are in `datafed_config.json` next to the controller, or
shown by the data layer's `config` command.

The script asks for one core for 30 minutes on `normal_q`. Almost all of that time goes into the
Globus transfers: each spectrum (a 30 kB file and a few milliseconds of computing) is downloaded
with its own transfer, and the DataFed client checks a transfer every 4 s, so one spectrum takes at
least 4 s and probably 10 to 30 s (not measured). 30 minutes therefore suit about 50 spectra. For
a longer queue either set `LIMIT="50"`, and submit the job again until the queue is empty, or give
the job more time in the `#SBATCH --time=` line, for example `0-04:00:00` for 400 spectra. When
Slurm ends a job at its time limit, nothing is lost and the next job continues, but the summary
line below is not written. The script stops with a message if a `<placeholder>` is still in it.

## The first run

On a login node:

```
cd /projects/<your-project-directory>/raman/data_layer/arc
sbatch classify_job.slurm
squeue
```

`sbatch` answers with the job number. `squeue` shows the job while it waits or runs;
`sacct` shows it afterwards, and `seff <job number>` what it used. For a first try, put
`LIMIT="1"` in the script: the job then classifies one spectrum and stops.

## Reading the output

The job writes `raman-classify-<job number>.out` into the folder you submitted from. It holds
the log, and as its **last line** one JSON object, the summary:

```
tail -n 1 raman-classify-<job number>.out | python3 -m json.tool
```

A job that Slurm ended at its time limit has no summary line, so this command fails; `sacct` then
shows the job as `TIMEOUT`. Submit it again with a `LIMIT` or more time (see
[Fill in the batch script](#fill-in-the-batch-script)).

| key | meaning |
|---|---|
| `ok` | `false` when nothing could be done; Slurm then shows the job as `FAILED` |
| `processed` | spectra that now have a prediction and left the queue |
| `predictions` | prediction records this job created (a spectrum whose prediction an earlier, interrupted job had saved counts in `processed` only) |
| `skipped` | spectra left in the queue because their file, or their dark spectrum's file, is not uploaded yet |
| `remaining` | spectra not looked at, because of `LIMIT` or because the job had to stop |
| `errors` | one entry per spectrum that failed, with the reason (it stays in the queue for the next job); an entry without a capture id says why the whole run stopped |
| `items` | per spectrum: label, top class, confidence, outcome, and whether a person has to review it |
| `error`, `detail`, `hint` | only when the job could not start its work: what went wrong and what to do |

Typical first-run messages:

- `DataFed credentials are missing or not valid ...`: step 5 was not done on ARC, or `~/.datafed`
  is not readable from the compute node. (The message goes on about "this Mac"; it was written
  for the instrument computer and means the computer the job runs on.)
- `DataFed cannot be reached`: the compute node could not connect to `datafed.ornl.gov`, port
  7512. Try `datafed user who` inside an `interact` session on the same partition.
- `DataFed cannot be used from this Mac` (detail `connect: ...`): the DataFed client on the
  cluster could not open a connection at all, for example because the server runs another API
  version or its server key cannot be used. "This Mac" means the computer the job runs on. Check
  `datafed user who` on a login node; do not upgrade the pinned datafed 5.1.0 without checking the
  data layer against it.
- `The datafed Python package is not available in this interpreter`: the job did not run in the
  environment of step 4. Check `VENV_DIR` and `PYTHON_MODULE` in `classify_job.slurm`. (The detail
  names `data_layer/setup_env.sh`, the instrument computer's way to make the environment.)
- `There is no collection <root>-to-classify`, or a refusal by DataFed that names that
  collection: wrong `ROOT_ALIAS` or `CONTEXT`, or the instrument computer has not synced anything
  yet. The job creates nothing in that case.
- `<root>-models holds 2 model versions; name the model record to use`: put the alias the message
  lists into `MODEL_RECORD`.
- `The model record ... has no files yet`: the instrument computer has not finished uploading the
  model; run `sync` there with Globus Connect Personal running.
- `The download of ... did not finish within 600 s`, and after the second one in a row
  `2 downloads in a row failed, so the run stopped`: the Globus transfers to the work folder are
  not moving; see step 6.

Running the job twice is harmless. A spectrum leaves the queue only after its prediction is
saved, and a job that was cancelled half-way is simply continued by the next one; no prediction
is made twice.

## Back on the instrument computer

The next `sync` there finds the new predictions and shows them in the Data tab; the ones that
need a person appear in the review list. Every prediction record names the Slurm job number and
the node it was computed on (`job.runner` is `arc`).

Scores computed on ARC can differ slightly from scores computed on the Mac, because the
numerical libraries differ. On the Mac, two such libraries gave scores that differed in the
eighth decimal; ARC was not measured. The label only changes if a score sits that close to the
threshold. Each prediction record lists the library versions it was computed with
(`job.software`).

## Not verified

These points could not be settled from the public documentation, and nothing here was run:

1. **Paths inside the Globus collection.** The DataFed client asks Globus to deliver files to
   `<default endpoint>` + the absolute path on the cluster, for example
   `/projects/<your-project-directory>/raman/work/...`. This only works if the ARC collection
   shows your project directory under that same path. ARC's documentation does not say how the
   path looks inside the collection. Compare it in the Globus web app; step 6 tests it. If the
   paths differ, this job cannot run as it is.
2. **DataFed and the ARC collection.** Whether DataFed is allowed to transfer to
   "Virginia Tech ARC Globus Projects Directories" on your behalf (consents, session timeouts,
   the collection's access rules) is not documented by either side. Step 6 tests it.
3. **Network access from compute nodes.** The job connects to `datafed.ornl.gov` on TCP port 7512
   from the compute node. ARC's documentation does not state whether compute nodes may open that
   connection. At start the DataFed client also asks pypi.org for its newest version; on a node
   without internet access that request only ends when the system's connection time-out does,
   which can take a few minutes.
4. **`datafed setup` on a login node** needs the same connection as point 3.
5. **Module names** change with ARC's software updates; `Python/3.12.3-GCCcore-13.3.0` is the one
   in ARC's examples on the date above.
6. **The DataFed adapter itself** (`raman_datafed/datafed_catalog.py`) has only been tested
   against stand-ins. That holds for the instrument computer as well, not only for ARC.

## Where the values come from

| value | source |
|---|---|
| clusters, login nodes, partition `normal_q` | <https://www.docs.arc.vt.edu/resources/compute/00tinkercliffs.html>, <https://www.docs.arc.vt.edu/resources/compute/01owl.html> |
| `--account` is required; where the account name is shown | <https://www.docs.arc.vt.edu/usage/job_scheduling/02_slurm_options.html>, <https://www.docs.arc.vt.edu/usage/00faq.html> |
| batch script layout, `module reset`, `sbatch`, `squeue`, `sacct`, `seff`, `quota`, `interact` | <https://www.docs.arc.vt.edu/usage/job_scheduling/01_slurm_overview.html> |
| allocations and ColdFront | <https://www.docs.arc.vt.edu/pi_info/allocations.html> |
| Python environments with `venv`, the Python module, where to build them | <https://www.docs.arc.vt.edu/usage/virtual_environments.html>, <https://www.docs.arc.vt.edu/usage/virtual_environments/virtual_env_pip.html> |
| the Globus collection, `/projects` only, "Share via Globus" | <https://www.docs.arc.vt.edu/software/globus.html> |
| `datatransfer.arc.vt.edu`, `/home` and `/projects` shared by the clusters | <https://www.docs.arc.vt.edu/usage/data_transfer.html> |
| DataFed account, Globus identity, allocation, endpoint of a cluster | <https://ornl.github.io/DataFed/system/getting_started.html> |
| `datafed setup`, key files in `~/.datafed`, default endpoint, server host and port | <https://ornl.github.io/DataFed/user/client/install.html> |
| `datafed` commands used above | <https://ornl.github.io/DataFed/user/cli/reference.html> |
