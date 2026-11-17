"""Tests for the Mac-native motor program (PORT_SPEC.md section 3).

Run:  /opt/anaconda3/bin/python3 -m unittest discover -s macos-native/motor/tests -v

Everything runs against the KDC101 simulator (RAMAN_MOTOR_SIMULATE=1) with a fake Qt
responder thread that answers every request.txt the way laser_test/tests/motor_path_test.cpp
does (`ACK <run> <id> OK <heartbeat>`), unless a test installs another reply policy.
"""

from __future__ import annotations

import contextlib
import gc
import io
import json
import math
import os
import re
import shutil
import struct
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
MOTOR_DIR = os.path.dirname(HERE)
PROJECT_ROOT = os.path.dirname(os.path.dirname(MOTOR_DIR))
# The Windows source the 2D sequence is checked against belongs to the original project. A copy of
# this folder that sits inside the original project (the catalog edition) finds it one level up;
# RAMAN_ORIGINAL_PROJECT names another place.
CPP_SOURCE = next((candidate for candidate in (
    os.path.join(root, "RamanAutofocus", "RamanAutofocus", "RamanAutofocusTest.cpp")
    for root in (os.environ.get("RAMAN_ORIGINAL_PROJECT") or PROJECT_ROOT, PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)))
    if os.path.isfile(candidate)), None)
sys.path.insert(0, MOTOR_DIR)

import apt_kdc101 as apt           # noqa: E402
import scan_handshake as hs        # noqa: E402
import raman_autofocus as ra       # noqa: E402

SERIAL_X, SERIAL_Y, SERIAL_Z = "27266528", "27266445", "27266446"
START_X, START_Y, START_Z = 69400, 72870, 131860     # motor_sdk_stub.h:12
ENV_KEYS = ("RAMAN_MOTOR_SIMULATE", "RAMAN_MOTOR_CONFIG", "RAMAN_MOTOR_ACK_TIMEOUT_MS",
            "RAMAN_MOTOR_SIM_START", "RAMAN_MOTOR_SIM_LOG", "RAMAN_MOTOR_DEBUG")

try:
    import pyftdi.serialext            # noqa: F401  (installed in /opt/anaconda3, PORT_SPEC.md)
    from pyftdi.ftdi import Ftdi
    from pyftdi.usbtools import UsbTools, UsbToolsError, UsbDeviceDescriptor
    HAVE_PYFTDI = True
except Exception:                      # pragma: no cover - the pyftdi test is skipped, never silently passed
    HAVE_PYFTDI = False


def heartbeat() -> str:
    return str(int(time.time() * 1000))


def ok_policy(_qt, _point):
    return "OK", heartbeat()


class FakeQt(threading.Thread):
    """Fake Qt controller: polls request.txt every 10 ms and replies according to `policy`
    (returns (state, heartbeat) or None for no reply). Records distinct requests."""

    def __init__(self, directory: str, run: str, policy=ok_policy):
        super().__init__(daemon=True)
        self.directory, self.run_id, self.policy = directory, run, policy
        self.requests = []          # (id, x, y, z) in order of first appearance
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                with open(os.path.join(self.directory, "request.txt"), "r") as handle:
                    tokens = handle.readline().split()
            except OSError:
                tokens = []
            if len(tokens) == 6 and tokens[0] == "STEP" and tokens[1] == self.run_id:
                point = int(tokens[2])
                if not self.requests or self.requests[-1][0] != point:
                    self.requests.append((point, int(tokens[3]), int(tokens[4]), int(tokens[5])))
                reply = self.policy(self, point)
                if reply is not None:
                    hs.publish(os.path.join(self.directory, "response.txt"),
                               "ACK %s %d %s %s\n" % (self.run_id, point, reply[0], reply[1]))
            time.sleep(0.010)

    def stop(self):
        self._stop.set()
        self.join(2.0)


