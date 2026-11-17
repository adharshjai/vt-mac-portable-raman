"""Stand-in for the data layer, for the controller's tests and window snapshots only.

It answers the commands of data_layer/PROTOCOL.md section 6 with canned JSON, in the shapes the
real package (data_layer/raman_catalog) gives in its local demo mode: failures carry "kind",
classify puts its predictions into the rows at once, a capture's metadata problem is reported by
the sync that finds it, review-show computes the classifier's curve also for a rejected spectrum.
It contacts nothing, uses nothing outside the standard library, and touches only these files in
the HOME folder it is given:

  fake_data_layer_calls.jsonl    one line per call: the arguments, the working directory, PYTHONPATH
  fake_data_layer_state.json     what earlier calls changed (offline switch, syncs, reviews, imports)
  fake_data_layer_control.json   written by a test to make a command misbehave or to change the
                                 canned catalog, for example
                                 {"sync": {"mode": "slow", "seconds": 2}, "status": {"mode": "error"},
                                  "catalog_changed": true}
                                 modes: error, slow, garbage, crash, silent;
                                 catalog_changed: the settings name another catalog than the one the
                                 rows describe, until the next sync
  fake_studio.pid, fake_studio_stop   the pretended Studio server writes the first and ends when
                                 the second appears

Uploads to ARC (the arc-* commands, config and config --set arc_upload.*) answer in the shapes of the
design's section 8 and of the fix plan's decisions I1 to I4; their uploads and settings are kept in the
state file under "arc". The control file's key "arc" steers them. status (also the one a sync brings)
gains arc_unfinished and arc_attention, counted from the uploads; the control file's keys of these names
replace the counts, for example
  {"arc": {"sessions": [{"folder": "...", "kind": "unknown", "end": "stopped", "points": 8}],
           "script": ["queued", "signing_in", "transferring:12/21", "verified"],
           "upload": "conflict:earlier_upload", "ready": {"globus": ["mac_collection_missing"]},
           "uploads": [{"state": "conflict", "transport": "globus"}], "config_set": "error", "absent": true,
           "check": {"ok": false, "lists": 1}, "settings": {"arc_upload.settle_seconds": 5}},
   "arc_unfinished": 2, "arc_attention": 1}
  sessions: the session folders arc-sessions lists, from a few values each (see arc_session); a folder
          without session.json has no session ID, sample or dark names, and one whose why_not is odd_name
          (a name the data layer does not take) is reported with 0 spectra, 2 files and 0 bytes, as the
          real one is
  script: the states a new upload goes through, one step at every arc-list; "transferring:12/21"
          (files) or "transferring:40%" (bytes), "failed:<reason>", "conflict:checking" (nothing sent),
          and the display states not_watched, not_started and interrupted ("not_watched:verifying" and
          "interrupted:submitting" name the stored state)
  upload: arc-upload refuses with this reason (a conflict reason makes the earlier upload it names)
  ready: the problems of a transport, by reason; else the missing collection ID or VT username
  uploads: uploads made by earlier runs, read when the state file has none yet; besides the keys of
          arc_new_record: "worker": false (a working state whose program has not started), "checks"
          (its checks so far: the newest one that looked, with "ok": false, shows check_failed; one with
          "looked": false could not look and changes nothing), "ssh_username", "task_status" (of its Globus
          transfer: SUCCEEDED, FAILED, ACTIVE or INACTIVE) and "may_run" (Globus may still run its transfer; by
          default, as the data layer has it, true while the task status is ACTIVE or INACTIVE, else without a
          task status for a Globus upload in submitting, uncertain or transferring)
  check: what Check status of an upload on ARC finds ("ok", "message"; "looked": false for a check that
          could not look, with its "fix") and after how many arc-lists its program ends ("lists", 0 by
          default: at the next one); the upload shows Stop meanwhile
  settings: the arc_upload values the settings file holds before any config --set, invalid ones too
          (every arc-* command but arc-doctor then refuses with settings_invalid)
  config_set: "error" refuses every config --set (config_detail: its detail)
  absent: a data layer without the arc-* commands, which it answers as unknown commands

The test build copies this file to <HOME>/data_layer/raman_catalog/__main__.py, so the controller
finds it the way it finds the real package: <interpreter> -m raman_catalog --home HOME <command>.
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
# The classifier's own wording (raman_catalog/classifier.py).
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
    changed = catalog_changed(state, control)
    problems = []
    if state["offline"]:
        problems.append("The catalog cannot be reached." + (" %d capture(s) wait for the next sync." % pending if pending else ""))
    if changed:
        problems.append("The catalog settings changed: the next sync registers every capture in %s."
                        % DEMO_LABEL)
    if counts["error"]:
        problems.append("%d capture(s) cannot be registered as their files are (their Error rows say why). Where Sample ID, "
                        "Operator or Location is missing, import the session folder again with these fields (Data tab: Import "
                        "scan folder; command line: import-session --folder FOLDER)." % counts["error"])
    return {
        "mode": "local-demo",
        "mode_label": DEMO_LABEL,
        "demo": True, "online": not state["offline"], "user": DEMO_USER,
        "context": DEMO_CONTEXT, "endpoint": None,
        "counts": counts, "total": len(rows), "pending": pending, "reviews_queued": reviews_queued,
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
                      "usage: python -m raman_catalog review-set ...", kind="usage", code=2)
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
                      "usage: python -m raman_catalog import-session ...", kind="usage", code=2)
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


# ---------------------------------------------------------------- uploads to ARC
# The arc-* commands (data_layer/PROTOCOL.md section 6; the design's sections 7, 8 and Appendix A),
# config and config --set arc_upload.*. Nothing is copied and nothing is contacted: the answers only
# have the shapes and the words of the real ones. An upload that arc-upload makes is kept in the state
# file and moves one step of its script at every arc-list.

ARC_COMMANDS = ("arc-sessions", "arc-upload", "arc-list", "arc-status", "arc-check", "arc-retry", "arc-stop", "arc-keep-copy",
                "arc-doctor")
ARC_DEFAULTS = {
    "arc_upload.demo": True, "arc_upload.transport": "globus",
    "arc_upload.folder": "/projects/pcl_photonics/raman_globus_upload", "arc_upload.include_simulated": False,
    "arc_upload.settle_seconds": 60, "arc_upload.demo_folder": "data_catalog/arc_demo",
    "arc_upload.globus.mac_collection": "", "arc_upload.globus.arc_collection": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
    "arc_upload.globus.collection_root": "/projects", "arc_upload.globus.client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2",
    "arc_upload.globus.deadline_hours": 24, "arc_upload.globus.force_login": False,
    "arc_upload.ssh.username": "", "arc_upload.ssh.host": "datatransfer.arc.vt.edu", "arc_upload.ssh.known_hosts": "",
}
ARC_TUTORIAL_CLIENT = "61338d24-54d5-408f-a10d-66c06b59f6d2"
# What config prints of a settings file that holds only the defaults (data_layer/raman_catalog/config.py).
ARC_CORE_SETTINGS = {"catalog": {"root_alias": "raman-portable"},
                     "review": {"confidence_threshold": None}, "classifier": {"model_dir": "models/pca_svm_pure_20260922"},
                     "sync": {"max_attempts": 5}}
ARC_WORKING = ("queued", "waiting_for_terminal", "preparing", "signing_in", "checking", "submitting", "transferring", "verifying",
               "placing")
ARC_LABELS = {"queued": "Waiting to start", "waiting_for_terminal": "Waiting for Terminal", "preparing": "Preparing the files",
              "signing_in": "Signing in", "checking": "Checking ARC", "submitting": "Handing over to Globus",
              "transferring": "Copying with Globus", "verifying": "Checking the copy on ARC", "placing": "Putting the copy in place",
              "uncertain": "Not sure Globus got it", "conflict": "Name already on ARC", "verified": "On ARC, checked", "failed": "Failed",
              "stopped": "Stopped", "not_watched": "Not being watched", "not_started": "Not started", "interrupted": "Interrupted",
              "check_failed": "On ARC, last check failed"}
# Fix plan I2: the display states of uploads that need the person, counted while they changed in the last
# ARC_ATTENTION_DAYS days (status.arc_attention, arc-list's attention).
ARC_ATTENTION = ("failed", "conflict", "uncertain", "interrupted", "not_started", "not_watched", "check_failed")
ARC_ATTENTION_DAYS = 7
# What a check of an upload on ARC finds (the Globus and demo texts of the data layer).
ARC_CHECK_TEXTS = {"ok": "Still on ARC: {files} files with the right sizes.",
                   "ok_demo": "Demo: still in the demo folder: {files} files with the right sizes.",
                   "differs": "The folder on ARC differs from this upload: 1 differ, for example point_0003.json.",
                   "differs_demo": "Demo: the folder in the demo folder differs from this upload: 1 differ, for example "
                                   "point_0003.json.",
                   # A check that could not look (review REG-01): its words and its fix (arc_records.check_note).
                   "not_looked": "You did not finish signing in within 10 minutes. Check status could not look.",
                   "not_looked_fix": "Press Check status again and sign in when the page opens."}
# The fix of a check that could not look, when the control file names none (arc_records.CHECK_AGAIN_FIX).
ARC_CHECK_AGAIN_FIX = "Nothing was changed. Press Check status to look again."
# The reasons for which the data layer stops before it counts a session folder: it says 0 spectra, 2 files and
# 0 bytes then (arc_session.check). A folder without a readable session.json is counted, but it has no session
# ID, sample, start or dark names.
ARC_NOT_READ = ("odd_name", "unknown_session")
ARC_NO_SESSION_JSON = ("no_session_json", "unreadable_session")
# Folders of HOME the demo folder must stay out of (arc_settings._KEEP_OUT).
ARC_KEEP_OUT = ("scan_sessions", "dark_measurements", "data_catalog/arc_uploads")
PLANNED_POINTS = {"single": 1, "1d-x": 13, "1d-y": 13, "2d": 124}
# The sessions of scan_sessions/, newest first: the two of the canned captures (made with the simulators)
# and the folder of the original controller, which has no session.json.
ARC_SESSIONS = [
    {"folder": SINGLE_SESSION[0], "session_id": SINGLE_SESSION[1], "scan_mode": "single", "sample_id": "TEST-MB-01", "points": 1,
     "started_utc": "2026-11-13T15:30:00.456Z", "kind": "simulated", "files": 6, "bytes": 61254},
    {"folder": LINE_SESSION[0], "session_id": LINE_SESSION[1], "scan_mode": "1d-x", "sample_id": "TEST-PLATE-A", "points": 13,
     "started_utc": "2026-11-13T15:15:00.123Z", "kind": "simulated", "files": 21, "bytes": 433104},
    {"folder": OLD_SESSION[0], "session_id": OLD_SESSION[1], "scan_mode": "2d", "sample_id": None, "points": 3,
     "started_utc": "2026-09-24T18:15:00.001Z", "kind": "unknown", "end": "ended", "files": 6, "bytes": 90210,
     "why_not": "no_session_json"},
]
# Appendix A.1: kind, error and fix of each refusal.
ARC_REFUSALS = {
    "unknown_session": ("not_found", "There is no session folder {folder} in scan_sessions.",
                        "Choose a session from the list (command line: arc-sessions)."),
    "unknown_upload": ("not_found", "There is no upload {id} on this Mac.", "Choose an upload under Recent uploads (command line: arc-list)."),
    "no_session_json": ("metadata", "{folder} has no session.json, so this edition did not record it.",
                        "Add it with Import scan folder in the Data tab (command line: import-session --folder {folder}), then upload."),
    "odd_name": ("refused", "This session holds a file (or has a name) that cannot be sent safely: {name}.", "Rename it, then upload."),
    "no_points": ("not_ready", "{folder} has no saved spectrum.", "There is nothing to upload."),
    "running": ("not_ready", "{folder} is still being recorded.",
                "Wait until the scan has ended; the chart title then says finished, stopped or failed."),
    "settling": ("not_ready", "This scan ended less than a minute ago, or is still being written.", "Try again in {seconds} s."),
    "future_time": ("not_ready", "A file of this session has a date in the future.", "Check this Mac's clock."),
    "link": ("refused", "This session folder holds a link ({name}); only plain files are sent.",
             "Replace it with a plain file or move it out of the folder."),
    "name_clash": ("refused", "{folder} has a folder of its own named dark, where the upload puts the dark spectra.",
                   "Rename that folder, then upload."),
    "simulated_not_allowed": ("refused", "This session was made in simulation; simulated sessions are not sent to ARC.",
                              "Tick Demo to try the upload. To send simulated sessions to ARC's simulated/ folder, turn on \"Also send "
                              "simulated sessions\" in ARC settings (command line: config --set arc_upload.include_simulated=true)."),
    "simulation_unknown": ("refused", "It is not recorded whether this session was made in simulation, so it is handled like a simulated one.",
                           "If it was measured with the real instruments, tick \"Measured with the real instruments\" (command line: "
                           "--declare-real). Otherwise turn on \"Also send simulated sessions\" in ARC settings."),
    "mixed": ("refused", "Some points of this session were made in simulation and some were not, so it is not uploaded.",
              "Check the session; nothing was sent."),
    "declare_real_not_unknown": ("refused", "This session records whether it was simulated; \"measured with the real instruments\" is "
                                 "only for sessions that do not.", "Upload it without that choice."),
    "in_progress": ("conflict", "This session is being uploaded already.", "Stop that upload first, or wait for it."),
    "earlier_upload": ("conflict", "This session has an earlier upload that ended as {label}.",
                       "Press Retry same request on it, or start a new upload (command line: --new)."),
    "already_on_arc": ("conflict", "{folder} is already on ARC: {path} (checked {date}).",
                       "To see that it is still there, select it under Recent uploads and press Check status. To upload it again "
                       "beside it: --new."),
    "another_live_upload": ("conflict", "Another upload to ARC is running ({folder}).", "Wait for it, or press Stop upload on it."),
    "busy": ("conflict", "The upload program is working on this upload right now.", "Wait a moment, or press Stop upload."),
    "mac_collection_missing": ("config", "This Mac's Globus collection ID is not set.",
                               "Open ARC settings and paste it: Globus Connect Personal (its icon in the menu bar) > Settings shows it "
                               "(command line: config --set arc_upload.globus.mac_collection=<ID>)."),
    "username_missing": ("config", "Your VT username is not set.",
                         "Open ARC settings and enter it: the part of your VT email before @vt.edu (command line: config --set "
                         "arc_upload.ssh.username=<name>)."),
    "globus_sdk_missing": ("not_installed", "The Globus part of the data layer is not installed (the globus-sdk package).",
                           "In Terminal, in Portable_Raman_Catalog/ (from deploy-macos/, cd .. first), run ./data_layer/setup_env.sh once, "
                           "or send with SSH."),
    "not_retryable": ("command", "This upload cannot be retried while it is {label}.", "(The button is disabled.)"),
    "not_checkable": ("command", "This upload cannot be checked while it is {label}.", "(The button is disabled.)"),
    "nothing_to_stop": ("command", "This upload cannot be stopped while it is {label}.", "(The button is disabled.)"),
    "no_conflict": ("command", "This upload cannot be kept as a new copy while it is {label}.", "(The button is disabled.)"),
    "terminal_not_opened": ("command", "Terminal could not be opened: {reason}.",
                            "Double-click ssh_upload.command in the upload's folder, or press Retry same request."),
    "settings_invalid": ("config", "The ARC settings are not valid: {problems}.",
                         "Correct them in ARC settings (command line: config --set key=value)."),
}
# Other wordings of some refusals (arc_records.REFUSAL_VARIANTS): (reason, variant) -> (error, fix).
ARC_REFUSAL_VARIANTS = {
    ("already_on_arc", "demo"): ("Demo: {folder} is already in the demo folder: {path} (checked {date}).",
                                 "To see that it is still there, press Check status on it. To upload it again beside it, press "
                                 "Upload again in the controller window (command line: --new)."),
    ("busy", "terminal"): ("The upload program is working on this upload right now, in a Terminal window.",
                           "Finish or close that Terminal window, or press Stop upload."),
}
# Appendix A.2: what a failed upload says, what to do, and whether Retry same request can continue it.
ARC_FAILURES = {
    "task_failed": ("Globus could not finish the copy: the connection to this Mac was lost. Files copied so far are in {incoming} on ARC.",
                    "Press Retry same request: the same files go into the same place.", True),
    "signin_timeout": ("You did not finish signing in within 10 minutes. Nothing was sent.",
                       "Press Retry same request and sign in when the page opens.", True),
    "verify_mismatch": ("The copy on ARC does not match this Mac's list of files (for example point_0004.json). It was not put in place.",
                        "Press Retry same request: the files are copied again.", True),
    "ssh_interrupted": ("The SSH connection ended during the upload. Files sent so far are in {incoming} on ARC.",
                        "Press Retry same request: all files are sent again, into the same folder on ARC.", True),
    "payload_changed": ("The files kept for this upload were changed on this Mac, so the same request cannot be sent.",
                        "Start a new upload.", False),
    "terminal_not_opened": ("Terminal could not be opened (the stand-in was told so).",
                            "Press Retry same request, or double-click ssh_upload.command in the upload's folder.", True),
    # Also while Globus still runs the transfer (an odd answer while the upload program watched it): with
    # "task_status": "ACTIVE", Stop of a check of it cancels that transfer.
    "globus_error": ("Globus could not be reached, or gave an answer this program does not expect (no connection).",
                     "Check that this Mac is online, then press Retry same request. If it happens again, send this "
                     "message to the team.", True),
}
# The demo wording of some results (arc_records.DEMO_RESULTS, review COR-05): the demo folder, not ARC. A fix of
# None is the result's own.
ARC_FAILURES_DEMO = {
    "task_failed": ("Globus could not finish the copy: the connection to this Mac was lost. Files copied so far are in {incoming}.",
                    None),
    "verify_mismatch": ("The copy in the demo folder does not match this Mac's list of files (for example point_0004.json). It was not "
                        "put in place.", None),
    "ssh_interrupted": ("The SSH connection ended during the upload. Files sent so far are in {incoming}.",
                        "Press Retry same request: all files are sent again, into the same folder."),
}


class ArcFailure(Failure):
    """A refusal of an arc-* command: a failure object with "reason", "fix" and sometimes more; `variant`
    picks another wording of the same reason (ARC_REFUSAL_VARIANTS)."""

    def __init__(self, refusal, upload=None, variant=None, **values):
        kind, error, fix = ARC_REFUSALS[refusal]
        if variant is not None:
            error, fix = ARC_REFUSAL_VARIANTS[(refusal, variant)]
        words = dict({"folder": "", "id": "", "label": "", "path": "", "date": "", "seconds": 0, "name": "", "reason": "",
                      "problems": ""}, **values)
        super().__init__(error.format(**words), "", kind=kind, code=1)
        self.extra = {"reason": refusal, "fix": fix.format(**words)}
        if upload is not None:
            self.extra["upload"] = upload
        if refusal == "settling":
            self.extra["retry_after_seconds"] = words["seconds"]


def arc_options(argv):
    """How the window passes the arc-* commands' values: "--name=value", bare flags ("--dry-run"), and
    config's --set=key=value as often as it is given."""
    options = {"set": []}
    words = iter(argv)
    for word in words:
        if not word.startswith("--"):
            continue
        name, equals, value = word[2:].partition("=")
        if name == "home":
            if not equals:
                next(words, None)
        elif name == "set":
            options["set"].append(value if equals else next(words, ""))
        else:
            options[name] = value if equals else True
    return options


