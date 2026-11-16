"""Uploads to ARC over SSH (design 11, the demo SSH of 12, and section 13 rows 13 to 15).

Live SSH never runs here: the test guard refuses /usr/bin/ssh and /usr/bin/open. Whole attempts
run against tests/fake_ssh.py, a stand-in ssh program that maps /projects/ to a temporary folder
and runs the real arc_remote.sh with /bin/bash, as ARC's login shell would. The demo runs the
same script with /bin/bash itself (arc_ssh.LocalBashRunner).
"""

import base64
import contextlib
import ctypes
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import arc_support
import support
from raman_datafed import arc_commands, arc_demo, arc_payload, arc_records, arc_session, arc_ssh, arc_worker, doctor
from raman_datafed.arc_worker import GuardRefused

SESSION = arc_support.SESSION
USER = arc_support.TEST_USERNAME
HOST = "datatransfer.arc.vt.edu"
ARC_FOLDER = arc_support.ARC_FOLDER
UPLOAD = "u0123456789ab"
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")
LIVE_SSH = {"demo": False, "transport": "ssh", "ssh": {"username": USER}}
# Runs the upload program as the Terminal window does, with the stand-in ssh (a test of signals).
DRIVER = ("import sys\n"
          "from raman_datafed import arc_ssh, arc_worker\n"
          "arc_ssh.SSH_PROGRAM = sys.argv[1]\n"
          "sys.exit(arc_worker.main(sys.argv[2:]))\n")
# Design 11.4 step 1, with ssh's questions in the order ssh asks them (decision I9): the host-key
# question first (its answer is shown on screen), then the password, then Duo.
TEXT_11_4 = textwrap.dedent("""\
    Portable Raman: upload to ARC over SSH
      Session  {folder}  (13 spectra, {files} files, {size})
      To       {user}@datatransfer.arc.vt.edu:/projects/pcl_photonics/raman_globus_upload/{folder}

    You sign in to ARC once for this upload. ssh asks, in this order:
      1. The first time only: ARC's key fingerprint, then "Are you sure you want to continue connecting
         (yes/no/[fingerprint])?". Type only yes or no here; what you type is shown on screen, so never
         type your password at this question. Answer yes only if the fingerprint matches the one in the
         README (section 14, Host key); otherwise answer no and ask the team.
      2. Your VT password. Typing shows nothing; that is normal. Press Return.
      3. Duo: approve the push on your phone, or type your password followed by a comma and a
         six-digit passcode at the password question. Choose Duo Push if a numbered menu appears.
    ssh may print a WARNING that the connection is not using a post-quantum key exchange
    algorithm. That is expected with ARC (ARC's FAQ explains it); continue.
    What you type goes to ssh only: this program does not see, keep or log it.
    ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN.
    If ssh warns that the REMOTE HOST IDENTIFICATION HAS CHANGED, stop here: nothing is sent.
    To stop, press Ctrl-C. Files already copied stay on ARC.
    """)
# The steps when the team's known-hosts file is set: ssh asks no host-key question.
TEAM_FILE_STEPS = textwrap.dedent("""\
    You sign in to ARC once for this upload. ssh asks, in this order:
      1. Your VT password. Typing shows nothing; that is normal. Press Return.
      2. Duo: approve the push on your phone, or type your password followed by a comma and a
         six-digit passcode at the password question. Choose Duo Push if a numbered menu appears.
    ssh checks ARC's key against the team's file {path}; it does not ask.
    """)
# The options a person's ~/.ssh/config could otherwise set for ARC (SSH-08), after IgnoreUnknown=*.
PINNED = ["-o", "IgnoreUnknown=*", "-o", "StdinNull=no", "-o", "SessionType=default", "-o",
          "ForkAfterAuthentication=no", "-o", "ChannelTimeout=none"]
OTHER_SESSION = "1d_y_scan_20261113_111500_456"


class Killed(BaseException):
    """Stands in for a worker killed at a fault point (a BaseException, so the worker records nothing)."""


def killed_at(wanted):
    def fault(name):
        if name == wanted:
            raise Killed(name)

    return fault


