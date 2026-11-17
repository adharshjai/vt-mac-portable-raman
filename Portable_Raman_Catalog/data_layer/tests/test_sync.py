"""Sync against the local demo catalog: the whole flow, offline work, crashes, conflicts, retries."""

import contextlib
import fcntl
import functools
import hashlib
import os
import shutil
import sys
import types
import unittest
from argparse import Namespace
from unittest import mock

import support
from raman_catalog import commands_core, sync
from raman_catalog.catalog import (
    CatalogAuthRequired,
    CatalogOffline,
    dark_alias,
    ensure_layout,
    layout_for,
    raw_alias,
    session_alias,
)
from raman_catalog.cli import Context
from raman_catalog.config import Paths, load_settings, seconds_since
from raman_catalog.ledger import Ledger
from raman_catalog.local_catalog import LocalDemoCatalog
from raman_catalog.metadata import sha256_file


REAL_OPTIONAL_MODULE = sync._optional_module


class Crash(BaseException):
    """Stands for the process dying: nothing in the data layer catches it."""


def tree_digest(folder):
    """{relative path: content hash} of every file under a folder, the lock file aside."""
    return {
        str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != ".lock"
    }


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.config = load_settings(self.home)
        self.paths = Paths(self.home)
        self.catalog = LocalDemoCatalog(self.paths.local_demo)
        self.ledger = self.open_ledger()
        self.dark = support.write_dark(self.home)
        # These tests are about the core. The classifier part, when installed, has its own tests.
        patcher = mock.patch.object(sync, "_optional_module", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_ledger(self):
        ledger = Ledger(self.paths.data_catalog)
        self.addCleanup(ledger.close)
        return ledger

    def sync(self, ledger=None, **options):
        return sync.run_sync(self.home, self.config, ledger or self.ledger, self.catalog, **options)

    def states(self):
        return {row["capture_id"]: row["state"] for row in self.ledger.rows(newest_first=False)}

    def settle_uploads(self, session):
        """Two passes: the first registers and starts the uploads, the second sees them finished."""
        self.sync()
        self.sync()
        self.assertEqual(set(self.states().values()), {"uploaded"})
        return [self.catalog.get_record(raw_alias(capture_id)) for capture_id in session["capture_ids"]]

    def names(self):
        return layout_for(self.config.root_alias)


class WholeFlow(SyncCase):
    def test_capture_register_upload_classify_review(self):
        session = support.make_session(self.home, points=3)

        first = self.sync()
        self.assertEqual(
            (first["busy"], first["online"], first["ingested"], first["registered"], first["uploads_started"],
             first["uploads_done"]),
            (False, True, 3, 3, 3, 0),
        )
        self.assertEqual(first["errors"], [])
        # Uploads are never waited for: the pass returns while they are still running.
        self.assertEqual(set(self.states().values()), {"uploading"})

        names = self.names()
        raw = self.catalog.get_record(raw_alias(session["capture_ids"][1]))
        self.assertEqual(raw.metadata["type"], "raman.raw_spectrum")
        self.assertEqual(raw.metadata["capture_id"], session["capture_ids"][1])
        self.assertEqual(raw.metadata["position_mm"], {"x": 2.0, "y": 2.5, "z": 4.0})
        self.assertEqual(raw.metadata["raw_file"]["sha256"], sha256_file(session["files"][1])[0])
        self.assertIn("[simulated]", raw.title)
        dark = self.catalog.get_record(dark_alias(sha256_file(self.dark)[0]))
        self.assertEqual(raw.metadata["dark"], {"file": support.DARK_NAME, "sha256": sha256_file(self.dark)[0],
                                               "record": dark.id, "kind": "simulated"})
        self.assertEqual((dark.metadata["type"], dark.metadata["simulated"]), ("raman.dark_spectrum", True))
        self.assertEqual(dark.title, "Dark spectrum %s [simulated]" % support.DARK_NAME)
        in_session = self.catalog.list_collection(session_alias(session["session_id"]))
        self.assertEqual(len(in_session), 3)
        self.assertEqual({item.id for item in self.catalog.list_collection(names.to_classify)},
                         {item.id for item in in_session})
        self.assertEqual([item.id for item in self.catalog.list_collection(names.darks)], [dark.id])
        self.assertEqual(raw.data_size, 0)

        second = self.sync()
        self.assertEqual((second["ingested"], second["registered"], second["uploads_started"], second["uploads_done"]),
                         (0, 0, 0, 3))
        self.assertEqual(set(self.states().values()), {"uploaded"})
        fetched = self.catalog.get_data(raw.id, self.home / "fetched")
        self.assertEqual(fetched.read_bytes(), session["files"][1].read_bytes())
        self.assertGreater(self.catalog.get_record(dark.id).data_size, 0)
        self.assertEqual(self.ledger.get_dark(sha256_file(self.dark)[0])["state"], "uploaded")

        raws = [self.catalog.get_record(raw_alias(capture_id)) for capture_id in session["capture_ids"]]
        predictions = [
            support.save_prediction(self.catalog, self.config.root_alias, raws[0], "confident", "r6g", 0.93),
            support.save_prediction(self.catalog, self.config.root_alias, raws[1], "low_confidence", "adenine", 0.61),
            support.save_prediction(self.catalog, self.config.root_alias, raws[2], "background", "di_water", 0.97),
        ]
        third = self.sync()
        self.assertEqual((third["predictions_found"], third["reviews_found"], third["errors"]), (3, 0, []))
        self.assertEqual(list(self.states().values()), ["accepted", "needs_review", "accepted"])
        row = self.ledger.get(session["capture_ids"][1])
        self.assertEqual(row["prediction_id"], predictions[1].id)

        review = {"status": "relabelled", "label": "adenine", "reviewer": "Test Reviewer",
                  "time_utc": "2026-11-13T16:00:00.000Z", "comment": "peak at 730"}
        reviewed = sync.apply_review(self.catalog, self.config, predictions[1].id, review)
        self.assertEqual(reviewed.metadata["review"]["status"], "relabelled")
        self.assertIs(reviewed.metadata["needs_review"], False)
        # The model's own answer is never overwritten by a review.
        self.assertEqual((reviewed.metadata["label"], reviewed.metadata["outcome"]), ("UNKNOWN", "low_confidence"))
        self.assertEqual(reviewed.metadata["scores"], predictions[1].metadata["scores"])
        self.assertEqual(self.catalog.list_collection(names.needs_review), [])

        fourth = self.sync()
        self.assertEqual((fourth["predictions_found"], fourth["reviews_found"]), (0, 1))
        self.assertEqual(list(self.states().values()), ["accepted", "reviewed", "accepted"])

    def test_second_sync_changes_nothing(self):
        session = support.make_session(self.home, points=3)
        raws = self.settle_uploads(session)
        support.save_prediction(self.catalog, self.config.root_alias, raws[0], "confident")
        support.save_prediction(self.catalog, self.config.root_alias, raws[1], "low_confidence", confidence=0.5)
        self.sync()

        catalog_before = tree_digest(self.paths.local_demo)
        rows_before = self.ledger.rows()
        again = self.sync()

        counters = {key: value for key, value in again.items()
                    if isinstance(value, int) and not isinstance(value, bool)}
        self.assertEqual(set(counters.values()), {0}, counters)
        self.assertEqual(again["errors"], [])
        self.assertEqual(tree_digest(self.paths.local_demo), catalog_before)
        self.assertEqual(self.ledger.rows(), rows_before)

    def test_folders_that_are_not_sessions_are_ignored(self):
        support.make_session(self.home, folder="_backend_work", points=1)
        support.make_session(self.home, folder="no_session_json", points=1, with_session_json=False)
        (self.paths.scan_sessions / "_sim_stage.json").write_text("{}", encoding="utf-8")
        result = self.sync()
        self.assertEqual((result["ingested"], result["errors"]), (0, []))
        self.assertEqual(self.ledger.count(), 0)

    def test_single_capture_has_no_position(self):
        session = support.make_session(self.home, folder="single_20261113_101500_123", points=1,
                                       scan_mode="single", steps=None)
        self.sync()
        record = self.catalog.get_record(raw_alias(session["capture_ids"][0]))
        self.assertIsNone(record.metadata["position_steps"])
        self.assertIsNone(record.metadata["position_mm"])

    def test_dark_file_that_is_not_here_is_named_but_not_registered(self):
        session = support.make_session(self.home, points=1, dark_file="dark_elsewhere.txt")
        result = self.sync()
        self.assertEqual(result["errors"], [])
        record = self.catalog.get_record(raw_alias(session["capture_ids"][0]))
        self.assertEqual(record.metadata["dark"],
                         {"file": "dark_elsewhere.txt", "sha256": None, "record": None, "kind": None})
        self.assertEqual(self.catalog.list_collection(self.names().darks), [])

    def test_renamed_session_folder_is_followed(self):
        session = support.make_session(self.home, points=2)
        self.settle_uploads(session)
        session["folder"].rename(session["folder"].with_name("renamed_session"))
        result = self.sync()
        self.assertEqual((result["ingested"], result["errors"]), (0, []))
        self.assertEqual({row["session_folder"] for row in self.ledger.rows()}, {"renamed_session"})
        self.assertEqual(self.ledger.count(), 2)

    def test_copied_session_folder_is_reported_not_registered_twice(self):
        session = support.make_session(self.home, points=2)
        self.sync()
        shutil.copytree(session["folder"], session["folder"].with_name("copy_of_session"))
        result = self.sync()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual([entry["stage"] for entry in result["errors"]], ["metadata", "metadata"])
        self.assertIn("repeats the capture", result["errors"][0]["message"])
        self.assertEqual(self.ledger.count(), 2)


class OfflineThenReconnect(SyncCase):
    def test_captures_wait_in_the_ledger_and_are_registered_on_reconnect(self):
        self.catalog.set_offline(True)
        session = support.make_session(self.home, points=3)

        offline = self.sync()
        self.assertIs(offline["online"], False)
        self.assertEqual((offline["ingested"], offline["registered"], offline["errors"]), (3, 0, []))
        self.assertEqual(set(self.states().values()), {"captured"})
        self.assertFalse((self.paths.local_demo / "records").exists())

        support.add_points(session, 2, capture=support.capture_block())
        still_offline = self.sync()
        self.assertEqual((still_offline["online"], still_offline["ingested"]), (False, 2))
        rows = self.ledger.rows()
        self.assertEqual(len(rows), 5)
        # Being offline is not a failure of any capture: nothing is counted as an attempt.
        self.assertEqual({(row["state"], row["register_attempts"], row["error_message"]) for row in rows},
                         {("captured", 0, None)})

        self.catalog.set_offline(False)
        online = self.sync()
        self.assertEqual((online["online"], online["ingested"], online["registered"], online["uploads_started"]),
                         (True, 0, 5, 5))
        self.sync()
        self.assertEqual(set(self.states().values()), {"uploaded"})

    def test_connection_lost_in_the_middle_keeps_what_was_done(self):
        session = support.make_session(self.home, points=4)
        real_create = self.catalog.create_record
        created = []

        def create_then_lose_connection(alias, *args, **kwargs):
            if alias.startswith("rs-") and len(created) == 2:
                raise CatalogOffline("connection lost")
            record = real_create(alias, *args, **kwargs)
            if alias.startswith("rs-"):
                created.append(alias)
            return record

        with mock.patch.object(self.catalog, "create_record", create_then_lose_connection):
            result = self.sync()
        self.assertIs(result["online"], False)
        self.assertEqual((result["registered"], result["errors"]), (2, []))
        self.assertEqual(list(self.states().values()), ["uploading", "uploading", "captured", "captured"])

        result = self.sync()
        self.assertEqual((result["online"], result["registered"]), (True, 2))
        self.assertEqual(len(self.catalog.list_collection(session_alias(session["session_id"]))), 4)

    def test_review_made_offline_is_sent_by_the_next_sync(self):
        session = support.make_session(self.home, points=1)
        raws = self.settle_uploads(session)
        prediction = support.save_prediction(self.catalog, self.config.root_alias, raws[0], "low_confidence",
                                             confidence=0.4)
        self.sync()
        self.assertEqual(list(self.states().values()), ["needs_review"])

        self.catalog.set_offline(True)
        review = {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer",
                  "time_utc": "2026-11-13T16:00:00.000Z", "comment": ""}
        with self.assertRaises(CatalogOffline):
            sync.apply_review(self.catalog, self.config, prediction.id, review)
        self.assertEqual(self.ledger.queue_review(prediction.id, review), session["capture_ids"][0])
        row = self.ledger.get(session["capture_ids"][0])
        self.assertEqual((row["state"], row["review_queued"]), ("reviewed", 1))

        offline = self.sync()
        self.assertEqual((offline["online"], offline["reviews_sent"]), (False, 0))
        self.assertEqual(len(self.ledger.queued_reviews(self.config.catalog_key)), 1)

        self.catalog.set_offline(False)
        online = self.sync()
        self.assertEqual((online["reviews_sent"], online["errors"]), (1, []))
        self.assertEqual(self.ledger.queued_reviews(self.config.catalog_key), [])
        saved = self.catalog.get_record(prediction.id)
        self.assertEqual(saved.metadata["review"]["status"], "confirmed")
        self.assertIs(saved.metadata["needs_review"], False)
        self.assertEqual(self.catalog.list_collection(self.names().needs_review), [])
        row = self.ledger.get(session["capture_ids"][0])
        self.assertEqual((row["state"], row["review_queued"]), ("reviewed", 0))

    def test_missing_credentials_end_the_network_part_and_are_reported(self):
        support.make_session(self.home, points=2)
        with mock.patch.object(self.catalog, "ensure_collection",
                               side_effect=CatalogAuthRequired("sign in yourself")):
            result = self.sync()
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in result["errors"]], [(None, "auth")])
        self.assertEqual(set(self.states().values()), {"captured"})
        self.assertEqual({row["register_attempts"] for row in self.ledger.rows()}, {0})