def arc_reason(reason, **values):
    kind, error, fix = ARC_REFUSALS[reason]
    words = dict({"folder": "", "seconds": 0, "name": ""}, **values)
    return {"reason": reason, "text": error.format(**words), "fix": fix.format(**words)}


def arc_settings(arc):
    settings = dict(ARC_DEFAULTS)
    settings.update(arc["config"])
    return settings


def arc_whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def arc_problems(settings, home):
    """The sentences of arc_settings.problems() for the values the stand-in judges; [] when valid."""
    found = []
    for key in ("arc_upload.demo", "arc_upload.include_simulated"):
        if not isinstance(settings[key], bool):
            found.append("%s must be true or false" % key)
    if settings["arc_upload.transport"] not in ("globus", "ssh"):
        found.append('arc_upload.transport must be "globus" or "ssh"')
    if not arc_whole(settings["arc_upload.settle_seconds"], 10, 3600):
        found.append("arc_upload.settle_seconds must be a whole number of seconds from 10 to 3600")
    folder = settings["arc_upload.demo_folder"]
    if not isinstance(folder, str) or not folder.strip():
        found.append("arc_upload.demo_folder must name a folder: a path relative to the controller's folder, such as "
                     "data_catalog/arc_demo, or an absolute path")
    else:
        root = os.path.normpath(folder if os.path.isabs(folder) else os.path.join(home, folder))
        for name in ARC_KEEP_OUT:
            kept = os.path.normpath(os.path.join(home, name))
            if root == kept or root.startswith(kept + os.sep):
                found.append("arc_upload.demo_folder must not be inside %s/ of the controller's folder" % name)
    if not arc_whole(settings["arc_upload.globus.deadline_hours"], 1, 168):
        found.append("arc_upload.globus.deadline_hours must be a whole number of hours from 1 to 168")
    if not isinstance(settings["arc_upload.globus.force_login"], bool):
        found.append("arc_upload.globus.force_login must be true or false")
    return found


