"""The local demo catalog: the catalog interface on files, with DataFed-like behaviour."""

import json
import subprocess
import sys
import unittest
from unittest import mock

import support
from raman_datafed.catalog import (
    Catalog,
    CatalogConflict,
    CatalogError,
    CatalogNotFound,
    CatalogOffline,
    Dep,
    RecordSummary,
    Transfer,
    fit_title,
    title_length,
)
from raman_datafed.local_catalog import LocalDemoCatalog


class LocalCatalogCase(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.root = self.home / "data_catalog" / "local_demo"
        self.catalog = LocalDemoCatalog(self.root)

    def raw_and_prediction(self):
        self.catalog.ensure_collection("spectra", "Spectra")
        raw = self.catalog.create_record("rs-one", "Raw", {"capture_id": "c1"}, "spectra", identity_key="capture_id")
        prediction = self.catalog.create_record(
            "rp-one", "Prediction", {"capture_id": "c1", "label": "r6g"}, "spectra", deps=[("der", raw.id)],
            identity_key="capture_id")
        return raw, prediction


class Identity(LocalCatalogCase):
    def test_it_is_a_catalog_and_says_it_is_a_demo(self):
        self.assertIsInstance(self.catalog, Catalog)
        self.assertEqual(self.catalog.name, "local-demo")
        self.assertIn("nothing is sent anywhere", self.catalog.label)
        who = self.catalog.whoami()
        self.assertEqual(set(who), {"online", "user", "context", "endpoint", "demo"})
        self.assertEqual((who["online"], who["demo"], who["endpoint"]), (True, True, None))

    def test_the_folder_explains_itself(self):
        self.catalog.ensure_collection("spectra", "Spectra")
        readme = (self.root / "README.txt").read_text(encoding="utf-8")
        self.assertIn("Nothing in here has been sent", readme)

    def test_ids_look_like_datafed_ids(self):
        collection = self.catalog.ensure_collection("spectra", "Spectra")
        raw, prediction = self.raw_and_prediction()
        transfer = self.catalog.put_data(raw.id, self.home / "missing.json")
        self.assertRegex(collection.id, r"^c/\d+$")
        self.assertRegex(raw.id, r"^d/\d+$")
        self.assertRegex(transfer.task_id, r"^task/\d+$")
        self.assertNotEqual(raw.id, prediction.id)


class Collections(LocalCatalogCase):
    def test_ensure_collection_is_idempotent(self):
        first = self.catalog.ensure_collection("spectra", "Spectra", "All of them")
        second = self.catalog.ensure_collection("spectra", "Another title")
        self.assertEqual(first, second)
        self.assertEqual((first.alias, first.title, first.description, first.parent), ("spectra", "Spectra",
                                                                                      "All of them", "c/root"))
        self.assertEqual(len(list((self.root / "collections").glob("c_*.json"))), 2)  # the root and this one

    def test_nested_collections_are_listed_in_their_parent(self):
        parent = self.catalog.ensure_collection("spectra", "Spectra")
        child = self.catalog.ensure_collection("session-1", "Session 1", parent="spectra")
        self.assertEqual(child.parent, parent.id)
        self.assertEqual(self.catalog.list_collection("spectra"), [RecordSummary(child.id, "session-1", "Session 1")])
        self.assertEqual(self.catalog.list_collection(parent.id), self.catalog.list_collection("spectra"))

    def test_unknown_parent_and_bad_alias_are_refused(self):
        with self.assertRaises(CatalogNotFound):
            self.catalog.ensure_collection("child", "Child", parent="no-such-parent")
        for alias in ("Upper", "has space", "x" * 41, "", ".hidden"):
            with self.assertRaises(CatalogError, msg=alias):
                self.catalog.ensure_collection(alias, "Bad")

    def test_alias_of_a_record_cannot_become_a_collection(self):
        self.raw_and_prediction()
        with self.assertRaises(CatalogConflict):
            self.catalog.ensure_collection("rs-one", "Clash")

    def test_link_and_unlink_are_idempotent(self):
        raw, _prediction = self.raw_and_prediction()
        self.catalog.ensure_collection("queue", "Queue")
        self.catalog.link("queue", raw.id)
        self.catalog.link("queue", "rs-one")
        self.assertEqual([item.id for item in self.catalog.list_collection("queue")], [raw.id])
        self.catalog.unlink("queue", raw.id)
        self.catalog.unlink("queue", raw.id)
        self.assertEqual(self.catalog.list_collection("queue"), [])
        # Still in the collection it was created in.
        self.assertIn(raw.id, [item.id for item in self.catalog.list_collection("spectra")])
        with self.assertRaises(CatalogNotFound):
            self.catalog.link("no-such-queue", raw.id)
        with self.assertRaises(CatalogNotFound):
            self.catalog.link("queue", "d/999")


class Records(LocalCatalogCase):
    def test_same_alias_and_identity_returns_the_existing_record(self):
        raw, _prediction = self.raw_and_prediction()
        self.assertTrue(raw.created)
        again = self.catalog.create_record("rs-one", "Other title", {"capture_id": "c1", "extra": 1}, "spectra",
                                           identity_key="capture_id")
        self.assertEqual(again.id, raw.id)
        self.assertFalse(again.created)
        self.assertEqual(again.metadata, {"capture_id": "c1"})
        self.assertEqual(len(self.catalog.list_collection("spectra")), 2)

    def test_same_alias_with_a_different_identity_is_a_conflict(self):
        self.raw_and_prediction()
        with self.assertRaises(CatalogConflict) as raised:
            self.catalog.create_record("rs-one", "Raw", {"capture_id": "c2"}, "spectra", identity_key="capture_id")
        self.assertIn("different capture_id", str(raised.exception))
        self.assertIsInstance(raised.exception, CatalogError)

    def test_without_identity_key_the_alias_alone_identifies(self):
        raw, _prediction = self.raw_and_prediction()
        self.assertEqual(self.catalog.create_record("rs-one", "Raw", {"anything": 1}, "spectra").id, raw.id)

    def test_identity_key_must_be_in_the_metadata(self):
        self.raw_and_prediction()
        with self.assertRaises(CatalogError):
            self.catalog.create_record("rs-one", "Raw", {"other": 1}, "spectra", identity_key="capture_id")

    def test_get_by_id_or_alias(self):
        raw, _prediction = self.raw_and_prediction()
        self.assertEqual(self.catalog.get_record(raw.id).alias, "rs-one")
        self.assertEqual(self.catalog.get_record("rs-one").id, raw.id)
        for name in ("d/999", "no-such-alias", "Not An Alias", "spectra"):
            with self.assertRaises(CatalogNotFound, msg=name):
                self.catalog.get_record(name)

    def test_dependencies_are_readable_from_both_ends(self):
        raw, prediction = self.raw_and_prediction()
        self.assertEqual(self.catalog.get_record(prediction.id).deps, (Dep("der", raw.id, "rs-one", "out"),))
        self.assertEqual(self.catalog.get_record(raw.id).deps, (Dep("der", prediction.id, "rp-one", "in"),))

    def test_unknown_dependency_target_or_relation_is_refused(self):
        self.catalog.ensure_collection("spectra", "Spectra")
        with self.assertRaises(CatalogNotFound):
            self.catalog.create_record("rp-two", "P", {}, "spectra", deps=[("der", "d/999")])
        with self.assertRaises(CatalogError):
            self.catalog.create_record("rp-two", "P", {}, "spectra", deps=[("copied-from", "d/1")])
        self.assertEqual(self.catalog.list_collection("spectra"), [])

    def test_update_merges_nested_metadata_and_drops_null_keys_like_datafed(self):
        _raw, prediction = self.raw_and_prediction()
        self.catalog.update_record(prediction.id, metadata={
            "review": {"status": "pending", "label": None, "reviewer": None}, "needs_review": True})
        updated = self.catalog.update_record("rp-one", metadata={
            "review": {"status": "confirmed", "reviewer": "Test Reviewer"}, "needs_review": False}, title="Checked")
        self.assertEqual(updated.metadata, {
            "capture_id": "c1", "label": "r6g", "needs_review": False,
            "review": {"status": "confirmed", "reviewer": "Test Reviewer"}})
        self.assertEqual(updated.title, "Checked")
        self.assertEqual(self.catalog.get_record("rp-one").metadata, updated.metadata)

    def test_update_can_add_a_dependency_once(self):
        raw, prediction = self.raw_and_prediction()
        model = self.catalog.create_record("rmodel-one", "Model", {"name": "m"}, "spectra")
        self.catalog.update_record(prediction.id, deps_add=[("der", model.id)])
        updated = self.catalog.update_record(prediction.id, deps_add=[Dep("der", model.id, None, "out")])
        self.assertEqual({(dep.id, dep.direction) for dep in updated.deps}, {(raw.id, "out"), (model.id, "out")})
        self.assertEqual(self.catalog.get_record(model.id).deps, (Dep("der", prediction.id, "rp-one", "in"),))

    def test_long_titles_are_shortened_like_datafed_requires(self):
        self.catalog.ensure_collection("spectra", "Spectra")
        record = self.catalog.create_record("rs-long", "T" * 200, {}, "spectra")
        self.assertEqual(len(record.title), 80)
        # Counted as the service's JavaScript counts: a character outside the BMP is two units.
        record = self.catalog.create_record("rs-emoji", "\U0001F52C" * 50, {}, "spectra")
        self.assertEqual((record.title, title_length(record.title)), ("\U0001F52C" * 38 + "...", 79))
        self.assertEqual(fit_title("S" * 100, " point 3 [simulated]"), "S" * 57 + "... point 3 [simulated]")
        self.assertEqual(fit_title("\U0001F52C" * 40, " point 3"), "\U0001F52C" * 34 + "... point 3")

    def test_interrupted_creation_is_finished_by_the_retry(self):
        self.catalog.ensure_collection("spectra", "Spectra")
        with mock.patch.object(LocalDemoCatalog, "_save", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.catalog.create_record("rs-one", "Raw", {"capture_id": "c1"}, "spectra", identity_key="capture_id")
        claimed = (self.root / "aliases" / "rs-one").read_text(encoding="utf-8").strip()
        record = self.catalog.create_record("rs-one", "Raw", {"capture_id": "c1"}, "spectra", identity_key="capture_id")
        self.assertEqual(record.id, claimed)
        self.assertTrue(record.created)
        self.assertEqual([item.id for item in self.catalog.list_collection("spectra")], [claimed])

    def test_files_are_complete_json_and_no_temporary_file_stays(self):
        self.raw_and_prediction()
        for path in self.root.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([path.name for path in self.root.rglob("*.tmp")], [])


class Transfers(LocalCatalogCase):
    def test_upload_advances_one_step_per_status_call(self):
        raw, _prediction = self.raw_and_prediction()
        source = self.home / "point_0001.json"
        source.write_text('{"intensity": [1, 2, 3]}', encoding="utf-8")

        started = self.catalog.put_data(raw.id, source)
        self.assertEqual(started, Transfer(started.task_id, "queued", ""))
        self.assertEqual(self.catalog.get_record(raw.id).data_size, 0)
        with self.assertRaises(CatalogNotFound):
            self.catalog.get_data(raw.id, self.home / "out")

        self.assertEqual(self.catalog.transfer_status(started.task_id).status, "running")
        self.assertEqual(self.catalog.get_record(raw.id).data_size, 0)
        self.assertEqual(self.catalog.transfer_status(started.task_id).status, "succeeded")
        self.assertEqual(self.catalog.transfer_status(started.task_id).status, "succeeded")
        self.assertEqual(self.catalog.get_record(raw.id).data_size, source.stat().st_size)

        fetched = self.catalog.get_data("rs-one", self.home / "out")
        self.assertEqual(fetched, self.home / "out" / "point_0001.json")
        self.assertEqual(fetched.read_bytes(), source.read_bytes())

    def test_missing_source_file_gives_a_failed_task(self):
        raw, _prediction = self.raw_and_prediction()
        started = self.catalog.put_data(raw.id, self.home / "gone.json")
        self.catalog.transfer_status(started.task_id)
        failed = self.catalog.transfer_status(started.task_id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("gone.json", failed.message)
        self.assertEqual(self.catalog.transfer_status(started.task_id), failed)

    def test_unknown_task_and_unknown_record(self):
        self.raw_and_prediction()
        with self.assertRaises(CatalogNotFound):
            self.catalog.transfer_status("task/999")
        with self.assertRaises(CatalogNotFound):
            self.catalog.put_data("d/999", self.home / "x.json")


class Offline(LocalCatalogCase):
    def test_offline_flag_makes_every_call_fail_and_changes_nothing(self):
        raw, prediction = self.raw_and_prediction()
        source = self.home / "point_0001.json"
        source.write_text("{}", encoding="utf-8")
        task = self.catalog.put_data(raw.id, source)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        self.catalog.set_offline(True)
        self.assertTrue((self.root / "OFFLINE").is_file())
        calls = [
            lambda: self.catalog.ensure_collection("other", "Other"),
            lambda: self.catalog.create_record("rs-two", "Raw", {}, "spectra"),
            lambda: self.catalog.get_record(raw.id),
            lambda: self.catalog.update_record(raw.id, metadata={"a": 1}),
            lambda: self.catalog.link("spectra", prediction.id),
            lambda: self.catalog.unlink("spectra", prediction.id),
            lambda: self.catalog.list_collection("spectra"),
            lambda: self.catalog.put_data(raw.id, source),
            lambda: self.catalog.transfer_status(task.task_id),
            lambda: self.catalog.get_data(raw.id, self.home / "out"),
        ]
        for call in calls:
            with self.assertRaises(CatalogOffline):
                call()
        self.assertIs(self.catalog.whoami()["online"], False)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file() and path.name != "OFFLINE"}
        self.assertEqual(after, before)

        self.catalog.set_offline(False)
        self.assertIs(self.catalog.whoami()["online"], True)
        self.assertEqual(self.catalog.transfer_status(task.task_id).status, "running")

    def test_flag_file_made_by_hand_counts(self):
        self.root.mkdir(parents=True)
        (self.root / "OFFLINE").write_text("", encoding="utf-8")
        with self.assertRaises(CatalogOffline):
            self.catalog.ensure_collection("spectra", "Spectra")


class TwoProcesses(LocalCatalogCase):
    def test_processes_creating_records_at_once_get_distinct_ids(self):
        self.catalog.ensure_collection("spectra", "Spectra")
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from raman_datafed.local_catalog import LocalDemoCatalog\n"
            "catalog = LocalDemoCatalog(sys.argv[2])\n"
            "for number in range(15):\n"
            "    alias = 'rs-%s-%d' % (sys.argv[3], number)\n"
            "    catalog.create_record(alias, alias, {'capture_id': alias}, 'spectra', identity_key='capture_id')\n"
        )
        workers = [
            subprocess.Popen([sys.executable, "-c", script, str(support.DATA_LAYER), str(self.root), name],
                             env=support.cli_environment(self.home))
            for name in ("a", "b", "c")
        ]
        self.assertEqual([worker.wait(timeout=60) for worker in workers], [0, 0, 0])
        items = self.catalog.list_collection("spectra")
        self.assertEqual(len(items), 45)
        self.assertEqual(len({item.id for item in items}), 45)
        self.assertEqual(len({item.alias for item in items}), 45)


if __name__ == "__main__":
    unittest.main()