class CrashAndRetry(SyncCase):
    def test_retry_after_dying_between_record_created_and_ledger_updated(self):
        session = support.make_session(self.home, points=3)
        real = Ledger.mark_registered
        seen = []

        def die_on_the_second(ledger, capture_id, *args):
            seen.append(capture_id)
            if len(seen) == 2:
                raise Crash()
            return real(ledger, capture_id, *args)

        with mock.patch.object(Ledger, "mark_registered", die_on_the_second):
            with self.assertRaises(Crash):
                self.sync()

        # The catalog has the second record; its ledger row never heard of it.
        orphan = self.catalog.get_record(raw_alias(session["capture_ids"][1]))
        self.assertEqual(list(self.states().values()), ["uploading", "captured", "captured"])
        self.assertIsNone(self.ledger.get(session["capture_ids"][1])["record_id"])

        # A new process: the lock of the dead one is gone and the rows are as it left them.
        result = self.sync(ledger=self.open_ledger())
        self.assertEqual((result["busy"], result["registered"], result["errors"]), (False, 2, []))
        self.assertEqual(self.ledger.get(session["capture_ids"][1])["record_id"], orphan.id)
        in_session = self.catalog.list_collection(session_alias(session["session_id"]))
        self.assertEqual(len(in_session), 3)
        self.assertEqual(len({item.id for item in in_session}), 3)
        self.assertEqual(len(self.catalog.list_collection(self.names().to_classify)), 3)
        self.assertEqual(len(list((self.paths.local_demo / "records").glob("d_*.json"))), 4)  # 3 spectra + 1 dark

    def test_retry_after_dying_between_upload_started_and_ledger_updated(self):
        session = support.make_session(self.home, points=1)
        with mock.patch.object(Ledger, "mark_uploading", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.sync()
        self.assertEqual(list(self.states().values()), ["registered"])
        self.sync()
        self.sync()
        self.assertEqual(list(self.states().values()), ["uploaded"])
        record = self.catalog.get_record(raw_alias(session["capture_ids"][0]))
        self.assertEqual(record.data_size, session["files"][0].stat().st_size)

    def test_alias_taken_by_a_different_capture_is_an_error_for_that_capture_only(self):
        self.config.set("sync.max_attempts", 2)
        session = support.make_session(self.home, points=3)
        names = ensure_layout(self.catalog, self.config.root_alias)
        self.catalog.create_record(raw_alias(session["capture_ids"][1]), "Somebody else's record",
                                   {"capture_id": "another-capture"}, names.sessions)

        first = self.sync()
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in first["errors"]],
                         [(session["capture_ids"][1], "register")])
        self.assertIn("different capture_id", first["errors"][0]["message"])
        self.assertEqual(list(self.states().values()), ["uploading", "error", "uploading"])
        row = self.ledger.get(session["capture_ids"][1])
        self.assertEqual((row["error_stage"], row["register_attempts"]), ("register", 1))

        self.assertEqual(len(self.sync()["errors"]), 1)
        self.assertEqual(self.ledger.get(session["capture_ids"][1])["register_attempts"], 2)
        # The limit is reached: later passes leave it alone.
        self.assertEqual(self.sync()["errors"], [])
        self.assertEqual(self.ledger.get(session["capture_ids"][1])["register_attempts"], 2)
        self.assertEqual(self.ledger.error_counts(2), {"metadata": 0, "retrying": 0, "given_up": 1})

        retried = self.sync(retry_failed=True)
        self.assertEqual(len(retried["errors"]), 1)
        self.assertEqual(self.ledger.get(session["capture_ids"][1])["register_attempts"], 1)
        # The foreign record was not touched.
        self.assertEqual(self.catalog.get_record(raw_alias(session["capture_ids"][1])).metadata,
                         {"capture_id": "another-capture"})

    def test_failed_upload_is_retried_by_a_later_sync(self):
        session = support.make_session(self.home, points=1)
        capture_id = session["capture_ids"][0]
        point_file = session["files"][0]
        self.sync()
        hidden = point_file.with_suffix(".hidden")
        point_file.rename(hidden)

        failed = self.sync()
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in failed["errors"]],
                         [(capture_id, "upload")])
        row = self.ledger.get(capture_id)
        self.assertEqual((row["state"], row["error_stage"], row["upload_attempts"], row["upload_status"]),
                         ("error", "upload", 1, "failed"))
        self.assertIn("cannot read the source file", row["error_message"])

        hidden.rename(point_file)
        retried = self.sync()
        self.assertEqual((retried["uploads_started"], retried["errors"]), (1, []))
        self.assertEqual(self.ledger.get(capture_id)["state"], "uploading")
        self.assertNotEqual(self.ledger.get(capture_id)["upload_task_id"], row["upload_task_id"])
        done = self.sync()
        self.assertEqual(done["uploads_done"], 1)
        row = self.ledger.get(capture_id)
        self.assertEqual((row["state"], row["upload_attempts"], row["error_message"]), ("uploaded", 0, None))
        self.assertEqual(self.catalog.get_record(row["record_id"]).data_size, point_file.stat().st_size)

    def test_upload_is_given_up_after_max_attempts_until_asked_to_retry(self):
        self.config.set("sync.max_attempts", 2)
        session = support.make_session(self.home, points=1)
        capture_id = session["capture_ids"][0]
        self.sync()
        hidden = session["files"][0].with_suffix(".hidden")
        session["files"][0].rename(hidden)
        self.sync()
        self.sync()
        self.assertEqual(self.ledger.get(capture_id)["upload_attempts"], 2)
        hidden.rename(session["files"][0])

        ignored = self.sync()
        self.assertEqual((ignored["uploads_started"], ignored["errors"]), (0, []))
        self.assertEqual(self.ledger.get(capture_id)["state"], "error")

        self.assertEqual(self.sync(retry_failed=True)["uploads_started"], 1)
        self.sync()
        self.assertEqual(self.ledger.get(capture_id)["state"], "uploaded")

    def test_point_file_changed_after_registration_is_not_uploaded(self):
        session = support.make_session(self.home, points=1)
        with mock.patch.object(Ledger, "mark_uploading", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.sync()
        session["files"][0].write_text(session["files"][0].read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = self.sync()
        self.assertEqual([entry["stage"] for entry in result["errors"]], ["upload"])
        self.assertIn("changed after it was registered", result["errors"][0]["message"])


class DyingAnywhere(SyncCase):
    """Kills the sync at every single call it makes to the catalog or the ledger, once just
    before the call and once just after it, and checks that later syncs finish the job with
    nothing lost and nothing made twice."""

    CATALOG_CALLS = ("ensure_collection", "create_record", "link", "put_data", "transfer_status")
    LEDGER_CALLS = ("bind_catalog", "add_capture", "mark_registered", "mark_uploading", "set_upload_status",
                    "mark_uploaded", "add_dark", "dark_uploading", "dark_status", "dark_uploaded", "set_value")

    def run_until_death(self, die_at, after_the_call):
        """Two syncs in which call number `die_at` is fatal. Returns False when they ran to the end."""
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

        def fatal_method(real):
            def method(ledger, *arguments, **options):
                return fatal(functools.partial(real, ledger))(*arguments, **options)

            return method

        with contextlib.ExitStack() as stack:
            for name in self.CATALOG_CALLS:
                stack.enter_context(mock.patch.object(self.catalog, name, fatal(getattr(self.catalog, name))))
            for name in self.LEDGER_CALLS:
                stack.enter_context(mock.patch.object(Ledger, name, fatal_method(getattr(Ledger, name))))
            ledger = Ledger(self.paths.data_catalog)
            try:
                self.sync(ledger=ledger)
                self.sync(ledger=ledger)
            except Crash:
                return True
            finally:
                ledger.close()
        return False

    def test_nothing_is_lost_and_nothing_is_made_twice(self):
        session = support.make_session(self.home, points=2)
        names = self.names()
        deaths = 0
        for after_the_call in (False, True):
            die_at = 0
            while True:
                die_at += 1
                shutil.rmtree(self.paths.data_catalog, ignore_errors=True)
                if not self.run_until_death(die_at, after_the_call):
                    break
                deaths += 1
                where = "died at call %d, %s it" % (die_at, "after" if after_the_call else "before")
                ledger = Ledger(self.paths.data_catalog)
                self.addCleanup(ledger.close)
                for _pass in range(4):
                    self.assertEqual(self.sync(ledger=ledger)["errors"], [], where)
                rows = ledger.rows()
                self.assertEqual(sorted(row["capture_id"] for row in rows), sorted(session["capture_ids"]), where)
                self.assertEqual({row["state"] for row in rows}, {"uploaded"}, where)
                self.assertEqual(support.record_types(self.paths.local_demo),
                                 {"raman.raw_spectrum": 2, "raman.dark_spectrum": 1}, where)
                record_ids = sorted(row["record_id"] for row in rows)
                for collection in (names.to_classify, session_alias(session["session_id"])):
                    self.assertEqual(sorted(item.id for item in self.catalog.list_collection(collection)),
                                     record_ids, where)
                for row, path in zip(ledger.rows(newest_first=False), session["files"]):
                    self.assertEqual(self.catalog.get_record(row["record_id"]).data_size, path.stat().st_size, where)
                self.assertEqual(ledger.get_dark(sha256_file(self.dark)[0])["state"], "uploaded", where)
                ledger.close()
        # Two full passes over two spectra and a dark make some forty such calls; each was fatal twice.
        self.assertGreaterEqual(deaths, 80)


class TwoSyncsAtOnce(SyncCase):
    def test_second_sync_in_another_ledger_handle_is_busy(self):
        support.make_session(self.home, points=2)
        other = self.open_ledger()
        with other.sync_lock() as held:
            self.assertTrue(held)
            result = self.sync()
        self.assertEqual((result["busy"], result["online"], result["ingested"]), (True, None, 0))
        self.assertEqual(self.ledger.count(), 0)
        self.assertEqual(self.sync()["ingested"], 2)


class CatalogChange(SyncCase):
    def test_switching_to_an_empty_catalog_registers_every_capture_there(self):
        session = support.make_session(self.home, points=2)
        raws = self.settle_uploads(session)
        support.save_prediction(self.catalog, self.config.root_alias, raws[0], "confident")
        self.sync()
        self.assertEqual(list(self.states().values()), ["accepted", "uploaded"])

        # Another catalog, as after `config --set catalog.root_alias=...`: here a second, empty demo catalog.
        self.catalog = LocalDemoCatalog(self.home / "data_catalog" / "another_catalog")
        self.config.set("catalog.root_alias", "second-root")
        moved = self.sync()
        self.assertEqual((moved["ingested"], moved["registered"], moved["uploads_started"], moved["errors"]),
                         (0, 2, 2, []))
        self.assertEqual(len(self.catalog.list_collection(session_alias(session["session_id"]))), 2)
        self.assertEqual(len(self.catalog.list_collection("second-root-to-classify")), 2)
        self.assertEqual(list(self.states().values()), ["uploading", "uploading"])

    def test_coming_back_to_a_catalog_finds_the_earlier_records_and_predictions(self):
        session = support.make_session(self.home, points=2)
        raws = self.settle_uploads(session)
        support.save_prediction(self.catalog, self.config.root_alias, raws[0], "confident")
        self.sync()
        records_before = len(list((self.paths.local_demo / "records").glob("d_*.json")))

        self.ledger.bind_catalog("some-other-catalog")
        back = self.sync()
        self.assertEqual((back["registered"], back["errors"]), (2, []))
        self.sync()
        final = self.sync()
        self.assertEqual(final["errors"], [])
        self.assertEqual(list(self.states().values()), ["accepted", "uploaded"])
        self.assertEqual(len(list((self.paths.local_demo / "records").glob("d_*.json"))), records_before)
        # The spectrum that already has a prediction is not queued for the classifier again.
        self.assertEqual([item.id for item in self.catalog.list_collection(self.names().to_classify)], [raws[1].id])


class MissingFields(SyncCase):
    def test_point_without_capture_block_waits_in_error_metadata(self):
        session = support.make_session(self.home, points=2, with_capture=False)
        result = self.sync()
        self.assertEqual(result["ingested"], 0)
        self.assertEqual([entry["stage"] for entry in result["errors"]], ["metadata", "metadata"])
        rows = self.ledger.rows()
        self.assertEqual({(row["state"], row["error_stage"]) for row in rows}, {("error", "metadata")})
        self.assertIn("capture block is missing", rows[0]["error_message"])
        # Nothing new to say on the next pass, and nothing was sent.
        self.assertEqual(self.sync()["errors"], [])
        self.assertFalse((self.paths.local_demo / "records").exists())

        for path in session["files"]:
            point = support.json.loads(path.read_text(encoding="utf-8"))
            point["capture"] = support.capture_block()
            path.write_text(support.json.dumps(point), encoding="utf-8")
        fixed = self.sync()
        self.assertEqual((fixed["ingested"], fixed["registered"], fixed["errors"]), (2, 2, []))

    def test_unreadable_point_file_is_reported_and_remembered_for_status(self):
        session = support.make_session(self.home, points=1)
        (session["folder"] / "point_0002.json").write_text("{not json", encoding="utf-8")
        result = self.sync()
        self.assertEqual(result["ingested"], 1)
        self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in result["errors"]], [(None, "metadata")])
        self.assertIn("point_0002.json", self.ledger.get_value(sync.LAST_PROBLEMS_KEY))


