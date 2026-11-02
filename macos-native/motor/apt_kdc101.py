"""Thorlabs APT protocol for the KDC101 K-Cube DC servo controller (macOS port).

Replaces the Kinesis C API (`Thorlabs.MotionControl.KCube.DCServo.h`) that the Windows
program `RamanAutofocus/RamanAutofocus/RamanAutofocusTest.cpp` uses.  Three layers:

1. the APT message codec (`Message`, `encode`, `decode`, the `msg_*` constructors and the
   `parse_*` helpers) - "APT Communications Protocol" framing, PORT_SPEC.md section 3;
2. transports behind one interface (`Transport`): `PyFtdiTransport` (pyftdi over libusb, the
   DEFAULT on macOS: the KDC101's FTDI chip carries Thorlabs' custom product id 0403:FAF0, which
   Apple's FTDI driver does not bind, so no /dev/cu.usbserial-* node ever appears, PORT_SPEC.md
   section 3 "Transport on macOS"), `PySerialTransport` (pyserial, for an explicit port path,
   e.g. with FTDI's own VCP driver or /dev/ttyUSB* on Linux) and `SimulatedTransport` (one
   emulated KDC101 per serial number).  `select_transport` implements the selection rule
   (config `transport` = auto | pyftdi | serial | simulate; env `RAMAN_MOTOR_SIMULATE=1`
   overrides) and `open_device` dispatches on it through `TRANSPORT_FACTORIES`;
3. `KDC101`, which offers the Kinesis-like operations the program needs (`home`,
   `move_to_position`, `move_relative`, `get_position`, `set_backlash`, `stop_immediate`,
   message queue of (type, id) events) on top of either transport.

Kinesis message-queue mapping used by the program (RamanAutofocusTest.cpp:91, :135, :209):
message type 2 = generic motor; id 0 = Homed, 1 = MoveCompleted, 2 = MoveStopped.
"""

from __future__ import annotations

import os
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------------------
# APT message identifiers (Thorlabs "APT Communications Protocol", PORT_SPEC.md section 3)
# ----------------------------------------------------------------------------------------
MGMSG_HW_REQ_INFO = 0x0005
MGMSG_HW_GET_INFO = 0x0006
MGMSG_HW_NO_FLASH_PROGRAMMING = 0x0018
MGMSG_MOD_SET_CHANENABLESTATE = 0x0210
MGMSG_MOT_REQ_POSCOUNTER = 0x0411
MGMSG_MOT_GET_POSCOUNTER = 0x0412
MGMSG_MOT_SET_GENMOVEPARAMS = 0x043A
MGMSG_MOT_MOVE_HOME = 0x0443
MGMSG_MOT_MOVE_HOMED = 0x0444
MGMSG_MOT_MOVE_RELATIVE = 0x0448
MGMSG_MOT_MOVE_ABSOLUTE = 0x0453
MGMSG_MOT_MOVE_COMPLETED = 0x0464
MGMSG_MOT_MOVE_STOP = 0x0465
MGMSG_MOT_MOVE_STOPPED = 0x0466
MGMSG_MOT_RESUME_ENDOFMOVEMSGS = 0x046C   # header only; undoes a SUSPEND_ENDOFMOVEMSGS (0x046B)

DEST_GENERIC_USB = 0x50   # the K-Cube itself (generic USB hardware unit)
SOURCE_HOST = 0x01        # host PC
CHANNEL = 1               # single-channel K-Cube

FTDI_VID = 0x0403         # FTDI
KDC101_PID = 0xFAF0       # Thorlabs' custom FTDI product id of the K-Cubes (PORT_SPEC.md: "APT")
KDC101_PID_NAME = "APT"

HEADER_SIZE = 6
HW_INFO_SIZE = 84
STATUS_SIZE = 14

# Kinesis-style (messageType, messageId) events, RamanAutofocusTest.cpp:91/:135 comments.
MSG_TYPE_GENERIC_MOTOR = 2
MSG_ID_HOMED = 0
MSG_ID_MOVE_COMPLETED = 1
MSG_ID_MOVE_STOPPED = 2
EVENT_IDS: Dict[int, int] = {
    MGMSG_MOT_MOVE_HOMED: MSG_ID_HOMED,
    MGMSG_MOT_MOVE_COMPLETED: MSG_ID_MOVE_COMPLETED,
    MGMSG_MOT_MOVE_STOPPED: MSG_ID_MOVE_STOPPED,
}


class MotorError(Exception):
    """Raised when the device does not answer or answers unexpectedly."""


