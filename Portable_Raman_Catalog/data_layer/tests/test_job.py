"""The classification job: the model record, the whole chain from a captured spectrum to a
reviewed prediction, and what happens when the job dies, a file is not there yet, a spectrum
is bad or the connection goes.

The spectra are the three real points in fixtures/. The catalog is the local demo: nothing here
connects to any service.
"""

import contextlib
import hashlib
import json
import shutil
import socket
import unittest
import zipfile
from unittest import mock

import fixtures
import support
from raman_catalog import classifier, cli, job
from raman_catalog.catalog import (
    CatalogAuthRequired,
    CatalogConflict,
    CatalogError,
    CatalogOffline,
    dark_alias,
    ensure_layout,
    model_alias,
    prediction_alias,
    raw_alias,
    session_predictions_alias,
)
from raman_catalog.config import load_settings
from raman_catalog.metadata import sha256_file

needs_model = fixtures.needs_model

MODEL_ALIAS = model_alias(fixtures.MODEL_HASH)
PREDICTION_KEYS = ["type", "capture_id", "sample_id", "point_id", "raw_record", "model", "dark", "label",
                   "top_class", "confidence", "scores", "outcome", "reason", "threshold", "needs_review",
                   "simulated", "job", "review"]


class Crash(BaseException):
    """Stands for the process dying: nothing in the data layer catches it."""


