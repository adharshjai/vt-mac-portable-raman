#!/opt/anaconda3/bin/python3
"""Tests of raman_sim_runner (data_layer/PROTOCOL.md section 7) against the real helper programs in
simulation: the deployed runner, backend and motor program, copied into a temporary HOME.

    /opt/anaconda3/bin/python3 -m unittest macos-native/sim_runner/test_sim_runner.py

Env RAMAN_DEPLOY_DIR names the deploy folder to copy (default: deploy-macos of this checkout, made
by macos-native/build_macos.sh). Standard library only.
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = Path(os.environ.get("RAMAN_DEPLOY_DIR") or ROOT / "deploy-macos")
# The Studio server sends SIGKILL this long after SIGTERM (studio_server.py), so a stop must end sooner.
STUDIO_GRACE_SECONDS = 5.0
RUN = ["--sample-id", "RUNNER-TEST", "--operator", "runner tests", "--location", "test bench"]


class RunnerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for part in ("raman_sim_runner", "handshake_v2", "virtual_sample"):
            # A hard failure, not a skip: a green run must mean the deployed runner works.
            if not (DEPLOY / part).exists():
                raise AssertionError("%s is missing: build the deploy folder first (macos-native/build_macos.sh)" % (DEPLOY / part))

    def setUp(self):
        # Resolved: the helper programs' command lines name the folder as the runner sees it (/private/var, not /var).
        self.home = Path(tempfile.mkdtemp(prefix="raman_runner_")).resolve()
        self.addCleanup(shutil.rmtree, self.home, True)
        shutil.copy2(DEPLOY / "raman_sim_runner", self.home)
        for folder in ("handshake_v2", "virtual_sample"):
            shutil.copytree(DEPLOY / folder, self.home / folder)
        (self.home / "scan_sessions").mkdir()

    def start(self, *arguments):
        # As the Studio starts it: its own session, HOME as working directory, output in one pipe.
        environment = dict(os.environ, RAMAN_BACKEND_SIMULATE="1", RAMAN_MOTOR_SIMULATE="1")
        process = subprocess.Popen([str(self.home / "raman_sim_runner")] + list(arguments), cwd=self.home, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        self.addCleanup(self.end, process)
        return process

    @staticmethod
    def end(process):
        if process.poll() is None:
            process.kill()
            process.wait(10)
        process.stdout.close()

    @staticmethod
    def read_until(process, text, lines):
        for raw in iter(process.stdout.readline, b""):
            lines.append(raw.decode("utf-8", "replace").rstrip("\n"))
            if text in lines[-1]:
                return True
        return False

    def run_to_end(self, *arguments):
        process = self.start(*arguments)
        output = process.stdout.read().decode("utf-8", "replace")
        return process.wait(120), output.splitlines()

    def helpers_left(self):
        listing = subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True, check=True).stdout
        return [line for line in listing.splitlines() if str(self.home) in line]

    def session(self, prefix):
        folders = sorted(self.home.glob("scan_sessions/%s*" % prefix))
        self.assertEqual(len(folders), 1, folders)
        return folders[0], json.loads((folders[0] / "session.json").read_text(encoding="utf-8"))

    def assert_in_order(self, lines, expected):
        position = 0
        for text in expected:
            found = [index for index, line in enumerate(lines) if index >= position and text in line]
            self.assertTrue(found, "%r missing after line %d of:\n%s" % (text, position, "\n".join(lines)))
            position = found[0] + 1


class Arguments(RunnerCase):
    def test_wrong_arguments_start_nothing(self):
        for arguments in ([], ["--mode", "3d", "--power", "HIGH"] + RUN, ["--mode", "single", "--power", "high"] + RUN,
                          ["--mode", "single", "--power", "HIGH", "--sample-id", " ", "--operator", "o", "--location", "l"]):
            code, lines = self.run_to_end(*arguments)
            self.assertEqual(code, 2, lines)
            self.assertTrue(lines and lines[-1].startswith("raman_sim_runner: "), lines)
        self.assertEqual(os.listdir(self.home / "scan_sessions"), [])
        self.assertFalse((self.home / "controller.lock").exists())

    def test_lock_file_that_cannot_be_created_is_not_a_held_lock(self):
        # Exit code 3 would tell the Studio that the window or another run is open.
        self.home.chmod(0o555)
        self.addCleanup(self.home.chmod, 0o755)
        code, lines = self.run_to_end("--mode", "single", "--power", "HIGH", *RUN)
        self.assertEqual(code, 1, lines)
        self.assertIn("controller.lock cannot be created (no permission)", lines[-1])
        self.assertEqual(os.listdir(self.home / "scan_sessions"), [])


class Runs(RunnerCase):
    def test_single_capture(self):
        code, lines = self.run_to_end("--mode", "single", "--power", "LOW", *RUN)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertTrue(all(line.startswith("[runner] ") for line in lines), lines)
        self.assert_in_order(lines, ["Simulated stage set back", "Capture backend ready: simulated device",
                                     "Laser ON confirmed (LOW)", "Dark saved: dark_measurements/dark_", "Spectrum saved: scan_sessions/single_",
                                     "Laser OFF confirmed", "Helper programs ended", "Done"])
        self.assertTrue(any(line.startswith("[runner] Capture backend ready: ") and line.endswith(", serial SIMULATED") for line in lines))
        folder, session = self.session("single_")
        darks = sorted(path.name for path in (self.home / "dark_measurements").glob("dark_*.txt"))
        self.assertEqual(len(darks), 1)
        self.assertEqual(session["dark_file"], darks[0], "the run is paired with the dark it captured")
        record = json.loads((self.home / "dark_measurements" / (darks[0][:-4] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual((record["schema"], record["dark_file"], record["simulated"]),
                         ("raman-dark-description/1", darks[0], {"backend": True, "motor": True}), "the dark's .json says simulated")
        self.assertEqual(record["instrument"]["serial"], "SIMULATED")
        self.assertEqual(session["scan_mode"], "single")
        self.assertEqual(session["simulated"], {"backend": True, "motor": True})
        self.assertEqual(session["laser_power_level"], "LOW")
        self.assertEqual(session["instrument"]["serial"], "SIMULATED")
        self.assertEqual((session["sample_id"], session["operator"], session["location"]), ("RUNNER-TEST", "runner tests", "test bench"))
        point = json.loads((folder / "point_0001.json").read_text(encoding="utf-8"))
        self.assertEqual(len(point["intensity"]), 2048)
        self.assertEqual(self.helpers_left(), [])
        self.assertFalse((self.home / "controller.lock").exists(), "the lock is released at the end")

    def test_stop_by_signal_while_a_second_run_is_refused(self):
        first = self.start("--mode", "1d-x", "--power", "HIGH", *RUN)
        lines = []
        self.assertTrue(self.read_until(first, "Scan started", lines), lines)
        code, refused = self.run_to_end("--mode", "single", "--power", "HIGH", *RUN)
        self.assertEqual(code, 3, refused)
        self.assertIn("controller.lock is held", refused[-1])
        self.assertTrue(self.read_until(first, "Point 2 of 13 saved", lines), lines)
        sent = time.monotonic()
        first.send_signal(signal.SIGTERM)
        lines.extend(first.stdout.read().decode("utf-8", "replace").splitlines())
        self.assertEqual(first.wait(30), 128 + signal.SIGTERM, "\n".join(lines))
        self.assertLess(time.monotonic() - sent, STUDIO_GRACE_SECONDS, "ended before the Studio's SIGKILL")
        self.assert_in_order(lines, ["Stop requested: stopping the scan as the Stop button does", "Laser OFF confirmed",
                                     "Helper programs ended", "Stopped on request (SIGTERM)"])
        folder, session = self.session("1d_x_scan_")
        saved = len(list(folder.glob("point_*.json")))
        self.assertTrue(2 <= saved < 13, saved)
        self.assertTrue((folder / "cancel.txt").exists())
        self.assertEqual(self.helpers_left(), [])

    def later_dark(self, record):
        """A dark dated after every other; its .json is written when record is not None."""
        folder = self.home / "dark_measurements"
        folder.mkdir(exist_ok=True)
        index = len(list(folder.glob("dark_*.txt")))
        name = "dark_29991231_235959_%03d.txt" % (900 + index)
        (folder / name).write_text("# Dark Measurement\n", encoding="utf-8")
        if record is not None:
            (folder / (name[:-4] + ".json")).write_text(record, encoding="utf-8")
        return name

    def test_later_real_or_unknown_dark_is_passed_over(self):
        # Lead decision D1: a simulated run is paired with a simulated dark only, however much later a
        # real dark (no .json, or one that says real) or a dark of unknown kind is dated.
        self.later_dark(None)
        self.later_dark(json.dumps({"simulated": {"backend": False, "motor": False}}))
        self.later_dark("not json")
        code, lines = self.run_to_end("--mode", "single", "--power", "HIGH", *RUN)
        self.assertEqual(code, 0, "\n".join(lines))
        _, session = self.session("single_")
        self.assertFalse(session["dark_file"].startswith("dark_2999"), session["dark_file"])
        self.assertIn("Dark saved: dark_measurements/" + session["dark_file"], "\n".join(lines))

    def test_later_simulated_dark_stops_the_run(self):
        later = self.later_dark(json.dumps({"simulated": {"backend": True, "motor": True}}))
        code, lines = self.run_to_end("--mode", "single", "--power", "HIGH", *RUN)
        self.assertEqual(code, 1, "\n".join(lines))
        self.assert_in_order(lines, ["Dark saved as dark_", "Laser OFF confirmed", "Helper programs ended"])
        self.assertIn("would be paired with dark_measurements/" + later, "\n".join(lines))
        self.assertEqual(list(self.home.glob("scan_sessions/single_*")), [], "nothing is captured with the wrong dark")
        self.assertEqual(self.helpers_left(), [])

    def test_stage_back_at_the_start_unless_kept(self):
        stage = self.home / "scan_sessions" / "_sim_stage.json"
        far = {"x_steps": 832800, "y_steps": 86750, "z_steps": 138800, "steps_per_mm": 34700, "moving": False,
               "updated_utc": "2026-11-13T15:15:01.250Z"}
        stage.write_text(json.dumps(far), encoding="utf-8")
        code, lines = self.run_to_end("--mode", "single", "--power", "HIGH", "--keep-stage", *RUN)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertIn("Simulated stage kept where the last simulated run left it", "\n".join(lines))
        self.assertEqual(json.loads(stage.read_text(encoding="utf-8")), far)
        code, lines = self.run_to_end("--mode", "single", "--power", "HIGH", *RUN)
        self.assertEqual(code, 0, "\n".join(lines))
        self.assertFalse(stage.exists(), "the next simulated move starts from the virtual sample's start position")


if __name__ == "__main__":
    unittest.main(verbosity=2)