# ----------------------------------------------------------------------------------------
# Codec
# ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Message:
    """One APT message. `dest` is stored WITHOUT the 0x80 data-packet flag."""
    msgid: int
    param1: int = 0
    param2: int = 0
    dest: int = DEST_GENERIC_USB
    source: int = SOURCE_HOST
    data: bytes = b""

    def to_bytes(self) -> bytes:
        return encode(self)


def encode(msg: Message) -> bytes:
    """Header: msgid u16, param1 u8, param2 u8, dest u8, source u8 (little-endian).
    With a data packet bytes 2-3 hold the data length (u16) and dest |= 0x80."""
    if msg.data:
        return struct.pack("<HHBB", msg.msgid, len(msg.data), msg.dest | 0x80, msg.source) + msg.data
    return struct.pack("<HBBBB", msg.msgid, msg.param1 & 0xFF, msg.param2 & 0xFF, msg.dest, msg.source)


def decode(buf: bytes) -> Optional[Tuple[Message, int]]:
    """Decode the first message in `buf`. Returns (message, bytes consumed) or None when
    the buffer does not yet hold a complete message."""
    if len(buf) < HEADER_SIZE:
        return None
    msgid, b2, b3, dest, source = struct.unpack_from("<HBBBB", buf, 0)
    if dest & 0x80:
        length = b2 | (b3 << 8)
        if len(buf) < HEADER_SIZE + length:
            return None
        data = bytes(buf[HEADER_SIZE:HEADER_SIZE + length])
        return Message(msgid, 0, 0, dest & 0x7F, source, data), HEADER_SIZE + length
    return Message(msgid, b2, b3, dest, source, b""), HEADER_SIZE


# Host -> device --------------------------------------------------------------------------
def msg_no_flash_programming() -> Message:
    return Message(MGMSG_HW_NO_FLASH_PROGRAMMING)


def msg_set_chan_enable(enable: bool = True, chan: int = CHANNEL) -> Message:
    return Message(MGMSG_MOD_SET_CHANENABLESTATE, chan, 0x01 if enable else 0x02)


def msg_resume_endofmovemsgs(chan: int = CHANNEL) -> Message:
    """MGMSG_MOT_RESUME_ENDOFMOVEMSGS: make sure MOVE_COMPLETED / MOVE_HOMED / MOVE_STOPPED are
    sent (a previous user may have suspended them with 0x046B; Kinesis sends this on connect)."""
    return Message(MGMSG_MOT_RESUME_ENDOFMOVEMSGS, chan, 0)


def msg_req_info() -> Message:
    return Message(MGMSG_HW_REQ_INFO)


def msg_req_poscounter(chan: int = CHANNEL) -> Message:
    return Message(MGMSG_MOT_REQ_POSCOUNTER, chan, 0)


def msg_move_home(chan: int = CHANNEL) -> Message:
    return Message(MGMSG_MOT_MOVE_HOME, chan, 0)


def msg_move_absolute(position: int, chan: int = CHANNEL) -> Message:
    """Long form: data = chan u16, position i32 (encoder counts)."""
    return Message(MGMSG_MOT_MOVE_ABSOLUTE, data=struct.pack("<Hi", chan, int(position)))


def msg_move_relative(distance: int, chan: int = CHANNEL) -> Message:
    """Long form: data = chan u16, distance i32 (encoder counts)."""
    return Message(MGMSG_MOT_MOVE_RELATIVE, data=struct.pack("<Hi", chan, int(distance)))


def msg_set_genmoveparams(backlash: int, chan: int = CHANNEL) -> Message:
    return Message(MGMSG_MOT_SET_GENMOVEPARAMS, data=struct.pack("<Hi", chan, int(backlash)))


def msg_move_stop(immediate: bool = True, chan: int = CHANNEL) -> Message:
    return Message(MGMSG_MOT_MOVE_STOP, chan, 0x01 if immediate else 0x02)


# Device -> host --------------------------------------------------------------------------
@dataclass(frozen=True)
class HwInfo:
    serial_number: int
    model: str
    hw_type: int
    firmware: Tuple[int, int, int]   # (major, interim, minor)
    notes: str
    hw_version: int
    mod_state: int
    channels: int


def msg_get_info(serial_number: int, model: str = "KDC101", hw_type: int = 16,
                 firmware: Tuple[int, int, int] = (1, 0, 0), notes: str = "APT DC Motor Controller",
                 hw_version: int = 1, mod_state: int = 0, channels: int = 1) -> Message:
    """84-byte GET_INFO data: serial u32, model 8 chars, type u16, firmware u32
    (minor, interim, major, unused), notes 48, empty 12, hw version u16, mod state u16, nchs u16."""
    major, interim, minor = firmware
    fw = bytes([minor & 0xFF, interim & 0xFF, major & 0xFF, 0])
    data = struct.pack("<I8sH4s48s12sHHH", serial_number, model.encode("ascii")[:8], hw_type, fw,
                       notes.encode("ascii")[:48], b"\0" * 12, hw_version, mod_state, channels)
    assert len(data) == HW_INFO_SIZE
    return Message(MGMSG_HW_GET_INFO, dest=SOURCE_HOST, source=DEST_GENERIC_USB, data=data)


