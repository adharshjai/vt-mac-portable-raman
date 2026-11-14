"""Tests for the simulated stage of the motor simulator (data_layer/PROTOCOL.md section 4).

Simulation only (RAMAN_MOTOR_SIMULATE=1): with a virtual sample the three simulated axes start
where the simulated stage was left and publish their positions in a stage file for the capture
simulator. Without a sample nothing may differ from before. No instrument is involved.

Run:  /opt/anaconda3/bin/python3 -m unittest discover -s macos-native/motor/tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import test_motor as base           # FakeQt, the reply policies and the module imports (apt, hs, ra)

apt, hs, ra = base.apt, base.hs, base.ra
SERIAL_X, SERIAL_Y, SERIAL_Z = base.SERIAL_X, base.SERIAL_Y, base.SERIAL_Z
SHIPPED_SAMPLE = os.path.join(base.PROJECT_ROOT, "virtual_sample")
ENV_KEYS = base.ENV_KEYS + ("RAMAN_SIM_SAMPLE_DIR", "RAMAN_SIM_STAGE_FILE")
START = {"x": 86750, "y": 86750, "z": 138800}      # 2.5 mm, 2.5 mm, 4.0 mm at 34700 steps per mm
LINE = [i * 69400 for i in range(13)]              # 0, 2, ... 24 mm
UPDATED_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def read_stage(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class SimulatedStageTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["RAMAN_MOTOR_SIMULATE"] = "1"
        # The layout the controller uses: HOME/virtual_sample, HOME/scan_sessions/<session folder>.
        self.home = tempfile.mkdtemp(prefix="raman_sim_stage_")
        self.sample_dir = os.path.join(self.home, "virtual_sample")
        self.sessions = os.path.join(self.home, "scan_sessions")
        os.makedirs(self.sessions)
        self.stage_file = os.path.join(self.sessions, "_sim_stage.json")
        apt.SIMULATED_DEVICES.clear()
        self._saved_parent = hs._ORIGINAL_PARENT
        hs.reset_stop()

    def tearDown(self):
        hs._ORIGINAL_PARENT = self._saved_parent
        hs.reset_stop()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.home, ignore_errors=True)

    # ---- helpers -------------------------------------------------------------------------
    def install_sample(self, folder=None, **changes):
        """A copy of the shipped virtual sample; `changes` replace top-level keys of sample.json."""
        folder = folder or self.sample_dir
        shutil.copytree(SHIPPED_SAMPLE, folder, ignore=shutil.ignore_patterns("*.py", "*.md", "__pycache__"))
        if changes:
            path = os.path.join(folder, "sample.json")
            with open(path, "r", encoding="utf-8") as handle:
                sample = json.load(handle)
            sample.update(changes)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(sample, handle)
        return folder

    def session(self, name):
        path = os.path.join(self.sessions, name)
        os.mkdir(path)
        return path

    def run_program(self, mode, session_dir, run="run1", policy=base.ok_policy):
        argv = ["RamanAutofocus", session_dir, run] + ([mode] if mode != "2d" else [])
        qt = base.FakeQt(session_dir, run, policy)
        qt.start()
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ra.main(argv)
        finally:
            qt.stop()
        return code, out.getvalue(), err.getvalue(), qt, dict(apt.SIMULATED_DEVICES)

    # ---- with a virtual sample -----------------------------------------------------------
    def test_starts_from_the_sample_and_publishes_the_position(self):
        """Default lookup rules: <session>/../../virtual_sample and <session>/../_sim_stage.json."""
        self.install_sample()
        code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_a"))
        self.assertEqual(code, 0, err)
        self.assertIn("[sim] simulated stage follows the virtual sample in %s\n" % self.sample_dir, out)
        # X is homed and scanned; Y and Z stay at the sample's start position (2.5 mm and 4.0 mm).
        self.assertEqual([r[1] for r in qt.requests], LINE)
        self.assertEqual({r[2] for r in qt.requests}, {START["y"]})
        self.assertEqual({r[3] for r in qt.requests}, {START["z"]})
        self.assertEqual(devices[SERIAL_X].journal[0], ("START", START["x"]))
        stage = read_stage(self.stage_file)
        self.assertEqual(sorted(stage), ["moving", "steps_per_mm", "updated_utc", "x_steps", "y_steps", "z_steps"])
        self.assertEqual((stage["x_steps"], stage["y_steps"], stage["z_steps"]), (832800, START["y"], START["z"]))
        self.assertEqual(stage["steps_per_mm"], 34700)
        self.assertIs(stage["moving"], False)
        self.assertRegex(stage["updated_utc"], UPDATED_UTC)
        self.assertEqual(sorted(os.listdir(self.sessions)), ["1d_x_scan_a", "_sim_stage.json"], "no temporary file may stay")

    def test_the_file_is_final_before_a_point_is_requested(self):
        """The capture simulator reads the file when the controller captures a point, i.e. after the
        request: by then the file must hold exactly the requested position and say `moving: false`."""
        self.install_sample()
        seen = []

        def policy(qt, point):
            if len(seen) < point:
                seen.append((qt.requests[-1], read_stage(self.stage_file)))
            return base.ok_policy(qt, point)

        code, out, err, qt, devices = self.run_program("1d-y", self.session("1d_y_scan_a"), policy=policy)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(seen), 13)
        for (point, x, y, z), stage in seen:
            self.assertEqual((stage["x_steps"], stage["y_steps"], stage["z_steps"]), (x, y, z), "point %d" % point)
            self.assertIs(stage["moving"], False, "point %d" % point)
        self.assertEqual([request[2] for request, _stage in seen], LINE)

    def test_the_file_is_updated_about_ten_times_a_second_while_moving(self):
        self.install_sample()
        writes = []                      # (time, positions, moving)
        original = apt.SimulatedStage._write

        def recording(stage, positions, moving):
            writes.append((time.monotonic(), positions, moving))
            original(stage, positions, moving)

        unreadable = []
        stop_reading = threading.Event()

        def reader():                    # a concurrent reader must never see a partial file
            while not stop_reading.is_set():
                try:
                    read_stage(self.stage_file)
                except FileNotFoundError:
                    pass
                except ValueError as error:
                    unreadable.append(str(error))

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            with mock.patch.object(apt.SimulatedStage, "_write", recording):
                code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_b"))
        finally:
            stop_reading.set()
            thread.join(5)
        self.assertEqual(code, 0, err)
        self.assertEqual(unreadable, [])
        # The writes fall into runs that each end with a `moving: false` write: one when the axes
        # are opened (the start position), one for homing, one for each of the 13 moves.
        runs, current = [], []
        for _when, positions, moving in writes:
            current.append(positions[0])
            if not moving:
                runs.append(current)
                current = []
        self.assertEqual(current, [], "the last write says `moving: false`")
        self.assertEqual([run[-1] for run in runs], [START["x"], 0] + LINE)
        # A 2 mm move takes 0.2 s in the simulator: it is reported when it starts, once or twice on
        # the way, and when it ends. A stalled test machine may miss the update on the way, so not
        # every move has to show one.
        reported = 0
        for target, run in zip(LINE[1:], runs[3:]):
            self.assertLessEqual(len(run), 4, "more than about 10 Hz during the move to %d: %r" % (target, run))
            reported += any(target - 69400 < position < target for position in run[1:-1])
        self.assertGreaterEqual(reported, 9, "moves with an update on the way, of 12")
        gaps = [later[0] - earlier[0] for earlier, later in zip(writes, writes[1:]) if later[2]]
        self.assertGreaterEqual(min(gaps), apt.STAGE_WRITE_INTERVAL * 0.99, "updates while moving are at least 0.1 s apart")

    def test_a_new_process_resumes_from_the_stage_file(self):
        """Two real processes through the launcher: the second starts where the first left the stage."""
        self.install_sample()
        launcher = os.path.join(base.MOTOR_DIR, "RamanAutofocus")
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")

        def scan(mode, name):
            session = self.session(name)
            qt = base.FakeQt(session, name)
            qt.start()
            try:
                done = subprocess.run([launcher, session, name, mode], cwd=base.MOTOR_DIR, env=env, timeout=120,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            finally:
                qt.stop()
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("[sim] simulated stage follows the virtual sample in ", done.stdout)
            return qt.requests

        first = scan("1d-x", "first")
        self.assertEqual([r[1] for r in first], LINE)
        self.assertEqual(read_stage(self.stage_file)["x_steps"], 832800)
        second = scan("1d-y", "second")
        self.assertEqual({r[1] for r in second}, {832800}, "X stays where the first scan left it")
        self.assertEqual([r[2] for r in second], LINE)
        self.assertEqual({r[3] for r in second}, {START["z"]})
        stage = read_stage(self.stage_file)
        self.assertEqual((stage["x_steps"], stage["y_steps"], stage["z_steps"], stage["moving"]), (832800, 832800, START["z"], False))

    def test_an_unusable_stage_file_means_the_start_position(self):
        self.install_sample()
        for index, content in enumerate(('{"x_steps": 1, "y_ste', '{"x_steps": 1.5, "y_steps": 2, "z_steps": 3}',
                                         '{"x_steps": 1, "y_steps": 2}', '[1, 2, 3]', '{"x_steps": true, "y_steps": 2, "z_steps": 3}')):
            with self.subTest(content=content):
                with open(self.stage_file, "w", encoding="utf-8") as handle:
                    handle.write(content)
                stage = apt.SimulatedStage.find(self.session("unusable_%d" % index), {"X": SERIAL_X, "Y": SERIAL_Y, "Z": SERIAL_Z})
                self.assertEqual(stage.start_steps, {"X": START["x"], "Y": START["y"], "Z": START["z"]})

    def test_environment_overrides(self):
        """RAMAN_SIM_SAMPLE_DIR and RAMAN_SIM_STAGE_FILE replace the default places, and an explicit
        RAMAN_MOTOR_SIM_START entry still wins over the stage file for that axis."""
        elsewhere = tempfile.mkdtemp(prefix="raman_sim_stage_elsewhere_")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        sample = self.install_sample(os.path.join(elsewhere, "sample"), start_position_mm={"x": 1.0, "y": 2.0, "z": 3.0})
        stage_file = os.path.join(elsewhere, "stage.json")
        os.environ["RAMAN_SIM_SAMPLE_DIR"] = sample
        os.environ["RAMAN_SIM_STAGE_FILE"] = stage_file
        code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_c"))
        self.assertEqual(code, 0, err)
        self.assertEqual({(r[2], r[3]) for r in qt.requests}, {(69400, 104100)}, "Y = 2.0 mm, Z = 3.0 mm from the other sample")
        self.assertEqual(read_stage(stage_file)["x_steps"], 832800)
        self.assertFalse(os.path.exists(self.stage_file), "the default place must not be used")

        os.environ["RAMAN_MOTOR_SIM_START"] = "%s=4242" % SERIAL_Z
        apt.SIMULATED_DEVICES.clear()
        code, out, err, qt, devices = self.run_program("1d-y", self.session("1d_y_scan_c"))
        self.assertEqual(code, 0, err)
        self.assertEqual({(r[1], r[3]) for r in qt.requests}, {(832800, 4242)}, "X from the stage file, Z from the override")
        self.assertEqual(read_stage(stage_file)["z_steps"], 4242)

    def test_a_stage_file_that_cannot_be_written_is_reported_once(self):
        self.install_sample()
        os.environ["RAMAN_SIM_STAGE_FILE"] = os.path.join(self.home, "no_such_folder", "stage.json")
        code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_i"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(qt.requests), 13, "the simulated scan itself does not depend on the file")
        notes = [line for line in out.splitlines() if line.startswith("[sim] cannot write the stage file ")]
        self.assertEqual(len(notes), 1, out)
        self.assertIn("the capture simulator does not see the simulated stage position", notes[0])
        self.assertEqual(sorted(os.listdir(self.home)), ["scan_sessions", "virtual_sample"])

    def test_a_stop_during_a_move_publishes_where_the_axis_stopped(self):
        self.install_sample()

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
        code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_d"))
        trigger.join(5)
        self.assertNotEqual(code, 0)
        self.assertEqual(devices[SERIAL_X].counts("STOP"), 1)
        stage = read_stage(self.stage_file)
        self.assertEqual((stage["x_steps"], stage["y_steps"], stage["z_steps"]),
                         (devices[SERIAL_X].position, START["y"], START["z"]))
        self.assertIs(stage["moving"], False)

    # ---- without a usable sample: exactly the behaviour from before ------------------------
    def assert_unchanged(self, out, qt, devices):
        self.assertNotIn("[sim]", out)
        self.assertEqual([r[1] for r in qt.requests], LINE)
        self.assertEqual({(r[2], r[3]) for r in qt.requests}, {(base.START_Y, base.START_Z)})
        self.assertEqual(devices[SERIAL_X].journal[0], ("START", base.START_X))
        self.assertFalse(os.path.exists(self.stage_file), "no stage file without a virtual sample")

    def test_without_a_sample_nothing_changes(self):
        code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_e"))
        self.assertEqual(code, 0, err)
        self.assert_unchanged(out, qt, devices)
        self.assertEqual(os.listdir(self.sessions), ["1d_x_scan_e"])

    def test_half_a_sample_is_no_sample(self):
        self.install_sample()
        os.remove(os.path.join(self.sample_dir, "reference_spectra.json"))
        code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_f"))
        self.assertEqual(code, 0, err)
        self.assert_unchanged(out, qt, devices)

    def test_an_unusable_sample_is_reported_and_ignored(self):
        self.install_sample()
        with open(os.path.join(self.sample_dir, "sample.json"), "w", encoding="utf-8") as handle:
            handle.write('{"schema": "raman-virtual-sample/1", "steps_per_mm": 34700}')     # no start position
        code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_g"))
        self.assertEqual(code, 0, err)
        self.assertIn("[sim] virtual sample in ", out)
        self.assertIn(" ignored (KeyError: 'start_position_mm'): built-in start positions, no stage file\n", out)
        self.assertNotIn("[sim] simulated stage follows", out)
        self.assertEqual({(r[2], r[3]) for r in qt.requests}, {(base.START_Y, base.START_Z)})
        self.assertFalse(os.path.exists(self.stage_file))

    def test_the_real_transports_never_get_a_stage(self):
        """The stage is offered to open_device only as the factory of the simulated transport."""
        self.install_sample()
        os.environ.pop("RAMAN_MOTOR_SIMULATE", None)
        config_path = os.path.join(self.home, "motor_config.json")
        missing = os.path.join(self.home, "no-such-port")
        config = json.loads(json.dumps(ra.DEFAULT_CONFIG))
        config.update(transport="serial", serial_ports={"X": missing, "Y": missing, "Z": missing})
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        os.environ["RAMAN_MOTOR_CONFIG"] = config_path
        with mock.patch.object(apt.SimulatedStage, "find") as find:
            code, out, err, qt, devices = self.run_program("1d-x", self.session("1d_x_scan_h"))
        self.assertEqual(code, 1)
        find.assert_not_called()
        self.assertNotIn("[sim]", out)
        self.assertEqual(err, "Failed to connect to X device %s\n" % SERIAL_X)
        self.assertFalse(os.path.exists(self.stage_file))


if __name__ == "__main__":
    unittest.main()
