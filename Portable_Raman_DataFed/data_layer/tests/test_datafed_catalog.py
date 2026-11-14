"""The DataFed adapter against fakes. Nothing here connects to DataFed.

The fake replies are built from the protobuf reply classes of the installed datafed package,
so a field name the adapter gets wrong fails here. The API object is an autospec of the real
class, so a keyword argument the real method does not have fails here too. The real API class
is never constructed: its constructor connects to the DataFed server.
"""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from datafed import envelope_pb2 as sdms
from datafed.CommandLib import API
from raman_datafed.catalog import (
    Catalog,
    CatalogAuthRequired,
    CatalogConflict,
    CatalogError,
    CatalogNotFound,
    CatalogOffline,
    Collection,
    Dep,
    RecordSummary,
    Transfer,
    open_catalog,
)
from raman_datafed.config import Paths, load_settings
from raman_datafed.datafed_catalog import DataFedCatalog

PROJECT = "p/raman.test"


def record_reply(record_id="d/101", alias="rs-abc", title="Raw", metadata=None, deps=(), size=0, source=""):
    reply = sdms.RecordDataReply()
    data = reply.data.add()
    data.id = record_id
    data.alias = alias
    data.title = title
    data.desc = "about"
    data.tags.append("raman")
    data.metadata = json.dumps(metadata if metadata is not None else {"capture_id": "c1"})
    data.size = size
    data.source = source
    for dep_type, direction, dep_id, dep_alias in deps:
        dep = data.deps.add()
        dep.type = dep_type
        dep.dir = direction
        dep.id = dep_id
        dep.alias = dep_alias
    return reply, "RecordDataReply"


def collection_reply(collection_id="c/7", alias="raman-portable", title="Portable Raman", parent="c/u_tester_root"):
    reply = sdms.CollDataReply()
    data = reply.coll.add()
    data.id = collection_id
    data.alias = alias
    data.title = title
    data.desc = "collection"
    data.parent_id = parent
    return reply, "CollDataReply"


def listing_reply(items, total, offset=0):
    reply = sdms.ListingReply()
    for item_id, alias, title in items:
        item = reply.item.add()
        item.id = item_id
        item.alias = alias
        item.title = title
    reply.offset = offset
    reply.count = len(items)
    reply.total = total
    return reply, "ListingReply"


def put_reply(task_id="task/55", status=sdms.TS_READY):
    reply = sdms.DataPutReply()
    reply.item.id = "d/101"
    reply.task.id = task_id
    reply.task.status = status
    return reply, "DataPutReply"


def task_reply(task_id="task/55", status=sdms.TS_SUCCEEDED, message=""):
    reply = sdms.TaskDataReply()
    task = reply.task.add()
    task.id = task_id
    task.type = sdms.TT_DATA_PUT
    task.status = status
    task.msg = message
    return reply, "TaskDataReply"


def refusal(text):
    """What the datafed package raises for a NackReply or a time-out: a plain Exception."""
    return Exception(text)


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.api = mock.create_autospec(API, instance=True)
        self.api.getAuthUser.return_value = "u/tester"
        self.api.endpointDefaultGet.return_value = "5a1b0e6e-1111-2222-3333-444455556666"
        self.catalog = DataFedCatalog(context=PROJECT, api=self.api)


class TestTools(unittest.TestCase):
    def test_the_fakes_are_strict(self):
        api = mock.create_autospec(API, instance=True)
        with self.assertRaises(TypeError):
            api.dataView("d/1", contxt=PROJECT)
        with self.assertRaises(AttributeError):
            sdms.RecordData().meta_data = "{}"
        with self.assertRaises(AttributeError):
            api.dataFetch