class ModelRecord(fixtures.HomeCase):
    def test_sync_creates_the_record_and_later_passes_upload_the_archive(self):
        fixtures.make_real_session(self.home)
        with mock.patch.object(self.catalog, "put_data", wraps=self.catalog.put_data) as uploads:
            first = self.sync()
            self.assertEqual(first["errors"], [])
            record = self.catalog.get_record(MODEL_ALIAS)
            self.assertEqual([entry.id for entry in self.catalog.list_collection(self.names.models)], [record.id])
            metadata = record.metadata
            self.assertEqual(list(metadata), ["type", "schema", "name", "sha256", "files", "meta"])
            self.assertEqual((metadata["type"], metadata["schema"], metadata["name"], metadata["sha256"]),
                             ("raman.model", "raman-model/1", "pca_svm_pure_20260922", fixtures.MODEL_HASH))
            self.assertEqual(metadata["meta"],
                             json.loads((fixtures.MODEL_DIR / "meta.json").read_text(encoding="utf-8")))
            self.assertEqual([entry["name"] for entry in metadata["files"]], list(classifier.MODEL_FILES))
            for entry in metadata["files"]:
                content = (fixtures.MODEL_DIR / entry["name"]).read_bytes()
                self.assertEqual((entry["sha256"], entry["bytes"]),
                                 (hashlib.sha256(content).hexdigest(), len(content)))
            # Like every upload it is started and not waited for.
            self.assertEqual(record.data_size, 0)

            self.assertEqual(self.sync(3)["errors"], [])
            started = [call.args[0] for call in uploads.call_args_list].count(record.id)
        self.assertEqual(started, 1)
        record = self.catalog.get_record(MODEL_ALIAS)
        self.assertGreater(record.data_size, 0)
        archive = self.catalog.get_data(record.id, self.home / "fetched")
        self.assertEqual(archive.name, "pca_svm_pure_20260922-f1f3e9ae9fcc.zip")
        with zipfile.ZipFile(archive) as unpacked:
            self.assertEqual(unpacked.namelist(), list(classifier.MODEL_FILES))
            for name in classifier.MODEL_FILES:
                self.assertEqual(unpacked.read(name), (fixtures.MODEL_DIR / name).read_bytes())
        # Nothing the upload needed is left behind on this Mac.
        self.assertEqual(list(self.paths.staging.iterdir()), [])
        self.assertEqual(self.kinds()["raman.model"], 1)

    def test_published_model_costs_one_look_per_pass(self):
        fixtures.make_real_session(self.home)
        self.sync(4)
        with contextlib.ExitStack() as stack:
            calls = {name: stack.enter_context(mock.patch.object(self.catalog, name, wraps=getattr(self.catalog, name)))
                     for name in ("get_record", "create_record", "ensure_collection", "put_data", "transfer_status",
                                  "list_collection")}
            record = job.ensure_model_record(self.catalog, self.config, self.home)
        self.assertEqual(record.alias, MODEL_ALIAS)
        calls.pop("get_record").assert_called_once_with(MODEL_ALIAS)
        for name, call in calls.items():
            call.assert_not_called()

    def test_archive_is_the_same_bytes_every_time(self):
        first = job.build_model_archive(fixtures.MODEL_DIR, self.home / "a").read_bytes()
        second = job.build_model_archive(fixtures.MODEL_DIR, self.home / "b").read_bytes()
        self.assertEqual(first, second)
        self.assertEqual([path.name for path in (self.home / "a").iterdir()],
                         ["pca_svm_pure_20260922-f1f3e9ae9fcc.zip"])

    def test_failed_upload_is_reported_once_and_offered_again_within_limits(self):
        self.config.set("sync.max_attempts", 2)
        state_file = self.paths.staging / job.MODEL_UPLOAD_STATE

        def publish():
            return job.ensure_model_record(self.catalog, self.config, self.home)

        with mock.patch.object(self.catalog, "put_data", wraps=self.catalog.put_data) as uploads:
            # An archive that is gone when the transfer wants to read it: the transfer fails.
            with mock.patch.object(job, "build_model_archive", return_value=self.home / "gone.zip"):
                for attempt in (1, 2):
                    publish()
                    self.assertEqual(uploads.call_count, attempt)
                    publish()
                    with self.assertRaises(CatalogError) as raised:
                        publish()
                    self.assertRegex(str(raised.exception),
                                     "^The upload of the model files failed: cannot read the source file gone.zip")
                # Two uploads in a row did not arrive: no third one for now, and nothing new to report.
                publish()
                publish()
                self.assertEqual(uploads.call_count, 2)
                state = json.loads(state_file.read_text(encoding="utf-8"))
                self.assertEqual((state["attempts"], state["task_id"]), (2, None))
                self.assertIn("cannot read the source file", state["last_error"])

            # An hour later the files are offered again, and this time they arrive.
            state["last_attempt_utc"] = "2026-11-13T10:00:00.000Z"
            state_file.write_text(json.dumps(state), encoding="utf-8")
            publish()
            self.assertEqual(uploads.call_count, 3)
            publish()
            publish()
            self.assertGreater(publish().data_size, 0)
            self.assertEqual(uploads.call_count, 3)
        self.assertFalse(state_file.exists())

    def test_upload_state_of_another_catalog_or_model_is_not_used(self):
        self.paths.staging.mkdir(parents=True)
        state_file = self.paths.staging / job.MODEL_UPLOAD_STATE
        state_file.write_text(json.dumps({"catalog": "another|catalog", "sha256": fixtures.MODEL_HASH,
                                          "task_id": "task/99", "attempts": 9}), encoding="utf-8")
        with mock.patch.object(self.catalog, "transfer_status", wraps=self.catalog.transfer_status) as asked:
            job.ensure_model_record(self.catalog, self.config, self.home)
            asked.assert_not_called()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual((state["catalog"], state["attempts"]), (self.config.catalog_key, 1))
        self.assertTrue(state["task_id"].startswith("task/"))

    def test_missing_model_folder_is_reported_and_captures_still_register(self):
        settings = load_settings(self.home)
        settings.set("classifier.model_dir", "models/not_there")
        settings.save()
        self.ctx.reload()
        support.write_dark(self.home)
        support.make_session(self.home, points=2)
        result = self.sync()
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in result["errors"]], [(None, "model")])
        self.assertIn("pure_scaler_minmax.joblib", result["errors"][0]["message"])
        self.assertEqual(result["registered"], 2)

    def test_missing_transfer_endpoint_does_not_keep_captures_from_being_registered(self):
        fixtures.make_real_session(self.home)
        no_endpoint = CatalogAuthRequired("No default Globus endpoint is set, so files cannot be transferred")
        with mock.patch.object(self.catalog, "put_data", side_effect=no_endpoint):
            result = self.sync()
        # The model's upload fails first; the pass still goes on to the captures, and only ends
        # where it would without a model: at the first capture's own upload.
        self.assertEqual([entry["stage"] for entry in result["errors"]], ["model", "auth"])
        self.assertIn("No default Globus endpoint is set", result["errors"][0]["message"])
        self.assertEqual(result["registered"], 1)
        self.assertFalse((self.paths.staging / job.MODEL_UPLOAD_STATE).exists())
        # With the endpoint in place everything goes through.
        self.assertEqual(self.sync(4)["errors"], [])
        self.assertGreater(self.catalog.get_record(MODEL_ALIAS).data_size, 0)
        self.assertEqual(set(self.states().values()), {"uploaded"})

    def test_unreachable_catalog_ends_the_pass_quietly(self):
        fixtures.make_real_session(self.home)
        self.catalog.set_offline(True)
        with self.assertRaises(CatalogOffline):
            job.ensure_model_record(self.catalog, self.config, self.home)
        result = self.sync()
        self.assertEqual((result["online"], result["errors"]), (False, []))
        self.assertFalse((self.paths.staging / job.MODEL_UPLOAD_STATE).exists())

    def test_alias_taken_by_something_else_is_a_conflict(self):
        names = ensure_layout(self.catalog, self.config.root_alias)
        self.catalog.create_record(MODEL_ALIAS, "Not a model", {"type": "note"}, names.models)
        with self.assertRaises(CatalogConflict):
            job.ensure_model_record(self.catalog, self.config, self.home)

    def test_title_keeps_the_simulated_marker_within_the_title_limit(self):
        title = job.prediction_title({"sample_id": "a very long sample name " * 10, "point_id": 12, "simulated": True})
        self.assertLessEqual(len(title), 80)
        self.assertTrue(title.startswith("Prediction a very long sample name"))
        self.assertTrue(title.endswith("... point 12 [simulated]"))
        self.assertEqual(job.prediction_title({"sample_id": "S-001", "point_id": 3, "simulated": False}),
                         "Prediction S-001 point 3")


