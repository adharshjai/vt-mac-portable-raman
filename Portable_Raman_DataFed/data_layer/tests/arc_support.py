"""Shared helpers of the tests of uploads to ARC (PROTOCOL.md 5.6).

Every ARC test sets RAMAN_ARC_TEST_GUARD=1 (the data layer then refuses to run ssh, open,
caffeinate or osascript, to sign in to Globus, and to start anything but its own worker), and
no_network() makes every socket connection fail, so no test can reach a browser, a Terminal or a
server. The instruments are never touched: the simulate variables are set as a third net.
"""

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import support
from raman_datafed import arc_records, arc_worker, cli
from raman_datafed.config import Paths

GUARD_ENVIRONMENT = {
    "RAMAN_ARC_TEST_GUARD": "1",
    "RAMAN_ARC_DEMO_PACE": "0",
    "RAMAN_BACKEND_SIMULATE": "1",
    "RAMAN_MOTOR_SIMULATE": "1",
}
SESSION = "1d_x_scan_20261113_101500_123"
# A VT username of the tests: plainly made up (never an example of a real one).
TEST_USERNAME = "test_user_0"
ARC_FOLDER = "/projects/pcl_photonics/raman_globus_upload"


def guard(test):
    """The guard variables for the length of a test; the fault hooks off."""
    patcher = mock.patch.dict(os.environ, GUARD_ENVIRONMENT)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in ("RAMAN_ARC_TEST_HOOKS", "RAMAN_ARC_FAULT"):
        if name in os.environ:
            removed = mock.patch.dict(os.environ)
            removed.start()
            test.addCleanup(removed.stop)
            del os.environ[name]


def _refuse_connection(*_arguments, **_options):
    raise AssertionError("an ARC test tried to open a network connection")


def no_network(test):
    """socket.socket.connect and socket.create_connection raise for the length of a test."""
    for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection")):
        patcher = mock.patch.object(target, name, _refuse_connection)
        patcher.start()
        test.addCleanup(patcher.stop)


def make_writable(folder):
    """Give the read-only snapshot folders their write permission back, so that the HOME can be removed."""
    for directory, folders, _files in os.walk(folder):
        for name in folders:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass


def arc_home(test):
    """An empty HOME, removed when the test ends (read-only snapshots included)."""
    home = support.make_home(test)
    # Cleanups run last in, first out: this one runs before make_home's removal.
    test.addCleanup(make_writable, home)
    return home


def set_age(path, seconds):
    """Set the modification time of a folder and everything below it to `seconds` ago."""
    moment = time.time() - seconds
    for directory, folders, files in os.walk(path):
        for name in folders + files:
            os.utime(os.path.join(directory, name), (moment, moment), follow_symlinks=False)
    os.utime(path, (moment, moment))


def ready_session(home, folder=SESSION, points=13, age=600, dark=True, **fields):
    """A finished session whose files are older than the settle time, with its dark spectrum."""
    if dark and not (Path(home) / "dark_measurements" / support.DARK_NAME).exists():
        support.write_dark(home, simulated=fields.get("simulated", True))
    session = support.make_session(home, folder=folder, points=points, **fields)
    set_age(session["folder"], age)
    return session


def write_settings(home, arc_upload=None, **core):
    """datafed_config.json with an arc_upload group (and core values)."""
    data = {"mode": "local-demo"}
    data.update(core)
    if arc_upload is not None:
        data["arc_upload"] = arc_upload
    path = Path(home) / "datafed_config.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


