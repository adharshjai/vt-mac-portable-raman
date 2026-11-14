"""The review commands on the local demo catalog: what a reviewer is shown, what a review
changes in the catalog and in the ledger, and reviews made while the catalog is unreachable.

The prediction under review is the real point 1 of fixtures/: the model's answer is r6g with a
score of 0.88, which a threshold of 0.9 sends to a person.
"""

import json
import re
import shutil
import threading
import unittest
from argparse import Namespace
from unittest import mock

import fixtures
import support
from raman_datafed import classifier, cli, commands_core, review, sync
from raman_datafed.catalog import CatalogOffline, prediction_alias, raw_alias
from raman_datafed.ledger import item_from_row
from raman_datafed.local_catalog import LocalDemoCatalog

needs_model = fixtures.needs_model
REVIEW_EXTRAS = {"prediction_id", "scores", "reason", "threshold", "model"}


class Crash(BaseException):
    """Stands for the process dying: nothing in the data layer catches it."""


class Axis(unittest.TestCase):
    def test_axis_is_the_calibration_table_of_the_controller(self):
        source = support.DATA_LAYER.parent / "laser_test" / "main.cpp"
        if not source.is_file():
            self.skipTest("the controller source is not next to the data layer (a deployed copy)")
        table = re.search(r"static const double kRamanShift\[kNumPixels\] = \{(.*?)\};",
                          source.read_text(encoding="utf-8"), re.S)
        self.assertIsNotNone(table)
        values = [float(text) for text in table.group(1).split(",") if text.strip()]
        self.assertEqual(list(review.raman_shift_axis()), values)

    def test_axis_is_what_the_controller_writes_next_to_a_real_dark_spectrum(self):
        axis = review.raman_shift_axis()
        self.assertEqual((len(axis), axis[0], axis[-1]), (2048, 57.42448267, 3234.590358))
        self.assertTrue(all(later > earlier for earlier, later in zip(axis, axis[1:])))
        rows = (fixtures.FIXTURES / fixtures.DARK_FILE).read_text(encoding="utf-8").splitlines()
        written = [float(row.split("\t")[1]) for row in rows if row[:1].isdigit()]
        self.assertEqual(len(written), 2048)
        for value, column in zip(axis, written):
            self.assertAlmostEqual(value, column, places=8)


@needs_model
class ReviewCase(fixtures.HomeCase):
    threshold = 0.9

    def setUp(self):
        super().setUp()
        self.session = self.uploaded_session()
        self.command("classify")
        self.capture_id = self.session["capture_ids"][0]
        self.prediction_id = self.ctx.ledger.get(self.capture_id)["prediction_id"]

    def saved(self, prediction_id=None):
        return self.catalog.get_record(prediction_id or self.prediction_id).metadata

    def row(self, capture_id=None):
        return self.ctx.ledger.get(capture_id or self.capture_id)

    def review(self, decision, *options, prediction=None, reviewer="Test Reviewer"):
        return self.command("review-set", "--prediction", prediction or self.prediction_id, "--decision", decision,
                            "--reviewer", reviewer, *options)