@needs_model
class WholeChain(fixtures.HomeCase):
    # Stricter than the model's own 0.8, so that one of the three real points needs a person.
    threshold = 0.9

    def test_capture_sync_classify_review(self):
        session = fixtures.make_real_session(self.home)
        self.sync(2)
        self.assertEqual(set(self.states().values()), {"uploaded"})
        self.assertEqual(len(self.queue()), 3)

        report = self.command("classify")
        self.assertEqual((report["runner"], report["processed"], report["predictions"], report["skipped"],
                          report["remaining"], report["errors"]), ("local", 3, 3, 0, 0, []))
        self.assertEqual([(item["label"], item["top_class"], item["outcome"], item["needs_review"], item["created"])
                          for item in report["items"]],
                         [("UNKNOWN", "r6g", "low_confidence", True, True),
                          ("methylene_blue", "methylene_blue", "confident", False, True),
                          ("UNKNOWN", "di_water", "background", False, True)])

        model = self.catalog.get_record(MODEL_ALIAS)
        dark = self.catalog.get_record(dark_alias(sha256_file(fixtures.FIXTURES / fixtures.DARK_FILE)[0]))
        self.assertEqual(report["model"], {"record": model.id, "name": "pca_svm_pure_20260922",
                                           "sha256": fixtures.MODEL_HASH})
        predictions = []
        for capture_id, name, item in zip(session["capture_ids"], fixtures.POINT_FILES, report["items"]):
            raw = self.catalog.get_record(raw_alias(capture_id))
            prediction = self.catalog.get_record(prediction_alias(capture_id, fixtures.MODEL_HASH))
            predictions.append(prediction)
            metadata = prediction.metadata
            self.assertEqual((item["capture_id"], item["raw_record"], item["prediction_record"]),
                             (capture_id, raw.id, prediction.id))
            self.assertEqual(list(metadata), PREDICTION_KEYS)
            self.assertEqual((metadata["type"], metadata["capture_id"], metadata["sample_id"], metadata["point_id"],
                              metadata["raw_record"]),
                             ("raman.prediction", capture_id, "R6G-MB-plate", raw.metadata["point_id"], raw.id))
            self.assertEqual(metadata["model"], {"record": model.id, "name": "pca_svm_pure_20260922",
                                                 "sha256": fixtures.MODEL_HASH})
            self.assertEqual(metadata["dark"], {"record": dark.id, "sha256": dark.metadata["sha256"]})
            for material, score in fixtures.EXPECTED_SCORES[name].items():
                self.assertAlmostEqual(metadata["scores"][material], score, delta=fixtures.SCORE_TOLERANCE)
            self.assertEqual(metadata["confidence"], metadata["scores"][metadata["top_class"]])
            self.assertEqual((metadata["threshold"], metadata["simulated"]), (0.9, False))
            block = metadata["job"]
            self.assertEqual(list(block), ["runner", "host", "slurm_job_id", "started_utc", "finished_utc", "software"])
            self.assertEqual((block["runner"], block["host"], block["slurm_job_id"]),
                             ("local", socket.gethostname(), None))
            self.assertLessEqual(block["started_utc"], block["finished_utc"])
            self.assertEqual(block["software"], classifier.library_versions())
            self.assertEqual(metadata["review"], {
                "status": "pending" if metadata["needs_review"] else "not_required", "label": None,
                "reviewer": None, "time_utc": None, "comment": ""})
            self.assertEqual(prediction.title, "Prediction R6G-MB-plate point %d" % raw.metadata["point_id"])
            # The three provenance links, seen from the prediction ...
            self.assertEqual({(dep.relation, dep.id, dep.direction) for dep in prediction.deps},
                             {("der", raw.id, "out"), ("der", model.id, "out"), ("der", dark.id, "out")})
            # ... and from the spectrum it was made from.
            self.assertEqual([(dep.relation, dep.id, dep.direction) for dep in raw.deps],
                             [("der", prediction.id, "in")])
        ids = sorted(prediction.id for prediction in predictions)
        for source in (model, dark):
            seen = self.catalog.get_record(source.id)
            self.assertEqual(sorted(dep.id for dep in seen.deps if (dep.relation, dep.direction) == ("der", "in")),
                             ids)
        # The predictions of one scan session have a collection of their own under the predictions collection.
        (collection,) = self.catalog.list_collection(self.names.predictions)
        self.assertEqual(collection.alias, session_predictions_alias(fixtures.SESSION_ID))
        self.assertEqual(collection.title, "Predictions R6G-MB-plate - %s" % fixtures.SESSION_FOLDER)
        self.assertEqual(sorted(entry.id for entry in self.catalog.list_collection(collection.id)), ids)
        self.assertEqual(self.queue(), [])
        self.assertEqual([entry.id for entry in self.catalog.list_collection(self.names.needs_review)],
                         [predictions[0].id])
        self.assertEqual(self.kinds(), {"raman.raw_spectrum": 3, "raman.dark_spectrum": 1, "raman.model": 1,
                                        "raman.prediction": 3})

        # The provenance command, started at the raw spectrum.
        chain = self.command("provenance", "--record", raw_alias(session["capture_ids"][0]))
        self.assertEqual({node["type"]: node["role"] for node in chain["nodes"]},
                         {"raman.raw_spectrum": "start", "raman.prediction": "derived", "raman.model": "source",
                          "raman.dark_spectrum": "source"})
        self.assertEqual(sorted(edge["to"] for edge in chain["edges"] if edge["from"] == predictions[0].id),
                         sorted([chain["nodes"][0]["id"], model.id, dark.id]))

        # The ledger was brought up to date by the run itself: the next list shows the results.
        self.assertEqual(list(self.states().values()), ["needs_review", "accepted", "accepted"])
        listed = {item["capture_id"]: item for item in self.command("list")["items"]}
        self.assertEqual(listed[session["capture_ids"][1]]["prediction"], {
            "record_id": predictions[1].id, "label": "methylene_blue", "top_class": "methylene_blue",
            "confidence": predictions[1].metadata["confidence"], "outcome": "confident", "needs_review": False})

        waiting = self.command("review-list")["items"]
        self.assertEqual([item["capture_id"] for item in waiting], [session["capture_ids"][0]])
        self.assertEqual((waiting[0]["prediction_id"], waiting[0]["reason"], waiting[0]["threshold"]),
                         (predictions[0].id, "Low pure-class confidence", 0.9))

        done = self.command("review-set", "--prediction", predictions[0].id, "--decision", "confirm",
                            "--reviewer", "Test Reviewer", "--comment", "bands at 612 and 1360")
        self.assertIs(done["queued"], False)
        saved = self.catalog.get_record(predictions[0].id).metadata
        self.assertEqual({key: saved["review"][key] for key in ("status", "label", "reviewer", "comment")},
                         {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer",
                          "comment": "bands at 612 and 1360"})
        self.assertIs(saved["needs_review"], False)
        # The model's own answer is still there, next to the person's.
        self.assertEqual((saved["label"], saved["top_class"], saved["outcome"], saved["scores"]),
                         ("UNKNOWN", "r6g", "low_confidence", predictions[0].metadata["scores"]))
        self.assertEqual(self.catalog.list_collection(self.names.needs_review), [])

        # Ledger and catalog agree: a sync finds nothing to change.
        row = self.ctx.ledger.get(session["capture_ids"][0])
        self.assertEqual((row["state"], row["review_queued"], json.loads(row["review_json"])),
                         ("reviewed", 0, saved["review"]))
        after = self.sync()
        self.assertEqual((after["predictions_found"], after["reviews_found"], after["reviews_sent"], after["errors"]),
                         (0, 0, 0, []))
        self.assertEqual(list(self.states().values()), ["reviewed", "accepted", "accepted"])
        self.assertEqual(self.command("review-list")["items"], [])
        self.assertEqual(self.command("classify")["processed"], 0)