class OneBadFile(SyncCase):
    """A file that cannot be used as it is fails on its own: the sync goes on with every other one."""

    def edit_point(self, path, **changes):
        point = support.json.loads(path.read_text(encoding="utf-8"))
        point.update(changes)
        # Lone surrogates are written as JSON escapes, as the window wrote them.
        path.write_text(support.json.dumps(point), encoding="utf-8")

    def test_a_broken_character_in_a_text_field_leaves_that_capture_in_error_metadata(self):
        broken = support.make_session(self.home, folder="1d_x_scan_broken", points=1,
                                      location="L" * 199 + "\ud83d")
        self.edit_point(broken["files"][0], scan_mode="1d-x\udc80")
        good = support.make_session(self.home, folder="1d_x_scan_good", points=1)
        result = self.sync()
        self.assertEqual((result["online"], result["ingested"], result["registered"]), (True, 1, 1))
        self.assertEqual(result["errors"], [{
            "capture_id": broken["capture_ids"][0], "stage": "metadata",
            "message": "point_0001.json cannot be registered: location holds a broken character; scan_mode holds a "
                       "broken character"}])
        row = self.ledger.get(broken["capture_ids"][0])
        self.assertEqual((row["state"], row["error_stage"], row["register_attempts"]), ("error", "metadata", 0))
        # The list shows the row, the broken half replaced; the file keeps what it has.
        self.assertEqual(row["scan_mode"], "1d-x\ufffd")
        self.assertIn("\\udc80", broken["files"][0].read_text(encoding="utf-8"))
        self.assertEqual(self.ledger.get(good["capture_ids"][0])["state"], "uploading")

        later = support.make_session(self.home, folder="1d_x_scan_later", points=1)
        again = self.sync()
        self.assertEqual((again["ingested"], again["registered"], again["errors"]), (1, 1, []))
        self.assertEqual(self.ledger.get(later["capture_ids"][0])["state"], "uploading")

        self.edit_point(broken["files"][0], scan_mode="1d-x", capture=support.capture_block(location="Bench 1"))
        fixed = self.sync()
        self.assertEqual((fixed["ingested"], fixed["registered"], fixed["errors"]), (1, 1, []))

    def test_a_broken_capture_id_is_reported_without_a_row(self):
        session = support.make_session(self.home, points=2)
        self.edit_point(session["files"][0], capture_id="S-\ud83d_1")
        result = self.sync()
        self.assertEqual((result["registered"], self.ledger.count()), (1, 1))
        self.assertEqual([(entry["capture_id"], entry["message"]) for entry in result["errors"]],
                         [(None, "%s: point_0001.json: capture_id holds a broken character" % session["folder"].name)])
        self.assertIn("capture_id holds a broken character", self.ledger.get_value(sync.LAST_PROBLEMS_KEY))

    def test_a_point_or_dark_description_nested_too_deeply_ends_no_sync(self):
        deep = '{"x": ' + "[" * 100000 + "]" * 100000 + ', "simulated": {"backend": true}}'
        session = support.make_session(self.home, points=2)
        session["files"][1].write_text(deep, encoding="utf-8")
        self.dark.with_suffix(".json").write_text(deep, encoding="utf-8")
        result = self.sync()
        self.assertEqual((result["online"], result["registered"]), (True, 1))
        self.assertEqual([entry["message"] for entry in result["errors"]],
                         ["%s: point_0002.json cannot be read as JSON" % session["folder"].name])
        # Qt's parser cannot read that description either: the dark's kind is unknown, so it is not paired.
        dark = self.catalog.get_record(raw_alias(session["capture_ids"][0])).metadata["dark"]
        self.assertEqual((dark["kind"], dark["record"]), ("unknown", None))
        self.assertIn("cannot be read", dark["reason"])

    def test_numbers_beyond_what_the_ledger_or_a_float_holds_end_no_sync(self):
        session = support.make_session(self.home, points=3, steps=(0, 0, 0))
        self.edit_point(session["files"][0], point_id=2 ** 63)
        self.edit_point(session["files"][1], controller_x_steps=10 ** 400)
        self.edit_point(session["files"][2], completed_utc=10 ** 30, scan_mode={"mode": "1d-x"})
        result = self.sync()
        self.assertEqual((result["online"], result["registered"]), (True, 2))
        self.assertEqual([entry["message"] for entry in result["errors"]],
                         ["%s: point_0001.json: point_id is not a whole number" % session["folder"].name])
        moved, odd = self.ledger.get(session["capture_ids"][1]), self.ledger.get(session["capture_ids"][2])
        self.assertEqual((moved["x_mm"], odd["timestamp_utc"], odd["scan_mode"]), (None, None, None))

    @unittest.skipIf(os.geteuid() == 0, "file permissions do not keep the superuser from reading")
    def test_a_dark_or_a_folder_that_cannot_be_read_holds_back_only_what_needs_it(self):
        with_dark = support.make_session(self.home, folder="1d_x_scan_with_dark", points=1)
        without = support.make_session(self.home, folder="1d_x_scan_without_dark", points=1, dark_file=None)
        hidden = support.make_session(self.home, folder="1d_x_scan_unlisted", points=1, dark_file=None)
        self.dark.chmod(0)
        self.addCleanup(self.dark.chmod, 0o644)
        # Its files can be opened by name, but the folder cannot be listed.
        hidden["folder"].chmod(0o300)
        self.addCleanup(hidden["folder"].chmod, 0o755)
        result = self.sync()
        self.assertEqual((result["online"], result["registered"]), (True, 1))
        self.assertEqual(self.ledger.get(without["capture_ids"][0])["state"], "uploading")
        row = self.ledger.get(with_dark["capture_ids"][0])
        self.assertEqual((row["state"], row["error_stage"]), ("error", "metadata"))
        self.assertIn("point_0001.json cannot be registered: [Errno 13] Permission denied", row["error_message"])
        self.assertIn("1d_x_scan_unlisted cannot be read", self.ledger.get_value(sync.LAST_PROBLEMS_KEY))

        self.dark.chmod(0o644)
        hidden["folder"].chmod(0o755)
        result = self.sync()
        self.assertEqual((result["registered"], result["errors"]), (2, []))


