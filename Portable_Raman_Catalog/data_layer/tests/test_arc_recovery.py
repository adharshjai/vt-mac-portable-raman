"""Crash and restart, step by step (PROTOCOL.md 5.6; design section 13): the 21 rows, with real processes.

Each row stops an upload at one step. A real process ends there with os._exit(97) (RAMAN_ARC_TEST_HOOKS=1
and RAMAN_ARC_FAULT=<name>), is killed or hung up, or two processes run at once; then the next command or
worker, a real process too, continues. The far ends are the demo's, at pace 0 unless a row needs a
transfer that is still running: the demo Globus service, which keeps its tasks on disk, and the real
remote script run with /bin/bash. Row 15 runs the Terminal worker of a live SSH upload with
tests/fake_ssh.py standing in for /usr/bin/ssh. Row 21 is the one row that mocks: os.fsync and the POST.

Every row ends verified, with the expected number of Globus tasks and request numbers, one final folder,
nothing left in _incoming, and final content equal to the manifest (SHA-256). Nothing here reaches a
network, a browser or a Terminal: every ARC test sets RAMAN_ARC_TEST_GUARD=1.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import arc_support
import support
from arc_support import SESSION
from raman_catalog import arc_commands, arc_demo, arc_globus, arc_payload, arc_records, arc_worker
from raman_catalog.config import utc_text

# A second session, for the rows that need two.
OTHER_SESSION = "1d_x_scan_20261113_111500_456"


def durably_synced_paths(test):
    """Record the path of every file and folder written to the drive itself (arc_records.durable_sync: on
    macOS F_FULLFSYNC, design I5) while the test runs (macOS F_GETPATH)."""
    import fcntl

    seen = []
    real = arc_records.durable_sync

    def durable_sync(handle):
        try:
            seen.append(fcntl.fcntl(handle, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode())
        except (OSError, AttributeError):
            seen.append(None)
        return real(handle)

    patcher = mock.patch.object(arc_records, "durable_sync", durable_sync)
    patcher.start()
    test.addCleanup(patcher.stop)
    return seen


class RecoveryCase(arc_support.ArcCase):
    """A finished simulated session (demo area simulated/). Commands run in this process (their spawn
    recorded) unless a row is about the command itself; every worker is a process of its own."""

    def setUp(self):
        super().setUp()
        self.session = arc_support.ready_session(self.home, points=13)
        self.demo_root = self.home / "data_catalog" / "arc_demo"
        self.base = arc_support.demo_base(self.home)
        # A worker that a row left running would write into a HOME that is being removed.
        self.addCleanup(arc_support.wait_for_workers, self.home)

    # -- the upload and its workers

    def start(self, transport="globus", *options, session=SESSION):
        answer = self.upload("--transport=" + transport, *options, session=session)
        self.take(answer["upload"]["upload_id"])
        return answer

    def take(self, upload_id):
        self.upload_id = upload_id
        self.folder = self.record_dir(upload_id)

    def worker(self, action="send", fault=None, pace=None):
        """python -m raman_catalog.arc_worker for this upload, a process of its own: its exit code."""
        extra = arc_support.hooks(fault) if fault else {}
        if pace is not None:
            extra["RAMAN_ARC_DEMO_PACE"] = str(pace)
        code, _stdout, _stderr = arc_support.run_worker_program(
            self.home, "--upload", self.upload_id, "--action", action, extra_environment=extra)
        return code

    def worker_process(self, pace):
        """The worker as a process that runs on while the test acts (a transfer at `pace` s per file)."""
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        env["RAMAN_ARC_DEMO_PACE"] = str(pace)
        process = subprocess.Popen(
            [sys.executable, "-m", "raman_catalog.arc_worker", "--home", str(self.home), "--upload", self.upload_id,
             "--action", "send"], cwd=str(self.home), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

        def end():
            if process.poll() is None:
                process.kill()
            process.wait(30)

        self.addCleanup(end)
        return process

    def cli(self, *arguments, fault=None, pace=None):
        """The command line as the controller runs it (a process of its own; its spawn is real)."""
        extra = arc_support.hooks(fault) if fault else {}
        if pace is not None:
            extra["RAMAN_ARC_DEMO_PACE"] = str(pace)
        code, answer, _stderr = arc_support.run_cli(self.home, *arguments, extra_environment=extra)
        return code, answer

    def wait_for(self, state, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.record(self.upload_id)["state"] == state:
                return
            time.sleep(0.02)
        now = self.record(self.upload_id)["state"]
        self.fail("upload %s never reached %s (it is %s)" % (self.upload_id, state, now))

    def settled(self):
        """The record once the real detached worker that a command started has ended."""
        record = arc_support.wait_for_record(self.home, self.upload_id)
        self.assertIn(record["state"], arc_support.RESTING_STATES, record)
        return record

    # -- what is on disk

    def shown(self):
        return self.ok("arc-status", "--upload=" + self.upload_id)["upload"]

    def events(self):
        return [event["text"] for event in arc_records.read_events(self.folder, last=0)]

    def snapshots(self):
        """The snapshot folders in the record folder: payload/ and any payload.tmp-* a copy left."""
        return sorted(path.name for path in self.folder.iterdir()
                      if path.name == arc_records.PAYLOAD_FOLDER or path.name.startswith(arc_payload.TEMPORARY_PREFIX))

    def service(self):
        return arc_demo.DemoGlobus(self.demo_root, pace=0)

    def tasks(self):
        """The demo Globus service's tasks of this upload (their label holds the upload id)."""
        return [task for task in self.service().task_list(limit=1000) if self.upload_id in (task.get("label") or "")]

    def incoming(self):
        return self.base / "_incoming" / ("%s.%s" % (self.record(self.upload_id)["session"]["folder"],
                                                     self.upload_id))

    def assert_on_arc(self, tasks=None, requests=None, name=None):
        """Verified; one final folder of this session, whose content equals the manifest (SHA-256);
        nothing left in _incoming; the snapshot deleted; for Globus the tasks and request numbers."""
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified", record.get("message"))
        folder = record["session"]["folder"]
        name = name or folder
        finals = sorted(path.name for path in self.base.iterdir() if path.name.startswith(folder))
        self.assertEqual(finals, [name])
        self.assertEqual(os.listdir(self.base / "_incoming"), [])
        manifest = arc_support.same_as_manifest(self, self.base / name)
        self.assertEqual(manifest["upload_id"], self.upload_id)
        self.assertEqual(record["destination"]["placed_as"], name)
        self.assertFalse((self.folder / arc_records.PAYLOAD_FOLDER).exists())
        self.assertIs(record["payload"]["kept"], False)
        self.assertIsNone(record["worker"])
        self.assertFalse(arc_records.worker_alive(self.folder))
        if tasks is not None:
            found = self.tasks()
            self.assertEqual(len(found), tasks, found)
            self.assertEqual(len({task["submission_id"] for task in found}), requests)
            self.assertIn(record["globus"]["task_id"], [task["task_id"] for task in found])
            for task in found:
                self.assertIn(task["status"], ("SUCCEEDED", "FAILED"))
        return record


