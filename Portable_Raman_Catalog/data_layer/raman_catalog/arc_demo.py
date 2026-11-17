"""The demo's stand-ins for ARC and for Globus (PROTOCOL.md 5.6, design section 12).

A demo upload runs the same worker, records, states and checks as a live one. Only the two far
ends are stand-ins on this Mac:

- The demo root, HOME/<arc_upload.demo_folder> (default HOME/data_catalog/arc_demo), stands in for
  ARC's file system: <demo root>/projects/... is what /projects/... is on ARC
  (arc_paths.demo_local_path). Both transports land in this one folder. It is made, or accepted as
  it is, by arc_records.ensure_demo_root (decision I6): a missing folder is made with mode 0700 and
  the marker file .raman_arc_demo; an existing one must be this user's own real folder, empty or
  marked, and its mode is never changed.
- DemoGlobus stands in for Globus. It is the client interface of arc_globus (design 10.9) with
  two collections: this Mac (DEMO_MAC_COLLECTION, absolute paths inside the controller's folder)
  and ARC (DEMO_ARC_COLLECTION, whose /X is <demo root>/<collection root>/X). It keeps its state
  in <demo root>/.demo_globus/: submissions.json (request number -> task) and tasks/<task id>.json.
  So a worker that dies and the next one see the same "service", and a request number it already
  knows is answered with Duplicate and the same task, as Globus does. Each get_task copies the
  files that are due by then (RAMAN_ARC_DEMO_PACE seconds per file) and computes their MD5
  checksums for successful_transfers. Errors carry Globus's codes (ClientError.NotFound 404,
  Exists 409, ExternalError.MkdirFailed.Exists 502).

The teaching faults of a demo upload (--demo-fault; once per upload): lose_submit_reply (the
request is kept, then its reply is lost) and fail_task_once (the task fails halfway).

No network, no account, no browser. Standard library and the package only.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import stat
import time
import uuid
from pathlib import Path

from . import arc_paths, arc_records, arc_worker
from .arc_globus import GlobusCallError

DEMO_MAC_COLLECTION = arc_records.DEMO_MAC_COLLECTION
DEMO_ARC_COLLECTION = arc_records.DEMO_ARC_COLLECTION
SERVICE_FOLDER = ".demo_globus"
SERVICE_LOCK = "service.lock"
SUBMISSIONS_FILE = "submissions.json"
TASKS_FOLDER = "tasks"
IDENTITY = "demo (no account)"
SIGN_IN_MESSAGE = "Demo: no sign-in. A real upload opens your browser here."
# The demo sign-in's pause at the default pace; it scales with RAMAN_ARC_DEMO_PACE (pace 0: none).
SIGN_IN_SECONDS = 1.5
GLOBUS_FAULTS = ("lose_submit_reply", "fail_task_once")
LOST_CONNECTION = "the connection to this Mac was lost"
LOCK_SECONDS = 10
TRANSFERS_PAGE = 100
LISTING_LIMIT = 100000
BLOCK = 1 << 20
_TASK_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def ensure_demo_root(root):
    """The demo root, ready for a demo upload (arc_records.ensure_demo_root, decision I6; reviews SEC-05,
    COR-09): made when missing (mode 0700 and the marker file: it holds copies of the sessions); a
    folder that exists is used as it is, never chmodded, and refused (config / demo_folder_unwritable)
    when it is a link, another user's, or holds other files without the marker."""
    return arc_records.ensure_demo_root(root)


def service_folder(root):
    """Where the demo Globus service keeps its state."""
    return Path(root) / SERVICE_FOLDER


def demo_scale():
    """How the demo's pauses scale: 1 at the default pace (0.1 s per file), 0 at pace 0."""
    return arc_worker.demo_pace() / arc_worker.DEFAULT_DEMO_PACE


