"""The arc_upload settings (PROTOCOL.md 5.1): defaults, rules, and what an invalid value stops."""

import json
import os
import unittest
from argparse import Namespace

import arc_support
import support
from raman_catalog import arc_settings, commands_core, config
from raman_catalog.config import ConfigError, load_settings

ARC = "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"
OTHER = "0b9e2d4c-1a2b-4c3d-8e4f-5a6b7c8d9e0f"


class Defaults(arc_support.ArcCase):
    def test_defaults_are_read_from_a_file_without_the_group_and_the_file_is_not_changed(self):
        path = arc_support.write_settings(self.home)
        before = path.read_bytes()
        settings = load_settings(self.home)
        self.assertEqual(arc_settings.effective(settings), arc_settings.DEFAULTS)
        self.assertEqual(arc_settings.problems(settings), [])
        self.assertEqual(self.ok("arc-list")["settings"]["arc_upload.folder"], arc_settings.DEFAULT_FOLDER)
        self.ok("arc-sessions")
        self.ok("arc-doctor")
        self.assertEqual(path.read_bytes(), before)

    def test_no_settings_file_is_written_by_the_arc_commands(self):
        for command in ("arc-sessions", "arc-list", "arc-doctor"):
            self.ok(command)
        self.assertFalse((self.home / "catalog_config.json").exists())

    def test_the_first_use_default_file_has_no_arc_group(self):
        settings = load_settings(self.home)
        self.assertNotIn("arc_upload", json.loads((self.home / "catalog_config.json").read_text(encoding="utf-8")))
        self.assertNotIn("arc_upload", settings.as_dict())
        self.assertNotIn("arc_upload", config.DEFAULTS)

    def test_flattened_defaults_are_the_known_keys(self):
        self.assertEqual(tuple(arc_settings.flatten(arc_settings.DEFAULTS)), config.ARC_UPLOAD_KEYS)
        self.assertEqual(len(config.ARC_UPLOAD_KEYS), 15)
        settings = load_settings(self.home)
        for key in config.ARC_UPLOAD_KEYS:
            self.assertTrue(settings.is_known(key), key)
        for key in ("arc.host", "arc_upload", "arc_upload.globus", "arc_upload.colour"):
            self.assertFalse(settings.is_known(key), key)

    def test_the_public_identifiers_are_the_defaults(self):
        values = arc_settings.DEFAULTS
        self.assertEqual(values["globus"]["arc_collection"], ARC)
        self.assertEqual(values["globus"]["client_id"], "61338d24-54d5-408f-a10d-66c06b59f6d2")
        self.assertEqual((values["globus"]["mac_collection"], values["ssh"]["username"]), ("", ""))
        self.assertEqual(values["ssh"]["host"], "datatransfer.arc.vt.edu")
        self.assertEqual(values["folder"], "/projects/pcl_photonics/raman_globus_upload")