# ---------------------------------------------------------------------- rows 1 and 2: arc-upload


class UploadCommand(RecoveryCase):
    def test_row_01_upload_ends_before_its_record_folder_is_renamed(self):
        code, answer = self.cli("arc-upload", "--session=" + SESSION, "--demo", "--transport=globus",
                                fault="create_before_rename")
        self.assertEqual((code, answer), (arc_worker.EXIT_FAULT, None))
        left = os.listdir(arc_records.uploads_folder(self.paths))
        self.assertEqual(len([name for name in left if name.startswith(arc_records.CREATING_PREFIX)]), 1, left)
        self.assertEqual([name for name in left if arc_records.UPLOAD_ID.fullmatch(name)], [])
        # Every reader leaves the half-made folder out: no upload, nothing unfinished.
        self.assertEqual(self.ok("arc-list")["total"], 0)
        self.assertEqual(self.ok("arc-sessions")["items"][0]["uploads"], {"live": None, "demo": None})
        self.assertEqual(self.ok("status")["arc_unfinished"], 0)

        code, answer = self.cli("arc-upload", "--session=" + SESSION, "--demo", "--transport=globus")
        self.assertEqual((code, answer["existing"], answer["worker"]), (0, False, "detached"))
        self.take(answer["upload"]["upload_id"])
        self.settled()
        self.assert_on_arc(tasks=1, requests=1)
        self.assertEqual(self.ok("arc-list")["total"], 1)

    def test_row_02_record_written_worker_not_started(self):
        code, answer = self.cli("arc-upload", "--session=" + SESSION, "--demo", "--transport=globus",
                                fault="upload_before_spawn")
        self.assertEqual((code, answer), (arc_worker.EXIT_FAULT, None))
        (item,) = self.ok("arc-list")["items"]
        self.take(item["upload_id"])
        self.assertEqual((item["state"], item["display_state"], item["actions"]), ("queued", "queued", ["stop"]))
        # Two minutes later nobody has started it.
        earlier = utc_text(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=121))
        arc_records.update_record(self.folder, lambda record: record.update(state_since_utc=earlier))
        shown = self.shown()
        self.assertEqual((shown["state"], shown["display_state"], shown["display_label"], shown["actions"]),
                         ("queued", "not_started", "Not started", ["retry", "stop"]))
        self.assertEqual(shown["message"], "Demo: the upload program did not start. Press Retry same request.")

        code, answer = self.cli("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual((code, answer["worker"]), (0, "detached"))
        self.settled()
        self.assert_on_arc(tasks=1, requests=1)


# ---------------------------------------------------------------------- rows 3 and 4: preparing


class Preparing(RecoveryCase):
    def test_row_03_snapshot_interrupted_halfway(self):
        self.start()
        self.assertEqual(self.worker(fault="snapshot_mid_copy"), arc_worker.EXIT_FAULT)
        self.assertEqual(self.record(self.upload_id)["state"], "preparing")
        (temporary,) = [path for path in self.folder.iterdir() if path.name.startswith(arc_payload.TEMPORARY_PREFIX)]
        copied = arc_support.files_below(temporary / SESSION)
        self.assertTrue(0 < len(copied) < len(arc_support.files_below(self.session["folder"])), copied)
        self.assertFalse((self.folder / arc_records.PAYLOAD_FOLDER).exists())
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["actions"]), ("interrupted", ["retry", "stop"]))

        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.snapshots(), [])
        self.assert_on_arc(tasks=1, requests=1)

    def test_row_04_snapshot_made_record_not_written(self):
        self.start()
        self.assertEqual(self.worker(fault="snapshot_before_record"), arc_worker.EXIT_FAULT)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["payload"]), ("preparing", None))
        made = (self.folder / arc_records.PAYLOAD_FOLDER / SESSION / arc_records.MANIFEST_NAME).read_bytes()

        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        self.assert_on_arc(tasks=1, requests=1)
        # Copied once: ARC holds the first run's snapshot, its manifest (with its time) byte for byte.
        self.assertEqual((self.base / SESSION / arc_records.MANIFEST_NAME).read_bytes(), made)
        self.assertIn("the snapshot was checked against its own lists and kept",
                      (self.folder / arc_records.WORKER_LOG).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- rows 5 to 12, 21: Globus


class GlobusSteps(RecoveryCase):
    def test_row_05_checking(self):
        self.start()
        self.assertEqual(self.worker(fault="check_mid"), arc_worker.EXIT_FAULT)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["globus"]["submission_id"]), ("checking", None))
        self.assertTrue(self.incoming().is_dir())
        self.assertEqual(self.tasks(), [])
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["actions"]), ("interrupted", ["retry", "stop"]))

        # The listings are made again, and making the folders that exist counts as done.
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        self.assert_on_arc(tasks=1, requests=1)

    def test_row_06_request_saved_before_the_post(self):
        self.start()
        self.assertEqual(self.worker(fault="submit_before_post"), arc_worker.EXIT_FAULT)
        record = self.record(self.upload_id)
        submission = record["globus"]["submission_id"]
        self.assertTrue(submission)
        self.assertEqual((record["state"], record["globus"]["posts"], record["globus"]["task_id"]),
                         ("submitting", [], None))
        body = json.loads((self.folder / arc_globus.REQUEST_FILE).read_text(encoding="utf-8"))
        self.assertEqual(body["submission_id"], submission)
        self.assertEqual(self.service().posts_of(submission), 0)

        self.assertIs(self.ok("arc-retry", "--upload=" + self.upload_id)["same_submission"], True)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc(tasks=1, requests=1)
        # The same body was posted once and accepted.
        self.assertEqual(record["globus"]["submission_id"], submission)
        self.assertEqual(json.loads((self.folder / arc_globus.REQUEST_FILE).read_text(encoding="utf-8")), body)
        self.assertEqual(self.service().posts_of(submission), 1)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["accepted"])
        self.assertEqual(record["globus"]["earlier_attempts"], [])

    def test_row_07_post_accepted_task_id_not_saved(self):
        self.start()
        self.assertEqual(self.worker(fault="submit_after_post"), arc_worker.EXIT_FAULT)
        record = self.record(self.upload_id)
        submission = record["globus"]["submission_id"]
        self.assertEqual((record["state"], record["globus"]["task_id"], record["globus"]["posts"]),
                         ("submitting", None, []))
        self.assertEqual((self.service().posts_of(submission), len(self.tasks())), (1, 1))

        self.assertIs(self.ok("arc-retry", "--upload=" + self.upload_id)["same_submission"], True)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc(tasks=1, requests=1)
        # Two POSTs of one request number; Globus answered the second with the same task.
        self.assertEqual(self.service().posts_of(submission), 2)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["duplicate"])
        self.assertEqual(record["globus"]["submission_id"], submission)

    def test_row_08_reply_lost_and_posted_again_by_itself(self):
        self.start("globus", "--demo-fault=lose_submit_reply")
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc(tasks=1, requests=1)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["unknown", "duplicate"])
        self.assertEqual(self.service().posts_of(record["globus"]["submission_id"]), 2)
        self.assertEqual(record["globus"]["demo_fault_used"], "lose_submit_reply")
        texts = self.events()
        self.assertIn("Demo: it is not known whether Globus received the request; asking again with the same request "
                      "number.", texts)
        self.assertIn("Demo: Globus answered: already received. One copy.", texts)

    def test_row_09_worker_killed_while_the_task_runs(self):
        self.start()
        # A paced transfer, so that the task still runs when the worker ends.
        self.assertEqual(self.worker(fault="poll_mid_transfer", pace=0.1), arc_worker.EXIT_FAULT)
        record = self.record(self.upload_id)
        task_id = record["globus"]["task_id"]
        self.assertEqual((record["state"], record["globus"]["task"]["status"]), ("transferring", "ACTIVE"))
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["display_label"], shown["actions"]),
                         ("not_watched", "Not being watched", ["check", "stop"]))
        self.assertEqual(shown["message"], "Demo: Globus keeps copying, but this Mac stopped watching. Press Check "
                                           "status to look.")

        # Check status watches the same task to its end, checks the copy and puts it in place.
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.worker("check"), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc(tasks=1, requests=1)
        self.assertEqual(record["globus"]["task_id"], task_id)

    def test_row_09_a_logout_or_restart_ends_the_worker_and_cancels_nothing(self):
        # COR-01 and I8: SIGTERM (as at a logout, restart or shutdown) is not a person's stop: the worker ends
        # at once, the Globus task runs on, and the listing shows the upload as not watched.
        self.start()
        process = self.worker_process(pace=0.2)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            record = self.record(self.upload_id)
            if record["state"] == "transferring" and record["globus"]["task_id"] and \
                    (record.get("worker") or {}).get("pid") == process.pid:
                break
            time.sleep(0.02)
        self.assertEqual(record["state"], "transferring")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(30), 128 + signal.SIGTERM)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"], record["worker"], record["requests"]["stop_utc"]),
                         ("transferring", None, None, None))
        (task,) = self.tasks()
        self.assertEqual(task["status"], "ACTIVE")
        self.assertIsNone(task.get("fatal_error"))
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["actions"]), ("not_watched", ["check", "stop"]))
        self.assertIn("The upload program was ended by the system (SIGTERM) before it finished. Nothing was stopped "
                      "or cancelled.", self.events())
        # Check status watches the same task to its end.
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.worker("check"), arc_worker.EXIT_VERIFIED)
        self.assert_on_arc(tasks=1, requests=1)

    def test_row_10_task_failed_then_copied_again_into_the_same_folder(self):
        self.start("globus", "--demo-fault=fail_task_once")
        self.assertEqual(self.worker(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "task_failed"))
        partial = arc_support.files_below(self.incoming())
        self.assertTrue(0 < len(partial) < record["payload"]["files"], partial)
        shown = self.shown()
        self.assertEqual((shown["actions"], shown["leftover_on_arc"]), (["retry", "check"], str(self.incoming())))

        answer = self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertNotIn("same_submission", answer)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        # One task per attempt, each with its own request number, into one folder.
        record = self.assert_on_arc(tasks=2, requests=2)
        globus = record["globus"]
        self.assertEqual((globus["attempt"], len(globus["earlier_attempts"])), (2, 1))
        self.assertEqual(globus["earlier_attempts"][0]["status"], "FAILED")

    def test_row_11_verifying(self):
        self.start()
        self.assertEqual(self.worker(fault="verify_mid"), arc_worker.EXIT_FAULT)
        self.assertEqual(self.record(self.upload_id)["state"], "verifying")
        self.assertFalse((self.base / SESSION).exists())
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["actions"]), ("not_watched", ["check", "stop"]))

        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.worker("check"), arc_worker.EXIT_VERIFIED)
        self.assert_on_arc(tasks=1, requests=1)

    def test_row_12_after_the_rename_before_the_record(self):
        self.start()
        self.assertEqual(self.worker(fault="place_after_rename"), arc_worker.EXIT_FAULT)
        self.assertEqual(self.record(self.upload_id)["state"], "placing")
        self.assertTrue((self.base / SESSION).is_dir())
        self.assertFalse(self.incoming().exists())

        # The rename answers NotFound; the final folder equals the snapshot: verified.
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.worker("check"), arc_worker.EXIT_VERIFIED)
        self.assert_on_arc(tasks=1, requests=1)
        self.assertIn("Demo: the copy was already in place as %s." % SESSION, self.events())

    def test_row_21_the_request_number_is_on_disk_before_the_post(self):
        self.start()
        # COR-V04: written to the drive itself (F_FULLFSYNC), not only handed to it (fsync).
        seen = durably_synced_paths(self)
        real_submit = arc_demo.DemoGlobus.submit
        checked = []

        def submit(service, body):
            # As if the power went off right after this POST: what is on disk must hold the request.
            folder = str(self.folder.resolve())
            request = [index for index, path in enumerate(seen) if path and arc_globus.REQUEST_FILE in path]
            records = [index for index, path in enumerate(seen)
                       if path and arc_records.RECORD_FILE in path and request and index > request[-1]]
            folders = [index for index, path in enumerate(seen) if path == folder]
            self.assertTrue(request and records, seen)
            self.assertTrue(any(index > request[-1] for index in folders), seen)
            self.assertTrue(any(index > max(records) for index in folders), seen)
            self.assertEqual(arc_records.read_record(self.folder)["globus"]["submission_id"], body["submission_id"])
            self.assertEqual(json.loads((self.folder / arc_globus.REQUEST_FILE).read_text(encoding="utf-8")), body)
            checked.append(body["submission_id"])
            return real_submit(service, body)

        with mock.patch.object(arc_demo.DemoGlobus, "submit", submit), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arc_worker.run_worker(self.home, self.upload_id, "send"), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(checked), 1)
        self.assert_on_arc(tasks=1, requests=1)


