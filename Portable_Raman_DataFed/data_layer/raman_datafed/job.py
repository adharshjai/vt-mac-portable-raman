"""The classification job, and the record of the model version it uses (PROTOCOL.md 5.2, 5.3).

`run_job` takes the spectra waiting in `<root>-to-classify`, runs the classifier, and saves each
prediction as a record derived from the raw spectrum, the model version and the dark spectrum.
It talks to the Catalog interface only (no ledger, no HOME), because the same function runs on
this Mac (`classify`) and on a cluster (`arc_job`).

Every step can be repeated: a job that dies half-way leaves the spectrum in the queue, and the
next run finds the prediction it had already saved instead of making a second one.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import re
import shutil
import socket
import tempfile
import zipfile
from pathlib import Path

from . import classifier
from .catalog import (
    PREDICTION_TYPE,
    RAW_TYPE,
    UNAVAILABLE_KEY,
    CatalogAuthRequired,
    CatalogConflict,
    CatalogError,
    CatalogNotFound,
    CatalogOffline,
    ensure_layout,
    fit_title,
    layout_for,
    model_alias,
    prediction_alias,
    session_predictions_alias,
    simulated_mark,
)
from .config import CommandError, DataLayerError, Paths, atomic_write_bytes, atomic_write_json, seconds_since, utc_now

log = logging.getLogger(__name__)

MODEL_TYPE = "raman.model"
MODEL_SCHEMA = "raman-model/1"
MODEL_UPLOAD_STATE = "model_upload.json"
# After sync.max_attempts uploads in a row that did not arrive, the model files are offered again
# only this often: a transfer problem that lasts for days must not start a transfer every minute.
MODEL_UPLOAD_RETRY_SECONDS = 3600
# Every zip entry gets this time stamp, so that the same model files always give the same archive.
_ARCHIVE_TIME = (1980, 1, 1, 0, 0, 0)
# A download that fails for a reason of its own (a transfer that never finishes takes minutes to
# say so) is one spectrum's problem; this many in a row are the transfer path's, and the run stops.
MAX_FAILED_DOWNLOADS = 2


class QueueMissing(CatalogNotFound):
    """There is no to-classify collection: nothing was ever registered under this root alias."""


class _ItemError(Exception):
    """One spectrum cannot be classified in this run; the others are not affected."""


class _Wait(Exception):
    """A file the spectrum needs has not been uploaded yet; the spectrum stays in the queue."""


def _problem(capture_id, stage, message):
    return {"capture_id": capture_id, "stage": stage, "message": str(message)}


# ---------------------------------------------------------------------- the model record


def model_metadata(model_dir):
    """The record metadata of the model version in a folder."""
    folder = Path(model_dir).resolve()
    sha256, files = classifier.model_fingerprint(folder)
    return {
        "type": MODEL_TYPE,
        "schema": MODEL_SCHEMA,
        "name": folder.name,
        "sha256": sha256,
        # A list, not an object keyed by file name: the names contain dots, which a catalog
        # query language reads as a path into the metadata.
        "files": [{"name": name, **files[name]} for name in classifier.MODEL_FILES],
        "meta": classifier.read_meta(folder),
    }


def find_or_create_model_record(catalog, root_alias, metadata):
    """The record of a model version, created on first use. Its files are uploaded separately."""
    alias = model_alias(metadata["sha256"])
    try:
        record = catalog.get_record(alias)
    except CatalogNotFound:
        names = ensure_layout(catalog, root_alias)
        return catalog.create_record(
            alias,
            "Classifier model %s" % metadata["name"],
            metadata,
            names.models,
            description="PCA-SVM classifier model files (scaler, PCA, SVM, meta.json) as one zip archive. "
            "Model hash (SHA-256 over the four files): %s." % metadata["sha256"],
            identity_key="sha256",
        )
    if record.metadata.get("type") != MODEL_TYPE or record.metadata.get("sha256") != metadata["sha256"]:
        raise CatalogConflict("The alias %s already names something that is not this model version" % alias)
    return record


def archive_name(metadata):
    """The file name of a model version's zip; it is the name the catalog hands the file back under."""
    return "%s-%s.zip" % (re.sub(r"[^A-Za-z0-9._-]", "_", metadata["name"]), metadata["sha256"][:12])


