"""Real measurements for the classifier tests, and helpers that put them into a test HOME.

The four data files in this folder are unchanged copies from the hand-off folder of the original
project (laser_test/build/Desktop_Qt_6_6_3_MSVC2019_64bit-Debug): points 1, 4 and 8 of
scan_sessions/1d_y_scan_20260921_152840_289, and dark_measurements/dark_20260921_153016_700.txt,
the dark spectrum measured with that scan. They are real spectra from the instrument.

What the helpers add around them (session.json, the capture block with a sample ID and an
operator) is test data in the format of this edition: the original files do not have it.
"""

import json
import shutil
import unittest
from argparse import Namespace
from pathlib import Path

import support
from raman_catalog import classifier, cli, commands_core, job
from raman_catalog.catalog import layout_for
from raman_catalog.config import load_settings

FIXTURES = Path(__file__).resolve().parent
MODEL_DIR = support.DATA_LAYER.parent / "models" / "pca_svm_pure_20260922"
MODEL_HASH = "f1f3e9ae9fcc99586ee7ec0eb93aa1a8a1fbe57db27225699ba63a70cab38c93"
CLASSES = ["r6g", "methylene_blue", "adenine", "di_water"]
DARK_FILE = "dark_20260921_153016_700.txt"
POINT_FILES = ("point_0001.json", "point_0004.json", "point_0008.json")
SESSION_FOLDER = "1d_y_scan_20260921_152840_289"
SESSION_ID = "5eadf18d-cc7e-4c19-aaa5-ff894d1b2391"

# What the original project's prediction code (svm_predictor.py) gives for these points with this
# dark spectrum and the shipped model, computed once with numpy 2.3.4, scikit-learn 1.8.0 and
# joblib 1.5.3 on macOS. Another numerical library moves the scores in about the eighth decimal.
EXPECTED_SCORES = {
    "point_0001.json": {"r6g": 0.8801429083914941, "methylene_blue": 0.06462419449983253,
                        "adenine": 0.019784618971187523, "di_water": 0.035448278137486064},
    "point_0004.json": {"r6g": 0.015265812066675748, "methylene_blue": 0.9454584623225979,
                        "adenine": 0.02368909450553462, "di_water": 0.015586631105191616},
    "point_0008.json": {"r6g": 0.05228428199372714, "methylene_blue": 0.019362618378533793,
                        "adenine": 0.01720187220573855, "di_water": 0.9111512274220004},
}
# The original's label for each point at the model's own threshold of 0.8.
EXPECTED_LABELS = {"point_0001.json": "r6g", "point_0004.json": "methylene_blue", "point_0008.json": "UNKNOWN"}
SCORE_TOLERANCE = 1e-6


def model_problem():
    """Why the shipped model cannot be loaded in this interpreter, or None when it can."""
    try:
        classifier.Predictor(MODEL_DIR)
    except classifier.ModelError as error:
        return "%s (%s)" % (error, error.detail)
    return None


MODEL_PROBLEM = model_problem()
# For the tests that run the model: they need numpy, joblib and the scikit-learn version that saved it.
needs_model = unittest.skipIf(MODEL_PROBLEM, "the shipped model cannot be loaded here: %s" % MODEL_PROBLEM)


def load_point(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def use_shipped_model(home, threshold=None):
    """Point the settings of a test HOME at the model shipped with this edition."""
    settings = load_settings(home)
    settings.set("classifier.model_dir", str(MODEL_DIR))
    settings.set("review.confidence_threshold", threshold)
    settings.save()
    return settings


def make_real_session(home, dark=True, points=POINT_FILES):
    """A session folder in HOME holding the real points, completed the way the controller writes
    them now. Returns the same dict as support.make_session."""
    home = Path(home)
    dark_name = None
    if dark:
        (home / "dark_measurements").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / DARK_FILE, home / "dark_measurements" / DARK_FILE)
        dark_name = DARK_FILE
    # Measured with the instrument, so the capture block does not call them simulated.
    capture = support.capture_block(sample_id="R6G-MB-plate", dark_file=dark_name, simulated=False)
    folder = home / "scan_sessions" / SESSION_FOLDER
    folder.mkdir(parents=True)
    session = {"schema": "raman-session/1", "session_id": SESSION_ID, "folder": SESSION_FOLDER, "scan_mode": "1d-y",
               "started_utc": "2026-09-21T19:28:40.289Z"}
    session.update({key: value for key, value in capture.items() if key != "schema"})
    (folder / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
    result = {"session_id": SESSION_ID, "folder": folder, "capture_ids": [], "files": []}
    for name in points:
        point = load_point(name)
        point["capture"] = capture
        path = folder / name
        path.write_text(json.dumps(point, indent=4, sort_keys=True), encoding="utf-8")
        result["files"].append(path)
        result["capture_ids"].append(point["capture_id"])
    return result


class HomeCase(unittest.TestCase):
    """A test HOME on the local demo catalog, with the shipped model in its settings.

    `threshold` is review.confidence_threshold; None leaves the model's own 0.8.
    """

    threshold = None

    def setUp(self):
        self.home = support.make_home(self)
        use_shipped_model(self.home, self.threshold)
        self.ctx = support.context(self, self.home)
        self.catalog = self.ctx.catalog
        self.config = self.ctx.config
        self.paths = self.ctx.paths
        self.names = layout_for(self.config.root_alias)
        self.work = self.home / "work"

    def sync(self, passes=1):
        for _pass in range(passes):
            result = commands_core.cmd_sync(Namespace(), self.ctx)
        return result

    def command(self, *arguments):
        """Run a command the way the Studio server does; it has to succeed."""
        result = cli.invoke(self.ctx, arguments)
        self.assertTrue(result["ok"], result)
        return result

    def refused(self, *arguments):
        """Run a command that has to fail; returns its failure object."""
        result = cli.invoke(self.ctx, arguments)
        self.assertFalse(result["ok"], result)
        return result

    def run_job(self, **options):
        return job.run_job(self.catalog, self.config, self.config.model_dir, self.work, **options)

    def uploaded_session(self, passes=2, **options):
        """The real points, registered and with their files in the catalog."""
        session = make_real_session(self.home, **options)
        self.sync(passes)
        return session

    def queue(self):
        return [entry.id for entry in self.catalog.list_collection(self.names.to_classify)]

    def predictions(self):
        """Every prediction record: they are kept in one collection per scan session."""
        records = []
        for entry in self.catalog.list_collection(self.names.predictions):
            if entry.id.startswith("c/"):
                records.extend(self.catalog.get_record(item.id) for item in self.catalog.list_collection(entry.id))
            else:
                records.append(self.catalog.get_record(entry.id))
        return records

    def states(self):
        return {row["capture_id"]: row["state"] for row in self.ctx.ledger.rows(newest_first=False)}

    def kinds(self):
        return support.record_types(self.paths.local_demo)

    def stored_file(self, record_id):
        """Where the local demo keeps the file of a record."""
        folder = self.paths.local_demo / "data" / record_id.replace("/", "_")
        (path,) = folder.iterdir()
        return path
