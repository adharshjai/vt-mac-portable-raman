"""Uploads to ARC: the window's stand-in and the Studio, against the real arc-* commands.

The window's tests (laser_test/tests/arc_ui_tests.cpp) talk to laser_test/tests/fake_data_layer.py, and
the Studio's page draws what its engine (studio/index.html) or its server makes. These tests run the
real arc-* commands on scratch HOMEs, with real upload programs, and check that the stand-in, the
Studio's engine and the Studio's server answer in the same shapes as the real commands: the keys of
every reply, the kinds and reasons of the refusals, and the states, labels, display states and actions
of the uploads (design 7.3, 7.5, 7.7, 8 and Appendix A).

Every upload here is a demo upload, with the guard on (RAMAN_ARC_TEST_GUARD=1) and the network off:
nothing leaves this Mac, and no browser, Terminal or ssh is started. The stand-in runs as the window
runs it, as a program of its own; the Studio's engine runs in node, which a test skips when it is
missing.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import arc_support
import support
import test_arc_studio as studio_tests   # the Studio server's test harness; it has no tests of its own here

from raman_datafed import arc_commands, arc_records, arc_session, arc_settings, arc_worker, cli, config

EDITION = support.DATA_LAYER.parent
FAKE = EDITION / "laser_test" / "tests" / "fake_data_layer.py"
STUDIO = EDITION / "studio" / "index.html"
PACE = arc_worker.PACE_VARIABLE

# Design 7.3: the stored states and their labels ("transferring" is named after its transport).
DESIGN_LABELS = {
    "queued": "Waiting to start", "waiting_for_terminal": "Waiting for Terminal", "preparing": "Preparing the files",
    "signing_in": "Signing in", "checking": "Checking ARC", "submitting": "Handing over to Globus",
    "transferring": {"globus": "Copying with Globus", "ssh": "Copying over SSH"},
    "verifying": "Checking the copy on ARC",
    "placing": "Putting the copy in place", "uncertain": "Not sure Globus got it", "conflict": "Name already on ARC",
    "verified": "On ARC, checked", "failed": "Failed", "stopped": "Stopped",
}
# Design 7.5: what the listing shows while nobody works on an upload in a working state; fix plan I1: a
# verified upload whose newest check failed.
DESIGN_DISPLAY_LABELS = {"not_watched": "Not being watched", "not_started": "Not started", "interrupted": "Interrupted",
                         "check_failed": "On ARC, last check failed"}
# Fix plan I2: the display states of uploads that need a person, and for how many days they count.
DESIGN_ATTENTION = ("failed", "conflict", "uncertain", "interrupted", "not_started", "not_watched", "check_failed")
DESIGN_ATTENTION_DAYS = 7
# Design 7.7: every button an upload may offer.
ACTIONS = ("stop", "check", "retry", "keep_copy", "copy_path", "show_in_finder")

# The session folders of both HOMEs. The first three are the stand-in's own (fake_data_layer.py).
LINE = arc_support.SESSION
SINGLE = "single_20261113_103000_456"
OLD = "2d_scan_20260924_141500_001"
YLINE = "1d_y_scan_20261113_104500_789"
TAKEN = "2d_scan_20261114_090000_001"
TAKEN_TOO = "2d_scan_20261114_093000_002"
SLOW = "1d_x_scan_20261114_110000_003"
YOUNG = "1d_x_scan_20261115_120000_004"
ODD = "copy of scan"     # a folder whose name the data layer does not take (odd_name)
# A made-up collection ID for the live dry runs (never contacted).
MAC_COLLECTION = "0f0e0d0c-0b0a-4000-8000-000000000001"
LIVE_SETTINGS = ("--set=arc_upload.include_simulated=true",
                 "--set=arc_upload.ssh.username=" + arc_support.TEST_USERNAME,
                 "--set=arc_upload.globus.mac_collection=" + MAC_COLLECTION)


# ---------------------------------------------------------------------- shapes


def shape(value):
    """The keys of a reply all the way down: an object gives {key: shape}, a list the merged shape of its
    items, a value "value" and null None (null has no shape: it agrees with anything)."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        found = None
        for item in value:
            found = merged(found, shape(item))
        return [found]
    return None if value is None else "value"


def merged(one, other):
    if one is None:
        return other
    if other is None:
        return one
    if isinstance(one, dict) and isinstance(other, dict):
        return {key: merged(one.get(key), other.get(key)) for key in set(one) | set(other)}
    if isinstance(one, list) and isinstance(other, list):
        return [merged(one[0], other[0])]
    if one == other:
        return one
    return "mixed"


def merged_shape(values):
    found = None
    for value in values:
        found = merged(found, shape(value))
    return found


def _what(found):
    return "an object" if isinstance(found, dict) else "a list" if isinstance(found, list) else str(found)


def differences(real, other, path="$"):
    """Where two shapes disagree: keys only one has, or an object against a list or a value."""
    if real is None or other is None:
        return []
    if isinstance(real, dict) and isinstance(other, dict):
        found = ["%s.%s: only in the real answer" % (path, key) for key in sorted(set(real) - set(other))]
        found += ["%s.%s: only in the other answer" % (path, key) for key in sorted(set(other) - set(real))]
        for key in sorted(set(real) & set(other)):
            found += differences(real[key], other[key], path + "." + key)
        return found
    if isinstance(real, list) and isinstance(other, list):
        return differences(real[0], other[0], path + "[]")
    if real == other:
        return []
    return ["%s: %s in the real answer, %s in the other" % (path, _what(real), _what(other))]


def items_in(reply):
    """Every upload item a reply holds: arc-list's items, and "upload" of the other replies."""
    if not isinstance(reply, dict):
        return []
    found = [item for item in reply.get("items") or [] if isinstance(item, dict) and "upload_id" in item]
    if isinstance(reply.get("upload"), dict):
        found.append(reply["upload"])
    if isinstance(reply.get("existing"), dict):
        found.append(reply["existing"])
    return found


ITEM_KEYS = sorted(arc_records.item({"schema": arc_records.SCHEMA, "state": "queued", "transport": "globus"}, False))
STATUS_EXTRA = ("attempts", "events", "manifest", "record_folder", "session")


def result_fix(reason, demo):
    """(fix, retryable) of a worker result, in the demo's words for a demo upload (arc_records.DEMO_RESULTS)."""
    _message, fix, retryable = arc_records.WORKER_RESULTS[reason]
    if demo and reason in arc_records.DEMO_RESULTS:
        fix = arc_records.DEMO_RESULTS[reason][1] or fix
    return fix, retryable


def label_of(state, transport):
    label = DESIGN_LABELS[state]
    return label[transport] if isinstance(label, dict) else label


# ---------------------------------------------------------------------- the two sides


def real_command(home, *argv):
    """One real command in this process, as the command line answers it."""
    with cli.Context(home) as ctx:
        return cli.invoke(ctx, [str(item) for item in argv])


def wait_until(home, upload_id, done, timeout=60):
    """The record once done(record) holds and no upload program holds the upload."""
    folder = arc_records.record_folder(config.Paths(home), upload_id)
    deadline = time.monotonic() + timeout
    record = None
    while time.monotonic() < deadline:
        try:
            record = arc_records.read_record(folder)
        except arc_records.UnreadableRecord:
            record = None
        if record is not None and done(record) and not arc_records.worker_alive(folder):
            return record
        time.sleep(0.05)
    raise AssertionError("upload %s did not get there in %d s: %s" % (upload_id, timeout, (record or {}).get("state")))


