"""The local web server behind the Simulation Studio (PROTOCOL.md section 7).

`python -m raman_catalog --home HOME studio` serves the Studio page and a small JSON API on
127.0.0.1. Served this way the page shows what the real code under HOME is doing: the scan
folders the controller writes, the simulated stage, and the data pipeline.

What the server can do is fixed here, not by the requests it gets:

- It reads the files of PROTOCOL.md sections 2 and 4. No request names a file; the one name a
  request may give, a session folder, has to be a folder found under HOME/scan_sessions.
- It runs data-layer commands in this process, through the front end the command line uses.
- It starts one program, HOME/raman_sim_runner: the controller without a window, which always
  simulates both instruments. A request picks the scan mode and the laser power level from the
  fixed lists and supplies three texts; it cannot name a program or add an argument.
- Uploads to ARC (the arc-* commands): it lists them, and it starts and acts on demo uploads only.
  The upload program of a demo upload copies the session into a folder of HOME that stands in for
  ARC; nothing leaves this Mac. A request names a session folder, Globus or SSH and a teaching
  fault of that transport from fixed lists, or an upload id. It cannot ask for a live upload, and
  the commands refuse to act on one for the Studio.

Standard library only, like the rest of the local-demo path.
"""

from __future__ import annotations

import collections
import contextlib
import datetime
import errno
import http.server
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from . import cli
from .catalog import open_catalog
from .config import MODE_LOCAL_DEMO, CommandError, DataLayerError, Paths, load_settings, utc_now
from .metadata import (
    LASER_POWER_LEVELS,
    SCAN_MODES,
    STEPS_PER_MM,
    is_session_folder,
    point_files,
    scan_mode_from_folder,
)

log = logging.getLogger(__name__)

BIND_ADDRESS = "127.0.0.1"
DEFAULT_PORT = 8765
# How many ports are tried, counting up from the first one, before giving up.
PORT_ATTEMPTS = 20
MAX_BODY_BYTES = 64 * 1024
# A connection that stays silent this long is closed, so that a stalled client frees its thread.
CONNECTION_TIMEOUT_SECONDS = 30
PAGE_NAME = "index.html"
# The studio folder of the source tree, two levels above this package. In a deployed HOME that
# is HOME/studio itself.
SOURCE_STUDIO = Path(__file__).resolve().parents[2] / "studio"

# A file larger than its limit is not what its name says; it is treated as unreadable.
SMALL_FILE_BYTES = 64 * 1024  # request.txt, response.txt, the stage file
JSON_FILE_BYTES = 1 << 20  # session.json, sample.json
LARGE_FILE_BYTES = 32 << 20  # a point file, the reference spectra, the page

SPECTRUM_BLOCK = 8
RECENT_SESSIONS = 12
# Point files of this many session folders stay parsed: the one on show and those looked at before it.
PARSED_SESSIONS = 3
# A scan counts as active while its handshake files changed within this time. During a scan the
# controller republishes its ACK every 50 ms while it waits for the motor program or is paused,
# and a capture takes about a second, so a scan in progress stays well inside it.
ACTIVE_SECONDS = 5.0
# The motor simulator rewrites the stage file about ten times a second during a move. A file
# that still says "moving" long after its last update was left behind by a simulator that died.
MOVING_SECONDS = 2.0
ACK_STATES = ("OK", "FAIL", "PAUSED", "HOLD")

STOP_GRACE_SECONDS = 5.0
KILLED_WAIT_SECONDS = 5.0
LOG_LINES = 200
LOG_LINE_BYTES = 2000
# How long the last lines of a run that ended are waited for before its end is written to the log.
OUTPUT_DRAIN_SECONDS = 1.0

TEXT_LIMIT = 200
COMMENT_LIMIT = 2000
# Embeddings, overrides and isolates (U+202A to U+202E, U+2066 to U+2069).
BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
LIST_LIMIT = 500
MAX_LIST_LIMIT = 5000
QUERY_FIELDS = 16
REVIEW_DECISIONS = ("confirm", "relabel", "reject")
# The "error" of the answer while a part of the edition that has the command is not installed.
NOT_AVAILABLE = "not available"
# Uploads to ARC: how many uploads GET /api/arc lists, and what a request may pick. A teaching fault
# belongs to one transport (design section 12); the demo of the other one would ignore it.
ARC_LIST_LIMIT = 12
ARC_TRANSPORTS = ("globus", "ssh")
ARC_DEMO_FAULTS = ("lose_submit_reply", "fail_task_once", "drop_connection")
ARC_FAULTS_OF = {"globus": ("lose_submit_reply", "fail_task_once"), "ssh": ("drop_connection",)}
# The ARC settings GET /api/arc passes on, from arc-list's: the page names the ARC folder. The VT
# username and the collection IDs stay out; the page has no use for them.
ARC_SETTINGS_SHOWN = ("demo", "transport", "folder", "include_simulated", "settle_seconds")

_WHOLE_NUMBER = re.compile(r"-?\d{1,15}", re.ASCII)
_CONTENT_LENGTH = re.compile(r"\d{1,12}", re.ASCII)
_UPLOAD_ID = re.compile(r"u[0-9a-f]{12}")
# HTTP status of a failed command, by the "kind" of its failure object; any other kind is 400.
_STATUS_OF_KIND = {"not_found": 404, "conflict": 409, "offline": 503, "auth_required": 503, "internal": 500,
                   "not_ready": 409, "refused": 403, "not_installed": 501}


class _Refusal(Exception):
    """A request that is not carried out; it is answered with the failure object and an HTTP status."""

    def __init__(self, status, error, detail="", headers=()):
        super().__init__(error)
        self.status = status
        self.detail = detail
        self.headers = tuple(headers)

    def answer(self):
        return {"ok": False, "error": str(self), "detail": self.detail}


# ---------------------------------------------------------------------- reading files


def _identity(status):
    """What tells one version of a file from the next."""
    return (status.st_ino, status.st_mtime_ns, status.st_size)


