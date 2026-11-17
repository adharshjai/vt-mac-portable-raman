"""Demo uploads end to end (PROTOCOL.md 5.6; design sections 4.8, 12 and 19.1).

Every command runs as the controller runs it: python -m raman_catalog --home HOME arc-..., a process of
its own, and the worker it starts is a real detached process. The demo folder HOME/data_catalog/arc_demo
stands in for ARC (its projects/... is ARC's /projects/...): the demo Globus service keeps its tasks on
disk, and demo SSH runs the real remote script with /bin/bash. The test guard is on
(RAMAN_ARC_TEST_GUARD=1), no socket may connect, and one test proves in a process of its own that the demo
never imports globus_sdk and never asks to run ssh, open, osascript or caffeinate.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time

import arc_support
import support
from arc_support import ARC_FOLDER, SESSION
from raman_catalog import arc_demo, arc_records, arc_ssh

REAL_SESSION = "1d_y_scan_20261113_120000_200"
UNKNOWN_SESSION = "single_20261113_130000_300"
MIXED_SESSION = "2d_scan_20261113_140000_400"
DARK = support.DARK_NAME
DARK_DESCRIPTION = DARK[:-len(".txt")] + ".json"
DEMO_TEXT = "Demo: %d files in the demo folder on this Mac, every one checked. Nothing left this Mac."
# Programs the demo must never ask for: they reach outside this Mac, open windows or keep it awake.
FAR_END_PROGRAMS = ("ssh", "scp", "sftp", "open", "osascript", "caffeinate")


def rewrite_simulated(folder, session=None, points=None):
    """Change the simulated flags a session's files record: None removes them (`...` keeps them)."""
    path = folder / "session.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if session is not ...:
        value.pop("simulated", None)
        if session is not None:
            value["simulated"] = {"backend": session, "motor": session}
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    for point in sorted(folder.glob("point_*.json")):
        value = json.loads(point.read_text(encoding="utf-8"))
        if points is not ...:
            value["capture"].pop("simulated", None)
            if points is not None:
                value["capture"]["simulated"] = {"backend": points, "motor": points}
        point.write_text(json.dumps(value, indent=4, sort_keys=True), encoding="utf-8")


