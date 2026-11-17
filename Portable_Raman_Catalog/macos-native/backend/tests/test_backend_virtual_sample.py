"""Virtual sample in the simulated capture backend (data_layer/PROTOCOL.md section 4).

Simulation only (RAMAN_BACKEND_SIMULATE=1 through backend_env; the motor program in the last test
runs with RAMAN_MOTOR_SIMULATE=1). With sample.json and reference_spectra.json in reach, a simulated
frame follows the simulated stage position, the laser-enable state, the power level and, for dark
captures, the capture id. The frames are compared with virtual_sample/sample_model.py, which states
the same rules in Python. Without the sample files the backend must behave as it always did.
Nothing here can reach a real instrument.

Run:  python3 -m unittest macos-native/backend/tests/test_backend_virtual_sample.py
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDITION = HERE.parents[2]
SHIPPED_SAMPLE = EDITION / "virtual_sample"
MOTOR_LAUNCHER = EDITION / "macos-native" / "motor" / "RamanAutofocus"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SHIPPED_SAMPLE))
from test_backend_protocol import Backend, LAUNCHER, PIXELS, RESULT_FILE, publish  # noqa: E402
import sample_model  # noqa: E402

STEPS_PER_MM = 34700


def install_sample(folder: Path, **changes) -> Path:
    """A copy of the shipped sample in `folder`; `changes` replace top-level keys of sample.json."""
    folder.mkdir(parents=True, exist_ok=True)
    sample = json.loads((SHIPPED_SAMPLE / "sample.json").read_text(encoding="utf-8"))
    sample.update(changes)
    (folder / "sample.json").write_text(json.dumps(sample), encoding="utf-8")
    shutil.copyfile(SHIPPED_SAMPLE / "reference_spectra.json", folder / "reference_spectra.json")
    return folder


def stage_content(x_mm: float, y_mm: float, z_mm: float) -> dict:
    return {"x_steps": round(x_mm * STEPS_PER_MM), "y_steps": round(y_mm * STEPS_PER_MM),
            "z_steps": round(z_mm * STEPS_PER_MM), "steps_per_mm": STEPS_PER_MM, "moving": False,
            "updated_utc": "2026-11-13T15:15:01.250Z"}


def set_environment(test, **values):
    """Environment for the backends a test starts (Backend copies os.environ); restored afterwards."""
    for name, value in values.items():
        saved = os.environ.get(name)
        os.environ[name] = str(value)
        if saved is None:
            test.addClassCleanup(os.environ.pop, name, None)
        else:
            test.addClassCleanup(os.environ.__setitem__, name, saved)


def start_backend(test, cwd: Path) -> Backend:
    if not LAUNCHER.exists():
        raise AssertionError("backend not published: %s missing" % LAUNCHER)
    backend = Backend(cwd)
    test.addClassCleanup(backend.proc.stdout.close)
    test.addClassCleanup(backend.stop)
    if backend.wait_line(lambda l: l.startswith("  Measure            (F) FrequencyMode"), 40) is None:
        raise AssertionError("backend did not start\n" + backend.transcript())
    return backend


def capture(backend: Backend, capture_id: str, timeout: float = 8.0):
    """`S <id>` like the controller's collectRamanData; returns the 2048 counts of the result file."""
    result = backend.cwd / "capture_results" / (capture_id + ".txt")
    (backend.cwd / "command_completed.txt").unlink(missing_ok=True)
    publish(backend.cwd / "command.txt", ("S %s\n" % capture_id).encode("ascii"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not result.exists():
        time.sleep(0.02)
    if not result.exists():
        raise AssertionError("no capture result for %s\n%s" % (capture_id, backend.transcript()))
    match = RESULT_FILE.match(result.read_text(encoding="utf-8"))
    if match is None or match.group("id") != capture_id:
        raise AssertionError("unexpected capture result for %s" % capture_id)
    return [int(value, 16) for value in match.group("row").split()]


def laser(backend: Backend, enable: bool, power=None):
    """Menu items 6 (enable) and 4 (power) with their parameters, as the controller sends them."""
    backend.send("6")
    backend.send("1" if enable else "0")
    if power is not None:
        backend.send("4")
        backend.send(str(power))


def synthetic_peak(frame) -> bool:
    """The spectrum the simulator produces without a sample: ~800 counts with a 5200-count peak at pixel 760."""
    return 5700 < frame[760] < 6400 and 700 < frame[100] < 900 and max(frame) < 7000


class FramesFollowTheSampleTest(unittest.TestCase):
    """One simulated backend, a noise-free copy of the shipped sample: every frame must equal the
    reference implementation count for count (methods run in name order)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="raman_vsample_"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.model = sample_model.VirtualSample.load(str(install_sample(cls.tmp / "sample", noise_counts_rms=0)))
        cls.stage_file = cls.tmp / "stage.json"
        cls.journal = cls.tmp / "journal.txt"
        set_environment(cls, RAMAN_SIM_SAMPLE_DIR=cls.tmp / "sample", RAMAN_SIM_STAGE_FILE=cls.stage_file,
                        RAMAN_SIM_JOURNAL=cls.journal)
        cwd = cls.tmp / "cwd"
        cwd.mkdir()
        cls.backend = start_backend(cls, cwd)
        cls.frames = {}

    def at(self, x: float, y: float, z: float, capture_id: str, level, laser_on: bool = True):
        """Put the simulated stage at (x, y, z) mm, capture, and compare with the reference."""
        stage = stage_content(x, y, z)
        publish(self.stage_file, json.dumps(stage).encode("ascii"))
        frame = capture(self.backend, capture_id)
        position = self.model.position_mm(stage)
        expected = self.model.frame(*position, laser_on=laser_on, level=level,
                                    dark_capture=sample_model.is_dark_capture(capture_id))
        self.assertEqual(len(frame), PIXELS)
        differing = [i for i in range(PIXELS) if frame[i] != expected[i]]
        self.assertEqual(differing, [], "%s at %r: first differing pixel %s" % (
            capture_id, position, differing and (differing[0], frame[differing[0]], expected[differing[0]])))
        self.frames[capture_id] = frame
        return frame

    def test_01_the_backend_says_which_sample_it_uses(self):
        b = self.backend
        note = "[sim] simulated spectra follow the virtual sample in "
        lines = [line for line in b.lines if line.startswith("[sim]")]
        self.assertEqual(len(lines), 1, b.transcript())
        self.assertTrue(lines[0].startswith(note), lines[0])
        self.assertEqual(os.path.realpath(lines[0][len(note):]), os.path.realpath(self.tmp / "sample"))
        self.assertLess(b.lines.index(lines[0]), b.lines.index("initialized device"))

    def test_02_laser_off_gives_the_dark_spectrum(self):
        # No stage file yet: the sample's start position, which is on the R6G droplet.
        self.assertFalse(self.stage_file.exists())
        frame = capture(self.backend, "off_start")
        self.assertEqual(frame, sample_model.clip_counts(self.model.dark))
        self.frames["dark"] = frame

    def test_03_laser_on_without_a_power_level_gives_no_signal(self):
        # The controller always sends a power level (menu item 4) with Laser ON; without one the
        # simulated power value is 0, which has no entry in power_scale.
        laser(self.backend, True)
        self.assertEqual(capture(self.backend, "on_no_power"), self.frames["dark"])

    def test_04_start_position_without_a_stage_file(self):
        laser(self.backend, True, power=3)
        frame = capture(self.backend, "on_start")
        self.assertEqual(frame, self.model.frame(*self.model.start_position_mm, level="HIGH"))
        self.frames["on_start"] = frame
        self.assertGreater(max(frame) - max(self.frames["dark"]), 10000, "R6G droplet at the start position")

    def test_05_frames_follow_the_stage_position(self):
        positions = [
            ("blank", 0.0, 2.5), ("r6g_band", 8.0, 2.5), ("r6g_mb_edge", 10.0, 2.5), ("mb_band", 14.0, 2.5),
            ("mb_adenine_edge", 18.0, 2.5), ("adenine_band", 20.0, 2.5), ("adenine_blank_edge", 24.0, 2.5),
            ("mb_band_along_y", 2.5, 14.0), ("off_the_plate", 30.0, 2.5),
            ("r6g_droplet", 2.36, 2.42), ("droplet_rim", 2.6, 2.4), ("droplets_overlap", 2.55, 2.55), ("mb_droplet", 2.72, 2.66),
        ]
        for name, x, y in positions:
            with self.subTest(position=name):
                self.at(x, y, 4.0, name, "HIGH")
        f = self.frames
        self.assertNotEqual(f["r6g_band"], f["mb_band"])
        self.assertNotEqual(f["r6g_band"], f["r6g_mb_edge"])
        self.assertNotEqual(f["blank"], f["adenine_blank_edge"])
        self.assertEqual(f["mb_band"], f["mb_band_along_y"], "frame regions use max(x, y)")
        self.assertEqual(f["blank"], f["off_the_plate"])
        # Where both droplets reach, their shares add up to more than 1 and are scaled down.
        weights = self.model.weights(2.55, 2.55)
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertGreater(weights["r6g"], 0.4)
        self.assertGreater(weights["methylene_blue"], 0.4)
        self.assertEqual(weights["di_water"], 0.0)

    def test_06_power_level_scales_the_signal(self):
        dark = self.frames["dark"]
        high = self.frames["mb_band"]
        self.assertEqual(sample_model.POWER_LEVELS[3], "HIGH")
        for power in (1, 2):                 # the values of the controller's power command (menu item 4)
            level = sample_model.POWER_LEVELS[power]
            with self.subTest(level=level):
                laser(self.backend, True, power=power)
                frame = self.at(14.0, 2.5, 4.0, "mb_band_" + level.lower(), level)
                peak = max(range(PIXELS), key=lambda i: high[i] - dark[i])
                self.assertAlmostEqual((frame[peak] - dark[peak]) / (high[peak] - dark[peak]),
                                       self.model.power_scale[level], places=3)
        self.assertEqual((self.model.power_scale["LOW"], self.model.power_scale["MEDIUM"]), (0.35, 0.65))
        laser(self.backend, True, power=3)

    def test_07_focus(self):
        dark = self.frames["dark"]
        high = self.frames["mb_band"]
        peak = max(range(PIXELS), key=lambda i: high[i] - dark[i])
        near = self.at(14.0, 2.5, 4.2, "mb_band_z42", "HIGH")
        self.assertAlmostEqual((near[peak] - dark[peak]) / (high[peak] - dark[peak]), 0.2, places=3)   # 1 / (1 + (0.2 / 0.1)^2)
        far = self.at(14.0, 2.5, 20.0, "mb_band_z20", "HIGH")
        self.assertLess(max(abs(far[i] - dark[i]) for i in range(PIXELS)), 5, "far out of focus: hardly any signal")

    def test_08_dark_capture_ignores_the_laser(self):
        # The controller's Capture Dark uses an id that starts with dark_ (the optical path is blocked).
        self.assertEqual(self.at(14.0, 2.5, 4.0, "dark_0f3a", "HIGH"), self.frames["dark"])
        # Only that prefix counts.
        self.assertEqual(self.at(14.0, 2.5, 4.0, "darkroom_1", "HIGH"), self.frames["mb_band"])

    def test_09_monitor_frame_follows_the_sample_too(self):
        b = self.backend
        publish(self.stage_file, json.dumps(stage_content(8.0, 2.5, 4.0)).encode("ascii"))
        start = b.line_count()
        (b.cwd / "command_completed.txt").unlink(missing_ok=True)
        publish(b.cwd / "command.txt", b"M monitor_1\n")
        line = b.wait_line(lambda l: l.startswith("MONITOR_FRAME "), 8, start)
        self.assertIsNotNone(line, b.transcript())
        tokens = line.split()
        self.assertEqual(tokens[1:5], ["CAPTURE", "monitor_1", "OK", "2048"])
        self.assertEqual([int(value, 16) for value in tokens[5:-2]], self.frames["r6g_band"])
        self.assertIsNotNone(b.wait_line(lambda l: l == "End of measurement", 4, start))

    def test_10_an_unusable_stage_file_means_the_start_position(self):
        b = self.backend
        start = b.line_count()
        publish(self.stage_file, b'{"x_steps": 277600, "y_steps": ')
        self.assertEqual(capture(b, "broken_stage"), self.frames["on_start"])
        self.assertIsNotNone(b.wait_line(lambda l: l.startswith("[sim] stage file ") and "not usable" in l, 2, start), b.transcript())

    def test_11_laser_off_again(self):
        laser(self.backend, False)
        self.assertEqual(self.at(14.0, 2.5, 4.0, "off_mb_band", None, laser_on=False), self.frames["dark"])

    def test_12_the_test_hooks_still_work(self):
        # RAMAN_SIM_JOURNAL (used by the safety-stop tests) recorded the laser calls of this class.
        journal = self.journal.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line for line in journal if line.startswith("USB2_setLD")], [
            "USB2_setLDPowerEnable 1",                                # test_03
            "USB2_setLDPowerEnable 1", "USB2_setLDPower 3",           # test_04
            "USB2_setLDPowerEnable 1", "USB2_setLDPower 1",           # test_06, LOW
            "USB2_setLDPowerEnable 1", "USB2_setLDPower 2",           # test_06, MEDIUM
            "USB2_setLDPowerEnable 1", "USB2_setLDPower 3",           # test_06, back to HIGH
            "USB2_setLDPowerEnable 0"])                               # test_11
        self.assertEqual(journal[-1], "USB2_captureStop")
        self.backend.send("Q")
        self.assertEqual(self.backend.proc.wait(timeout=15), 0, self.backend.transcript())


class NoiseTest(unittest.TestCase):
    """A copy of the shipped sample with noise_counts_rms = 6: frames carry Gaussian noise of that
    size around the reference."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="raman_vsample_noise_"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        cls.model = sample_model.VirtualSample.load(str(install_sample(cls.tmp / "sample", noise_counts_rms=6)))
        cls.stage_file = cls.tmp / "stage.json"
        set_environment(cls, RAMAN_SIM_SAMPLE_DIR=cls.tmp / "sample", RAMAN_SIM_STAGE_FILE=cls.stage_file)
        cwd = cls.tmp / "cwd"
        cwd.mkdir()
        cls.backend = start_backend(cls, cwd)

    def check_noise(self, frame, expected):
        residual = [frame[i] - expected[i] for i in range(PIXELS)]
        self.assertLess(abs(statistics.fmean(residual)), 0.6, "mean of 2048 samples with sigma 6: 4.5 sigma is 0.6")
        self.assertAlmostEqual(statistics.pstdev(residual), 6.0, delta=0.6)
        self.assertLess(max(abs(value) for value in residual), 40)

    def test_noise_around_the_reference(self):
        dark_a = capture(self.backend, "off_a")
        dark_b = capture(self.backend, "off_b")
        self.assertNotEqual(dark_a, dark_b, "two frames never carry the same noise")
        self.check_noise(dark_a, self.model.dark)
        self.check_noise(dark_b, self.model.dark)
        laser(self.backend, True, power=3)
        stage = stage_content(20.0, 2.5, 4.0)
        publish(self.stage_file, json.dumps(stage).encode("ascii"))
        frame = capture(self.backend, "adenine")
        self.check_noise(frame, self.model.expected(*self.model.position_mm(stage), level="HIGH"))
        self.check_noise(capture(self.backend, "dark_1"), self.model.dark)


class WithoutASampleTest(unittest.TestCase):
    """No usable sample: the synthetic spectrum of before, whatever the laser and the stage file say."""

    def run_backend(self, sample_dir: Path, tmp: Path):
        os.environ["RAMAN_SIM_SAMPLE_DIR"] = str(sample_dir)
        self.addCleanup(os.environ.pop, "RAMAN_SIM_SAMPLE_DIR", None)
        os.environ["RAMAN_SIM_STAGE_FILE"] = str(tmp / "stage.json")
        self.addCleanup(os.environ.pop, "RAMAN_SIM_STAGE_FILE", None)
        publish(tmp / "stage.json", json.dumps(stage_content(14.0, 2.5, 4.0)).encode("ascii"))
        cwd = tmp / "cwd"
        cwd.mkdir()
        backend = Backend(cwd)
        self.addCleanup(backend.proc.stdout.close)
        self.addCleanup(backend.stop)
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("  Measure            (F) FrequencyMode"), 40),
                             backend.transcript())
        return backend

    def temporary(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="raman_vsample_none_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        return tmp

    def check_synthetic(self, backend: Backend):
        off = capture(backend, "off_1")
        self.assertTrue(synthetic_peak(off), "laser off: still the synthetic spectrum")
        laser(backend, True, power=3)
        self.assertTrue(synthetic_peak(capture(backend, "on_1")))
        self.assertTrue(synthetic_peak(capture(backend, "dark_1")), "the capture id plays no part")
        backend.send("Q")
        self.assertEqual(backend.proc.wait(timeout=15), 0, backend.transcript())

    def test_no_sample_files(self):
        tmp = self.temporary()
        (tmp / "sample").mkdir()
        backend = self.run_backend(tmp / "sample", tmp)
        self.check_synthetic(backend)
        self.assertFalse(any(line.startswith("[sim]") for line in backend.lines), backend.transcript())

    def test_half_a_sample_is_no_sample(self):
        tmp = self.temporary()
        install_sample(tmp / "sample")
        (tmp / "sample" / "reference_spectra.json").unlink()
        backend = self.run_backend(tmp / "sample", tmp)
        self.check_synthetic(backend)
        self.assertFalse(any(line.startswith("[sim]") for line in backend.lines), backend.transcript())

    def test_an_unusable_sample_is_reported_and_ignored(self):
        tmp = self.temporary()
        install_sample(tmp / "sample", background="no_such_material")
        backend = self.run_backend(tmp / "sample", tmp)
        notes = [line for line in backend.lines if line.startswith("[sim]")]
        self.assertEqual(notes, ["[sim] virtual sample ignored (reference_spectra.json: no signal for material "
                                 "no_such_material): synthetic spectrum in use"])
        self.check_synthetic(backend)

    def test_a_missing_key_is_named(self):
        tmp = self.temporary()
        install_sample(tmp / "sample")
        path = tmp / "sample" / "sample.json"
        sample = json.loads(path.read_text(encoding="utf-8"))
        del sample["focus"]
        path.write_text(json.dumps(sample), encoding="utf-8")
        backend = self.run_backend(tmp / "sample", tmp)
        self.assertEqual([line for line in backend.lines if line.startswith("[sim]")],
                         ["[sim] virtual sample ignored (sample.json: focus is missing): synthetic spectrum in use"])
        self.assertTrue(synthetic_peak(capture(backend, "off_1")))


class BothSimulatorsTest(unittest.TestCase):
    """The layout the controller uses, no RAMAN_SIM_* variable: HOME/virtual_sample, the backend in
    HOME/scan_sessions/_backend_<id>, the motor program on a session folder in HOME/scan_sessions.
    The simulated motor program runs a 1D X scan; at every point the capture simulator must see the
    position the motor simulator published in HOME/scan_sessions/_sim_stage.json."""

    def test_line_scan_through_the_default_places(self):
        home = Path(tempfile.mkdtemp(prefix="raman_vsample_home_"))
        self.addCleanup(shutil.rmtree, home, True)
        model = sample_model.VirtualSample.load(str(install_sample(home / "virtual_sample")))
        noise = model.noise_counts_rms
        session = home / "scan_sessions" / "1d_x_scan_test"
        session.mkdir(parents=True)
        cwd = home / "scan_sessions" / "_backend_test"
        cwd.mkdir()
        for name in ("RAMAN_SIM_SAMPLE_DIR", "RAMAN_SIM_STAGE_FILE"):
            self.assertNotIn(name, os.environ)
        backend = Backend(cwd)
        self.addCleanup(backend.proc.stdout.close)
        self.addCleanup(backend.stop)
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("  Measure            (F) FrequencyMode"), 40),
                             backend.transcript())
        self.assertTrue(any(line.startswith("[sim] simulated spectra follow the virtual sample in ") for line in backend.lines),
                        backend.transcript())
        laser(backend, True, power=3)

        run = "run1"
        points, problems = [], []
        done = threading.Event()

        def controller():
            """Stand-in for the controller's handleScanPoint: capture at every STEP, then acknowledge."""
            answered = 0
            while not done.is_set():
                try:
                    tokens = (session / "request.txt").read_text().split()
                except OSError:
                    tokens = []
                if len(tokens) == 6 and tokens[0] == "STEP" and tokens[1] == run and int(tokens[2]) == answered + 1:
                    point = int(tokens[2])
                    try:
                        frame = capture(backend, "%s_%d" % (run, point))
                    except AssertionError as error:
                        problems.append(str(error))
                        return
                    points.append((point, int(tokens[3]), int(tokens[4]), int(tokens[5]), frame))
                    publish(session / "response.txt", ("ACK %s %d OK %d\n" % (run, point, time.time() * 1000)).encode("ascii"))
                    answered = point
                time.sleep(0.01)

        thread = threading.Thread(target=controller, daemon=True)
        thread.start()
        env = dict(os.environ, RAMAN_MOTOR_SIMULATE="1", RAMAN_BACKEND_SIMULATE="1")
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        try:
            motor = subprocess.run([str(MOTOR_LAUNCHER), str(session), run, "1d-x"], cwd=str(MOTOR_LAUNCHER.parent), env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
        finally:
            done.set()
            thread.join(10)
        self.assertEqual(problems, [])
        self.assertEqual(motor.returncode, 0, motor.stdout + motor.stderr)
        self.assertIn("[sim] simulated stage follows the virtual sample in ", motor.stdout)
        self.assertEqual([p[:4] for p in points], [(i + 1, i * 2 * STEPS_PER_MM, 86750, 138800) for i in range(13)])
        stage = json.loads((home / "scan_sessions" / "_sim_stage.json").read_text())
        self.assertEqual((stage["x_steps"], stage["y_steps"], stage["z_steps"], stage["moving"]), (832800, 86750, 138800, False))
        for point, x, y, z, frame in points:
            expected = model.expected(x / STEPS_PER_MM, y / STEPS_PER_MM, z / STEPS_PER_MM, level="HIGH")
            worst = max(abs(frame[i] - expected[i]) for i in range(PIXELS))
            self.assertLess(worst, 7 * noise + 1, "point %d at x = %d steps does not show the sample there" % (point, x))
        # 0 mm blank, 8 mm R6G band, 14 mm methylene blue band: clearly different spectra.
        blank, r6g, mb = points[0][4], points[4][4], points[7][4]
        self.assertGreater(max(r6g) - max(blank), 3000)
        self.assertGreater(max(mb) - max(r6g), 20000)
        backend.send("Q")
        self.assertEqual(backend.proc.wait(timeout=15), 0, backend.transcript())


if __name__ == "__main__":
    unittest.main(verbosity=2)
