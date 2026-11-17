"""The arc-* commands as the window and the Studio run them, and the worker program (design 8, 9, 13)."""

import contextlib
import fcntl
import io
import json
import logging
import os
import shlex
import signal
import stat
import string
import subprocess
import sys
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import arc_support
import support
from raman_catalog import arc_commands, arc_payload, arc_records, arc_settings, arc_worker, doctor
from raman_catalog.arc_records import REFUSALS, WORKER_RESULTS, set_state
from raman_catalog.arc_worker import GuardRefused

SESSION = arc_support.SESSION
APPENDIX_A1 = (
    "unknown_session", "unknown_upload", "no_session_json", "unreadable_session", "no_points", "running", "settling",
    "future_time", "odd_name", "link", "special_file", "name_clash", "simulated_not_allowed", "simulation_unknown",
    "mixed", "declare_real_not_unknown", "not_demo", "in_progress", "earlier_upload", "already_on_arc",
    "another_live_upload", "busy", "settings_invalid", "mac_collection_missing", "username_missing",
    "known_hosts_unreadable", "globus_sdk_missing", "ssh_missing", "terminal_missing", "not_macos", "not_retryable",
    "not_checkable", "nothing_to_stop", "no_conflict", "payload_missing", "terminal_not_opened")
APPENDIX_A2 = (
    "no_space", "changed_during_copy", "payload_changed", "signin_timeout", "no_browser", "signin_failed", "consent",
    "mac_collection_unknown", "gcp_not_connected", "source_not_visible", "source_mismatch", "listing_truncated",
    "arc_folder_not_dir", "destination_permission", "refused_by_globus", "request_conflict", "task_failed",
    "task_not_visible", "verify_mismatch", "ssh_signin_failed", "ssh_host_refused_commands", "ssh_interrupted",
    "terminal_closed", "remote_tools", "remote_failed", "terminal_not_opened", "internal")
LIVE_SSH = {"demo": False, "transport": "ssh", "ssh": {"username": arc_support.TEST_USERNAME}}


def fields(template):
    return {name: "X" for _text, name, _spec, _conversion in string.Formatter().parse(template) if name}


def one_object(test, stdout):
    test.assertEqual(stdout.count("\n"), 1, stdout)
    value = json.loads(stdout)
    test.assertIsInstance(value, dict)
    return value


class Texts(unittest.TestCase):
    def test_every_reason_of_appendix_a_has_its_text_and_a_fix(self):
        self.assertLessEqual(set(APPENDIX_A1), set(REFUSALS))
        self.assertLessEqual(set(APPENDIX_A2), set(WORKER_RESULTS))
        for reason, (kind, error, fix) in REFUSALS.items():
            made = arc_records.refusal(reason, **{**fields(error), **fields(fix)})
            self.assertEqual((made.kind, made.reason), (kind, reason))
            self.assertTrue(str(made) and made.fix, reason)
            self.assertNotIn("{", str(made) + made.fix, reason)
            self.assertEqual(made.extra["reason"], reason)
        for reason, (message, fix, retryable) in WORKER_RESULTS.items():
            made = arc_records.worker_error(reason, **{**fields(message), **fields(fix)})
            self.assertEqual(set(made), {"kind", "reason", "message", "fix", "retryable"})
            self.assertIs(made["retryable"], retryable)
            self.assertTrue(made["message"] and made["fix"])

    def test_texts_of_the_design(self):
        self.assertEqual(str(arc_records.refusal("unknown_session", folder="f")),
                         "There is no session folder f in scan_sessions.")
        self.assertEqual(arc_records.refusal("mac_collection_missing").fix,
                         "Open ARC settings and paste it: Globus Connect Personal (its icon in the menu bar) > "
                         "Settings shows it (command line: config --set arc_upload.globus.mac_collection=<ID>).")
        self.assertEqual(arc_records.refusal("not_retryable", label="Failed").fix, "(The button is disabled.)")
        self.assertEqual(arc_records.worker_error("ssh_interrupted", incoming="/p/_incoming/x")["message"],
                         "The SSH connection ended during the upload. Files sent so far are in /p/_incoming/x on ARC.")

    def test_demo_messages(self):
        self.assertEqual(arc_records.demo_text("Waiting to start."), "Demo: waiting to start.")
        self.assertEqual(arc_records.demo_text("ARC could not rename the folder."),
                         "Demo: ARC could not rename the folder.")
        self.assertEqual(arc_records.demo_text("Globus keeps copying."), "Demo: Globus keeps copying.")
        self.assertEqual(arc_records.demo_text("Demo: as it is."), "Demo: as it is.")
        self.assertEqual(arc_records.demo_text("Waiting.", False), "Waiting.")
        # "A" is a word, not a short form like SSH.
        self.assertEqual(arc_records.demo_text("A copy of this upload is in the demo folder."),
                         "Demo: a copy of this upload is in the demo folder.")
        self.assertEqual(arc_records.demo_text("SSH ended."), "Demo: SSH ended.")


class Contract(arc_support.ArcCase):
    """Run as the window runs it: a child process, one JSON object on stdout."""

    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home)

    def run_cli(self, *arguments, extra_environment=None):
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        env.update(extra_environment or {})
        done = subprocess.run([sys.executable, "-m", "raman_catalog", "--home", str(self.home), *arguments],
                              capture_output=True, text=True, timeout=60, cwd=str(self.home), env=env)
        return done.returncode, done.stdout, done.stderr

    def test_every_command_prints_one_json_object_and_exits_0(self):
        for arguments in (("arc-sessions",), ("arc-sessions", "--limit=5", "--controller-idle"), ("arc-list",),
                          ("arc-list", "--limit=3", "--session=" + SESSION), ("arc-doctor",),
                          ("arc-upload", "--session=" + SESSION, "--demo", "--dry-run")):
            code, stdout, _stderr = self.run_cli(*arguments)
            value = one_object(self, stdout)
            self.assertEqual((code, value["ok"]), (0, True), arguments)
            self.assertEqual(list(value)[0], "ok")

    def test_a_failure_has_the_four_keys_then_reason_and_fix_and_exits_1(self):
        for arguments, kind, reason in ((("arc-status", "--upload=u000000000000"), "not_found", "unknown_upload"),
                                        (("arc-upload", "--session=nothing", "--demo"), "not_found",
                                         "unknown_session"),
                                        (("arc-retry", "--upload=x"), "not_found", "unknown_upload")):
            code, stdout, _stderr = self.run_cli(*arguments)
            value = one_object(self, stdout)
            self.assertEqual(code, 1, arguments)
            self.assertEqual(list(value), ["ok", "error", "detail", "kind", "reason", "fix"])
            self.assertEqual((value["kind"], value["reason"]), (kind, reason))
            self.assertTrue(value["fix"])

    def test_wrong_arguments_are_usage_with_exit_2(self):
        for arguments in (("arc-upload",), ("arc-upload", "--session=" + SESSION, "--transport=scp"),
                          ("arc-list", "--surprise"), ("arc-list", "--limit=many"),
                          ("arc-upload", "--session=" + SESSION, "--demo", "--live"),
                          ("arc-upload", "--session", "--live"), ("arc-status",), ("arc-retry", "--upload"),
                          ("arc-upload", "--session=" + SESSION, "--demo", "--demo-fault=nope"),
                          ("arc-sessions", "--limit=0")):
            code, stdout, stderr = self.run_cli(*arguments)
            value = one_object(self, stdout)
            self.assertEqual((code, value["ok"], value["kind"], value["reason"]), (2, False, "usage", "usage"),
                             arguments)
            self.assertTrue(value["fix"])
            self.assertEqual(stderr, "", arguments)

    def test_help_is_a_json_object(self):
        code, stdout, _stderr = self.run_cli("arc-upload", "-h")
        value = one_object(self, stdout)
        self.assertEqual((code, value["ok"]), (0, True))
        self.assertIn("--declare-real", value["help"])

    def test_a_session_value_is_never_read_as_an_option(self):
        code, stdout, _stderr = self.run_cli("arc-upload", "--session=--live")
        value = one_object(self, stdout)
        self.assertEqual((code, value["kind"], value["reason"]), (1, "not_found", "unknown_session"))
        self.assertIn("--live", value["error"])

    def test_a_demo_fault_needs_a_demo_upload(self):
        code, stdout, _stderr = self.run_cli("arc-upload", "--session=" + SESSION, "--live",
                                             "--demo-fault=lose_submit_reply")
        self.assertEqual((code, one_object(self, stdout)["kind"]), (2, "usage"))
        arc_support.write_settings(self.home, {"demo": False})
        code, stdout, _stderr = self.run_cli("arc-upload", "--session=" + SESSION, "--demo-fault=drop_connection")
        self.assertEqual((code, one_object(self, stdout)["kind"]), (2, "usage"))

    def test_a_dry_run_creates_nothing(self):
        code, stdout, _stderr = self.run_cli("arc-upload", "--session=" + SESSION, "--demo", "--dry-run")
        self.assertEqual(code, 0)
        plan = one_object(self, stdout)["plan"]
        self.assertEqual(plan["files"], 18)
        self.assertFalse((self.home / "data_catalog").exists())
        self.assertFalse((self.home / "catalog_config.json").exists())

    def test_a_demo_fault_is_kept_in_the_record(self):
        upload_id = self.upload("--demo-fault=fail_task_once")["upload"]["upload_id"]
        self.assertEqual(self.record(upload_id)["demo_fault"], "fail_task_once")