class DuringAScan(SyncCase):
    """A sync, while a scan is still writing its folder, sees files that are not complete yet. The
    controller publishes each file in one step; a folder copied in by hand or by another program
    is not, and that is what is imitated here."""

    def half_written(self, session, point_id):
        """The first half of the next point file of a session, as a copy in progress leaves it."""
        point = support.point_dict(session["session_id"], point_id, capture=support.capture_block())
        text = support.json.dumps(point, indent=4, sort_keys=True)
        path = session["folder"] / ("point_%04d.json" % point_id)
        path.write_text(text[: len(text) // 2], encoding="utf-8")
        return path, text

    def test_a_point_still_being_written_is_registered_once_it_is_complete(self):
        self.config.set("sync.max_attempts", 2)
        session = support.make_session(self.home, points=1)
        path, complete = self.half_written(session, 2)
        for _pass in range(4):
            result = self.sync()
            self.assertEqual([(entry["capture_id"], entry["stage"]) for entry in result["errors"]],
                             [(None, "metadata")])
            self.assertIn("point_0002.json cannot be read as JSON", result["errors"][0]["message"])
        # Never in the ledger while it cannot be read, so never marked as failed, let alone given up.
        self.assertEqual(self.ledger.count(), 1)
        self.assertEqual(self.ledger.count(states=["error"]), 0)

        path.write_text(complete, encoding="utf-8")
        result = self.sync()
        self.assertEqual((result["ingested"], result["registered"], result["errors"]), (1, 1, []))
        self.assertEqual(self.ledger.get_value(sync.LAST_PROBLEMS_KEY), "[]")
        self.assertEqual(self.catalog.get_record(raw_alias("%s_2" % session["session_id"])).metadata["raw_file"],
                         {"name": "point_0002.json", "sha256": hashlib.sha256(complete.encode()).hexdigest(),
                          "bytes": len(complete)})

    def test_a_session_json_still_being_written_holds_its_folder_back(self):
        session = support.make_session(self.home, points=2)
        session_json = session["folder"] / "session.json"
        complete = session_json.read_text(encoding="utf-8")
        session_json.write_text(complete[: len(complete) // 2], encoding="utf-8")
        result = self.sync()
        self.assertEqual((result["ingested"], self.ledger.count()), (0, 0))
        self.assertIn("session.json cannot be read as JSON", result["errors"][0]["message"])

        session_json.write_text(complete, encoding="utf-8")
        self.assertEqual(self.sync()["registered"], 2)

    @unittest.skipIf(os.geteuid() == 0, "file permissions do not keep the superuser from reading")
    def test_a_point_that_cannot_be_read_when_it_is_registered_is_tried_again_not_given_up(self):
        self.config.set("sync.max_attempts", 2)
        session = support.make_session(self.home, points=1)
        self.catalog.set_offline(True)
        self.sync()
        self.catalog.set_offline(False)
        path = session["files"][0]
        path.chmod(0)
        self.addCleanup(path.chmod, 0o644)
        for _pass in range(3):
            result = self.sync()
            self.assertEqual([entry["stage"] for entry in result["errors"]], ["metadata"])
        row = self.ledger.get(session["capture_ids"][0])
        self.assertEqual((row["state"], row["error_stage"], row["register_attempts"]), ("error", "metadata", 0))

        path.chmod(0o644)
        result = self.sync()
        self.assertEqual((result["registered"], result["errors"]), (1, []))
        self.assertEqual(self.ledger.get(session["capture_ids"][0])["state"], "uploading")

    def test_an_import_of_the_folder_leaves_a_point_still_being_written_for_a_later_sync(self):
        session = support.make_session(self.home, points=2)
        path, complete = self.half_written(session, 3)
        with Context(self.home, catalog=self.catalog) as ctx:
            imported = commands_core.cmd_import_session(
                Namespace(folder=str(session["folder"]), sample_id=None, operator=None, location=None), ctx)
        self.assertEqual((imported["ingested"], imported["copied"]), (2, False))
        self.assertTrue(any("point_0003.json" in entry["message"] for entry in imported["errors"]))
        self.assertIsNone(self.ledger.get("%s_3" % session["session_id"]))
        # The import changed nothing in the file being written.
        self.assertEqual(path.read_text(encoding="utf-8"), complete[: len(complete) // 2])

        path.write_text(complete, encoding="utf-8")
        self.sync()
        self.assertEqual(self.ledger.get("%s_3" % session["session_id"])["state"], "uploading")


class DarkKinds(SyncCase):
    def test_a_dark_of_the_other_kind_gets_no_record_and_the_spectrum_says_why(self):
        measured = support.make_session(self.home, folder="1d_x_scan_real", points=1, simulated=False)
        result = self.sync()
        self.assertEqual((result["registered"], result["errors"]), (1, []))
        raw = self.catalog.get_record(raw_alias(measured["capture_ids"][0]))
        self.assertEqual(raw.metadata["dark"], {
            "file": support.DARK_NAME, "sha256": sha256_file(self.dark)[0], "record": None, "kind": "simulated",
            "reason": "dark not used: the dark was taken in simulation; this spectrum is real"})
        # Nothing is registered for the dark, so nothing can be derived from it.
        self.assertEqual(self.catalog.list_collection(self.names().darks), [])
        self.assertIsNone(self.ledger.get_dark(sha256_file(self.dark)[0]))

        simulated = support.make_session(self.home, folder="1d_x_scan_simulated", points=1)
        self.sync()
        raw = self.catalog.get_record(raw_alias(simulated["capture_ids"][0]))
        dark = self.catalog.get_record(dark_alias(sha256_file(self.dark)[0]))
        self.assertEqual((raw.metadata["dark"]["record"], raw.metadata["dark"]["kind"]), (dark.id, "simulated"))
        self.assertNotIn("reason", raw.metadata["dark"])

    def test_the_local_demo_registers_simulated_captures(self):
        support.make_session(self.home, points=2)
        result = self.sync()
        self.assertEqual(result["registered"], 2)
        self.assertNotIn("held_simulated", result)


class LostDarkFile(SyncCase):
    """A dark whose file disappears after its record was made, before the file reached the catalog."""

    def lose_the_dark(self):
        session = support.make_session(self.home, points=1)
        self.sync()
        self.assertEqual(self.ledger.get_dark(sha256_file(self.dark)[0])["state"], "uploading")
        hidden = self.dark.with_suffix(".hidden")
        self.dark.rename(hidden)
        return session, hidden

    def test_it_is_marked_unavailable_in_its_record_once_given_up_and_the_mark_goes_with_the_file_back(self):
        self.config.set("sync.max_attempts", 3)
        _session, hidden = self.lose_the_dark()
        record_id = self.ledger.get_dark(sha256_file(hidden)[0])["record_id"]
        messages = []
        for _pass in range(3):
            messages.extend(entry["message"] for entry in self.sync()["errors"])
            # Until the last try the record says nothing: the file may still come back.
            if len(messages) < 3:
                self.assertNotIn("unavailable", self.catalog.get_record(record_id).metadata)
        self.assertEqual(len(messages), 3)
        self.assertIn("cannot read the source file", messages[0])
        self.assertEqual(self.catalog.get_record(record_id).metadata["unavailable"],
                         "the file %s is missing" % support.DARK_NAME)
        self.assertEqual(self.sync()["errors"], [], "given up: later passes leave it alone")

        hidden.rename(self.dark)
        self.sync(retry_failed=True)
        self.sync()
        self.assertEqual(self.ledger.get_dark(sha256_file(self.dark)[0])["state"], "uploaded")
        record = self.catalog.get_record(record_id)
        self.assertGreater(record.data_size, 0)
        self.assertNotIn("unavailable", record.metadata)

    def test_a_dark_that_only_failed_to_transfer_is_not_marked(self):
        self.config.set("sync.max_attempts", 2)
        support.make_session(self.home, points=1)
        real_put = self.catalog.put_data

        def refuse_darks(record, path):
            if str(path).endswith(support.DARK_NAME):
                raise sync.CatalogError("the endpoint refused the transfer")
            return real_put(record, path)

        with mock.patch.object(self.catalog, "put_data", refuse_darks):
            for _pass in range(3):
                self.sync()
        row = self.ledger.get_dark(sha256_file(self.dark)[0])
        self.assertEqual((row["state"], row["attempts"]), ("error", 2))
        self.assertNotIn("unavailable", self.catalog.get_record(row["record_id"]).metadata)
        with Context(self.home, catalog=self.catalog) as ctx:
            problems = commands_core.build_status(ctx)["problems"]
        self.assertIn("1 dark spectrum file(s) could not be uploaded (the last: the endpoint refused the transfer); "
                      "Sync now tries again.", problems)

    def test_offline_while_marking_leaves_the_last_try_for_the_next_pass(self):
        self.config.set("sync.max_attempts", 2)
        _session, hidden = self.lose_the_dark()
        self.sync()
        with mock.patch.object(self.catalog, "update_record", side_effect=CatalogOffline("connection lost")):
            self.assertIs(self.sync()["online"], False)
        row = self.ledger.get_dark(sha256_file(hidden)[0])
        self.assertEqual(row["attempts"], 1, "the try that could not mark the record does not count")
        self.sync()
        self.assertIn("unavailable", self.catalog.get_record(row["record_id"]).metadata)
        self.assertEqual(self.ledger.get_dark(sha256_file(hidden)[0])["attempts"], 2)


class ReviewOrder(SyncCase):
    """A review queued offline must not replace a later review of the same prediction."""

    def waiting_prediction(self):
        session = support.make_session(self.home, points=1)
        raws = self.settle_uploads(session)
        prediction = support.save_prediction(self.catalog, self.config.root_alias, raws[0], "low_confidence",
                                             confidence=0.5)
        self.sync()
        return session["capture_ids"][0], prediction

    @staticmethod
    def review(status, minute, reviewer):
        return {"status": status, "label": "r6g" if status == "confirmed" else "di_water", "reviewer": reviewer,
                "time_utc": "2026-11-13T16:%02d:00.000Z" % minute, "comment": ""}

    def test_a_later_review_in_the_catalog_stays_and_the_ledger_shows_it(self):
        capture_id, prediction = self.waiting_prediction()
        earlier, later = self.review("confirmed", 1, "first decision"), self.review("relabelled", 2, "second decision")
        self.ledger.queue_review(prediction.id, earlier)
        sync.apply_review(self.catalog, self.config, prediction.id, later)

        result = self.sync()
        self.assertEqual((result["reviews_sent"], result["errors"]), (0, []))
        self.assertEqual(self.catalog.get_record(prediction.id).metadata["review"]["reviewer"], "second decision")
        row = self.ledger.get(capture_id)
        self.assertEqual((row["state"], row["review_queued"], support.json.loads(row["review_json"])["reviewer"]),
                         ("reviewed", 0, "second decision"))
        self.assertEqual(self.ledger.queued_reviews(self.config.catalog_key), [])
        # The same review sent again, or a later one, is written as before.
        self.assertEqual(sync.apply_review(self.catalog, self.config, prediction.id, later).metadata["review"],
                         later)
        latest = self.review("confirmed", 3, "third decision")
        self.assertEqual(sync.apply_review(self.catalog, self.config, prediction.id, latest).metadata["review"],
                         latest)

    def test_the_first_request_of_the_pass_is_made_before_the_review_lock_is_taken(self):
        """The first request of a process opens the catalog; a review-set waiting for the lock
        meanwhile would wait for that as well."""
        _capture_id, prediction = self.waiting_prediction()
        self.ledger.queue_review(prediction.id, self.review("confirmed", 1, "first decision"))
        lock_file = self.paths.data_catalog / "review.lock"
        held_at_each_read = []
        real_get_record = self.catalog.get_record

        def get_record(record):
            with open(lock_file, "a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held_at_each_read.append(True)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    held_at_each_read.append(False)
            return real_get_record(record)

        with mock.patch.object(self.catalog, "get_record", get_record):
            self.assertEqual(self.sync()["reviews_sent"], 1)
        # The first read connects, outside the lock; the review is read again under it before it is written.
        self.assertEqual(held_at_each_read[:2], [False, True])

    def test_a_queued_review_replaced_after_the_queue_was_read_is_not_sent(self):
        _capture_id, prediction = self.waiting_prediction()
        self.ledger.queue_review(prediction.id, self.review("confirmed", 1, "first decision"))
        read_before = self.ledger.queued_reviews(self.config.catalog_key, self.config.max_attempts)
        # What review-set does while the catalog cannot be reached: the newer review takes the place in the queue.
        self.ledger.queue_review(prediction.id, self.review("relabelled", 2, "second decision"))
        with mock.patch.object(self.ledger, "queued_reviews", return_value=read_before):
            self.assertEqual(self.sync()["reviews_sent"], 0)
        self.assertEqual(self.catalog.get_record(prediction.id).metadata["review"]["status"], "pending")
        self.assertEqual(self.sync()["reviews_sent"], 1)
        self.assertEqual(self.catalog.get_record(prediction.id).metadata["review"]["reviewer"], "second decision")


class PredictionsWaitingForAPerson(SyncCase):
    def test_are_not_read_one_by_one_while_they_are_in_the_review_queue(self):
        session = support.make_session(self.home, points=3)
        raws = self.settle_uploads(session)
        predictions = [support.save_prediction(self.catalog, self.config.root_alias, raw, "low_confidence",
                                               confidence=0.5) for raw in raws]
        self.sync()
        self.assertEqual(set(self.states().values()), {"needs_review"})
        with mock.patch.object(self.catalog, "get_record", wraps=self.catalog.get_record) as asked, \
                mock.patch.object(self.catalog, "list_collection", wraps=self.catalog.list_collection) as listed:
            self.assertEqual(self.sync()["reviews_found"], 0)
            asked.assert_not_called()
            self.assertEqual([call.args[0] for call in listed.call_args_list], [self.names().needs_review])

            # Reviewed somewhere else: that prediction left the queue, and only it is read.
            sync.apply_review(self.catalog, self.config, predictions[1].id,
                              {"status": "confirmed", "label": "r6g", "reviewer": "Somebody Else",
                               "time_utc": "2026-11-13T16:00:00.000Z", "comment": ""})
            asked.reset_mock()
            self.assertEqual(self.sync()["reviews_found"], 1)
            self.assertEqual(asked.call_args_list[0].args[0], raws[1].id)
            self.assertEqual(asked.call_count, 2)
        self.assertEqual(list(self.states().values()), ["needs_review", "reviewed", "needs_review"])

        # The periodic full check still reads every one, should the queue ever be out of step.
        self.ledger.set_value(sync.LAST_FULL_CHECK_KEY, "2026-11-13T15:00:00.000Z")
        with mock.patch.object(self.catalog, "get_record", wraps=self.catalog.get_record) as asked:
            self.sync()
        self.assertEqual(asked.call_count, 4)


class ModelHook(SyncCase):
    def run_with_job(self, ensure_model_record):
        job = types.SimpleNamespace(ensure_model_record=ensure_model_record)
        with mock.patch.object(sync, "_optional_module", return_value=job) as lookup:
            result = self.sync()
        lookup.assert_called_once_with("raman_catalog.job")
        return result

    def test_called_once_per_sync_before_registering(self):
        support.make_session(self.home, points=2)
        calls = []

        def ensure_model_record(catalog, config, home):
            calls.append((catalog, config, home, self.ledger.count(states=["registered", "uploading"])))

        result = self.run_with_job(ensure_model_record)
        self.assertEqual(calls, [(self.catalog, self.config, self.home, 0)])
        self.assertEqual((result["registered"], result["errors"]), (2, []))

    def test_offline_in_the_hook_ends_the_network_part(self):
        support.make_session(self.home, points=2)
        result = self.run_with_job(mock.Mock(side_effect=CatalogOffline("unreachable")))
        self.assertEqual((result["online"], result["registered"], result["errors"]), (False, 0, []))

    def test_any_other_failure_of_the_hook_does_not_stop_the_captures(self):
        support.make_session(self.home, points=2)
        result = self.run_with_job(mock.Mock(side_effect=RuntimeError("model files missing")))
        self.assertEqual([(entry["stage"], entry["message"]) for entry in result["errors"]],
                         [("model", "model files missing")])
        self.assertEqual(result["registered"], 2)

    def test_only_a_module_that_does_not_exist_is_skipped(self):
        self.assertIsNone(REAL_OPTIONAL_MODULE("raman_catalog.no_such_part"))
        # A module that exists but cannot be imported is a fault to show, not a part to skip.
        (self.home / "broken_part.py").write_text("import a_package_nobody_installed\n", encoding="utf-8")
        with mock.patch.object(sys, "path", [str(self.home)] + sys.path):
            with self.assertRaises(ModuleNotFoundError) as raised:
                REAL_OPTIONAL_MODULE("broken_part")
        self.assertEqual(raised.exception.name, "a_package_nobody_installed")


class PredictionPickup(SyncCase):
    def test_pickup_for_named_captures_right_after_a_classification_run(self):
        session = support.make_session(self.home, points=2)
        raws = self.settle_uploads(session)
        support.save_prediction(self.catalog, self.config.root_alias, raws[0], "confident")

        report = sync.pick_up_predictions(self.catalog, self.ledger, capture_ids=[session["capture_ids"][0], "unknown"])
        self.assertEqual(report, {"found": 1, "updated": 0, "errors": []})
        self.assertEqual(list(self.states().values()), ["accepted", "uploaded"])
        self.assertEqual(sync.pick_up_predictions(self.catalog, self.ledger)["found"], 0)

    def test_newest_prediction_wins_when_classified_twice(self):
        session = support.make_session(self.home, points=1)
        raws = self.settle_uploads(session)
        support.save_prediction(self.catalog, self.config.root_alias, raws[0], "low_confidence", confidence=0.5,
                                finished_utc="2026-11-13T15:20:00.000Z")
        self.sync()
        newer = support.save_prediction(self.catalog, self.config.root_alias, raws[0], "confident", confidence=0.95,
                                        model_hash="7a" * 32, finished_utc="2026-11-14T09:00:00.000Z")
        report = sync.pick_up_predictions(self.catalog, self.ledger, capture_ids=session["capture_ids"])
        self.assertEqual((report["found"], report["updated"]), (1, 0))
        row = self.ledger.get(session["capture_ids"][0])
        self.assertEqual((row["state"], row["prediction_id"]), ("accepted", newer.id))

    def test_captures_still_waiting_for_the_classifier_are_not_asked_about_one_by_one(self):
        session = support.make_session(self.home, points=3)
        raws = self.settle_uploads(session)
        with mock.patch.object(self.catalog, "get_record", wraps=self.catalog.get_record) as asked:
            self.assertEqual(self.sync()["predictions_found"], 0)
            asked.assert_not_called()
            support.save_prediction(self.catalog, self.config.root_alias, raws[1], "confident")
            asked.reset_mock()
            self.assertEqual(self.sync()["predictions_found"], 1)
        # Only the spectrum that left the queue was looked at: itself and its prediction.
        self.assertEqual([call.args[0] for call in asked.call_args_list][0], raws[1].id)
        self.assertEqual(asked.call_count, 2)

    def test_prediction_left_in_the_queue_is_found_by_the_periodic_full_check(self):
        session = support.make_session(self.home, points=1)
        raws = self.settle_uploads(session)
        names = self.names()
        support.save_prediction(self.catalog, self.config.root_alias, raws[0], "confident")
        self.catalog.link(names.to_classify, raws[0].id)
        self.assertEqual(self.sync()["predictions_found"], 0)
        self.ledger.set_value(sync.LAST_FULL_CHECK_KEY, "2026-11-13T15:00:00.000Z")
        self.assertEqual(self.sync()["predictions_found"], 1)
        self.assertLess(seconds_since(self.ledger.get_value(sync.LAST_FULL_CHECK_KEY)), 60)

    def test_review_waiting_to_be_sent_is_not_overwritten_by_the_catalog(self):
        session = support.make_session(self.home, points=1)
        raws = self.settle_uploads(session)
        prediction = support.save_prediction(self.catalog, self.config.root_alias, raws[0], "low_confidence",
                                             confidence=0.5)
        self.sync()
        self.ledger.queue_review(prediction.id, {"status": "rejected", "label": None, "reviewer": "Test Reviewer",
                                                 "time_utc": "2026-11-13T16:00:00.000Z", "comment": ""})
        report = sync.pick_up_predictions(self.catalog, self.ledger, capture_ids=session["capture_ids"])
        self.assertEqual((report["found"], report["updated"]), (0, 0))
        self.assertEqual(list(self.states().values()), ["reviewed"])

    def test_apply_review_refuses_a_record_that_is_not_a_prediction(self):
        session = support.make_session(self.home, points=1)
        raws = self.settle_uploads(session)
        review = {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer", "time_utc": "", "comment": ""}
        with self.assertRaisesRegex(Exception, "not a prediction record"):
            sync.apply_review(self.catalog, self.config, raws[0].id, review)
        with self.assertRaises(ValueError):
            sync.apply_review(self.catalog, self.config, raws[0].id, {"status": "pending"})
        self.assertNotIn("review", self.catalog.get_record(raws[0].id).metadata)


if __name__ == "__main__":
    unittest.main()
