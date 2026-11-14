"""Stand-in for the data layer, for the controller's tests and window snapshots only.

It answers the commands of data_layer/PROTOCOL.md section 6 with canned JSON, in the shapes the
real package (data_layer/raman_datafed) gives in its local demo mode: failures carry "kind",
classify puts its predictions into the rows at once, a capture's metadata problem is reported by
the sync that finds it, review-show computes the classifier's curve also for a rejected spectrum.
It contacts nothing, uses nothing outside the standard library, and touches only these files in
the HOME folder it is given:

  fake_data_layer_calls.jsonl    one line per call: the arguments, the working directory, PYTHONPATH
  fake_data_layer_state.json     what earlier calls changed (offline switch, syncs, reviews, imports)
  fake_data_layer_control.json   written by a test to make a command misbehave or to change the
                                 canned catalog, for example
                                 {"sync": {"mode": "slow", "seconds": 2}, "status": {"mode": "error"},
                                  "catalog": "datafed", "held_simulated": 4, "catalog_changed": true}
                                 modes: error, slow, garbage, crash, silent; held_simulated counts
                                 simulated captures that DataFed mode keeps on this Mac;
                                 catalog_changed: the settings name another catalog than the one the
                                 rows describe, until the next sync
  fake_studio.pid, fake_studio_stop   the pretended Studio server writes the first and ends when
                                 the second appears

The test build copies this file to <HOME>/data_layer/raman_datafed/__main__.py, so the controller
finds it the way it finds the real package: <interpreter> -m raman_datafed --home HOME <command>.
Every value below is made up; none of it is a measurement.
"""

import datetime
import hashlib
import json
import math
import os
import signal
import sys
import time

try:
    import fcntl
except ImportError:      # Windows: the tests there run one command at a time
    fcntl = None

STATES = ("captured", "registered", "uploading", "uploaded", "classified", "needs_review", "reviewed", "accepted", "error")
STATE_LABELS = {"captured": "Captured", "registered": "Registered", "uploading": "Uploading", "uploaded": "Uploaded",
                "classified": "Classified", "needs_review": "Needs review", "reviewed": "Reviewed", "accepted": "Accepted",
                "error": "Error"}
PENDING = ("captured", "registered", "uploading", "error")
CLASSES = ["r6g", "methylene_blue", "adenine", "di_water"]
FLAGS = ("on", "off", "no-open", "verbose", "retry-failed")
DROPPED_PIXELS = 90      # the first pixels, which the model does not see
DEMO_LABEL = "Local demo catalog (files on this Mac only; nothing is sent anywhere)"
DEMO_USER, DEMO_CONTEXT = "demo-user", "local demo (this Mac)"
THRESHOLD = 0.8
MODEL = {"name": "pca_svm_pure_20260922", "sha256": hashlib.sha256(b"stand-in model").hexdigest(), "record": "d/3"}
DARK = {"file": "dark_20261113_101400_001.txt", "sha256": hashlib.sha256(b"stand-in dark").hexdigest(), "record": "d/5"}
LINE_SESSION = ("1d_x_scan_20261113_101500_123", "5f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f")
SINGLE_SESSION = ("single_20261113_103000_456", "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d")
OLD_SESSION = ("2d_scan_20260924_141500_001", "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b")
PEAKS = {"r6g": (612, 773, 1185, 1311, 1362, 1509, 1650), "methylene_blue": (449, 502, 772, 1396, 1623),
         "adenine": (623, 723, 1248, 1333, 1482), "di_water": (1640, 3230)}