class Spawning(arc_support.ArcCase):
    spawn_workers = True

    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home)

    def test_a_detached_worker_is_started_with_nothing_inherited(self):
        with mock.patch.dict(os.environ, {"PYTHONSTARTUP": "/x.py", "PYTHONINSPECT": "1"}), \
                mock.patch.object(doctor, "_running_commands", return_value=[]), \
                mock.patch.object(arc_worker.subprocess, "Popen") as popen:
            answer = self.upload()
        upload_id = answer["upload"]["upload_id"]
        self.assertEqual((answer["worker"], answer["message"]),
                         ("detached", "Demo upload started. Nothing leaves this Mac."))
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "raman_catalog.arc_worker", "--home",
                                                   str(self.home), "--upload", upload_id, "--action", "send"])
        options = popen.call_args.kwargs
        self.assertEqual(options["cwd"], str(self.home))
        for name in ("stdin", "stdout", "stderr"):
            self.assertIs(options[name], subprocess.DEVNULL, name)
        self.assertIs(options["start_new_session"], True)
        self.assertIs(options["close_fds"], True)
        env = options["env"]
        self.assertTrue(env["PYTHONPATH"].startswith(str(arc_worker.data_layer_folder())))
        self.assertTrue((Path(env["PYTHONPATH"].split(os.pathsep)[0]) / "raman_catalog" / "arc_worker.py").is_file())
        self.assertNotIn("PYTHONSTARTUP", env)
        self.assertNotIn("PYTHONINSPECT", env)
        self.assertEqual(env["RAMAN_ARC_TEST_GUARD"], "1")

    def test_ended_workers_are_reaped(self):
        ended, running = mock.Mock(), mock.Mock()
        ended.poll.return_value = 0
        running.poll.return_value = None
        with mock.patch.object(arc_worker, "_CHILDREN", [ended, running]):
            self.ok("arc-list")
            self.assertEqual(arc_worker._CHILDREN, [running])

    def test_the_guard_starts_nothing_but_the_worker(self):
        with self.assertRaises(GuardRefused):
            arc_worker.check_spawn([sys.executable, "-m", "raman_catalog.studio_server"])
        arc_worker.check_spawn(arc_worker.worker_argv(self.home, "u0123456789ab", "send"))


class Guard(arc_support.ArcCase):
    def test_programs_that_reach_out_are_refused_before_they_start(self):
        with mock.patch.object(arc_worker.subprocess, "run") as run, \
                mock.patch.object(arc_worker.subprocess, "Popen") as popen:
            for argv in (["/usr/bin/ssh", "-T", "x"], ["/usr/bin/open", "-a", "Terminal", "x"],
                         ["/usr/bin/caffeinate", "-i"], ["osascript", "-e", "x"], ["/usr/bin/scp", "a", "b"]):
                with self.assertRaises(GuardRefused):
                    arc_worker.run_program(argv, 1)
                with self.assertRaises(GuardRefused):
                    arc_worker.popen_program(argv)
            with self.assertRaises(GuardRefused):
                arc_worker.open_terminal(self.home / "ssh_upload.command")
            run.assert_not_called()
            popen.assert_not_called()

    def test_the_live_globus_client_is_refused(self):
        with self.assertRaises(GuardRefused):
            arc_worker.check_live_globus()

    def test_a_harmless_program_runs(self):
        done = arc_worker.run_program(["/bin/echo", "x"], 10, capture_output=True, text=True)
        self.assertEqual(done.stdout, "x\n")

    def test_every_arc_test_runs_with_the_guard(self):
        self.assertEqual(os.environ.get("RAMAN_ARC_TEST_GUARD"), "1")

    def test_the_demo_pace(self):
        self.assertEqual(arc_worker.demo_pace(), 0.0)
        for value, pace in (("0.5", 0.5), ("fast", 0.1), ("-1", 0.1)):
            with mock.patch.dict(os.environ, {"RAMAN_ARC_DEMO_PACE": value}):
                self.assertEqual(arc_worker.demo_pace(), pace, value)
        with mock.patch.dict(os.environ):
            del os.environ["RAMAN_ARC_DEMO_PACE"]
            self.assertEqual(arc_worker.demo_pace(), 0.1)


class LiveSsh(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        arc_support.write_settings(self.home, LIVE_SSH)
        arc_support.ready_session(self.home, simulated=False)
        self.programs = []
        self.open_result = subprocess.CompletedProcess([], 0, "", "")

        def refuse(*_arguments, **_options):
            raise AssertionError("the upload ran a program other than through run_program")

        # Nothing in the upload part may run a program but through run_program (here: recorded).
        no_programs = types.SimpleNamespace(run=refuse, Popen=refuse, DEVNULL=subprocess.DEVNULL,
                                            PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired)
        for target, name, value in ((arc_commands, "setup_errors", mock.Mock(return_value=[])),
                                    (doctor, "_running_commands", mock.Mock(return_value=[])),
                                    (arc_worker, "run_program", mock.Mock(side_effect=self._run_program)),
                                    (arc_worker, "subprocess", no_programs)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run_program(self, argv, timeout, **options):
        self.programs.append((list(argv), timeout))
        return self.open_result

    def expected_script(self, upload_id, attempt, action="send"):
        home, data_layer = str(self.home), str(arc_worker.data_layer_folder())
        return textwrap.dedent('''\
            #!/bin/bash
            # Portable Raman: upload %s to ARC over SSH (upload %s, attempt %d).
            # Written by the data layer for this one attempt. It runs the data layer, which runs /usr/bin/ssh.
            # Your VT password and Duo go to ssh only. This file holds no password, and nothing here saves one.
            export PATH=/usr/bin:/bin:/usr/sbin:/sbin
            unset PYTHONSTARTUP PYTHONINSPECT PYTHONHOME BASH_ENV ENV SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY
            export PYTHONPATH='%s'
            cd '%s' || exit 1
            '%s' -m raman_catalog.arc_worker --home '%s' --upload %s --action %s --terminal --attempt %d
            status=$?
            echo
            read -r -p "Press Return to close this window. " _
            exit $status
            ''') % (SESSION, upload_id, attempt, data_layer, home, sys.executable, home, upload_id, action, attempt)

    def test_a_terminal_window_runs_the_attempt(self):
        answer = self.ok("arc-upload", "--session=" + SESSION)
        upload_id = answer["upload"]["upload_id"]
        # SEC-03: the reply names the VT account the window signs in with.
        self.assertEqual((answer["worker"], answer["message"]),
                         ("terminal", "A Terminal window opens. Sign in there once as %s: the VT password of that "
                                      "account, then Duo." % arc_support.TEST_USERNAME))
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["ssh"]["attempt"], record["demo"]),
                         ("waiting_for_terminal", 1, False))
        self.assertTrue(record["ssh"]["terminal_opened_utc"])
        self.assertEqual(record["ssh"]["username"], arc_support.TEST_USERNAME)
        self.assertEqual(answer["upload"]["destination"]["ssh_target"],
                         "%s@%s:%s/%s" % (arc_support.TEST_USERNAME, "datatransfer.arc.vt.edu", arc_support.ARC_FOLDER,
                                          SESSION))
        self.assertEqual(answer["upload"]["needs"], "terminal")
        script = self.record_dir(upload_id) / "ssh_upload.command"
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
        self.assertEqual(script.read_text(encoding="utf-8"), self.expected_script(upload_id, 1))
        self.assertEqual(self.programs, [(["/usr/bin/open", "-a", "Terminal", str(script)], 15)])
        self.assertFalse(any("osascript" in " ".join(argv) for argv, _timeout in self.programs))
        self.assertEqual(self.spawned, [])
        self.assertNotIn("password", json.dumps(record).lower().replace("vt password", ""))

    def test_values_with_spaces_and_quotes_round_trip(self):
        home = "/tmp/a b'c \"d\" $(x)"
        text = arc_worker.terminal_script_text(home, "u0123456789ab", 2, "send", SESSION,
                                               interpreter="/opt/my venv/bin/python", data_layer=home + "/data_layer")
        lines = text.splitlines()
        self.assertEqual(shlex.split(lines[8]), ["/opt/my venv/bin/python", "-m", "raman_catalog.arc_worker", "--home",
                                                 home, "--upload", "u0123456789ab", "--action", "send", "--terminal",
                                                 "--attempt", "2"])
        self.assertEqual(shlex.split(lines[6]), ["export", "PYTHONPATH=" + home + "/data_layer"])
        self.assertEqual(shlex.split(lines[7]), ["cd", home, "||", "exit", "1"])
        for bad in ({"home": "/tmp/a\nb"}, {"upload_id": "u01"}, {"action": "stop"}, {"folder_name": "a b"}):
            values = {"home": "/tmp", "upload_id": "u0123456789ab", "attempt": 1, "action": "send",
                      "folder_name": SESSION}
            values.update(bad)
            with self.assertRaises(ValueError):
                arc_worker.terminal_script_text(**values)

    def test_a_terminal_that_does_not_open_leaves_the_attempt_to_a_double_click_or_retry(self):
        # SSH-04: the upload waits for its Terminal window (so that ssh_upload.command still runs this attempt
        # when it is double-clicked or when Terminal opens late), and shows as not started at once.
        self.open_result = subprocess.CompletedProcess([], 1, "", "LSOpenURLsWithRole() failed.\n")
        answer = self.refused("command", "terminal_not_opened", "arc-upload", "--session=" + SESSION)
        self.assertEqual(answer["error"], "Terminal could not be opened: LSOpenURLsWithRole() failed.")
        self.assertEqual(answer["fix"], "Double-click ssh_upload.command in the upload's folder, or press Retry same "
                                        "request.")
        upload_id = answer["upload"]["upload_id"]
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"], record["ssh"]["attempt"]),
                         ("waiting_for_terminal", None, 1))
        self.assertTrue(record["ssh"]["terminal_open_failed_utc"])
        self.assertEqual((answer["upload"]["display_state"], answer["upload"]["actions"]),
                         ("not_started", ["retry", "stop"]))
        # A double-click runs the attempt's script: the worker takes it.
        script = self.record_dir(upload_id) / "ssh_upload.command"
        self.assertEqual(script.read_text(encoding="utf-8"), self.expected_script(upload_id, 1))
        self.assertIsNone(arc_worker._not_its_turn(record, "send", True, 1))
        # Retry same request: a new attempt, a new window.
        self.open_result = subprocess.CompletedProcess([], 0, "", "")
        retried = self.ok("arc-retry", "--upload=" + upload_id)
        self.assertEqual(retried["worker"], "terminal")
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["ssh"]["attempt"], record["ssh"]["terminal_open_failed_utc"]),
                         ("waiting_for_terminal", 2, None))
        self.assertEqual(self.ok("arc-status", "--upload=" + upload_id)["upload"]["display_state"],
                         "waiting_for_terminal")
        self.assertEqual(script.read_text(encoding="utf-8"), self.expected_script(upload_id, 2))

    def test_a_check_whose_terminal_does_not_open_names_check_status(self):
        upload_id = self.ok("arc-upload", "--session=" + SESSION)["upload"]["upload_id"]
        arc_records.update_record(self.record_dir(upload_id), lambda current: current.update(state="verified"))
        self.open_result = subprocess.CompletedProcess([], 1, "", "failed\n")
        answer = self.refused("command", "terminal_not_opened", "arc-check", "--upload=" + upload_id)
        self.assertEqual(answer["fix"], "Double-click ssh_upload.command in the upload's folder, or press Check "
                                        "status again.")
        self.assertEqual(self.record(upload_id)["state"], "verified")

    def test_check_status_opens_a_terminal_in_check_mode_and_keeps_the_state(self):
        upload_id = self.ok("arc-upload", "--session=" + SESSION)["upload"]["upload_id"]
        arc_records.update_record(self.record_dir(upload_id), lambda current: (
            set_state(current, "failed", "Failed."), current.update(error={"retryable": True}),
            current["ssh"].update(sent=True)))
        answer = self.ok("arc-check", "--upload=" + upload_id)
        self.assertEqual(answer["message"],
                         "A Terminal window opens. Sign in there once as %s: the VT password of that account, then "
                         "Duo. Nothing is sent." % arc_support.TEST_USERNAME)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["ssh"]["attempt"]), ("failed", 2))
        script = self.record_dir(upload_id) / "ssh_upload.command"
        self.assertEqual(script.read_text(encoding="utf-8"), self.expected_script(upload_id, 2, "check"))

    def test_retry_names_the_account_the_upload_keeps(self):
        # SEC-03: Retry same request signs in with the VT username saved with the upload, not a newer setting.
        upload_id = self.ok("arc-upload", "--session=" + SESSION)["upload"]["upload_id"]
        arc_records.update_record(self.record_dir(upload_id), lambda current: (
            set_state(current, "failed", "Failed."), current.update(error={"retryable": True})))
        arc_support.write_settings(self.home, dict(LIVE_SSH, ssh={"username": "other_user_1"}))
        answer = self.ok("arc-retry", "--upload=" + upload_id)
        self.assertIn("Sign in there once as %s:" % arc_support.TEST_USERNAME, answer["message"])
        self.assertTrue(answer["upload"]["destination"]["ssh_target"].startswith(arc_support.TEST_USERNAME + "@"))

    def test_a_window_of_an_earlier_attempt_does_nothing(self):
        upload_id = self.ok("arc-upload", "--session=" + SESSION)["upload"]["upload_id"]
        arc_records.update_record(self.record_dir(upload_id), lambda current: current["ssh"].update(attempt=2))
        code, _stdout, stderr = arc_support.run_worker_program(self.home, "--upload", upload_id, "--action", "send",
                                                               "--terminal", "--attempt", "1")
        self.assertEqual(code, arc_worker.EXIT_NOT_ITS_TURN)
        self.assertIn(arc_worker.EARLIER_ATTEMPT_TEXT, stderr)
        self.assertEqual(self.record(upload_id)["state"], "waiting_for_terminal")

    def test_another_live_upload_that_runs_refuses_a_second(self):
        first = self.ok("arc-upload", "--session=" + SESSION)["upload"]["upload_id"]
        arc_support.hold_lock(self, self.record_dir(first) / "worker.lock")
        arc_support.ready_session(self.home, folder="1d_x_scan_other", simulated=False, dark=False)
        answer = self.refused("conflict", "another_live_upload", "arc-upload", "--session=1d_x_scan_other")
        self.assertEqual(answer["error"], "Another upload to ARC is running (%s)." % SESSION)
        # A demo upload is not held back.
        self.ok("arc-upload", "--session=1d_x_scan_other", "--demo")
        # A second click on the running one shows it.
        again = self.ok("arc-upload", "--session=" + SESSION)
        self.assertEqual((again["existing"], again["upload"]["upload_id"]), (True, first))


