"""Which sessions may go to ARC (the readiness rule R1 to R7), how they ended, and what goes with them."""

import ast
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import arc_support
import support
from raman_datafed import arc_session, doctor
from raman_datafed.config import Paths

EDITION = Path(__file__).resolve().parents[2]
SESSION = arc_support.SESSION


def check(home, name=SESSION, settle=60, idle=False, full=True, declare=False, commands=(), now=None):
    return arc_session.check(Paths(home), Path(home) / "scan_sessions" / name, settle, idle, full=full,
                             declare_real=declare, commands=list(commands) if commands is not None else None,
                             now=now)


class Readiness(arc_support.ArcCase):
    def test_a_finished_session_is_ready(self):
        arc_support.ready_session(self.home)
        result = check(self.home)
        self.assertIsNone(result.refusal)
        self.assertEqual((result.points, result.planned, result.end, result.kind), (13, 13, "finished", "simulated"))
        self.assertEqual(result.end_text(), "finished")
        self.assertEqual(result.warnings, [])

    def test_r1_names_that_cannot_be_sent(self):
        # (One spelling of "simulated" only: the Mac's file system ignores case.)
        for name in ("bad name", "Simulated", "x" * 129, "-leading"):
            arc_support.ready_session(self.home, folder=name)
            self.assertEqual(check(self.home, name).refusal.reason, "odd_name", name)
        arc_support.ready_session(self.home, folder="Ok.name-1_2")
        self.assertIsNone(check(self.home, "Ok.name-1_2").refusal)

    def test_r2_session_json_missing_or_unreadable(self):
        session = arc_support.ready_session(self.home)
        (session["folder"] / "session.json").unlink()
        error = check(self.home).refusal
        self.assertEqual((error.kind, error.reason), ("metadata", "no_session_json"))
        self.assertEqual(str(error), "%s has no session.json, so this edition did not record it." % SESSION)
        self.assertIn("import-session --folder %s" % SESSION, error.fix)
        (session["folder"] / "session.json").write_text("{not json", encoding="utf-8")
        error = check(self.home).refusal
        self.assertEqual((error.kind, error.reason), ("metadata", "unreadable_session"))
        self.assertTrue(str(error).startswith("session.json of %s cannot be read: " % SESSION))

    def test_a_folder_without_session_json_is_listed_with_its_spectra(self):
        # COR-11: a session this edition did not record (until it is imported) is not listed as empty.
        session = arc_support.ready_session(self.home, folder="2d_scan_20261023_133801_863", points=8,
                                            scan_mode="2d")
        (session["folder"] / "session.json").unlink()
        (session["folder"] / "cancel.txt").write_text("stop\n", encoding="utf-8")
        arc_support.set_age(session["folder"], 600)
        result = check(self.home, "2d_scan_20261023_133801_863")
        self.assertEqual(result.refusal.reason, "no_session_json")
        self.assertEqual((result.points, result.planned, result.end, result.end_text()),
                         (8, 124, "stopped", "stopped early (8 of 124)"))
        self.assertEqual(result.file_count(), 8 + 1 + 2)
        item = {entry["folder"]: entry for entry in self.ok("arc-sessions")["items"]}["2d_scan_20261023_133801_863"]
        self.assertEqual((item["points"], item["points_planned"], item["end_text"], item["ready"]),
                         (8, 124, "stopped early (8 of 124)", False))
        self.assertGreater(item["bytes"], 0)

    def test_r3_no_point(self):
        session = arc_support.ready_session(self.home)
        for path in session["files"]:
            path.unlink()
        error = check(self.home).refusal
        self.assertEqual((error.kind, error.reason, str(error)),
                         ("not_ready", "no_points", "%s has no saved spectrum." % SESSION))

    def test_r4_a_program_that_names_the_session_is_running(self):
        session = arc_support.ready_session(self.home)
        folder = session["folder"]
        lines = ["/usr/bin/python3 raman_autofocus.py %s %s 1d-x" % (folder, session["session_id"])]
        self.assertEqual(check(self.home, commands=lines).refusal.reason, "running")
        self.assertEqual(check(self.home, commands=["motor %s" % session["session_id"]]).refusal.reason, "running")
        self.assertEqual(check(self.home, commands=["import-session --folder=%s/" % folder]).refusal.reason, "running")
        # Another session whose name only starts the same is not this one.
        others = ["motor %s0" % folder, "motor x%s" % session["session_id"]]
        self.assertIsNone(check(self.home, commands=others).refusal)
        result = check(self.home, commands=lines)
        self.assertEqual(result.end_text(), "still running")

    def test_r4_the_listing_comes_from_ps_and_a_failure_is_a_warning(self):
        session = arc_support.ready_session(self.home)
        with mock.patch.object(doctor, "_running_commands", return_value=["x %s" % session["folder"]]):
            self.refused("not_ready", "running", "arc-upload", "--session=" + SESSION, "--demo")
        with mock.patch.object(doctor, "_running_commands", side_effect=OSError("no ps")):
            answer = self.ok("arc-upload", "--session=" + SESSION, "--demo", "--dry-run")
        self.assertIn("could not be listed", answer["plan"]["warnings"][0])

    def test_r5_settling_with_the_time_left(self):
        session = arc_support.ready_session(self.home)
        newest = max(path.stat().st_mtime for path in session["files"])
        result = check(self.home, now=newest + 20)
        self.assertEqual((result.refusal.kind, result.refusal.reason), ("not_ready", "settling"))
        self.assertEqual(result.retry_after_seconds, 40)
        self.assertEqual(result.refusal.extra["retry_after_seconds"], 40)
        self.assertEqual(result.refusal.fix, "Try again in 40 s.")
        self.assertEqual(result.end_text(), "ended 20 s ago")
        self.assertIsNone(check(self.home, now=newest + 61).refusal)
        self.assertEqual(check(self.home, settle=120, now=newest + 61).retry_after_seconds, 59)
        # Up to a day in the future counts as just now; more is a clock problem.
        self.assertEqual(check(self.home, now=newest - 3600).refusal.reason, "settling")
        self.assertEqual(check(self.home, now=newest - 86400 - 60).refusal.reason, "future_time")
        # The controller says nothing acquires: no waiting.
        self.assertIsNone(check(self.home, idle=True, now=newest + 1).refusal)

    def test_r5_through_the_command_answers_retry_after_seconds(self):
        session = arc_support.ready_session(self.home)
        os.utime(session["files"][0])
        answer = self.refused("not_ready", "settling", "arc-upload", "--session=" + SESSION, "--demo")
        self.assertGreater(answer["retry_after_seconds"], 50)
        item = self.ok("arc-sessions")["items"][0]
        self.assertFalse(item["ready"])
        self.assertEqual(item["why_not"]["reason"], "settling")
        self.assertTrue(item["end_text"].startswith("ended "))

    def test_r6_contents(self):
        session = arc_support.ready_session(self.home)
        folder = session["folder"]
        (folder / "dark").mkdir()
        self.assertEqual(check(self.home, idle=True).refusal.reason, "name_clash")
        (folder / "dark").rmdir()
        (folder / "Dark").write_text("x", encoding="utf-8")
        self.assertEqual(check(self.home, idle=True).refusal.reason, "name_clash")
        (folder / "Dark").unlink()
        (folder / "notes\\x.txt").write_text("x", encoding="utf-8")
        error = check(self.home, idle=True).refusal
        # A name that cannot be shown as it is appears in quotes, its odd characters escaped.
        self.assertEqual(error.reason, "odd_name")
        self.assertEqual(str(error), "This session holds a file (or has a name) that cannot be sent safely: "
                                     "'notes\\\\x.txt'.")
        (folder / "notes\\x.txt").unlink()
        (folder / "tab\there.txt").write_text("x", encoding="utf-8")
        self.assertEqual(check(self.home, idle=True).refusal.reason, "odd_name")
        (folder / "tab\there.txt").unlink()
        (folder / "link.json").symlink_to(session["files"][0])
        error = check(self.home, idle=True).refusal
        self.assertEqual((error.kind, error.reason), ("refused", "link"))
        self.assertIn("(link.json)", str(error))
        (folder / "link.json").unlink()
        os.mkfifo(folder / "pipe")
        self.assertEqual(check(self.home, idle=True).refusal.reason, "special_file")
        (folder / "pipe").unlink()
        deep = folder / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "e.txt").write_text("x", encoding="utf-8")
        self.assertEqual(check(self.home, idle=True).refusal.reason, "special_file")
        (deep / "e.txt").unlink()
        (deep.parent / "d.txt").write_text("x", encoding="utf-8")
        self.assertIsNone(check(self.home, idle=True).refusal)

    def test_r6_a_link_inside_a_hidden_folder_is_refused_too(self):
        session = arc_support.ready_session(self.home)
        hidden = session["folder"] / ".cache"
        hidden.mkdir()
        (hidden / "x").symlink_to("/etc/hosts")
        self.assertEqual(check(self.home, idle=True).refusal.reason, "link")

    def test_what_is_left_out(self):
        session = arc_support.ready_session(self.home)
        folder = session["folder"]
        names = (".DS_Store", "._point_0001.json", ".point_0001.json.ab12cd.tmp", ".session.json.importing",
                 "Icon\r", "Thumbs.db", "desktop.ini", "raman_upload_manifest.json", "SHA256SUMS",
                 "response.txt.Ab12Cd", "point_0003.json.x1y2z3", "response.txt", "notes.txt")
        for name in names:
            (folder / name).write_text("x", encoding="utf-8")
        (folder / ".hidden").mkdir()
        (folder / ".hidden" / "inside.txt").write_text("x", encoding="utf-8")
        result = check(self.home, idle=True)
        self.assertIsNone(result.refusal)
        excluded = {entry["path"]: entry["reason"] for entry in result.listing.excluded}
        self.assertEqual(excluded, {
            ".DS_Store": "a hidden file", "._point_0001.json": "a hidden file",
            ".point_0001.json.ab12cd.tmp": "a hidden file", ".session.json.importing": "a hidden file",
            "Icon\r": "a Finder file", "Thumbs.db": "a file Windows makes", "desktop.ini": "a file Windows makes",
            "raman_upload_manifest.json": "the list of an earlier upload",
            "SHA256SUMS": "the list of an earlier upload",
            "response.txt.Ab12Cd": "a temporary file of the controller",
            "point_0003.json.x1y2z3": "a temporary file of the controller", ".hidden": "a hidden folder"})
        sent = {path for path, _info in result.listing.files}
        self.assertIn("response.txt", sent)
        self.assertIn("notes.txt", sent)
        self.assertEqual(result.file_count(), 13 + 1 + 2 + 2 + 2)

    def test_a_young_temporary_file_of_the_controller_makes_the_session_settle(self):
        session = arc_support.ready_session(self.home)
        (session["folder"] / "response.txt.Ab12Cd").write_text("x", encoding="utf-8")
        self.assertEqual(check(self.home).refusal.reason, "settling")
        self.assertIsNone(check(self.home, idle=True).refusal)