def parse_cpp_sequence():
    """The 120 handwritten scanY/scanX calls between the Sequence begin/end markers of the
    C++ main, and the (axis, direction) list of the 0.1 mm relative steps they produce."""
    with open(CPP_SOURCE, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    begin = text.index("---- Sequence begin (X/Y) ----")
    end = text.index("---- Sequence end (X/Y) ----")
    calls = re.findall(r"scan([XY])\(serial[XY],\s*home[XY],\s*([0-9.]+),\s*([0-9.]+)\)", text[begin:end])
    parsed = [(axis, float(start), float(stop)) for axis, start, stop in calls]
    steps = []
    for axis, start, stop in parsed:
        count = int(math.floor(abs(stop - start) / 0.1 + 0.5))    # std::round(distance / STEP)
        direction = 1 if stop >= start else -1
        steps.extend([(axis, direction)] * count)
    return parsed, steps


def read_file(path: str) -> str:
    try:
        with open(path, "r") as handle:
            return handle.read()
    except OSError:
        return ""


class MotorTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["RAMAN_MOTOR_SIMULATE"] = "1"
        self.tmp = tempfile.mkdtemp(prefix="raman_motor_test_")
        apt.SIMULATED_DEVICES.clear()
        self._saved_parent = hs._ORIGINAL_PARENT      # safety stop state (scan_handshake.py)
        hs.reset_stop()

    def tearDown(self):
        hs._ORIGINAL_PARENT = self._saved_parent
        hs.reset_stop()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_program(self, mode, run="run1", policy=ok_policy, session_dir=None):
        """Run raman_autofocus.main in-process with a fake Qt responder; returns
        (exit code, stdout, stderr, responder, {serial: SimulatedTransport})."""
        session_dir = session_dir or self.tmp
        argv = ["RamanAutofocus", session_dir, run] + ([mode] if mode != "2d" else [])
        qt = FakeQt(session_dir, run, policy)
        qt.start()
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ra.main(argv)
        finally:
            qt.stop()
        return code, out.getvalue(), err.getvalue(), qt, dict(apt.SIMULATED_DEVICES)

    # ---- codec ---------------------------------------------------------------------
    def test_codec_round_trips_every_message(self):
        host_messages = {
            "no_flash": (apt.msg_no_flash_programming(), bytes.fromhex("18 00 00 00 50 01")),
            "enable": (apt.msg_set_chan_enable(True), bytes.fromhex("10 02 01 01 50 01")),
            "req_info": (apt.msg_req_info(), bytes.fromhex("05 00 00 00 50 01")),
            "req_pos": (apt.msg_req_poscounter(), bytes.fromhex("11 04 01 00 50 01")),
            "home": (apt.msg_move_home(), bytes.fromhex("43 04 01 00 50 01")),
            "move_abs": (apt.msg_move_absolute(69400), bytes.fromhex("53 04 06 00 d0 01 01 00 18 0f 01 00")),
            "move_rel": (apt.msg_move_relative(-3470), bytes.fromhex("48 04 06 00 d0 01 01 00 72 f2 ff ff")),
            "backlash": (apt.msg_set_genmoveparams(0), bytes.fromhex("3a 04 06 00 d0 01 01 00 00 00 00 00")),
            "stop": (apt.msg_move_stop(immediate=True), bytes.fromhex("65 04 01 01 50 01")),
            "resume_endofmove": (apt.msg_resume_endofmovemsgs(), bytes.fromhex("6c 04 01 00 50 01")),
        }
        for name, (message, expected) in host_messages.items():
            with self.subTest(message=name):
                encoded = apt.encode(message)
                self.assertEqual(encoded, expected)
                decoded, used = apt.decode(encoded + b"\x99\x99")
                self.assertEqual(used, len(expected))
                self.assertEqual(decoded, message)
        device_messages = {
            "get_info": apt.msg_get_info(27266528, "KDC101"),
            "get_pos": apt.msg_get_poscounter(-123456),
            "homed": apt.msg_move_homed(0),
            "completed": apt.msg_move_completed(832800),
            "stopped": apt.msg_move_stopped(42),
        }
        for name, message in device_messages.items():
            with self.subTest(message=name):
                encoded = apt.encode(message)
                self.assertEqual(encoded[4], 0x01 | 0x80)            # dest host, data flag
                self.assertEqual(encoded[5], 0x50)                   # source: the K-Cube
                self.assertEqual(struct.unpack_from("<H", encoded, 2)[0], len(message.data))
                decoded, used = apt.decode(encoded)
                self.assertEqual((decoded, used), (message, len(encoded)))
        info = apt.parse_hw_info(device_messages["get_info"].data)
        self.assertEqual((info.serial_number, info.model, info.channels), (27266528, "KDC101", 1))
        self.assertEqual(len(device_messages["get_info"].data), 84)
        self.assertEqual(apt.parse_poscounter(device_messages["get_pos"].data), (1, -123456))
        status = apt.parse_status(device_messages["completed"])
        self.assertEqual((status.channel, status.position), (1, 832800))
        self.assertEqual(len(device_messages["completed"].data), 14)
        self.assertEqual(apt.parse_status(apt.Message(apt.MGMSG_MOT_MOVE_HOMED, 1, 0, 0x01, 0x50)).channel, 1)
        self.assertIsNone(apt.decode(bytes.fromhex("53 04 06 00 d0 01 01 00")))   # incomplete
        self.assertIsNone(apt.decode(b"\x11\x04\x01"))

    def test_reader_tolerates_unsolicited_and_fragmented_input(self):
        class NoisyTransport(apt.SimulatedTransport):
            def read(self, n):                       # deliver at most 3 bytes per read
                return super().read(min(n, 3))

            def _handle(self, msg):
                if msg.msgid == apt.MGMSG_MOT_REQ_POSCOUNTER:
                    # an unsolicited status update with data, and an unknown header-only message
                    self._send(apt.Message(0x0491, dest=0x01, source=0x50, data=b"\x01\x00" + b"\xAA" * 12))
                    self._send(apt.Message(0x0002, 0x07, 0x00, 0x01, 0x50))
                super()._handle(msg)

        transport = NoisyTransport(SERIAL_X, initial_position=4242)
        dev = apt.KDC101(SERIAL_X, transport)
        self.assertEqual(dev.info.serial_number, 27266528)
        self.assertEqual(dev.get_position(), 4242)
        self.assertEqual(dev.message_queue_size(), 0)   # noise never becomes a motor event
        with self.assertRaises(apt.MotorError):          # wrong serial on the port is rejected
            apt.KDC101(SERIAL_Y, apt.SimulatedTransport(SERIAL_X))

    def test_simulated_device_home_move_stop(self):
        dev = apt.KDC101(SERIAL_Z, apt.SimulatedTransport(SERIAL_Z))
        self.assertEqual(dev.get_position(), START_Z)

        def wait_event(expected, timeout=2.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if dev.message_queue_size() > 0 and dev.get_next_message()[:2] == expected:
                    return True
                time.sleep(0.02)
            return False

        started = time.monotonic()
        self.assertEqual(dev.home(), 0)
        self.assertTrue(wait_event((2, 0)))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertEqual(dev.get_position(), 0)
        dev.move_to_position(69400)
        self.assertTrue(wait_event((2, 1)))
        self.assertEqual(dev.get_position(), 69400)
        dev.move_relative(-3470)
        self.assertTrue(wait_event((2, 1)))
        self.assertEqual(dev.get_position(), 65930)
        dev.move_to_position(600000)
        dev.stop_immediate()
        self.assertTrue(wait_event((2, 2)))
        self.assertLess(dev.get_position(), 600000)
        dev.set_backlash(0)
        dev.close()
        journal = apt.SIMULATED_DEVICES[SERIAL_Z].journal
        self.assertEqual([e for e in journal if e[0] in ("HOME", "MOVE_ABS", "MOVE_REL", "STOP", "BACKLASH")][:4],
                         [("HOME",), ("MOVE_ABS", 69400), ("MOVE_REL", -3470), ("MOVE_ABS", 600000)])
        # open sequence: no-flash, enable, resume end-of-move messages, then GET_INFO
        self.assertEqual([e[0] for e in journal[:5]], ["START", "NO_FLASH", "ENABLE", "RESUME_ENDOFMOVEMSGS", "REQ_INFO"])

    # ---- 1D ------------------------------------------------------------------------
    def check_line_scan(self, mode):
        selected = SERIAL_X if mode == "1d-x" else SERIAL_Y
        code, out, err, qt, devices = self.run_program(mode)
        self.assertEqual(code, 0, err)
        self.assertEqual(ra.pointId, 13)
        self.assertEqual(len(qt.requests), 13)
        self.assertEqual(read_file(os.path.join(self.tmp, "advanced.txt")), "13")
        self.assertIn("Scan complete.", out)
        self.assertIn("[1D] %s at 24 mm" % ("X" if mode == "1d-x" else "Y"), out)
        self.assertIn("[Point 13] saved, advance", out)
        self.assertEqual(out.count("saved, advance"), 13)
        expected = [i * 69400 for i in range(13)]
        self.assertEqual(devices[selected].counts("HOME"), 1)
        self.assertEqual([e[1] for e in devices[selected].events("MOVE_ABS")], expected)
        self.assertEqual(devices[selected].events("MOVE_REL"), [])
        for other, start in ((SERIAL_X, START_X), (SERIAL_Y, START_Y), (SERIAL_Z, START_Z)):
            if other == selected:
                continue
            self.assertEqual(devices[other].events("HOME", "MOVE_ABS", "MOVE_REL"), [], other)
            self.assertEqual(devices[other].position, start)
        xs = [r[1] for r in qt.requests]
        ys = [r[2] for r in qt.requests]
        zs = [r[3] for r in qt.requests]
        self.assertEqual([r[0] for r in qt.requests], list(range(1, 14)))
        self.assertEqual(xs, expected if mode == "1d-x" else [START_X] * 13)
        self.assertEqual(ys, expected if mode == "1d-y" else [START_Y] * 13)
        self.assertEqual(zs, [START_Z] * 13)

    def test_line_scan_x(self):
        self.check_line_scan("1d-x")

    def test_line_scan_y(self):
        self.check_line_scan("1d-y")

    def test_line_scan_rejected_by_20mm_limit(self):
        self.assertEqual(hs.line_scan_positions(), [i * 69400 for i in range(13)])
        self.assertTrue(hs.line_scan_within_limits(hs.line_scan_positions(), 0, 867500))
        self.assertFalse(hs.line_scan_within_limits(hs.line_scan_positions(), 0, 694000))
        self.assertFalse(hs.line_scan_within_limits([], 0, 1))
        self.assertFalse(hs.line_scan_within_limits([0], 5, 5))
        config = json.loads(json.dumps(ra.DEFAULT_CONFIG))
        config["travel_limits_steps"] = {"min": 0, "max": 694000}
        config_path = os.path.join(self.tmp, "motor_config.json")
        with open(config_path, "w") as handle:
            json.dump(config, handle)
        os.environ["RAMAN_MOTOR_CONFIG"] = config_path
        code, out, err, qt, devices = self.run_program("1d-y")
        self.assertNotEqual(code, 0)
        self.assertIn("[1D] 0..24 mm exceeds/has unavailable controller limits: 0..694000 steps. No movement.", err)
        self.assertIn("Scan aborted; no further point acquired.", out)
        self.assertEqual(qt.requests, [])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "request.txt")))
        for serial in (SERIAL_X, SERIAL_Y, SERIAL_Z):
            self.assertEqual(devices[serial].events("HOME", "MOVE_ABS", "MOVE_REL"), [], serial)

    # ---- 2D ------------------------------------------------------------------------
    def test_two_dimensional_scan_matches_cpp_sequence(self):
        if CPP_SOURCE is None:
            self.skipTest("RamanAutofocusTest.cpp of the original project was not found "
                          "(set RAMAN_ORIGINAL_PROJECT to the original project folder)")
        calls, expected_steps = parse_cpp_sequence()
        self.assertEqual(len(calls), 120)
        self.assertEqual(len(expected_steps), 120)
        self.assertEqual([(a, s, e) for a, s, e, _label in ra.XY_SEQUENCE], calls)

        code, out, err, qt, devices = self.run_program("2d")
        self.assertEqual(code, 0, err)
        self.assertEqual(ra.pointId, 124)
        self.assertEqual(len(qt.requests), 124)
        self.assertEqual(out.count("saved, advance"), 124)
        self.assertEqual(read_file(os.path.join(self.tmp, "advanced.txt")), "124")
        self.assertIn("---- Sequence end (X/Y) ----", out)
        self.assertIn("Scan complete.", out)

        for serial in (SERIAL_X, SERIAL_Y, SERIAL_Z):
            self.assertEqual(devices[serial].counts("HOME"), 1, serial)
            self.assertEqual(devices[serial].events("BACKLASH"), [("BACKLASH", 0)], serial)

        # Z: 20 mm, back to 3.8 mm, start 3.8 mm, 4 scan points, working height 4.0 mm; then constant.
        home_z = 0
        self.assertEqual([e[1] for e in devices[SERIAL_Z].events("MOVE_ABS")],
                         [home_z + 694000, 131860, 131860, 135330, 138800, 142270, 145740, 138800])
        self.assertEqual(devices[SERIAL_Z].events("MOVE_REL"), [])
        self.assertEqual([r[3] for r in qt.requests[:4]], [135330, 138800, 142270, 145740])
        self.assertEqual({r[3] for r in qt.requests[4:]}, {138800})
        self.assertEqual(devices[SERIAL_Z].position, 138800)

        # X/Y: one absolute move to 2.0 mm each, then only +/-3470 relative steps in the C++ order.
        self.assertEqual([e[1] for e in devices[SERIAL_X].events("MOVE_ABS")], [69400])
        self.assertEqual([e[1] for e in devices[SERIAL_Y].events("MOVE_ABS")], [69400])
        actual_steps = []
        for serial, axis in ((SERIAL_X, "X"), (SERIAL_Y, "Y")):
            for index, entry in enumerate(devices[serial].journal):
                if entry[0] == "MOVE_REL":
                    self.assertEqual(abs(entry[1]), 3470)
                    actual_steps.append((index, serial, axis, 1 if entry[1] > 0 else -1))
        # interleave the two journals by the order of the acknowledged points
        xy_requests = qt.requests[4:]
        self.assertEqual(len(xy_requests), 120)
        derived = []
        last_x, last_y = 69400, 69400
        for _id, x, y, _z in xy_requests:
            if x != last_x:
                derived.append(("X", 1 if x > last_x else -1))
            elif y != last_y:
                derived.append(("Y", 1 if y > last_y else -1))
            else:
                derived.append(("?", 0))
            self.assertIn(abs(x - last_x) + abs(y - last_y), (3470,))
            last_x, last_y = x, y
        self.assertEqual(derived, expected_steps)
        self.assertEqual(sorted((a, d) for _i, _s, a, d in actual_steps), sorted(expected_steps))
        self.assertEqual([(a, d) for _i, _s, a, d in actual_steps if a == "X"],
                         [s for s in expected_steps if s[0] == "X"])
        self.assertEqual([(a, d) for _i, _s, a, d in actual_steps if a == "Y"],
                         [s for s in expected_steps if s[0] == "Y"])
        self.assertEqual((devices[SERIAL_X].position, devices[SERIAL_Y].position), (104100, 104100))
        # sticky std::fixed formatting after the Z scan loop (RamanAutofocusTest.cpp:407 ff.)
        self.assertIn("Moving to Z_offset =  3.9000 mm (motor pos: 135330) ... OK", out)
        self.assertIn("Moving to Z = 4.0000 mm (motor pos: 138800)... OK", out)
        self.assertIn("Move X to start (+2.0000 mm) pos=69400 ... OK", out)
        self.assertIn("Move Z to 20 mm (motor pos: 694000)...", out)
        self.assertIn("Move Z back to 3.8 mm (motor pos: 131860)...", out)
        self.assertIn("Step size: 0.1 mm, total steps: 4", out)
        self.assertIn("Y -> 2.1000 mm (motor pos: 72870) ... OK", out)
        self.assertIn("X -> 3.0000 mm (motor pos: 104100) ... OK", out)

    # ---- cancel / heartbeat ----------------------------------------------------------
    def test_cancel_file_stops_without_advancing(self):
        def policy(qt, point):
            if point <= 3:
                return "OK", heartbeat()
            hs.publish(os.path.join(qt.directory, "cancel.txt"), "STOP\n")
            return None

        code, out, err, qt, devices = self.run_program("1d-x", policy=policy)
        self.assertNotEqual(code, 0)
        self.assertEqual(read_file(os.path.join(self.tmp, "advanced.txt")), "3")
        self.assertIn("[Point 4] FAILED/cancelled, do not advance", out)
        self.assertIn("Scan aborted; no further point acquired.", out)
        self.assertIn("All device connections closed.", out)
        self.assertEqual(out.count("saved, advance"), 3)
        self.assertEqual(len(devices[SERIAL_X].events("MOVE_ABS")), 4)
        self.assertEqual(ra.pointId, 4)

    # ---- safety stop (Mac-native addition, scan_handshake.py) --------------------------------
    def test_safety_stop_when_the_controller_is_gone(self):
        """A changed parent process id (the controller that started the program died) cancels the
        scan like cancel.txt does: no further point, controllers closed, non-zero exit."""
        def policy(qt, point):
            if point <= 3:
                return "OK", heartbeat()
            hs._ORIGINAL_PARENT = os.getppid() + 100000     # as if the process had been re-parented
            return None

        code, out, err, qt, devices = self.run_program("1d-x", policy=policy)
        self.assertNotEqual(code, 0)
        self.assertEqual(read_file(os.path.join(self.tmp, "advanced.txt")), "3")
        self.assertIn("[Point 4] FAILED/cancelled, do not advance", out)
        self.assertIn("Scan aborted; no further point acquired.", out)
        self.assertIn("All device connections closed.", out)
        self.assertIn("[Safety stop] the controller process", err)
        self.assertEqual(len(devices[SERIAL_X].events("MOVE_ABS")), 4)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "cancel.txt")))

    def test_safety_stop_during_homing_stops_the_axis(self):
        """A stop requested while an axis is moving: the move is stopped at once (STOP is sent),
        no further move is commanded and the other axes are never touched."""
        def stop_when_homing_starts():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                device = apt.SIMULATED_DEVICES.get(SERIAL_X)
                if device is not None and device.counts("HOME") >= 1:
                    hs.request_stop("test stop")
                    return
                time.sleep(0.005)

        trigger = threading.Thread(target=stop_when_homing_starts, daemon=True)
        trigger.start()
        code, out, err, qt, devices = self.run_program("1d-x")
        trigger.join(5)
        self.assertNotEqual(code, 0)
        self.assertIn("[Error] Movement cancelled or timed out; scan aborted.", err)
        self.assertIn("[Safety stop] test stop; scan aborted.", err)
        self.assertEqual(devices[SERIAL_X].counts("STOP"), 1)
        self.assertEqual(devices[SERIAL_X].events("MOVE_ABS"), [])
        self.assertEqual(len(qt.requests), 0)
        for other in (SERIAL_Y, SERIAL_Z):
            moved = devices[other].counts("HOME") + devices[other].counts("MOVE_ABS") + devices[other].counts("MOVE_REL")
            self.assertEqual(moved, 0, "axis %s must not be touched" % other)

    def test_without_a_stop_the_parent_check_changes_nothing(self):
        self.assertFalse(hs.controller_gone())
        self.assertFalse(hs.cancelled(self.tmp))
        self.assertEqual(hs.stop_reason(), "")
        hs.request_stop("first")
        hs.request_stop("second")
        self.assertTrue(hs.cancelled(self.tmp))
        self.assertEqual(hs.stop_reason(), "first")
        hs.reset_stop()
        self.assertFalse(hs.cancelled(self.tmp))

    def test_termination_signals_cancel_the_scan_and_close_the_controllers(self):
        """SIGTERM / SIGINT / SIGHUP / SIGQUIT to the real program, started through the launcher:
        the scan is cancelled through the normal path instead of the process ending with the
        controllers open."""
        launcher = os.path.join(MOTOR_DIR, "RamanAutofocus")
        env = dict(os.environ)
        env["RAMAN_MOTOR_SIMULATE"] = "1"
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        for name in ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT"):
            with self.subTest(signal=name):
                session = os.path.join(self.tmp, name)
                os.mkdir(session)
                sim_log = os.path.join(session, "sim.log")
                qt = FakeQt(session, "run_" + name)
                qt.start()
                proc = subprocess.Popen([launcher, session, "run_" + name, "1d-y"], cwd=MOTOR_DIR,
                                        env=dict(env, RAMAN_MOTOR_SIM_LOG=sim_log),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 30
                    while len(qt.requests) < 2 and time.monotonic() < deadline and proc.poll() is None:
                        time.sleep(0.01)
                    self.assertGreaterEqual(len(qt.requests), 2, "the scan did not reach its second point")
                    proc.send_signal(getattr(signal, name))
                    out, err = proc.communicate(timeout=20)
                finally:
                    qt.stop()
                    if proc.poll() is None:
                        proc.kill()
                self.assertEqual(proc.returncode, 1, err)
                self.assertIn("[Safety stop] received %s; scan aborted." % name, err)
                self.assertIn("All device connections closed.", out)
                self.assertIn("Scan aborted; no further point acquired.", out)
                self.assertLess(len(qt.requests), 13)
                journal = read_file(sim_log)
                for serial in (SERIAL_X, SERIAL_Y, SERIAL_Z):
                    self.assertIn("%s CLOSE" % serial, journal)

    def test_controller_death_stops_the_program_and_closes_the_controllers(self):
        """The process that started the program through the launcher is killed outright. The program
        must notice, command nothing further, close the three controllers and end, although its
        stdout and stderr pipes are broken from that moment on."""
        stand_in = r"""
import os, subprocess, sys
launcher, session, run, pidfile = sys.argv[1:5]
child = subprocess.Popen([launcher, session, run, "1d-y"], cwd=os.path.dirname(launcher),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
with open(pidfile, "w") as handle:
    handle.write(str(child.pid))
for _line in child.stdout:
    pass
"""
        launcher = os.path.join(MOTOR_DIR, "RamanAutofocus")
        sim_log, pidfile = os.path.join(self.tmp, "sim.log"), os.path.join(self.tmp, "motor.pid")
        env = dict(os.environ)
        env["RAMAN_MOTOR_SIMULATE"] = "1"
        env["RAMAN_MOTOR_SIM_LOG"] = sim_log
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        env.pop("RAMAN_CONTROLLER_PID", None)

        def alive(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            return True

        qt = FakeQt(self.tmp, "orphan1")
        qt.start()
        controller = subprocess.Popen([sys.executable, "-c", stand_in, launcher, self.tmp, "orphan1", pidfile],
                                      env=env, stdin=subprocess.DEVNULL)
        motor_pid = None
        try:
            deadline = time.monotonic() + 30
            while not read_file(pidfile).strip().isdigit() and time.monotonic() < deadline:
                time.sleep(0.01)
            motor_pid = int(read_file(pidfile))
            while len(qt.requests) < 2 and time.monotonic() < deadline and alive(motor_pid):
                time.sleep(0.01)
            self.assertGreaterEqual(len(qt.requests), 2, "the scan did not reach its second point")
            controller.kill()
            controller.wait(timeout=5)
            deadline = time.monotonic() + 10
            while alive(motor_pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(alive(motor_pid), "the orphaned motor program is still running")
        finally:
            qt.stop()
            if controller.poll() is None:
                controller.kill()
            if motor_pid is not None and alive(motor_pid):
                os.kill(motor_pid, signal.SIGKILL)
        journal = read_file(sim_log)
        for serial in (SERIAL_X, SERIAL_Y, SERIAL_Z):
            self.assertIn("%s CLOSE" % serial, journal)
        self.assertLess(len(qt.requests), 13)

    def test_nothing_is_started_when_the_controller_is_already_gone(self):
        hs._ORIGINAL_PARENT = os.getppid() + 100000      # as if orphaned before start-up finished
        code, out, err, qt, devices = self.run_program("1d-x")
        self.assertEqual(code, 1)
        self.assertIn("[Safety stop] the controller process", err)
        self.assertIn("nothing was started", err)
        self.assertEqual(devices, {}, "no controller may be opened")
        self.assertNotIn("Building device list", out)

    def test_heartbeat_contact_timeout(self):
        run, directory = "hb", self.tmp
        response = os.path.join(directory, "response.txt")

        hs.publish(response, "ACK hb 1 PAUSED 42\n")             # frozen heartbeat
        started = time.monotonic()
        self.assertFalse(hs.wait_for_ack(directory, run, 1, timeout_ms=300))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 2.0)

        hs.publish(response, "ACK hb 1 FAIL 43\n")
        self.assertFalse(hs.wait_for_ack(directory, run, 1, timeout_ms=5000))
        hs.publish(response, "ACK hb 7 OK 44\n")                 # stale point id: ignored
        self.assertFalse(hs.wait_for_ack(directory, run, 1, timeout_ms=200))
        hs.publish(response, "ACK other 1 OK 45\n")              # other run: ignored
        self.assertFalse(hs.wait_for_ack(directory, run, 1, timeout_ms=200))
        hs.publish(response, "garbage\n")
        self.assertFalse(hs.wait_for_ack(directory, run, 1, timeout_ms=200))

        def keep_alive(state, point):
            for beat in range(10):                                # 1.0 s of changing heartbeats
                hs.publish(response, "ACK hb %d %s %d\n" % (point, state, beat))
                time.sleep(0.1)
            hs.publish(response, "ACK hb %d OK done\n" % point)

        for state, point in (("PAUSED", 2), ("HOLD", 4)):
            with self.subTest(state=state):
                hs.publish(response, "ACK hb %d %s stale\n" % (point, state))
                writer = threading.Thread(target=keep_alive, args=(state, point), daemon=True)
                started = time.monotonic()
                writer.start()
                try:
                    # the 300 ms contact timeout is refreshed by every new heartbeat
                    self.assertTrue(hs.wait_for_ack(directory, run, point, timeout_ms=300))
                    self.assertGreaterEqual(time.monotonic() - started, 0.9)
                finally:
                    writer.join(3.0)

        hs.publish(response, "ACK hb 3 OK 1\n")
        self.assertTrue(hs.wait_for_ack(directory, run, 3, timeout_ms=300))
        hs.publish(os.path.join(directory, "cancel.txt"), "STOP\n")
        self.assertFalse(hs.wait_for_ack(directory, run, 3, timeout_ms=5000))

    def test_program_times_out_on_frozen_pause_heartbeat(self):
        os.environ["RAMAN_MOTOR_ACK_TIMEOUT_MS"] = "300"
        code, out, err, qt, devices = self.run_program("1d-x", policy=lambda qt, point: ("PAUSED", "frozen"))
        self.assertNotEqual(code, 0)
        self.assertIn("[Point 1] FAILED/cancelled, do not advance", out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "advanced.txt")))
        self.assertEqual(len(devices[SERIAL_X].events("MOVE_ABS")), 1)

    # ---- arguments, publish, launcher ------------------------------------------------
    def test_argument_validation_and_exit_codes(self):
        cases = [
            (["RamanAutofocus"], "Start this version from the updated Qt controller."),
            (["RamanAutofocus", self.tmp], "Start this version from the updated Qt controller."),
            (["RamanAutofocus", self.tmp, "r", "1d-z"], "Invalid scan mode."),
            (["RamanAutofocus", self.tmp, "r", "2d"], "Invalid scan mode."),
            (["RamanAutofocus", self.tmp + "/é", "r"], "Handshake path must be ASCII."),
            (["RamanAutofocus", self.tmp, "rü", "1d-x"], "Handshake path must be ASCII."),
            (["RamanAutofocus", os.path.join(self.tmp, "missing"), "r"], ""),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = ra.main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(err.getvalue().strip(), message)
                self.assertEqual(out.getvalue(), "")

    def test_publish_is_atomic_and_cancel_detection(self):
        path = os.path.join(self.tmp, "request.txt")
        self.assertTrue(hs.publish(path, "STEP run 1 0 0 0\n"))
        self.assertEqual(read_file(path), "STEP run 1 0 0 0\n")
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertTrue(hs.publish(path, b"STEP run 2 1 2 3\n"))
        self.assertEqual(read_file(path), "STEP run 2 1 2 3\n")
        self.assertFalse(hs.publish(os.path.join(self.tmp, "missing", "x.txt"), "x"))
        self.assertFalse(hs.cancelled(self.tmp))
        hs.publish(os.path.join(self.tmp, "cancel.txt"), "STOP\n")
        self.assertTrue(hs.cancelled(self.tmp))

    def test_cout_flushes_completed_lines_only(self):
        """RamanAutofocusTest.cpp:201-210: the "X -> ... " prefix is written without a flush and
        std::endl after "OK" delivers the whole line; the controller (laser_test/main.cpp:2327)
        logs every stdout chunk as its own line, so the prefix must not be flushed alone.
        std::cerr is tied to std::cout, so a stderr message flushes pending stdout first."""
        class Pipe(io.StringIO):
            def __init__(self):
                super().__init__()
                self.flushes = []

            def flush(self):
                self.flushes.append(self.getvalue())
                super().flush()

        out, err = Pipe(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ra.cout("X -> 2.1000 mm (motor pos: 72870) ... ", end="")
            self.assertEqual(out.flushes, [])
            ra.cout("OK")
            self.assertEqual(out.flushes, ["X -> 2.1000 mm (motor pos: 72870) ... OK\n"])
            ra.cout("\n---- Sequence begin (X/Y) ----")
            self.assertEqual(len(out.flushes), 2)
            ra.cout("Move X to start (+2.0000 mm) pos=69400 ... ", end="")
            ra.cerr("Failed.")
            self.assertEqual(len(out.flushes), 3)
        self.assertEqual(err.getvalue(), "Failed.\n")

    # ---- transport selection (PORT_SPEC.md section 3, "Transport on macOS") -----------------
    def test_transport_selection_table(self):
        port = "/dev/cu.usbserial-27266528"
        table = [
            # (config `transport`, explicit port, env RAMAN_MOTOR_SIMULATE) -> transport kind
            (None, None, None, "pyftdi"),          # no config value: auto without a port
            ("auto", None, None, "pyftdi"),
            ("auto", port, None, "serial"),        # auto with an explicit port: pyserial
            ("auto", None, "0", "pyftdi"),         # only "1" overrides
            ("auto", None, "", "pyftdi"),
            ("auto", None, "1", "simulate"),
            ("auto", port, "1", "simulate"),
            ("pyftdi", port, None, "pyftdi"),      # an explicit transport wins over the port
            ("serial", None, None, "serial"),
            ("serial", port, None, "serial"),
            ("simulate", port, None, "simulate"),
            ("simulate", None, "0", "simulate"),
            ("pyftdi", None, "1", "simulate"),     # the environment overrides the config
            ("serial", port, "1", "simulate"),
            ("PyFTDI", None, None, "pyftdi"),      # case-insensitive
            (" serial ", None, None, "serial"),
            ("pyserial", None, None, "serial"),    # alias
            ("", None, None, "pyftdi"),            # empty value = auto
            (True, None, None, "simulate"),        # former open_device(serial, simulate=True)
            (False, port, None, "serial"),
        ]
        for config_transport, explicit, env, expected in table:
            with self.subTest(transport=config_transport, port=explicit, env=env):
                self.assertEqual(apt.select_transport(config_transport, explicit, env), expected)
        for bogus in ("usb", "kinesis", "yes", 3, "simulator"):
            with self.subTest(transport=bogus):
                with self.assertRaises(apt.MotorError):
                    apt.select_transport(bogus, None, None)
        self.assertEqual(apt.select_transport("kinesis", None, "1"), "simulate")   # env wins first
        self.assertEqual(set(apt.TRANSPORT_FACTORIES), {"pyftdi", "serial", "simulate"})
        self.assertEqual(apt.TRANSPORT_NAMES, ("auto", "pyftdi", "serial", "simulate"))

    def test_open_device_dispatches_on_selection(self):
        calls = []

        def factory(kind):
            def make(serial_number, explicit_port, baudrate):
                calls.append((kind, serial_number, explicit_port, baudrate))
                return apt.SimulatedTransport(serial_number)
            return make

        factories = {kind: factory(kind) for kind in ("simulate", "serial", "pyftdi")}
        os.environ.pop("RAMAN_MOTOR_SIMULATE", None)
        cases = [
            (dict(transport="auto"), "pyftdi"),
            (dict(transport="auto", explicit_port="/dev/ttyUSB0"), "serial"),
            (dict(transport="serial"), "serial"),
            (dict(transport="pyftdi", explicit_port="/dev/ttyUSB0"), "pyftdi"),
            (dict(transport="simulate"), "simulate"),
            (dict(transport="pyftdi", env_simulate="1"), "simulate"),
            (dict(transport="auto", explicit_port="/dev/ttyUSB0", env_simulate="1"), "simulate"),
            (dict(transport="simulate", env_simulate="0"), "simulate"),
        ]
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                calls.clear()
                dev = apt.open_device(SERIAL_X, factories=factories, baudrate=115200, **kwargs)
                self.assertEqual([c[0] for c in calls], [expected])
                self.assertEqual(calls[0][1:], (SERIAL_X, kwargs.get("explicit_port"), 115200))
                self.assertEqual(dev.info.serial_number, 27266528)
                dev.close()
        # env_simulate not given: the environment is consulted
        os.environ["RAMAN_MOTOR_SIMULATE"] = "1"
        calls.clear()
        apt.open_device(SERIAL_X, transport="pyftdi", factories=factories).close()
        self.assertEqual(calls[0][0], "simulate")
        os.environ["RAMAN_MOTOR_SIMULATE"] = "0"
        calls.clear()
        apt.open_device(SERIAL_X, transport="pyftdi", factories=factories).close()
        self.assertEqual(calls[0][0], "pyftdi")
        with self.assertRaises(apt.MotorError):
            apt.open_device(SERIAL_X, transport="kinesis", env_simulate="", factories=factories)
        # the default table: the serial transport without any port fails before touching hardware
        with self.assertRaises(apt.MotorError):
            apt.open_device("1", transport="serial", env_simulate="")
        # a transport whose open sequence fails (no GET_INFO reply) is closed again
        closed = []

        class Mute(apt.SimulatedTransport):
            def _handle(self, msg):
                if msg.msgid != apt.MGMSG_HW_REQ_INFO:
                    super()._handle(msg)

            def close(self):
                closed.append(self.serial_number)
                super().close()

        saved = apt.KDC101.RESPONSE_TIMEOUT
        apt.KDC101.RESPONSE_TIMEOUT = 0.2
        try:
            with self.assertRaises(apt.MotorError):
                apt.open_device(SERIAL_Y, transport="pyftdi", env_simulate="",
                                factories={"pyftdi": lambda sn, port, baud: Mute(sn)})
        finally:
            apt.KDC101.RESPONSE_TIMEOUT = saved
        self.assertEqual(closed, [SERIAL_Y])

    @unittest.skipUnless(HAVE_PYFTDI, "pyftdi is not installed")
    def test_pyftdi_transport_with_mocked_pyftdi(self):
        """PyFtdiTransport against pyftdi's real URL parser and a fake of the pyserial-compatible
        object serial_for_url returns (backed by the simulator): registration of 0403:FAF0, the
        URL, the open settings, purge + RTS, discovery, and a full open/home/move through the
        transport (whose polling must not rely on pyftdi's in_waiting, which is always 0)."""
        apt.PyFtdiTransport.register_product()
        apt.PyFtdiTransport.register_product()              # idempotent, no ValueError
        self.assertEqual(Ftdi.PRODUCT_IDS[0x0403].get("APT"), 0xFAF0)
        url = apt.PyFtdiTransport.url_for(SERIAL_X)
        self.assertEqual(url, "ftdi://0x0403:0xfaf0:27266528/1")

        cubes = [(UsbDeviceDescriptor(0x0403, 0xFAF0, 1, 4, SERIAL_X, None, "APT DC Motor Controller"), 1),
                 (UsbDeviceDescriptor(0x0403, 0xFAF0, 1, 5, SERIAL_Y, None, "APT DC Motor Controller"), 1),
                 (UsbDeviceDescriptor(0x0403, 0x6001, 1, 6, "FT1234", None, "FT232R USB UART"), 1)]
        find_calls = []

        def fake_find_all(vps, nocache=False):
            find_calls.append((sorted(vps), nocache))
            wanted = set(vps)
            return [(d, n) for d, n in cubes if (d.vid, d.pid) in wanted]

        opened = []

        class FakeFtdiSerial:
            """What pyftdi.serialext.serial_for_url returns: pyserial API + `.ftdi` (Ftdi)."""
            def __init__(self, url, **kwargs):
                self.url, self.kwargs = url, kwargs
                self.rts_history, self.purged, self.closed = [], [], False
                self.timeouts, self.latency, self.dynamic = (5000, 5000), None, None
                self.is_open, self.sim = False, None
                self.ftdi = self

            def open(self):                                   # FtdiSerial.open(): Ftdi.create_from_url
                serial = self.url.split(":")[-1].split("/")[0]
                if serial not in [d.sn for d, _ in cubes if d.pid == 0xFAF0]:
                    raise UsbToolsError("No USB device with S/N %s" % serial)
                self.sim = apt.SimulatedTransport(serial)
                self.udev, self.is_open = self, True

            def read_data_bytes(self, size, attempt=1):      # Ftdi: one USB bulk IN
                waiting = self.sim.in_waiting
                return bytearray(self.sim.read(min(size, waiting))) if waiting else bytearray()

            def set_dynamic_latency(self, lmin, lmax, threshold):
                self.dynamic = (lmin, lmax, threshold)

            def set_latency_timer(self, ms):
                self.latency = ms

            def read(self, size=1):
                return self.sim.read(size)

            def write(self, data):
                self.sim.write(data)
                return len(data)

            def reset_input_buffer(self):
                self.purged.append("rx")

            def reset_output_buffer(self):
                self.purged.append("tx")

            @property
            def in_waiting(self):
                return 0                                      # pyftdi: "not implemented"

            @property
            def rts(self):
                return self.rts_history[-1] if self.rts_history else None

            @rts.setter
            def rts(self, value):
                self.rts_history.append(bool(value))

            def close(self):
                self.closed = True
                self.sim.close()

        def fake_serial_for_url(url, **kwargs):         # pyserial.serial_for_url semantics
            do_not_open = kwargs.pop("do_not_open", False)
            ser = FakeFtdiSerial(url, **kwargs)
            opened.append(ser)
            if not do_not_open:
                ser.open()
            return ser

        # The failure path with the REAL pyftdi classes (only USB enumeration is faked): an
        # absent cube is a MotorError, and the half-built Serial object must not leave an
        # "Exception ignored in: Serial<...>" trace on stderr when it is garbage collected.
        unraisable = []
        with mock.patch.object(UsbTools, "find_all", staticmethod(fake_find_all)), \
                mock.patch.object(sys, "unraisablehook", lambda args: unraisable.append(args)):
            with self.assertRaises(apt.MotorError) as caught:
                apt.PyFtdiTransport("99999999")
            gc.collect()
        self.assertIn("No USB device with S/N 99999999", str(caught.exception))
        self.assertEqual(unraisable, [])

        with mock.patch.object(UsbTools, "find_all", staticmethod(fake_find_all)), \
                mock.patch.object(pyftdi.serialext, "serial_for_url", fake_serial_for_url):
            # discovery: only 0403:FAF0, by USB serial-number string
            self.assertEqual(apt.PyFtdiTransport.list_devices(), [SERIAL_Y, SERIAL_X])
            self.assertEqual(find_calls[-1], ([(0x0403, 0xFAF0)], True))
            # pyftdi's own URL parser resolves the spec'd URL to the cube once the id is registered
            desc, interface = UsbTools.parse_url(url, "ftdi", Ftdi.VENDOR_IDS, Ftdi.PRODUCT_IDS, Ftdi.DEFAULT_VENDOR)
            self.assertEqual((desc.vid, desc.pid, desc.sn, interface), (0x0403, 0xFAF0, SERIAL_X, 1))
            # (pyftdi reads a locator <= 255 as a device index; Thorlabs serials are 8 digits)
            with self.assertRaises(UsbToolsError):
                UsbTools.parse_url(apt.PyFtdiTransport.url_for("99999999"), "ftdi", Ftdi.VENDOR_IDS,
                                   Ftdi.PRODUCT_IDS, Ftdi.DEFAULT_VENDOR)

            transport = apt.PyFtdiTransport(SERIAL_X)
            ser = opened[-1]
            self.assertTrue(ser.is_open)
            self.assertEqual(ser.url, url)
            self.assertEqual(ser.kwargs, dict(baudrate=115200, bytesize=8, parity="N", stopbits=1,
                                              rtscts=True, timeout=transport.timeout))
            self.assertEqual(ser.purged, ["rx", "tx"])
            self.assertEqual(ser.rts_history, [True])
            self.assertEqual(ser.timeouts, (apt.PyFtdiTransport.USB_TIMEOUT_MS,) * 2)
            self.assertEqual(ser.dynamic, (0, 0, 0))
            self.assertEqual(ser.latency, apt.PyFtdiTransport.LATENCY_MS)

            dev = apt.KDC101(SERIAL_X, transport)                 # open sequence over pyftdi
            self.assertEqual(dev.info.serial_number, 27266528)
            self.assertEqual(dev.get_position(), START_X)
            self.assertEqual(dev.message_queue_size(), 0)

            def wait_event(expected, timeout=2.0):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if dev.message_queue_size() > 0 and dev.get_next_message()[:2] == expected:
                        return True
                    time.sleep(0.02)                              # the program's 20 ms poll
                return False

            dev.home()
            self.assertTrue(wait_event((2, 0)))                   # Homed, seen by non-blocking polls
            self.assertEqual(dev.get_position(), 0)
            dev.move_to_position(69400)
            self.assertTrue(wait_event((2, 1)))                   # MoveCompleted
            self.assertEqual(dev.get_position(), 69400)
            dev.close()
            self.assertTrue(ser.closed)
            journal = [e[0] for e in ser.sim.journal]
            self.assertEqual(journal[:5], ["START", "NO_FLASH", "ENABLE", "RESUME_ENDOFMOVEMSGS", "REQ_INFO"])

            # unknown serial: pyftdi's error becomes a MotorError, both directly and via open_device
            with self.assertRaises(apt.MotorError):
                apt.PyFtdiTransport("99999999")
            with self.assertRaises(apt.MotorError):
                apt.open_device("99999999", transport="auto", env_simulate="")
            self.assertEqual([s.is_open for s in opened], [True, False, False])

    def test_program_transport_from_config_and_environment(self):
        """The whole program: config `transport`, `serial_ports` and RAMAN_MOTOR_SIMULATE."""
        def write_config(**overrides):
            config = json.loads(json.dumps(ra.DEFAULT_CONFIG))
            config.update(overrides)
            path = os.path.join(self.tmp, "motor_config.json")
            with open(path, "w") as handle:
                json.dump(config, handle)
            os.environ["RAMAN_MOTOR_CONFIG"] = path

        prefix = "Current working directory: %s\nBuilding device list...\n" % os.getcwd()
        session = {}

        def fresh(name):                     # one session directory per case
            session[name] = os.path.join(self.tmp, name)
            os.mkdir(session[name])
            return session[name]

        # 1. transport=simulate in the config, environment unset -> simulator, full 1D run
        os.environ.pop("RAMAN_MOTOR_SIMULATE", None)
        write_config(transport="simulate")
        code, out, err, qt, devices = self.run_program("1d-x", run="cfg1", session_dir=fresh("cfg1"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(qt.requests), 13)
        self.assertEqual(set(devices), {SERIAL_X, SERIAL_Y, SERIAL_Z})

        # 2. transport=pyftdi in the config but RAMAN_MOTOR_SIMULATE=1 -> simulator
        apt.SIMULATED_DEVICES.clear()
        os.environ["RAMAN_MOTOR_SIMULATE"] = "1"
        write_config(transport="pyftdi")
        code, out, err, qt, devices = self.run_program("1d-y", run="cfg2", session_dir=fresh("cfg2"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(qt.requests), 13)
        self.assertEqual(devices[SERIAL_Y].counts("HOME"), 1)

        # 3. transport=serial with explicit ports that do not exist -> X cannot be opened:
        #    exit 1, exactly the C++ stderr line, nothing moves, no request published
        apt.SIMULATED_DEVICES.clear()
        os.environ.pop("RAMAN_MOTOR_SIMULATE", None)
        missing = os.path.join(self.tmp, "no-such-port")
        write_config(transport="serial", serial_ports={"X": missing, "Y": missing, "Z": missing})
        code, out, err, qt, devices = self.run_program("1d-x", run="cfg3", session_dir=fresh("cfg3"))
        self.assertEqual(code, 1)
        self.assertEqual(err, "Failed to connect to X device 27266528\n")
        self.assertEqual(out, prefix + "Opening X device 27266528 ...\n")
        self.assertEqual(qt.requests, [])
        self.assertEqual(devices, {})
        self.assertFalse(os.path.exists(os.path.join(session["cfg3"], "request.txt")))
        # ... and with RAMAN_MOTOR_DEBUG=1 the reason follows the C++ line
        os.environ["RAMAN_MOTOR_DEBUG"] = "1"
        code, out, err, qt, devices = self.run_program("1d-x", run="cfg4", session_dir=fresh("cfg4"))
        self.assertEqual(code, 1)
        lines = err.splitlines()
        index = lines.index("Failed to connect to X device 27266528")
        self.assertEqual(lines[index + 1][:8], "[Error] ", err)
        self.assertEqual(lines[index + 1], lines[-1])
        self.assertIn(missing, lines[-1])
        os.environ.pop("RAMAN_MOTOR_DEBUG")

        # 4. auto without ports -> pyftdi (factory and discovery patched: no USB access needed);
        #    the cube is absent, so X fails like on a Mac without the hardware
        write_config(transport="auto")
        attempts = []

        def no_cube(serial_number, explicit_port, baudrate):
            attempts.append((serial_number, explicit_port))
            raise apt.MotorError("cannot open %s: No USB device with S/N %s"
                                 % (apt.PyFtdiTransport.url_for(serial_number), serial_number))

        with mock.patch.dict(apt.TRANSPORT_FACTORIES, {"pyftdi": no_cube}), \
                mock.patch.object(apt.PyFtdiTransport, "list_devices", classmethod(lambda cls: [])):
            code, out, err, qt, devices = self.run_program("1d-x", run="cfg5", session_dir=fresh("cfg5"))
        self.assertEqual(code, 1)
        self.assertEqual(attempts, [(SERIAL_X, None)])
        self.assertEqual(err, "Failed to connect to X device 27266528\n")
        self.assertEqual(out, prefix + "Opening X device 27266528 ...\n")

        # 5. auto, but an explicit port on one axis -> that axis uses pyserial, the others pyftdi
        kinds = []

        def record(kind):
            def make(serial_number, explicit_port, baudrate):
                kinds.append((kind, serial_number, explicit_port))
                return apt.SimulatedTransport(serial_number)
            return make

        write_config(transport="auto", serial_ports={"X": None, "Y": "/dev/ttyUSB7", "Z": None})
        with mock.patch.dict(apt.TRANSPORT_FACTORIES, {"pyftdi": record("pyftdi"), "serial": record("serial")}), \
                mock.patch.object(apt.PyFtdiTransport, "list_devices", classmethod(lambda cls: [SERIAL_X, SERIAL_Z])), \
                mock.patch.object(ra, "build_device_list", wraps=ra.build_device_list) as listing:
            code, out, err, qt, devices = self.run_program("1d-x", run="cfg6", session_dir=fresh("cfg6"))
        self.assertEqual(code, 0, err)
        self.assertEqual(kinds, [("pyftdi", SERIAL_X, None), ("serial", SERIAL_Y, "/dev/ttyUSB7"), ("pyftdi", SERIAL_Z, None)])
        self.assertEqual(listing.call_args[0][0], {"pyftdi", "serial"})
        self.assertEqual(len(qt.requests), 13)

        # 6. discovery failure (e.g. no libusb backend) -> device-list failure before any open
        def boom(cls):
            raise RuntimeError("No backend available")

        write_config(transport="pyftdi")
        with mock.patch.object(apt.PyFtdiTransport, "list_devices", classmethod(boom)):
            code, out, err, qt, devices = self.run_program("1d-x", run="cfg7", session_dir=fresh("cfg7"))
        self.assertEqual(code, 1)
        self.assertEqual(err, "Failed to build device list. Please check connections.\n")
        self.assertEqual(out, prefix)

        # 7. an unknown transport name is a configuration error: reported, then exit 1
        write_config(transport="kinesis")
        code, out, err, qt, devices = self.run_program("1d-x", run="cfg8", session_dir=fresh("cfg8"))
        self.assertEqual(code, 1)
        self.assertIn("unknown transport 'kinesis'", err)
        self.assertTrue(err.endswith("Failed to build device list. Please check connections.\n"), err)
        self.assertEqual(out, prefix)

    def test_launcher_runs_end_to_end_with_live_stdout(self):
        launcher = os.path.join(MOTOR_DIR, "RamanAutofocus")
        self.assertTrue(os.access(launcher, os.X_OK))
        env = dict(os.environ)
        env["RAMAN_MOTOR_SIMULATE"] = "1"
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        qt = FakeQt(self.tmp, "sub1")
        qt.start()
        started = time.monotonic()
        try:
            proc = subprocess.Popen([launcher, self.tmp, "sub1", "1d-y"], cwd=MOTOR_DIR, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            first_line = proc.stdout.readline()
            first_line_at = time.monotonic() - started
            rest, err = proc.communicate(timeout=60)
        finally:
            qt.stop()
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(first_line, "Current working directory: %s\n" % MOTOR_DIR)
        self.assertLess(first_line_at, 3.0, "stdout must be flushed line by line")
        self.assertIn("Scan complete.", rest)
        self.assertEqual(rest.count("saved, advance"), 13)
        self.assertEqual(len(qt.requests), 13)
        self.assertEqual([r[2] for r in qt.requests], [i * 69400 for i in range(13)])
        self.assertEqual({r[1] for r in qt.requests}, {START_X})
        self.assertEqual({r[3] for r in qt.requests}, {START_Z})
        self.assertEqual(read_file(os.path.join(self.tmp, "advanced.txt")), "13")


if __name__ == "__main__":
    unittest.main()