class ArcCase(unittest.TestCase):
    """A test of the ARC part: the guard, no network, a HOME, and spawn() recorded instead of run."""

    spawn_workers = False

    def setUp(self):
        guard(self)
        no_network(self)
        self.home = arc_home(self)
        self.paths = Paths(self.home)
        self.spawned = []
        if not self.spawn_workers:
            patcher = mock.patch.object(arc_worker, "spawn", side_effect=self._record_spawn)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _record_spawn(self, home, upload_id, action):
        self.spawned.append((str(home), upload_id, action))

    def invoke(self, *argv):
        """Run one command in this process; the answer as the command line prints it."""
        with cli.Context(self.home) as ctx:
            return cli.invoke(ctx, [str(item) for item in argv])

    def ok(self, *argv):
        answer = self.invoke(*argv)
        self.assertTrue(answer.get("ok"), answer)
        return answer

    def refused(self, kind, reason, *argv):
        answer = self.invoke(*argv)
        self.assertFalse(answer.get("ok"), answer)
        self.assertEqual((answer["kind"], answer["reason"]), (kind, reason), answer)
        self.assertTrue(answer["fix"], answer)
        return answer

    def record_dir(self, upload_id):
        return arc_records.record_folder(self.paths, upload_id)

    def record(self, upload_id):
        return arc_records.read_record(self.record_dir(upload_id))

    def upload(self, *options, session=SESSION):
        """arc-upload of a session (demo unless --live is given); returns the answer."""
        mode = () if "--live" in options or "--demo" in options else ("--demo",)
        return self.ok("arc-upload", "--session=" + session, *mode, *options)


def fake_transport(test, function):
    """Make the worker call `function(ctx)` instead of the transport module (arc_globus or arc_ssh)."""
    patcher = mock.patch.object(arc_worker._Run, "entry_point", lambda run: function)
    patcher.start()
    test.addCleanup(patcher.stop)


def placing_transport(ctx):
    """A transport that does what a successful one does, without any far end: the copy in place."""
    ctx.set_state("signing_in", ctx.text("Signed in."))
    ctx.set_state("checking", ctx.text("Checking."))
    ctx.set_state("transferring", ctx.text("Copying."))
    ctx.set_state("verifying", ctx.text("Checking the copy."))
    ctx.set_state("placing", ctx.text("Putting the copy in place."))
    ctx.placed_as(ctx.session_folder)
    return arc_worker.Outcome("verified", ctx.text("On ARC."), fields={"verification": {"method": "test"}})


def hold_lock(test, path):
    """A child process that holds an flock on `path` until the test ends."""
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys, time\n"
         "handle = open(sys.argv[1], 'a')\n"
         "fcntl.flock(handle, fcntl.LOCK_EX)\n"
         "print('locked', flush=True)\n"
         "time.sleep(120)\n", str(path)],
        stdout=subprocess.PIPE, text=True)
    test.addCleanup(holder.stdout.close)
    test.addCleanup(holder.wait)
    test.addCleanup(holder.kill)
    test.assertEqual(holder.stdout.readline().strip(), "locked")
    return holder


def lock_is_free(path):
    handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(handle)


def wait_for_workers(home, timeout=30):
    """Wait until no worker holds a worker.lock under HOME (a real worker started by a test)."""
    folder = arc_records.uploads_folder(Paths(home))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        locks = list(folder.glob("u*/" + arc_records.WORKER_LOCK)) if folder.is_dir() else []
        if all(lock_is_free(path) for path in locks):
            return True
        time.sleep(0.05)
    return False


def run_cli(home, *arguments, extra_environment=None, timeout=60):
    """The command line as the controller runs it, with the guard: (exit code, object, stderr)."""
    env = support.cli_environment(home)
    env.update(GUARD_ENVIRONMENT)
    for name in ("RAMAN_ARC_TEST_HOOKS", "RAMAN_ARC_FAULT"):
        env.pop(name, None)
    env.update(extra_environment or {})
    done = subprocess.run([sys.executable, "-m", "raman_datafed", "--home", str(home), *map(str, arguments)],
                          capture_output=True, text=True, timeout=timeout, cwd=str(home), env=env)
    try:
        value = json.loads(done.stdout) if done.stdout.strip() else None
    except ValueError:
        value = done.stdout
    return done.returncode, value, done.stderr