def build_model_archive(model_dir, dest_dir):
    """Write the zip of the four model files into dest_dir and return its path."""
    folder = Path(model_dir).resolve()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in classifier.MODEL_FILES:
            entry = zipfile.ZipInfo(name, date_time=_ARCHIVE_TIME)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, (folder / name).read_bytes())
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / archive_name({"name": folder.name, "sha256": classifier.model_hash(folder)})
    # Replaced in one step: an upload started earlier may still be reading the previous, identical file.
    atomic_write_bytes(path, buffer.getvalue())
    return path


def _upload_state(path, catalog_key, sha256):
    """What is remembered about uploading this model version to this catalog."""
    fresh = {"catalog": catalog_key, "sha256": sha256, "task_id": None, "attempts": 0, "last_attempt_utc": None,
             "last_error": ""}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    if not isinstance(state, dict) or (state.get("catalog"), state.get("sha256")) != (catalog_key, sha256):
        # Another catalog or another model: its transfer says nothing about this one.
        return fresh
    return {key: state.get(key, default) for key, default in fresh.items()}


def _upload_allowed(state, max_attempts):
    if state["attempts"] < max_attempts or not state["last_attempt_utc"]:
        return True
    try:
        return seconds_since(state["last_attempt_utc"]) >= MODEL_UPLOAD_RETRY_SECONDS
    except ValueError:
        return True


def ensure_model_record(catalog, config, home):
    """Publish the model version the settings name: its record, and the zip of its files.

    Called by every sync pass before captures are registered, so it does as little as possible
    once the model is in the catalog: one look at its record. An upload is never waited for; a
    later pass sees how the transfer went. A failed transfer is reported once, by raising. The
    files are offered again by the next pass, and after sync.max_attempts uploads in a row that
    did not arrive, once an hour.
    """
    paths = Paths(home)
    metadata = model_metadata(config.model_dir)
    record = find_or_create_model_record(catalog, config.root_alias, metadata)
    state_file = paths.staging / MODEL_UPLOAD_STATE
    if record.data_size > 0:
        # The files are in the catalog: what the upload needed on this Mac can go.
        state_file.unlink(missing_ok=True)
        (paths.staging / archive_name(metadata)).unlink(missing_ok=True)
        return record

    state = _upload_state(state_file, config.catalog_key, metadata["sha256"])
    failure = None
    if state["task_id"]:
        try:
            transfer = catalog.transfer_status(state["task_id"])
            status, message = transfer.status, transfer.message
        except CatalogNotFound:
            status, message = "failed", "the catalog no longer knows the transfer %s" % state["task_id"]
        if status in ("queued", "running"):
            return record
        # Finished either way. After a success the record shows its data on the next pass.
        state["task_id"] = None
        if status == "failed":
            failure = message or "the transfer failed"
            state["last_error"] = failure
    elif _upload_allowed(state, config.max_attempts):
        archive = build_model_archive(config.model_dir, paths.staging)
        try:
            transfer = catalog.put_data(record.id, archive)
        except CatalogAuthRequired as error:
            # Records were just read or made, so the credentials are there: what is missing is
            # the endpoint for transfers. Captures can be registered without one, and a sync
            # only goes on to them when this is not an error that ends its pass.
            raise CatalogError("The model files cannot be uploaded: %s" % error, detail=error.detail) from error
        state.update(task_id=transfer.task_id, attempts=state["attempts"] + 1, last_attempt_utc=utc_now())
        log.info("uploading the model files %s (transfer %s)", archive.name, transfer.task_id)
    else:
        return record
    paths.staging.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_file, state)
    if failure is not None:
        # The reason is part of the message: a sync reports the message only.
        raise CatalogError("The upload of the model files failed: %s" % failure)
    return record


def choose_model_record(catalog, names, wanted=None):
    """The model record a job without a model folder uses: the named one, else the only one."""
    if wanted:
        record = catalog.get_record(wanted)
        if record.metadata.get("type") != MODEL_TYPE:
            raise CatalogError("%s is not a model record" % wanted)
        return record
    try:
        entries = [entry for entry in catalog.list_collection(names.models) if not entry.id.startswith("c/")]
    except CatalogNotFound:
        entries = []
    records = [record for record in (catalog.get_record(entry.id) for entry in entries)
               if record.metadata.get("type") == MODEL_TYPE]
    if not records:
        raise CatalogError(
            "There is no model record in %s" % names.models,
            detail="The instrument computer publishes the model with its next sync; or name a model folder.",
        )
    if len(records) > 1:
        # Guessing could classify with a model nobody chose; the caller has to say which.
        raise CatalogError(
            "%s holds %d model versions; name the model record to use" % (names.models, len(records)),
            detail="Model records: %s." % ", ".join(
                "%s (%s)" % (record.alias or record.id, record.metadata.get("name")) for record in records),
        )
    return records[0]