class Rules(arc_support.ArcCase):
    def config(self, *assignments):
        with support.context(self, self.home) as ctx:
            return commands_core.cmd_config(Namespace(set=[list(assignments)] if assignments else None), ctx)

    def saved(self):
        return json.loads((self.home / "catalog_config.json").read_text(encoding="utf-8"))

    def assert_refused(self, assignment, key):
        before = self.saved()
        with self.assertRaises(ConfigError) as raised:
            self.config(assignment)
        self.assertIn(key, raised.exception.detail, assignment)
        self.assertIn("ARC settings", str(raised.exception))
        self.assertEqual(self.saved(), before, assignment)

    def setUp(self):
        super().setUp()
        load_settings(self.home)

    def test_valid_values_are_saved(self):
        known = self.home / "known_hosts_arc"
        known.write_text("datatransfer.arc.vt.edu ssh-ed25519 AAAA\n", encoding="utf-8")
        os.chmod(known, 0o600)
        shown = self.config(
            "arc_upload.demo=false", "arc_upload.transport=ssh", "arc_upload.folder=/projects/team/raman/uploads",
            "arc_upload.include_simulated=true", "arc_upload.settle_seconds=10",
            "arc_upload.demo_folder=elsewhere/arc_demo", "arc_upload.globus.mac_collection=" + OTHER.upper(),
            "arc_upload.globus.deadline_hours=168", "arc_upload.globus.force_login=true",
            "arc_upload.ssh.username=" + arc_support.TEST_USERNAME, "arc_upload.ssh.host=tinkercliffs1.arc.vt.edu",
            "arc_upload.ssh.known_hosts=" + str(known))
        group = self.saved()["arc_upload"]
        self.assertEqual(shown["config"]["arc_upload"], group)
        self.assertNotIn("warnings", shown)
        self.assertIs(group["demo"], False)
        self.assertEqual(group["settle_seconds"], 10)
        # Stored in the canonical, lowercase form.
        self.assertEqual(group["globus"]["mac_collection"], OTHER)
        self.assertEqual(group["ssh"]["username"], arc_support.TEST_USERNAME)
        settings = load_settings(self.home)
        self.assertEqual(arc_settings.problems(settings), [])
        self.assertEqual(arc_settings.globus_folder(arc_settings.effective(settings)), "/team/raman/uploads")

    def test_the_whole_cluster_as_the_collection_root(self):
        self.config("arc_upload.globus.collection_root=/", "arc_upload.folder=/projects/team/uploads")
        values = arc_settings.effective(load_settings(self.home))
        self.assertEqual(arc_settings.globus_folder(values), "/projects/team/uploads")

    def test_every_invalid_value_is_refused_and_nothing_is_saved(self):
        cases = {
            "arc_upload.demo": ["False", "yes", "1", "null"],
            "arc_upload.transport": ["scp", "Globus", ""],
            "arc_upload.include_simulated": ["False", "yes", "0"],
            "arc_upload.settle_seconds": ["9", "3601", "60.5", "true", '"60"'],
            "arc_upload.folder": ["/projects", "/projects/x", "/projects/../x", "/projects/x/.y", "/projects/x/-y",
                                  "/projects/x/y/", "projects/x/y", "/home/x/y", "/projects/x//y", "2026"],
            "arc_upload.demo_folder": ["", "scan_sessions/demo", "dark_measurements", "data_catalog/arc_uploads/x"],
            "arc_upload.globus.mac_collection": [ARC, ARC.upper(), "not-a-uuid", "1234"],
            "arc_upload.globus.arc_collection": ["", "x"],
            "arc_upload.globus.collection_root": ["projects", "/projects/", "/pro jects", ""],
            "arc_upload.globus.client_id": ["", "tutorial"],
            "arc_upload.globus.deadline_hours": ["0", "169", "true"],
            "arc_upload.globus.force_login": ["yes", "1"],
            "arc_upload.ssh.username": ["Abc", "a@b", "-oProxyCommand=x", "root", "a", "1234", "ab cd"],
            "arc_upload.ssh.host": ["login.example.org", "DATATRANSFER.ARC.VT.EDU", "arc.vt.edu",
                                    "x.arc.vt.edu.evil.org", "-oProxyCommand=x"],
            "arc_upload.ssh.known_hosts": ["relative/file", "/nowhere/known_hosts"],
        }
        for key, values in cases.items():
            for value in values:
                self.assert_refused("%s=%s" % (key, value), key)

    def test_a_value_with_a_newline_at_its_end_is_refused(self):
        # In Python "$" also matches before a final newline: every rule is a fullmatch.
        cases = {"arc_upload.ssh.username": arc_support.TEST_USERNAME + "\n",
                 "arc_upload.ssh.host": "datatransfer.arc.vt.edu\n",
                 "arc_upload.globus.mac_collection": OTHER + "\n",
                 "arc_upload.globus.arc_collection": ARC + "\n",
                 "arc_upload.globus.client_id": OTHER + "\n",
                 "arc_upload.globus.collection_root": "/projects\n",
                 "arc_upload.folder": "/projects/team/uploads\n"}
        for key, value in cases.items():
            self.assert_refused("%s=%s" % (key, value), key)

    def test_a_known_hosts_path_that_ssh_reads_differently_is_refused(self):
        folder = self.home / "team keys"
        folder.mkdir()
        for name in ("known_hosts", "known%h"):
            path = (folder if name == "known_hosts" else self.home) / name
            path.write_text("x\n", encoding="utf-8")
            os.chmod(path, 0o600)
            self.assert_refused("arc_upload.ssh.known_hosts=%s" % path, "arc_upload.ssh.known_hosts")
            self.assertIn("a character that ssh reads differently", arc_settings.known_hosts_problem(str(path)))

    def test_a_known_hosts_file_others_can_change_or_a_link_is_refused(self):
        writable = self.home / "known_group"
        writable.write_text("x\n", encoding="utf-8")
        os.chmod(writable, 0o664)
        self.assert_refused("arc_upload.ssh.known_hosts=%s" % writable, "arc_upload.ssh.known_hosts")
        good = self.home / "known_good"
        good.write_text("x\n", encoding="utf-8")
        os.chmod(good, 0o600)
        link = self.home / "known_link"
        link.symlink_to(good)
        self.assert_refused("arc_upload.ssh.known_hosts=%s" % link, "arc_upload.ssh.known_hosts")
        folder = self.home / "known_folder"
        folder.mkdir()
        self.assert_refused("arc_upload.ssh.known_hosts=%s" % folder, "arc_upload.ssh.known_hosts")
        self.config("arc_upload.ssh.known_hosts=%s" % good)
        self.config("arc_upload.ssh.known_hosts=")
        self.assertEqual(self.saved()["arc_upload"]["ssh"]["known_hosts"], "")

    def test_the_folder_must_be_inside_the_collection_root(self):
        self.assert_refused("arc_upload.globus.collection_root=/scratch", "arc_upload.folder")
        self.config("arc_upload.globus.collection_root=/scratch", "arc_upload.folder=/scratch/team/uploads")

    def test_text_keys_stay_text(self):
        settings = load_settings(self.home)
        for key in ("arc_upload.ssh.username", "arc_upload.folder", "arc_upload.globus.mac_collection",
                    "arc_upload.transport", "arc_upload.demo_folder"):
            settings.set_from_text(key, "1234")
            self.assertEqual(settings.get(key), "1234", key)
        settings.set_from_text("arc_upload.demo", "false")
        settings.set_from_text("arc_upload.settle_seconds", "30")
        self.assertIs(settings.get("arc_upload.demo"), False)
        self.assertEqual(settings.get("arc_upload.settle_seconds"), 30)

    def test_problem_sentences_name_the_key_and_no_example_person(self):
        settings = load_settings(self.home)
        settings.set("arc_upload.ssh.username", "Not Valid")
        found = arc_settings.problems(settings)
        self.assertEqual(len(found), 1)
        self.assertIn("arc_upload.ssh.username", found[0])
        self.assertNotIn("@vt.edu", found[0])

    def test_a_group_that_is_not_an_object_is_one_problem(self):
        settings = load_settings(self.home)
        settings.data["arc_upload"] = "on"
        self.assertEqual(arc_settings.problems(settings), ["arc_upload must be a group of settings (a JSON object)"])
        settings.data["arc_upload"] = {"globus": []}
        self.assertIn("arc_upload.globus must be a group of settings (a JSON object)", arc_settings.problems(settings))

    def test_the_unknown_arc_key_of_the_edition_still_warns(self):
        shown = self.config("arc.partition=normal_q")
        self.assertEqual(shown["config"]["arc"], {"partition": "normal_q"})
        self.assertIn("arc.partition is not a setting this version knows", shown["warnings"][0])
        self.assertNotIn("arc_upload", shown["config"])

    def test_an_unknown_key_inside_the_group_warns_and_is_kept(self):
        shown = self.config("arc_upload.colour=blue")
        self.assertIn("arc_upload.colour is not a setting this version knows", shown["warnings"][0])
        self.assertEqual(self.saved()["arc_upload"], {"colour": "blue"})