class ReviewList(ReviewCase):
    def test_item_is_the_list_item_plus_what_a_reviewer_needs(self):
        listing = self.command("review-list")
        self.assertIs(listing["online"], True)
        (item,) = listing["items"]
        listed = {entry["capture_id"]: entry for entry in self.command("list")["items"]}[self.capture_id]
        self.assertEqual(set(item), set(listed) | REVIEW_EXTRAS)
        self.assertEqual({key: item[key] for key in listed}, listed)
        self.assertEqual((item["state"], item["prediction_id"], item["reason"], item["threshold"]),
                         ("needs_review", self.prediction_id, "Low pure-class confidence", 0.9))
        self.assertEqual(list(item["scores"]), fixtures.CLASSES)
        self.assertAlmostEqual(item["scores"]["r6g"], 0.8801429083914941, delta=fixtures.SCORE_TOLERANCE)
        self.assertEqual(item["model"], {"name": "pca_svm_pure_20260922", "sha256": fixtures.MODEL_HASH,
                                         "record": self.saved()["model"]["record"]})
        self.assertEqual(item["review"], {"status": "pending", "label": None, "reviewer": None, "time_utc": None})

    def test_unreachable_catalog_still_lists_what_the_ledger_knows(self):
        self.catalog.set_offline(True)
        listing = self.command("review-list")
        self.assertIs(listing["online"], False)
        (item,) = listing["items"]
        self.assertEqual((item["capture_id"], item["prediction_id"], item["prediction"]["top_class"]),
                         (self.capture_id, self.prediction_id, "r6g"))
        self.assertEqual((item["scores"], item["reason"], item["threshold"], item["model"]), (None, None, None, None))

    def test_review_made_somewhere_else_leaves_the_list_and_reaches_the_ledger(self):
        done = {"status": "confirmed", "label": "r6g", "reviewer": "Somebody Else",
                "time_utc": "2026-11-13T16:00:00.000Z", "comment": ""}
        sync.apply_review(self.catalog, self.config, self.prediction_id, done)
        self.assertEqual(self.row()["state"], "needs_review")
        self.assertEqual(self.command("review-list")["items"], [])
        self.assertEqual((self.row()["state"], json.loads(self.row()["review_json"])["reviewer"]),
                         ("reviewed", "Somebody Else"))

    def test_nothing_waiting_does_not_ask_the_catalog(self):
        self.review("confirm")
        with mock.patch.object(self.catalog, "get_record", side_effect=AssertionError("asked the catalog")):
            self.assertEqual(self.command("review-list"), {"ok": True, "items": [], "online": None})

    def test_after_a_change_of_catalog_nothing_is_reviewed_until_the_next_sync(self):
        commands_core.cmd_config(Namespace(set=[["datafed.root_alias=second-root"]]), self.ctx)
        self.assertEqual(self.command("review-list"), {"ok": True, "items": [], "online": None})
        for arguments in (("review-show", "--prediction", self.prediction_id),
                          ("review-set", "--prediction", self.prediction_id, "--decision", "confirm",
                           "--reviewer", "Test Reviewer")):
            refusal = self.refused(*arguments)
            self.assertEqual(refusal["kind"], "command")
            self.assertIn("catalog settings changed", refusal["error"])
            self.assertIn("Run sync first", refusal["detail"])
        self.assertEqual(self.saved()["review"]["status"], "pending")


