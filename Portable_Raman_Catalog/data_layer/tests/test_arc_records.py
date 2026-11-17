"""Records of uploads: states and transitions, atomic creation, locks, what the listing shows, actions."""

import copy
import datetime
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import arc_support
import support
from raman_catalog import arc_records, arc_worker, commands_core
from raman_catalog.arc_records import TRANSITIONS, TransitionError, set_state

SESSION = arc_support.SESSION
NOW = datetime.datetime(2026, 11, 15, 16, 0, 0, tzinfo=datetime.timezone.utc)


def utc(seconds_before):
    return arc_records.utc_now() if seconds_before is None else (
        (NOW - datetime.timedelta(seconds=seconds_before)).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z")


def record(state, transport="globus", demo=False, age=10, **changes):
    """A record as the listing functions see it (only the keys they read)."""
    value = {
        "schema": arc_records.SCHEMA, "upload_id": "u0123456789ab", "created_utc": utc(age),
        "updated_utc": utc(age), "transport": transport, "demo": demo, "state": state,
        "state_since_utc": utc(age), "message": "stored message", "error": None, "warnings": [], "notes": [],
        "resume": None, "conflict_at": None,
        "session": {"folder": SESSION, "session_id": "s-1", "kind": "real", "sample_id": "S-001"},
        "destination": {"key": "live:%s|real" % arc_support.ARC_FOLDER, "area": "real",
                        "final_path": arc_support.ARC_FOLDER + "/" + SESSION,
                        "incoming_path": arc_support.ARC_FOLDER + "/_incoming/%s.u0123456789ab" % SESSION,
                        "demo_root": "/tmp/demo" if demo else None, "placed_as": None},
        "settings_used": {"ssh_username": arc_support.TEST_USERNAME, "ssh_host": "datatransfer.arc.vt.edu"},
        "payload": None, "progress": None, "requests": {"stop_utc": None}, "worker": None,
        "globus": {"submission_id": None, "task_id": None, "task": None, "earlier_attempts": []}
        if transport == "globus" else None,
        "ssh": {"attempt": 1, "sent": False, "runs": []} if transport == "ssh" else None,
        "verification": None, "checks": [],
    }
    for key, change in changes.items():
        if isinstance(change, dict) and isinstance(value.get(key), dict):
            value[key].update(change)
        else:
            value[key] = change
    return value


class Transitions(unittest.TestCase):
    def test_every_allowed_change_is_accepted(self):
        for old, new in TRANSITIONS:
            if old is None:
                continue
            changed = set_state({"state": old}, new, "text")
            self.assertEqual((changed["state"], changed["message"]), (new, "text"))

    def test_every_other_change_raises(self):
        for old in arc_records.STATES:
            for new in arc_records.STATES:
                if old == new or (old, new) in TRANSITIONS:
                    continue
                with self.assertRaises(TransitionError, msg=(old, new)):
                    set_state({"state": old}, new)

    def test_the_paths_of_both_transports_and_the_named_changes(self):
        for path in (arc_records.GLOBUS_PATH, arc_records.SSH_PATH,
                     tuple(state for state in arc_records.SSH_PATH if state != "waiting_for_terminal")):
            for old, new in zip(path, path[1:]):
                self.assertIn((old, new), TRANSITIONS)
        for pair in [(None, "queued"), ("queued", "waiting_for_terminal"), ("checking", "conflict"),
                     ("placing", "conflict"), ("submitting", "uncertain"), ("uncertain", "transferring"),
                     ("verifying", "verified"), ("conflict", "stopped"), ("uncertain", "stopped"),
                     ("failed", "queued"), ("stopped", "waiting_for_terminal"), ("verified", "verified"),
                     ("signing_in", "placing"), ("signing_in", "verified"), ("failed", "transferring")]:
            self.assertIn(pair, TRANSITIONS)
        for state in arc_records.WORKING_STATES:
            for end in ("failed", "stopped", "queued", "waiting_for_terminal"):
                self.assertIn((state, end), TRANSITIONS)
        for pair in [("queued", "transferring"), ("preparing", "checking"), ("verified", "failed"),
                     ("verified", "queued"), ("conflict", "verified"), ("checking", "preparing"),
                     ("transferring", "submitting"), ("stopped", "conflict"), (None, "preparing")]:
            self.assertNotIn(pair, TRANSITIONS)

    def test_the_same_state_is_an_update_and_resume_is_cleared_when_reached(self):
        value = set_state({"state": "failed", "resume": {"at": "transferring"}}, "failed", "again")
        self.assertEqual((value["state"], value["message"]), ("failed", "again"))
        value = set_state({"state": "signing_in", "resume": {"at": "transferring"}}, "checking")
        self.assertEqual(value["resume"], {"at": "transferring"})
        value = set_state(value, "transferring")
        self.assertIsNone(value["resume"])

    def test_labels(self):
        self.assertEqual(set(arc_records.STATE_LABELS), set(arc_records.STATES))
        self.assertEqual(len(arc_records.STATES), 14)
        self.assertEqual(arc_records.state_label("transferring", "ssh"), "Copying over SSH")
        self.assertEqual(arc_records.state_label("transferring", "globus"), "Copying with Globus")
        self.assertEqual(arc_records.STATE_LABELS["verified"], "On ARC, checked")


class Files(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home)

    def test_a_record_folder_appears_in_one_step(self):
        answer = self.upload()
        folder = self.record_dir(answer["upload"]["upload_id"])
        uploads = folder.parent
        self.assertEqual(stat.S_IMODE(uploads.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for name in ("record.json", "events.jsonl"):
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE((uploads / "requests.lock").stat().st_mode), 0o600)
        # A folder still being made is ignored by every reader.
        creating = uploads / ".creating-abcdef"
        creating.mkdir()
        (creating / "record.json").write_text(json.dumps(record("queued")), encoding="utf-8")
        self.assertEqual([entry[0].name for entry in arc_records.list_records(self.paths)], [folder.name])
        self.assertEqual(self.ok("arc-list")["total"], 1)
        self.assertEqual(arc_records.count_unfinished(self.paths), 1)

    def test_records_are_written_with_fsync_of_the_file_and_the_folder(self):
        fsynced_folders = []
        real_fsync_folder = arc_records.fsync_folder
        with mock.patch.object(arc_records, "fsync_folder",
                               side_effect=lambda folder: (fsynced_folders.append(Path(folder)),
                                                           real_fsync_folder(folder))), \
                mock.patch("os.fsync", wraps=os.fsync) as fsync:
            answer = self.upload()
            created_fsyncs = fsync.call_count
            folder = self.record_dir(answer["upload"]["upload_id"])
            self.assertIn(folder.parent, fsynced_folders)
            self.assertTrue(any(path.name.startswith(".creating-") for path in fsynced_folders))
            self.assertGreaterEqual(created_fsyncs, 1)

    def test_a_record_is_written_to_the_drive_itself_file_and_folder(self):
        # COR-V04: on macOS fsync only hands the data to the drive; record.json (it holds the request
        # number before every POST) is written with F_FULLFSYNC, the file and then its folder.
        folder = self.record_dir(self.upload()["upload"]["upload_id"])
        synced = []
        real = fcntl.fcntl

        def spy(handle, command, *arguments):
            if command == fcntl.F_FULLFSYNC:
                synced.append(real(handle, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode())
            return real(handle, command, *arguments)

        with mock.patch.object(arc_records.fcntl, "fcntl", spy):
            arc_records.update_record(folder, lambda current: current.update(message="x"))
        real_folder = os.path.realpath(folder)
        self.assertEqual(len(synced), 2, synced)
        self.assertEqual(os.path.dirname(synced[0]), real_folder)
        self.assertTrue(os.path.basename(synced[0]).startswith(".record.json."), synced)
        self.assertEqual(synced[1], real_folder)
        self.assertEqual(self.record(folder.name)["message"], "x")
        self.assertEqual(stat.S_IMODE((folder / "record.json").stat().st_mode), 0o600)
        self.assertEqual([path.name for path in folder.iterdir() if path.name.startswith(".")], [])
        # Where the drive refuses F_FULLFSYNC, os.fsync is used.
        refused = []

        def refuse(handle, command, *arguments):
            if command == fcntl.F_FULLFSYNC:
                refused.append(handle)
                raise OSError(45, "Operation not supported")
            return real(handle, command, *arguments)

        with mock.patch.object(arc_records.fcntl, "fcntl", refuse), mock.patch("os.fsync", wraps=os.fsync) as fsync:
            arc_records.update_record(folder, lambda current: current.update(message="y"))
        self.assertEqual((len(refused), fsync.call_count), (2, 2))
        self.assertEqual(self.record(folder.name)["message"], "y")

    def test_events_are_appended_and_a_line_being_written_is_left_out(self):
        folder = self.record_dir(self.upload()["upload"]["upload_id"])
        arc_records.append_event(folder, "queued", "second")
        with open(folder / "events.jsonl", "a", encoding="utf-8") as stream:
            stream.write('{"utc": "half')
        events = arc_records.read_events(folder)
        self.assertEqual([event["text"] for event in events], ["Demo: waiting to start.", "second"])
        self.assertEqual(stat.S_IMODE((folder / "events.jsonl").stat().st_mode), 0o600)

    def test_an_unreadable_record_and_a_newer_one_are_listed_and_never_acted_on(self):
        uploads = arc_records.ensure_uploads_folder(self.paths)
        broken = uploads / "u00000000000a"
        broken.mkdir()
        (broken / "record.json").write_text("{not json", encoding="utf-8")
        newer = uploads / "u00000000000b"
        newer.mkdir()
        content = record("failed", error={"retryable": True}, upload_id="u00000000000b")
        content["schema"] = "raman-arc-upload/2"
        (newer / "record.json").write_text(json.dumps(content), encoding="utf-8")
        items = {item["upload_id"]: item for item in self.ok("arc-list")["items"]}
        self.assertEqual((items["u00000000000a"]["state"], items["u00000000000a"]["actions"]), ("unreadable", []))
        self.assertEqual(items["u00000000000b"]["actions"], [])
        self.assertIn("newer version", items["u00000000000b"]["notes"][-1])
        self.refused("command", "not_retryable", "arc-retry", "--upload=u00000000000a")
        self.refused("command", "not_retryable", "arc-retry", "--upload=u00000000000b")
        self.refused("command", "nothing_to_stop", "arc-stop", "--upload=u00000000000b")
        shown = self.ok("arc-status", "--upload=u00000000000a")["upload"]
        self.assertEqual(shown["state"], "unreadable")
        self.assertEqual(arc_records.count_unfinished(self.paths), 0)

    def test_status_counts_unfinished_uploads(self):
        uploads = arc_records.ensure_uploads_folder(self.paths)
        states = ["queued", "transferring", "uncertain", "conflict", "verified", "failed", "stopped"]
        for index, state in enumerate(states):
            folder = uploads / ("u%012x" % index)
            folder.mkdir()
            (folder / "record.json").write_text(json.dumps(record(state)), encoding="utf-8")
        status = self.ok("status")
        self.assertEqual(status["arc_unfinished"], 4)
        with mock.patch.object(commands_core, "_optional", return_value=None):
            self.assertEqual(self.ok("status")["arc_unfinished"], 0)
        with mock.patch("raman_catalog.arc_records.count_unfinished", side_effect=OSError("disk")):
            self.assertEqual(self.ok("status")["arc_unfinished"], 0)

    def test_status_without_any_upload(self):
        self.assertEqual(self.ok("status")["arc_unfinished"], 0)


class Display(arc_support.ArcCase):
    """What the listing shows while nobody works on an upload (design 7.5)."""

    def show(self, value, alive=False):
        return arc_records.display(value, alive, now=NOW)

    def test_a_worker_alive_shows_the_stored_state(self):
        for state in ("transferring", "queued", "signing_in"):
            self.assertEqual(self.show(record(state, age=1000), alive=True)[:2],
                             (state, arc_records.STATE_LABELS[state]))

    def test_a_globus_transfer_nobody_watches(self):
        for state in ("transferring", "verifying", "placing"):
            value = record(state, globus={"task_id": "t-1"})
            shown = self.show(value)
            self.assertEqual(shown[:2], ("not_watched", "Not being watched"))
            self.assertEqual(shown[2], arc_records.MESSAGES["not_watched"])
            self.assertEqual(arc_records.actions(value, shown[0], False), ["check", "stop"])
        self.assertEqual(self.show(record("transferring", demo=True, globus={"task_id": "t-1"}))[2],
                         "Demo: Globus keeps copying, but this Mac stopped watching. Press Check status to look.")

    def test_a_worker_that_never_started(self):
        shown = self.show(record("waiting_for_terminal", transport="ssh", age=121))
        self.assertEqual(shown[:2], ("not_started", "Not started"))
        self.assertIn("double-click ssh_upload.command", shown[2])
        shown = self.show(record("queued", age=121))
        self.assertEqual(shown, ("not_started", "Not started", "The upload program did not start. Press Retry same "
                                                               "request."))
        self.assertEqual(arc_records.actions(record("queued", age=121), "not_started", False), ["retry", "stop"])
        # A worker is still starting.
        self.assertEqual(self.show(record("queued", age=119))[0], "queued")
        self.assertEqual(arc_records.actions(record("queued", age=119), "queued", False), ["stop"])

    def test_a_worker_that_stopped(self):
        for state in ("preparing", "signing_in", "checking", "submitting", "verifying"):
            shown = self.show(record(state))
            self.assertEqual(shown[:2], ("interrupted", "Interrupted"), state)
            self.assertEqual(arc_records.actions(record(state), "interrupted", False), ["retry", "stop"])
        self.assertEqual(self.show(record("transferring", transport="ssh"))[0], "interrupted")

    def test_waiting_and_final_states_show_as_stored(self):
        for state in ("uncertain", "conflict", "verified", "failed", "stopped"):
            self.assertEqual(self.show(record(state, age=99999))[0], state)

    def test_the_lock_held_by_another_process_is_a_live_worker(self):
        arc_support.ready_session(self.home)
        upload_id = self.upload()["upload"]["upload_id"]
        folder = self.record_dir(upload_id)
        arc_records.update_record(folder, lambda current: current.update(
            state="transferring", state_since_utc=utc(5000)))
        self.assertEqual(self.ok("arc-status", "--upload=" + upload_id)["upload"]["display_state"], "interrupted")
        arc_support.hold_lock(self, folder / "worker.lock")
        shown = self.ok("arc-status", "--upload=" + upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["worker"]["alive"], shown["actions"]),
                         ("transferring", True, ["stop"]))
        listing = self.ok("arc-list")
        self.assertEqual((listing["active"], listing["unfinished"]), (1, 1))
        self.refused("conflict", "busy", "arc-retry", "--upload=" + upload_id)
        self.refused("conflict", "busy", "arc-check", "--upload=" + upload_id)


class Actions(unittest.TestCase):
    """The buttons of each state (design 7.7)."""

    def test_failed_and_stopped(self):
        kept = {"files": 1, "bytes": 1, "manifest_sha256": "0" * 64, "kept": True}
        gone = dict(kept, kept=False)
        cases = [
            (record("failed", error={"retryable": True}), ["retry"]),
            (record("failed", error={"retryable": False}), []),
            (record("failed", error={"retryable": True}, payload=gone), []),
            (record("failed", error={"retryable": True}, payload=kept, globus={"task_id": "t"}), ["retry", "check"]),
            (record("failed", error={"retryable": False}, globus={"earlier_attempts": [{"task_id": "t"}]}),
             ["check"]),
            (record("failed", transport="ssh", error={"retryable": True}, ssh={"sent": True}), ["retry", "check"]),
            (record("stopped"), ["retry"]),
            (record("stopped", payload=gone), []),
            (record("stopped", transport="ssh", ssh={"sent": True}), ["retry", "check"]),
        ]
        for value, expected in cases:
            self.assertEqual(arc_records.actions(value, value["state"], False), expected, value)

    def test_waiting_and_verified(self):
        self.assertEqual(arc_records.actions(record("uncertain"), "uncertain", False), ["retry", "check", "stop"])
        self.assertEqual(arc_records.actions(record("conflict"), "conflict", False), ["keep_copy", "retry", "stop"])
        self.assertEqual(arc_records.actions(record("verified"), "verified", False), ["check", "copy_path"])
        self.assertEqual(arc_records.actions(record("verified", demo=True), "verified", False),
                         ["check", "show_in_finder"])
        self.assertEqual(arc_records.actions(record("transferring"), "transferring", True), ["stop"])

    def test_needs(self):
        self.assertEqual(arc_records.needs(record("signing_in"), "signing_in", True), "browser")
        self.assertEqual(arc_records.needs(record("waiting_for_terminal", transport="ssh"), "waiting_for_terminal",
                                           False), "terminal")
        self.assertEqual(arc_records.needs(record("conflict"), "conflict", False), "decision")
        self.assertIsNone(arc_records.needs(record("signing_in", demo=True), "signing_in", True))


class RetryTarget(unittest.TestCase):
    def target(self, value, display_state=None):
        return arc_records.retry_target(value, display_state or value["state"])

    def test_the_rows_of_the_table(self):
        kept = {"files": 1, "bytes": 1, "manifest_sha256": "0" * 64, "kept": True}
        rows = [
            (record("failed", payload=dict(kept, kept=False)), "payload_missing"),
            (record("failed", transport="ssh"), ("waiting_for_terminal", None)),
            (record("stopped", transport="ssh", demo=True), ("queued", None)),
            (record("uncertain", globus={"submission_id": "s"}), ("queued", "submitting")),
            (record("submitting", globus={"submission_id": "s"}), ("queued", "submitting")),
            (record("failed", globus={"task_id": "t", "task": {"status": "ACTIVE"}}), ("queued", "transferring")),
            (record("failed", globus={"task_id": "t", "task": {"status": "INACTIVE"}}), ("queued", "transferring")),
            (record("failed", globus={"task_id": "t", "task": {"status": "FAILED"}}), ("queued", "checking")),
            (record("failed", error={"reason": "verify_mismatch"},
                    globus={"task_id": "t", "task": {"status": "SUCCEEDED"}}), ("queued", "checking")),
            (record("conflict", conflict_at="placing"), ("queued", "placing")),
            (record("placing", globus={"task_id": "t", "task": {"status": "SUCCEEDED"}}), ("queued", "placing")),
            (record("conflict", conflict_at="checking"), ("queued", "checking")),
            (record("failed"), ("queued", "checking")),
            (record("stopped", globus={"task_id": "t", "task": {"status": "SUCCEEDED"}}), ("queued", "verifying")),
        ]
        for value, expected in rows:
            if expected == "payload_missing":
                with self.assertRaises(arc_records.ArcError) as raised:
                    self.target(value)
                self.assertEqual(raised.exception.reason, "payload_missing")
            else:
                self.assertEqual(self.target(value), expected, value)

    def test_it_is_a_pure_function(self):
        value = record("uncertain", globus={"submission_id": "s"})
        before = copy.deepcopy(value)
        self.target(value)
        self.assertEqual(value, before)


TWO_UPLOADS = textwrap.dedent('''
    import json, os, sys, time
    from unittest import mock
    sys.path.insert(0, sys.argv[1])
    from raman_catalog import arc_worker, cli
    go = sys.argv[3]
    while not os.path.exists(go):
        time.sleep(0.005)
    with mock.patch.object(arc_worker, "spawn"):
        code = cli.main(["--home", sys.argv[2], "arc-upload", "--session=%s", "--demo"])
    sys.exit(code)
''' % SESSION)


class Requests(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home)

    def test_two_uploads_at_once_make_one_record(self):
        go = self.home / "go"
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        processes = [subprocess.Popen([sys.executable, "-c", TWO_UPLOADS, str(support.DATA_LAYER), str(self.home),
                                       str(go)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                      cwd=str(self.home)) for _index in range(2)]
        time.sleep(0.5)
        go.write_text("go", encoding="utf-8")
        answers = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            answers.append(json.loads(stdout))
        self.assertEqual(sorted(answer["existing"] for answer in answers), [False, True])
        self.assertEqual(len({answer["upload"]["upload_id"] for answer in answers}), 1)
        self.assertEqual(len(arc_records.list_records(self.paths)), 1)

    def test_earlier_uploads_of_the_same_session(self):
        first = self.upload()["upload"]["upload_id"]
        # A double click shows the running upload.
        again = self.upload()
        self.assertEqual((again["existing"], again["upload"]["upload_id"]), (True, first))
        answer = self.refused("conflict", "in_progress", "arc-upload", "--session=" + SESSION, "--demo", "--new")
        self.assertEqual(answer["upload"]["upload_id"], first)
        dry = self.ok("arc-upload", "--session=" + SESSION, "--demo", "--dry-run")
        self.assertEqual(dry["existing"]["upload_id"], first)

        folder = self.record_dir(first)
        arc_records.update_record(folder, lambda current: (
            set_state(current, "failed", "Failed."), current.update(error={"retryable": True})))
        answer = self.refused("conflict", "earlier_upload", "arc-upload", "--session=" + SESSION, "--demo")
        self.assertEqual(answer["error"], "This session has an earlier upload that ended as Failed.")
        self.assertEqual(answer["upload"]["upload_id"], first)

        arc_records.update_record(folder, lambda current: current.update(state="verified", verification={
            "method": "test", "utc": "2026-11-15T16:02:39.110Z"}))
        answer = self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + SESSION, "--demo")
        # COR-05: a demo upload is in the demo folder on this Mac, not on ARC.
        local = self.record(first)["destination"]["local_final_path"]
        self.assertEqual(answer["error"], "Demo: %s is already in the demo folder: %s (checked 2026-11-15 16:02 UTC)."
                         % (SESSION, local))
        self.assertNotIn("on ARC", answer["error"])
        self.assertIn("Upload again", answer["fix"])
        second = self.upload("--new")["upload"]["upload_id"]
        self.assertNotEqual(second, first)
        new = self.record(second)
        self.assertTrue(new["destination"]["new_copy"])
        self.assertIn("It goes beside the earlier copy, under the first free name of %s, %s_copy2, %s_copy3 and so "
                      "on." % (SESSION, SESSION, SESSION), new["notes"])

    def test_a_live_upload_already_on_arc_and_the_date_of_its_newest_check(self):
        real = "1d_x_scan_20261113_111500_456"
        arc_support.ready_session(self.home, folder=real, simulated=False, dark=False)
        folder = self.record_dir(self.upload(session=real)["upload"]["upload_id"])

        def make_live(current):
            # The record as a live upload's (no far end runs here).
            current.update(state="verified", message="On ARC.", demo=False,
                           verification={"method": "test", "utc": "2026-11-15T16:02:39.110Z"})
            current["destination"].update(key="live:%s|real" % arc_support.ARC_FOLDER, demo_root=None,
                                          local_final_path=None)

        arc_records.update_record(folder, make_live)
        with mock.patch("raman_catalog.arc_commands.setup_errors", return_value=[]):
            answer = self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + real, "--live",
                                  "--dry-run")
            self.assertEqual(answer["error"], "%s is already on ARC: %s/%s (checked 2026-11-15 16:02 UTC)."
                             % (real, arc_support.ARC_FOLDER, real))
            self.assertTrue(answer["fix"].endswith("To upload it again beside it: --new."))
            # COR-V02: a later check that found it quotes its own date.
            arc_records.update_record(folder, lambda current: current.update(checks=[
                {"utc": "2026-11-20T09:30:00.000Z", "ok": True, "message": "Still on ARC."}]))
            answer = self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + real, "--live",
                                  "--dry-run")
        self.assertIn("(checked 2026-11-20 09:30 UTC)", answer["error"])

    def test_after_a_check_found_the_copy_gone_a_new_upload_goes_under_the_sessions_own_name(self):
        # COR-V02 and I4: the newest check found the copy missing or different (On ARC, last check failed):
        # Upload to ARC goes ahead without --new, and not as a copy beside it.
        first = self.upload()["upload"]["upload_id"]
        arc_records.update_record(self.record_dir(first), lambda current: current.update(
            state="verified", message="Demo: on ARC.", checks=[
                {"utc": "2026-11-20T09:30:00.000Z", "ok": False, "message": "Demo: the folder is no longer there."}]))
        shown = self.ok("arc-status", "--upload=" + first)["upload"]
        self.assertEqual((shown["display_state"], shown["display_label"], shown["message"]),
                         ("check_failed", "On ARC, last check failed", "Demo: the folder is no longer there."))
        plan = self.ok("arc-upload", "--session=" + SESSION, "--demo", "--dry-run")["plan"]
        self.assertIs(plan["new_copy"], False)
        second = self.upload()["upload"]["upload_id"]
        self.assertNotEqual(second, first)
        self.assertIs(self.record(second)["destination"]["new_copy"], False)
        self.assertFalse([note for note in self.record(second)["notes"] if "beside" in note])

    def test_a_check_that_could_not_look_keeps_the_session_on_arc(self):
        # REG-01: the entry a check of a verified upload writes when its sign-in did not happen. The upload stays
        # "On ARC, checked", needs nobody, and Upload to ARC still answers already_on_arc with the date the copy
        # was last seen; only --new puts a copy beside it.
        first = self.upload()["upload"]["upload_id"]
        note = dict(arc_records.check_note("Demo: you did not finish signing in within 10 minutes.", "signin_timeout"),
                    utc="2026-11-20T09:30:00.000Z")
        arc_records.update_record(self.record_dir(first), lambda current: current.update(
            state="verified", message="Demo: on ARC.", checks=[note],
            verification={"method": "test", "utc": "2026-11-15T16:02:39.110Z"}))
        shown = self.ok("arc-status", "--upload=" + first)["upload"]
        self.assertEqual((shown["display_state"], shown["message"], shown["last_check"]),
                         ("verified", "Demo: on ARC.", note))
        self.assertEqual((self.ok("status")["arc_attention"], self.ok("arc-list")["attention"]), (0, 0))
        answer = self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + SESSION, "--demo",
                              "--dry-run")
        self.assertIn("(checked 2026-11-15 16:02 UTC)", answer["error"])
        self.assertIs(self.ok("arc-upload", "--session=" + SESSION, "--demo", "--dry-run", "--new")["plan"]["new_copy"],
                      True)

    def test_a_copy_of_a_session_folder_is_named_with_the_folder_it_copies(self):
        # COR-10: a copy (cp -R) holds the same session; the refusal says which folder is on ARC.
        first = self.upload()["upload"]["upload_id"]
        arc_records.update_record(self.record_dir(first), lambda current: current.update(state="verified"))
        backup = SESSION + "_backup"
        shutil.copytree(self.home / "scan_sessions" / SESSION, self.home / "scan_sessions" / backup)
        arc_support.set_age(self.home / "scan_sessions" / backup, 600)
        answer = self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + backup, "--demo")
        self.assertTrue(answer["error"].startswith("Demo: %s holds the same session as %s, which is already in the "
                                                   "demo folder: " % (backup, SESSION)), answer["error"])

    def test_a_simulated_session_cannot_go_live_whatever_a_demo_upload_did(self):
        self.upload()
        with mock.patch("raman_catalog.arc_commands.setup_errors", return_value=[]), \
                mock.patch.object(arc_worker, "open_terminal", return_value=(True, "")):
            answer = self.invoke("arc-upload", "--session=" + SESSION, "--live")
        # The session is simulated: refused live, whatever the demo upload did.
        self.assertEqual(answer["reason"], "simulated_not_allowed")