class ControllerIdle(arc_support.ArcCase):
    def test_the_lock_of_a_live_controller_skips_the_wait(self):
        session = arc_support.ready_session(self.home)
        os.utime(session["files"][-1])
        (self.home / "controller.lock").write_text("%d\nlaser_test\nhost\n" % os.getpid(), encoding="utf-8")
        answer = self.ok("arc-sessions", "--controller-idle")
        self.assertTrue(answer["controller_idle_used"])
        self.assertTrue(answer["items"][0]["ready"])
        self.ok("arc-upload", "--session=" + SESSION, "--demo", "--controller-idle", "--dry-run")
        # Without the flag the 60 s rule applies.
        self.assertFalse(self.ok("arc-sessions")["items"][0]["ready"])
        self.assertFalse(self.ok("arc-sessions")["controller_idle_used"])

    def test_the_lock_of_a_process_that_ended_is_ignored(self):
        session = arc_support.ready_session(self.home)
        os.utime(session["files"][-1])
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        (self.home / "controller.lock").write_text("%d\nlaser_test\nhost\n" % ended.pid, encoding="utf-8")
        answer = self.ok("arc-sessions", "--controller-idle")
        self.assertFalse(answer["controller_idle_used"])
        self.assertFalse(answer["items"][0]["ready"])
        (self.home / "controller.lock").write_text("not a pid\n", encoding="utf-8")
        self.assertFalse(self.ok("arc-sessions", "--controller-idle")["controller_idle_used"])
        (self.home / "controller.lock").unlink()
        self.assertFalse(self.ok("arc-sessions", "--controller-idle")["controller_idle_used"])


