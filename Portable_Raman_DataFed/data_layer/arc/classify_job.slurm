#!/bin/bash
# Classification job of the Portable Raman controller, DataFed edition, for Virginia Tech ARC.
#
# NOT RUN ON ARC YET. The job logic is tested on a Mac against the local demo catalog and an
# in-memory stand-in for DataFed; this script and the steps in README.md follow ARC's public
# documentation and have not been tried on a cluster. Read README.md first.
#
# Submit from a login node:   sbatch classify_job.slurm
#
#SBATCH --job-name=raman-classify
#SBATCH --account=<your-slurm-account>
#SBATCH --partition=normal_q
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --time=0-00:30:00
#SBATCH --output=raman-classify-%j.out

set -eo pipefail

# ---- set these before the first run (README.md says where each value comes from) ----

# Folder under /projects that holds data_layer/. Its project directory must have "Share via
# Globus" switched on in ColdFront: DataFed delivers the spectra into $RAMAN_DIR/work with Globus.
RAMAN_DIR="/projects/<your-project-directory>/raman"
# The Python environment made in README step 4, and the module it was made with.
VENV_DIR="$HOME/venvs/raman-datafed"
PYTHON_MODULE="Python/3.12.3-GCCcore-13.3.0"
# datafed.root_alias and datafed.context of the instrument computer's settings.
# CONTEXT="" means your own DataFed space instead of a project.
ROOT_ALIAS="raman-portable"
CONTEXT="<p/your-datafed-project>"
# review.confidence_threshold and datafed.repo_id of the instrument computer's settings.
# Leave empty when they are null / empty there.
CONFIDENCE_THRESHOLD=""
REPO_ID=""
# Empty: the model files come from the model record in DataFed (its hash is checked before use).
# Or name a folder with the four model files.
MODEL_DIR=""
# Only when <root>-models holds more than one model version: the alias or id of the one to use.
MODEL_RECORD=""
# Empty: classify everything that waits. A number: at most that many spectra.
LIMIT=""

# ---- nothing below needs editing ----

case "$RAMAN_DIR $CONTEXT" in
  *"<"*)
    echo "Replace the <placeholders> at the top of $0 first (see README.md)." >&2
    exit 2
    ;;
esac

module reset
module load "$PYTHON_MODULE"
source "$VENV_DIR/bin/activate"
# Only from here on: the module command and activate scripts may read variables that are not set.
set -u

export PYTHONPATH="$RAMAN_DIR/data_layer${PYTHONPATH:+:$PYTHONPATH}"
WORK_DIR="$RAMAN_DIR/work"
mkdir -p "$WORK_DIR"

arguments=(--root-alias "$ROOT_ALIAS" --work-dir "$WORK_DIR")
if [ -n "$CONTEXT" ]; then arguments+=(--context "$CONTEXT"); fi
if [ -n "$CONFIDENCE_THRESHOLD" ]; then arguments+=(--confidence-threshold "$CONFIDENCE_THRESHOLD"); fi
if [ -n "$REPO_ID" ]; then arguments+=(--repo-id "$REPO_ID"); fi
if [ -n "$MODEL_DIR" ]; then arguments+=(--model-dir "$MODEL_DIR"); fi
if [ -n "$MODEL_RECORD" ]; then arguments+=(--model-record "$MODEL_RECORD"); fi
if [ -n "$LIMIT" ]; then arguments+=(--limit "$LIMIT"); fi

echo "Job $SLURM_JOB_ID on $(hostname): python -m raman_datafed.arc_job ${arguments[*]}"
# The last line it prints is the JSON summary. A non-zero exit code (nothing could be done)
# ends this script too, so that Slurm shows the job as FAILED.
python -m raman_datafed.arc_job "${arguments[@]}"