def fake_ssh_program(test):
    """An executable that runs tests/fake_ssh.py (its name is not "ssh": the guard lets it run)."""
    folder = Path(tempfile.mkdtemp(prefix="fake_ssh_")).resolve()
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    program = folder / "fake-ssh"
    program.write_text("#!/bin/sh\nexec %s -I %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(str(FAKE_SSH))))
    os.chmod(program, 0o755)
    return program, folder


def files_below(top):
    """{relative path: sha256} of every file under a folder."""
    found = {}
    for directory, _folders, files in os.walk(top):
        for name in files:
            path = os.path.join(directory, name)
            found[os.path.relpath(path, top)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return found


def set_xattr(path):
    """Give a file an extended attribute (macOS setxattr); False when that is not possible here."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        value = b"1"
        return libc.setxattr(str(path).encode(), b"com.example.raman-test", value, len(value), 0, 0) == 0
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------- the command line


class CommandLine(unittest.TestCase):
    def setUp(self):
        arc_support.guard(self)

    def test_the_command_line_with_the_persons_own_known_hosts(self):
        self.assertEqual(arc_ssh.ssh_argv(USER, HOST, "", "COMMAND"), [
            "/usr/bin/ssh", "-T", "-e", "none",
            "-o", "BatchMode=no", "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "StrictHostKeyChecking=ask",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
            "-o", "PermitLocalCommand=no",
            "-o", "RequestTTY=no", "-o", "RemoteCommand=none",
            "-o", "IgnoreUnknown=*", "-o", "StdinNull=no", "-o", "SessionType=default",
            "-o", "ForkAfterAuthentication=no", "-o", "ChannelTimeout=none",
            "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
            "-l", USER, "--", HOST, "COMMAND"])

    def test_a_persons_config_cannot_empty_stdin_fork_or_close_the_session(self):
        # SSH-08: StdinNull, SessionType, ForkAfterAuthentication and ChannelTimeout from ~/.ssh/config
        # would give the remote script no stdin, no session, a forked ssh, or a closed session while ARC
        # checks a big copy. IgnoreUnknown=* comes first: an older ssh skips what it does not know, and a
        # person's own IgnoreUnknown (which a narrower list here would replace) keeps working.
        for known_hosts in ("", "/Users/lab/arc_known_hosts"):
            argv = arc_ssh.ssh_argv(USER, HOST, known_hosts, "COMMAND")
            start = argv.index("IgnoreUnknown=*") - 1
            self.assertEqual(argv[start:start + len(PINNED)], PINNED)
            self.assertLess(start, argv.index("--"))
            for option in ("StdinNull", "SessionType", "ForkAfterAuthentication", "ChannelTimeout", "IgnoreUnknown"):
                self.assertEqual(sum(1 for item in argv if item.startswith(option + "=")), 1, option)

    def test_the_command_line_with_the_teams_file(self):
        argv = arc_ssh.ssh_argv(USER, HOST, "/Users/lab/arc_known_hosts", "COMMAND")
        self.assertEqual(argv[10:16], ["-o", "StrictHostKeyChecking=yes",
                                       "-o", "UserKnownHostsFile=/Users/lab/arc_known_hosts",
                                       "-o", "GlobalKnownHostsFile=/dev/null"])
        self.assertEqual(argv[-5:], ["-l", USER, "--", HOST, "COMMAND"])

    def test_host_keys_are_never_taken_blindly(self):
        for known_hosts in ("", "/Users/lab/arc_known_hosts"):
            argv = arc_ssh.ssh_argv(USER, HOST, known_hosts, "COMMAND")
            text = " ".join(argv).lower()
            self.assertNotIn("accept-new", text)
            self.assertNotIn("stricthostkeychecking=no", text)
            self.assertNotIn("stricthostkeychecking=off", text)
            self.assertEqual(sum(1 for item in argv if item.startswith("StrictHostKeyChecking=")), 1)
            self.assertEqual(argv[0], "/usr/bin/ssh")
            self.assertIn("ControlMaster=no", argv)
            self.assertIn("ControlPath=none", argv)
            self.assertEqual(argv[argv.index("-l") + 1], USER)
            self.assertEqual(argv.index("--") + 1, argv.index(HOST))
        source = Path(arc_ssh.__file__).read_text(encoding="utf-8")
        for value in ("accept-new", "no", "off"):
            self.assertNotIn('"StrictHostKeyChecking=%s"' % value, source)

    def test_values_that_could_become_options_are_refused(self):
        for username in ("Test_user", "a@b", "-oProxyCommand=x", "root", "", "x", "a b", USER + "\n"):
            with self.assertRaises(ValueError, msg=username):
                arc_ssh.ssh_argv(username, HOST, "", "COMMAND")
        for host in ("example.org", "arc.vt.edu.example.org", "-oProxyCommand=x", "Tinkercliffs1.arc.vt.edu", "",
                     HOST + "\n"):
            with self.assertRaises(ValueError, msg=host):
                arc_ssh.ssh_argv(USER, host, "", "COMMAND")
        for known_hosts in ("/Users/lab/my files/kh", "/Users/lab/%d/kh", "/Users/${HOME}/kh", "relative/kh",
                            "/Users/lab/kh\n"):
            with self.assertRaises(ValueError, msg=known_hosts):
                arc_ssh.ssh_argv(USER, HOST, known_hosts, "COMMAND")
        self.assertEqual(arc_ssh.ssh_argv(USER, "tinkercliffs1.arc.vt.edu", "", "C")[-2], "tinkercliffs1.arc.vt.edu")

    def test_the_remote_command_carries_the_script_byte_for_byte(self):
        command = arc_ssh.remote_command("upload", ARC_FOLDER, SESSION, UPLOAD, SESSION)
        match = re.fullmatch(r"bash -c \"\$\(printf %s '([A-Za-z0-9+/=]+)' \| base64 --decode\)\" raman_arc_upload "
                             r"upload (.*)", command)
        self.assertIsNotNone(match, command)
        script = arc_ssh.REMOTE_SCRIPT.read_bytes()
        self.assertEqual(base64.b64decode(match.group(1)), script)
        self.assertEqual(match.group(2), "'%s' '%s' '%s' '%s'" % (ARC_FOLDER, SESSION, UPLOAD, SESSION))
        self.assertEqual(shlex.split(command)[3:], ["raman_arc_upload", "upload", ARC_FOLDER, SESSION, UPLOAD, SESSION])
        # Decoded the way the remote shell decodes it.
        decoded = subprocess.run(["/bin/bash", "-c", "printf %%s '%s' | base64 --decode" % match.group(1)],
                                 capture_output=True, env={"PATH": arc_ssh.LOCAL_PATH})
        self.assertEqual(decoded.stdout, script)
        copy = arc_ssh.remote_command("upload-copy", ARC_FOLDER + "/simulated", SESSION, UPLOAD, SESSION + "_copy2")
        self.assertTrue(copy.endswith(" raman_arc_upload upload-copy '%s/simulated' '%s' '%s' '%s_copy2'"
                                      % (ARC_FOLDER, SESSION, UPLOAD, SESSION)))

    def test_the_remote_command_takes_validated_characters_only(self):
        good = {"mode": "upload", "base": ARC_FOLDER, "folder": SESSION, "upload_id": UPLOAD, "name": SESSION}
        for bad in ({"mode": "rm"}, {"base": "/projects/a b"}, {"base": "/projects/x'y"}, {"base": "/projects/../etc"},
                    {"base": "/projects/./x"}, {"base": "/projects/$(x)"}, {"folder": "a/b"}, {"folder": "a;b"},
                    {"upload_id": "u12"}, {"upload_id": "x0123456789ab"}, {"upload_id": UPLOAD + "\n"},
                    {"name": "x$(y)"}, {"name": "a b"}, {"name": SESSION + "\n"}, {"name": "../x"},
                    {"base": "projects/x"}, {"base": ARC_FOLDER + "\n"}):
            values = dict(good, **bad)
            with self.assertRaises(ValueError, msg=bad):
                arc_ssh.remote_command(**values)


# ---------------------------------------------------------------------- the tar stream


class Stream(unittest.TestCase):
    def setUp(self):
        arc_support.guard(self)
        self.top = Path(tempfile.mkdtemp(prefix="stream_")).resolve() / SESSION
        self.addCleanup(shutil.rmtree, self.top.parent, ignore_errors=True)
        (self.top / "dark").mkdir(parents=True)
        files = {"session.json": b"{}\n", "point_0001.json": b"1" * 30000, "SHA256SUMS": b"sums\n",
                 "dark/dark_1.txt": b"dark\n", "dark/dark_1.json": b"{}\n", "advanced.txt": b"0\n",
                 arc_payload.MANIFEST_NAME: b'{\n  "upload_id": "%s",\n}\n' % UPLOAD.encode()}
        for index, (relative, data) in enumerate(sorted(files.items())):
            path = self.top / relative
            path.write_bytes(data)
            os.utime(path, (1700000000 + index, 1700000000.75 + index))
        self.files = files
        self.with_xattr = set_xattr(self.top / "point_0001.json")

    def stream(self):
        out = io.BytesIO()
        arc_ssh.write_tar(self.top, out)
        return out.getvalue()

    def test_the_stream_holds_the_snapshot_and_nothing_of_this_mac(self):
        data = self.stream()
        self.assertEqual(len(data) % tarfile.RECORDSIZE, 0)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tar:
            members = []
            for member in tar:
                members.append(member)
                if member.isfile():
                    self.assertEqual(tar.extractfile(member).read(), self.files[member.name])
            self.assertEqual(tar.pax_headers, {})
        names = [member.name for member in members]
        self.assertEqual(names[0], arc_payload.MANIFEST_NAME)
        self.assertEqual(names[1:], sorted(names[1:], key=lambda name: name.encode()))
        self.assertEqual(sorted(names), sorted(list(self.files) + ["dark"]))
        for member in members:
            self.assertFalse(member.name.startswith("._") or "/._" in member.name, member.name)
            self.assertEqual(member.pax_headers, {}, member.name)
            self.assertEqual((member.uid, member.gid, member.uname, member.gname), (0, 0, "", ""), member.name)
            self.assertEqual(member.mode, 0o750 if member.isdir() else 0o640, member.name)
            self.assertTrue(member.isdir() or member.isfile(), member.name)
            self.assertEqual(member.mtime, int(os.lstat(self.top / member.name).st_mtime), member.name)
        self.assertNotIn(b"xattr", data)
        self.assertNotIn(b"com.example", data)
        self.assertNotIn(str(self.top).encode(), data)

    def test_a_link_in_the_snapshot_is_refused(self):
        os.symlink("/etc/hosts", self.top / "hosts")
        with self.assertRaises(arc_payload.PayloadError) as caught:
            self.stream()
        self.assertEqual(caught.exception.reason, "payload_changed")

    def test_an_error_never_finishes_the_archive(self):
        out = io.BytesIO()

        def breaks(count):
            raise OSError("the disk went away")

        with self.assertRaises(OSError):
            arc_ssh.write_tar(self.top, out, on_bytes=breaks)
        written = out.getvalue()
        with self.assertRaises(tarfile.TarError):
            with tarfile.open(fileobj=io.BytesIO(written), mode="r|") as tar:
                for member in tar:
                    tar.extractfile(member).read() if member.isfile() else None
        self.assertNotEqual(written[-1024:], b"\0" * 1024)


# ---------------------------------------------------------------------- the script sent, and names the demo refuses


class ScriptReadOnce(unittest.TestCase):
    """Decision I10: a worker sends the arc_remote.sh of the code it started with."""

    def setUp(self):
        arc_support.guard(self)

    def test_the_script_is_read_once_per_process(self):
        sent = arc_ssh.remote_script()
        self.assertEqual(sent, arc_ssh.REMOTE_SCRIPT.read_bytes())
        folder = Path(tempfile.mkdtemp(prefix="script_")).resolve()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        newer = folder / "arc_remote.sh"
        newer.write_bytes(b"#!/bin/bash\necho a newer install\n")
        with mock.patch.object(arc_ssh, "REMOTE_SCRIPT", newer):
            self.assertEqual(arc_ssh.remote_script(), sent)
            command = arc_ssh.remote_command("upload", ARC_FOLDER, SESSION, UPLOAD, SESSION)
            self.assertIn("'%s'" % base64.b64encode(sent).decode("ascii"), command)
            self.assertEqual(arc_ssh.LocalBashRunner("check", "/tmp/x", SESSION, UPLOAD, SESSION).argv[2],
                             sent.decode("utf-8"))

    def test_the_script_a_worker_read_when_it_started_is_the_one_sent(self):
        given = b"#!/bin/bash\necho the script of this worker\n"
        record = {"destination": {"arc_folder": ARC_FOLDER, "area": arc_session.AREA_SIMULATED,
                                  "base": ARC_FOLDER + "/simulated"}, "session": {}, "payload": {}}
        demo = SimpleNamespace(session_folder=SESSION, upload_id=UPLOAD, demo=True, demo_root=Path("/tmp/demo root"),
                               settings_used={}, remote_script=given)
        self.assertEqual(arc_ssh.Plan(demo, record, "check").runner().argv[2], given.decode("utf-8"))
        live = SimpleNamespace(session_folder=SESSION, upload_id=UPLOAD, demo=False, demo_root=None,
                               settings_used={"ssh_username": USER, "ssh_host": HOST, "ssh_known_hosts": ""},
                               remote_script=given)
        argv = arc_ssh.Plan(live, record, "check").runner().argv
        self.assertIn("'%s'" % base64.b64encode(given).decode("ascii"), argv[-1])


class Lines(unittest.TestCase):
    """What the lines of the remote script tell the Mac: when to stop sending (SSH-05), and where the copy
    may be when a run ends (ssh.may_be_placed, SSH-V01)."""

    def setUp(self):
        arc_support.guard(self)

    def read(self, *lines, send_files=True):
        record = {"state": "signing_in", "ssh": {}}
        ctx = SimpleNamespace(read=lambda: dict(record), update=lambda mutate: mutate(record) or record,
                              text=lambda text: text, event=lambda *arguments: None, terminal=False)
        attempt = arc_ssh.Attempt(ctx, SimpleNamespace(files=1, bytes=1, demo=False), None, send_files)
        for line in lines:
            attempt._line(("RAMAN %s\n" % line).encode())
        return attempt, record

    def test_the_stream_stops_once_the_far_end_reads_no_more_of_it(self):
        for lines, unneeded in ((("STEP prepare", "STEP receive"), False),
                                (("STEP prepare", "SOMETHING a remote .bashrc printed"), False),
                                (("STEP prepare", "FAIL access /projects/x/_incoming"), True),
                                (("FAIL tools tar is not installed",), True),
                                (("STEP verify",), True),
                                (("STEP prepare", "STEP verify"), True)):
            self.assertEqual(self.read(*lines)[0].stream_unneeded.is_set(), unneeded, lines)

    def test_where_the_copy_may_be_when_a_run_ends(self):
        received = ("STEP prepare", "STEP receive", "STEP verify", "DIGESTS-BEGIN", "DIGESTS-END")
        for lines, placed in ((received + ("STEP place", "DONE placed /projects/x/f"), True),
                              (received + ("STEP place",), True),
                              (received, True),
                              (("STEP prepare", "STEP receive"), False),
                              (received + ("STEP place", "CONFLICT /projects/x/f"), False),
                              (received + ("STEP place", "FAIL place cannot rename"), False),
                              (("STEP prepare", "FAIL access /projects/x/_incoming"), False),
                              (("STEP verify", "DIGESTS-BEGIN", "DIGESTS-END", "FAIL verify the copy in /projects/x/f "
                                "does not match its list"), True),
                              (("FAIL tools tar is not installed",), None),
                              (("FAIL prepare /projects/x is a link",), None),
                              ((), None)):
            self.assertEqual(self.read(*lines)[0].placed_now(), placed, lines)
        for lines, placed in ((("STEP verify", "STAGED complete /projects/x/_incoming/f.u0"), False),
                              (("MISSING /projects/x/f",), False),
                              (("STEP verify", "DIGESTS-BEGIN", "DIGESTS-END", "DONE already /projects/x/f"), True)):
            self.assertEqual(self.read(*lines, send_files=False)[0].placed_now(), placed, lines)
        # Written as soon as the script reaches its rename, in case this worker never ends its run.
        self.assertTrue(self.read(*received, "STEP place")[1]["ssh"]["may_be_placed"])
        self.assertNotIn("may_be_placed", self.read(*received)[1]["ssh"])


class CheckThatCouldNotLook(unittest.TestCase):
    """What Check status says when ssh or the remote script ended before it could look (review REG-01): a
    verified upload gets a "checks" entry with "looked": false and its fix; any other keeps its state and
    says why. The texts are the check's own, not those of an upload."""

    def setUp(self):
        arc_support.guard(self)

    def outcome(self, state, demo=False, result=None, lines=0, exit_code=255):
        ctx = SimpleNamespace(demo=demo, stop_requested=lambda: False, payload_digests=lambda: {},
                              text=lambda text: arc_records.demo_text(text, demo),
                              error=lambda reason, **values: arc_records.worker_error(reason, demo=demo, **values))
        base = "/projects/x"
        plan = SimpleNamespace(incoming=base + "/_incoming/%s.u0" % SESSION, name=SESSION, demo=demo, remote_base=base,
                               remote_top=base, host=None if demo else HOST, shown=lambda name: base + "/" + name,
                               takes_name=lambda name: arc_ssh.Plan.takes_name(SimpleNamespace(folder=SESSION), name))
        # A FAIL line's text as the far end printed it (Attempt.fail_text).
        fail_text = result[2] if result and result[0] == "fail" else ""
        attempt = SimpleNamespace(result=result, stopped=False, lines=lines, exit_code=exit_code, digests={},
                                  found_in_place=lambda: False, fail_text=fail_text)
        return arc_ssh._check_outcome(ctx, plan, attempt, state)

    def test_the_checks_own_words_and_fixes(self):
        # Review R2-03: a check makes no folder, and it runs on the SSH computer saved with the upload, so another
        # check meets the same problem: a refused folder and a computer that cannot run it name who can help.
        signin = arc_records.WORKER_RESULTS["ssh_signin_failed"][0]
        unreadable = "/projects/x/" + SESSION
        for kwargs, message, fix in (
                ({}, signin + " Check status could not look.", arc_records.CHECK_FIXES["ssh_signin_failed"]),
                ({"exit_code": 1}, "The check cannot run on %s: that computer did not run its commands. Check status "
                                   "could not look." % HOST, arc_records.CHECK_FIXES["ssh_host_refused_commands"]),
                ({"lines": 2}, "The SSH connection ended before the check was done. Check status could not look.",
                 arc_records.CHECK_AGAIN_FIX),
                ({"result": ("fail", "tools", "tar is not installed"), "lines": 1},
                 "The check cannot run on %s: tar is not installed. Check status could not look." % HOST,
                 arc_records.CHECK_FIXES["remote_tools"]),
                ({"result": ("fail", "prepare", "/projects/x is a link"), "lines": 1},
                 "ARC refused a folder on the way to the copy: /projects/x is a link. Check status could not look.",
                 arc_records.CHECK_FIXES["folder_refused"]),
                # Review R2-06: a folder the check may not read is a note, never "Nothing of this upload is on ARC.".
                ({"result": ("fail", "access", unreadable), "lines": 1},
                 "Your VT account may not read %s on ARC. Check status could not look." % unreadable,
                 arc_records.CHECK_FIXES["folder_not_readable"])):
            with self.subTest(**{key: str(value) for key, value in kwargs.items()}):
                found = self.outcome("verified", **kwargs)
                self.assertEqual((found.state, found.message), ("verified", None))
                self.assertEqual(found.check, {"ok": False, "looked": False, "message": message, "fix": fix})
                kept = self.outcome("failed", **kwargs)
                self.assertEqual((kept.state, kept.message, kept.check), ("failed", message, None))
        for fix in arc_records.CHECK_FIXES.values():
            self.assertNotIn("Retry same request", fix)
        for reason in ("remote_tools", "ssh_host_refused_commands", "folder_refused", "folder_not_readable"):
            self.assertIn("Ask the team", arc_records.CHECK_FIXES[reason], reason)
        # Review R3-04: the fix of a computer that cannot run the check says what to ask; a demo upload's names this
        # Mac, which runs every check of it.
        for reason in ("remote_tools", "ssh_host_refused_commands"):
            self.assertEqual(arc_records.CHECK_FIXES[reason], "Nothing was changed. Ask the team to fix that computer: "
                                                              "every check of this upload runs on it. Then press "
                                                              "Check status again.")
            self.assertEqual(arc_records.CHECK_FIXES[reason + "_demo"], "Nothing was changed. Ask the team to fix this "
                                                                        "Mac: every check of this upload runs on it. "
                                                                        "Then press Check status again.")
        # Review R3-02: a check looks in the demo folder saved with the upload, so another demo folder cannot help. The
        # demo fixes say what does.
        for reason, fix in arc_records.CHECK_FIXES.items():
            self.assertNotIn("demo folder in ARC settings", fix, reason)
            self.assertNotIn("another demo folder", fix, reason)
        self.assertEqual(arc_records.CHECK_FIXES["folder_not_readable_demo"], "Make that folder and the folders in it "
                                                                              "readable again for this Mac's account. "
                                                                              "Then press Check status again.")
        self.assertEqual(arc_records.CHECK_FIXES["folder_refused_demo"], "Nothing was changed. Put the moved folder "
                                                                         "back in place of the link. Then press Check "
                                                                         "status again.")
        # The demo's words name the demo folder or this Mac, never ARC, with the demo's own fix where it has one.
        for kwargs, message, fix in (
                ({"exit_code": 1}, "Demo: the check cannot run on this Mac: its commands did not run. Check status "
                                   "could not look.", arc_records.CHECK_FIXES["ssh_host_refused_commands_demo"]),
                ({"result": ("fail", "tools", "tar is not installed"), "lines": 1},
                 "Demo: the check cannot run on this Mac: tar is not installed. Check status could not look.",
                 arc_records.CHECK_FIXES["remote_tools_demo"]),
                ({"result": ("fail", "prepare", "/projects/x is a link"), "lines": 1},
                 "Demo: the demo refused a folder on the way to the copy: /projects/x is a link. Check status could "
                 "not look.", arc_records.CHECK_FIXES["folder_refused_demo"]),
                ({"result": ("fail", "access", unreadable), "lines": 1},
                 "Demo: this Mac's account may not read %s in the demo folder. Check status could not look."
                 % unreadable,
                 arc_records.CHECK_FIXES["folder_not_readable_demo"])):
            with self.subTest(demo=True, **{key: str(value) for key, value in kwargs.items()}):
                found = self.outcome("verified", demo=True, **kwargs)
                self.assertEqual(found.check, {"ok": False, "looked": False, "message": message, "fix": fix})
                self.assertNotIn(" on ARC", message + " " + fix)

    def test_a_folder_the_check_may_not_read_is_one_it_looks_into(self):
        # FAIL access of a check names <base>, the copy, _incoming, this upload's folder in it, or a folder above
        # <base>; any other folder is not what the script prints (the worker records internal). Review R3-03: also a
        # copy found under <FOLDER>_copyN, and any folder in a copy it compares.
        for path in ("/projects/x", "/projects/x/" + SESSION, "/projects/x/_incoming",
                     "/projects/x/_incoming/%s.u0" % SESSION, "/projects", "/",
                     "/projects/x/%s/dark" % SESSION, "/projects/x/_incoming/%s.u0/dark" % SESSION,
                     "/projects/x/%s_copy2" % SESSION, "/projects/x/%s_copy99/dark/old" % SESSION):
            with self.subTest(path=path):
                found = self.outcome("verified", result=("fail", "access", path), lines=1).check
                self.assertEqual((found["looked"], found["message"]), (False, "Your VT account may not read %s on ARC. "
                                                                              "Check status could not look." % path))
        for path in ("/projects/xy", "/projects/x/other", "/home", "projects", "/projects/x/other/dark",
                     "/projects/x/_incoming/%s.u1/dark" % SESSION, "/projects/x/_incoming/other",
                     "/projects/x/%s_copy100/dark" % SESSION, "/projects/x/%s/../other" % SESSION,
                     "/projects/x/%s//dark" % SESSION, "/projects/x/%s/./dark" % SESSION):
            with self.subTest(path=path), self.assertRaises(arc_ssh.ProtocolError):
                self.outcome("verified", result=("fail", "access", path), lines=1)

    def test_a_check_that_looked_is_a_finding(self):
        for result, message in ((("missing", SESSION), "Nothing of this upload is on ARC."),
                                (("elsewhere", SESSION), "ARC has a folder with this name that this upload did not "
                                                         "make.")):
            found = self.outcome("verified", result=result, lines=1)
            self.assertEqual(found.check, {"ok": False, "looked": True, "message": message, "fix": None})
        self.assertEqual(self.outcome("verified", demo=True, result=("missing", SESSION), lines=1).check["message"],
                         "Demo: nothing of this upload is in the demo folder.")


class TerminalText(unittest.TestCase):
    """Decision I9 (DOC-01, SEC-V02, DOC-05, DOC-06): the Terminal text follows the order of ssh's questions."""

    def setUp(self):
        arc_support.guard(self)

    def plan(self, known_hosts=""):
        record = {"destination": {"arc_folder": ARC_FOLDER, "area": arc_session.AREA_REAL, "base": ARC_FOLDER},
                  "session": {"points": 13}, "payload": {"files": 18, "bytes": 433104}}
        ctx = SimpleNamespace(session_folder=SESSION, upload_id=UPLOAD, demo=False, demo_root=None,
                              settings_used={"ssh_username": USER, "ssh_host": HOST, "ssh_known_hosts": known_hosts})
        return arc_ssh.Plan(ctx, record, "upload")

    def test_the_host_key_question_comes_before_the_password(self):
        for check in (False, True):
            text = arc_ssh.terminal_text(self.plan(), check=check)
            order = [text.index(words) for words in ("Are you sure you want to continue connecting",
                                                     "Your VT password", "Duo: approve the push on your phone")]
            self.assertEqual(order, sorted(order), text)
            self.assertIn("Type only yes or no here; what you type is shown on screen, so never\n     type your "
                          "password at this question.", text)
            self.assertIn("a comma and a\n     six-digit passcode at the password question. Choose Duo Push if a "
                          "numbered menu appears.", text)
            self.assertIn("ssh may print a WARNING that the connection is not using a post-quantum key exchange",
                          text)
            self.assertIn("\nARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN.\n", text)
            self.assertNotIn("Type 1", text)
            self.assertTrue(all(len(line) <= 100 for line in text.splitlines()[3:]), text)

    def test_no_host_key_question_with_the_teams_file(self):
        folder = Path(tempfile.mkdtemp(prefix="known_hosts_")).resolve()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        known_hosts = folder / "arc_known_hosts"
        known_hosts.write_text("datatransfer.arc.vt.edu ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST\n")
        os.chmod(known_hosts, 0o600)
        text = arc_ssh.terminal_text(self.plan(str(known_hosts)))
        self.assertNotIn("Are you sure", text)
        self.assertIn(TEAM_FILE_STEPS.format(path=known_hosts), text)


class DemoNames(unittest.TestCase):
    """SSH-10: the file names this Mac's tar (bsdtar) writes with other bytes than the stream holds."""

    # True: bsdtar changes the name (a precomposed letter, Hangul, combining marks out of their canonical
    # order). False: it keeps it (ASCII, decomposed already, no decomposition, a singleton, the ranges
    # macOS never decomposes, composites that NFC would not make again, compatibility forms).
    NAMES = {
        "notiz_ü.txt": True, "café.txt": True, "가.txt": True, "ἀ.txt": True,
        "Å.txt": True, "ẛ.txt": True, "ぱ.txt": True, "ȩ́.txt": True,
        "plain.txt": False, "ü.txt": False, "日本.txt": False, "\U0001F600.txt": False,
        "豈.txt": False, "Å.txt": False, "Ω.txt": False, "≠.txt": False, "̈́.txt": False,
        "क़.txt": False, "יִ.txt": False, "ﾊﾟ.txt": False,
    }

    def setUp(self):
        arc_support.guard(self)

    def test_the_rule(self):
        for name, changed in self.NAMES.items():
            self.assertEqual(arc_ssh.mac_tar_changes(name), changed, ascii(name))

    def test_the_rule_agrees_with_this_macs_tar(self):
        folder = Path(tempfile.mkdtemp(prefix="names_")).resolve()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        names = sorted(self.NAMES)
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for index, name in enumerate(names):
                entry = tarfile.TarInfo("%02d_%s" % (index, name))
                entry.size = 2
                tar.addfile(entry, io.BytesIO(b"x\n"))
        subprocess.run(["/usr/bin/tar", "-x", "-f", "-", "-C", str(folder), "--no-same-owner"], input=out.getvalue(),
                       env={"PATH": arc_ssh.LOCAL_PATH}, check=True, capture_output=True, timeout=60)
        found = {entry[:2]: entry for entry in os.listdir(os.fsencode(folder))}
        for index, name in enumerate(names):
            kept = found[b"%02d" % index] == ("%02d_%s" % (index, name)).encode("utf-8")
            self.assertEqual(not kept, arc_ssh.mac_tar_changes(name), ascii(name))


# ---------------------------------------------------------------------- the two far ends


class Runners(arc_support.ArcCase):
    def test_ssh_runs_with_pipes_and_the_terminal_as_its_stderr(self):
        seen = {}

        def popen(argv, **options):
            seen.update(argv=list(argv), options=options)
            return mock.Mock()

        argv = arc_ssh.ssh_argv(USER, HOST, "", "COMMAND")
        with mock.patch.object(arc_worker, "popen_program", side_effect=popen):
            arc_ssh.SshRunner(argv).start()
        self.assertEqual(seen["argv"], argv)
        self.assertEqual(seen["options"], {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE})

    def test_the_guard_refuses_ssh(self):
        with self.assertRaises(GuardRefused):
            arc_ssh.SshRunner(arc_ssh.ssh_argv(USER, HOST, "", "COMMAND")).start()

    def test_the_demo_runs_the_script_with_bin_bash(self):
        runner = arc_ssh.LocalBashRunner("upload", "/tmp/demo base", SESSION, UPLOAD, SESSION)
        self.assertEqual(runner.argv, ["/bin/bash", "-c", arc_ssh.REMOTE_SCRIPT.read_text(encoding="utf-8"),
                                       "raman_arc_upload", "upload", "/tmp/demo base", SESSION, UPLOAD, SESSION])
        with mock.patch.dict(os.environ, {"BASH_ENV": "/tmp/x", "BASH_FUNC_ls%%": "() { :; }", "PATH": "/opt/x"}):
            env = arc_ssh._local_environment()
        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertNotIn("BASH_ENV", env)
        self.assertNotIn("BASH_FUNC_ls%%", env)

    def test_texts(self):
        self.assertEqual(arc_ssh.copy_text(250000, 600000), "Copying over SSH: 41% (0.25 of 0.6 MB).")
        self.assertEqual(arc_ssh.copy_text(0, 433104), "Copying over SSH: 0% (0 of 0.43 MB).")
        self.assertEqual(arc_ssh._size_text(433104), "433 kB")
        self.assertEqual(arc_ssh._digest("%s  dark/a b.txt" % ("0" * 64)), ("dark/a b.txt", "0" * 64))
        self.assertIsNone(arc_ssh._digest("%s dark/a.txt" % ("0" * 64)))
        self.assertIsNone(arc_ssh._digest("xyz  a.txt"))
        self.assertIsNone(arc_ssh.difference({"a": "1", "b": "2"}, {"a": "1", "b": "2"}))
        self.assertEqual(arc_ssh.difference({"a": "1", "b": "2"}, {"a": "1", "b": "3"}), "b")
        self.assertEqual(arc_ssh.difference({"b": "2"}, {"a": "1", "b": "2"}), "a")
        self.assertEqual(arc_ssh.difference(None, {}), "SHA256SUMS")


# ---------------------------------------------------------------------- live SSH, with the stand-in ssh


class LiveSsh(arc_support.ArcCase):
    """A live SSH upload as the Terminal window runs it: the worker with --terminal, ssh = fake_ssh.py."""

    settings = LIVE_SSH

    def setUp(self):
        super().setUp()
        arc_support.write_settings(self.home, self.settings)
        arc_support.ready_session(self.home, points=13, simulated=False)
        self.program, tools = fake_ssh_program(self)
        self.arc = Path(tempfile.mkdtemp(prefix="fake_arc_")).resolve()
        self.addCleanup(shutil.rmtree, self.arc, ignore_errors=True)
        self.addCleanup(arc_support.make_writable, self.arc)
        self.log = tools / "ssh.log"
        self.saved = tools / "stream.tar"
        self.base = self.arc / "projects" / "pcl_photonics" / "raman_globus_upload"
        environment = {"RAMAN_FAKE_SSH_ROOT": str(self.arc), "RAMAN_FAKE_SSH_LOG": str(self.log)}
        self.opened = []
        for patcher in (mock.patch.dict(os.environ, environment),
                        mock.patch.object(arc_ssh, "SSH_PROGRAM", str(self.program)),
                        mock.patch.object(arc_commands, "setup_errors", return_value=[]),
                        mock.patch.object(doctor, "_running_commands", return_value=[]),
                        mock.patch.object(arc_worker, "run_program", side_effect=self._open)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.upload_id = self.ok("arc-upload", "--session=" + SESSION)["upload"]["upload_id"]
        self.folder = self.record_dir(self.upload_id)
        self.stage = self.base / "_incoming" / ("%s.%s" % (SESSION, self.upload_id))

    def _open(self, argv, timeout, **options):
        self.assertEqual(list(argv[:3]), ["/usr/bin/open", "-a", "Terminal"])
        self.opened.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def work(self, action="send", attempt=None, **environment):
        """The Terminal worker of the record's current attempt, in this process: (exit code, Terminal text)."""
        if attempt is None:
            attempt = self.record(self.upload_id)["ssh"]["attempt"]
        terminal = io.StringIO()
        with mock.patch.dict(os.environ, {name: str(value) for name, value in environment.items()}), \
                contextlib.redirect_stderr(terminal):
            code = arc_worker.run_worker(self.home, self.upload_id, action, terminal=True, attempt=attempt)
        return code, terminal.getvalue()

    def runs(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def expected_files(self):
        """{path: sha256} of every file the upload holds (SHA256SUMS lists them; itself included here)."""
        sums = (self.folder / "SHA256SUMS").read_bytes()
        found = {path: digest for digest, path in (line.split("  ", 1) for line in sums.decode().splitlines())}
        found["SHA256SUMS"] = hashlib.sha256(sums).hexdigest()
        return found

    def assert_one_folder(self, name=SESSION, others=()):
        """The upload is on ARC once, complete, and nothing of it is left in _incoming."""
        self.assertEqual(sorted(path.name for path in self.base.iterdir()), sorted([name, "_incoming", *others]))
        self.assertEqual(list((self.base / "_incoming").iterdir()), [])
        self.assertEqual(files_below(self.base / name), self.expected_files())

    def state(self):
        record = self.record(self.upload_id)
        error = record.get("error") or {}
        return record["state"], error.get("reason")

    def payload_bytes(self):
        return self.record(self.upload_id)["progress"]["bytes"]

    def kill_after_done(self):
        """A worker killed after ARC put the copy in place, before this Mac wrote it down (13, row 14)."""
        with mock.patch.object(arc_worker, "fault", side_effect=killed_at("ssh_after_done")), \
                self.assertRaises(Killed):
            self.work()
        self.assertEqual(self.state(), ("placing", None))

    # -- uploads

    def test_an_upload_is_checked_on_arc_and_put_in_place(self):
        code, terminal = self.work(RAMAN_FAKE_SSH_SAVE=self.saved)
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        record = self.record(self.upload_id)
        files = record["payload"]["files"]
        final = "%s/%s" % (ARC_FOLDER, SESSION)
        self.assertEqual(record["state"], "verified")
        self.assertEqual(record["message"], "On ARC: %d files in %s. ARC computed the SHA-256 of every file, and each "
                                            "matches this Mac's copy." % (files, final))
        verification = record["verification"]
        self.assertEqual((verification["method"], verification["algorithm"], verification["files"]),
                         ("sha256-on-arc", "SHA-256", files))
        self.assertEqual((record["destination"]["placed_as"], record["destination"]["final_path"]), (SESSION, final))
        self.assertEqual(record["progress"]["bytes_done"], record["payload"]["bytes"])
        self.assert_one_folder()
        for directory, folders, names in os.walk(self.base / SESSION):
            for name in folders:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode), 0o750)
            for name in names:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode), 0o640)
        # The Terminal window: the text of 11.4, then the steps, then the end.
        size = "%d kB" % round(record["payload"]["bytes"] / 1000)
        self.assertTrue(terminal.startswith(TEXT_11_4.format(folder=SESSION, files=files, size=size, user=USER)),
                        terminal)
        self.assertIn("Done: all %d files are on ARC and match this Mac's copy.\n  %s\n" % (files, final), terminal)
        self.assertTrue(terminal.rstrip().endswith(record["message"]), terminal)
        # ssh: exactly the command line of 11.2, one connection.
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        command = arc_ssh.remote_command("upload", ARC_FOLDER, SESSION, self.upload_id, SESSION)
        self.assertEqual(runs[0]["argv"], arc_ssh.ssh_argv(USER, HOST, "", command)[1:])
        ssh = record["ssh"]
        self.assertTrue(ssh["sent"])
        self.assertEqual({key: ssh["runs"][-1][key] for key in ("attempt", "action", "mode", "exit_code", "outcome")},
                         {"attempt": 1, "action": "send", "mode": "upload", "exit_code": 0, "outcome": "placed"})
        states = [event["state"] for event in arc_records.read_events(self.folder)]
        self.assertEqual([state for index, state in enumerate(states) if index == 0 or states[index - 1] != state],
                         ["queued", "waiting_for_terminal", "preparing", "signing_in", "checking", "transferring",
                          "verifying", "placing", "verified"])
        self.assertFalse(record["payload"]["kept"])
        # The stream that arrived: the snapshot only, with nothing of this Mac's accounts.
        with tarfile.open(self.saved, mode="r|") as tar:
            members = list(tar)
        self.assertEqual(members[0].name, arc_payload.MANIFEST_NAME)
        for member in members:
            self.assertEqual((member.uid, member.gid, member.uname, member.gname, member.pax_headers),
                             (0, 0, "", "", {}))
            self.assertEqual(member.mode, 0o750 if member.isdir() else 0o640)
            self.assertNotIn("._", member.name)
        self.assertEqual(sorted(member.name for member in members if member.isfile()), sorted(self.expected_files()))

    def test_the_teams_known_hosts_file(self):
        folder = Path(tempfile.mkdtemp(prefix="known_hosts_")).resolve()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        known_hosts = folder / "arc_known_hosts"
        known_hosts.write_text("datatransfer.arc.vt.edu ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITEST\n")
        os.chmod(known_hosts, 0o600)
        arc_records.update_record(self.folder, lambda record: record["settings_used"].update(
            ssh_known_hosts=str(known_hosts)))
        code, terminal = self.work()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        argv = self.runs()[0]["argv"]
        self.assertEqual(argv[9:15], ["-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=%s" % known_hosts,
                                      "-o", "GlobalKnownHostsFile=/dev/null"])
        self.assertIn("\nssh checks ARC's key against the team's file %s; it does not ask.\n" % known_hosts, terminal)
        self.assertNotIn("The first time", terminal)
        self.assertIn("\n" + TEAM_FILE_STEPS.format(path=known_hosts), terminal)

    def test_a_known_hosts_file_others_may_change_is_refused_before_ssh(self):
        folder = Path(tempfile.mkdtemp(prefix="known_hosts_")).resolve()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        known_hosts = folder / "arc_known_hosts"
        known_hosts.write_text("x\n")
        os.chmod(known_hosts, 0o666)
        arc_records.update_record(self.folder, lambda record: record["settings_used"].update(
            ssh_known_hosts=str(known_hosts)))
        self.assertEqual(self.work()[0], arc_worker.EXIT_ENDED)
        self.assertEqual(self.state(), ("failed", "known_hosts_unreadable"))
        self.assertEqual(self.runs(), [])

    def test_a_record_changed_by_hand_never_reaches_ssh(self):
        arc_records.update_record(self.folder, lambda record: record["destination"].update(
            base=ARC_FOLDER + "'; touch /tmp/x; '"))
        self.assertEqual(self.work()[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "internal"))
        self.assertEqual(self.runs(), [])

    def test_the_same_upload_again_finds_its_copy_in_place(self):
        self.kill_after_done()
        self.assertTrue((self.base / SESSION).is_dir())
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual(shown["display_state"], "interrupted")
        self.assertIn("retry", shown["actions"])
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.record(self.upload_id)["ssh"]["attempt"], 2)
        code, terminal = self.work()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        record = self.record(self.upload_id)
        self.assertEqual(record["ssh"]["runs"][-1]["outcome"], "already")
        self.assertEqual(record["destination"]["placed_as"], SESSION)
        self.assert_one_folder()
        self.assertEqual(len(self.runs()), 2)

    def test_a_name_already_on_arc_is_a_conflict_until_kept_as_a_new_copy(self):
        theirs = self.base / SESSION
        theirs.mkdir(parents=True)
        (theirs / "theirs.txt").write_text("another person's folder\n")
        code, _terminal = self.work()
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["conflict_at"], record["error"]), ("conflict", "placing", None))
        self.assertEqual(record["message"], "Your copy is complete and checked in %s/_incoming/%s.%s on ARC. Keep it "
                                            "as a new copy, or move the other folder on ARC and press Retry same "
                                            "request."
                         % (ARC_FOLDER, SESSION, self.upload_id))
        self.assertEqual(files_below(self.stage), self.expected_files())
        self.assertEqual(files_below(theirs), {"theirs.txt": hashlib.sha256(b"another person's folder\n").hexdigest()})
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["actions"],
                         ["keep_copy", "retry", "stop"])

        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["ssh"]["attempt"], record["destination"]["new_copy"]),
                         ("waiting_for_terminal", 2, True))
        code, terminal = self.work()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        record = self.record(self.upload_id)
        self.assertEqual(record["destination"]["placed_as"], SESSION + "_copy2")
        self.assertEqual(record["destination"]["final_path"], "%s/%s_copy2" % (ARC_FOLDER, SESSION))
        self.assertEqual(self.runs()[-1]["mode"], "upload-copy")
        self.assertIn("%s/%s (a new copy: this name or the first free _copyN)" % (ARC_FOLDER, SESSION), terminal)
        # The checked copy in _incoming was put in place without being sent again.
        events = [event["state"] for event in arc_records.read_events(self.folder)]
        self.assertEqual(events[events.index("conflict"):].count("transferring"), 0)
        self.assert_one_folder(SESSION + "_copy2", others=[SESSION])

    def test_a_connection_cut_while_sending_is_finished_by_retry(self):
        code, _terminal = self.work(RAMAN_FAKE_SSH_CUT_AFTER=self.payload_bytes() // 2)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "ssh_interrupted", True))
        self.assertEqual(record["message"], "The SSH connection ended during the upload. Files sent so far are in "
                                            "%s on ARC." % incoming)
        # SSH sends every file again on Retry (SSH-05): the fix says so, and promises no resume.
        self.assertEqual(record["error"]["fix"], "Press Retry same request: all files are sent again, into the same "
                                                 "folder on ARC.")
        self.assertTrue(record["ssh"]["sent"])
        self.assertEqual(record["ssh"]["runs"][-1]["outcome"], "cut")
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["actions"], shown["leftover_on_arc"]), (["retry", "check"], incoming))
        self.assertTrue(self.stage.is_dir())
        self.assertFalse((self.base / SESSION).exists())

        self.ok("arc-retry", "--upload=" + self.upload_id)
        code, terminal = self.work()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        self.assert_one_folder()

    def test_a_byte_changed_on_the_way_is_found_and_copied_again(self):
        # The manifest is the first entry: its content starts at byte 512 of the stream.
        code, _terminal = self.work(RAMAN_FAKE_SSH_FLIP_AT=517)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "verify_mismatch"))
        self.assertEqual(record["error"]["message"], "The copy on ARC does not match this Mac's list of files (for "
                                                     "example raman_upload_manifest.json). It was not put in place.")
        self.assertFalse((self.base / SESSION).exists())
        self.ok("arc-retry", "--upload=" + self.upload_id)
        code, terminal = self.work()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        self.assert_one_folder()

    def test_signing_in_that_fails_changes_nothing_on_arc(self):
        code, terminal = self.work(RAMAN_FAKE_SSH_EXIT=255)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "ssh_signin_failed", True))
        self.assertEqual(record["message"], "Signing in to ARC did not work (ssh ended with code 255). The Terminal "
                                            "window shows ssh's own reason. Nothing was changed on ARC.")
        self.assertIn("If ssh says the host key changed, stop and ask the team: do not continue.", terminal)
        # Decision I9: ARC refuses VT Open WiFi, which a person may take for the VT network (DOC-05).
        # Review DOC-V03: without an ARC account in the project, ssh ends the same way.
        self.assertEqual(record["error"]["fix"], "Check your VT username in ARC settings, your password and Duo, and "
                                                 "that you have an ARC account in the project that owns the ARC "
                                                 "folder. ARC refuses VT Open WiFi: use eduroam, a wired VT "
                                                 "connection or the VT VPN. Then press Retry same request. If ssh "
                                                 "says the host key changed, stop and ask the team: do not continue.")
        self.assertFalse(record["ssh"]["sent"])
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["actions"], ["retry"])
        self.assertEqual(list(self.arc.iterdir()), [])

    def test_a_host_that_does_not_run_commands(self):
        self.assertEqual(self.work(RAMAN_FAKE_SSH_REFUSE=1)[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "ssh_host_refused_commands", False))
        self.assertIn("tinkercliffs1.arc.vt.edu", record["error"]["fix"])
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["actions"], [])

    def test_a_host_without_the_tools_the_upload_needs(self):
        tools = Path(tempfile.mkdtemp(prefix="remote_tools_")).resolve()
        self.addCleanup(shutil.rmtree, tools, ignore_errors=True)
        for name in ("bash", "base64", "cat", "mkdir", "find", "sort", "sed", "grep", "mv", "tar"):
            os.symlink(shutil.which(name, path=arc_ssh.LOCAL_PATH), tools / name)
        self.assertEqual(self.work(RAMAN_FAKE_SSH_PATH=tools)[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "remote_tools", False))
        self.assertEqual(record["message"], "ARC lacks a tool the upload needs: neither sha256sum nor shasum is "
                                            "installed.")
        self.assertEqual(list(self.arc.iterdir()), [])

    def test_an_arc_folder_that_is_a_link_on_arc(self):
        elsewhere = self.arc / "elsewhere"
        elsewhere.mkdir()
        self.base.parent.mkdir(parents=True)
        os.symlink(elsewhere, self.base)
        self.assertEqual(self.work()[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "remote_failed", True))
        self.assertEqual(record["message"], "ARC could not make the folder: %s is a link." % ARC_FOLDER)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse(record["ssh"]["sent"])

    def test_lines_a_remote_bashrc_prints_are_left_out(self):
        code, terminal = self.work(RAMAN_FAKE_SSH_BANNER="Welcome to ARC. Status: RAMAN STEP place is not a step.")
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        self.assert_one_folder()

    def test_an_earlier_attempts_window_does_nothing(self):
        arc_records.update_record(self.folder, lambda record: record["ssh"].update(attempt=2))
        code, terminal = self.work(attempt=1)
        self.assertEqual(code, arc_worker.EXIT_NOT_ITS_TURN)
        self.assertIn(arc_worker.EARLIER_ATTEMPT_TEXT, terminal)
        self.assertEqual(self.runs(), [])

    # -- Check status (11.5): its five answers

    def check(self, **environment):
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.opened[-1][-1], str(self.folder / "ssh_upload.command"))
        self.assertIn("--action check", (self.folder / "ssh_upload.command").read_text())
        return self.work("check", **environment)

    def test_check_of_a_verified_upload_finds_it_still_there(self):
        self.work()
        code, terminal = self.check()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertEqual(record["checks"][-1]["ok"], True)
        self.assertEqual(record["checks"][-1]["message"], "Still on ARC: %d files in %s/%s, every one matching this "
                         "Mac's list (SHA-256)." % (record["payload"]["files"], ARC_FOLDER, SESSION))
        self.assertTrue(terminal.startswith("Portable Raman: check an upload on ARC over SSH\n"), terminal)
        self.assertIn("To stop, press Ctrl-C. A check sends, moves and deletes nothing.", terminal)
        self.assertEqual(self.runs()[-1]["mode"], "check")
        self.assertEqual(self.record(self.upload_id)["ssh"]["runs"][-1]["action"], "check")

        # Changed on ARC by someone: the check says so, and the upload stays verified.
        point = self.base / SESSION / "point_0003.json"
        os.chmod(point, 0o640)
        point.write_bytes(point.read_bytes() + b"\n")
        self.assertEqual(self.check()[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["checks"][-1]["ok"]), ("verified", False))
        self.assertIn("does not match", record["checks"][-1]["message"])

    def test_check_of_an_upload_in_place_that_this_mac_did_not_write_down(self):
        self.kill_after_done()
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertTrue(answer["stopped_now"])
        self.assertEqual(self.state(), ("stopped", None))
        # SSH-V01: arc-stop says the copy may already be in its folder, not that files stay in _incoming.
        self.assertEqual((answer["message"], self.record(self.upload_id)["message"]),
                         (arc_records.MESSAGES["stopped_placing"],) * 2)
        # SSH-V01: the copy is in its final folder, so no folder is named as left in _incoming.
        self.assertTrue(self.record(self.upload_id)["ssh"]["may_be_placed"])
        self.assertIsNone(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["leftover_on_arc"])
        code, terminal = self.check()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["destination"]["placed_as"]), ("verified", SESSION))
        self.assertEqual(record["verification"]["method"], "sha256-on-arc")
        self.assert_one_folder()

    def test_check_finds_a_complete_copy_in_incoming(self):
        (self.base / SESSION).mkdir(parents=True)
        self.work()
        self.assertEqual(self.state(), ("conflict", None))
        self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertEqual(self.check()[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]),
                         ("stopped", "The copy is complete in _incoming on ARC but not in place yet. Retry same "
                                     "request puts it in place."))

    def test_check_finds_part_of_the_copy(self):
        self.work(RAMAN_FAKE_SSH_CUT_AFTER=self.payload_bytes() // 2)
        error = self.record(self.upload_id)["error"]
        self.assertEqual(self.check()[0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"], record["error"]),
                         ("failed", "Part of the copy is in _incoming on ARC. Retry same request finishes it.", error))

    def test_check_finds_nothing_or_another_folder(self):
        self.work(RAMAN_FAKE_SSH_CUT_AFTER=self.payload_bytes() // 2)
        arc_support.make_writable(self.stage)
        shutil.rmtree(self.stage)
        self.check()
        self.assertEqual(self.record(self.upload_id)["message"], "Nothing of this upload is on ARC.")
        (self.base / SESSION).mkdir()
        self.check()
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]),
                         ("failed", "ARC has a folder with this name that this upload did not make."))

    def test_check_that_cannot_sign_in_keeps_the_state(self):
        # REG-01: ssh ended with 255 (a mistyped password, Duo refused): the check could not look. The upload stays
        # on ARC and needs nobody, Upload to ARC still says so, and the Terminal window ends with what to do.
        self.work()
        self.ok("arc-check", "--upload=" + self.upload_id)
        code, terminal = self.work("check", RAMAN_FAKE_SSH_EXIT=255)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        note = record["checks"][-1]
        self.assertEqual((record["state"], note["ok"], note["looked"]), ("verified", False, False))
        self.assertIn("ssh ended with code 255", note["message"])
        self.assertEqual(note["message"], "Signing in to ARC did not work (ssh ended with code 255). The Terminal "
                                          "window shows ssh's own reason. Nothing was changed on ARC. Check status "
                                          "could not look.")
        self.assertEqual(note["fix"], arc_records.CHECK_FIXES["ssh_signin_failed"])
        self.assertTrue(terminal.endswith("\n%s\n%s\n" % (note["message"], note["fix"])), terminal[-500:])
        self.assertNotIn(arc_records.CHECK_FAILED_TERMINAL, terminal)
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["display_label"], shown["last_check"]),
                         ("verified", "On ARC, checked", note))
        self.assertEqual(self.ok("status")["arc_attention"], 0)
        self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + SESSION, "--dry-run")

    def test_a_check_that_could_not_look_says_who_can_help(self):
        # Reviews R2-03 and R2-06: a copy this VT account may no longer read, a folder on the way to it that became a
        # link, a computer without the tools, and one that runs no commands. Each check could not look: a note in
        # the check's own words with its fix, and the upload stays on ARC, needs nobody and is not sent again.
        self.work()
        final = self.base / SESSION
        tools = Path(tempfile.mkdtemp(prefix="remote_tools_")).resolve()
        self.addCleanup(shutil.rmtree, tools, ignore_errors=True)
        for name in ("bash", "base64", "cat", "mkdir", "find", "sort", "sed", "grep", "mv", "tar"):
            os.symlink(shutil.which(name, path=arc_ssh.LOCAL_PATH), tools / name)
        moved = self.arc / "moved_away"

        def unreadable():
            os.chmod(final, 0)
            return lambda: os.chmod(final, 0o750)

        def linked():
            self.base.rename(moved)
            os.symlink(moved, self.base)
            return lambda: (os.remove(self.base), moved.rename(self.base))

        cases = (
            ("folder_not_readable", unreadable, {},
             "Your VT account may not read %s/%s on ARC." % (ARC_FOLDER, SESSION)),
            ("folder_refused", linked, {}, "ARC refused a folder on the way to the copy: %s is a link." % ARC_FOLDER),
            ("remote_tools", lambda: None, {"RAMAN_FAKE_SSH_PATH": tools},
             "The check cannot run on %s: neither sha256sum nor shasum is installed." % HOST),
            ("ssh_host_refused_commands", lambda: None, {"RAMAN_FAKE_SSH_REFUSE": 1},
             "The check cannot run on %s: that computer did not run its commands." % HOST))
        for reason, change, environment, why in cases:
            with self.subTest(reason=reason):
                undo = change()
                try:
                    self.ok("arc-check", "--upload=" + self.upload_id)
                    code, terminal = self.work("check", **environment)
                finally:
                    if undo:
                        undo()
                self.assertEqual(code, arc_worker.EXIT_ENDED)
                note = self.record(self.upload_id)["checks"][-1]
                self.assertEqual({key: note[key] for key in ("ok", "looked", "message", "fix")},
                                 {"ok": False, "looked": False, "message": why + " Check status could not look.",
                                  "fix": arc_records.CHECK_FIXES[reason]})
                self.assertTrue(terminal.endswith("\n%s\n%s\n" % (note["message"], note["fix"])), terminal[-500:])
                shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
                self.assertEqual((shown["display_state"], shown["last_check"]), ("verified", note))
                self.assertEqual(self.ok("status")["arc_attention"], 0)
                self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + SESSION, "--dry-run")
        # Everything as it was: the check looks and finds the copy.
        self.assertEqual(self.check()[0], arc_worker.EXIT_VERIFIED)
        self.assertTrue(self.record(self.upload_id)["checks"][-1]["ok"])
        self.assert_one_folder()

    # -- stopping (11.4 step 8; 13 row 15)

    def test_a_stop_request_ends_ssh(self):
        done = []
        thread = threading.Thread(target=lambda: done.append(self.work(RAMAN_FAKE_SSH_STALL_AFTER=60000)))
        thread.start()
        self.addCleanup(thread.join, 30)
        deadline = time.monotonic() + 30
        while self.state()[0] != "transferring" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.state()[0], "transferring")
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertFalse(answer["stopped_now"])
        thread.join(30)
        self.assertEqual(done[0][0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]),
                         ("stopped", "Stopped. Files already copied stay in %s/_incoming/%s.%s on ARC; nothing was "
                                     "deleted." % (ARC_FOLDER, SESSION, self.upload_id)))
        self.assertEqual(record["ssh"]["runs"][-1]["outcome"], "stopped")
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual(shown["actions"], ["retry", "check"])
        # Stopped while copying: the files are in _incoming, and the listing names that folder.
        self.assertFalse(record["ssh"]["may_be_placed"])
        self.assertEqual(shown["leftover_on_arc"], "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id))
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work()[0], arc_worker.EXIT_VERIFIED)
        self.assert_one_folder()

    def worker_process(self, action="send", **environment):
        """The upload program in a process of its own, as Terminal starts it, with the stand-in ssh. A send
        is returned once it copies; a check once ssh has started (the record keeps its state)."""
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        env.update(RAMAN_FAKE_SSH_ROOT=str(self.arc), RAMAN_FAKE_SSH_LOG=str(self.log))
        env.update({name: str(value) for name, value in environment.items()})
        terminal = open(self.log.with_name("terminal.txt"), "wb")
        self.addCleanup(terminal.close)
        runs = len(self.runs())
        process = subprocess.Popen(
            [sys.executable, "-c", DRIVER, str(self.program), "--home", str(self.home), "--upload", self.upload_id,
             "--action", action, "--terminal", "--attempt", str(self.record(self.upload_id)["ssh"]["attempt"])],
            cwd=str(self.home), env=env, stdin=subprocess.DEVNULL, stdout=terminal, stderr=terminal,
            start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL))

        def ready():
            return self.state()[0] == "transferring" if action == "send" else len(self.runs()) > runs

        deadline = time.monotonic() + 30
        while not ready() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        self.assertTrue(ready(), self.log.with_name("terminal.txt").read_text())
        return process

    def test_ctrl_c_in_the_terminal_window_stops_the_upload(self):
        process = self.worker_process(RAMAN_FAKE_SSH_STALL_AFTER=60000)
        os.killpg(process.pid, signal.SIGINT)
        self.assertEqual(process.wait(30), arc_worker.EXIT_INTERRUPTED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "stopped")
        self.assertIsNone(record["worker"])
        self.assertTrue(arc_support.lock_is_free(self.folder / "worker.lock"))

    def test_closing_the_terminal_window_fails_the_attempt_and_retry_finishes_it(self):
        process = self.worker_process(RAMAN_FAKE_SSH_STALL_AFTER=60000)
        os.killpg(process.pid, signal.SIGHUP)
        self.assertEqual(process.wait(30), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "terminal_closed", True))
        self.assertIn(b"worker", (self.folder / "worker.log").read_bytes())
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work()[0], arc_worker.EXIT_VERIFIED)
        self.assert_one_folder()

    # -- shared folders on ARC (decision I7) and what the Mac stops sending (SSH-05)

    def slow_writer(self, seconds):
        """The stream writer waits this long after each file; returns the patcher (stop() ends it)."""
        real = arc_ssh.Attempt._on_file

        def slow(attempt, relative):
            time.sleep(seconds)
            return real(attempt, relative)

        patcher = mock.patch.object(arc_ssh.Attempt, "_on_file", slow)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def stream_bytes(self):
        """The size of the whole tar stream of this upload."""
        out = io.BytesIO()
        arc_ssh.write_tar(arc_payload.payload_folder(self.folder) / SESSION, out)
        return len(out.getvalue())

    def test_a_shared_folder_another_member_made_ends_the_upload_before_it_is_sent(self):
        incoming = self.base / "_incoming"
        incoming.mkdir(parents=True)
        os.chmod(incoming, 0o550)
        slow = self.slow_writer(0.25)
        code, terminal = self.work(RAMAN_FAKE_SSH_SAVE=self.saved)
        self.assertEqual(code, arc_worker.EXIT_ENDED, terminal)
        record = self.record(self.upload_id)
        shown = ARC_FOLDER + "/_incoming"
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "shared_folder_not_writable", True))
        self.assertEqual(record["message"], "Your VT account may not write into %s on ARC: that shared folder is not "
                                            "group-writable." % shown)
        self.assertEqual(record["error"]["fix"], "Ask the folder's owner to run chmod 3770 %s on ARC (README section "
                                                 "14), or to add you to the ARC project if you are not in it. Then "
                                                 "press Retry same request." % shown)
        self.assertIn(record["error"]["fix"], terminal)
        self.assertFalse(record["ssh"]["sent"])
        self.assertEqual(list(incoming.iterdir()), [])
        # ARC said so before it read the stream, and the Mac stopped sending it at once.
        self.assertLess(self.saved.stat().st_size, self.stream_bytes() // 2)
        # The folder's owner makes it shared (README section 14); Retry same request then works.
        slow.stop()
        os.chmod(incoming, 0o3770)
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work()[0], arc_worker.EXIT_VERIFIED)
        self.assert_one_folder()

    def assert_not_writable(self, code, terminal, folder):
        """The upload ended before anything was sent, naming `folder` (as ARC knows it) and its owner's fix."""
        self.assertEqual(code, arc_worker.EXIT_ENDED, terminal)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "shared_folder_not_writable", True))
        self.assertEqual(record["message"], "Your VT account may not write into %s on ARC: that shared folder is not "
                                            "group-writable." % folder)
        self.assertEqual(record["error"]["fix"], "Ask the folder's owner to run chmod 3770 %s on ARC (README section "
                                                 "14), or to add you to the ARC project if you are not in it. Then "
                                                 "press Retry same request." % folder)
        self.assertIn(record["error"]["fix"], terminal)
        self.assertFalse(record["ssh"]["sent"])

    def test_the_arc_folder_above_simulated_without_group_write_is_named(self):
        # REG-02: the first simulated upload into an ARC folder another member made without group write. The script
        # names the ARC folder itself, since simulated/ is still to be made in it: shared_folder_not_writable with
        # the chmod 3770 fix for that folder, not an error of this program.
        arc_support.write_settings(self.home, dict(LIVE_SSH, include_simulated=True))
        arc_support.ready_session(self.home, folder=OTHER_SESSION, points=13, simulated=True)
        self.upload_id = self.ok("arc-upload", "--session=" + OTHER_SESSION)["upload"]["upload_id"]
        self.base.mkdir(parents=True)
        os.chmod(self.base, 0o550)
        self.assert_not_writable(*self.work(), ARC_FOLDER)
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertIn("retry", self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["actions"])
        # The folder's owner makes it shared (README section 14); Retry same request then works.
        os.chmod(self.base, 0o3770)
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work()[0], arc_worker.EXIT_VERIFIED)
        self.assertTrue((self.base / "simulated" / OTHER_SESSION / "session.json").is_file())

    def test_an_arc_folder_without_group_write_and_without_incoming_is_named(self):
        # SSH-02, the case F3 left: the ARC folder exists without group write, and _incoming is still to be made in
        # it. The script names the ARC folder before anything is received (FAIL access), and the Mac says so.
        self.base.mkdir(parents=True)
        os.chmod(self.base, 0o550)
        self.assert_not_writable(*self.work(), ARC_FOLDER)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_a_copy_changed_on_arc_after_it_was_placed_is_not_sent_again(self):
        # SSH-06: Retry finds this upload's own copy in place, changed by someone on ARC since.
        self.kill_after_done()
        point = self.base / SESSION / "point_0003.json"
        point.write_bytes(point.read_bytes() + b"\n")
        self.ok("arc-retry", "--upload=" + self.upload_id)
        code, terminal = self.work()
        self.assertEqual(code, arc_worker.EXIT_ENDED, terminal)
        record = self.record(self.upload_id)
        final = "%s/%s" % (ARC_FOLDER, SESSION)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "placed_changed", False))
        self.assertEqual(record["message"], "This upload's copy on ARC (%s) was changed after it was put in place: it "
                                            "no longer matches this Mac's list of files (for example "
                                            "point_0003.json)." % final)
        self.assertEqual((record["destination"]["placed_as"], record["destination"]["final_path"]), (SESSION, final))
        self.assertEqual(record["ssh"]["runs"][-1]["outcome"], "fail_verify")
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertNotIn("retry", shown["actions"])
        self.assertIn("check", shown["actions"])
        self.assertIsNone(shown["leftover_on_arc"])
        self.assertEqual(list((self.base / "_incoming").iterdir()), [])
        self.check()
        self.assertEqual(self.record(self.upload_id)["message"], "A copy of this upload is on ARC (%s), but it does "
                                                                 "not match this Mac's list (for example "
                                                                 "point_0003.json)." % final)

    def test_a_stop_that_crosses_the_rename_on_arc(self):
        # SSH-V01 and SSH-11: sshd only closes the script's pipes when ssh ends, so a stop that comes after
        # STEP place cannot keep the script from its rename. The record must not say "in _incoming".
        done = []
        environment = {"RAMAN_FAKE_SSH_LIKE_SSHD": 1, "RAMAN_FAKE_SSH_HOLD_AFTER": "RAMAN STEP place"}
        thread = threading.Thread(target=lambda: done.append(self.work(**environment)))
        thread.start()
        self.addCleanup(thread.join, 30)
        deadline = time.monotonic() + 30
        while self.state()[0] != "placing" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.state()[0], "placing")
        self.assertFalse(self.ok("arc-stop", "--upload=" + self.upload_id)["stopped_now"])
        thread.join(30)
        self.assertEqual(done[0][0], arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("stopped", arc_records.MESSAGES["stopped_placing"]))
        self.assertEqual(record["ssh"]["runs"][-1]["outcome"], "stopped")
        self.assertTrue(record["ssh"]["may_be_placed"])
        self.assertIsNone(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["leftover_on_arc"])
        deadline = time.monotonic() + 10
        while not (self.base / SESSION).is_dir() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assert_one_folder()
        self.assertEqual(self.check()[0], arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.record(self.upload_id)["state"], "verified")

    # -- Check status in the Terminal window (decisions I1, I3 and I8)

    def test_check_status_in_the_terminal_says_what_it_found(self):
        # SSH-01: the window ends with the check's own message, ok or not, never the stored one.
        self.work()
        preamble = "A check sends, moves and deletes nothing.\n"
        code, terminal = self.check()
        self.assertEqual(code, arc_worker.EXIT_VERIFIED, terminal)
        self.assertIn(self.record(self.upload_id)["checks"][-1]["message"], terminal.split(preamble, 1)[1])
        point = self.base / SESSION / "point_0003.json"
        point.write_bytes(point.read_bytes() + b"\n")
        for environment in ({}, {"RAMAN_FAKE_SSH_EXIT": 255}, {"moved": True}):
            with self.subTest(environment=environment):
                if environment.pop("moved", False):
                    os.rename(self.base / SESSION, self.base.parent / "moved_away")
                code, terminal = self.check(**environment)
                self.assertEqual(code, arc_worker.EXIT_ENDED, terminal)
                record = self.record(self.upload_id)
                found = record["checks"][-1]
                self.assertEqual((record["state"], found["ok"]), ("verified", False))
                after = terminal.split(preamble, 1)[1]
                self.assertIn(found["message"], after)
                self.assertNotIn("each matches this Mac's copy", after)

    def close_the_check_window(self):
        """Check status in a Terminal window of its own, closed (SIGHUP) while ssh waits for the password."""
        before = self.record(self.upload_id)
        self.ok("arc-check", "--upload=" + self.upload_id)
        process = self.worker_process("check", RAMAN_FAKE_SSH_HOLD=60)
        os.killpg(process.pid, signal.SIGHUP)
        process.wait(30)
        after = self.record(self.upload_id)
        self.assertEqual((after["state"], after["message"], after["error"]),
                         (before["state"], before["message"], before["error"]))
        self.assertIsNone(after["worker"])
        self.assertTrue(arc_support.lock_is_free(self.folder / "worker.lock"))

    def test_closing_the_window_of_a_check_keeps_a_failed_upload_as_it_was(self):
        # SSH-03, decision I8: a check worker's closed window is no failure of the upload.
        self.work(RAMAN_FAKE_SSH_CUT_AFTER=self.payload_bytes() // 2)
        self.assertEqual(self.state(), ("failed", "ssh_interrupted"))
        self.close_the_check_window()

    def test_closing_the_window_of_a_check_keeps_a_stopped_upload_as_it_was(self):
        self.kill_after_done()
        self.assertTrue(self.ok("arc-stop", "--upload=" + self.upload_id)["stopped_now"])
        self.assertEqual(self.state(), ("stopped", None))
        self.close_the_check_window()

    def test_a_check_waiting_for_its_sign_in_can_be_stopped_and_holds_up_no_upload(self):
        # SEC-01, decision I3: Stop is offered while the check waits; ssh ends, the upload stays verified.
        self.work()
        arc_support.ready_session(self.home, folder=OTHER_SESSION, points=13, simulated=False)
        self.ok("arc-check", "--upload=" + self.upload_id)
        done = []
        thread = threading.Thread(target=lambda: done.append(self.work("check", RAMAN_FAKE_SSH_HOLD=60)))
        thread.start()
        self.addCleanup(thread.join, 30)
        deadline = time.monotonic() + 30
        while not [run for run in self.runs() if run["mode"] == "check"] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("stop", self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["actions"])
        self.ok("arc-upload", "--session=" + OTHER_SESSION, "--dry-run")
        self.assertFalse(self.ok("arc-stop", "--upload=" + self.upload_id)["stopped_now"])
        thread.join(30)
        self.assertFalse(thread.is_alive())
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertEqual({key: record["ssh"]["runs"][-1][key] for key in ("action", "outcome")},
                         {"action": "check", "outcome": "stopped"})


# ---------------------------------------------------------------------- demo SSH (design 12)


class DemoSsh(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home, points=13)
        self.patch_programs()

    def patch_programs(self):
        """Every program the demo starts is recorded: only /bin/bash may run."""
        self.programs = []
        real = arc_worker.popen_program

        def popen(argv, **options):
            self.programs.append(list(argv))
            return real(argv, **options)

        patcher = mock.patch.object(arc_worker, "popen_program", side_effect=popen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, *options):
        return self.ok("arc-upload", "--session=" + SESSION, "--demo", "--transport=ssh", *options)["upload"]

    def work(self, upload_id, action="send"):
        return arc_worker.run_worker(self.home, upload_id, action)

    def final(self, name=SESSION):
        return self.home / "data_catalog" / "arc_demo" / ARC_FOLDER.lstrip("/") / "simulated" / name

    def test_a_demo_upload_runs_the_real_script_with_bin_bash(self):
        upload_id = self.upload()["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        record = self.record(upload_id)
        files = record["payload"]["files"]
        self.assertEqual(record["message"], "Demo: %d files in the demo folder on this Mac, every one checked. Nothing "
                                            "left this Mac." % files)
        self.assertEqual(record["destination"]["local_final_path"], str(self.final()))
        self.assertEqual(len(files_below(self.final())), files)
        root = self.home / "data_catalog" / "arc_demo"
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual([argv[0] for argv in self.programs if argv[0] != arc_worker.CAFFEINATE], ["/bin/bash"])
        self.assertEqual(self.programs[0][3:5], ["raman_arc_upload", "upload"])
        messages = [event["text"] for event in arc_records.read_events(self.record_dir(upload_id))]
        self.assertIn("Demo: no Terminal and no password. A real SSH upload asks for your VT password and Duo in a "
                      "Terminal window here.", messages)
        self.assertTrue(all(text.startswith("Demo: ") for text in messages if not text.startswith("Copied ")),
                        messages)

        self.ok("arc-check", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_VERIFIED)
        self.assertTrue(self.record(upload_id)["checks"][-1]["ok"])

    def test_a_check_that_may_not_read_a_folder_is_a_note(self):
        # Review R2-06 in the demo, which runs the same script: the copy's folder, then simulated/ above it, cannot
        # be read by this Mac's account. Each check could not look: a note in the demo's words with the demo's fix,
        # never "nothing of this upload is in the demo folder". The upload stays in the demo folder.
        upload_id = self.upload()["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        for folder in (self.final(), self.final().parent):
            with self.subTest(folder=folder.name):
                kept = stat.S_IMODE(folder.stat().st_mode)
                os.chmod(folder, 0)
                try:
                    self.ok("arc-check", "--upload=" + upload_id)
                    self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_ENDED)
                finally:
                    os.chmod(folder, kept)
                note = self.record(upload_id)["checks"][-1]
                self.assertEqual({key: note[key] for key in ("ok", "looked", "message", "fix")}, {
                    "ok": False, "looked": False,
                    "message": "Demo: this Mac's account may not read %s in the demo folder. Check status could not "
                               "look." % folder,
                    "fix": arc_records.CHECK_FIXES["folder_not_readable_demo"]})
                shown = self.ok("arc-status", "--upload=" + upload_id)["upload"]
                self.assertEqual((shown["display_state"], shown["display_label"]), ("verified", "On ARC, checked"))
                self.assertEqual(self.ok("arc-list")["attention"], 0)
                self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + SESSION, "--demo",
                             "--transport=ssh", "--dry-run")
        self.ok("arc-check", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_VERIFIED)
        self.assertTrue(self.record(upload_id)["checks"][-1]["ok"])

    def test_a_check_that_may_not_read_a_folder_in_the_copy_is_a_note(self):
        # Review R3-03: this Mac's account may not read the copy's dark/ folder. The check names it before it compares
        # the copy, so it could not look: a note, as a Globus check gives, never "it does not match this Mac's list".
        # The upload stays in the demo folder, needs nobody, and a new upload still finds it there.
        upload_id = self.upload()["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        dark = self.final() / "dark"
        self.assertTrue(any(dark.iterdir()), "the copy has its dark spectrum in dark/")
        kept = stat.S_IMODE(dark.stat().st_mode)
        os.chmod(dark, 0)
        try:
            self.ok("arc-check", "--upload=" + upload_id)
            self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_ENDED)
        finally:
            os.chmod(dark, kept)
        note = self.record(upload_id)["checks"][-1]
        self.assertEqual({key: note[key] for key in ("ok", "looked", "message", "fix")}, {
            "ok": False, "looked": False,
            "message": "Demo: this Mac's account may not read %s in the demo folder. Check status could not "
                       "look." % dark,
            "fix": "Make that folder and the folders in it readable again for this Mac's account. Then press Check "
                   "status again."})
        shown = self.ok("arc-status", "--upload=" + upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["display_label"]), ("verified", "On ARC, checked"))
        self.assertEqual(self.ok("arc-list")["attention"], 0)
        self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + SESSION, "--demo", "--transport=ssh",
                     "--dry-run")
        # Readable again: the next check looks and finds the copy.
        self.ok("arc-check", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_VERIFIED)
        self.assertTrue(self.record(upload_id)["checks"][-1]["ok"])

    def test_the_demo_fix_of_a_check_says_what_helps(self):
        # Review R3-02: every check of an upload looks in the demo folder saved with it, so another demo folder in ARC
        # settings cannot help, and the fix does not suggest one. simulated/ was moved and a link put in its place:
        # the check names the link; once the moved folder is back in place of the link, the check finds the copy.
        upload_id = self.upload()["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        simulated = self.final().parent
        moved = simulated.parent / "simulated_moved"
        simulated.rename(moved)
        os.symlink(moved, simulated)
        arc_support.write_settings(self.home, {"demo_folder": "data_catalog/arc_demo_other"})
        self.ok("arc-check", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_ENDED)
        note = self.record(upload_id)["checks"][-1]
        self.assertEqual({key: note[key] for key in ("ok", "looked", "message", "fix")}, {
            "ok": False, "looked": False,
            "message": "Demo: the demo refused a folder on the way to the copy: %s is a link. Check status could not "
                       "look." % simulated,
            "fix": "Nothing was changed. Put the moved folder back in place of the link. Then press Check status "
                   "again."})
        os.remove(simulated)
        moved.rename(simulated)
        self.ok("arc-check", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_VERIFIED)
        self.assertTrue(self.record(upload_id)["checks"][-1]["ok"])

    def test_the_teaching_fault_drop_connection(self):
        upload_id = self.upload("--demo-fault=drop_connection")["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        incoming = self.final().parent / "_incoming" / ("%s.%s" % (SESSION, upload_id))
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "ssh_interrupted"))
        self.assertEqual(record["message"], "Demo: the SSH connection ended during the upload. Files sent so far are "
                                            "in %s." % incoming)
        self.assertTrue(record["ssh"]["demo_fault_used"])
        texts = [event["text"] for event in arc_records.read_events(self.record_dir(upload_id))]
        self.assertIn(arc_ssh.DROPPED, texts)
        self.assertTrue(incoming.is_dir())
        self.assertFalse(self.final().exists())

        self.ok("arc-retry", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        self.assertEqual(sorted(path.name for path in self.final().parent.iterdir()), [SESSION, "_incoming"])
        self.assertEqual(list(incoming.parent.iterdir()), [])
        texts = [event["text"] for event in arc_records.read_events(self.record_dir(upload_id))]
        self.assertEqual(texts.count(arc_ssh.DROPPED), 1)

    def test_an_arc_folder_of_the_demo_folder_that_may_not_be_written_is_named(self):
        # REG-02 in the demo, which runs the same script: the demo's ARC folder exists without write access and
        # simulated/ is still to be made in it. The upload ends before anything is sent, in the demo's words for
        # that folder (COR-05), and Retry same request finishes it once the folder is writable again.
        upload_id = self.upload()["upload_id"]
        top = self.final().parent.parent
        arc_records.ensure_demo_root(self.home / "data_catalog" / "arc_demo")
        top.mkdir(parents=True)
        os.chmod(top, 0o550)
        self.addCleanup(os.chmod, top, 0o700)
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "shared_folder_not_writable", True))
        self.assertEqual(record["message"], "Demo: this Mac's account may not write into %s, a folder of the demo "
                                            "folder." % top)
        self.assertEqual(record["error"]["fix"], "Make it writable again (in Terminal: chmod u+w %s). Then press "
                                                 "Retry same request: it uses the demo folder saved with the upload."
                         % top)
        self.assertNotIn("another demo folder", record["error"]["fix"])
        self.assertEqual(list(top.iterdir()), [])
        os.chmod(top, 0o700)
        self.ok("arc-retry", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        self.assertTrue((self.final() / "session.json").is_file())

    def test_a_demo_stop_before_anything_was_sent(self):
        upload_id = self.upload()["upload_id"]

        def stopped(ctx):
            ctx.request_stop()
            return arc_ssh.send(ctx)

        with mock.patch.object(arc_worker._Run, "entry_point", lambda run: stopped):
            self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["message"]), ("stopped", "Demo: stopped before anything was sent."))
        self.assertEqual(self.programs, [])

    def add_file(self, name):
        """A file of the session with this name (the session stays older than the settle time)."""
        folder = self.home / "scan_sessions" / SESSION
        (folder / name).write_text("a note\n", encoding="utf-8")
        arc_support.set_age(folder, 600)

    def test_a_name_this_macs_tar_would_change_is_refused_before_anything_is_sent(self):
        # SSH-10: bsdtar writes "ü" decomposed, so the demo's check would report a mismatch.
        self.add_file("notiz_ü.txt")
        upload_id = self.upload()["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "demo_ssh_name", False))
        self.assertEqual(record["message"], "Demo: this Mac's tar changes accented letters in file names, so demo SSH "
                                            "cannot send notiz_ü.txt: its check would fail. Nothing was sent.")
        self.assertEqual(record["error"]["fix"], "Try the demo with Globus. A live upload over SSH keeps such names as "
                                                 "they are.")
        self.assertEqual([argv for argv in self.programs if argv[0] == "/bin/bash"], [])

    def test_a_name_this_macs_tar_keeps_goes_through(self):
        self.add_file("note_日本.txt")
        upload_id = self.upload()["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        self.assertTrue((self.final() / "note_日本.txt").is_file())

    def test_a_demo_folder_this_program_did_not_make_is_never_taken_over(self):
        # SEC-05, decision I6: an existing folder is used as it is, or refused; its mode is never changed.
        upload_id = self.upload()["upload_id"]
        root = self.home / "data_catalog" / "arc_demo"
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True)
        (root / "theirs.txt").write_text("not the demo's\n")
        os.chmod(root, 0o755)
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "demo_folder_unwritable"))
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["theirs.txt"])
        self.assertEqual([argv for argv in self.programs if argv[0] == "/bin/bash"], [])

    def test_the_demo_folder_comes_from_the_shared_helper(self):
        # Decision I6: demo SSH asks arc_records.ensure_demo_root itself, not the Globus demo's module.
        upload_id = self.upload()["upload_id"]
        with mock.patch.object(arc_records, "ensure_demo_root", wraps=arc_records.ensure_demo_root) as helper, \
                mock.patch.object(arc_demo, "ensure_demo_root", side_effect=AssertionError("not the shared helper")):
            self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        self.assertIn(Path(self.record(upload_id)["destination"]["demo_root"]),
                      [Path(call.args[0]) for call in helper.call_args_list])

    def test_a_far_end_that_names_another_folder_is_internal(self):
        upload_id = self.upload()["upload_id"]
        real = arc_ssh.Attempt._line

        def other_folder(attempt, raw):
            if raw.startswith(b"RAMAN DONE placed "):
                raw = raw.rstrip(b"\n") + b"_elsewhere\n"
            return real(attempt, raw)

        with mock.patch.object(arc_ssh.Attempt, "_line", other_folder):
            self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "internal"))
        self.assertIn("(ProtocolError)", record["error"]["message"])


