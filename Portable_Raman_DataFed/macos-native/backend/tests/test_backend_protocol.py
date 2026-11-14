#!/opt/anaconda3/bin/python3
"""Protocol test for the Mac-native capture backend (PORT_SPEC.md section 4).

Starts the published backend through its launcher (SampleApl/bin/publish/SampleApl) in
simulation mode (RAMAN_BACKEND_SIMULATE=1) inside a temporary working directory and drives it
through the file protocol exactly as the controller does (laser_test/main.cpp:
sendLaserCommand / setLaserEnabledAndConfirm / collectRamanData):

    6 then 1 (enable), 4 then 2 (power), 5 -> stdout "LD Power Enable : 0x00000001",
    S abc123 -> capture_results/abc123.txt, M mon1 -> MONITOR_FRAME line, Q -> exit 0.

The two start-up lines of the DataFed edition (data_layer/PROTOCOL.md section 3), CAPTURE_SETTINGS
and MODULE_INFO, are checked in simulation as well, including `MODULE_INFO unavailable`.

Also runs `--selftest` and asserts `SELFTEST OK` (PORT_SPEC.md section 4): a backend published
without libwinusb_shim.dylib (self-test exit 3, "shim missing") is an incomplete deliverable and
FAILS this test; it is not skipped.  Build first:
    make -C macos-native/backend/shim
    cd macos-native/backend/SampleApl && dotnet publish -c Release -o bin/publish
Run:
    /opt/anaconda3/bin/python3 -m unittest macos-native/backend/tests/test_backend_protocol.py
Env RAMAN_BACKEND_PUBLISH_DIR points the test at another published tree (used to exercise the
failure paths against a copy without the shim).
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parent
PUBLISH_DIR = Path(os.environ.get("RAMAN_BACKEND_PUBLISH_DIR") or (BACKEND_DIR / "SampleApl" / "bin" / "publish"))
LAUNCHER = PUBLISH_DIR / "SampleApl"
PIXELS = 2048
CAPTURE_SETTINGS = "CAPTURE_SETTINGS exposure_time_us=1000000 cycle_time_us=1100000"   # EXPOSURE_TIME / CYCLE_TIME of Program.cs
MODULE_INFO = re.compile(r'^MODULE_INFO model="([^"]*)" serial="([^"]*)" unit="([^"]*)" firmware="([^"]*)"$')
HEX_ROW = re.compile(r"^(?:[0-9A-F]{4} ){%d}$" % PIXELS)
RESULT_FILE = re.compile(r"^CAPTURE (?P<id>[A-Za-z0-9_-]+) OK 2048\n(?P<row>(?:[0-9A-F]{4} ){2048})\nEND (?P<end>[A-Za-z0-9_-]+)\n$")


def backend_env():
    env = dict(os.environ)
    env["RAMAN_BACKEND_SIMULATE"] = "1"
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env.setdefault("DOTNET_ROOT", str(Path.home() / ".dotnet"))
    env["PATH"] = env["DOTNET_ROOT"] + os.pathsep + env.get("PATH", "")
    return env


def hamamatsu_attached() -> bool:
    """True when macOS lists a USB device with Hamamatsu's vendor id 0x0661 (1633). Passive: it
    reads the kernel's device registry and opens nothing."""
    try:
        registry = subprocess.run(["/usr/sbin/ioreg", "-p", "IOUSB", "-l", "-w0"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return b'"idVendor" = 1633' in registry


def publish(path: Path, data: bytes):
    """Atomic publication like AcquisitionProtocol::publish (temp file + rename)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


class Backend:
    """One backend process plus a stdout pump; mirrors the controller's QProcess handling."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.proc = subprocess.Popen(
            [str(LAUNCHER)], cwd=str(cwd), env=backend_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.lines = []
        self.cond = threading.Condition()
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()

    def _pump(self):
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            with self.cond:
                self.lines.append(line)
                self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()

    def transcript(self, tail=40):
        with self.cond:
            lines = self.lines[-tail:]
        return "\n".join(l if len(l) < 200 else l[:200] + "..." for l in lines)

    def line_count(self):
        with self.cond:
            return len(self.lines)

    def wait_line(self, predicate, timeout, start=0):
        """First line (index >= start) satisfying predicate, or None after timeout."""
        deadline = time.monotonic() + timeout
        scanned = start
        with self.cond:
            while True:
                for i in range(scanned, len(self.lines)):
                    if predicate(self.lines[i]):
                        return self.lines[i]
                scanned = len(self.lines)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cond.wait(min(remaining, 0.05))

    # main.cpp sendLaserCommand: remove the ack, publish command.txt, wait for "done", remove it.
    def send(self, cmd: str, timeout=4.0):
        command = self.cwd / "command.txt"
        completed = self.cwd / "command_completed.txt"
        if completed.exists():
            completed.unlink()
        if command.exists():
            raise AssertionError("pending backend command")
        publish(command, cmd.encode("utf-8"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if completed.exists():
                try:
                    text = completed.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if "done" in text:
                    completed.unlink()
                    return True
            time.sleep(0.02)
        raise AssertionError("timeout waiting for command processing of %r\n%s" % (cmd, self.transcript()))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


class BackendProtocolTest(unittest.TestCase):
    """The controller's command sequence against one simulated backend (methods run in name order)."""

    @classmethod
    def setUpClass(cls):
        if not LAUNCHER.exists():
            # A hard failure, not a skip: a green run must mean the published backend works.
            raise AssertionError(
                "backend not published (%s missing): run `dotnet publish -c Release -o bin/publish` in %s"
                % (LAUNCHER, BACKEND_DIR / "SampleApl"))
        cls.tmp = tempfile.TemporaryDirectory(prefix="raman_backend_")
        cls.cwd = Path(cls.tmp.name)
        cls.backend = Backend(cls.cwd)

    @classmethod
    def tearDownClass(cls):
        cls.backend.stop()
        cls.tmp.cleanup()

    def test_01_startup_banner(self):
        b = self.backend
        self.assertIsNotNone(b.wait_line(lambda l: l == "initialized device", 30),
                             "backend did not initialise\n" + b.transcript())
        self.assertIsNotNone(b.wait_line(lambda l: l.startswith("  Measure            (F) FrequencyMode"), 5))
        self.assertIsNone(b.proc.poll(), "backend exited early\n" + b.transcript())
        self.assertFalse((self.cwd / "test_write.txt").exists())
        # DataFed edition: the capture settings and the module information, between the
        # initialisation and the menu.
        banner = b.lines[:b.lines.index("  Menu               (0) Menu")]
        initialised = banner.index("initialized device")
        self.assertEqual(banner[initialised + 1], CAPTURE_SETTINGS, b.transcript())
        info = MODULE_INFO.match(banner[initialised + 2])
        self.assertIsNotNone(info, banner[initialised + 2])
        # The simulated device says what it is: the same strings menu item 1 prints (test_06).
        self.assertEqual(info.groups(), ("C13560-0274", "SIMULATED", "SIM", "1.00"))

    def test_02_enable_laser_and_confirm(self):
        # main.cpp setLaserEnabledAndConfirm(true): 6, 1, 4, <power>, 5 -> "LD Power Enable : 0x00000001"
        b = self.backend
        start = b.line_count()
        # The prompts ("Set LD Power Enable > ") have no newline until the value is echoed, so
        # do not wait for them (the controller does not either); check the echoed line afterwards.
        b.send("6")
        b.send("1")
        self.assertIsNotNone(b.wait_line(lambda l: l == "Set LD Power Enable > 1", 4, start), b.transcript())
        b.send("4")
        b.send("2")
        self.assertIsNotNone(b.wait_line(lambda l: l == "Set LD Power > 2", 4, start), b.transcript())
        b.send("5")
        line = b.wait_line(lambda l: l.startswith("LD Power Enable"), 4, start)
        self.assertEqual(line, "LD Power Enable : 0x00000001", b.transcript())
        self.assertTrue(re.search(r"LD Power Enable\s*:\s*0x([0-9a-fA-F]+)", line))  # controller regex
        b.send("3")
        self.assertEqual(b.wait_line(lambda l: l.startswith("LD Power :"), 4, start), "LD Power : 0x00000002")
        self.assertFalse(any("Error code" in l for l in b.lines[start:]), b.transcript())

    def test_03_single_capture_result_file(self):
        # main.cpp collectRamanData: publish "S <id>\n", wait for capture_results/<id>.txt
        b = self.backend
        start = b.line_count()
        result = self.cwd / "capture_results" / "abc123.txt"
        completed = self.cwd / "command_completed.txt"
        if completed.exists():
            completed.unlink()
        publish(self.cwd / "command.txt", b"S abc123\n")
        t0 = time.monotonic()
        deadline = t0 + 6.0
        while time.monotonic() < deadline and not result.exists():
            time.sleep(0.02)
        elapsed = time.monotonic() - t0
        self.assertTrue(result.exists(), "no capture result within 6 s\n" + b.transcript())
        print("\n[test] S abc123 result published after %.2f s" % elapsed, file=sys.stderr)
        self.assertLessEqual(elapsed, 3.0, "capture took too long (%.2f s)" % elapsed)
        self.assertFalse(result.with_name(result.name + ".tmp").exists())
        text = result.read_text(encoding="utf-8")
        match = RESULT_FILE.match(text)
        self.assertIsNotNone(match, "unexpected result file content:\n" + text[:200])
        self.assertEqual(match.group("id"), "abc123")
        self.assertEqual(match.group("end"), "abc123")
        pixels = [int(v, 16) for v in match.group("row").split()]
        self.assertEqual(len(pixels), PIXELS)
        self.assertTrue(all(0 <= v <= 65535 for v in pixels))
        self.assertGreater(max(pixels), 1000, "synthetic spectrum should have peaks")
        self.assertGreater(min(pixels), 500, "synthetic spectrum should have a ~800 count baseline")
        # Legacy console output for the non-preview capture.
        handshake = b.wait_line(lambda l: l.startswith("[Capture handshake v2.2] Waiting "), 1, start)
        self.assertIsNotNone(handshake, b.transcript())
        self.assertIn("rc=1 polls the SAME capture, deadline=8000 ms.", handshake)
        self.assertIsNotNone(b.wait_line(lambda l: l.startswith("[Capture ready] elapsed_ms="), 1, start))
        image = b.wait_line(lambda l: l.startswith("Image Data "), 2, start)
        self.assertIsNotNone(image, b.transcript())
        self.assertEqual(image[len("Image Data "):].split(), [f"{v:04X}" for v in pixels])
        self.assertIsNotNone(b.wait_line(lambda l: l == "End of measurement", 2, start))
        self.assertFalse(any(l.startswith("[Capture failed]") for l in b.lines[start:]), b.transcript())
        # The ack file is written for S as well (consumption only).
        self.assertTrue(completed.exists())

    def test_04_monitor_frame(self):
        # main.cpp collectRamanData(preview=true): "M <id>\n" -> MONITOR_FRAME line on stdout
        b = self.backend
        start = b.line_count()
        images_before = sum(1 for l in b.lines if l.startswith("Image Data "))
        completed = self.cwd / "command_completed.txt"
        if completed.exists():
            completed.unlink()
        publish(self.cwd / "command.txt", b"M mon1\n")
        line = b.wait_line(lambda l: l.startswith("MONITOR_FRAME "), 6, start)
        self.assertIsNotNone(line, "no MONITOR_FRAME line\n" + b.transcript())
        payload = line[len("MONITOR_FRAME "):]
        self.assertTrue(payload.startswith("CAPTURE mon1 OK 2048 "), payload[:60])
        tokens = payload.split()
        self.assertEqual(tokens[:4], ["CAPTURE", "mon1", "OK", "2048"])
        self.assertEqual(tokens[-2:], ["END", "mon1"])
        self.assertEqual(len(tokens), PIXELS + 6)
        self.assertTrue(all(re.fullmatch(r"[0-9A-F]{4}", t) for t in tokens[4:-2]))
        self.assertFalse((self.cwd / "capture_results" / "mon1.txt").exists(), "preview must not write a file")
        # Preview never prints the Image Data row.
        self.assertIsNotNone(b.wait_line(lambda l: l == "End of measurement", 2, start))
        self.assertEqual(sum(1 for l in b.lines if l.startswith("Image Data ")), images_before)

    def test_05_disable_laser(self):
        b = self.backend
        start = b.line_count()
        b.send("6")
        b.send("0")
        self.assertIsNotNone(b.wait_line(lambda l: l == "Set LD Power Enable > 0", 4, start), b.transcript())
        b.send("5")
        self.assertEqual(b.wait_line(lambda l: l.startswith("LD Power Enable"), 4, start), "LD Power Enable : 0x00000000")

    def test_06_other_menu_items(self):
        b = self.backend
        start = b.line_count()
        b.send("1")
        self.assertEqual(b.wait_line(lambda l: l.startswith("SerialNo"), 4, start), "SerialNo  : SIMULATED")
        self.assertEqual(b.wait_line(lambda l: l.startswith("UnitID"), 4, start), "UnitID    : SIM")
        b.send("2")
        self.assertIsNotNone(b.wait_line(lambda l: re.fullmatch(r"  B5 : -?\d\.\d{6}E[+-]\d{3}", l), 4, start))
        b.send("7")
        self.assertEqual(b.wait_line(lambda l: l.startswith("LDTemp"), 4, start), "LDTemp : 25[degC]")
        b.send("8")
        self.assertEqual(b.wait_line(lambda l: l.startswith("LDBoardTemp"), 4, start), "LDBoardTemp : 25[degC]")

    def test_07_quit_exit_code(self):
        b = self.backend
        b.send("Q")
        try:
            code = b.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("backend did not exit after Q\n" + b.transcript())
        self.assertEqual(code, 0, b.transcript())
        b.pump.join(5)
        # Printed once per start, however many commands followed (menu item 1 included).
        self.assertEqual([l for l in b.lines if l.startswith("CAPTURE_SETTINGS")], [CAPTURE_SETTINGS])
        self.assertEqual(len([l for l in b.lines if l.startswith("MODULE_INFO")]), 1)


class StartupInfoTest(unittest.TestCase):
    """MODULE_INFO must never stop the backend: when the information call fails (simulated device
    test hooks) the line reads `MODULE_INFO unavailable` and everything else works as before."""

    def start(self, hook: str, value: str) -> Backend:
        self.assertTrue(LAUNCHER.exists(), "backend not published: %s missing" % LAUNCHER)
        os.environ[hook] = value
        self.addCleanup(os.environ.pop, hook, None)
        tmp = tempfile.TemporaryDirectory(prefix="raman_backend_info_")
        self.addCleanup(tmp.cleanup)
        backend = Backend(Path(tmp.name))
        self.addCleanup(backend.proc.stdout.close)
        self.addCleanup(backend.stop)
        self.assertIsNotNone(backend.wait_line(lambda l: l.startswith("  Measure            (F) FrequencyMode"), 40),
                             backend.transcript())
        return backend

    def check_unavailable(self, hook: str, value: str):
        b = self.start(hook, value)
        banner = b.lines[:b.lines.index("  Menu               (0) Menu")]
        initialised = banner.index("initialized device")
        self.assertEqual(banner[initialised + 1:initialised + 3], [CAPTURE_SETTINGS, "MODULE_INFO unavailable"], b.transcript())
        # The controller reads "Error code" in the backend output as a failed laser command.
        self.assertFalse(any("Error code" in line for line in banner), b.transcript())
        # The backend carries on: laser read-back and a capture work.
        start = b.line_count()
        b.send("5")
        self.assertEqual(b.wait_line(lambda l: l.startswith("LD Power Enable"), 4, start), "LD Power Enable : 0x00000000")
        result = b.cwd / "capture_results" / "after_info.txt"
        publish(b.cwd / "command.txt", b"S after_info\n")
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and not result.exists():
            time.sleep(0.02)
        self.assertTrue(result.exists(), b.transcript())
        self.assertIsNotNone(RESULT_FILE.match(result.read_text(encoding="utf-8")))
        b.send("Q")
        self.assertEqual(b.proc.wait(timeout=15), 0, b.transcript())
        self.assertFalse((b.cwd / "watchdog.log").exists(), "no safety stop")

    def test_padding_and_quotes_are_removed_from_the_values(self):
        # The real library pads its strings with NULs; the hook makes the simulated ones do the same
        # and puts a double quote into each. The line must come out as if they were clean.
        b = self.start("RAMAN_SIM_MODULE_INFO_PADDING", "1")
        info = [line for line in b.lines if line.startswith("MODULE_INFO")]
        self.assertEqual(info, ['MODULE_INFO model="C13560-0274" serial="SIMULATED" unit="SIM" firmware="1.00"'])
        # Menu item 1 still prints the strings as the device returned them.
        start = b.line_count()
        b.send("1")
        serial = b.wait_line(lambda l: l.startswith("SerialNo"), 4, start)
        self.assertEqual(serial, 'SerialNo  : S"IMULATED' + "\x00" * 5, repr(serial))
        b.send("Q")
        self.assertEqual(b.proc.wait(timeout=15), 0, b.transcript())

    def test_refused_information_call(self):
        self.check_unavailable("RAMAN_SIM_MODULE_INFO_RC", "5")

    def test_information_call_that_throws(self):
        self.check_unavailable("RAMAN_SIM_THROW_IN_MODULE_INFO", "1")


class SelfTestCase(unittest.TestCase):
    """`SampleApl --selftest` must load wrrusb2a.dll through the shim and print SELFTEST OK."""

    def test_selftest(self):
        if hamamatsu_attached():
            # Hardware safety, not a missing deliverable: with the real module on USB the self-test
            # would open it through the real library. Unplug the spectrometer to run this test.
            self.skipTest("a Hamamatsu module is attached: the real-library self-test is skipped so that "
                          "nothing talks to the instrument")
        self.assertTrue(LAUNCHER.exists(), "backend not published: %s missing" % LAUNCHER)
        with tempfile.TemporaryDirectory(prefix="raman_selftest_") as tmp:
            env = backend_env()
            env.pop("RAMAN_BACKEND_SIMULATE", None)
            run = subprocess.run([str(LAUNCHER), "--selftest"], cwd=tmp, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=120)
        output = run.stdout.decode("utf-8", "replace")
        lines = output.splitlines()
        if run.returncode == 3 and "shim missing" in output:
            # PORT_SPEC.md section 4: the test asserts SELFTEST OK. A missing shim means the
            # deliverable is incomplete, so this is a failure, never a skip.
            self.fail("libwinusb_shim.dylib is not deployed next to SampleApl.dll (build it with "
                      "`make -C macos-native/backend/shim` and re-publish). Backend said: "
                      + [l for l in lines if "shim missing" in l][-1])
        self.assertEqual(run.returncode, 0, output)
        self.assertIn("SELFTEST OK", lines, output)
        # The DLL is really loaded through the shim ...
        self.assertTrue(any(l.startswith("[selftest] USB2_initialize rc=") for l in lines), output)
        self.assertTrue(any(l.startswith("[selftest] USB2_getModuleConnectionList rc=") for l in lines), output)
        # ... and every library name the resolver maps was resolved and called (ShimProbes.cs),
        # including the shell32 ordinal entry points the DLL never reaches without hardware.
        probes = [l for l in lines if l.startswith("[selftest] resolver probes OK: ")]
        self.assertEqual(len(probes), 1, output)
        for name in ("winusb.dll", "setupapi.dll", "kernel32.dll", "kernel32", "user32.dll", "shell32.dll"):
            self.assertIn(" " + name + " ", probes[0] + " ", output)
        self.assertTrue(any(l.startswith("[selftest] probe shell32.dll #2 ") for l in lines), output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
