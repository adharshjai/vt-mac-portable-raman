"""The upload program: one process per upload action, never waited for (PROTOCOL.md 5.6).

    python -m raman_datafed.arc_worker --home HOME --upload ID --action send|check|stop [--terminal --attempt N]

Like `python -m raman_datafed.arc_job` it is a program of its own, not a section 6 command: the
window never calls it and it prints no JSON object. The arc-* commands start it detached
(spawn(), stdout and stderr /dev/null) or, for a live SSH upload, inside a Terminal window
(write_terminal_script() and open_terminal()). Exit codes: 0 the upload is verified (also after a
check that found it so), 1 it ended failed, stopped, conflict, uncertain or not watched (or a check
did not find the copy, or was stopped), 2 wrong arguments, 3 not its turn (worker.lock held, an older
--attempt, or a state the action does not take), 97 a test fault (fault()), 130 stopped with Ctrl-C,
128 + N ended by signal N without a final state (143 SIGTERM; 129 and 130 for SIGHUP and SIGINT of a
detached worker).

What the worker does itself, for every transport:
- takes <record>/worker.lock (3 tries over 2 s), then, in one step under record.lock, re-reads the
  record, checks the action and the state and writes record["worker"]; clears it at the end,
  whatever the end;
- send: the state "preparing": the readiness rule again and the snapshot (arc_payload), or, on a
  retry, a check of the snapshot against its own lists; then the transport;
- polls requests.stop_utc every second (a stop worker does not: it is the stop). A person's stop is
  that request, or, in a Terminal window, Ctrl-C (SIGINT) and closing the window (SIGHUP). Any other
  signal (SIGTERM at a logout, restart or shutdown; SIGHUP or SIGINT of a detached worker) ends the
  work at once (WorkerEnding): nothing is stopped or cancelled and no final state is written, so the
  listing shows the upload as not watched or interrupted;
- imports every module it may need and reads arc_remote.sh before its first step (load_code());
- runs /usr/bin/caffeinate -i -w <pid> for live uploads (idle sleep would stop Globus Connect
  Personal from serving the files);
- logs to <record>/worker.log only (rotated at 1 MB, one old file kept), through a filter that
  drops any line with a token, a code or a password marker; exceptions are logged as their type
  and file:line:function frames, never their text;
- applies the transport's Outcome: the final state (checked against arc_records.TRANSITIONS),
  message, error and fields; once verified is written, deletes the snapshot and compares the session
  folder with what was sent (notes and warnings), in a second write;
- a Terminal worker drops what was typed meanwhile, then prints its last lines: a check's own result
  (and, when it did not find the copy, what to do), else the upload's message and the fix of a failure.

TRANSPORT INTERFACE (what arc_globus and arc_ssh implement; step 2 of the build)
================================================================================

Entry points. The worker imports raman_datafed.arc_<transport> (arc_globus or arc_ssh) only when
it needs it and calls one function of it with a WorkerContext:

    send(ctx)  -> Outcome   action send: the snapshot is ready, the record is in "preparing".
                            The transport goes on with "signing_in" and its own steps. On a
                            retry, ctx.resume_at says where to resume (arc_records.retry_target):
                            Globus "checking" (with a task_id known: a new attempt, design 10.4),
                            "submitting" (the saved body again), "transferring", "verifying" or
                            "placing"; SSH: none (the remote script finds what is left; mode
                            upload-copy whenever destination.new_copy is true).
    check(ctx) -> Outcome   action check: the record keeps its state while the transport signs in
                            (no "signing_in"); then it may move to a later state (TRANSITIONS).
    stop(ctx)  -> Outcome   action stop, Globus only: settle the request and cancel the task
                            (design 10.8). arc-stop handles every other stop without a worker.

Demo or live is ctx.demo; each transport picks its far end itself: arc_demo.DemoGlobus or the
live Globus client (which must call check_live_globus() before it signs in); arc_ssh's
LocalBashRunner or /usr/bin/ssh (run through check_program() / popen_program()).

Results. A transport returns an Outcome (it may also raise, see below):

    Outcome(state, message=None, error=None, fields=None, check=None)

    state    "verified", "failed", "stopped", "conflict" or "uncertain": the worker writes it,
             checked against TRANSITIONS. Or the state the record is in now (for example
             "transferring" after the 90-minute watch, or whatever a check worker leaves as it was):
             then only the message and the fields change.
    message  the record's message (None keeps the current one); demo texts start with "Demo: "
             (ctx.text() adds it).
    error    for "failed": ctx.error(reason, **values), the Appendix A.2 object
             {"kind", "reason", "message", "fix", "retryable"}; ctx.failed(reason, **values) builds
             the whole Outcome. For any other state the record's error becomes null.
    fields   top-level record keys to set with the final state, for example
             {"conflict_at": "placing"} or {"verification": {"method", "algorithm", "files", "utc"}}.
             Transport sub-records ("globus", "ssh") are better kept current with ctx.update().
    check    a check of a verified upload: arc_records.check_result(ok, message) when it looked at the
             copy, arc_records.check_note(message, reason) when it could not ({"ok", "looked", "message",
             "fix"}); appended to "checks" with its time, the state stays "verified". Only a check that
             looked and found the copy missing or different makes the upload check_failed (review REG-01).

    Raising instead: StopRequested (ctx.check_stop()) -> "stopped" with the standard message
    ("Stopped before anything was sent." or "Stopped. Files already copied stay in <incoming> on
    ARC; nothing was deleted.", from what the record says was sent); for a check worker, the state
    stays as it was (Check status changes nothing when it is stopped); arc_payload.PayloadError
    (ctx.check_payload()) -> "failed" with its reason (payload_changed); arc_records.ArcError (a
    refusal) -> "failed" with that refusal's kind, reason, message and fix; WorkerEnding (a signal
    that ends the process) -> no final state; anything else -> "failed" / internal with the
    exception's type name only. A check or stop worker leaves a stored state that is not a working
    state as it was (a verified upload gets a "checks" entry instead: a check that could not look).

    The worker then applies, whatever the transport returned: a Terminal send worker whose window
    was closed (SIGHUP) ends "failed" / terminal_closed unless the outcome is verified or conflict
    (a check worker leaves the upload as it was); after Ctrl-C (SIGINT) a "failed" outcome of a
    working state becomes "stopped" (exit 130). An outcome that TRANSITIONS does not allow from the
    stored state becomes "failed" / internal (TransitionError). On "verified" the worker writes the
    record first, then deletes the snapshot (copies of the manifest, SHA256SUMS and
    payload_hashes.json stay) and compares the session folder with the manifest; "requests.stop_utc",
    "worker" and (outside working states) "resume" are cleared.

The context (WorkerContext). Facts fixed when the worker starts:
    ctx.home, ctx.paths, ctx.upload_id, ctx.record_dir, ctx.action ("send", "check", "stop"),
    ctx.transport ("globus", "ssh"), ctx.demo, ctx.terminal (a Terminal worker), ctx.attempt
    (--attempt of a Terminal worker, else the record's ssh.attempt or globus.attempt),
    ctx.settings_used (dict: the record's settings_used; Retry never uses the current settings),
    ctx.remote_script (the bytes of arc_remote.sh read when the worker started, or None: an SSH
    attempt sends these, so that a new install meanwhile cannot mix versions),
    ctx.session_folder (name), ctx.payload_dir (Path <record>/payload/<session folder>, the
    folder that is sent: it holds the manifest and SHA256SUMS), ctx.demo_root (Path or None),
    ctx.resume_at (record["resume"]["at"] at start, or None), ctx.log (a logging.Logger writing
    to worker.log), ctx.started_monotonic.
The record (every write is a read-modify-write under record.lock; record.json is never written
any other way):
    ctx.read() -> dict                            a fresh copy of record.json
    ctx.update(mutate) -> dict                    mutate(record) changes the dict in place
    ctx.set_state(state, message=None, event=None, **fields) -> dict
                                                  checked against TRANSITIONS (same state: plain
                                                  update); sets state_since_utc, the message,
                                                  top-level fields; clears "resume" once its
                                                  state is reached; appends an event (the event
                                                  text, else the message)
    ctx.event(text)                               one line in events.jsonl (never a secret)
    ctx.progress(files_done=None, bytes_done=None, message=None, force=False) -> bool
                                                  record["progress"] (and the message), written at
                                                  most once a second unless force
    ctx.placed_as(name) -> dict                   destination.placed_as = name, final_path (and the
                                                  Globus and local paths) of <base>/<name>
Stop requests (arc-stop; in a Terminal window Ctrl-C and closing it):
    ctx.stop_requested() -> bool; ctx.check_stop() raises StopRequested;
    ctx.wait(seconds) -> bool                     sleeps, returns True at once on a stop request
The snapshot:
    ctx.payload_digests() -> {path: {"bytes", "sha256", "md5"}}, every file of ctx.payload_dir
                                                  (the manifest and SHA256SUMS included), from the
                                                  snapshot pass
    ctx.check_payload() -> the same, hashed again now; raises arc_payload.PayloadError
Texts and results:
    ctx.text(text) -> text with "Demo: " in front for a demo upload
    ctx.error(reason, **values) -> dict           arc_records.worker_error (Appendix A.2; a demo upload
                                                  gets the demo wording of DEMO_RESULTS)
    ctx.failed(reason, **values) -> Outcome
    ctx.outcome(state, message=None, error=None, **fields) -> Outcome
Test hooks: ctx.fault(name) (fault()), and the guard functions below.

Record keys the transports keep current (with ctx.update / ctx.set_state; design 7.2): "globus"
(identity, attempt, submission_id, request_saved_utc, label, posts, task_id, task,
earlier_attempts), "ssh" (runs; "sent": true once a run got past `RAMAN STEP receive`, which makes
Check status available after a failure; "attempt" is set by the commands for every Terminal
attempt and for every retry), "verification" and "conflict_at" (Outcome fields),
destination.placed_as (ctx.placed_as). settings_used holds, besides the keys of design 7.2,
include_simulated and settle_seconds; a demo Globus upload has arc_records.DEMO_MAC_COLLECTION and
DEMO_ARC_COLLECTION as mac_collection and arc_collection. Files a transport writes into the
record folder (globus_request.json) use arc_records.write_private_file (0600, atomic, fsync of the
file and the folder).

record["demo_fault"] is the teaching fault of a demo upload (lose_submit_reply, fail_task_once,
drop_connection; design 12), to be shown once per upload: the transport notes in its own
sub-record that it was used. record["destination"] holds every path (cluster, Globus, incoming,
base, demo_root, local_final_path, new_copy), computed once by arc-upload.

A Terminal worker (ctx.terminal): the transport prints its own lines (the sign-in text of design
11.4) on sys.stderr, which is the Terminal window; the worker prints the final message, and the fix
of a failure, after the transport returned.

Other helpers: arc_records.MESSAGES and message(key, demo, **values) (the state messages of
Appendix A.2), arc_records.local_path(demo_root, cluster_path) (the demo folder that stands for a
cluster path), demo_pace() (RAMAN_ARC_DEMO_PACE), arc_payload.MANIFEST_NAME / SUMS_NAME.

Logging: use ctx.log or logging.getLogger(__name__); never log a token, an authorization code, a
sign-in URL, a password or a Duo answer (as a second net, the filter drops lines with SECRET_MARKERS or
SECRET_WORDS, and shows a long run shaped like a token or a code as [hidden]).

The test guard (RAMAN_ARC_TEST_GUARD=1): check_program(argv) refuses ssh, scp, sftp, open,
osascript and caffeinate; check_spawn(argv) anything but this module; check_live_globus() the
live Globus client. fault(name) ends the process with code 97 when RAMAN_ARC_TEST_HOOKS=1 and
RAMAN_ARC_FAULT=name (fault names: design section 13).
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import importlib
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import arc_payload, arc_records, arc_session
from .arc_records import ArcError, set_state
from .config import DataLayerError, Paths, utc_now

log = logging.getLogger(__name__)

WORKER_MODULE = "raman_datafed.arc_worker"
ACTIONS = ("send", "check", "stop")
EXIT_VERIFIED, EXIT_ENDED, EXIT_USAGE, EXIT_NOT_ITS_TURN, EXIT_FAULT, EXIT_INTERRUPTED = 0, 1, 2, 3, 97, 130
LOCK_TRIES, LOCK_SPAN_SECONDS = 3, 2.0
PROGRESS_SECONDS = 1.0
STOP_POLL_SECONDS = 1.0
LOG_BYTES, LOG_BACKUPS = 1 << 20, 1
OPEN_PROGRAM = "/usr/bin/open"
CAFFEINATE = "/usr/bin/caffeinate"
OPEN_SECONDS = 15
GUARD_VARIABLE = "RAMAN_ARC_TEST_GUARD"
HOOKS_VARIABLE = "RAMAN_ARC_TEST_HOOKS"
FAULT_VARIABLE = "RAMAN_ARC_FAULT"
PACE_VARIABLE = "RAMAN_ARC_DEMO_PACE"
DEFAULT_DEMO_PACE = 0.1
# Programs that reach outside this Mac, open windows, or keep it awake: refused under the test guard.
_GUARDED_PROGRAMS = ("ssh", "scp", "sftp", "open", "osascript", "caffeinate")
# Lines with any of these (in any letter case) never reach worker.log; SECRET_WORDS count as whole words
# only, so that a function name in a traceback frame (revoke_tokens) does not drop the line.
SECRET_MARKERS = ("code=", "state=", "access_token", "refresh_token", "id_token", "Authorization", "Bearer",
                  "code_verifier", "client_secret", "password")
SECRET_WORDS = re.compile(r"\b(token|tokens|verifier|secret|passcode)\b", re.IGNORECASE)
# A run of 24 or more characters of the kind a token, an authorization code or a PKCE verifier is made of,
# holding letters and digits: shown as [hidden] in worker.log, whatever the words around it (a second net
# behind the markers). A Globus task or request number (a UUID) stays.
SECRET_RUN = re.compile(r"[A-Za-z0-9_~+=-]{24,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EARLIER_ATTEMPT_TEXT = ("This Terminal window belongs to an earlier attempt of this upload; use the newest one, or "
                        "press Retry same request in the controller.")
# Modules a worker may need, imported when it starts (with arc_remote.sh read), so that a new install of the
# data layer during an upload never mixes two versions in one run. globus_sdk stays out: only the live
# Globus client imports it.
TRANSPORT_MODULES = ("raman_datafed.arc_globus", "raman_datafed.arc_ssh", "raman_datafed.arc_demo")
ENDED_BY_SIGNAL_TEXT = ("The upload program was ended by the system ({signal}) before it finished. Nothing was "
                        "stopped or cancelled.")
# The event of a stop that came as a worker ended, handed to a stop worker (review GLO-V02).
STOP_HANDED_ON_TEXT = "Stop requested as the upload program ended; a worker cancels the transfer."

# Workers this process started (the Studio server is long-lived): reap() polls them (8.11).
_CHILDREN = []
_CHILDREN_LOCK = threading.Lock()


# ---------------------------------------------------------------------- test hooks and the guard


class GuardRefused(DataLayerError):
    """The test guard refused something that would reach outside this Mac."""

    kind = "internal"


def guard_active():
    return os.environ.get(GUARD_VARIABLE) == "1"


def fault(name):
    """End the process with code 97 here when a test asks for this fault (design 9.5 and 13)."""
    if os.environ.get(HOOKS_VARIABLE) == "1" and os.environ.get(FAULT_VARIABLE) == name:
        logging.shutdown()
        os._exit(EXIT_FAULT)


def demo_pace():
    """Seconds per file of a demo copy (RAMAN_ARC_DEMO_PACE, default 0.1; the tests use 0). The demo's
    other pauses (the 1.5 s sign-in, the automatic resend delays) scale with it: pace 0, no pause."""
    try:
        pace = float(os.environ.get(PACE_VARIABLE, DEFAULT_DEMO_PACE))
    except ValueError:
        return DEFAULT_DEMO_PACE
    return pace if 0 <= pace <= 10 else DEFAULT_DEMO_PACE


def check_program(argv):
    """Refuse, under the test guard, a program that reaches outside this Mac, opens a window or keeps
    it awake (/usr/bin/ssh, /usr/bin/open, /usr/bin/caffeinate, osascript...)."""
    if guard_active():
        name = os.path.basename(str(argv[0])) if argv else ""
        if name in _GUARDED_PROGRAMS:
            raise GuardRefused("The test guard (%s=1) refuses to run %s" % (GUARD_VARIABLE, name))


def check_spawn(argv):
    """Refuse, under the test guard, to start anything but this program."""
    if guard_active() and list(argv[1:3]) != ["-m", WORKER_MODULE]:
        raise GuardRefused("The test guard (%s=1) starts only %s" % (GUARD_VARIABLE, WORKER_MODULE))


def check_live_globus():
    """Called by the live Globus client before anything else: refused under the test guard."""
    if guard_active():
        raise GuardRefused("The test guard (%s=1) refuses to sign in to Globus" % GUARD_VARIABLE)


def run_program(argv, timeout, **options):
    """subprocess.run of a program, after the guard (argument list, never a shell)."""
    check_program(argv)
    return subprocess.run([str(item) for item in argv], timeout=timeout, **options)


def popen_program(argv, **options):
    """subprocess.Popen of a program, after the guard (argument list, never a shell)."""
    check_program(argv)
    return subprocess.Popen([str(item) for item in argv], **options)


# ---------------------------------------------------------------------- starting workers


def data_layer_folder():
    """The folder that holds the running raman_datafed package (HOME/data_layer when deployed)."""
    return Path(__file__).resolve().parents[1]


def worker_environment(environ=None):
    """The environment of a worker: this one, the data layer first on PYTHONPATH, without
    PYTHONSTARTUP and PYTHONINSPECT."""
    env = dict(os.environ if environ is None else environ)
    for name in ("PYTHONSTARTUP", "PYTHONINSPECT"):
        env.pop(name, None)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(data_layer_folder()) + (os.pathsep + existing if existing else "")
    return env


def worker_argv(home, upload_id, action):
    return [sys.executable, "-m", WORKER_MODULE, "--home", str(home), "--upload", upload_id, "--action", action]


def spawn(home, upload_id, action):
    """Start a detached worker (design 9.2): its own session, no inherited pipe, output to /dev/null."""
    argv = worker_argv(home, upload_id, action)
    check_spawn(argv)
    process = subprocess.Popen(
        argv, cwd=str(home), env=worker_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    with _CHILDREN_LOCK:
        _CHILDREN.append(process)
    return process


def reap():
    """Collect the workers this process started that have ended, so none stays a zombie."""
    with _CHILDREN_LOCK:
        _CHILDREN[:] = [process for process in _CHILDREN if process.poll() is None]


def _quote(value):
    """A shell word in single quotes, always (as the Terminal script shows every path)."""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _safe_text(value):
    text = str(value)
    if not text or any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError("a value of the Terminal script holds a control character")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("a value of the Terminal script is not text") from error
    return text


def terminal_script_text(home, upload_id, attempt, action, folder_name, interpreter=None, data_layer=None):
    """The text of <record>/ssh_upload.command for one attempt of a live SSH upload (design 9.3)."""
    if not arc_records.UPLOAD_ID.fullmatch(str(upload_id)):
        raise ValueError("not an upload id")
    if action not in ("send", "check"):
        raise ValueError("not an action of a Terminal worker")
    attempt = int(attempt)
    if not arc_session.SESSION_NAME.fullmatch(str(folder_name)):
        raise ValueError("not a session folder name")
    home = _safe_text(home)
    interpreter = _safe_text(interpreter or sys.executable)
    data_layer = _safe_text(data_layer or data_layer_folder())
    lines = [
        "#!/bin/bash",
        "# Portable Raman: upload %s to ARC over SSH (upload %s, attempt %d)." % (folder_name, upload_id, attempt),
        "# Written by the data layer for this one attempt. It runs the data layer, which runs /usr/bin/ssh.",
        "# Your VT password and Duo go to ssh only. This file holds no password, and nothing here saves one.",
        "export PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        "unset PYTHONSTARTUP PYTHONINSPECT PYTHONHOME BASH_ENV ENV SSH_ASKPASS SSH_ASKPASS_REQUIRE DISPLAY",
        "export PYTHONPATH=%s" % _quote(data_layer),
        "cd %s || exit 1" % _quote(home),
        "%s -m %s --home %s --upload %s --action %s --terminal --attempt %d" % (
            _quote(interpreter), WORKER_MODULE, _quote(home), upload_id, action, attempt),
        "status=$?",
        "echo",
        'read -r -p "Press Return to close this window. " _',
        "exit $status",
    ]
    return "\n".join(lines) + "\n"


def write_terminal_script(home, record_dir, record, action, attempt):
    """Write <record>/ssh_upload.command anew (mode 0700) for this attempt; returns its path."""
    text = terminal_script_text(home, record["upload_id"], attempt, action, record["session"]["folder"])
    path = Path(record_dir) / arc_records.TERMINAL_SCRIPT
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    arc_records.write_new_file(path, text.encode("utf-8"), 0o700)
    os.chmod(path, 0o700)
    return path


def open_terminal(script):
    """Run /usr/bin/open -a Terminal <script> (Launch Services, like a double-click; no AppleScript).

    Returns (True, "") or (False, why). The test guard raises GuardRefused.
    """
    argv = [OPEN_PROGRAM, "-a", "Terminal", str(script)]
    try:
        done = run_program(argv, OPEN_SECONDS, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return False, "open did not answer within %d s" % OPEN_SECONDS
    except OSError as error:
        return False, error.strerror or str(error)
    if done.returncode != 0:
        why = " ".join((done.stderr or "").split()) or "open ended with code %d" % done.returncode
        return False, why[:300]
    return True, ""


# ---------------------------------------------------------------------- logging


def _frames(error):
    """The exception type and its traceback as file:line:function frames; never the exception's text,
    which may quote a request (design 9.4)."""
    frames = traceback.extract_tb(error.__traceback__) if error.__traceback__ is not None else []
    shown = ["%s:%d:%s" % (os.path.basename(frame.filename), frame.lineno or 0, frame.name) for frame in frames]
    return "%s at %s" % (type(error).__name__, " < ".join(reversed(shown)) or "?")


_MARKERS = tuple(marker.lower() for marker in SECRET_MARKERS)


def _hidden(match):
    """A long run with a long stretch of letters and digits mixed (a token, a code, a verifier) is hidden; a
    UUID, a name made of short parts (a session folder: 1d_x_scan_20261113_101500_123), a long plain word or
    a run of digits alone is not one."""
    text = match.group(0)
    if _UUID.fullmatch(text):
        return text
    longest = max(re.split(r"[_-]", text), key=len)
    if len(longest) >= 16 and any(character.isdigit() for character in longest) and \
            any(character.isalpha() for character in longest):
        return "[hidden]"
    return text


class _SecretFilter(logging.Filter):
    """The second net of worker.log (design 9.4): a line with a secret marker is dropped; a long run shaped
    like a token or a code in any other line is shown as [hidden]."""

    def filter(self, record):
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log call is dropped, not raised
            return False
        if any(marker in text.lower() for marker in _MARKERS) or SECRET_WORDS.search(text):
            return False
        hidden = SECRET_RUN.sub(_hidden, text)
        if hidden != text:
            record.msg, record.args = hidden, None
        return True


class _Formatter(logging.Formatter):
    def formatException(self, exc_info):
        return _frames(exc_info[1]) if exc_info and exc_info[1] is not None else ""


def _setup_logging(record_dir):
    path = Path(record_dir) / arc_records.WORKER_LOG
    if not path.exists():
        with contextlib.suppress(FileExistsError):
            os.close(os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS,
                                                   encoding="utf-8")
    handler.addFilter(_SecretFilter())
    handler.setFormatter(_Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def excepthook(kind, error, trace):
        logging.getLogger(__name__).error("uncaught %s", _frames(error.with_traceback(trace)))

    def thread_excepthook(arguments):
        if arguments.exc_value is not None:
            logging.getLogger(__name__).error("uncaught in thread %s: %s",
                                              getattr(arguments.thread, "name", "?"), _frames(arguments.exc_value))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook
    return handler


# ---------------------------------------------------------------------- the transport interface


class StopRequested(Exception):
    """Raised by WorkerContext.check_stop(): the worker writes "stopped" with the standard message."""


class Outcome:
    """How a transport's run ended (the TRANSPORT INTERFACE above says what each field does)."""

    def __init__(self, state, message=None, error=None, fields=None, check=None):
        self.state = state
        self.message = message
        self.error = error
        self.fields = dict(fields or {})
        self.check = check

    def __repr__(self):
        reason = (self.error or {}).get("reason")
        return "Outcome(%s%s)" % (self.state, ", %s" % reason if reason else "")