class DemoCase(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        self.demo_root = self.home / "data_catalog" / "arc_demo"
        self.addCleanup(arc_support.wait_for_workers, self.home)

    def cli(self, *arguments, pace=None):
        """One command as the controller runs it: its answer (exit code 0 with ok, 1 without)."""
        extra = {"RAMAN_ARC_DEMO_PACE": str(pace)} if pace is not None else None
        code, answer, stderr = arc_support.run_cli(self.home, *arguments, extra_environment=extra)
        self.assertIsInstance(answer, dict, stderr)
        self.assertEqual(code, 0 if answer["ok"] else 2 if answer["kind"] == "usage" else 1, answer)
        return answer

    def settled(self, upload_id, after=None):
        """The record once the worker a command started has ended: it rests, no worker holds it, and (for
        a check, which leaves the state as it was) it was written after `after`."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            record = arc_support.wait_for_record(self.home, upload_id, timeout=max(0.1, deadline - time.monotonic()))
            if record["worker"] is None and (after is None or record["updated_utc"] > after):
                break
            time.sleep(0.05)
        self.assertIn(record["state"], arc_support.RESTING_STATES, record)
        self.assertIsNone(record["worker"], record)
        return record

    def send(self, session, transport, *options, pace=None):
        """arc-upload --demo, then its worker to the end: the record."""
        answer = self.cli("arc-upload", "--session=" + session, "--demo", "--transport=" + transport, *options,
                          pace=pace)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((answer["existing"], answer["worker"], answer["message"]),
                         (False, "detached", "Demo upload started. Nothing leaves this Mac."))
        return self.settled(answer["upload"]["upload_id"])

    def act(self, command, upload_id, pace=None):
        """arc-retry, arc-check or arc-keep-copy, then its worker to the end: the record."""
        answer = self.cli(command, "--upload=" + upload_id, pace=pace)
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["worker"], "detached")
        return self.settled(upload_id, after=answer["upload"]["updated_utc"])

    def shown(self, upload_id):
        return self.ok("arc-status", "--upload=" + upload_id)["upload"]

    def texts(self, upload_id):
        return [event["text"] for event in arc_records.read_events(self.record_dir(upload_id), last=0)]

    def tasks(self, upload_id):
        service = arc_demo.DemoGlobus(self.demo_root, pace=0)
        return [task for task in service.task_list(limit=1000) if upload_id in (task.get("label") or "")]

    def assert_verified(self, record, name=None, area="simulated"):
        """Verified into its own folder of the demo folder (layout 4.8), whose content equals the manifest."""
        self.assertEqual(record["state"], "verified", record.get("message"))
        name = name or record["session"]["folder"]
        final = arc_support.demo_base(self.home, area) / name
        manifest = arc_support.same_as_manifest(self, final)
        self.assertEqual(manifest["upload_id"], record["upload_id"])
        self.assertEqual(record["message"], DEMO_TEXT % record["payload"]["files"])
        destination = record["destination"]
        base = ARC_FOLDER + ("/simulated" if area == "simulated" else "")
        self.assertEqual((destination["final_path"], destination["placed_as"], destination["local_final_path"]),
                         (base + "/" + name, name, str(final)))
        # This upload's own folder in _incoming became the final folder.
        self.assertFalse((final.parent / "_incoming" / ("%s.%s" % (record["session"]["folder"],
                                                                   record["upload_id"]))).exists())
        self.assertFalse((self.record_dir(record["upload_id"]) / arc_records.PAYLOAD_FOLDER).exists())
        return final


class Destinations(DemoCase):
    def test_both_transports_land_in_one_folder_as_on_arc(self):
        arc_support.ready_session(self.home, points=13, simulated=False)
        arc_support.ready_session(self.home, folder=REAL_SESSION, points=13, simulated=False, scan_mode="1d-y")
        before = self.home_files()
        by_globus = self.send(SESSION, "globus")
        by_ssh = self.send(REAL_SESSION, "ssh")
        base = arc_support.demo_base(self.home, "real")
        self.assertEqual(base, self.demo_root / "projects" / "pcl_photonics" / "raman_globus_upload")
        self.assertEqual(sorted(os.listdir(base)), sorted([SESSION, REAL_SESSION, "_incoming"]))
        for record, method in ((by_globus, "globus-checksum"), (by_ssh, "sha256-on-arc")):
            final = self.assert_verified(record, area="real")
            self.assertEqual(record["verification"]["method"], method)
            points = ["point_%04d.json" % number for number in range(1, 14)]
            self.assertEqual(sorted(arc_support.files_below(final)), sorted(
                points + ["session.json", "dark/" + DARK, "dark/" + DARK_DESCRIPTION, arc_records.MANIFEST_NAME,
                          arc_records.SUMS_NAME]))
        # Nothing outside the demo folder and the record folders was written.
        self.assertEqual(self.home_files(), before)
        items = {item["folder"]: item for item in self.ok("arc-sessions")["items"]}
        for folder, record in ((SESSION, by_globus), (REAL_SESSION, by_ssh)):
            uploads = items[folder]["uploads"]
            self.assertIsNone(uploads["live"])
            self.assertEqual((uploads["demo"]["upload_id"], uploads["demo"]["display_label"],
                              uploads["demo"]["final_path"]),
                             (record["upload_id"], "On ARC, checked", ARC_FOLDER + "/" + folder))

    def home_files(self):
        """Every file of HOME except those of the uploads' records and the demo folder."""
        found = set()
        for directory, _folders, files in os.walk(self.home):
            relative = os.path.relpath(directory, self.home)
            if relative.startswith(("data_catalog/arc_uploads", "data_catalog/arc_demo")):
                continue
            found.update(os.path.join(relative, name) for name in files)
        return found

    def test_the_same_session_with_both_transports_goes_beside_itself(self):
        arc_support.ready_session(self.home, points=13)
        first = self.send(SESSION, "globus")
        self.assert_verified(first)
        answer = self.cli("arc-upload", "--session=" + SESSION, "--demo", "--transport=ssh")
        self.assertEqual((answer["kind"], answer["reason"], answer["upload"]["upload_id"]),
                         ("conflict", "already_on_arc", first["upload_id"]))
        second = self.send(SESSION, "ssh", "--new")
        self.assert_verified(second, name=SESSION + "_copy2")
        self.assertIn("It goes beside the earlier copy, under the first free name of %s, %s_copy2, %s_copy3 and so on."
                      % (SESSION, SESSION, SESSION), second["notes"])
        base = arc_support.demo_base(self.home)
        self.assertEqual(sorted(os.listdir(base)), sorted([SESSION, SESSION + "_copy2", "_incoming"]))
        arc_support.same_as_manifest(self, base / SESSION)

    def test_the_kind_of_a_session_chooses_its_folder(self):
        arc_support.ready_session(self.home, points=13)
        unknown = arc_support.ready_session(self.home, folder=UNKNOWN_SESSION, points=1, scan_mode="single")
        rewrite_simulated(unknown["folder"], None, None)
        mixed = arc_support.ready_session(self.home, folder=MIXED_SESSION, points=4, scan_mode="2d")
        rewrite_simulated(mixed["folder"], False, ...)
        arc_support.set_age(unknown["folder"], 600)
        arc_support.set_age(mixed["folder"], 600)

        simulated = self.send(SESSION, "globus")
        self.assert_verified(simulated, area="simulated")
        self.assertIn("This session was made in simulation. It goes into the separate folder simulated/.",
                      simulated["notes"])
        # Not recorded: handled like a simulated session, also in the demo.
        handled = self.send(UNKNOWN_SESSION, "ssh")
        self.assert_verified(handled, area="simulated")
        self.assertEqual((handled["session"]["kind"], handled["destination"]["area"]), ("unknown", "simulated"))
        self.assertIn("It is not recorded whether this session was made in simulation, so it goes into the "
                      "separate folder simulated/.", handled["notes"])
        # Declared real by the uploader: the measured folder, and the manifest says who decided.
        declared = self.send(UNKNOWN_SESSION, "globus", "--declare-real")
        final = self.assert_verified(declared, area="real")
        manifest = json.loads((final / arc_records.MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual((manifest["session"]["kind"], manifest["session"]["kind_declared_by_uploader"]),
                         ("unknown", True))
        self.assertEqual(manifest["destination"], {"arc_folder": ARC_FOLDER, "area": "real"})
        # Some points simulated and some not: refused for the demo as well.
        for transport in ("globus", "ssh"):
            answer = self.cli("arc-upload", "--session=" + MIXED_SESSION, "--demo", "--transport=" + transport)
            self.assertEqual((answer["kind"], answer["reason"]), ("refused", "mixed"))
        answer = self.cli("arc-upload", "--session=" + SESSION, "--demo", "--declare-real")
        self.assertEqual((answer["kind"], answer["reason"]), ("refused", "declare_real_not_unknown"))

    def test_the_demo_folder_setting(self):
        arc_support.ready_session(self.home, points=13)
        self.assertTrue(self.cli("config", "--set", "arc_upload.demo_folder=demo_arc")["ok"])
        record = self.send(SESSION, "ssh")
        final = self.home / "demo_arc" / ARC_FOLDER.lstrip("/") / "simulated" / SESSION
        self.assertEqual(record["destination"]["local_final_path"], str(final))
        arc_support.same_as_manifest(self, final)
        self.assertFalse(self.demo_root.exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.home / "demo_arc").st_mode), 0o700)

    def test_records_and_the_demo_folder_are_this_users_only(self):
        arc_support.ready_session(self.home, points=13)
        by_globus = self.send(SESSION, "globus")
        by_ssh = self.send(SESSION, "ssh", "--new")
        uploads = arc_records.uploads_folder(self.paths)
        self.assertEqual(stat.S_IMODE(os.stat(uploads).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.demo_root).st_mode), 0o700)
        for record in (by_globus, by_ssh):
            folder = self.record_dir(record["upload_id"])
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
            for path in folder.iterdir():
                self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600, path.name)
            names = {path.name for path in folder.iterdir()}
            self.assertTrue({"record.json", "events.jsonl", "raman_upload_manifest.json", "SHA256SUMS",
                             "worker.log"} <= names, names)
            self.assertEqual("globus_request.json" in names, record["transport"] == "globus")
        # What the demo's remote script makes: files 0640, folders 0750 (umask 027), as on ARC.
        copy = arc_support.demo_base(self.home) / (SESSION + "_copy2")
        for directory, folders, files in os.walk(copy):
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o750, directory)
            for name in files:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode), 0o640, name)