class ReviewShow(ReviewCase):
    def expected_curves(self):
        point = fixtures.load_point("point_0001.json")
        dark = classifier.load_dark(fixtures.FIXTURES / fixtures.DARK_FILE)
        features = classifier.preprocess(point["intensity"], dark)[0]
        return point["intensity"], [int(value) for value in dark], [round(float(value), 6) for value in features]

    def assert_real_spectrum(self, spectrum, source):
        intensity, dark, processed = self.expected_curves()
        self.assertEqual(set(spectrum), {"raman_shift", "intensity", "dark", "processed", "processed_start", "source"})
        self.assertEqual([len(spectrum[key]) for key in ("raman_shift", "intensity", "dark", "processed")], [2048] * 4)
        self.assertEqual(spectrum["raman_shift"], list(review.raman_shift_axis()))
        self.assertEqual(spectrum["intensity"], intensity)
        self.assertEqual(spectrum["dark"], dark)
        # The curve is aligned with the axis: the model never sees the first 90 pixels.
        self.assertEqual((spectrum["processed_start"], spectrum["processed"][:90]), (90, [None] * 90))
        self.assertEqual(spectrum["processed"][90:], processed)
        self.assertEqual((min(processed), max(processed)), (0.0, 1.0))
        self.assertEqual(spectrum["source"], source)

    def test_item_spectrum_provenance_and_classes(self):
        shown = self.command("review-show", "--prediction", self.prediction_id)
        self.assertEqual(set(shown), {"ok", "item", "spectrum", "provenance", "classes", "online", "problems"})
        self.assertEqual(shown["item"], self.command("review-list")["items"][0])
        self.assert_real_spectrum(shown["spectrum"], "local file")
        saved = self.saved()
        self.assertEqual([(entry["id"], entry["type"], entry["role"], entry["relation"], entry["label"])
                          for entry in shown["provenance"]],
                         [(saved["raw_record"], "raman.raw_spectrum", "source", "der", "is derived from"),
                          (saved["model"]["record"], "raman.model", "source", "der", "is derived from"),
                          (saved["dark"]["record"], "raman.dark_spectrum", "source", "der", "is derived from")])
        self.assertEqual(shown["provenance"][2]["info"]["file"], fixtures.DARK_FILE)
        self.assertEqual((shown["classes"], shown["online"], shown["problems"]), (fixtures.CLASSES, True, []))
        json.dumps(shown, allow_nan=False)

    def test_alias_names_the_prediction_too(self):
        by_alias = self.command("review-show", "--prediction", prediction_alias(self.capture_id, fixtures.MODEL_HASH))
        self.assertEqual(by_alias["item"]["prediction_id"], self.prediction_id)

    def test_files_that_are_no_longer_on_this_mac_come_from_the_catalog(self):
        shutil.rmtree(self.session["folder"])
        (self.paths.dark_measurements / fixtures.DARK_FILE).unlink()
        shown = self.command("review-show", "--prediction", self.prediction_id)
        self.assert_real_spectrum(shown["spectrum"], "catalog")
        self.assertEqual(shown["problems"], [])
        # Nothing that was fetched for the plot is kept.
        self.assertEqual(list(self.paths.staging.glob("review-*")), [])

    def test_another_dark_file_of_the_same_name_is_not_shown_as_the_one_used(self):
        support.write_dark(self.home, name=fixtures.DARK_FILE)
        shown = self.command("review-show", "--prediction", self.prediction_id)
        self.assert_real_spectrum(shown["spectrum"], "local file")

    def test_unreachable_catalog_shows_what_is_on_this_mac(self):
        self.catalog.set_offline(True)
        shown = self.command("review-show", "--prediction", self.prediction_id)
        self.assertIs(shown["online"], False)
        self.assertEqual((shown["item"]["capture_id"], shown["item"]["prediction"]["top_class"],
                          shown["item"]["scores"]), (self.capture_id, "r6g", None))
        self.assert_real_spectrum(shown["spectrum"], "local file")
        self.assertEqual((shown["provenance"], shown["classes"]), ([], fixtures.CLASSES))
        self.assertEqual(len(shown["problems"]), 1)
        self.assertIn("cannot be reached", shown["problems"][0])

        shutil.rmtree(self.session["folder"])
        shown = self.command("review-show", "--prediction", self.prediction_id)
        self.assertIsNone(shown["spectrum"])
        self.assertIn("not on this Mac and the catalog cannot be reached", shown["problems"][0])
        # A prediction this ledger has never seen cannot be shown without the catalog.
        self.assertEqual(self.refused("review-show", "--prediction", "d/404")["kind"], "offline")

    def test_what_is_not_a_prediction(self):
        raw = self.catalog.get_record(raw_alias(self.capture_id))
        refusal = self.refused("review-show", "--prediction", raw.id)
        self.assertEqual((refusal["kind"], refusal["error"]), ("command", "%s is not a prediction record" % raw.id))
        self.assertEqual(self.refused("review-show", "--prediction", "d/404")["kind"], "not_found")
        self.assertEqual(self.refused("review-show")["kind"], "usage")