class WorkerContext:
    """What a transport gets from the worker (the TRANSPORT INTERFACE above)."""

    def __init__(self, paths, record_dir, record, action, terminal=False, attempt=None):
        self.home = Path(paths.home)
        self.paths = paths
        self.record_dir = Path(record_dir)
        self.upload_id = record["upload_id"]
        self.action = action
        self.transport = record["transport"]
        self.demo = bool(record.get("demo"))
        self.terminal = bool(terminal)
        if attempt is None:
            sub = record.get(self.transport) if isinstance(record.get(self.transport), dict) else {}
            attempt = sub.get("attempt")
        self.attempt = attempt
        self.settings_used = copy.deepcopy(record.get("settings_used") or {})
        self.session_folder = record["session"]["folder"]
        self.payload_dir = arc_payload.payload_folder(self.record_dir) / self.session_folder
        demo_root = (record.get("destination") or {}).get("demo_root")
        self.demo_root = Path(demo_root) if demo_root else None
        resume = record.get("resume")
        self.resume_at = resume.get("at") if isinstance(resume, dict) else None
        self.log = logging.getLogger("raman_datafed.arc_worker.%s" % self.transport)
        self.started_monotonic = time.monotonic()
        self.fault = fault
        # The bytes of arc_remote.sh as they were when this worker started (load_code()); None until then, or
        # when the file could not be read.
        self.remote_script = None
        self._stop = threading.Event()
        self.signal_name = None
        # True once the transport acted on a person's stop (it asked Globus to cancel, or looked for the
        # transfer to cancel it): a stop request still in the record at the end is then not handed on.
        self.stop_tried = False
        self._last_progress = 0.0

    # -- the record

    def read(self):
        return arc_records.read_record(self.record_dir)

    def update(self, mutate):
        return arc_records.update_record(self.record_dir, mutate)

    def set_state(self, state, message=None, event=None, **fields):
        def mutate(record):
            set_state(record, state, message)
            record.update(fields)

        record = self.update(mutate)
        text = event or message
        if text:
            self.event(text, state)
        return record

    def event(self, text, state=None):
        if state is None:
            state = self.read().get("state")
        arc_records.append_event(self.record_dir, state, text)

    def progress(self, files_done=None, bytes_done=None, message=None, force=False):
        now = time.monotonic()
        if not force and now - self._last_progress < PROGRESS_SECONDS:
            return False
        self._last_progress = now

        def mutate(record):
            progress = record.get("progress") if isinstance(record.get("progress"), dict) else {}
            if files_done is not None:
                progress["files_done"] = files_done
            if bytes_done is not None:
                progress["bytes_done"] = bytes_done
            record["progress"] = progress
            if message is not None:
                record["message"] = message

        self.update(mutate)
        return True

    def placed_as(self, name):
        def mutate(record):
            destination = record["destination"]
            destination["placed_as"] = name
            destination["final_path"] = destination["base"] + "/" + name
            if destination.get("globus_base"):
                destination["globus_final_path"] = destination["globus_base"].rstrip("/") + "/" + name
            if destination.get("demo_root"):
                destination["local_final_path"] = arc_records.local_path(destination["demo_root"],
                                                                         destination["final_path"])

        return self.update(mutate)

    # -- stop requests

    def request_stop(self, signal_name=None):
        if signal_name and self.signal_name is None:
            self.signal_name = signal_name
        self._stop.set()

    def stop_requested(self):
        return self._stop.is_set()

    def check_stop(self):
        if self._stop.is_set():
            raise StopRequested()

    def wait(self, seconds):
        return self._stop.wait(max(0.0, seconds))

    # -- the snapshot

    def payload_digests(self):
        return arc_payload.digests(self.record_dir)

    def check_payload(self):
        return arc_payload.check_payload(self.record_dir, self.session_folder)

    # -- texts and results

    def text(self, text):
        return arc_records.demo_text(text, self.demo)

    def error(self, reason, /, **values):
        return arc_records.worker_error(reason, demo=self.demo, **values)

    def failed(self, reason, /, **values):
        error = self.error(reason, **values)
        return Outcome("failed", self.text(error["message"]), error)

    def outcome(self, state, message=None, error=None, **fields):
        return Outcome(state, message, error, fields)