def _read(path, limit):
    """(content, os.stat_result) of a file of at most `limit` bytes, or (None, None).

    The scan files are replaced while a scan runs. A file that is missing, unreadable or
    implausibly large at this moment counts as not there; the next request looks again.
    """
    try:
        with open(path, "rb") as stream:
            status = os.fstat(stream.fileno())
            data = stream.read(limit + 1)
    except OSError:
        return None, None
    if len(data) > limit:
        return None, None
    return data, status


def _cached(cache, key, path, limit, parse):
    """`parse(content)` of a file, taken from `cache` for as long as the file is the same one.

    None when the file cannot be read now, and when `parse` made nothing of it.
    """
    try:
        identity = _identity(os.stat(path))
    except OSError:
        cache.pop(key, None)
        return None
    kept = cache.get(key)
    if kept is not None and kept[0] == identity:
        return kept[1]
    data, status = _read(path, limit)
    if data is None:
        return None
    value = parse(data)
    cache[key] = (_identity(status), value)
    return value


def _not_json(name):
    raise ValueError("%s is not JSON" % name)


def _finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("%s is too large for a number" % text)
    return value


def _json_object(data):
    """The JSON object in `data`, or None for anything else (cut short, not an object).

    NaN and infinity are refused although Python reads them, and so is a number too large to
    be one: an answer that carried them on could not be parsed by the browser.
    """
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_not_json, parse_float=_finite)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _load(path, limit):
    data, _status = _read(path, limit)
    return _json_object(data) if data is not None else None


def _whole(value):
    return type(value) is int


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _text_or_none(value):
    return value if isinstance(value, str) else None


def _millimetres(steps, per_mm=STEPS_PER_MM):
    try:
        return steps / per_mm
    except OverflowError:
        return None


