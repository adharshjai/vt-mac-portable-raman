"""The ARC side of an SSH upload alone: arc_remote.sh under this Mac's /bin/bash 3.2 and BSD tools
(design 11.3 and Appendix B).

The script runs here the way the demo runs it: /bin/bash -c <script> raman_arc_upload MODE BASE
FOLDER UPLOAD_ID NAME, with a tar stream made by the data layer's own writer on stdin, against a
temporary folder that stands in for ARC. No ssh, no network. Every run checks that the script read
its stdin to the end, and that nothing that was there before disappeared or changed, except this
upload's own folder in _incoming, whose one rename puts it in place.

On ARC the same script runs with GNU bash and coreutils. What differs there (GNU mv -T, GNU tar,
sha256sum, folders that inherit the setgid bit of the folder above) is reasoned from the GNU manuals,
not run here.
"""

import hashlib
import io
import json
import os
import select
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

import arc_support
from raman_catalog import arc_payload, arc_ssh
from raman_catalog.arc_records import MANIFEST_NAME, SUMS_NAME

# SHA-256 of the script text of design Appendix B ("byte for byte"), as changed by the review fixes
# (shared folders 3770, FAIL before the rest of stdin is read, a new copy under the plain name first,
# no linked _incoming, FAIL access also for a shared folder this account may not make a folder in, and in a
# check for a folder it may not read, also a folder in a copy it compares).
APPENDIX_B_SHA256 = "9265e26ad8776dd6436f5759233b8673dbd1f424f9b35ee25e6e94fcce3f4211"
# The shared folders (the ARC folder, simulated/, and their _incoming/) as a run makes them.
SHARED_MODE = 0o3770
# How long a line the script prints before it reads stdin may take to come.
LINE_SECONDS = 10
FOLDER = "1d_x_scan_20261113_101500_123"
FIRST, SECOND, THIRD = "u00000000a001", "u00000000a002", "u00000000a003"
# What the script needs besides sha256sum or shasum and tar (design 11.6).
TOOLS = ("cat", "mkdir", "find", "sort", "sed", "grep", "mv")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def make_payload(root, upload_id, folder=FOLDER, seed=1, wrong_digest=False):
    """A snapshot folder as the worker makes it: files, dark/, the manifest and SHA256SUMS."""
    top = Path(root) / upload_id / folder
    (top / "dark").mkdir(parents=True)
    files = {"session.json": json.dumps({"folder": folder, "seed": seed}).encode() + b"\n",
             "dark/dark_20261113_101400_001.txt": b"index\tintensity\n" + b"0\t4500\n" * 2000,
             "dark/dark_20261113_101400_001.json": b'{"simulated": true}\n',
             "response.txt": b"ACK\n"}
    for point in range(1, 4):
        files["point_%04d.json" % point] = (b'{"point_id": %d, "seed": %d, "intensity": [' % (point, seed) +
                                            b", ".join(b"%d" % (4500 + (i * 7 + point) % 900) for i in range(6000)) +
                                            b"]}\n")
    for relative, data in files.items():
        (top / relative).write_bytes(data)
    manifest = arc_payload.manifest_bytes({"schema": arc_payload.MANIFEST_SCHEMA, "upload_id": upload_id,
                                           "session": {"folder": folder}})
    (top / MANIFEST_NAME).write_bytes(manifest)
    entries = [(relative, sha256(data)) for relative, data in files.items()] + [(MANIFEST_NAME, sha256(manifest))]
    if wrong_digest:
        entries[0] = (entries[0][0], "0" * 64)
    (top / SUMS_NAME).write_bytes(arc_payload.sums_bytes(entries))
    return top


def stream_of(top):
    out = io.BytesIO()
    arc_ssh.write_tar(top, out)
    return out.getvalue()


