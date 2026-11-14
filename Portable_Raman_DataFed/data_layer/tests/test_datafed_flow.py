"""Sync in datafed mode, end to end, against an in-memory stand-in for the DataFed server.

Nothing here connects to DataFed. The stand-in sits behind an autospec of the real API class,
so every call the adapter makes is checked against the real method signatures, and it answers
with the real protobuf reply classes. Its refusals use wording the adapter cannot know, to
show that no decision depends on how the real server phrases an error.
"""

import json
import os
import shutil
import unittest
from argparse import Namespace
from unittest import mock

import support
from datafed import envelope_pb2 as sdms
from datafed.CommandLib import API
from raman_datafed import commands_core, sync
from raman_datafed.catalog import ensure_layout, layout_for, raw_alias, session_alias
from raman_datafed.cli import Context
from raman_datafed.config import load_settings
from raman_datafed.datafed_catalog import DataFedCatalog
from raman_datafed.ledger import Ledger

PROJECT = "p/raman.test"
ROOT_COLLECTION = "c/p_raman.test_root"


class Crash(BaseException):
    """Stands for the process dying: nothing in the data layer catches it."""


class FakeDataFed:
    """Records, collections, aliases and transfer tasks in memory, behaving like the service:
    aliases are unique, linking twice is refused, transfers advance on their own."""

    def __init__(self, staging):
        self.staging = staging
        self.records = {}
        self.collections = {ROOT_COLLECTION: {"alias": "root", "title": "Root", "desc": "", "parent": "", "items": []}}
        self.aliases = {"root": ROOT_COLLECTION}
        self.tasks = {}
        self.offline = False
        self.user = "u/tester"
        self.calls = []
        # (number of the request at which the client dies, True when the server has already acted on it)
        self.fatal = None
        self._next = 100

    def behind(self, api):
        """Make an autospec'd API object answer from this stand-in."""
        for name in ("dataCreate", "dataView", "dataUpdate", "dataPut", "dataGet", "collectionView",
                     "collectionCreate", "collectionItemsList", "collectionItemsUpdate", "taskView", "userView",
                     "projectView"):
            getattr(api, name).side_effect = self._answering(name)
        api.getAuthUser.side_effect = lambda: self.user
        api.endpointDefaultGet.return_value = "5a1b0e6e-1111-2222-3333-444455556666"
        return api

    def _answering(self, name):
        def answer(*args, **kwargs):
            self.calls.append(name)
            if self.offline:
                raise Exception("Timeout!!!!!!!!!")
            number = len(self.calls)
            if self.fatal == (number, False):
                raise Crash()
            try:
                return getattr(self, name)(*args, **kwargs)
            finally:
                # The client can die after any answer, a refusal included.
                if self.fatal == (number, True):
                    raise Crash()

        return answer

    def _new_id(self, prefix):
        self._next += 1
        return "%s/%d" % (prefix, self._next)

    def _resolve(self, name, prefix):
        item_id = name if name[1:2] == "/" or name.startswith("task/") else self.aliases.get(name)
        if item_id is None or not item_id.startswith(prefix + "/"):
            raise Exception("E44: nothing is known by '%s'" % name)
        if item_id not in (self.records if prefix == "d" else self.collections):
            raise Exception("E44: nothing is known by '%s'" % name)
        return item_id

    def _record_reply(self, record_id):
        record = self.records[record_id]
        reply = sdms.RecordDataReply()
        data = reply.data.add()
        data.id = record_id
        data.alias = record["alias"]
        data.title = record["title"]
        data.desc = record["desc"]
        data.metadata = json.dumps(record["metadata"])
        data.size = record["size"]
        data.source = record["source"]
        for target in record["deps"]:
            dep = data.deps.add()
            dep.id, dep.alias, dep.type, dep.dir = target, self.records[target]["alias"], 0, sdms.DIR_OUT
        for other_id, other in self.records.items():
            if record_id in other["deps"]:
                dep = data.deps.add()
                dep.id, dep.alias, dep.type, dep.dir = other_id, other["alias"], 0, sdms.DIR_IN
        return reply, "RecordDataReply"

    def _collection_reply(self, collection_id):
        collection = self.collections[collection_id]
        reply = sdms.CollDataReply()
        data = reply.coll.add()
        data.id, data.alias, data.title = collection_id, collection["alias"], collection["title"]
        data.desc, data.parent_id = collection["desc"], collection["parent"]
        return reply, "CollDataReply"

    # -- the methods of datafed.CommandLib.API that the adapter uses, with the same parameters

    def dataCreate(self, title, alias=None, description=None, tags=None, extension=None, metadata=None,
                   metadata_file=None, schema=None, schema_enforce=None, parent_id="root", deps=None, repo_id=None,
                   raw_data_file=None, external=None, context=None):
        if alias in self.aliases:
            raise Exception("E17: that name is taken")
        if len(title) > 80:
            raise Exception("E30: title too long")
        parent = self._resolve(parent_id, "c")
        targets = [self._resolve(target, "d") for relation, target in deps or [] if relation == "der"]
        record_id = self._new_id("d")
        self.records[record_id] = {"alias": alias, "title": title, "desc": description or "",
                                   "metadata": json.loads(metadata), "deps": targets, "size": 0, "source": ""}
        self.aliases[alias] = record_id
        self.collections[parent]["items"].append(record_id)
        return self._record_reply(record_id)

    def dataView(self, data_id, details=False, context=None):
        return self._record_reply(self._resolve(data_id, "d"))

    def dataUpdate(self, data_id, title=None, alias=None, description=None, tags=None, extension=None, metadata=None,
                   metadata_file=None, metadata_set=False, schema=None, schema_enforce=None, deps_add=None,
                   deps_rem=None, raw_data_file=None, context=None):
        record = self.records[self._resolve(data_id, "d")]
        if metadata is not None:
            record["metadata"] = self._merged(record["metadata"], json.loads(metadata))
        if title is not None:
            record["title"] = title
        for _relation, target in deps_add or []:
            record["deps"].append(self._resolve(target, "d"))
        return self._record_reply(self._resolve(data_id, "d"))

    @classmethod
    def _merged(cls, base, patch):
        result = dict(base)
        for key, value in patch.items():
            if value is None:
                result.pop(key, None)
            elif isinstance(value, dict):
                result[key] = cls._merged(result[key] if isinstance(result.get(key), dict) else {}, value)
            else:
                result[key] = value
        return result

    def collectionView(self, coll_id, context=None):
        return self._collection_reply(self._resolve(coll_id, "c"))

    def collectionCreate(self, title, alias=None, description=None, tags=None, topic=None, parent_id="root",
                         context=None):
        if alias in self.aliases:
            raise Exception("E17: that name is taken")
        parent = self._resolve(parent_id, "c")
        collection_id = self._new_id("c")
        self.collections[collection_id] = {"alias": alias, "title": title, "desc": description or "",
                                           "parent": parent, "items": []}
        self.aliases[alias] = collection_id
        self.collections[parent]["items"].append(collection_id)
        return self._collection_reply(collection_id)

    def collectionItemsList(self, coll_id, offset=0, count=20, context=None):
        items = self.collections[self._resolve(coll_id, "c")]["items"]
        reply = sdms.ListingReply()
        for item_id in items[offset:offset + count]:
            source = self.records.get(item_id) or self.collections[item_id]
            item = reply.item.add()
            item.id, item.alias, item.title = item_id, source["alias"], source["title"]
        reply.offset, reply.count, reply.total = offset, count, len(items)
        return reply, "ListingReply"

    def collectionItemsUpdate(self, coll_id, add_ids=None, rem_ids=None, context=None):
        items = self.collections[self._resolve(coll_id, "c")]["items"]
        for name in add_ids or []:
            item_id = self._resolve(name, "d")
            if item_id in items:
                raise Exception("E21: it is in there already")
            items.append(item_id)
        for name in rem_ids or []:
            item_id = self._resolve(name, "d")
            if item_id not in items:
                raise Exception("E22: it is not in there")
            items.remove(item_id)
        return sdms.ListingReply(), "ListingReply"

    def dataPut(self, data_id, path, encrypt=sdms.ENCRYPT_AVAIL, wait=False, timeout_sec=0, extension=None,
                context=None):
        if wait:
            raise AssertionError("an upload must never be waited for")
        task_id = self._new_id("task")
        self.tasks[task_id] = {"status": sdms.TS_READY, "msg": "", "record": self._resolve(data_id, "d"), "path": path}
        reply = sdms.DataPutReply()
        reply.task.id, reply.task.status = task_id, sdms.TS_READY
        return reply, "DataPutReply"

    def taskView(self, task_id=None):
        task = self.tasks.get(task_id)
        if task is None:
            raise Exception("E44: nothing is known by '%s'" % task_id)
        if task["status"] == sdms.TS_READY:
            task["status"] = sdms.TS_RUNNING
        elif task["status"] == sdms.TS_RUNNING:
            if os.path.isfile(task["path"]):
                name = task["record"].replace("/", "_") + "_" + os.path.basename(task["path"])
                stored = os.path.join(self.staging, name)
                shutil.copyfile(task["path"], stored)
                self.records[task["record"]].update(size=os.path.getsize(stored), source="5a1b0e6e" + task["path"],
                                                    stored=stored)
                task["status"] = sdms.TS_SUCCEEDED
            else:
                task["status"], task["msg"] = sdms.TS_FAILED, "globus: source path not found"
        reply = sdms.TaskDataReply()
        data = reply.task.add()
        data.id, data.status, data.msg = task_id, task["status"], task["msg"]
        return reply, "TaskDataReply"

    def dataGet(self, item_id, path, encrypt=sdms.ENCRYPT_AVAIL, orig_fname=False, wait=False, timeout_sec=0,
                context=None):
        record = self.records[self._resolve(item_id, "d")]
        if not record["size"]:
            raise Exception("Specified record(s) contain no raw data.")
        name = os.path.basename(record["source"]) if orig_fname else item_id.replace("/", "_")
        shutil.copyfile(record["stored"], os.path.join(path, name))
        reply = sdms.TaskDataReply()
        data = reply.task.add()
        data.id, data.status = self._new_id("task"), sdms.TS_SUCCEEDED
        return reply, "TaskDataReply"

    def userView(self, uid):
        return sdms.UserDataReply(), "UserDataReply"

    def projectView(self, project_id):
        return sdms.ProjectDataReply(), "ProjectDataReply"