# ---------------------------------------------------------------------- rows 13 and 14: SSH


class SshSteps(RecoveryCase):
    def test_row_13_connection_cut_while_sending(self):
        self.start("ssh", "--demo-fault=drop_connection")
        self.assertEqual(self.worker(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "ssh_interrupted"))
        self.assertTrue(arc_support.files_below(self.incoming()))
        self.assertFalse((self.base / SESSION).exists())
        self.assertEqual(self.shown()["actions"], ["retry", "check"])

        # The remote script receives the rest, checks every file and renames the folder into place.
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc()
        self.assertEqual([run["outcome"] for run in record["ssh"]["runs"]], ["cut", "placed"])

    def test_row_14_placed_on_arc_before_the_record_was_written(self):
        self.start("ssh")
        self.assertEqual(self.worker(fault="ssh_after_done"), arc_worker.EXIT_FAULT)
        self.assertEqual(self.record(self.upload_id)["state"], "placing")
        self.assertTrue((self.base / SESSION).is_dir())
        shown = self.shown()
        # Something was sent: Check status is offered too (COR-03), and settles it as well as Retry.
        self.assertEqual((shown["display_state"], shown["actions"]), ("interrupted", ["retry", "check", "stop"]))

        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc()
        self.assertEqual(record["ssh"]["runs"][-1]["outcome"], "already")

    def test_row_14_check_status_settles_it_when_the_snapshot_is_gone(self):
        # COR-03: the record still says the snapshot is kept, but its folder is gone: no Retry (it could only
        # be refused); Check status finds the copy in place and the upload verified.
        self.start("ssh")
        self.assertEqual(self.worker(fault="ssh_after_done"), arc_worker.EXIT_FAULT)
        arc_payload.remove_tree(self.folder / arc_records.PAYLOAD_FOLDER)
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["actions"]), ("interrupted", ["check", "stop"]))
        self.refused("command", "payload_missing", "arc-retry", "--upload=" + self.upload_id)
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.worker("check"), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        arc_support.same_as_manifest(self, self.base / SESSION)


