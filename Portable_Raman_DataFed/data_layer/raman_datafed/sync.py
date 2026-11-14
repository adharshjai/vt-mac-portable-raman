"""One synchronisation pass between the scan folders on disk, the local ledger and the catalog.

A pass never waits: it finds new points, sends what can be sent, starts uploads, asks how the
uploads started earlier are doing, and looks for predictions. Whatever is not finished is
picked up by the next pass. Every step can be repeated safely, so a pass that dies half-way,
or runs while the catalog is unreachable, loses nothing.
"""

from __future__ import annotations

import datetime
import importlib
import json
import logging

from .catalog import (
    PREDICTION_TYPE,
    UNAVAILABLE_KEY,
    CatalogAuthRequired,
    CatalogError,
    CatalogNotFound,
    CatalogOffline,
    dark_alias,
    ensure_layout,
    fit_title,
    layout_for,
    raw_alias,
    session_alias,
    simulated_mark,
)
from .config import Paths, seconds_since, utc_now
from .ledger import AWAITING_PREDICTION_STATES, REVIEW_DONE_STATUSES
from .metadata import (
    MetadataError,
    build_dark_metadata,
    build_raw_metadata,
    is_session_folder,
    point_files,
    read_point,
    read_session,
    sha256_file,
)

log = logging.getLogger(__name__)

JOB_MODULE = "raman_datafed.job"
LAST_SYNC_KEY = "last_sync_utc"
LAST_PROBLEMS_KEY = "last_sync_problems"
LAST_FULL_CHECK_KEY = "last_full_prediction_check_utc"
# A spectrum still in the to-classify queue has no prediction, so a sync does not ask the catalog
# about it. Should a classifier ever leave spectra in the queue after classifying them, they are
# still found: this often, every uploaded capture is looked at regardless of the queue.
FULL_PREDICTION_CHECK_SECONDS = 900


class _ItemError(Exception):
    """One capture or dark spectrum cannot go further in this pass; the others are not affected."""


class _FileLost(_ItemError):
    """The file a record describes is gone from this Mac or was replaced: retrying cannot upload
    it unless somebody puts the file back."""


def problem(capture_id, stage, message):
    """One entry of an "errors" list in a command's output."""
    return {"capture_id": capture_id, "stage": stage, "message": str(message)}


def _stops_the_pass(error):
    """Offline and missing credentials concern every item, so they end the network part instead."""
    return isinstance(error, (CatalogOffline, CatalogAuthRequired))