class Setup(arc_support.ArcCase):
    def test_what_a_live_upload_lacks(self):
        values = arc_settings.effective(_settings(self.home, {}))
        reasons = [error.reason for error in arc_commands.setup_errors("globus", values, platform="linux")]
        self.assertEqual(reasons, ["not_macos"])
        with mock.patch("importlib.util.find_spec", side_effect=_find_spec_without_globus):
            reasons = [error.reason for error in arc_commands.setup_errors("globus", values, platform="darwin")]
        self.assertEqual(reasons, ["mac_collection_missing", "globus_sdk_missing"])
        writable = self.home / "known_hosts"
        writable.write_text("x\n", encoding="utf-8")
        os.chmod(writable, 0o666)
        values["ssh"]["known_hosts"] = str(writable)
        with mock.patch.object(arc_commands, "_executable", return_value=False):
            errors = arc_commands.setup_errors("ssh", values, platform="darwin")
        self.assertEqual([error.reason for error in errors],
                         ["username_missing", "known_hosts_unreadable", "ssh_missing", "terminal_missing"])
        self.assertEqual(str(errors[1]), "The team file of ARC's SSH keys cannot be used: %s." % writable)

    def test_a_live_globus_upload_without_the_sdk_or_the_collection(self):
        arc_support.write_settings(self.home, {"demo": False})
        arc_support.ready_session(self.home, simulated=False)
        with mock.patch.object(arc_commands.sys, "platform", "darwin"):
            self.refused("config", "mac_collection_missing", "arc-upload", "--session=" + SESSION)
            arc_support.write_settings(self.home, {"demo": False, "globus": {
                "mac_collection": "0b9e2d4c-1a2b-4c3d-8e4f-5a6b7c8d9e0f"}})
            with mock.patch("importlib.util.find_spec", side_effect=_find_spec_without_globus):
                answer = self.refused("not_installed", "globus_sdk_missing", "arc-upload", "--session=" + SESSION)
        # Review DOC-10: the folder setup_env.sh is run in, as seen from the deploy folder.
        self.assertEqual(answer["fix"], "In Terminal, in Portable_Raman_Catalog/ (from deploy-macos/, cd .. first), "
                                        "run ./data_layer/setup_env.sh once, or send with SSH.")

    def test_arc_list_says_what_each_transport_lacks(self):
        with mock.patch("importlib.util.find_spec", side_effect=_find_spec_without_globus):
            ready = self.ok("arc-list")["ready"]
        self.assertEqual(ready["demo"], {"ok": True, "problems": []})
        reasons = [problem["reason"] for problem in ready["globus"]["problems"]]
        self.assertIn("globus_sdk_missing", reasons)
        self.assertFalse(ready["ssh"]["ok"])
        for problem in ready["globus"]["problems"] + ready["ssh"]["problems"]:
            self.assertEqual(set(problem), {"reason", "text", "fix"})

    def test_arc_doctor(self):
        answer = self.ok("arc-doctor")
        names = [check["name"] for check in answer["checks"]]
        expected = ["settings_valid", "macos", "globus_sdk_installed", "mac_collection_set"]
        if sys.platform == "darwin":
            expected += ["globus_connect_personal_installed", "globus_connect_personal_running"]
        expected += ["client_id_is_registered", "ssh_username_set", "ssh_program", "terminal_app", "open_program",
                     "known_hosts_file", "demo_folder_writable", "remote_script_present"]
        self.assertEqual(names, expected)
        for check in answer["checks"]:
            self.assertEqual(set(check), {"name", "ok", "detail", "fix"})
            self.assertEqual(bool(check["fix"]), not check["ok"], check)
        tutorial = {check["name"]: check for check in answer["checks"]}["client_id_is_registered"]
        self.assertEqual((tutorial["ok"], tutorial["detail"]),
                         (False, "Sign-in uses the Globus SDK's public example app."))
        self.assertEqual(tutorial["fix"], "For regular use, register an app for the team and set "
                                          "arc_upload.globus.client_id.")
        self.assertEqual(set(answer["ready"]), {"globus", "ssh", "demo"})
        self.assertTrue(answer["ready"]["demo"])
        self.assertEqual(answer["settings"], arc_settings.flatten(arc_settings.DEFAULTS))


def _settings(home, group):
    from raman_catalog.config import Settings, DEFAULTS
    import copy

    data = copy.deepcopy(DEFAULTS)
    data["arc_upload"] = group
    return Settings(data, Path(home) / "catalog_config.json", home)


_REAL_FIND_SPEC = __import__("importlib.util").util.find_spec


def _find_spec_without_globus(name, *arguments):
    return None if name == "globus_sdk" else _REAL_FIND_SPEC(name, *arguments)