# ---------------------------------------------------------------------- the worker


def _parser():
    parser = argparse.ArgumentParser(prog="python -m %s" % WORKER_MODULE,
                                     description="Work on one upload to ARC (started by the arc-* commands).")
    parser.add_argument("--home", required=True, metavar="HOME")
    parser.add_argument("--upload", required=True, metavar="ID")
    parser.add_argument("--action", required=True, choices=ACTIONS)
    parser.add_argument("--terminal", action="store_true", help="a live SSH worker in a Terminal window")
    parser.add_argument("--attempt", type=int, metavar="N", help="the attempt a Terminal worker was written for")
    return parser


def _take_worker_lock(record_dir):
    """The open worker.lock, held; None when another process holds it (3 tries over 2 s)."""
    handle = os.open(str(Path(record_dir) / arc_records.WORKER_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    for attempt in range(LOCK_TRIES):
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if attempt < LOCK_TRIES - 1:
                time.sleep(LOCK_SPAN_SECONDS / (LOCK_TRIES - 1))
    os.close(handle)
    return None


class _NotItsTurn(Exception):
    """Raised inside the claim of a record: this worker may not work on it now (the text says why)."""


def _not_its_turn(record, action, terminal, attempt):
    """Why this worker may not work on the record now, or None."""
    if not arc_records.is_current(record):
        return "the record was made by another version"
    transport = record.get("transport")
    state = record.get("state")
    live_ssh = transport == "ssh" and not record.get("demo")
    if terminal != live_ssh:
        return "a live SSH upload runs in a Terminal window; every other upload runs detached"
    if terminal:
        ssh = record.get("ssh") if isinstance(record.get("ssh"), dict) else {}
        if attempt is None or attempt != ssh.get("attempt"):
            return "earlier attempt"
    if action == "send":
        wanted = "waiting_for_terminal" if terminal else "queued"
        return None if state == wanted else "the upload is %s" % state
    if action == "stop":
        globus = record.get("globus") if isinstance(record.get("globus"), dict) else {}
        if transport != "globus":
            return "only a Globus upload has a stop worker"
        if not (globus.get("submission_id") or globus.get("task_id")):
            return "Globus has no request of this upload to settle"
    display_state = arc_records.display(record, False)[0]
    if action not in arc_records.actions(record, display_state, False):
        return "the upload is %s" % state
    return None


def _say(terminal, text):
    """A line in the Terminal window (a detached worker's stderr is /dev/null)."""
    if terminal and text:
        with contextlib.suppress(OSError, ValueError):
            print(text, file=sys.stderr, flush=True)


def _discard_typed_input():
    """Drop what was typed in the Terminal window while the upload ran. ssh reads the terminal only while it
    asks, so a Return pressed during the copy stays queued, and would answer the window's last question
    ("Press Return to close this window") before the result could be read."""
    try:
        import termios

        termios.tcflush(0, termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 - termios.error (not a terminal), ImportError, OSError: nothing to drop
        pass


def load_code():
    """Import every module a worker may need, and read arc_remote.sh, before the first step: a new install
    of the data layer while an upload runs (macos-native/build_macos.sh) then cannot mix two versions in one
    run. Returns the bytes of arc_remote.sh (WorkerContext.remote_script), or None when it cannot be read
    (the SSH transport then says so when it needs it)."""
    for name in TRANSPORT_MODULES:
        importlib.import_module(name)
    try:
        return sys.modules["raman_datafed.arc_ssh"].remote_script()
    except OSError:
        return None


def _write_stop_request(record_dir):
    """requests.stop_utc, written by a stop worker that could not take the upload: the worker that holds it
    (a send or a check that was just starting when Stop was pressed) stops within a second."""
    def mutate(current):
        requests = current.get("requests") if isinstance(current.get("requests"), dict) else {}
        requests["stop_utc"] = utc_now()
        current["requests"] = requests

    try:
        arc_records.update_record(record_dir, mutate)
    except DataLayerError as error:
        log.error("the stop request could not be written: %s", type(error).__name__)


class WorkerEnding(BaseException):
    """Raised by the signal handler in the middle of the work: this process must end now (SIGTERM, as at a
    logout, restart or shutdown; SIGHUP or SIGINT of a detached worker). It is not a person's stop, so
    nothing is stopped or cancelled and no final state is written: the listing shows the upload as not
    watched or interrupted, and Check status or Retry same request goes on from there (design 7.5).
    A BaseException, so that no `except Exception` of a transport takes it for a failure."""

    def __init__(self, signal_name):
        super().__init__(signal_name)
        self.signal_name = signal_name


class _Run:
    """One worker's run: locks, signals, the stop poller, caffeinate, and applying the outcome."""

    def __init__(self, paths, record_dir, action, terminal, attempt):
        self.paths = paths
        self.record_dir = Path(record_dir)
        self.action = action
        self.terminal = terminal
        self.attempt = attempt
        self.ctx = None
        self.caffeinate = None
        self.poller = None
        self.done = threading.Event()
        # "starting" (until the work begins), "working" (a signal that ends the process raises WorkerEnding),
        # "finishing" (the outcome is being written: such a signal only marks the end).
        self.phase = "starting"
        self.ending = None
        self.check_stopped = False
        # A person's stop that came as this worker ended, left to a stop worker (review GLO-V02).
        self.stop_handover = False

    # -- signals and the stop poller

    def _on_signal(self, number, _frame):
        """A person's stop is the Stop button (requests.stop_utc) or, in a Terminal window, Ctrl-C (SIGINT)
        and closing the window (SIGHUP). Every other signal (SIGTERM, or SIGHUP and SIGINT of a detached
        worker) ends the process at once, without cancelling anything (design I8)."""
        name = signal.Signals(number).name
        if self.terminal and number in (signal.SIGINT, signal.SIGHUP):
            if self.ctx is not None:
                self.ctx.request_stop(name)
            else:
                self.ending = self.ending or name
            return
        self.ending = self.ending or name
        if self.phase == "working":
            raise WorkerEnding(name)

    def install_signals(self):
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, self._on_signal)

    def _poll_stop(self):
        if self.action == "stop":
            # A stop worker is the stop: the request that started it is not one for it to stop on.
            return
        while not self.done.wait(STOP_POLL_SECONDS):
            try:
                record = arc_records.read_record(self.record_dir)
            except DataLayerError:
                continue
            requests = record.get("requests") if isinstance(record.get("requests"), dict) else {}
            if requests.get("stop_utc"):
                self.ctx.request_stop()
                return

    def start_poller(self):
        self.poller = threading.Thread(target=self._poll_stop, name="stop-requests", daemon=True)
        self.poller.start()

    def start_caffeinate(self):
        if self.ctx.demo:
            return
        try:
            self.caffeinate = popen_program([CAFFEINATE, "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (GuardRefused, OSError) as error:
            log.info("caffeinate not started: %s", type(error).__name__)

    def stop_helpers(self):
        self.done.set()
        if self.caffeinate is not None:
            with contextlib.suppress(OSError):
                self.caffeinate.terminate()
            with contextlib.suppress(Exception):
                self.caffeinate.wait(timeout=5)

    # -- the work

    def prepare(self):
        """State "preparing": the snapshot, made now or checked again (design 4.6)."""
        ctx = self.ctx
        record = ctx.read()
        ctx.set_state("preparing", ctx.text(arc_records.MESSAGES["preparing"]))
        arc_payload.remove_temporaries(self.record_dir)
        payload = arc_payload.payload_folder(self.record_dir)
        if payload.exists():
            try:
                summary = arc_payload.keep(self.record_dir, ctx.session_folder)
            except arc_payload.PayloadError:
                if arc_records.payload_made(record):
                    raise
                # Never recorded, so never sent: made again below.
                log.info("an unrecorded snapshot did not match its lists; it is made again")
                arc_payload.remove_tree(payload)
            else:
                ctx.update(lambda current: current.update(
                    payload=summary, progress={"files": summary["files"], "files_done": 0, "bytes": summary["bytes"],
                                               "bytes_done": 0}))
                log.info("the snapshot was checked against its own lists and kept")
                return None
        elif arc_records.payload_made(record):
            raise arc_payload.PayloadError("payload_changed", "The snapshot of this upload is missing")
        return self._snapshot(record)

    def _snapshot(self, record):
        ctx = self.ctx
        used = ctx.settings_used
        session = record["session"]
        folder = Path(self.paths.scan_sessions) / ctx.session_folder
        if not arc_session.is_session_entry(self.paths.scan_sessions, ctx.session_folder):
            error = arc_records.refusal("unknown_session", folder=ctx.session_folder)
            return Outcome("failed", ctx.text(str(error)), arc_records.error_of_refusal(error))
        declared = bool(session.get("kind_declared_by_uploader"))
        check = arc_session.check(self.paths, folder, used.get("settle_seconds", 60),
                                  bool(record.get("controller_idle_used")), full=True, declare_real=declared,
                                  commands=arc_session.running_commands())
        refused = check.refusal
        area = None
        if refused is None:
            area, refused = arc_session.check_policy(check, record["demo"], bool(used.get("include_simulated")),
                                                     declared)
        if refused is not None:
            return Outcome("failed", ctx.text(str(refused)), arc_records.error_of_refusal(refused))
        session_id = check.session.get("session_id")
        if session_id != session.get("session_id") or area != record["destination"]["area"]:
            what = "it is another session now" if session_id != session.get("session_id") else \
                "it no longer records the same about whether it was simulated"
            error = ctx.error("session_changed", what=what)
            return Outcome("failed", ctx.text(error["message"]), error)
        summary = check.summary(declared)

        def refresh(current):
            current["session"] = summary
            current["warnings"] = list(check.warnings)

        ctx.update(refresh)
        record = ctx.read()
        payload = arc_payload.snapshot(self.record_dir, self.paths.home, check, record, fault=fault)
        ctx.update(lambda current: current.update(
            payload=payload, progress={"files": payload["files"], "files_done": 0, "bytes": payload["bytes"],
                                       "bytes_done": 0}))
        ctx.event("Copied %d files (%d bytes) into the upload copy on this Mac and listed them in the manifest."
                  % (payload["files"], payload["bytes"]), "preparing")
        return None

    def entry_point(self):
        module = importlib.import_module("raman_datafed.arc_%s" % self.ctx.transport)
        return getattr(module, self.action)

    def transfer_may_run(self, record=None):
        """True when Globus may still run a transfer of this upload (arc_globus.may_run): only a worker that
        cancels it may stop such an upload."""
        if self.ctx.transport != "globus":
            return False
        return importlib.import_module("raman_datafed.arc_globus").may_run(record or self.ctx.read())

    def work(self):
        ctx = self.ctx
        # Before the first step: every module this run may need, and the ARC side of an SSH upload.
        ctx.remote_script = load_code()
        if self.action == "send":
            early = self.prepare()
            if early is not None:
                return early
            if not self.transfer_may_run():
                # Nothing of this upload runs on Globus: a stop pressed while the worker started ends it here.
                # Otherwise the transport settles the stop: it signs in, finds the transfer and cancels it
                # (review COR-04).
                ctx.check_stop()
        outcome = self.entry_point()(ctx)
        if not isinstance(outcome, Outcome):
            raise TypeError("a transport returned %s, not an Outcome" % type(outcome).__name__)
        return outcome

    # -- the end

    def stopped_outcome(self, record):
        """"stopped" with the standard message, from what the record says was sent. A check ends at once
        and leaves the upload as it was (Check status changes nothing when it is stopped)."""
        state = record.get("state")
        if self.action == "check" or state not in arc_records.WORKING_STATES:
            self.check_stopped = self.action == "check"
            return Outcome(state)
        globus = record.get("globus") if isinstance(record.get("globus"), dict) else {}
        if arc_records.may_be_on_arc(record) or globus.get("submission_id") or state in ("placing", "conflict"):
            text = arc_records.message("stopped", self.ctx.demo, incoming=_incoming_shown(record))
        else:
            text = arc_records.message("stopped_nothing_sent", self.ctx.demo)
        return Outcome("stopped", text)

    def end_at_once(self, signal_name):
        """The process must end (WorkerEnding): nothing is stopped or cancelled, and no final state is
        written; only `worker` is cleared, and an event says what happened. Exit code 128 + the signal."""
        text = ENDED_BY_SIGNAL_TEXT.format(signal=signal_name)
        log.info("ended by %s: nothing was stopped or cancelled", signal_name)
        with contextlib.suppress(Exception):
            record = arc_records.update_record(self.record_dir, lambda current: current.update(worker=None))
            arc_records.append_event(self.record_dir, record.get("state"), text)
        _say(self.terminal, text)
        number = getattr(signal, signal_name, None)
        return 128 + int(number) if number is not None else EXIT_ENDED

    def internal(self, error):
        """The outcome of an unexpected exception: failed / internal; a check or stop worker leaves a
        state that is not a working state as it was, and says why."""
        ctx = self.ctx
        found = ctx.error("internal", type=type(error).__name__, folder=str(self.record_dir))
        state = ctx.read().get("state")
        if self.action in ("check", "stop") and state not in arc_records.WORKING_STATES:
            if state == "verified":
                # The check could not look: the copy on ARC is not in question (review REG-01).
                return Outcome(state, None, check=arc_records.check_note(ctx.text(found["message"]), "internal"))
            return Outcome(state, ctx.text(found["message"]))
        return Outcome("failed", ctx.text(found["message"]), found)

    def settle(self, outcome, state):
        """The outcome as it may be written over `state` (TRANSITIONS); a closed Terminal window."""
        ctx = self.ctx
        if ctx.signal_name == "SIGHUP" and self.terminal and outcome.state not in ("verified", "conflict"):
            if self.action == "send":
                # The Terminal window was closed: ssh went with it.
                outcome = ctx.failed("terminal_closed")
            else:
                # Check status whose window was closed: it did not finish, and the upload stays as it was.
                self.check_stopped = True
                outcome = Outcome(state)
        elif ctx.signal_name == "SIGINT" and outcome.state == "failed" and state in arc_records.WORKING_STATES:
            # Ctrl-C in Terminal: what failed with it failed because of the stop.
            outcome = self.stopped_outcome(ctx.read())
        if outcome.state == state or (state, outcome.state) in arc_records.TRANSITIONS:
            return outcome
        log.error("an outcome %s cannot follow the state %s", outcome.state, state)
        if state == "verified":
            # A check that ended in an error (a refusal it met, a changed snapshot): it could not look, and the
            # upload stays on ARC (review REG-01).
            reason = outcome.error.get("reason") if isinstance(outcome.error, dict) else None
            return Outcome(state, None, check=arc_records.check_note(outcome.message, reason))
        if state in arc_records.WORKING_STATES:
            found = ctx.error("internal", type="TransitionError", folder=str(self.record_dir))
            return Outcome("failed", ctx.text(found["message"]), found)
        return Outcome(state, outcome.message)

    def finish(self, outcome):
        """Write the outcome into the record (one read-modify-write); returns the exit code.

        A verified upload is written as verified first, with its snapshot still kept; only then is the
        snapshot deleted and the session folder compared with what was sent (a second write). So a crash
        in between leaves an upload that is verified, never one in a working state whose snapshot is gone.
        """
        ctx = self.ctx
        state = ctx.read().get("state")
        outcome = self.settle(outcome, state)

        def mutate(record):
            before = record.get("state")
            set_state(record, outcome.state, outcome.message)
            if outcome.state == "failed":
                record["error"] = outcome.error or record.get("error")
            elif outcome.state != before:
                record["error"] = None
            if outcome.state != "conflict" and outcome.state != before:
                record["conflict_at"] = None
            record.update(outcome.fields)
            if outcome.check is not None:
                # Every entry has the same keys: a check given without "looked" looked (review REG-01).
                entry = dict({"looked": True, "fix": None}, **outcome.check, utc=utc_now())
                record["checks"] = list(record.get("checks") or []) + [entry]
            if outcome.state not in arc_records.WORKING_STATES:
                record["resume"] = None
            requests = record.get("requests") if isinstance(record.get("requests"), dict) else {}
            self.stop_handover = self.stop_left_over(record, requests)
            if self.stop_handover:
                # The request stays, and the listing knows that a stop worker comes (as arc-stop writes it).
                record["worker"] = {"pid": None, "action": "stop", "started_utc": None, "terminal": False}
            else:
                requests["stop_utc"] = None
                record["worker"] = None
            record["requests"] = requests

        record = ctx.update(mutate)
        text = record.get("message")
        if text and (outcome.message is not None or outcome.state != state):
            arc_records.append_event(self.record_dir, record["state"], text)
        if self.stop_handover:
            log.info("a stop came as the worker ended; a stop worker cancels the transfer")
            arc_records.append_event(self.record_dir, record["state"], STOP_HANDED_ON_TEXT)
        if outcome.check is not None:
            # A check that could not look says so in the history with what to do (review REG-01).
            arc_records.append_event(self.record_dir, record["state"], " ".join(
                part for part in (outcome.check.get("message") or "Checked.", outcome.check.get("fix")) if part))
        if self.check_stopped:
            arc_records.append_event(self.record_dir, record["state"], "The check was not finished: %s." % (
                "its Terminal window was closed" if ctx.signal_name == "SIGHUP" else "it was stopped"))
        kept = (record.get("payload") if isinstance(record.get("payload"), dict) else {}).get("kept") is True
        if record["state"] == "verified" and (kept or arc_payload.payload_folder(self.record_dir).exists()):
            fault("verified_before_delete")
            record = self.delete_snapshot()
        if self.terminal:
            # A Return typed while the upload ran must not answer the window's last question.
            _discard_typed_input()
            _say(True, "")
            for line in self.final_lines(record, outcome):
                _say(True, line)
        if self.check_stopped:
            return EXIT_INTERRUPTED if ctx.signal_name == "SIGINT" else EXIT_ENDED
        if record["state"] == "verified":
            return EXIT_ENDED if outcome.check is not None and outcome.check.get("ok") is False else EXIT_VERIFIED
        if ctx.signal_name == "SIGINT" and record["state"] == "stopped":
            return EXIT_INTERRUPTED
        return EXIT_ENDED

    def stop_left_over(self, record, requests):
        """True when a person's stop came as this worker ended (signing out of Globus takes up to a few
        seconds), after its transport had decided to leave a transfer running on Globus, and nothing acted on
        it: a stop worker then signs in and cancels the transfer, as arc-stop does for an upload that no
        program works on (review GLO-V02). `record` is the record with the outcome written."""
        if self.action == "stop" or not requests.get("stop_utc") or self.ctx.stop_tried:
            return False
        if record.get("transport") != "globus" or not self.transfer_may_run(record):
            return False
        return "stop" in arc_records.actions(record, arc_records.display(record, False)[0], False)

    def hand_over_the_stop(self):
        """Start the stop worker of stop_left_over(), once this worker has let go of the upload."""
        try:
            spawn(self.paths.home, self.ctx.upload_id, "stop")
        except Exception as error:  # noqa: BLE001 - the request stays in the record; Stop is still offered
            log.error("the stop worker could not be started: %s", type(error).__name__)

    def delete_snapshot(self):
        """The upload is verified: delete its snapshot (the copies of its lists stay in the record folder),
        compare the session folder with what was sent (notes and warnings), and write that. Also done by a
        later worker of a verified upload whose snapshot is still there (a crash before it was deleted)."""
        ctx = self.ctx
        manifest = arc_payload.read_manifest(self.record_dir)
        try:
            arc_payload.delete(self.record_dir)
        except OSError as error:
            log.warning("the upload copy could not be deleted: %s", type(error).__name__)
        notes, warnings = [], []
        if manifest is not None:
            notes, warnings = arc_payload.after_check(Path(self.paths.scan_sessions) / ctx.session_folder, manifest,
                                                      demo=ctx.demo)

        def mutate(record):
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            payload["kept"] = arc_payload.payload_folder(self.record_dir).exists()
            record["payload"] = payload
            for key, found in (("notes", notes), ("warnings", warnings)):
                kept = list(record.get(key) or [])
                record[key] = kept + [sentence for sentence in found if sentence not in kept]

        return ctx.update(mutate)

    def final_lines(self, record, outcome):
        """What a Terminal window says at the end: a check's own result (whether it found the copy or not, or
        why it could not look, with what to do), or the upload's message, and the fix of a failure."""
        if outcome.check is not None:
            lines = [outcome.check.get("message")]
            if not arc_records.looked(outcome.check):
                lines.append(outcome.check.get("fix"))
            elif outcome.check.get("ok") is False:
                lines.append(arc_records.CHECK_FAILED_TERMINAL)
            return lines
        if self.check_stopped:
            return [arc_records.CHECK_STOPPED_TERMINAL]
        lines = [record.get("message")]
        if record["state"] == "failed" and isinstance(record.get("error"), dict):
            lines.append(record["error"].get("fix"))
        return lines


def _incoming_shown(record):
    """The incoming folder as the person knows it: on ARC, or in the demo folder on this Mac."""
    destination = record.get("destination") or {}
    incoming = destination.get("incoming_path")
    if record.get("demo") and destination.get("demo_root") and incoming:
        return arc_records.local_path(destination["demo_root"], incoming)
    return incoming


def run_worker(home, upload_id, action, terminal=False, attempt=None, program=False):
    """The worker's whole run; returns the exit code.

    `program` (main()) also sends the log to worker.log, installs the signal handlers and the
    exception hooks; tests call this in their own process without it.
    """
    home = Path(home).expanduser()
    paths = Paths(home.resolve() if home.exists() else home)
    if not arc_records.UPLOAD_ID.fullmatch(str(upload_id)):
        _say(True, "Not an upload id: %r" % (upload_id,))
        return EXIT_USAGE
    record_dir = arc_records.record_folder(paths, upload_id)
    if not record_dir.is_dir() or record_dir.is_symlink():
        # A record folder that is a link is never worked on (as the commands never act on one).
        _say(True, "There is no upload %s on this Mac." % upload_id)
        return EXIT_USAGE
    handle = _take_worker_lock(record_dir)
    if handle is None:
        if action == "stop":
            # Stop was pressed while a send or check worker was starting, and that worker took the upload.
            _write_stop_request(record_dir)
        _say(terminal, "Another program is working on this upload right now.")
        return EXIT_NOT_ITS_TURN
    run = _Run(paths, record_dir, action, terminal, attempt)
    try:
        if program:
            _setup_logging(record_dir)
            run.install_signals()

        def claim(current):
            why = _not_its_turn(current, action, terminal, attempt)
            if why is not None:
                raise _NotItsTurn(why)
            current["worker"] = {"pid": os.getpid(), "action": action, "started_utc": utc_now(),
                                 "terminal": bool(terminal)}

        try:
            # One step under record.lock: a command that changes the record at this moment (arc-stop of
            # an upload whose worker it did not see yet) either comes first or sees this worker.
            record = arc_records.update_record(record_dir, claim)
        except _NotItsTurn as error:
            why = str(error)
            log.info("not this worker's turn (%s): %s", action, why)
            _say(terminal, EARLIER_ATTEMPT_TEXT if why == "earlier attempt" else "Nothing to do: %s." % why)
            return EXIT_NOT_ITS_TURN
        except DataLayerError as error:
            log.error("the record cannot be read, or its lock was held too long: %s", type(error).__name__)
            return EXIT_NOT_ITS_TURN
        run.ctx = WorkerContext(paths, record_dir, record, action, terminal, attempt)
        if run.ending:
            # A signal came while the upload was being claimed.
            return run.end_at_once(run.ending)
        log.info("worker %d started: %s, %s upload %s (%s)", os.getpid(), action, record["transport"], upload_id,
                 "demo" if record.get("demo") else "live")
        run.start_poller()
        run.start_caffeinate()
        try:
            try:
                run.phase = "working"
                if run.ending:
                    raise WorkerEnding(run.ending)
                outcome = run.work()
            finally:
                # From here on, a signal that ends the process only marks the end: the outcome is written.
                run.phase = "finishing"
        except WorkerEnding as ending:
            return run.end_at_once(ending.signal_name)
        except StopRequested:
            outcome = run.stopped_outcome(run.ctx.read())
        except arc_payload.PayloadError as error:
            found = run.ctx.error(error.reason, **error.values)
            outcome = Outcome("failed", run.ctx.text(found["message"]), found)
            log.info("%s (%s)", error.reason, error.detail or error)
        except ArcError as error:
            outcome = Outcome("failed", run.ctx.text(str(error)), arc_records.error_of_refusal(error))
        except Exception as error:  # noqa: BLE001 - every end is recorded
            log.error("the upload program failed: %s", _frames(error))
            outcome = run.internal(error)
        log.info("worker ends: %r", outcome)
        return run.finish(outcome)
    except Exception as error:  # noqa: BLE001 - the end could not be written; the lock still goes
        log.error("the end of the run could not be written: %s", _frames(error))
        with contextlib.suppress(Exception):
            arc_records.update_record(record_dir, lambda current: current.update(worker=None))
        return EXIT_ENDED
    finally:
        run.stop_helpers()
        os.close(handle)
        if run.stop_handover:
            run.hand_over_the_stop()


def main(argv=None):
    try:
        args = _parser().parse_args(argv)
    except SystemExit as error:
        return EXIT_USAGE if error.code else 0
    if args.terminal and args.attempt is None:
        print("--terminal needs --attempt", file=sys.stderr)
        return EXIT_USAGE
    # Every file the worker makes is the user's alone (the snapshot sets its own modes).
    os.umask(0o077)
    return run_worker(args.home, args.upload, args.action, args.terminal, args.attempt, program=True)


if __name__ == "__main__":
    # Run inside the module of its real name, so that the transports and this program share one
    # copy of it (python -m runs this file as __main__).
    from raman_datafed import arc_worker as _worker

    sys.exit(_worker.main())