class Connection(unittest.TestCase):
    def test_nothing_connects_until_a_catalog_call_is_made(self):
        home = support.make_home(self)
        settings = load_settings(home)
        settings.set("mode", "datafed")
        settings.set("datafed.context", PROJECT)
        with mock.patch("datafed.CommandLib.API") as constructor:
            catalog = open_catalog(settings, Paths(home))
            self.assertIsInstance(catalog, Catalog)
            self.assertEqual((catalog.name, catalog.label), ("datafed", "DataFed (project p/raman.test)"))
            constructor.assert_not_called()
        self.assertEqual(DataFedCatalog().label, "DataFed (your own space)")

    def test_the_api_object_is_built_once_on_first_use(self):
        api = mock.create_autospec(API, instance=True)
        api.getAuthUser.return_value = "u/tester"
        api.dataView.return_value = record_reply()
        with mock.patch("datafed.CommandLib.API", return_value=api) as constructor:
            catalog = DataFedCatalog()
            catalog.get_record("d/101")
            catalog.get_record("d/101")
        constructor.assert_called_once_with()

    def failure_on_connect(self, text):
        with mock.patch("datafed.CommandLib.API", side_effect=refusal(text)):
            with self.assertRaises(CatalogError) as raised:
                DataFedCatalog().get_record("d/101")
        return raised.exception

    def test_unreachable_server_is_offline(self):
        for text in ("Timeout!!!!!!!!!", "Timeout waiting for server connection. Make sure the right ports are open.",
                     "Could not open server public key file: /nowhere/datafed-core-key.pub"):
            self.assertIsInstance(self.failure_on_connect(text), CatalogOffline, text)

    def test_a_client_that_cannot_connect_at_all_is_like_an_unreachable_server(self):
        # No request was made, so this is no refusal of one: it ends a sync without spending an attempt.
        for text in ("Incompatible server api detected 4.0.0, you are running 3.1.0 consider upgrading.",
                     "Invalid server public key", "Could not find location of server public key file."):
            error = self.failure_on_connect(text)
            self.assertIsInstance(error, CatalogOffline, text)
            self.assertEqual(str(error), "DataFed cannot be used from this Mac")
            self.assertEqual(error.detail, "connect: " + text)
        with mock.patch("datafed.CommandLib.API", side_effect=refusal("Invalid server public key")):
            who = DataFedCatalog(context=PROJECT).whoami()
        self.assertEqual((who["online"], who["problem"]),
                         (False, "DataFed cannot be used from this Mac (connect: Invalid server public key)."))

    def test_unusable_credentials_tell_the_user_what_to_run(self):
        for text in ("Client key file(s) or client configuration directory not specified or invalid.",
                     "Invalid client private key"):
            error = self.failure_on_connect(text)
            self.assertIsInstance(error, CatalogAuthRequired, text)
            self.assertIn("'datafed setup'", str(error))
            self.assertIn("'datafed ep default set <endpoint>'", str(error))

    def test_anonymous_connection_needs_credentials(self):
        api = mock.create_autospec(API, instance=True)
        api.getAuthUser.return_value = None
        catalog = DataFedCatalog(api=api)
        with self.assertRaises(CatalogAuthRequired) as raised:
            catalog.get_record("d/101")
        self.assertIn("run 'datafed setup' yourself", str(raised.exception))
        self.assertIn("'datafed ep default set <endpoint>'", str(raised.exception))
        api.dataView.assert_not_called()

    def test_missing_package_is_reported_with_the_way_to_install_it(self):
        with mock.patch.dict("sys.modules", {"datafed.CommandLib": None}):
            with self.assertRaises(CatalogError) as raised:
                DataFedCatalog().get_record("d/101")
        self.assertIn("setup_env.sh", raised.exception.detail)

    def test_what_the_package_prints_does_not_reach_stdout(self):
        api = mock.create_autospec(API, instance=True)
        api.getAuthUser.return_value = "u/tester"

        def noisy_view(*_args, **_kwargs):
            print("File downloaded successfully.")
            return record_reply()

        api.dataView.side_effect = noisy_view
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            DataFedCatalog(api=api).get_record("d/101")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("File downloaded successfully.", err.getvalue())


