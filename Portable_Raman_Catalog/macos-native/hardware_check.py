#!/usr/bin/env python3
"""hardware_check.py - first-contact checks for the Mac-native Portable Raman rig.

This tool cannot move a motor and cannot switch the laser on:
  * it contains no motion command (no home, no move, no stop, no backlash change), and
  * it never sends the capture backend's state-changing menu items; in particular 4 (set laser
    power) and 6 (laser enable) are not in its command list (see Backend.READ_ONLY).

Checks (the first argument):
  detect         what macOS sees on USB. Passive: reads the kernel's device registry, opens nothing.
  motors         opens each KDC101 exactly like the motor program does (the open sequence switches
                 the channel on and asks for the hardware information), reads the position counter
                 twice, closes it.
  spectrometer   starts the real capture backend (Hamamatsu's library on the libusb shim), reads the
                 laser-enable state first, then identity, laser power setting, temperatures and
                 calibration, and quits it.
  all            detect, motors, spectrometer.

Options:
  --dark         with spectrometer/all: also take ONE spectrum, and only after the laser has read
                 back as disabled twice. With the laser off this is a dark frame.
  --simulate     use both simulators instead of the instruments (for testing this tool itself).
  --no-trace     do not record the USB calls the Hamamatsu library makes (WRRUSB_SHIM_DEBUG).
  --deploy DIR   the deploy folder (default: the folder of this file if it holds handshake_v2/,
                 else ../deploy-macos).

Run it with the interpreter that has pyserial/pyftdi, e.g.
    /opt/anaconda3/bin/python3 macos-native/hardware_check.py all --dark
or through deploy-macos/hardware_check.sh, which names that interpreter.
Exit code 0 when every requested check passed, 1 otherwise.
"""
import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
IOREG = "/usr/sbin/ioreg"
HAMAMATSU_VID = 0x0661                 # 1633 in the registry
APT_VID, APT_PID = 0x0403, 0xFAF0      # 1027 / 64240: Thorlabs APT controller (KDC101)
PIXELS = 2048


class CheckFailed(Exception):
    """A check could not be completed; the message says why."""


def say(text=""):
    print(text, flush=True)


def default_deploy() -> Path:
    return HERE if (HERE / "handshake_v2").is_dir() else HERE.parent / "deploy-macos"


