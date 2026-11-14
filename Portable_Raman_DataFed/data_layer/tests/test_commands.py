"""The core commands, called in this process the way the Studio server calls them."""

import json
import os
import shutil
import unittest
from argparse import Namespace
from unittest import mock

import support
from raman_datafed import cli, commands_core, sync
from raman_datafed.catalog import CatalogNotFound, raw_alias
from raman_datafed.config import CommandError, ConfigError, load_settings
from raman_datafed.datafed_catalog import DataFedCatalog
from raman_datafed.ledger import STATES
from raman_datafed.metadata import MetadataError

STATUS_KEYS = {"mode", "mode_label", "demo", "online", "user", "context", "endpoint", "counts", "pending",
               "held_simulated", "sync_work", "last_sync_utc", "problems"}
SYNC_KEYS = {"online", "ingested", "registered", "uploads_started", "uploads_done", "predictions_found",
             "reviews_sent", "held_simulated", "errors", "status"}


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.ctx = support.context(self, self.home)
        support.write_dark(self.home)
        # The classifier part, when installed, is tested on its own; here sync is the core only.
        patcher = mock.patch.object(sync, "_optional_module", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self, **options):
        return commands_core.cmd_sync(Namespace(**options), self.ctx)

    def set_mode(self, mode):
        settings = load_settings(self.home)
        settings.set("mode", mode)
        settings.save()
        self.ctx.reload()