class InvalidByHand(arc_support.ArcCase):
    """A value edited by hand into catalog_config.json stops only the arc-* commands."""

    def setUp(self):
        super().setUp()
        arc_support.write_settings(self.home, {"demo": "yes", "ssh": {"username": "Not Valid"}})
        arc_support.ready_session(self.home)

    def test_the_core_commands_still_answer(self):
        for command in (("status",), ("sync",), ("list",), ("config",)):
            answer = self.invoke(*command)
            self.assertTrue(answer["ok"], (command, answer))

    def test_config_reports_arc_problems_and_core_problems_stay_empty(self):
        shown = self.ok("config")
        self.assertNotIn("problems", shown)
        self.assertEqual(len(shown["arc_problems"]), 2)
        self.assertIn("arc_upload.demo must be true or false", shown["arc_problems"])

    def test_the_commands_that_start_work_answer_settings_invalid(self):
        for argv in (("arc-sessions",), ("arc-upload", "--session=" + arc_support.SESSION),
                     ("arc-check", "--upload=u000000000000"), ("arc-retry", "--upload=u000000000000"),
                     ("arc-keep-copy", "--upload=u000000000000")):
            answer = self.refused("config", "settings_invalid", *argv)
            self.assertIn("arc_upload.demo", answer["error"])
            self.assertIn("config --set", answer["fix"])

    def test_the_commands_that_act_on_records_answer_whatever_the_settings(self):
        # SEC-04: arc-list, arc-status and arc-stop act on records, which keep the settings they were made
        # with: an invalid value never takes Stop upload away.
        listing = self.ok("arc-list")
        self.assertEqual(listing["items"], [])
        for transport in ("globus", "ssh", "demo"):
            (problem,) = listing["ready"][transport]["problems"]
            self.assertEqual(problem["reason"], "settings_invalid")
            self.assertIn("arc_upload.demo", problem["text"])
            self.assertFalse(listing["ready"][transport]["ok"])
        self.assertEqual(listing["settings"]["arc_upload.demo"], "yes")
        self.refused("not_found", "unknown_upload", "arc-status", "--upload=u000000000000")
        self.refused("not_found", "unknown_upload", "arc-stop", "--upload=u000000000000")

    def test_a_running_upload_can_be_stopped_with_invalid_settings(self):
        arc_support.write_settings(self.home)
        upload_id = self.upload()["upload"]["upload_id"]
        folder = self.record_dir(upload_id)
        arc_support.write_settings(self.home, {"settle_seconds": 5, "demo_folder": None})
        shown = self.ok("arc-status", "--upload=" + upload_id)["upload"]
        self.assertEqual(shown["actions"], ["stop"])
        arc_support.hold_lock(self, folder / "worker.lock")
        self.assertEqual(self.ok("arc-list")["items"][0]["actions"], ["stop"])
        answer = self.ok("arc-stop", "--upload=" + upload_id)
        self.assertIs(answer["stopped_now"], False)
        self.assertTrue(self.record(upload_id)["requests"]["stop_utc"])

    def test_arc_doctor_shows_the_problem_so_that_it_can_be_repaired(self):
        answer = self.ok("arc-doctor")
        check = {entry["name"]: entry for entry in answer["checks"]}["settings_valid"]
        self.assertFalse(check["ok"])
        self.assertIn("arc_upload.ssh.username", check["detail"])
        self.assertEqual(answer["settings"]["arc_upload.demo"], "yes")
        self.assertFalse(answer["ready"]["demo"])

    def test_arc_doctor_answers_with_a_demo_folder_that_is_not_text(self):
        # COR-V01: the ARC settings dialog opens with arc-doctor; it must answer to be able to repair it.
        for value in (None, 5, [], {"x": 1}):
            arc_support.write_settings(self.home, {"demo_folder": value})
            answer = self.ok("arc-doctor")
            checks = {entry["name"]: entry for entry in answer["checks"]}
            self.assertFalse(checks["settings_valid"]["ok"], value)
            self.assertIn("arc_upload.demo_folder", checks["settings_valid"]["detail"])
            self.assertFalse(checks["demo_folder_writable"]["ok"], value)
            self.assertIn("see settings_valid", checks["demo_folder_writable"]["detail"])
            self.assertFalse(answer["ready"]["demo"])
            self.assertEqual(answer["settings"]["arc_upload.demo_folder"], value)
            self.ok("arc-list")

    def test_a_core_key_is_saved_whatever_the_arc_values(self):
        # COR-06 and SEC-04: an invalid ARC value never holds back a setting of the rest of the edition.
        shown = commands_core.cmd_config(Namespace(set=[["catalog.root_alias=raman-portable", "sync.max_attempts=7"]]),
                                         support.context(self, self.home))
        self.assertEqual(self.saved()["sync"]["max_attempts"], 7)
        self.assertEqual(len(shown["arc_problems"]), 2)

    def test_an_arc_value_is_saved_while_another_one_is_invalid(self):
        # SEC-04: the ARC settings dialog saves the values it shows; one it does not show (here demo) is
        # reported, not a reason to refuse the save.
        shown = commands_core.cmd_config(Namespace(set=[["arc_upload.ssh.username=" + arc_support.TEST_USERNAME]]),
                                         support.context(self, self.home))
        self.assertEqual(self.saved()["arc_upload"]["ssh"]["username"], arc_support.TEST_USERNAME)
        self.assertEqual(shown["arc_problems"], ["arc_upload.demo must be true or false"])
        # A value it sets must still be valid, and it may not bring a new problem.
        with self.assertRaises(ConfigError) as raised:
            commands_core.cmd_config(Namespace(set=[["arc_upload.ssh.username=Not Valid Either"]]),
                                     support.context(self, self.home))
        self.assertIn("arc_upload.ssh.username", raised.exception.detail)
        self.assertNotIn("arc_upload.demo", raised.exception.detail)
        with self.assertRaises(ConfigError) as raised:
            commands_core.cmd_config(Namespace(set=[["arc_upload.globus.collection_root=/scratch"]]),
                                     support.context(self, self.home))
        self.assertIn("arc_upload.folder", raised.exception.detail)
        self.assertEqual(self.saved()["arc_upload"]["ssh"]["username"], arc_support.TEST_USERNAME)
        # Repairing the last one leaves nothing to report.
        shown = commands_core.cmd_config(Namespace(set=[["arc_upload.demo=true"]]), support.context(self, self.home))
        self.assertNotIn("arc_problems", shown)
        self.ok("arc-list")

    def test_a_known_hosts_file_that_went_missing_holds_back_only_its_own_key(self):
        # COR-06: the team file is looked at when this command sets it, not for every other key.
        known = self.home / "known_hosts_team"
        known.write_text("datatransfer.arc.vt.edu ssh-ed25519 AAAA\n", encoding="utf-8")
        os.chmod(known, 0o600)
        arc_support.write_settings(self.home, {"ssh": {"known_hosts": str(known)}})
        known.unlink()
        shown = commands_core.cmd_config(Namespace(set=[["sync.max_attempts=7", "arc_upload.transport=ssh"]]),
                                         support.context(self, self.home))
        self.assertEqual(self.saved()["arc_upload"]["transport"], "ssh")
        self.assertIn("there is no file", shown["arc_problems"][0])
        with self.assertRaises(ConfigError):
            commands_core.cmd_config(Namespace(set=[["arc_upload.ssh.known_hosts=" + str(known)]]),
                                     support.context(self, self.home))
        known.write_text("x\n", encoding="utf-8")
        os.chmod(known, 0o664)
        with self.assertRaises(ConfigError):
            commands_core.cmd_config(Namespace(set=[["arc_upload.ssh.known_hosts=" + str(known)]]),
                                     support.context(self, self.home))
        commands_core.cmd_config(Namespace(set=[["arc_upload.ssh.known_hosts="]]), support.context(self, self.home))
        self.assertEqual(self.saved()["arc_upload"]["ssh"]["known_hosts"], "")

    def saved(self):
        return json.loads((self.home / "catalog_config.json").read_text(encoding="utf-8"))

    def test_a_broken_settings_file_is_settings_invalid_for_arc_commands(self):
        (self.home / "catalog_config.json").write_text("{broken", encoding="utf-8")
        answer = self.refused("config", "settings_invalid", "arc-sessions")
        self.assertIn("not valid JSON", answer["error"])
        # The uploads are still listed, and say why nothing can start.
        problem = self.ok("arc-list")["ready"]["globus"]["problems"][0]
        self.assertEqual(problem["reason"], "settings_invalid")
        self.assertIn("not valid JSON", problem["text"])


