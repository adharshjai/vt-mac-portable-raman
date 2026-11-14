"""The review commands: review-list, review-show, review-set (PROTOCOL.md section 6).

A person checks the predictions the classifier was not sure of. A review is merged into the
prediction record; the model's own label, scores and outcome are never changed. While the
catalog cannot be reached, the spectrum is still shown from the files on this Mac and the review
is kept in the ledger, from where the next sync sends it.
"""

from __future__ import annotations

import functools
import json
import logging
import shutil
import tempfile
from pathlib import Path

from . import classifier, commands_core, sync
from .catalog import PREDICTION_TYPE, CatalogError, CatalogNotFound, CatalogOffline
from .config import CommandError, utc_now
from .ledger import STATE_LABELS, item_from_row, review_state
from .metadata import dark_reference, sha256_file

log = logging.getLogger(__name__)

AXIS_FILE = Path(__file__).with_name("raman_shift.json")
# decision on the command line -> review.status in the record
DECISIONS = {"confirm": "confirmed", "relabel": "relabelled", "reject": "rejected"}
# What a reviewer chooses when the spectrum shows none of the model's classes.
UNKNOWN_CHOICE = "unknown"


@functools.lru_cache(maxsize=1)
def raman_shift_axis():
    """The Raman shift in 1/cm of each of the 2048 detector pixels.

    The values are the controller's calibration table (kRamanShift in laser_test/main.cpp),
    copied once into raman_shift.json; the tests compare the two.
    """
    values = json.loads(AXIS_FILE.read_text(encoding="utf-8"))["raman_shift"]
    if len(values) != classifier.PIXELS:
        raise CommandError("%s does not hold %d values" % (AXIS_FILE.name, classifier.PIXELS))
    return tuple(values)


# ---------------------------------------------------------------------- finding a prediction


def _ledger_is_current(ctx):
    """False between a change of catalog in the settings and the sync that follows it: until
    then the ledger rows, and the record ids in them, belong to the previous catalog."""
    return ctx.ledger.catalog_key in (None, ctx.config.catalog_key)


def _require_current_ledger(ctx):
    if not _ledger_is_current(ctx):
        raise CommandError(
            "The catalog settings changed since the last sync",
            detail="Run sync first: what this Mac knows about predictions still belongs to the previous catalog.",
        )


def _find(ctx, prediction):
    """(prediction record, ledger row) for a record id or alias; either can be None.

    The record is None when the catalog cannot be reached and the ledger knows the prediction;
    the row is None when the prediction is not of a capture of this HOME.
    """
    try:
        record = ctx.catalog.get_record(prediction)
    except CatalogOffline:
        row = ctx.ledger.find_by_prediction(prediction)
        if row is None:
            # Nothing is known here about it, so there is nothing to show or to queue a review for.
            raise
        return None, row
    if record.metadata.get("type") != PREDICTION_TYPE:
        raise CommandError("%s is not a prediction record" % prediction)
    row = ctx.ledger.find_by_prediction(record.id)
    if row is not None and ctx.ledger.set_prediction(row["capture_id"], record.id, record.metadata):
        # The record was reviewed from somewhere else since the last sync.
        row = ctx.ledger.find_by_prediction(record.id)
    return record, row


def _raw_record(ctx, prediction):
    """The raw spectrum record a prediction names, or None when it cannot be had now."""
    raw_id = prediction.metadata.get("raw_record")
    if not raw_id:
        return None
    try:
        return ctx.catalog.get_record(raw_id)
    except CatalogError:
        return None


# ---------------------------------------------------------------------- items


def _item_from_catalog(prediction, raw):
    """The `list` item for a prediction whose capture is not in this HOME's ledger."""
    saved = prediction.metadata
    source = raw.metadata if raw is not None else {}
    summary = {"record_id": prediction.id, "label": saved.get("label"), "top_class": saved.get("top_class"),
               "confidence": saved.get("confidence"), "outcome": saved.get("outcome"),
               "needs_review": bool(saved.get("needs_review"))}
    review = saved.get("review") if isinstance(saved.get("review"), dict) else None
    state = review_state(summary, review)
    if review is not None:
        review = {key: review.get(key) for key in ("status", "label", "reviewer", "time_utc")}
    return {
        "capture_id": saved.get("capture_id"),
        "session_id": source.get("session_id"),
        "session_folder": source.get("session_folder"),
        "point_id": saved.get("point_id"),
        "point_file": (source.get("raw_file") or {}).get("name"),
        "scan_mode": source.get("scan_mode"),
        "sample_id": saved.get("sample_id"),
        "operator": source.get("operator"),
        "timestamp_utc": source.get("timestamp_utc"),
        "simulated": saved.get("simulated"),
        "position_mm": source.get("position_mm"),
        "state": state,
        "state_label": STATE_LABELS[state],
        "record_id": raw.id if raw is not None else saved.get("raw_record"),
        "record_alias": raw.alias if raw is not None else None,
        "upload": None,
        "prediction": summary,
        "review": review,
        "error": None,
    }


