"""HTTP-contract tests: real Globus SDK, mocked HTTP, no credentials or transfers."""
import json
from pathlib import Path
import tempfile
import unittest

import globus_sdk
from globus_sdk.transport import RetryConfig
import requests
import responses

import bridge

API = bridge.TRANSFER_API
SUBMISSION = "c5746e3a-a79b-4cbd-b17e-06a6ef6277a5"
TASK = "922a121c-ad73-499b-afcb-bdf6f47cb98f"


class APIContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = globus_sdk.TransferClient(authorizer=globus_sdk.NullAuthorizer(),
                                                retry_config=RetryConfig(max_retries=0))
        self.addCleanup(self.client.close)
        self.service = bridge.GlobusBridge(record_root=Path(self.temp.name), client=self.client)
        self.spec = bridge.tutorial_spec()

    def submission_response(self):
        responses.get(API + "/submission_id", json={"DATA_TYPE": "submission_id", "value": SUBMISSION})
        responses.post(API + "/transfer", json={"DATA_TYPE": "transfer_result", "code": "Accepted", "task_id": TASK})

    @responses.activate
    def test_offline_preview_uses_sdk_without_network_or_task_id(self):
        path, result = self.service.preview(self.spec)
        self.assertFalse(result["submitted"])
        self.assertIsNone(result["task_id"])
        self.assertEqual(result["payload"]["DATA_TYPE"], "transfer")
        self.assertEqual(result["payload"]["DATA"][0]["source_path"], "/share/godata/file1.txt")
        self.assertNotIn("submission_id", result["payload"])
        self.assertEqual(len(responses.calls), 0)
        self.assertEqual(bridge.read_json(path), result)

    @responses.activate
    def test_live_submit_uses_correct_sdk_http_routes_and_body(self):
        self.submission_response()
        path, record = self.service.submit(self.spec)
        self.assertEqual(record["state"], "SUBMITTED")
        self.assertEqual(record["task_id"], TASK)
        self.assertEqual([call.request.method for call in responses.calls], ["GET", "POST"])
        body = json.loads(responses.calls[1].request.body)
        self.assertEqual(body["source_endpoint"], bridge.TUTORIAL_SOURCE)
        self.assertEqual(body["destination_endpoint"], bridge.TUTORIAL_DESTINATION)
        self.assertEqual(body["submission_id"], SUBMISSION)
        self.assertTrue(body["verify_checksum"])
        self.assertFalse(body["delete_destination_extra"])
        self.assertEqual(body["DATA"][0]["destination_path"], self.spec.destination_path)
        self.assertFalse(body["DATA"][0]["recursive"])
        self.assertEqual(bridge.read_json(path)["payload"], body)

    @responses.activate
    def test_status_and_events_use_official_routes(self):
        responses.get(API + "/task/" + TASK, json={"DATA_TYPE": "task", "task_id": TASK, "status": "SUCCEEDED", "files_transferred": 1})
        responses.get(API + "/task/" + TASK + "/event_list", json={"DATA_TYPE": "event_list", "DATA": [{"code": "SUCCEEDED", "description": "Test response"}]})
        self.assertEqual(self.service.task_status(TASK)["status"], "SUCCEEDED")
        self.assertEqual(self.service.events(TASK)["DATA"][0]["code"], "SUCCEEDED")

    @responses.activate
    def test_list_collection_directory_uses_operation_api(self):
        url = API + "/operation/endpoint/" + bridge.TUTORIAL_SOURCE + "/ls"
        responses.get(url, json={"DATA_TYPE": "file_list", "path": "/share/godata", "DATA": [{"name": "file1.txt", "type": "file"}]})
        result = self.service.list_directory(bridge.TUTORIAL_SOURCE, "/share/godata")
        self.assertEqual(result["DATA"][0]["name"], "file1.txt")
        self.assertIn("path=%2Fshare%2Fgodata", responses.calls[0].request.url)

    @responses.activate
    def test_cancel_is_post_and_does_not_claim_task_stopped(self):
        self.submission_response()
        path, record = self.service.submit(self.spec)
        responses.post(API + "/task/" + TASK + "/cancel", json={"DATA_TYPE": "result", "code": "Canceled", "message": "Cancel accepted"})
        result = self.service.cancel(TASK, path)
        self.assertEqual(result["code"], "Canceled")
        self.assertEqual(responses.calls[-1].request.method, "POST")
        saved = bridge.read_json(path)
        self.assertIn("cancel_requested_utc", saved)
        self.assertEqual(saved["state"], "SUBMITTED")

    @responses.activate
    def test_uncertain_submission_reuses_same_server_submission_id(self):
        responses.get(API + "/submission_id", json={"value": SUBMISSION})
        responses.post(API + "/transfer", body=requests.ConnectionError("Test-only connection loss"))
        with self.assertRaises(bridge.SubmissionUncertain) as context:
            self.service.submit(self.spec)
        path = context.exception.record_path
        self.assertEqual(bridge.read_json(path)["state"], "SUBMISSION_UNCERTAIN")
        responses.replace(responses.POST, API + "/transfer", json={"task_id": TASK})
        _, record = self.service.retry_saved(path)
        post_bodies = [json.loads(call.request.body) for call in responses.calls if call.request.method == "POST"]
        self.assertEqual(post_bodies[0], post_bodies[1])
        self.assertEqual(record["task_id"], TASK)
        self.assertEqual(sum(call.request.method == "GET" for call in responses.calls), 1)

    @responses.activate
    def test_edited_payload_cannot_reuse_submission_id(self):
        responses.get(API + "/submission_id", json={"value": SUBMISSION})
        responses.post(API + "/transfer", body=requests.ConnectionError("Test-only failure"))
        with self.assertRaises(bridge.SubmissionUncertain) as context:
            self.service.submit(self.spec)
        path = context.exception.record_path
        record = bridge.read_json(path)
        record["payload"]["DATA"][0]["destination_path"] = "/different-destination.txt"
        bridge.save_json(path, record)
        with self.assertRaisesRegex(ValueError, "payload changed"):
            self.service.retry_saved(path)
        self.assertEqual(sum(call.request.method == "POST" for call in responses.calls), 1)

    @responses.activate
    def test_consent_error_is_not_reported_as_success(self):
        responses.get(API + "/task/" + TASK, status=403, json={"code": "PermissionDenied", "message": "Test denied"})
        with self.assertRaises(globus_sdk.TransferAPIError) as context:
            self.service.task_status(TASK)
        self.assertIn("PermissionDenied", bridge.safe_error(context.exception))

    def test_collection_paths_are_not_local_windows_paths(self):
        for path in ("C:/Users/Jeremy666/file.txt", "C:\\file.txt", "/foo/../bar", ""):
            with self.assertRaises(ValueError):
                bridge.collection_path(path)

    def test_real_app_configuration_is_session_only_and_constructs_offline(self):
        service = bridge.GlobusBridge(record_root=Path(self.temp.name))
        try:
            service.ensure_client()
            self.assertIsInstance(service.app, globus_sdk.UserApp)
            self.assertIsInstance(service.client, globus_sdk.TransferClient)
            self.assertTrue(service.app.login_required())
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