class Records(AdapterCase):
    def test_get_record_maps_fields_and_both_dependency_directions(self):
        self.api.dataView.return_value = record_reply(
            alias="u:tester:rs-abc", size=12345,
            deps=[(sdms.DEP_IS_DERIVED_FROM, sdms.DIR_IN, "d/202", "rp-xyz"),
                  (sdms.DEP_IS_COMPONENT_OF, sdms.DIR_OUT, "d/303", ""),
                  (sdms.DEP_IS_NEW_VERSION_OF, sdms.DIR_OUT, "d/404", "p:raman.test:old")])
        record = self.catalog.get_record("rs-abc")
        self.api.dataView.assert_called_once_with("rs-abc", context=PROJECT)
        self.assertEqual((record.id, record.alias, record.title, record.description),
                         ("d/101", "rs-abc", "Raw", "about"))
        self.assertEqual((record.metadata, record.tags, record.data_size, record.created),
                         ({"capture_id": "c1"}, ("raman",), 12345, False))
        self.assertEqual(record.deps, (Dep("der", "d/202", "rp-xyz", "in"), Dep("comp", "d/303", None, "out"),
                                       Dep("ver", "d/404", "old", "out")))

    def test_get_record_that_cannot_be_viewed_is_not_found(self):
        for text in ("Record 'd/999' does not exist.", "Alias 'nope' not found", "Permission denied"):
            self.api.dataView.side_effect = refusal(text)
            with self.assertRaises(CatalogNotFound, msg=text):
                self.catalog.get_record("d/999")
        self.api.dataView.side_effect = None
        self.api.dataView.return_value = (sdms.RecordDataReply(), "RecordDataReply")
        with self.assertRaises(CatalogNotFound):
            self.catalog.get_record("d/999")

    def test_time_out_and_authentication_refusals_are_told_apart(self):
        self.api.dataView.side_effect = refusal("Timeout!!!!!!!!!")
        with self.assertRaises(CatalogOffline):
            self.catalog.get_record("d/101")
        for text in ("Authentication required", "Server error 5", "Server error 6"):
            self.api.dataView.side_effect = refusal(text)
            with self.assertRaises(CatalogAuthRequired, msg=text):
                self.catalog.get_record("d/101")
        self.api.dataView.side_effect = ConnectionResetError("peer")
        with self.assertRaises(CatalogOffline):
            self.catalog.get_record("d/101")

    def test_names_repeated_in_a_refusal_are_not_taken_for_its_reason(self):
        # The alias holds words that would otherwise read as "credentials", "not found" and "timeout".
        alias = "credential-not-found-timeout"
        self.api.dataCreate.side_effect = refusal("Alias, p:raman.test:%s, already in use" % alias)
        self.api.dataView.return_value = record_reply(alias=alias)
        record = self.catalog.create_record(alias, "Raw", {"capture_id": "c1"}, "rsess-1", identity_key="capture_id")
        self.assertEqual((record.id, record.created), ("d/101", False))

        self.api.collectionView.side_effect = refusal("Collection 'timeout-lab-authenticated' is unknown")
        self.api.collectionCreate.return_value = collection_reply("c/9", "timeout-lab-authenticated", "Lab")
        self.assertEqual(self.catalog.ensure_collection("timeout-lab-authenticated", "Lab").id, "c/9")

    def test_unexpected_reply_type_is_an_error(self):
        self.api.dataView.return_value = collection_reply()
        with self.assertRaises(CatalogError):
            self.catalog.get_record("d/101")

    def test_create_record_sends_alias_metadata_parent_and_dependencies(self):
        metadata = {"type": "raman.prediction", "capture_id": "c1", "confidence": 0.93}
        self.api.dataCreate.return_value = record_reply("d/202", "rp-xyz", "Prediction", metadata)
        record = self.catalog.create_record(
            "rp-xyz", "Prediction", metadata, "raman-portable-predictions", deps=[("der", "d/101"), ("der", "d/50")],
            description="made by a test", tags=("raman",), identity_key="capture_id")
        self.api.dataCreate.assert_called_once_with(
            "Prediction", alias="rp-xyz", description="made by a test", tags=["raman"], metadata=mock.ANY,
            parent_id="raman-portable-predictions", deps=[["der", "d/101"], ["der", "d/50"]], repo_id=None,
            context=PROJECT)
        self.assertEqual(json.loads(self.api.dataCreate.call_args.kwargs["metadata"]), metadata)
        self.assertEqual((record.id, record.alias, record.metadata, record.created),
                         ("d/202", "rp-xyz", metadata, True))
        self.api.dataView.assert_not_called()

    def test_create_record_uses_the_configured_allocation_and_shortens_the_title(self):
        catalog = DataFedCatalog(repo_id="repo/lab", api=self.api)
        self.api.dataCreate.return_value = record_reply()
        catalog.create_record("rs-abc", "T" * 120, {"capture_id": "c1"}, "rsess-1", identity_key="capture_id")
        arguments = self.api.dataCreate.call_args
        self.assertEqual(len(arguments.args[0]), 80)
        self.assertEqual((arguments.kwargs["repo_id"], arguments.kwargs["context"], arguments.kwargs["deps"],
                          arguments.kwargs["tags"], arguments.kwargs["description"]),
                         ("repo/lab", None, None, None, None))

    def test_alias_in_use_with_the_same_identity_returns_the_existing_record(self):
        self.api.dataCreate.side_effect = refusal("Alias, p:raman.test:rs-abc, already in use")
        self.api.dataView.return_value = record_reply(metadata={"capture_id": "c1", "sample_id": "S-001"})
        record = self.catalog.create_record("rs-abc", "Raw", {"capture_id": "c1"}, "rsess-1", identity_key="capture_id")
        self.api.dataView.assert_called_once_with("rs-abc", context=PROJECT)
        self.assertEqual((record.id, record.created), ("d/101", False))
        self.assertEqual(record.metadata["sample_id"], "S-001")

    def test_alias_in_use_with_a_different_identity_is_a_conflict(self):
        self.api.dataCreate.side_effect = refusal("Alias, p:raman.test:rs-abc, already in use")
        self.api.dataView.return_value = record_reply(metadata={"capture_id": "someone-else"})
        with self.assertRaises(CatalogConflict) as raised:
            self.catalog.create_record("rs-abc", "Raw", {"capture_id": "c1"}, "rsess-1", identity_key="capture_id")
        self.assertIn("different capture_id", str(raised.exception))

    def test_refused_creation_without_an_existing_record_keeps_the_servers_reason(self):
        self.api.dataCreate.side_effect = refusal("No allocation available")
        self.api.dataView.side_effect = refusal("Alias 'rs-abc' does not exist")
        with self.assertRaises(CatalogError) as raised:
            self.catalog.create_record("rs-abc", "Raw", {"capture_id": "c1"}, "rsess-1", identity_key="capture_id")
        self.assertNotIsInstance(raised.exception, (CatalogConflict, CatalogNotFound, CatalogOffline))
        self.assertIn("No allocation available", str(raised.exception))

    def test_creation_while_offline_asks_nothing_else(self):
        self.api.dataCreate.side_effect = refusal("Timeout!!!!!!!!!")
        with self.assertRaises(CatalogOffline):
            self.catalog.create_record("rs-abc", "Raw", {"capture_id": "c1"}, "rsess-1", identity_key="capture_id")
        self.api.dataView.assert_not_called()

    def test_invalid_alias_or_relation_is_refused_before_any_request(self):
        with self.assertRaises(CatalogError):
            self.catalog.create_record("Not Valid", "Raw", {}, "rsess-1")
        with self.assertRaises(CatalogError):
            self.catalog.create_record("rs-abc", "Raw", {}, "rsess-1", deps=[("copy", "d/1")])
        with self.assertRaises(CatalogError):
            self.catalog.create_record("rs-abc", "Raw", {}, "rsess-1", identity_key="capture_id")
        self.api.dataCreate.assert_not_called()

    def test_update_record_merges_and_then_views_the_record(self):
        self.api.dataUpdate.return_value = record_reply()
        self.api.dataView.return_value = record_reply(metadata={"capture_id": "c1", "needs_review": False})
        patch = {"review": {"status": "confirmed"}, "needs_review": False}
        record = self.catalog.update_record("d/202", metadata=patch, deps_add=[("der", "d/9")], title="Checked")
        self.api.dataUpdate.assert_called_once_with(
            "d/202", title="Checked", metadata=json.dumps(patch), deps_add=[["der", "d/9"]], context=PROJECT)
        self.api.dataView.assert_called_once_with("d/202", context=PROJECT)
        self.assertIs(record.metadata["needs_review"], False)

    def test_update_record_without_changes_to_title_or_dependencies(self):
        self.api.dataUpdate.return_value = record_reply()
        self.api.dataView.return_value = record_reply()
        self.catalog.update_record("d/202", metadata={"a": 1})
        self.api.dataUpdate.assert_called_once_with("d/202", title=None, metadata='{"a": 1}', deps_add=None,
                                                    context=PROJECT)