def review_item(row, prediction, raw=None):
    """A `review-list` item: the `list` item plus what a reviewer needs from the prediction.

    `prediction` None (catalog unreachable) leaves scores, reason, threshold and model null.
    """
    item = item_from_row(row) if row is not None else _item_from_catalog(prediction, raw)
    saved = prediction.metadata if prediction is not None else {}
    model = saved.get("model") if isinstance(saved.get("model"), dict) else None
    item["prediction_id"] = prediction.id if prediction is not None else row["prediction_id"]
    item["scores"] = saved.get("scores")
    item["reason"] = saved.get("reason")
    item["threshold"] = saved.get("threshold")
    item["model"] = None if model is None else {key: model.get(key) for key in ("name", "sha256", "record")}
    return item


# ---------------------------------------------------------------------- review-list


def cmd_review_list(args, ctx):
    """The predictions of this HOME's captures that wait for a person."""
    # "online" stays null when the catalog did not have to be asked.
    online = None
    items = []
    if not _ledger_is_current(ctx):
        return {"items": items, "online": online}
    for row in ctx.ledger.rows(states=["needs_review"]):
        prediction = None
        if online is not False:
            try:
                prediction = ctx.catalog.get_record(row["prediction_id"])
                online = True
            except CatalogOffline:
                # The ledger still knows what waits; the details come back with the connection.
                online = False
            except CatalogNotFound:
                log.warning("the prediction %s of %s is no longer in the catalog", row["prediction_id"],
                            row["capture_id"])
        if prediction is not None and ctx.ledger.set_prediction(row["capture_id"], prediction.id, prediction.metadata):
            # Reviewed from somewhere else since the last sync: the row is up to date again.
            row = ctx.ledger.get(row["capture_id"])
            if row["state"] != "needs_review":
                continue
        items.append(review_item(row, prediction))
    return {"items": items, "online": online}


# ---------------------------------------------------------------------- review-show


def _local_point(ctx, row):
    """The point file of a ledger row, when it is (still) on this Mac."""
    if row is None:
        return None
    path = ctx.paths.scan_sessions / row["session_folder"] / row["point_file"]
    try:
        point = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return point if isinstance(point, dict) and point.get("capture_id") == row["capture_id"] else None


def _fetched(ctx, record_id, read):
    """`read(path)` of a record's file, downloaded for the moment it takes to read it."""
    ctx.paths.staging.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="review-", dir=str(ctx.paths.staging)))
    try:
        return read(ctx.catalog.get_data(record_id, folder))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _dark_values(ctx, point, saved, online, problems):
    """The dark spectrum the classifier used: from this Mac when the same file is here, else
    from the catalog. None when it used none or it cannot be had.

    Online the prediction says which dark it used. Offline only the capture is known here: its
    dark is shown when the pairing rule of registration lets the spectrum use it.
    """
    capture = point.get("capture") if isinstance(point.get("capture"), dict) else {}
    name = capture.get("dark_file")
    if online:
        used = saved.get("dark") if isinstance(saved.get("dark"), dict) else None
        if used is None:
            if name:
                problems.append("The classifier used no dark spectrum for this prediction, so none is shown.")
            return None
    else:
        simulated = capture.get("simulated")
        reference = dark_reference(ctx.home, name, simulated.get("backend") if isinstance(simulated, dict) else None)
        if reference is not None and reference.get("reason"):
            reason = reference["reason"]
            problems.append("%s%s (%s)." % (reason[0].upper(), reason[1:], name))
            return None
        used = {}
    try:
        if name:
            path = ctx.paths.dark_measurements / name
            # Online, only the very file the classifier used; another dark may have the same name.
            if path.is_file() and (not online or used.get("sha256") == sha256_file(path)[0]):
                return classifier.load_dark(path)
        if online and used.get("record"):
            return _fetched(ctx, used["record"], classifier.load_dark)
    except ValueError as error:
        problems.append("The dark spectrum cannot be used: %s." % error)
        return None
    except CatalogError as error:
        problems.append("The dark spectrum could not be fetched from the catalog: %s." % error)
        return None
    if name or used.get("record"):
        problems.append("The dark spectrum the classifier used (%s) is not on this Mac%s." % (
            name or used.get("record"), "" if online else " and the catalog cannot be reached"))
    return None