# ---------------------------------------------------------------------- the Terminal script (design 9.3)


RECORDER = """#!/bin/sh
# Stands in for the interpreter: writes what it was started with.
{
  for argument in "$@"; do printf 'ARG=%s\\n' "$argument"; done
  printf 'PATH=%s\\n' "$PATH"
  printf 'PYTHONPATH=%s\\n' "$PYTHONPATH"
  printf 'SSH_ASKPASS=%s\\n' "${SSH_ASKPASS-(unset)}"
  printf 'SSH_ASKPASS_REQUIRE=%s\\n' "${SSH_ASKPASS_REQUIRE-(unset)}"
  printf 'DISPLAY=%s\\n' "${DISPLAY-(unset)}"
  printf 'PYTHONSTARTUP=%s\\n' "${PYTHONSTARTUP-(unset)}"
  printf 'PWD=%s\\n' "$(pwd -P)"
} > "$RAMAN_RECORD_TO"
exit 3
"""


class TerminalScript(arc_support.ArcCase):
    def test_bash_runs_the_worker_with_every_value_intact(self):
        home = Path(tempfile.mkdtemp(prefix="raman home 'a' \"b\" $(c) ")).resolve()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        recorder = home / "my python's bin" / "python3"
        recorder.parent.mkdir()
        recorder.write_text(RECORDER)
        os.chmod(recorder, 0o755)
        data_layer = home / "data layer"
        text = arc_worker.terminal_script_text(home, UPLOAD, 3, "check", SESSION, interpreter=str(recorder),
                                               data_layer=str(data_layer))
        script = home / "ssh_upload.command"
        script.write_text(text)
        os.chmod(script, 0o700)
        written = home / "written.txt"
        env = {"PATH": "/opt/elsewhere/bin:/usr/bin:/bin", "SSH_ASKPASS": "/opt/askpass",
               "SSH_ASKPASS_REQUIRE": "force", "DISPLAY": ":0", "PYTHONSTARTUP": "/opt/startup.py",
               "RAMAN_RECORD_TO": str(written)}
        done = subprocess.run(["/bin/bash", str(script)], stdin=subprocess.DEVNULL, capture_output=True, env=env,
                              timeout=30)
        # The worker's exit code, after the "Press Return" line (read gets no input here).
        self.assertEqual(done.returncode, 3, done.stderr)
        lines = written.read_text().splitlines()
        self.assertEqual([line[4:] for line in lines if line.startswith("ARG=")],
                         ["-m", "raman_datafed.arc_worker", "--home", str(home), "--upload", UPLOAD, "--action",
                          "check", "--terminal", "--attempt", "3"])
        values = dict(line.split("=", 1) for line in lines if not line.startswith("ARG="))
        self.assertEqual(values, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": str(data_layer),
                                  "SSH_ASKPASS": "(unset)", "SSH_ASKPASS_REQUIRE": "(unset)", "DISPLAY": "(unset)",
                                  "PYTHONSTARTUP": "(unset)", "PWD": str(home)})
        # shlex reads the worker line the same way.
        self.assertEqual(shlex.split(text.splitlines()[8])[0], str(recorder))

    def test_the_script_is_written_anew_for_each_attempt_and_holds_no_secret(self):
        arc_support.ready_session(self.home)
        record = self.record(self.upload(*("--transport=ssh",))["upload"]["upload_id"])
        folder = self.record_dir(record["upload_id"])
        record["ssh"]["attempt"] = 4
        path = arc_worker.write_terminal_script(self.home, folder, record, "send", 4)
        first = os.stat(path)
        self.assertEqual(stat.S_IMODE(first.st_mode), 0o700)
        os.chmod(path, 0o777)
        path = arc_worker.write_terminal_script(self.home, folder, record, "send", 5)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)
        text = path.read_text()
        self.assertIn("--attempt 5\n", text)
        for secret in (USER, "token", "Bearer", "code=", "SSH_ASKPASS=", "sshpass"):
            self.assertNotIn(secret, text)


