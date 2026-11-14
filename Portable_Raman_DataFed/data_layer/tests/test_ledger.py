"""The local ledger: states, attempt counters, the review queue, and use by several processes."""

import sqlite3
import subprocess
import sys
import unittest

import support
from raman_datafed import ledger as ledger_module
from raman_datafed.ledger import Ledger, item_from_row

LIST_ITEM_KEYS = {
    "capture_id", "session_id", "session_folder", "point_id", "scan_mode", "sample_id", "operator",
    "timestamp_utc", "simulated", "position_mm", "state", "state_label", "record_id", "record_alias", "upload",
    "prediction", "review", "error",
}
PREDICTION = {"label": "UNKNOWN", "top_class": "r6g", "confidence": 0.61, "outcome": "low_confidence",
              "needs_review": True, "scores": {"r6g": 0.61},
              "review": {"status": "pending", "label": None, "reviewer": None, "time_utc": None, "comment": ""}}
REVIEW = {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer",
          "time_utc": "2026-11-13T16:00:00.000Z", "comment": "clear peaks"}


def entry(number, session="session-1", folder="1d_x_scan_1"):
    return {
        "capture_id": "%s_%d" % (session, number), "session_id": session, "session_folder": folder,
        "point_file": "point_%04d.json" % number, "point_id": number, "scan_mode": "1d-x", "sample_id": "S-001",
        "operator": "Test Operator", "location": "Bench 1",
        "timestamp_utc": "2026-11-13T15:15:%02d.000Z" % number, "simulated": 1,
        "x_mm": 2.0 * number, "y_mm": 2.5, "z_mm": 4.0,
    }


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.ledger = self.open()

    def open(self):
        ledger = Ledger(self.home / "data_catalog")
        self.addCleanup(ledger.close)
        return ledger

    def add(self, count, **options):
        for number in range(1, count + 1):
            self.assertEqual(self.ledger.add_capture(entry(number, **options)), "added")
        return [entry(number, **options)["capture_id"] for number in range(1, count + 1)]


