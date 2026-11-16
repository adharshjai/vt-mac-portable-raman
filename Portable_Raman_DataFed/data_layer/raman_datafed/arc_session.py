"""Which scan sessions may go to ARC, and what an upload of one carries (PROTOCOL.md 5.6).

- session_folders(): the folders of HOME/scan_sessions that are sessions (newest first);
- check(): the readiness rule R1 to R7, how the scan ended, measured or simulated, the dark
  spectra that go with it, and the files that go (and those left out, with the reason);
- policy(): where a session of a kind may go, live and in the demo.

Reads files only. The running programs are listed once per command (`ps`, through doctor).
"""

from __future__ import annotations

import math
import os
import re
import stat
import time
from pathlib import Path

from . import arc_records, doctor, metadata
from .arc_records import refusal

PLANNED_POINTS = {"single": 1, "1d-x": 13, "1d-y": 13, "2d": 124}
# R1. Always used with fullmatch(): in Python "$" also matches before a newline at the end.
SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_DEPTH = 4
DARK_FOLDER = "dark"
FUTURE_LIMIT_SECONDS = 86400
CONTROLLER_LOCK = "controller.lock"
# Files the controller publishes through a temporary <name>.<6 letters or digits> beside them.
_CONTROLLER_FILES = ("session.json", "request.txt", "response.txt", "cancel.txt", "failure.txt", "advanced.txt",
                     "laser_log_combined.txt", "command.txt")
_CONTROLLER_TEMPORARY = re.compile(
    r"^(%s|point_\d{4,}\.json)\.[A-Za-z0-9]{6}$" % "|".join(re.escape(name) for name in _CONTROLLER_FILES))
_WINDOWS_FILES = ("Thumbs.db", "desktop.ini")

END_WORDS = {"finished": "finished", "stopped": "stopped early ({points} of {planned})",
             "failed": "ended with an error", "ended": "ended (no end mark)"}

KIND_REAL, KIND_SIMULATED, KIND_UNKNOWN, KIND_MIXED = "real", "simulated", "unknown", "mixed"
AREA_REAL, AREA_SIMULATED = "real", "simulated"


def _lstat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def is_session_entry(scan_sessions, name):
    """A folder of 4.1: a real directory (not a link) whose name starts with neither "_" nor "."."""
    if not isinstance(name, str) or not name or "/" in name or "\0" in name or name in (".", ".."):
        return False
    if name.startswith((".", "_")):
        return False
    info = _lstat(Path(scan_sessions) / name)
    return info is not None and stat.S_ISDIR(info.st_mode)


def _started_key(folder):
    """Sort key: session.json's started_utc, else the folder's modification time."""
    try:
        started = metadata.read_json_object(folder / "session.json").get("started_utc")
    except metadata.MetadataError:
        started = None
    if isinstance(started, str) and started:
        return (1, started)
    info = _lstat(folder)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(info.st_mtime)) if info else ""
    return (1, stamp + ".000Z") if stamp else (0, "")


def session_folders(paths):
    """The session folders of HOME/scan_sessions (4.1), newest first."""
    root = Path(paths.scan_sessions)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    folders = [root / name for name in names if is_session_entry(root, name)]
    return sorted(folders, key=_started_key, reverse=True)


# ---------------------------------------------------------------------- the files of a session


def plain_name(name):
    """A path part that can be sent safely: valid UTF-8, no control character, no backslash."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(name) and not any(ord(character) < 32 or ord(character) == 127 or character == "\\"
                                  for character in name)


def excluded_reason(name, top, folder=False):
    """Why an entry is left out of an upload (4.6), or None when it goes."""
    if name.startswith("."):
        return "a hidden folder" if folder else "a hidden file"
    if name == "Icon\r":
        return "a Finder file"
    if name in _WINDOWS_FILES:
        return "a file Windows makes"
    if top and name in (arc_records.MANIFEST_NAME, arc_records.SUMS_NAME):
        return "the list of an earlier upload"
    if top and not folder and _CONTROLLER_TEMPORARY.fullmatch(name):
        return "a temporary file of the controller"
    return None


class Listing:
    """What a session folder holds: `files` [(relative path, lstat)] that go, `excluded`
    [{"path", "reason"}], the newest modification time (ns), the first R6 problem as
    (reason, name) or None, and `folder_id`: the folder's own (device, inode) from lstat."""

    def __init__(self):
        self.files = []
        self.excluded = []
        self.newest_ns = 0
        self.problem = None
        self.folder_id = None

    def bytes(self):
        return sum(info.st_size for _path, info in self.files)

    def signature(self):
        """What must not change while the session is copied: the folder itself (a folder swapped for a
        link is another one), and every file that goes, with its size, modification time and inode."""
        return self.folder_id, sorted((path, info.st_size, info.st_mtime_ns, info.st_ino)
                                      for path, info in self.files)


