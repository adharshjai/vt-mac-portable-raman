"""The catalog interface on the real DataFed service, through the `datafed` Python package.

The data layer never logs in. It uses the credentials the user installed themselves with
`datafed setup`, and says so when there are none. Nothing is contacted until the first catalog
call, and a test passes its own `api` object instead of connecting.

Calls go through datafed.CommandLib.API; every reply is a (protobuf message, message type name)
pair. The API raises plain `Exception` for everything (time-outs, refusals by the server), so
failures are told apart by their text.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
from pathlib import Path

from .catalog import (
    Catalog,
    CatalogAuthRequired,
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

log = logging.getLogger(__name__)

AUTH_HELP = (
    "DataFed credentials are missing or not valid on this Mac. In a terminal, run 'datafed setup' yourself "
    "(it asks for your DataFed user ID and password once), then set this Mac's Globus endpoint with "
    "'datafed ep default set <endpoint>'. The data layer never logs in for you."
)
ENDPOINT_HELP = (
    "No default Globus endpoint is set, so files cannot be transferred. In a terminal, run "
    "'datafed ep default set <endpoint>' with the id of this Mac's Globus Connect Personal collection."
)

_PAGE_SIZE = 100

# Names of the enum values in the reply messages of the datafed package.
_RELATIONS = {"DEP_IS_DERIVED_FROM": "der", "DEP_IS_COMPONENT_OF": "comp", "DEP_IS_NEW_VERSION_OF": "ver"}
# DIR_IN: the other record points at this one (it "is derived from" this one).
_DIRECTIONS = {"DIR_IN": "in", "DIR_OUT": "out"}
# DataFed's "blocked" and "ready" both mean the transfer has not started.
_TASK_STATUSES = {
    "TS_BLOCKED": "queued",
    "TS_READY": "queued",
    "TS_RUNNING": "running",
    "TS_SUCCEEDED": "succeeded",
    "TS_FAILED": "failed",
}

# How the datafed client's own messages begin when the server did not answer.
_NO_ANSWER_MESSAGES = (
    "timeout",
    # The server key is downloaded on first use; without it there is nothing to connect with.
    "could not open server public key file",
    # A late reply to a request that timed out: the connection is out of step.
    "mismatched reply",
)
# "Server error 5" and "6" are NackReply codes AUTHN_REQUIRED and AUTHN_ERROR sent without a text.
_AUTH_PATTERN = re.compile(
    r"authenticat|authn|credential|client key file|client public key|client private key|no endpoint set"
    r"|^server error [56]$"
)
_NOT_FOUND_HINTS = ("not found", "does not exist", "doesn't exist", "no such")
_NETWORK_MODULES = ("zmq", "requests", "urllib3", "socket")


class _Refused(CatalogError):
    """DataFed answered the request with a refusal this adapter has no more specific name for."""


class _Unusable(CatalogOffline):
    """The client could not open a connection at all (another server version, a server key it
    cannot use). No request was made and nothing changed; the next request fails the same way
    until somebody acts, so it ends a sync like an unreachable server instead of counting as a
    refusal of each capture."""


def _sent_texts(values):
    for value in values:
        if isinstance(value, str):
            yield value
        elif isinstance(value, (list, tuple)):
            yield from _sent_texts(value)


def _translate(error, action, sent=()):
    """The catalog error for something the datafed package raised while doing `action`.

    `sent` holds the arguments of the request. A refusal often repeats them ("Alias x already
    in use"), and a name chosen by the user must not be mistaken for the reason of the refusal,
    so they are taken out of the text before it is read.
    """
    text = str(error).strip() or type(error).__name__
    network = isinstance(error, (ConnectionError, TimeoutError))
    network = network or type(error).__module__.split(".")[0] in _NETWORK_MODULES
    if network or text.lower().startswith(_NO_ANSWER_MESSAGES):
        return CatalogOffline("DataFed cannot be reached", detail="%s: %s" % (action, text))
    said = text.lower()
    for value in sorted(_sent_texts(sent), key=len, reverse=True):
        if len(value) > 2:
            said = said.replace(value.lower(), " ")
    if _AUTH_PATTERN.search(said):
        return CatalogAuthRequired(AUTH_HELP, detail="%s: %s" % (action, text))
    if any(hint in said for hint in _NOT_FOUND_HINTS):
        return CatalogNotFound("DataFed: %s" % text, detail=action)
    return _Refused("DataFed: %s" % text, detail=action)


def _refused(error):
    """True when DataFed answered and said no.

    Only then may the adapter draw a conclusion such as "the alias is taken" or "there is no
    such collection". Unreachable, missing credentials and faults of the adapter itself (package
    not installed, a reply it cannot read) are never taken for an answer.
    """
    return isinstance(error, (_Refused, CatalogNotFound))


def _enum_name(message, field):
    values = message.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number
    value = values.get(getattr(message, field))
    return value.name if value is not None else ""


def _short_alias(alias):
    """Aliases can come back with their owner ("u:name:alias"); the catalog interface uses the bare alias."""
    return alias.rsplit(":", 1)[-1] or None


class DataFedCatalog(Catalog):
    name = "datafed"

    def __init__(self, context="", repo_id="", api=None, download_timeout_sec=600):
        self._context = context or None
        self._repo_id = repo_id or None
        self._api = api
        self._injected = api is not None
        self._download_timeout_sec = download_timeout_sec

    @property
    def label(self):
        return "DataFed (%s)" % ("project %s" % self._context if self._context else "your own space")

    # ------------------------------------------------------------------ connection

    def _connect(self):
        try:
            from datafed.CommandLib import API
        except ImportError as error:
            # Like a client that cannot connect: no request was made, and every capture would fail the
            # same way until the package is installed, so no attempt of theirs is spent on it.
            raise _Unusable(
                "The datafed Python package is not available in this interpreter",
                detail="%s; create the data layer's environment with data_layer/setup_env.sh" % error,
            ) from error
        log.info("connecting to DataFed")
        try:
            # The package prints on stdout, which belongs to the command's single JSON object.
            with contextlib.redirect_stdout(sys.stderr):
                return API()
        except Exception as error:
            translated = _translate(error, "connect")
            if isinstance(translated, (CatalogOffline, CatalogAuthRequired)):
                raise translated from error
            raise _Unusable("DataFed cannot be used from this Mac",
                            detail="connect: %s" % (str(error).strip() or type(error).__name__)) from error

    def _session(self):
        """The API object, connected on first use. Raises CatalogAuthRequired while nobody is signed in."""
        if self._api is None:
            self._api = self._connect()
        if self._api.getAuthUser() is None:
            raise CatalogAuthRequired(
                AUTH_HELP, detail="DataFed answered, but this Mac has no usable user credentials for it."
            )
        return self._api

    def _request(self, method, *args, **kwargs):
        api = self._session()
        try:
            with contextlib.redirect_stdout(sys.stderr):
                reply, kind = getattr(api, method)(*args, **kwargs)
        except Exception as error:
            translated = _translate(error, method, sent=list(args) + list(kwargs.values()))
            if isinstance(translated, CatalogOffline) and not self._injected:
                # The reply to a request that timed out may still arrive; a new connection starts clean.
                self._api = None
            raise translated from error
        if reply is None:
            raise CatalogOffline("DataFed cannot be reached", detail="%s: no reply" % method)
        return reply, kind

    def _call(self, method, expect, *args, **kwargs):
        """Make a request whose reply is read: a reply of another type than `expect` is an error."""
        reply, kind = self._request(method, *args, **kwargs)
        if kind != expect:
            raise CatalogError(
                "DataFed sent an unexpected reply", detail="%s: expected %s, got %s" % (method, expect, kind)
            )
        return reply

    # ------------------------------------------------------------------ conversions

    @staticmethod
    def _record(data, created=False):
        try:
            metadata = json.loads(data.metadata) if data.metadata else {}
        except ValueError as error:
            raise CatalogError("DataFed returned metadata that is not JSON", detail=data.id) from error
        deps = []
        for dep in data.deps:
            relation = _RELATIONS.get(_enum_name(dep, "type"))
            direction = _DIRECTIONS.get(_enum_name(dep, "dir"))
            if relation is None or direction is None:
                raise CatalogError("DataFed reported a dependency this version does not understand", detail=data.id)
            deps.append(Dep(relation, dep.id, _short_alias(dep.alias), direction))
        return Record(
            id=data.id,
            alias=_short_alias(data.alias),
            title=data.title,
            metadata=metadata,
            deps=tuple(deps),
            description=data.desc,
            tags=tuple(data.tags),
            data_size=int(data.size),
            created=created,
        )

    @staticmethod
    def _collection(data):
        return Collection(data.id, _short_alias(data.alias), data.title, data.desc, data.parent_id or None)

    @staticmethod
    def _transfer(task):
        status = _TASK_STATUSES.get(_enum_name(task, "status"))
        if status is None:
            raise CatalogError("DataFed reported a transfer status this version does not understand", detail=task.id)
        return Transfer(task.id, status, task.msg)

    # ------------------------------------------------------------------ interface

    def whoami(self):
        info = {"online": False, "user": None, "context": self._context or "", "endpoint": None, "demo": False}
        try:
            api = self._session()
            info["online"] = True
            info["user"] = api.getAuthUser()
            if not self._context:
                info["context"] = info["user"]
            # One request, so that "online" means DataFed answered now and not when the object was built.
            self._request("userView", info["user"])
            if self._context:
                self._request("projectView", self._context)
            info["endpoint"] = api.endpointDefaultGet() or None
        except CatalogOffline as error:
            info["online"] = False
            info["problem"] = "%s (%s)." % (error, error.detail)
            return info
        except CatalogAuthRequired as error:
            # Online only when a connection exists; credentials can also fail before any is made.
            info["online"] = self._api is not None
            info["problem"] = str(error)
            return info
        except CatalogError as error:
            info["problem"] = "%s." % error
            return info
        if info["endpoint"] is None:
            info["problem"] = ENDPOINT_HELP
        return info

    def _view_collection(self, alias):
        reply = self._call("collectionView", "CollDataReply", alias, context=self._context)
        if not reply.coll:
            raise CatalogNotFound("DataFed has no collection named %s" % alias)
        return self._collection(reply.coll[0])

    def ensure_collection(self, alias, title, description="", parent=None):
        check_alias(alias)
        try:
            return self._view_collection(alias)
        except CatalogError as error:
            # Whatever the server's wording for "no such collection": creating it is the next thing to try.
            if not _refused(error):
                raise
        try:
            reply = self._call(
                "collectionCreate",
                "CollDataReply",
                fit_title(title),
                alias=alias,
                description=description or None,
                parent_id=parent or "root",
                context=self._context,
            )
        except CatalogError as error:
            if not _refused(error):
                raise
            # Another process may have created it between the two calls.
            try:
                return self._view_collection(alias)
            except CatalogError as second:
                if not _refused(second):
                    raise
                raise error from None
        if not reply.coll:
            raise CatalogError("DataFed did not return the collection it created", detail=alias)
        return self._collection(reply.coll[0])

    def create_record(self, alias, title, metadata, parent, deps=(), description="", tags=(), identity_key=None):
        check_alias(alias)
        pairs = dependency_pairs(deps)
        if identity_key is not None and identity_key not in metadata:
            raise CatalogError("The metadata has no %r to identify the record by" % identity_key)
        try:
            reply = self._call(
                "dataCreate",
                "RecordDataReply",
                fit_title(title),
                alias=alias,
                description=description or None,
                tags=list(tags) or None,
                metadata=json.dumps(metadata),
                parent_id=parent,
                deps=[list(pair) for pair in pairs] or None,
                repo_id=self._repo_id,
                context=self._context,
            )
        except CatalogError as error:
            if not _refused(error):
                raise
            # The usual refusal is "alias already in use". The server's wording is not relied on:
            # if a record with this alias exists, the identity rule decides; if not, the refusal stands.
            try:
                existing = self.get_record(alias)
            except CatalogNotFound:
                raise error from None
            if not same_identity(existing.metadata, metadata, identity_key):
                raise CatalogConflict(
                    "The alias %s already names a record with a different %s" % (alias, identity_key),
                    detail="existing %s: %r, new: %r"
                    % (identity_key, existing.metadata.get(identity_key), metadata[identity_key]),
                ) from None
            return existing
        if not reply.data:
            raise CatalogError("DataFed did not return the record it created", detail=alias)
        return self._record(reply.data[0], created=True)

    def get_record(self, id_or_alias):
        try:
            reply = self._call("dataView", "RecordDataReply", id_or_alias, context=self._context)
        except _Refused as error:
            # A view is refused for a record that is missing or not visible to this user; either
            # way the record cannot be had, which is what callers need to know.
            raise CatalogNotFound("DataFed cannot show the record %s" % id_or_alias, detail=str(error)) from error
        if not reply.data:
            raise CatalogNotFound("DataFed has no record %s" % id_or_alias)
        return self._record(reply.data[0])

    def update_record(self, id_or_alias, metadata=None, deps_add=(), title=None):
        pairs = dependency_pairs(deps_add)
        self._request(
            "dataUpdate",
            id_or_alias,
            title=fit_title(title) if title is not None else None,
            metadata=json.dumps(metadata) if metadata is not None else None,
            deps_add=[list(pair) for pair in pairs] or None,
            context=self._context,
        )
        # The update reply is not relied on for the merged metadata and both dependency directions.
        return self.get_record(id_or_alias)

    def _contains(self, collection, record):
        try:
            return any(record in (item.id, item.alias) for item in self.list_collection(collection))
        except CatalogError as error:
            if not _refused(error):
                raise
            return False

    def link(self, collection, record):
        try:
            self._request("collectionItemsUpdate", collection, add_ids=[record], context=self._context)
        except CatalogError as error:
            # Linking twice must not be an error, whatever the server says about it.
            if not _refused(error) or not self._contains(collection, record):
                raise

    def unlink(self, collection, record):
        try:
            self._request("collectionItemsUpdate", collection, rem_ids=[record], context=self._context)
        except CatalogError as error:
            if not _refused(error) or self._contains(collection, record):
                raise

    def list_collection(self, collection):
        items, offset = [], 0
        while True:
            reply = self._call(
                "collectionItemsList", "ListingReply", collection, offset=offset, count=_PAGE_SIZE,
                context=self._context,
            )
            items.extend(RecordSummary(item.id, _short_alias(item.alias), item.title) for item in reply.item)
            page = len(reply.item)
            offset += page
            if page == 0 or (reply.total and offset >= reply.total) or (not reply.total and page < _PAGE_SIZE):
                return items

    def put_data(self, record, local_path):
        # A plain local path: the datafed package puts the user's default endpoint in front of it.
        reply = self._call(
            "dataPut", "DataPutReply", record, os.path.abspath(local_path), wait=False, context=self._context
        )
        if not reply.HasField("task"):
            raise CatalogError("DataFed started no transfer for the upload", detail=str(record))
        return self._transfer(reply.task)

    def transfer_status(self, task_id):
        reply = self._call("taskView", "TaskDataReply", task_id)
        if not reply.task:
            raise CatalogNotFound("DataFed has no transfer task %s" % task_id)
        return self._transfer(reply.task[0])

    def get_data(self, record, dest_dir):
        dest_dir = Path(os.path.abspath(dest_dir))
        dest_dir.mkdir(parents=True, exist_ok=True)
        view = self._call("dataView", "RecordDataReply", record, context=self._context)
        if not view.data:
            raise CatalogNotFound("DataFed has no record %s" % record)
        data = view.data[0]
        expected = os.path.basename(data.source) if data.source else ""
        before = set(os.listdir(dest_dir))
        try:
            reply, kind = self._request(
                "dataGet", data.id, str(dest_dir), orig_fname=True, wait=True,
                timeout_sec=self._download_timeout_sec, context=self._context,
            )
        except CatalogError as error:
            if "no raw data" in str(error).lower():
                raise CatalogNotFound(
                    "The record %s has no raw data (its upload has not finished)" % data.id
                ) from error
            raise
        if kind != "TaskDataReply" or not reply.task:
            raise CatalogError("DataFed started no transfer for the download", detail="%s: %s" % (data.id, kind))
        transfer = self._transfer(reply.task[0])
        if transfer.status == "failed":
            raise CatalogError("The download of %s failed" % data.id, detail=transfer.message)
        if transfer.status != "succeeded":
            raise CatalogError(
                "The download of %s did not finish within %d s" % (data.id, self._download_timeout_sec),
                detail="Transfer %s is still %s. Is Globus Connect Personal running?"
                % (transfer.task_id, transfer.status),
            )
        if expected and (dest_dir / expected).is_file():
            return dest_dir / expected
        arrived = sorted(set(os.listdir(dest_dir)) - before)
        if len(arrived) == 1:
            return dest_dir / arrived[0]
        raise CatalogError("The download of %s finished but its file is not in %s" % (data.id, dest_dir))