class AfterReview(unittest.TestCase):
    """What a person sees and may do, as the review asked (display states and actions)."""

    def test_a_verified_upload_whose_newest_check_failed(self):
        # I1 (UI-01, SSH-01): its own display state and label, the check's message, still Check status.
        checks = [{"utc": utc(50), "ok": True, "message": "Still on ARC."},
                  {"utc": utc(10), "ok": False, "message": "Nothing of this upload is on ARC."}]
        for demo, local in ((False, "copy_path"), (True, "show_in_finder")):
            value = record("verified", demo=demo, checks=checks)
            self.assertEqual(arc_records.display(value, False, now=NOW),
                             ("check_failed", "On ARC, last check failed", "Nothing of this upload is on ARC."))
            self.assertEqual(arc_records.actions(value, "check_failed", False), ["check", local])
            shown = arc_records.item(value, False, now=NOW)
            self.assertEqual((shown["state"], shown["display_state"], shown["last_check"]["ok"]),
                             ("verified", "check_failed", False))
        # The newest check found it: on ARC, checked.
        value = record("verified", checks=list(reversed(checks)))
        self.assertEqual(arc_records.display(value, False, now=NOW)[0], "verified")
        # While a new check runs, the failed one is still the newest.
        self.assertEqual(arc_records.display(record("verified", checks=checks), True, now=NOW)[0], "check_failed")

    def test_stop_is_offered_whatever_a_worker_does(self):
        # I3 (SEC-01, SSH-V03): a Check status worker on a verified, failed or stopped upload can be stopped;
        # nothing else starts while it works.
        for state, local in (("verified", ["copy_path"]), ("failed", []), ("stopped", []), ("uncertain", []),
                             ("conflict", []), ("transferring", [])):
            value = record(state, error={"retryable": True}, globus={"task_id": "t"})
            self.assertEqual(arc_records.actions(value, state, True), ["stop"] + local, state)

    def test_a_check_in_terminal_needs_the_terminal(self):
        value = record("failed", transport="ssh", worker={"action": "check", "terminal": True})
        self.assertEqual(arc_records.needs(value, "failed", True), "terminal")
        self.assertIsNone(arc_records.needs(value, "failed", False))
        self.assertIsNone(arc_records.needs(record("failed", transport="ssh", worker={"action": "check"}),
                                            "failed", True))

    def test_retry_only_while_the_snapshot_is_there(self):
        # COR-03: a snapshot that is gone (the record still says kept) offers no Retry, which could only be
        # refused; an SSH upload that sent something offers Check status, which settles it.
        kept = {"files": 1, "bytes": 1, "manifest_sha256": "0" * 64, "kept": True}
        value = record("placing", transport="ssh", payload=kept, ssh={"sent": True})
        self.assertEqual(arc_records.actions(value, "interrupted", False), ["retry", "check", "stop"])
        self.assertEqual(arc_records.actions(value, "interrupted", False, payload_present=False), ["check", "stop"])
        value = record("failed", error={"retryable": True}, payload=kept)
        self.assertEqual(arc_records.actions(value, "failed", False, payload_present=False), [])
        self.assertEqual(arc_records.actions(record("conflict", payload=kept), "conflict", False,
                                             payload_present=False), ["stop"])
        # Nothing was sent: nothing to check.
        self.assertEqual(arc_records.actions(record("checking"), "interrupted", False), ["retry", "stop"])

    def test_check_status_when_globus_has_a_request_number(self):
        # COR-07: a failure with a request number and no task: Check status looks the transfer up by its label.
        error = arc_records.worker_error("request_conflict")
        value = record("failed", error=error, globus={"submission_id": "s", "task_id": None})
        self.assertEqual(arc_records.actions(value, "failed", False), ["retry", "check"])
        self.assertEqual(arc_records.actions(record("submitting", globus={"submission_id": "s"}), "interrupted",
                                             False), ["retry", "check", "stop"])

    def test_an_unexpected_end_can_be_retried(self):
        # COR-V03: after failed / internal, Retry same request is offered (it resumes from the record), as
        # the texts of a check of such an upload say.
        error = arc_records.worker_error("internal", type="OSError", folder="/x")
        self.assertIs(error["retryable"], True)
        self.assertTrue(error["fix"].startswith("Press Retry same request."))
        value = record("failed", transport="ssh", error=error, ssh={"sent": True})
        self.assertEqual(arc_records.actions(value, "failed", False), ["retry", "check"])

    def test_a_terminal_that_did_not_open_is_not_started_at_once(self):
        # SSH-04: /usr/bin/open failed; nothing will start the attempt unless the person acts.
        value = record("waiting_for_terminal", transport="ssh", age=5,
                       ssh={"terminal_open_failed_utc": utc(5)})
        shown = arc_records.display(value, False, now=NOW)
        self.assertEqual(shown[0], "not_started")
        self.assertEqual(arc_records.actions(value, "not_started", False), ["retry", "stop"])
        # SSH-V02: the text names the macOS permission that stops a Terminal window from starting it.
        self.assertIn("Operation not permitted", shown[2])
        self.assertIn("Privacy & Security > Files and Folders", shown[2])
        self.assertEqual(arc_records.display(record("waiting_for_terminal", transport="ssh", age=5), False,
                                             now=NOW)[0], "waiting_for_terminal")

    def test_the_demo_stop_texts_name_no_arc(self):
        # COR-05: a demo's copy is in the demo folder on this Mac.
        for key in ("stopped", "stopped_complete", "stopped_placing"):
            text = arc_records.message(key, True, incoming="/x/arc_demo/projects/p/_incoming/s.u1")
            self.assertTrue(text.startswith("Demo: "), text)
            self.assertNotIn("on ARC", text)
            self.assertIn("on ARC", arc_records.message(key, False, incoming="/projects/p/_incoming/s.u1"))

    def test_the_demo_texts_of_the_steps_and_the_results_name_the_demo_folder(self):
        # COR-05, the rest: a demo upload's steps and results name the demo folder, never ARC as the place.
        for key in ("checking_globus", "verifying", "placing"):
            live, demo = arc_records.message(key, False, identity="x"), arc_records.message(key, True, identity="x")
            self.assertIn("ARC", live)
            self.assertTrue(demo.startswith("Demo: ") and "demo folder" in demo and "ARC" not in demo, demo)
        values = {"description": "the connection was lost", "incoming": "/x/arc_demo/p/_incoming/s.u1", "file": "a",
                  "path": "/x/arc_demo/p/s", "what": "make the folder", "text": "cannot make /x"}
        for reason in arc_records.DEMO_RESULTS:
            live = arc_records.worker_error(reason, **values)
            demo = arc_records.worker_error(reason, demo=True, **values)
            with self.subTest(reason=reason):
                self.assertIn("ARC", live["message"] + live["fix"])
                self.assertNotIn("on ARC", demo["message"] + " " + demo["fix"])
                self.assertNotIn("ARC could", demo["message"])
                self.assertEqual((demo["kind"], demo["reason"], demo["retryable"]),
                                 (live["kind"], live["reason"], live["retryable"]))
        self.assertEqual(arc_records.worker_error("ssh_interrupted", demo=True, **values)["fix"],
                         "Press Retry same request: all files are sent again, into the same folder.")

    def test_a_check_that_could_not_look_changes_nothing(self):
        # REG-01: a check that could not look (a sign-in that timed out, ssh that ended with 255, an error of this
        # program) says nothing about the copy: no check_failed, no attention, and Upload to ARC still finds the
        # session on ARC. It says why, and what to do.
        note = dict(arc_records.check_note("You did not finish signing in within 10 minutes.", "signin_timeout"),
                    utc=utc(5))
        self.assertEqual(note, {"ok": False, "looked": False, "utc": utc(5),
                                "message": "You did not finish signing in within 10 minutes. Check status could not "
                                           "look.",
                                "fix": "Press Check status again and sign in when the page opens."})
        for demo in (False, True):
            value = record("verified", demo=demo, checks=[note])
            self.assertEqual(arc_records.display(value, False, now=NOW), ("verified", "On ARC, checked",
                                                                          "stored message"))
            self.assertFalse(arc_records.check_failed(value))
            self.assertFalse(arc_records.needs_attention("verified", value, now=NOW))
            shown = arc_records.item(value, False, now=NOW)
            self.assertEqual((shown["display_state"], shown["last_check"]), ("verified", note))
        # A failed look stays what the listing shows while later checks cannot look; a look that finds the copy
        # again ends it.
        gone = dict(arc_records.check_result(False, "Nothing of this upload is on ARC."), utc=utc(50))
        self.assertEqual(gone, {"ok": False, "looked": True, "fix": None, "utc": utc(50),
                                "message": "Nothing of this upload is on ARC."})
        value = record("verified", checks=[gone, note])
        self.assertEqual(arc_records.display(value, False, now=NOW),
                         ("check_failed", "On ARC, last check failed", "Nothing of this upload is on ARC."))
        self.assertTrue(arc_records.needs_attention("check_failed", value, now=NOW))
        found = dict(arc_records.check_result(True, "Still on ARC."), utc=utc(3))
        self.assertEqual(arc_records.display(record("verified", checks=[gone, note, found]), False, now=NOW)[0],
                         "verified")
        # An entry without "looked" (written before this field) counts as a look.
        self.assertEqual(arc_records.display(record("verified", checks=[{"utc": utc(1), "ok": False, "message": "x"}]),
                                             False, now=NOW)[0], "check_failed")
        # Every reason a check may meet has a fix that names Check status, never Retry same request.
        for reason in (None, "signin_timeout", "no_browser", "signin_failed", "consent", "globus_error",
                       "ssh_signin_failed", "ssh_interrupted", "remote_tools", "internal", "demo_folder_unwritable",
                       "folder_not_readable", "folder_refused", "ssh_host_refused_commands"):
            for demo in (False, True):
                fix = arc_records.check_note("Why.", reason, demo)["fix"]
                self.assertIn("Check status", fix, reason)
                self.assertNotIn("Retry", fix, reason)
        # Review R2-03: a demo upload takes the demo's fix where there is one; it never names ARC as the place.
        self.assertEqual(arc_records.check_note("Why.", "folder_refused", True)["fix"],
                         arc_records.CHECK_FIXES["folder_refused_demo"])
        # Review R3-04: the demo's check runs on this Mac, and its fix says so.
        for reason in ("remote_tools", "ssh_host_refused_commands"):
            self.assertEqual(arc_records.check_note("Why.", reason, True)["fix"],
                             arc_records.CHECK_FIXES[reason + "_demo"])
            self.assertIn("this Mac", arc_records.CHECK_FIXES[reason + "_demo"])
            self.assertEqual(arc_records.check_note("Why.", reason)["fix"], arc_records.CHECK_FIXES[reason])
        for reason in ("folder_not_readable_demo", "folder_refused_demo", "remote_tools_demo",
                       "ssh_host_refused_commands_demo"):
            self.assertNotIn(" on ARC", arc_records.CHECK_FIXES[reason])
            # Review R3-02: a check uses the demo folder saved with the upload, never the one ARC settings name now.
            self.assertNotIn("ARC settings", arc_records.CHECK_FIXES[reason])
        # Decision I9's network sentence, as in the fix of a send whose sign-in failed.
        network = "ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN."
        self.assertIn(network, arc_records.CHECK_FIXES["ssh_signin_failed"])
        self.assertIn(network, arc_records.WORKER_RESULTS["ssh_signin_failed"][1])

    def test_the_listing_says_whether_globus_may_still_copy(self):
        # REG-03: "may_run", from the record alone: what a stop, also the stop of a running check, cancels.
        cases = ((record("failed", globus={"task_id": "t", "task": {"status": "ACTIVE"}}), True),
                 (record("failed", globus={"task_id": "t", "task": {"status": "FAILED"}}), False),
                 (record("verified", globus={"task_id": "t", "task": {"status": "SUCCEEDED"}}), False),
                 (record("uncertain", globus={"submission_id": "s", "posts": [{"outcome": "unknown"}]}), True),
                 (record("failed", globus={"submission_id": "s", "posts": [{"outcome": "refused"}]}), False),
                 (record("failed", transport="ssh", ssh={"sent": True}), False))
        for value, expected in cases:
            with self.subTest(state=value["state"], globus=value.get("globus")):
                self.assertIs(arc_records.item(value, True, now=NOW)["may_run"], expected)