@needs_model
class JobDetails(fixtures.HomeCase):
    def test_nothing_registered_yet_is_not_a_failure(self):
        report = self.command("classify")
        self.assertEqual((report["processed"], report["predictions"], report["skipped"], report["errors"]),
                         (0, 0, 0, []))
        self.assertFalse((self.paths.local_demo / "records").exists())
        with self.assertRaises(job.QueueMissing):
            self.run_job()

    def test_raw_file_not_uploaded_yet_stays_queued(self):
        fixtures.make_real_session(self.home)
        self.sync()
        self.assertEqual(set(self.states().values()), {"uploading"})
        with mock.patch.object(classifier, "Predictor", side_effect=AssertionError("the model was loaded")):
            report = self.command("classify")
        self.assertEqual((report["processed"], report["predictions"], report["skipped"], report["errors"]),
                         (0, 0, 3, []))
        self.assertEqual(len(self.queue()), 3)
        self.assertEqual(self.predictions(), [])

        self.sync()
        report = self.command("classify")
        self.assertEqual((report["processed"], report["skipped"]), (3, 0))
        self.assertEqual(self.queue(), [])

    def test_spectrum_waits_for_its_dark_file(self):
        fixtures.make_real_session(self.home)
        dark_file = self.paths.dark_measurements / fixtures.DARK_FILE
        self.sync()
        hidden = dark_file.with_suffix(".hidden")
        dark_file.rename(hidden)
        failed = self.sync()
        self.assertEqual([entry["stage"] for entry in failed["errors"]], ["dark"])
        self.assertEqual(set(self.states().values()), {"uploaded"})

        report = self.command("classify")
        self.assertEqual((report["processed"], report["skipped"], report["errors"]), (0, 3, []))
        self.assertEqual(len(self.queue()), 3)

        hidden.rename(dark_file)
        self.sync(2)
        report = self.command("classify")
        self.assertEqual((report["processed"], report["skipped"]), (3, 0))

    def test_no_dark_spectrum_is_rejected_and_links_two_sources(self):
        session = fixtures.make_real_session(self.home, dark=False)
        self.sync(2)
        with mock.patch.object(self.catalog, "get_data", wraps=self.catalog.get_data) as downloads:
            report = self.command("classify")
            # Without a dark spectrum the answer does not depend on the raw file, so none is fetched.
            downloads.assert_not_called()
        self.assertEqual([(item["label"], item["outcome"], item["needs_review"]) for item in report["items"]],
                         [("REJECTED", "rejected", True)] * 3)
        model = self.catalog.get_record(MODEL_ALIAS)
        for capture_id in session["capture_ids"]:
            raw = self.catalog.get_record(raw_alias(capture_id))
            prediction = self.catalog.get_record(prediction_alias(capture_id, fixtures.MODEL_HASH))
            metadata = prediction.metadata
            self.assertEqual((metadata["reason"], metadata.get("dark"), metadata.get("top_class"),
                              metadata.get("confidence"), metadata["scores"], metadata["review"]["status"]),
                             ("no dark spectrum", None, None, None, {}, "pending"))
            self.assertEqual({(dep.relation, dep.id) for dep in prediction.deps if dep.direction == "out"},
                             {("der", raw.id), ("der", model.id)})
        self.assertEqual(len(self.catalog.list_collection(self.names.needs_review)), 3)
        self.assertEqual(set(self.states().values()), {"needs_review"})

    def test_dark_file_that_is_not_on_this_mac_counts_as_no_dark(self):
        session = fixtures.make_real_session(self.home)
        (self.paths.dark_measurements / fixtures.DARK_FILE).unlink()
        self.sync(2)
        report = self.command("classify")
        self.assertEqual([item["outcome"] for item in report["items"]], ["rejected"] * 3)
        metadata = self.catalog.get_record(prediction_alias(session["capture_ids"][0], fixtures.MODEL_HASH)).metadata
        self.assertEqual((metadata["reason"], metadata.get("dark")), ("no dark spectrum", None))

    def test_unusable_dark_file_is_rejected_and_still_linked(self):
        session = fixtures.make_real_session(self.home)
        dark_file = self.paths.dark_measurements / fixtures.DARK_FILE
        dark_file.write_text("\n".join(dark_file.read_text(encoding="utf-8").splitlines()[:-1]), encoding="utf-8")
        self.sync(2)
        report = self.command("classify")
        self.assertEqual([item["outcome"] for item in report["items"]], ["rejected"] * 3)
        prediction = self.catalog.get_record(prediction_alias(session["capture_ids"][0], fixtures.MODEL_HASH))
        dark = self.catalog.get_record(dark_alias(sha256_file(dark_file)[0]))
        self.assertEqual(prediction.metadata["reason"], "A complete finite 2048-pixel dark spectrum is required")
        self.assertEqual(prediction.metadata["dark"], {"record": dark.id, "sha256": dark.metadata["sha256"]})
        self.assertEqual(len([dep for dep in prediction.deps if dep.direction == "out"]), 3)

    def test_limit(self):
        self.uploaded_session()
        report = self.command("classify", "--limit", "2")
        self.assertEqual((report["processed"], report["remaining"], len(report["items"])), (2, 1, 2))
        self.assertEqual(len(self.queue()), 1)
        report = self.command("classify", "--limit", "2")
        self.assertEqual((report["processed"], report["remaining"]), (1, 0))
        refused = cli.invoke(self.ctx, ["classify", "--limit", "0"])
        self.assertEqual((refused["ok"], refused["kind"]), (False, "command"))

    def test_one_bad_spectrum_does_not_stop_the_job(self):
        session = self.uploaded_session()
        bad = self.catalog.get_record(raw_alias(session["capture_ids"][1]))
        stored = self.stored_file(bad.id)
        original = stored.read_bytes()
        stored.write_bytes(original + b" ")

        report = self.command("classify")
        self.assertEqual((report["processed"], report["predictions"]), (2, 2))
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in report["errors"]],
                         [(session["capture_ids"][1], "read")])
        self.assertIn("SHA-256 differs", report["errors"][0]["message"])
        # It stays in the queue, without a prediction, and is reported again by the next run.
        self.assertEqual(self.queue(), [bad.id])
        self.assertEqual(len(self.predictions()), 2)
        self.assertEqual(list(self.states().values()), ["accepted", "uploaded", "accepted"])
        again = self.command("classify")
        self.assertEqual((again["processed"], len(again["errors"])), (0, 1))

        stored.write_bytes(original)
        self.assertEqual(self.command("classify")["processed"], 1)
        # Nothing that was downloaded is kept.
        self.assertEqual(list((self.paths.staging / "classify").iterdir()), [])

    def test_raw_file_of_another_capture_or_no_json_is_an_error_for_that_spectrum(self):
        session = self.uploaded_session()
        records = [self.catalog.get_record(raw_alias(capture_id)) for capture_id in session["capture_ids"]]
        swapped = self.stored_file(records[0].id)
        swapped.write_bytes(self.stored_file(records[1].id).read_bytes())
        broken = self.stored_file(records[2].id)
        broken.write_text("{not json", encoding="utf-8")
        # The local demo knows nothing of checksums; say that these are the registered files.
        for record, path in ((records[0], swapped), (records[2], broken)):
            self.catalog.update_record(record.id, metadata={"raw_file": {"sha256": sha256_file(path)[0]}})
        report = self.command("classify")
        self.assertEqual(report["processed"], 1)
        self.assertEqual(sorted((entry["capture_id"], entry["stage"]) for entry in report["errors"]),
                         sorted([(session["capture_ids"][0], "read"), (session["capture_ids"][2], "read")]))
        messages = " ".join(entry["message"] for entry in report["errors"])
        self.assertIn("belongs to another capture", messages)
        self.assertIn("is not JSON", messages)

    def test_foreign_record_in_the_queue_is_reported(self):
        self.uploaded_session()
        note = self.catalog.create_record("a-note", "Not a spectrum", {"type": "note"}, self.names.sessions)
        self.catalog.link(self.names.to_classify, note.id)
        report = self.command("classify")
        self.assertEqual(report["processed"], 3)
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in report["errors"]], [(None, "read")])
        self.assertIn("a-note is in the queue but is not a raw spectrum record", report["errors"][0]["message"])
        self.assertEqual(self.queue(), [note.id])

    def test_simulated_spectra_say_so(self):
        support.write_dark(self.home)
        session = support.make_session(self.home, points=1, sample_id="S-042")
        self.sync(2)
        report = self.command("classify")
        self.assertEqual((report["processed"], report["errors"]), (1, []))
        prediction = self.catalog.get_record(prediction_alias(session["capture_ids"][0], fixtures.MODEL_HASH))
        self.assertEqual(prediction.title, "Prediction S-042 point 1 [simulated]")
        self.assertIs(prediction.metadata["simulated"], True)
        self.assertIn("simulated, not measured", prediction.description)

    def test_ledger_rows_still_tracking_an_upload_are_left_to_sync(self):
        session = fixtures.make_real_session(self.home)
        self.sync()
        # The transfers finish without this Mac's ledger hearing of it.
        for row in self.ctx.ledger.rows():
            self.catalog.transfer_status(row["upload_task_id"])
        for row in self.ctx.ledger.darks_to_advance(self.config.max_attempts):
            self.catalog.transfer_status(row["upload_task_id"])
        report = self.command("classify")
        self.assertEqual((report["processed"], report["errors"]), (3, []))
        self.assertEqual(set(self.states().values()), {"uploading"})

        result = self.sync()
        self.assertEqual((result["uploads_done"], result["predictions_found"], result["errors"]), (3, 3, []))
        self.assertEqual(list(self.states().values()), ["accepted"] * 3)
        self.assertEqual(self.ctx.ledger.get(session["capture_ids"][0])["upload_status"], "succeeded")

    def test_ledger_bound_to_another_catalog_is_not_touched(self):
        self.uploaded_session()
        self.ctx.ledger.bind_catalog("some-other-catalog")
        report = self.command("classify")
        self.assertEqual(report["processed"], 3)
        self.assertEqual({(row["state"], row["prediction_id"]) for row in self.ctx.ledger.rows()},
                         {("captured", None)})
        # The next syncs bind the ledger to this catalog again and find what is there.
        self.sync(3)
        self.assertEqual(list(self.states().values()), ["accepted"] * 3)
        self.assertEqual(self.kinds()["raman.prediction"], 3)