def worker_started(home, upload_id, timeout=20):
    folder = arc_records.record_folder(config.Paths(home), upload_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if arc_records.worker_alive(folder):
            return
        time.sleep(0.02)
    raise AssertionError("no upload program took upload %s" % upload_id)


class FakeDataLayer:
    """The window's stand-in, run as the window runs it: a program of its own on its own HOME."""

    def __init__(self, home):
        self.home = Path(home)
        self.control = {"arc": {"sessions": [
            {"folder": LINE}, {"folder": SINGLE},
            {"folder": YLINE, "session_id": "6a1b2c3d-4e5f-4a6b-8c7d-0e9f8a7b6c5d", "scan_mode": "1d-y",
             "sample_id": "S-001", "points": 13, "started_utc": "2026-11-13T15:45:00.789Z"},
            {"folder": TAKEN, "session_id": "7b2c3d4e-5f6a-4b7c-9d8e-1f0a9b8c7d6e", "scan_mode": "2d",
             "sample_id": "S-001", "points": 4, "end": "ended", "started_utc": "2026-11-14T14:00:00.001Z"},
            {"folder": TAKEN_TOO, "session_id": "8c3d4e5f-6a7b-4c8d-ae9f-2a1b0c9d8e7f", "scan_mode": "2d",
             "sample_id": "S-001", "points": 4, "end": "ended", "started_utc": "2026-11-14T14:30:00.002Z"},
            {"folder": SLOW, "session_id": "9d4e5f6a-7b8c-4d9e-bf0a-3b2c1d0e9f8a", "sample_id": "S-001",
             "started_utc": "2026-11-14T16:00:00.003Z"},
            {"folder": YOUNG, "session_id": "ae5f6a7b-8c9d-4eaf-80b1-4c3d2e1f0a9b", "sample_id": "S-001", "points": 2,
             "end": "ended", "why_not": "settling", "retry_after_seconds": 59,
             "started_utc": "2026-11-15T17:00:00.004Z"},
            {"folder": OLD},
            {"folder": ODD, "why_not": "odd_name", "points": 3},
        ]}}

    def set(self, **arc):
        self.control["arc"].update(arc)
        (self.home / "fake_data_layer_control.json").write_text(json.dumps(self.control), encoding="utf-8")

    def __call__(self, *argv):
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        done = subprocess.run([sys.executable, "-I", str(FAKE), "--home", str(self.home), *map(str, argv)],
                              capture_output=True, text=True, timeout=60, cwd=str(self.home), env=env)
        if done.stdout.count("\n") != 1:
            raise AssertionError("the stand-in did not print one JSON object for %s: %r %r"
                                 % (argv, done.stdout, done.stderr))
        return json.loads(done.stdout)

    def listed(self, times):
        """arc-list, `times` times: every upload of the stand-in moves one step of its script each time."""
        answer = None
        for _ in range(times):
            answer = self("arc-list", "--limit=12")
        return answer


def item_of(answer, upload_id):
    for item in answer["items"]:
        if item["upload_id"] == upload_id:
            return item
    raise AssertionError("upload %s is not listed" % upload_id)


def build_real(home):
    """The scenarios on the real side: (replies by kind, items by scenario, refusals by scenario)."""
    replies, pairs, refusals = {}, {}, {}

    def keep(kind, answer):
        replies.setdefault(kind, []).append(answer)
        return answer

    def run(*argv):
        return real_command(home, *argv)

    def started(answer):
        assert answer.get("ok"), answer
        return answer["upload"]["upload_id"]

    def listed(upload_id):
        return item_of(keep("list", run("arc-list", "--limit=12")), upload_id)

    keep("sessions", run("arc-sessions", "--limit=30"))
    keep("dry_run", run("arc-upload", "--session=" + LINE, "--transport=globus", "--demo", "--dry-run"))
    line = started(keep("upload", run("arc-upload", "--session=" + LINE, "--transport=globus", "--demo")))
    wait_until(home, line, lambda record: record["state"] in arc_support.RESTING_STATES)
    pairs["verified_globus"] = listed(line)
    folder = arc_records.record_folder(config.Paths(home), line)
    for name in (arc_records.RECORD_FILE, arc_records.MANIFEST_NAME, "globus_request.json"):
        keep("file:" + name, json.loads((folder / name).read_text(encoding="utf-8")))
    keep("status", run("arc-status", "--upload=" + line))
    refusals["already_on_arc"] = run("arc-upload", "--session=" + LINE, "--transport=globus", "--demo")
    refusals["not_retryable"] = run("arc-retry", "--upload=" + line)
    keep("check", run("arc-check", "--upload=" + line))
    wait_until(home, line, lambda record: record.get("checks"))
    pairs["checked"] = listed(line)

    # Fix plan I3: a program checks the upload on ARC (a slow demo sign-in keeps it at work); Stop is
    # offered, and it ends the check only.
    os.environ[PACE] = "0.2"
    try:
        keep("check", run("arc-check", "--upload=" + line))
        worker_started(home, line)
    finally:
        os.environ[PACE] = "0"
    pairs["check_running"] = listed(line)
    keep("stop_check", run("arc-stop", "--upload=" + line))
    wait_until(home, line, lambda record: True)
    pairs["check_stopped"] = listed(line)
    # Review REG-01: a check that could not look (the copy's folder cannot be listed) leaves the upload on ARC,
    # and Upload to ARC still refuses with already_on_arc.
    before = len(arc_records.read_record(folder).get("checks") or [])
    placed = Path(pairs["verified_globus"]["destination"]["local_final_path"])
    mode = placed.stat().st_mode & 0o7777
    os.chmod(placed, 0)
    try:
        keep("check", run("arc-check", "--upload=" + line))
        wait_until(home, line, lambda record: len(record.get("checks") or []) > before)
    finally:
        os.chmod(placed, mode)
    pairs["check_not_looked"] = listed(line)
    refusals["already_on_arc_after_a_note"] = run("arc-upload", "--session=" + LINE, "--transport=globus", "--demo")
    # Fix plan I1 and I4: a check that finds the copy changed shows check_failed; then the session goes again,
    # under its own name: no already_on_arc, new_copy false.
    before = len(arc_records.read_record(folder).get("checks") or [])
    arc_support.make_writable(placed)
    (placed / "point_0003.json").unlink()
    keep("check", run("arc-check", "--upload=" + line))
    wait_until(home, line, lambda record: len(record.get("checks") or []) > before)
    pairs["check_failed"] = listed(line)
    keep("dry_run_after_failed_check",
         run("arc-upload", "--session=" + LINE, "--transport=globus", "--demo", "--dry-run"))

    single = started(keep("upload", run("arc-upload", "--session=" + SINGLE, "--transport=globus", "--demo",
                                        "--demo-fault=fail_task_once")))
    wait_until(home, single, lambda record: record["state"] in arc_support.RESTING_STATES)
    pairs["failed"] = listed(single)
    keep("status", run("arc-status", "--upload=" + single))
    refusals["earlier_upload"] = run("arc-upload", "--session=" + SINGLE, "--transport=globus", "--demo")
    keep("retry", run("arc-retry", "--upload=" + single))
    wait_until(home, single, lambda record: record["state"] in arc_support.RESTING_STATES)

    yline = started(keep("upload", run("arc-upload", "--session=" + YLINE, "--transport=ssh", "--demo")))
    wait_until(home, yline, lambda record: record["state"] in arc_support.RESTING_STATES)
    pairs["verified_ssh"] = listed(yline)
    keep("status", run("arc-status", "--upload=" + yline))

    # A folder of these names is on "ARC" already: Globus finds the name taken while checking.
    for name in (TAKEN, TAKEN_TOO):
        taken = arc_support.demo_base(home) / name
        taken.mkdir(parents=True)
        (taken / "placed_by_someone_else.txt").write_text("x\n", encoding="utf-8")
    taken = started(keep("upload", run("arc-upload", "--session=" + TAKEN, "--transport=globus", "--demo")))
    wait_until(home, taken, lambda record: record["state"] in arc_support.RESTING_STATES)
    pairs["conflict"] = listed(taken)
    keep("upload_existing", run("arc-upload", "--session=" + TAKEN, "--transport=globus", "--demo"))
    keep("dry_run", run("arc-upload", "--session=" + TAKEN, "--transport=globus", "--demo", "--dry-run"))
    keep("keep_copy", run("arc-keep-copy", "--upload=" + taken))
    wait_until(home, taken, lambda record: record["state"] in arc_support.RESTING_STATES)
    pairs["kept_copy"] = listed(taken)
    taken_too = started(keep("upload", run("arc-upload", "--session=" + TAKEN_TOO, "--transport=globus", "--demo")))
    wait_until(home, taken_too, lambda record: record["state"] in arc_support.RESTING_STATES)
    keep("stop", run("arc-stop", "--upload=" + taken_too))
    pairs["stopped_now"] = listed(taken_too)

    # A slow upload, stopped while its program works on it.
    os.environ[PACE] = "0.2"
    try:
        slow = started(keep("upload", run("arc-upload", "--session=" + SLOW, "--transport=globus", "--demo")))
        worker_started(home, slow)
    finally:
        os.environ[PACE] = "0"
    pairs["alive"] = listed(slow)
    refusals["busy"] = run("arc-retry", "--upload=" + slow)
    keep("stop", run("arc-stop", "--upload=" + slow))
    wait_until(home, slow, lambda record: record["state"] in arc_support.RESTING_STATES)
    pairs["stopped_while_working"] = listed(slow)

    keep("list", run("arc-list", "--limit=12"))
    keep("core_status", run("status"))
    refusals["unknown_upload"] = run("arc-status", "--upload=u000000000000")
    refusals["unknown_session"] = run("arc-upload", "--session=no_such_session", "--demo")
    refusals["settling"] = run("arc-upload", "--session=" + YOUNG, "--demo")
    refusals["no_session_json"] = run("arc-upload", "--session=" + OLD, "--demo")
    keep("doctor", run("arc-doctor"))
    keep("config", run("config"))
    refusals["config"] = run("config", "--set=arc_upload.folder=/projects")
    keep("config_set", run("config", *LIVE_SETTINGS))
    keep("config", run("config"))
    for transport in ("ssh", "globus"):
        answer = run("arc-upload", "--session=" + LINE, "--transport=" + transport, "--live", "--dry-run")
        # Only where this computer has what a live upload needs (macOS, ssh, Terminal, the Globus SDK).
        if answer.get("ok") or answer.get("kind") != "not_installed":
            keep("dry_run", answer)
    with mock.patch.object(cli, "FEATURE_MODULES", []):
        refusals["absent"] = run("arc-list", "--limit=12")
    return replies, pairs, refusals


def build_fake(fake):
    """The same scenarios with the stand-in: (replies by kind, items by scenario, refusals by scenario)."""
    replies, pairs, refusals = {}, {}, {}

    def keep(kind, answer):
        replies.setdefault(kind, []).append(answer)
        return answer

    def started(answer):
        assert answer.get("ok"), answer
        return answer["upload"]["upload_id"]

    def listed(upload_id, times=1):
        return item_of(keep("list", fake.listed(times)), upload_id)

    fake.set()
    keep("sessions", fake("arc-sessions", "--limit=30"))
    keep("dry_run", fake("arc-upload", "--session=" + LINE, "--transport=globus", "--demo", "--dry-run"))
    line = started(keep("upload", fake("arc-upload", "--session=" + LINE, "--transport=globus", "--demo")))
    pairs["verified_globus"] = listed(line, 9)
    keep("status", fake("arc-status", "--upload=" + line))
    refusals["already_on_arc"] = fake("arc-upload", "--session=" + LINE, "--transport=globus", "--demo")
    refusals["not_retryable"] = fake("arc-retry", "--upload=" + line)
    keep("check", fake("arc-check", "--upload=" + line))
    pairs["checked"] = listed(line)

    fake.set(check={"lists": 2})
    keep("check", fake("arc-check", "--upload=" + line))
    pairs["check_running"] = listed(line)
    keep("stop_check", fake("arc-stop", "--upload=" + line))
    pairs["check_stopped"] = listed(line)
    fake.set(check={"ok": False, "looked": False})
    keep("check", fake("arc-check", "--upload=" + line))
    pairs["check_not_looked"] = listed(line)
    refusals["already_on_arc_after_a_note"] = fake("arc-upload", "--session=" + LINE, "--transport=globus", "--demo")
    fake.set(check={"ok": False})
    keep("check", fake("arc-check", "--upload=" + line))
    pairs["check_failed"] = listed(line)
    keep("dry_run_after_failed_check",
         fake("arc-upload", "--session=" + LINE, "--transport=globus", "--demo", "--dry-run"))
    fake.set(check=None)

    fake.set(script=["queued", "preparing", "signing_in", "checking", "submitting", "transferring:9/18",
                     "failed:task_failed"])
    single = started(keep("upload", fake("arc-upload", "--session=" + SINGLE, "--transport=globus", "--demo")))
    pairs["failed"] = listed(single, 7)
    keep("status", fake("arc-status", "--upload=" + single))
    refusals["earlier_upload"] = fake("arc-upload", "--session=" + SINGLE, "--transport=globus", "--demo")
    keep("retry", fake("arc-retry", "--upload=" + single))
    fake.listed(10)

    fake.set(script=None)
    yline = started(keep("upload", fake("arc-upload", "--session=" + YLINE, "--transport=ssh", "--demo")))
    pairs["verified_ssh"] = listed(yline, 10)
    keep("status", fake("arc-status", "--upload=" + yline))

    fake.set(script=["queued", "preparing", "signing_in", "conflict:checking"])
    taken = started(keep("upload", fake("arc-upload", "--session=" + TAKEN, "--transport=globus", "--demo")))
    pairs["conflict"] = listed(taken, 4)
    keep("upload_existing", fake("arc-upload", "--session=" + TAKEN, "--transport=globus", "--demo"))
    keep("dry_run", fake("arc-upload", "--session=" + TAKEN, "--transport=globus", "--demo", "--dry-run"))
    keep("keep_copy", fake("arc-keep-copy", "--upload=" + taken))
    pairs["kept_copy"] = listed(taken, 3)
    taken_too = started(keep("upload", fake("arc-upload", "--session=" + TAKEN_TOO, "--transport=globus", "--demo")))
    fake.listed(4)
    keep("stop", fake("arc-stop", "--upload=" + taken_too))
    pairs["stopped_now"] = listed(taken_too)

    fake.set(script=None)
    slow = started(keep("upload", fake("arc-upload", "--session=" + SLOW, "--transport=globus", "--demo")))
    pairs["alive"] = listed(slow, 2)
    refusals["busy"] = fake("arc-retry", "--upload=" + slow)
    keep("stop", fake("arc-stop", "--upload=" + slow))
    pairs["stopped_while_working"] = listed(slow)

    keep("list", fake("arc-list", "--limit=12"))
    keep("core_status", fake("status"))
    refusals["unknown_upload"] = fake("arc-status", "--upload=u000000000000")
    refusals["unknown_session"] = fake("arc-upload", "--session=no_such_session", "--demo")
    refusals["settling"] = fake("arc-upload", "--session=" + YOUNG, "--demo")
    refusals["no_session_json"] = fake("arc-upload", "--session=" + OLD, "--demo")
    keep("doctor", fake("arc-doctor"))
    keep("config", fake("config"))
    fake.set(config_set="error")
    refusals["config"] = fake("config", "--set=arc_upload.folder=/projects")
    fake.set(config_set=None)
    keep("config_set", fake("config", *LIVE_SETTINGS))
    keep("config", fake("config"))
    for transport in ("ssh", "globus"):
        keep("dry_run", fake("arc-upload", "--session=" + LINE, "--transport=" + transport, "--live", "--dry-run"))
    fake.set(absent=True)
    refusals["absent"] = fake("arc-list", "--limit=12")
    return replies, pairs, refusals


# ---------------------------------------------------------------------- the window's stand-in


def protocol_examples():
    """The JSON examples of PROTOCOL.md 5.6 and 6, by what they show, and the Terminal script of 5.6."""
    text = (support.DATA_LAYER / "PROTOCOL.md").read_text(encoding="utf-8")
    found = {}
    for block in re.findall(r"```json\n(.*?)```", text, re.S):
        try:
            value = json.loads(block)
        except ValueError:
            continue    # an example with "..." in it: not one of these
        if not isinstance(value, dict):
            continue
        if value.get("schema") == "raman-arc-upload-manifest/1":
            found["manifest"] = value
        elif value.get("schema") == arc_records.SCHEMA:
            found["record"] = value
        elif value.get("DATA_TYPE") == "transfer":
            found["request"] = value
        elif "end_text" in value:
            found["session"] = value
        elif "state_label" in value and "upload_id" in value:
            found["item"] = value
    scripts = [block for block in re.findall(r"```bash\n(.*?)```", text, re.S) if "arc_worker" in block]
    found["terminal_script"] = scripts[0] if scripts else None
    return found


class StandInAgainstTheRealCommands(unittest.TestCase):
    """The answers of fake_data_layer.py, and the examples of PROTOCOL.md, have the shapes of the real
    commands' answers and of the files the upload program writes."""

    real = fake = None

    @classmethod
    def setUpClass(cls):
        environment = mock.patch.dict(os.environ, arc_support.GUARD_ENVIRONMENT)
        environment.start()
        cls.addClassCleanup(environment.stop)
        for name in ("RAMAN_ARC_TEST_HOOKS", "RAMAN_ARC_FAULT"):
            os.environ.pop(name, None)
        for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection")):
            patcher = mock.patch.object(target, name, arc_support._refuse_connection)
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        homes = [Path(tempfile.mkdtemp(prefix="raman_arc_shapes_")).resolve() for _ in range(2)]
        for home in homes:
            cls.addClassCleanup(shutil.rmtree, home, ignore_errors=True)
            cls.addClassCleanup(arc_support.make_writable, home)
        real_home, fake_home = homes
        cls.addClassCleanup(arc_support.wait_for_workers, real_home)
        arc_support.ready_session(real_home, folder=LINE, points=13)
        arc_support.ready_session(real_home, folder=SINGLE, points=1, scan_mode="single")
        arc_support.ready_session(real_home, folder=YLINE, points=13, scan_mode="1d-y")
        arc_support.ready_session(real_home, folder=TAKEN, points=4, scan_mode="2d")
        arc_support.ready_session(real_home, folder=TAKEN_TOO, points=4, scan_mode="2d")
        arc_support.ready_session(real_home, folder=SLOW, points=13)
        arc_support.ready_session(real_home, folder=YOUNG, points=2, age=1)
        old = support.make_session(real_home, folder=OLD, points=3, scan_mode="2d", with_session_json=False)
        arc_support.set_age(old["folder"], 600)
        # A copy of a session under a name with spaces: listed, but refused before it is counted.
        shutil.copytree(old["folder"], Path(old["folder"]).parent / ODD)
        arc_support.set_age(Path(old["folder"]).parent / ODD, 600)
        cls.real = build_real(real_home)
        cls.fake = build_fake(FakeDataLayer(fake_home))

    def compare(self, kind, real=None, fake=None):
        real = self.real[0][kind] if real is None else real
        fake = self.fake[0][kind] if fake is None else fake
        self.assertTrue(real and fake, kind)
        self.assertEqual(differences(merged_shape(real), merged_shape(fake)), [],
                         "%s: the stand-in's answers and the real ones have other keys" % kind)

    def test_each_kind_of_answer_has_the_keys_of_the_real_one(self):
        for kind in ("sessions", "dry_run", "upload", "upload_existing", "list", "status", "check", "retry",
                     "keep_copy", "stop", "doctor", "config", "config_set", "stop_check", "dry_run_after_failed_check"):
            with self.subTest(kind=kind):
                self.compare(kind)

    def test_status_and_the_list_count_alike(self):
        """Fix plan I2: status's arc_unfinished and arc_attention, and arc-list's unfinished and attention."""
        for side in (self.real, self.fake):
            self.assertIn("arc_unfinished", side[0]["core_status"][0])
            self.assertIn("arc_attention", side[0]["core_status"][0])
        real_list, fake_list = self.real[0]["list"][-1], self.fake[0]["list"][-1]
        real_status, fake_status = self.real[0]["core_status"][0], self.fake[0]["core_status"][0]
        # The scenarios end with one upload that needs a person: the check that found the copy changed.
        self.assertEqual((real_list["unfinished"], real_list.get("attention")), (0, 1))
        self.assertEqual((fake_list["unfinished"], fake_list.get("attention")),
                         (real_list["unfinished"], real_list.get("attention")))
        self.assertEqual((real_status["arc_unfinished"], real_status["arc_attention"]),
                         (real_list["unfinished"], real_list.get("attention")))
        self.assertEqual((fake_status["arc_unfinished"], fake_status["arc_attention"]),
                         (real_status["arc_unfinished"], real_status["arc_attention"]))

    def test_a_check_that_runs_and_one_that_failed(self):
        """Fix plan I1, I3 and I4: Stop while a check runs, check_failed afterwards, and an upload of the
        session again under its own name."""
        real, fake = self.real[1], self.fake[1]
        for side in (real, fake):
            self.assertEqual(side["check_running"]["worker"], {"alive": True, "action": "check", "terminal": False})
            self.assertIn("stop", side["check_running"]["actions"])
            self.assertEqual(side["check_stopped"]["last_check"], side["checked"]["last_check"],
                             "a stopped check adds no result")
            self.assertEqual((side["check_failed"]["display_state"], side["check_failed"]["last_check"]["ok"]),
                             ("check_failed", False))
            self.assertEqual(side["check_failed"]["message"], side["check_failed"]["last_check"]["message"])
        for side in (self.real[0], self.fake[0]):
            stop = side["stop_check"][0]
            self.assertIs(stop["ok"], True, stop)
            self.assertEqual((stop["stopped_now"], stop["upload"]["state"]), (False, "verified"))
            again = side["dry_run_after_failed_check"][0]
            self.assertIs(again["ok"], True, again)
            self.assertIs(again["plan"]["new_copy"], False)

    def test_a_check_that_could_not_look_leaves_the_upload_on_arc(self):
        """Review REG-01: a check that could not look is a note with its fix. The upload stays "On ARC, checked",
        and Upload to ARC still answers already_on_arc, on both sides."""
        for side in (self.real, self.fake):
            item = side[1]["check_not_looked"]
            note = item["last_check"]
            self.assertEqual((item["display_state"], item["display_label"], note["ok"], note["looked"]),
                             ("verified", "On ARC, checked", False, False))
            self.assertTrue(note["message"].endswith(arc_records.CHECK_NOT_LOOKED), note["message"])
            self.assertIn("Check status", note["fix"])
            self.assertEqual(side[2]["already_on_arc_after_a_note"]["reason"], "already_on_arc")
            self.assertEqual(sorted(note), ["fix", "looked", "message", "ok", "utc"])

    def test_folders_the_data_layer_does_not_record(self):
        """A session folder without session.json is counted but has no session ID, sample or dark; a folder
        whose name the data layer does not take is not counted (0 spectra, 2 files, 0 bytes); a session that
        may not go live has no path on ARC. The stand-in says the same as the real command (UI-07, UI-10)."""
        def session_of(side, folder):
            return next(item for item in side[0]["sessions"][0]["items"] if item["folder"] == folder)

        fields = ("points", "points_planned", "end", "end_text", "session_id", "sample_id", "started_utc", "kind",
                  "darks", "ready")
        for folder, reason, more in ((OLD, "no_session_json", ()), (ODD, "odd_name", ("files", "bytes"))):
            real, fake = session_of(self.real, folder), session_of(self.fake, folder)
            with self.subTest(folder=folder):
                self.assertEqual({key: fake[key] for key in fields + more}, {key: real[key] for key in fields + more})
                self.assertEqual((fake["why_not"]["reason"], real["why_not"]["reason"]), (reason, reason))
        self.assertEqual((session_of(self.real, OLD)["points"], session_of(self.real, ODD)["points"]), (3, 0))
        for folder in (OLD, SINGLE):
            real, fake = session_of(self.real, folder), session_of(self.fake, folder)
            with self.subTest(folder=folder):
                self.assertEqual((fake["live"]["cluster_path"], fake["live"]["globus_path"]), (None, None))
                self.assertEqual((real["live"]["cluster_path"], real["live"]["globus_path"]), (None, None))
                self.assertEqual(fake["demo"]["demo_path"] is None, real["demo"]["demo_path"] is None)

    def test_the_top_level_keys_are_the_same_answer_by_answer(self):
        for kind in ("upload", "upload_existing", "check", "retry", "keep_copy", "doctor", "config_set"):
            with self.subTest(kind=kind):
                real = {tuple(sorted(answer)) for answer in self.real[0][kind]}
                fake = {tuple(sorted(answer)) for answer in self.fake[0][kind]}
                self.assertEqual(fake, real)
        # A dry run: with an upload under way, its plan and that upload.
        for answers in (self.real[0]["dry_run"], self.fake[0]["dry_run"]):
            self.assertTrue(all(sorted(answer) == ["dry_run", "existing", "ok", "plan"] for answer in answers), answers)
            self.assertTrue(all(isinstance(answer["plan"], dict) for answer in answers))
        stops = [(sorted(answer), answer["stopped_now"], answer["worker"]) for answer in self.real[0]["stop"]]
        self.assertEqual([(sorted(answer), answer["stopped_now"], answer["worker"]) for answer in self.fake[0]["stop"]],
                         stops)

    def test_every_item_has_the_keys_of_an_arc_list_item(self):
        for side, (replies, pairs, refusals) in (("real", self.real), ("stand-in", self.fake)):
            found = [item for answers in replies.values() for answer in answers for item in items_in(answer)]
            found += list(pairs.values()) + [item for answer in refusals.values() for item in items_in(answer)]
            self.assertGreater(len(found), 20, side)
            for item in found:
                extra = STATUS_EXTRA if "events" in item else ()
                self.assertEqual(sorted(set(item) - set(extra)), ITEM_KEYS, "%s: %s" % (side, item.get("upload_id")))
            self.assertEqual(differences(merged_shape(found), merged_shape(
                [item for answers in self.real[0].values() for answer in answers for item in items_in(answer)]
                + list(self.real[1].values()))), [], side)

    def test_states_labels_and_display_states_are_the_real_ones(self):
        known = set(arc_records.STATES) | set(arc_records.DISPLAY_LABELS)
        for side, (replies, pairs, refusals) in (("real", self.real), ("stand-in", self.fake)):
            found = [item for answers in replies.values() for answer in answers for item in items_in(answer)]
            found += list(pairs.values())
            for item in found:
                with self.subTest(side=side, upload=item["upload_id"], state=item["state"]):
                    self.assertIn(item["state"], arc_records.STATES)
                    self.assertIn(item["display_state"], known)
                    self.assertEqual(item["state_label"], label_of(item["state"], item["transport"]))
                    expected = (label_of(item["state"], item["transport"]) if item["display_state"] == item["state"]
                                else DESIGN_DISPLAY_LABELS[item["display_state"]])
                    self.assertEqual(item["display_label"], expected)
                    self.assertTrue(set(item["actions"]) <= set(ACTIONS), item["actions"])
                    self.assertEqual(item["final"], item["state"] in arc_records.FINAL_STATES)
                    if item["error"] is not None:
                        self.assertEqual(sorted(item["error"]), ["fix", "kind", "message", "reason", "retryable"])
                        self.assertIn(item["error"]["reason"], arc_records.WORKER_RESULTS)
                        self.assertEqual(item["error"]["kind"],
                                         "internal" if item["error"]["reason"] == "internal" else "upload")
                        self.assertEqual((item["error"]["fix"], item["error"]["retryable"]),
                                         result_fix(item["error"]["reason"], item["demo"]))

    def test_the_same_situation_gives_the_same_state_actions_and_display(self):
        # may_run too (review R2-04): the window's Stop question of a running check depends on it.
        fields = ("state", "state_label", "display_state", "display_label", "final", "actions", "needs", "transport",
                  "demo", "area", "kind", "may_run")
        real, fake = self.real[1], self.fake[1]
        self.assertEqual(sorted(real), sorted(fake))
        for name in sorted(real):
            with self.subTest(situation=name):
                if name == "alive":
                    # The real program may be preparing or signing in; what matters is that it works on it.
                    picked = ("display_state", "actions", "worker", "final", "needs")
                    self.assertEqual({key: fake[name][key] for key in picked if key != "display_state"},
                                     {key: real[name][key] for key in picked if key != "display_state"})
                    self.assertIn(real[name]["state"], arc_records.WORKING_STATES)
                    self.assertIn(fake[name]["state"], arc_records.WORKING_STATES)
                    continue
                self.assertEqual({key: fake[name][key] for key in fields}, {key: real[name][key] for key in fields})
                for key in ("error", "verification", "last_check"):
                    self.assertEqual(fake[name][key] is None, real[name][key] is None, key)
                if real[name]["error"]:
                    picked = ("kind", "reason", "retryable", "fix")
                    self.assertEqual({key: fake[name]["error"][key] for key in picked},
                                     {key: real[name]["error"][key] for key in picked})
                if real[name]["verification"]:
                    self.assertEqual((fake[name]["verification"]["method"], fake[name]["verification"]["algorithm"]),
                                     (real[name]["verification"]["method"], real[name]["verification"]["algorithm"]))
                self.assertEqual(fake[name]["destination"]["placed_as"], real[name]["destination"]["placed_as"])
                self.assertEqual(fake[name]["leftover_on_arc"] is None, real[name]["leftover_on_arc"] is None)

    def test_refusals_have_the_kind_reason_and_keys_of_the_real_ones(self):
        real, fake = self.real[2], self.fake[2]
        self.assertEqual(sorted(real), sorted(fake))
        for name in sorted(real):
            with self.subTest(refusal=name):
                self.assertIs(real[name]["ok"], False, real[name])
                self.assertIs(fake[name]["ok"], False, fake[name])
                self.assertEqual((fake[name]["kind"], fake[name].get("reason")),
                                 (real[name]["kind"], real[name].get("reason")))
                self.assertEqual(sorted(fake[name]), sorted(real[name]))
                if "upload" in real[name]:
                    self.assertEqual(sorted(fake[name]["upload"]), sorted(real[name]["upload"]))
                if real[name].get("reason") in arc_records.REFUSALS:
                    # The same words; a number of seconds may differ.
                    self.assertEqual(re.sub(r"\d+", "N", fake[name]["fix"]), re.sub(r"\d+", "N", real[name]["fix"]))

    def test_the_settings_and_the_checks_are_the_real_ones(self):
        real_list, fake_list = self.real[0]["list"][-1], self.fake[0]["list"][-1]
        self.assertEqual(sorted(fake_list["settings"]), sorted(config.ARC_UPLOAD_KEYS))
        self.assertEqual(sorted(real_list["settings"]), sorted(config.ARC_UPLOAD_KEYS))
        for transport in ("globus", "ssh", "demo"):
            self.assertEqual(sorted(fake_list["ready"][transport]), ["ok", "problems"])
        real_checks = [check["name"] for check in self.real[0]["doctor"][0]["checks"]]
        fake_checks = [check["name"] for check in self.fake[0]["doctor"][0]["checks"]]
        if sys.platform == "darwin":
            self.assertEqual(fake_checks, real_checks)
        else:
            self.assertEqual([name for name in fake_checks if name in real_checks], real_checks)
        self.assertEqual(sorted(self.fake[0]["doctor"][0]["settings"]), sorted(config.ARC_UPLOAD_KEYS))
        # config prints the settings file: arc_upload only once something was set, nested as the file holds it.
        self.assertNotIn("arc_upload", self.real[0]["config"][0]["config"])
        self.assertNotIn("arc_upload", self.fake[0]["config"][0]["config"])
        for side in (self.real, self.fake):
            group = side[0]["config"][-1]["config"]["arc_upload"]
            self.assertEqual((group["include_simulated"], group["ssh"]["username"], group["globus"]["mac_collection"]),
                             (True, arc_support.TEST_USERNAME, MAC_COLLECTION))

    # Keys the record has only once a step needed them (PROTOCOL.md 5.6 names them).
    LATER_GLOBUS_KEYS = ("rename", "verification", "demo_fault_used")

    def compare_example(self, name, real, optional=()):
        example = protocol_examples()[name]
        found = differences(shape(real), shape(example))
        found = [line for line in found
                 if not any(line.endswith("%s: only in the real answer" % key) for key in optional)]
        self.assertEqual(found, [], "PROTOCOL.md's %s example and the real one have other keys" % name)

    def test_the_examples_have_the_keys_of_the_real_answers(self):
        replies, pairs = self.real[0], self.real[1]
        self.compare_example("session", replies["sessions"][0]["items"][0])
        self.compare_example("item", pairs["verified_globus"])
        self.compare_example("manifest", replies["file:" + arc_records.MANIFEST_NAME][0])
        self.compare_example("request", replies["file:globus_request.json"][0])
        record = replies["file:" + arc_records.RECORD_FILE][0]
        self.compare_example("record", record, optional=[".globus." + key for key in self.LATER_GLOBUS_KEYS])

    def test_the_fixed_values_of_the_globus_request(self):
        request = self.real[0]["file:globus_request.json"][0]
        example = protocol_examples()["request"]
        fixed = ("DATA_TYPE", "verify_checksum", "encrypt_data", "preserve_timestamp", "delete_destination_extra",
                 "fail_on_quota_errors", "skip_source_errors", "notify_on_succeeded", "notify_on_failed")
        self.assertEqual({key: request[key] for key in fixed}, {key: example[key] for key in fixed})
        self.assertEqual([sorted(item) for item in request["DATA"]], [sorted(item) for item in example["DATA"]])
        self.assertEqual((request["DATA"][0]["DATA_TYPE"], request["DATA"][0]["recursive"]), ("transfer_item", True))

    def test_the_terminal_script_is_the_one_the_data_layer_writes(self):
        written = arc_worker.terminal_script_text("<HOME>", "u3f9a0c1d2e4b", 2, "send", LINE,
                                                  interpreter="<interpreter>", data_layer="<HOME>/data_layer")
        self.assertEqual(protocol_examples()["terminal_script"], written)


class MayRunAgainstTheRealCommands(unittest.TestCase):
    """Review R2-04: the stand-in's may_run is the data layer's (arc_records.globus_may_run) in the same situations.
    True while Globus may still run the upload's transfer: its task runs, also behind a failed upload; false once the
    task has ended (an upload program that ended while it checked or placed the copy). The window's Stop question of
    a running check follows it: may_run true cancels the transfer, else the upload stays as it is."""

    # situation: (session, the step at which the upload program ends, its seconds per file, the state it leaves)
    REAL = {"transferring": (LINE, "poll_mid_transfer", "0.2", "transferring"),
            "verifying": (SINGLE, "verify_mid", "0", "verifying"),
            "placing": (YLINE, "place_after_rename", "0", "placing"),
            "failed_while_copying": (SLOW, "poll_mid_transfer", "0.2", "transferring")}
    # The stand-in's uploads of the same situations ("task_status": what the record keeps of the Globus task).
    FAKE = {"transferring": {"state": "not_watched"}, "verifying": {"state": "not_watched:verifying"},
            "placing": {"state": "not_watched:placing"},
            "failed_while_copying": {"state": "failed:globus_error", "task_status": "ACTIVE"}}
    EXPECTED = {"transferring": ("transferring", "not_watched", ["check", "stop"], True),
                "verifying": ("verifying", "not_watched", ["check", "stop"], False),
                "placing": ("placing", "not_watched", ["check", "stop"], False),
                "failed_while_copying": ("failed", "failed", ["retry", "check"], True)}

    @classmethod
    def setUpClass(cls):
        environment = mock.patch.dict(os.environ, arc_support.GUARD_ENVIRONMENT)
        environment.start()
        cls.addClassCleanup(environment.stop)
        for name in ("RAMAN_ARC_TEST_HOOKS", "RAMAN_ARC_FAULT"):
            os.environ.pop(name, None)
        for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection")):
            patcher = mock.patch.object(target, name, arc_support._refuse_connection)
            patcher.start()
            cls.addClassCleanup(patcher.stop)
        homes = [Path(tempfile.mkdtemp(prefix="raman_arc_may_run_")).resolve() for _ in range(2)]
        for home in homes:
            cls.addClassCleanup(shutil.rmtree, home, ignore_errors=True)
            cls.addClassCleanup(arc_support.make_writable, home)
        real_home, fake_home = homes
        cls.addClassCleanup(arc_support.wait_for_workers, real_home)
        cls.real = {}
        for name, (session, fault, pace, left) in cls.REAL.items():
            arc_support.ready_session(real_home, folder=session, points=13)
            # The upload program ends at that step (os._exit 97, design 13), as at a restart: nobody watches.
            with mock.patch.dict(os.environ, dict(arc_support.hooks(fault), **{PACE: pace})):
                answer = real_command(real_home, "arc-upload", "--session=" + session, "--transport=globus", "--demo")
                upload_id = answer["upload"]["upload_id"]
                wait_until(real_home, upload_id, lambda record, left=left: record["state"] == left)
            cls.real[name] = upload_id
        # An odd answer from Globus while the upload program watched the transfer: failed, while the task still runs.
        folder = arc_records.record_folder(config.Paths(real_home), cls.real["failed_while_copying"])

        def failed(record):
            error = arc_records.worker_error("globus_error", demo=True, what="no connection")
            arc_records.set_state(record, "failed", arc_records.demo_text(error["message"]))
            record["error"] = error

        arc_records.update_record(folder, failed)
        cls.tasks = {name: arc_records.read_record(arc_records.record_folder(config.Paths(real_home), upload_id))[
            "globus"]["task"]["status"] for name, upload_id in cls.real.items()}
        listed = real_command(real_home, "arc-list", "--limit=12")
        cls.real = {name: item_of(listed, upload_id) for name, upload_id in cls.real.items()}
        fake = FakeDataLayer(fake_home)
        made = {name: "2026-11-15T10:0%d:00.000Z" % index for index, name in enumerate(cls.FAKE)}
        fake.set(uploads=[dict(spec, demo=True, transport="globus", created_utc=made[name])
                          for name, spec in cls.FAKE.items()])
        listed = fake("arc-list", "--limit=12")
        cls.fake = {name: next(item for item in listed["items"] if item["created_utc"] == made[name]) for name in made}

    def test_the_real_situations_are_the_ones_named(self):
        # The task Globus runs for it: still copying, or ended once the upload program checks or places the copy.
        self.assertEqual(self.tasks, {"transferring": "ACTIVE", "verifying": "SUCCEEDED", "placing": "SUCCEEDED",
                                      "failed_while_copying": "ACTIVE"})

    def test_may_run_and_what_the_listing_shows_are_the_real_ones(self):
        for name, expected in self.EXPECTED.items():
            with self.subTest(situation=name):
                for side, item in (("real", self.real[name]), ("stand-in", self.fake[name])):
                    self.assertEqual((item["state"], item["display_state"], item["actions"], item["may_run"]), expected,
                                     side)
                self.assertEqual(self.fake[name]["worker"]["alive"], self.real[name]["worker"]["alive"])


class StandInTexts(unittest.TestCase):
    """The stand-in's tables are the data layer's: the labels, the defaults, the refusals and the results."""

    @classmethod
    def setUpClass(cls):
        # Read as a module of its own, without a bytecode file beside it in laser_test/tests/.
        cls.module = types.ModuleType("fake_data_layer_for_arc_shapes")
        cls.module.__file__ = str(FAKE)
        exec(compile(FAKE.read_text(encoding="utf-8"), str(FAKE), "exec"), cls.module.__dict__)

    @staticmethod
    def pattern(text):
        """A text with its {placeholders} as wildcards."""
        parts = re.split(r"\{[^{}]*\}", text)
        return "^" + ".+".join(re.escape(part) for part in parts) + "$"

    def test_labels_states_and_defaults(self):
        fake = self.module
        labels = dict(arc_records.STATE_LABELS)
        labels.update({state: label for state, label in arc_records.DISPLAY_LABELS.items() if state != "unreadable"})
        self.assertEqual(fake.ARC_LABELS, labels)
        self.assertEqual(fake.ARC_LABELS["check_failed"], DESIGN_DISPLAY_LABELS["check_failed"])
        # Fix plan I2: what needs a person, and for how long.
        self.assertEqual((fake.ARC_ATTENTION, fake.ARC_ATTENTION_DAYS), (DESIGN_ATTENTION, DESIGN_ATTENTION_DAYS))
        self.assertEqual(fake.ARC_ATTENTION, getattr(arc_records, "ATTENTION_STATES", None))
        self.assertEqual(fake.ARC_ATTENTION_DAYS, getattr(arc_records, "ATTENTION_DAYS", None))
        self.assertEqual(fake.ARC_WORKING, arc_records.WORKING_STATES)
        self.assertEqual(fake.ARC_DEFAULTS, arc_settings.flatten(arc_settings.DEFAULTS))
        self.assertEqual(fake.ARC_TUTORIAL_CLIENT, arc_settings.TUTORIAL_CLIENT_ID)
        self.assertEqual(fake.PLANNED_POINTS, arc_session.PLANNED_POINTS)
        subparsers = argparse.ArgumentParser(prog="test").add_subparsers()
        arc_commands.register(subparsers)
        self.assertEqual(sorted(fake.ARC_COMMANDS), sorted(subparsers.choices))
        self.assertEqual(fake.ARC_CORE_SETTINGS, config.DEFAULTS)

    def test_refusals_are_the_real_texts(self):
        for reason, (kind, error, fix) in self.module.ARC_REFUSALS.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, arc_records.REFUSALS)
                real_kind, real_error, real_fix = arc_records.REFUSALS[reason]
                self.assertEqual(kind, real_kind)
                self.assertEqual(re.sub(r"\{[^{}]*\}", "{}", error), re.sub(r"\{[^{}]*\}", "{}", real_error))
                self.assertEqual(re.sub(r"\{[^{}]*\}", "{}", fix), re.sub(r"\{[^{}]*\}", "{}", real_fix))

    def test_refusal_variants_are_the_real_texts(self):
        variants = getattr(arc_records, "REFUSAL_VARIANTS", {})
        for (reason, variant), (error, fix) in self.module.ARC_REFUSAL_VARIANTS.items():
            with self.subTest(reason=reason, variant=variant):
                self.assertIn((reason, variant), variants)
                real_error, real_fix = variants[(reason, variant)]
                self.assertEqual(re.sub(r"\{[^{}]*\}", "{}", error), re.sub(r"\{[^{}]*\}", "{}", real_error))
                self.assertEqual(re.sub(r"\{[^{}]*\}", "{}", fix), re.sub(r"\{[^{}]*\}", "{}", real_fix))

    def test_the_demo_results_and_the_check_texts_are_the_real_ones(self):
        """Reviews COR-05 and REG-01: the demo's wording of the results, and a check that could not look."""
        for reason, (message, fix) in self.module.ARC_FAILURES_DEMO.items():
            with self.subTest(reason=reason):
                real_message, real_fix = arc_records.DEMO_RESULTS[reason]
                self.assertRegex(re.sub(r"\{[^{}]*\}", "X", message), self.pattern(real_message))
                self.assertEqual(fix, real_fix)
        texts = self.module.ARC_CHECK_TEXTS
        note = arc_records.check_note("You did not finish signing in within 10 minutes.", "signin_timeout")
        self.assertEqual((texts["not_looked"], texts["not_looked_fix"]), (note["message"], note["fix"]))
        self.assertEqual(self.module.ARC_CHECK_AGAIN_FIX, arc_records.CHECK_AGAIN_FIX)

    def test_results_are_the_real_texts(self):
        for reason, (message, fix, retryable) in self.module.ARC_FAILURES.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, arc_records.WORKER_RESULTS)
                real_message, real_fix, real_retryable = arc_records.WORKER_RESULTS[reason]
                filled = re.sub(r"\{[^{}]*\}", "X", message)
                self.assertRegex(filled, self.pattern(real_message))
                self.assertEqual((fix, retryable), (real_fix, real_retryable))