def fetch_model(catalog, record, dest_dir):
    """Download the zip of a model record and unpack it under dest_dir. Returns the model folder.

    The folder is only handed out when its model hash equals the one in the record: the model
    files are pickles, and loading a pickle runs whatever code it holds.
    """
    expected = record.metadata.get("sha256")
    name = record.alias or record.id.replace("/", "_")
    sizes = {entry.get("name"): entry.get("bytes") for entry in record.metadata.get("files") or []
             if isinstance(entry, dict)}
    dest_dir = Path(dest_dir)
    folder = dest_dir / name
    if folder.is_dir():
        with contextlib.suppress(classifier.ModelError):
            if classifier.model_hash(folder) == expected:
                return folder
    if record.data_size <= 0:
        raise CatalogError(
            "The model record %s has no files yet" % name,
            detail="Their upload from the instrument computer has not finished. Run sync there, or name a "
            "model folder.",
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    download = Path(tempfile.mkdtemp(prefix="model-download-", dir=str(dest_dir)))
    try:
        archive_path = catalog.get_data(record.id, download)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in classifier.MODEL_FILES:
                    # Checked before unpacking, so that a wrong archive is not read into memory.
                    if archive.getinfo(member).file_size != sizes.get(member):
                        raise CatalogError("The model archive of %s does not match its record" % name,
                                           detail="%s has another size than the record states." % member)
                    atomic_write_bytes(folder / member, archive.read(member))
        except (KeyError, zipfile.BadZipFile) as error:
            raise CatalogError("The model archive of %s cannot be used" % name, detail=str(error)) from error
    finally:
        shutil.rmtree(download, ignore_errors=True)
    if classifier.model_hash(folder) != expected:
        shutil.rmtree(folder, ignore_errors=True)
        raise CatalogError("The model archive of %s does not match its record" % name,
                           detail="The model hash of the downloaded files is not the one in the record.")
    return folder


# ---------------------------------------------------------------------- the job


def prediction_title(raw_metadata):
    """Free text in front is shortened; the tail, which carries the "[simulated]" marker, is kept."""
    return fit_title("Prediction %s" % (raw_metadata.get("sample_id") or ""),
                     " point %s%s" % (raw_metadata.get("point_id"), simulated_mark(raw_metadata)))


def session_predictions_title(raw_metadata):
    return fit_title("Predictions %s" % (raw_metadata.get("sample_id") or ""),
                     " - %s%s" % (raw_metadata.get("session_folder"), simulated_mark(raw_metadata)))


class _Job:
    """One run over the queue. The model, the predictor and the dark spectra are found once."""

    def __init__(self, catalog, config, model_dir, work_dir, runner, slurm_job_id, model_record):
        self.catalog = catalog
        self.root_alias = config.root_alias
        self.names = layout_for(config.root_alias)
        self.threshold = config.confidence_threshold
        self.model_dir = Path(model_dir) if model_dir is not None else None
        self.work_dir = Path(work_dir)
        self.wanted_model = model_record
        self.runner = runner
        self.slurm_job_id = slurm_job_id
        self.started_utc = utc_now()
        self.software = None
        # What the run is doing, for the "stage" of an error entry.
        self.stage = "read"
        self.capture_id = None
        self.model_record = None
        self.loaded = None
        self.failed_downloads = 0
        self._darks = {}
        self._prediction_collections = set()

    def model(self):
        """The model record. Knowing it is enough to recognise predictions made earlier."""
        if self.model_record is None:
            if self.model_dir is not None:
                self.model_record = find_or_create_model_record(self.catalog, self.root_alias,
                                                                model_metadata(self.model_dir))
            else:
                self.model_record = choose_model_record(self.catalog, self.names, self.wanted_model)
        return self.model_record

    def predictor(self):
        """The loaded model. Loading waits until a spectrum really has to be classified."""
        if self.loaded is None:
            record = self.model()
            folder = self.model_dir
            if folder is None:
                folder = fetch_model(self.catalog, record, self.work_dir)
            predictor = classifier.Predictor(folder, threshold=self.threshold)
            if predictor.sha256 != record.metadata.get("sha256"):
                raise classifier.ModelError("The model files changed while the job was starting")
            self.software = classifier.library_versions()
            self.loaded = predictor
        return self.loaded

    @contextlib.contextmanager
    def _downloaded(self, record_id, expected_sha256, what):
        """The file of a record, after checking that it is the file the record describes."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="download-", dir=str(self.work_dir)))
        try:
            try:
                path = self.catalog.get_data(record_id, folder)
            except (CatalogOffline, CatalogAuthRequired, CatalogNotFound):
                raise
            except CatalogError:
                self.failed_downloads += 1
                raise
            self.failed_downloads = 0
            if expected_sha256 and hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
                raise _ItemError("the %s stored in %s is not the file its record describes (SHA-256 differs)"
                                 % (what, record_id))
            yield path
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _dark(self, reference):
        """(dark values or None, why there are none, the "dark" entry of the prediction).

        A raw record without a dark record has no dark to use: none was named, its file was not
        on the instrument computer, or it is of the other kind than the spectrum, which the
        record's "reason" then says (metadata.unpaired_reason).
        """
        reference = reference if isinstance(reference, dict) else {}
        record_id = reference.get("record")
        if not record_id:
            reason = reference.get("reason")
            return None, reason if isinstance(reason, str) and reason else classifier.NO_DARK, None
        if record_id not in self._darks:
            self._darks[record_id] = self._load_dark(record_id)
        found = self._darks[record_id]
        if isinstance(found, Exception):
            # The same answer for every spectrum taken with this dark, without asking again.
            raise type(found)(str(found))
        return found

    def _load_dark(self, record_id):
        record = self.catalog.get_record(record_id)
        entry = {"record": record.id, "sha256": record.metadata.get("sha256")}
        if record.data_size <= 0:
            lost = record.metadata.get(UNAVAILABLE_KEY)
            if isinstance(lost, str) and lost:
                # The instrument computer gave up on the upload: waiting would leave the spectrum
                # in the queue for ever, so it goes to a person with the reason.
                return None, "the dark spectrum %s was never uploaded: %s" % (
                    record.metadata.get("file") or record.alias or record.id, lost), entry
            return _Wait("the dark spectrum %s is not uploaded yet" % (record.alias or record.id))
        try:
            with self._downloaded(record.id, entry["sha256"], "dark spectrum") as path:
                return classifier.load_dark(path), None, entry
        except ValueError as error:
            return None, str(error), entry
        except _ItemError as error:
            return error

    def _point(self, raw):
        with self._downloaded(raw.id, (raw.metadata.get("raw_file") or {}).get("sha256"), "raw file") as path:
            content = path.read_bytes()
        try:
            point = json.loads(content.decode("utf-8-sig"))
        except ValueError as error:
            raise _ItemError("the raw file of %s is not JSON (%s)" % (raw.id, error)) from error
        if not isinstance(point, dict) or point.get("capture_id") != raw.metadata["capture_id"]:
            raise _ItemError("the raw file of %s belongs to another capture" % raw.id)
        return point

    def _existing(self, raw, alias):
        """The prediction this model already made for the spectrum, when the raw record shows it."""
        for dep in raw.deps:
            if dep.direction == "in" and dep.relation == "der" and dep.alias == alias:
                return self.catalog.get_record(dep.id)
        return None

    def _save(self, raw, alias, result, dark_entry):
        source, model = raw.metadata, self.model()
        metadata = {
            "type": PREDICTION_TYPE,
            "capture_id": source["capture_id"],
            "sample_id": source.get("sample_id"),
            "point_id": source.get("point_id"),
            "raw_record": raw.id,
            "model": {"record": model.id, "name": model.metadata.get("name"), "sha256": model.metadata["sha256"]},
            "dark": dark_entry,
            "label": result["label"],
            "top_class": result["top_class"],
            "confidence": result["confidence"],
            "scores": result["scores"],
            "outcome": result["outcome"],
            "reason": result["reason"],
            "threshold": result["threshold"],
            "needs_review": result["needs_review"],
            "simulated": source.get("simulated"),
            "job": {"runner": self.runner, "host": socket.gethostname(), "slurm_job_id": self.slurm_job_id,
                    "started_utc": self.started_utc, "finished_utc": utc_now(), "software": self.software},
            "review": {"status": "pending" if result["needs_review"] else "not_required", "label": None,
                       "reviewer": None, "time_utc": None, "comment": ""},
        }
        deps = [("der", raw.id), ("der", model.id)]
        if dark_entry is not None:
            deps.append(("der", dark_entry["record"]))
        description = ("Classifier output for one raw spectrum, derived from the spectrum, the model version "
                       "and the dark spectrum.")
        if source.get("simulated"):
            description += " The spectrum is simulated, not measured."
        # Idempotent on the alias: should an earlier run have saved this prediction without the raw
        # record showing it, that record comes back here and nothing is made twice.
        return self.catalog.create_record(alias, prediction_title(source), metadata, self._collection_for(source),
                                          deps=deps, description=description, identity_key="capture_id")

    def _collection_for(self, source):
        """The predictions collection of the spectrum's scan session, made the first time it is needed.

        One collection per session rather than one for every prediction ever made: a DataFed
        collection is understood to hold a limited number of items (not checked against a live
        server), and a session holds the spectra of one scan only.
        """
        session_id = source.get("session_id")
        if not session_id:
            return self.names.predictions
        alias = session_predictions_alias(session_id)
        if alias not in self._prediction_collections:
            self.catalog.ensure_collection(
                alias, session_predictions_title(source),
                "Predictions of the spectra of scan session %s." % session_id, parent=self.names.predictions)
            self._prediction_collections.add(alias)
        return alias

    def classify(self, entry):
        """Bring one queue entry to "prediction saved, out of the queue". Returns its report item.

        Raises _Wait when a file it needs is not uploaded yet, and anything else when it fails.
        """
        self.stage, self.capture_id = "read", None
        raw = self.catalog.get_record(entry.id)
        capture_id = raw.metadata.get("capture_id")
        if raw.metadata.get("type") != RAW_TYPE or not capture_id:
            raise _ItemError("%s is in the queue but is not a raw spectrum record" % (raw.alias or raw.id))
        self.capture_id = capture_id
        if raw.data_size <= 0:
            raise _Wait("the raw file of %s is not uploaded yet" % (raw.alias or raw.id))

        self.stage = "model"
        alias = prediction_alias(capture_id, self.model().metadata["sha256"])
        self.stage = "read"
        prediction = self._existing(raw, alias)
        created = False
        if prediction is None:
            # The model first: when it cannot be had, nothing else needs to be transferred.
            self.stage = "model"
            predictor = self.predictor()
            self.stage = "read"
            values, reason, dark_entry = self._dark(raw.metadata.get("dark"))
            # Without a usable dark spectrum the outcome does not depend on the raw file.
            point = self._point(raw) if values is not None else None
            self.stage = "classify"
            result = predictor.reject(reason) if values is None else predictor.predict_point(point, values)
            self.stage = "save"
            prediction = self._save(raw, alias, result, dark_entry)
            created = prediction.created
        self.stage = "save"
        saved = prediction.metadata
        if saved.get("needs_review"):
            self.catalog.link(self.names.needs_review, prediction.id)
        # Last, so that a run that dies before this line classifies the spectrum again and finds
        # the prediction above instead of making another one.
        self.catalog.unlink(self.names.to_classify, raw.id)
        return {"capture_id": capture_id, "raw_record": raw.id, "prediction_record": prediction.id,
                "label": saved.get("label"), "top_class": saved.get("top_class"),
                "confidence": saved.get("confidence"), "outcome": saved.get("outcome"),
                "needs_review": bool(saved.get("needs_review")), "created": created}


def run_job(catalog, config, model_dir, work_dir, limit=None, runner="local", slurm_job_id=None, model_record=None):
    """Classify what waits in `<root>-to-classify`, at most `limit` spectra.

    `config` supplies root_alias and confidence_threshold (None: the model's own). `model_dir`
    None fetches the model files from the model record: `model_record`, or the only one there
    is. `work_dir` receives the downloads; on a cluster it has to be a folder that the Globus
    endpoint of the DataFed client can write to.

    Returns {"runner", "processed", "predictions", "skipped", "remaining", "errors", "items",
    "model"}: `processed` spectra left the queue with a prediction, of which `predictions` were
    made by this run (the others had been saved by a run that died); `skipped` wait for a file
    that is not uploaded yet; `remaining` were not looked at, because of `limit` or because the
    run had to stop. One spectrum that fails is listed under `errors` and stays in the queue.

    Raises QueueMissing when the queue collection does not exist, and the error itself when
    the catalog or the model failed before anything was done.
    """
    job = _Job(catalog, config, model_dir, work_dir, runner, slurm_job_id, model_record)
    report = {"runner": runner, "processed": 0, "predictions": 0, "skipped": 0, "remaining": 0, "errors": [],
              "items": [], "model": None}
    try:
        queue = [entry for entry in catalog.list_collection(job.names.to_classify) if not entry.id.startswith("c/")]
    except CatalogNotFound as error:
        raise QueueMissing(
            "There is no collection %s" % job.names.to_classify,
            detail="Nothing has been registered under the root alias %s yet." % config.root_alias,
        ) from error

    for position, entry in enumerate(queue):
        if limit is not None and report["processed"] >= limit:
            report["remaining"] = len(queue) - position
            break
        try:
            item = job.classify(entry)
        except _Wait as waiting:
            report["skipped"] += 1
            log.info("%s; it stays in the queue", waiting)
            continue
        except Exception as error:
            # An unreachable catalog, missing credentials and a model that cannot be had concern
            # every spectrum, not this one: the run ends here.
            for_all = isinstance(error, (CatalogOffline, CatalogAuthRequired, classifier.ModelError)) or (
                job.stage == "model" and job.loaded is None)
            if for_all and not report["processed"] and not report["errors"]:
                raise
            if not isinstance(error, (DataLayerError, _ItemError, OSError)):
                log.exception("classifying %s failed", entry.id)
            if for_all:
                stage = "catalog" if isinstance(error, (CatalogOffline, CatalogAuthRequired)) else "model"
                report["errors"].append(_problem(None, stage, error))
                report["remaining"] = len(queue) - position
                break
            report["errors"].append(_problem(job.capture_id, job.stage, error))
            log.warning("%s: %s failed: %s", job.capture_id or entry.id, job.stage, error)
            if job.failed_downloads >= MAX_FAILED_DOWNLOADS:
                report["errors"].append(_problem(
                    None, "catalog", "%d downloads in a row failed, so the run stopped: the transfers from the "
                    "catalog to this computer do not seem to work" % job.failed_downloads))
                report["remaining"] = len(queue) - position - 1
                break
            continue
        report["items"].append(item)
        report["processed"] += 1
        report["predictions"] += int(item["created"])
        log.info("%s: %s (%s)%s", item["capture_id"], item["label"], item["outcome"],
                 "" if item["created"] else ", saved by an earlier run")

    if job.model_record is not None:
        report["model"] = {"record": job.model_record.id, "name": job.model_record.metadata.get("name"),
                           "sha256": job.model_record.metadata.get("sha256")}
    return report


# ---------------------------------------------------------------------- the command


def _update_ledger(ctx, report):
    """Show the new predictions in the ledger at once instead of after the next sync."""
    # Imported here: the ledger and sync are this Mac's side, which the job itself does not need.
    from . import sync
    from .ledger import PENDING_STATES

    if ctx.ledger.catalog_key != ctx.config.catalog_key:
        # The rows describe another catalog until the next sync has bound them to this one.
        return
    capture_ids = []
    for item in report["items"]:
        row = ctx.ledger.get(item["capture_id"])
        # A row that still tracks its upload is left to sync, which finishes that first.
        if row is not None and row["record_id"] == item["raw_record"] and row["state"] not in PENDING_STATES:
            capture_ids.append(item["capture_id"])
    if not capture_ids:
        return
    try:
        picked = sync.pick_up_predictions(ctx.catalog, ctx.ledger, capture_ids=capture_ids)
    except CatalogOffline:
        # The predictions are saved; the next sync copies them into the ledger.
        return
    report["errors"].extend(picked["errors"])


def cmd_classify(args, ctx):
    limit = getattr(args, "limit", None)
    if limit is not None and limit < 1:
        raise CommandError("--limit must be at least 1")
    config = ctx.config
    try:
        report = run_job(ctx.catalog, config, config.model_dir, ctx.paths.staging / "classify", limit=limit,
                         runner="local")
    except QueueMissing:
        # Before the first sync there is no queue yet: nothing to classify, not a failure.
        return {"runner": "local", "processed": 0, "predictions": 0, "skipped": 0, "remaining": 0, "errors": [],
                "items": [], "model": None}
    _update_ledger(ctx, report)
    return report


def register(subparsers):
    command = subparsers.add_parser(
        "classify", help="run the classifier on this computer for the spectra waiting in the catalog")
    command.add_argument("--limit", type=int, metavar="N", help="classify at most N spectra")
    command.set_defaults(func=cmd_classify)