def _whole_or_float(values):
    return [int(value) if float(value).is_integer() else float(value) for value in values]


def _spectrum(ctx, row, prediction, problems):
    """Raw intensity, dark, the curve the classifier saw and the Raman-shift axis, index-aligned."""
    online = prediction is not None
    saved = prediction.metadata if online else {}
    point, source = _local_point(ctx, row), "local file"
    if point is None and online and saved.get("raw_record"):
        try:
            point = _fetched(ctx, saved["raw_record"], lambda path: json.loads(path.read_text(encoding="utf-8-sig")))
            source = "catalog"
        except (CatalogError, ValueError) as error:
            problems.append("The raw spectrum could not be fetched from the catalog: %s." % error)
    if not isinstance(point, dict) or not isinstance(point.get("intensity"), list):
        if not problems:
            problems.append("The raw spectrum is not on this Mac%s." % (
                "" if online else " and the catalog cannot be reached"))
        return None
    try:
        dark = _dark_values(ctx, point, saved, online, problems)
        processed = None
        if dark is not None:
            try:
                features = classifier.preprocess(point["intensity"], dark)[0]
            except ValueError:
                # The classifier rejected this spectrum before it had a curve; the reason is in the item.
                features = None
            if features is not None:
                # The model does not see the first pixels; null keeps the curve aligned with the axis.
                processed = [None] * classifier.DROPPED_PIXELS + [round(float(value), 6) for value in features]
    except ImportError as error:
        raise CommandError("Showing a spectrum needs numpy in this interpreter",
                           detail="%s. Create the data layer's environment with data_layer/setup_env.sh." % error) \
            from error
    return {
        "raman_shift": list(raman_shift_axis()),
        "intensity": point["intensity"],
        "dark": None if dark is None else _whole_or_float(dark),
        "processed": processed,
        "processed_start": classifier.DROPPED_PIXELS,
        "source": source,
    }


def _sources(ctx, prediction):
    """What the prediction was derived from, as a flat list for display."""
    chain = commands_core.provenance(ctx.catalog, prediction.id)
    nodes = {node["id"]: node for node in chain["nodes"]}
    return [dict(nodes[edge["to"]], relation=edge["relation"], label=edge["label"])
            for edge in chain["edges"] if edge["from"] == prediction.id and edge["to"] in nodes]


def model_classes(ctx, prediction):
    """The class names of the model that made a prediction; the settings' model when the catalog
    cannot say."""
    saved = prediction.metadata if prediction is not None else {}
    model_id = (saved.get("model") or {}).get("record") if isinstance(saved.get("model"), dict) else None
    candidates = []
    if model_id:
        try:
            candidates.append((ctx.catalog.get_record(model_id).metadata.get("meta") or {}).get("classes"))
        except CatalogError:
            # Unreachable or gone: the model folder of the settings is asked instead.
            pass
    try:
        candidates.append(classifier.read_meta(ctx.config.model_dir)["classes"])
    except classifier.ModelError:
        pass
    if isinstance(saved.get("scores"), dict):
        candidates.append(list(saved["scores"]))
    for classes in candidates:
        if isinstance(classes, list) and classes and all(isinstance(name, str) for name in classes):
            return list(classes)
    return []


def cmd_review_show(args, ctx):
    _require_current_ledger(ctx)
    prediction, row = _find(ctx, args.prediction)
    problems = []
    raw = _raw_record(ctx, prediction) if prediction is not None and row is None else None
    result = {
        "item": review_item(row, prediction, raw),
        "spectrum": _spectrum(ctx, row, prediction, problems),
        "provenance": [],
        "classes": model_classes(ctx, prediction),
        "online": prediction is not None,
    }
    if prediction is not None:
        try:
            result["provenance"] = _sources(ctx, prediction)
        except CatalogError as error:
            problems.append("The provenance could not be read: %s." % error)
    else:
        problems.append("The catalog cannot be reached: scores and provenance are shown once it answers again.")
    result["problems"] = problems
    return result


# ---------------------------------------------------------------------- review-set