def list_folder(folder):
    """Walk a session folder with lstat (links are never followed)."""
    folder = Path(folder)
    listing = Listing()
    info = os.lstat(folder)
    listing.newest_ns = info.st_mtime_ns
    listing.folder_id = (info.st_dev, info.st_ino)

    def note(reason, name):
        if listing.problem is None:
            listing.problem = (reason, name)

    def walk(directory, parts, excluded_above):
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            note("special_file", "/".join(parts) or folder.name)
            return
        for entry in entries:
            relative_parts = parts + [entry.name]
            relative = "/".join(relative_parts)
            try:
                entry_info = entry.stat(follow_symlinks=False)
            except OSError:
                note("special_file", relative)
                continue
            listing.newest_ns = max(listing.newest_ns, entry_info.st_mtime_ns)
            is_dir = stat.S_ISDIR(entry_info.st_mode)
            if stat.S_ISLNK(entry_info.st_mode):
                note("link", relative)
                continue
            if not (is_dir or stat.S_ISREG(entry_info.st_mode)):
                note("special_file", relative)
                continue
            reason = None if excluded_above else excluded_reason(entry.name, not parts, is_dir)
            left_out = excluded_above or reason is not None
            if reason is not None:
                listing.excluded.append({"path": relative, "reason": reason})
            if not left_out:
                if not plain_name(entry.name):
                    note("odd_name", relative)
                elif len(relative_parts) > MAX_DEPTH:
                    note("special_file", relative)
                elif not parts and entry.name.lower() == DARK_FOLDER:
                    note("name_clash", relative)
                elif not is_dir:
                    listing.files.append((relative, entry_info))
            if is_dir:
                walk(entry.path, relative_parts, left_out)

    walk(folder, [], False)
    return listing


# ---------------------------------------------------------------------- how the scan ended, and its kind


def end_of(folder, scan_mode, points):
    """(end, planned) of 4.3."""
    planned = PLANNED_POINTS.get(scan_mode)
    folder = Path(folder)
    if os.path.lexists(folder / "failure.txt"):
        return "failed", planned
    if planned is not None and points >= planned:
        return "finished", planned
    if planned is not None and os.path.lexists(folder / "cancel.txt"):
        return "stopped", planned
    return "ended", planned


def end_words(end, points, planned):
    return END_WORDS[end].format(points=points, planned=planned)


def source_kind(value):
    """real (both flags false), simulated (either true) or None for a simulated object of 4.4."""
    if isinstance(value, dict) and isinstance(value.get("backend"), bool) and isinstance(value.get("motor"), bool):
        return KIND_SIMULATED if value["backend"] or value["motor"] else KIND_REAL
    return None


def combine_kinds(kinds):
    found = {kind for kind in kinds if kind is not None}
    if found == {KIND_REAL, KIND_SIMULATED}:
        return KIND_MIXED
    return found.pop() if found else KIND_UNKNOWN


def policy(kind, demo, include_simulated, declare_real=False):
    """(area, None) when a session of `kind` may go, else (None, refusal reason) (4.4)."""
    if kind == KIND_MIXED:
        return None, "mixed"
    if kind == KIND_REAL or (kind == KIND_UNKNOWN and declare_real):
        return AREA_REAL, None
    if demo or include_simulated:
        return AREA_SIMULATED, None
    return None, ("simulated_not_allowed" if kind == KIND_SIMULATED else "simulation_unknown")


# ---------------------------------------------------------------------- dark spectra


def _dark_name_usable(name):
    return isinstance(name, str) and plain_name(name) and not name.startswith(".") and Path(name).name == name \
        and name not in (".", "..")


