"""A stand-in for /usr/bin/ssh in the tests of uploads to ARC over SSH (design 19.1).

    python fake_ssh.py [ssh options] -- HOST "REMOTE COMMAND"

Nothing here uses a network. The program checks the command line the data layer gives ssh, maps
/projects/ to the folder RAMAN_FAKE_SSH_ROOT names, and runs the remote command the way ARC's
login shell does: /bin/bash -c "<command>", which decodes and runs the real arc_remote.sh. Its
stdin (the tar stream) is passed on, and the script's stdout comes back with the paths mapped
back, so the data layer sees ARC's paths. Standard library only.

Variables (RAMAN_FAKE_SSH_ROOT is required):
  RAMAN_FAKE_SSH_ROOT         the folder that stands in for ARC's file system
  RAMAN_FAKE_SSH_LOG          a file: one JSON line per run is appended (the argv, or what was wrong)
  RAMAN_FAKE_SSH_SAVE         a file: the stream that arrived on stdin is written into it
  RAMAN_FAKE_SSH_EXIT         end at once with this code and run nothing (255: signing in failed)
  RAMAN_FAKE_SSH_REFUSE       the host does not run commands: end with code 1 and run nothing
  RAMAN_FAKE_SSH_BANNER       a line a remote .bashrc prints on stdout before the command runs
  RAMAN_FAKE_SSH_CUT_AFTER    pass on this many bytes of the stream, then cut the connection (255)
  RAMAN_FAKE_SSH_FLIP_AT      flip the lowest bit of the stream's byte at this offset
  RAMAN_FAKE_SSH_STALL_AFTER  pass on this many bytes, then wait until a signal ends the program
                              (or 60 s pass: a test that failed never leaves it running)
  RAMAN_FAKE_SSH_PATH         the PATH of the remote shell (default: this Mac's system folders)
  RAMAN_FAKE_SSH_HOLD         wait this many seconds (60 at most) before the command runs, as ssh
                              does while it waits for the password; a signal ends the wait
  RAMAN_FAKE_SSH_HOLD_AFTER   pass on the remote script's lines up to the first one that starts with
                              this text, then hold the rest until a signal ends the program (or 60 s)
  RAMAN_FAKE_SSH_LIKE_SSHD    "1": on a signal or a cut, leave the remote script running with its
                              pipes closed, as sshd does with a command that has no terminal (the
                              script goes on until its next write), instead of killing it

SIGTERM, SIGINT and SIGHUP end the program at once with code 255, and the remote script with it
(unless RAMAN_FAKE_SSH_LIKE_SSHD is set), as when ssh is ended or its Terminal window is closed.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

COMMAND = re.compile(
    r"^bash -c \"\$\(printf %s '([A-Za-z0-9+/=]+)' \| base64 --decode\)\" raman_arc_upload "
    r"(upload|upload-copy|check) '(/[A-Za-z0-9._/-]+)' '([A-Za-z0-9._-]+)' '(u[0-9a-f]{12})' '([A-Za-z0-9._-]+)'$")
# Options of ssh that take a value.
WITH_VALUE = ("-o", "-l", "-e", "-p", "-i", "-F", "-J", "-b", "-c", "-m", "-E")
REMOTE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
CUT_WAIT_SECONDS = 10
STALL_SECONDS = 60

_child = None


def _log(entry):
    path = os.environ.get("RAMAN_FAKE_SSH_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def _say(text):
    """What ssh itself would print in the Terminal window: into <log>.stderr when there is a log."""
    path = os.environ.get("RAMAN_FAKE_SSH_LOG")
    if path:
        with open(path + ".stderr", "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        sys.stderr.write(text + "\n")


def _refuse(why):
    _log({"error": why, "argv": sys.argv[1:]})
    _say("fake ssh: %s" % why)
    sys.exit(255)


def _number(name):
    value = os.environ.get(name)
    return int(value) if value else None


def _like_sshd():
    return os.environ.get("RAMAN_FAKE_SSH_LIKE_SSHD") == "1"


def _end_child(number=signal.SIGKILL):
    if _like_sshd():
        # sshd closes the pipes of a command without a terminal and leaves it running: ending this
        # program closes them.
        return
    if _child is not None and _child.poll() is None:
        try:
            os.killpg(_child.pid, number)
        except OSError:
            pass


def _on_signal(number, _frame):
    _end_child()
    os._exit(255)


def parse(argv):
    """(options, host, command) of an ssh command line; refuses anything else."""
    options, index = [], 0
    while index < len(argv) and argv[index] != "--":
        if not argv[index].startswith("-"):
            _refuse("the host comes before --")
        if argv[index] in WITH_VALUE:
            if index + 1 >= len(argv):
                _refuse("%s without its value" % argv[index])
            options.append([argv[index], argv[index + 1]])
            index += 2
        else:
            options.append([argv[index], None])
            index += 1
    rest = argv[index + 1:]
    if index >= len(argv) or len(rest) != 2:
        _refuse("expected: options -- HOST COMMAND")
    return options, rest[0], rest[1]


class Pump:
    """Passes the stream on from stdin to the remote script, with the faults the variables ask for."""

    def __init__(self, child, stage, receiving, host):
        self.child = child
        self.stage = stage
        self.receiving = receiving
        self.host = host
        self.cut_after = _number("RAMAN_FAKE_SSH_CUT_AFTER")
        self.flip_at = _number("RAMAN_FAKE_SSH_FLIP_AT")
        self.stall_after = _number("RAMAN_FAKE_SSH_STALL_AFTER")
        self.save = os.environ.get("RAMAN_FAKE_SSH_SAVE")
        self.cut = False
        self.offset = 0

    def _write(self, data, saved):
        if saved is not None:
            saved.write(data)
        if data:
            self.child.stdin.write(data)
            self.child.stdin.flush()
        self.offset += len(data)

    def _cut_now(self):
        """The connection drops: once tar has begun, the remote script ends in the middle of the stream."""
        self.receiving.wait(CUT_WAIT_SECONDS)
        deadline = time.monotonic() + CUT_WAIT_SECONDS
        while time.monotonic() < deadline and not (os.path.isdir(self.stage) and os.listdir(self.stage)):
            time.sleep(0.02)
        self.cut = True
        if _like_sshd():
            # Nothing more reaches this Mac; the script reads the end of its stdin and goes on.
            _say("fake ssh: Connection to %s closed by remote host." % self.host)
            os._exit(255)
        _end_child()

    def run(self):
        saved = open(self.save, "wb") if self.save else None
        try:
            while True:
                data = os.read(0, 65536)
                if not data:
                    break
                if self.flip_at is not None and self.offset <= self.flip_at < self.offset + len(data):
                    index = self.flip_at - self.offset
                    data = data[:index] + bytes([data[index] ^ 1]) + data[index + 1:]
                if self.cut_after is not None and self.offset + len(data) >= self.cut_after:
                    self._write(data[:self.cut_after - self.offset], saved)
                    self._cut_now()
                    return
                if self.stall_after is not None and self.offset + len(data) >= self.stall_after:
                    self._write(data[:self.stall_after - self.offset], saved)
                    threading.Event().wait(STALL_SECONDS)
                    _end_child()
                    os._exit(255)
                self._write(data, saved)
        except (BrokenPipeError, OSError):
            pass
        finally:
            if saved is not None:
                saved.close()
            try:
                self.child.stdin.close()
            except OSError:
                pass


def main():
    global _child
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, _on_signal)
    root = os.environ.get("RAMAN_FAKE_SSH_ROOT", "")
    if not root or "'" in root or " " in root or not os.path.isdir(root):
        _refuse("RAMAN_FAKE_SSH_ROOT must name a folder without spaces or quotes")
    options, host, command = parse(sys.argv[1:])
    match = COMMAND.fullmatch(command)
    _log({"argv": sys.argv[1:], "host": host, "options": options,
          "mode": match.group(2) if match else None, "arguments": list(match.groups()[2:]) if match else None})
    hold = _number("RAMAN_FAKE_SSH_HOLD")
    if hold:
        threading.Event().wait(min(hold, STALL_SECONDS))
    code = _number("RAMAN_FAKE_SSH_EXIT")
    if code is not None:
        _say("fake ssh: Permission denied (keyboard-interactive).")
        sys.exit(code)
    if os.environ.get("RAMAN_FAKE_SSH_REFUSE"):
        _say("This service allows file copies only.")
        sys.exit(1)
    if match is None:
        _refuse("the remote command is not the one of the data layer")
    if not match.group(3).startswith("/projects/"):
        _refuse("the ARC folder is not under /projects/")
    mapped = command[:match.start(3)] + root + command[match.start(3):]
    stage = root + match.group(3) + "/_incoming/" + match.group(4) + "." + match.group(5)
    log_path = os.environ.get("RAMAN_FAKE_SSH_LOG")
    errors = open(log_path + ".stderr", "ab") if log_path else None
    env = {"PATH": os.environ.get("RAMAN_FAKE_SSH_PATH") or REMOTE_PATH, "HOME": root}
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        if name in os.environ:
            env[name] = os.environ[name]
    out = sys.stdout.buffer
    banner = os.environ.get("RAMAN_FAKE_SSH_BANNER")
    if banner:
        out.write(banner.encode("utf-8") + b"\n")
        out.flush()
    _child = subprocess.Popen(["/bin/bash", "-c", mapped], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=errors, env=env, cwd=root, start_new_session=True, close_fds=True)
    receiving = threading.Event()
    pump = Pump(_child, stage, receiving, host)
    feeder = threading.Thread(target=pump.run, daemon=True)
    feeder.start()
    back = (root + "/projects/").encode("utf-8")
    hold_after = os.environ.get("RAMAN_FAKE_SSH_HOLD_AFTER", "").encode("utf-8")
    for line in iter(_child.stdout.readline, b""):
        if line.startswith(b"RAMAN STEP receive"):
            receiving.set()
        try:
            out.write(line.replace(back, b"/projects/"))
            out.flush()
        except BrokenPipeError:
            _end_child()
            os._exit(255)
        if hold_after and line.startswith(hold_after):
            # The rest of the script's lines never arrive (until a signal ends this program).
            threading.Event().wait(STALL_SECONDS)
            _end_child()
            os._exit(255)
    status = _child.wait()
    if errors is not None:
        errors.close()
    if pump.cut:
        _say("fake ssh: Connection to %s closed by remote host." % host)
        os._exit(255)
    feeder.join(5)
    os._exit(status if status >= 0 else 255)


if __name__ == "__main__":
    main()