PANEL = EDITION / "laser_test" / "arc_panel.h"


def cpp_constant(text, name):
    """The value of `static inline const QString <name> = "..." "...";` in a C++ header: its string
    literals joined, with \\" and \\\\ read back."""
    found = re.search(r"\b%s\s*=\s*((?:\s*\"(?:[^\"\\]|\\.)*\")+)\s*;" % re.escape(name), text)
    if found is None:
        raise AssertionError("%s is not in %s" % (name, PANEL.name))
    parts = re.findall(r"\"((?:[^\"\\]|\\.)*)\"", found.group(1))
    return "".join(re.sub(r"\\(.)", r"\1", part) for part in parts)


class WindowTexts(unittest.TestCase):
    """The window says what the data layer says where they must agree: the sign-in steps of a live SSH
    upload are the Terminal window's own words (fix plan I9)."""

    def test_the_ssh_steps_of_the_confirmation_are_the_terminal_text(self):
        from raman_datafed import arc_ssh
        panel = PANEL.read_text(encoding="utf-8")
        for name, lines in (("kSshHostKey", arc_ssh.HOST_KEY_STEP), ("kSshPassword", arc_ssh.PASSWORD_STEP),
                            ("kSshDuo", arc_ssh.DUO_STEP), ("kSshWarning", arc_ssh.WARNING_TEXT),
                            ("kSshNetwork", (arc_ssh.NETWORK_TEXT,))):
            with self.subTest(text=name):
                self.assertEqual(cpp_constant(panel, name), " ".join(lines))