class Attention(arc_support.ArcCase):
    """I2 (UI-V01): status and arc-list count the uploads that wait for a person."""

    def write(self, index, state, age=60, **changes):
        folder = arc_records.ensure_uploads_folder(self.paths) / ("u%012x" % index)
        folder.mkdir()
        value = record(state, upload_id=folder.name, **changes)
        value["updated_utc"] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age)
                                ).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        (folder / "record.json").write_text(json.dumps(value), encoding="utf-8")
        return folder

    def test_uploads_that_need_a_person(self):
        self.write(1, "failed", error={"retryable": True})
        self.write(2, "conflict")
        self.write(3, "uncertain", globus={"submission_id": "s"})
        self.write(4, "transferring")                                    # interrupted
        self.write(5, "verifying", globus={"task_id": "t"})              # not watched
        self.write(6, "verified", checks=[{"utc": utc(1), "ok": False, "message": "Gone."}])
        self.write(7, "verified")                                         # on ARC: nothing to do
        self.write(8, "stopped")                                          # the person stopped it
        self.write(9, "failed", age=8 * 86400)                            # more than a week ago
        live = self.write(10, "transferring")                             # a worker works on it
        arc_support.hold_lock(self, live / "worker.lock")
        self.assertEqual(arc_records.count_attention(self.paths), 6)
        status = self.ok("status")
        self.assertEqual((status["arc_attention"], status["arc_unfinished"]), (6, 5))
        listing = self.ok("arc-list", "--limit=1")
        self.assertEqual((listing["attention"], len(listing["items"]) >= 1), (6, True))
        with mock.patch.object(commands_core, "_optional", return_value=None):
            self.assertEqual(self.ok("status")["arc_attention"], 0)
        with mock.patch("raman_catalog.arc_records.count_attention", side_effect=OSError("disk")):
            self.assertEqual(self.ok("status")["arc_attention"], 0)

    def test_none(self):
        self.assertEqual((self.ok("status")["arc_attention"], self.ok("arc-list")["attention"]), (0, 0))