class TeachingFaults(DemoCase):
    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home, points=13)

    def test_lose_submit_reply_asks_again_and_globus_keeps_one_task(self):
        record = self.send(SESSION, "globus", "--demo-fault=lose_submit_reply")
        self.assert_verified(record)
        globus = record["globus"]
        self.assertEqual([post["outcome"] for post in globus["posts"]], ["unknown", "duplicate"])
        (task,) = self.tasks(record["upload_id"])
        self.assertEqual((task["task_id"], task["submission_id"]), (globus["task_id"], globus["submission_id"]))
        self.assertEqual(arc_demo.DemoGlobus(self.demo_root, pace=0).posts_of(globus["submission_id"]), 2)
        texts = self.texts(record["upload_id"])
        self.assertLess(texts.index("Demo: it is not known whether Globus received the request; asking again with "
                                    "the same request number."),
                        texts.index("Demo: Globus answered: already received. One copy."))

    def test_fail_task_once_then_retry_copies_into_the_same_folder(self):
        record = self.send(SESSION, "globus", "--demo-fault=fail_task_once")
        upload_id = record["upload_id"]
        incoming = arc_support.demo_base(self.home) / "_incoming" / ("%s.%s" % (SESSION, upload_id))
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "task_failed", True))
        # COR-05: the files are in the demo folder on this Mac, not on ARC.
        self.assertEqual(record["message"], "Demo: Globus could not finish the copy: the connection to this Mac was "
                                            "lost. Files copied so far are in %s." % incoming)
        self.assertEqual("Demo: " + record["error"]["message"], record["message"])
        self.assertTrue(arc_support.files_below(incoming))
        shown = self.shown(upload_id)
        self.assertEqual((shown["actions"], shown["leftover_on_arc"]), (["retry", "check"], str(incoming)))
        record = self.act("arc-retry", upload_id)
        self.assert_verified(record)
        self.assertEqual((record["globus"]["attempt"], len(record["globus"]["earlier_attempts"])), (2, 1))
        self.assertEqual(len(self.tasks(upload_id)), 2)
        self.assertEqual(record["globus"]["demo_fault_used"], "fail_task_once")

    def test_drop_connection_then_retry_finishes_the_upload(self):
        record = self.send(SESSION, "ssh", "--demo-fault=drop_connection")
        upload_id = record["upload_id"]
        incoming = arc_support.demo_base(self.home) / "_incoming" / ("%s.%s" % (SESSION, upload_id))
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "ssh_interrupted"))
        self.assertEqual(record["message"], "Demo: the SSH connection ended during the upload. Files sent so far are "
                                            "in %s." % incoming)
        self.assertEqual(record["error"]["fix"], "Press Retry same request: all files are sent again, into the same "
                                                 "folder.")
        self.assertTrue(arc_support.files_below(incoming))
        self.assertFalse((arc_support.demo_base(self.home) / SESSION).exists())
        record = self.act("arc-retry", upload_id)
        self.assert_verified(record)
        self.assertEqual(self.texts(upload_id).count(arc_ssh.DROPPED), 1)
        self.assertIs(record["ssh"]["demo_fault_used"], True)

    def test_a_teaching_fault_needs_a_demo_upload(self):
        answer = self.cli("arc-upload", "--session=" + SESSION, "--live", "--demo-fault=fail_task_once")
        self.assertEqual((answer["kind"], answer["reason"]), ("usage", "usage"))