# ---------------------------------------------------------------------- the Studio


@unittest.skipUnless(importlib.util.find_spec("raman_datafed.arc_commands"), "the arc-* commands are not installed")
class StudioServerAgainstTheRealCommands(studio_tests.ArcServerCase):
    """The Studio server's ARC endpoints answer what the real commands answer (design 15.4), with real
    demo uploads in a HOME of their own."""

    feature_modules = ("raman_datafed.arc_commands",)

    def setUp(self):
        super().setUp()
        self.patch(mock.patch.dict(os.environ, {PACE: "0"}))
        self.addCleanup(arc_support.make_writable, self.home)
        self.addCleanup(arc_support.wait_for_workers, self.home)
        arc_support.ready_session(self.home, folder=LINE, points=13)
        arc_support.ready_session(self.home, folder=YOUNG, points=2, age=1)

    def run_here(self, *argv):
        return real_command(self.home, *argv)

    def test_the_endpoints_answer_as_the_commands(self):
        status, answer = self.get("/api/arc")
        self.assertEqual(status, 200, answer)
        # GET /api/arc answers from arc-list alone: its uploads and counts, and some of its settings.
        self.assertLessEqual({"active", "ok", "settings", "unfinished", "uploads"}, set(answer))
        listing = self.run_here("arc-list", "--limit=12")
        for key in set(answer) - {"ok", "uploads", "settings"}:
            self.assertEqual(answer[key], listing[key], key)
        for name, value in answer["settings"].items():
            self.assertEqual(value, listing["settings"]["arc_upload." + name], name)

        status, started = self.post("/api/arc/upload", {"session": LINE, "transport": "globus"})
        self.assertEqual(status, 200, started)
        self.assertEqual(sorted(started), ["existing", "message", "ok", "upload", "worker"])
        self.assertEqual((started["upload"]["demo"], started["worker"], started["message"]),
                         (True, "detached", arc_commands.UPLOAD_MESSAGES["demo"]))
        upload_id = started["upload"]["upload_id"]
        wait_until(self.home, upload_id, lambda record: record["state"] in arc_support.RESTING_STATES)
        status, answer = self.get("/api/arc")
        listing = self.run_here("arc-list", "--limit=12")
        self.assertEqual((status, answer["active"], answer["unfinished"]),
                         (200, listing["active"], listing["unfinished"]))
        self.assertEqual(sorted(answer["uploads"][0]), ITEM_KEYS)
        self.assertEqual(answer["uploads"], listing["items"])
        self.assertEqual((answer["uploads"][0]["state"], answer["uploads"][0]["display_label"]),
                         ("verified", "On ARC, checked"))

        status, checked = self.post("/api/arc/check", {"upload": upload_id})
        self.assertEqual(status, 200, checked)
        self.assertEqual(sorted(checked), ["message", "ok", "upload", "worker"])
        wait_until(self.home, upload_id, lambda record: record.get("checks"))

        # Refusals come back as the command's failure object, with the HTTP status of their kind.
        for path, fields, status_code, kind, reason in (
                ("/api/arc/retry", {"upload": upload_id}, 400, "command", "not_retryable"),
                ("/api/arc/keep-copy", {"upload": upload_id}, 400, "command", "no_conflict"),
                ("/api/arc/stop", {"upload": upload_id}, 400, "command", "nothing_to_stop"),
                ("/api/arc/upload", {"session": LINE, "transport": "globus"}, 409, "conflict", "already_on_arc"),
                ("/api/arc/upload", {"session": YOUNG, "transport": "ssh"}, 409, "not_ready", "settling"),
                ("/api/arc/check", {"upload": "u000000000000"}, 404, "not_found", "unknown_upload")):
            with self.subTest(path=path, reason=reason):
                status, refused = self.post(path, fields)
                command = path.rsplit("/", 1)[1]
                argv = ["arc-%s" % command, "--upload=" + fields["upload"], "--demo-only"] if "upload" in fields else \
                    ["arc-upload", "--session=" + fields["session"], "--transport=" + fields["transport"], "--demo"]
                direct = self.run_here(*argv)
                self.assertEqual((status, refused["kind"], refused["reason"]), (status_code, kind, reason), refused)
                self.assertEqual(sorted(refused), sorted(direct))
                self.assertEqual(refused["fix"], direct["fix"])

        # An upload made in the window (live) is shown but never acted on: 403 refused / not_demo.
        record = arc_records.read_record(arc_records.record_folder(config.Paths(self.home), upload_id))
        record.update(upload_id="u0000000000aa", demo=False)
        record["destination"]["key"] = "live:%s|simulated" % record["destination"]["arc_folder"]
        arc_records.create_record(config.Paths(self.home), record)
        status, refused = self.post("/api/arc/stop", {"upload": "u0000000000aa"})
        self.assertEqual((status, refused["kind"], refused["reason"]), (403, "refused", "not_demo"), refused)
        self.assertEqual(refused["fix"], arc_records.REFUSALS["not_demo"][2])