# ---------------------------------------------------------------------- row 15: the Terminal window (live SSH)


class TerminalWindow(arc_support.ArcCase):
    """A live SSH upload whose worker runs as Terminal starts it, a process of its own, with
    tests/fake_ssh.py standing in for /usr/bin/ssh (the guard refuses the real one and /usr/bin/open)."""

    def setUp(self):
        super().setUp()
        arc_support.write_settings(self.home, {"demo": False, "transport": "ssh",
                                               "ssh": {"username": arc_support.TEST_USERNAME}})
        arc_support.ready_session(self.home, points=13, simulated=False)
        self.program, tools = arc_support.fake_ssh_program(self)
        self.arc = Path(tempfile.mkdtemp(prefix="fake_arc_")).resolve()
        self.addCleanup(shutil.rmtree, self.arc, ignore_errors=True)
        self.addCleanup(arc_support.make_writable, self.arc)
        self.output = tools / "terminal.txt"
        self.base = self.arc / arc_support.ARC_FOLDER.lstrip("/")
        self.opened = []
        for patcher in (mock.patch.object(arc_commands, "setup_errors", return_value=[]),
                        mock.patch.object(arc_worker, "run_program", side_effect=self._open)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.upload_id = self.ok("arc-upload", "--session=" + SESSION)["upload"]["upload_id"]
        self.folder = self.record_dir(self.upload_id)

    def _open(self, argv, timeout, **options):
        """/usr/bin/open -a Terminal <script>: recorded; the test starts the Terminal worker itself."""
        self.assertEqual(list(argv[:3]), ["/usr/bin/open", "-a", "Terminal"])
        self.opened.append(Path(argv[3]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def terminal(self, stall=False, action="send", program=None, stdin=subprocess.DEVNULL):
        """The Terminal worker of the record's current attempt, in a process group of its own."""
        attempt = self.record(self.upload_id)["ssh"]["attempt"]
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        env["RAMAN_FAKE_SSH_ROOT"] = str(self.arc)
        if stall:
            # ssh passes on the first 60000 bytes, then hangs: the upload stays in "transferring".
            env["RAMAN_FAKE_SSH_STALL_AFTER"] = "60000"
        output = open(self.output, "ab")
        self.addCleanup(output.close)
        process = subprocess.Popen(
            [sys.executable, "-c", arc_support.TERMINAL_DRIVER, str(program or self.program), "--home",
             str(self.home), "--upload", self.upload_id, "--action", action, "--terminal", "--attempt", str(attempt)],
            cwd=str(self.home), env=env, stdin=stdin, stdout=output, stderr=output, start_new_session=True)
        self.addCleanup(self.end_group, process)
        return process

    @staticmethod
    def end_group(process):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)

    def transferring(self, process):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            if self.record(self.upload_id)["state"] == "transferring":
                return
            time.sleep(0.02)
        self.fail("the Terminal worker never sent anything: %s" % self.output.read_text(errors="replace"))

    def gone(self, timeout=15):
        """Wait until no process of this upload runs (the remote script that fake ssh started lives in a
        session of its own, and ends once its stream does)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            listing = subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True).stdout
            if self.upload_id not in listing:
                return
            time.sleep(0.05)
        self.fail("a process of upload %s still runs" % self.upload_id)

    def retried(self):
        answer = self.ok("arc-retry", "--upload=" + self.upload_id)
        record = self.record(self.upload_id)
        self.assertEqual((answer["worker"], record["state"], record["ssh"]["attempt"]),
                         ("terminal", "waiting_for_terminal", 2))
        self.assertEqual(len(self.opened), 2)
        process = self.terminal()
        self.assertEqual(process.wait(60), arc_worker.EXIT_VERIFIED, self.output.read_text(errors="replace"))
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), [SESSION, "_incoming"])
        self.assertEqual(os.listdir(self.base / "_incoming"), [])
        manifest = arc_support.same_as_manifest(self, self.base / SESSION)
        self.assertEqual(manifest["upload_id"], self.upload_id)
        self.assertFalse((self.folder / arc_records.PAYLOAD_FOLDER).exists())
        return record

    def test_row_15_terminal_window_closed(self):
        self.assertEqual(self.record(self.upload_id)["state"], "waiting_for_terminal")
        process = self.terminal(stall=True)
        self.transferring(process)
        # Closing a Terminal window hangs up the processes it runs: the worker and ssh.
        os.killpg(process.pid, signal.SIGHUP)
        self.assertEqual(process.wait(30), arc_worker.EXIT_ENDED)
        self.gone()
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "terminal_closed", True))
        self.assertEqual(record["message"], "The Terminal window was closed before the upload finished.")
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["actions"], ["retry", "check"])
        self.retried()

    def test_row_15_terminal_worker_ended_before_it_could_write(self):
        process = self.terminal(stall=True)
        self.transferring(process)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(30)
        self.gone()
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["state"], shown["display_state"], shown["actions"]),
                         ("transferring", "interrupted", ["retry", "check", "stop"]))
        self.retried()

    def test_closing_the_window_of_a_check_leaves_the_upload_as_it_was(self):
        # SSH-03 and I8: a check's Terminal window closed at the password prompt: the upload stays failed with
        # its own reason and message (before the fix it became failed / terminal_closed).
        process = self.terminal(stall=True)
        self.transferring(process)
        os.killpg(process.pid, signal.SIGHUP)
        process.wait(30)
        self.gone()
        before = self.record(self.upload_id)
        self.assertEqual((before["state"], before["error"]["reason"]), ("failed", "terminal_closed"))
        arc_records.update_record(self.folder, lambda current: current.update(
            message="The SSH connection ended during the upload.", error=arc_records.worker_error(
                "ssh_interrupted", incoming="/x")))
        before = self.record(self.upload_id)
        self.ok("arc-check", "--upload=" + self.upload_id)
        # ssh that waits at its password prompt until the window is closed.
        slow = self.program.with_name("slow-ssh")
        slow.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
        os.chmod(slow, 0o755)
        process = self.terminal(action="check", program=slow)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and (self.record(self.upload_id).get("worker") or {}).get("pid") is None:
            time.sleep(0.02)
        time.sleep(0.3)
        os.killpg(process.pid, signal.SIGHUP)
        self.assertEqual(process.wait(30), arc_worker.EXIT_ENDED)
        after = self.record(self.upload_id)
        self.assertEqual((after["state"], after["error"], after["message"]),
                         (before["state"], before["error"], before["message"]))
        self.assertIsNone(after["worker"])
        self.assertIn(arc_records.CHECK_STOPPED_TERMINAL, self.output.read_text(errors="replace"))

    def test_a_return_typed_during_the_upload_does_not_close_the_window(self):
        # SSH-07 and I9: what was typed while the upload ran is dropped before the last lines, so the script's
        # "Press Return to close this window" waits for a Return typed after the result.
        import pty
        import select

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        os.write(master, b"\n")
        self.assertEqual(select.select([slave], [], [], 1)[0], [slave])
        process = self.terminal(stdin=slave)
        self.assertEqual(process.wait(60), arc_worker.EXIT_VERIFIED, self.output.read_text(errors="replace"))
        self.assertEqual(select.select([slave], [], [], 0.5)[0], [])
        self.assertIn("Done: all", self.output.read_text(errors="replace"))


# ---------------------------------------------------------------------- row 16: the window closed


class WindowClosed(RecoveryCase):
    @staticmethod
    def end_group(process):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)

    def test_row_16_window_closed_during_the_transfer(self):
        # A stand-in for the window: it runs arc-upload as its child, as the window's QProcess does, and
        # stays open until it is closed with its whole process group.
        window = ("import subprocess, sys, time\n"
                  "subprocess.run([sys.executable, '-m', 'raman_catalog', '--home', sys.argv[1], 'arc-upload',\n"
                  "                '--session=' + sys.argv[2], '--demo', '--transport=globus'],\n"
                  "               capture_output=True, timeout=60)\n"
                  "print('asked', flush=True)\n"
                  "time.sleep(120)\n")
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        env["RAMAN_ARC_DEMO_PACE"] = "0.1"
        process = subprocess.Popen([sys.executable, "-c", window, str(self.home), SESSION], cwd=str(self.home),
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.addCleanup(process.stdout.close)
        self.addCleanup(self.end_group, process)
        self.assertEqual(process.stdout.readline().strip(), "asked")
        (item,) = self.ok("arc-list")["items"]
        self.take(item["upload_id"])
        self.wait_for("transferring")

        for number in (signal.SIGHUP, signal.SIGKILL):
            # (A process group whose processes have all ended answers EPERM on macOS.)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, number)
        process.wait(30)
        # The worker has a session of its own: it runs on without any window.
        self.assertTrue(arc_records.worker_alive(self.folder))
        self.settled()
        self.assert_on_arc(tasks=1, requests=1)


# ---------------------------------------------------------------------- rows 17 and 18: the session changes


class SessionChanges(RecoveryCase):
    def test_row_17_a_file_that_keeps_changing_is_not_sent_then_its_final_state_is(self):
        # The window holds controller.lock and acquires nothing: the 60-second rule is skipped, so the
        # snapshot itself meets the change.
        (self.home / "controller.lock").write_text("%d\n" % os.getpid())
        self.start("globus", "--controller-idle")
        self.assertIs(self.record(self.upload_id)["controller_idle_used"], True)
        point = self.session["folder"] / "point_0005.json"
        text = point.read_text(encoding="utf-8")
        stop = threading.Event()

        def keep_writing():
            count = 0
            while not stop.is_set():
                count += 1
                point.write_text(text + " " * (count % 7), encoding="utf-8")
                time.sleep(0.001)

        writer = threading.Thread(target=keep_writing, daemon=True)
        writer.start()
        try:
            code = self.worker()
        finally:
            stop.set()
            writer.join(10)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "changed_during_copy", True))
        self.assertEqual(record["message"], "Demo: the session's files kept changing while they were copied. Nothing "
                                            "was sent.")
        self.assertEqual(self.snapshots(), [])
        self.assertFalse(self.base.exists() and any(self.base.iterdir()))
        self.assertEqual(self.shown()["actions"], ["retry"])

        final = text.replace("\n", "\n ", 1)
        point.write_text(final, encoding="utf-8")
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        self.assert_on_arc(tasks=1, requests=1)
        self.assertEqual((self.base / SESSION / "point_0005.json").read_text(encoding="utf-8"), final)

    def test_row_17_a_file_changed_while_copied_is_copied_again(self):
        self.start("globus")
        changed = []
        real_copy = arc_payload._copy_file

        def copy_file(source, target, hooks=None, **options):
            if Path(source).name == "point_0005.json":
                changed.append(source)
                if len(changed) == 1:
                    def after_read(path):
                        Path(path).write_text(Path(path).read_text(encoding="utf-8") + "\n", encoding="utf-8")

                    hooks = {"after_read": after_read}
            return real_copy(source, target, hooks, **options)

        with mock.patch.object(arc_payload, "_copy_file", copy_file), \
                mock.patch.object(arc_payload, "RETRY_PAUSE_SECONDS", 0), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arc_worker.run_worker(self.home, self.upload_id, "send"), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(changed), 2)
        record = self.assert_on_arc(tasks=1, requests=1)
        final = (self.session["folder"] / "point_0005.json").read_bytes()
        self.assertEqual((self.base / SESSION / "point_0005.json").read_bytes(), final)
        self.assertEqual(record["warnings"], [])

    def test_row_18_a_file_changed_after_the_snapshot_is_named_in_a_warning(self):
        self.start("globus")
        self.assertEqual(self.worker(fault="snapshot_before_record"), arc_worker.EXIT_FAULT)
        manifest = json.loads((self.folder / arc_records.PAYLOAD_FOLDER / SESSION /
                               arc_records.MANIFEST_NAME).read_text(encoding="utf-8"))
        point = self.session["folder"] / "point_0003.json"
        sent = point.read_bytes()
        point.write_bytes(sent + b"\n")

        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc(tasks=1, requests=1)
        # The upload carries the snapshot, not the file as it is now.
        self.assertEqual((self.base / SESSION / "point_0003.json").read_bytes(), sent)
        # A demo upload: the copy is in the demo folder on this Mac (review COR-05).
        self.assertEqual(record["warnings"], [
            "The session folder on this Mac changed after it was copied for this upload (point_0003.json). The demo "
            "folder has the copy from %s." % manifest["made_utc"]])
        self.assertEqual(self.shown()["warnings"], record["warnings"])

    def test_row_18_cancel_txt_written_after_the_upload_is_a_note(self):
        session = arc_support.ready_session(self.home, folder=OTHER_SESSION, points=13)
        self.start("ssh", session=OTHER_SESSION)
        self.assertEqual(self.worker(fault="snapshot_before_record"), arc_worker.EXIT_FAULT)
        (session["folder"] / "cancel.txt").write_text("cancel\n", encoding="utf-8")

        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_on_arc()
        self.assertFalse((self.base / OTHER_SESSION / "cancel.txt").exists())
        self.assertEqual(record["warnings"], [])
        self.assertIn("cancel.txt was written after the upload (closing the window or pressing Stop writes it); the "
                      "scan data in the demo folder is complete.", record["notes"])


# ---------------------------------------------------------------------- rows 19 and 20: two at once


class TwoAtOnce(RecoveryCase):
    def test_row_19_two_uploads_at_once_make_one(self):
        arc_records.ensure_uploads_folder(self.paths)
        holder = arc_support.hold_lock(self, arc_records.uploads_folder(self.paths) / arc_records.REQUESTS_LOCK)
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        # A paced upload: the first is still under way when the second looks.
        env["RAMAN_ARC_DEMO_PACE"] = "0.1"
        argv = [sys.executable, "-m", "raman_catalog", "--home", str(self.home), "arc-upload", "--session=" + SESSION,
                "--demo", "--transport=globus"]
        clicks = [subprocess.Popen(argv, cwd=str(self.home), env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) for _ in range(2)]
        for click in clicks:
            self.addCleanup(lambda click=click: click.poll() is None and click.kill())
        # Both wait for requests.lock, then take it one after the other.
        time.sleep(0.5)
        holder.kill()
        holder.wait()
        answers = [json.loads(click.communicate(timeout=30)[0]) for click in clicks]
        self.assertEqual([click.returncode for click in clicks], [0, 0], answers)
        self.assertEqual(sorted(answer["existing"] for answer in answers), [False, True])
        (upload_id,) = {answer["upload"]["upload_id"] for answer in answers}
        self.take(upload_id)
        later = [answer for answer in answers if answer["existing"]][0]
        self.assertEqual(later["worker"], None)
        self.settled()
        self.assert_on_arc(tasks=1, requests=1)
        self.assertEqual(self.ok("arc-list")["total"], 1)

    def test_row_20_two_workers_for_one_upload(self):
        self.start("globus")
        first = self.worker_process(pace=0.15)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and (self.record(self.upload_id).get("worker") or {}).get("pid") != first.pid:
            time.sleep(0.02)
        self.assertEqual(self.record(self.upload_id)["worker"]["pid"], first.pid)
        # The second worker finds worker.lock held and leaves the upload alone.
        self.assertEqual(self.worker(), arc_worker.EXIT_NOT_ITS_TURN)
        self.assertEqual(self.record(self.upload_id)["worker"]["pid"], first.pid)
        self.assertEqual(first.wait(60), arc_worker.EXIT_VERIFIED)
        self.assert_on_arc(tasks=1, requests=1)


if __name__ == "__main__":
    import unittest

    unittest.main()