def _iso(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _not_found(path):
    return GlobusCallError(404, "ClientError.NotFound", "%s does not exist" % path)


class DemoGlobus:
    """The demo's Globus service: the client interface of arc_globus.GlobusFlow, acting on this Mac."""

    def __init__(self, demo_root, collection_root="/projects", mac_root=None, mac_collection=DEMO_MAC_COLLECTION,
                 arc_collection=DEMO_ARC_COLLECTION, fault=None, on_fault=None, pace=None, clock=time.time,
                 sleep=time.sleep):
        self.root = ensure_demo_root(demo_root)
        self.collection_root = collection_root or "/"
        self.mac_root = Path(mac_root).resolve() if mac_root else None
        self.mac_collection = str(mac_collection).lower()
        self.arc_collection = str(arc_collection).lower()
        self.fault = fault if fault in GLOBUS_FAULTS else None
        self.on_fault = on_fault
        self.pace = arc_worker.demo_pace() if pace is None else max(0.0, float(pace))
        self.clock = clock
        self.sleep = sleep
        self.service = service_folder(self.root)
        (self.service / TASKS_FOLDER).mkdir(parents=True, exist_ok=True)
        # The top of ARC's collection always exists on the real service.
        self._local(self.arc_collection, "/").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ sign-in

    def login(self, gare=None, force_login=False):
        """No account and no browser: a short pause stands in for the sign-in."""
        pause = SIGN_IN_SECONDS * (self.pace / arc_worker.DEFAULT_DEMO_PACE)
        if pause > 0:
            self.sleep(pause)

    def identity(self):
        return IDENTITY

    def identity_id(self):
        """No account, so no Globus ID: the demo's collections have no owner to compare it with."""
        return None

    def logout(self):
        """Nothing to revoke: the demo holds no token."""

    # ------------------------------------------------------------------ collections and paths

    def _local(self, collection, path):
        """The folder or file on this Mac that a collection's path stands for."""
        if not isinstance(path, str) or not path.startswith("/"):
            raise GlobusCallError(400, "ClientError.BadRequest", "A path must start with /")
        parts = [part for part in path.split("/") if part]
        if any(part in (".", "..") for part in parts):
            raise GlobusCallError(400, "ClientError.BadRequest", "A path must not hold . or ..")
        clean = "/" + "/".join(parts)
        collection = str(collection).lower()
        if collection == self.mac_collection:
            local = Path(clean)
            if self.mac_root is not None and local != self.mac_root and self.mac_root not in local.parents:
                raise GlobusCallError(403, "ClientError.PermissionDenied",
                                      "This Mac's demo collection shares only the controller's folder")
            return local
        if collection == self.arc_collection:
            top = "" if self.collection_root == "/" else self.collection_root.rstrip("/")
            return arc_paths.demo_local_path(self.root, (top + clean) if clean != "/" else (top or "/"))
        raise GlobusCallError(404, "ClientError.NotFound", "There is no collection %s" % collection)

    def get_endpoint(self, collection):
        collection = str(collection).lower()
        if collection == self.mac_collection:
            return {"DATA_TYPE": "endpoint", "id": collection, "display_name": "This Mac (demo)",
                    "is_globus_connect": True, "gcp_connected": True, "gcp_paused": False}
        if collection == self.arc_collection:
            return {"DATA_TYPE": "endpoint", "id": collection, "display_name": "ARC (demo)",
                    "is_globus_connect": False, "gcp_connected": None, "gcp_paused": None}
        raise GlobusCallError(404, "ClientError.NotFound", "There is no collection %s" % collection)

    @staticmethod
    def _entry(path, name=None):
        info = os.lstat(path)
        link = None
        kind = None
        if stat.S_ISLNK(info.st_mode):
            link = os.readlink(path)
            try:
                info = os.stat(path)
            except OSError:
                kind = "invalid_symlink"
        if kind is None:
            kind = "dir" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "special"
        return {"DATA_TYPE": "file", "name": name or Path(path).name, "type": kind, "link_target": link,
                "size": info.st_size, "permissions": "%04o" % stat.S_IMODE(info.st_mode),
                "last_modified": _iso(info.st_mtime)}

    def ls(self, collection, path, offset=0, limit=1000):
        """One page of a folder's entries (hidden ones included), in name order."""
        local = self._local(collection, path)
        try:
            names = sorted(os.listdir(local))
        except FileNotFoundError:
            raise _not_found(path) from None
        except NotADirectoryError:
            raise GlobusCallError(502, "ExternalError.DirListingFailed.NotDirectory", "%s is not a folder" % path) \
                from None
        except PermissionError:
            raise GlobusCallError(403, "ClientError.PermissionDenied", "%s cannot be listed" % path) from None
        if len(names) > LISTING_LIMIT:
            raise GlobusCallError(502, "ExternalError.DirListingFailed.DirectorySizeLimit",
                                  "%s holds more than %d entries" % (path, LISTING_LIMIT))
        entries = []
        for name in names[int(offset):int(offset) + int(limit)]:
            with contextlib.suppress(FileNotFoundError):
                entries.append(self._entry(local / name, name))
        return entries

    def stat(self, collection, path):
        local = self._local(collection, path)
        try:
            return self._entry(local)
        except FileNotFoundError:
            raise _not_found(path) from None

    def mkdir(self, collection, path):
        local = self._local(collection, path)
        try:
            os.mkdir(local)
        except FileExistsError:
            raise GlobusCallError(502, "ExternalError.MkdirFailed.Exists", "Path already exists") from None
        except FileNotFoundError:
            raise _not_found(str(Path(path).parent)) from None
        return {"DATA_TYPE": "mkdir_result", "code": "DirectoryCreated", "message": "The directory was created."}

    def rename(self, collection, old_path, new_path):
        """One rename that never replaces anything: Exists when the new name is taken."""
        source = self._local(collection, old_path)
        target = self._local(collection, new_path)
        with self._lock():
            if not os.path.lexists(source):
                raise _not_found(old_path)
            if os.path.lexists(target):
                raise GlobusCallError(409, "Exists", "Destination path already exists")
            if not target.parent.is_dir():
                raise _not_found(str(Path(new_path).parent))
            os.rename(source, target)
        return {"DATA_TYPE": "result", "code": "FileRenamed", "message": "File or directory renamed."}

    # ------------------------------------------------------------------ the service's state

    def _lock(self):
        return arc_records.flock(self.service / SERVICE_LOCK, LOCK_SECONDS, "The demo Globus service")

    def _read_json(self, path, default):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as error:
            raise GlobusCallError(500, "ServiceUnavailable", "The demo service's state cannot be read") from error
        return value if isinstance(value, type(default)) else default

    def _write_json(self, path, value):
        arc_records.write_private_file(path, (json.dumps(value, indent=1, sort_keys=True) + "\n").encode("utf-8"))

    def _task_path(self, task_id):
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise GlobusCallError(404, "ClientError.NotFound", "Task %s not found" % (task_id,))
        return self.service / TASKS_FOLDER / (task_id + ".json")

    def _read_task(self, task_id):
        path = self._task_path(task_id)
        task = self._read_json(path, {})
        if not task:
            raise GlobusCallError(404, "ClientError.NotFound", "Task %s not found" % task_id)
        return task

    def _write_task(self, task):
        self._write_json(self._task_path(task["task_id"]), task)

    @staticmethod
    def _public(task):
        return {key: value for key, value in task.items() if not key.startswith("_")}

    # ------------------------------------------------------------------ submission

    def submission_id(self):
        return str(uuid.uuid4())

    def _items(self, body):
        """The files of the transfer's items, as Globus expands a recursive item."""
        items, problem = [], None
        for item in body.get("DATA") or []:
            source = self._local(self.mac_collection, item.get("source_path"))
            destination = str(item.get("destination_path") or "")
            self._local(self.arc_collection, destination)
            if not item.get("recursive"):
                if source.is_file() and not source.is_symlink():
                    items.append({"source": str(source), "destination": destination, "size": source.stat().st_size})
                else:
                    problem = "the file %s is not on this Mac" % source.name
                continue
            if not source.is_dir() or source.is_symlink():
                problem = "the folder %s is not on this Mac" % source.name
                continue
            for directory, folders, files in os.walk(source):
                folders.sort()
                for name in sorted(files):
                    path = Path(directory) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    relative = path.relative_to(source).as_posix()
                    items.append({"source": str(path), "destination": destination.rstrip("/") + "/" + relative,
                                  "size": path.stat().st_size})
        return items, problem

    def submit(self, body):
        """POST /transfer: a request number already received answers Duplicate with the same task."""
        body = body if isinstance(body, dict) else {}
        submission = body.get("submission_id")
        if body.get("DATA_TYPE") != "transfer" or not isinstance(submission, str) or not submission:
            raise GlobusCallError(400, "ClientError.BadRequest",
                                  "A transfer needs DATA_TYPE transfer and a submission_id")
        if str(body.get("source_endpoint")).lower() != self.mac_collection or \
                str(body.get("destination_endpoint")).lower() != self.arc_collection:
            raise GlobusCallError(404, "ClientError.NotFound", "The demo knows only its two collections")
        fault = None
        with self._lock():
            path = self.service / SUBMISSIONS_FILE
            submissions = self._read_json(path, {})
            known = submissions.get(submission)
            if isinstance(known, dict) and known.get("task_id"):
                known["posts"] = int(known.get("posts") or 1) + 1
                self._write_json(path, submissions)
                return {"DATA_TYPE": "transfer_result", "code": "Duplicate", "task_id": known["task_id"],
                        "submission_id": submission, "request_id": secrets.token_hex(5),
                        "message": "A transfer with this submission id was already received."}
            items, problem = self._items(body)
            now = self.clock()
            task = {
                "DATA_TYPE": "task", "type": "TRANSFER", "task_id": str(uuid.uuid4()), "submission_id": submission,
                "label": body.get("label"), "status": "ACTIVE", "nice_status": "Queued",
                "nice_status_short_description": None, "source_endpoint_id": self.mac_collection,
                "destination_endpoint_id": self.arc_collection, "request_time": _iso(now), "completion_time": None,
                "deadline": body.get("deadline"), "files": len(items), "files_transferred": 0, "files_skipped": 0,
                "bytes_transferred": 0, "faults": 0, "fatal_error": None, "is_paused": False,
                "verify_checksum": body.get("verify_checksum"),
                "_demo": {"started": now, "items": items, "done": 0, "fail_at": None, "transfers": [], "events": [],
                          "problem": problem},
            }
            if self.fault is not None:
                fault, self.fault = self.fault, None
                if fault == "fail_task_once":
                    task["_demo"]["fail_at"] = max(1, len(items) // 2)
            self._write_task(task)
            submissions[submission] = {"task_id": task["task_id"], "posts": 1}
            self._write_json(path, submissions)
        if fault is not None and self.on_fault is not None:
            self.on_fault(fault)
        if fault == "lose_submit_reply":
            # The service has the request; its answer never arrives.
            raise GlobusCallError(0, "network", "the reply was lost on its way back")
        return {"DATA_TYPE": "transfer_result", "code": "Accepted", "task_id": task["task_id"],
                "submission_id": submission, "request_id": secrets.token_hex(5),
                "message": "The transfer has been accepted and a task has been created and queued for execution"}

    def posts_of(self, submission):
        """How many times a request number was posted (for the tests and the recovery checks)."""
        known = self._read_json(self.service / SUBMISSIONS_FILE, {}).get(submission)
        return int(known.get("posts") or 0) if isinstance(known, dict) else 0

    # ------------------------------------------------------------------ tasks

    def _fail(self, task, code, description):
        now = self.clock()
        task.update(status="FAILED", nice_status=None, completion_time=_iso(now),
                    fatal_error={"code": code, "description": description})
        task["_demo"]["events"].append({"DATA_TYPE": "event", "code": code, "description": description,
                                        "details": description, "is_error": True, "time": _iso(now)})

    def _copy(self, item):
        """Copy one file to the demo ARC folder (times kept); returns (bytes, MD5)."""
        source = Path(item["source"])
        target = self._local(self.arc_collection, item["destination"])
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.md5(), 0
        reader = os.open(str(source), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(reader)
            writer = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
            try:
                while True:
                    block = os.read(reader, BLOCK)
                    if not block:
                        break
                    digest.update(block)
                    size += len(block)
                    view = memoryview(block)
                    while view:
                        view = view[os.write(writer, view):]
                os.fsync(writer)
            finally:
                os.close(writer)
        finally:
            os.close(reader)
        os.utime(str(target), ns=(info.st_atime_ns, info.st_mtime_ns))
        return size, digest.hexdigest()

    def _advance(self, task):
        """Copy the files that are due by now; True when the task changed."""
        demo = task["_demo"]
        if task.get("status") not in ("ACTIVE", "INACTIVE"):
            return False
        if demo.get("problem"):
            self._fail(task, "SOURCE_MISSING", demo["problem"])
            return True
        items = demo["items"]
        due = len(items) if self.pace <= 0 else min(len(items), int((self.clock() - demo["started"]) / self.pace))
        if demo.get("fail_at") is not None:
            due = min(due, demo["fail_at"])
        changed = False
        while demo["done"] < due:
            item = items[demo["done"]]
            try:
                size, md5 = self._copy(item)
            except OSError:
                self._fail(task, "SOURCE_UNREADABLE", "a file of the upload copy on this Mac could not be read")
                return True
            demo["transfers"].append({"DATA_TYPE": "successful_transfer", "source_path": item["source"],
                                      "destination_path": item["destination"], "size": size, "checksum": md5,
                                      "checksum_algorithm": "MD5"})
            demo["done"] += 1
            task["files_transferred"] += 1
            task["bytes_transferred"] += size
            changed = True
        if changed and task.get("nice_status") == "Queued":
            task["nice_status"] = "OK"
        if demo.get("fail_at") is not None and demo["done"] >= demo["fail_at"]:
            self._fail(task, "CONNECTION_LOST", LOST_CONNECTION)
            return True
        if demo["done"] >= len(items):
            task.update(status="SUCCEEDED", nice_status=None, completion_time=_iso(self.clock()))
            return True
        return changed

    def get_task(self, task_id):
        with self._lock():
            task = self._read_task(task_id)
            if self._advance(task):
                self._write_task(task)
        return self._public(task)

    def successful_transfers(self, task_id, marker=None):
        with self._lock():
            transfers = self._read_task(task_id)["_demo"]["transfers"]
        start = int(marker) if marker else 0
        end = start + TRANSFERS_PAGE
        return {"DATA_TYPE": "successful_transfers", "marker": marker,
                "next_marker": str(end) if end < len(transfers) else None, "DATA": transfers[start:end]}

    def task_list(self, filter=None, limit=10):  # noqa: A002 - the name Globus uses
        """Tasks of this service, newest first; a filter {"label": text} keeps the exact label only."""
        label = filter.get("label") if isinstance(filter, dict) else None
        found = []
        for path in (self.service / TASKS_FOLDER).glob("*.json"):
            task = self._read_json(path, {})
            if task and (label is None or task.get("label") == label):
                found.append(self._public(task))
        found.sort(key=lambda task: task.get("request_time") or "", reverse=True)
        return found[:int(limit)]

    def cancel(self, task_id):
        """Cancel a running task (it ends FAILED, as Globus marks a cancelled task)."""
        with self._lock():
            task = self._read_task(task_id)
            self._advance(task)
            if task.get("status") in ("ACTIVE", "INACTIVE"):
                self._fail(task, "CANCELED", "The task was cancelled.")
                self._write_task(task)
                return {"DATA_TYPE": "result", "code": "Canceled", "message": "The task has been cancelled."}
            self._write_task(task)
        return {"DATA_TYPE": "result", "code": "TaskComplete", "message": "The task had completed before the cancel."}

    def task_events(self, task_id, limit=10):
        with self._lock():
            events = self._read_task(task_id)["_demo"]["events"]
        return list(reversed(events))[:int(limit)]