def _optional_module(name):
    """The module, or None only when that exact module is not part of this installation."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        return None


# ---------------------------------------------------------------------- step 1: find new points


def _ledger_entry(folder, path, point):
    return {
        "capture_id": point["capture_id"],
        "session_id": point["session_id"],
        "session_folder": folder.name,
        "point_file": path.name,
        "point_id": point["point_id"],
        "scan_mode": point.get("scan_mode"),
        "timestamp_utc": point.get("completed_utc"),
    }


def _describe(entry, metadata):
    """Copy what the list shows from the record metadata into a ledger entry."""
    position = metadata["position_mm"] or {}
    entry.update(
        sample_id=metadata["sample_id"],
        operator=metadata["operator"],
        location=metadata["location"],
        simulated=None if metadata["simulated"] is None else int(metadata["simulated"]),
        x_mm=position.get("x"),
        y_mm=position.get("y"),
        z_mm=position.get("z"),
    )


def ingest_folder(paths, ledger, folder, known=None):
    """Put the points of one session folder into the ledger.

    Returns {"ingested": points that are now `captured`, "skipped": points the ledger already
    had, "errors": [...]}. A point whose file cannot be registered as it is gets a row in
    error/metadata, so that it shows in the list until its fields are supplied.
    """
    report = {"ingested": 0, "skipped": 0, "errors": []}
    known = ledger.known_files() if known is None else known
    try:
        session = read_session(folder)
    except MetadataError as error:
        report["errors"].append(problem(None, "metadata", "%s: %s" % (folder.name, error)))
        return report
    for path in point_files(folder):
        if (folder.name, path.name) in known:
            report["skipped"] += 1
            continue
        try:
            point = read_point(path)
        except MetadataError as error:
            report["errors"].append(problem(None, "metadata", "%s: %s" % (folder.name, error)))
            continue
        capture_id = point["capture_id"]
        entry = _ledger_entry(folder, path, point)
        message = None
        if point["session_id"] != session["session_id"]:
            message = "%s belongs to another session than the session.json of its folder" % path.name
        else:
            try:
                _describe(entry, build_raw_metadata(paths.home, folder.name, point, path))
            except MetadataError as error:
                message = str(error)
        outcome = ledger.add_capture(entry, error_message=message)
        if outcome == "duplicate":
            other = ledger.get(capture_id)
            if (paths.scan_sessions / other["session_folder"] / other["point_file"]).is_file():
                report["errors"].append(problem(
                    capture_id, "metadata", "%s/%s repeats the capture of %s/%s and is ignored"
                    % (folder.name, path.name, other["session_folder"], other["point_file"])))
                continue
            # The file the row pointed at is gone and the same capture is here: the folder was renamed.
            ledger.relocate(capture_id, folder.name, path.name)
            outcome = ledger.add_capture(entry, error_message=message)
        if outcome == "unchanged":
            report["skipped"] += 1
        elif message is None:
            report["ingested"] += 1
        else:
            report["errors"].append(problem(capture_id, "metadata", message))
    return report


def discover(paths, ledger):
    """Step 1: every session folder under HOME/scan_sessions that has a session.json."""
    report = {"ingested": 0, "skipped": 0, "errors": []}
    if not paths.scan_sessions.is_dir():
        return report
    known = ledger.known_files()
    for folder in sorted(paths.scan_sessions.iterdir()):
        if not is_session_folder(folder) or not (folder / "session.json").is_file():
            continue
        found = ingest_folder(paths, ledger, folder, known)
        for key in ("ingested", "skipped"):
            report[key] += found[key]
        report["errors"].extend(found["errors"])
    return report


# ---------------------------------------------------------------------- reviews


def _review_time(review):
    """When a review was made, or None when it does not say in the form utc_now writes."""
    text = review.get("time_utc") if isinstance(review, dict) else None
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ")
    except (TypeError, ValueError):
        return None


def _later_review(existing, review):
    """True when the prediction already carries a review made after `review`."""
    if not isinstance(existing, dict) or existing.get("status") not in REVIEW_DONE_STATUSES:
        return False
    before, after = _review_time(review), _review_time(existing)
    return before is not None and after is not None and after > before


def apply_review(catalog, config, prediction_id, review):
    """Record a person's review on a prediction record and take it out of the review queue.

    Only `review` and `needs_review` change; the model's own label, scores and outcome stay.
    Safe to repeat. A review made after this one, which the record already carries, is kept: a
    review queued offline can reach the catalog after a newer one made somewhere else. Returns
    the record as it is now. Raises CatalogOffline when the catalog is unreachable (the caller
    then queues the review in the ledger and the next sync sends it).
    """
    return _apply_review(catalog, config, prediction_id, review)[0]


def _apply_review(catalog, config, prediction_id, review):
    """apply_review, and whether the review was written (False: a later one stays)."""
    if not isinstance(review, dict) or review.get("status") not in REVIEW_DONE_STATUSES:
        raise ValueError("a review needs a status of %s" % ", ".join(REVIEW_DONE_STATUSES))
    current = catalog.get_record(prediction_id)
    if current.metadata.get("type") != PREDICTION_TYPE:
        raise CatalogError("%s is not a prediction record, so it cannot be reviewed" % prediction_id)
    if _later_review(current.metadata.get("review"), review):
        log.info("%s carries a review made after this one, which stays", current.id)
        return current, False
    record = catalog.update_record(current.id, metadata={"review": review, "needs_review": False})
    try:
        catalog.unlink(layout_for(config.root_alias).needs_review, record.id)
    except CatalogNotFound:
        # No review queue collection exists, so the record is not linked in one.
        pass
    return record, True


def send_queued_reviews(catalog, config, ledger):
    """Step 2: send the reviews that were recorded while the catalog was unreachable.

    Each one is sent under the review lock and read again first: a review-set since this list
    was read has replaced or sent it, and then there is nothing left to send. Returns (number
    written to the catalog, errors).
    """
    sent, errors = 0, []
    for listed in ledger.queued_reviews(config.catalog_key, config.max_attempts):
        with ledger.review_lock():
            entry = ledger.queued_review(listed["id"])
            if entry is None:
                continue
            try:
                record, written = _apply_review(catalog, config, entry["prediction_id"], entry["review"])
            except CatalogError as error:
                if _stops_the_pass(error):
                    raise
                ledger.review_failed(entry["id"], str(error))
                row = ledger.find_by_prediction(entry["prediction_id"])
                errors.append(problem(row["capture_id"] if row else None, "review",
                                      "review of %s: %s" % (entry["prediction_id"], error)))
                continue
            ledger.review_sent(entry["id"])
            # The row shows what the catalog holds now, which is a later review when there was one.
            row = ledger.find_by_prediction(record.id)
            if row is not None:
                ledger.set_prediction(row["capture_id"], record.id, record.metadata)
            sent += int(written)
    return sent, errors


# ---------------------------------------------------------------------- step 4: predictions


def _newest_prediction(catalog, record_id):
    """The prediction record derived from a raw record; the most recent one when the spectrum
    was classified more than once (a newer model)."""
    raw = catalog.get_record(record_id)
    candidates = []
    for dep in raw.deps:
        if dep.direction == "in" and dep.relation == "der":
            record = catalog.get_record(dep.id)
            if record.metadata.get("type") == PREDICTION_TYPE:
                candidates.append(record)
    if not candidates:
        return None
    return max(candidates, key=lambda record: (record.metadata.get("job") or {}).get("finished_utc") or "")


def _items(catalog, collection):
    try:
        return {item.id for item in catalog.list_collection(collection)}
    except CatalogNotFound:
        return set()


def still_in_queues(catalog, config, ledger):
    """(ids of the records in the to-classify queue, ids of those in the review queue), or None
    when it is time to look at every capture whatever the queues say.

    A spectrum in the to-classify queue has no prediction yet, and a prediction in the review
    queue has not been reviewed anywhere, so neither has to be read: one listing of a queue saves
    two reads per capture waiting on it. A queue nothing waits on is not listed.
    """
    last = ledger.get_value(LAST_FULL_CHECK_KEY)
    try:
        due = last is None or seconds_since(last) >= FULL_PREDICTION_CHECK_SECONDS
    except ValueError:
        due = True
    if due:
        return None
    names = layout_for(config.root_alias)
    to_classify = _items(catalog, names.to_classify) if ledger.count(states=["uploaded"]) else set()
    to_review = _items(catalog, names.needs_review) if ledger.count(states=["needs_review"]) else set()
    return to_classify, to_review


def pick_up_predictions(catalog, ledger, capture_ids=None, queued=None):
    """Step 4: copy predictions, and reviews made elsewhere, from the catalog into the ledger.

    Without `capture_ids` it looks at every capture that has no final state yet, except an
    uploaded one whose record is still in the to-classify queue and one waiting for review whose
    prediction is still in the review queue (`queued`, as still_in_queues gives them). With
    `capture_ids` it looks at exactly those captures whatever their state, which is what a local
    classification run calls right after it saved its predictions.

    Returns {"found": captures that got a new prediction, "updated": captures whose known
    prediction or its review changed, "errors": [...]}. Raises CatalogOffline when the catalog
    is unreachable.
    """
    if capture_ids is None:
        rows = ledger.rows(states=AWAITING_PREDICTION_STATES, newest_first=False)
    else:
        rows = [row for row in map(ledger.get, capture_ids) if row is not None]
    to_classify, to_review = queued if queued is not None else ((), ())
    report = {"found": 0, "updated": 0, "errors": []}
    for row in rows:
        if not row["record_id"]:
            continue
        if capture_ids is None and ((row["state"] == "uploaded" and row["record_id"] in to_classify)
                                    or (row["state"] == "needs_review" and row["prediction_id"] in to_review)):
            continue
        try:
            prediction = _newest_prediction(catalog, row["record_id"])
        except CatalogError as error:
            if _stops_the_pass(error):
                raise
            report["errors"].append(problem(row["capture_id"], "prediction", error))
            continue
        if prediction is None:
            continue
        if ledger.set_prediction(row["capture_id"], prediction.id, prediction.metadata):
            report["found" if row["prediction_id"] != prediction.id else "updated"] += 1
    return report


# ---------------------------------------------------------------------- step 3: register and upload


def raw_title(metadata):
    tail = " point %s" % metadata["point_id"]
    if metadata.get("scan_mode"):
        tail += " (%s)" % metadata["scan_mode"]
    return fit_title(metadata["sample_id"], tail + simulated_mark(metadata))


def session_title(metadata):
    return fit_title(metadata["sample_id"], " - %s%s" % (metadata["session_folder"], simulated_mark(metadata)))


class _Pass:
    """Step 3 of one sync. What was already ensured in the catalog is remembered for the pass."""

    def __init__(self, paths, config, ledger, catalog, result):
        self.paths = paths
        self.config = config
        self.ledger = ledger
        self.catalog = catalog
        self.result = result
        self._layout = None
        self._sessions = set()
        self._darks = {}

    def layout(self):
        if self._layout is None:
            self._layout = ensure_layout(self.catalog, self.config.root_alias)
        return self._layout

    def _session_collection(self, metadata):
        alias = session_alias(metadata["session_id"])
        if alias not in self._sessions:
            description = "Scan session %s, mode %s." % (metadata["session_id"], metadata["scan_mode"] or "unknown")
            self.catalog.ensure_collection(alias, session_title(metadata), description, parent=self.layout().sessions)
            self._sessions.add(alias)
        return alias

    def _dark_record(self, dark):
        """The id of the dark spectrum's record, creating the record the first time it is needed."""
        digest = dark["sha256"]
        if digest is None:
            # The capture names a dark file that is not on this Mac; there is nothing to register.
            return None
        if digest not in self._darks:
            row = self.ledger.get_dark(digest)
            if row is None:
                metadata = build_dark_metadata(self.paths.dark_measurements / dark["file"])
                record = self.catalog.create_record(
                    dark_alias(digest),
                    fit_title("Dark spectrum %s" % dark["file"], simulated_mark(metadata)),
                    metadata,
                    self.layout().darks,
                    identity_key="sha256",
                )
                self.ledger.add_dark(digest, dark["file"], record.id, record.alias)
                self._darks[digest] = record.id
            else:
                self._darks[digest] = row["record_id"]
        return self._darks[digest]

    def _register(self, row):
        folder = self.paths.scan_sessions / row["session_folder"]
        path = folder / row["point_file"]
        metadata = build_raw_metadata(self.paths.home, folder.name, read_point(path), path)
        names = self.layout()
        parent = self._session_collection(metadata)
        # A dark of another kind than the spectrum (its "reason" says so) gets no record from it.
        if metadata["dark"] is not None and not metadata["dark"].get("reason"):
            metadata["dark"]["record"] = self._dark_record(metadata["dark"])
        record = self.catalog.create_record(
            raw_alias(metadata["capture_id"]), raw_title(metadata), metadata, parent, identity_key="capture_id"
        )
        # A record that existed before and already has something derived from it was classified
        # earlier (the ledger was reset by a change of catalog). Queueing it again would only
        # make the classifier repeat the work; step 4 finds the prediction.
        if record.created or not any(dep.direction == "in" and dep.relation == "der" for dep in record.deps):
            self.catalog.link(names.to_classify, record.id)
        self.ledger.mark_registered(row["capture_id"], record.id, record.alias, metadata["raw_file"]["sha256"])
        self.result["registered"] += 1
        log.debug("registered %s as %s", row["capture_id"], record.id)

    @staticmethod
    def _unchanged_file(path, expected_sha256):
        """The file to upload, after checking that it still is the file the record describes."""
        try:
            digest = sha256_file(path)[0]
        except FileNotFoundError as error:
            raise _FileLost("the file %s is missing" % path.name) from error
        if expected_sha256 and digest != expected_sha256:
            raise _FileLost(
                "%s changed after it was registered (its SHA-256 no longer matches the record)" % path.name)
        return path

    def _start_upload(self, row):
        path = self._unchanged_file(
            self.paths.scan_sessions / row["session_folder"] / row["point_file"], row["raw_sha256"])
        transfer = self.catalog.put_data(row["record_id"], path)
        self.ledger.mark_uploading(row["capture_id"], transfer.task_id, transfer.status, transfer.message)
        self.result["uploads_started"] += 1

    def _poll_upload(self, row):
        try:
            transfer = self.catalog.transfer_status(row["upload_task_id"])
        except CatalogNotFound as error:
            raise _ItemError("the catalog no longer knows the transfer %s" % row["upload_task_id"]) from error
        if transfer.status == "succeeded":
            self.ledger.mark_uploaded(row["capture_id"])
            self.result["uploads_done"] += 1
        elif transfer.status == "failed":
            raise _ItemError(transfer.message or "the transfer failed")
        elif (transfer.status, transfer.message) != (row["upload_status"], row["upload_message"] or ""):
            self.ledger.set_upload_status(row["capture_id"], transfer.status, transfer.message)

    def _advance(self, row):
        """Move one capture as far along as it can go now."""
        capture_id = row["capture_id"]
        state = row["state"]
        if state == "error":
            # A failed step is done again from where it starts.
            state = {"register": "captured", "upload": "registered"}[row["error_stage"]]
        stage = "register"
        try:
            if state == "captured":
                self._register(row)
                state = "registered"
            stage = "upload"
            if state == "registered":
                self._start_upload(self.ledger.get(capture_id))
                state = "uploading"
            if state == "uploading":
                self._poll_upload(self.ledger.get(capture_id))
        except MetadataError as error:
            self._failed(capture_id, "metadata", error)
        except (CatalogError, OSError, _ItemError) as error:
            if _stops_the_pass(error):
                raise
            self._failed(capture_id, stage, error)

    def _failed(self, capture_id, stage, error):
        self.ledger.mark_error(capture_id, stage, str(error))
        self.result["errors"].append(problem(capture_id, stage, error))
        log.warning("%s: %s failed: %s", capture_id, stage, error)

    def advance_captures(self):
        for row in self.ledger.rows_to_advance(self.config.max_attempts, self.config.holds_back_simulated):
            self._advance(row)

    def advance_darks(self):
        """Upload the dark spectra the raw records refer to. Their records exist already, so a
        raw record never waits for its dark file to arrive."""
        for row in self.ledger.darks_to_advance(self.config.max_attempts):
            digest = row["sha256"]
            try:
                if row["state"] != "uploading":
                    path = self._unchanged_file(self.paths.dark_measurements / row["file"], digest)
                    started = self.catalog.put_data(row["record_id"], path)
                    self.ledger.dark_uploading(digest, started.task_id, started.status)
                    row = self.ledger.get_dark(digest)
                transfer = self.catalog.transfer_status(row["upload_task_id"])
                if transfer.status == "succeeded":
                    if row["last_error"]:
                        self._found_again(row)
                    self.ledger.dark_uploaded(digest)
                elif transfer.status == "failed":
                    raise _ItemError(transfer.message or "the transfer failed")
                elif transfer.status != row["upload_status"]:
                    self.ledger.dark_status(digest, transfer.status)
            except (CatalogError, OSError, _ItemError) as error:
                if _stops_the_pass(error):
                    raise
                if isinstance(error, _FileLost) and row["attempts"] + 1 >= self.config.max_attempts:
                    self._mark_unavailable(row, error)
                self.ledger.dark_failed(digest, str(error))
                self.result["errors"].append(problem(None, "dark", "dark spectrum %s: %s" % (row["file"], error)))
                log.warning("dark spectrum %s: upload failed: %s", row["file"], error)

    def _mark_unavailable(self, row, lost):
        """Say in the dark's record that its file will not come, on the last attempt to upload it.

        The classifier, here or on a cluster, sees only the catalog. Without this it would wait
        for the file for ever and leave the spectra taken with this dark unclassified; with it,
        they are rejected with this reason and go to a person. Raises what ends the pass before
        the attempt is counted, so that the next pass marks the record instead.
        """
        try:
            self.catalog.update_record(row["record_id"], metadata={UNAVAILABLE_KEY: str(lost)})
        except CatalogError as error:
            if _stops_the_pass(error):
                raise
            self.result["errors"].append(problem(None, "dark", "dark spectrum %s: %s" % (row["file"], error)))

    def _found_again(self, row):
        """A dark that failed before has arrived: the record no longer says that its file will not come."""
        try:
            self.catalog.update_record(row["record_id"], metadata={UNAVAILABLE_KEY: None})
        except CatalogError as error:
            if _stops_the_pass(error):
                raise
            # The classifier reads the mark only while the record has no file, so a mark left over is harmless.
            log.warning("dark spectrum %s: the old mark could not be removed: %s", row["file"], error)


