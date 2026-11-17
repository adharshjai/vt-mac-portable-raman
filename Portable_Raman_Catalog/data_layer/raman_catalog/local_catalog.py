"""The local demo catalog: the catalog interface on plain files under HOME/data_catalog/local_demo/.

It is the catalog of this edition. Everything it stores is a file on this
Mac; nothing is sent anywhere. Uploads are asynchronous on purpose, like Globus transfers, so
that the code using it has to cope with "not finished yet".

    records/d_<n>.json       one record: metadata, dependencies, where its raw file is
    collections/c_<n>.json   one collection and the ids of its items
    aliases/<alias>          the id the alias names
    data/d_<n>/<file>        the raw file of a record
    tasks/task_<n>.json      one upload
    OFFLINE                  while this file exists every call fails with CatalogOffline
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .catalog import (
    Catalog,
    CatalogConflict,
    CatalogError,
    CatalogNotFound,
    CatalogOffline,
    Collection,
    Dep,
    Record,
    RecordSummary,
    Transfer,
    check_alias,
    dependency_pairs,
    fit_title,
    same_identity,
)
from .config import ALIAS_PATTERN, atomic_write_bytes, atomic_write_json, atomic_write_text, utc_now

ROOT_ID = "c/root"
OFFLINE_FLAG = "OFFLINE"

_README = (
    "Local demo catalog of the Portable Raman controller, catalog edition.\n"
    "\n"
    "Every record, collection and raw file in this folder is a plain file on this Mac.\n"
    "Nothing in here has been sent to any service.\n"
    "Deleting this folder (and ../ledger.sqlite) starts the demo catalog from empty.\n"
)


def _merged(base, patch):
    """Merge metadata on update: nested objects merge and a null value removes the key.

    A null-valued key is dropped when a record is updated, so code must not rely on null keys
    surviving an update.
    """
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            current = result.get(key)
            result[key] = _merged(current if isinstance(current, dict) else {}, value)
        else:
            result[key] = value
    return result


def _as_json(metadata):
    try:
        return json.loads(json.dumps(metadata))
    except (TypeError, ValueError) as error:
        raise CatalogError("The metadata cannot be stored as JSON", detail=str(error)) from error


class LocalDemoCatalog(Catalog):
    name = "local-demo"
    label = "Local demo catalog (files on this Mac only; nothing is sent anywhere)"
    demo_user = "demo-user"

    def __init__(self, root):
        self.root = Path(root)

    # ------------------------------------------------------------------ offline flag

    def is_offline(self):
        return (self.root / OFFLINE_FLAG).exists()

    def set_offline(self, offline):
        """Simulate losing (True) or regaining (False) the connection."""
        flag = self.root / OFFLINE_FLAG
        if offline:
            self.root.mkdir(parents=True, exist_ok=True)
            atomic_write_text(flag, "The local demo catalog acts as unreachable while this file exists.\n")
        else:
            with contextlib.suppress(FileNotFoundError):
                flag.unlink()

    def _require_online(self):
        if self.is_offline():
            raise CatalogOffline(
                "The local demo catalog is switched to offline",
                detail="Simulated: the file %s exists. Remove it with: offline --off" % (self.root / OFFLINE_FLAG),
            )

    # ------------------------------------------------------------------ files

    @contextlib.contextmanager
    def _locked(self):
        """One writer at a time across processes; every change below happens inside this."""
        for folder in ("records", "collections", "aliases", "data", "tasks"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                # Created under the lock: a second process must not replace a root that already has items.
                if not self._file("collections", ROOT_ID).exists():
                    atomic_write_text(self.root / "README.txt", _README)
                    self._save(
                        "collections",
                        ROOT_ID,
                        {"id": ROOT_ID, "alias": None, "title": "Local demo root", "description": "",
                         "parent": None, "items": [], "created_utc": utc_now()},
                    )
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _file(self, kind, item_id):
        return self.root / kind / (item_id.replace("/", "_") + ".json")

    def _load(self, kind, item_id):
        try:
            return json.loads(self._file(kind, item_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def _require(self, kind, item_id):
        data = self._load(kind, item_id)
        if data is None:
            raise CatalogNotFound("Nothing in the catalog has the id %s" % item_id)
        return data

    def _save(self, kind, item_id, data):
        atomic_write_json(self._file(kind, item_id), data)

    def _next_id(self, prefix):
        counters_file = self.root / "counters.json"
        try:
            counters = json.loads(counters_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            counters = {}
        counters[prefix] = counters.get(prefix, 0) + 1
        atomic_write_json(counters_file, counters)
        return "%s/%d" % (prefix, counters[prefix])

    def _alias_target(self, alias):
        try:
            return (self.root / "aliases" / alias).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None

    def _resolve(self, name, prefix):
        """The id for an id or alias of the wanted kind ("d" record, "c" collection)."""
        what = "record" if prefix == "d" else "collection"
        if not isinstance(name, str) or not name:
            raise CatalogNotFound("No %s was named" % what)
        if name.startswith(prefix + "/"):
            return name
        target = self._alias_target(name) if ALIAS_PATTERN.match(name) else None
        if target is None or not target.startswith(prefix + "/"):
            raise CatalogNotFound("The catalog has no %s named %s" % (what, name))
        return target

    def _add_item(self, collection_id, item_id):
        collection = self._require("collections", collection_id)
        if item_id not in collection["items"]:
            collection["items"].append(item_id)
            self._save("collections", collection_id, collection)

    def _connect(self, record):
        """Make the record's parent and the records it depends on point back at it.

        Also called for a record that already exists: a creation interrupted after the record
        file was written is completed by the retry instead of leaving a record nobody lists.
        """
        self._add_item(record["parent"], record["id"])
        for dep in record["deps"]:
            target = self._require("records", dep["id"])
            back = {"relation": dep["relation"], "id": record["id"]}
            if back not in target["referrers"]:
                target["referrers"].append(back)
                self._save("records", target["id"], target)

    def _record(self, data, created=False):
        deps = []
        for dep in data["deps"]:
            other = self._load("records", dep["id"])
            deps.append(Dep(dep["relation"], dep["id"], other.get("alias") if other else None, "out"))
        for dep in data["referrers"]:
            other = self._load("records", dep["id"])
            if other is not None:
                deps.append(Dep(dep["relation"], dep["id"], other.get("alias"), "in"))
        return Record(
            id=data["id"],
            alias=data["alias"],
            title=data["title"],
            metadata=_as_json(data["metadata"]),
            deps=tuple(deps),
            description=data.get("description", ""),
            tags=tuple(data.get("tags", ())),
            data_size=data["data"]["bytes"] if data.get("data") else 0,
            created=created,
        )

    @staticmethod
    def _collection(data):
        return Collection(data["id"], data["alias"], data["title"], data.get("description", ""), data["parent"])

    # ------------------------------------------------------------------ interface

    def whoami(self):
        return {
            "online": not self.is_offline(),
            "user": self.demo_user,
            "context": "local demo (this Mac)",
            "endpoint": None,
            "demo": True,
        }

    def ensure_collection(self, alias, title, description="", parent=None):
        self._require_online()
        check_alias(alias)
        with self._locked():
            collection_id = self._alias_target(alias)
            if collection_id is not None:
                if not collection_id.startswith("c/"):
                    raise CatalogConflict("The alias %s already names a record, not a collection" % alias)
                existing = self._load("collections", collection_id)
                if existing is not None:
                    self._add_item(existing["parent"], collection_id)
                    return self._collection(existing)
            parent_id = ROOT_ID if parent is None else self._resolve(parent, "c")
            self._require("collections", parent_id)
            if collection_id is None:
                collection_id = self._next_id("c")
                atomic_write_text(self.root / "aliases" / alias, collection_id + "\n")
            data = {"id": collection_id, "alias": alias, "title": fit_title(title), "description": description,
                    "parent": parent_id, "items": [], "created_utc": utc_now()}
            self._save("collections", collection_id, data)
            self._add_item(parent_id, collection_id)
            return self._collection(data)

    def create_record(self, alias, title, metadata, parent, deps=(), description="", tags=(), identity_key=None):
        self._require_online()
        check_alias(alias)
        metadata = _as_json(metadata)
        pairs = dependency_pairs(deps)
        with self._locked():
            record_id = self._alias_target(alias)
            if record_id is not None:
                if not record_id.startswith("d/"):
                    raise CatalogConflict("The alias %s already names a collection, not a record" % alias)
                existing = self._load("records", record_id)
                if existing is not None:
                    if not same_identity(existing["metadata"], metadata, identity_key):
                        raise CatalogConflict(
                            "The alias %s already names a record with a different %s" % (alias, identity_key),
                            detail="existing %s: %r, new: %r"
                            % (identity_key, existing["metadata"].get(identity_key), metadata[identity_key]),
                        )
                    self._connect(existing)
                    return self._record(existing)
            parent_id = self._resolve(parent, "c")
            self._require("collections", parent_id)
            resolved = []
            for relation, target in pairs:
                target_id = self._resolve(target, "d")
                self._require("records", target_id)
                resolved.append({"relation": relation, "id": target_id})
            if record_id is None:
                record_id = self._next_id("d")
                # The alias is written before the record: a retry after an interruption finds the
                # alias, sees that the record file is missing, and finishes this same record.
                atomic_write_text(self.root / "aliases" / alias, record_id + "\n")
            now = utc_now()
            data = {"id": record_id, "alias": alias, "title": fit_title(title), "description": description,
                    "tags": list(tags), "metadata": metadata, "parent": parent_id, "deps": resolved,
                    "referrers": [], "data": None, "created_utc": now, "updated_utc": now}
            self._save("records", record_id, data)
            self._connect(data)
            return self._record(data, created=True)

    def get_record(self, id_or_alias):
        self._require_online()
        return self._record(self._require("records", self._resolve(id_or_alias, "d")))

    def update_record(self, id_or_alias, metadata=None, deps_add=(), title=None):
        self._require_online()
        pairs = dependency_pairs(deps_add)
        with self._locked():
            data = self._require("records", self._resolve(id_or_alias, "d"))
            if metadata is not None:
                data["metadata"] = _merged(data["metadata"], _as_json(metadata))
            if title is not None:
                data["title"] = fit_title(title)
            for relation, target in pairs:
                target_id = self._resolve(target, "d")
                self._require("records", target_id)
                dep = {"relation": relation, "id": target_id}
                if dep not in data["deps"]:
                    data["deps"].append(dep)
            data["updated_utc"] = utc_now()
            self._save("records", data["id"], data)
            self._connect(data)
            return self._record(data)

    def link(self, collection, record):
        self._require_online()
        with self._locked():
            collection_id = self._resolve(collection, "c")
            record_id = self._resolve(record, "d")
            self._require("records", record_id)
            self._add_item(collection_id, record_id)

    def unlink(self, collection, record):
        """A record taken out of its last collection is not moved to the root:
        this edition only ever unlinks records from the two queue collections."""
        self._require_online()
        with self._locked():
            collection_id = self._resolve(collection, "c")
            record_id = self._resolve(record, "d")
            data = self._require("collections", collection_id)
            if record_id in data["items"]:
                data["items"].remove(record_id)
                self._save("collections", collection_id, data)

    def list_collection(self, collection):
        self._require_online()
        data = self._require("collections", self._resolve(collection, "c"))
        items = []
        for item_id in data["items"]:
            item = self._load("collections" if item_id.startswith("c/") else "records", item_id)
            if item is not None:
                items.append(RecordSummary(item["id"], item["alias"], item["title"]))
        return items

    def put_data(self, record, local_path):
        self._require_online()
        with self._locked():
            record_id = self._resolve(record, "d")
            self._require("records", record_id)
            task_id = self._next_id("task")
            now = utc_now()
            # Like a Globus transfer, the file is not looked at yet: a missing file shows up as a failed task.
            self._save("tasks", task_id, {"id": task_id, "record": record_id, "source": os.path.abspath(local_path),
                                          "status": "queued", "message": "", "created_utc": now, "updated_utc": now})
            return Transfer(task_id, "queued", "")

    def transfer_status(self, task_id):
        self._require_online()
        with self._locked():
            task = self._load("tasks", task_id) if str(task_id).startswith("task/") else None
            if task is None:
                raise CatalogNotFound("The catalog has no transfer task %s" % task_id)
            if task["status"] == "queued":
                task["status"] = "running"
            elif task["status"] == "running":
                self._finish_transfer(task)
            else:
                return Transfer(task["id"], task["status"], task["message"])
            task["updated_utc"] = utc_now()
            self._save("tasks", task["id"], task)
            return Transfer(task["id"], task["status"], task["message"])

    def _finish_transfer(self, task):
        source = Path(task["source"])
        record = self._load("records", task["record"])
        if record is None:
            task["status"], task["message"] = "failed", "the record %s no longer exists" % task["record"]
            return
        try:
            content = source.read_bytes()
        except OSError as error:
            task["status"] = "failed"
            task["message"] = "cannot read the source file %s (%s)" % (source.name, error.strerror or error)
            return
        folder = self.root / "data" / record["id"].replace("/", "_")
        folder.mkdir(parents=True, exist_ok=True)
        previous = record.get("data")
        if previous and previous["name"] != source.name:
            with contextlib.suppress(FileNotFoundError):
                (folder / previous["name"]).unlink()
        atomic_write_bytes(folder / source.name, content)
        record["data"] = {"name": source.name, "bytes": len(content),
                          "sha256": hashlib.sha256(content).hexdigest(), "stored_utc": utc_now()}
        record["updated_utc"] = record["data"]["stored_utc"]
        self._save("records", record["id"], record)
        task["status"], task["message"] = "succeeded", ""

    def get_data(self, record, dest_dir):
        self._require_online()
        data = self._require("records", self._resolve(record, "d"))
        stored = data.get("data")
        if not stored:
            raise CatalogNotFound("The record %s has no raw data (its upload has not finished)" % data["id"])
        source = self.root / "data" / data["id"].replace("/", "_") / stored["name"]
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / stored["name"]
        atomic_write_bytes(target, source.read_bytes())
        return target