def parse_hw_info(data: bytes) -> HwInfo:
    if len(data) < HW_INFO_SIZE:
        raise MotorError("GET_INFO data too short: %d bytes" % len(data))
    serial, model, hw_type, fw, notes, _empty, hw_version, mod_state, channels = \
        struct.unpack_from("<I8sH4s48s12sHHH", data, 0)
    return HwInfo(serial, model.rstrip(b"\0 ").decode("ascii", "replace"), hw_type,
                  (fw[2], fw[1], fw[0]), notes.rstrip(b"\0 ").decode("ascii", "replace"),
                  hw_version, mod_state, channels)


def msg_get_poscounter(position: int, chan: int = CHANNEL) -> Message:
    return Message(MGMSG_MOT_GET_POSCOUNTER, dest=SOURCE_HOST, source=DEST_GENERIC_USB,
                   data=struct.pack("<Hi", chan, int(position)))


def parse_poscounter(data: bytes) -> Tuple[int, int]:
    """Returns (channel, position)."""
    if len(data) < 6:
        raise MotorError("GET_POSCOUNTER data too short: %d bytes" % len(data))
    chan, position = struct.unpack_from("<Hi", data, 0)
    return chan, position


@dataclass(frozen=True)
class MotorStatus:
    channel: int
    position: int
    velocity: int
    status_bits: int


def _status_data(position: int, chan: int, velocity: int = 0, status_bits: int = 0) -> bytes:
    """14-byte status packet: chan u16, position i32, velocity u16, reserved u16, status u32."""
    data = struct.pack("<HiHHI", chan, int(position), velocity & 0xFFFF, 0, status_bits & 0xFFFFFFFF)
    assert len(data) == STATUS_SIZE
    return data


def msg_move_homed(position: int = 0, chan: int = CHANNEL, status_bits: int = 0x0400) -> Message:
    return Message(MGMSG_MOT_MOVE_HOMED, dest=SOURCE_HOST, source=DEST_GENERIC_USB,
                   data=_status_data(position, chan, 0, status_bits))


def msg_move_completed(position: int, chan: int = CHANNEL, status_bits: int = 0x0400) -> Message:
    return Message(MGMSG_MOT_MOVE_COMPLETED, dest=SOURCE_HOST, source=DEST_GENERIC_USB,
                   data=_status_data(position, chan, 0, status_bits))


def msg_move_stopped(position: int, chan: int = CHANNEL, status_bits: int = 0x0400) -> Message:
    return Message(MGMSG_MOT_MOVE_STOPPED, dest=SOURCE_HOST, source=DEST_GENERIC_USB,
                   data=_status_data(position, chan, 0, status_bits))


def parse_status(msg: Message) -> MotorStatus:
    """MOVE_COMPLETED / MOVE_STOPPED (and, on some firmware, MOVE_HOMED) carry a 14-byte
    status packet; a header-only MOVE_HOMED carries the channel in param1."""
    if len(msg.data) >= STATUS_SIZE:
        chan, position, velocity, _res, bits = struct.unpack_from("<HiHHI", msg.data, 0)
        return MotorStatus(chan, position, velocity, bits)
    return MotorStatus(msg.param1, 0, 0, 0)


# ----------------------------------------------------------------------------------------
# Transports
# ----------------------------------------------------------------------------------------
class Transport:
    """Byte stream to one K-Cube. `read(n)` returns at most n bytes and must return (possibly
    empty) within the transport's short timeout so callers can loop without blocking forever;
    `in_waiting` / `read_available()` report what is pending right now without waiting."""
    timeout: float = 0.05

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, n: int) -> bytes:
        raise NotImplementedError

    @property
    def in_waiting(self) -> int:
        return 0

    def read_available(self) -> bytes:
        """Whatever is pending now (possibly a fragment), without waiting for more."""
        waiting = self.in_waiting
        return self.read(waiting) if waiting > 0 else b""

    def close(self) -> None:
        pass


