"""Shared fixtures for the data-layer tests: temporary HOME folders holding fake scan sessions.

The spectra here are made-up numbers written in the controller's file formats. Nothing in these
tests starts the controller or a helper program, and nothing connects to DataFed or Globus.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

DATA_LAYER = Path(__file__).resolve().parents[1]
if str(DATA_LAYER) not in sys.path:
    sys.path.insert(0, str(DATA_LAYER))

from raman_datafed import catalog as catalog_module  # noqa: E402
from raman_datafed.cli import Context  # noqa: E402

# The failures the tests provoke are logged as warnings; they would only clutter the test output.
logging.getLogger("raman_datafed").addHandler(logging.NullHandler())


def _refuse_to_connect(self, *_arguments, **_options):
    raise AssertionError("a test tried to construct the real datafed API, which connects to the DataFed server")


try:
    from datafed.CommandLib import API as _DataFedAPI
except ImportError:
    # The local-demo tests also run in an interpreter that does not have the datafed package.
    pass
else:
    # A safety net for every test that imports this module: the adapter is tested with an
    # autospec of this class or an injected object, never with the real thing.
    _DataFedAPI.__init__ = _refuse_to_connect

PIXELS = 2048
DARK_NAME = "dark_20261113_101400_001.txt"
SOFTWARE = {"name": "portable-raman-controller-datafed", "version": "1.0.0"}
INSTRUMENT = {"model": "C15471", "serial": "TEST0001", "unit": "test unit", "firmware": "1.03"}
MODEL_HASH = "5f" * 32


def make_home(test):
    """An empty HOME folder that is removed when the test ends."""
    home = Path(tempfile.mkdtemp(prefix="raman_home_")).resolve()
    test.addCleanup(shutil.rmtree, home, ignore_errors=True)
    return home


def write_dark(home, name=DARK_NAME, level=4500, simulated=True):
    """A dark spectrum file in the format the controller saves, with the description the
    controller writes beside it saying whether it was simulated. `simulated` None writes no
    description: a dark from before there were descriptions, which counts as real."""
    folder = Path(home) / "dark_measurements"
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["# Dark Measurement", "# Timestamp: 2026-11-13 10:14:00.001", "# Points: %d" % PIXELS,
             "index\traman_shift_cm-1\tintensity"]
    lines.extend("%d\t%.8f\t%d" % (index, 57.4 + 2.86 * index, level + index % 7) for index in range(PIXELS))
    path = folder / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if simulated is not None:
        write_dark_description(path, simulated)
    return path


def write_dark_description(dark_path, backend, motor=None):
    """dark_<stamp>.json beside a dark spectrum file, as the controller writes it."""
    description = {"schema": "raman-dark-description/1", "dark_file": Path(dark_path).name,
                   "captured_utc": "2026-11-13T15:14:00.001Z",
                   "simulated": {"backend": backend, "motor": backend if motor is None else motor},
                   "instrument": dict(INSTRUMENT), "software": dict(SOFTWARE)}
    path = Path(dark_path).with_suffix(".json")
    path.write_text(json.dumps(description, indent=2), encoding="utf-8")
    return path


def capture_block(sample_id="S-001", operator="Test Operator", location="Bench 1", notes="", dark_file=DARK_NAME,
                  simulated=True, laser_power_level="HIGH"):
    return {
        "schema": "raman-capture/1",
        "sample_id": sample_id,
        "operator": operator,
        "location": location,
        "notes": notes,
        "laser_power_level": laser_power_level,
        "integration_time_us": 1000000,
        "instrument": dict(INSTRUMENT),
        "simulated": {"backend": simulated, "motor": simulated},
        "dark_file": dark_file,
        "software": dict(SOFTWARE),
    }


def point_dict(session_id, point_id, scan_mode="1d-x", steps=(0, 86750, 138800), capture=None):
    """A point file as the controller writes it; `capture` None gives a file of the original project."""
    second = 1 + point_id
    point = {
        "capture_elapsed_ms": 1100,
        "capture_id": "%s_%d" % (session_id, point_id),
        "completed_utc": "2026-11-13T15:15:%02d.250Z" % second,
        "controller_x_steps": None if steps is None else steps[0] + 69400 * (point_id - 1),
        "controller_y_steps": None if steps is None else steps[1],
        "controller_z_steps": None if steps is None else steps[2],
        "intensity": [4600 + (index * 7 + point_id * 13) % 900 for index in range(PIXELS)],
        "pixel_count": PIXELS,
        "point_id": point_id,
        "protocol": 2,
        "scan_mode": scan_mode,
        "session_id": session_id,
        "started_utc": "2026-11-13T15:15:%02d.150Z" % second,
        "status": "OK",
    }
    if capture is not None:
        point["capture"] = capture
    return point


def write_point(folder, point):
    path = Path(folder) / ("point_%04d.json" % point["point_id"])
    path.write_text(json.dumps(point, indent=4, sort_keys=True), encoding="utf-8")
    return path


def make_session(home, folder="1d_x_scan_20261113_101500_123", points=3, scan_mode="1d-x", session_id=None,
                 with_capture=True, with_session_json=True, steps=(0, 86750, 138800), root=None, **fields):
    """Write a scan session folder. Returns {"session_id", "folder", "capture_ids", "files"}.

    `with_capture` False and `with_session_json` False give a session of the original project.
    `root` puts the folder somewhere else than HOME/scan_sessions.
    """
    session_id = session_id or str(uuid.uuid4())
    path = Path(root) / folder if root is not None else Path(home) / "scan_sessions" / folder
    path.mkdir(parents=True)
    capture = capture_block(**fields) if with_capture else None
    if with_session_json:
        session = {"schema": "raman-session/1", "session_id": session_id, "folder": folder, "scan_mode": scan_mode,
                   "started_utc": "2026-11-13T15:15:00.123Z"}
        session.update({key: value for key, value in capture_block(**fields).items() if key != "schema"})
        (path / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
    result = {"session_id": session_id, "folder": path, "capture_ids": [], "files": []}
    add_points(result, points, scan_mode=scan_mode, capture=capture, steps=steps)
    return result


def add_points(session, count, scan_mode="1d-x", capture=None, steps=(0, 86750, 138800)):
    """Append `count` more points to a session made by `make_session`."""
    first = len(session["capture_ids"]) + 1
    for point_id in range(first, first + count):
        point = point_dict(session["session_id"], point_id, scan_mode, steps, capture)
        session["files"].append(write_point(session["folder"], point))
        session["capture_ids"].append(point["capture_id"])
    return session


def context(test, home, catalog=None):
    """A command context on `home`, closed when the test ends."""
    ctx = Context(home, catalog=catalog)
    test.addCleanup(ctx.close)
    return ctx


def save_prediction(catalog, root_alias, raw_record, outcome="confident", top_class="r6g", confidence=0.93,
                    model_hash=MODEL_HASH, finished_utc="2026-11-13T15:20:00.000Z"):
    """Save the records a classification run saves for one raw spectrum (PROTOCOL.md 5.3).

    Stands in for the classifier job, which is not part of these tests.
    """
    names = catalog_module.ensure_layout(catalog, root_alias)
    model = catalog.create_record(
        catalog_module.model_alias(model_hash), "Test model",
        {"type": "raman.model", "name": "test-model", "sha256": model_hash}, names.models, identity_key="sha256")
    raw = raw_record.metadata
    needs_review = outcome in ("low_confidence", "rejected")
    labels = {"confident": top_class, "background": "UNKNOWN", "low_confidence": "UNKNOWN", "rejected": "REJECTED"}
    label = labels[outcome]
    deps = [("der", raw_record.id), ("der", model.id)]
    dark = None
    if raw.get("dark") and raw["dark"].get("record"):
        dark = {"record": raw["dark"]["record"], "sha256": raw["dark"]["sha256"]}
        deps.append(("der", raw["dark"]["record"]))
    metadata = {
        "type": "raman.prediction", "capture_id": raw["capture_id"], "sample_id": raw["sample_id"],
        "point_id": raw["point_id"], "raw_record": raw_record.id,
        "model": {"record": model.id, "name": "test-model", "sha256": model_hash}, "dark": dark,
        "label": label, "top_class": top_class, "confidence": confidence,
        "scores": {top_class: confidence, "di_water": round(1 - confidence, 4)},
        "outcome": outcome, "reason": "", "threshold": 0.8, "needs_review": needs_review,
        "simulated": raw["simulated"],
        "job": {"runner": "local", "host": "test-host", "slurm_job_id": None,
                "started_utc": "2026-11-13T15:19:59.000Z", "finished_utc": finished_utc},
        "review": {"status": "pending" if needs_review else "not_required", "label": None, "reviewer": None,
                   "time_utc": None, "comment": ""},
    }
    prediction = catalog.create_record(
        catalog_module.prediction_alias(raw["capture_id"], model_hash),
        "Prediction %s point %s" % (raw["sample_id"], raw["point_id"]),
        metadata, names.predictions, deps=deps, identity_key="capture_id")
    if needs_review:
        catalog.link(names.needs_review, prediction.id)
    catalog.unlink(names.to_classify, raw_record.id)
    return prediction


def cli_environment(user_home):
    """Environment for running the command line as the controller does.

    HOME points into the test folder so that nothing looks at the real ~/.datafed.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(DATA_LAYER)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HOME"] = str(user_home)
    for name in list(env):
        if name.startswith("DATAFED_"):
            del env[name]
    # A second safety net, for child processes: should a test ever run the command line in datafed
    # mode, the datafed package would look for its server on this machine and find none.
    env["DATAFED_SERVER_HOST"] = "127.0.0.1"
    env["DATAFED_SERVER_PORT"] = "9"
    return env


def run_cli(home, *arguments, user_home=None, timeout=60):
    """Run `python -m raman_datafed --home HOME ...`; returns (exit code, stdout text, stderr text)."""
    user_home = user_home or home
    done = subprocess.run(
        [sys.executable, "-m", "raman_datafed", "--home", str(home), *[str(item) for item in arguments]],
        capture_output=True, text=True, timeout=timeout, cwd=str(home if Path(home).is_dir() else user_home),
        env=cli_environment(user_home),
    )
    return done.returncode, done.stdout, done.stderr


def record_types(local_demo):
    """How many records of each metadata type the local demo catalog holds."""
    counts = {}
    for path in (Path(local_demo) / "records").glob("d_*.json"):
        kind = json.loads(path.read_text(encoding="utf-8"))["metadata"].get("type")
        counts[kind] = counts.get(kind, 0) + 1
    return counts
