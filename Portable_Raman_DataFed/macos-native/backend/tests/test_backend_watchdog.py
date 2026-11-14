"""Safety stop of the Mac-native capture backend (SampleApl/Watchdog.cs).

Simulation only (RAMAN_BACKEND_SIMULATE=1 through backend_env): the simulated device stores the
laser-enable state and journals every laser call, so these tests switch the simulated laser on,
make the controller disappear or the backend fail in different ways, and check in watchdog.log and
in the journal what the backend did. Nothing here can reach a real instrument.

Exit codes: 5 = laser confirmed off, stop ran on the main thread; 6 = laser confirmed off by the
watchdog side (main thread stuck); 7 = the laser could not be confirmed off.

Run:  python3 -m unittest macos-native/backend/tests/test_backend_watchdog.py
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_backend_protocol import Backend, LAUNCHER, backend_env, publish  # noqa: E402

DISABLED = "laser disable rc=0; readback rc=0, LD Power Enable : 0x00000000"
NOT_CONFIRMED = "THE LASER IS NOT CONFIRMED OFF"
ENABLE, DISABLE = "USB2_setLDPowerEnable 1", "USB2_setLDPowerEnable 0"
CONTROLLER_KILL_DEADLINE = 2.0     # laser_test/main.cpp: terminate(), wait 2000 ms (3000 on macOS), kill()

# Stand-in for the controller: starts the backend, keeps the read end of its stdout pipe and
# copies it to a file. Killing it breaks that pipe and re-parents the backend, exactly like a
# controller that crashed or was force-quit.
CONTROLLER_STAND_IN = r'''
import subprocess, sys
launcher, cwd, pidfile, logfile = sys.argv[1:5]
child = subprocess.Popen([launcher], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
with open(pidfile, "w") as handle:
    handle.write(str(child.pid))
with open(logfile, "ab", buffering=0) as log:
    for line in child.stdout:
        log.write(line)
'''

# A controller that dies right after starting the backend. Like laser_test it hands its own pid
# down in RAMAN_CONTROLLER_PID; then it is gone before the backend has even initialised.
SHORT_LIVED_CONTROLLER = r'''
import os, subprocess, sys
launcher, cwd, pidfile = sys.argv[1:4]
env = dict(os.environ, RAMAN_CONTROLLER_PID=str(os.getpid()))
child = subprocess.Popen([launcher], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
with open(pidfile, "w") as handle:
    handle.write(str(child.pid))
os._exit(0)
'''


def read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wait_until(predicate, timeout: float, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(LAUNCHER.exists(), "backend not published: %s missing" % LAUNCHER)
        self._tmp = tempfile.TemporaryDirectory(prefix="raman_watchdog_")
        self.addCleanup(self._tmp.cleanup)
        self.cwd = Path(self._tmp.name)
        self.log = self.cwd / "watchdog.log"
        self.journal = self.cwd / "sim_journal.txt"
        self.hook("RAMAN_SIM_JOURNAL", str(self.journal))
        os.environ.pop("RAMAN_CONTROLLER_PID", None)

    def hook(self, name: str, value: str) -> None:
        os.environ[name] = value
        self.addCleanup(os.environ.pop, name, None)

    def start(self) -> Backend:
        backend = Backend(self.cwd)
        self.addCleanup(backend.stop)
        self.assertIsNotNone(backend.wait_line(lambda l: l == "initialized device", 40), backend.transcript())
        return backend

    def laser_on(self, backend: Backend) -> None:
        backend.send("6")
        backend.send("1")
        start = backend.line_count()
        backend.send("5")
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("LD Power Enable : 0x00000001"), 5, start),
                             backend.transcript())

    def start_capture(self, backend: Backend, capture_id: str) -> int:
        (self.cwd / "command_completed.txt").unlink(missing_ok=True)
        start = backend.line_count()
        publish(self.cwd / "command.txt", ("S %s\n" % capture_id).encode("ascii"))
        return start

    def laser_calls(self):
        return [line for line in read_text(self.journal).splitlines() if line.startswith("USB2_setLDPowerEnable")]

    def assert_orderly_stop(self, reason: str) -> None:
        log = read_text(self.log)
        self.assertIn("safety stop: " + reason, log)
        self.assertIn(DISABLED, log)
        self.assertIn("device closed", log)
        self.assertIn("exiting with code 5", log)
        self.assertNotIn(NOT_CONFIRMED, log)
        self.assertEqual(self.laser_calls()[-1], DISABLE, "the last laser call must be the disable")

    # ---- every way of asking the backend to terminate ------------------------------------------
    def _signal_stops(self, signum, name: str) -> None:
        backend = self.start()
        self.laser_on(backend)
        backend.proc.send_signal(signum)
        self.assertEqual(backend.proc.wait(timeout=10), 5, backend.transcript())
        self.assert_orderly_stop("received " + name)

    def test_sigterm_switches_the_laser_off(self):
        """What the controller sends when its window closes (QProcess::terminate), or pkill."""
        self._signal_stops(signal.SIGTERM, "SIGTERM")

    def test_sighup_switches_the_laser_off(self):
        self._signal_stops(signal.SIGHUP, "SIGHUP")

    def test_sigquit_switches_the_laser_off(self):
        self._signal_stops(signal.SIGQUIT, "SIGQUIT")

    # ---- a stop around the laser-enable command must never switch the laser ON -------------------
    def test_stop_while_waiting_for_the_laser_parameter(self):
        """Menu item 6 waits for its value in the next command file. A stop at that moment must not
        fall through to the device call: the only laser call is the stop's own disable."""
        backend = self.start()
        backend.send("6")
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("Disable : Other than 1"), 5), backend.transcript())
        backend.proc.send_signal(signal.SIGINT)
        self.assertEqual(backend.proc.wait(timeout=10), 5, backend.transcript())
        self.assert_orderly_stop("received SIGINT")
        self.assertEqual(self.laser_calls(), [DISABLE], "menu item 6 must not have called the device")

    def test_stop_right_after_the_enable_value_was_read(self):
        """The value "1" has just been read and the stop arrives before the device call: the laser
        must not be switched on after a stop was requested."""
        backend = self.start()
        backend.send("6")
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("Disable : Other than 1"), 5), backend.transcript())
        completed = self.cwd / "command_completed.txt"
        completed.unlink(missing_ok=True)
        publish(self.cwd / "command.txt", b"1")
        self.assertTrue(wait_until(completed.exists, 5, step=0.001), "the value was not read")
        backend.proc.send_signal(signal.SIGTERM)           # lands in the 100 ms that follow the read
        self.assertEqual(backend.proc.wait(timeout=10), 5, backend.transcript())
        self.assertNotIn(ENABLE, self.laser_calls(), "the laser was switched on after the stop request")
        self.assert_orderly_stop("received SIGTERM")

    # ---- a stop during a capture ---------------------------------------------------------------
    def test_stop_during_the_exposure_wait(self):
        backend = self.start()
        self.laser_on(backend)
        start = self.start_capture(backend, "cap1")
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("[Capture handshake v2.2]"), 10, start),
                             backend.transcript())
        sent = time.monotonic()
        backend.proc.send_signal(signal.SIGTERM)
        self.assertEqual(backend.proc.wait(timeout=10), 5, backend.transcript())
        self.assertLess(time.monotonic() - sent, CONTROLLER_KILL_DEADLINE, "the exposure wait must end at once")
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("[Capture failed] safety stop requested during the exposure"), 2, start),
                             backend.transcript())
        self.assert_orderly_stop("received SIGTERM")

    def test_stop_while_polling_for_the_frame(self):
        """The 50 ms status poll (the path the real module takes when the frame is not ready yet)."""
        self.hook("RAMAN_SIM_FRAME_READY_MS", "8000")
        backend = self.start()
        self.laser_on(backend)
        start = self.start_capture(backend, "poll1")
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("[Capture waiting]"), 10, start), backend.transcript())
        sent = time.monotonic()
        backend.proc.send_signal(signal.SIGTERM)
        self.assertEqual(backend.proc.wait(timeout=10), 5, backend.transcript())
        self.assertLess(time.monotonic() - sent, CONTROLLER_KILL_DEADLINE)
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("[Capture failed] safety stop requested while waiting for the frame"), 2, start),
                             backend.transcript())
        self.assert_orderly_stop("received SIGTERM")

    def test_a_late_frame_is_still_polled_and_captured(self):
        """Normal operation through the same patched poll: no stop, the capture must succeed."""
        self.hook("RAMAN_SIM_FRAME_READY_MS", "2000")
        backend = self.start()
        start = self.start_capture(backend, "late1")
        result = self.cwd / "capture_results" / "late1.txt"
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("[Capture waiting] rc=1"), 10, start), backend.transcript())
        self.assertTrue(wait_until(result.exists, 8), backend.transcript())
        self.assertTrue(read_text(result).startswith("CAPTURE late1 OK 2048\n"), read_text(result)[:60])
        backend.send("Q")
        self.assertEqual(backend.proc.wait(timeout=15), 0, backend.transcript())
        self.assertFalse(self.log.exists(), "no safety stop in normal operation")

    # ---- the controller disappears ---------------------------------------------------------------
    def test_controller_death_switches_the_laser_off(self):
        """The controller is killed outright (crash, force quit, Qt Creator's Stop button): the
        orphaned backend must notice, switch the laser off, close the device and exit."""
        pidfile, stdout_log = self.cwd / "backend.pid", self.cwd / "backend_stdout.log"
        controller = subprocess.Popen([sys.executable, "-c", CONTROLLER_STAND_IN, str(LAUNCHER), str(self.cwd),
                                       str(pidfile), str(stdout_log)], env=backend_env(), stdin=subprocess.DEVNULL)
        backend_pid = None
        try:
            self.assertTrue(wait_until(lambda: read_text(pidfile).strip().isdigit(), 20), "backend not started")
            backend_pid = int(read_text(pidfile))
            self.assertTrue(wait_until(lambda: "initialized device" in read_text(stdout_log), 40), read_text(stdout_log))

            def send(command: str) -> None:
                completed = self.cwd / "command_completed.txt"
                completed.unlink(missing_ok=True)
                publish(self.cwd / "command.txt", command.encode("utf-8"))
                self.assertTrue(wait_until(lambda: "done" in read_text(completed), 5), "command %r not consumed" % command)
                completed.unlink(missing_ok=True)

            send("6")
            send("1")
            send("5")
            self.assertTrue(wait_until(lambda: "LD Power Enable : 0x00000001" in read_text(stdout_log), 5), read_text(stdout_log))

            controller.kill()                      # SIGKILL: the controller gets no chance to clean up
            controller.wait(timeout=5)
            self.assertTrue(wait_until(lambda: not alive(backend_pid), 10), "the orphaned backend is still running")
            self.assert_orderly_stop("the controller process %d is gone" % controller.pid)
        finally:
            if controller.poll() is None:
                controller.kill()
            if backend_pid is not None and alive(backend_pid):
                os.kill(backend_pid, signal.SIGKILL)

    def test_controller_gone_before_startup_finished(self):
        """The controller dies while the backend is still starting. The backend must not conclude
        that there is nobody to watch: it stops as soon as it is up."""
        pidfile = self.cwd / "backend.pid"
        controller = subprocess.run([sys.executable, "-c", SHORT_LIVED_CONTROLLER, str(LAUNCHER), str(self.cwd), str(pidfile)],
                                    env=backend_env(), stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(controller.returncode, 0)
        backend_pid = int(read_text(pidfile))
        try:
            self.assertTrue(wait_until(lambda: not alive(backend_pid), 30), "the orphaned backend is still running")
            log = read_text(self.log)
            self.assertIn("was gone before start-up finished", log)
            self.assertIn(DISABLED, log)
            self.assertIn("exiting with code 5", log)
        finally:
            if alive(backend_pid):
                os.kill(backend_pid, signal.SIGKILL)

    # ---- the last resort ---------------------------------------------------------------------------
    def test_last_resort_when_a_device_call_hangs(self):
        """The main thread is stuck inside a device call (test hook), so it can never run the stop.
        The watchdog side must switch the laser off itself, and do so before the controller's
        terminate / wait / kill sequence gets to the kill."""
        self.hook("RAMAN_SIM_HANG_IN_DEVICE_CALL", "1")
        backend = self.start()
        self.laser_on(backend)
        start = self.start_capture(backend, "hang1")
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("[sim] hanging in USB2_getCaptureStatus"), 15, start),
                             backend.transcript())
        sent = time.monotonic()
        backend.proc.send_signal(signal.SIGTERM)
        try:
            code = backend.proc.wait(timeout=CONTROLLER_KILL_DEADLINE)
        except subprocess.TimeoutExpired:
            self.fail("still running %.1f s after SIGTERM: the controller would kill it before the laser is off"
                      % CONTROLLER_KILL_DEADLINE)
        self.assertEqual(code, 6, backend.transcript())
        self.assertGreater(time.monotonic() - sent, 0.9, "the main thread gets its grace period first")
        log = read_text(self.log)
        self.assertIn("the watchdog side did it instead", log)
        self.assertIn(DISABLED, log)
        self.assertIn("exiting with code 6", log)
        self.assertEqual(self.laser_calls()[-1], DISABLE)

    def test_a_refused_disable_is_retried_and_then_reported(self):
        """The library refuses the disable (test hook, return code 35 = now capturing). That must
        not be reported as a completed stop."""
        self.hook("RAMAN_SIM_LASER_DISABLE_RC", "35")
        backend = self.start()
        self.laser_on(backend)
        backend.proc.send_signal(signal.SIGTERM)
        self.assertEqual(backend.proc.wait(timeout=10), 7, backend.transcript())
        log = read_text(self.log)
        self.assertIn("laser disable rc=35", log)
        self.assertIn("(attempt 3)", log)
        self.assertIn(NOT_CONFIRMED, log)
        self.assertIn("exiting with code 7", log)
        self.assertNotIn("exiting with code 5", log)
        self.assertEqual(self.laser_calls().count(DISABLE), 3)
        self.assertEqual(read_text(self.journal).splitlines().count("USB2_captureStop"), 2, "a capture stop before each retry")

    def test_a_disable_that_never_returns_ends_with_the_warning(self):
        """Even the disable call hangs (test hook), on the main thread and then in the last resort.
        The process must still end, in time, and say that the laser is not confirmed off."""
        self.hook("RAMAN_SIM_HANG_IN_LASER_DISABLE", "1")
        backend = self.start()
        self.laser_on(backend)
        backend.proc.send_signal(signal.SIGTERM)
        try:
            code = backend.proc.wait(timeout=CONTROLLER_KILL_DEADLINE + 0.5)
        except subprocess.TimeoutExpired:
            self.fail("the process did not end although every attempt hung")
        self.assertEqual(code, 7, backend.transcript())
        log = read_text(self.log)
        self.assertIn("the emergency laser-off did not return within 700 ms", log)
        self.assertIn(NOT_CONFIRMED, log)

    def test_an_exception_in_the_command_loop_switches_the_laser_off(self):
        """A device call throws (test hook). Before, that ended the process with the laser as it was."""
        self.hook("RAMAN_SIM_THROW_IN_DEVICE_CALL", "1")
        backend = self.start()
        self.laser_on(backend)
        backend.send("3")
        self.assertEqual(backend.proc.wait(timeout=10), 5, backend.transcript())
        self.assert_orderly_stop("unhandled InvalidOperationException in the command loop")

    def test_normal_quit_is_unchanged(self):
        backend = self.start()
        backend.send("Q")
        self.assertEqual(backend.proc.wait(timeout=15), 0, backend.transcript())
        self.assertFalse(self.log.exists(), "no safety stop on a normal quit")


if __name__ == "__main__":
    unittest.main()