class CheckAndRetry(DemoCase):
    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home, points=13)

    def test_check_status_of_verified_uploads(self):
        by_globus = self.send(SESSION, "globus")
        by_ssh = self.send(SESSION, "ssh", "--new")
        files = by_globus["payload"]["files"]
        checked = self.act("arc-check", by_globus["upload_id"])
        self.assertEqual((checked["state"], checked["checks"][-1]["ok"], checked["checks"][-1]["message"]),
                         ("verified", True, "Demo: still in the demo folder: %d files with the right sizes." % files))
        checked = self.act("arc-check", by_ssh["upload_id"])
        self.assertEqual((checked["state"], checked["checks"][-1]["ok"], checked["checks"][-1]["message"]),
                         ("verified", True, "Demo: still in the demo folder: %d files, every one matching this "
                                            "Mac's list (SHA-256)." % files))
        self.assertEqual(self.shown(by_ssh["upload_id"])["last_check"], checked["checks"][-1])
        # A file gone from the folder: the check says so, and the upload stays on record as verified.
        base = arc_support.demo_base(self.home)
        for name in (SESSION, SESSION + "_copy2"):
            (base / name / "point_0007.json").unlink()
        checked = self.act("arc-check", by_globus["upload_id"])
        self.assertEqual((checked["state"], checked["checks"][-1]["ok"]), ("verified", False))
        self.assertIn("point_0007.json", checked["checks"][-1]["message"])
        # COR-05: the folder is in the demo folder on this Mac, not on ARC.
        self.assertEqual(checked["checks"][-1]["message"], "Demo: the folder in the demo folder differs from this "
                                                           "upload: 1 differ, for example point_0007.json.")
        checked = self.act("arc-check", by_ssh["upload_id"])
        self.assertEqual((checked["state"], checked["checks"][-1]["ok"]), ("verified", False))
        self.assertIn("point_0007.json", checked["checks"][-1]["message"])
        self.assertEqual(checked["checks"][-1]["message"],
                         "Demo: a copy of this upload is in the demo folder (%s), but it does not match this Mac's "
                         "list (for example point_0007.json)." % (base / (SESSION + "_copy2")))
        self.assertEqual(checked["message"], DEMO_TEXT % files)
        # Gone from the demo folder: each check says so, in the demo's words; both looked (REG-01).
        for name in (SESSION, SESSION + "_copy2"):
            arc_support.make_writable(base / name)
            shutil.rmtree(base / name)
        checked = self.act("arc-check", by_globus["upload_id"])
        self.assertEqual((checked["checks"][-1]["looked"], checked["checks"][-1]["message"]),
                         (True, "Demo: the folder %s is no longer in the demo folder." % (base / SESSION)))
        checked = self.act("arc-check", by_ssh["upload_id"])
        self.assertEqual((checked["checks"][-1]["looked"], checked["checks"][-1]["message"]),
                         (True, "Demo: nothing of this upload is in the demo folder."))

    def test_stop_check_and_retry_while_copying(self):
        # Two paced uploads at once, one per transport, stopped while they copy.
        other = arc_support.ready_session(self.home, folder=REAL_SESSION, points=13, scan_mode="1d-y")
        self.assertTrue(other["folder"].is_dir())
        uploads = {}
        for session, transport in ((SESSION, "globus"), (REAL_SESSION, "ssh")):
            answer = self.cli("arc-upload", "--session=" + session, "--demo", "--transport=" + transport, pace=0.2)
            uploads[transport] = answer["upload"]["upload_id"]
        for transport, upload_id in uploads.items():
            deadline = time.monotonic() + 30
            while self.record(upload_id)["state"] != "transferring" and time.monotonic() < deadline:
                time.sleep(0.02)
            answer = self.cli("arc-stop", "--upload=" + upload_id)
            self.assertEqual((answer["stopped_now"], answer["worker"], answer["message"]),
                             (False, None, "The upload program stops the upload within a second."))
        for transport, upload_id in uploads.items():
            record = self.settled(upload_id)
            session = record["session"]["folder"]
            incoming = arc_support.demo_base(self.home) / "_incoming" / ("%s.%s" % (session, upload_id))
            self.assertEqual(record["state"], "stopped", record["message"])
            # COR-05: the demo's copy is in the demo folder on this Mac, not on ARC.
            self.assertIn(str(incoming), record["message"])
            self.assertTrue(record["message"].startswith("Demo: stopped."), record["message"])
            if transport == "ssh":
                self.assertEqual(record["message"], "Demo: stopped. Files already copied stay in %s; nothing was "
                                                    "deleted." % incoming)
            self.assertEqual(self.shown(upload_id)["actions"], ["retry", "check"])
            self.assertFalse((arc_support.demo_base(self.home) / session).exists())
            # Check status changes nothing and says what is in the demo folder.
            record = self.act("arc-check", upload_id)
            self.assertEqual(record["state"], "stopped")
            if transport == "ssh":
                self.assertEqual(record["message"], "Demo: part of the copy is in _incoming of the demo folder. Retry "
                                                    "same request finishes it.")
            record = self.act("arc-retry", upload_id)
            self.assert_verified(record)

    def test_conflicts_keep_as_new_copy_stop_and_retry(self):
        other = arc_support.ready_session(self.home, folder=REAL_SESSION, points=13, scan_mode="1d-y")
        self.assertTrue(other["folder"].is_dir())
        base = arc_support.demo_base(self.home)
        # Someone else's folder has the session's name: Globus finds it while checking. (The demo folder is
        # made as the demo makes it, with its mark: a folder that holds other files is never used, I6.)
        arc_records.ensure_demo_root(self.demo_root)
        (base / SESSION).mkdir(parents=True)
        (base / SESSION / "kept.txt").write_text("not this upload's\n", encoding="utf-8")
        record = self.send(SESSION, "globus")
        upload_id = record["upload_id"]
        self.assertEqual((record["state"], record["conflict_at"]), ("conflict", "checking"))
        self.assertEqual(record["message"], "Demo: the demo folder already has a folder named %s in %s. Nothing was "
                                            "sent." % (SESSION, base))
        shown = self.shown(upload_id)
        self.assertEqual((shown["display_label"], shown["actions"], shown["needs"]),
                         ("Name already on ARC", ["keep_copy", "retry", "stop"], "decision"))
        record = self.act("arc-keep-copy", upload_id)
        self.assert_verified(record, name=SESSION + "_copy2")
        self.assertEqual(os.listdir(base / SESSION), ["kept.txt"])

        # Over SSH the name is taken when the copy is complete: stopping keeps the checked copy in _incoming.
        (base / REAL_SESSION).mkdir()
        record = self.send(REAL_SESSION, "ssh")
        upload_id = record["upload_id"]
        incoming = base / "_incoming" / ("%s.%s" % (REAL_SESSION, upload_id))
        self.assertEqual((record["state"], record["conflict_at"]), ("conflict", "placing"))
        self.assertEqual(record["message"], "Demo: your copy is complete and checked in %s. Keep it as a new copy, or "
                                            "move the other folder and press Retry same request." % incoming)
        answer = self.cli("arc-stop", "--upload=" + upload_id)
        self.assertEqual((answer["stopped_now"], answer["worker"], answer["message"]), (
            True, None, "Demo: stopped. Files already copied stay in %s; nothing was deleted." % incoming))
        arc_support.same_as_manifest(self, incoming)
        # The other folder is moved away by hand; Retry same request puts the copy in place.
        (base / REAL_SESSION).rename(base / (REAL_SESSION + "_moved"))
        record = self.act("arc-retry", upload_id)
        self.assert_verified(record)
        self.assertEqual(record["ssh"]["runs"][-1]["outcome"], "placed")

    def test_a_globus_conflict_at_placing_is_stopped_at_once(self):
        answer = self.cli("arc-upload", "--session=" + SESSION, "--demo", "--transport=globus", pace=0.1)
        upload_id = answer["upload"]["upload_id"]
        deadline = time.monotonic() + 30
        while self.record(upload_id)["state"] != "transferring" and time.monotonic() < deadline:
            time.sleep(0.02)
        base = arc_support.demo_base(self.home)
        (base / SESSION).mkdir()
        record = self.settled(upload_id)
        incoming = base / "_incoming" / ("%s.%s" % (SESSION, upload_id))
        self.assertEqual((record["state"], record["conflict_at"]), ("conflict", "placing"))
        self.assertEqual(record["message"], "Demo: your copy is complete and checked in %s. Keep it as a new copy, or "
                                            "move the other folder and press Retry same request." % incoming)
        # Nothing runs on Globus any more: no stop worker, no sign-in.
        answer = self.cli("arc-stop", "--upload=" + upload_id)
        self.assertEqual((answer["stopped_now"], answer["worker"], answer["message"]), (
            True, None, "Demo: stopped. Files already copied stay in %s; nothing was deleted." % incoming))
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["conflict_at"], record["error"]), ("stopped", None, None))
        self.assertEqual(self.shown(upload_id)["actions"], ["retry", "check"])
        arc_support.same_as_manifest(self, incoming)
        self.assertEqual(os.listdir(base / SESSION), [])