class Rows(LedgerCase):
    def test_files_are_where_the_contract_says(self):
        self.assertTrue((self.home / "data_catalog" / "ledger.sqlite").is_file())
        self.assertEqual(self.ledger.lock_path, self.home / "data_catalog" / "sync.lock")

    def test_one_row_per_capture(self):
        ids = self.add(3)
        self.assertEqual(self.ledger.add_capture(entry(2)), "unchanged")
        self.assertEqual(self.ledger.count(), 3)
        row = self.ledger.get(ids[1])
        self.assertEqual((row["state"], row["point_id"], row["x_mm"], row["register_attempts"], row["upload_attempts"]),
                         ("captured", 2, 4.0, 0, 0))
        self.assertIsNone(self.ledger.get("unknown"))
        self.assertEqual(self.ledger.known_files()[("1d_x_scan_1", "point_0002.json")], ids[1])

    def test_same_capture_in_another_file_is_a_duplicate(self):
        self.add(1)
        self.assertEqual(self.ledger.add_capture(entry(1, folder="copy_of_scan")), "duplicate")
        self.ledger.relocate("session-1_1", "copy_of_scan", "point_0001.json")
        self.assertEqual(self.ledger.add_capture(entry(1, folder="copy_of_scan")), "unchanged")
        self.assertEqual(self.ledger.get("session-1_1")["session_folder"], "copy_of_scan")

    def test_point_lacking_fields_waits_in_error_metadata_until_fixed(self):
        broken = {key: value for key, value in entry(1).items() if key not in ("sample_id", "operator", "location")}
        self.assertEqual(self.ledger.add_capture(broken, error_message="sample_id is empty"), "added")
        row = self.ledger.get(broken["capture_id"])
        self.assertEqual((row["state"], row["error_stage"], row["error_message"], row["sample_id"]),
                         ("error", "metadata", "sample_id is empty", None))
        # Its file is looked at again by every sync.
        self.assertNotIn(("1d_x_scan_1", "point_0001.json"), self.ledger.known_files())
        self.assertEqual(self.ledger.add_capture(broken, error_message="sample_id is empty"), "unchanged")
        self.assertEqual(self.ledger.rows_to_advance(5), [])

        self.assertEqual(self.ledger.add_capture(entry(1)), "fixed")
        row = self.ledger.get(broken["capture_id"])
        self.assertEqual((row["state"], row["error_stage"], row["error_message"], row["sample_id"]),
                         ("captured", None, None, "S-001"))
        self.assertEqual(len(self.ledger.rows_to_advance(5)), 1)

    def test_values_sqlite_cannot_take_do_not_keep_a_point_out_of_the_ledger(self):
        odd = dict(entry(1), scan_mode="1d-x\ud83d", sample_id="\udc80S", timestamp_utc={"utc": "now"},
                   point_id=2 ** 64, x_mm=float("inf"))
        self.assertEqual(self.ledger.add_capture(odd, error_message="scan_mode holds a broken character"), "added")
        row = self.ledger.get(odd["capture_id"])
        # Half a character becomes U+FFFD; what SQLite cannot hold at all is NULL.
        self.assertEqual((row["scan_mode"], row["sample_id"], row["timestamp_utc"], row["point_id"], row["x_mm"]),
                         ("1d-x\ufffd", "\ufffdS", None, None, float("inf")))
        self.assertEqual(row["state"], "error")
        self.assertEqual(self.ledger.add_capture(dict(odd, scan_mode=["1d-x"]), error_message="other"), "unchanged")
        self.assertEqual(self.ledger.get(odd["capture_id"])["scan_mode"], None)

    def test_counts_have_every_state(self):
        ids = self.add(4)
        self.ledger.mark_registered(ids[1], "d/1", "rs-1", "aa")
        self.ledger.mark_error(ids[2], "register", "refused")
        counts = self.ledger.counts()
        self.assertEqual(list(counts), list(ledger_module.STATES))
        self.assertEqual(counts, {"captured": 2, "registered": 1, "uploading": 0, "uploaded": 0, "classified": 0,
                                  "needs_review": 0, "reviewed": 0, "accepted": 0, "error": 1})
        self.assertEqual(set(ledger_module.STATE_LABELS), set(ledger_module.STATES))

    def test_rows_filter_order_and_limit(self):
        self.add(3)
        self.add(2, session="session-2", folder="2d_scan_2")
        self.ledger.mark_registered("session-2_1", "d/9", "rs-9", "aa")
        self.assertEqual([row["capture_id"] for row in self.ledger.rows()],
                         ["session-1_3", "session-2_2", "session-1_2", "session-2_1", "session-1_1"])
        self.assertEqual([row["capture_id"] for row in self.ledger.rows(newest_first=False, limit=2)],
                         ["session-1_1", "session-2_1"])
        self.assertEqual([row["capture_id"] for row in self.ledger.rows(session="session-2")],
                         ["session-2_2", "session-2_1"])
        self.assertEqual(len(self.ledger.rows(session="2d_scan_2")), 2)
        self.assertEqual([row["capture_id"] for row in self.ledger.rows(states=["registered"])], ["session-2_1"])
        self.assertEqual(self.ledger.count(states=["captured"], session="session-2"), 1)