class Status(CommandCase):
    def test_status_of_an_empty_home(self):
        status = commands_core.cmd_status(Namespace(), self.ctx)
        self.assertLessEqual(STATUS_KEYS, set(status))
        self.assertEqual((status["mode"], status["demo"], status["online"]), ("local-demo", True, True))
        self.assertIn("nothing is sent anywhere", status["mode_label"])
        self.assertEqual(list(status["counts"]), list(STATES))
        self.assertEqual((status["pending"], status["held_simulated"], status["sync_work"], status["last_sync_utc"],
                          status["problems"]), (0, 0, 0, None, []))

    def test_sync_work_leaves_out_what_a_sync_cannot_move_on(self):
        settings = load_settings(self.home)
        settings.set("sync.max_attempts", 1)
        settings.save()
        support.make_session(self.home, points=3)
        support.make_session(self.home, folder="old_scan", points=2, with_capture=False)
        self.ctx.catalog.set_offline(True)
        status = self.sync()["status"]
        # The two captures without their fields wait for import-session, not for a sync.
        self.assertEqual((status["pending"], status["sync_work"]), (5, 3))
        self.ctx.catalog.set_offline(False)
        status = self.sync()["status"]
        self.assertEqual((status["counts"]["uploading"], status["sync_work"]), (3, 4), "three uploads and the dark's")
        status = self.sync()["status"]
        self.assertEqual((status["counts"]["uploaded"], status["pending"], status["sync_work"]), (3, 2, 0))

        lost = support.make_session(self.home, folder="lost_scan", points=1)
        self.sync()
        lost["files"][0].unlink()
        status = self.sync()["status"]
        # Failed once with one attempt allowed: only Sync now (sync --retry-failed) tries it again.
        self.assertEqual((status["counts"]["error"], status["sync_work"]), (3, 0))

    def test_status_does_nothing_but_report(self):
        support.make_session(self.home, points=2)
        status = commands_core.cmd_status(Namespace(), self.ctx)
        self.assertEqual(status["total"], 0)
        self.assertFalse((self.home / "data_catalog" / "local_demo").exists())

    def test_status_counts_pending_and_names_problems(self):
        support.make_session(self.home, points=3)
        support.make_session(self.home, folder="old_scan", points=2, with_capture=False)
        self.ctx.catalog.set_offline(True)
        result = self.sync()
        status = result["status"]
        self.assertEqual((result["online"], status["online"]), (False, False))
        self.assertEqual((status["counts"]["captured"], status["counts"]["error"], status["pending"]), (3, 2, 5))
        self.assertRegex(status["last_sync_utc"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(len(status["problems"]), 2)
        self.assertEqual(status["problems"][0], "The catalog cannot be reached. 5 capture(s) wait for the next sync.")
        self.assertIn("2 capture(s) cannot be registered as their files are", status["problems"][1])
        # The window's way and the command line's way to supply the fields.
        self.assertIn("Data tab: Import scan folder", status["problems"][1])
        self.assertIn("import-session --folder", status["problems"][1])

    def test_status_names_captures_that_are_retried_or_given_up(self):
        settings = load_settings(self.home)
        settings.set("sync.max_attempts", 2)
        settings.save()
        session = support.make_session(self.home, points=1)
        self.sync()
        session["files"][0].unlink()
        problems = self.sync()["status"]["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn("1 capture(s) failed and will be tried again", problems[0])
        problems = self.sync()["status"]["problems"]
        self.assertIn("failed 2 times and are no longer retried", problems[0])
        self.assertIn("sync --retry-failed", problems[0])

    def test_status_after_the_settings_changed_says_what_the_next_sync_does(self):
        support.make_session(self.home, points=1)
        self.sync()
        commands_core.cmd_config(Namespace(set=[["datafed.root_alias=second-root"]]), self.ctx)
        problems = commands_core.cmd_status(Namespace(), self.ctx)["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn("the next sync registers every capture", problems[0])

    def test_invalid_settings_stop_commands_that_act_on_them(self):
        (self.home / "datafed_config.json").write_text(json.dumps({"mode": "cloud"}), encoding="utf-8")
        with self.assertRaises(ConfigError) as raised:
            commands_core.cmd_status(Namespace(), self.ctx)
        self.assertIn("mode must be", raised.exception.detail)


class Sync(CommandCase):
    def test_sync_reports_counters_and_the_status(self):
        support.make_session(self.home, points=2)
        result = self.sync()
        self.assertLessEqual(SYNC_KEYS, set(result))
        self.assertEqual((result["busy"], result["online"], result["ingested"], result["registered"],
                          result["uploads_started"], result["errors"]), (False, True, 2, 2, 2, []))
        self.assertLessEqual(STATUS_KEYS, set(result["status"]))
        self.assertEqual(result["status"]["counts"]["uploading"], 2)
        self.assertEqual(result["status"], commands_core.cmd_status(Namespace(), self.ctx))

    def test_sync_with_nothing_to_send_still_says_whether_the_catalog_is_reachable(self):
        self.ctx.catalog.set_offline(True)
        result = self.sync()
        self.assertEqual((result["online"], result["status"]["online"]), (False, False))

    def test_busy_sync_answers_at_once_without_asking_the_catalog(self):
        support.make_session(self.home, points=2)
        with self.ctx.ledger.sync_lock():
            with mock.patch.object(self.ctx.catalog, "whoami", side_effect=AssertionError("asked the catalog")):
                result = self.sync()
        self.assertEqual((result["busy"], result["online"], result["ingested"]), (True, None, 0))
        self.assertEqual((result["status"]["online"], result["status"]["total"]), (None, 0))

    def test_retry_failed_option(self):
        settings = load_settings(self.home)
        settings.set("sync.max_attempts", 1)
        settings.save()
        session = support.make_session(self.home, points=1)
        self.sync()
        hidden = session["files"][0].with_suffix(".hidden")
        session["files"][0].rename(hidden)
        self.sync()
        hidden.rename(session["files"][0])
        self.assertEqual(self.sync()["uploads_started"], 0)
        self.assertEqual(self.sync(retry_failed=True)["uploads_started"], 1)


class List(CommandCase):
    def setUp(self):
        super().setUp()
        self.first = support.make_session(self.home, folder="1d_x_scan_a", points=3)
        self.second = support.make_session(self.home, folder="2d_scan_b", points=2, scan_mode="2d")
        self.sync()
        self.sync()
        raw = self.ctx.catalog.get_record(raw_alias(self.first["capture_ids"][0]))
        self.prediction = support.save_prediction(self.ctx.catalog, self.ctx.config.root_alias, raw,
                                                  "low_confidence", confidence=0.61)
        self.sync()

    def list(self, **options):
        return commands_core.cmd_list(Namespace(**options), self.ctx)

    def test_items_newest_first_with_total(self):
        listing = self.list()
        self.assertEqual(listing["total"], 5)
        times = [item["timestamp_utc"] for item in listing["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(listing["items"]), 5)

    def test_item_shape(self):
        items = {item["capture_id"]: item for item in self.list()["items"]}
        item = items[self.first["capture_ids"][0]]
        record = self.ctx.catalog.get_record(raw_alias(item["capture_id"]))
        self.assertEqual(item, {
            "capture_id": self.first["capture_ids"][0], "session_id": self.first["session_id"],
            "session_folder": "1d_x_scan_a", "point_id": 1, "point_file": "point_0001.json", "scan_mode": "1d-x",
            "sample_id": "S-001", "operator": "Test Operator", "timestamp_utc": "2026-11-13T15:15:02.250Z",
            "simulated": True, "position_mm": {"x": 0.0, "y": 2.5, "z": 4.0},
            "state": "needs_review", "state_label": "Needs review",
            "record_id": record.id, "record_alias": record.alias,
            "upload": {"task_id": item["upload"]["task_id"], "status": "succeeded"},
            "prediction": {"record_id": self.prediction.id, "label": "UNKNOWN", "top_class": "r6g",
                           "confidence": 0.61, "outcome": "low_confidence", "needs_review": True},
            "review": {"status": "pending", "label": None, "reviewer": None, "time_utc": None},
            "error": None,
        })
        self.assertRegex(item["upload"]["task_id"], r"^task/\d+$")
        other = items[self.second["capture_ids"][0]]
        self.assertEqual((other["state"], other["prediction"], other["review"], other["scan_mode"]),
                         ("uploaded", None, None, "2d"))
        json.dumps(items)

    def test_filters_and_limit(self):
        self.assertEqual(self.list(session=self.second["session_id"])["total"], 2)
        self.assertEqual(self.list(session="1d_x_scan_a")["total"], 3)
        needs_review = self.list(state="needs_review")
        self.assertEqual([item["capture_id"] for item in needs_review["items"]], [self.first["capture_ids"][0]])
        limited = self.list(limit=2)
        self.assertEqual((len(limited["items"]), limited["total"]), (2, 5))
        self.assertEqual(self.list(state="error"), {"items": [], "total": 0})
        with self.assertRaises(CommandError):
            self.list(state="finished")
        with self.assertRaises(CommandError):
            self.list(limit=0)


class Provenance(CommandCase):
    def setUp(self):
        super().setUp()
        self.session = support.make_session(self.home, points=2)
        self.sync()
        self.sync()
        catalog, root = self.ctx.catalog, self.ctx.config.root_alias
        self.raws = [catalog.get_record(raw_alias(capture_id)) for capture_id in self.session["capture_ids"]]
        self.predictions = [support.save_prediction(catalog, root, raw, "confident") for raw in self.raws]

    def provenance(self, record):
        return commands_core.cmd_provenance(Namespace(record=record), self.ctx)

    def test_chain_around_a_raw_spectrum(self):
        chain = self.provenance(self.raws[0].id)
        nodes = {node["id"]: node for node in chain["nodes"]}
        model = self.predictions[0].metadata["model"]["record"]
        dark = self.raws[0].metadata["dark"]["record"]
        # The other spectrum and its prediction share the model and the dark, but are not this record's history.
        self.assertEqual(set(nodes), {self.raws[0].id, self.predictions[0].id, model, dark})
        self.assertEqual({node["role"] for node in nodes.values()}, {"start", "derived", "source"})
        self.assertEqual((nodes[self.raws[0].id]["role"], nodes[self.raws[0].id]["type"]),
                         ("start", "raman.raw_spectrum"))
        self.assertEqual(nodes[self.predictions[0].id]["info"]["label"], "r6g")
        self.assertEqual(nodes[self.predictions[0].id]["info"]["review_status"], "not_required")
        self.assertEqual({(edge["from"], edge["to"], edge["relation"]) for edge in chain["edges"]}, {
            (self.predictions[0].id, self.raws[0].id, "der"),
            (self.predictions[0].id, model, "der"),
            (self.predictions[0].id, dark, "der"),
        })
        self.assertEqual(chain["edges"][0]["label"], "is derived from")
        self.assertIs(chain["truncated"], False)

    def test_chain_from_a_prediction_and_by_alias(self):
        chain = self.provenance(self.predictions[1].alias)
        self.assertEqual(len(chain["nodes"]), 4)
        self.assertEqual(chain["nodes"][0]["id"], self.predictions[1].id)
        self.assertEqual({edge["from"] for edge in chain["edges"]}, {self.predictions[1].id})

    def test_chain_from_the_model_lists_what_was_made_with_it(self):
        model = self.predictions[0].metadata["model"]["record"]
        chain = self.provenance(model)
        self.assertEqual(len(chain["nodes"]), 6)
        limited = commands_core.provenance(self.ctx.catalog, model, limit=3)
        self.assertEqual((len(limited["nodes"]), limited["truncated"]), (3, True))

    def test_unknown_record(self):
        with self.assertRaises(CatalogNotFound):
            self.provenance("d/99999")


class ImportSession(CommandCase):
    def setUp(self):
        super().setUp()
        self.outside = support.make_home(self)

    def legacy(self, **options):
        return support.make_session(self.home, root=self.outside, with_capture=False, with_session_json=False,
                                    **options)

    def run_import(self, folder, **options):
        options.setdefault("sample_id", "S-OLD")
        options.setdefault("operator", "Test Operator")
        options.setdefault("location", "Bench 2")
        return commands_core.cmd_import_session(Namespace(folder=str(folder), **options), self.ctx)

    def test_folder_from_elsewhere_is_copied_completed_and_put_in_the_ledger(self):
        session = self.legacy(folder="2d_scan_20260918_174032_408", points=3, scan_mode="2d")
        (session["folder"] / "laser_log_combined.txt").write_text("log", encoding="utf-8")
        before = {path.name: path.read_bytes() for path in session["folder"].iterdir()}

        result = self.run_import(session["folder"], simulated="yes", notes="imported for the demo")
        self.assertEqual(result, {"ingested": 3, "skipped": 0, "errors": [], "folder": "2d_scan_20260918_174032_408",
                                  "session_id": session["session_id"], "copied": True})
        # The source folder is never changed.
        self.assertEqual({path.name: path.read_bytes() for path in session["folder"].iterdir()}, before)
        copy = self.home / "scan_sessions" / "2d_scan_20260918_174032_408"
        self.assertEqual({path.name for path in copy.iterdir()},
                         set(before) | {"session.json"})
        self.assertEqual([path.name for path in (self.home / "scan_sessions").iterdir()], [copy.name])
        for name, content in before.items():
            if name.startswith("point_"):
                old, new = json.loads(content), json.loads((copy / name).read_text(encoding="utf-8"))
                capture = new.pop("capture")
                self.assertEqual(new, old)
                self.assertEqual((capture["sample_id"], capture["imported"], capture["simulated"], capture["notes"]),
                                 ("S-OLD", True, {"backend": True, "motor": True}, "imported for the demo"))
        self.assertEqual(self.ctx.ledger.counts()["captured"], 3)

        synced = self.sync()
        self.assertEqual((synced["ingested"], synced["registered"], synced["errors"]), (0, 3, []))
        record = self.ctx.catalog.get_record(raw_alias(session["capture_ids"][0]))
        self.assertEqual((record.metadata["sample_id"], record.metadata["imported"], record.metadata["simulated"]),
                         ("S-OLD", True, True))

    def test_importing_twice_adds_nothing(self):
        session = self.legacy(points=2)
        self.run_import(session["folder"])
        again = self.run_import(session["folder"])
        self.assertEqual((again["ingested"], again["skipped"], again["copied"], again["folder"]),
                         (0, 2, False, session["folder"].name))
        self.assertEqual(len(list((self.home / "scan_sessions").iterdir())), 1)
        self.assertEqual(self.ctx.ledger.count(), 2)

    def test_another_session_with_the_same_folder_name_gets_its_own_folder(self):
        first = self.legacy(folder="scan", points=1)
        self.run_import(first["folder"])
        shutil.rmtree(first["folder"])
        second = self.legacy(folder="scan", points=1)
        result = self.run_import(second["folder"])
        self.assertEqual((result["folder"], result["copied"], result["ingested"]), ("scan_1", True, 1))
        self.assertEqual(json.loads((self.home / "scan_sessions" / "scan_1" / "session.json").read_text())["folder"],
                         "scan_1")

    def test_session_already_in_scan_sessions_is_completed_in_place(self):
        session = support.make_session(self.home, points=2, with_capture=False, with_session_json=False)
        self.assertEqual(self.sync()["ingested"], 0)
        result = self.run_import(session["folder"])
        self.assertEqual((result["ingested"], result["copied"], result["folder"]), (2, False, session["folder"].name))
        self.assertTrue((session["folder"] / "session.json").is_file())
        self.assertEqual(self.sync()["registered"], 2)

    def test_points_waiting_in_error_metadata_are_fixed_by_the_import(self):
        session = support.make_session(self.home, points=2, with_capture=False)
        self.sync()
        self.assertEqual(self.ctx.ledger.counts()["error"], 2)
        result = self.run_import(session["folder"])
        self.assertEqual((result["ingested"], result["errors"]), (2, []))
        self.assertEqual(self.ctx.ledger.counts()["captured"], 2)

    def test_missing_fields_are_asked_for_and_nothing_is_copied(self):
        session = self.legacy(points=1)
        dark = support.write_dark(self.outside, name="dark_20260922_124418_305.txt")
        with self.assertRaises(MetadataError) as raised:
            self.run_import(session["folder"], operator=None, location="  ", dark_file=str(dark))
        self.assertIn("--operator, --location", str(raised.exception))
        self.assertFalse((self.home / "scan_sessions").exists())
        self.assertFalse((self.home / "dark_measurements" / dark.name).exists())

    def test_folder_that_does_not_exist_or_is_no_session(self):
        with self.assertRaises(CommandError):
            self.run_import(self.outside / "nowhere")
        (self.outside / "notes").mkdir()
        with self.assertRaises(MetadataError):
            self.run_import(self.outside / "notes")
        working = support.make_session(self.home, folder="_backend_work", points=1, with_capture=False)
        with self.assertRaisesRegex(CommandError, "start"):
            self.run_import(working["folder"])

    def test_folder_name_starting_with_an_underscore_is_renamed_on_copy(self):
        session = self.legacy(folder="_old_scan", points=1)
        self.assertEqual(self.run_import(session["folder"])["folder"], "old_scan")

    def test_dark_file_by_name_or_copied_from_a_path(self):
        session = self.legacy(folder="by_name", points=1)
        self.run_import(session["folder"], dark_file=support.DARK_NAME, laser_power="MEDIUM")
        capture = json.loads((self.home / "scan_sessions" / "by_name" / "point_0001.json").read_text())["capture"]
        self.assertEqual((capture["dark_file"], capture["laser_power_level"]), (support.DARK_NAME, "MEDIUM"))

        other_dark = support.write_dark(self.outside, name="dark_20260922_124418_305.txt", level=4400)
        session = self.legacy(folder="by_path", points=1)
        self.run_import(session["folder"], dark_file=str(other_dark))
        copied = self.home / "dark_measurements" / other_dark.name
        self.assertEqual(copied.read_bytes(), other_dark.read_bytes())
        # The description beside the dark comes along: without it a simulated dark would count as real.
        self.assertEqual(copied.with_suffix(".json").read_bytes(), other_dark.with_suffix(".json").read_bytes())
        self.assertEqual([path.name for path in copied.parent.iterdir() if path.name.startswith(".")], [])
        self.sync()
        record = self.ctx.catalog.get_record(raw_alias(session["capture_ids"][0]))
        self.assertEqual(record.metadata["dark"]["file"], other_dark.name)
        self.assertIsNotNone(record.metadata["dark"]["record"])

        with self.assertRaises(CommandError):
            self.run_import(self.legacy(folder="no_dark", points=1)["folder"], dark_file="dark_missing.txt")
        clash = support.write_dark(support.make_home(self), name=support.DARK_NAME, level=1)
        with self.assertRaisesRegex(CommandError, "different file"):
            self.run_import(self.legacy(folder="clash", points=1)["folder"], dark_file=str(clash))


class Config(CommandCase):
    def config(self, *assignments):
        return commands_core.cmd_config(Namespace(set=[list(assignments)] if assignments else None), self.ctx)

    def test_show(self):
        shown = self.config()
        self.assertEqual(set(shown), {"config", "path"})
        self.assertEqual(shown["path"], str(self.home / "datafed_config.json"))
        self.assertEqual(shown["config"]["mode"], "local-demo")

    def test_set_dotted_keys(self):
        shown = self.config("datafed.context=p/lab", "review.confidence_threshold=0.7", "sync.max_attempts=3")
        self.assertEqual((shown["config"]["datafed"]["context"], shown["config"]["review"]["confidence_threshold"],
                          shown["config"]["sync"]["max_attempts"]), ("p/lab", 0.7, 3))
        saved = json.loads((self.home / "datafed_config.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, shown["config"])
        self.assertEqual(self.ctx.config.max_attempts, 3)
        self.assertNotIn("warnings", shown)

    def test_set_may_be_given_as_plain_strings_by_a_caller_in_this_process(self):
        shown = commands_core.cmd_config(Namespace(set=["sync.max_attempts=4"]), self.ctx)
        self.assertEqual(shown["config"]["sync"]["max_attempts"], 4)

    def test_invalid_value_is_refused_and_nothing_is_saved(self):
        before = self.config()["config"]
        for assignment in ("mode=cloud", "datafed.root_alias=Not Valid", "sync.max_attempts=0",
                           "datafed.include_simulated=False", "datafed.include_simulated=yes"):
            with self.assertRaises(ConfigError):
                self.config(assignment)
        with self.assertRaises(CommandError):
            self.config("mode")
        self.assertEqual(self.config()["config"], before)

    def test_unknown_key_is_stored_with_a_warning(self):
        shown = self.config("arc.partition=normal_q")
        self.assertEqual(shown["config"]["arc"], {"partition": "normal_q"})
        self.assertIn("arc.partition is not a setting this version knows", shown["warnings"][0])

    def test_broken_settings_are_shown_and_can_be_repaired(self):
        (self.home / "datafed_config.json").write_text(json.dumps({"mode": "cloud", "keep": 1}), encoding="utf-8")
        shown = self.config()
        self.assertIn("mode must be", shown["problems"][0])
        repaired = self.config("mode=local-demo")
        self.assertNotIn("problems", repaired)
        self.assertEqual(repaired["config"]["keep"], 1)

    def test_switching_mode_switches_the_catalog_of_the_context(self):
        self.assertEqual(self.ctx.catalog.name, "local-demo")
        self.config("mode=datafed")
        self.assertEqual(self.ctx.catalog.name, "datafed")


class Doctor(CommandCase):
    def doctor(self, **options):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            for name in [name for name in os.environ if name.startswith("DATAFED_")]:
                del os.environ[name]
            return commands_core.cmd_doctor(Namespace(**options), self.ctx)

    def test_checks_have_name_ok_detail_fix(self):
        report = self.doctor()
        self.assertEqual((report["mode"], report["online_checked"], report["datafed_ready"]),
                         ("local-demo", False, False))
        for check in report["checks"]:
            self.assertEqual(set(check), {"name", "ok", "detail", "fix"})
            self.assertIsInstance(check["ok"], bool)
            self.assertTrue(check["detail"])
            self.assertEqual(bool(check["fix"]), not check["ok"], check)
        names = [check["name"] for check in report["checks"]]
        self.assertEqual(names[:4], ["python", "datafed_package", "datafed_credentials", "globus_default_endpoint"])
        self.assertEqual(names[-1], "settings")

    def test_never_connects_without_online_even_in_datafed_mode(self):
        self.set_mode("datafed")
        with mock.patch.object(DataFedCatalog, "_connect", side_effect=AssertionError("connected")):
            report = self.doctor()
            self.assertIs(report["online_checked"], False)
            self.assertNotIn("datafed_connection", [check["name"] for check in report["checks"]])

    def test_online_is_ignored_in_local_demo_mode(self):
        with mock.patch.object(DataFedCatalog, "_connect", side_effect=AssertionError("connected")):
            report = self.doctor(online=True)
        self.assertIs(report["online_checked"], False)

    def test_online_in_datafed_mode_asks_the_catalog_who_the_user_is(self):
        self.set_mode("datafed")
        catalog = mock.create_autospec(DataFedCatalog, instance=True)
        catalog.whoami.return_value = {"online": True, "user": "u/tester", "context": "u/tester",
                                       "endpoint": "5a1b0e6e", "demo": False}
        ctx = support.context(self, self.home, catalog=catalog)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            report = commands_core.cmd_doctor(Namespace(online=True), ctx)
        self.assertIs(report["online_checked"], True)
        self.assertEqual(report["checks"][-1]["name"], "datafed_connection")
        self.assertIs(report["checks"][-1]["ok"], True)
        self.assertIn("u/tester", report["checks"][-1]["detail"])
        catalog.whoami.assert_called_once_with()

    def test_invalid_settings_are_a_failed_check_not_a_failed_command(self):
        (self.home / "datafed_config.json").write_text(json.dumps({"mode": "cloud"}), encoding="utf-8")
        report = self.doctor(online=True)
        settings_check = report["checks"][-1]
        self.assertEqual((settings_check["name"], settings_check["ok"]), ("settings", False))
        self.assertIn("config --set", settings_check["fix"])

    def test_shows_whether_simulated_captures_are_sent_to_datafed(self):
        self.set_mode("datafed")
        report = self.doctor()
        self.assertIs(report["include_simulated"], False)
        self.assertIn("datafed.include_simulated false", report["checks"][-1]["detail"])
        self.assertIn("config --set datafed.include_simulated=true", report["checks"][-1]["detail"])
        commands_core.cmd_config(Namespace(set=[["datafed.include_simulated=true"]]), self.ctx)
        report = self.doctor()
        self.assertIs(report["include_simulated"], True)
        self.assertIn("simulated captures are sent to DataFed as well", report["checks"][-1]["detail"])


class Offline(CommandCase):
    def offline(self, **options):
        return commands_core.cmd_offline(Namespace(**options), self.ctx)

    def test_on_off_and_report(self):
        self.assertEqual(self.offline(), {"offline": False})
        self.assertEqual(self.offline(on=True), {"offline": True})
        self.assertTrue((self.home / "data_catalog" / "local_demo" / "OFFLINE").is_file())
        self.assertEqual(self.offline(), {"offline": True})
        self.assertIs(commands_core.cmd_status(Namespace(), self.ctx)["online"], False)
        self.assertEqual(self.offline(off=True), {"offline": False})
        self.assertIs(commands_core.cmd_status(Namespace(), self.ctx)["online"], True)

    def test_only_the_local_demo_can_be_switched_offline(self):
        self.set_mode("datafed")
        with mock.patch.object(DataFedCatalog, "_connect", side_effect=AssertionError("connected")):
            with self.assertRaisesRegex(CommandError, "local demo catalog only"):
                self.offline(on=True)


class DatafedModeWithoutConnecting(CommandCase):
    def test_commands_that_need_no_catalog_never_connect(self):
        self.set_mode("datafed")
        session = support.make_session(self.home, points=2, with_capture=False, with_session_json=False)
        with mock.patch.object(DataFedCatalog, "_connect", side_effect=AssertionError("connected")):
            imported = commands_core.cmd_import_session(
                Namespace(folder=str(session["folder"]), sample_id="S-OLD", operator="Test Operator",
                          location="Bench 2"), self.ctx)
            self.assertEqual(imported["ingested"], 2)
            self.assertEqual(commands_core.cmd_list(Namespace(), self.ctx)["total"], 2)
            self.assertEqual(commands_core.cmd_config(Namespace(), self.ctx)["config"]["mode"], "datafed")
            self.assertEqual(self.ctx.catalog.label, "DataFed (your own space)")
            with self.assertRaises(AssertionError):
                commands_core.cmd_status(Namespace(), self.ctx)


class InProcess(CommandCase):
    def test_invoke_returns_what_the_command_line_prints(self):
        support.make_session(self.home, points=1)
        self.assertEqual(cli.invoke(self.ctx, ["sync"])["ingested"], 1)
        listing = cli.invoke(self.ctx, ["list", "--limit", 5])
        self.assertEqual((listing["ok"], listing["total"]), (True, 1))
        failure = cli.invoke(self.ctx, ["provenance", "--record", "d/404"])
        self.assertEqual((failure["ok"], failure["kind"]), (False, "not_found"))
        usage = cli.invoke(self.ctx, ["list", "--state", "finished"])
        self.assertEqual((usage["ok"], usage["kind"]), (False, "usage"))

    def test_context_gives_home_paths_config_ledger_and_catalog(self):
        with cli.Context(self.home) as ctx:
            self.assertEqual(ctx.home, self.home)
            self.assertEqual(ctx.paths.scan_sessions, self.home / "scan_sessions")
            self.assertEqual(ctx.config.mode, "local-demo")
            self.assertEqual(ctx.ledger.count(), 0)
            self.assertEqual(ctx.catalog.name, "local-demo")
            self.assertIs(ctx.catalog, ctx.catalog)
        with self.assertRaisesRegex(CommandError, "HOME folder does not exist"):
            cli.Context(self.home / "missing").config


if __name__ == "__main__":
    unittest.main()