class Ends(arc_support.ArcCase):
    def end(self, points, mode="1d-x", folder=SESSION, marks=()):
        session = arc_support.ready_session(self.home, folder=folder, points=points, scan_mode=mode)
        for mark in marks:
            (session["folder"] / mark).write_text("x", encoding="utf-8")
        result = check(self.home, folder, idle=True)
        return result.end, result.planned, result.end_text()

    def test_failure_txt_means_failed(self):
        self.assertEqual(self.end(13, marks=("failure.txt", "cancel.txt")), ("failed", 13, "ended with an error"))

    def test_cancel_txt_after_every_point_is_finished(self):
        self.assertEqual(self.end(13, marks=("cancel.txt",)), ("finished", 13, "finished"))

    def test_cancel_txt_before_the_last_point_is_stopped(self):
        self.assertEqual(self.end(8, marks=("cancel.txt",)), ("stopped", 13, "stopped early (8 of 13)"))

    def test_no_mark_is_ended(self):
        self.assertEqual(self.end(8), ("ended", 13, "ended (no end mark)"))

    def test_a_2d_scan_needs_124_points(self):
        self.assertEqual(self.end(124, mode="2d", folder="2d_scan_20261113_101500_123"), ("finished", 124, "finished"))
        self.assertEqual(self.end(30, mode="2d", folder="2d_scan_b", marks=("cancel.txt",))[0], "stopped")

    def test_a_single_capture(self):
        self.assertEqual(self.end(1, mode="single", folder="single_20261113_101500_123"), ("finished", 1, "finished"))

    def test_planned_points_match_the_controller_and_the_motor_program(self):
        sources = [path for path in (EDITION / "laser_test").glob("*.[ch]*") if path.suffix in (".cpp", ".h")]
        found = None
        for path in sources:
            match = re.search(r"kLineScanPointCount\s*=\s*(\d+)", path.read_text(encoding="utf-8", errors="replace"))
            if match:
                found = int(match.group(1))
        motor = EDITION / "macos-native" / "motor" / "raman_autofocus.py"
        if found is None or not motor.is_file():
            self.skipTest("the controller or the motor program is not in this checkout")
        self.assertEqual(arc_session.PLANNED_POINTS["1d-x"], found)
        self.assertEqual(arc_session.PLANNED_POINTS["1d-y"], found)
        tree = ast.parse(motor.read_text(encoding="utf-8"))
        sequence = [node for node in ast.walk(tree) if isinstance(node, ast.AnnAssign) and
                    getattr(node.target, "id", None) == "XY_SEQUENCE"]
        self.assertEqual(len(sequence), 1)
        self.assertEqual(len(sequence[0].value.elts), 120)
        self.assertEqual(arc_session.PLANNED_POINTS["2d"], 4 + 120)
        self.assertEqual(arc_session.PLANNED_POINTS["single"], 1)