NO_GLOBUS_IMPORT = textwrap.dedent('''
    import contextlib, io, json, sys
    sys.path.insert(0, sys.argv[1])
    from raman_catalog import cli
    home = sys.argv[2]
    for arguments in (["arc-sessions"], ["arc-list"], ["arc-doctor"], ["arc-status", "--upload=u000000000000"],
                      ["arc-upload", "--session=%s", "--demo", "--dry-run"],
                      ["arc-upload", "--session=%s", "--live", "--transport=globus"]):
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--home", home] + arguments)
    print(json.dumps(sorted(name for name in sys.modules if name.split(".")[0] == "globus_sdk")))
''' % (SESSION, SESSION))


class Imports(arc_support.ArcCase):
    def test_no_command_imports_globus_sdk(self):
        arc_support.ready_session(self.home)
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        done = subprocess.run([sys.executable, "-c", NO_GLOBUS_IMPORT, str(support.DATA_LAYER), str(self.home)],
                              capture_output=True, text=True, timeout=120, env=env, cwd=str(self.home))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]), [])


class Timing(arc_support.ArcCase):
    def test_listing_answers_at_once_while_a_worker_works(self):
        arc_support.ready_session(self.home)
        upload_id = self.upload()["upload"]["upload_id"]
        folder = self.record_dir(upload_id)
        arc_records.update_record(folder, lambda current: current.update(state="transferring"))
        arc_support.hold_lock(self, folder / "worker.lock")
        for arguments in (("arc-list",), ("arc-status", "--upload=" + upload_id), ("arc-sessions",)):
            started = time.monotonic()
            code, value, _stderr = arc_support.run_cli(self.home, *arguments)
            self.assertLess(time.monotonic() - started, 2.0, arguments)
            self.assertEqual(code, 0, value)
        self.assertEqual(value["items"][0]["uploads"]["demo"]["upload_id"], upload_id)
        code, value, _stderr = arc_support.run_cli(self.home, "arc-list")
        self.assertEqual((value["active"], value["items"][0]["worker"]["alive"], value["items"][0]["actions"]),
                         (1, True, ["stop"]))