class Collections(AdapterCase):
    def test_existing_collection_is_returned_without_creating(self):
        self.api.collectionView.return_value = collection_reply(alias="p:raman.test:raman-portable")
        collection = self.catalog.ensure_collection("raman-portable", "Portable Raman")
        self.api.collectionView.assert_called_once_with("raman-portable", context=PROJECT)
        self.api.collectionCreate.assert_not_called()
        self.assertEqual(collection,
                         Collection("c/7", "raman-portable", "Portable Raman", "collection", "c/u_tester_root"))

    def test_missing_collection_is_created_under_the_root_or_the_given_parent(self):
        self.api.collectionView.side_effect = refusal("Alias 'raman-portable' does not exist")
        self.api.collectionCreate.return_value = collection_reply()
        self.catalog.ensure_collection("raman-portable", "Portable Raman", "Spectra")
        self.api.collectionCreate.assert_called_once_with(
            "Portable Raman", alias="raman-portable", description="Spectra", parent_id="root", context=PROJECT)

        self.api.collectionCreate.reset_mock()
        self.api.collectionCreate.return_value = collection_reply("c/8", "raman-portable-darks", "Dark spectra", "c/7")
        child = self.catalog.ensure_collection("raman-portable-darks", "Dark spectra", parent="raman-portable")
        self.api.collectionCreate.assert_called_once_with(
            "Dark spectra", alias="raman-portable-darks", description=None, parent_id="raman-portable", context=PROJECT)
        self.assertEqual((child.id, child.parent), ("c/8", "c/7"))

    def test_collection_created_by_someone_else_in_between_is_found(self):
        self.api.collectionView.side_effect = [refusal("does not exist"), collection_reply()]
        self.api.collectionCreate.side_effect = refusal("Alias, p:raman.test:raman-portable, already in use")
        self.assertEqual(self.catalog.ensure_collection("raman-portable", "Portable Raman").id, "c/7")

    def test_creation_refused_for_another_reason_keeps_that_reason(self):
        self.api.collectionView.side_effect = refusal("does not exist")
        self.api.collectionCreate.side_effect = refusal("Permission denied")
        with self.assertRaises(CatalogError) as raised:
            self.catalog.ensure_collection("raman-portable", "Portable Raman")
        self.assertIn("Permission denied", str(raised.exception))

    def test_offline_while_looking_does_not_try_to_create(self):
        self.api.collectionView.side_effect = refusal("Timeout!!!!!!!!!")
        with self.assertRaises(CatalogOffline):
            self.catalog.ensure_collection("raman-portable", "Portable Raman")
        self.api.collectionCreate.assert_not_called()

    def test_link_and_unlink_send_the_item(self):
        self.api.collectionItemsUpdate.return_value = listing_reply([], 0)
        self.catalog.link("raman-portable-to-classify", "d/101")
        self.api.collectionItemsUpdate.assert_called_once_with("raman-portable-to-classify", add_ids=["d/101"],
                                                               context=PROJECT)
        self.api.collectionItemsUpdate.reset_mock()
        self.catalog.unlink("raman-portable-needs-review", "d/202")
        self.api.collectionItemsUpdate.assert_called_once_with("raman-portable-needs-review", rem_ids=["d/202"],
                                                               context=PROJECT)

    def test_linking_twice_is_not_an_error_whatever_the_server_says(self):
        self.api.collectionItemsUpdate.side_effect = refusal("Item already linked")
        self.api.collectionItemsList.return_value = listing_reply([("d/101", "rs-abc", "Raw")], 1)
        self.catalog.link("raman-portable-to-classify", "d/101")
        self.catalog.link("raman-portable-to-classify", "rs-abc")

    def test_refused_link_of_an_item_that_is_not_there_is_an_error(self):
        self.api.collectionItemsUpdate.side_effect = refusal("Permission denied")
        self.api.collectionItemsList.return_value = listing_reply([], 0)
        with self.assertRaises(CatalogError):
            self.catalog.link("raman-portable-to-classify", "d/101")

    def test_unlinking_what_is_not_linked_is_not_an_error(self):
        self.api.collectionItemsUpdate.side_effect = refusal("Item not linked")
        self.api.collectionItemsList.return_value = listing_reply([], 0)
        self.catalog.unlink("raman-portable-needs-review", "d/202")
        self.api.collectionItemsList.return_value = listing_reply([("d/202", "rp-xyz", "Prediction")], 1)
        with self.assertRaises(CatalogError):
            self.catalog.unlink("raman-portable-needs-review", "d/202")

    def test_list_collection_reads_every_page(self):
        first = [("d/%d" % number, "rs-%d" % number, "Raw %d" % number) for number in range(100)]
        second = [("d/%d" % number, "", "Raw %d" % number) for number in range(100, 150)]
        self.api.collectionItemsList.side_effect = [listing_reply(first, 150), listing_reply(second, 150, offset=100)]
        items = self.catalog.list_collection("raman-portable-to-classify")
        self.assertEqual(len(items), 150)
        self.assertEqual(items[0], RecordSummary("d/0", "rs-0", "Raw 0"))
        self.assertEqual(items[149], RecordSummary("d/149", None, "Raw 149"))
        self.assertEqual(self.api.collectionItemsList.call_args_list, [
            mock.call("raman-portable-to-classify", offset=0, count=100, context=PROJECT),
            mock.call("raman-portable-to-classify", offset=100, count=100, context=PROJECT)])

    def test_list_of_an_empty_collection(self):
        self.api.collectionItemsList.return_value = listing_reply([], 0)
        self.assertEqual(self.catalog.list_collection("raman-portable-needs-review"), [])
        self.api.collectionItemsList.assert_called_once()