def cut_inside_a_file(stream):
    """The stream cut in the middle of the content of the file that holds its middle byte."""
    with tarfile.open(fileobj=io.BytesIO(stream), mode="r:") as tar:
        members = [member for member in tar.getmembers() if member.isfile() and member.size > 1000]
    middle = len(stream) // 2
    for member in members:
        if member.offset_data + member.size > middle:
            return stream[:max(middle, member.offset_data) + member.size // 4]
    raise AssertionError("no file in the second half of the stream")


def expected_digests(top):
    """{path: sha256} that SHA256SUMS lists (every file but itself)."""
    found = {}
    for line in (Path(top) / SUMS_NAME).read_text().splitlines():
        digest, _separator, path = line.partition("  ")
        found[path] = digest
    return found


class RemoteScript(unittest.TestCase):
    def setUp(self):
        arc_support.guard(self)
        self.arc = Path(tempfile.mkdtemp(prefix="remote_arc_")).resolve()
        self.local = Path(tempfile.mkdtemp(prefix="remote_local_")).resolve()
        for folder in (self.arc, self.local):
            self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
            self.addCleanup(arc_support.make_writable, folder)
        # A group of this user's own, as a project group is on ARC: only then may a folder keep its
        # setgid bit (macOS clears it in a folder of a group the user is not in).
        os.chown(self.arc, -1, os.getgid())
        self.base = self.arc / "projects" / "pcl_photonics" / "raman_globus_upload"
        self.runs = 0

    # -- running the script

    def tree(self):
        found = {}
        for directory, folders, files in os.walk(self.arc):
            for name in folders:
                path = os.path.join(directory, name)
                found[os.path.relpath(path, self.arc)] = ("link" if os.path.islink(path) else "dir", None)
            for name in files:
                path = os.path.join(directory, name)
                if os.path.islink(path):
                    found[os.path.relpath(path, self.arc)] = ("link", None)
                    continue
                try:
                    digest = sha256(Path(path).read_bytes())
                except PermissionError:
                    # A file in a folder that may be listed but not entered (a test took its x away).
                    digest = "unreadable"
                found[os.path.relpath(path, self.arc)] = ("file", digest)
        return found

    def check_tree(self, before, after, stage, lines):
        """Only additions, and this upload's stage either kept (its files may be completed) or renamed
        once into the place the DONE line names."""
        target = None
        if lines and lines[-1].startswith("RAMAN DONE placed "):
            target = os.path.relpath(lines[-1][len("RAMAN DONE placed "):], self.arc)
        for path, (kind, digest) in before.items():
            inside = path == stage or path.startswith(stage + "/")
            if inside and target is not None:
                moved = target + path[len(stage):]
                self.assertIn(moved, after, "%s was not moved with its folder" % path)
                self.assertEqual(after[moved][0], kind, path)
                continue
            self.assertIn(path, after, "%s disappeared" % path)
            self.assertEqual(after[path][0], kind, path)
            if not inside:
                self.assertEqual(after[path][1], digest, "%s changed" % path)

    def run_script(self, mode, upload_id, data=b"", name=FOLDER, base=None, path=None):
        """(exit code, lines) of one run; stdin is a file, so what the script read can be measured."""
        base = Path(base or self.base)
        self.runs += 1
        source = self.local / ("stdin-%d.bin" % self.runs)
        source.write_bytes(data)
        before = self.tree()
        argv = arc_ssh.LocalBashRunner(mode, base, FOLDER, upload_id, name).argv
        environment = {"PATH": path or arc_ssh.LOCAL_PATH}
        if mode == "check":
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, env=environment, timeout=60)
        else:
            with open(source, "rb") as handle:
                done = subprocess.run(argv, stdin=handle, capture_output=True, env=environment, timeout=60)
                read = os.lseek(handle.fileno(), 0, os.SEEK_CUR)
            self.assertEqual(read, len(data), "the script did not read its stdin to the end")
        lines = done.stdout.decode("utf-8").splitlines()
        for line in lines:
            self.assertTrue(line.startswith("RAMAN "), line)
        stage = os.path.relpath(base / "_incoming" / ("%s.%s" % (FOLDER, upload_id)), self.arc)
        self.check_tree(before, self.tree(), stage, lines)
        return done.returncode, lines

    def digests_of(self, lines):
        begin, end = lines.index("RAMAN DIGESTS-BEGIN"), lines.index("RAMAN DIGESTS-END")
        found = {}
        for line in lines[begin + 1:end]:
            path, value = arc_ssh._digest(line[len("RAMAN DIGEST "):])
            found[path] = value
        return found

    def stage(self, upload_id, base=None):
        return Path(base or self.base) / "_incoming" / ("%s.%s" % (FOLDER, upload_id))

    def tools(self, *names, extra=None):
        """A folder for PATH holding links to the named tools only (and `extra` scripts)."""
        folder = self.local / ("tools-%d" % len(list(self.local.glob("tools-*"))))
        folder.mkdir()
        for name in names:
            source = shutil.which(name, path=arc_ssh.LOCAL_PATH)
            self.assertIsNotNone(source, name)
            os.symlink(source, folder / name)
        for name, text in (extra or {}).items():
            (folder / name).write_text(text)
            os.chmod(folder / name, 0o755)
        return str(folder)

    def run_open(self, mode, upload_id, until, path=None):
        """(exit code, lines) of a run whose stdin stays open, with nothing written to it, until the line
        `until` has come; only then is stdin closed. A script that read stdin first would never print
        it (the test fails after LINE_SECONDS)."""
        argv = arc_ssh.LocalBashRunner(mode, self.base, FOLDER, upload_id, FOLDER).argv
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env={"PATH": path or arc_ssh.LOCAL_PATH})
        self.addCleanup(lambda: process.poll() is None and process.kill())
        lines, pending = [], b""
        deadline = time.monotonic() + LINE_SECONDS
        try:
            while until not in lines:
                ready, _write, _error = select.select([process.stdout], [], [], max(0.0, deadline - time.monotonic()))
                self.assertTrue(ready, "%r did not come while stdin was open; came: %r" % (until, lines))
                data = os.read(process.stdout.fileno(), 65536)
                self.assertTrue(data, "the script ended before %r: %r" % (until, lines))
                pending += data
                *complete, pending = pending.split(b"\n")
                lines += [line.decode("utf-8") for line in complete]
        finally:
            process.stdin.close()
        rest = (pending + process.stdout.read()).decode("utf-8").splitlines()
        process.stdout.close()
        return process.wait(LINE_SECONDS), lines + rest

    def mode_of(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    # -- the script itself

    def test_the_script_is_appendix_b(self):
        data = arc_ssh.REMOTE_SCRIPT.read_bytes()
        self.assertEqual(sha256(data), APPENDIX_B_SHA256)
        self.assertTrue(data.startswith(b"#!/bin/bash\n"))
        mode = stat.S_IMODE(arc_ssh.REMOTE_SCRIPT.stat().st_mode)
        self.assertEqual(mode & 0o111, 0, "sent as text, never run as a file here")
        self.assertEqual(mode & 0o444, 0o444)
        self.assertEqual(subprocess.run(["/bin/bash", "-n", str(arc_ssh.REMOTE_SCRIPT)]).returncode, 0)
        version = subprocess.run(["/bin/bash", "--version"], capture_output=True, text=True).stdout
        self.assertIn("version 3.2", version.splitlines()[0])

    # -- the 15 cases of design 11.3

    def test_an_upload_is_placed_found_again_and_checked(self):
        top = make_payload(self.local, FIRST)
        stream = stream_of(top)
        code, lines = self.run_script("upload", FIRST, stream)
        final = self.base / FOLDER
        self.assertEqual(code, 0)
        self.assertEqual(lines[:3], ["RAMAN STEP prepare", "RAMAN STEP receive", "RAMAN STEP verify"])
        self.assertEqual(lines[-2:], ["RAMAN STEP place", "RAMAN DONE placed %s" % final])
        self.assertEqual(self.digests_of(lines), expected_digests(top))
        self.assertFalse(self.stage(FIRST).exists())
        for directory, folders, files in os.walk(final):
            for name in folders:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode), 0o750)
            for name in files:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode), 0o640)
        # The shared folders: every member of the project may add to them (decision I7).
        for folder in (self.base, self.base / "_incoming"):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), SHARED_MODE)
        sums = subprocess.run(["/usr/bin/shasum", "-a", "256", "-c", SUMS_NAME], cwd=final, capture_output=True)
        self.assertEqual(sums.returncode, 0, sums.stdout)

        # The same upload again: found in place, checked, nothing received (the stream is still read).
        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual(code, 0)
        self.assertNotIn("RAMAN STEP receive", lines)
        self.assertEqual(lines[0], "RAMAN STEP verify")
        self.assertEqual(lines[-1], "RAMAN DONE already %s" % final)
        self.assertEqual(self.digests_of(lines), expected_digests(top))

        # Check after placing.
        code, lines = self.run_script("check", FIRST)
        self.assertEqual((code, lines[0], lines[-1]), (0, "RAMAN STEP verify", "RAMAN DONE already %s" % final))

    def test_a_name_taken_by_another_upload_then_a_new_copy(self):
        self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        top = make_payload(self.local, SECOND, seed=2)
        stream = stream_of(top)
        final, copy2 = self.base / FOLDER, self.base / (FOLDER + "_copy2")

        code, lines = self.run_script("upload", SECOND, stream)
        self.assertEqual((code, lines[-1]), (24, "RAMAN CONFLICT %s" % final))
        self.assertEqual(lines[-2], "RAMAN STEP place")
        self.assertEqual(self.digests_of(lines), expected_digests(top))
        first_manifest = (self.local / FIRST / FOLDER / MANIFEST_NAME).read_bytes()
        self.assertEqual((final / MANIFEST_NAME).read_bytes(), first_manifest)

        code, lines = self.run_script("check", SECOND)
        self.assertEqual((code, lines[-1]), (0, "RAMAN STAGED complete %s" % self.stage(SECOND)))

        code, lines = self.run_script("upload-copy", SECOND, stream)
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE placed %s" % copy2))
        self.assertNotIn("RAMAN STEP receive", lines)
        self.assertFalse(self.stage(SECOND).exists())

        code, lines = self.run_script("upload-copy", SECOND, stream)
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE already %s" % copy2))

        code, lines = self.run_script("check", SECOND, name=FOLDER + "_copy2")
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE already %s" % copy2))
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), sorted([FOLDER, FOLDER + "_copy2",
                                                                                    "_incoming"]))

    def test_a_stream_cut_in_the_middle_then_sent_again(self):
        top = make_payload(self.local, FIRST)
        stream = stream_of(top)
        code, lines = self.run_script("upload", FIRST, cut_inside_a_file(stream))
        self.assertEqual(code, 22)
        self.assertEqual(lines[-1], "RAMAN FAIL receive the files did not arrive completely")
        self.assertFalse((self.base / FOLDER).exists())
        self.assertTrue(self.stage(FIRST).is_dir())
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), ["_incoming"])

        code, lines = self.run_script("check", FIRST)
        self.assertEqual((code, lines), (0, ["RAMAN STEP verify", "RAMAN STAGED partial %s" % self.stage(FIRST)]))

        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE placed %s" % (self.base / FOLDER)))
        self.assertIn("RAMAN STEP receive", lines)

    def test_a_stream_that_is_not_a_tar_fails_to_arrive(self):
        code, lines = self.run_script("upload", FIRST, b"not a tar stream\n" * 4000)
        self.assertEqual((code, lines[-1]), (22, "RAMAN FAIL receive the files did not arrive completely"))

    def test_a_list_with_one_wrong_digest_places_nothing(self):
        top = make_payload(self.local, FIRST, wrong_digest=True)
        code, lines = self.run_script("upload", FIRST, stream_of(top))
        self.assertEqual((code, lines[-1]), (23, "RAMAN FAIL verify the files that arrived do not match their list"))
        self.assertIn("RAMAN DIGESTS-BEGIN", lines)
        self.assertNotIn("RAMAN STEP place", lines)
        self.assertFalse((self.base / FOLDER).exists())

    def test_check_of_an_unknown_upload_missing_or_elsewhere(self):
        code, lines = self.run_script("check", THIRD)
        self.assertEqual((code, lines), (0, ["RAMAN MISSING %s" % (self.base / FOLDER)]))
        (self.base / FOLDER).mkdir(parents=True)
        (self.base / FOLDER / "theirs.txt").write_text("someone else's\n")
        code, lines = self.run_script("check", THIRD)
        self.assertEqual((code, lines), (0, ["RAMAN ELSEWHERE %s" % (self.base / FOLDER)]))

    def test_a_check_names_a_folder_this_account_may_not_read(self):
        # Review R2-06: a folder the check looks into, or the folder above the ARC folder, that this account may not
        # read is named (FAIL access, exit 21). The copy is never reported missing (MISSING), another upload's
        # (ELSEWHERE) or changed because the check could not see it.
        self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        self.run_script("upload", SECOND, stream_of(make_payload(self.local, SECOND, seed=2)))
        final, incoming, stage = self.base / FOLDER, self.base / "_incoming", self.stage(SECOND)
        self.assertTrue(stage.is_dir(), "the second upload waits in _incoming: its name is taken")
        cases = ((FIRST, final, 0o000), (FIRST, final, 0o300), (FIRST, self.base, 0o000), (FIRST, self.base, 0o300),
                 (FIRST, self.base.parent, 0o600), (SECOND, incoming, 0o000), (SECOND, stage, 0o000))
        for upload_id, folder, mode in cases:
            with self.subTest(folder=folder.name, mode=oct(mode)):
                kept = self.mode_of(folder)
                os.chmod(folder, mode)
                try:
                    code, lines = self.run_script("check", upload_id)
                finally:
                    os.chmod(folder, kept)
                self.assertEqual((code, lines), (21, ["RAMAN FAIL access %s" % folder]))
        # Readable again: the same checks look.
        self.assertEqual(self.run_script("check", FIRST)[1][-1], "RAMAN DONE already %s" % final)
        self.assertEqual(self.run_script("check", SECOND)[1][-1], "RAMAN STAGED complete %s" % stage)
        # A folder that is not there, below one this account may enter, is still missing.
        code, lines = self.run_script("check", THIRD, base=self.base / "simulated")
        self.assertEqual((code, lines), (0, ["RAMAN MISSING %s" % (self.base / "simulated" / FOLDER)]))

    def test_a_check_names_a_folder_in_a_copy_that_it_may_not_read(self):
        # Review R3-03: find leaves out the files of a folder it cannot enter, so the copy would look changed. A check
        # names the first folder of a copy it compares (the copy in place, a copy found under <FOLDER>_copyN, or this
        # upload's folder in _incoming) that this account may not read or enter: FAIL access, exit 21, before
        # STEP verify and before any digest.
        self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        second = stream_of(make_payload(self.local, SECOND, seed=2))
        self.run_script("upload", SECOND, second)
        final, stage, copy2 = self.base / FOLDER, self.stage(SECOND), self.base / (FOLDER + "_copy2")
        self.assertTrue(stage.is_dir(), "the second upload waits in _incoming: its name is taken")

        def check_with(upload_id, folder, mode):
            kept = self.mode_of(folder)
            os.chmod(folder, mode)
            try:
                return self.run_script("check", upload_id)
            finally:
                os.chmod(folder, kept)

        for upload_id, folder in ((FIRST, final / "dark"), (SECOND, stage / "dark")):
            for mode in (0o000, 0o300, 0o600):
                with self.subTest(folder=os.path.relpath(folder, self.base), mode=oct(mode)):
                    self.assertEqual(check_with(upload_id, folder, mode), (21, ["RAMAN FAIL access %s" % folder]))
        self.assertEqual(self.run_script("check", SECOND)[1][-1], "RAMAN STAGED complete %s" % stage)
        # Kept as a new copy: the check finds it by its manifest under <FOLDER>_copy2 (NAME is still FOLDER).
        self.assertEqual(self.run_script("upload-copy", SECOND, second)[1][-1], "RAMAN DONE placed %s" % copy2)
        for folder, mode in ((copy2 / "dark", 0o000), (copy2, 0o100)):
            with self.subTest(folder=os.path.relpath(folder, self.base), mode=oct(mode)):
                self.assertEqual(check_with(SECOND, folder, mode), (21, ["RAMAN FAIL access %s" % folder]))
        # Readable again: the same checks look, and find each copy as it was sent.
        code, lines = self.run_script("check", FIRST)
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE already %s" % final))
        self.assertEqual(self.digests_of(lines), expected_digests(self.local / FIRST / FOLDER))
        code, lines = self.run_script("check", SECOND)
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE already %s" % copy2))
        self.assertEqual(self.digests_of(lines), expected_digests(self.local / SECOND / FOLDER))

    def test_an_arc_folder_that_is_a_link(self):
        real = self.arc / "elsewhere"
        real.mkdir()
        self.base.parent.mkdir(parents=True)
        os.symlink(real, self.base)
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        self.assertEqual((code, lines), (21, ["RAMAN FAIL prepare %s is a link" % self.base]))
        self.assertEqual(list(real.iterdir()), [])

    # -- every exit code, 20 to 25

    def test_tools_missing_is_20(self):
        stream = stream_of(make_payload(self.local, FIRST))
        code, lines = self.run_script("upload", FIRST, stream, path=self.tools(*TOOLS, "tar"))
        self.assertEqual((code, lines), (20, ["RAMAN FAIL tools neither sha256sum nor shasum is installed"]))
        code, lines = self.run_script("upload", FIRST, stream, path=self.tools(*TOOLS, "shasum"))
        self.assertEqual((code, lines), (20, ["RAMAN FAIL tools tar is not installed"]))
        self.assertFalse(self.base.exists())

    def test_shasum_serves_where_sha256sum_is_missing(self):
        top = make_payload(self.local, FIRST)
        code, lines = self.run_script("upload", FIRST, stream_of(top), path=self.tools(*TOOLS, "tar", "shasum"))
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE placed %s" % (self.base / FOLDER)))
        self.assertEqual(self.digests_of(lines), expected_digests(top))

    def test_gnu_mv_renames_with_minus_t(self):
        gnu_mv = ("#!/bin/sh\n"
                  "if [ \"$1\" = --version ]; then echo 'mv (GNU coreutils) 9.4'; exit 0; fi\n"
                  "if [ \"$1\" != -T ] || [ \"$2\" != -- ] || [ $# -ne 4 ]; then\n"
                  "  echo 'expected -T --' >&2; exit 64\n"
                  "fi\n"
                  "if [ -e \"$4\" ]; then exit 1; fi\n"
                  "exec /bin/mv -- \"$3\" \"$4\"\n")
        names = [name for name in TOOLS if name != "mv"] + ["tar", "shasum"]
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)),
                                      path=self.tools(*names, extra={"mv": gnu_mv}))
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE placed %s" % (self.base / FOLDER)))

    def test_a_folder_that_cannot_be_made_is_21(self):
        # The project folder above the ARC folder: not a shared folder of these uploads.
        self.base.parent.mkdir(parents=True)
        os.chmod(self.base.parent, 0o555)
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL prepare cannot make %s" % self.base]))

    def test_a_stage_that_is_a_link_is_21(self):
        (self.base / "_incoming").mkdir(parents=True)
        os.symlink(self.arc, self.stage(FIRST))
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL prepare %s is a link"
                                              % self.stage(FIRST)]))

    def test_a_placed_copy_that_no_longer_matches_is_23(self):
        stream = stream_of(make_payload(self.local, FIRST))
        self.run_script("upload", FIRST, stream)
        point = self.base / FOLDER / "point_0002.json"
        point.write_bytes(point.read_bytes() + b" ")
        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual(code, 23)
        self.assertEqual(lines[-1], "RAMAN FAIL verify the copy in %s does not match its list" % (self.base / FOLDER))
        self.assertIn("RAMAN DIGESTS-BEGIN", lines)

    def test_a_name_taken_is_24_and_so_is_no_free_copy_name(self):
        for number in range(2, 100):
            (self.base / ("%s_copy%d" % (FOLDER, number))).mkdir(parents=True)
        (self.base / FOLDER).mkdir()
        stream = stream_of(make_payload(self.local, FIRST))
        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual((code, lines[-1]), (24, "RAMAN CONFLICT %s" % (self.base / FOLDER)))
        code, lines = self.run_script("upload-copy", FIRST, stream)
        self.assertEqual((code, lines[-1]), (24, "RAMAN FAIL place no free name from %s_copy2 to %s_copy99"
                                             % (FOLDER, FOLDER)))
        self.assertTrue(self.stage(FIRST).is_dir())

    def test_a_folder_without_this_uploads_manifest_is_25(self):
        # A complete, self-consistent copy that names another upload in its manifest.
        top = make_payload(self.local / "other", SECOND)
        code, lines = self.run_script("upload", FIRST, stream_of(top))
        self.assertEqual((code, lines[-1]),
                         (25, "RAMAN FAIL place the folder in _incoming does not hold this upload's manifest"))
        self.assertFalse((self.base / FOLDER).exists())

    # -- shared folders on ARC (decision I7)

    def test_the_shared_folders_of_the_simulated_area_are_made_3770(self):
        simulated = self.base / "simulated"
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)), base=simulated)
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE placed %s" % (simulated / FOLDER)))
        for folder in (self.base, simulated, simulated / "_incoming"):
            self.assertEqual(self.mode_of(folder), SHARED_MODE, folder)
        # Above the ARC folder: made as any folder is (the group may enter, not write).
        for folder in (self.arc / "projects", self.arc / "projects" / "pcl_photonics"):
            self.assertEqual(self.mode_of(folder), 0o750, folder)
        # This upload's own folder and files: the group reads them.
        self.assertEqual(self.mode_of(simulated / FOLDER), 0o750)
        self.assertEqual(self.mode_of(simulated / FOLDER / "session.json"), 0o640)

    def test_a_shared_folder_made_before_keeps_its_mode(self):
        (self.base / "_incoming").mkdir(parents=True)
        os.chmod(self.base, 0o755)
        os.chmod(self.base / "_incoming", 0o2775)
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        self.assertEqual(code, 0, lines)
        self.assertEqual((self.mode_of(self.base), self.mode_of(self.base / "_incoming")), (0o755, 0o2775))

    def test_a_shared_folder_this_account_may_not_write_into_is_21(self):
        # _incoming made by another member without group write: named before anything is received.
        incoming = self.base / "_incoming"
        incoming.mkdir(parents=True)
        os.chmod(incoming, 0o550)
        stream = stream_of(make_payload(self.local, FIRST))
        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL access %s" % incoming]))
        self.assertEqual(list(incoming.iterdir()), [])
        # The ARC folder itself, which the rename writes into.
        os.chmod(incoming, 0o750)
        os.chmod(self.base, 0o550)
        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL access %s" % self.base]))
        self.assertEqual(list(incoming.iterdir()), [])
        # A shared folder made before without group write, with the folder this run makes in it still missing
        # (fix plan I7): the shared folder is named, not "cannot make" the folder inside it.
        os.chmod(self.base, 0o750)
        os.rmdir(incoming)
        os.chmod(self.base, 0o550)
        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL access %s" % self.base]))
        self.assertFalse(incoming.exists())
        # The same for the simulated area: simulated/ in the ARC folder, then _incoming in simulated/.
        simulated = self.base / "simulated"
        code, lines = self.run_script("upload", FIRST, stream, base=simulated)
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL access %s" % self.base]))
        self.assertFalse(simulated.exists())
        os.chmod(self.base, 0o750)
        simulated.mkdir(mode=0o550)
        code, lines = self.run_script("upload", FIRST, stream, base=simulated)
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL access %s" % simulated]))
        self.assertEqual(list(simulated.iterdir()), [])

    # -- what the sender may stop sending (SSH-05)

    def test_a_fail_line_comes_before_the_stream_is_read(self):
        incoming = self.base / "_incoming"
        incoming.mkdir(parents=True)
        os.chmod(incoming, 0o550)
        code, lines = self.run_open("upload", FIRST, "RAMAN FAIL access %s" % incoming)
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare", "RAMAN FAIL access %s" % incoming]))
        code, lines = self.run_open("upload", FIRST, "RAMAN FAIL tools neither sha256sum nor shasum is installed",
                                    path=self.tools(*TOOLS, "tar"))
        self.assertEqual(code, 20)

    def test_a_copy_the_script_has_already_needs_no_stream(self):
        # Placed by an earlier run (step 1), and complete in _incoming after a conflict (step 2): the
        # script says so before it reads stdin.
        self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        code, lines = self.run_open("upload", FIRST, "RAMAN STEP verify")
        self.assertEqual((code, lines[0], lines[-1]), (0, "RAMAN STEP verify",
                                                       "RAMAN DONE already %s" % (self.base / FOLDER)))
        code, lines = self.run_script("upload", SECOND, stream_of(make_payload(self.local, SECOND, seed=2)))
        self.assertEqual(code, 24)
        code, lines = self.run_open("upload", SECOND, "RAMAN STEP verify")
        self.assertEqual((code, lines[:2], lines[-1]), (24, ["RAMAN STEP prepare", "RAMAN STEP verify"],
                                                        "RAMAN CONFLICT %s" % (self.base / FOLDER)))

    # -- links (SSH-09)

    def test_a_linked_incoming_folder_is_21(self):
        elsewhere = self.arc / "elsewhere"
        elsewhere.mkdir()
        self.base.mkdir(parents=True)
        os.symlink(elsewhere, self.base / "_incoming")
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)))
        self.assertEqual((code, lines), (21, ["RAMAN STEP prepare",
                                              "RAMAN FAIL prepare %s/_incoming is a link" % self.base]))
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_linked_arc_folder_is_refused_for_the_simulated_area_too(self):
        elsewhere = self.arc / "elsewhere"
        elsewhere.mkdir()
        self.base.parent.mkdir(parents=True)
        os.symlink(elsewhere, self.base)
        simulated = self.base / "simulated"
        code, lines = self.run_script("upload", FIRST, stream_of(make_payload(self.local, FIRST)), base=simulated)
        self.assertEqual((code, lines), (21, ["RAMAN FAIL prepare %s is a link" % self.base]))
        code, lines = self.run_script("check", FIRST, base=simulated)
        self.assertEqual((code, lines), (21, ["RAMAN FAIL prepare %s is a link" % self.base]))
        self.assertEqual(list(elsewhere.iterdir()), [])

    # -- a new copy (decision I4) and a copy changed on ARC (SSH-06)

    def test_a_new_copy_takes_the_folder_name_when_it_is_free(self):
        code, lines = self.run_script("upload-copy", FIRST, stream_of(make_payload(self.local, FIRST)))
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE placed %s" % (self.base / FOLDER)))
        code, lines = self.run_script("upload-copy", SECOND, stream_of(make_payload(self.local, SECOND, seed=2)))
        self.assertEqual((code, lines[-1]), (0, "RAMAN DONE placed %s_copy2" % (self.base / FOLDER)))

    def test_a_placed_copy_that_changed_is_named_by_step_1(self):
        # The Mac tells this FAIL verify from one of a copy just received: no STEP prepare comes before it,
        # and it names the folder.
        stream = stream_of(make_payload(self.local, FIRST))
        self.run_script("upload", FIRST, stream)
        point = self.base / FOLDER / "point_0002.json"
        point.write_bytes(point.read_bytes() + b" ")
        code, lines = self.run_script("upload", FIRST, stream)
        self.assertEqual((code, lines[0]), (23, "RAMAN STEP verify"))
        self.assertNotIn("RAMAN STEP prepare", lines)
        self.assertEqual(lines[-1], "RAMAN FAIL verify the copy in %s does not match its list" % (self.base / FOLDER))


if __name__ == "__main__":
    unittest.main()