class DemoFolderRule(arc_support.ArcCase):
    """arc_upload.demo_folder keeps out of the folders of the edition, in any letter case (SEC-05)."""

    def setUp(self):
        super().setUp()
        load_settings(self.home)

    def config(self, *assignments):
        with support.context(self, self.home) as ctx:
            return commands_core.cmd_config(Namespace(set=[list(assignments)]), ctx)

    def test_the_folders_of_the_edition_in_any_letter_case_and_the_controllers_folder(self):
        (self.home / "scan_sessions").mkdir(exist_ok=True)
        (self.home / "dark_measurements").mkdir(exist_ok=True)
        (self.home / "data_catalog" / "arc_uploads").mkdir(parents=True, exist_ok=True)
        refused = ["scan_sessions/demo", "Scan_Sessions/demo", "SCAN_SESSIONS", "DARK_measurements/x",
                   "data_catalog/ARC_UPLOADS/x", "Data_Catalog/arc_uploads", ".", "..", str(self.home),
                   str(self.home.parent), "/"]
        for value in refused:
            with self.assertRaises(ConfigError, msg=value) as raised:
                self.config("arc_upload.demo_folder=" + value)
            self.assertIn("arc_upload.demo_folder", raised.exception.detail, value)
        for value in ("data_catalog/arc_demo", "demo_arc", "data_catalog/Arc_Demo2",
                      str(self.home.parent / "elsewhere_demo")):
            self.config("arc_upload.demo_folder=" + value)


if __name__ == "__main__":
    unittest.main()
