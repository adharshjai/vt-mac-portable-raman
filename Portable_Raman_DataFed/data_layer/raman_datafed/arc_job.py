"""The classification job as a batch job on a cluster (written for Virginia Tech ARC):

    python -m raman_datafed.arc_job --root-alias A [--context p/...] --work-dir DIR
                                    [--model-dir DIR | --model-record ID] [--limit N]
                                    [--confidence-threshold T] [--repo-id REPO]

It connects to DataFed with the credentials the user installed on the cluster themselves
(`datafed setup`), classifies what waits in `<root>-to-classify`, and saves the predictions,
exactly as `classify` does on the instrument computer. It never logs in.

The last line on stdout is one JSON object, the summary of the run; the log goes to stderr. The
exit code is 0 unless nothing could be done: an error before the first spectrum, or errors and
not one spectrum classified.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import logging
import os
import socket
import sys
from pathlib import Path

from . import job
from .config import DEFAULTS, MODE_DATAFED, CommandError, DataLayerError, Settings, utc_now

log = logging.getLogger(__name__)

SETUP_HINT = (
    "On the cluster, run 'datafed setup' once on a login node and set the default endpoint to the cluster's "
    "Globus collection (data_layer/arc/README.md)."
)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m raman_datafed.arc_job",
        description="Classify the spectra waiting in a DataFed queue collection and save the predictions.",
    )
    parser.add_argument("--root-alias", required=True, metavar="A",
                        help="datafed.root_alias of the instrument computer's settings")
    parser.add_argument("--context", default="", metavar="p/...",
                        help="DataFed project id; leave out for your own space")
    parser.add_argument("--work-dir", required=True, metavar="DIR",
                        help="folder for downloads; the default Globus endpoint of the DataFed client must be "
                        "able to write there")
    parser.add_argument("--model-dir", metavar="DIR",
                        help="folder with the four model files; left out, they come from the model record")
    parser.add_argument("--model-record", metavar="ID",
                        help="model record to fetch when the models collection holds more than one")
    parser.add_argument("--limit", type=int, metavar="N", help="classify at most N spectra")
    parser.add_argument("--confidence-threshold", type=float, metavar="T",
                        help="review.confidence_threshold of the instrument computer's settings; left out, the "
                        "model's own")
    parser.add_argument("--repo-id", default="", metavar="REPO",
                        help="datafed.repo_id of the instrument computer's settings, when it is not empty")
    return parser


def job_settings(args):
    """The settings the job needs, checked by the same rules as the settings file of the instrument
    computer. Nothing is read from or written to a file."""
    data = copy.deepcopy(DEFAULTS)
    data["mode"] = MODE_DATAFED
    data["datafed"].update(context=args.context, root_alias=args.root_alias, repo_id=args.repo_id)
    data["review"]["confidence_threshold"] = args.confidence_threshold
    work_dir = Path(args.work_dir).expanduser().resolve()
    settings = Settings(data, work_dir / "datafed_config.json", work_dir)
    problems = settings.problems()
    if args.limit is not None and args.limit < 1:
        problems.append("--limit must be at least 1")
    if args.model_dir and args.model_record:
        problems.append("--model-dir and --model-record exclude each other")
    if problems:
        raise CommandError("The arguments cannot be used", detail="; ".join(problems))
    return settings, work_dir


def run(args, catalog=None):
    """Run the job. Returns (summary, exit code). `catalog` replaces the DataFed connection in tests."""
    summary = {"ok": False, "runner": "arc", "host": socket.gethostname(),
               "slurm_job_id": os.environ.get("SLURM_JOB_ID"), "root_alias": args.root_alias,
               "context": args.context, "started_utc": utc_now()}
    try:
        settings, work_dir = job_settings(args)
        if catalog is None:
            from .datafed_catalog import DataFedCatalog

            catalog = DataFedCatalog(context=settings.context, repo_id=settings.repo_id)
        log.info("classifying what waits in %s-to-classify (%s)", settings.root_alias, catalog.label)
        report = job.run_job(
            catalog, settings, Path(args.model_dir).expanduser().resolve() if args.model_dir else None, work_dir,
            limit=args.limit, runner="arc", slurm_job_id=summary["slurm_job_id"], model_record=args.model_record,
        )
    except Exception as error:
        if isinstance(error, DataLayerError):
            summary.update(error=str(error), detail=error.detail, kind=error.kind)
            if error.kind in ("auth_required", "offline"):
                summary["hint"] = SETUP_HINT
        else:
            log.exception("the job failed")
            summary.update(error="%s: %s" % (type(error).__name__, error), detail="The traceback is in the log.",
                           kind="internal")
        summary["finished_utc"] = utc_now()
        return summary, 1
    summary.update(report)
    summary["finished_utc"] = utc_now()
    # Errors and not one spectrum classified: the batch system should show the job as failed.
    summary["ok"] = not (report["errors"] and not report["processed"])
    return summary, 0 if summary["ok"] else 1


def main(argv=None, catalog=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        force=True)
    stdout = sys.stdout
    # The datafed package prints on stdout; the summary must stay the only thing there.
    with contextlib.redirect_stdout(sys.stderr):
        summary, code = run(args, catalog)
    if "error" in summary:
        log.error("nothing was classified: %s (%s)", summary["error"], summary["detail"])
    else:
        log.info("classified %d (%d new predictions), %d wait for an upload, %d not looked at, %d failed",
                 summary["processed"], summary["predictions"], summary["skipped"], summary["remaining"],
                 len(summary["errors"]))
    stdout.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
    stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