def darks(paths, names, backend_simulated):
    """The dark spectra a session names (4.5): one entry per distinct name, with what goes.

    {"file", "on_this_mac", "kind", "sent", "reason", "txt" (Path or None), "description" (Path or
    None), "warning" (a sentence or None)}.
    """
    entries = []
    for name in names:
        entry = {"file": name, "on_this_mac": False, "kind": None, "sent": False, "reason": None, "txt": None,
                 "description": None, "warning": None}
        path = Path(paths.dark_measurements) / name if _dark_name_usable(name) else None
        info = _lstat(path) if path is not None else None
        if info is None or not stat.S_ISREG(info.st_mode):
            entry["warning"] = "The dark spectrum %s that this session names is not on this Mac, so it was not " \
                               "sent." % name
            entries.append(entry)
            continue
        entry["on_this_mac"] = True
        kind, why = metadata.dark_kind(path)
        entry["kind"] = kind
        reason = metadata.unpaired_reason(kind, why, backend_simulated)
        if reason is not None:
            entry["reason"] = reason
            shown = reason[len("dark not used: "):] if reason.startswith("dark not used: ") else reason
            entry["warning"] = "The dark spectrum %s was not sent: %s." % (name, shown)
            entries.append(entry)
            continue
        entry["sent"] = True
        entry["txt"] = path
        description = metadata.dark_description(path)
        described = _lstat(description)
        if description != path and described is not None and stat.S_ISREG(described.st_mode):
            entry["description"] = description
        entries.append(entry)
    return entries


def public_darks(entries):
    """The darks as the replies show them."""
    return [{"file": entry["file"], "on_this_mac": entry["on_this_mac"], "kind": entry["kind"],
             "sent": entry["sent"], "reason": entry["reason"]} for entry in entries]


# ---------------------------------------------------------------------- the readiness rule