def find_serial_port(serial_number: str, explicit: Optional[str] = None) -> Optional[str]:
    """Locate a serial-port node of the K-Cube for the pyserial transport: an explicit path from
    motor_config.json wins; otherwise `serial.tools.list_ports` matching `serial_number` (prefer
    /dev/cu.* on macOS); finally the FTDI VCP naming convention /dev/cu.usbserial-<serial>.
    On macOS without FTDI's own VCP driver a KDC101 has no such node at all (custom product id
    0403:FAF0); the pyftdi transport is used instead."""
    if explicit:
        return explicit
    candidates: List[str] = []
    try:
        from serial.tools import list_ports
        for port in list_ports.comports():
            sn = (port.serial_number or "").strip()
            if sn and (sn == serial_number or sn.startswith(serial_number)):
                candidates.append(port.device)
    except Exception:
        pass
    candidates.sort(key=lambda d: (not d.startswith("/dev/cu."), d))
    if candidates:
        return candidates[0]
    guess = "/dev/cu.usbserial-%s" % serial_number
    if os.path.exists(guess):
        return guess
    return None


class PySerialTransport(Transport):
    """pyserial port (explicit `serial_ports` path, FTDI VCP driver or Linux /dev/ttyUSB*):
    115200 baud, 8N1, RTS/CTS flow control, RTS asserted.  Reads are bounded by `timeout`,
    writes by write_timeout; the port is never drained (tcdrain has no timeout and would block
    forever while the cube holds CTS low)."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.05):
        import serial  # pyserial 3.5
        self.timeout = timeout
        self.port = port
        self._ser = serial.Serial(port=None, baudrate=baudrate, bytesize=serial.EIGHTBITS,
                                  parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                  rtscts=True, timeout=timeout, write_timeout=1.0)
        self._ser.port = port
        self._ser.rts = True
        self._ser.dtr = True
        self._ser.open()
        try:
            self._ser.rts = True      # with rtscts=True pyserial leaves RTS to the driver at open
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        except Exception:
            pass

    def write(self, data: bytes) -> None:
        try:
            self._ser.write(data)          # returns once handed to the driver (write_timeout=1 s)
        except Exception as error:
            raise MotorError("write to %s failed: %s" % (self.port, error)) from error

    def read(self, n: int) -> bytes:
        try:
            return self._ser.read(n)
        except Exception as error:
            raise MotorError("read from %s failed: %s" % (self.port, error)) from error

    @property
    def in_waiting(self) -> int:
        try:
            return int(self._ser.in_waiting)
        except Exception:
            return 0

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


SerialTransport = PySerialTransport   # former name


class PyFtdiTransport(Transport):
    """pyftdi (libusb) transport: the DEFAULT real transport on macOS (PORT_SPEC.md section 3,
    "Transport on macOS").

    The KDC101's FTDI chip carries Thorlabs' custom product id 0403:FAF0, which Apple's FTDI
    driver does not bind, so no /dev/cu.usbserial-* node ever appears; pyftdi talks to the chip
    through libusb instead.  The custom id is registered once with
    `Ftdi.add_custom_product(0x0403, 0xFAF0, 'APT')`, the port is opened with
    `pyftdi.serialext.serial_for_url('ftdi://0x0403:0xfaf0:<serial>/1', 115200 8N1, rtscts)`,
    RX/TX are purged and RTS is asserted.  Discovery (`list_devices`): `UsbTools.find_all(
    [(0x0403, 0xFAF0)])` matched on the USB serial-number string, which is the Thorlabs serial.

    pyftdi's pyserial-compatible object reports `in_waiting` as 0 (not implemented), so pending
    input is polled with one USB bulk IN (`Ftdi.read_data_bytes(..., attempt=1)`): the chip
    answers within its latency timer, with an empty payload when idle.  pyftdi's dynamic latency
    (12..200 ms) is disabled and the timer fixed at LATENCY_MS, so a poll never waits longer than
    that, and every USB transfer is bounded by USB_TIMEOUT_MS.  Verified only against the
    simulator (no K-Cube is attached to this Mac).
    """
    LATENCY_MS = 12          # fixed FTDI latency timer (ms): an idle poll returns within this
    USB_TIMEOUT_MS = 1000    # bound for every USB bulk transfer (pyftdi default: 5000)
    POLL_SIZE = 4096         # bytes requested per poll (one FTDI read chunk)
    _product_registered = False

    @classmethod
    def register_product(cls) -> None:
        """`Ftdi.add_custom_product(0x0403, 0xFAF0, 'APT')` once (pyftdi rejects duplicates)."""
        if cls._product_registered:
            return
        from pyftdi.ftdi import Ftdi
        if KDC101_PID not in Ftdi.PRODUCT_IDS.get(FTDI_VID, {}).values():
            Ftdi.add_custom_product(FTDI_VID, KDC101_PID, KDC101_PID_NAME)
        cls._product_registered = True

    @staticmethod
    def url_for(serial_number: str) -> str:
        return "ftdi://0x%04x:0x%04x:%s/1" % (FTDI_VID, KDC101_PID, serial_number)

    @classmethod
    def list_devices(cls) -> List[str]:
        """USB serial-number strings of every KDC101 pyftdi can see (sorted)."""
        cls.register_product()
        from pyftdi.usbtools import UsbTools
        found = UsbTools.find_all([(FTDI_VID, KDC101_PID)], nocache=True)
        return sorted((descriptor.sn or "").strip() for descriptor, _interfaces in found)

    def __init__(self, serial_number: str, baudrate: int = 115200, timeout: float = 0.05):
        self.register_product()
        import pyftdi.serialext          # registers the ftdi:// handler with pyserial
        self.timeout = timeout
        self.serial_number = str(serial_number)
        self.url = self.url_for(self.serial_number)
        self._pending = bytearray()
        try:
            self._ser = pyftdi.serialext.serial_for_url(
                self.url, baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
                rtscts=True, timeout=timeout, do_not_open=True)
            # pyftdi's close() reads `udev`, which its open() only sets on success; without
            # this, a missing cube makes pyserial's __del__ print an "Exception ignored" trace.
            self._ser.udev = None
            self._ser.open()
        except Exception as error:
            raise MotorError("cannot open %s: %s" % (self.url, error)) from error
        try:
            ftdi = self._ser.ftdi
            ftdi.timeouts = (self.USB_TIMEOUT_MS, self.USB_TIMEOUT_MS)
            ftdi.set_dynamic_latency(0, 0, 0)        # threshold 0: fixed latency timer
            ftdi.set_latency_timer(self.LATENCY_MS)
            self._ser.reset_input_buffer()           # purge RX
            self._ser.reset_output_buffer()          # purge TX
            self._ser.rts = True                     # set RTS (FT_SetRts in the APT document)
        except Exception as error:
            self.close()
            raise MotorError("cannot configure %s: %s" % (self.url, error)) from error

    def _poll(self) -> None:
        """One USB bulk IN into the pending buffer; empty when the chip has nothing."""
        try:
            data = self._ser.ftdi.read_data_bytes(self.POLL_SIZE, attempt=1)
        except Exception as error:
            raise MotorError("USB read from %s failed: %s" % (self.url, error)) from error
        if data:
            self._pending += data

    def write(self, data: bytes) -> None:
        try:
            self._ser.write(data)                    # bulk OUT, bounded by USB_TIMEOUT_MS
        except Exception as error:
            raise MotorError("USB write to %s failed: %s" % (self.url, error)) from error

    def read(self, n: int) -> bytes:
        if not self._pending:
            try:
                first = self._ser.read(1)            # waits up to `timeout` for the first byte
            except Exception as error:
                raise MotorError("USB read from %s failed: %s" % (self.url, error)) from error
            if first:
                self._pending += first
                self._poll()                         # ... plus whatever followed it
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    @property
    def in_waiting(self) -> int:
        if not self._pending:
            self._poll()
        return len(self._pending)

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


# Default start positions of the simulated axes: the values of the Windows path-test stub
# (laser_test/tests/motor_sdk_stub.h:12). Env RAMAN_MOTOR_SIM_START="serial=pos,..." overrides.
DEFAULT_SIM_POSITIONS: Dict[str, int] = {"27266528": 69400, "27266445": 72870, "27266446": 131860}

# Registry of simulated devices (serial -> most recently created SimulatedTransport) so tests
# running the program in-process can inspect the journal and the final positions.
SIMULATED_DEVICES: Dict[str, "SimulatedTransport"] = {}


def simulated_start_position(serial_number: str) -> int:
    spec = os.environ.get("RAMAN_MOTOR_SIM_START", "")
    for item in spec.split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            if key.strip() == serial_number:
                try:
                    return int(value.strip())
                except ValueError:
                    pass
    return DEFAULT_SIM_POSITIONS.get(serial_number, 0)


class SimulatedTransport(Transport):
    """Emulates one KDC101 behind an APT byte stream.

    Homing takes HOME_SECONDS (~0.3 s), then the position is 0 and MOVE_HOMED is sent.
    Absolute/relative moves update the position after a delay proportional to the distance
    (capped at MOVE_CAP_SECONDS = 0.2 s) and send MOVE_COMPLETED.  REQ_POSCOUNTER and
    REQ_INFO are answered; backlash / enable / no-flash are accepted silently.
    """
    STEPS_PER_SECOND = 80000.0      # ~2.3 mm/s at 34700 counts/mm (Z825B max velocity)
    HOME_SECONDS = 0.3
    MOVE_CAP_SECONDS = 0.2

    def __init__(self, serial_number: str, initial_position: Optional[int] = None,
                 timeout: float = 0.02, model: str = "KDC101"):
        self.timeout = timeout
        self.serial_number = str(serial_number)
        self.model = model
        self.position = simulated_start_position(self.serial_number) if initial_position is None \
            else int(initial_position)
        self.homed = False
        self.enabled = False
        self.backlash: Optional[int] = None
        self.journal: List[Tuple] = []          # ("HOME",), ("MOVE_ABS", pos), ("MOVE_REL", d), ...
        self._rx = bytearray()                   # bytes the host may read
        self._tx = bytearray()                   # bytes written by the host, pending parse
        self._move_from = self.position
        self._move_target: Optional[int] = None
        self._move_start = 0.0
        self._move_done_at: Optional[float] = None
        self._home_done_at: Optional[float] = None
        self._log_path = os.environ.get("RAMAN_MOTOR_SIM_LOG") or None
        SIMULATED_DEVICES[self.serial_number] = self
        self._record("START", self.position)

    # -- journal ----------------------------------------------------------------------
    def _record(self, event: str, *args) -> None:
        self.journal.append((event,) + tuple(args))
        if self._log_path:
            try:
                with open(self._log_path, "a", encoding="ascii") as log:
                    log.write(" ".join([self.serial_number, event] + [str(a) for a in args]) + "\n")
            except OSError:
                pass

    def counts(self, event: str) -> int:
        return sum(1 for entry in self.journal if entry[0] == event)

    def events(self, *names: str) -> List[Tuple]:
        return [entry for entry in self.journal if entry[0] in names]

    # -- transport API ----------------------------------------------------------------
    def write(self, data: bytes) -> None:
        self._tx += data
        while True:
            decoded = decode(bytes(self._tx))
            if decoded is None:
                break
            msg, used = decoded
            del self._tx[:used]
            self._handle(msg)

    def read(self, n: int) -> bytes:
        self._tick()
        if not self._rx:
            wait = self.timeout
            due = [t for t in (self._move_done_at, self._home_done_at) if t is not None]
            if due:
                wait = max(0.0, min(wait, min(due) - time.monotonic()))
            if wait > 0:
                time.sleep(wait)
            self._tick()
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    @property
    def in_waiting(self) -> int:
        self._tick()
        return len(self._rx)

    def close(self) -> None:
        self._record("CLOSE", self.position)

    # -- emulation --------------------------------------------------------------------
    def _send(self, msg: Message) -> None:
        self._rx += encode(msg)

    def current_position(self) -> int:
        """Position while a move is in progress is interpolated linearly."""
        if self._move_target is not None and self._move_done_at is not None:
            now = time.monotonic()
            total = self._move_done_at - self._move_start
            if total <= 0 or now >= self._move_done_at:
                return self._move_target
            frac = (now - self._move_start) / total
            return int(round(self._move_from + (self._move_target - self._move_from) * frac))
        return self.position

    def _tick(self) -> None:
        now = time.monotonic()
        if self._home_done_at is not None and now >= self._home_done_at:
            self._home_done_at = None
            self.position = 0
            self.homed = True
            self._record("HOMED", 0)
            self._send(msg_move_homed(0))
        if self._move_done_at is not None and now >= self._move_done_at:
            target = self._move_target
            self._move_done_at = None
            self._move_target = None
            self.position = int(target)
            self._record("MOVED", self.position)
            self._send(msg_move_completed(self.position))

    def _start_move(self, target: int) -> None:
        self._move_from = self.current_position()
        self._move_target = int(target)
        self._move_start = time.monotonic()
        distance = abs(self._move_target - self._move_from)
        self._move_done_at = self._move_start + min(self.MOVE_CAP_SECONDS, distance / self.STEPS_PER_SECOND)

    def _handle(self, msg: Message) -> None:
        if msg.msgid == MGMSG_HW_NO_FLASH_PROGRAMMING:
            self._record("NO_FLASH")
        elif msg.msgid == MGMSG_MOD_SET_CHANENABLESTATE:
            self.enabled = msg.param2 == 0x01
            self._record("ENABLE", int(self.enabled))
        elif msg.msgid == MGMSG_HW_REQ_INFO:
            self._record("REQ_INFO")
            self._send(msg_get_info(int(self.serial_number), self.model))
        elif msg.msgid == MGMSG_MOT_REQ_POSCOUNTER:
            self._tick()
            self._send(msg_get_poscounter(self.current_position()))
        elif msg.msgid == MGMSG_MOT_SET_GENMOVEPARAMS:
            _chan, self.backlash = struct.unpack_from("<Hi", msg.data, 0)
            self._record("BACKLASH", self.backlash)
        elif msg.msgid == MGMSG_MOT_MOVE_HOME:
            self._record("HOME")
            self._move_target = None
            self._move_done_at = None
            self._home_done_at = time.monotonic() + self.HOME_SECONDS
        elif msg.msgid == MGMSG_MOT_MOVE_ABSOLUTE:
            _chan, target = struct.unpack_from("<Hi", msg.data, 0)
            self._record("MOVE_ABS", target)
            self._start_move(target)
        elif msg.msgid == MGMSG_MOT_MOVE_RELATIVE:
            _chan, distance = struct.unpack_from("<Hi", msg.data, 0)
            self._record("MOVE_REL", distance)
            self._start_move(self.current_position() + distance)
        elif msg.msgid == MGMSG_MOT_RESUME_ENDOFMOVEMSGS:
            self._record("RESUME_ENDOFMOVEMSGS")     # accepted silently; events are always sent
        elif msg.msgid == MGMSG_MOT_MOVE_STOP:
            self.position = self.current_position()
            self._move_target = None
            self._move_done_at = None
            self._home_done_at = None
            self._record("STOP", self.position)
            self._send(msg_move_stopped(self.position))
        else:
            self._record("UNKNOWN", msg.msgid)


# ----------------------------------------------------------------------------------------
# Device
# ----------------------------------------------------------------------------------------
class KDC101:
    """One KDC101 over a Transport, with the Kinesis-like operations used by the program.

    The reader parses by header, reads the data length when `dest & 0x80`, keeps device
    events (Homed / MoveCompleted / MoveStopped) in a Kinesis-style message queue, keeps the
    latest GET_INFO / GET_POSCOUNTER reply for the request/response helpers and drops anything
    else (unsolicited status updates).  Every wait has a deadline; nothing blocks forever.
    """
    RESPONSE_TIMEOUT = 2.0

    def __init__(self, serial_number: str, transport: Transport, verify: bool = True):
        self.serial_number = str(serial_number)
        self.transport = transport
        self._buf = bytearray()
        self._queue: Deque[Tuple[int, int, int]] = deque()
        self._responses: Dict[int, Message] = {}
        self.info: Optional[HwInfo] = None
        self.last_position: Optional[int] = None
        self._polling = False
        self._open_sequence(verify)

    # -- opening ----------------------------------------------------------------------
    def _open_sequence(self, verify: bool) -> None:
        self._send(msg_no_flash_programming())          # once after opening
        self._send(msg_set_chan_enable(True))
        self._send(msg_resume_endofmovemsgs())          # as Kinesis does on connect (see msg_*)
        self._send(msg_req_info())
        reply = self._wait_response(MGMSG_HW_GET_INFO, self.RESPONSE_TIMEOUT)
        self.info = parse_hw_info(reply.data)
        if verify and self.info.serial_number != int(self.serial_number):
            raise MotorError("device on port reports serial %d, expected %s"
                             % (self.info.serial_number, self.serial_number))

    # -- low level --------------------------------------------------------------------
    def _send(self, msg: Message) -> None:
        self.transport.write(encode(msg))

    def _feed(self, data: bytes) -> None:
        self._buf += data
        while True:
            decoded = decode(bytes(self._buf))
            if decoded is None:
                break
            msg, used = decoded
            del self._buf[:used]
            if msg.msgid in EVENT_IDS:
                status = parse_status(msg)
                self._queue.append((MSG_TYPE_GENERIC_MOTOR, EVENT_IDS[msg.msgid], status.status_bits))
            elif msg.msgid in (MGMSG_HW_GET_INFO, MGMSG_MOT_GET_POSCOUNTER):
                self._responses[msg.msgid] = msg
            # anything else: tolerated and dropped

    def _pump(self, blocking: bool) -> None:
        """Feed the parser with what the transport has pending; when `blocking`, wait up to the
        transport's short timeout for the first byte."""
        data = self.transport.read_available()
        if not data and blocking:
            data = self.transport.read(1)
        while data:
            self._feed(data)
            data = self.transport.read_available()

    def _wait_response(self, msgid: int, timeout: float) -> Message:
        deadline = time.monotonic() + timeout
        self._responses.pop(msgid, None)
        while True:
            self._pump(blocking=True)
            if msgid in self._responses:
                return self._responses.pop(msgid)
            if time.monotonic() >= deadline:
                raise MotorError("no reply 0x%04X from device %s within %.1f s" % (msgid, self.serial_number, timeout))

    # -- Kinesis-like API (names follow the CC_* calls in RamanAutofocusTest.cpp) ---------
    def start_polling(self, interval_ms: int) -> bool:
        """Kinesis background polling has no equivalent here: positions are read
        synchronously with REQ_POSCOUNTER. Kept so the program's sequence stays identical."""
        self._polling = True
        return True

    def stop_polling(self) -> None:
        self._polling = False

    def close(self) -> None:
        self.transport.close()

    def clear_message_queue(self) -> None:
        self._pump(blocking=False)
        self._queue.clear()

    def message_queue_size(self) -> int:
        self._pump(blocking=False)
        return len(self._queue)

    def get_next_message(self) -> Optional[Tuple[int, int, int]]:
        """Returns (messageType, messageId, messageData) like CC_GetNextMessage, or None."""
        self._pump(blocking=False)
        if not self._queue:
            return None
        return self._queue.popleft()

    def get_position(self, timeout: float = 1.0) -> int:
        self._send(msg_req_poscounter())
        reply = self._wait_response(MGMSG_MOT_GET_POSCOUNTER, timeout)
        _chan, position = parse_poscounter(reply.data)
        self.last_position = position
        return position

    def home(self) -> int:
        self._send(msg_move_home())
        return 0

    def move_to_position(self, position: int) -> int:
        self._send(msg_move_absolute(int(position)))
        return 0

    def move_relative(self, distance: int) -> int:
        self._send(msg_move_relative(int(distance)))
        return 0

    def stop_immediate(self) -> int:
        self._send(msg_move_stop(immediate=True))
        return 0

    def set_backlash(self, steps: int) -> int:
        self._send(msg_set_genmoveparams(int(steps)))
        return 0