def _publish_model(catalog, config, home, result):
    """Let the classifier part publish its model record, when that part is installed."""
    job = _optional_module(JOB_MODULE)
    if job is None:
        return
    try:
        job.ensure_model_record(catalog, config, home)
    except Exception as error:
        if _stops_the_pass(error):
            raise
        # Whatever goes wrong with the model record must not keep captures from being registered.
        result["errors"].append(problem(None, "model", error))
        log.warning("the model record could not be published: %s", error)


# ---------------------------------------------------------------------- the pass


def run_sync(home, config, ledger, catalog, retry_failed=False):
    """One sync pass. Returns the counters of PROTOCOL.md section 6 (without "status").

    "busy" is true, and nothing was done, when another process is in the middle of a sync.
    "online" is false when the catalog could not be reached: new points were still put into
    the ledger, and every state stays as it was. "held_simulated" counts the simulated captures
    this pass left out of the catalog (Settings.holds_back_simulated).
    """
    paths = Paths(home)
    result = {
        "busy": False,
        "online": True,
        "ingested": 0,
        "registered": 0,
        "uploads_started": 0,
        "uploads_done": 0,
        "predictions_found": 0,
        "reviews_found": 0,
        "reviews_sent": 0,
        "held_simulated": 0,
        "errors": [],
    }
    with ledger.sync_lock() as mine:
        if not mine:
            result["busy"] = True
            result["online"] = None
            return result
        if ledger.bind_catalog(config.catalog_key):
            log.info("the catalog changed: every capture will be registered in %s", catalog.label)
        if retry_failed:
            ledger.reset_attempts()

        found = discover(paths, ledger)
        result["ingested"] = found["ingested"]
        result["errors"].extend(found["errors"])
        # Problems with files that have no ledger row would otherwise be visible in this result only.
        lasting = [entry["message"] for entry in found["errors"] if entry["capture_id"] is None]
        if config.holds_back_simulated:
            result["held_simulated"] = ledger.count_held_simulated()

        try:
            result["reviews_sent"], errors = send_queued_reviews(catalog, config, ledger)
            result["errors"].extend(errors)
            _publish_model(catalog, config, paths.home, result)
            step = _Pass(paths, config, ledger, catalog, result)
            step.advance_captures()
            step.advance_darks()
            queued = still_in_queues(catalog, config, ledger)
            picked = pick_up_predictions(catalog, ledger, queued=queued)
            if queued is None:
                ledger.set_value(LAST_FULL_CHECK_KEY, utc_now())
            result["predictions_found"] = picked["found"]
            result["reviews_found"] = picked["updated"]
            result["errors"].extend(picked["errors"])
        except CatalogOffline as error:
            result["online"] = False
            log.info("the catalog is not reachable; everything waits for the next sync (%s)", error.detail or error)
        except CatalogAuthRequired as error:
            result["errors"].append(problem(None, "auth", error))
            log.warning("%s", error)

        ledger.set_value(LAST_PROBLEMS_KEY, json.dumps(lasting))
        ledger.set_value(LAST_SYNC_KEY, utc_now())
    return result