class DemoWords(DemoCase):
    """Reviews COR-05 and R2-05: a demo upload's copy is in the demo folder on this Mac, never on ARC. No text a
    person reads of a demo upload says "on ARC": its history, message, error and fix, warnings, notes, and the
    results and fixes of its checks, also of checks that could not look."""

    ON_ARC = re.compile(r"\bon ARC\b", re.IGNORECASE)

    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home, points=13)
        other = arc_support.ready_session(self.home, folder=REAL_SESSION, points=13, scan_mode="1d-y")
        self.assertTrue(other["folder"].is_dir())

    def words(self, upload_id):
        """Every text of the upload that a person reads, with where it is."""
        record = self.record(upload_id)
        shown = self.shown(upload_id)
        error = record.get("error") or {}
        found = [("event", text) for text in self.texts(upload_id)]
        found += [("message", record.get("message")), ("listed message", shown["message"]),
                  ("error", error.get("message")), ("fix", error.get("fix"))]
        found += [("warning", text) for text in record.get("warnings") or []]
        found += [("note", text) for text in record.get("notes") or []]
        for check in record.get("checks") or []:
            found += [("check", check.get("message")), ("check fix", check.get("fix"))]
        return [(where, text) for where, text in found if text]

    def look_without(self, upload_id, change):
        """Check status while `change` keeps the check from looking (it gives back what undoes it): a note."""
        undo = change()
        try:
            record = self.act("arc-check", upload_id)
        finally:
            undo()
        self.assertIs(record["checks"][-1]["looked"], False, record["checks"][-1])
        return record

    def test_no_text_of_a_demo_upload_says_on_arc(self):
        by_globus = self.send(SESSION, "globus")
        by_ssh = self.send(SESSION, "ssh", "--new")
        files = by_globus["payload"]["files"]
        texts = self.texts(by_globus["upload_id"])
        incoming = arc_support.demo_base(self.home) / "_incoming" / ("%s.%s" % (SESSION, by_globus["upload_id"]))
        self.assertIn("Demo: the folders in the demo folder are ready: %s." % incoming, texts)
        self.assertIn("Demo: checking the files in the demo folder (SHA-256)...", self.texts(by_ssh["upload_id"]))
        self.assertEqual(self.act("arc-check", by_globus["upload_id"])["checks"][-1]["message"],
                         "Demo: still in the demo folder: %d files with the right sizes." % files)
        self.assertTrue(self.act("arc-check", by_ssh["upload_id"])["checks"][-1]["ok"])
        # An SSH upload whose name is taken waits complete in _incoming; stopped, then checked (STAGED complete).
        base = arc_support.demo_base(self.home)
        (base / REAL_SESSION).mkdir()
        staged = self.send(REAL_SESSION, "ssh")["upload_id"]
        self.assertTrue(self.cli("arc-stop", "--upload=" + staged)["stopped_now"])
        self.assertEqual(self.act("arc-check", staged)["message"],
                         "Demo: the copy is complete in _incoming of the demo folder but not in place yet. Retry same "
                         "request puts it in place.")
        # Checks that could not look (reviews R2-03, R2-06): a copy that may not be read, by Globus and over SSH, and
        # a folder on the way to the copy that became a link.
        final = base / SESSION

        def unreadable(folder):
            def change():
                kept = stat.S_IMODE(folder.stat().st_mode)
                os.chmod(folder, 0)
                return lambda: os.chmod(folder, kept)
            return change

        def linked():
            moved = base.parent / "simulated_moved"
            base.rename(moved)
            os.symlink(moved, base)
            return lambda: (os.remove(base), moved.rename(base))

        self.look_without(by_globus["upload_id"], unreadable(final))
        self.look_without(by_ssh["upload_id"], unreadable(base / (SESSION + "_copy2")))
        self.look_without(by_ssh["upload_id"], linked)
        for upload_id in (by_globus["upload_id"], by_ssh["upload_id"], staged):
            words = self.words(upload_id)
            self.assertGreater(len(words), 10)
            for where, text in words:
                with self.subTest(upload=upload_id, where=where):
                    self.assertIsNone(self.ON_ARC.search(text), text)