# ------------------------------------------------------------------------------------------
# detect: passive
# ------------------------------------------------------------------------------------------
def usb_devices():
    """The USB devices in the macOS registry: [{node, idVendor, idProduct, 'USB Product Name',
    'USB Vendor Name', 'USB Serial Number'}]. `ioreg` prints the kernel's cached descriptors; no
    device is opened and nothing is sent to any device."""
    try:
        registry = subprocess.run([IOREG, "-p", "IOUSB", "-l", "-w0"], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise CheckFailed("cannot read the USB registry with %s: %s" % (IOREG, error))
    devices, current = [], None
    for line in registry.decode("utf-8", "replace").splitlines():
        node = re.search(r"\+-o (.*?)\s+<class ", line)
        if node:
            current = {"node": node.group(1)}
            devices.append(current)
            continue
        if current is None:
            continue
        number = re.search(r'"(idVendor|idProduct)" = (\d+)', line)
        if number:
            current[number.group(1)] = int(number.group(2))
            continue
        text = re.search(r'"(USB Product Name|USB Vendor Name|USB Serial Number)" = "(.*)"', line)
        if text:
            current[text.group(1)] = text.group(2)
    return [device for device in devices if "idVendor" in device and "idProduct" in device]


def load_motor_config(deploy: Path) -> dict:
    path = deploy / "handshake_v2" / "motor" / "motor_config.json"
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
        serials = config["serials"]
        for axis in "XYZ":
            str(serials[axis])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CheckFailed("cannot use %s: %r" % (path, error))
    return config


def check_detect(deploy: Path) -> bool:
    say("== USB detection (passive: reads the macOS device registry, opens nothing) ==")
    devices = usb_devices()
    spectrometers = [d for d in devices if d["idVendor"] == HAMAMATSU_VID]
    cubes = [d for d in devices if d["idVendor"] == APT_VID and d["idProduct"] == APT_PID]
    ok = True
    if spectrometers:
        for device in spectrometers:
            say("  spectrometer  found      %04x:%04x  (%s / %s)" % (
                device["idVendor"], device["idProduct"],
                device.get("USB Vendor Name", "?"), device.get("USB Product Name", "?")))
        if len(spectrometers) > 1:
            say("  NOTE: more than one Hamamatsu device is attached; the backend opens the first it lists.")
    else:
        ok = False
        say("  spectrometer  NOT FOUND  (no USB device with Hamamatsu's vendor id 0661)")
    serials = {axis: str(load_motor_config(deploy)["serials"][axis]) for axis in "XYZ"}
    attached = {device.get("USB Serial Number", "") for device in cubes}
    for axis in "XYZ":
        if serials[axis] in attached:
            say("  motor %s       found      KDC101 serial %s" % (axis, serials[axis]))
        else:
            ok = False
            say("  motor %s       NOT FOUND  (no Thorlabs controller 0403:faf0 with serial %s)" % (axis, serials[axis]))
    extra = sorted(attached - set(serials.values()) - {""})
    if extra:
        say("  NOTE: Thorlabs controllers attached but not in motor_config.json: %s" % ", ".join(extra))
    return ok


# ------------------------------------------------------------------------------------------
# motors: identity and position only
# ------------------------------------------------------------------------------------------
class ReadOnlyCube:
    """Everything this tool does with an opened controller: read its hardware information and
    its position counter, and close it. There is deliberately no other method."""

    def __init__(self, device):
        self._device = device

    @property
    def info(self):
        return self._device.info

    def position(self) -> int:
        return self._device.get_position()

    def close(self) -> None:
        self._device.close()


def check_motors(deploy: Path, simulate: bool) -> bool:
    say("== Motor controllers: identity and position (this tool has no motion command) ==")
    motor_dir = deploy / "handshake_v2" / "motor"
    config = load_motor_config(deploy)
    sys.path.insert(0, str(motor_dir))
    try:
        import apt_kdc101 as apt
    except Exception as error:
        raise CheckFailed("cannot import the motor library from %s: %r" % (motor_dir, error))
    env_simulate = "1" if simulate else ""      # decided here, never taken from the environment
    ok = True
    for axis in "XYZ":
        serial = str(config["serials"][axis])
        port = (config.get("serial_ports") or {}).get(axis)
        steps_per_mm = float((config.get("steps_per_mm") or {}).get(axis, 34700))
        cube = None
        try:
            kind = apt.select_transport(config.get("transport"), port, env_simulate)
            if kind == "simulate" and not simulate:
                raise CheckFailed("motor_config.json selects the simulator (transport: simulate); "
                                  "that is not a hardware check")
            cube = ReadOnlyCube(apt.open_device(serial, transport=config.get("transport", "auto"),
                                                explicit_port=port, baudrate=int(config.get("baudrate", 115200)),
                                                env_simulate=env_simulate))
            first = cube.position()
            time.sleep(0.5)
            second = cube.position()
            info = cube.info
            say("  %s  serial %s  model %s  firmware %s  (%s)  via %s" % (
                axis, info.serial_number, info.model, ".".join(str(part) for part in info.firmware),
                info.notes, kind))
            say("     position %d counts = %.4f mm at %g counts/mm" % (second, second / steps_per_mm, steps_per_mm))
            if second != first:
                say("     NOTE: the position changed by %+d counts between two reads 0.5 s apart" % (second - first))
        except Exception as error:
            ok = False
            say("  %s  serial %s  FAILED: %s" % (axis, serial, error))
        finally:
            if cube is not None:
                try:
                    cube.close()
                except Exception:
                    pass
    return ok


# ------------------------------------------------------------------------------------------
# spectrometer: the real backend, read-only menu items, optional dark frame
# ------------------------------------------------------------------------------------------
class Backend:
    """The deployed capture backend, driven through its file protocol like the controller does
    (command.txt -> command_completed.txt, results on stdout or in capture_results/)."""

    # Every menu item this tool may send besides Q (quit) and one "S <id>" capture: the read-only
    # ones. 4 (set laser power) and 6 (laser enable) each read the NEXT command as their parameter;
    # they are not here, so the tool can neither send them nor be mistaken for sending them.
    READ_ONLY = ("1", "2", "3", "5", "7", "8")
    CAPTURE = re.compile(r"^S hwcheck_[A-Za-z0-9_]{1,60}$")
    FAILURE = re.compile(r"Error code|\[Error\]|\[SDK failure\]|\[SDK status error\]|\[Capture failed\]|"
                         r"\[Result publish failed\]")

    def __init__(self, deploy: Path, workdir: Path, simulate: bool, trace: bool):
        launcher = deploy / "handshake_v2" / "backend" / "SampleApl"
        if not launcher.exists():
            raise CheckFailed("%s is missing: run macos-native/build_macos.sh" % launcher)
        self.cwd = workdir
        env = dict(os.environ)
        env.pop("RAMAN_BACKEND_SIMULATE", None)     # decided here, never taken from the environment
        env.pop("WRRUSB_SHIM_DEBUG", None)
        if simulate:
            env["RAMAN_BACKEND_SIMULATE"] = "1"
        if trace:
            env["WRRUSB_SHIM_DEBUG"] = "1"
        self.lines = []
        self._lock = threading.Lock()
        self._eof = threading.Event()
        self._out = open(workdir / "backend_stdout.log", "w", encoding="utf-8")
        self._err = open(workdir / "backend_stderr.log", "wb")
        self.proc = subprocess.Popen([str(launcher)], cwd=str(workdir), env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=self._err)
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                with self._lock:
                    self.lines.append(line)
                self._out.write(line + "\n")
                self._out.flush()
        finally:
            self._eof.set()

    def count(self) -> int:
        with self._lock:
            return len(self.lines)

    def lines_from(self, start: int):
        with self._lock:
            return list(self.lines[start:])

    def wait_line(self, pattern: str, timeout: float, start: int = 0):
        """The first stdout line at index >= start matching `pattern`. A failure line before it,
        the end of the backend, or the timeout raises CheckFailed."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + timeout
        position = start
        while True:
            for line in self.lines_from(position):
                position += 1
                match = regex.search(line)
                if match:
                    return match
                if self.FAILURE.search(line):
                    raise CheckFailed("the backend reported: %s" % line.strip())
            if self._eof.is_set() and position >= self.count():
                raise CheckFailed("the backend ended (exit code %s) before printing %r" % (self.proc.poll(), pattern))
            if time.monotonic() >= deadline:
                raise CheckFailed("the backend did not print %r within %.0f s" % (pattern, timeout))
            time.sleep(0.05)

    def send(self, command: str, timeout: float = 8.0) -> int:
        """Publish one command and wait until the backend has consumed it. Returns the stdout
        line count at the moment of sending, for wait_line(start=...)."""
        if command not in self.READ_ONLY and command != "Q" and not self.CAPTURE.match(command):
            raise AssertionError("refusing to send %r: not a read-only command" % (command,))
        command_file = self.cwd / "command.txt"
        completed = self.cwd / "command_completed.txt"
        if command_file.exists():
            raise CheckFailed("the backend has not consumed the previous command")
        try:
            completed.unlink()
        except FileNotFoundError:
            pass
        start = self.count()
        temporary = self.cwd / "command.txt.tmp"
        temporary.write_bytes((command + "\n").encode("ascii"))
        os.replace(temporary, command_file)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if "done" in completed.read_text(encoding="utf-8", errors="replace"):
                    try:
                        completed.unlink()
                    except FileNotFoundError:
                        pass
                    return start
            except OSError:
                pass
            if self.proc.poll() is not None:
                raise CheckFailed("the backend ended (exit code %s) before consuming %r" % (self.proc.returncode, command))
            time.sleep(0.05)
        raise CheckFailed("the backend did not consume %r within %.0f s" % (command, timeout))

    def shutdown(self):
        """Quit the backend (Q closes the device), or stop it if it no longer answers, so that
        nothing keeps the USB device open. Returns the exit code."""
        if self.proc.poll() is None:
            try:
                self.send("Q", timeout=5.0)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=5)
        self._reader.join(timeout=3)
        for handle in (self._out, self._err):
            try:
                handle.close()
            except OSError:
                pass
        return self.proc.returncode


def read_laser_enable(backend: Backend) -> int:
    start = backend.send("5")
    match = backend.wait_line(r"LD Power Enable\s*:\s*0x([0-9a-fA-F]+)", 15.0, start)
    return int(match.group(1), 16)


def capture_spectrum(backend: Backend, workdir: Path, capture_id: str):
    """One 'S <id>' capture, read back from capture_results/<id>.txt (published atomically)."""
    result = workdir / "capture_results" / (capture_id + ".txt")
    backend.send("S " + capture_id)
    deadline = time.monotonic() + 30.0
    while not result.exists():
        if backend.proc.poll() is not None:
            raise CheckFailed("the backend ended (exit code %s) during the capture" % backend.proc.returncode)
        if time.monotonic() >= deadline:
            raise CheckFailed("no capture result within 30 s (see backend_stdout.log)")
        time.sleep(0.05)
    rows = result.read_text(encoding="utf-8").split("\n")
    header = rows[0].split()
    if len(header) < 4 or header[0] != "CAPTURE" or header[1] != capture_id:
        raise CheckFailed("malformed capture result: %r" % rows[0])
    if header[2] != "OK":
        raise CheckFailed("the capture failed: %r (see backend_stdout.log)" % rows[0])
    try:
        values = [int(token, 16) for token in rows[1].split()]
    except (IndexError, ValueError):
        raise CheckFailed("malformed capture data row")
    if len(values) != PIXELS or len(rows) < 3 or rows[2].strip() != "END " + capture_id:
        raise CheckFailed("incomplete capture result: %d values" % len(values))
    return values


def check_spectrometer(deploy: Path, simulate: bool, trace: bool, dark: bool) -> bool:
    say("== Spectrometer: identity, laser state, temperatures%s (this tool never sends a laser command) =="
        % (", one dark frame" if dark else ""))
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    workdir = deploy / "scan_sessions" / ("_hwcheck_" + stamp)
    try:
        workdir.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise CheckFailed("cannot create %s: %s" % (workdir, error))
    say("  logs: %s" % workdir)
    ok = True
    backend = Backend(deploy, workdir, simulate, trace)
    try:
        backend.wait_line(r"^initialized device$", 40.0)
        say("  backend started and opened the device")

        enable = read_laser_enable(backend)             # first thing: the laser state
        say("  laser enable reads back 0x%08x -> %s" % (enable, "DISABLED" if enable == 0 else "NOT DISABLED"))
        if enable != 0:
            ok = False
            say("  !! The laser does not read back as disabled. This tool will not change that.")
            say("  !! Unplug the spectrometer's USB cable (it is USB powered) before doing anything else.")

        start = backend.send("1")
        backend.wait_line(r"^FirmVer\s*:", 15.0, start)
        info = {}
        for line in backend.lines_from(start):
            field = re.match(r"^(ModelName|SerialNo|UnitID|FirmVer)\s*:\s*(.*)$", line)
            if field:
                info[field.group(1)] = field.group(2).replace("\x00", "").strip()   # the module pads with NULs
        say("  model %s   serial %s   unit %s   firmware %s" % (
            info.get("ModelName", "?"), info.get("SerialNo", "?"), info.get("UnitID", "?"), info.get("FirmVer", "?")))

        start = backend.send("3")
        power = backend.wait_line(r"LD Power\s*:\s*0x([0-9a-fA-F]+)", 15.0, start)
        start = backend.send("7")
        laser_temp = backend.wait_line(r"^LDTemp\s*:\s*(\S+?)\[degC\]", 15.0, start)
        start = backend.send("8")
        board_temp = backend.wait_line(r"^LDBoardTemp\s*:\s*(\S+?)\[degC\]", 15.0, start)
        say("  laser power setting 0x%s   laser diode %s degC   laser board %s degC" % (
            power.group(1), laser_temp.group(1), board_temp.group(1)))

        start = backend.send("2")
        backend.wait_line(r"^\s*B5\s*:", 15.0, start)
        coefficients = [line.split(":", 1)[1].strip() for line in backend.lines_from(start)
                        if re.match(r"^\s*(A0|B[1-5])\s*:", line)]
        say("  calibration coefficients: %s" % "  ".join(coefficients))

        if dark and ok:
            if read_laser_enable(backend) != 0:         # re-read immediately before the capture
                raise CheckFailed("the laser no longer reads back as disabled; no capture taken")
            values = capture_spectrum(backend, workdir, "hwcheck_" + stamp)
            with open(workdir / "dark_spectrum.tsv", "w", encoding="utf-8") as handle:
                handle.write("pixel\tcounts\n")
                handle.writelines("%d\t%d\n" % (index, value) for index, value in enumerate(values))
            zeros, saturated = values.count(0), values.count(65535)
            say("  dark frame: %d pixels  min %d  max %d  mean %.1f  stdev %.1f  zeros %d  saturated %d" % (
                len(values), min(values), max(values), statistics.fmean(values), statistics.pstdev(values),
                zeros, saturated))
            if len(set(values)) == 1:
                ok = False
                say("  !! every pixel has the same value: that is not a real sensor readout")
            elif saturated > PIXELS // 2:
                ok = False
                say("  !! more than half of the pixels are saturated")
            enable = read_laser_enable(backend)
            say("  laser enable after the capture: 0x%08x -> %s" % (enable, "DISABLED" if enable == 0 else "NOT DISABLED"))
            if enable != 0:
                ok = False
        elif dark:
            say("  dark frame SKIPPED because the laser did not read back as disabled")
    except CheckFailed as error:
        ok = False
        say("  FAILED: %s" % error)
        for line in backend.lines_from(max(0, backend.count() - 12)):
            say("     | %s" % line)
    finally:
        code = backend.shutdown()
    if code != 0:
        ok = False
        say("  backend exit code %s (0 expected)" % code)
    else:
        say("  backend closed the device and exited normally")
    return ok


# ------------------------------------------------------------------------------------------
def main(argv) -> int:
    parser = argparse.ArgumentParser(
        description="First-contact checks for the Mac-native Portable Raman rig. "
                    "Cannot move a motor and cannot switch the laser on.")
    parser.add_argument("check", choices=("detect", "motors", "spectrometer", "all"))
    parser.add_argument("--dark", action="store_true", help="also take one spectrum with the laser confirmed off")
    parser.add_argument("--simulate", action="store_true", help="use the simulators (tests this tool, not the instruments)")
    parser.add_argument("--no-trace", action="store_true", help="do not record the Hamamatsu library's USB calls")
    parser.add_argument("--deploy", type=Path, default=None, help="deploy folder (default: auto)")
    args = parser.parse_args(argv[1:])
    deploy = (args.deploy or default_deploy()).resolve()
    if not (deploy / "handshake_v2").is_dir():
        say("Deploy folder %s has no handshake_v2/: run macos-native/build_macos.sh first." % deploy)
        return 1

    say("Portable Raman hardware check - %s" % ("SIMULATED instruments" if args.simulate else "REAL instruments"))
    say("This tool cannot move a motor and cannot switch the laser on.")
    say("deploy folder: %s" % deploy)
    results = {}
    try:
        if args.check in ("detect", "all"):
            results["detect"] = check_detect(deploy)
            if args.simulate and not results["detect"]:
                say("  (simulated run: detection is informational only)")
                results["detect"] = True
        if args.check == "all" and not results["detect"]:
            say("Stopping: plug in and power everything listed as NOT FOUND, then run this again.")
        else:
            if args.check in ("motors", "all"):
                results["motors"] = check_motors(deploy, args.simulate)
            if args.check in ("spectrometer", "all"):
                results["spectrometer"] = check_spectrometer(deploy, args.simulate, not args.no_trace, args.dark)
    except CheckFailed as error:
        say("FAILED: %s" % error)
        results["error"] = False
    say("")
    say("Summary: " + "   ".join("%s %s" % (name, "PASS" if passed else "FAIL") for name, passed in results.items()))
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
