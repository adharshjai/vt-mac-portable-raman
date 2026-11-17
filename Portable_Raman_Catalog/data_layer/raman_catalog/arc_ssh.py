"""Uploads to ARC over SSH, live and demo (PROTOCOL.md 5.6; design sections 11 and 12).

One attempt is one connection. /usr/bin/ssh runs the remote script arc_remote.sh, which travels
with the command as base64, so ARC needs nothing installed beforehand. The script makes this
upload's own folder in _incoming, receives a tar stream of the snapshot on stdin, checks every
file against SHA256SUMS, prints the SHA-256 of every file (compared here with this Mac's list)
and renames the folder into place with one rename. It never deletes or replaces anything on ARC.
The shared folders it makes (the ARC folder, simulated/ and their _incoming/) are 3770, so every
member of the project can upload into them; a shared folder this account may not write into ends
the attempt before anything is received (shared_folder_not_writable). The stream stops as soon as
the script prints a line after which it reads no more files (a FAIL, or a copy it already has).

- Live: the worker runs in a Terminal window (arc_worker --terminal). ssh reads the VT password,
  the Duo answer and any host-key question from that window, never from the pipes used here, so
  this program never sees them. ssh never shares a connection (ControlMaster=no,
  ControlPath=none): one password and one Duo per attempt.
- Demo: LocalBashRunner runs the same script with /bin/bash against the demo folder on this Mac,
  with the same tar stream, in the detached worker. No network, no account, no Terminal. The demo
  refuses a file name that this Mac's tar would change (demo_ssh_name); ARC's tar keeps it.

The worker calls send(ctx) and check(ctx) with an arc_worker.WorkerContext (the transport
interface at the top of arc_worker.py). SSH has no stop worker: arc-stop stops an SSH upload
without one, and a stop request while a run is under way (an upload, or a Check status that waits
for its sign-in) ends ssh with SIGTERM. A stop after the script reached its rename is told that the
copy may already be in its folder (stopped_placing): sshd lets the script run on to its next write.

Standard library and the package only.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import unicodedata
from pathlib import Path

from . import arc_paths, arc_payload, arc_records, arc_session, arc_settings, arc_worker
from .arc_records import MANIFEST_NAME, SUMS_NAME
from .arc_worker import Outcome, StopRequested
from .config import DataLayerError, utc_now

log = logging.getLogger(__name__)

SSH_PROGRAM = "/usr/bin/ssh"
BASH_PROGRAM = "/bin/bash"
REMOTE_SCRIPT = Path(__file__).with_name("arc_remote.sh")
# $0 of the remote script.
SCRIPT_NAME = "raman_arc_upload"
MODES = ("upload", "upload-copy", "check")
# The PATH of the demo's /bin/bash: the system's own tools, as on ARC.
LOCAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Variables that would make bash run something before the script, or read it differently.
_SHELL_VARIABLES = ("BASH_ENV", "ENV", "CDPATH", "GLOBIGNORE", "SHELLOPTS", "BASHOPTS", "IFS", "PS4",
                    "POSIXLY_CORRECT")

# Modes of this upload's own files and folders on ARC (umask 027 there gives the same): the project
# group reads. The shared folders above them are 3770 when the remote script makes them.
FILE_MODE, FOLDER_MODE = 0o640, 0o750
# Options every ssh command line pins, after IgnoreUnknown=*: an ssh too old to know one of them
# ignores it (a person's config cannot set it there either), and a person's own IgnoreUnknown, which
# a narrower list here would replace, keeps working.
PINNED_OPTIONS = ("StdinNull=no", "SessionType=default", "ForkAfterAuthentication=no", "ChannelTimeout=none")
BLOCK = 64 * 1024
LINE_LIMIT = 64 * 1024
PROGRESS_SECONDS = 1.0
# How long the end of an attempt waits for ssh and the stream writer.
END_SECONDS = 30
STOP_POLL_SECONDS = 0.2
# The demo's pause in "signing_in" at the default pace (RAMAN_ARC_DEMO_PACE 0.1; it scales with it).
DEMO_SIGN_IN_SECONDS = 1.5
# The teaching fault drop_connection waits this long at most for the demo's tar to have begun.
FAULT_WAIT_SECONDS = 5.0

# Values put in single quotes in the remote command: these characters never need escaping (S-16).
# Always used with fullmatch(): in Python "$" also matches before a newline at the end.
ARGUMENT = re.compile(r"^[A-Za-z0-9._/-]+$")
# A folder name (the session folder, or the name it has on ARC): no "/".
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# A team known-hosts file is passed as -o UserKnownHostsFile=<path>; ssh splits that value at
# spaces and expands % and ${ in it, so only plain characters are taken (the settings' own rule).
KNOWN_HOSTS_PATH = arc_settings.KNOWN_HOSTS_PATH
DIGEST = re.compile(r"^[0-9a-f]{64}$")

# Texts (design 11.4, 11.5, 12; Appendix A.2 for the states' messages).
CONNECTED = "Connected. Sending {files} files..."
COPYING = "Copying over SSH: {percent}% ({done} of {total} MB)."
VERIFYING = "Checking the files on ARC (SHA-256)..."
VERIFYING_DEMO = "Checking the files in the demo folder (SHA-256)..."
VERIFIED = "On ARC: {files} files in {path}. ARC computed the SHA-256 of every file, and each matches this Mac's copy."
VERIFIED_DEMO = "Demo: {files} files in the demo folder on this Mac, every one checked. Nothing left this Mac."
DONE_TERMINAL = "Done: all {files} files are on ARC and match this Mac's copy."
DEMO_SIGN_IN = ("Demo: no Terminal and no password. A real SSH upload asks for your VT password and Duo in a "
                "Terminal window here.")
CONFLICT = ("Your copy is complete and checked in {incoming} on ARC. Keep it as a new copy, or move the other "
            "folder on ARC and press Retry same request.")
STAGED_COMPLETE = "The copy is complete in _incoming on ARC but not in place yet. Retry same request puts it in place."
STAGED_PARTIAL = "Part of the copy is in _incoming on ARC. Retry same request finishes it."
MISSING = "Nothing of this upload is on ARC."
ELSEWHERE = "ARC has a folder with this name that this upload did not make."
# What a check could not do (review REG-01): the connection ended, or the SSH computer did not run it. A check
# has its own words where the upload's do not fit (review R2-03, R2-06): it makes no folder, and it runs on the
# SSH computer saved with the upload, so another check meets the same problem.
CHECK_CUT = "The SSH connection ended before the check was done."
CHECK_REFUSED = "The check cannot run on {host}: that computer did not run its commands."
CHECK_TOOLS = "The check cannot run on {host}: {why}."
CHECK_FOLDER_REFUSED = "ARC refused a folder on the way to the copy: {why}."
# FAIL access of a check: a folder it looks into that this account may not read, also a folder in the copy it
# compares (review R3-03). The copy is not reported missing or changed.
NOT_READABLE = "Your VT account may not read {path} on ARC."
STILL_THERE = "Still on ARC: {files} files in {path}, every one matching this Mac's list (SHA-256)."
STILL_THERE_DEMO = "Demo: still in the demo folder: {files} files, every one matching this Mac's list (SHA-256)."
NOT_MATCHING = "A copy of this upload is on ARC ({path}), but it does not match this Mac's list (for example {file})."
PLACED_BUT_DIFFERENT = ("ARC put this copy in place as {path}, but it does not match this Mac's list of files. Do not "
                        "use that folder; start a new upload.")
# The demo wording of the texts above that name ARC: the copy is in the demo folder on this Mac (review COR-05).
DEMO_TEXTS = {
    CONFLICT: "Your copy is complete and checked in {incoming}. Keep it as a new copy, or move the other folder and "
              "press Retry same request.",
    STAGED_COMPLETE: "The copy is complete in _incoming of the demo folder but not in place yet. Retry same request "
                     "puts it in place.",
    STAGED_PARTIAL: "Part of the copy is in _incoming of the demo folder. Retry same request finishes it.",
    MISSING: "Nothing of this upload is in the demo folder.",
    ELSEWHERE: "The demo folder has a folder with this name that this upload did not make.",
    NOT_MATCHING: "A copy of this upload is in the demo folder ({path}), but it does not match this Mac's list (for "
                  "example {file}).",
    PLACED_BUT_DIFFERENT: "This copy was put in place as {path} in the demo folder, but it does not match this Mac's "
                          "list of files. Do not use that folder; start a new upload.",
    CHECK_REFUSED: "The check cannot run on this Mac: its commands did not run.",
    CHECK_TOOLS: "The check cannot run on this Mac: {why}.",
    CHECK_FOLDER_REFUSED: "The demo refused a folder on the way to the copy: {why}.",
    NOT_READABLE: "This Mac's account may not read {path} in the demo folder.",
}
DROPPED = "Demo: the connection was cut halfway (the teaching fault). Press Retry same request to finish the upload."
REMOTE_WORDS = {"prepare": "make the folder", "receive": "receive the files", "place": "rename the folder"}
# The sign-in steps of the Terminal window, in the order ssh asks (decision I9; the same words are in
# README section 14 and the window's SSH confirmation). The host-key question comes first, and only
# while ARC's key is not known yet; ssh shows on screen what is typed there.
HOST_KEY_STEP = (
    "The first time only: ARC's key fingerprint, then \"Are you sure you want to continue connecting",
    "(yes/no/[fingerprint])?\". Type only yes or no here; what you type is shown on screen, so never",
    "type your password at this question. Answer yes only if the fingerprint matches the one in the",
    "README (section 14, Host key); otherwise answer no and ask the team.")
PASSWORD_STEP = ("Your VT password. Typing shows nothing; that is normal. Press Return.",)
DUO_STEP = ("Duo: approve the push on your phone, or type your password followed by a comma and a",
            "six-digit passcode at the password question. Choose Duo Push if a numbered menu appears.")
WARNING_TEXT = ("ssh may print a WARNING that the connection is not using a post-quantum key exchange",
                "algorithm. That is expected with ARC (ARC's FAQ explains it); continue.")
NETWORK_TEXT = "ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN."
# A demo SSH upload refuses a file name that this Mac's tar (bsdtar) would write in decomposed form,
# as the demo's check would then fail; ARC's GNU tar keeps names as they are (SSH-10). bsdtar splits
# a precomposed letter (ü, é, Hangul) and puts combining marks in their canonical order, but keeps
# these ranges, which macOS never decomposes, and the composites that NFC would not make again.
_MAC_TAR_KEEPS = ((0x2000, 0x2FFF), (0xF900, 0xFAFF), (0x2F800, 0x2FAFF))

# The step lines of the remote script and the state each one starts (11.4 step 4).
STEP_STATES = {"prepare": "checking", "receive": "transferring", "verify": "verifying", "place": "placing"}
_ORDER = ("signing_in", "checking", "transferring", "verifying", "placing")
# Lines after which the remote script reads no more of the stream (it reads the rest and drops it):
# these and every STEP line but prepare and receive. A word the script never prints does not count
# (a remote .bashrc may print anything).
_LAST_READ_LINES = ("FAIL", "DONE", "CONFLICT", "STAGED", "MISSING", "ELSEWHERE", "DIGESTS-BEGIN", "DIGEST",
                    "DIGESTS-END")
# FAIL verify of step 1 (a copy this upload put in place earlier) names the copy: "the copy in <path>
# does not match its list".
_CHANGED_COPY = ("the copy in ", " does not match its list")


class ProtocolError(Exception):
    """The far end printed something the remote script never prints (the worker records internal)."""


class _Abort(Exception):
    """Ends the stream writer early: the far end reads no more of it (SSH-05), or the teaching fault
    drop_connection."""


# ---------------------------------------------------------------------- the command line (11.2)


def _read_remote_script():
    try:
        return REMOTE_SCRIPT.read_bytes()
    except OSError:
        return None


# arc_remote.sh as this process read it when it imported this module. A worker imports its
# transports when it starts (decision I10), so every attempt it makes sends the script of the code it
# started with, even when the data layer is rebuilt during the upload.
_SCRIPT = _read_remote_script()


def remote_script():
    """The bytes of arc_remote.sh (design Appendix B), sent with every SSH upload: read once, when this
    module is imported (or at the first use, when the file could not be read then)."""
    global _SCRIPT
    if _SCRIPT is None:
        _SCRIPT = REMOTE_SCRIPT.read_bytes()
    return _SCRIPT


def _argument(value, what, pattern=ARGUMENT):
    text = str(value)
    if not pattern.fullmatch(text) or "/../" in "/" + text + "/" or "/./" in "/" + text + "/":
        raise ValueError("%s holds a character the remote command does not take" % what)
    return text


def remote_command(mode, base, folder, upload_id, name, script=None):
    """The one string the remote login shell runs (11.2): the script as base64, decoded there, and
    its five arguments, each in single quotes and made only of [A-Za-z0-9._/-]."""
    if mode not in MODES:
        raise ValueError("not a mode of the remote script")
    if not arc_records.UPLOAD_ID.fullmatch(str(upload_id)):
        raise ValueError("not an upload id")
    if not str(base).startswith("/"):
        raise ValueError("the ARC folder is not an absolute path")
    values = [_argument(base, "the ARC folder"), _argument(folder, "the session folder", NAME), str(upload_id),
              _argument(name, "the folder name", NAME)]
    encoded = base64.b64encode(remote_script() if script is None else script).decode("ascii")
    return "bash -c \"$(printf %%s '%s' | base64 --decode)\" %s %s %s" % (
        encoded, SCRIPT_NAME, mode, " ".join("'%s'" % value for value in values))


def ssh_argv(username, host, known_hosts, command):
    """The argument list of /usr/bin/ssh for one attempt (11.2).

    One connection of its own (never a shared one), the person's own known_hosts with
    StrictHostKeyChecking=ask, or the team's file with StrictHostKeyChecking=yes; never
    accept-new or no. Options on the command line win over ~/.ssh/config, so a config cannot give
    the remote script an empty stdin (StdinNull), open no session (SessionType), fork away
    (ForkAfterAuthentication) or close the session while ARC checks a big copy (ChannelTimeout).
    """
    if not (isinstance(username, str) and arc_settings.USERNAME.fullmatch(username) and username != "root"):
        raise ValueError("not a VT username")
    if not (isinstance(host, str) and arc_settings.HOST.fullmatch(host)):
        raise ValueError("not an ARC computer")
    if known_hosts and not KNOWN_HOSTS_PATH.fullmatch(known_hosts):
        raise ValueError("the known-hosts path holds a character ssh would read differently")
    argv = [SSH_PROGRAM, "-T", "-e", "none", "-o", "BatchMode=no", "-o", "ControlMaster=no", "-o", "ControlPath=none"]
    if known_hosts:
        argv += ["-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + known_hosts,
                 "-o", "GlobalKnownHostsFile=/dev/null"]
    else:
        argv += ["-o", "StrictHostKeyChecking=ask"]
    argv += ["-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
             "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no", "-o", "RemoteCommand=none",
             "-o", "IgnoreUnknown=*"]
    for option in PINNED_OPTIONS:
        argv += ["-o", option]
    argv += ["-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
             "-l", username, "--", host, command]
    return argv


# ---------------------------------------------------------------------- the two far ends


def _signal(process, number, group=False):
    """Send a signal to a process that has not been waited for (or to its process group)."""
    if process is None or process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if group:
            os.killpg(process.pid, number)
        else:
            os.kill(process.pid, number)


class SshRunner:
    """Live: /usr/bin/ssh with the argv of 11.2. Its stderr is the Terminal window (inherited) and
    its environment is the worker's; the tar stream goes in on stdin, RAMAN lines come out on stdout."""

    demo = False

    def __init__(self, argv):
        self.argv = list(argv)
        self.process = None

    def start(self):
        try:
            self.process = arc_worker.popen_program(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except FileNotFoundError:
            raise arc_records.refusal("ssh_missing") from None
        return self.process

    def end(self):
        """A stop request: ssh ends, and ARC sees the connection close."""
        _signal(self.process, signal.SIGTERM)

    def kill(self):
        _signal(self.process, signal.SIGKILL)

    def cut(self):
        self.kill()

    def finish(self):
        """After the run: what the far end said on stderr (nothing here: that is the Terminal)."""
        return ""


def _local_environment():
    """The demo's environment for /bin/bash: this one without what changes how bash starts, and the
    system's tools first."""
    env = {name: value for name, value in os.environ.items()
           if name not in _SHELL_VARIABLES and not name.startswith("BASH_FUNC_")}
    env["PATH"] = LOCAL_PATH
    return env


class LocalBashRunner:
    """Demo: the remote script run with /bin/bash on this Mac, against the demo folder (design 12):
    ["/bin/bash", "-c", <arc_remote.sh>, "raman_arc_upload", MODE, <demo base>, FOLDER, ID, NAME].
    It runs in a process group of its own, so that a stop or the teaching fault ends tar with it."""

    demo = True

    def __init__(self, mode, base, folder, upload_id, name, script=None):
        if mode not in MODES:
            raise ValueError("not a mode of the remote script")
        text = (remote_script() if script is None else script).decode("utf-8")
        self.argv = [BASH_PROGRAM, "-c", text, SCRIPT_NAME, mode, str(base), str(folder), str(upload_id), str(name)]
        self.process = None
        self._stderr = []
        self._reader = None

    def start(self):
        self.process = arc_worker.popen_program(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, env=_local_environment(),
                                                start_new_session=True, close_fds=True)
        self._reader = threading.Thread(target=self._read_errors, name="demo-ssh-stderr", daemon=True)
        self._reader.start()
        return self.process

    def _read_errors(self):
        with contextlib.suppress(OSError, ValueError):
            for raw in iter(lambda: self.process.stderr.readline(LINE_LIMIT), b""):
                self._stderr = (self._stderr + [raw.decode("utf-8", "replace").strip()])[-5:]

    def end(self):
        _signal(self.process, signal.SIGTERM, group=True)

    def kill(self):
        _signal(self.process, signal.SIGKILL, group=True)

    def cut(self):
        """The teaching fault drop_connection: the far end goes away in the middle of the stream."""
        self.kill()

    def finish(self):
        """After the run: the last lines the script wrote on stderr (for worker.log), its pipe closed."""
        if self._reader is not None:
            self._reader.join(2)
        if self.process is not None and self.process.stderr is not None:
            with contextlib.suppress(OSError, ValueError):
                self.process.stderr.close()
        return " | ".join(line for line in self._stderr if line)[:300]


# ---------------------------------------------------------------------- the tar stream (11.4 step 3)


def payload_entries(top):
    """[(relative path, is a folder, lstat)] of the snapshot folder: the manifest first, then every
    folder and file sorted bytewise by path. Anything but a plain file or folder means the snapshot
    was changed (it is made of plain files only)."""
    top = Path(top)
    found = []
    for directory, folders, files in os.walk(top):
        for name in folders + files:
            path = os.path.join(directory, name)
            info = os.lstat(path)
            relative = os.path.relpath(path, top).replace(os.sep, "/")
            if stat.S_ISDIR(info.st_mode):
                found.append((relative, True, info))
            elif stat.S_ISREG(info.st_mode):
                found.append((relative, False, info))
            else:
                raise arc_payload.PayloadError("payload_changed", "The upload copy holds something that is not a "
                                               "plain file", detail=relative)
    manifest = [entry for entry in found if entry[0] == MANIFEST_NAME]
    if not manifest or manifest[0][1]:
        raise arc_payload.PayloadError("payload_changed", "The upload copy has no manifest")
    rest = sorted((entry for entry in found if entry[0] != MANIFEST_NAME),
                  key=lambda entry: arc_payload.sort_key(entry[0]))
    return manifest + rest


def _tar_entry(relative, folder, info, size=0):
    """The header of one entry: relative name, owner 0 and no names (no account name on the wire),
    0640 or 0750, the snapshot's time in whole seconds (so no extended header is needed)."""
    entry = tarfile.TarInfo(relative)
    entry.uid = entry.gid = 0
    entry.uname = entry.gname = ""
    entry.mtime = max(0, int(info.st_mtime))
    if folder:
        entry.type = tarfile.DIRTYPE
        entry.mode = FOLDER_MODE
    else:
        entry.type = tarfile.REGTYPE
        entry.mode = FILE_MODE
        entry.size = size
    return entry


class _Reader:
    """A file object over an open file that reports what tarfile reads from it."""

    def __init__(self, handle, on_bytes=None):
        self.handle = handle
        self.on_bytes = on_bytes

    def read(self, size=-1):
        size = BLOCK if size is None or size < 0 else size
        parts, wanted = [], size
        while wanted > 0:
            block = os.read(self.handle, min(wanted, 1 << 20))
            if not block:
                break
            parts.append(block)
            wanted -= len(block)
        data = b"".join(parts)
        if data and self.on_bytes is not None:
            self.on_bytes(len(data))
        return data


def write_tar(top, out, entries=None, on_bytes=None, on_file=None):
    """Write the tar stream of the snapshot folder `top` to `out` (a PAX stream, written in order).

    Regular files and folders only, opened without following a link; no macOS extended attributes
    and no ._ files (tarfile writes none). `on_bytes(count)` is called with the file bytes as they
    go, `on_file(relative)` after each file.
    """
    top = Path(top)
    entries = payload_entries(top) if entries is None else entries
    tar = tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT)
    try:
        for relative, folder, info in entries:
            if folder:
                tar.addfile(_tar_entry(relative, True, info))
                continue
            handle = os.open(str(top / relative), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(handle)
                if not stat.S_ISREG(opened.st_mode):
                    raise arc_payload.PayloadError("payload_changed", "The upload copy holds something that is "
                                                   "not a plain file", detail=relative)
                tar.addfile(_tar_entry(relative, False, info, opened.st_size), _Reader(handle, on_bytes))
            finally:
                os.close(handle)
            if on_file is not None:
                on_file(relative)
        tar.close()
    except BaseException:
        # Never finish the stream after an error (the far end must not see a complete archive), and
        # never write the rest later, when the objects are collected.
        tar.closed = True
        with contextlib.suppress(AttributeError):
            tar.fileobj.closed = True
        raise


# ---------------------------------------------------------------------- one attempt (11.4)


class Plan:
    """The validated values of one attempt (values from the record are checked again: S-13)."""

    def __init__(self, ctx, record, mode):
        destination = record.get("destination") if isinstance(record.get("destination"), dict) else {}
        session = record.get("session") if isinstance(record.get("session"), dict) else {}
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        self.mode = mode
        self.folder = ctx.session_folder
        if not arc_session.SESSION_NAME.fullmatch(self.folder) or self.folder.lower() == "simulated":
            raise ValueError("not a session folder name")
        self.upload_id = ctx.upload_id
        arc_folder = destination.get("arc_folder")
        if not isinstance(arc_folder, str) or arc_paths.check_cluster_folder(arc_folder, "/"):
            raise ValueError("not an ARC folder")
        area = destination.get("area")
        self.base = destination.get("base")
        expected = arc_folder if area == arc_session.AREA_REAL else arc_folder + "/simulated"
        if area not in (arc_session.AREA_REAL, arc_session.AREA_SIMULATED) or self.base != expected:
            raise ValueError("the record's ARC folder and area do not agree")
        self.area = area
        self.name = destination.get("placed_as") or self.folder
        if not self.takes_name(self.name):
            raise ValueError("not a name this upload may have on ARC")
        self.points = session.get("points") or 0
        self.files = payload.get("files") or 0
        self.bytes = payload.get("bytes") or 0
        self.new_copy = bool(destination.get("new_copy"))
        # arc_remote.sh as the worker read it when it started (decision I10), when it gives it; else the
        # bytes this module read when it was imported.
        script = getattr(ctx, "remote_script", None)
        self.script = script.encode("utf-8") if isinstance(script, str) else script
        if self.script is not None and not isinstance(self.script, bytes):
            raise ValueError("the remote script is not text")
        self.demo = ctx.demo
        if ctx.demo:
            root = ctx.demo_root
            if root is None:
                raise ValueError("a demo upload without its demo folder")
            self.remote_base = arc_records.local_path(root, self.base)
            self.username = self.host = None
            self.known_hosts = ""
        else:
            self.remote_base = _argument(self.base, "the ARC folder")
            used = ctx.settings_used
            self.username = used.get("ssh_username") or ""
            self.host = used.get("ssh_host") or ""
            self.known_hosts = used.get("ssh_known_hosts") or ""
            self._check_live()
        self.incoming = self.remote_base + "/_incoming/" + self.folder + "." + self.upload_id
        # The ARC folder as the remote script names it: <base> itself, or the folder above simulated/.
        self.remote_top = self.remote_base[:-len("/simulated")] if area == arc_session.AREA_SIMULATED else \
            self.remote_base

    def _check_live(self):
        if not self.username:
            raise arc_records.refusal("username_missing")
        if not (arc_settings.USERNAME.fullmatch(self.username) and self.username != "root"):
            raise arc_records.refusal("settings_invalid", problems="arc_upload.ssh.username is not a VT username")
        if not arc_settings.HOST.fullmatch(self.host):
            raise arc_records.refusal("settings_invalid", problems="arc_upload.ssh.host is not an ARC computer")
        if self.known_hosts:
            # The settings' rule, which also takes only the characters ssh reads as they are.
            problem = arc_settings.known_hosts_problem(self.known_hosts)
            if problem is not None:
                raise arc_records.refusal("known_hosts_unreadable", path=self.known_hosts, detail=problem)

    def takes_name(self, name):
        """The session folder's own name, or <folder>_copy2 ... _copy99 (the names the remote script uses)."""
        return name == self.folder or bool(re.fullmatch(re.escape(self.folder) + r"_copy([2-9]|[1-9][0-9])", str(name)))

    def shown(self, name):
        """A folder as the person finds it: on ARC, or in the demo folder on this Mac."""
        return self.remote_base + "/" + name

    def runner(self):
        if self.demo:
            return LocalBashRunner(self.mode, self.remote_base, self.folder, self.upload_id, self.name,
                                   script=self.script)
        command = remote_command(self.mode, self.remote_base, self.folder, self.upload_id, self.name,
                                 script=self.script)
        return SshRunner(ssh_argv(self.username, self.host, self.known_hosts, command))


class Attempt:
    """One run of the remote script: start it, send the snapshot (upload modes), read its RAMAN lines,
    and move the record through the states the lines name (sending only)."""

    def __init__(self, ctx, plan, runner, send_files, fault=False):
        self.ctx = ctx
        self.plan = plan
        self.runner = runner
        self.send_files = send_files
        self.fault = fault
        self.lines = 0
        self.ignored = 0
        self.digests = {}
        self.collecting = False
        self.digests_ended = False
        # The STEP words of the remote script, in the order they came.
        self.steps = []
        self.result = None
        # The text of a FAIL line as the far end printed it (result[2] is shortened for the record).
        self.fail_text = ""
        # Set once the far end prints a line after which it reads no more of the stream (11.3).
        self.stream_unneeded = threading.Event()
        self.exit_code = None
        self.stopped = False
        self.writer_error = None
        self.fault_fired = False
        self.state = ctx.read().get("state")
        self.sent_marked = False
        self.receiving = threading.Event()
        self.done = threading.Event()
        self._progress_lock = threading.Lock()
        self._last_progress = 0.0
        self.files_done = 0
        self.bytes_done = 0
        self.total_bytes = 0

    # -- the record

    def _advance(self, step):
        """A step line of the remote script: the state it starts (forward only), with its message."""
        state = STEP_STATES.get(step)
        if state is None or not self.send_files:
            return
        if self.state in _ORDER and _ORDER.index(state) <= _ORDER.index(self.state):
            return
        demo = self.plan.demo
        message = {"checking": CONNECTED.format(files=self.plan.files),
                   "transferring": self._copy_text(),
                   "verifying": VERIFYING_DEMO if demo else VERIFYING,
                   "placing": arc_records.MESSAGES["placing_demo" if demo else "placing"]}[state]
        text = self.ctx.text(message)
        sent = state != "checking"

        placing = state == "placing" or (state == "verifying" and self.found_in_place())

        def mutate(record):
            arc_records.set_state(record, state, text)
            if sent:
                _ssh_of(record)["sent"] = True
            if placing:
                # From now on ARC may have the copy in its final folder, even if this worker is killed.
                _ssh_of(record)["may_be_placed"] = True
            if state == "transferring":
                progress = record.get("progress") if isinstance(record.get("progress"), dict) else {}
                progress.update(files_done=self.files_done, bytes_done=self.bytes_done)
                record["progress"] = progress

        self.ctx.update(mutate)
        self.ctx.event(text, state)
        self.state = state
        self.sent_marked = self.sent_marked or sent
        _tell(self.ctx, message)

    def _mark_sent(self):
        """Something of this upload is on ARC now (Check status then has something to look at)."""
        if self.sent_marked:
            return
        self.sent_marked = True
        self.ctx.update(lambda record: _ssh_of(record).update(sent=True))

    def _copy_text(self):
        return copy_text(self.bytes_done, self.plan.bytes or self.total_bytes)

    def _progress(self, force=False):
        """Bytes handed to ssh, in the record at most once a second; the message only while copying."""
        if not self.receiving.is_set():
            return
        with self._progress_lock:
            now = time.monotonic()
            if not force and now - self._last_progress < PROGRESS_SECONDS:
                return
            self._last_progress = now
            files_done, bytes_done = self.files_done, self.bytes_done
            text = self.ctx.text(copy_text(bytes_done, self.plan.bytes or self.total_bytes))

            def mutate(record):
                progress = record.get("progress") if isinstance(record.get("progress"), dict) else {}
                progress.update(files_done=files_done, bytes_done=bytes_done)
                record["progress"] = progress
                if record.get("state") == "transferring":
                    record["message"] = text

            try:
                self.ctx.update(mutate)
            except DataLayerError as error:
                # Only the progress shown is late; the next update carries it.
                log.info("progress not written: %s", type(error).__name__)

    # -- the stream writer (a thread)

    def _on_bytes(self, count):
        if self.stream_unneeded.is_set():
            raise _Abort()
        self.bytes_done += count
        if self.fault and not self.fault_fired and self.bytes_done * 2 >= max(1, self.total_bytes):
            self._cut()
        self._progress()

    def _on_file(self, _relative):
        self.files_done += 1
        if self.stream_unneeded.is_set():
            raise _Abort()
        if self.plan.demo:
            pace = arc_worker.demo_pace()
            if pace and self.ctx.wait(pace):
                raise _Abort()
        self._progress()

    def _cut(self):
        """drop_connection: once the demo's tar has begun, the far end goes away mid-stream."""
        self.fault_fired = True
        self.receiving.wait(FAULT_WAIT_SECONDS)
        deadline = time.monotonic() + FAULT_WAIT_SECONDS
        stage = Path(self.plan.incoming)
        while time.monotonic() < deadline and not (stage.is_dir() and any(stage.iterdir())):
            time.sleep(0.02)
        self.ctx.event(DROPPED)
        self.runner.cut()
        raise _Abort()

    def _write(self, process, entries):
        try:
            write_tar(self.plan_top, process.stdin, entries, self._on_bytes, self._on_file)
            process.stdin.flush()
        except _Abort:
            pass
        except (BrokenPipeError, ConnectionResetError):
            # The far end went away; what it printed says how the attempt ended.
            log.info("the stream ended early: the far end closed it")
        except Exception as error:  # noqa: BLE001 - the attempt ends with this error
            self.writer_error = error
            log.error("the stream could not be written: %s", type(error).__name__)
            self.runner.end()
        finally:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()
        self._progress(force=True)

    # -- the reader (the main thread)

    def _watch_stop(self):
        while not self.done.is_set():
            if self.ctx.wait(STOP_POLL_SECONDS):
                if not self.done.is_set():
                    self.stopped = True
                    log.info("a stop request ends the attempt")
                    self.runner.end()
                return

    def _line(self, raw):
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not text.startswith("RAMAN "):
            # A remote .bashrc that prints, or anything else: only RAMAN lines count.
            self.ignored += 1
            return
        self.lines += 1
        word, _space, rest = text[len("RAMAN "):].partition(" ")
        if word in _LAST_READ_LINES or (word == "STEP" and rest not in ("prepare", "receive")):
            # The far end reads no more of the stream (it ignores the rest): stop sending it.
            self.stream_unneeded.set()
        if word == "STEP":
            self.steps.append(rest)
            if rest == "receive":
                self.receiving.set()
            self._advance(rest)
            if rest in ("receive", "verify", "place"):
                self._mark_sent()
        elif word == "DIGESTS-BEGIN":
            self.collecting = True
            self.digests = {}
            self.digests_ended = False
        elif word == "DIGEST" and self.collecting:
            found = _digest(rest)
            if found is None:
                raise ProtocolError("a digest line the remote script does not print")
            self.digests[found[0]] = found[1]
        elif word == "DIGESTS-END":
            self.collecting = False
            self.digests_ended = True
        elif word in ("DONE", "STAGED") and self.result is None:
            how, _space, path = rest.partition(" ")
            self.result = (word.lower(), how, path)
            self._mark_sent()
        elif word in ("CONFLICT", "MISSING", "ELSEWHERE") and self.result is None:
            self.result = (word.lower(), None, rest)
            if word == "CONFLICT":
                self._mark_sent()
        elif word == "FAIL" and self.result is None:
            what, _space, why = rest.partition(" ")
            self.result = ("fail", what, " ".join(why.split())[:300])
            self.fail_text = why
        else:
            log.info("a RAMAN line that is not used: %s", word[:20])

    def found_in_place(self):
        """True when the far end found this upload's copy in place, put there by an earlier run (step 1
        of the script prints STEP verify without STEP prepare)."""
        return bool(self.steps) and self.steps[0] == "verify"

    def may_be_placed(self):
        """True when ARC may have the copy in its final folder although no DONE line came: the script
        found it in place, reached its rename (STEP place), or had checked the complete copy, after
        which the rename is its next step. A real sshd only closes the script's pipes when ssh ends, so
        the script goes on until its next write: a stop can cross the rename (decision I8, SSH-V01)."""
        if self.found_in_place() or "place" in self.steps:
            return True
        return "prepare" in self.steps and self.digests_ended and self.result is None

    def placed_now(self):
        """Whether this upload's copy may be in its final folder after this run, for ssh.may_be_placed
        (None: the run tells nothing). DONE, a copy found in place (also one that changed), or no answer
        after the script reached its rename: yes. CONFLICT, STAGED, MISSING, ELSEWHERE, or a FAIL of
        step 2 (a FAIL place leaves the copy in _incoming): no. Nothing printed, or a FAIL before step 1
        (tools, a linked folder): nothing is known."""
        if not self.lines:
            return None
        kind = self.result[0] if self.result else None
        if kind == "done" or (kind == "fail" and self.found_in_place()):
            return True
        if kind == "fail":
            return False if "prepare" in self.steps else None
        if kind is not None:
            return False
        return self.may_be_placed()

    def run(self):
        """Start the far end, stream, read to the end; returns self (the run is in ssh.runs)."""
        entries = None
        if self.send_files:
            self.plan_top = self.ctx.payload_dir
            entries = payload_entries(self.plan_top)
            self.total_bytes = sum(info.st_size for _relative, folder, info in entries if not folder)
        started = _begin_run(self.ctx, self.plan.mode)
        try:
            process = self.runner.start()
        except BaseException:
            _end_run(self.ctx, started, None, "not_started")
            raise
        writer = None
        if self.send_files:
            writer = threading.Thread(target=self._write, args=(process, entries), name="ssh-stream", daemon=True)
            writer.start()
        else:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()
        watcher = threading.Thread(target=self._watch_stop, name="ssh-stop", daemon=True)
        watcher.start()
        try:
            for raw in iter(lambda: process.stdout.readline(LINE_LIMIT), b""):
                self._line(raw)
        except BaseException:
            self.runner.kill()
            raise
        finally:
            self.done.set()
            self.exit_code = _finish(process, self.runner)
            if writer is not None:
                writer.join(END_SECONDS)
                if writer.is_alive():
                    log.error("the stream writer did not end")
            watcher.join(END_SECONDS)
            with contextlib.suppress(OSError, ValueError):
                process.stdout.close()
            words = self.runner.finish()
            if words:
                log.info("the demo's script said on stderr: %s", words)
            _end_run(self.ctx, started, self.exit_code, self.summary(), self.placed_now())
        return self

    def summary(self):
        """A short word for ssh.runs: how this run ended."""
        if self.result is not None:
            kind, how, _path = self.result
            if kind in ("done", "staged", "fail"):
                return "%s_%s" % (kind, how) if kind != "done" else how
            return kind
        if self.stopped:
            return "stopped"
        if self.writer_error is not None:
            return "stream_failed"
        return "no_reply" if not self.lines else "cut"


def _finish(process, runner):
    """Wait for the far end to end; ended at last if it does not. Returns its exit code."""
    try:
        return process.wait(END_SECONDS)
    except subprocess.TimeoutExpired:
        runner.end()
    try:
        return process.wait(5)
    except subprocess.TimeoutExpired:
        runner.kill()
        return process.wait()


def _digest(rest):
    """(path, hex) of `<64 hex>  <path>`, or None."""
    value, separator, path = rest[:64], rest[64:66], rest[66:]
    if separator != "  " or not DIGEST.fullmatch(value) or not path:
        return None
    return path, value


def _ssh_of(record):
    ssh = record.get("ssh")
    if not isinstance(ssh, dict):
        ssh = {"attempt": 1, "host": None, "username": None, "known_hosts": None, "terminal_opened_utc": None,
               "runs": [], "sent": False}
        record["ssh"] = ssh
    return ssh


def _begin_run(ctx, mode):
    started = utc_now()
    entry = {"attempt": ctx.attempt, "action": ctx.action, "mode": mode, "started_utc": started, "ended_utc": None,
             "exit_code": None, "outcome": None}

    def mutate(record):
        ssh = _ssh_of(record)
        ssh["runs"] = list(ssh.get("runs") or []) + [entry]

    ctx.update(mutate)
    return started


def _end_run(ctx, started, exit_code, outcome, placed=None):
    """The end of a run in ssh.runs, and ssh.may_be_placed: whether the copy may be in its final folder on
    ARC rather than in _incoming (None keeps what earlier runs said; arc_records.leftover_on_arc)."""
    def mutate(record):
        ssh = _ssh_of(record)
        for run in reversed(ssh.get("runs") or []):
            if isinstance(run, dict) and run.get("started_utc") == started:
                run.update(ended_utc=utc_now(), exit_code=exit_code, outcome=outcome)
                break
        if placed is not None:
            ssh["may_be_placed"] = bool(placed)

    ctx.update(mutate)


def run_attempt(ctx, plan, send_files, fault=False, runner=None):
    """One attempt (11.4): the far end of `plan` started, the snapshot streamed (send_files), every
    RAMAN line read. Returns the Attempt, whose result says how the remote script ended."""
    attempt = Attempt(ctx, plan, runner or plan.runner(), send_files, fault)
    return attempt.run()


# ---------------------------------------------------------------------- texts


def _megabytes(count):
    return ("%.2f" % (count / 1e6)).rstrip("0").rstrip(".") or "0"


def copy_text(done, total):
    """"Copying over SSH: 40% (0.25 of 0.6 MB).\""""
    percent = int(100 * done / total) if total else 100
    return COPYING.format(percent=min(100, percent), done=_megabytes(done), total=_megabytes(total))


def _size_text(count):
    if count < 1000:
        return "%d bytes" % count
    if count < 1000 * 1000:
        return "%d kB" % round(count / 1000)
    return "%s MB" % _megabytes(count)


def terminal_text(plan, check=False):
    """What the Terminal window shows before ssh asks for anything (design 11.4 step 1), with ssh's
    questions in the order ssh asks them (decision I9)."""
    target = plan.shown(plan.name)
    if plan.mode == "upload-copy":
        target = "%s/%s (a new copy: this name or the first free _copyN)" % (plan.remote_base, plan.folder)
    steps = ([] if plan.known_hosts else [HOST_KEY_STEP]) + [PASSWORD_STEP, DUO_STEP]
    lines = ["Portable Raman: %s over SSH" % ("check an upload on ARC" if check else "upload to ARC"),
             "  Session  %s  (%d spectra, %d files, %s)" % (
                 plan.folder, plan.points, plan.files, _size_text(plan.bytes)),
             "  %s%s@%s:%s" % ("On       " if check else "To       ", plan.username, plan.host, target),
             "",
             "You sign in to ARC once for this %s. ssh asks, in this order:" % ("check" if check else "upload")]
    for number, step in enumerate(steps, 1):
        lines.append("  %d. %s" % (number, step[0]))
        lines += ["     " + more for more in step[1:]]
    if plan.known_hosts:
        lines.append("ssh checks ARC's key against the team's file %s; it does not ask." % plan.known_hosts)
    lines += list(WARNING_TEXT)
    lines.append("What you type goes to ssh only: this program does not see, keep or log it.")
    lines.append(NETWORK_TEXT)
    lines.append("If ssh warns that the REMOTE HOST IDENTIFICATION HAS CHANGED, stop here: nothing is sent.")
    lines.append("To stop, press Ctrl-C. " + ("A check sends, moves and deletes nothing." if check else
                                              "Files already copied stay on ARC."))
    return "\n".join(lines) + "\n"


def _tell(ctx, text):
    """A line in the Terminal window of a live upload (a detached worker's stderr is /dev/null)."""
    if ctx.terminal and text:
        with contextlib.suppress(OSError, ValueError):
            print(text, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------- results


def _expected(ctx):
    """{path: sha256} of every file ARC must hold (SHA256SUMS lists them, the manifest included)."""
    found = ctx.payload_digests()
    if not isinstance(found, dict):
        return None
    return {path: entry.get("sha256") for path, entry in found.items() if path != SUMS_NAME}


def difference(expected, digests):
    """The first path (bytewise) whose digest differs, is missing or is extra; None when equal."""
    if expected is None:
        return SUMS_NAME
    for path in sorted(set(expected) | set(digests), key=arc_payload.sort_key):
        if expected.get(path) != digests.get(path):
            return path
    return None


def _placed_name(plan, path):
    """The folder name in a DONE line, checked: under this upload's ARC folder, a name it may have."""
    parent, _slash, name = str(path).rpartition("/")
    if parent != plan.remote_base or not plan.takes_name(name):
        raise ProtocolError("the far end reported a folder this upload does not place")
    return name


def _verified(ctx, plan, name):
    files = plan.files
    shown = plan.shown(name)
    message = VERIFIED_DEMO.format(files=files) if ctx.demo else VERIFIED.format(files=files, path=shown)
    fields = {"verification": {"method": "sha256-on-arc", "algorithm": "SHA-256", "files": files, "utc": utc_now()},
              "progress": {"files": files, "files_done": files, "bytes": plan.bytes, "bytes_done": plan.bytes}}
    return message, fields


def _no_end(ctx, plan, attempt):
    """An attempt whose remote script did not finish: why, as a worker result (11.4 step 5)."""
    if not attempt.lines:
        if attempt.exit_code == 255:
            return "ssh_signin_failed", {}
        return "ssh_host_refused_commands", {}
    return "ssh_interrupted", {"incoming": plan.incoming}


def _changed_copy(plan, attempt):
    """The folder named by FAIL verify of step 1 (this upload's copy, put in place by an earlier run)."""
    text = attempt.fail_text
    start, end = _CHANGED_COPY
    path = text[len(start):-len(end)] if text.startswith(start) and text.endswith(end) else ""
    return _placed_name(plan, path)


def _not_writable(plan, attempt):
    """The shared folder named by FAIL access: <base> (the ARC folder, or simulated/) or its _incoming/, or,
    in the simulated area, the ARC folder above simulated/ when simulated/ is still to be made in it (review
    REG-02)."""
    path = attempt.fail_text
    if path not in (plan.remote_base, plan.remote_base + "/_incoming", plan.remote_top):
        raise ProtocolError("the far end named a folder this upload does not write into")
    return path


def _unreadable(plan, attempt):
    """The folder named by FAIL access in a check: one the check looks into (<base>, <base>/_incoming, review R2-06),
    a copy it compares (this upload's folder in _incoming, or a copy under <base> with a name this upload may have:
    NAME, <FOLDER>_copyN) or any folder in that copy (review R3-03), or a folder above <base> that this account may
    not enter."""
    path = attempt.fail_text
    base = plan.remote_base
    if path in (base, base + "/_incoming") or (path.startswith("/") and base.startswith(path.rstrip("/") + "/")):
        return path
    parts = path[len(base) + 1:].split("/") if path.startswith(base + "/") else []
    if parts[:2] == ["_incoming", plan.incoming.rpartition("/")[2]]:
        inside = parts[2:]
    elif parts and plan.takes_name(parts[0]):
        inside = parts[1:]
    else:
        inside = None
    if inside is None or any(part in ("", ".", "..") for part in inside):
        raise ProtocolError("the far end named a folder this check does not look into")
    return path


def _failure(plan, attempt, expected):
    """A FAIL line as a worker result: (reason, values)."""
    _kind, what, why = attempt.result
    if what == "verify":
        found = difference(expected, attempt.digests) or MANIFEST_NAME
        if attempt.found_in_place():
            # Step 1 of the script: the copy an earlier run of this upload put in place was changed on
            # ARC since. Sending the files again cannot change that (SSH-06).
            return "placed_changed", {"name": _changed_copy(plan, attempt), "file": found}
        return "verify_mismatch", {"file": found}
    if what == "access":
        return "shared_folder_not_writable", {"path": _not_writable(plan, attempt)}
    if what == "tools":
        return "remote_tools", {"text": why}
    return "remote_failed", {"what": REMOTE_WORDS.get(what, "finish the upload"), "text": why}


def _send_outcome(ctx, plan, attempt):
    if attempt.writer_error is not None:
        raise attempt.writer_error
    kind = attempt.result[0] if attempt.result else None
    if kind == "done":
        name = _placed_name(plan, attempt.result[2])
        # Design 13, row 14: ARC put the copy in place, and this Mac has not written it down yet.
        ctx.fault("ssh_after_done")
        ctx.placed_as(name)
        found = difference(_expected(ctx), attempt.digests)
        if found is not None:
            error = ctx.error("verify_mismatch", file=found)
            warnings = list(ctx.read().get("warnings") or [])
            warnings.append(_text(ctx, PLACED_BUT_DIFFERENT, path=plan.shown(name)))
            return Outcome("failed", ctx.text(error["message"]), error, fields={"warnings": warnings})
        message, fields = _verified(ctx, plan, name)
        _tell(ctx, DONE_TERMINAL.format(files=plan.files))
        _tell(ctx, "  " + plan.shown(name))
        return Outcome("verified", message, fields=fields)
    if kind == "conflict":
        return Outcome("conflict", ctx.text(_text(ctx, CONFLICT, incoming=plan.incoming)),
                       fields={"conflict_at": "placing"})
    if attempt.stopped or ctx.stop_requested():
        # Stopped (Stop upload, Ctrl-C, or the Terminal window closed): ssh ended with it. When ARC
        # may have the copy in its folder already, the files are not "in _incoming" (SSH-V01).
        if kind is None and attempt.may_be_placed():
            return Outcome("stopped", arc_records.message("stopped_placing", ctx.demo))
        raise StopRequested()
    if kind == "fail":
        reason, values = _failure(plan, attempt, _expected(ctx))
        if reason == "placed_changed":
            ctx.placed_as(values["name"])
            return ctx.failed(reason, path=plan.shown(values["name"]), file=values["file"])
        return ctx.failed(reason, **values)
    if kind is not None:
        raise ProtocolError("a check result in an upload")
    reason, values = _no_end(ctx, plan, attempt)
    return ctx.failed(reason, **values)


def _text(ctx, text, **values):
    """A text of this module as the upload shows it: a demo upload gets its demo wording (DEMO_TEXTS), which
    names the demo folder instead of ARC (review COR-05)."""
    if ctx.demo and text in DEMO_TEXTS:
        text = DEMO_TEXTS[text]
    return text.format(**values) if values else text


def _note(ctx, state, text, reason=None, looked=True):
    """What a check found, kept with the state: a "checks" entry for a verified upload, else the message. A check
    that looked found the copy missing or different; one that could not look (`looked` false) says why, with
    the fix of `reason` (review REG-01)."""
    if state == "verified":
        found = arc_records.check_result(False, text) if looked else arc_records.check_note(text, reason, ctx.demo)
        return Outcome("verified", None, check=found)
    return Outcome(state, text if looked else " ".join((text, arc_records.CHECK_NOT_LOOKED)))


def _check_outcome(ctx, plan, attempt, state):
    kind = attempt.result[0] if attempt.result else None
    if (attempt.stopped or ctx.stop_requested()) and kind != "done":
        raise StopRequested()
    if kind == "fail" and attempt.result[1] == "access":
        # A folder the check looks into that this account may not read: the check could not look, so the copy is
        # neither missing nor another upload's (review R2-06), nor changed when the folder is in it (review R3-03).
        text = _text(ctx, NOT_READABLE, path=_unreadable(plan, attempt))
        return _note(ctx, state, ctx.text(text), "folder_not_readable", looked=False)
    expected = _expected(ctx)
    if kind == "done":
        name = _placed_name(plan, attempt.result[2])
        found = difference(expected, attempt.digests)
        if found is not None:
            return _note(ctx, state, ctx.text(_text(ctx, NOT_MATCHING, path=plan.shown(name), file=found)))
        if state == "verified":
            text = STILL_THERE_DEMO.format(files=plan.files) if ctx.demo else \
                STILL_THERE.format(files=plan.files, path=plan.shown(name))
            return Outcome("verified", None, check=arc_records.check_result(True, text))
        ctx.placed_as(name)
        message, fields = _verified(ctx, plan, name)
        return Outcome("verified", message, fields=fields)
    if kind == "staged":
        complete = attempt.result[1] == "complete" and difference(expected, attempt.digests) is None
        return _note(ctx, state, ctx.text(_text(ctx, STAGED_COMPLETE if complete else STAGED_PARTIAL)))
    if kind == "missing":
        return _note(ctx, state, ctx.text(_text(ctx, MISSING)))
    if kind == "elsewhere":
        return _note(ctx, state, ctx.text(_text(ctx, ELSEWHERE)))
    if kind == "fail":
        reason, values = _failure(plan, attempt, expected)
        if reason in ("verify_mismatch", "placed_changed"):
            name = values.get("name") or plan.name
            return _note(ctx, state, ctx.text(_text(ctx, NOT_MATCHING, path=plan.shown(name), file=values["file"])))
        # The SSH computer lacks a tool, or a folder on the way to the copy is a link there: the check could not
        # look, in its own words (review R2-03).
        if reason == "remote_tools":
            text = _text(ctx, CHECK_TOOLS, host=plan.host, why=values["text"])
        elif attempt.result[1] == "prepare":
            reason, text = "folder_refused", _text(ctx, CHECK_FOLDER_REFUSED, why=values["text"])
        else:
            text = ctx.error(reason, **values)["message"]
    elif kind is not None:
        raise ProtocolError("an upload result in a check")
    else:
        # ssh ended before the script did: the sign-in did not work, the host did not run the commands, or the
        # connection ended. The check could not look (review REG-01).
        reason, values = _no_end(ctx, plan, attempt)
        text = {"ssh_interrupted": CHECK_CUT,
                "ssh_host_refused_commands": _text(ctx, CHECK_REFUSED, host=plan.host)}.get(reason) or \
            ctx.error(reason, **values)["message"]
    return _note(ctx, state, ctx.text(text), reason, looked=False)


# ---------------------------------------------------------------------- the transport interface


def _demo_fault(ctx, record):
    """True when this attempt shows the teaching fault drop_connection (once per upload)."""
    if not ctx.demo or record.get("demo_fault") != "drop_connection":
        return False
    if (record.get("ssh") or {}).get("demo_fault_used"):
        return False
    ctx.update(lambda current: _ssh_of(current).update(demo_fault_used=True))
    return True


def _demo_root_ready(ctx):
    """The demo folder that stands in for ARC (decision I6): made 0700 with its marker when missing; an
    existing folder is used as it is, or refused (config / demo_folder_unwritable) when it is not one
    this program made."""
    arc_records.ensure_demo_root(ctx.demo_root)


def mac_tar_changes(name):
    """True when this Mac's tar would write `name` with other bytes (decomposed, or with its combining
    marks reordered), so that the demo's check of the copy would fail."""
    previous = 0
    for character in str(name):
        combining = unicodedata.combining(character)
        if 0 < combining < previous:
            return True
        previous = combining
        code = ord(character)
        if any(low <= code <= high for low, high in _MAC_TAR_KEEPS):
            continue
        parts = unicodedata.normalize("NFD", character)
        if parts != character and unicodedata.normalize("NFC", parts) == character:
            return True
    return False


def _name_the_demo_cannot_send(entries):
    """The first path of the snapshot whose name this Mac's tar would change, or None."""
    for relative, _folder, _info in entries:
        if not relative.isascii() and mac_tar_changes(relative):
            return relative
    return None


def send(ctx):
    """Send the snapshot (action send; the record is in "preparing" with the snapshot ready)."""
    record = ctx.read()
    destination = record.get("destination") if isinstance(record.get("destination"), dict) else {}
    plan = Plan(ctx, record, "upload-copy" if destination.get("new_copy") else "upload")
    runner = plan.runner()
    if ctx.demo:
        changed = _name_the_demo_cannot_send(payload_entries(ctx.payload_dir))
        if changed is not None:
            return ctx.failed("demo_ssh_name", name=changed)
        _demo_root_ready(ctx)
        ctx.set_state("signing_in", DEMO_SIGN_IN)
        pause = DEMO_SIGN_IN_SECONDS * arc_worker.demo_pace() / arc_worker.DEFAULT_DEMO_PACE
        if pause and ctx.wait(pause):
            raise StopRequested()
    else:
        ctx.set_state("signing_in", ctx.text(arc_records.MESSAGES["signing_in_ssh"]))
        _tell(ctx, terminal_text(plan))
    ctx.check_stop()
    fault = _demo_fault(ctx, record)
    attempt = run_attempt(ctx, plan, True, fault=fault, runner=runner)
    return _send_outcome(ctx, plan, attempt)


def check(ctx):
    """Look at what is on ARC (action check): nothing is sent or moved, the state is kept while it runs."""
    record = ctx.read()
    plan = Plan(ctx, record, "check")
    runner = plan.runner()
    if ctx.demo:
        _demo_root_ready(ctx)
    else:
        _tell(ctx, terminal_text(plan, check=True))
    attempt = run_attempt(ctx, plan, False, runner=runner)
    return _check_outcome(ctx, plan, attempt, record.get("state"))