# The demo, end to end in one process of its own: which modules it imports and which programs it asks for.
NO_FAR_END = r'''
import json
import re
import subprocess
import sys
from pathlib import Path

started = []
_Popen = subprocess.Popen


class Recorded(_Popen):
    def __init__(self, args, *rest, **options):
        argv = [args] if isinstance(args, (str, bytes)) else list(args)
        started.append([str(item) for item in argv])
        super().__init__(args, *rest, **options)


subprocess.Popen = Recorded

from raman_catalog import arc_worker, cli  # noqa: E402

asked = []
real_check = arc_worker.check_program


def check_program(argv):
    asked.append([str(item) for item in argv])
    return real_check(argv)


arc_worker.check_program = check_program
spawned = []
arc_worker.spawn = lambda home, upload_id, action: spawned.append(action)
home, session = Path(sys.argv[1]), sys.argv[2]


def command(*argv):
    with cli.Context(home) as ctx:
        return cli.invoke(ctx, list(argv))


codes, answers = [], []
for transport, fault in (("globus", "lose_submit_reply"), ("globus", "fail_task_once"), ("ssh", "drop_connection")):
    answer = command("arc-upload", "--session=" + session, "--demo", "--transport=" + transport, "--new",
                     "--demo-fault=" + fault)
    answers.append(answer.get("ok"))
    upload = answer["upload"]["upload_id"]
    codes.append(arc_worker.run_worker(home, upload, "send"))
    if command("arc-status", "--upload=" + upload)["upload"]["state"] == "failed":
        answers.append(command("arc-retry", "--upload=" + upload).get("ok"))
        codes.append(arc_worker.run_worker(home, upload, "send"))
    answers.append(command("arc-check", "--upload=" + upload).get("ok"))
    codes.append(arc_worker.run_worker(home, upload, "check"))
for name in ("arc-sessions", "arc-list", "arc-doctor"):
    answers.append(command(name).get("ok"))
print(json.dumps({"modules": sorted(name for name in sys.modules if name.split(".")[0] == "globus_sdk"),
                  "started": started, "asked": asked, "codes": codes, "answers": answers, "spawned": spawned}))
'''