# ----------------------------------------------------------------------------------------
# Transport selection (PORT_SPEC.md section 3, "Transport on macOS")
# ----------------------------------------------------------------------------------------
TRANSPORT_NAMES = ("auto", "pyftdi", "serial", "simulate")


def select_transport(config_transport, explicit_port: Optional[str], env_simulate: Optional[str]) -> str:
    """The selection rule: env RAMAN_MOTOR_SIMULATE=1 overrides to the simulator; otherwise the
    config value `transport` = auto (explicit port -> pyserial, else pyftdi), pyftdi, serial or
    simulate.  Returns 'pyftdi', 'serial' or 'simulate'; an unknown name raises MotorError."""
    if str(env_simulate or "").strip() == "1":
        return "simulate"
    if isinstance(config_transport, bool):            # former open_device(serial, simulate=bool)
        config_transport = "simulate" if config_transport else "auto"
    name = str(config_transport if config_transport is not None else "auto").strip().lower() or "auto"
    if name == "pyserial":
        name = "serial"
    if name == "auto":
        return "serial" if explicit_port else "pyftdi"
    if name in ("pyftdi", "serial", "simulate"):
        return name
    raise MotorError("unknown transport %r in motor_config.json (expected auto, pyftdi, serial or simulate)"
                     % (config_transport,))


