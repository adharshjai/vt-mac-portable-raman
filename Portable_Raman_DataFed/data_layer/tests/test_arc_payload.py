"""The snapshot an upload sends: copy, hashes, manifest, SHA256SUMS, change detection, deletion."""

import getpass
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

import arc_support
import support
from raman_datafed import arc_payload, arc_records, arc_session, arc_worker
from raman_datafed.arc_records import ArcError
from raman_datafed.config import Paths

SESSION = arc_support.SESSION
Usage = namedtuple("Usage", "total used free")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SnapshotCase(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        self.session = arc_support.ready_session(self.home)
        self.folder = self.session["folder"]

    def new_upload(self, *options):
        upload_id = self.upload(*options)["upload"]["upload_id"]
        return upload_id, self.record_dir(upload_id)

    def check(self):
        return arc_session.check(self.paths, self.folder, 60, True, full=True, commands=[])

    def snapshot(self, upload_id, **options):
        options.setdefault("sleep", lambda _seconds: None)
        return arc_payload.snapshot(self.record_dir(upload_id), self.home, self.check(), self.record(upload_id),
                                    **options)

    def top(self, record_dir):
        return Path(record_dir) / "payload" / SESSION


class Copy(SnapshotCase):
    def test_the_snapshot_equals_the_session_with_its_dark(self):
        upload_id, record_dir = self.new_upload()
        summary = self.snapshot(upload_id)
        top = self.top(record_dir)
        for path in [self.folder / "session.json"] + self.session["files"]:
            copy = top / path.name
            self.assertEqual(copy.read_bytes(), path.read_bytes())
            self.assertEqual(copy.stat().st_mtime_ns, path.stat().st_mtime_ns)
        dark = self.home / "dark_measurements" / support.DARK_NAME
        self.assertEqual((top / "dark" / support.DARK_NAME).read_bytes(), dark.read_bytes())
        self.assertTrue((top / "dark" / dark.with_suffix(".json").name).is_file())
        names = sorted(path.relative_to(top).as_posix() for path in top.rglob("*") if path.is_file())
        self.assertEqual(len(names), 13 + 1 + 2 + 2)
        self.assertEqual(summary["files"], len(names))
        self.assertEqual(summary["bytes"], sum((top / name).stat().st_size for name in names))
        self.assertEqual(summary["manifest_sha256"], sha256(top / "raman_upload_manifest.json"))
        self.assertTrue(summary["kept"])
        # I12 (DOC-08): read-only files in folders this user may change, so that a kept copy can be deleted.
        for path in [top] + list(top.rglob("*")):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o700 if path.is_dir() else 0o400, path)
        self.assertFalse(list(Path(record_dir).glob("payload.tmp-*")))
        for name in ("raman_upload_manifest.json", "SHA256SUMS", "payload_hashes.json"):
            self.assertEqual(stat.S_IMODE((Path(record_dir) / name).stat().st_mode), 0o600, name)

    def test_md5_and_sha256_of_every_file(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        top = self.top(record_dir)
        digests = arc_payload.digests(record_dir)
        self.assertEqual(set(digests), {path.relative_to(top).as_posix() for path in top.rglob("*") if path.is_file()})
        for relative, entry in digests.items():
            data = (top / relative).read_bytes()
            self.assertEqual(entry, {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                     "md5": hashlib.md5(data).hexdigest()}, relative)

    def test_the_manifest(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        top = self.top(record_dir)
        text = (top / "raman_upload_manifest.json").read_text(encoding="utf-8")
        self.assertIn('\n  "upload_id": "%s",\n' % upload_id, text)
        manifest = json.loads(text)
        self.assertEqual(list(manifest), ["schema", "upload_id", "made_utc", "transport", "demo", "session", "darks",
                                          "files", "totals", "excluded", "adjusted", "destination", "software"])
        self.assertEqual(list(manifest["session"]), [
            "folder", "session_id", "scan_mode", "started_utc", "sample_id", "points", "points_planned", "end",
            "kind", "kind_declared_by_uploader", "simulated"])
        self.assertEqual(manifest["schema"], "raman-arc-upload-manifest/1")
        self.assertEqual((manifest["transport"], manifest["demo"]), ("globus", True))
        self.assertEqual(manifest["session"]["session_id"], self.session["session_id"])
        self.assertEqual((manifest["session"]["points"], manifest["session"]["points_planned"],
                          manifest["session"]["end"]), (13, 13, "finished"))
        self.assertEqual(manifest["session"]["simulated"], {"backend": True, "motor": True})
        self.assertEqual(manifest["darks"], [{
            "file": support.DARK_NAME, "kind": "simulated", "sent": True, "reason": None,
            "path": "dark/" + support.DARK_NAME, "description": "dark/dark_20261113_101400_001.json"}])
        paths = [entry["path"] for entry in manifest["files"]]
        self.assertEqual(paths, sorted(paths, key=lambda path: path.encode("utf-8")))
        self.assertNotIn("raman_upload_manifest.json", paths)
        self.assertNotIn("SHA256SUMS", paths)
        for entry in manifest["files"]:
            self.assertEqual((entry["bytes"], entry["sha256"]),
                             ((top / entry["path"]).stat().st_size, sha256(top / entry["path"])))
        files = [path for path in top.rglob("*") if path.is_file()]
        self.assertEqual(manifest["totals"], {"files": len(files), "bytes": sum(path.stat().st_size for path in files)})
        self.assertEqual(manifest["destination"], {"arc_folder": arc_support.ARC_FOLDER, "area": "simulated"})
        self.assertEqual(manifest["software"]["controller"], support.SOFTWARE)
        self.assertEqual(manifest["software"]["data_layer"]["name"], "raman_datafed")
        self.assertEqual(manifest["excluded"], [])
        self.assertEqual(manifest["adjusted"], [])
        self.assertEqual((top / "session.json").read_bytes(), (self.folder / "session.json").read_bytes())

    def test_the_manifest_names_no_local_path_account_host_or_collection(self):
        collection = "0b9e2d4c-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
        settings = arc_support.write_settings(self.home, {"globus": {"mac_collection": collection},
                                                          "ssh": {"username": arc_support.TEST_USERNAME}})
        self.assertTrue(settings.is_file())
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        text = (self.top(record_dir) / "raman_upload_manifest.json").read_text(encoding="utf-8")
        for forbidden in (str(self.home), os.path.realpath(self.home), str(Path.home()), "0b9e2d4c",
                          arc_support.TEST_USERNAME, "arc_uploads", upload_id + "/payload"):
            self.assertNotIn(forbidden, text)
        # Checked only when long enough not to be found inside an ordinary word or a checksum.
        for name in (getpass.getuser(), socket.gethostname()):
            if len(name) >= 6:
                self.assertNotIn(name, text)

    def test_sha256sums_lists_every_file_and_checks_with_shasum(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        top = self.top(record_dir)
        lines = (top / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        paths = [line.split("  ", 1)[1] for line in lines]
        expected = sorted((path.relative_to(top).as_posix() for path in top.rglob("*")
                           if path.is_file() and path.name != "SHA256SUMS"), key=lambda path: path.encode("utf-8"))
        self.assertEqual(paths, expected)
        self.assertIn("raman_upload_manifest.json", paths)
        for line in lines:
            digest, path = line.split("  ", 1)
            self.assertRegex(digest, "^[0-9a-f]{64}$")
            self.assertEqual(digest, sha256(top / path))
        self.assertTrue((top / "SHA256SUMS").read_bytes().endswith(b"\n"))
        if not Path("/usr/bin/shasum").exists():
            self.skipTest("shasum is not on this computer")
        done = subprocess.run(["/usr/bin/shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=str(top),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_what_is_left_out_is_listed_in_the_manifest(self):
        for name in (".DS_Store", "._session.json", ".point_0001.json.abc.tmp", "raman_upload_manifest.json",
                     "SHA256SUMS", "response.txt.Ab12Cd", "Thumbs.db"):
            (self.folder / name).write_text("x", encoding="utf-8")
        arc_support.set_age(self.folder, 600)
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        manifest = arc_payload.read_manifest(record_dir)
        excluded = {entry["path"]: entry["reason"] for entry in manifest["excluded"]}
        self.assertEqual(set(excluded), {".DS_Store", "._session.json", ".point_0001.json.abc.tmp",
                                         "raman_upload_manifest.json", "SHA256SUMS", "response.txt.Ab12Cd",
                                         "Thumbs.db"})
        top = self.top(record_dir)
        self.assertFalse((top / ".DS_Store").exists())
        self.assertFalse((top / "Thumbs.db").exists())
        self.assertNotEqual((top / "SHA256SUMS").read_text(encoding="utf-8"), "x")


class ImportedSession(SnapshotCase):
    """I11 (COR-02): an imported session's session.json names the folder it was imported from with this Mac's
    absolute path (and so the account name); the copy that goes to ARC keeps the folder's name only."""

    def imported(self, source):
        path = self.folder / "session.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["imported"] = {"utc": "2026-11-14T10:00:00.000Z", "source_folder": source}
        text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8")
        arc_support.set_age(self.folder, 600)
        return text

    def test_the_copy_names_the_source_folder_only(self):
        source = str(self.home / "scan_sessions" / SESSION)
        original = self.imported(source)
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        top = self.top(record_dir)
        sent = (top / "session.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(sent)["imported"]["source_folder"], SESSION)
        self.assertEqual(sent, original.replace(json.dumps(source), json.dumps(SESSION)))
        for forbidden in (str(self.home), os.path.realpath(self.home), str(Path.home())):
            self.assertNotIn(forbidden, sent)
        manifest = arc_payload.read_manifest(record_dir)
        self.assertEqual(manifest["adjusted"], [{"path": "session.json",
                                                 "what": "imported.source_folder shortened to the folder name"}])
        entry = {item["path"]: item for item in manifest["files"]}["session.json"]
        self.assertEqual((entry["bytes"], entry["sha256"]), (len(sent.encode("utf-8")), sha256(top / "session.json")))
        # The copy's own lists hold, and the session folder (unchanged) is not reported as changed.
        arc_payload.check_payload(record_dir, SESSION)
        self.assertEqual(arc_payload.after_check(self.folder, manifest), ([], []))
        # A later change of the session's own session.json is still found.
        (self.folder / "session.json").write_text(original.replace("S-001", "S-002"), encoding="utf-8")
        self.assertIn("session.json", arc_payload.after_check(self.folder, manifest)[1][0])
        # COR-05: where the copy is, for a live upload and for a demo one.
        self.assertTrue(arc_payload.after_check(self.folder, manifest)[1][0].endswith(
            "ARC has the copy from %s." % manifest["made_utc"]))
        self.assertTrue(arc_payload.after_check(self.folder, manifest, demo=True)[1][0].endswith(
            "The demo folder has the copy from %s." % manifest["made_utc"]))

    def test_paths_of_other_systems_and_values_left_alone(self):
        cases = {"C:\\Users\\someone\\scans\\" + SESSION: SESSION, "~/Downloads/" + SESSION: SESSION,
                 "/": "", SESSION: None, "": None, 5: None}
        for source, expected in cases.items():
            data = json.dumps({"schema": "x", "imported": {"source_folder": source}}).encode("utf-8")
            content, adjusted = arc_payload.adjusted_session_json(data)
            if expected is None:
                self.assertEqual((content, adjusted), (data, []), source)
            else:
                self.assertEqual(json.loads(content)["imported"]["source_folder"], expected, source)
                self.assertEqual(len(adjusted), 1)
        self.assertEqual(arc_payload.adjusted_session_json(b"{not json"), (b"{not json", []))


class Changes(SnapshotCase):
    def test_a_link_swapped_in_before_the_copy_is_never_followed(self):
        upload_id, record_dir = self.new_upload()
        target = self.session["files"][1]
        secret = self.home / "elsewhere.json"
        secret.write_text('{"secret": true}', encoding="utf-8")

        def swap(source):
            if Path(source) == target and not target.is_symlink():
                target.unlink()
                target.symlink_to(secret)

        with self.assertRaises(ArcError) as raised:
            self.snapshot(upload_id, hooks={"before_open": swap})
        self.assertEqual(raised.exception.reason, "link")
        self.assertFalse((Path(record_dir) / "payload").exists())
        self.assertFalse(list(Path(record_dir).glob("payload.tmp-*")))

    def swapper(self, folder, target, name):
        """Hooks of an attacker who swaps `folder` for a link to `target` just before `name` is opened, and swaps
        it back as soon as the next file is opened, or before the snapshot starts again (SEC-02)."""
        aside = folder.with_name(folder.name + ".aside")
        swapped = []

        def restore(*_arguments):
            if folder.is_symlink():
                folder.unlink()
                aside.rename(folder)

        def before_open(source):
            if Path(source).name == name and not swapped:
                swapped.append(True)
                folder.rename(aside)
                folder.symlink_to(target)
            else:
                restore()

        return {"before_open": before_open}, restore

    def test_a_folder_swapped_for_a_link_while_copying_is_never_followed(self):
        # SEC-02: a subfolder of the session swapped for a link to a folder that holds a file of the same
        # name: the snapshot never holds the outside content.
        (self.folder / "extra").mkdir()
        (self.folder / "extra" / "notes.txt").write_text("the session's own notes\n", encoding="utf-8")
        arc_support.set_age(self.folder, 600)
        outside = self.home / "outside"
        (outside / "extra").mkdir(parents=True)
        (outside / "extra" / "notes.txt").write_text("outside text\n", encoding="utf-8")
        upload_id, record_dir = self.new_upload()
        hooks, restore = self.swapper(self.folder / "extra", outside / "extra", "notes.txt")
        self.snapshot(upload_id, hooks=hooks, sleep=restore)
        top = self.top(record_dir)
        self.assertEqual((top / "extra" / "notes.txt").read_text(encoding="utf-8"), "the session's own notes\n")
        manifest = arc_payload.read_manifest(record_dir)
        digests = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
        self.assertEqual(digests["extra/notes.txt"], sha256(self.folder / "extra" / "notes.txt"))

    def test_the_session_folder_swapped_for_a_link_while_copying(self):
        decoy = self.home / "decoy"
        shutil.copytree(self.folder, decoy)
        (decoy / "point_0005.json").write_text('{"decoy": true}', encoding="utf-8")
        upload_id, record_dir = self.new_upload()
        hooks, restore = self.swapper(self.folder, decoy, "point_0005.json")
        self.snapshot(upload_id, hooks=hooks, sleep=restore)
        sent = (self.top(record_dir) / "point_0005.json").read_bytes()
        self.assertNotIn(b"decoy", sent)
        self.assertEqual(sent, (self.folder / "point_0005.json").read_bytes())
        manifest = arc_payload.read_manifest(record_dir)
        digests = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
        self.assertEqual(digests["point_0005.json"], sha256(self.folder / "point_0005.json"))

    def test_a_file_changed_while_it_is_copied_is_copied_again(self):
        upload_id, record_dir = self.new_upload()
        target = self.session["files"][2]
        pauses = []

        def change_once(source):
            if Path(source) == target and not pauses:
                with open(target, "a", encoding="utf-8") as stream:
                    stream.write("\n")

        self.snapshot(upload_id, hooks={"after_read": change_once}, sleep=pauses.append)
        self.assertEqual(pauses, [arc_payload.RETRY_PAUSE_SECONDS])
        self.assertEqual((self.top(record_dir) / target.name).read_bytes(), target.read_bytes())

    def test_a_session_that_keeps_changing_fails_after_three_tries(self):
        upload_id, record_dir = self.new_upload()
        target = self.session["files"][0]
        pauses = []

        def change(source):
            if Path(source) == target:
                with open(target, "a", encoding="utf-8") as stream:
                    stream.write(" ")

        with self.assertRaises(arc_payload.PayloadError) as raised:
            self.snapshot(upload_id, hooks={"after_read": change}, sleep=pauses.append)
        self.assertEqual(raised.exception.reason, "changed_during_copy")
        self.assertEqual(len(pauses), arc_payload.SNAPSHOT_TRIES - 1)
        self.assertFalse(list(Path(record_dir).glob("payload*")))

    def test_a_file_added_while_copying_is_copied_on_the_next_try(self):
        upload_id, record_dir = self.new_upload()
        added = self.folder / "point_0014.json"

        def add(source):
            if not added.exists():
                shutil.copyfile(self.session["files"][0], added)

        self.snapshot(upload_id, hooks={"after_read": add})
        self.assertTrue((self.top(record_dir) / "point_0014.json").is_file())
        manifest = arc_payload.read_manifest(record_dir)
        self.assertIn("point_0014.json", [entry["path"] for entry in manifest["files"]])

    def test_not_enough_free_space(self):
        upload_id, record_dir = self.new_upload()
        with self.assertRaises(arc_payload.PayloadError) as raised:
            self.snapshot(upload_id, disk_usage=lambda _path: Usage(10 ** 9, 10 ** 9, 50 * 10 ** 6))
        self.assertEqual(raised.exception.reason, "no_space")
        needed = arc_payload.free_space_needed(self.check().byte_count())
        self.assertEqual(raised.exception.values, {"mb": -(-needed // 1000000)})
        error = arc_records.worker_error("no_space", **raised.exception.values)
        self.assertIn("needs %d MB" % -(-needed // 1000000), error["message"])
        self.assertFalse(list(Path(record_dir).glob("payload*")))

    def test_a_changed_snapshot_is_found(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        arc_payload.check_payload(record_dir, SESSION)
        top = self.top(record_dir)
        os.chmod(top, 0o755)
        os.chmod(top / "point_0001.json", 0o644)
        (top / "point_0001.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(arc_payload.PayloadError) as raised:
            arc_payload.check_payload(record_dir, SESSION)
        self.assertEqual(raised.exception.reason, "payload_changed")
        (top / "point_0001.json").unlink()
        with self.assertRaises(arc_payload.PayloadError):
            arc_payload.check_payload(record_dir, SESSION)

    def test_an_extra_file_or_a_changed_list_is_found(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        top = self.top(record_dir)
        os.chmod(top, 0o755)
        (top / "extra.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(arc_payload.PayloadError):
            arc_payload.check_payload(record_dir, SESSION)
        (top / "extra.txt").unlink()
        os.chmod(top / "SHA256SUMS", 0o644)
        with open(top / "SHA256SUMS", "a", encoding="utf-8") as stream:
            stream.write("\n")
        with self.assertRaises(arc_payload.PayloadError):
            arc_payload.check_payload(record_dir, SESSION)


class Deleting(SnapshotCase):
    """SEC-06 and I12: deleting a snapshot never follows a link, and a kept copy can be deleted by hand."""

    def test_a_link_in_the_place_of_the_snapshot_is_removed_itself(self):
        upload_id, record_dir = self.new_upload()
        outside = self.home / "outside"
        (outside / "shared" / "deeper").mkdir(parents=True)
        for folder in (outside, outside / "shared", outside / "shared" / "deeper"):
            os.chmod(folder, 0o755)
        (Path(record_dir) / "payload").symlink_to(outside)
        (Path(record_dir) / "payload.tmp-deadbeef").symlink_to(outside / "shared")
        arc_payload.remove_temporaries(record_dir)
        arc_payload.delete(record_dir)
        self.assertFalse(os.path.lexists(Path(record_dir) / "payload"))
        self.assertFalse(os.path.lexists(Path(record_dir) / "payload.tmp-deadbeef"))
        for folder in (outside, outside / "shared", outside / "shared" / "deeper"):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755, folder)

    def test_a_link_inside_the_snapshot_is_removed_as_a_link(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        top = self.top(record_dir)
        outside = self.home / "outside_target"
        outside.mkdir()
        os.chmod(outside, 0o755)
        (outside / "kept.txt").write_text("x", encoding="utf-8")
        (top / "linked").symlink_to(outside)
        arc_payload.delete(record_dir)
        self.assertFalse((Path(record_dir) / "payload").exists())
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o755)
        self.assertTrue((outside / "kept.txt").is_file())

    def test_a_kept_copy_is_deleted_with_rm(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        done = subprocess.run(["/bin/rm", "-r", str(Path(record_dir) / "payload")], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse((Path(record_dir) / "payload").exists())


class ThroughTheWorker(SnapshotCase):
    def work(self, upload_id, transport, action="send"):
        with mock.patch.object(arc_worker._Run, "entry_point", lambda run: transport):
            return arc_worker.run_worker(self.home, upload_id, action)

    def test_verified_deletes_the_snapshot_and_keeps_the_copies(self):
        upload_id, record_dir = self.new_upload()
        self.assertEqual(self.work(upload_id, arc_support.placing_transport), arc_worker.EXIT_VERIFIED)
        record = self.record(upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertFalse((Path(record_dir) / "payload").exists())
        self.assertIs(record["payload"]["kept"], False)
        for name in ("raman_upload_manifest.json", "SHA256SUMS", "payload_hashes.json"):
            self.assertTrue((Path(record_dir) / name).is_file(), name)
        self.assertEqual(record["notes"][-1:], ["This session was made in simulation. It goes into the separate "
                                                "folder simulated/."])
        self.assertEqual(record["warnings"], [])

    def test_the_snapshot_is_kept_while_the_upload_is_not_verified(self):
        upload_id, record_dir = self.new_upload()

        def fails(ctx):
            ctx.set_state("signing_in")
            return ctx.failed("signin_timeout")

        self.assertEqual(self.work(upload_id, fails), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "signin_timeout"))
        self.assertTrue(record["payload"]["kept"])
        self.assertTrue((Path(record_dir) / "payload" / SESSION / "SHA256SUMS").is_file())

    def test_after_the_upload_a_cancel_txt_is_a_note_and_other_changes_a_warning(self):
        upload_id, _record_dir = self.new_upload()

        def then_cancel(ctx):
            (self.folder / "cancel.txt").write_text("stop\n", encoding="utf-8")
            return arc_support.placing_transport(ctx)

        self.work(upload_id, then_cancel)
        # COR-05: a demo upload's copy is in the demo folder on this Mac.
        self.assertIn("cancel.txt was written after the upload (closing the window or pressing Stop writes it); the "
                      "scan data in the demo folder is complete.", self.record(upload_id)["notes"])

        self.home = arc_support.arc_home(self)
        self.paths = Paths(self.home)
        self.session = arc_support.ready_session(self.home)
        self.folder = self.session["folder"]
        upload_id, _record_dir = self.new_upload()

        def then_change(ctx):
            with open(self.session["files"][0], "a", encoding="utf-8") as stream:
                stream.write(" ")
            return arc_support.placing_transport(ctx)

        self.work(upload_id, then_change)
        record = self.record(upload_id)
        manifest = arc_payload.read_manifest(self.record_dir(upload_id))
        self.assertEqual(record["warnings"], ["The session folder on this Mac changed after it was copied for this "
                                              "upload (point_0001.json). The demo folder has the copy from %s."
                                              % manifest["made_utc"]])

    def test_a_snapshot_left_by_a_copy_that_stopped_halfway_is_removed(self):
        upload_id, record_dir = self.new_upload()
        stale = Path(record_dir) / "payload.tmp-dead"
        (stale / SESSION).mkdir(parents=True)
        (stale / SESSION / "point_0001.json").write_text("half", encoding="utf-8")
        os.chmod(stale / SESSION, 0o555)
        self.assertEqual(self.work(upload_id, arc_support.placing_transport), arc_worker.EXIT_VERIFIED)
        self.assertFalse(stale.exists())

    def test_a_snapshot_whose_record_was_not_written_is_checked_and_kept(self):
        upload_id, record_dir = self.new_upload()
        self.snapshot(upload_id)
        inode = (self.top(record_dir) / "point_0001.json").stat().st_ino
        seen = {}

        def look(ctx):
            seen["inode"] = (ctx.payload_dir / "point_0001.json").stat().st_ino
            seen["payload"] = ctx.read()["payload"]
            return ctx.failed("signin_timeout")

        self.work(upload_id, look)
        self.assertEqual(seen["inode"], inode)
        self.assertTrue(seen["payload"]["kept"])

    def test_a_snapshot_that_changed_after_it_was_recorded_is_not_sent(self):
        upload_id, record_dir = self.new_upload()

        def fails(ctx):
            ctx.set_state("signing_in")
            return ctx.failed("gcp_not_connected")

        self.work(upload_id, fails)
        top = self.top(record_dir)
        os.chmod(top, 0o755)
        os.chmod(top / "point_0002.json", 0o644)
        (top / "point_0002.json").write_text("{}", encoding="utf-8")
        self.ok("arc-retry", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id, arc_support.placing_transport), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "payload_changed", False))
        self.assertNotIn("retry", self.ok("arc-status", "--upload=" + upload_id)["upload"]["actions"])

    def test_the_worker_refuses_a_session_that_changed_its_identity(self):
        upload_id, _record_dir = self.new_upload()
        path = self.folder / "session.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        content["session_id"] = "another-session"
        path.write_text(json.dumps(content), encoding="utf-8")
        arc_support.set_age(self.folder, 600)
        self.work(upload_id, arc_support.placing_transport)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "session_changed"))
        self.assertFalse((self.record_dir(upload_id) / "payload").exists())


if __name__ == "__main__":
    unittest.main()