class NoFarEnd(DemoCase):
    def test_the_demo_never_imports_globus_sdk_and_never_asks_for_ssh_open_or_osascript(self):
        arc_support.ready_session(self.home, points=13)
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        done = subprocess.run([sys.executable, "-c", NO_FAR_END, str(self.home), SESSION], capture_output=True,
                              text=True, timeout=120, cwd=str(self.home), env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        found = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(found["modules"], [])
        self.assertEqual(found["codes"], [0, 0, 1, 0, 0, 1, 0, 0])
        self.assertTrue(all(found["answers"]), found["answers"])
        self.assertEqual(found["spawned"], ["send", "check", "send", "send", "check", "send", "send", "check"])
        for argv in found["started"] + found["asked"]:
            self.assertNotIn(os.path.basename(argv[0]), FAR_END_PROGRAMS, argv)
        # The only program the demo asks to run is the remote script under /bin/bash; ps lists programs.
        self.assertEqual({argv[0] for argv in found["asked"]}, {"/bin/bash"})
        self.assertEqual({argv[0] for argv in found["started"]}, {"/bin/bash", "/bin/ps"})
        base = arc_support.demo_base(self.home)
        self.assertEqual(sorted(os.listdir(base)), sorted([SESSION, SESSION + "_copy2", SESSION + "_copy3",
                                                           "_incoming"]))
        for name in (SESSION, SESSION + "_copy2", SESSION + "_copy3"):
            arc_support.same_as_manifest(self, base / name)


if __name__ == "__main__":
    import unittest

    unittest.main()