def arc_value(key, text):
    """What config --set stores: a text key as typed, any other JSON when it parses (config.set_from_text)."""
    if isinstance(ARC_DEFAULTS.get(key), str):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def arc_collection_path(path, root):
    if root == "/":
        return path
    return "/" + path[len(root) + 1:] if path.startswith(root + "/") else None


def arc_demo_root(settings, home):
    folder = settings["arc_upload.demo_folder"]
    if not isinstance(folder, str) or not folder.strip():
        folder = ARC_DEFAULTS["arc_upload.demo_folder"]      # an invalid value: arc_problems() names it
    return os.path.normpath(folder if os.path.isabs(folder) else os.path.join(home, folder))


def arc_area(kind, live, settings, declared=False):
    """The kind rule of section 4.4: (area, refusal reason or None)."""
    if kind == "mixed":
        return None, "mixed"
    if kind == "real" or (kind == "unknown" and declared):
        return "real", None
    if not live or settings["arc_upload.include_simulated"]:
        return "simulated", None
    return None, "simulated_not_allowed" if kind == "simulated" else "simulation_unknown"


def arc_base(settings, area):
    return settings["arc_upload.folder"] + ("/simulated" if area == "simulated" else "")


def arc_state(state, control):
    arc = state.setdefault("arc", {})
    if "config" not in arc:
        # What the settings file holds before any config --set: the control file's "settings" (invalid ones too).
        arc["config"] = dict(control.get("settings") or {})
    arc.setdefault("made", 0)
    if "uploads" not in arc:
        # The uploads the control file names, as if earlier runs had made them: newest first, each a second
        # older than the one before it unless the control file gives its created_utc, so that arc-list,
        # which sorts by created_utc, keeps their order (two made in the same millisecond could swap).
        arc["uploads"] = []
        now = datetime.datetime.now(datetime.timezone.utc)
        for index, spec in enumerate(control.get("uploads") or []):
            spec = dict(spec)
            if not spec.get("created_utc"):
                spec["created_utc"] = arc_utc(now - datetime.timedelta(seconds=index + 1))
            arc_new_record(arc, arc_settings(arc), spec)
    return arc