class Actions(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home)
        self.upload_id = self.upload()["upload"]["upload_id"]
        self.folder = self.record_dir(self.upload_id)

    def change(self, **values):
        def mutate(current):
            for key, value in values.items():
                if isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value

        arc_records.update_record(self.folder, mutate)

    def test_retry_of_a_failed_upload_starts_a_worker_with_the_same_request(self):
        self.change(state="failed", error={"retryable": True, "reason": "gcp_not_connected"},
                    globus={"submission_id": "0b9e2d4c-0000-4000-8000-000000000001"},
                    requests={"stop_utc": "2026-11-15T16:00:00.000Z"})
        answer = self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertTrue(answer["same_submission"])
        self.assertEqual(answer["worker"], "detached")
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["resume"], record["error"], record["requests"]["stop_utc"]),
                         ("queued", {"at": "submitting"}, None, None))
        self.assertEqual(self.spawned[-1], (str(self.home), self.upload_id, "send"))

    def test_retry_is_refused_where_the_table_says_so(self):
        self.change(state="failed", error={"retryable": False, "reason": "payload_changed"})
        answer = self.refused("command", "not_retryable", "arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(answer["error"], "This upload cannot be retried while it is Failed.")
        self.change(error={"retryable": True}, payload={"files": 1, "bytes": 1, "manifest_sha256": "0" * 64,
                                                        "kept": True})
        self.refused("command", "payload_missing", "arc-retry", "--upload=" + self.upload_id)

    def test_keep_as_new_copy(self):
        self.refused("command", "no_conflict", "arc-keep-copy", "--upload=" + self.upload_id)
        self.change(state="conflict", conflict_at="placing")
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["resume"], record["destination"]["new_copy"],
                          record["conflict_at"]), ("queued", {"at": "placing"}, True, None))
        self.change(state="conflict", conflict_at="checking", resume=None)
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        self.assertEqual(self.record(self.upload_id)["resume"], {"at": "checking"})

    def test_check_status(self):
        self.refused("command", "not_checkable", "arc-check", "--upload=" + self.upload_id)
        self.change(state="verified")
        answer = self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(answer["message"], "Demo: checking the upload. Nothing leaves this Mac.")
        self.assertEqual(self.spawned[-1][2], "check")
        self.assertEqual(self.record(self.upload_id)["state"], "verified")

    def test_stop_before_anything_was_sent(self):
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertTrue(answer["stopped_now"])
        self.assertEqual(answer["message"], "Demo: stopped before anything was sent.")
        self.assertEqual(self.record(self.upload_id)["state"], "stopped")
        self.refused("command", "nothing_to_stop", "arc-stop", "--upload=" + self.upload_id)

    def test_stop_of_a_conflict_says_where_the_copy_stays(self):
        self.change(state="conflict", conflict_at="placing")
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertIn("Files already copied stay in", answer["message"])
        self.assertIn("arc_demo/projects/pcl_photonics/raman_globus_upload/simulated/_incoming/", answer["message"])

    def test_stop_of_a_running_worker_is_a_request(self):
        self.change(state="transferring")
        arc_support.hold_lock(self, self.folder / "worker.lock")
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertFalse(answer["stopped_now"])
        record = self.record(self.upload_id)
        self.assertTrue(record["requests"]["stop_utc"])
        self.assertEqual(record["state"], "transferring")

    def test_stop_of_a_globus_task_nobody_watches_starts_a_stop_worker(self):
        self.change(state="transferring", globus={"task_id": "6f1d2c3b-0000-4000-8000-000000000001"})
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertEqual((answer["stopped_now"], answer["worker"]), (False, "detached"))
        self.assertEqual(self.spawned[-1][2], "stop")

    def test_the_studio_acts_on_demo_uploads_only(self):
        self.ok("arc-status", "--upload=" + self.upload_id)
        self.change(demo=False)
        for command in ("arc-check", "arc-retry", "arc-stop", "arc-keep-copy"):
            answer = self.refused("refused", "not_demo", command, "--upload=" + self.upload_id, "--demo-only")
            self.assertEqual(answer["error"], "The Studio acts on demo uploads only.")

    def test_an_action_is_checked_again_under_the_record_lock(self):
        self.change(state="failed", error={"retryable": True})
        real = arc_records.retry_target
        spawned = list(self.spawned)

        def finished_meanwhile(record, display_state):
            # Another program acts first: the upload is verified by the time this one writes.
            arc_records.write_record(self.folder, dict(arc_records.read_record(self.folder), state="verified"))
            return real(record, display_state)

        with mock.patch.object(arc_records, "retry_target", side_effect=finished_meanwhile):
            self.refused("command", "not_retryable", "arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.record(self.upload_id)["state"], "verified")
        self.assertEqual(self.spawned, spawned)

    def test_a_stop_after_the_worker_ended_is_refused_cleanly(self):
        self.change(state="transferring")
        holder = arc_support.hold_lock(self, self.folder / "worker.lock")
        real = arc_commands._still_allowed

        def ended_meanwhile(folder, record, action, reason):
            holder.kill()
            holder.wait()
            record["state"] = "verified"
            return real(folder, record, action, reason)

        with mock.patch.object(arc_commands, "_still_allowed", side_effect=ended_meanwhile):
            self.refused("command", "nothing_to_stop", "arc-stop", "--upload=" + self.upload_id)
        self.assertEqual(self.record(self.upload_id)["state"], "transferring")

    def test_a_session_folder_that_goes_away_while_listed(self):
        arc_support.ready_session(self.home, folder="1d_x_scan_gone", dark=False)
        original = arc_commands.arc_session.metadata.point_files

        def gone(folder):
            if folder.name == "1d_x_scan_gone":
                raise FileNotFoundError(str(folder))
            return original(folder)

        with mock.patch.object(arc_commands.arc_session.metadata, "point_files", side_effect=gone):
            items = {item["folder"]: item for item in self.ok("arc-sessions")["items"]}
            self.refused("not_found", "unknown_session", "arc-upload", "--session=1d_x_scan_gone", "--demo")
        self.assertEqual(items["1d_x_scan_gone"]["why_not"]["reason"], "unknown_session")
        self.assertTrue(items[SESSION]["ready"])

    def test_status_shows_the_history_and_the_record_folder(self):
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual(shown["record_folder"], str(self.folder))
        self.assertEqual(shown["events"][0]["text"], "Demo: waiting to start.")
        self.assertIsNone(shown["manifest"])
        self.assertEqual(shown["attempts"][0]["attempt"], 1)


class WorkerProgram(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        self.session = arc_support.ready_session(self.home)
        self.upload_id = self.upload()["upload"]["upload_id"]
        self.folder = self.record_dir(self.upload_id)

    def work(self, transport, action="send", **options):
        with mock.patch.object(arc_worker._Run, "entry_point", lambda run: transport), \
                contextlib.redirect_stderr(io.StringIO()):
            return arc_worker.run_worker(self.home, self.upload_id, action, **options)

    def test_wrong_arguments_exit_2(self):
        for arguments in (("--upload", self.upload_id), ("--upload", "nonsense", "--action", "send"),
                          ("--upload", "u000000000000", "--action", "send"),
                          ("--upload", self.upload_id, "--action", "send", "--terminal")):
            code, _stdout, _stderr = arc_support.run_worker_program(self.home, *arguments)
            self.assertEqual(code, arc_worker.EXIT_USAGE, arguments)

    def test_a_second_worker_for_one_upload_exits_3(self):
        arc_support.hold_lock(self, self.folder / "worker.lock")
        started = time.monotonic()
        code, _stdout, _stderr = arc_support.run_worker_program(self.home, "--upload", self.upload_id, "--action",
                                                                "send")
        self.assertEqual(code, arc_worker.EXIT_NOT_ITS_TURN)
        self.assertGreater(time.monotonic() - started, 1.5)
        self.assertEqual(self.record(self.upload_id)["state"], "queued")

    def test_a_state_the_action_does_not_take_exits_3(self):
        self.assertEqual(self.work(arc_support.placing_transport, action="check"), arc_worker.EXIT_NOT_ITS_TURN)
        self.assertEqual(self.work(arc_support.placing_transport, action="stop"), arc_worker.EXIT_NOT_ITS_TURN)
        self.assertEqual(self.work(arc_support.placing_transport), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.work(arc_support.placing_transport), arc_worker.EXIT_NOT_ITS_TURN)

    def test_the_worker_writes_who_works_and_clears_it(self):
        seen = {}

        def look(ctx):
            seen.update(ctx.read()["worker"])
            seen["alive"] = arc_records.worker_alive(ctx.record_dir)
            return arc_support.placing_transport(ctx)

        self.work(look)
        self.assertEqual((seen["pid"], seen["action"], seen["terminal"], seen["alive"]),
                         (os.getpid(), "send", False, True))
        record = self.record(self.upload_id)
        self.assertIsNone(record["worker"])
        self.assertEqual(record["destination"]["placed_as"], SESSION)
        self.assertEqual([event["state"] for event in arc_records.read_events(self.folder)][:3],
                         ["queued", "preparing", "preparing"])

    def test_a_transport_that_breaks_is_recorded_as_internal(self):
        def breaks(ctx):
            raise RuntimeError("Bearer abc: a message that must never be stored")

        self.assertEqual(self.work(breaks), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "internal"))
        self.assertIn("(RuntimeError)", record["error"]["message"])
        self.assertNotIn("Bearer", json.dumps(record))

    def test_an_outcome_the_state_cannot_reach_is_internal(self):
        self.assertEqual(self.work(lambda ctx: arc_worker.Outcome("uncertain")), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "failed")
        self.assertIn("(TransitionError)", record["error"]["message"])

    def test_a_stop_request_reaches_the_worker_within_a_second(self):
        started = threading.Event()
        answers = []

        def waits(ctx):
            ctx.set_state("signing_in", ctx.text("Signing in."))
            started.set()
            while not ctx.wait(0.05):
                pass
            ctx.check_stop()

        def stop():
            started.wait(10)
            answers.append(self.invoke("arc-stop", "--upload=" + self.upload_id))

        thread = threading.Thread(target=stop)
        thread.start()
        begun = time.monotonic()
        code = self.work(waits)
        thread.join(10)
        self.assertLess(time.monotonic() - begun, 5)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        self.assertEqual(answers[0]["stopped_now"], False)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"], record["requests"]["stop_utc"]),
                         ("stopped", "Demo: stopped before anything was sent.", None))

    def live_ssh_upload(self):
        arc_support.write_settings(self.home, LIVE_SSH)
        arc_support.ready_session(self.home, folder="1d_x_scan_real", simulated=False, dark=False)
        with mock.patch.object(arc_commands, "setup_errors", return_value=[]), \
                mock.patch.object(arc_worker, "run_program",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")):
            upload_id = self.ok("arc-upload", "--session=1d_x_scan_real")["upload"]["upload_id"]
        self.upload_id = upload_id
        self.folder = self.record_dir(upload_id)
        return upload_id

    def test_closing_the_terminal_window_is_terminal_closed(self):
        self.live_ssh_upload()

        def hangs_up(ctx):
            ctx.set_state("signing_in")
            ctx.request_stop("SIGHUP")
            return ctx.failed("ssh_interrupted", incoming="x")

        self.assertEqual(self.work(hangs_up, terminal=True, attempt=1), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "terminal_closed", True))

    def test_ctrl_c_in_terminal_is_stopped(self):
        self.live_ssh_upload()

        def interrupted(ctx):
            ctx.set_state("signing_in")
            ctx.request_stop("SIGINT")
            return ctx.failed("ssh_signin_failed")

        self.assertEqual(self.work(interrupted, terminal=True, attempt=1), arc_worker.EXIT_INTERRUPTED)
        self.assertEqual(self.record(self.upload_id)["state"], "stopped")

    def test_a_detached_worker_never_takes_a_live_ssh_upload(self):
        self.live_ssh_upload()
        self.assertEqual(self.work(arc_support.placing_transport), arc_worker.EXIT_NOT_ITS_TURN)

    def test_a_check_that_fails_leaves_a_verified_upload_verified(self):
        self.work(arc_support.placing_transport)
        self.ok("arc-check", "--upload=" + self.upload_id)

        def breaks(ctx):
            raise OSError("listing failed")

        self.assertEqual(self.work(breaks, action="check"), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertEqual(record["checks"][-1]["ok"], False)
        self.assertTrue(record["checks"][-1]["utc"])
        # REG-01: an error of the check could not look: the upload stays on ARC and needs nobody.
        note = record["checks"][-1]
        self.assertEqual((note["looked"], note["fix"]), (False, arc_records.CHECK_FIXES["internal"]))
        self.assertTrue(note["message"].startswith("Demo: something went wrong in the upload program (OSError). The "
                                                   "record and the log are in "), note["message"])
        self.assertTrue(note["message"].endswith(self.upload_id + ". Check status could not look."), note["message"])
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["display_state"], "verified")
        self.assertEqual(self.ok("status")["arc_attention"], 0)

        # A refusal the check meets (here: the demo folder), an outcome that cannot follow "verified": the same.
        def refused(ctx):
            raise arc_records.refusal("demo_folder_unwritable", path="/x",
                                      problem="it is a link; name the folder itself")

        self.assertEqual(self.work(refused, action="check"), arc_worker.EXIT_ENDED)
        note = self.record(self.upload_id)["checks"][-1]
        self.assertEqual((note["ok"], note["looked"], note["fix"]), (False, False, arc_records.CHECK_AGAIN_FIX))
        self.assertTrue(note["message"].startswith("Demo: the demo folder /x cannot be used"), note["message"])
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["display_state"], "verified")

        def still_there(ctx):
            return arc_worker.Outcome("verified", check={"ok": True, "message": "Still on ARC: 18 files."})

        self.assertEqual(self.work(still_there, action="check"), arc_worker.EXIT_VERIFIED)
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual(shown["last_check"]["message"], "Still on ARC: 18 files.")

    def test_a_name_already_on_arc_is_a_conflict_until_kept_as_a_new_copy(self):
        def taken(ctx):
            for state in ("signing_in", "checking", "transferring", "verifying", "placing"):
                ctx.set_state(state)
            return arc_worker.Outcome("conflict", ctx.text("ARC already has a folder named x."),
                                      fields={"conflict_at": "placing"})

        self.assertEqual(self.work(taken), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["conflict_at"], record["error"]), ("conflict", "placing", None))
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["actions"], shown["needs"]), (["keep_copy", "retry", "stop"], "decision"))
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)

        def beside(ctx):
            seen["resume"] = ctx.resume_at
            ctx.set_state("signing_in")
            ctx.set_state("placing")
            ctx.placed_as(ctx.session_folder + "_copy2")
            return arc_worker.Outcome("verified", ctx.text("On ARC."))

        seen = {}
        self.assertEqual(self.work(beside), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual(seen["resume"], "placing")
        self.assertEqual((record["resume"], record["conflict_at"], record["destination"]["placed_as"]),
                         (None, None, SESSION + "_copy2"))
        self.assertTrue(record["destination"]["final_path"].endswith("/" + SESSION + "_copy2"))
        self.assertTrue(record["destination"]["local_final_path"].endswith("/simulated/" + SESSION + "_copy2"))

    def test_not_sure_globus_got_it(self):
        def lost(ctx):
            for state in ("signing_in", "checking", "submitting"):
                ctx.set_state(state)
            ctx.update(lambda record: record["globus"].update(submission_id="0b9e2d4c-0000-4000-8000-0000000000aa"))
            return arc_worker.Outcome("uncertain", ctx.text(arc_records.MESSAGES["uncertain"]))

        self.work(lost)
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["state"], shown["display_label"], shown["actions"]),
                         ("uncertain", "Not sure Globus got it", ["retry", "check", "stop"]))
        self.assertTrue(self.ok("arc-retry", "--upload=" + self.upload_id)["same_submission"])

    def test_a_watch_that_ends_leaves_the_transfer_not_watched(self):
        def watches(ctx):
            for state in ("signing_in", "checking", "submitting", "transferring"):
                ctx.set_state(state)
            task = {"task_id": "6f1d2c3b-0000-4000-8000-0000000000bb", "task": {"status": "ACTIVE"}}
            ctx.update(lambda record: record["globus"].update(task))
            return arc_worker.Outcome("transferring", ctx.text("Globus asks for a new sign-in. Press Check status."))

        self.assertEqual(self.work(watches), arc_worker.EXIT_ENDED)
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["state"], shown["display_state"], shown["actions"], shown["worker"]["alive"]),
                         ("transferring", "not_watched", ["check", "stop"], False))
        self.assertIsNone(self.record(self.upload_id)["worker"])

    def test_the_signals_a_person_gives_and_the_signals_that_end_the_process(self):
        # I8 (COR-01): only Ctrl-C and closing the window of a Terminal worker are a person's stop. SIGTERM
        # (a logout, restart or shutdown), and SIGHUP or SIGINT of a detached worker, end the work at once.
        for terminal, number, person in ((False, signal.SIGTERM, False), (False, signal.SIGHUP, False),
                                         (False, signal.SIGINT, False), (True, signal.SIGTERM, False),
                                         (True, signal.SIGINT, True), (True, signal.SIGHUP, True)):
            run = arc_worker._Run(self.paths, self.folder, "send", terminal, None)
            run.ctx = arc_worker.WorkerContext(self.paths, self.folder, self.record(self.upload_id), "send")
            run.phase = "working"
            if person:
                run._on_signal(number, None)
                self.assertTrue(run.ctx.stop_requested())
                self.assertEqual(run.ctx.signal_name, signal.Signals(number).name)
                continue
            with self.assertRaises(arc_worker.WorkerEnding) as raised:
                run._on_signal(number, None)
            self.assertEqual(raised.exception.signal_name, signal.Signals(number).name)
            self.assertFalse(run.ctx.stop_requested())
            # While the outcome is written, the end is only marked.
            run.phase = "finishing"
            run._on_signal(number, None)
            self.assertEqual(run.ending, signal.Signals(number).name)

    def test_a_worker_ended_by_the_system_writes_no_state_and_cancels_nothing(self):
        def copies(ctx):
            for state in ("signing_in", "checking", "submitting", "transferring"):
                ctx.set_state(state)
            ctx.update(lambda record: record["globus"].update(task_id="6f1d2c3b-0000-4000-8000-0000000000cc"))
            raise arc_worker.WorkerEnding("SIGTERM")

        with contextlib.redirect_stderr(io.StringIO()):
            code = self.work(copies)
        self.assertEqual(code, 128 + signal.SIGTERM)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["worker"], record["error"]), ("transferring", None, None))
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["actions"]), ("not_watched", ["check", "stop"]))
        self.assertIn("ended by the system (SIGTERM)", shown["events"][-1]["text"])

    def test_progress_is_written_at_most_once_a_second(self):
        seen = {}

        def copies(ctx):
            ctx.set_state("signing_in")
            ctx.set_state("transferring")
            seen["written"] = [ctx.progress(files_done=index) for index in range(1, 50)].count(True)
            ctx.progress(files_done=18, force=True)
            seen["done"] = ctx.read()["progress"]["files_done"]
            ctx.set_state("verifying")
            ctx.set_state("placing")
            ctx.placed_as(ctx.session_folder)
            return arc_worker.Outcome("verified", ctx.text("On ARC."))

        self.assertEqual(self.work(copies), arc_worker.EXIT_VERIFIED)
        self.assertEqual((seen["written"], seen["done"]), (1, 18))