class Transfers(AdapterCase):
    def test_put_data_starts_a_transfer_from_the_local_path_and_returns_at_once(self):
        self.api.dataPut.return_value = put_reply("task/55", sdms.TS_READY)
        transfer = self.catalog.put_data("d/101", "/Users/tester/raman/scan_sessions/s1/point_0001.json")
        self.api.dataPut.assert_called_once_with(
            "d/101", "/Users/tester/raman/scan_sessions/s1/point_0001.json", wait=False, context=PROJECT)
        self.assertEqual(transfer, Transfer("task/55", "queued", ""))

    def test_put_data_makes_a_relative_path_absolute(self):
        self.api.dataPut.return_value = put_reply()
        self.catalog.put_data("d/101", "point_0001.json")
        self.assertEqual(self.api.dataPut.call_args.args[1], os.path.abspath("point_0001.json"))

    def test_put_data_without_a_task_or_an_endpoint(self):
        self.api.dataPut.return_value = (sdms.DataPutReply(), "DataPutReply")
        with self.assertRaises(CatalogError):
            self.catalog.put_data("d/101", "/x/point_0001.json")
        self.api.dataPut.side_effect = refusal("No endpoint set.")
        with self.assertRaises(CatalogAuthRequired) as raised:
            self.catalog.put_data("d/101", "/x/point_0001.json")
        self.assertIn("'datafed ep default set <endpoint>'", str(raised.exception))

    def test_transfer_status_maps_every_datafed_status(self):
        expected = {sdms.TS_BLOCKED: "queued", sdms.TS_READY: "queued", sdms.TS_RUNNING: "running",
                    sdms.TS_SUCCEEDED: "succeeded", sdms.TS_FAILED: "failed"}
        self.assertEqual(set(expected), set(sdms.TaskStatus.values()))
        for value, status in expected.items():
            self.api.taskView.return_value = task_reply("task/55", value, "why" if status == "failed" else "")
            transfer = self.catalog.transfer_status("task/55")
            self.assertEqual((transfer.task_id, transfer.status), ("task/55", status))
        self.assertEqual(transfer.message, "why")
        self.api.taskView.assert_called_with("task/55")

    def test_transfer_status_of_an_unknown_task(self):
        self.api.taskView.return_value = (sdms.TaskDataReply(), "TaskDataReply")
        with self.assertRaises(CatalogNotFound):
            self.catalog.transfer_status("task/999")

    def test_get_data_waits_for_the_download_and_returns_the_file(self):
        dest = support.make_home(self) / "staging"
        self.api.dataView.return_value = record_reply(size=20, source="5a1b0e6e/Users/tester/s1/point_0001.json")

        def download(record_id, path, **_options):
            with open(os.path.join(path, "point_0001.json"), "w", encoding="utf-8") as stream:
                stream.write('{"intensity": []}')
            return task_reply("task/77", sdms.TS_SUCCEEDED)

        self.api.dataGet.side_effect = download
        path = self.catalog.get_data("rs-abc", dest)
        self.api.dataGet.assert_called_once_with("d/101", str(dest), orig_fname=True, wait=True, timeout_sec=600,
                                                 context=PROJECT)
        self.assertEqual(path, dest / "point_0001.json")
        self.assertTrue(path.is_file())

    def test_get_data_finds_the_file_when_the_record_names_no_source(self):
        dest = support.make_home(self)
        self.api.dataView.return_value = record_reply(size=20)

        def download(record_id, path, **_options):
            with open(os.path.join(path, "model.zip"), "wb") as stream:
                stream.write(b"zip")
            return task_reply("task/77", sdms.TS_SUCCEEDED)

        self.api.dataGet.side_effect = download
        self.assertEqual(self.catalog.get_data("d/101", dest), dest / "model.zip")

    def test_get_data_failures(self):
        dest = support.make_home(self)
        self.api.dataView.return_value = record_reply()
        self.api.dataGet.side_effect = refusal("Specified record(s) contain no raw data.")
        with self.assertRaises(CatalogNotFound):
            self.catalog.get_data("d/101", dest)
        self.api.dataGet.side_effect = None
        self.api.dataGet.return_value = task_reply("task/77", sdms.TS_FAILED, "endpoint not connected")
        with self.assertRaises(CatalogError) as raised:
            self.catalog.get_data("d/101", dest)
        self.assertEqual(raised.exception.detail, "endpoint not connected")
        self.api.dataGet.return_value = task_reply("task/77", sdms.TS_RUNNING)
        with self.assertRaisesRegex(CatalogError, "did not finish"):
            self.catalog.get_data("d/101", dest)
        self.api.dataGet.return_value = task_reply("task/77", sdms.TS_SUCCEEDED)
        with self.assertRaisesRegex(CatalogError, "its file is not in"):
            self.catalog.get_data("d/101", dest)