def _epoch(text):
    """Seconds since 1970 for a time as the controller writes it (ISO 8601, UTC), or None."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.timezone.utc)
        return moment.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def reduce_spectrum(values):
    """The largest of every 8 neighbouring pixels: 2048 pixels become 256 values.

    The maximum keeps a narrow Raman peak as high in the small curve as it is in the spectrum;
    a mean would flatten it. None when `values` is not a list of numbers.
    """
    if not isinstance(values, list) or not values or not all(_number(value) for value in values):
        return None
    return [max(values[start:start + SPECTRUM_BLOCK]) for start in range(0, len(values), SPECTRUM_BLOCK)]


def _reduced_reference(reference):
    """reference_spectra.json with every curve reduced like a spectrum, or None when it is unusable."""
    if reference is None or not isinstance(reference.get("signals"), dict):
        return None
    dark = reduce_spectrum(reference.get("dark"))
    signals = {name: reduce_spectrum(curve) for name, curve in reference["signals"].items()}
    if dark is None or None in signals.values():
        return None
    reduced = {key: value for key, value in reference.items() if key not in ("dark", "signals")}
    reduced.update(
        dark=dark,
        signals=signals,
        reduced_to=len(dark),
        reduction="block maximum of %d neighbouring pixels" % SPECTRUM_BLOCK,
    )
    return reduced


# ---------------------------------------------------------------------- the scan folders


def _point(data):
    """What the page draws of one point file, or None when the file is not a saved point."""
    point = _json_object(data)
    if point is None or not _whole(point.get("point_id")) or not isinstance(point.get("capture_id"), str):
        return None
    steps = [point.get("controller_%s_steps" % axis) for axis in "xyz"]
    if not all(_whole(value) for value in steps):
        # A single capture runs without the motor program, so nobody knows its position.
        steps = [None, None, None]
    x_mm, y_mm, z_mm = (None if value is None else _millimetres(value) for value in steps)
    return {
        "point_id": point["point_id"],
        "x_mm": x_mm,
        "y_mm": y_mm,
        "z_mm": z_mm,
        "completed_utc": _text_or_none(point.get("completed_utc")),
        "capture_id": point["capture_id"],
        "spectrum": reduce_spectrum(point.get("intensity")),
    }


def _handshake_line(path, tag, session_id, fields):
    """The fields after `<tag> <session id>` on the first line of a handshake file, or None.

    A line written for another session is not this session's.
    """
    data, _status = _read(path, SMALL_FILE_BYTES)
    if data is None:
        return None
    tokens = data.decode("utf-8", "replace").split("\n", 1)[0].split()
    if len(tokens) < 2 + fields or tokens[0] != tag or (session_id is not None and tokens[1] != session_id):
        return None
    return tokens[2:2 + fields]


def _last_step(folder, session_id):
    """The last request of the motor program: `STEP <session> <id> <x> <y> <z>`, positions in motor steps."""
    tokens = _handshake_line(folder / "request.txt", "STEP", session_id, 4)
    if tokens is None or not all(_WHOLE_NUMBER.fullmatch(token) for token in tokens):
        return None
    point_id, x_steps, y_steps, z_steps = (int(token) for token in tokens)
    return {"point_id": point_id, "x_mm": _millimetres(x_steps), "y_mm": _millimetres(y_steps),
            "z_mm": _millimetres(z_steps)}


def _ack_of(folder, session_id, request):
    """The controller's answer to `request`: `ACK <session> <id> <status> <ms>`.

    None until the controller has answered that very point: an ACK still on disk from the
    point before says nothing about the request on show.
    """
    tokens = _handshake_line(folder / "response.txt", "ACK", session_id, 2)
    if request is None or tokens is None or tokens[1] not in ACK_STATES:
        return None
    return tokens[1] if tokens[0] == str(request["point_id"]) else None


def _changed_lately(path, now):
    try:
        age = now - os.stat(path).st_mtime
    except OSError:
        return False
    # A time stamp slightly ahead of the clock is rounding; one far ahead is a wrong clock.
    return -1.0 <= age <= ACTIVE_SECONDS


def _scan_in_progress(folder, now):
    """True when the motor program and the controller exchanged a STEP or an ACK moments ago.

    A scan that was told to stop (cancel.txt) takes no further point.
    """
    if (folder / "cancel.txt").exists():
        return False
    return _changed_lately(folder / "request.txt", now) or _changed_lately(folder / "response.txt", now)


@dataclass
class _Session:
    name: str
    path: Path
    # session.json (PROTOCOL.md 2.2); None for a folder that has none that can be read.
    described: dict | None
    # What "newest" is decided by: when the session started, else when its folder last changed.
    order: float

    def summary(self):
        described = self.described or {}
        # The page shows ground truth only for spectra that both simulators made. A list item cannot tell
        # it: its "simulated" is true when either did. None when session.json does not say both.
        flags = described.get("simulated")
        known = isinstance(flags, dict) and all(isinstance(flags.get(key), bool) for key in ("backend", "motor"))
        return {
            "folder": self.name,
            "session_id": _text_or_none(described.get("session_id")),
            "scan_mode": _text_or_none(described.get("scan_mode")) or scan_mode_from_folder(self.name),
            "started_utc": _text_or_none(described.get("started_utc")),
            "sample_id": _text_or_none(described.get("sample_id")),
            "simulated": {"backend": flags["backend"], "motor": flags["motor"]} if known else None,
        }


class _ScanFiles:
    """Reads HOME/scan_sessions and the simulated stage file for GET /api/scan.

    It only reads. The page asks several times a second while a scan runs, so what was parsed
    is kept, and a file is read again only when it changed.
    """

    def __init__(self, paths):
        self._paths = paths
        self._lock = threading.Lock()
        self._described = {}
        self._points = collections.OrderedDict()

    def snapshot(self, selected=None):
        """The part of the /api/scan answer that comes from files.

        `selected` names the session folder to show; None shows the newest.
        """
        now = time.time()
        with self._lock:
            sessions = self._sessions()
            shown = sessions[0] if sessions else None
            if selected is not None:
                shown = next((session for session in sessions if session.name == selected), None)
                if shown is None:
                    raise _Refusal(404, "There is no session folder named %s" % selected)
            answer = {
                "folder": None,
                "session": None,
                # Only the newest session can be the one a controller is working on.
                "active": bool(sessions) and _scan_in_progress(sessions[0].path, now),
                "stage": self._stage(now),
                "request": None,
                "ack": None,
                "points": [],
                "sessions": [session.summary() for session in sessions[:RECENT_SESSIONS]],
            }
            if shown is not None:
                session_id = _text_or_none((shown.described or {}).get("session_id"))
                request = _last_step(shown.path, session_id)
                answer.update(
                    folder=shown.name,
                    session=shown.described,
                    request=request,
                    ack=_ack_of(shown.path, session_id, request),
                    points=self._points_of(shown),
                )
            return answer

    def _sessions(self):
        """The session folders, newest first."""
        try:
            folders = [path for path in self._paths.scan_sessions.iterdir() if is_session_folder(path)]
        except OSError:
            folders = []
        sessions = []
        for folder in folders:
            try:
                changed = folder.stat().st_mtime
            except OSError:
                continue
            described = _cached(self._described, folder.name, folder / "session.json", JSON_FILE_BYTES, _json_object)
            started = _epoch(described.get("started_utc")) if described else None
            sessions.append(_Session(folder.name, folder, described, changed if started is None else started))
        names = {session.name for session in sessions}
        for name in [name for name in self._described if name not in names]:
            del self._described[name]
        sessions.sort(key=lambda session: (session.order, session.name), reverse=True)
        return sessions

    def _points_of(self, session):
        """The saved points of a session, in point order."""
        known = self._points.pop(session.name, {})
        try:
            files = point_files(session.path)
        except OSError:
            files = []
        points = []
        for path in files:
            point = _cached(known, path.name, path, LARGE_FILE_BYTES, _point)
            if point is not None:
                points.append(point)
        listed = {path.name for path in files}
        self._points[session.name] = {name: kept for name, kept in known.items() if name in listed}
        while len(self._points) > PARSED_SESSIONS:
            self._points.popitem(last=False)
        return points

    def _stage(self, now):
        """Where the simulated stage is (PROTOCOL.md section 4), or None without a usable stage file."""
        data, status = _read(self._paths.sim_stage_file, SMALL_FILE_BYTES)
        stage = _json_object(data) if data is not None else None
        if stage is None:
            return None
        per_mm = stage.get("steps_per_mm")
        if not _number(per_mm) or per_mm <= 0:
            per_mm = STEPS_PER_MM
        steps = [stage.get(axis + "_steps") for axis in "xyz"]
        if not all(_number(value) for value in steps):
            return None
        x_mm, y_mm, z_mm = (_millimetres(value, per_mm) for value in steps)
        return {
            "x_mm": x_mm,
            "y_mm": y_mm,
            "z_mm": z_mm,
            "moving": stage.get("moving") is True and now - status.st_mtime <= MOVING_SECONDS,
            "updated_utc": _text_or_none(stage.get("updated_utc")),
            "simulated": True,
        }


# ---------------------------------------------------------------------- the simulated run


def _ending(code):
    """How a process ended, in words. A negative code is the number of the signal that ended it."""
    if code >= 0:
        return "exit code %d" % code
    try:
        return "ended by %s" % signal.Signals(-code).name
    except ValueError:
        return "ended by signal %d" % -code


class _Run:
    """One start of HOME/raman_sim_runner: the process, what it was asked for, what it printed."""

    def __init__(self, process, asked):
        self.process = process
        self.asked = asked
        self.lines = collections.deque(maxlen=LOG_LINES)
        self.finished_utc = None
        self.stop_requested = False


class _SimulatedRuns:
    """Starts and stops HOME/raman_sim_runner, one run at a time, and keeps the end of its output."""

    def __init__(self, paths, grace_seconds):
        self._paths = paths
        self._grace_seconds = grace_seconds
        self._lock = threading.Lock()
        self._stopping = threading.Lock()
        self._run = None

    def available(self):
        runner = self._paths.sim_runner
        return runner.is_file() and os.access(runner, os.X_OK)

    def alive(self):
        with self._lock:
            return self._run is not None and self._run.process.poll() is None

    def start(self, mode, power, sample_id, operator, location):
        runner = self._paths.sim_runner
        with self._lock:
            if self._run is not None and self._run.process.poll() is None:
                raise _Refusal(
                    409, "A simulated run is already in progress",
                    "Process %d, started %s. Stop it first, or wait for it to end."
                    % (self._run.process.pid, self._run.asked["started_utc"]))
            if not self.available():
                raise _Refusal(
                    501, "The simulated runner is not installed",
                    "%s is missing or cannot be executed. It comes with a built edition." % runner)
            environment = dict(os.environ)
            # The runner forces both itself; forced here as well, so that nothing started from
            # the Studio can reach an instrument whatever environment this server was started in.
            environment["RAMAN_BACKEND_SIMULATE"] = "1"
            environment["RAMAN_MOTOR_SIMULATE"] = "1"
            arguments = [str(runner), "--mode", mode, "--sample-id", sample_id, "--operator", operator,
                         "--location", location, "--power", power]
            try:
                # Its own session: Ctrl-C in the terminal of this server then reaches the run only
                # as the SIGTERM of close(), the same way /api/stop ends it.
                process = subprocess.Popen(
                    arguments, cwd=str(self._paths.home), env=environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                raise _Refusal(500, "The simulated runner could not be started", str(error)) from error
            run = _Run(process, {"simulated": True, "mode": mode, "power": power, "sample_id": sample_id,
                                 "operator": operator, "location": location, "started_utc": utc_now()})
            run.lines.append("[studio] simulated run started (process %d): %s, laser power %s"
                             % (process.pid, mode, power))
            self._run = run
        output = threading.Thread(target=self._collect, args=(run,), name="studio-run-output", daemon=True)
        output.start()
        threading.Thread(target=self._await_end, args=(run, output), name="studio-run-end", daemon=True).start()
        return {"pid": process.pid, "simulated": True}

    def _note(self, run, text):
        with self._lock:
            run.lines.append(text)

    def _collect(self, run):
        stream = run.process.stdout
        try:
            with stream:
                for raw in iter(lambda: stream.readline(LOG_LINE_BYTES), b""):
                    text = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if text.strip():
                        self._note(run, text)
        except (OSError, ValueError):
            # The pipe broke under the reader; whatever was read is in the log.
            pass

    def _await_end(self, run, output):
        code = run.process.wait()
        with self._lock:
            run.finished_utc = utc_now()
        # A helper program the run left behind keeps the pipe open; the end is still reported.
        output.join(OUTPUT_DRAIN_SECONDS)
        self._note(run, "[studio] simulated run ended: %s" % _ending(code))

    def stop(self):
        """End the run: SIGTERM, then SIGKILL if it has not ended after the grace period."""
        with self._stopping:
            with self._lock:
                run = self._run
                if run is None or run.process.poll() is not None:
                    raise _Refusal(
                        409, "No run started from the Studio is in progress",
                        "A scan started in the controller window is stopped there.")
                run.stop_requested = True
                run.lines.append("[studio] stop requested: SIGTERM sent to the simulated run")
            process = run.process
            process.terminate()
            forced = False
            try:
                process.wait(self._grace_seconds)
            except subprocess.TimeoutExpired:
                forced = True
                self._note(run, "[studio] the run did not end within %g s: SIGKILL sent" % self._grace_seconds)
                # SIGTERM goes to the run alone, which then ends its helper programs itself. A run
                # that has to be killed cannot: its whole process group goes. The run leads that
                # group (its own session, see start), and the helpers in it are simulated like it.
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                try:
                    process.wait(KILLED_WAIT_SECONDS)
                except subprocess.TimeoutExpired as error:
                    raise _Refusal(500, "The simulated run did not end",
                                   "Process %d is still there after SIGKILL." % process.pid) from error
            return {"stopped": True, "pid": process.pid, "exit_code": process.returncode, "forced": forced}

    def state(self):
        """The run this server started last, for the page; None when it started none."""
        with self._lock:
            run = self._run
            if run is None:
                return None
            code = run.process.poll()
            state = dict(run.asked)
            state.update(pid=run.process.pid, running=code is None, exit_code=code,
                         finished_utc=run.finished_utc, stop_requested=run.stop_requested)
            return state

    def log_tail(self):
        with self._lock:
            return list(self._run.lines) if self._run is not None else []

    def close(self):
        """A run this server started does not go on without it."""
        try:
            self.stop()
        except _Refusal as refusal:
            if refusal.status != 409:
                log.warning("%s: %s", refusal, refusal.detail)


# ---------------------------------------------------------------------- data-layer commands


class _Commands:
    """Runs data-layer commands in this process, through the front end the command line uses."""

    def __init__(self, home):
        self._home = home
        # One command that changes something at a time.
        self._turn = threading.Lock()
        self._slot = threading.Lock()
        self._arc_turn = threading.Lock()
        self._catalog = None
        self._catalog_key = None

    def _context(self):
        """A command context for one request.

        The catalog object is kept for as long as the settings select the same catalog.
        """
        try:
            settings = load_settings(self._home)
            usable = not settings.problems()
        except (DataLayerError, OSError):
            usable = False
        if not usable:
            # Without a catalog of its own the command itself reports what is wrong with the settings.
            return cli.Context(self._home)
        key = settings.catalog_key
        with self._slot:
            if key != self._catalog_key:
                self._catalog = open_catalog(settings, Paths(self._home))
                self._catalog_key = key
            catalog = self._catalog
        return cli.Context(self._home, catalog=catalog)

    def missing(self, command):
        """The failure object to answer with while `command` cannot be run, else None.

        The front end is asked for the command's help. A part of the edition that is not
        installed has registered no command, which the front end reports as wrong usage; a
        command module that is there and broken is reported as what it is.
        """
        with cli.Context(self._home) as ctx:
            probe = cli.invoke(ctx, [command, "-h"])
        if probe.get("ok"):
            return None
        if probe.get("kind") == "usage":
            return {"ok": False, "error": NOT_AVAILABLE,
                    "detail": "This installation has no '%s' command." % command}
        return probe

    def run(self, arguments, changes=False):
        """The answer of one command, as the command line would print it."""
        with self._context() as ctx:
            if not changes:
                return cli.invoke(ctx, arguments)
            with self._turn:
                return cli.invoke(ctx, arguments)

    def arc(self, arguments):
        """The answer of an arc-* command (uploads to ARC).

        These commands work on files under HOME alone and never on the catalog, so they need no
        usable catalog settings and do not wait while another command has the catalog. Their own
        locks keep this server, the window and the upload programs apart; in this server they also
        run one at a time, as each first looks after the upload programs this process started.
        """
        with self._arc_turn, cli.Context(self._home) as ctx:
            return cli.invoke(ctx, arguments)

    def pipeline(self, limit, session):
        """`status`, `list` and `review-list` in one answer (GET /api/pipeline)."""
        with self._context() as ctx:
            return self._pipeline(ctx, limit, session)

    def _pipeline(self, ctx, limit, session):
        status = cli.invoke(ctx, ["status"])
        if not status.get("ok"):
            return status
        arguments = ["list", "--limit=%d" % limit]
        if session is not None:
            arguments.append("--session=" + session)
        listing = cli.invoke(ctx, arguments)
        if not listing.get("ok"):
            return listing
        answer = {
            "ok": True,
            "status": {key: value for key, value in status.items() if key != "ok"},
            "items": listing["items"],
            "total": listing["total"],
            "review": [],
            "as_of_utc": utc_now(),
        }
        # The page must still get the status and the captures when the review list cannot be
        # had: its commands are not installed, or the catalog they ask is unreachable.
        review = self.missing("review-list") or cli.invoke(ctx, ["review-list"])
        if review.get("ok"):
            answer["review"] = review.get("items", [])
        else:
            answer["review_problem"] = review.get("error") or NOT_AVAILABLE
        return answer


# ---------------------------------------------------------------------- what the endpoints do


def _text(fields, name, limit=TEXT_LIMIT, required=True, lines=False):
    """A text field of a request, trimmed. `lines` allows line breaks in it.

    Control characters, line separators and half surrogates are refused: the text becomes a
    command-line argument and a line in records and logs. So are the invisible characters that
    reorder text for display, with which a sample ID could read differently from what is stored.
    Letters of any script are fine, and so are the joiners some scripts and emoji need.
    """
    value = fields.get(name)
    if value is None and not required:
        return None
    if value is not None and not isinstance(value, str):
        raise _Refusal(400, "%s must be text" % name)
    value = (value or "").strip()
    if required and not value:
        raise _Refusal(400, "%s is required" % name)
    if len(value) > limit:
        raise _Refusal(400, "%s is longer than %d characters" % (name, limit))
    for character in value:
        if (unicodedata.category(character) in ("Cc", "Cs", "Zl", "Zp") and not (lines and character in "\n\t")) \
                or character in BIDI_CONTROLS:
            raise _Refusal(400, "%s holds a character that cannot be used" % name)
    return value


def _argument_text(fields, name):
    """A text the runner gets as one command-line argument.

    It is never an option: a value starting with "-" is refused, so that whatever way the
    runner reads its arguments, the page can supply three values and nothing else.
    """
    value = _text(fields, name)
    if value.startswith("-"):
        raise _Refusal(400, "%s must not start with '-'" % name)
    return value


def _count(value, name, largest):
    """A whole number from 1 to `largest`, given as a JSON number or as query text."""
    if isinstance(value, str) and _WHOLE_NUMBER.fullmatch(value):
        value = int(value)
    if not _whole(value) or not 1 <= value <= largest:
        raise _Refusal(400, "%s must be a whole number from 1 to %d" % (name, largest))
    return value


def _only(fields, names):
    """Refuse a body that holds a field the endpoint does not take. Left out silently, a misspelt
    or extra field would leave the request doing something other than what was meant."""
    if any(name not in names for name in fields):
        raise _Refusal(400, "The body may hold only: %s" % ", ".join(names))


class Studio:
    """What the endpoints do. Each method takes the query or body fields of a request and
    returns the answer: an object for the API, bytes for the page."""

    def __init__(self, home, stop_grace_seconds=STOP_GRACE_SECONDS):
        self.home = Path(home).expanduser().resolve()
        self.paths = Paths(self.home)
        self._files = _ScanFiles(self.paths)
        self._runs = _SimulatedRuns(self.paths, stop_grace_seconds)
        self._commands = _Commands(self.home)

    def page(self, _fields):
        for folder in (self.paths.studio, SOURCE_STUDIO):
            data, _status = _read(folder / PAGE_NAME, LARGE_FILE_BYTES)
            if data is not None:
                return data
        raise _Refusal(404, "The Studio page is not installed",
                       "%s does not exist." % (self.paths.studio / PAGE_NAME))

    def ping(self, _fields):
        answer = {"ok": True, "home": str(self.home), "mode": None,
                  "simulated_stack_available": self._runs.available()}
        try:
            settings = load_settings(self.home)
        except (DataLayerError, OSError) as error:
            answer["problem"] = str(error)
            return answer
        answer["mode"] = MODE_LOCAL_DEMO
        problems = settings.problems()
        if problems:
            answer["problem"] = "The settings are not valid: " + "; ".join(problems)
        return answer

    def sample(self, _fields):
        folder = self.paths.virtual_sample
        sample = _load(folder / "sample.json", JSON_FILE_BYTES)
        if sample is None:
            raise _Refusal(404, "This HOME has no virtual sample",
                           "%s is missing or is not a JSON object." % (folder / "sample.json"))
        answer = dict(sample)
        answer["reference_spectra"] = _reduced_reference(_load(folder / "reference_spectra.json", LARGE_FILE_BYTES))
        answer["ok"] = True
        return answer

    def scan(self, fields):
        selected = fields.get("session")
        # The name only ever selects among the folders found on disk; this is for a clear answer.
        if selected is not None and (_text(fields, "session") != selected or "/" in selected
                                     or selected.startswith(("_", "."))):
            raise _Refusal(400, "session must be the name of a session folder")
        answer = self._files.snapshot(selected)
        answer["ok"] = True
        answer["active"] = answer["active"] or self._runs.alive()
        answer["run"] = self._runs.state()
        answer["log_tail"] = self._runs.log_tail()
        return answer

    def pipeline(self, fields):
        limit = _count(fields.get("limit", LIST_LIMIT), "limit", MAX_LIST_LIMIT)
        return self._commands.pipeline(limit, _text(fields, "session", required=False) or None)

    def provenance(self, fields):
        return self._commands.run(["provenance", "--record=" + _text(fields, "record")])

    def sync(self, fields):
        retry = fields.get("retry_failed", False)
        if not isinstance(retry, bool):
            raise _Refusal(400, "retry_failed must be true or false")
        return self._commands.run(["sync", "--retry-failed"] if retry else ["sync"], changes=True)

    def classify(self, fields):
        arguments = ["classify"]
        if fields.get("limit") is not None:
            arguments.append("--limit=%d" % _count(fields["limit"], "limit", MAX_LIST_LIMIT))
        return self._optional(arguments)

    def review(self, fields):
        if fields.get("decision") not in REVIEW_DECISIONS:
            raise _Refusal(400, "decision must be one of: %s" % ", ".join(REVIEW_DECISIONS))
        arguments = ["review-set", "--prediction=" + _text(fields, "prediction"),
                     "--decision=" + fields["decision"], "--reviewer=" + _text(fields, "reviewer")]
        label = _text(fields, "label", required=False)
        if label:
            arguments.append("--label=" + label)
        comment = _text(fields, "comment", COMMENT_LIMIT, required=False, lines=True)
        if comment:
            arguments.append("--comment=" + comment)
        return self._optional(arguments)

    def _optional(self, arguments):
        """Run a changing command of a part of the edition that may not be installed."""
        missing = self._commands.missing(arguments[0])
        if missing is None:
            return self._commands.run(arguments, changes=True)
        if missing["error"] == NOT_AVAILABLE:
            raise _Refusal(501, NOT_AVAILABLE, missing["detail"])
        return missing

    def offline(self, fields):
        if not isinstance(fields.get("offline"), bool):
            raise _Refusal(400, 'The body must say "offline": true or false')
        return self._commands.run(["offline", "--on" if fields["offline"] else "--off"], changes=True)

    def run(self, fields):
        mode, power = fields.get("mode"), fields.get("power")
        if not isinstance(mode, str) or mode not in SCAN_MODES:
            raise _Refusal(400, "mode must be one of: %s" % ", ".join(SCAN_MODES))
        if not isinstance(power, str) or power not in LASER_POWER_LEVELS:
            raise _Refusal(400, "power must be one of: %s" % ", ".join(LASER_POWER_LEVELS))
        texts = [_argument_text(fields, name) for name in ("sample_id", "operator", "location")]
        answer = self._runs.start(mode, power, *texts)
        answer["ok"] = True
        return answer

    def stop(self, _fields):
        answer = self._runs.stop()
        answer["ok"] = True
        return answer

    # ------------------------------------------------------------------ uploads to ARC
    def arc(self, _fields):
        """GET /api/arc: the newest uploads and a few ARC settings, from arc-list alone.

        The page asks every second while an upload works. arc-sessions is not run: it reads every
        session folder and lists the running programs, and the page has no use for what it says.
        """
        uploads = self._arc(["arc-list", "--limit=%d" % ARC_LIST_LIMIT])
        if not uploads.get("ok"):
            return uploads
        flat = uploads.get("settings") if isinstance(uploads.get("settings"), dict) else {}
        return {"ok": True, "uploads": uploads.get("items", []), "active": uploads.get("active", 0),
                "unfinished": uploads.get("unfinished", 0),
                "settings": {name: flat.get("arc_upload." + name) for name in ARC_SETTINGS_SHOWN}}

    def arc_upload(self, fields):
        """POST /api/arc/upload: a demo upload of a session folder."""
        _only(fields, ("session", "transport", "fault"))
        session = _text(fields, "session")
        if session != fields["session"] or "/" in session or session.startswith(("_", ".")):
            raise _Refusal(400, "session must be the name of a session folder")
        transport = fields.get("transport")
        if not isinstance(transport, str) or transport not in ARC_TRANSPORTS:
            raise _Refusal(400, "transport must be one of: %s" % ", ".join(ARC_TRANSPORTS))
        fault = fields.get("fault")
        if "fault" in fields and (not isinstance(fault, str) or fault not in ARC_DEMO_FAULTS):
            raise _Refusal(400, "fault must be one of: %s" % ", ".join(ARC_DEMO_FAULTS))
        if fault is not None and fault not in ARC_FAULTS_OF[transport]:
            raise _Refusal(400, "fault with transport %s must be one of: %s"
                           % (transport, ", ".join(ARC_FAULTS_OF[transport])))
        # Always a demo upload: the Studio sends nothing to ARC. And never --controller-idle: the
        # Studio cannot tell that a scan has ended, so the session must have been quiet for a while.
        arguments = ["arc-upload", "--session=" + session, "--transport=" + transport, "--demo"]
        if fault is not None:
            arguments.append("--demo-fault=" + fault)
        return self._arc(arguments)

    def arc_retry(self, fields):
        return self._arc_action("arc-retry", fields)

    def arc_stop(self, fields):
        return self._arc_action("arc-stop", fields)

    def arc_check(self, fields):
        return self._arc_action("arc-check", fields)

    def arc_keep_copy(self, fields):
        return self._arc_action("arc-keep-copy", fields)

    def _arc_action(self, command, fields):
        _only(fields, ("upload",))
        upload = fields.get("upload")
        if not isinstance(upload, str) or not _UPLOAD_ID.fullmatch(upload):
            raise _Refusal(400, "upload must be an upload id: u and 12 lowercase hexadecimal digits")
        # --demo-only: the command refuses an upload that is not a demo one (refused, HTTP 403).
        return self._arc([command, "--upload=" + upload, "--demo-only"])

    def _arc(self, arguments):
        """Run an arc-* command; 501 "not available" when this installation has none."""
        missing = self._commands.missing(arguments[0])
        if missing is None:
            return self._commands.arc(arguments)
        if missing["error"] == NOT_AVAILABLE:
            raise _Refusal(501, NOT_AVAILABLE, missing["detail"])
        return missing

    def run_alive(self):
        return self._runs.alive()

    def close(self):
        self._runs.close()


_GET = {
    "/": Studio.page,
    "/api/ping": Studio.ping,
    "/api/sample": Studio.sample,
    "/api/scan": Studio.scan,
    "/api/pipeline": Studio.pipeline,
    "/api/provenance": Studio.provenance,
    "/api/arc": Studio.arc,
}
_POST = {
    "/api/sync": Studio.sync,
    "/api/classify": Studio.classify,
    "/api/review": Studio.review,
    "/api/offline": Studio.offline,
    "/api/run": Studio.run,
    "/api/stop": Studio.stop,
    "/api/arc/upload": Studio.arc_upload,
    "/api/arc/retry": Studio.arc_retry,
    "/api/arc/stop": Studio.arc_stop,
    "/api/arc/check": Studio.arc_check,
    "/api/arc/keep-copy": Studio.arc_keep_copy,
}


# ---------------------------------------------------------------------- HTTP


class _Handler(http.server.BaseHTTPRequestHandler):
    """HTTP for the Studio: the page at /, JSON under /api/, and nothing else."""

    protocol_version = "HTTP/1.1"
    server_version = "RamanStudio"
    sys_version = ""
    timeout = CONNECTION_TIMEOUT_SECONDS

    def do_GET(self):
        self._handle(_GET)

    def do_POST(self):
        self._handle(_POST)

    def _other_method(self):
        self._handle({})

    do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _other_method

    def log_message(self, format, *args):
        # The page asks several times a second; its requests are worth a line only when debugging.
        log.debug("%s %r", self.client_address[0], format % args)

    def _handle(self, routes):
        headers = ()
        try:
            answer = self._answer(routes)
            status = 200 if isinstance(answer, bytes) or answer.get("ok") else _STATUS_OF_KIND.get(
                answer.get("kind"), 400)
        except _Refusal as refusal:
            status, answer, headers = refusal.status, refusal.answer(), refusal.headers
        except Exception as error:
            log.exception("%s %s failed", self.command, self.path)
            status, answer = 500, cli.error_payload(error)
        self._send(status, answer, headers)

    def _answer(self, routes):
        posted = self.command == "POST"
        body = self._body() if posted else None
        if not posted and (self.headers.get("Content-Length", "0").strip() != "0"
                           or self.headers.get("Transfer-Encoding")):
            # A body that nobody reads would be taken for the next request on this connection.
            self.close_connection = True
        host = self._host()
        path, _, query = self.path.partition("?")
        if posted:
            self._own_origin(host)
        elif path.startswith("/api/"):
            self._not_from_another_site()
        endpoint = routes.get(path)
        if endpoint is None:
            allowed = [method for method, table in (("GET", _GET), ("POST", _POST)) if path in table]
            if allowed:
                raise _Refusal(405, "This address takes %s only" % allowed[0], headers=[("Allow", allowed[0])])
            raise _Refusal(404, "This server has nothing at that address")
        with self.server.in_use():
            return endpoint(self.server.studio, self._posted_fields(body) if posted else self._query_fields(query))

    def _host(self):
        """The Host header, which has to name this server.

        A page of another site can make the browser resolve that site's own name to 127.0.0.1
        and would then read this API as if it were its own. Such a request still carries the
        other name in Host.
        """
        named = self.headers.get_all("Host") or []
        host = named[0].strip().lower() if len(named) == 1 else ""
        port = self.server.server_address[1]
        if host not in ("%s:%d" % (BIND_ADDRESS, port), "localhost:%d" % port):
            raise _Refusal(403, "This server answers only when it is addressed as 127.0.0.1 or localhost")
        return host

    def _own_origin(self, host):
        """Refuse a command sent by a page this server did not serve.

        A page of another site cannot read the answer to a POST it sends here, but the command
        would still be carried out. The browser names the sending page in Origin.
        """
        origins = [origin.strip().lower() for origin in self.headers.get_all("Origin") or []]
        if origins and origins != ["http://" + host]:
            raise _Refusal(403, "Commands are accepted only from the Studio page this server serves")

    def _not_from_another_site(self):
        """Refuse a read of the API that a page of another site made the browser send.

        That page cannot read the answer, but a review list also writes to the ledger. Browsers name
        where a request comes from in Sec-Fetch-Site: "same-origin" from the Studio page, "none"
        when the user typed the address. Programs that send no such header are not browsers.
        """
        sites = [site.strip().lower() for site in self.headers.get_all("Sec-Fetch-Site") or []]
        if any(site not in ("same-origin", "none") for site in sites):
            raise _Refusal(403, "The API answers only the Studio page this server serves")

    def _body(self):
        """The body of a POST.

        It is read before anything is refused, so that the connection can carry the next
        request; where it cannot be read, the connection is closed after the answer.
        """
        declared = (self.headers.get("Content-Length") or "").strip()
        if self.headers.get("Transfer-Encoding") or not _CONTENT_LENGTH.fullmatch(declared):
            self.close_connection = True
            raise _Refusal(411, "A POST needs a Content-Length")
        if int(declared) > MAX_BODY_BYTES:
            self.close_connection = True
            raise _Refusal(413, "The request body is larger than %d bytes" % MAX_BODY_BYTES)
        try:
            body = self.rfile.read(int(declared))
        except OSError:
            body = b""
        if len(body) != int(declared):
            self.close_connection = True
            raise _Refusal(400, "The request body did not arrive in full")
        return body

    def _posted_fields(self, body):
        # A page of another site can post a form here without asking first, but not this type.
        if self.headers.get_content_type() != "application/json":
            raise _Refusal(415, "A POST body must be JSON (Content-Type: application/json)")
        fields = _json_object(body)
        if fields is None:
            raise _Refusal(400, "The request body must be a JSON object")
        return fields

    @staticmethod
    def _query_fields(query):
        try:
            fields = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=QUERY_FIELDS)
        except ValueError as error:
            raise _Refusal(400, "The query has too many fields") from error
        if any(len(values) > 1 for values in fields.values()):
            raise _Refusal(400, "A query field was given more than once")
        return {name: values[0] for name, values in fields.items()}

    def _send(self, status, answer, headers=()):
        if isinstance(answer, bytes):
            body, kind = answer, "text/html; charset=utf-8"
            # One click on the page starts a run, so the page must not sit in another site's frame.
            headers = tuple(headers) + (("X-Frame-Options", "DENY"),)
        else:
            kind = "application/json; charset=utf-8"
            try:
                body = json.dumps(answer, allow_nan=False, default=str).encode("ascii")
            except ValueError as error:
                # NaN or infinity reached the answer; the browser could not parse it.
                status = 500
                body = json.dumps(cli.error_payload(error)).encode("ascii")
        try:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in headers:
                self.send_header(name, value)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError:
            # The client went away; there is nobody left to answer.
            self.close_connection = True


class StudioServer(http.server.ThreadingHTTPServer):
    """The Studio on 127.0.0.1:port; port 0 takes any free port. One thread per connection, so
    that a command that takes long does not keep the page from being answered."""

    def __init__(self, home, port=0, stop_grace_seconds=STOP_GRACE_SECONDS):
        self.studio = Studio(home, stop_grace_seconds)
        self._use = threading.Lock()
        self._being_answered = 0
        self._last_use = time.monotonic()
        super().__init__((BIND_ADDRESS, port), _Handler)

    @property
    def url(self):
        return "http://%s:%d/" % (BIND_ADDRESS, self.server_address[1])

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, OSError):
            # A connection that broke: the browser closed the page or stopped waiting.
            log.debug("connection from %s ended: %s", client_address[0], error)
        else:
            log.exception("a request from %s could not be handled", client_address[0])

    @contextlib.contextmanager
    def in_use(self):
        """Around the answering of a request this server accepted."""
        with self._use:
            self._being_answered += 1
        try:
            yield
        finally:
            with self._use:
                self._being_answered -= 1
                self._last_use = time.monotonic()

    def idle_seconds(self):
        """How long nobody has used the Studio: no request being answered or answered since,
        and no run of its own in progress."""
        with self._use:
            if self._being_answered or self.studio.run_alive():
                self._last_use = time.monotonic()
            return time.monotonic() - self._last_use

    def close(self):
        self.server_close()
        self.studio.close()


# ---------------------------------------------------------------------- the command


def _answers(port):
    """True when something already accepts connections on 127.0.0.1:port.

    Binding alone does not tell. The server socket may reuse an address, so that the port is
    free again right after a restart, and macOS then lets it bind next to a program that
    listens on every address: this server would take that program's local connections away.
    """
    try:
        socket.create_connection((BIND_ADDRESS, port), timeout=0.5).close()
    except OSError:
        return False
    return True


def open_server(home, port=DEFAULT_PORT, **options):
    """The Studio server on `port`, or on the next free port when that one is taken."""
    if port == 0:
        return StudioServer(home, 0, **options)
    last = min(port + PORT_ATTEMPTS, 65536) - 1
    for candidate in range(port, last + 1):
        if _answers(candidate):
            continue
        try:
            return StudioServer(home, candidate, **options)
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise CommandError("The Studio cannot listen on port %d" % candidate, detail=str(error)) from error
    raise CommandError("No free port for the Studio", detail="Ports %d to %d on %s are all taken."
                       % (port, last, BIND_ADDRESS))


def _serve(server, idle_exit):
    """Serve until interrupted: Ctrl-C, or SIGTERM or SIGHUP from whoever started the server.

    With `idle_exit` the server also ends once nobody has used it for that many seconds. A
    server that was started detached has nobody to interrupt it.
    """

    def interrupt(_number, _frame):
        raise KeyboardInterrupt

    def end_when_idle():
        while server.idle_seconds() < idle_exit:
            time.sleep(min(1.0, idle_exit))
        log.info("nobody has used the Studio for %g s; stopping", idle_exit)
        server.shutdown()

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGTERM, signal.SIGHUP):
            # Without a handler the process would end at once and leave a run it started behind.
            # A signal the server was started to ignore (nohup) stays ignored.
            if signal.getsignal(number) is not signal.SIG_IGN:
                previous[number] = signal.signal(number, interrupt)
    if idle_exit:
        threading.Thread(target=end_when_idle, name="studio-idle", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("interrupted; stopping")
    finally:
        try:
            server.close()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def cmd_studio(args, ctx):
    if not ctx.home.is_dir():
        raise CommandError("The HOME folder does not exist", detail=str(ctx.home))
    port = getattr(args, "port", DEFAULT_PORT)
    idle_exit = getattr(args, "idle_exit", 0)
    if not 0 <= port <= 65535:
        raise CommandError("--port must be a number from 0 to 65535")
    if not math.isfinite(idle_exit) or idle_exit < 0:
        raise CommandError("--idle-exit must be a number of seconds, 0 or more")
    server = open_server(ctx.home, port)
    url = server.url
    try:
        # The one line on stdout; whoever started the server reads the address from it.
        ctx.emit({"url": url})
        log.info("Simulation Studio at %s for %s; Ctrl-C stops it", url, ctx.home)
        if not getattr(args, "no_open", False) and not webbrowser.open(url):
            log.warning("no browser could be opened; the page is at %s", url)
    except BaseException:
        server.close()
        raise
    _serve(server, idle_exit)
    return {"url": url}


def register(subparsers):
    command = subparsers.add_parser(
        "studio", help="serve the Simulation Studio page on 127.0.0.1 until interrupted")
    command.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="N",
                         help="first port to try (default %d); the next free one is used when it is taken"
                         % DEFAULT_PORT)
    command.add_argument("--no-open", action="store_true", help="do not open the page in the default browser")
    command.add_argument("--idle-exit", type=float, default=0, metavar="SECONDS",
                         help="also stop once nobody has used the Studio for this long (default: never)")
    command.set_defaults(func=cmd_studio)