class Shape(arc_support.ArcCase):
    """COR-08: a record that parses but has a key of the wrong type is listed as unreadable."""

    def test_a_malformed_record_is_unreadable_and_hides_no_other(self):
        arc_support.ready_session(self.home)
        good = self.upload()["upload"]["upload_id"]
        uploads = arc_records.uploads_folder(self.paths)
        cases = {"u00000000000a": {"destination": ["x"]}, "u00000000000b": {"session": ["x"]},
                 "u00000000000c": {"checks": ["x"]}, "u00000000000d": {"state": 5},
                 "u00000000000e": {"worker": "x"}, "u00000000000f": {"destination": {"demo_root": 5}}}
        for name, change in cases.items():
            (uploads / name).mkdir()
            value = record("verified", upload_id=name)
            value.update(change)
            (uploads / name / "record.json").write_text(json.dumps(value), encoding="utf-8")
        items = {item["upload_id"]: item for item in self.ok("arc-list")["items"]}
        self.assertEqual(items[good]["state"], "queued")
        for name in cases:
            self.assertEqual((items[name]["state"], items[name]["actions"]), ("unreadable", []), name)
            self.assertIn("does not have the shape of a record", items[name]["message"])
        self.ok("arc-sessions")
        self.assertEqual(self.upload()["upload"]["upload_id"], good)
        self.ok("arc-upload", "--session=" + SESSION, "--demo", "--dry-run")
        self.refused("command", "nothing_to_stop", "arc-stop", "--upload=u00000000000b")
        self.assertEqual(self.ok("status")["arc_unfinished"], 1)