NODE = shutil.which("node") or ("/usr/local/bin/node" if os.access("/usr/local/bin/node", os.X_OK) else None)

# Runs the Studio's engine (the studio-data and studio-engine blocks, as test_engine.mjs does) through
# uploads in every state it shows, and prints its labels and the items it listed.
ENGINE_SCRIPT = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const page = fs.readFileSync(process.argv[1], 'utf8');
const block = (id) => page.match(new RegExp('<script id="' + id + '"[^>]*>([\\s\\S]*?)</script>'))[1];
const sandbox = vm.createContext({});
vm.runInContext(block('studio-data'), sandbox);
vm.runInContext(block('studio-engine'), sandbox);
const E = sandbox.StudioEngine;
const arc = E.createArcDemo({ folder: '/projects/pcl_photonics/raman_globus_upload', collectionRoot: '/projects' });
const items = [];
const take = () => arc.uploads().forEach((item) => items.push(JSON.parse(JSON.stringify(item))));
const run = (ms) => { for (let spent = 0; spent < ms; spent += 100) { arc.advance(100); take(); } };
const session = (folder, mode, points) => ({ folder: folder, session_id: 'id-' + folder, scan_mode: mode,
                                             points: points, sample_id: 'S-001', active: false });
const line = session('1d_x_scan_20261113_101500_123', '1d-x', 13);
const first = arc.upload(line, 'globus', { fault: null }); take(); run(8000);
arc.check(first.id); take();
const dropped = arc.upload(session('1d_y_scan_20261113_104500_789', '1d-y', 13), 'ssh', { fault: 'drop_connection' });
run(6000); arc.retry(dropped.id); take(); run(6000);
const again = arc.upload(line, 'globus', { fault: null }); run(4000); arc.keepCopy(again.id); take(); run(8000);
const third = arc.upload(line, 'globus', { fault: null }); run(4000); arc.stop(third.id); take();
const single = session('single_20261113_103000_456', 'single', 1);
const lost = arc.upload(single, 'globus', { fault: 'lose_submit_reply' }); run(8000);
const stopped = arc.upload(session('2d_scan_20261114_090000_001', '2d', 4), 'ssh', { fault: null }); run(2800);
arc.stop(stopped.id); take(); run(500);
console.log(JSON.stringify({ labels: E.ARC_STATE_LABELS, items: items }));
"""


@unittest.skipUnless(NODE and STUDIO.is_file(), "node or the Studio page is missing")
class StudioEngineAgainstTheRealCommands(unittest.TestCase):
    """The Studio's own engine shows uploads in the shape of arc-list items, with the data layer's states,
    labels and actions (design 15.1)."""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, "--input-type=module", "-e", ENGINE_SCRIPT, str(STUDIO)], capture_output=True,
                              text=True, timeout=120)
        if done.returncode != 0:
            raise AssertionError("the Studio engine did not run: %s" % done.stderr[-2000:])
        found = json.loads(done.stdout)
        cls.labels, cls.items = found["labels"], found["items"]

    def test_the_labels_are_those_of_the_design_and_the_data_layer(self):
        self.assertEqual(self.labels, DESIGN_LABELS)
        layer = {state: arc_records.STATE_LABELS[state] for state in arc_records.STATES}
        layer["transferring"] = dict(arc_records.TRANSFERRING_LABELS)
        self.assertEqual(self.labels, layer)

    def test_every_state_is_shown(self):
        seen = {item["state"] for item in self.items}
        self.assertEqual(seen, set(arc_records.STATES) - {"waiting_for_terminal", "uncertain"})

    def test_items_have_the_keys_and_the_rules_of_arc_list_items(self):
        real_item = arc_records.item({"schema": arc_records.SCHEMA, "state": "verified", "transport": "globus",
                                      "demo": True, "destination": {}, "progress": {}}, False)
        nested = {"destination": sorted(real_item["destination"]), "worker": sorted(real_item["worker"]),
                  "progress": ["bytes", "bytes_done", "files", "files_done"],
                  "error": ["fix", "kind", "message", "reason", "retryable"],
                  "verification": ["algorithm", "files", "method", "utc"],
                  "last_check": ["fix", "looked", "message", "ok", "utc"]}
        for item in self.items:
            with self.subTest(upload=item["upload_id"], state=item["state"]):
                self.assertLessEqual(set(ITEM_KEYS), set(item))
                for key, keys in nested.items():
                    if item[key] is not None:
                        self.assertEqual(sorted(item[key]), keys, key)
                self.assertIn(item["state"], arc_records.STATES)
                self.assertEqual(item["state_label"], label_of(item["state"], item["transport"]))
                self.assertEqual((item["display_state"], item["display_label"]), (item["state"], item["state_label"]))
                self.assertTrue(item["demo"])
                self.assertEqual(item["final"], item["state"] in arc_records.FINAL_STATES)
                self.assertEqual(item["needs"], "decision" if item["state"] in ("conflict", "uncertain") else None)
                working = item["state"] in arc_records.WORKING_STATES
                self.assertEqual(item["worker"],
                                 {"alive": working, "action": "send" if working else None, "terminal": False})
                expected = {"verified": ["check", "show_in_finder"], "conflict": ["keep_copy", "retry", "stop"],
                            "uncertain": ["retry", "check", "stop"]}
                if working:
                    self.assertEqual(item["actions"], ["stop"])
                elif item["state"] in expected:
                    self.assertEqual(item["actions"], expected[item["state"]])
                else:
                    retry = item["state"] == "stopped" or bool(item["error"] and item["error"]["retryable"])
                    self.assertEqual("retry" in item["actions"], retry)
                    self.assertLessEqual(set(item["actions"]), {"retry", "check"})
                if item["error"] is not None:
                    reason = item["error"]["reason"]
                    self.assertEqual((item["error"]["kind"], item["error"]["fix"], item["error"]["retryable"]),
                                     ("upload",) + result_fix(reason, True))
                if item["verification"] is not None:
                    self.assertIn((item["verification"]["method"], item["verification"]["algorithm"]),
                                  {("globus-checksum", "MD5"), ("sha256-on-arc", "SHA-256")})


if __name__ == "__main__":
    unittest.main()