@needs_model
class DarkKinds(fixtures.HomeCase):
    """A spectrum is classified with a dark of its own kind only: real with real, simulated with simulated."""

    def rejected_for_the_dark(self, capture_id):
        prediction = self.catalog.get_record(prediction_alias(capture_id, fixtures.MODEL_HASH))
        self.assertEqual((prediction.metadata["outcome"], prediction.metadata["label"], prediction.metadata["dark"]),
                         ("rejected", "REJECTED", None))
        # Derived from the spectrum and the model only: no link to the dark it did not use.
        raw = self.catalog.get_record(raw_alias(capture_id))
        model = self.catalog.get_record(MODEL_ALIAS)
        self.assertEqual({(dep.relation, dep.id) for dep in prediction.deps if dep.direction == "out"},
                         {("der", raw.id), ("der", model.id)})
        return prediction

    def test_a_measured_spectrum_with_a_simulated_dark(self):
        session = fixtures.make_real_session(self.home)
        support.write_dark_description(self.paths.dark_measurements / fixtures.DARK_FILE, True)
        self.sync(2)
        report = self.command("classify")
        self.assertEqual((report["processed"], report["errors"]), (3, []))
        prediction = self.rejected_for_the_dark(session["capture_ids"][0])
        self.assertEqual(prediction.metadata["reason"],
                         "dark not used: the dark was taken in simulation; this spectrum is real")
        self.assertNotIn("raman.dark_spectrum", self.kinds())

        # The Review tab shows the spectrum without a dark, and says why, online and offline.
        shown = self.command("review-show", "--prediction", prediction.id)
        self.assertEqual((shown["spectrum"]["dark"], shown["spectrum"]["processed"]), (None, None))
        self.assertEqual(shown["item"]["reason"], prediction.metadata["reason"])
        self.assertEqual(shown["problems"],
                         ["The classifier used no dark spectrum for this prediction, so none is shown."])
        self.catalog.set_offline(True)
        shown = self.command("review-show", "--prediction", prediction.id)
        self.assertIsNone(shown["spectrum"]["dark"])
        self.assertIn("Dark not used: the dark was taken in simulation; this spectrum is real (%s)."
                      % fixtures.DARK_FILE, shown["problems"])

    def test_a_simulated_spectrum_with_a_real_dark_or_one_of_unknown_kind(self):
        support.write_dark(self.home, simulated=None)
        with_real_dark = support.make_session(self.home, points=1)
        unknown = support.write_dark(self.home, name="dark_20261113_101500_002.txt")
        (self.paths.dark_measurements / "dark_20261113_101500_002.json").write_text("{half", encoding="utf-8")
        with_unknown_dark = support.make_session(self.home, folder="1d_x_scan_other", points=1,
                                                 dark_file=unknown.name)
        self.sync(2)
        self.assertEqual(self.command("classify")["processed"], 2)
        self.assertEqual(self.rejected_for_the_dark(with_real_dark["capture_ids"][0]).metadata["reason"],
                         "dark not used: the dark was taken with the real spectrometer; this spectrum is simulated")
        self.assertEqual(self.rejected_for_the_dark(with_unknown_dark["capture_ids"][0]).metadata["reason"],
                         "dark not used: it is not known whether the dark was taken in simulation "
                         "(dark_20261113_101500_002.json cannot be read); this spectrum is simulated")
        self.assertEqual(set(self.states().values()), {"needs_review"})

    def test_an_imported_session_uses_its_dark_whatever_its_kind(self):
        dark = self.paths.dark_measurements / fixtures.DARK_FILE
        dark.parent.mkdir(parents=True)
        shutil.copyfile(fixtures.FIXTURES / fixtures.DARK_FILE, dark)
        support.write_dark_description(dark, True)
        legacy = support.make_home(self) / fixtures.SESSION_FOLDER
        legacy.mkdir()
        for name in fixtures.POINT_FILES:
            shutil.copyfile(fixtures.FIXTURES / name, legacy / name)
        self.command("import-session", "--folder", legacy, "--sample-id", "R6G-MB-plate", "--operator",
                     "Test Operator", "--location", "Bench 2", "--dark-file", fixtures.DARK_FILE)
        self.sync(2)
        report = self.command("classify")
        self.assertEqual([item["label"] for item in report["items"]],
                         [fixtures.EXPECTED_LABELS[name] for name in fixtures.POINT_FILES])
        dark_record = self.catalog.get_record(dark_alias(sha256_file(dark)[0]))
        self.assertEqual((dark_record.metadata["simulated"], dark_record.title),
                         (True, "Dark spectrum %s [simulated]" % fixtures.DARK_FILE))

    def test_a_dark_whose_file_never_arrives_sends_its_spectra_to_a_person(self):
        settings = load_settings(self.home)
        settings.set("sync.max_attempts", 2)
        settings.save()
        self.ctx.reload()
        session = fixtures.make_real_session(self.home)
        dark_file = self.paths.dark_measurements / fixtures.DARK_FILE
        self.sync()
        dark_file.unlink()
        self.sync()
        # One try left: the spectra wait for the file, which may still come back.
        report = self.command("classify")
        self.assertEqual((report["processed"], report["skipped"]), (0, 3))

        self.sync()
        report = self.command("classify")
        self.assertEqual((report["processed"], report["errors"]), (3, []))
        prediction = self.catalog.get_record(prediction_alias(session["capture_ids"][0], fixtures.MODEL_HASH))
        dark = self.catalog.get_record(dark_alias(prediction.metadata["dark"]["sha256"]))
        self.assertEqual((prediction.metadata["outcome"], prediction.metadata["reason"]),
                         ("rejected", "the dark spectrum %s was never uploaded: the file %s is missing"
                          % (fixtures.DARK_FILE, fixtures.DARK_FILE)))
        self.assertEqual(prediction.metadata["dark"], {"record": dark.id, "sha256": dark.metadata["sha256"]})
        self.assertEqual(set(self.states().values()), {"needs_review"})
        self.assertEqual(len(self.command("review-list")["items"]), 3)

    def test_classify_during_a_scan_leaves_a_point_still_being_written_alone(self):
        session = fixtures.make_real_session(self.home)
        point = fixtures.load_point("point_0004.json")
        point.update(point_id=9, capture_id="%s_9" % fixtures.SESSION_ID,
                     capture=json.loads(session["files"][0].read_text(encoding="utf-8"))["capture"])
        complete = json.dumps(point, indent=4, sort_keys=True)
        writing = session["folder"] / "point_0009.json"
        writing.write_text(complete[: len(complete) // 2], encoding="utf-8")
        self.sync(2)
        report = self.command("classify")
        self.assertEqual((report["processed"], report["errors"]), (3, []))
        self.assertIsNone(self.ctx.ledger.get(point["capture_id"]))

        writing.write_text(complete, encoding="utf-8")
        self.sync(2)
        report = self.command("classify")
        self.assertEqual([(item["capture_id"], item["label"]) for item in report["items"]],
                         [(point["capture_id"], "methylene_blue")])


@needs_model
class KilledHalfWay(fixtures.HomeCase):
    threshold = 0.9
    CATALOG_CALLS = ("list_collection", "get_record", "get_data", "ensure_collection", "create_record", "link",
                     "unlink")

    def assert_nothing_made_twice(self, session, where=""):
        self.assertEqual(self.kinds(), {"raman.raw_spectrum": 3, "raman.dark_spectrum": 1, "raman.model": 1,
                                        "raman.prediction": 3}, where)
        self.assertEqual(self.queue(), [], where)
        waiting = self.catalog.get_record(prediction_alias(session["capture_ids"][0], fixtures.MODEL_HASH))
        self.assertEqual([entry.id for entry in self.catalog.list_collection(self.names.needs_review)],
                         [waiting.id], where)
        for capture_id in session["capture_ids"]:
            raw = self.catalog.get_record(raw_alias(capture_id))
            derived = [dep.id for dep in raw.deps if dep.direction == "in"]
            self.assertEqual(len(derived), 1, where)
            prediction = self.catalog.get_record(derived[0])
            self.assertEqual(prediction.alias, prediction_alias(capture_id, fixtures.MODEL_HASH), where)
            self.assertEqual(len([dep for dep in prediction.deps if dep.direction == "out"]), 3, where)

    def test_killed_between_saving_the_prediction_and_leaving_the_queue(self):
        session = self.uploaded_session()
        with mock.patch.object(self.catalog, "unlink", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.run_job()
        # The first prediction is saved and waits for a person; its spectrum is still queued.
        first = self.catalog.get_record(prediction_alias(session["capture_ids"][0], fixtures.MODEL_HASH))
        self.assertEqual(len(self.queue()), 3)
        self.assertEqual(len(self.predictions()), 1)

        report = self.run_job()
        self.assertEqual((report["processed"], report["predictions"], report["errors"]), (3, 2, []))
        self.assertEqual([item["created"] for item in report["items"]], [False, True, True])
        # It is the prediction of the run that died, not a second one.
        self.assertEqual(self.catalog.get_record(first.id).metadata, first.metadata)
        self.assert_nothing_made_twice(session)

    def test_prediction_the_raw_record_does_not_show_is_still_not_made_twice(self):
        # A catalog may report a dependency without its alias; then only the alias rule protects.
        session = self.uploaded_session()
        with mock.patch.object(self.catalog, "unlink", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.run_job()
        first = self.catalog.get_record(prediction_alias(session["capture_ids"][0], fixtures.MODEL_HASH))
        with mock.patch.object(job._Job, "_existing", return_value=None):
            report = self.run_job()
        self.assertEqual((report["processed"], report["predictions"], report["errors"]), (3, 2, []))
        self.assertEqual(self.catalog.get_record(first.id).metadata, first.metadata)
        self.assert_nothing_made_twice(session)

    def run_until_death(self, die_at, after_the_call):
        """A job in which catalog call number `die_at` is fatal. Returns False when it ran to the end."""
        calls = 0

        def fatal(real):
            def call(*arguments, **options):
                nonlocal calls
                calls += 1
                mine = calls
                if mine == die_at and not after_the_call:
                    raise Crash()
                result = real(*arguments, **options)
                if mine == die_at and after_the_call:
                    raise Crash()
                return result

            return call

        with contextlib.ExitStack() as stack:
            for name in self.CATALOG_CALLS:
                stack.enter_context(mock.patch.object(self.catalog, name, fatal(getattr(self.catalog, name))))
            try:
                self.run_job()
            except Crash:
                return True
        return False

    def test_dying_at_any_catalog_call_makes_nothing_twice(self):
        session = self.uploaded_session()
        template = self.home / "catalog_before_the_job"
        shutil.copytree(self.paths.local_demo, template)
        deaths = 0
        found_again = 0
        for after_the_call in (False, True):
            die_at = 0
            while True:
                die_at += 1
                shutil.rmtree(self.paths.local_demo)
                shutil.copytree(template, self.paths.local_demo)
                if not self.run_until_death(die_at, after_the_call):
                    break
                deaths += 1
                where = "died at call %d, %s it" % (die_at, "after" if after_the_call else "before")
                # Nothing of the dead run is left in the work folder.
                self.assertEqual(list(self.work.iterdir()) if self.work.exists() else [], [], where)
                report = self.run_job()
                self.assertEqual(report["errors"], [], where)
                found_again += len([item for item in report["items"] if not item["created"]])
                self.assert_nothing_made_twice(session, where)
                again = self.run_job()
                self.assertEqual((again["processed"], again["errors"]), (0, []), where)
        # One run makes some twenty catalog calls; each was fatal twice.
        self.assertGreaterEqual(deaths, 30)
        # Several of those deaths left a saved prediction behind that the next run had to find.
        self.assertGreaterEqual(found_again, 6)


@needs_model
class Connection(fixtures.HomeCase):
    threshold = 0.9

    def test_unreachable_catalog_is_the_offline_failure(self):
        self.uploaded_session()
        self.catalog.set_offline(True)
        result = cli.invoke(self.ctx, ["classify"])
        self.assertEqual((result["ok"], result["kind"]), (False, "offline"))
        self.catalog.set_offline(False)
        self.assertEqual(len(self.queue()), 3)

    def test_connection_lost_half_way_keeps_what_was_done(self):
        session = self.uploaded_session()
        real = self.catalog.get_data
        downloads = []

        def lose_the_connection(record, dest_dir):
            # The dark spectrum and the first raw file arrive; then the connection is gone.
            if len(downloads) == 2:
                raise CatalogOffline("connection lost")
            downloads.append(record)
            return real(record, dest_dir)

        with mock.patch.object(self.catalog, "get_data", lose_the_connection):
            report = self.command("classify")
        self.assertEqual((report["processed"], report["predictions"], report["remaining"]), (1, 1, 2))
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in report["errors"]], [(None, "catalog")])
        self.assertEqual(len(self.queue()), 2)
        self.assertEqual(list(self.states().values()), ["needs_review", "uploaded", "uploaded"])

        report = self.command("classify")
        self.assertEqual((report["processed"], report["errors"]), (2, []))
        self.assertEqual(self.kinds()["raman.prediction"], 3)
        self.assertEqual(self.ctx.ledger.get(session["capture_ids"][2])["state"], "accepted")


    def test_downloads_that_keep_failing_stop_the_run(self):
        session = self.uploaded_session()
        real = self.catalog.get_data
        raws = [self.catalog.get_record(raw_alias(capture_id)).id for capture_id in session["capture_ids"]]

        def transfer_never_finishes(record, dest_dir):
            if record in raws[:2]:
                raise CatalogError("The download of %s did not finish within 600 s" % record)
            return real(record, dest_dir)

        with mock.patch.object(self.catalog, "get_data", transfer_never_finishes):
            report = self.run_job()
        # Two in a row: the third spectrum is not even tried, although its download would work.
        self.assertEqual((report["processed"], report["remaining"]), (0, 1))
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in report["errors"]],
                         [(session["capture_ids"][0], "read"), (session["capture_ids"][1], "read"), (None, "catalog")])
        self.assertIn("2 downloads in a row failed", report["errors"][2]["message"])
        self.assertEqual(len(self.queue()), 3)

        def every_other_transfer_fails(record, dest_dir):
            if record in (raws[0], raws[2]):
                raise CatalogError("The download of %s failed" % record)
            return real(record, dest_dir)

        with mock.patch.object(self.catalog, "get_data", every_other_transfer_fails):
            report = self.run_job()
        # Failures with a download that works in between are those spectra's problem only.
        self.assertEqual((report["processed"], report["remaining"]), (1, 0))
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in report["errors"]],
                         [(session["capture_ids"][0], "read"), (session["capture_ids"][2], "read")])
        self.assertEqual(self.queue(), [raws[0], raws[2]])


if __name__ == "__main__":
    unittest.main()