class Faults(arc_support.ArcCase):
    """The fault hooks of the steps this part owns (design 13, rows 1 to 4)."""

    def setUp(self):
        super().setUp()
        self.session = arc_support.ready_session(self.home)

    def faulted(self, *arguments, program="cli", fault):
        hooks = {"RAMAN_ARC_TEST_HOOKS": "1", "RAMAN_ARC_FAULT": fault}
        if program == "cli":
            return arc_support.run_cli(self.home, *arguments, extra_environment=hooks)
        return arc_support.run_worker_program(self.home, *arguments, extra_environment=hooks)

    def work(self, upload_id):
        with mock.patch.object(arc_worker._Run, "entry_point", lambda run: arc_support.placing_transport):
            return arc_worker.run_worker(self.home, upload_id, "send")

    def test_row_1_before_the_record_folder_is_renamed(self):
        code, value, _stderr = self.faulted("arc-upload", "--session=" + SESSION, "--demo",
                                            fault="create_before_rename")
        self.assertEqual((code, value), (arc_worker.EXIT_FAULT, None))
        uploads = arc_records.uploads_folder(self.paths)
        self.assertEqual(len(list(uploads.glob(".creating-*"))), 1)
        self.assertEqual(self.ok("arc-list")["total"], 0)
        self.assertEqual(self.ok("arc-sessions")["items"][0]["uploads"], {"live": None, "demo": None})
        upload_id = self.upload()["upload"]["upload_id"]
        self.assertEqual([folder.name for folder, _record, _error in arc_records.list_records(self.paths)],
                         [upload_id])

    def test_row_2_record_written_and_no_worker_started(self):
        code, value, _stderr = self.faulted("arc-upload", "--session=" + SESSION, "--demo",
                                            fault="upload_before_spawn")
        self.assertEqual((code, value), (arc_worker.EXIT_FAULT, None))
        (folder, record, _error), = arc_records.list_records(self.paths)
        self.assertEqual(record["state"], "queued")
        self.assertFalse((folder / "worker.lock").exists())
        arc_records.update_record(folder, lambda current: current.update(state_since_utc="2026-01-01T00:00:00.000Z"))
        item = self.ok("arc-list")["items"][0]
        self.assertEqual((item["display_state"], item["actions"]), ("not_started", ["retry", "stop"]))
        self.ok("arc-retry", "--upload=" + record["upload_id"])
        self.assertEqual(self.spawned, [(str(self.home), record["upload_id"], "send")])
        self.assertEqual(self.work(record["upload_id"]), arc_worker.EXIT_VERIFIED)

    def test_row_3_halfway_through_the_copy(self):
        upload_id = self.upload()["upload"]["upload_id"]
        folder = self.record_dir(upload_id)
        code, _stdout, _stderr = self.faulted("--upload", upload_id, "--action", "send", program="worker",
                                              fault="snapshot_mid_copy")
        self.assertEqual(code, arc_worker.EXIT_FAULT)
        self.assertEqual(len(list(folder.glob("payload.tmp-*"))), 1)
        self.assertFalse((folder / "payload").exists())
        item = self.ok("arc-status", "--upload=" + upload_id)["upload"]
        self.assertEqual((item["state"], item["display_state"]), ("preparing", "interrupted"))
        retried = self.ok("arc-retry", "--upload=" + upload_id)
        self.assertNotIn("same_submission", retried)
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        self.assertFalse(list(folder.glob("payload.tmp-*")))
        self.assertFalse((folder / "payload").exists())
        self.assertEqual(self.record(upload_id)["state"], "verified")

    def test_row_4_snapshot_made_and_the_record_not_written(self):
        upload_id = self.upload()["upload"]["upload_id"]
        folder = self.record_dir(upload_id)
        code, _stdout, _stderr = self.faulted("--upload", upload_id, "--action", "send", program="worker",
                                              fault="snapshot_before_record")
        self.assertEqual(code, arc_worker.EXIT_FAULT)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["payload"]), ("preparing", None))
        top = folder / "payload" / SESSION
        inode = (top / "point_0001.json").stat().st_ino
        manifest = (top / "raman_upload_manifest.json").read_bytes()
        self.ok("arc-retry", "--upload=" + upload_id)
        seen = {}

        def look(ctx):
            seen["inode"] = (ctx.payload_dir / "point_0001.json").stat().st_ino
            seen["manifest"] = (ctx.payload_dir / "raman_upload_manifest.json").read_bytes()
            return arc_support.placing_transport(ctx)

        with mock.patch.object(arc_worker._Run, "entry_point", lambda run: look):
            self.assertEqual(arc_worker.run_worker(self.home, upload_id, "send"), arc_worker.EXIT_VERIFIED)
        self.assertEqual((seen["inode"], seen["manifest"]), (inode, manifest))

    def test_no_fault_without_the_hooks_variable(self):
        upload_id = self.upload()["upload"]["upload_id"]
        code, _stdout, _stderr = arc_support.run_worker_program(
            self.home, "--upload", upload_id, "--action", "send",
            extra_environment={"RAMAN_ARC_FAULT": "snapshot_mid_copy"})
        self.assertNotEqual(code, arc_worker.EXIT_FAULT)
        self.assertTrue(arc_support.wait_for_workers(self.home))


class Logging(arc_support.ArcCase):
    def test_the_log_drops_secrets_and_exception_texts(self):
        folder = self.home / "record"
        folder.mkdir()
        root = logging.getLogger()
        saved = (list(root.handlers), root.level, sys.excepthook, threading.excepthook)

        def restore():
            for handler in list(root.handlers):
                root.removeHandler(handler)
                handler.close()
            for handler in saved[0]:
                root.addHandler(handler)
            root.setLevel(saved[1])
            sys.excepthook, threading.excepthook = saved[2], saved[3]

        self.addCleanup(restore)
        arc_worker._setup_logging(folder)
        log = logging.getLogger("raman_catalog.arc_globus")
        log.info("kept line")
        for secret in ("GET /?code=abc&state=xyz", "access_token=1", "refresh_token", "id_token", "Authorization: x",
                       "Bearer 123", "invalid token x", "{'token': 'x'}", "code_verifier:x", "Token expired",
                       "the password was x", "a client_secret"):
            log.info("dropped %s", secret)
        # SEC-08: a token or a code without any marker word is hidden, the line kept.
        token = "Ag" + "x7Kq2LmP9vRt4WzN8bYc3DfH6jSa1Ue5Go0Ii"
        log.info("exchanging %s for the transfer", token)
        logging.getLogger("globus_sdk.services.auth").warning("OAuthTokenResponse(%r)", {"data": token})
        log.info("task 6f1d2c3b-0000-4000-8000-0000000000dd of 1d_x_scan_20261113_101500_123 in revoke_tokens")
        try:
            raise ValueError("code=secret-text in a library message")
        except ValueError:
            log.exception("a failure")
        try:
            raise KeyError("another text")
        except KeyError as error:
            sys.excepthook(type(error), error, error.__traceback__)
        for handler in root.handlers:
            handler.flush()
        text = (folder / "worker.log").read_text(encoding="utf-8")
        self.assertIn("kept line", text)
        self.assertNotIn("dropped", text)
        self.assertNotIn(token, text)
        self.assertIn("exchanging [hidden] for the transfer", text)
        self.assertIn("task 6f1d2c3b-0000-4000-8000-0000000000dd of 1d_x_scan_20261113_101500_123 in revoke_tokens",
                      text)
        self.assertNotIn("secret-text", text)
        self.assertNotIn("another text", text)
        self.assertIn("ValueError at test_arc_cli.py:", text)
        self.assertIn("uncaught KeyError at test_arc_cli.py:", text)
        self.assertEqual(stat.S_IMODE((folder / "worker.log").stat().st_mode), 0o600)