def _reviewed_label(decision, given, summary, classes):
    """The label a review stores: the reviewer's choice, the model's answer, or none."""
    if decision == "relabel":
        if not given:
            raise CommandError("relabel needs --label", detail="One of: %s." % ", ".join(classes + [UNKNOWN_CHOICE]))
        if given.lower() == UNKNOWN_CHOICE:
            return classifier.LABEL_UNKNOWN
        if given in classes:
            return given
        if not classes:
            raise CommandError("The classes of the model are not known here, so the label cannot be checked",
                               detail="The model record cannot be read and classifier.model_dir has no meta.json.")
        raise CommandError("%s is not a class of the model" % given,
                           detail="One of: %s." % ", ".join(classes + [UNKNOWN_CHOICE]))
    if given:
        raise CommandError("--label goes with --decision relabel only")
    if decision == "confirm":
        # Confirmed is the model's best answer, which for a low-confidence prediction is the
        # top class and not the label UNKNOWN it was given.
        return summary.get("top_class") or summary.get("label")
    return None


def _waiting_to_be_sent(ctx, prediction_id):
    """The reviews of a prediction that were recorded offline and not sent yet."""
    # "" is the key of reviews queued before this ledger's first sync bound it to a catalog.
    return [entry for key in (ctx.config.catalog_key, "") for entry in ctx.ledger.queued_reviews(key)
            if entry["prediction_id"] == prediction_id]


def cmd_review_set(args, ctx):
    _require_current_ledger(ctx)
    decision = args.decision
    if decision not in DECISIONS:
        raise CommandError("The decision is confirm, relabel or reject")
    reviewer = " ".join((getattr(args, "reviewer", None) or "").split())
    if not reviewer:
        raise CommandError("A reviewer name is required", detail="It is stored with the review: --reviewer NAME")
    given = (getattr(args, "label", None) or "").strip()
    prediction, row = _find(ctx, args.prediction)
    summary = prediction.metadata if prediction is not None else (item_from_row(row)["prediction"] or {})
    review = {
        "status": DECISIONS[decision],
        "label": _reviewed_label(decision, given, summary, model_classes(ctx, prediction) if decision == "relabel"
                                 else []),
        "reviewer": reviewer,
        "time_utc": None,
        "comment": (getattr(args, "comment", None) or "").strip(),
    }
    prediction_id = prediction.id if prediction is not None else row["prediction_id"]
    # A sync sending a review queued offline holds this lock while it writes; so does another
    # review-set. Timed inside it, reviews reach the catalog in the order of their times, and the
    # later one stays (sync.apply_review).
    with ctx.ledger.review_lock():
        review["time_utc"] = utc_now()
        if prediction is not None:
            if _waiting_to_be_sent(ctx, prediction_id):
                # An earlier review of this prediction still waits for a sync. This one takes its
                # place in the queue first, so that the older one can never be sent after it.
                ctx.ledger.queue_review(prediction_id, review)
            try:
                prediction = sync.apply_review(ctx.catalog, ctx.config, prediction_id, review)
            except CatalogOffline:
                # The connection went away after the record was read: keep the review like any other
                # made offline. Sending it again later is safe.
                prediction = None
        queued = prediction is None
        if queued:
            ctx.ledger.queue_review(prediction_id, review)
        else:
            for entry in _waiting_to_be_sent(ctx, prediction_id):
                ctx.ledger.review_sent(entry["id"])
            # What the catalog holds: this review, or one made after it somewhere else.
            ctx.ledger.record_review(prediction_id, prediction.metadata.get("review") or review)
    row = ctx.ledger.find_by_prediction(prediction_id)
    raw = _raw_record(ctx, prediction) if prediction is not None and row is None else None
    item = review_item(row, prediction, raw) if row is not None or prediction is not None else None
    return {"item": item, "queued": queued}


def register(subparsers):
    command = subparsers.add_parser("review-list", help="predictions waiting for a person")
    command.set_defaults(func=cmd_review_list)

    command = subparsers.add_parser(
        "review-show", help="one prediction with its spectrum, dark spectrum, the curve the classifier saw, "
        "and what it was derived from")
    command.add_argument("--prediction", required=True, metavar="ID", help="prediction record id or alias")
    command.set_defaults(func=cmd_review_show)

    command = subparsers.add_parser("review-set", help="record a person's review of a prediction")
    command.add_argument("--prediction", required=True, metavar="ID", help="prediction record id or alias")
    command.add_argument("--decision", required=True, choices=tuple(DECISIONS),
                         help="confirm the model's best answer, relabel, or reject the spectrum")
    command.add_argument("--label", metavar="CLASS",
                         help="with relabel: one of the model's classes, or %s" % UNKNOWN_CHOICE)
    command.add_argument("--reviewer", required=True, metavar="NAME", help="who reviewed; stored with the review")
    command.add_argument("--comment", metavar="TEXT")
    command.set_defaults(func=cmd_review_set)