class DemoRoot(arc_support.ArcCase):
    """I6 (COR-09, SEC-05): the demo folder is made 0700 with a mark when missing; a folder that exists is
    used as it is only when it is empty or marked, and its mode is never changed."""

    def test_a_missing_folder_is_made_private_and_marked(self):
        root = self.home / "data_catalog" / "arc_demo"
        self.assertIsNone(arc_records.demo_root_problem(root))
        self.assertEqual(arc_records.ensure_demo_root(root), root)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertTrue((root / arc_records.DEMO_MARKER).is_file())
        (root / "projects").mkdir()
        # Marked: used again as it is.
        self.assertEqual(arc_records.ensure_demo_root(root), root)

    def test_an_empty_folder_is_used_as_it_is(self):
        root = self.home / "team_demo"
        root.mkdir()
        os.chmod(root, 0o755)
        arc_records.ensure_demo_root(root)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
        self.assertTrue((root / arc_records.DEMO_MARKER).is_file())

    def test_a_folder_that_holds_other_files_a_link_or_a_file_is_refused_and_left_alone(self):
        shared = self.home / "shared_team_folder"
        shared.mkdir()
        (shared / "team_notes.txt").write_text("notes\n", encoding="utf-8")
        os.chmod(shared, 0o775)
        target = self.home / "elsewhere"
        target.mkdir()
        link = self.home / "linked_demo"
        link.symlink_to(target)
        plain = self.home / "a_file"
        plain.write_text("x", encoding="utf-8")
        for path, words in ((shared, "already holds other files"), (link, "is a link"), (plain, "not a folder"),
                            (plain / "below", "is not a folder")):
            with self.assertRaises(arc_records.ArcError) as raised:
                arc_records.ensure_demo_root(path)
            self.assertEqual((raised.exception.kind, raised.exception.reason), ("config", "demo_folder_unwritable"))
            self.assertIn(words, str(raised.exception))
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o775)
        self.assertEqual(sorted(os.listdir(shared)), ["team_notes.txt"])
        self.assertEqual(os.listdir(target), [])

    def test_a_demo_upload_into_a_shared_folder_is_refused_before_anything_runs(self):
        arc_support.ready_session(self.home)
        shared = self.home.parent / (self.home.name + "_shared")
        shared.mkdir()
        self.addCleanup(shutil.rmtree, shared, ignore_errors=True)
        (shared / "team_notes.txt").write_text("notes\n", encoding="utf-8")
        os.chmod(shared, 0o775)
        arc_support.write_settings(self.home, {"demo_folder": str(shared)})
        answer = self.refused("config", "demo_folder_unwritable", "arc-upload", "--session=" + SESSION, "--demo")
        self.assertIn("already holds other files", answer["error"])
        ready = self.ok("arc-list")["ready"]["demo"]
        self.assertEqual((ready["ok"], ready["problems"][0]["reason"]), (False, "demo_folder_unwritable"))
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o775)
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()