@needs_model
class ReviewShowRejected(fixtures.HomeCase):
    def test_spectrum_without_a_dark_has_no_processed_curve(self):
        session = self.uploaded_session(dark=False)
        self.command("classify")
        waiting = self.command("review-list")["items"]
        self.assertEqual(len(waiting), 3)
        shown = self.command("review-show", "--prediction", waiting[0]["prediction_id"])
        self.assertEqual((shown["item"]["reason"], shown["item"]["scores"], shown["item"]["prediction"]["label"]),
                         ("no dark spectrum", {}, "REJECTED"))
        spectrum = shown["spectrum"]
        self.assertEqual(spectrum["intensity"],
                         json.loads((session["folder"] / waiting[0]["point_file"]).read_text())["intensity"])
        self.assertEqual((spectrum["dark"], spectrum["processed"], len(spectrum["raman_shift"])), (None, None, 2048))
        self.assertEqual([entry["type"] for entry in shown["provenance"]], ["raman.raw_spectrum", "raman.model"])
        # A rejected prediction has no scores to read the classes from; the model record has them.
        self.assertEqual((shown["classes"], shown["problems"]), (fixtures.CLASSES, []))

        # Confirming a rejection keeps the model's word for it.
        done = self.command("review-set", "--prediction", waiting[0]["prediction_id"], "--decision", "confirm",
                            "--reviewer", "Test Reviewer")
        self.assertEqual((done["item"]["review"]["status"], done["item"]["review"]["label"]),
                         ("confirmed", "REJECTED"))

    def test_saturated_spectrum_shows_raw_and_dark_only(self):
        session = fixtures.make_real_session(self.home)
        point = json.loads(session["files"][0].read_text(encoding="utf-8"))
        point["intensity"][500] = 65535
        session["files"][0].write_text(json.dumps(point), encoding="utf-8")
        self.sync(2)
        self.command("classify")
        (waiting,) = self.command("review-list")["items"]
        self.assertEqual(waiting["reason"], "Invalid, incomplete or saturated spectrum")
        spectrum = self.command("review-show", "--prediction", waiting["prediction_id"])["spectrum"]
        self.assertEqual((spectrum["intensity"][500], len(spectrum["dark"]), spectrum["processed"]),
                         (65535, 2048, None))