class DataFedCase(unittest.TestCase):
    """A HOME in datafed mode whose catalog is the adapter in front of the in-memory stand-in."""

    def setUp(self):
        self.home = support.make_home(self)
        settings = load_settings(self.home)
        settings.set("mode", "datafed")
        settings.set("datafed.context", PROJECT)
        settings.save()
        self.server = FakeDataFed(support.make_home(self))
        self.api = self.server.behind(mock.create_autospec(API, instance=True))
        self.catalog = DataFedCatalog(context=PROJECT, api=self.api)
        self.ctx = support.context(self, self.home, catalog=self.catalog)
        # Measured captures and a measured dark: simulated ones stay on this Mac in datafed mode
        # unless asked for (SimulatedCaptures below).
        self.dark = support.write_dark(self.home, simulated=False)
        patcher = mock.patch.object(sync, "_optional_module", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, **options):
        options.setdefault("simulated", False)
        return support.make_session(self.home, **options)

    def sync(self):
        return commands_core.cmd_sync(Namespace(), self.ctx)

    def states(self):
        return [row["state"] for row in self.ctx.ledger.rows(newest_first=False)]

    def items(self, alias):
        return self.server.collections[self.server.aliases[alias]]["items"]


class DataFedFlow(DataFedCase):
    def test_capture_to_review_through_the_adapter(self):
        session = self.session(points=3)
        names = layout_for("raman-portable")

        first = self.sync()
        self.assertEqual((first["online"], first["ingested"], first["registered"], first["uploads_started"],
                          first["errors"]), (True, 3, 3, 3, []))
        self.assertEqual(self.states(), ["uploading"] * 3)
        self.assertEqual(first["status"]["mode_label"], "DataFed (project p/raman.test)")
        self.assertEqual((first["status"]["demo"], first["status"]["user"], first["status"]["context"]),
                         (False, "u/tester", PROJECT))
        self.assertEqual(len(self.items(session_alias(session["session_id"]))), 3)
        self.assertEqual(self.items(names.to_classify), self.items(session_alias(session["session_id"])))
        self.assertEqual(len(self.items(names.darks)), 1)
        self.assertEqual(self.server.collections[self.server.aliases[names.sessions]]["parent"],
                         self.server.aliases[names.root])
        # Every upload was started from the plain local path of the file.
        self.assertEqual(sorted(task["path"] for task in self.server.tasks.values()),
                         sorted([str(path) for path in session["files"]] + [str(self.dark)]))

        second = self.sync()
        self.assertEqual((second["uploads_done"], second["registered"], second["errors"]), (3, 0, []))
        self.assertEqual(self.states(), ["uploaded"] * 3)

        raws = [self.catalog.get_record(raw_alias(capture_id)) for capture_id in session["capture_ids"]]
        self.assertEqual(raws[0].data_size, session["files"][0].stat().st_size)
        fetched = self.catalog.get_data(raws[0].id, self.home / "data_catalog" / "staging")
        self.assertEqual(fetched.read_bytes(), session["files"][0].read_bytes())
        dark = self.catalog.get_data(raws[0].metadata["dark"]["record"], self.home / "data_catalog" / "staging")
        self.assertEqual(dark.read_bytes(), self.dark.read_bytes())

        support.save_prediction(self.catalog, "raman-portable", raws[0], "confident")
        waiting = support.save_prediction(self.catalog, "raman-portable", raws[1], "low_confidence", confidence=0.55)
        third = self.sync()
        self.assertEqual((third["predictions_found"], third["errors"]), (2, []))
        self.assertEqual(self.states(), ["accepted", "needs_review", "uploaded"])
        self.assertEqual(self.items(names.needs_review), [waiting.id])
        self.assertEqual(self.items(names.to_classify), [raws[2].id])

        review = {"status": "confirmed", "label": "r6g", "reviewer": "Test Reviewer",
                  "time_utc": "2026-11-13T16:00:00.000Z", "comment": ""}
        reviewed = sync.apply_review(self.catalog, self.ctx.config, waiting.id, review)
        self.assertEqual((reviewed.metadata["review"]["status"], reviewed.metadata["needs_review"],
                          reviewed.metadata["label"]), ("confirmed", False, "UNKNOWN"))
        self.assertEqual(self.items(names.needs_review), [])
        # Doing it again is harmless although the server refuses the second unlink.
        sync.apply_review(self.catalog, self.ctx.config, waiting.id, review)

        fourth = self.sync()
        self.assertEqual((fourth["reviews_found"], fourth["predictions_found"]), (1, 0))
        self.assertEqual(self.states(), ["accepted", "reviewed", "uploaded"])

        before = len(self.server.calls)
        fifth = self.sync()
        self.assertEqual({key: value for key, value in fifth.items()
                          if isinstance(value, int) and not isinstance(value, bool) and value}, {})
        # A settled pass asks little: who the user is, and what still waits for the classifier.
        self.assertLessEqual(len(self.server.calls) - before, 4)

    def test_retry_after_dying_between_record_created_and_ledger_updated(self):
        session = self.session(points=2)
        with mock.patch.object(Ledger, "mark_registered", side_effect=Crash()):
            with self.assertRaises(Crash):
                self.sync()
        created = dict(self.server.aliases)
        self.assertIn(raw_alias(session["capture_ids"][0]), created)

        result = self.sync()
        self.assertEqual((result["registered"], result["errors"]), (2, []))
        # The record made before the crash was found by its alias, not made again.
        self.assertEqual(self.server.aliases[raw_alias(session["capture_ids"][0])],
                         created[raw_alias(session["capture_ids"][0])])
        self.assertEqual(len([record for record in self.server.records.values()
                              if record["metadata"].get("type") == "raman.raw_spectrum"]), 2)
        self.assertEqual(len(self.items(layout_for("raman-portable").to_classify)), 2)

    def test_dying_at_any_request_loses_nothing_and_makes_nothing_twice(self):
        session = self.session(points=2)
        names = layout_for("raman-portable")
        deaths = 0
        for server_acted in (False, True):
            die_at = 0
            while True:
                die_at += 1
                shutil.rmtree(self.home / "data_catalog", ignore_errors=True)
                server = FakeDataFed(self.server.staging)
                server.fatal = (die_at, server_acted)
                catalog = DataFedCatalog(context=PROJECT, api=server.behind(mock.create_autospec(API, instance=True)))
                with Context(self.home, catalog=catalog) as ctx:
                    try:
                        commands_core.cmd_sync(Namespace(), ctx)
                        commands_core.cmd_sync(Namespace(), ctx)
                    except Crash:
                        deaths += 1
                    else:
                        break
                server.fatal = None
                where = "died at request %d, %s the server acted" % (die_at, "after" if server_acted else "before")
                with Context(self.home, catalog=catalog) as ctx:
                    for _pass in range(4):
                        self.assertEqual(commands_core.cmd_sync(Namespace(), ctx)["errors"], [], where)
                    rows = ctx.ledger.rows()
                self.assertEqual({row["state"] for row in rows}, {"uploaded"}, where)
                kinds = [record["metadata"]["type"] for record in server.records.values()]
                self.assertEqual(sorted(kinds), ["raman.dark_spectrum", "raman.raw_spectrum", "raman.raw_spectrum"],
                                 where)
                record_ids = sorted(row["record_id"] for row in rows)
                for alias in (names.to_classify, session_alias(session["session_id"])):
                    self.assertEqual(sorted(server.collections[server.aliases[alias]]["items"]), record_ids, where)
                self.assertTrue(all(record["size"] for record in server.records.values()), where)
        # Two full passes send 35 requests or so; each was fatal both ways.
        self.assertGreaterEqual(deaths, 60)

    def test_offline_then_reconnect(self):
        self.session(points=2)
        self.server.offline = True
        offline = self.sync()
        self.assertEqual((offline["online"], offline["ingested"], offline["registered"], offline["errors"]),
                         (False, 2, 0, []))
        self.assertEqual(offline["status"]["online"], False)
        self.assertEqual(self.states(), ["captured", "captured"])
        self.assertEqual(self.server.records, {})

        self.server.offline = False
        online = self.sync()
        self.assertEqual((online["online"], online["registered"]), (True, 2))

    def test_missing_credentials_are_reported_with_what_to_run(self):
        self.session(points=1)
        self.server.user = None
        result = self.sync()
        self.assertEqual([entry["stage"] for entry in result["errors"]], ["auth"])
        self.assertIn("'datafed setup'", result["errors"][0]["message"])
        self.assertEqual(self.states(), ["captured"])
        self.assertIsNone(result["status"]["user"])
        self.assertIn("'datafed ep default set <endpoint>'", result["status"]["problems"][0])

    def test_failed_transfer_is_retried(self):
        session = self.session(points=1, dark_file=None)
        self.sync()
        hidden = session["files"][0].with_suffix(".hidden")
        session["files"][0].rename(hidden)
        failed = self.sync()
        self.assertEqual([(entry["stage"], entry["message"]) for entry in failed["errors"]],
                         [("upload", "globus: source path not found")])
        hidden.rename(session["files"][0])
        self.sync()
        self.assertEqual(self.sync()["uploads_done"], 1)
        self.assertEqual(self.states(), ["uploaded"])

    def test_uploads_without_progress_are_named_in_the_status(self):
        self.session(points=1, dark_file=None)
        self.sync()
        self.assertEqual(self.sync()["status"]["problems"], [])
        self.ctx.ledger._db.execute("UPDATE captures SET state = 'uploading', updated_utc = '2026-11-13T15:00:00.000Z'")
        problems = commands_core.cmd_status(Namespace(), self.ctx)["problems"]
        self.assertEqual(len(problems), 1)
        self.assertIn("1 upload(s) have made no progress", problems[0])
        self.assertIn("Globus Connect Personal", problems[0])

    def test_a_client_that_cannot_connect_at_all_spends_no_attempt(self):
        self.session(points=1)
        text = "Incompatible server api detected 4.0.0, you are running 3.1.0 consider upgrading the datafed client."
        catalog = DataFedCatalog(context=PROJECT)
        ctx = support.context(self, self.home, catalog=catalog)
        with mock.patch("datafed.CommandLib.API", side_effect=Exception(text)) as constructor:
            for _pass in range(7):
                result = commands_core.cmd_sync(Namespace(), ctx)
                self.assertEqual((result["online"], result["errors"]), (False, []))
        self.assertGreaterEqual(constructor.call_count, 7)
        (row,) = ctx.ledger.rows()
        self.assertEqual((row["state"], row["register_attempts"]), ("captured", 0))
        self.assertEqual(result["status"]["problems"],
                         ["The catalog cannot be reached. 1 capture(s) wait for the next sync."])
        with mock.patch("datafed.CommandLib.API", side_effect=Exception(text)):
            status = commands_core.cmd_status(Namespace(), ctx)
        self.assertIs(status["online"], False)
        self.assertIn("DataFed cannot be used from this Mac (connect: Incompatible server api detected",
                      status["problems"][0])

    def test_without_the_datafed_package_a_sync_spends_no_attempt(self):
        self.session(points=2)
        ctx = support.context(self, self.home, catalog=DataFedCatalog(context=PROJECT))
        # An interpreter without the package, or without a module it needs.
        with mock.patch.dict("sys.modules", {"datafed": None, "datafed.CommandLib": None}):
            for _pass in range(7):
                result = commands_core.cmd_sync(Namespace(), ctx)
                self.assertEqual((result["online"], result["errors"]), (False, []))
            status = commands_core.cmd_status(Namespace(), ctx)
        self.assertEqual([(row["state"], row["register_attempts"]) for row in ctx.ledger.rows()], [("captured", 0)] * 2)
        self.assertEqual((status["online"], status["sync_work"]), (False, 2))
        self.assertIn("The datafed Python package is not available in this interpreter", status["problems"][0])
        self.assertIn("setup_env.sh", status["problems"][0])
        self.assertEqual(self.server.calls, [])