def arc_utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def arc_age_seconds(utc_text):
    try:
        then = datetime.datetime.strptime(utc_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def arc_counts(items):
    """(unfinished, attention) of arc-list items: uploads in a working state, uncertain or in conflict, and
    uploads in a display state of ARC_ATTENTION that changed less than ARC_ATTENTION_DAYS ago."""
    unfinished = sum(1 for item in items if item["state"] in ARC_WORKING + ("uncertain", "conflict"))
    attention = sum(1 for item in items if item["display_state"] in ARC_ATTENTION
                    and arc_age_seconds(item["updated_utc"]) < ARC_ATTENTION_DAYS * 86400)
    return unfinished, attention


def arc_status_counts(files, control):
    """What status gains (arc_unfinished, arc_attention), counted from the stand-in's uploads as the real
    status counts them from the records; numbers in the control file win (the toggle's tests give them)."""
    told = control.get("arc") or {}
    counts = {"arc_unfinished": 0, "arc_attention": 0}
    if not told.get("absent"):
        state = files.read("fake_data_layer_state.json", {})
        arc = arc_state({"arc": dict(state.get("arc") or {})}, told)
        settings = arc_settings(arc)
        unfinished, attention = arc_counts([arc_item(record, settings, files.home) for record in arc["uploads"]])
        counts = {"arc_unfinished": unfinished, "arc_attention": attention}
    for key in counts:
        if key in control:
            counts[key] = control[key]
    return counts


def arc_session(spec, settings, home, arc):
    """A session item of arc-sessions (section 8.2) from a few given values."""
    base = next((dict(item) for item in ARC_SESSIONS if item["folder"] == spec.get("folder")), dict(ARC_SESSIONS[1]))
    session = dict({"end": "finished", "files": 21, "bytes": 433104, "dark": "paired"}, **base)
    session.update(spec)
    if session.get("why_not") in ARC_NOT_READ + ARC_NO_SESSION_JSON:
        # Nothing of session.json: no sample, no session ID, no start, no dark names, kind unknown.
        session.update(session_id=None, sample_id=None, started_utc=None, dark="none", kind="unknown")
    if session.get("why_not") in ARC_NOT_READ:
        # The data layer stops before it counts the points (arc_session.check): 0 spectra, ended without an end
        # mark, and only the manifest and SHA256SUMS to send. The given values describe the folder, not the answer.
        session.update(points=0, points_planned=None, end="ended", files=2, bytes=0)
    folder, points = session["folder"], session["points"]
    planned = session.get("points_planned", PLANNED_POINTS.get(session["scan_mode"]))
    end = session["end"]
    words = {"finished": "finished", "failed": "ended with an error", "ended": "ended (no end mark)",
             "stopped": "stopped early (%d of %s)" % (points, planned)}
    end_text = words.get(end, end)
    why = None
    if session.get("why_not"):
        seconds = int(session.get("retry_after_seconds", 42))
        why = arc_reason(session["why_not"], folder=folder, seconds=seconds,
                         name=folder if session["why_not"] == "odd_name" else "data_link")
        if session["why_not"] == "running":
            end_text = "still running"
        elif session["why_not"] == "settling":
            end_text = "ended %d s ago" % (settings["arc_upload.settle_seconds"] - seconds)
    live_area, live_refusal = arc_area(session["kind"], True, settings)
    demo_area, demo_refusal = arc_area(session["kind"], False, settings)
    # As the real arc-sessions: no path where the session may not go (the window then shows none).
    cluster = arc_base(settings, live_area) + "/" + folder if live_area else None
    demo_cluster = arc_base(settings, demo_area) + "/" + folder if demo_area else None
    root = settings["arc_upload.globus.collection_root"]
    dark = "dark_20261113_101400_001.txt"
    darks = [] if session["dark"] == "none" else [
        {"file": dark, "on_this_mac": session["dark"] != "missing", "kind": "simulated", "sent": session["dark"] == "paired",
         "reason": None if session["dark"] == "paired" else "missing"}]
    uploads = {"live": None, "demo": None}
    for record in sorted(arc["uploads"], key=lambda made: made["created_utc"]):
        # Uploads are found by the session ID, which a folder that was not read does not have.
        if record["session_folder"] == folder and session["session_id"]:
            item = arc_item(record, settings, home)
            uploads["demo" if record["demo"] else "live"] = {key: item[key] for key in (
                "upload_id", "state", "display_state", "display_label", "transport", "updated_utc")}
            uploads["demo" if record["demo"] else "live"]["final_path"] = item["destination"]["final_path"]
    for where, shown in (session.get("uploads") or {}).items():
        uploads[where] = {"upload_id": "u00000000000%d" % (where == "demo"), "state": "verified" if shown == "check_failed" else shown,
                          "display_state": shown, "display_label": ARC_LABELS.get(shown, shown), "transport": "globus",
                          "updated_utc": utc_now(), "final_path": cluster or arc_base(settings, "simulated") + "/" + folder}
    return {
        "folder": folder, "session_id": session["session_id"], "scan_mode": session["scan_mode"], "started_utc": session["started_utc"],
        "sample_id": session["sample_id"], "points": points, "points_planned": planned, "end": end, "end_text": end_text,
        "kind": session["kind"], "files": session["files"], "bytes": session["bytes"], "darks": darks,
        "ready": why is None, "retry_after_seconds": int(session.get("retry_after_seconds", 42)) if why else 0, "why_not": why,
        "live": {"allowed": live_refusal is None, "why_not": arc_reason(live_refusal) if live_refusal else None, "area": live_area,
                 "cluster_path": cluster, "globus_path": arc_collection_path(cluster, root) if cluster else None},
        "demo": {"allowed": demo_refusal is None, "why_not": arc_reason(demo_refusal) if demo_refusal else None, "area": demo_area,
                 "demo_path": arc_demo_root(settings, home) + demo_cluster if demo_cluster else None},
        "uploads": uploads, "warnings": list(session.get("warnings") or []),
    }


def arc_sessions(arc, settings, home, control):
    return [arc_session(spec, settings, home, arc) for spec in (control["sessions"] if "sessions" in control else ARC_SESSIONS)]


def arc_script(transport, demo):
    if transport == "ssh":
        middle = ["checking", "transferring:40%", "transferring:100%"] if not demo else ["checking", "transferring:12/21"]
        return ["queued"] + ([] if demo else ["waiting_for_terminal"]) + ["preparing", "signing_in"] + middle + ["verifying", "placing", "verified"]
    return ["queued", "preparing", "signing_in", "checking", "submitting", "transferring:12/21", "verifying", "placing", "verified"]


def arc_new_record(arc, settings, spec, shown_session=None):
    """An upload record, made by arc-upload (for the arc-sessions item `shown_session`) or named by the
    control file ("uploads")."""
    arc["made"] += 1
    session = shown_session or next((item for item in ARC_SESSIONS if item["folder"] == spec.get("session")), ARC_SESSIONS[1])
    transport, demo = spec.get("transport", "globus"), bool(spec.get("demo", False))
    created = spec.get("created_utc") or utc_now()
    script = spec.get("script") or ([spec["state"]] if spec.get("state") else arc_script(transport, demo))
    record = {
        "upload_id": "u" + hashlib.sha256(("stand-in upload %d" % arc["made"]).encode("utf-8")).hexdigest()[:12],
        "created_utc": created, "updated_utc": created, "session_folder": session["folder"], "session_id": session["session_id"],
        "sample_id": session["sample_id"], "kind": spec.get("kind", session["kind"]), "area": spec.get("area", "simulated"),
        "transport": transport, "demo": demo, "script": script, "step": 0, "events": [], "checks": [],
        "files": spec.get("files", 21), "bytes": spec.get("bytes", 433104), "points": session["points"],
        "end": session.get("end", "finished"), "declared": bool(spec.get("declared")), "new_copy": bool(spec.get("new_copy")),
        "placed_as": spec.get("placed_as"), "payload_kept": spec.get("payload_kept", True),
        "sent_something": spec.get("sent_something", True), "stop_requested": False, "attempt": 1,
        "scan_mode": session.get("scan_mode", "1d-x"), "started_utc": session.get("started_utc"),
        "points_planned": session.get("points_planned", PLANNED_POINTS.get(session.get("scan_mode", "1d-x"))),
        # The VT username saved with a live SSH upload (settings_used): Retry and Check sign in with it.
        "ssh_username": spec.get("ssh_username", settings["arc_upload.ssh.username"]),
        # "worker": false names an upload in a working state whose program has not started yet (no worker.lock).
        "no_worker": spec.get("worker") is False,
        # A check of an upload that is not in a working state: {"lists": arc-lists before it ends, "ok", "message",
        # "looked", "fix"}. The control file may start an upload with one running ("check_worker": {"lists": 99}).
        "check_worker": spec.get("check_worker"),
        # Whether Globus may still run the upload's transfer (arc_records.globus_may_run); None: from its task status
        # (SUCCEEDED, FAILED, ACTIVE or INACTIVE), or without one from its state.
        "may_run": spec.get("may_run"),
        "task_status": spec.get("task_status"),
    }
    for check in spec.get("checks") or []:
        record["checks"].append(dict({"utc": created, "ok": True, "looked": True, "fix": None,
                                      "message": "Demo: still in the demo folder." if demo else "Still on ARC."}, **check))
    if arc_step(record)[0] == "verified":
        record["verified_utc"] = spec.get("verified_utc") or created
    arc["uploads"].append(record)
    arc_event(record, settings)
    return record


def arc_step(record):
    """The step the upload's script has reached: (stored state, display state, detail)."""
    name, _, detail = record["script"][min(record["step"], len(record["script"]) - 1)].partition(":")
    # A display state may name the stored state it stands for: "not_watched:verifying", "interrupted:submitting".
    if name == "not_watched":
        return detail or "transferring", name, ""
    if name == "not_started":
        return "queued", name, detail
    if name == "interrupted":
        return detail or "preparing", name, ""
    return name, name, detail


def arc_size(count):
    return "%d kB" % round(count / 1000.0)


def arc_item(record, settings, home):
    """An item of arc-list (section 8.4)."""
    state, shown, detail = arc_step(record)
    ssh, demo, folder = record["transport"] == "ssh", record["demo"], record["session_folder"]
    base = arc_base(settings, record["area"])
    final, incoming = base + "/" + folder, base + "/_incoming/" + folder + "." + record["upload_id"]
    root = settings["arc_upload.globus.collection_root"]
    demo_root = arc_demo_root(settings, home)
    # Where the person finds a folder: on ARC, or for a demo upload in the demo folder on this Mac (review COR-05).
    shown_base, shown_incoming = (demo_root + base, demo_root + incoming) if demo else (base, incoming)
    placed = (record["placed_as"] or (folder + "_copy2" if record["new_copy"] else folder)) if state == "verified" else None
    files, size = record["files"], record["bytes"]
    progress = {"files": files, "files_done": 0, "bytes": size, "bytes_done": 0}
    if state == "transferring" and detail.endswith("%"):
        progress["bytes_done"] = size * int(detail[:-1]) // 100
        progress["files_done"] = files * int(detail[:-1]) // 100
    elif state == "transferring" and "/" in detail:
        done, total = (int(part) for part in detail.split("/"))
        progress.update(files=total, files_done=done, bytes_done=size * done // max(1, total))
    elif state in ("verifying", "placing", "verified"):
        progress.update(files_done=files, bytes_done=size)
    identity = "demo (no account)" if demo else None if ssh else "stand-in Globus identity"
    error = None
    if state == "failed":
        message, fix, retryable = ARC_FAILURES[detail or "task_failed"]
        reason = detail or "task_failed"
        if demo and reason in ARC_FAILURES_DEMO:
            message, fix = ARC_FAILURES_DEMO[reason][0], ARC_FAILURES_DEMO[reason][1] or fix
        error = {"kind": "internal" if reason == "internal" else "upload", "reason": reason,
                 "message": message.format(incoming=shown_incoming), "fix": fix, "retryable": retryable}
    if shown == "not_watched":
        # arc_records.MESSAGES not_watched and not_watched_demo (review GLO-V03: keep the Mac awake).
        text = ("Globus keeps copying, but this Mac stopped watching. Press Check status to look." if demo else
                "Globus keeps copying, but this Mac stopped watching. Press Check status to look; it asks for a Globus sign-in. "
                "Keep this Mac awake until the copy is done.")
    elif shown == "not_started":
        # arc_records.MESSAGES not_started_ssh and not_started (review SSH-V02).
        text = ("The Terminal window did not start the upload. If it says \"Operation not permitted\", allow Terminal to use the "
                "Downloads folder: System Settings > Privacy & Security > Files and Folders. Then press Retry same request. If no "
                "Terminal window opens, double-click ssh_upload.command in the upload's folder." if ssh and not demo
                else "The upload program did not start. Press Retry same request.")
    elif shown == "interrupted":
        text = "The upload program stopped (for example when this Mac restarted). Retry same request continues where it stopped."
    elif state == "signing_in" and demo:
        text = ("no Terminal and no password. A real SSH upload asks for your VT password and Duo in a Terminal window here." if ssh
                else "no sign-in. A real upload opens your browser here.")
    elif state == "transferring" and ssh:
        # In bytes, also in the demo, as arc_ssh.COPYING says it; demo_text() writes its first word small.
        text = ("copying over SSH: %d%% (%.2f of %.2f MB)." if demo else "Copying over SSH: %d%% (%.2f of %.1f MB).") % (
            100 * progress["bytes_done"] // max(1, size), progress["bytes_done"] / 1e6, size / 1e6)
    elif state == "transferring":
        text = ("Copying with Globus: %d of %d files (%s of %s). Keep Globus Connect Personal running."
                % (progress["files_done"], progress["files"], arc_size(progress["bytes_done"]), arc_size(size)))
    elif state == "verified" and demo:
        text = "%d files in the demo folder on this Mac, every one checked. Nothing left this Mac." % files
    elif state == "verified":
        text = "On ARC: %d files in %s/%s. %s" % (files, base, placed, "ARC computed the SHA-256 of every file, and every one matches this "
                                                  "Mac's list." if ssh else "Globus checked every file, and its MD5 checksums match this Mac's copy.")
    elif state == "failed":
        text = error["message"]
    else:
        text = {"queued": "Waiting to start.", "waiting_for_terminal": "A Terminal window opens. Sign in there once: your VT password, then Duo.",
                "preparing": "Preparing the files: copying the session and its dark spectrum and computing a checksum for every file.",
                "signing_in": "Type your VT password, then approve Duo, in the Terminal window." if ssh
                else "A web page has opened for the Globus sign-in. Sign in there; nothing is sent before that.",
                "checking": "Connected. Sending %d files..." % files if ssh else "checking this Mac's files and the demo folder." if demo
                else "Signed in to Globus as %s. Checking this Mac's files and the ARC folder. If the browser asks to allow access to ARC, "
                "allow it." % identity,
                "submitting": "Handing the copy to Globus.",
                "verifying": "checking the copy in the demo folder." if demo else "Checking the copy on ARC.",
                "placing": "putting the copy in its folder in the demo folder." if demo else "Putting the copy in its folder on ARC.",
                "uncertain": "Not sure Globus received the request. Press Retry same request: Globus will not start a second transfer.",
                "conflict": ("the demo folder already has a folder named %s in %s. Nothing was sent." if demo else
                             "ARC already has a folder named %s in %s. Nothing was sent.") % (folder, shown_base) if detail == "checking"
                else ("your copy is complete and checked in %s. Keep it as a new copy, or move the other folder and press Retry same "
                      "request." if demo else "Your copy is complete and checked in %s on ARC. Keep it as a new copy, or move the other folder "
                      "on ARC and press Retry same request.") % shown_incoming,
                "stopped": ("stopped. Files already copied stay in %s; nothing was deleted." if demo else
                            "Stopped. Files already copied stay in %s on ARC; nothing was deleted.") % shown_incoming if record["sent_something"]
                else "Stopped before anything was sent."}.get(state, state)
    if demo and not text.startswith("Demo:"):
        text = "Demo: " + text
    # Fix plan I1: an upload on ARC whose newest check that looked failed is shown as check_failed, with that check's
    # words; a check that could not look ("looked": false) changes nothing (review REG-01).
    last = record["checks"][-1] if record["checks"] else None
    looks = [check for check in record["checks"] if check.get("looked") is not False]
    if state == "verified" and looks and looks[-1].get("ok") is False:
        shown, text = "check_failed", looks[-1]["message"]
    checking = record.get("check_worker") is not None
    alive = (state in ARC_WORKING and shown == state and not record.get("no_worker")) or checking
    if alive:
        # Fix plan I3: Stop is offered whenever a program works on an upload, a check of one on ARC included;
        # nothing else may start meanwhile, but its folder can still be looked at (arc_records.actions()).
        actions = ["stop"] + (["show_in_finder" if demo else "copy_path"] if state == "verified" else [])
    elif state in ARC_WORKING and shown == state:
        actions = ["stop"]
    elif shown == "not_watched":
        actions = ["check", "stop"]
    elif shown in ("interrupted", "not_started"):
        actions = ["retry", "stop"]
    elif state == "uncertain":
        actions = ["retry", "check", "stop"]
    elif state == "conflict":
        actions = ["keep_copy", "retry", "stop"]
    elif state in ("failed", "stopped"):
        actions = (["retry"] if record["payload_kept"] and (state == "stopped" or error["retryable"]) else []) + \
                  (["check"] if record["sent_something"] and not ssh else [])
    elif state == "verified":
        actions = ["check", "show_in_finder" if demo else "copy_path"]
    else:
        actions = []
    label = ARC_LABELS.get(state, state)
    if state == "transferring" and ssh:
        label = "Copying over SSH"
    # Review REG-03: Globus may still run this upload's transfer, which a stop, also of a running check, cancels. As
    # arc_records.globus_may_run has it (review R2-04): while the task status is ACTIVE or INACTIVE, whatever the stored
    # state (a failed or stopped upload's too); without one, while its request may have reached Globus (submitting,
    # uncertain) or its task runs (transferring). From verifying on, the task has ended: nothing runs on Globus.
    may_run = record.get("may_run")
    if may_run is None and record.get("task_status"):
        may_run = record["task_status"] in ("ACTIVE", "INACTIVE")
    elif may_run is None:
        may_run = state in ("uncertain", "submitting", "transferring")
    may_run = record["transport"] == "globus" and bool(may_run)
    # needs as arc_records.needs() has it: a decision, the browser of a Globus sign-in, or the Terminal window of
    # a live SSH upload that waits for it or signs in there.
    if state in ("conflict", "uncertain"):
        needs = "decision"
    elif demo:
        needs = None
    elif not ssh and alive and state == "signing_in":
        needs = "browser"
    elif ssh and ((state == "waiting_for_terminal" and shown == state) or (alive and state == "signing_in")):
        needs = "terminal"
    elif ssh and checking:
        # Check status of a live SSH upload runs in a Terminal window that waits for the password and Duo.
        needs = "terminal"
    else:
        needs = None
    username = record.get("ssh_username", settings["arc_upload.ssh.username"])
    return {
        "upload_id": record["upload_id"], "created_utc": record["created_utc"], "updated_utc": record["updated_utc"],
        "session_folder": folder, "session_id": record["session_id"], "sample_id": record["sample_id"], "kind": record["kind"],
        "area": record["area"], "transport": record["transport"], "demo": demo, "state": state, "state_label": label,
        "display_state": shown, "display_label": label if shown == state else ARC_LABELS[shown], "final": state in ("verified", "failed", "stopped"),
        "message": text, "progress": progress,
        "destination": {"final_path": final, "globus_final_path": None if ssh else arc_collection_path(final, root),
                        "incoming_path": incoming, "placed_as": placed,
                        # Only with the VT username saved with the upload, as arc_records.item() gives it.
                        "ssh_target": "%s@%s:%s" % (username, settings["arc_upload.ssh.host"], final) if ssh and not demo and username else None,
                        "local_final_path": demo_root + base + "/" + placed if demo and placed else None},
        "worker": {"alive": alive, "action": ("check" if checking else "send") if alive else None, "terminal": alive and ssh and not demo},
        "may_run": may_run,
        "identity": identity if state not in ("queued", "waiting_for_terminal", "preparing", "signing_in") else None,
        "needs": needs,
        "actions": actions, "error": error, "warnings": [], "notes": [],
        "verification": {"method": "sha256-on-arc" if ssh else "globus-checksum", "algorithm": "SHA-256" if ssh else "MD5", "files": files,
                         "utc": record.get("verified_utc") or record["updated_utc"]} if state == "verified" else None,
        "last_check": last,
        "leftover_on_arc": shown_incoming if state in ("failed", "stopped") and record["sent_something"] else None,
    }


def arc_event(record, settings, home=""):
    item = arc_item(record, settings, home)
    record["events"].append({"utc": record["updated_utc"], "state": item["state"], "text": item["message"]})


def arc_advance(record, settings):
    """One step of the script at every arc-list, or the stop that was asked for."""
    worker = record.get("check_worker")
    if worker is not None:
        # A check of an upload that is not in a working state: a stop ends it at once and it leaves the state
        # as it was (fix plan I3); else it ends after its lists, a verified upload gaining a "checks" entry.
        if record["stop_requested"]:
            record["check_worker"], record["stop_requested"] = None, False
        elif worker["lists"] > 0:
            worker["lists"] -= 1
            return
        else:
            record["check_worker"] = None
            looked = worker.get("looked", True) is not False
            fix = None if looked else worker.get("fix") or ARC_CHECK_AGAIN_FIX
            record["checks"].append({"utc": utc_now(), "ok": worker["ok"], "looked": looked, "message": worker["message"], "fix": fix})
            # A check that could not look says so in the history with what to do (review REG-01).
            record["events"].append({"utc": utc_now(), "state": arc_step(record)[0],
                                     "text": worker["message"] + ("" if looked else " " + fix)})
        record["updated_utc"] = utc_now()
        return
    if record["stop_requested"]:
        if arc_step(record)[0] not in ("transferring", "verifying", "placing"):
            # Stopped before the copy began: nothing of it is on ARC.
            record["sent_something"] = False
        record["script"], record["step"], record["stop_requested"] = ["stopped"], 0, False
    elif record.get("no_worker"):
        # Its program has not started (Terminal not used yet): nothing moves.
        return
    elif record["step"] < len(record["script"]) - 1:
        record["step"] += 1
    else:
        return
    record["updated_utc"] = utc_now()
    if arc_step(record)[0] == "verified" and not record.get("verified_utc"):
        record["verified_utc"] = record["updated_utc"]
    arc_event(record, settings)


def arc_ready(settings, control):
    problems = {"globus": [], "ssh": [], "demo": []}
    if not settings["arc_upload.globus.mac_collection"]:
        problems["globus"].append(arc_reason("mac_collection_missing"))
    if not settings["arc_upload.ssh.username"]:
        problems["ssh"].append(arc_reason("username_missing"))
    for transport, given in (control.get("ready") or {}).items():
        problems[transport] = [arc_reason(reason) if isinstance(reason, str) else reason for reason in given]
    return {transport: {"ok": not found, "problems": found} for transport, found in problems.items()}


def arc_record(arc, options):
    for record in arc["uploads"]:
        if record["upload_id"] == options.get("upload"):
            return record
    raise ArcFailure("unknown_upload", id=options.get("upload") or "")


def arc_upload(arc, settings, home, options, control):
    """arc-upload (section 8.3): the checks in their order, a dry run's plan, or a new upload."""
    folder, new, dry = options.get("session") or "", bool(options.get("new")), bool(options.get("dry-run"))
    demo = bool(options.get("demo")) or (not options.get("live") and settings["arc_upload.demo"])
    transport, declared = options.get("transport") or settings["arc_upload.transport"], bool(options.get("declare-real"))
    session = next((item for item in arc_sessions(arc, settings, home, control) if item["folder"] == folder), None)
    if session is None:
        raise ArcFailure("unknown_session", folder=folder)
    area, refusal = arc_area(session["kind"], not demo, settings, declared)
    told = control.get("upload") or ""
    reason = told.partition(":")[2] or told
    if told and not (new and reason in ("earlier_upload", "already_on_arc")) and not (dry and reason == "terminal_not_opened"):
        if reason in ("earlier_upload", "already_on_arc", "terminal_not_opened"):
            spec = {"session": folder, "transport": transport, "demo": demo, "area": area or "simulated", "kind": session["kind"],
                    "state": {"earlier_upload": "failed:task_failed", "already_on_arc": "verified"}.get(reason, "failed:terminal_not_opened")}
            record = arc_new_record(arc, settings, spec)
            item = arc_item(record, settings, home)
            raise ArcFailure(reason, upload=item, variant="demo" if demo and reason == "already_on_arc" else None, folder=folder,
                             label=item["display_label"], path=item["destination"]["final_path"] + "",
                             date=record["updated_utc"][:16].replace("T", " "), reason="the stand-in was told so")
        raise ArcFailure(reason, folder=folder, seconds=42)
    if session["why_not"]:
        raise ArcFailure(session["why_not"]["reason"], folder=folder, seconds=session["retry_after_seconds"])
    if declared and session["kind"] != "unknown":
        raise ArcFailure("declare_real_not_unknown")
    if refusal:
        raise ArcFailure(refusal)
    if not demo:
        problems = arc_ready(settings, control)[transport]["problems"]
        if problems:
            raise ArcFailure(problems[0]["reason"])
    same = sorted((record for record in arc["uploads"] if record["session_folder"] == folder and record["demo"] == demo
                   and record["area"] == area), key=lambda record: record["created_utc"])
    running = [record for record in same if arc_step(record)[0] in ARC_WORKING + ("uncertain", "conflict")]
    existing = arc_item(running[-1], settings, home) if running else None
    if running and new:
        raise ArcFailure("in_progress", upload=existing)
    last = arc_step(same[-1])[0] if same else None

    def on_arc(record):
        """Verified, and not found missing or changed by its newest check that looked (fix plan I4); a check that
        could not look changes nothing (review REG-01)."""
        looks = [check for check in record["checks"] if check.get("looked") is not False]
        return arc_step(record)[0] == "verified" and not (looks and looks[-1].get("ok") is False)

    if not running and last in ("failed", "stopped") and not new:
        item = arc_item(same[-1], settings, home)
        raise ArcFailure("earlier_upload", upload=item, label=item["display_label"])
    if not running and last == "verified" and on_arc(same[-1]) and not new:
        item = arc_item(same[-1], settings, home)
        # The date the copy was last found as it should be: the newest check when it found it, else its verification.
        last = same[-1]["checks"][-1] if same[-1]["checks"] else {}
        checked = last["utc"] if last.get("ok") is True else item["verification"]["utc"]
        # A demo upload is in the demo folder, not on ARC: the demo's wording (REFUSAL_VARIANTS).
        raise ArcFailure("already_on_arc", upload=item, variant="demo" if demo else None, folder=folder,
                         path=item["destination"]["local_final_path"] if demo else item["destination"]["final_path"],
                         date=checked[:16].replace("T", " ") + " UTC")
    new_copy = not running and any(on_arc(record) for record in same)
    cluster = arc_base(settings, area) + "/" + folder
    if dry:
        notes = []
        planned = session["points_planned"]
        if session["end"] == "stopped":
            notes.append("This scan was stopped early: %d of %d spectra." % (session["points"], planned))
        elif session["end"] == "failed":
            notes.append("This scan ended with an error after %d spectra." % session["points"])
        elif session["end"] == "ended":
            notes.append("This scan has no end mark: %d spectra." % session["points"])
        if area == "simulated":
            notes.append("This session was made in simulation. It goes into the separate folder simulated/." if session["kind"] == "simulated"
                         else "It is not recorded whether this session was made in simulation, so it goes into the separate folder simulated/.")
        if declared:
            notes.append("You said this session was measured with the real instruments; the upload records that.")
        if new_copy:
            notes.append("It goes beside the earlier copy as %s_copy2 (or the next free name)." % folder)
        warnings = ["The dark spectrum %s that this session names is not on this Mac, so it was not sent." % dark["file"]
                    for dark in session["darks"] if not dark["on_this_mac"]]
        plan = {"session": {"folder": folder, "session_id": session["session_id"], "scan_mode": session["scan_mode"],
                            "started_utc": session["started_utc"], "sample_id": session["sample_id"], "points": session["points"],
                            "points_planned": planned, "end": session["end"], "kind": session["kind"], "kind_declared_by_uploader": declared},
                "transport": transport, "demo": demo, "files": session["files"], "bytes": session["bytes"], "darks": session["darks"],
                "excluded": [], "warnings": warnings, "notes": notes, "area": area, "new_copy": new_copy,
                "destination": {"cluster_path": cluster, "globus_path": arc_collection_path(cluster, settings["arc_upload.globus.collection_root"]),
                                "demo_path": arc_demo_root(settings, home) + cluster if demo else None,
                                "ssh_target": "%s@%s:%s" % (settings["arc_upload.ssh.username"], settings["arc_upload.ssh.host"], cluster)
                                if transport == "ssh" and not demo else None}}
        return {"dry_run": True, "plan": plan, "existing": existing}
    if existing is not None:
        return {"upload": existing, "existing": True, "worker": None,
                "message": "An upload of this session is already under way (%s)." % existing["display_label"]}
    record = arc_new_record(arc, settings, {"session": folder, "transport": transport, "demo": demo, "area": area, "declared": declared,
                                            "new_copy": new_copy, "kind": session["kind"], "files": session["files"],
                                            "bytes": session["bytes"], "script": control.get("script")}, session)
    message = ("Demo upload started. Nothing leaves this Mac." if demo
               else "A Terminal window opens. Sign in there once: your VT password, then Duo." if transport == "ssh"
               else "A web page opens for you to sign in to Globus. Come back here when you have signed in.")
    return {"upload": arc_item(record, settings, home), "existing": False, "worker": "terminal" if transport == "ssh" and not demo
            else "detached", "message": message}


def arc_act(command, arc, settings, home, options, control):
    """arc-check, arc-retry, arc-stop and arc-keep-copy (section 8.6)."""
    record = arc_record(arc, options)
    item = arc_item(record, settings, home)
    wanted = {"arc-check": ("check", "not_checkable"), "arc-retry": ("retry", "not_retryable"), "arc-stop": ("stop", "nothing_to_stop"),
              "arc-keep-copy": ("keep_copy", "no_conflict")}[command]
    if item["worker"]["alive"] and wanted[0] != "stop":
        # A program in a Terminal window (a live SSH upload or its check): the busy wording that says so.
        raise ArcFailure("busy", upload=item, variant="terminal" if item["worker"]["terminal"] else None)
    if wanted[0] not in item["actions"]:
        raise ArcFailure(wanted[1], upload=item, label=item["display_label"])
    terminal = record["transport"] == "ssh" and not record["demo"]
    state = item["state"]
    answer = {"worker": "terminal" if terminal else "detached"}
    if command == "arc-stop" and item["worker"]["alive"]:
        record["stop_requested"] = True
        # arc_commands.STOP_MESSAGES requested, and requested_globus for a live Globus upload.
        message = ("The upload program stops the upload. If Globus may still be copying it, the transfer is cancelled first, after "
                   "the Globus sign-in if that page is still open." if record["transport"] == "globus" and not record["demo"]
                   else "The upload program stops the upload within a second.")
        return {"upload": item, "stopped_now": False, "worker": None, "message": message}
    if command == "arc-stop" and record["transport"] == "globus" and (item["display_state"] == "not_watched" or state == "uncertain"):
        # Globus may still run something: a stop worker signs in, settles the request and cancels the task;
        # the stand-in's upload is stopped at the next arc-list.
        record["script"], record["step"] = [record["script"][min(record["step"], len(record["script"]) - 1)], "stopped"], 0
        return {"upload": item, "stopped_now": False, "worker": "detached",
                "message": "Demo: stopping the upload. Nothing leaves this Mac." if record["demo"]
                else "A web page opens for you to sign in to Globus; then the transfer is cancelled."}
    if command == "arc-stop":
        if state == "conflict" and arc_step(record)[2] == "checking":
            record["sent_something"] = False
        record["script"], record["step"], record["updated_utc"] = ["stopped"], 0, utc_now()
        arc_event(record, settings)
        stopped = arc_item(record, settings, home)
        return {"upload": stopped, "stopped_now": True, "worker": None, "message": stopped["message"]}
    if command == "arc-check" and state == "verified":
        # A check program takes the upload; it ends at a later arc-list (the control file's "check" says after
        # how many lists, and what it finds), the state staying verified. A failed check shows check_failed.
        told = control.get("check") or {}
        looked = told.get("looked", True) is not False
        ok = told.get("ok", True) is not False and looked
        found = told.get("message") or (ARC_CHECK_TEXTS["not_looked"] if not looked
                                         else ARC_CHECK_TEXTS["ok_demo" if record["demo"] else "ok"].format(files=record["files"]) if ok
                                         else ARC_CHECK_TEXTS["differs_demo" if record["demo"] else "differs"])
        if record["demo"] and not found.startswith("Demo: "):
            found = "Demo: " + found[0].lower() + found[1:]
        record["check_worker"] = {"lists": int(told.get("lists", 0)), "ok": ok, "message": found, "looked": looked,
                                  "fix": told.get("fix") or (ARC_CHECK_TEXTS["not_looked_fix"] if not looked else None)}
        record["stop_requested"] = False
        record["updated_utc"] = utc_now()
        script = None
    elif command == "arc-check":
        script = ["checking", "transferring:15/21", "verifying", "placing", "verified"]
    elif command == "arc-keep-copy":
        record["new_copy"] = True
        script = ["placing", "verified"]
    elif state == "uncertain":
        script = ["submitting", "transferring:12/21", "verifying", "placing", "verified"]
    elif state == "conflict":
        script = ["placing", "verified"]
    else:
        script = control.get("retry_script") or (["waiting_for_terminal", "signing_in", "checking", "transferring:60%", "verifying",
                                                  "placing", "verified"] if terminal else ["queued", "preparing", "signing_in", "checking",
                                                  "submitting", "transferring:12/21", "verifying", "placing", "verified"])
    if script:
        record["script"], record["step"], record["updated_utc"] = script, 0, utc_now()
        record["attempt"] += command == "arc-retry"
        arc_event(record, settings)
    words = {"arc-check": "Checking the upload.", "arc-retry": "Sending the same request again.", "arc-keep-copy": "Putting the upload beside the other folder."}
    message = ("Demo: %s Nothing leaves this Mac." if record["demo"] else "%s A Terminal window opens: sign in there once." if terminal
               else "%s A web page opens for you to sign in to Globus.") % words[command]
    answer.update(upload=arc_item(record, settings, home), message=message)
    if command == "arc-retry" and state == "uncertain":
        answer["same_submission"] = True
    return answer


def arc_answer(command, options, files, control):
    """The answer of an arc-* command or of config; the state file keeps what they changed."""
    told = control.get("arc") or {}
    if told.get("absent") and command in ARC_COMMANDS:
        raise Failure("Invalid arguments: argument <command>: invalid choice: '%s'" % command,
                      "usage: python -m raman_catalog [-h] [--home HOME] [--verbose] <command> ...", kind="usage", code=2)
    state = files.load_state()
    arc = arc_state(state, told)
    try:
        return arc_command(command, arc, options, files.home, told)
    finally:
        # Also after a refusal: an earlier upload that a refusal names stays known.
        files.save_state(state)


def arc_command(command, arc, options, home, told):
    settings = arc_settings(arc)
    problems = arc_problems(settings, home)
    if problems and command not in ("config", "arc-doctor"):
        # As arc_commands._settings(): an invalid ARC value stops every arc-* command but arc-doctor.
        raise ArcFailure("settings_invalid", problems="; ".join(problems))
    if command == "config":
        if options["set"] and told.get("config_set") == "error":
            raise Failure("The ARC settings in catalog_config.json are not valid", told.get("config_detail", "arc_upload.folder: use a folder "
                          "under /projects with two folders below it, such as /projects/pcl_photonics/raman_globus_upload"), kind="config")
        changed = dict(arc["config"])
        for text in options["set"]:
            key, _, value = text.partition("=")
            key, value = key.strip(), value.strip()
            changed[key] = arc_value(key, value)
        # The whole group is judged, and nothing is saved when a value is not valid (commands_core.cmd_config).
        found = arc_problems(dict(ARC_DEFAULTS, **changed), home) if options["set"] else []
        if found:
            raise Failure("The ARC settings in catalog_config.json are not valid", "; ".join(found), kind="config")
        arc["config"] = changed
        config = json.loads(json.dumps(ARC_CORE_SETTINGS))
        for key, value in sorted(arc["config"].items()):
            group = config
            parts = key.split(".")
            for part in parts[:-1]:
                group = group.setdefault(part, {})
            group[parts[-1]] = value
        result = {"config": config, "path": os.path.join(home, "catalog_config.json")}
    elif command == "arc-sessions":
        items = arc_sessions(arc, settings, home, told)
        limit = int(options.get("limit") or 30)
        result = {"items": items[:limit], "total": len(items), "controller_idle_used": bool(options.get("controller-idle")),
                  "settings": {"demo": settings["arc_upload.demo"], "transport": settings["arc_upload.transport"],
                               "folder": settings["arc_upload.folder"],
                               "globus_folder": arc_collection_path(settings["arc_upload.folder"], settings["arc_upload.globus.collection_root"]),
                               "include_simulated": settings["arc_upload.include_simulated"],
                               "settle_seconds": settings["arc_upload.settle_seconds"]}}
    elif command == "arc-list":
        for record in arc["uploads"]:
            arc_advance(record, settings)
        records = sorted(arc["uploads"], key=lambda record: record["created_utc"], reverse=True)
        items = [arc_item(record, settings, home) for record in records]
        limit = int(options.get("limit") or 12)
        shown = items[:limit] + [item for item in items[limit:] if item["state"] in ARC_WORKING + ("uncertain", "conflict")]
        unfinished, attention = arc_counts(items)
        result = {"items": shown, "total": len(items), "active": sum(1 for item in items if item["worker"]["alive"]),
                  "unfinished": unfinished, "attention": attention,
                  "ready": arc_ready(settings, told), "settings": settings, "as_of_utc": utc_now()}
    elif command == "arc-status":
        record = arc_record(arc, options)
        result = {"upload": dict(arc_item(record, settings, home), events=record["events"][-30:],
                                 manifest={"files": record["files"], "bytes": record["bytes"],
                                           "darks": [{"file": "dark_20261113_101400_001.txt", "kind": "simulated", "sent": True, "reason": None,
                                                      "path": "dark/dark_20261113_101400_001.txt",
                                                      "description": "dark/dark_20261113_101400_001.json"}]},
                                 attempts=[], record_folder=os.path.join(home, "data_catalog", "arc_uploads", record["upload_id"]),
                                 session={"folder": record["session_folder"], "session_id": record["session_id"],
                                          "scan_mode": record.get("scan_mode", "1d-x"), "started_utc": record.get("started_utc"),
                                          "sample_id": record["sample_id"], "points": record["points"],
                                          "points_planned": record.get("points_planned"), "end": record["end"], "kind": record["kind"],
                                          "kind_declared_by_uploader": record["declared"]})}
    elif command == "arc-upload":
        result = arc_upload(arc, settings, home, options, told)
    elif command == "arc-doctor":
        ready = arc_ready(settings, told)
        mac, user = settings["arc_upload.globus.mac_collection"], settings["arc_upload.ssh.username"]
        registered = settings["arc_upload.globus.client_id"] != ARC_TUTORIAL_CLIENT
        checks = [
            {"name": "settings_valid", "ok": not problems,
             "detail": "; ".join(problems) + "." if problems else "The ARC settings are valid.",
             "fix": "Correct them in ARC settings (command line: config --set key=value)." if problems else ""},
            {"name": "macos", "ok": True, "detail": "This is macOS.", "fix": ""},
            {"name": "globus_sdk_installed", "ok": True, "detail": "The Globus SDK (globus-sdk) is installed.", "fix": ""},
            {"name": "mac_collection_set", "ok": bool(mac), "detail": "This Mac's Globus collection ID is " + ("set: %s." % mac if mac else "not set."),
             "fix": "" if mac else ARC_REFUSALS["mac_collection_missing"][2]},
            {"name": "globus_connect_personal_installed", "ok": True, "detail": "Globus Connect Personal is installed (stand-in).", "fix": ""},
            {"name": "globus_connect_personal_running", "ok": True, "detail": "Globus Connect Personal is running (stand-in).", "fix": ""},
            {"name": "client_id_is_registered", "ok": registered,
             "detail": "Sign-in uses the Globus app %s." % settings["arc_upload.globus.client_id"] if registered
             else "Sign-in uses the Globus SDK's public example app.",
             "fix": "" if registered else "For regular use, register an app for the team and set arc_upload.globus.client_id."},
            {"name": "ssh_username_set", "ok": bool(user), "detail": "The VT username is set: %s." % user if user else "Your VT username is not set.",
             "fix": "" if user else ARC_REFUSALS["username_missing"][2]},
            {"name": "ssh_program", "ok": True, "detail": "/usr/bin/ssh is there.", "fix": ""},
            {"name": "terminal_app", "ok": True, "detail": "Terminal is there.", "fix": ""},
            {"name": "open_program", "ok": True, "detail": "/usr/bin/open is there.", "fix": ""},
            {"name": "known_hosts_file", "ok": True, "detail": "ssh uses your own ~/.ssh/known_hosts and asks you to confirm ARC's key the "
             "first time.", "fix": ""},
            {"name": "demo_folder_writable", "ok": True, "detail": "The demo folder %s can be written." % arc_demo_root(settings, home), "fix": ""},
            {"name": "remote_script_present", "ok": True, "detail": "The ARC side of SSH uploads (arc_remote.sh) is there.", "fix": ""},
        ]
        result = {"checks": checks, "ready": {transport: found["ok"] for transport, found in ready.items()}, "settings": settings}
    else:
        result = arc_act(command, arc, settings, home, options, told)
    return result


def answer(command, options, files, control):
    if command in ARC_COMMANDS or command == "config":
        return arc_answer(command, options, files, control)
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
        if options.get("on") or options.get("off"):
            state["offline"] = bool(options.get("on"))
        result = {"offline": state["offline"]}
    else:
        raise Failure("Invalid arguments: argument <command>: invalid choice: '%s'" % command,
                      "usage: python -m raman_catalog [-h] [--home HOME] [--verbose] <command> ...", kind="usage", code=2)
    files.save_state(state)
    return result


def main(argv):
    home, command, options = parse(argv)
    if command in ARC_COMMANDS or command == "config":
        options = arc_options(argv)
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
        # The numbers of uploads to ARC that are not finished and that need the person, which every status
        # gains (also the one a sync brings): counted from the uploads, or given by a test.
        if command == "status":
            payload.update(arc_status_counts(files, control))
        elif isinstance(payload.get("status"), dict):
            payload["status"].update(arc_status_counts(files, control))
    except Failure as failure:
        payload, code = {"ok": False, "error": str(failure), "detail": failure.detail, "kind": failure.kind}, failure.code
        payload.update(getattr(failure, "extra", {}))
    sys.stdout.write(json.dumps(payload) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