class Kinds(arc_support.ArcCase):
    def session_with(self, session_simulated, point_simulated=None, points=3):
        """A session whose session.json and points record `simulated` as given (None: not recorded)."""
        session = arc_support.ready_session(self.home, points=points)
        path = session["folder"] / "session.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        content["simulated"] = session_simulated
        path.write_text(json.dumps(content), encoding="utf-8")
        for index, point_path in enumerate(session["files"]):
            point = json.loads(point_path.read_text(encoding="utf-8"))
            value = point_simulated(index) if callable(point_simulated) else point_simulated
            point["capture"]["simulated"] = value
            point_path.write_text(json.dumps(point), encoding="utf-8")
        arc_support.set_age(session["folder"], 600)
        return session

    def test_kinds(self):
        real, simulated = {"backend": False, "motor": False}, {"backend": True, "motor": False}
        cases = [
            (real, real, "real"), (simulated, simulated, "simulated"), (None, None, "unknown"),
            (real, None, "real"), (None, simulated, "simulated"), (real, simulated, "mixed"),
            (real, lambda index: simulated if index == 2 else real, "mixed"),
            ({"backend": "no", "motor": False}, None, "unknown"), (True, None, "unknown"),
        ]
        for session_value, point_value, kind in cases:
            self.home = arc_support.arc_home(self)
            self.paths = Paths(self.home)
            self.session_with(session_value, point_value)
            self.assertEqual(check(self.home).kind, kind, (session_value, point_value))

    def test_arc_sessions_reads_session_json_only(self):
        self.session_with({"backend": False, "motor": False}, {"backend": True, "motor": True})
        self.assertEqual(check(self.home, full=False).kind, "real")
        self.assertEqual(self.ok("arc-sessions")["items"][0]["kind"], "real")
        self.assertEqual(check(self.home).kind, "mixed")

    def test_the_policy(self):
        cases = {
            # kind, demo, include_simulated, declared -> (area, refusal)
            ("real", False, False, False): ("real", None),
            ("real", True, False, False): ("real", None),
            ("simulated", False, False, False): (None, "simulated_not_allowed"),
            ("simulated", False, True, False): ("simulated", None),
            ("simulated", True, False, False): ("simulated", None),
            ("unknown", False, False, False): (None, "simulation_unknown"),
            ("unknown", False, True, False): ("simulated", None),
            ("unknown", True, False, False): ("simulated", None),
            ("unknown", False, False, True): ("real", None),
            ("unknown", True, False, True): ("real", None),
            ("mixed", False, True, False): (None, "mixed"),
            ("mixed", True, True, False): (None, "mixed"),
        }
        for (kind, demo, include, declared), expected in cases.items():
            self.assertEqual(arc_session.policy(kind, demo, include, declared), expected,
                             (kind, demo, include, declared))

    def test_the_policy_through_arc_upload(self):
        self.session_with({"backend": True, "motor": True}, {"backend": True, "motor": True})
        with mock.patch("raman_datafed.arc_commands.setup_errors", return_value=[]):
            self.refused("refused", "simulated_not_allowed", "arc-upload", "--session=" + SESSION, "--live")
            self.refused("refused", "declare_real_not_unknown", "arc-upload", "--session=" + SESSION, "--demo",
                         "--declare-real")
            plan = self.ok("arc-upload", "--session=" + SESSION, "--demo", "--dry-run")["plan"]
            self.assertEqual(plan["area"], "simulated")
            self.assertIn("simulated/" + SESSION, plan["destination"]["cluster_path"])

    def test_declare_real_is_only_for_an_unknown_kind(self):
        self.session_with(None, None)
        with mock.patch("raman_datafed.arc_commands.setup_errors", return_value=[]):
            self.refused("refused", "simulation_unknown", "arc-upload", "--session=" + SESSION, "--live")
            plan = self.ok("arc-upload", "--session=" + SESSION, "--live", "--declare-real", "--dry-run")["plan"]
        self.assertEqual((plan["area"], plan["session"]["kind_declared_by_uploader"]), ("real", True))
        self.assertIn("You said this session was measured with the real instruments; the upload records that.",
                      plan["notes"])

    def test_a_mixed_session_is_refused_even_in_the_demo(self):
        self.session_with({"backend": False, "motor": False}, {"backend": True, "motor": True})
        answer = self.refused("refused", "mixed", "arc-upload", "--session=" + SESSION, "--demo")
        self.assertEqual(answer["error"], "Some points of this session were made in simulation and some were not, "
                                          "so it is not uploaded.")