class SimulatedCaptures(DataFedCase):
    """In datafed mode simulated captures stay on this Mac unless datafed.include_simulated is true."""

    def simulated_session(self, folder="1d_x_scan_simulated", points=2):
        dark = support.write_dark(self.home, name="dark_20261113_101500_002.txt", simulated=True)
        return support.make_session(self.home, folder=folder, points=points, dark_file=dark.name)

    def set_include_simulated(self, value):
        commands_core.cmd_config(Namespace(set=[["datafed.include_simulated=%s" % value]]), self.ctx)

    def raw_records(self):
        return [record for record in self.server.records.values()
                if record["metadata"].get("type") == "raman.raw_spectrum"]

    def test_they_wait_without_failing_and_are_counted_with_how_to_send_them(self):
        simulated = self.simulated_session()
        measured = self.session(folder="1d_x_scan_measured", points=1)
        hint = ("2 simulated capture(s) stay on this Mac and are not sent to DataFed: to send them as well, run "
                "config --set datafed.include_simulated=true.")
        for _pass in range(4):
            result = self.sync()
            self.assertEqual((result["held_simulated"], result["errors"]), (2, []))
            self.assertEqual(result["status"]["held_simulated"], 2)
            self.assertIn(hint, result["status"]["problems"])
        self.assertEqual([record["metadata"]["capture_id"] for record in self.raw_records()], measured["capture_ids"])
        # Neither the simulated spectra nor their simulated dark went to DataFed.
        self.assertEqual([record["metadata"]["file"] for record in self.server.records.values()
                          if record["metadata"].get("type") == "raman.dark_spectrum"], [self.dark.name])
        for capture_id in simulated["capture_ids"]:
            row = self.ctx.ledger.get(capture_id)
            self.assertEqual((row["state"], row["register_attempts"], row["error_message"]), ("captured", 0, None))
        # Held on purpose, so not waiting: the window's timer does not keep syncing for them.
        status = result["status"]
        self.assertEqual((status["counts"]["captured"], status["pending"], status["sync_work"]), (2, 0, 0))

        self.set_include_simulated("true")
        result = self.sync()
        self.assertEqual((result["registered"], result["held_simulated"], result["errors"]), (2, 0, []))
        self.assertEqual(result["status"]["held_simulated"], 0)
        self.assertFalse(any("simulated capture(s)" in problem for problem in result["status"]["problems"]))
        self.assertEqual(len(self.raw_records()), 3)
        titles = [record["title"] for record in self.raw_records()]
        self.assertEqual(sum(title.endswith("[simulated]") for title in titles), 2)
        self.sync()
        self.assertEqual(self.states(), ["uploaded"] * 3)

    def test_the_offline_sentence_counts_only_what_waits(self):
        self.simulated_session(points=1)
        self.session(folder="1d_x_scan_measured", points=1)
        self.server.offline = True
        status = self.sync()["status"]
        self.assertEqual((status["pending"], status["held_simulated"]), (1, 1))
        self.assertEqual(status["problems"][0], "The catalog cannot be reached. 1 capture(s) wait for the next sync.")

    def test_a_change_of_catalog_settings_is_said_to_leave_them_on_this_mac(self):
        self.simulated_session(points=1)
        self.session(folder="1d_x_scan_measured", points=1)
        self.sync()
        commands_core.cmd_config(Namespace(set=[["datafed.root_alias=second-root"]]), self.ctx)
        changed = ("The catalog settings changed: the next sync registers every capture in "
                   "DataFed (project p/raman.test)")
        self.assertIn(changed + ", except the simulated ones, which stay on this Mac.",
                      commands_core.cmd_status(Namespace(), self.ctx)["problems"])
        self.set_include_simulated("true")
        self.assertIn(changed + ".", commands_core.cmd_status(Namespace(), self.ctx)["problems"])

    def test_after_switching_from_the_demo_status_counts_what_the_first_sync_holds_and_sends(self):
        settings = load_settings(self.home)
        settings.set("mode", "local-demo")
        settings.save()
        demo = support.context(self, self.home)
        self.simulated_session(points=3)
        self.session(folder="1d_x_scan_measured", points=2)
        for _pass in range(2):
            commands_core.cmd_sync(Namespace(), demo)
        self.assertEqual(demo.ledger.counts()["uploaded"], 5)

        settings.set("mode", "datafed")
        settings.save()
        self.ctx.reload()
        before = commands_core.cmd_status(Namespace(), self.ctx)
        # Every capture is registered again in DataFed, except the simulated ones, which the settings hold.
        self.assertEqual((before["held_simulated"], before["pending"], before["sync_work"], before["catalog_changed"]),
                         (3, 2, 2, True))
        self.assertIn("3 simulated capture(s) stay on this Mac", " ".join(before["problems"]))
        result = self.sync()
        self.assertEqual((result["registered"], result["held_simulated"]), (2, 3))
        after = result["status"]
        self.assertEqual((after["held_simulated"], after["pending"], after["catalog_changed"]), (3, 2, False))

    def test_with_include_simulated_they_are_registered_like_any_other(self):
        self.set_include_simulated("true")
        self.simulated_session()
        result = self.sync()
        self.assertEqual((result["registered"], result["held_simulated"]), (2, 0))
        self.assertEqual(result["status"]["problems"], [])

    def test_the_collection_of_a_session_in_a_long_named_folder_still_says_simulated(self):
        self.set_include_simulated("true")
        folder = "measurements of the plate from the old instrument, second day, repeated after cleaning"
        session = self.simulated_session(folder=folder, points=1)
        self.assertEqual(self.sync()["errors"], [])
        title = self.server.collections[self.server.aliases[session_alias(session["session_id"])]]["title"]
        self.assertEqual(title, "S-001 - measurements of the plate from the old instrument, second... [simulated]")

    def test_an_imported_session_nobody_recorded_simulated_for_is_sent(self):
        legacy = support.make_session(self.home, root=support.make_home(self), points=2, with_capture=False,
                                      with_session_json=False)
        commands_core.cmd_import_session(Namespace(folder=str(legacy["folder"]), sample_id="S-OLD",
                                                   operator="Test Operator", location="Bench 2"), self.ctx)
        result = self.sync()
        self.assertEqual((result["registered"], result["held_simulated"]), (2, 0))

    def test_a_simulated_capture_whose_registration_failed_is_held_too_and_not_retried(self):
        self.set_include_simulated("true")
        session = self.simulated_session(points=1)
        names = ensure_layout(self.catalog, "raman-portable")
        self.catalog.create_record(raw_alias(session["capture_ids"][0]), "Somebody else's record",
                                   {"capture_id": "another-capture"}, names.sessions)
        self.assertEqual([entry["stage"] for entry in self.sync()["errors"]], ["register"])

        self.set_include_simulated("false")
        result = self.sync()
        self.assertEqual((result["errors"], result["held_simulated"]), ([], 1))
        row = self.ctx.ledger.get(session["capture_ids"][0])
        self.assertEqual((row["state"], row["error_stage"], row["register_attempts"]), ("error", "register", 1))
        problems = result["status"]["problems"]
        self.assertFalse(any("will be tried again" in problem for problem in problems), problems)
        self.assertIn("1 simulated capture(s) stay on this Mac", " ".join(problems))

        self.set_include_simulated("true")
        self.assertEqual([entry["stage"] for entry in self.sync()["errors"]], ["register"])
        self.assertEqual(self.ctx.ledger.get(session["capture_ids"][0])["register_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