# ---------------------------------------------------------------------- section 13, rows 13 and 14, as real processes


class Recovery(arc_support.ArcCase):
    """The detached upload program as a process of its own (demo SSH, the guard on)."""

    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home, points=13)

    def final(self, name=SESSION):
        return self.home / "data_catalog" / "arc_demo" / ARC_FOLDER.lstrip("/") / "simulated" / name

    def program(self, upload_id, **environment):
        code, _stdout, stderr = arc_support.run_worker_program(
            self.home, "--upload", upload_id, "--action", "send", extra_environment=environment)
        return code

    def assert_one_folder(self):
        base = self.final().parent
        self.assertEqual(sorted(path.name for path in base.iterdir()), [SESSION, "_incoming"])
        self.assertEqual(list((base / "_incoming").iterdir()), [])

    def test_row_13_a_connection_cut_while_sending(self):
        upload_id = self.ok("arc-upload", "--session=" + SESSION, "--demo", "--transport=ssh",
                            "--demo-fault=drop_connection")["upload"]["upload_id"]
        self.assertEqual(self.program(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "ssh_interrupted"))
        self.assertFalse(self.final().exists())
        self.assertTrue(self.final().parent.joinpath("_incoming", "%s.%s" % (SESSION, upload_id)).is_dir())
        self.ok("arc-retry", "--upload=" + upload_id)
        self.assertEqual(self.spawned[-1], (str(self.home), upload_id, "send"))
        self.assertEqual(self.program(upload_id), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.record(upload_id)["state"], "verified")
        self.assert_one_folder()

    def test_row_14_the_copy_put_in_place_before_the_record_was_written(self):
        upload_id = self.ok("arc-upload", "--session=" + SESSION, "--demo", "--transport=ssh")["upload"]["upload_id"]
        hooks = {"RAMAN_ARC_TEST_HOOKS": "1", "RAMAN_ARC_FAULT": "ssh_after_done"}
        self.assertEqual(self.program(upload_id, **hooks), arc_worker.EXIT_FAULT)
        record = self.record(upload_id)
        self.assertEqual(record["state"], "placing")
        self.assertTrue(self.final().is_dir())
        shown = self.ok("arc-status", "--upload=" + upload_id)["upload"]
        self.assertEqual(shown["display_state"], "interrupted")
        self.assertIn("retry", shown["actions"])
        self.ok("arc-retry", "--upload=" + upload_id)
        self.assertEqual(self.program(upload_id), arc_worker.EXIT_VERIFIED)
        record = self.record(upload_id)
        self.assertEqual((record["state"], record["ssh"]["runs"][-1]["outcome"]), ("verified", "already"))
        self.assert_one_folder()


if __name__ == "__main__":
    unittest.main()