def run_worker_program(home, *arguments, extra_environment=None, timeout=60):
    """python -m raman_datafed.arc_worker ... with the guard: (exit code, stdout, stderr)."""
    env = support.cli_environment(home)
    env.update(GUARD_ENVIRONMENT)
    env.update(extra_environment or {})
    done = subprocess.run([sys.executable, "-m", "raman_datafed.arc_worker", "--home", str(home),
                           *map(str, arguments)], capture_output=True, text=True, timeout=timeout, cwd=str(home),
                          env=env)
    return done.returncode, done.stdout, done.stderr


# ---------------------------------------------------------------------- end-to-end helpers (demo, recovery)

# The states in which nobody works on an upload any more until a person acts.
RESTING_STATES = ("verified", "failed", "stopped", "conflict", "uncertain")


def hooks(fault):
    """The variables that make arc_worker.fault(<fault>) end the process with code 97 (design 9.5)."""
    return {"RAMAN_ARC_TEST_HOOKS": "1", "RAMAN_ARC_FAULT": fault}


def wait_for_record(home, upload_id, states=RESTING_STATES, timeout=60):
    """The record once it is in one of `states` and no worker holds the upload (a real detached worker
    started by a command); the last record read when the time is up."""
    folder = arc_records.record_folder(Paths(home), upload_id)
    deadline = time.monotonic() + timeout
    record = None
    while time.monotonic() < deadline:
        try:
            record = arc_records.read_record(folder)
        except arc_records.UnreadableRecord:
            record = None
        if record is not None and record.get("state") in states and not arc_records.worker_alive(folder):
            return record
        time.sleep(0.05)
    return record


def demo_base(home, area="simulated"):
    """The demo folder that stands in for <ARC folder> (area real) or <ARC folder>/simulated."""
    base = Path(home) / "data_catalog" / "arc_demo" / ARC_FOLDER.lstrip("/")
    return base / "simulated" if area == "simulated" else base


def files_below(top):
    """{relative path: sha256} of every file under a folder."""
    import hashlib

    found = {}
    for directory, _folders, files in os.walk(top):
        for name in files:
            path = os.path.join(directory, name)
            with open(path, "rb") as stream:
                found[os.path.relpath(path, top)] = hashlib.sha256(stream.read()).hexdigest()
    return found


def same_as_manifest(test, folder):
    """The folder holds exactly the files its manifest lists, with their SHA-256, plus the manifest and
    SHA256SUMS, which lists every file but itself. Returns the manifest."""
    folder = Path(folder)
    manifest = json.loads((folder / arc_records.MANIFEST_NAME).read_text(encoding="utf-8"))
    expected = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    found = files_below(folder)
    test.assertEqual(set(found), set(expected) | {arc_records.MANIFEST_NAME, arc_records.SUMS_NAME}, folder)
    for path, digest in expected.items():
        test.assertEqual(found[path], digest, path)
    sums = (folder / arc_records.SUMS_NAME).read_text(encoding="utf-8").splitlines()
    listed = dict(reversed(line.split("  ", 1)) for line in sums)
    test.assertEqual(set(listed), set(expected) | {arc_records.MANIFEST_NAME})
    for path, digest in listed.items():
        test.assertEqual(found[path], digest, path)
    return manifest


# Runs the upload program as the Terminal window of a live SSH upload does, with a stand-in ssh.
TERMINAL_DRIVER = ("import sys\n"
                   "from raman_datafed import arc_ssh, arc_worker\n"
                   "arc_ssh.SSH_PROGRAM = sys.argv[1]\n"
                   "sys.exit(arc_worker.main(sys.argv[2:]))\n")
FAKE_SSH = Path(__file__).resolve().with_name("fake_ssh.py")


def fake_ssh_program(test):
    """An executable that runs tests/fake_ssh.py (its name is not "ssh", so the guard lets it run), in a
    temporary folder removed when the test ends. Returns (program, folder)."""
    import shlex
    import shutil
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="fake_ssh_")).resolve()
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    program = folder / "fake-ssh"
    program.write_text("#!/bin/sh\nexec %s -I %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(str(FAKE_SSH))))
    os.chmod(program, 0o755)
    return program, folder