def h12(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse(argv):
    """--home HOME <command> [options]; an option is "--name value", "--name=value" or a flag."""
    home, command, options = None, None, {}
    words = iter(argv)
    for word in words:
        if word.startswith("--"):
            name, equals, value = word[2:].partition("=")
            if not equals:
                value = True if name in FLAGS else next(words, None)
            if command is None and name == "home":
                home = value
            else:
                options[name] = value
        elif command is None:
            command = word
    return home, command, options


class Files:
    def __init__(self, home):
        self.home = home
        self.state_path = os.path.join(home, "fake_data_layer_state.json")
        self.lock = None

    def read(self, name, default):
        try:
            with open(os.path.join(self.home, name), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    def log_call(self, argv, command):
        entry = {"command": command, "argv": argv, "cwd": os.getcwd(), "pythonpath": os.environ.get("PYTHONPATH", "")}
        with open(os.path.join(self.home, "fake_data_layer_calls.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def load_state(self):
        """The state, locked until save_state or the end of the process: two commands may run at once."""
        if fcntl is not None:
            self.lock = open(self.state_path + ".lock", "w")
            fcntl.flock(self.lock, fcntl.LOCK_EX)
        state = {"offline": False, "syncs": 0, "classified": [], "reported": [], "reviews": {}, "imported": [], "fixed": None,
                 "last_sync_utc": None}
        state.update(self.read("fake_data_layer_state.json", {}))
        return state

    def save_state(self, state):
        partial = self.state_path + ".writing"
        with open(partial, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(partial, self.state_path)


def prediction(record, label, top, confidence, outcome):
    return {"record_id": record, "label": label, "top_class": top, "confidence": confidence, "outcome": outcome,
            "needs_review": outcome in ("low_confidence", "rejected")}


def capture(number, session, mode, point, sample, minute, state, simulated=True, made=None, extra=None):
    folder, session_id = session
    capture_id = ("single_" + session_id) if mode == "single" else "%s_%d" % (session_id, point)
    item = {
        "capture_id": capture_id, "session_id": session_id, "session_folder": folder, "point_id": point,
        "point_file": "point_%04d.json" % point, "scan_mode": mode, "sample_id": sample, "operator": "ui tests",
        "timestamp_utc": "2026-11-13T15:%02d:%02d.123Z" % (minute, (point * 2) % 60), "simulated": simulated,
        "position_mm": None if mode == "single" else {"x": 2.0 * (point - 1), "y": 2.5, "z": 4.0},
        "state": state, "state_label": STATE_LABELS[state],
        "record_id": None, "record_alias": None, "upload": None, "prediction": None, "review": None, "error": None,
        "_number": number,
    }
    if state not in ("captured", "error"):
        item["record_id"] = "d/%d" % (100 + number)
        item["record_alias"] = "rs-" + h12(capture_id)
        item["upload"] = {"task_id": "task/%d" % number, "status": "running" if state == "uploading" else "succeeded"}
    if made is not None:
        item["prediction"] = made
        item["review"] = {"status": "pending" if made["needs_review"] else "not_required", "label": None, "reviewer": None,
                          "time_utc": None}
    if extra:
        item.update(extra)
    return item


SCORES = {
    2: {"r6g": 0.61, "methylene_blue": 0.24, "adenine": 0.10, "di_water": 0.05},
    3: {},
    8: {"methylene_blue": 0.74, "r6g": 0.15, "di_water": 0.07, "adenine": 0.04},
}
# The classifier's own wording (raman_datafed/classifier.py).
REASONS = {2: "Low pure-class confidence", 3: "Low raw signal", 8: "Low pure-class confidence"}
# What classify predicts for the captures that wait for it; an imported capture is adenine.
LATER = {5: ("adenine", 0.91), 6: ("methylene_blue", 0.88), 7: ("methylene_blue", 0.86), 9: ("r6g", 0.95)}


def later(number):
    return LATER.get(number, ("adenine", 0.9))


def captures(state):
    """The canned ledger, newest first, after what the earlier calls changed."""
    syncs = state["syncs"]
    waiting = "captured" if syncs == 0 else "uploading" if syncs == 1 else "uploaded"
    moving = "uploading" if syncs == 0 else "uploaded"

    def classified(stage, number, label, confidence):
        # Like the real classify, which copies its predictions into the rows itself.
        if stage == "uploaded" and number in state["classified"]:
            return "accepted", prediction("d/%d" % (200 + number), label, label, confidence, "confident")
        return stage, None

    rows = [
        capture(1, LINE_SESSION, "1d-x", 1, "TEST-PLATE-A", 15, "accepted", made=prediction("d/201", "r6g", "r6g", 0.97, "confident")),
        capture(2, LINE_SESSION, "1d-x", 2, "TEST-PLATE-A", 15, "needs_review",
                made=prediction("d/202", "UNKNOWN", "r6g", 0.61, "low_confidence")),
        capture(3, LINE_SESSION, "1d-x", 3, "TEST-PLATE-A", 15, "needs_review",
                made=prediction("d/203", "REJECTED", None, None, "rejected")),
        capture(4, LINE_SESSION, "1d-x", 4, "TEST-PLATE-A", 15, "accepted",
                made=prediction("d/204", "UNKNOWN", "di_water", 0.93, "background")),
    ]
    for number, stage in ((5, "uploaded"), (6, moving), (7, waiting)):
        stage, made = classified(stage, number, *later(number))
        rows.append(capture(number, LINE_SESSION, "1d-x", number, "TEST-PLATE-A", 15, stage, made=made))
    rows.append(capture(8, SINGLE_SESSION, "single", 1, "TEST-MB-01", 30, "needs_review",
                        made=prediction("d/208", "UNKNOWN", "methylene_blue", 0.74, "low_confidence")))
    if state["fixed"]:
        stage, made = classified("captured" if syncs <= state.get("fixed_at", 0) else "uploaded", 9, *later(9))
        rows.append(capture(9, OLD_SESSION, "2d", 3, state["fixed"], 5, stage, simulated=None, made=made))
    else:
        rows.append(capture(9, OLD_SESSION, "2d", 3, None, 5, "error", simulated=None, extra={
            "operator": None,
            "error": {"stage": "metadata", "attempts": 0,
                      "message": "point_0003.json has no capture block: sample ID, operator and location are missing"}}))
    for index, imported in enumerate(state["imported"]):
        session = (imported["folder"], "1mp0%04d-0000-4000-8000-000000000000" % index)
        for point in (1, 2):
            number = 20 + 2 * index + point
            stage, made = classified("captured" if syncs <= imported["at"] else "uploaded", number, *later(number))
            rows.append(capture(number, session, "2d", point, "IMPORTED-%d" % (index + 1), 40 + index, stage, simulated=None, made=made))
    for row in rows:
        made = row["prediction"]
        review = state["reviews"].get(made["record_id"]) if made else None
        if review:
            row["review"] = {key: review.get(key) for key in ("status", "label", "reviewer", "time_utc")}
            if review.get("queued"):
                row["review"]["queued"] = True
            made["needs_review"] = False
            row["state"], row["state_label"] = "reviewed", STATE_LABELS["reviewed"]
    rows.sort(key=lambda row: row["timestamp_utc"], reverse=True)
    return rows


def public(row):
    return {key: value for key, value in row.items() if not key.startswith("_")}


def catalog_changed(state, control):
    """The settings name another catalog than the rows describe: until the first sync after the
    change, which binds the rows to the new one as the real ledger does."""
    return bool(control.get("catalog_changed")) and not state.get("rebound")


def status(state, control):
    rows = captures(state)
    counts = dict.fromkeys(STATES, 0)
    for row in rows:
        counts[row["state"]] += 1
    pending = sum(counts[name] for name in PENDING)
    reviews_queued = sum(1 for review in state["reviews"].values() if review.get("queued"))
    demo = control.get("catalog") != "datafed"
    # Simulated captures that DataFed mode keeps on this Mac (datafed.include_simulated false). The
    # canned rows have none; a test gives their number. The demo catalog takes everything.
    held = 0 if demo else int(control.get("held_simulated", 0))
    changed = catalog_changed(state, control)
    problems = []
    if state["offline"]:
        problems.append("The catalog cannot be reached." + (" %d capture(s) wait for the next sync." % pending if pending else ""))
    if changed:
        problems.append("The catalog settings changed: the next sync registers every capture in %s."
                        % (DEMO_LABEL if demo else "DataFed (your own space)"))
    if counts["error"]:
        problems.append("%d capture(s) cannot be registered as their files are (their Error rows say why). Where Sample ID, "
                        "Operator or Location is missing, import the session folder again with these fields (Data tab: Import "
                        "scan folder; command line: import-session --folder FOLDER)." % counts["error"])
    if held:
        problems.append("%d simulated capture(s) stay on this Mac and are not sent to DataFed: to send them as well, run "
                        "config --set datafed.include_simulated=true." % held)
    return {
        "mode": "local-demo" if demo else "datafed",
        "mode_label": DEMO_LABEL if demo else "DataFed (your own space)",
        "demo": demo, "online": not state["offline"], "user": DEMO_USER if demo else "u/testuser",
        "context": DEMO_CONTEXT if demo else "", "endpoint": None if demo else "stand-in-endpoint",
        "counts": counts, "total": len(rows), "pending": pending, "held_simulated": held, "reviews_queued": reviews_queued,
        "catalog_changed": changed,
        # What a plain sync can move on. The canned error row lacks its fields: only import-session moves it.
        "sync_work": counts["captured"] + counts["registered"] + counts["uploading"] + reviews_queued,
        "last_sync_utc": state["last_sync_utc"], "problems": problems,
    }


def review_item(row, offline=False):
    """A review-list item. Offline it comes from the ledger alone: what only the catalog knows is null."""
    number = row["_number"]
    item = public(row)
    item["prediction_id"] = row["prediction"]["record_id"]
    if offline:
        item.update({"scores": None, "reason": None, "threshold": None, "model": None})
    else:
        item.update({"scores": SCORES.get(number, {}), "reason": REASONS.get(number, ""), "threshold": THRESHOLD, "model": MODEL})
    return item


def review_items(state):
    return [review_item(row, state["offline"]) for row in captures(state) if row["state"] == "needs_review"]


def review_list(state, control):
    """"online" is null when nothing waits: the catalog then did not have to be asked. Until the sync
    after a change of catalog nothing waits either: the predictions are the previous catalog's."""
    items = [] if catalog_changed(state, control) else review_items(state)
    return {"items": items, "online": (not state["offline"]) if items else None}


def spectrum(number):
    """A made-up spectrum: a flat dark level, a slope and the peaks of the top class; number 3, which
    the classifier rejected, is next to no signal. As in the real review-show, the curve the
    classifier would be given is computed whenever the dark is there, also for the rejected one."""
    top = max(SCORES.get(number) or {"di_water": 1.0}, key=lambda name: (SCORES.get(number) or {"di_water": 1.0})[name])
    weak = number == 3
    shift, raw, dark = [], [], []
    for index in range(2048):
        x = 57.42 + 1.676 * index - 6.1e-5 * index * index
        ripple = math.sin(index * 12.9898) * 43758.5453
        noise = (ripple - math.floor(ripple) - 0.5) * (4.0 if weak else 14.0)
        signal_level = 0.0 if weak else 220.0 * math.exp(-x / 1900.0)
        for center in PEAKS[top]:
            width = 160.0 if center > 3000 else 11.0
            signal_level += (0.0 if weak else 1500.0) * math.exp(-((x - center) / width) ** 2)
        shift.append(round(x, 2))
        dark.append(900 + int(round(6 * math.sin(index / 97.0))))
        raw.append(dark[-1] + int(round(signal_level + noise)))
    net = [value - level for value, level in zip(raw, dark)][DROPPED_PIXELS:]
    low, high = min(net), max(net)
    processed = [None] * DROPPED_PIXELS + [round((value - low) / (high - low), 6) for value in net]
    return {"raman_shift": shift, "intensity": raw, "dark": dark, "processed": processed, "processed_start": DROPPED_PIXELS,
            "source": "local file"}


def node(record, alias, title, kind, role, info):
    return {"id": record, "alias": alias, "title": title, "type": kind, "role": role, "info": info}


def provenance(state, record):
    """The chain around a raw spectrum or a prediction record, as the real command builds it."""
    for row in captures(state):
        made = row["prediction"]
        if record not in (row["record_id"], row["record_alias"], made and made["record_id"]):
            continue
        if row["record_id"] is None:
            break
        from_prediction = bool(made) and record == made["record_id"]
        # Titles, types and info keys as the real package makes them (sync.raw_title, job.prediction_title).
        marked = " [simulated]" if row["simulated"] else ""
        raw = node(row["record_id"], row["record_alias"], "%s point %s (%s)%s" % (row["sample_id"], row["point_id"], row["scan_mode"], marked),
                   "raman.raw_spectrum", "source" if from_prediction else "start",
                   {"capture_id": row["capture_id"], "sample_id": row["sample_id"], "point_id": row["point_id"],
                    "timestamp_utc": row["timestamp_utc"], "simulated": row["simulated"]})
        if not made:
            return {"nodes": [raw], "edges": [], "truncated": False}
        review = row["review"] or {}
        result = node(made["record_id"], "rp-" + h12(row["capture_id"] + ":" + MODEL["sha256"]),
                      "Prediction %s point %s%s" % (row["sample_id"], row["point_id"], marked), "raman.prediction",
                      "start" if from_prediction else "derived",
                      {"capture_id": row["capture_id"], "sample_id": row["sample_id"], "point_id": row["point_id"],
                       "simulated": row["simulated"], "label": made["label"], "top_class": made["top_class"],
                       "confidence": made["confidence"], "outcome": made["outcome"], "needs_review": made["needs_review"],
                       "review_status": review.get("status")})
        model = node(MODEL["record"], "rmodel-" + MODEL["sha256"][:12], "Classifier model " + MODEL["name"], "raman.model", "source",
                     {"name": MODEL["name"], "sha256": MODEL["sha256"]})
        dark = node(DARK["record"], "rdark-" + h12(DARK["sha256"]), "Dark spectrum " + DARK["file"], "raman.dark_spectrum", "source",
                    {"file": DARK["file"], "sha256": DARK["sha256"]})
        nodes = [result, raw, model, dark] if from_prediction else [raw, result, model, dark]
        edges = [{"from": made["record_id"], "to": other["id"], "relation": "der", "label": "is derived from"}
                 for other in (raw, model, dark)]
        return {"nodes": nodes, "edges": edges, "truncated": False}
    raise not_found(record)


def review_show(state, prediction):
    for row in captures(state):
        if not row["prediction"] or row["prediction"]["record_id"] != prediction:
            continue
        if state["offline"]:
            return {"item": review_item(row, offline=True), "spectrum": spectrum(row["_number"]), "provenance": [], "classes": CLASSES,
                    "online": False,
                    "problems": ["The catalog cannot be reached: scores and provenance are shown once it answers again."]}
        chain = provenance(state, prediction)
        nodes = {node["id"]: node for node in chain["nodes"]}
        sources = [dict(nodes[edge["to"]], relation=edge["relation"], label=edge["label"]) for edge in chain["edges"]]
        return {"item": review_item(row), "spectrum": spectrum(row["_number"]), "provenance": sources, "classes": CLASSES,
                "online": True, "problems": []}
    raise not_found(prediction)


class Failure(Exception):
    """A failure object of section 6; kind and exit code as the real package gives them."""

    def __init__(self, error, detail="", kind="command", code=1):
        super().__init__(error)
        self.detail, self.kind, self.code = detail, kind, code


def not_found(record):
    return Failure("Nothing in the catalog has the id %s" % record, "The stand-in knows the records of its canned captures only.",
                   kind="not_found")


def offline_failure():
    return Failure("The local demo catalog is switched to offline", "Simulated by the stand-in. Remove it with: offline --off",
                   kind="offline")


def sync(state, control):
    result = {"busy": False, "online": not state["offline"], "ingested": 0, "registered": 0, "uploads_started": 0,
              "uploads_done": 0, "predictions_found": 0, "reviews_found": 0, "reviews_sent": 0, "errors": []}
    # The real sync binds the ledger to the catalog in use before it contacts any catalog.
    if control.get("catalog_changed"):
        state["rebound"] = True
    if not state["offline"]:
        before = {row["capture_id"]: row["state"] for row in captures(state)}
        state["syncs"] += 1
        for row in captures(state):
            step = (before.get(row["capture_id"]), row["state"])
            if step == ("captured", "uploading") or step == ("captured", "uploaded"):
                result["registered"] += 1
                result["uploads_started"] += 1
            if step[0] in ("captured", "uploading") and step[1] == "uploaded":
                result["uploads_done"] += 1
            if step == ("uploaded", "accepted"):
                result["predictions_found"] += 1
        for review in state["reviews"].values():
            if review.pop("queued", False):
                result["reviews_sent"] += 1
    # A capture's metadata problem is in the result of the sync that finds it; later ones leave it to status.
    for row in captures(state):
        if row["state"] == "error" and row["capture_id"] not in state["reported"]:
            state["reported"].append(row["capture_id"])
            result["errors"].append({"capture_id": row["capture_id"], "stage": "metadata", "message": row["error"]["message"]})
    state["last_sync_utc"] = utc_now()
    result["status"] = status(state, control)
    return result


def classify(state):
    if state["offline"]:
        raise offline_failure()
    waiting = [row for row in captures(state) if row["state"] == "uploaded" and row["prediction"] is None]
    state["classified"].extend(row["_number"] for row in waiting)
    left = sum(1 for row in captures(state) if row["state"] == "uploading")
    items = []
    for row in waiting:
        label, confidence = later(row["_number"])
        items.append({"capture_id": row["capture_id"], "raw_record": row["record_id"], "prediction_record": "d/%d" % (200 + row["_number"]),
                      "label": label, "top_class": label, "confidence": confidence, "outcome": "confident", "needs_review": False,
                      "created": True})
    return {"runner": "local", "processed": len(waiting), "predictions": len(waiting), "skipped": left, "remaining": 0, "errors": [],
            "items": items, "model": MODEL}


def review_set(state, options):
    decision, label = options.get("decision"), (options.get("label") or "").strip()
    reviewer = " ".join((options.get("reviewer") or "").split())
    if decision not in ("confirm", "relabel", "reject"):
        raise Failure("Invalid arguments: argument --decision: invalid choice: %s" % decision,
                      "usage: python -m raman_datafed review-set ...", kind="usage", code=2)
    if not reviewer:
        raise Failure("A reviewer name is required", "It is stored with the review: --reviewer NAME")
    choices = ", ".join(CLASSES + ["unknown"]) + "."
    if decision == "relabel" and not label:
        raise Failure("relabel needs --label", "One of: " + choices)
    if decision == "relabel" and label.lower() != "unknown" and label not in CLASSES:
        raise Failure("%s is not a class of the model" % label, "One of: " + choices)
    if decision != "relabel" and label:
        raise Failure("--label goes with --decision relabel only")
    for row in captures(state):
        made = row["prediction"]
        if made and made["record_id"] == options.get("prediction"):
            state["reviews"][made["record_id"]] = {
                "status": {"confirm": "confirmed", "relabel": "relabelled", "reject": "rejected"}[decision],
                # confirm keeps the model's best answer: the top class, or REJECTED for a rejected spectrum.
                "label": ("UNKNOWN" if label.lower() == "unknown" else label) if decision == "relabel"
                else (made["top_class"] or made["label"]) if decision == "confirm" else None,
                "reviewer": reviewer, "time_utc": utc_now(), "comment": (options.get("comment") or "").strip(),
                "queued": bool(state["offline"]),
            }
            updated = [item for item in captures(state) if item["capture_id"] == row["capture_id"]][0]
            return {"item": review_item(updated, state["offline"]), "queued": bool(state["offline"])}
    raise not_found(options.get("prediction"))


def import_session(state, options):
    folder = options.get("folder")
    if not folder:
        raise Failure("Invalid arguments: the following arguments are required: --folder",
                      "usage: python -m raman_datafed import-session ...", kind="usage", code=2)
    name = os.path.basename(os.path.normpath(folder))
    if name == OLD_SESSION[0]:
        # The canned folder of the original controller, already under scan_sessions: its point becomes
        # usable once the fields are supplied. Without them the real command refuses the whole folder.
        about = {"folder": name, "session_id": OLD_SESSION[1], "copied": False}
        if state["fixed"]:
            return dict(about, ingested=0, skipped=1, errors=[])
        missing = ["--" + key for key in ("sample-id", "operator", "location") if not (options.get(key) or "").strip()]
        if missing:
            raise Failure("%s lacks fields that have to be supplied: %s" % (name, ", ".join(missing)),
                          "Run import-session again with those options.", kind="metadata")
        state["fixed"], state["fixed_at"] = options["sample-id"], state["syncs"]
        return dict(about, ingested=1, skipped=0, errors=[])
    # Any other folder counts as one from elsewhere, which the real command copies under scan_sessions.
    about = {"folder": name, "session_id": "1mp0%04d-0000-4000-8000-000000000000" % len(state["imported"]), "copied": True}
    if any(imported["folder"] == name for imported in state["imported"]):
        return dict(about, ingested=0, skipped=2, errors=[], copied=False)
    state["imported"].append({"folder": name, "at": state["syncs"]})
    return dict(about, ingested=2, skipped=0, errors=[])


def studio(files, options):
    """Pretends to be the Studio server: one line with an address, then it stays until told to stop."""
    address = {"ok": True, "url": "http://127.0.0.1:%s/" % (options.get("port") or 8765)}
    sys.stdout.write(json.dumps(address) + "\n")
    sys.stdout.flush()
    with open(os.path.join(files.home, "fake_studio.pid"), "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    stop = os.path.join(files.home, "fake_studio_stop")
    deadline = time.time() + 60        # never left behind by a test that failed half-way
    while not os.path.exists(stop) and time.time() < deadline:
        time.sleep(0.05)
    return 0


def answer(command, options, files, control):
    if command == "status":
        return status(files.load_state(), control)
    if command == "list":
        rows = [public(row) for row in captures(files.load_state())]
        if options.get("state"):
            rows = [row for row in rows if row["state"] == options["state"]]
        if options.get("session"):
            rows = [row for row in rows if options["session"] in (row["session_id"], row["session_folder"])]
        limit = int(options["limit"]) if options.get("limit") else len(rows)
        return {"items": rows[:limit], "total": len(rows)}
    if command == "review-list":
        return review_list(files.load_state(), control)
    if command == "review-show":
        return review_show(files.load_state(), options.get("prediction"))
    if command == "provenance":
        state = files.load_state()
        if state["offline"]:
            raise offline_failure()
        return provenance(state, options.get("record"))
    state = files.load_state()
    if command == "sync":
        result = sync(state, control)
    elif command == "classify":
        result = classify(state)
    elif command == "review-set":
        result = review_set(state, options)
    elif command == "import-session":
        result = import_session(state, options)
    elif command == "offline":
        if control.get("catalog") == "datafed":
            raise Failure("offline is a switch of the local demo catalog only",
                          "In datafed mode the connection is the real one and cannot be simulated away.")
        if options.get("on") or options.get("off"):
            state["offline"] = bool(options.get("on"))
        result = {"offline": state["offline"]}
    else:
        raise Failure("Invalid arguments: argument <command>: invalid choice: '%s'" % command,
                      "usage: python -m raman_datafed [-h] [--home HOME] [--verbose] <command> ...", kind="usage", code=2)
    files.save_state(state)
    return result


def main(argv):
    home, command, options = parse(argv)
    files = Files(home or os.getcwd())
    files.log_call(argv, command)
    control = files.read("fake_data_layer_control.json", {})
    behaviour = control.get(command) or {}
    mode = behaviour.get("mode")
    if mode == "slow":
        time.sleep(float(behaviour.get("seconds", 1.0)))
    elif mode == "garbage":
        sys.stdout.write("this is not the JSON object the controller expects\n")
        return 0
    elif mode == "silent":
        sys.stderr.write("stand-in: ending without an answer, as asked\n")
        return 3
    elif mode == "crash":
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    try:
        if mode == "error":
            raise Failure(behaviour.get("error", "The stand-in was told to fail"),
                          behaviour.get("detail", "Canned failure of the stand-in data layer."))
        if command == "studio":
            return studio(files, options)
        payload = {"ok": True}
        payload.update(answer(command, options, files, control))
        code = 0
    except Failure as failure:
        payload, code = {"ok": False, "error": str(failure), "detail": failure.detail, "kind": failure.kind}, failure.code
    sys.stdout.write(json.dumps(payload) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