class WhoAmI(AdapterCase):
    def test_signed_in_user_with_project_context(self):
        self.api.userView.return_value = (sdms.UserDataReply(), "UserDataReply")
        self.api.projectView.return_value = (sdms.ProjectDataReply(), "ProjectDataReply")
        self.assertEqual(self.catalog.whoami(), {
            "online": True, "user": "u/tester", "context": PROJECT,
            "endpoint": "5a1b0e6e-1111-2222-3333-444455556666", "demo": False})
        self.api.userView.assert_called_once_with("u/tester")
        self.api.projectView.assert_called_once_with(PROJECT)

    def test_own_space_uses_the_user_as_context(self):
        self.api.userView.return_value = (sdms.UserDataReply(), "UserDataReply")
        who = DataFedCatalog(api=self.api).whoami()
        self.assertEqual((who["context"], who["user"]), ("u/tester", "u/tester"))
        self.api.projectView.assert_not_called()

    def test_offline_is_reported_not_raised(self):
        self.api.userView.side_effect = refusal("Timeout!!!!!!!!!")
        who = self.catalog.whoami()
        self.assertEqual((who["online"], who["demo"]), (False, False))
        self.assertIn("cannot be reached", who["problem"])

    def test_missing_credentials_are_reported_not_raised(self):
        self.api.getAuthUser.return_value = None
        who = self.catalog.whoami()
        self.assertEqual((who["online"], who["user"]), (True, None))
        self.assertIn("'datafed setup'", who["problem"])

    def test_missing_endpoint_and_unknown_project_are_problems(self):
        self.api.userView.return_value = (sdms.UserDataReply(), "UserDataReply")
        self.api.projectView.return_value = (sdms.ProjectDataReply(), "ProjectDataReply")
        self.api.endpointDefaultGet.return_value = None
        who = self.catalog.whoami()
        self.assertEqual((who["online"], who["endpoint"]), (True, None))
        self.assertIn("'datafed ep default set <endpoint>'", who["problem"])

        self.api.projectView.side_effect = refusal("Project 'p/raman.test' does not exist")
        who = self.catalog.whoami()
        self.assertEqual((who["online"], who["user"]), (True, "u/tester"))
        self.assertIn("does not exist", who["problem"])


if __name__ == "__main__":
    unittest.main()