class Darks(arc_support.ArcCase):
    def darks(self, declare=False, full=True):
        return check(self.home, idle=True, declare=declare, full=full).darks

    def test_a_dark_of_the_sessions_kind_goes_with_its_description(self):
        arc_support.ready_session(self.home)
        (entry,) = self.darks()
        self.assertEqual((entry["on_this_mac"], entry["kind"], entry["sent"], entry["reason"]),
                         (True, "simulated", True, None))
        self.assertEqual(entry["description"].name, "dark_20261113_101400_001.json")

    def test_a_missing_dark_is_a_warning(self):
        arc_support.ready_session(self.home, dark=False)
        result = check(self.home, idle=True)
        (entry,) = result.darks
        self.assertEqual((entry["on_this_mac"], entry["sent"]), (False, False))
        self.assertEqual(result.warnings, ["The dark spectrum %s that this session names is not on this Mac, so it was "
                                           "not sent." % support.DARK_NAME])
        self.assertIsNone(result.refusal)

    def test_a_dark_of_the_other_kind_is_not_sent(self):
        support.write_dark(self.home, simulated=False)
        arc_support.ready_session(self.home, simulated=True)
        result = check(self.home, idle=True)
        (entry,) = result.darks
        self.assertEqual((entry["kind"], entry["sent"]), ("real", False))
        self.assertTrue(entry["reason"].startswith("dark not used: the dark was taken with the real spectrometer"))
        self.assertEqual(result.warnings, ["The dark spectrum %s was not sent: the dark was taken with the real "
                                           "spectrometer; this spectrum is simulated." % support.DARK_NAME])

    def test_an_unknown_dark_with_a_real_session_is_not_sent(self):
        dark = support.write_dark(self.home, simulated=False)
        dark.with_suffix(".json").write_text("{not json", encoding="utf-8")
        arc_support.ready_session(self.home, simulated=False)
        (entry,) = self.darks()
        self.assertEqual((entry["kind"], entry["sent"]), ("unknown", False))

    def test_a_session_of_unknown_kind_takes_its_dark_whatever_the_kind(self):
        support.write_dark(self.home, simulated=True)
        session = arc_support.ready_session(self.home)
        for path in [session["folder"] / "session.json"] + session["files"]:
            content = json.loads(path.read_text(encoding="utf-8"))
            (content.get("capture") or content)["simulated"] = None
            path.write_text(json.dumps(content), encoding="utf-8")
        arc_support.set_age(session["folder"], 600)
        (entry,) = self.darks()
        self.assertEqual((entry["kind"], entry["sent"]), ("simulated", True))
        # Declared real: only a real dark goes.
        (entry,) = self.darks(declare=True)
        self.assertEqual((entry["kind"], entry["sent"]), ("simulated", False))

    def test_a_motor_only_simulation_takes_the_real_dark_its_spectra_were_paired_with(self):
        support.write_dark(self.home, simulated=False)
        session = arc_support.ready_session(self.home, simulated=False)
        for path in [session["folder"] / "session.json"] + session["files"]:
            content = json.loads(path.read_text(encoding="utf-8"))
            (content.get("capture") or content)["simulated"] = {"backend": False, "motor": True}
            path.write_text(json.dumps(content), encoding="utf-8")
        arc_support.set_age(session["folder"], 600)
        result = check(self.home, idle=True)
        self.assertEqual(result.kind, "simulated")
        self.assertTrue(result.darks[0]["sent"])

    def test_two_dark_names_from_the_session_and_its_points(self):
        support.write_dark(self.home)
        other = "dark_20261113_101300_001.txt"
        support.write_dark(self.home, name=other)
        session = arc_support.ready_session(self.home, points=2)
        point = json.loads(session["files"][1].read_text(encoding="utf-8"))
        point["capture"]["dark_file"] = other
        session["files"][1].write_text(json.dumps(point), encoding="utf-8")
        arc_support.set_age(session["folder"], 600)
        self.assertEqual([entry["file"] for entry in self.darks()], [support.DARK_NAME, other])
        self.assertEqual([entry["file"] for entry in self.darks(full=False)], [support.DARK_NAME])

    def test_a_dark_name_that_points_elsewhere_is_never_read(self):
        session = arc_support.ready_session(self.home)
        path = session["folder"] / "session.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        content["dark_file"] = "../datafed_config.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        arc_support.set_age(session["folder"], 600)
        (entry,) = check(self.home, idle=True, full=False).darks
        self.assertEqual((entry["on_this_mac"], entry["sent"]), (False, False))