def controller_idle(paths, asked):
    """True when --controller-idle may be used: asked, and HOME/controller.lock names a live process."""
    if not asked:
        return False
    try:
        with open(Path(paths.home) / CONTROLLER_LOCK, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
        pid = int(first)
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # EPERM: the process exists but belongs to someone else.
        return True
    except OSError:
        return False
    return True


def running_commands():
    """The command lines of the running programs, or None when they cannot be listed."""
    try:
        return doctor._running_commands()
    except Exception:  # noqa: BLE001 - when ps fails, R4 is skipped with a warning
        return None


def _name_character(character):
    return bool(character) and (character.isalnum() or character in "._-")


def _mentions(line, text):
    """True when `text` appears in `line` as a whole word or path (not as part of a longer name)."""
    if not text:
        return False
    start = line.find(text)
    while start >= 0:
        before = line[start - 1:start] if start else ""
        after = line[start + len(text):start + len(text) + 1]
        if not _name_character(after) and not (_name_character(before) and text[0] != "/"):
            return True
        start = line.find(text, start + 1)
    return False


class Check:
    """The result of check(): everything the replies, the record and the snapshot need.

    `refusal` is the first rule of R1 to R6 that failed (an ArcError) or None; R7 is `policy()`.
    """

    def __init__(self, folder):
        self.folder = Path(folder)
        self.name = self.folder.name
        self.session = None
        self.points = 0
        self.planned = None
        self.end = "ended"
        self.kind = KIND_UNKNOWN
        self.backend_simulated = None
        self.dark_names = []
        self.darks = []
        self.listing = None
        self.refusal = None
        self.retry_after_seconds = 0
        self.running = False
        self.settling_seconds_ago = None
        self.warnings = []
        self.controller_idle_used = False

    @property
    def scan_mode(self):
        mode = self.session.get("scan_mode") if isinstance(self.session, dict) else None
        return mode if mode in PLANNED_POINTS else metadata.scan_mode_from_folder(self.name)

    def end_text(self):
        if self.running:
            return "still running"
        if self.settling_seconds_ago is not None:
            return "ended %d s ago" % self.settling_seconds_ago
        return end_words(self.end, self.points, self.planned)

    def summary(self, declared=False):
        """The "session" object of a record, a plan and the manifest (less `simulated`)."""
        session = self.session if isinstance(self.session, dict) else {}
        sample = session.get("sample_id")
        return {"folder": self.name, "session_id": session.get("session_id"), "scan_mode": self.scan_mode,
                "started_utc": session.get("started_utc") if isinstance(session.get("started_utc"), str) else None,
                "sample_id": sample.strip() if isinstance(sample, str) else None, "points": self.points,
                "points_planned": self.planned, "end": self.end, "kind": self.kind,
                "kind_declared_by_uploader": bool(declared)}

    def sent_darks(self):
        return [entry for entry in self.darks if entry["sent"]]

    def file_count(self):
        """Files that go: the session's, the darks', the manifest and SHA256SUMS."""
        files = len(self.listing.files) if self.listing else 0
        for entry in self.sent_darks():
            files += 1 + (entry["description"] is not None)
        return files + 2

    def byte_count(self):
        """Bytes of the session's files and the darks' (the manifest and SHA256SUMS not counted)."""
        total = self.listing.bytes() if self.listing else 0
        for entry in self.sent_darks():
            for path in (entry["txt"], entry["description"]):
                if path is not None:
                    info = _lstat(path)
                    total += info.st_size if info else 0
        return total


def check(paths, folder, settle_seconds, controller_idle_used=False, full=False, declare_real=False,
          commands=None, now=None):
    """Evaluate R1 to R6, how the scan ended, its kind and its darks (4.2 to 4.5).

    `full` reads every point's capture block (arc-upload and the worker); without it the kind and
    the darks come from session.json alone (arc-sessions). `commands`: the running programs'
    command lines (running_commands()), None when `ps` failed. `now`: seconds, for tests.
    """
    result = Check(folder)
    result.controller_idle_used = bool(controller_idle_used)
    name = result.name
    shown = name if plain_name(name) else repr(name)
    if not SESSION_NAME.fullmatch(name) or name.lower() == "simulated":
        result.refusal = refusal("odd_name", name=shown)
        return result
    # The spectra, the files and how the scan ended are counted before session.json is read, so that a
    # folder this edition did not record (until it is imported) is listed with them, not as empty.
    try:
        point_paths = metadata.point_files(result.folder)
        result.listing = list_folder(result.folder)
    except OSError:
        # The folder went away while it was looked at.
        result.refusal = refusal("unknown_session", folder=name)
        return result
    result.points = len(point_paths)
    result.end, result.planned = end_of(result.folder, result.scan_mode, result.points)
    if not os.path.lexists(result.folder / "session.json"):
        result.refusal = refusal("no_session_json", folder=name)
        return result
    try:
        result.session = metadata.read_session(result.folder)
    except metadata.MetadataError as error:
        result.refusal = refusal("unreadable_session", folder=name, why=str(error).rstrip("."), detail=error.detail)
        return result
    # session.json may name the scan mode: the planned count again.
    result.end, result.planned = end_of(result.folder, result.scan_mode, result.points)

    sources = [result.session.get("simulated")]
    names = []

    def add_dark(value):
        if isinstance(value, str) and value and value not in names:
            names.append(value)

    add_dark(result.session.get("dark_file"))
    if full:
        for path in point_paths:
            try:
                capture = metadata.read_json_object(path).get("capture")
            except metadata.MetadataError:
                continue
            if isinstance(capture, dict):
                sources.append(capture.get("simulated"))
                add_dark(capture.get("dark_file"))
    result.kind = combine_kinds(source_kind(value) for value in sources)
    backends = {value["backend"] for value in sources if source_kind(value) is not None}
    if len(backends) == 1:
        # The catalog's own rule: a dark is paired by the capture's simulated.backend.
        result.backend_simulated = backends.pop()
    elif not backends and declare_real and result.kind == KIND_UNKNOWN:
        result.backend_simulated = False
    result.dark_names = names
    result.darks = darks(paths, names, result.backend_simulated)
    result.warnings.extend(entry["warning"] for entry in result.darks if entry["warning"])

    if not point_paths:
        result.refusal = refusal("no_points", folder=name)
        return result

    if commands is None:
        result.warnings.insert(0, "The running programs could not be listed, so it was not checked whether a "
                                  "program still has this session open.")
    else:
        texts = {str(result.folder), os.path.realpath(result.folder), result.session.get("session_id")}
        if any(_mentions(line, text) for line in commands for text in texts):
            result.running = True
            result.refusal = refusal("running", folder=name)
            return result

    now = time.time() if now is None else now
    newest = result.listing.newest_ns / 1e9
    if not controller_idle_used:
        if newest > now + FUTURE_LIMIT_SECONDS:
            result.refusal = refusal("future_time")
            return result
        # Up to a day in the future counts as just now.
        age = max(0.0, now - newest)
        if age < settle_seconds:
            result.retry_after_seconds = max(1, int(math.ceil(settle_seconds - age)))
            result.settling_seconds_ago = int(age)
            result.refusal = refusal("settling", seconds=result.retry_after_seconds,
                                     extra={"retry_after_seconds": result.retry_after_seconds})
            return result

    if result.listing.problem is not None:
        reason, entry = result.listing.problem
        entry = entry if plain_name(entry) else repr(entry)
        if reason == "name_clash":
            result.refusal = refusal("name_clash", folder=name)
        else:
            result.refusal = refusal(reason, name=entry)
        return result
    return result


def check_policy(result, demo, include_simulated, declare_real):
    """R7: (area, None) or (None, ArcError)."""
    if declare_real and result.kind != KIND_UNKNOWN:
        return None, refusal("declare_real_not_unknown")
    area, reason = policy(result.kind, demo, include_simulated, declare_real)
    return area, (refusal(reason) if reason else None)