class BetweenTheParts(arc_support.ArcCase):
    """What the commands and the worker do where the data layer's parts meet (step 3 of the build)."""

    def setUp(self):
        super().setUp()
        self.session = arc_support.ready_session(self.home)

    def test_a_name_or_id_with_a_newline_at_its_end_is_refused(self):
        # In Python "$" also matches before a final newline: every name and id rule is a fullmatch.
        odd = SESSION + "\n"
        (self.home / "scan_sessions" / odd).mkdir()
        items = {item["folder"]: item for item in self.ok("arc-sessions")["items"]}
        self.assertEqual(items[odd]["why_not"]["reason"], "odd_name")
        answer = self.refused("refused", "odd_name", "arc-upload", "--session=" + odd, "--demo")
        self.assertIn(repr(odd), answer["error"])
        upload_id = self.upload()["upload"]["upload_id"]
        self.refused("not_found", "unknown_upload", "arc-status", "--upload=" + upload_id + "\n")
        os.mkdir(arc_records.record_folder(self.paths, "u0123456789ab\n"))
        self.assertEqual([item["upload_id"] for item in self.ok("arc-list")["items"]], [upload_id])
        with self.assertRaises(ValueError):
            arc_worker.terminal_script_text(self.home, upload_id, 1, "send", odd)
        with self.assertRaises(ValueError):
            arc_worker.terminal_script_text(self.home, upload_id + "\n", 1, "send", SESSION)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arc_worker.run_worker(self.home, upload_id + "\n", "send"), arc_worker.EXIT_USAGE)

    def test_a_known_hosts_path_that_ssh_reads_differently_is_refused_at_once(self):
        folder = self.home / "team keys"
        folder.mkdir()
        spaced = folder / "known_hosts"
        spaced.write_text("datatransfer.arc.vt.edu ssh-ed25519 AAAA\n", encoding="utf-8")
        os.chmod(spaced, 0o600)
        problem = arc_settings.known_hosts_problem(str(spaced))
        self.assertIn("ssh reads differently", problem)
        arc_support.write_settings(self.home, {"demo": False, "transport": "ssh",
                                               "ssh": {"username": arc_support.TEST_USERNAME,
                                                       "known_hosts": str(spaced)}})
        self.ok("arc-list")
        found = {check["name"]: check for check in self.ok("arc-doctor")["checks"]}
        self.assertFalse(found["known_hosts_file"]["ok"])
        self.assertFalse(found["settings_valid"]["ok"])
        with mock.patch.object(sys, "platform", "darwin"):
            errors = arc_commands.setup_errors("ssh", arc_settings.effective(arc_commands.read_settings(
                types.SimpleNamespace(paths=self.paths, home=self.home))))
        self.assertIn("known_hosts_unreadable", [error.reason for error in errors])

    def test_a_demo_upload_needs_a_demo_folder_it_can_write(self):
        blocker = self.home / "not_a_folder"
        blocker.write_text("a file\n", encoding="utf-8")
        arc_support.write_settings(self.home, {"demo_folder": "not_a_folder/arc_demo"})
        answer = self.refused("config", "demo_folder_unwritable", "arc-upload", "--session=" + SESSION, "--demo")
        self.assertIn("not_a_folder/arc_demo", answer["error"])
        self.refused("config", "demo_folder_unwritable", "arc-upload", "--session=" + SESSION, "--demo", "--dry-run")
        self.assertEqual(self.ok("arc-list")["items"], [])
        self.assertEqual(self.spawned, [])

    def test_the_worker_decides_whether_it_is_its_turn_under_the_record_lock(self):
        upload_id = self.upload()["upload"]["upload_id"]
        held = []
        real = arc_worker._not_its_turn

        def looks(record, action, terminal, attempt):
            folder = self.record_dir(record["upload_id"])
            handle = os.open(str(folder / arc_records.RECORD_LOCK), os.O_RDWR)
            try:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    held.append(True)
                else:
                    held.append(False)
            finally:
                os.close(handle)
            return real(record, action, terminal, attempt)

        with mock.patch.object(arc_worker, "_not_its_turn", side_effect=looks), \
                mock.patch.object(arc_worker._Run, "entry_point", lambda run: arc_support.placing_transport), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arc_worker.run_worker(self.home, upload_id, "send"), arc_worker.EXIT_VERIFIED)
            # An upload that a command stopped before the worker looked: it leaves the record alone.
            second = self.upload("--new")["upload"]["upload_id"]
            self.ok("arc-stop", "--upload=" + second)
            before = self.record(second)
            self.assertEqual(arc_worker.run_worker(self.home, second, "send"), arc_worker.EXIT_NOT_ITS_TURN)
        self.assertEqual(held, [True, True])
        self.assertEqual(self.record(second), before)

    def test_stop_of_a_globus_upload_whose_transfer_ended_needs_no_worker(self):
        upload_id = self.upload()["upload"]["upload_id"]
        folder = self.record_dir(upload_id)
        incoming = arc_records.local_path(self.record(upload_id)["destination"]["demo_root"],
                                          self.record(upload_id)["destination"]["incoming_path"])
        task_id = "6f1d2c3b-0000-4000-8000-000000000001"
        cases = (
            ({"state": "verifying", "globus": {"task_id": task_id, "task": {"status": "SUCCEEDED"}}},
             arc_records.message("stopped_complete", True, incoming=incoming)),
            ({"state": "placing",
              "globus": {"task_id": task_id, "task": {"status": "SUCCEEDED"}, "rename": {"to": "/x"}}},
             arc_records.message("stopped_placing", True)),
            ({"state": "transferring", "globus": {"task_id": task_id, "task": {"status": "FAILED"}}},
             arc_records.message("stopped", True, incoming=incoming)),
            ({"state": "conflict", "conflict_at": "placing",
              "globus": {"task_id": task_id, "task": {"status": "SUCCEEDED"}, "rename": {"to": "/x"}}},
             arc_records.message("stopped", True, incoming=incoming)),
            ({"state": "conflict", "conflict_at": "checking", "globus": {"task_id": None, "task": None}},
             arc_records.message("stopped_nothing_sent", True)),
        )
        for values, text in cases:
            def mutate(current, values=values):
                for key, value in values.items():
                    if isinstance(value, dict) and isinstance(current.get(key), dict):
                        current[key].update(value)
                    else:
                        current[key] = value

            arc_records.update_record(folder, mutate)
            answer = self.ok("arc-stop", "--upload=" + upload_id)
            self.assertEqual((answer["stopped_now"], answer["worker"], answer["message"]), (True, None, text), values)
            self.assertEqual(self.record(upload_id)["state"], "stopped")
        self.assertEqual(self.spawned[1:], [])