class ReviewSet(ReviewCase):
    def test_confirm_takes_the_models_best_answer(self):
        before = self.saved()
        done = self.review("confirm", "--comment", "  bands at 612 and 1360 ", reviewer="  Test   Reviewer ")
        self.assertIs(done["queued"], False)
        after = self.saved()
        self.assertEqual({key: after["review"][key] for key in ("status", "label", "reviewer", "comment")},
                         {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer",
                          "comment": "bands at 612 and 1360"})
        self.assertRegex(after["review"]["time_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertIs(after["needs_review"], False)
        # Only the review and needs_review changed.
        for key in before:
            if key not in ("review", "needs_review"):
                self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.catalog.list_collection(self.names.needs_review), [])

        item = done["item"]
        self.assertEqual((item["state"], item["state_label"], item["prediction_id"]),
                         ("reviewed", "Reviewed", self.prediction_id))
        self.assertEqual(item["review"], {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer",
                                          "time_utc": after["review"]["time_utc"]})
        self.assertEqual((item["prediction"]["needs_review"], item["prediction"]["label"]), (False, "UNKNOWN"))
        self.assertEqual(item["scores"], before["scores"])
        self.assertEqual(item, dict(item_from_row(self.row()), **{key: item[key] for key in REVIEW_EXTRAS}))
        self.assertEqual(self.command("review-list")["items"], [])

    def test_relabel_needs_a_class_of_the_model_or_unknown(self):
        done = self.review("relabel", "--label", "adenine")
        self.assertEqual((self.saved()["review"]["status"], self.saved()["review"]["label"]), ("relabelled", "adenine"))
        self.assertEqual(done["item"]["review"]["label"], "adenine")
        # A person can change their mind: the later review replaces the earlier one.
        for typed in ("unknown", "Unknown", "UNKNOWN"):
            self.review("relabel", "--label", typed)
            self.assertEqual(self.saved()["review"]["label"], "UNKNOWN")
        self.assertEqual(self.row()["state"], "reviewed")

        refusal = self.refused("review-set", "--prediction", self.prediction_id, "--decision", "relabel",
                               "--label", "gold", "--reviewer", "Test Reviewer")
        self.assertEqual((refusal["kind"], refusal["error"]), ("command", "gold is not a class of the model"))
        self.assertEqual(refusal["detail"], "One of: r6g, methylene_blue, adenine, di_water, unknown.")
        refusal = self.refused("review-set", "--prediction", self.prediction_id, "--decision", "relabel",
                               "--reviewer", "Test Reviewer")
        self.assertEqual(refusal["error"], "relabel needs --label")
        for decision in ("confirm", "reject"):
            refusal = self.refused("review-set", "--prediction", self.prediction_id, "--decision", decision,
                                   "--label", "adenine", "--reviewer", "Test Reviewer")
            self.assertIn("--label goes with --decision relabel only", refusal["error"])
        self.assertEqual(self.saved()["review"]["label"], "UNKNOWN")

    def test_reject_stores_no_label(self):
        self.review("confirm")
        done = self.review("reject", "--comment", "cosmic ray")
        review_now = self.saved()["review"]
        self.assertEqual((review_now["status"], review_now.get("label"), review_now["comment"]),
                         ("rejected", None, "cosmic ray"))
        self.assertEqual((done["item"]["state"], done["item"]["review"]["status"], done["item"]["review"]["label"]),
                         ("reviewed", "rejected", None))
        self.assertEqual((self.saved()["label"], self.saved()["outcome"]), ("UNKNOWN", "low_confidence"))

    def test_reviewer_and_decision_are_required(self):
        base = ("review-set", "--prediction", self.prediction_id)
        refusal = self.refused(*base, "--decision", "confirm", "--reviewer", "   ")
        self.assertEqual((refusal["kind"], refusal["error"]), ("command", "A reviewer name is required"))
        self.assertEqual(self.refused(*base, "--decision", "confirm")["kind"], "usage")
        self.assertEqual(self.refused(*base, "--reviewer", "Test Reviewer")["kind"], "usage")
        self.assertEqual(self.refused(*base, "--decision", "approve", "--reviewer", "Test Reviewer")["kind"], "usage")
        with self.assertRaisesRegex(Exception, "confirm, relabel or reject"):
            review.cmd_review_set(Namespace(prediction=self.prediction_id, decision="approve",
                                            reviewer="Test Reviewer"), self.ctx)
        self.assertEqual(self.saved()["review"]["status"], "pending")
        self.assertEqual(self.row()["state"], "needs_review")

    def test_a_prediction_that_did_not_ask_for_a_review_can_be_corrected(self):
        capture_id = self.session["capture_ids"][1]
        prediction_id = self.row(capture_id)["prediction_id"]
        self.assertEqual((self.row(capture_id)["state"], self.saved(prediction_id)["review"]["status"]),
                         ("accepted", "not_required"))
        done = self.review("relabel", "--label", "adenine", prediction=prediction_id)
        saved = self.saved(prediction_id)
        self.assertEqual((saved["review"]["status"], saved["review"]["label"], saved["label"], saved["outcome"]),
                         ("relabelled", "adenine", "methylene_blue", "confident"))
        self.assertEqual((done["item"]["state"], self.row(capture_id)["state"]), ("reviewed", "reviewed"))

    def test_what_is_not_a_prediction_cannot_be_reviewed(self):
        raw = self.catalog.get_record(raw_alias(self.capture_id))
        refusal = self.refused("review-set", "--prediction", raw.id, "--decision", "confirm",
                               "--reviewer", "Test Reviewer")
        self.assertIn("is not a prediction record", refusal["error"])
        self.assertNotIn("review", self.catalog.get_record(raw.id).metadata)
        self.assertEqual(self.refused("review-set", "--prediction", "d/404", "--decision", "confirm",
                                      "--reviewer", "Test Reviewer")["kind"], "not_found")


class ReviewWhileOffline(ReviewCase):
    def test_review_made_offline_is_queued_and_sent_by_the_next_sync(self):
        self.command("offline", "--on")
        done = self.review("relabel", "--label", "adenine", "--comment", "seen offline")
        self.assertIs(done["queued"], True)
        item = done["item"]
        self.assertEqual((item["state"], item["review"]["status"], item["review"]["label"], item["review"]["queued"]),
                         ("reviewed", "relabelled", "adenine", True))
        self.assertEqual((item["prediction_id"], item["scores"]), (self.prediction_id, None))
        status = self.command("status")
        self.assertEqual((status["online"], status["reviews_queued"], status["sync_work"]), (False, 1, 1))
        self.assertEqual(self.command("review-list")["items"], [])
        # A class the model does not have is refused offline as well, from the model on this Mac.
        refusal = self.refused("review-set", "--prediction", self.prediction_id, "--decision", "relabel",
                               "--label", "gold", "--reviewer", "Test Reviewer")
        self.assertEqual(refusal["error"], "gold is not a class of the model")

        still_offline = self.sync()
        self.assertEqual((still_offline["online"], still_offline["reviews_sent"]), (False, 0))

        self.command("offline", "--off")
        # Until the sync the review exists on this Mac only.
        self.assertEqual((self.saved()["review"]["status"], self.saved()["needs_review"]), ("pending", True))
        result = self.sync()
        self.assertEqual((result["reviews_sent"], result["errors"]), (1, []))
        saved = self.saved()
        self.assertEqual({key: saved["review"][key] for key in ("status", "label", "reviewer", "comment")},
                         {"status": "relabelled", "label": "adenine", "reviewer": "Test Reviewer",
                          "comment": "seen offline"})
        self.assertIs(saved["needs_review"], False)
        self.assertEqual(self.catalog.list_collection(self.names.needs_review), [])
        row = self.row()
        self.assertEqual((row["state"], row["review_queued"], json.loads(row["review_json"])),
                         ("reviewed", 0, saved["review"]))
        status = self.command("status")
        self.assertEqual((status["reviews_queued"], status["sync_work"]), (0, 0))
        after = self.sync()
        self.assertEqual((after["reviews_sent"], after["reviews_found"], after["errors"]), (0, 0, []))

    def test_confirm_offline_uses_the_answer_the_ledger_remembers(self):
        self.catalog.set_offline(True)
        done = self.review("confirm")
        self.assertEqual((done["queued"], done["item"]["review"]["label"]), (True, "r6g"))
        # A later review of the same prediction replaces the one that still waits.
        self.review("reject")
        self.assertEqual(len(self.ctx.ledger.queued_reviews(self.config.catalog_key)), 1)
        self.catalog.set_offline(False)
        self.assertEqual(self.sync()["reviews_sent"], 1)
        self.assertEqual((self.saved()["review"]["status"], self.saved()["review"].get("label")), ("rejected", None))

    def test_connection_lost_between_reading_and_writing_queues_the_review(self):
        with mock.patch.object(sync, "apply_review", side_effect=CatalogOffline("connection lost")):
            done = self.review("confirm")
        self.assertEqual((done["queued"], done["item"]["state"], done["item"]["review"]["queued"]),
                         (True, "reviewed", True))
        self.assertEqual(self.saved()["review"]["status"], "pending")
        self.assertEqual(self.sync()["reviews_sent"], 1)
        self.assertEqual((self.saved()["review"]["status"], self.saved()["review"]["label"]), ("confirmed", "r6g"))

    def test_review_made_online_takes_the_place_of_one_that_still_waits(self):
        self.catalog.set_offline(True)
        self.review("reject")
        self.catalog.set_offline(False)
        done = self.review("confirm")
        self.assertEqual((done["queued"], done["item"]["review"]["status"]), (False, "confirmed"))
        self.assertNotIn("queued", done["item"]["review"])
        self.assertEqual(self.ctx.ledger.queued_reviews(self.config.catalog_key), [])
        self.assertEqual(self.command("status")["reviews_queued"], 0)
        # The older review is not sent after the newer one.
        result = self.sync()
        self.assertEqual((result["reviews_sent"], result["errors"]), (0, []))
        self.assertEqual((self.saved()["review"]["status"], self.saved()["review"]["label"]), ("confirmed", "r6g"))
        self.assertEqual((self.row()["state"], self.row()["review_queued"]), ("reviewed", 0))

    def test_dying_while_replacing_a_waiting_review_leaves_the_newer_one_to_be_sent(self):
        self.catalog.set_offline(True)
        self.review("reject")
        self.catalog.set_offline(False)
        with mock.patch.object(sync, "apply_review", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.review("confirm")
        (waiting,) = self.ctx.ledger.queued_reviews(self.config.catalog_key)
        self.assertEqual(waiting["review"]["status"], "confirmed")
        self.assertEqual(self.sync()["reviews_sent"], 1)
        self.assertEqual((self.saved()["review"]["status"], self.saved()["review"]["label"]), ("confirmed", "r6g"))

    def test_unknown_prediction_cannot_be_queued(self):
        self.catalog.set_offline(True)
        refusal = self.refused("review-set", "--prediction", "d/404", "--decision", "confirm",
                               "--reviewer", "Test Reviewer")
        self.assertEqual(refusal["kind"], "offline")
        self.assertEqual(self.ctx.ledger.queued_reviews(self.config.catalog_key), [])

    def test_a_review_made_while_a_sync_sends_an_older_one_waits_for_it_and_stays(self):
        # The window and a sync are separate processes; here they are two threads, each with its
        # own connection to the ledger, as two processes would have.
        self.catalog.set_offline(True)
        self.review("confirm", reviewer="First Reviewer")
        self.catalog.set_offline(False)
        writing, go_on = threading.Event(), threading.Event()
        real_update = LocalDemoCatalog.update_record

        def slow_update(catalog, id_or_alias, metadata=None, deps_add=(), title=None):
            if (metadata or {}).get("review", {}).get("reviewer") == "First Reviewer":
                writing.set()
                go_on.wait(30)
            return real_update(catalog, id_or_alias, metadata=metadata, deps_add=deps_add, title=title)

        results = {}

        def in_own_context(name, arguments):
            with cli.Context(self.home) as ctx:
                results[name] = cli.invoke(ctx, arguments)

        with mock.patch.object(LocalDemoCatalog, "update_record", slow_update):
            syncing = threading.Thread(target=in_own_context, args=("sync", ["sync"]))
            syncing.start()
            self.assertTrue(writing.wait(30), "the sync began to send the queued review")
            reviewing = threading.Thread(target=in_own_context, args=("review", [
                "review-set", "--prediction", self.prediction_id, "--decision", "relabel", "--label", "di_water",
                "--reviewer", "Second Reviewer"]))
            reviewing.start()
            reviewing.join(0.5)
            self.assertTrue(reviewing.is_alive(), "the newer review waits until the older one is written")
            go_on.set()
            syncing.join(30)
            reviewing.join(30)
        self.assertEqual((results["sync"]["ok"], results["sync"]["reviews_sent"]), (True, 1))
        self.assertEqual((results["review"]["ok"], results["review"]["queued"]), (True, False))
        saved = self.saved()["review"]
        self.assertEqual((saved["status"], saved["label"], saved["reviewer"]), ("relabelled", "di_water",
                                                                                "Second Reviewer"))
        row = self.row()
        self.assertEqual((row["state"], row["review_queued"], json.loads(row["review_json"])["reviewer"]),
                         ("reviewed", 0, "Second Reviewer"))
        self.assertEqual(self.ctx.ledger.queued_reviews(self.config.catalog_key), [])
        # Catalog and ledger agree, so later syncs change nothing.
        self.assertEqual((self.sync()["reviews_sent"], self.saved()["review"]["reviewer"]), (0, "Second Reviewer"))


class AnotherComputer(ReviewCase):
    """A second HOME on the same catalog: its ledger has none of the captures."""

    def setUp(self):
        super().setUp()
        other_home = support.make_home(self)
        fixtures.use_shipped_model(other_home, self.threshold)
        self.other = support.context(self, other_home, catalog=self.catalog)

    def test_list_is_about_this_homes_captures_but_show_and_set_take_any_prediction(self):
        local = self.command("review-list")["items"][0]
        self.assertEqual(cli.invoke(self.other, ["review-list"]), {"ok": True, "items": [], "online": None})

        shown = cli.invoke(self.other, ["review-show", "--prediction", self.prediction_id])
        self.assertTrue(shown["ok"], shown)
        item = shown["item"]
        self.assertEqual(set(item), set(local))
        for key in ("capture_id", "session_id", "session_folder", "point_id", "point_file", "scan_mode", "sample_id",
                    "operator", "timestamp_utc", "simulated", "position_mm", "state", "state_label", "record_id",
                    "record_alias", "prediction", "review", "error", "prediction_id", "scores", "reason", "threshold",
                    "model"):
            self.assertEqual(item[key], local[key], key)
        self.assertIsNone(item["upload"])
        self.assertEqual((shown["spectrum"]["source"], shown["spectrum"]["intensity"], shown["problems"]),
                         ("catalog", fixtures.load_point("point_0001.json")["intensity"], []))
        self.assertEqual(len(shown["provenance"]), 3)

        done = cli.invoke(self.other, ["review-set", "--prediction", self.prediction_id, "--decision", "confirm",
                                       "--reviewer", "Second Reviewer"])
        self.assertEqual((done["ok"], done["queued"], done["item"]["state"]), (True, False, "reviewed"))
        self.assertEqual((done["item"]["review"]["status"], done["item"]["review"]["reviewer"]),
                         ("confirmed", "Second Reviewer"))
        self.assertEqual(self.other.ledger.count(), 0)

        # The computer that made the capture hears of it.
        self.assertEqual(self.row()["state"], "needs_review")
        self.assertEqual(self.sync()["reviews_found"], 1)
        self.assertEqual((self.row()["state"], json.loads(self.row()["review_json"])["reviewer"]),
                         ("reviewed", "Second Reviewer"))


@needs_model
class CommandLine(unittest.TestCase):
    """The commands run the way the controller runs them: as child processes."""

    def run_cli(self, home, *arguments, code=0):
        got, stdout, stderr = support.run_cli(home, *arguments)
        self.assertEqual(stdout.count("\n"), 1, stdout)
        self.assertEqual(got, code, stderr)
        return json.loads(stdout)

    def test_classify_and_review_print_one_json_object_each(self):
        home = support.make_home(self)
        fixtures.use_shipped_model(home, 0.9)
        fixtures.make_real_session(home)
        self.assertEqual(self.run_cli(home, "sync")["registered"], 3)
        self.assertEqual(self.run_cli(home, "sync")["uploads_done"], 3)

        report = self.run_cli(home, "classify")
        self.assertEqual(list(report)[:5], ["ok", "runner", "processed", "predictions", "skipped"])
        self.assertEqual((report["ok"], report["runner"], report["processed"], report["predictions"], report["errors"]),
                         (True, "local", 3, 3, []))
        self.assertEqual([item["state"] for item in self.run_cli(home, "list")["items"]].count("needs_review"), 1)

        (waiting,) = self.run_cli(home, "review-list")["items"]
        shown = self.run_cli(home, "review-show", "--prediction", waiting["prediction_id"])
        self.assertEqual([len(shown["spectrum"][key]) for key in ("raman_shift", "intensity", "dark", "processed")],
                         [2048] * 4)
        done = self.run_cli(home, "review-set", "--prediction", waiting["prediction_id"], "--decision", "relabel",
                            "--label", "unknown", "--reviewer", "Test Reviewer")
        self.assertEqual((done["ok"], done["queued"], done["item"]["review"]["label"]), (True, False, "UNKNOWN"))
        self.assertEqual(self.run_cli(home, "review-list")["items"], [])

        refusal = self.run_cli(home, "review-set", "--prediction", waiting["prediction_id"], "--decision", "confirm",
                               code=2)
        self.assertEqual((refusal["ok"], refusal["kind"]), (False, "usage"))
        refusal = self.run_cli(home, "review-show", "--prediction", "d/404", code=1)
        self.assertEqual((refusal["ok"], refusal["kind"]), (False, "not_found"))
        for command in ("classify", "review-list", "review-show", "review-set"):
            self.assertIn("usage:", self.run_cli(home, command, "-h")["help"])


if __name__ == "__main__":
    unittest.main()