class StatesAndAttempts(LedgerCase):
    def test_the_way_through_the_states(self):
        (capture_id,) = self.add(1)
        self.ledger.mark_registered(capture_id, "d/12", "rs-abc", "ab" * 32)
        row = self.ledger.get(capture_id)
        self.assertEqual((row["state"], row["record_id"], row["record_alias"], row["raw_sha256"]),
                         ("registered", "d/12", "rs-abc", "ab" * 32))
        self.ledger.mark_uploading(capture_id, "task/3", "queued")
        self.ledger.set_upload_status(capture_id, "running")
        row = self.ledger.get(capture_id)
        self.assertEqual((row["state"], row["upload_task_id"], row["upload_status"]),
                         ("uploading", "task/3", "running"))
        self.ledger.mark_uploaded(capture_id)
        row = self.ledger.get(capture_id)
        self.assertEqual((row["state"], row["upload_status"]), ("uploaded", "succeeded"))
        self.assertEqual(self.ledger.rows_to_advance(5), [])

    def test_errors_count_attempts_per_stage_and_stop_at_the_limit(self):
        (capture_id,) = self.add(1)
        self.ledger.mark_error(capture_id, "register", "alias taken")
        self.ledger.mark_error(capture_id, "register", "alias taken again")
        row = self.ledger.get(capture_id)
        self.assertEqual((row["state"], row["error_stage"], row["error_message"], row["register_attempts"],
                          row["upload_attempts"], row["last_error"]),
                         ("error", "register", "alias taken again", 2, 0, "alias taken again"))
        self.assertRegex(row["last_error_utc"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(len(self.ledger.rows_to_advance(3)), 1)
        self.assertEqual(self.ledger.rows_to_advance(2), [])
        self.assertEqual(self.ledger.error_counts(3), {"metadata": 0, "retrying": 1, "given_up": 0})
        self.assertEqual(self.ledger.error_counts(2), {"metadata": 0, "retrying": 0, "given_up": 1})

        self.ledger.mark_registered(capture_id, "d/1", "rs-1", "aa")
        row = self.ledger.get(capture_id)
        self.assertEqual((row["register_attempts"], row["error_stage"], row["error_message"]), (0, None, None))
        # The last failure stays readable after the capture recovered.
        self.assertEqual(row["last_error"], "alias taken again")

        self.ledger.mark_uploading(capture_id, "task/1", "running")
        self.ledger.mark_error(capture_id, "upload", "endpoint not connected")
        row = self.ledger.get(capture_id)
        self.assertEqual((row["error_stage"], row["upload_attempts"], row["upload_status"], row["upload_message"]),
                         ("upload", 1, "failed", "endpoint not connected"))
        self.ledger.reset_attempts()
        self.assertEqual(self.ledger.get(capture_id)["upload_attempts"], 0)
        with self.assertRaises(ValueError):
            self.ledger.mark_error(capture_id, "network", "no such stage")

    def test_prediction_sets_the_state_it_stands_for(self):
        ids = self.add(3)
        for capture_id in ids:
            self.ledger.mark_registered(capture_id, "d/raw", "rs", "aa")
            self.ledger.mark_uploaded(capture_id)
        confident = dict(PREDICTION, label="r6g", confidence=0.93, outcome="confident", needs_review=False,
                         review={"status": "not_required", "label": None, "reviewer": None, "time_utc": None})
        self.assertTrue(self.ledger.set_prediction(ids[0], "d/40", confident))
        self.assertTrue(self.ledger.set_prediction(ids[1], "d/41", PREDICTION))
        self.assertTrue(self.ledger.set_prediction(ids[2], "d/42", dict(PREDICTION, needs_review=False, review=REVIEW)))
        self.assertEqual([self.ledger.get(capture_id)["state"] for capture_id in ids],
                         ["accepted", "needs_review", "reviewed"])
        # Nothing new: no change, and a sync counts nothing.
        self.assertFalse(self.ledger.set_prediction(ids[1], "d/41", PREDICTION))
        self.assertFalse(self.ledger.set_prediction("unknown", "d/41", PREDICTION))

        item = item_from_row(self.ledger.get(ids[1]))
        self.assertEqual(item["prediction"], {"record_id": "d/41", "label": "UNKNOWN", "top_class": "r6g",
                                              "confidence": 0.61, "outcome": "low_confidence", "needs_review": True})
        self.assertEqual(item["review"], {"status": "pending", "label": None, "reviewer": None, "time_utc": None})
        self.assertEqual(self.ledger.find_by_prediction("d/41")["capture_id"], ids[1])

    def test_review_keys_dropped_by_the_catalog_read_as_null(self):
        (capture_id,) = self.add(1)
        trimmed = dict(PREDICTION, needs_review=False, review={"status": "rejected", "reviewer": "Test Reviewer"})
        self.ledger.set_prediction(capture_id, "d/41", trimmed)
        self.assertEqual(item_from_row(self.ledger.get(capture_id))["review"],
                         {"status": "rejected", "label": None, "reviewer": "Test Reviewer", "time_utc": None})


class ListItem(LedgerCase):
    def test_item_has_the_keys_of_the_contract(self):
        ids = self.add(2)
        fresh = item_from_row(self.ledger.get(ids[0]))
        self.assertEqual(set(fresh) - {"point_file"}, LIST_ITEM_KEYS)
        self.assertEqual((fresh["state"], fresh["state_label"], fresh["simulated"], fresh["position_mm"]),
                         ("captured", "Captured", True, {"x": 2.0, "y": 2.5, "z": 4.0}))
        self.assertEqual((fresh["record_id"], fresh["upload"], fresh["prediction"], fresh["review"], fresh["error"]),
                         (None, None, None, None, None))

        self.ledger.mark_registered(ids[1], "d/12", "rs-abc", "aa")
        self.ledger.mark_uploading(ids[1], "task/3", "running")
        self.ledger.mark_error(ids[1], "upload", "endpoint not connected")
        failed = item_from_row(self.ledger.get(ids[1]))
        self.assertEqual((failed["state"], failed["state_label"], failed["record_id"], failed["record_alias"]),
                         ("error", "Error", "d/12", "rs-abc"))
        self.assertEqual(failed["upload"], {"task_id": "task/3", "status": "failed"})
        self.assertEqual(failed["error"], {"stage": "upload", "message": "endpoint not connected", "attempts": 1})

    def test_single_capture_and_unknown_simulation(self):
        single = dict(entry(1), x_mm=None, y_mm=None, z_mm=None, simulated=None)
        self.ledger.add_capture(single)
        item = item_from_row(self.ledger.get(single["capture_id"]))
        self.assertEqual((item["position_mm"], item["simulated"]), (None, None))


class Reviews(LedgerCase):
    def classified(self):
        (capture_id,) = self.add(1)
        self.ledger.bind_catalog("local-demo|raman-portable")
        self.ledger.set_prediction(capture_id, "d/41", PREDICTION)
        return capture_id

    def test_review_that_is_in_the_catalog(self):
        capture_id = self.classified()
        self.assertEqual(self.ledger.record_review("d/41", REVIEW), capture_id)
        item = item_from_row(self.ledger.get(capture_id))
        self.assertEqual(item["state"], "reviewed")
        self.assertIs(item["prediction"]["needs_review"], False)
        self.assertEqual(item["review"], {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer",
                                          "time_utc": "2026-11-13T16:00:00.000Z"})
        self.assertIsNone(self.ledger.record_review("d/unknown", REVIEW))

    def test_review_made_offline_waits_in_the_queue(self):
        capture_id = self.classified()
        self.assertEqual(self.ledger.queue_review("d/41", REVIEW), capture_id)
        item = item_from_row(self.ledger.get(capture_id))
        self.assertEqual(item["state"], "reviewed")
        self.assertIs(item["review"]["queued"], True)
        queue = self.ledger.queued_reviews("local-demo|raman-portable")
        self.assertEqual([(entry_["prediction_id"], entry_["review"], entry_["attempts"]) for entry_ in queue],
                         [("d/41", REVIEW, 0)])
        self.assertEqual(self.ledger.queued_reviews("datafed||raman-portable"), [])

        # A second decision on the same prediction replaces the one still waiting.
        changed = dict(REVIEW, status="rejected")
        self.ledger.queue_review("d/41", changed)
        queue = self.ledger.queued_reviews("local-demo|raman-portable")
        self.assertEqual([entry_["review"]["status"] for entry_ in queue], ["rejected"])
        self.assertEqual(len(queue), 1)

        self.ledger.review_failed(queue[0]["id"], "record not found")
        self.assertEqual(self.ledger.queued_reviews("local-demo|raman-portable", max_attempts=1), [])
        self.assertEqual(self.ledger.queued_reviews("local-demo|raman-portable")[0]["last_error"], "record not found")

        self.ledger.review_sent(queue[0]["id"])
        self.assertEqual(self.ledger.queued_reviews("local-demo|raman-portable"), [])
        self.assertNotIn("queued", item_from_row(self.ledger.get(capture_id))["review"])

    def test_queued_review_is_not_overwritten_by_the_catalogs_older_state(self):
        capture_id = self.classified()
        self.ledger.queue_review("d/41", REVIEW)
        self.assertFalse(self.ledger.set_prediction(capture_id, "d/41", PREDICTION))
        self.assertEqual(self.ledger.get(capture_id)["state"], "reviewed")
        self.ledger.review_sent(self.ledger.queued_reviews("local-demo|raman-portable")[0]["id"])
        self.assertTrue(self.ledger.set_prediction(capture_id, "d/41", PREDICTION))


class CatalogBinding(LedgerCase):
    def test_changing_catalog_makes_every_capture_captured_again(self):
        ids = self.add(3)
        self.assertIsNone(self.ledger.catalog_key)
        self.assertFalse(self.ledger.bind_catalog("local-demo|raman-portable"))
        self.assertFalse(self.ledger.bind_catalog("local-demo|raman-portable"))
        self.ledger.mark_registered(ids[0], "d/1", "rs-1", "aa")
        self.ledger.mark_uploaded(ids[0])
        self.ledger.set_prediction(ids[0], "d/41", PREDICTION)
        self.ledger.queue_review("d/41", REVIEW)
        self.ledger.mark_error(ids[1], "upload", "failed")
        broken = dict(entry(9), sample_id=None)
        self.ledger.add_capture(broken, error_message="sample_id is empty")
        self.ledger.add_dark("cd" * 32, "dark_1.txt", "d/5", "rdark-1")

        self.assertTrue(self.ledger.bind_catalog("datafed|p/lab|raman-portable"))
        self.assertEqual(self.ledger.catalog_key, "datafed|p/lab|raman-portable")
        for capture_id in ids:
            row = self.ledger.get(capture_id)
            self.assertEqual(
                (row["state"], row["record_id"], row["upload_task_id"], row["prediction_id"], row["review_json"],
                 row["upload_attempts"], row["error_stage"]),
                ("captured", None, None, None, None, 0, None))
        # A capture that lacks fields still lacks them, whatever the catalog.
        self.assertEqual(self.ledger.get(broken["capture_id"])["error_stage"], "metadata")
        self.assertIsNone(self.ledger.get_dark("cd" * 32))
        # The review made for the first catalog is kept for when the user goes back to it.
        self.assertEqual(len(self.ledger.queued_reviews("local-demo|raman-portable")), 1)
        self.assertEqual(self.ledger.queued_reviews("datafed|p/lab|raman-portable"), [])

    def test_what_a_change_of_catalog_would_make_captured_again_is_counted_without_a_change(self):
        ids = self.add(3)
        self.ledger.add_capture(dict(entry(4), simulated=0))
        self.ledger.add_capture(dict(entry(5), simulated=None))
        self.ledger.add_capture(dict(entry(9), sample_id=None), error_message="sample_id is empty")
        self.ledger.bind_catalog("local-demo|raman-portable")
        self.ledger.mark_registered(ids[0], "d/1", "rs-1", "aa")
        self.ledger.mark_error(ids[1], "upload", "failed")
        before = self.ledger.rows(newest_first=False)
        # Five captures, three of them simulated; the one lacking fields stays as it is.
        self.assertEqual(self.ledger.count_rebound(), (5, 3))
        self.assertEqual(self.ledger.rows(newest_first=False), before)
        self.assertTrue(self.ledger.bind_catalog("local-demo|second-root"))
        self.assertEqual(self.ledger.count(states=["captured"]), 5)
        self.assertEqual(self.ledger.count_held_simulated(), 3)


    def test_review_queued_before_the_first_binding_belongs_to_the_first_catalog(self):
        (capture_id,) = self.add(1)
        self.ledger.set_prediction(capture_id, "d/41", PREDICTION)
        self.ledger.queue_review("d/41", REVIEW)
        self.assertFalse(self.ledger.bind_catalog("local-demo|raman-portable"))
        self.assertEqual(len(self.ledger.queued_reviews("local-demo|raman-portable")), 1)
        self.assertEqual(self.ledger.get(capture_id)["state"], "reviewed")


class DarkSpectra(LedgerCase):
    def test_dark_upload_states(self):
        digest = "cd" * 32
        self.assertIsNone(self.ledger.get_dark(digest))
        self.ledger.add_dark(digest, "dark_1.txt", "d/5", "rdark-1")
        self.ledger.add_dark(digest, "dark_copy.txt", "d/6", "rdark-1")
        row = self.ledger.get_dark(digest)
        self.assertEqual((row["file"], row["record_id"], row["state"]), ("dark_1.txt", "d/5", "registered"))
        self.ledger.dark_uploading(digest, "task/8", "queued")
        self.ledger.dark_status(digest, "running")
        self.assertEqual(self.ledger.get_dark(digest)["upload_status"], "running")
        self.ledger.dark_failed(digest, "endpoint not connected")
        self.assertEqual([(row["file"], row["last_error"]) for row in self.ledger.failed_darks()],
                         [("dark_1.txt", "endpoint not connected")])
        self.assertEqual(len(self.ledger.darks_to_advance(2)), 1)
        self.assertEqual(self.ledger.darks_to_advance(1), [])
        self.ledger.dark_uploaded(digest)
        row = self.ledger.get_dark(digest)
        self.assertEqual((row["state"], row["attempts"], row["last_error"]), ("uploaded", 0, None))
        self.assertEqual(self.ledger.darks_to_advance(5), [])


class SmallValues(LedgerCase):
    def test_key_value_table(self):
        self.assertIsNone(self.ledger.get_value("last_sync_utc"))
        self.assertEqual(self.ledger.get_value("last_sync_utc", "never"), "never")
        self.ledger.set_value("last_sync_utc", "2026-11-13T15:15:00.000Z")
        self.ledger.set_value("last_sync_utc", "2026-11-13T15:16:00.000Z")
        self.assertEqual(self.open().get_value("last_sync_utc"), "2026-11-13T15:16:00.000Z")


class SeveralProcesses(LedgerCase):
    def test_sync_lock_is_exclusive_and_given_back(self):
        other = self.open()
        with self.ledger.sync_lock() as first:
            self.assertTrue(first)
            with other.sync_lock() as second:
                self.assertFalse(second)
        with other.sync_lock() as third:
            self.assertTrue(third)

    def test_sync_lock_is_given_back_when_the_holder_dies(self):
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys, time\n"
             "handle = open(sys.argv[1], 'a')\n"
             "fcntl.flock(handle, fcntl.LOCK_EX)\n"
             "print('locked', flush=True)\n"
             "time.sleep(60)\n",
             str(self.ledger.lock_path)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        with self.ledger.sync_lock() as held:
            self.assertFalse(held)
        holder.kill()
        holder.wait()
        holder.stdout.close()
        with self.ledger.sync_lock() as held:
            self.assertTrue(held)

    def test_processes_writing_at_once_lose_no_row(self):
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from raman_datafed.ledger import Ledger\n"
            "ledger = Ledger(sys.argv[2])\n"
            "for number in range(40):\n"
            "    capture_id = '%s_%d' % (sys.argv[3], number)\n"
            "    ledger.add_capture({'capture_id': capture_id, 'session_id': sys.argv[3],\n"
            "                        'session_folder': sys.argv[3], 'point_file': 'point_%04d.json' % number,\n"
            "                        'point_id': number})\n"
            "    ledger.mark_registered(capture_id, 'd/%d' % number, 'rs-%d' % number, 'aa')\n"
        )
        writers = [
            subprocess.Popen([sys.executable, "-c", script, str(support.DATA_LAYER), str(self.home / "data_catalog"),
                              name], env=support.cli_environment(self.home))
            for name in ("a", "b", "c", "d")
        ]
        self.assertEqual([writer.wait(timeout=60) for writer in writers], [0, 0, 0, 0])
        self.assertEqual(self.ledger.counts()["registered"], 160)

    def test_a_write_that_fails_half_way_leaves_nothing_behind(self):
        self.add(1)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger._write() as db:
                db.execute("UPDATE captures SET state = 'registered'")
                db.execute("INSERT INTO captures (capture_id) VALUES ('incomplete')")
        self.assertEqual(self.ledger.counts()["captured"], 1)
        self.assertEqual(self.ledger.add_capture(entry(2)), "added")


if __name__ == "__main__":
    unittest.main()