class ChecksAndStops(arc_support.ArcCase):
    """Check status, Stop upload and the worker's end, as the review asked (I1, I3, I9, I10, COR-03, COR-04)."""

    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home)
        self.upload_id = self.upload()["upload"]["upload_id"]
        self.folder = self.record_dir(self.upload_id)

    def work(self, transport, action="send", **options):
        output = io.StringIO()
        with mock.patch.object(arc_worker._Run, "entry_point", lambda run: transport), \
                contextlib.redirect_stderr(output):
            code = arc_worker.run_worker(self.home, self.upload_id, action, **options)
        return code, output.getvalue()

    def verified(self):
        self.assertEqual(self.work(arc_support.placing_transport)[0], arc_worker.EXIT_VERIFIED)

    def test_a_check_worker_can_be_stopped_and_holds_back_no_other_upload(self):
        # SEC-01: Check status of a verified upload: Stop upload is offered and asks the check to end.
        self.verified()
        arc_records.update_record(self.folder, lambda current: current.update(worker={
            "pid": 1, "action": "check", "started_utc": "2026-11-15T16:00:00.000Z", "terminal": False}))
        arc_support.hold_lock(self, self.folder / "worker.lock")
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["actions"]), ("verified", ["stop", "show_in_finder"]))
        self.refused("conflict", "busy", "arc-check", "--upload=" + self.upload_id)
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertEqual((answer["stopped_now"], answer["worker"]), (False, None))
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], bool(record["requests"]["stop_utc"])), ("verified", True))
        # The check of a live upload never holds back a live upload of another session.
        arc_records.update_record(self.folder, lambda current: (
            current.update(demo=False), current["destination"].update(key="live:%s|real" % arc_support.ARC_FOLDER)))
        self.assertIsNone(arc_commands._live_worker_elsewhere(self.paths, "another-session", "live:x|real"))
        arc_records.update_record(self.folder, lambda current: current["worker"].update(action="send"))
        self.assertEqual(arc_commands._live_worker_elsewhere(self.paths, "another-session", "live:x|real"), SESSION)

    def test_the_listing_knows_what_a_worker_does_from_the_moment_it_takes_the_upload(self):
        # A command writes the worker it starts (its action) before it starts it: a worker that has taken
        # worker.lock and not yet written its pid is already listed as doing that.
        self.verified()
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.record(self.upload_id)["worker"],
                         {"pid": None, "action": "check", "started_utc": None, "terminal": False})
        arc_support.hold_lock(self, self.folder / "worker.lock")
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["worker"], shown["actions"]),
                         ({"alive": True, "action": "check", "terminal": False}, ["stop", "show_in_finder"]))

    def test_a_check_in_a_terminal_window_says_so_when_it_is_busy(self):
        # SSH-V03: the upload is busy in a Terminal window: the refusal says where.
        arc_records.update_record(self.folder, lambda current: current.update(state="failed", error={
            "retryable": True}, worker={"pid": 1, "action": "check", "terminal": True}))
        arc_support.hold_lock(self, self.folder / "worker.lock")
        answer = self.refused("conflict", "busy", "arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(answer["fix"], "Finish or close that Terminal window, or press Stop upload.")
        self.assertEqual(answer["upload"]["actions"], ["stop"])

    def test_a_stop_while_a_worker_starts_is_not_lost(self):
        # COR-04: arc-stop of an upload nobody works on starts a stop worker and writes the request too, so
        # that a send or check worker that takes the upload first stops within a second.
        task = {"task_id": "6f1d2c3b-0000-4000-8000-000000000001", "task": {"status": "ACTIVE"}}
        arc_records.update_record(self.folder, lambda current: (current.update(state="transferring"),
                                                                current["globus"].update(task)))
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertEqual((answer["stopped_now"], answer["worker"]), (False, "detached"))
        self.assertEqual(self.spawned[-1][2], "stop")
        self.assertTrue(self.record(self.upload_id)["requests"]["stop_utc"])
        # A stop worker that finds the upload taken writes the request before it ends.
        arc_records.update_record(self.folder, lambda current: current.update(requests={"stop_utc": None}))
        arc_support.hold_lock(self, self.folder / "worker.lock")
        code, _stdout, _stderr = arc_support.run_worker_program(self.home, "--upload", self.upload_id, "--action",
                                                                "stop")
        self.assertEqual(code, arc_worker.EXIT_NOT_ITS_TURN)
        self.assertTrue(self.record(self.upload_id)["requests"]["stop_utc"])

    def test_a_stop_worker_does_not_stop_on_its_own_request(self):
        run = arc_worker._Run(self.paths, self.folder, "stop", False, None)
        run.ctx = arc_worker.WorkerContext(self.paths, self.folder, self.record(self.upload_id), "stop")
        arc_records.update_record(self.folder, lambda current: current.update(requests={"stop_utc": "now"}))
        with mock.patch.object(arc_worker, "STOP_POLL_SECONDS", 0.01):
            run.start_poller()
            run.poller.join(5)
        self.assertFalse(run.ctx.stop_requested())

    def live_ssh(self, state="verified"):
        """This upload as a live SSH upload of a Terminal window, in `state`."""
        def make(current):
            current.update(state=state, transport="ssh", demo=False, globus=None,
                           ssh={"attempt": 2, "sent": True, "runs": []})
            current["destination"].update(demo_root=None, local_final_path=None)

        arc_records.update_record(self.folder, make)

    def test_check_status_in_terminal_prints_what_it_found(self):
        # SSH-01: the window ends with the check's own result, not the message of the verified upload.
        self.verified()
        self.live_ssh()

        def gone(ctx):
            return arc_worker.Outcome("verified", None,
                                      check={"ok": False, "message": "Nothing of this upload is on ARC."})

        code, output = self.work(gone, "check", terminal=True, attempt=2)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        lines = [line for line in output.splitlines() if line]
        self.assertEqual(lines[-2:], ["Nothing of this upload is on ARC.", arc_records.CHECK_FAILED_TERMINAL])
        self.assertNotIn("On ARC.", output)
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["message"]),
                         ("check_failed", "Nothing of this upload is on ARC."))

        def still(ctx):
            return arc_worker.Outcome("verified", None, check={"ok": True, "message": "Still on ARC: 18 files."})

        code, output = self.work(still, "check", terminal=True, attempt=2)
        self.assertEqual(code, arc_worker.EXIT_VERIFIED)
        self.assertEqual([line for line in output.splitlines() if line][-1], "Still on ARC: 18 files.")
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["display_state"], "verified")

        # REG-01: a check that could not look ends with why and what to do, not with "last check failed".
        def could_not_look(ctx):
            return arc_worker.Outcome("verified", None, check=arc_records.check_note(
                "Signing in to ARC did not work (ssh ended with code 255).", "ssh_signin_failed"))

        code, output = self.work(could_not_look, "check", terminal=True, attempt=2)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        lines = [line for line in output.splitlines() if line]
        self.assertEqual(lines[-2:], ["Signing in to ARC did not work (ssh ended with code 255). Check status could "
                                      "not look.", arc_records.CHECK_FIXES["ssh_signin_failed"]])
        self.assertNotIn(arc_records.CHECK_FAILED_TERMINAL, output)
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["display_state"], "verified")

    def test_closing_the_window_of_a_check_changes_nothing(self):
        # SSH-03: a check whose Terminal window was closed leaves the upload as it was.
        for state, error in (("stopped", None), ("failed", {"kind": "upload", "reason": "ssh_interrupted",
                                                            "message": "m", "fix": "f", "retryable": True})):
            arc_records.update_record(self.folder, lambda current: current.update(
                state=state, message="Stopped. Files already copied stay in x.", error=error))
            self.live_ssh(state)

            def hangs_up(ctx):
                ctx.request_stop("SIGHUP")
                raise arc_worker.StopRequested()

            code, output = self.work(hangs_up, "check", terminal=True, attempt=2)
            self.assertEqual(code, arc_worker.EXIT_ENDED)
            record = self.record(self.upload_id)
            self.assertEqual((record["state"], record["error"], record["message"]),
                             (state, error, "Stopped. Files already copied stay in x."))
            self.assertIn(arc_records.CHECK_STOPPED_TERMINAL, output)
            self.assertIn("The check was not finished: its Terminal window was closed.",
                          [event["text"] for event in arc_records.read_events(self.folder)])

    def test_the_end_of_a_terminal_window_drops_what_was_typed_meanwhile(self):
        # SSH-07 and I9: before its last lines, a Terminal worker discards the Returns typed while it ran.
        import termios

        self.verified()
        self.live_ssh()
        order = []

        def still(ctx):
            return arc_worker.Outcome("verified", None, check={"ok": True, "message": "Still on ARC."})

        with mock.patch.object(termios, "tcflush", side_effect=lambda fd, queue: order.append(("flush", fd, queue))), \
                mock.patch.object(arc_worker, "_say", side_effect=lambda terminal, text: order.append(("say", text))):
            self.work(still, "check", terminal=True, attempt=2)
        self.assertEqual(order, [("flush", 0, termios.TCIFLUSH), ("say", ""), ("say", "Still on ARC.")])

    def test_the_code_of_a_run_is_loaded_before_its_first_step(self):
        # I10 (DOC-07): every module a worker may need, and arc_remote.sh, before "preparing".
        from raman_catalog import arc_ssh

        order = []
        real_prepare = arc_worker._Run.prepare

        def prepare(run):
            order.append(("prepare", run.ctx.remote_script))
            return real_prepare(run)

        with mock.patch.object(arc_ssh, "remote_script", side_effect=lambda: order.append("read") or b"script"), \
                mock.patch.object(arc_worker._Run, "prepare", prepare):
            self.verified()
        self.assertEqual(order[:2], ["read", ("prepare", b"script")])
        done = subprocess.run([sys.executable, "-c", LOADED, str(support.DATA_LAYER)], capture_output=True, text=True,
                              timeout=60, env=dict(os.environ, PYTHONPATH=str(support.DATA_LAYER)))
        self.assertEqual(done.returncode, 0, done.stderr)
        before, after, script, sdk = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual((before, after, sdk), ([], sorted(arc_worker.TRANSPORT_MODULES), []))
        self.assertTrue(script)

    def test_verified_is_written_before_the_snapshot_is_deleted(self):
        # COR-03: a crash between them leaves a verified upload; a later worker of it deletes the snapshot.
        class PowerCut(BaseException):
            pass

        def fault(name):
            if name == "verified_before_delete":
                raise PowerCut()

        with mock.patch.object(arc_worker, "fault", fault), self.assertRaises(PowerCut):
            self.work(arc_support.placing_transport)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["payload"]["kept"]), ("verified", True))
        self.assertTrue((self.folder / "payload").is_dir())
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["actions"]), ("verified", ["check", "show_in_finder"]))

        def still(ctx):
            return arc_worker.Outcome("verified", None, check={"ok": True, "message": "Still there."})

        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work(still, "check")[0], arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual((record["payload"]["kept"], (self.folder / "payload").exists()), (False, False))

    def test_a_crash_after_the_snapshot_was_deleted_leaves_no_retry_that_fails(self):
        # COR-03: before the fix, the record stayed in a working state with Retry offered and always refused.
        with mock.patch.object(arc_payload, "after_check", side_effect=RuntimeError("power cut")):
            self.assertEqual(self.work(arc_support.placing_transport)[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertFalse((self.folder / "payload").exists())
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["actions"]), ("verified", ["check", "show_in_finder"]))

    def test_a_record_folder_that_is_a_link_is_never_worked_on(self):
        # SEC-06: as the commands, the worker never acts through a linked record folder.
        moved = self.folder.with_name("u0000000000ff")
        os.rename(self.folder, moved)
        os.symlink(moved, self.folder)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arc_worker.run_worker(self.home, self.upload_id, "send"), arc_worker.EXIT_USAGE)
        self.assertEqual(arc_records.read_record(moved)["state"], "queued")


# A worker process that imports only arc_worker: which modules it loaded before and after its first step.
LOADED = textwrap.dedent('''
    import json, sys
    from raman_catalog import arc_worker
    names = sorted(arc_worker.TRANSPORT_MODULES)
    before = [name for name in names if name in sys.modules]
    script = arc_worker.load_code()
    after = [name for name in names if name in sys.modules]
    sdk = [name for name in sys.modules if name.split(".")[0] == "globus_sdk"]
    print(json.dumps([before, after, bool(script), sdk]))
''')


if __name__ == "__main__":
    unittest.main()
