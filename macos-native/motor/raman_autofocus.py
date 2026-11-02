#!/usr/bin/env python3
"""Mac-native port of RamanAutofocus/RamanAutofocus/RamanAutofocusTest.cpp (822 lines).

Usage (started by the Qt controller):  raman_autofocus.py <sessionDir> <sessionId> [1d-x|1d-y]

The Kinesis C API of the Windows build is replaced by the APT serial protocol in
`apt_kdc101.py`; the file handshake with the controller is `scan_handshake.py`.  Names,
constants, log text, exit codes and timings follow the C++ source; direct ports cite
`RamanAutofocusTest.cpp:<line>`.

Differences from the Windows build (documented, PORT_SPEC.md section 3):
- 1D travel limits: Windows asks Kinesis' settings database (CC_RequestSettings /
  CC_GetStageAxisMin/MaxPos); here they come from motor_config.json (`travel_limits_steps`,
  default 0..867500 steps = 25 mm of a Z825B).  The 500 ms settle after the request is kept.
- CC_StartPolling/CC_StopPolling have no equivalent (positions are read synchronously);
  only the INIT_DELAY_MS sleep after "starting polling" remains.
- After an acknowledged point `advanced.txt` = `<pointId>` is published in addition (the
  controller's test mocks do the same; it is harmless and useful for tests).
- Transport: the Kinesis USB driver is replaced by the APT protocol over pyftdi/libusb (the
  default on macOS: the KDC101's FTDI chip carries Thorlabs' product id 0403:FAF0, which
  Apple's FTDI driver does not bind), over pyserial for an explicit `serial_ports` path, or
  over the simulator; motor_config.json `transport` = auto | pyftdi | serial | simulate and
  RAMAN_MOTOR_SIMULATE=1 overrides (apt_kdc101.select_transport).
- stderr carries exactly the C++ failure lines; the underlying reason (exception text) is
  added only with RAMAN_MOTOR_DEBUG=1.

Environment:
  RAMAN_MOTOR_SIMULATE=1        use the KDC101 simulator (no serial port needed)
  RAMAN_MOTOR_DEBUG=1           also print the reason behind device-list / connection failures
  RAMAN_MOTOR_CONFIG=<path>     alternative motor_config.json (default: next to this file)
  RAMAN_MOTOR_ACK_TIMEOUT_MS    test hook: the 30 s Qt contact timeout of waitForAck
  RAMAN_MOTOR_SIM_START/_LOG    simulator start positions / journal file (see apt_kdc101.py)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import apt_kdc101                      # noqa: E402
import scan_handshake as ScanHandshake  # noqa: E402
from apt_kdc101 import KDC101, MotorError  # noqa: E402
from scan_handshake import line_scan_positions, line_scan_within_limits  # noqa: E402

# RamanAutofocusTest.cpp:21-22
sessionDir: str = ""
sessionId: str = ""
pointId: int = 0

# ======================= Z axis parameters =======================  (RamanAutofocusTest.cpp:25-31)
Z_OFFSET_AFTER_HOME = 3.8    # go to this Z (mm) after homing
Z_RANGE = 0.4                # Z scan range (mm)
Z_STEP = 0.1                 # Z step (mm)
MOTOR_SERIAL_NO_Z = 27266446  # Z device serial number
STEPS_PER_MM_Z = 34700       # Z steps per mm

# ======================= X/Y axis parameters =======================  (RamanAutofocusTest.cpp:33-45)
X_OFFSET_AFTER_HOME = 2.0    # X start (mm from home)
X_RANGE = 1.0                # X scan range (mm)
X_STEP = 0.1                 # X step (mm)
MOTOR_SERIAL_NO_X = 27266528  # X device serial number
STEPS_PER_MM_X = 34700       # X steps per mm

Y_OFFSET_AFTER_HOME = 2.0    # Y start (mm from home)
Y_RANGE = 1.0                # Y scan range (mm)
Y_STEP = 0.1                 # Y step (mm)
MOTOR_SERIAL_NO_Y = 27266445  # Y device serial number
STEPS_PER_MM_Y = 34700       # Y steps per mm

# ======================= Common timing parameters =======================  (RamanAutofocusTest.cpp:47-51)
POLLING_INTERVAL_MS = 200    # device polling interval
INIT_DELAY_MS = 100          # wait after starting polling
MOVE_DELAY_MS = 10           # wait after each move + Qt trigger

WAIT_MESSAGE_TIMEOUT_S = 120  # RamanAutofocusTest.cpp:67
LINE_SCAN_STEPS_PER_MM = 34700.0  # RamanAutofocusTest.cpp:339 (`target / 34700.0`)

# Serial number -> steps per mm, filled from the config (defaults above).
_steps_per_mm: Dict[str, float] = {}
_devices: Dict[str, KDC101] = {}


# ======================= stdout / stderr helpers =======================
# The controller shows stdout live: every completed line is flushed (line-buffered behaviour).
_cout_fixed = False  # std::fixed/setprecision(4) is sticky on std::cout once used (:407)


def cout(text: str, end: str = "\n") -> None:
    """std::cout: flushed when a line is completed (std::endl / '\\n').  A partial line such as
    "X -> 2.1000 mm (motor pos: 72870) ... " stays buffered until its "OK" like in the C++
    (:201-210), so the controller receives the two as one chunk, i.e. one log line."""
    out = text + end
    sys.stdout.write(out)
    if out.endswith("\n"):
        sys.stdout.flush()


def cerr(text: str, end: str = "\n") -> None:
    """std::cerr: unit-buffered and tied to std::cout, so pending stdout is flushed first."""
    sys.stdout.flush()
    sys.stderr.write(text + end)
    sys.stderr.flush()


def cdebug(text: str) -> None:
    """Diagnostics beyond the C++ messages, only with RAMAN_MOTOR_DEBUG=1 (stderr)."""
    if os.environ.get("RAMAN_MOTOR_DEBUG", "") == "1":
        cerr(text)


def fmt(value: float) -> str:
    """`std::cout << double`: default general format (6 significant digits) until the Z scan
    loop sets std::fixed / setprecision(4), which then applies to later doubles too."""
    return "%.4f" % value if _cout_fixed else "%.6g" % value


def sleep_ms(ms: float) -> None:
    time.sleep(ms / 1000.0)


def cpp_round(value: float) -> int:
    """std::round: half away from zero."""
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


# ======================= Helpers: unit conversion =======================  (RamanAutofocusTest.cpp:53-57)
def relativeStepsZ(mm: float) -> int:
    return int(mm * STEPS_PER_MM_Z + 0.5)


def relativeStepsX(mm: float) -> int:
    return int(mm * STEPS_PER_MM_X + 0.5)


def relativeStepsY(mm: float) -> int:
    return int(mm * STEPS_PER_MM_Y + 0.5)


# ======================= Configuration =======================
DEFAULT_CONFIG = {
    "transport": "auto",      # auto | pyftdi | serial | simulate (apt_kdc101.select_transport)
    "serials": {"X": str(MOTOR_SERIAL_NO_X), "Y": str(MOTOR_SERIAL_NO_Y), "Z": str(MOTOR_SERIAL_NO_Z)},
    "steps_per_mm": {"X": STEPS_PER_MM_X, "Y": STEPS_PER_MM_Y, "Z": STEPS_PER_MM_Z},
    "travel_limits_steps": {"min": 0, "max": 867500},
    "serial_ports": {"X": None, "Y": None, "Z": None},
    "baudrate": 115200,
}


def config_path() -> str:
    return os.environ.get("RAMAN_MOTOR_CONFIG") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "motor_config.json")


def load_config() -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError) as error:
            cerr("[Warn] Cannot read %s (%s); using built-in defaults." % (path, error))
            loaded = {}
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def travel_limits(config: dict, axis: str) -> Optional[Tuple[int, int]]:
    """Windows: CC_RequestSettings + CC_GetStageAxisMin/MaxPos from the Kinesis settings
    database.  Here: motor_config.json `travel_limits_steps`, either {"min","max"} or
    per axis {"X": {"min","max"}, ...}."""
    limits = config.get("travel_limits_steps") or {}
    entry = limits.get(axis, limits) if isinstance(limits, dict) else None
    if not isinstance(entry, dict) or "min" not in entry or "max" not in entry:
        return None
    try:
        return int(entry["min"]), int(entry["max"])
    except (TypeError, ValueError):
        return None


# ======================= Helpers: wait (specific message) =======================  (RamanAutofocusTest.cpp:59-73)
def waitForMessage(dev: KDC101, expType: int, expId: int) -> bool:
    """Block until a specific (type,id) message is received from the device."""
    started = time.monotonic()
    while not ScanHandshake.cancelled(sessionDir):
        if dev.message_queue_size() > 0:
            msg = dev.get_next_message()
            if msg is not None and msg[0] == expType and msg[1] == expId:
                return True
        if time.monotonic() - started > WAIT_MESSAGE_TIMEOUT_S:
            break
        sleep_ms(20)
    dev.stop_immediate()
    cerr("[Error] Movement cancelled or timed out; scan aborted.")
    return False


# ======================= Robust homing for any axis =======================  (RamanAutofocusTest.cpp:75-124)
def homeAxisAndConfirm(dev: KDC101, name: str, stableWindowSteps: int = 5, maxDriftAccept: int = 1000,
                       settleMs: int = 500, checkIters: int = 10, checkIntervalMs: int = 100) -> Tuple[bool, int]:
    """Do homing, then wait for position to stabilize. If final position is far from 0,
    retry once. Returns (success, final "home" steps)."""
    homePosOut = 0

    def doHome(announce: bool) -> bool:
        nonlocal homePosOut
        if ScanHandshake.cancelled(sessionDir):
            return False
        if announce:
            cout("Homing %s..." % name)
        dev.clear_message_queue()                # drop old messages
        if dev.home() != 0:                      # start homing
            cerr("[Error] CC_Home(%s) invoke failed" % name)
            return False
        if not waitForMessage(dev, 2, 0):        # (2,0) = Homed
            return False
        if announce:
            cout("%s homing message received, stabilizing..." % name)
        sleep_ms(settleMs)                       # small settle time after homed

        # Read position multiple times until change is small.
        pos = dev.get_position()
        last = pos
        for _i in range(checkIters):
            sleep_ms(checkIntervalMs)
            pos = dev.get_position()
            if abs(pos - last) <= stableWindowSteps:
                break
            last = pos
        homePosOut = pos
        cout("%s final position after homing = %d steps" % (name, pos))
        return True

    # First homing try.
    if not doHome(True):
        return False, homePosOut

    # If far from 0, try one more time. If still bad, fail.
    if abs(homePosOut) > maxDriftAccept:
        cerr("[Warn] %s homed but far from zero (%d steps). Retrying homing once..." % (name, homePosOut))
        if not doHome(False):
            return False, homePosOut
        if abs(homePosOut) > maxDriftAccept:
            cerr("[Error] %s still far from zero after retry (%d steps). Please check limit switch / parameters."
                 % (name, homePosOut))
            return False, homePosOut
    return True, homePosOut


# ======================= Helpers: move and wait =======================  (RamanAutofocusTest.cpp:126-136)
def moveMotorToPosition(dev: KDC101, position: int) -> bool:
    """Move to an absolute step position and wait for MoveCompleted."""
    if ScanHandshake.cancelled(sessionDir):
        return False
    dev.clear_message_queue()                    # drop old messages
    if dev.move_to_position(position) != 0:
        cerr("Failed to start movement to position %d" % position)
        return False
    return waitForMessage(dev, 2, 1)             # (2,1) = MoveCompleted


# ======================= Qt trigger handshake =======================  (RamanAutofocusTest.cpp:138-150)
def ack_timeout_ms() -> int:
    try:
        return int(os.environ.get("RAMAN_MOTOR_ACK_TIMEOUT_MS", ScanHandshake.DEFAULT_ACK_TIMEOUT_MS))
    except ValueError:
        return ScanHandshake.DEFAULT_ACK_TIMEOUT_MS


def triggerAndWaitQt() -> bool:
    """A stale reply, failed acquisition, or missing Qt heartbeat never advances a point."""
    global pointId
    pointId += 1
    request = "STEP %s %d %d %d %d\n" % (sessionId, pointId,
                                        _devices[str(MOTOR_SERIAL_NO_X)].get_position(),
                                        _devices[str(MOTOR_SERIAL_NO_Y)].get_position(),
                                        _devices[str(MOTOR_SERIAL_NO_Z)].get_position())
    ok = ScanHandshake.publish(sessionDir + "/request.txt", request) and \
        ScanHandshake.wait_for_ack(sessionDir, sessionId, pointId, ack_timeout_ms())
    cout("[Point %d] %s" % (pointId, "saved, advance" if ok else "FAILED/cancelled, do not advance"))
    if ok:
        ScanHandshake.publish(sessionDir + "/advanced.txt", str(pointId))
    return ok


# ======================= Helpers: scan utilities (inclusive endpoints) =======================
# NOTE: strictly "relative moves of 0.1 mm per step" (RamanAutofocusTest.cpp:177-252).
# start / end are only used to determine the direction (+/-) and how many steps to take.
def _scanAxis(dev: KDC101, name: str, step_mm: float, relativeSteps: Callable[[float], int],
              start: float, end: float) -> bool:
    if ScanHandshake.cancelled(sessionDir):
        return False
    distance = abs(end - start)
    steps = cpp_round(distance / step_mm)
    if steps <= 0:
        return True

    deltaSteps = relativeSteps(step_mm)
    direction = 1.0 if end >= start else -1.0
    if direction < 0:
        deltaSteps = -deltaSteps

    curMm = start
    for _i in range(steps):
        if ScanHandshake.cancelled(sessionDir):
            return False
        curMm += direction * step_mm

        currentPos = dev.get_position()
        targetPos = currentPos + deltaSteps

        cout("%s -> %.4f mm (motor pos: %d) ... " % (name, curMm, targetPos), end="")

        dev.clear_message_queue()
        if dev.move_relative(deltaSteps) != 0:
            cerr("Failed.")
            return False
        if not waitForMessage(dev, 2, 1):        # MoveCompleted
            return False
        cout("OK")

        if not triggerAndWaitQt():
            return False
        sleep_ms(MOVE_DELAY_MS)
    return True


def scanX(dev: KDC101, _homeX: int, start: float, end: float) -> bool:
    """Scan X from start to end with step X_STEP. Each point triggers Qt. (:183)"""
    return _scanAxis(dev, "X", X_STEP, relativeStepsX, start, end)


def scanY(dev: KDC101, _homeY: int, start: float, end: float) -> bool:
    """Scan Y from start to end with step Y_STEP. Each point triggers Qt. (:219)"""
    return _scanAxis(dev, "Y", Y_STEP, relativeStepsY, start, end)


# The 120 handwritten scanY/scanX calls of RamanAutofocusTest.cpp:445-803, transcribed in
# order as (axis, start, end, label printed before the call).
XY_SEQUENCE: List[Tuple[str, float, float, str]] = [
    ("Y", 2.0, 2.1, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.1, 2.2, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.2, 2.3, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.3, 2.4, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.4, 2.5, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.5, 2.6, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.6, 2.7, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.7, 2.8, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.8, 2.9, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.9, 3.0, "Scanning Y: 2.0 -> 3.0"),
    ("X", 2.0, 2.1, "Scanning X: 2.0 -> 2.1"),
    ("Y", 3.0, 2.9, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.9, 2.8, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.8, 2.7, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.7, 2.6, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.6, 2.5, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.5, 2.4, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.4, 2.3, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.3, 2.2, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.2, 2.1, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.1, 2.0, "Scanning Y: 3.0 -> 2.0"),
    ("X", 2.1, 2.2, "Scanning X: 2.1 -> 2.2"),
    ("Y", 2.0, 2.1, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.1, 2.2, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.2, 2.3, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.3, 2.4, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.4, 2.5, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.5, 2.6, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.6, 2.7, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.7, 2.8, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.8, 2.9, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.9, 3.0, "Scanning Y: 2.0 -> 3.0"),
    ("X", 2.2, 2.3, "Scanning X: 2.2 -> 2.3"),
    ("Y", 3.0, 2.9, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.9, 2.8, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.8, 2.7, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.7, 2.6, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.6, 2.5, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.5, 2.4, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.4, 2.3, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.3, 2.2, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.2, 2.1, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.1, 2.0, "Scanning Y: 3.0 -> 2.0"),
    ("X", 2.3, 2.4, "Scanning X: 2.3 -> 2.4"),
    ("Y", 2.0, 2.1, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.1, 2.2, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.2, 2.3, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.3, 2.4, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.4, 2.5, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.5, 2.6, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.6, 2.7, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.7, 2.8, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.8, 2.9, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.9, 3.0, "Scanning Y: 2.0 -> 3.0"),
    ("X", 2.4, 2.5, "Scanning X: 2.4 -> 2.5"),
    ("Y", 3.0, 2.9, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.9, 2.8, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.8, 2.7, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.7, 2.6, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.6, 2.5, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.5, 2.4, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.4, 2.3, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.3, 2.2, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.2, 2.1, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.1, 2.0, "Scanning Y: 3.0 -> 2.0"),
    ("X", 2.5, 2.6, "Scanning X: 2.5 -> 2.6"),
    ("Y", 2.0, 2.1, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.1, 2.2, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.2, 2.3, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.3, 2.4, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.4, 2.5, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.5, 2.6, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.6, 2.7, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.7, 2.8, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.8, 2.9, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.9, 3.0, "Scanning Y: 2.0 -> 3.0"),
    ("X", 2.6, 2.7, "Scanning X: 2.6 -> 2.7"),
    ("Y", 3.0, 2.9, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.9, 2.8, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.8, 2.7, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.7, 2.6, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.6, 2.5, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.5, 2.4, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.4, 2.3, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.3, 2.2, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.2, 2.1, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.1, 2.0, "Scanning Y: 3.0 -> 2.0"),
    ("X", 2.7, 2.8, "Scanning X: 2.7 -> 2.8"),
    ("Y", 2.0, 2.1, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.1, 2.2, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.2, 2.3, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.3, 2.4, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.4, 2.5, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.5, 2.6, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.6, 2.7, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.7, 2.8, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.8, 2.9, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.9, 3.0, "Scanning Y: 2.0 -> 3.0"),
    ("X", 2.8, 2.9, "Scanning X: 2.8 -> 2.9"),
    ("Y", 3.0, 2.9, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.9, 2.8, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.8, 2.7, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.7, 2.6, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.6, 2.5, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.5, 2.4, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.4, 2.3, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.3, 2.2, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.2, 2.1, "Scanning Y: 3.0 -> 2.0"),
    ("Y", 2.1, 2.0, "Scanning Y: 3.0 -> 2.0"),
    ("X", 2.9, 3.0, "Scanning X: 2.9 -> 3.0"),
    ("Y", 2.0, 2.1, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.1, 2.2, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.2, 2.3, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.3, 2.4, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.4, 2.5, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.5, 2.6, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.6, 2.7, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.7, 2.8, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.8, 2.9, "Scanning Y: 2.0 -> 3.0"),
    ("Y", 2.9, 3.0, "Scanning Y: 2.0 -> 3.0"),
]


# ======================= Main sequence =======================  (RamanAutofocusTest.cpp:254-821)
def _scan_body(lineX: bool, lineY: bool, config: dict, devX: KDC101, devY: KDC101, devZ: KDC101) -> int:
    """Everything between the INIT_DELAY sleep and ALL_CLEANUP; returns scanResult."""
    global _cout_fixed
    scanResult = 1

    # Declare before cleanup jumps; 1D must bypass ALL legacy XYZ homing/Z motion. (:311)
    if lineX or lineY:
        selected = devX if lineX else devY
        name = "X" if lineX else "Y"
        axis_steps_per_mm = int(_steps_per_mm.get(selected.serial_number, LINE_SCAN_STEPS_PER_MM))
        points = line_scan_positions(axis_steps_per_mm)
        limits = travel_limits(config, name)          # Windows: CC_RequestSettings (:316)
        if limits is None:
            cerr("[1D] Cannot request travel limits; no movement.")
            return scanResult
        sleep_ms(500)                                  # :320 (settle after the settings request)
        minimum, maximum = limits
        if not line_scan_within_limits(points, minimum, maximum):
            cerr("[1D] 0..24 mm exceeds/has unavailable controller limits: %d..%d steps. No movement."
                 % (minimum, maximum))
            return scanResult
        cout("[1D] %s: home selected axis, then 0..24 mm / 2 mm / 13 spectra. Other axes remain fixed." % name)
        ok, _selectedHome = homeAxisAndConfirm(selected, name)
        if not ok:
            return scanResult
        for target in points:
            if not moveMotorToPosition(selected, target):
                return scanResult
            # Move-complete can represent a clipped move: check the actual endpoint. (:333)
            sleep_ms(POLLING_INTERVAL_MS)
            if abs(selected.get_position() - target) > 100:
                cerr("[1D] Target not reached; abort without acquiring at wrong position.")
                return scanResult
            cout("[1D] %s at %s mm" % (name, fmt(target / float(axis_steps_per_mm))))
            if not triggerAndWaitQt():
                return scanResult
        scanResult = 0
        return scanResult

    # ===== Disable backlash here ===== (:346-349)
    devX.set_backlash(0)   # set X-axis backlash to 0 step
    devY.set_backlash(0)   # set Y-axis backlash to 0 step
    devZ.set_backlash(0)   # set Z-axis backlash to 0 step

    # Home X, then Y, then Z using the robust homing helper. Abort on failure. (:351-364)
    ok, homeX = homeAxisAndConfirm(devX, "X")
    if not ok:
        cerr("[Fatal] X homing failed.")
        return scanResult
    ok, homeY = homeAxisAndConfirm(devY, "Y")
    if not ok:
        cerr("[Fatal] Y homing failed.")
        return scanResult
    ok, homeZ = homeAxisAndConfirm(devZ, "Z")
    if not ok:
        cerr("[Fatal] Z homing failed.")
        return scanResult
    cout("Homed positions: X=%d, Y=%d, Z=%d" % (homeX, homeY, homeZ))

    # ===== After Z homing: move to 20 mm, then back to the configured offset ===== (:366-383)
    highZ = 20.0
    posHighZ = homeZ + relativeStepsZ(highZ)
    cout("Move Z to %s mm (motor pos: %d)..." % (fmt(highZ), posHighZ))
    if not moveMotorToPosition(devZ, posHighZ):
        cerr("Z move to 20 mm failed.")
        return scanResult

    backZ = Z_OFFSET_AFTER_HOME  # configured Z offset
    posBackZ = homeZ + relativeStepsZ(backZ)
    cout("Move Z back to %s mm (motor pos: %d)..." % (fmt(backZ), posBackZ))
    if not moveMotorToPosition(devZ, posBackZ):
        cerr("Z move back to 7.1 mm failed.")
        return scanResult

    # -------- Z scan block -------- (:385-428)
    startPosZ = homeZ + relativeStepsZ(Z_OFFSET_AFTER_HOME)
    cout("Move Z up %smm to startPos: %d" % (fmt(Z_OFFSET_AFTER_HOME), startPosZ))
    if not moveMotorToPosition(devZ, startPosZ):
        cerr("Z move to start failed.")
        return scanResult

    cout("At (home + %smm), start Z scan..." % fmt(Z_OFFSET_AFTER_HOME))

    zList: List[float] = []
    # Move only 0.1 mm each time; the current position is already at the offset, so start
    # scanning from offset + 0.1 mm (same accumulating double loop as :399).
    z = Z_STEP
    while z <= Z_RANGE + 1e-8:
        zList.append(z)
        z += Z_STEP

    cout("\n---- Moving Z motor ----")
    cout("Step size: %s mm, total steps: %d" % (fmt(Z_STEP), len(zList)))

    for zPosRel in zList:                          # relative to start offset
        motorPos = startPosZ + relativeStepsZ(zPosRel)
        _cout_fixed = True                         # std::setw(7) << std::fixed << std::setprecision(4) (:407)
        cout("Moving to Z_offset = %7.4f mm (motor pos: %d) ... " % (Z_OFFSET_AFTER_HOME + zPosRel, motorPos), end="")
        if not moveMotorToPosition(devZ, motorPos):
            cerr("Failed to move, aborting Z scan.")
            return scanResult
        cout("OK")
        if not triggerAndWaitQt():                 # acquire at this unchanged Z
            return scanResult
        sleep_ms(MOVE_DELAY_MS)                    # small margin for acquisition

    # Go back to (offset + range - 0.2) as the final Z position. (:418-427)
    targetBackPos = Z_OFFSET_AFTER_HOME + Z_RANGE - 0.2  # fixed XY working height
    backMotorPos = homeZ + relativeStepsZ(targetBackPos)
    cout("Moving to Z = %s mm (motor pos: %d)... " % (fmt(targetBackPos), backMotorPos), end="")
    if not moveMotorToPosition(devZ, backMotorPos):
        cerr("Failed to move to Z = %s mm!" % ("%.6g" % targetBackPos))
        return scanResult
    cout("OK")

    # -------- X/Y scan block -------- (:430-806)
    # Move X/Y to initial 2.0 mm (no trigger), then run the interleaved scans.
    startX = homeX + relativeStepsX(X_OFFSET_AFTER_HOME)
    startY = homeY + relativeStepsY(Y_OFFSET_AFTER_HOME)
    cout("Move X to start (+%s mm) pos=%d ... " % (fmt(X_OFFSET_AFTER_HOME), startX), end="")
    if not moveMotorToPosition(devX, startX):
        cerr("Failed.")
        return scanResult
    cout("OK")
    cout("Move Y to start (+%s mm) pos=%d ... " % (fmt(Y_OFFSET_AFTER_HOME), startY), end="")
    if not moveMotorToPosition(devY, startY):
        cerr("Failed.")
        return scanResult
    cout("OK")

    cout("\n---- Sequence begin (X/Y) ----")

    # Keep the original interleaved order. Each step triggers Qt and waits. (:445-803)
    for axis, start, end, label in XY_SEQUENCE:
        cout("\n" + label)
        if axis == "Y":
            if not scanY(devY, homeY, start, end):
                return scanResult
        else:
            if not scanX(devX, homeX, start, end):
                return scanResult

    cout("\n---- Sequence end (X/Y) ----")
    scanResult = 0
    return scanResult


def build_device_list(kinds) -> int:
    """TLI_BuildDeviceList (:275): enumerate the K-Cubes reachable through the selected
    transports - pyftdi: UsbTools.find_all on 0403:FAF0 (PyFtdiTransport.list_devices);
    serial: pyserial's list_ports; simulate: nothing.  As with Kinesis only an enumeration
    failure (e.g. no libusb backend) is an error; a missing cube fails when it is opened."""
    try:
        if "pyftdi" in kinds:
            cubes = apt_kdc101.PyFtdiTransport.list_devices()
            cdebug("[Debug] pyftdi sees KDC101 serial numbers: %s" % (", ".join(cubes) or "none"))
        if "serial" in kinds:
            from serial.tools import list_ports
            nodes = [port.device for port in list_ports.comports()]
            cdebug("[Debug] serial ports: %s" % (", ".join(nodes) or "none"))
        return 0
    except Exception as error:
        cdebug("[Error] %s" % error)
        return 1


def main(argv: List[str]) -> int:
    global sessionDir, sessionId, pointId, _cout_fixed
    sessionDir, sessionId, pointId = "", "", 0
    _cout_fixed = False
    _devices.clear()
    _steps_per_mm.clear()

    argc = len(argv)
    if argc != 3 and argc != 4:                                           # :257
        cerr("Start this version from the updated Qt controller.")
        return 2
    lineX = argc == 4 and argv[3] == "1d-x"
    lineY = argc == 4 and argv[3] == "1d-y"
    if argc == 4 and not lineX and not lineY:                             # :260
        cerr("Invalid scan mode.")
        return 2
    # This installation uses an ASCII workspace path; reject lossy conversion. (:261-267)
    for a in (1, 2):
        for ch in argv[a]:
            if ord(ch) > 127:
                cerr("Handshake path must be ASCII.")
                return 2
    sessionDir, sessionId = argv[1], argv[2]
    if not os.path.exists(sessionDir):                                    # :268
        return 2
    scanResult = 1
    cout("Current working directory: %s" % os.getcwd())                   # :270-271

    env_simulate = os.environ.get("RAMAN_MOTOR_SIMULATE", "")
    config = load_config()
    serials = {axis: str(config["serials"].get(axis, DEFAULT_CONFIG["serials"][axis])) for axis in "XYZ"}
    for axis in "XYZ":
        _steps_per_mm[serials[axis]] = float(config["steps_per_mm"].get(axis, DEFAULT_CONFIG["steps_per_mm"][axis]))
    ports = {axis: (config.get("serial_ports") or {}).get(axis) for axis in "XYZ"}

    # Build device list (USB/driver discovery). Exit on failure. (:273-278)
    # Transport per axis (PORT_SPEC.md section 3): config `transport` auto | pyftdi | serial |
    # simulate, RAMAN_MOTOR_SIMULATE=1 overrides; under `auto` an explicit port means pyserial.
    cout("Building device list...")
    try:
        transports = {axis: apt_kdc101.select_transport(config.get("transport"), ports[axis], env_simulate)
                      for axis in "XYZ"}
    except MotorError as error:                       # an unknown `transport` value in the config
        cerr("[Error] %s" % error)
        transports = {}
    if not transports or build_device_list(set(transports.values())) != 0:
        cerr("Failed to build device list. Please check connections.")
        return 1

    # Open X, Y, Z devices in order. If any fails, close what was opened and exit. (:280-302)
    serialX, serialY, serialZ = serials["X"], serials["Y"], serials["Z"]
    opened: List[KDC101] = []

    def open_axis(axis: str, serial: str) -> Optional[KDC101]:
        cout("Opening %s device %s ..." % (axis, serial))
        try:
            dev = apt_kdc101.open_device(serial, transport=transports[axis], explicit_port=ports[axis],
                                         baudrate=int(config.get("baudrate", 115200)), env_simulate=env_simulate)
        except Exception as error:
            cerr("Failed to connect to %s device %s" % (axis, serial))     # :287 / :292 / :298
            cdebug("[Error] %s" % error)
            for previous in opened:
                previous.close()
            return None
        opened.append(dev)
        return dev

    devX = open_axis("X", serialX)
    if devX is None:
        return 1
    devY = open_axis("Y", serialY)
    if devY is None:
        return 1
    devZ = open_axis("Z", serialZ)
    if devZ is None:
        return 1
    _devices.update({serialX: devX, serialY: devY, serialZ: devZ})
    # triggerAndWaitQt reads the three literal serials of the C++ (:144-145)
    _devices.setdefault(str(MOTOR_SERIAL_NO_X), devX)
    _devices.setdefault(str(MOTOR_SERIAL_NO_Y), devY)
    _devices.setdefault(str(MOTOR_SERIAL_NO_Z), devZ)

    # Start polling on all three, then wait a short time for state to settle. (:304-308)
    devX.start_polling(POLLING_INTERVAL_MS)
    devY.start_polling(POLLING_INTERVAL_MS)
    devZ.start_polling(POLLING_INTERVAL_MS)
    sleep_ms(INIT_DELAY_MS)

    try:
        scanResult = _scan_body(lineX, lineY, config, devX, devY, devZ)
    except MotorError as error:
        cerr("[Error] %s; scan aborted." % error)
        scanResult = 1
    except Exception as error:  # never leave the devices open
        cerr("[Error] Unexpected failure: %r; scan aborted." % (error,))
        scanResult = 1
    finally:
        # ALL_CLEANUP: stop polling and close all devices. Always do this before exit. (:809-819)
        for dev in (devX, devY, devZ):
            dev.stop_polling()
        for dev in (devX, devY, devZ):
            try:
                dev.close()
            except Exception:
                pass
        cout("All device connections closed.")
        cout("Scan complete." if scanResult == 0 else "Scan aborted; no further point acquired.")
    return scanResult


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv))