def _open_simulated(serial_number: str, explicit_port: Optional[str], baudrate: int) -> Transport:
    return SimulatedTransport(serial_number)


def _open_pyserial(serial_number: str, explicit_port: Optional[str], baudrate: int) -> Transport:
    port = find_serial_port(serial_number, explicit_port)
    if port is None:
        raise MotorError("no serial port found for device %s (transport 'serial': set serial_ports in "
                         "motor_config.json; on macOS a KDC101 is reached with transport 'pyftdi')"
                         % serial_number)
    return PySerialTransport(port, baudrate=baudrate)


def _open_pyftdi(serial_number: str, explicit_port: Optional[str], baudrate: int) -> Transport:
    return PyFtdiTransport(serial_number, baudrate=baudrate)


# kind -> factory(serial_number, explicit_port, baudrate) -> Transport.  Tests inject fakes via
# open_device(..., factories=...) or by patching entries of this table.
TRANSPORT_FACTORIES = {"simulate": _open_simulated, "serial": _open_pyserial, "pyftdi": _open_pyftdi}


def open_device(serial_number: str, transport="auto", explicit_port: Optional[str] = None,
                baudrate: int = 115200, env_simulate: Optional[str] = None, factories=None) -> KDC101:
    """Open the K-Cube with the given serial number over the selected transport.
    `transport` is the motor_config.json value (auto | pyftdi | serial | simulate) and
    `env_simulate` the value of RAMAN_MOTOR_SIMULATE (None: read the environment; "1" forces
    the simulator).  A transport whose open sequence fails is closed again."""
    if env_simulate is None:
        env_simulate = os.environ.get("RAMAN_MOTOR_SIMULATE", "")
    kind = select_transport(transport, explicit_port, env_simulate)
    table = dict(TRANSPORT_FACTORIES)
    if factories:
        table.update(factories)
    link = table[kind](str(serial_number), explicit_port, int(baudrate))
    try:
        return KDC101(serial_number, link)
    except Exception:
        link.close()
        raise