class Listing(arc_support.ArcCase):
    def test_sessions_newest_first_and_only_real_folders(self):
        arc_support.ready_session(self.home, folder="1d_x_scan_a")
        newer = arc_support.ready_session(self.home, folder="1d_x_scan_b", dark=False)
        path = newer["folder"] / "session.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        content["started_utc"] = "2026-11-14T10:00:00.000Z"
        path.write_text(json.dumps(content), encoding="utf-8")
        scan_sessions = self.home / "scan_sessions"
        (scan_sessions / "_sim_stage.json").write_text("{}", encoding="utf-8")
        (scan_sessions / "_importing_x").mkdir()
        (scan_sessions / ".hidden").mkdir()
        (scan_sessions / "linked").symlink_to(newer["folder"])
        names = [folder.name for folder in arc_session.session_folders(self.paths)]
        self.assertEqual(names, ["1d_x_scan_b", "1d_x_scan_a"])
        answer = self.ok("arc-sessions", "--limit=1")
        self.assertEqual((answer["total"], [item["folder"] for item in answer["items"]]), (2, ["1d_x_scan_b"]))
        self.refused("not_found", "unknown_session", "arc-upload", "--session=linked", "--demo")
        self.refused("not_found", "unknown_session", "arc-upload", "--session=_importing_x", "--demo")
        self.refused("not_found", "unknown_session", "arc-upload", "--session=../scan_sessions/1d_x_scan_a", "--demo")

    def test_the_item_of_arc_sessions(self):
        arc_support.ready_session(self.home)
        started = time.monotonic()
        item = self.ok("arc-sessions")["items"][0]
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(item["files"], 13 + 1 + 2 + 2)
        self.assertEqual(item["darks"][0]["file"], support.DARK_NAME)
        self.assertEqual((item["live"]["allowed"], item["live"]["why_not"]["reason"]),
                         (False, "simulated_not_allowed"))
        self.assertEqual((item["demo"]["allowed"], item["demo"]["area"]), (True, "simulated"))
        self.assertTrue(item["demo"]["demo_path"].endswith(
            "data_catalog/arc_demo/projects/pcl_photonics/raman_globus_upload/simulated/" + SESSION))
        self.assertEqual(item["uploads"], {"live": None, "demo": None})


if __name__ == "__main__":
    unittest.main()
