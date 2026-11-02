"""No hardware: exercise RunEngine, saved spectra, and a private fake Qt mailbox."""
import hashlib
import json
import logging
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from event_model import DocumentNames, schema_validators

import bridge


class FakeQt:
    def __init__(self, root, armed=True, stall=False):
        self.root = Path(root)
        self.armed, self.stall = armed, stall
        self.sid, self.active, self.phase, self.count = "", False, "idle", 0
        self.calls = []
        self.seed = bridge.SimulatedScan()
        self.seed.trigger()

    def status(self):
        if self.active and not self.stall:
            record, _ = self.seed.poll()[0]
            self.count += 1
            record["session_id"] = self.sid
            bridge.save_json(self.root / self.sid / f"point_{self.count:04d}.json", record)
            if self.count == 13:
                self.active, self.phase = False, "completed"
        return dict(boot_id="fake-boot", utc_ms=int(time.time()*1000), session_id=self.sid,
                    session_dir=str(self.root / self.sid), scan_state=self.phase,
                    scan_active=self.active, last_completed_point=self.count,
                    laser_on=True, laser_state_uncertain=False, backend_fault=False,
                    collecting=self.active, remote_scan_enabled=self.armed)

    def request(self, operation, request_id, **arguments):
        self.calls.append((operation, request_id, arguments))
        if operation == "start_scan":
            self.sid, self.active, self.phase = "fake-session", True, "running"
            (self.root / self.sid).mkdir()
        else:
            assert arguments["session_id"] == self.sid
            self.active, self.phase = False, "cancelled"
        return dict(ok=True, session_id=self.sid, session_dir=str(self.root / self.sid))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = dict(bridge.settings(), output_dir=self.root / "runs", poll_seconds=0.05)
        logging.getLogger("bluesky").setLevel(logging.CRITICAL)

    def fake_run(self, mailbox, **kwargs):
        paths = dict(bridge.device_settings(self.cfg), scan_root=self.root / "scans")
        paths["scan_root"].mkdir(exist_ok=True)
        with patch.object(bridge, "qt_client", return_value=mailbox), patch.object(bridge, "device_settings", return_value=paths):
            return bridge.run_experiment("hardware", cfg=self.cfg, analyze=False, **kwargs)

    def test_simulation_and_valid_event_documents(self):
        result = bridge.run_experiment(cfg=self.cfg)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["recorded_points"], 13)
        docs = [json.loads(line) for line in (Path(result["output_directory"]) / "documents.jsonl").read_text().splitlines()]
        for item in docs:
            schema_validators[DocumentNames(item["name"])].validate(item["doc"])
        events = [item["doc"] for item in docs if item["name"] == "event"]
        self.assertEqual([doc["seq_num"] for doc in events], list(range(1, 14)))
        self.assertEqual(len(events[0]["data"]["spectrum"]), 2048)
        self.assertEqual(result["labels"], {"SIMULATED": 13})

    def test_qt_complete_scan_and_one_start(self):
        mailbox = FakeQt(self.root / "scans")
        result = self.fake_run(mailbox)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["recorded_points"], 13)
        self.assertEqual(len(mailbox.calls), 1)
        self.assertEqual(mailbox.calls[0][0], "start_scan")
        self.assertEqual(result["qt_session_id"], "fake-session")

    def test_unarmed_device_rejected_without_start(self):
        mailbox = FakeQt(self.root / "scans", armed=False)
        result = self.fake_run(mailbox)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(mailbox.calls, [])
        self.assertIn("Enable remote scans", result["error"])

    def test_previous_status_tick_after_start_is_not_a_session_change(self):
        mailbox = FakeQt(self.root / "scans")
        original_request, original_status = mailbox.request, mailbox.status
        stale_pending = [False]
        def request(*args, **kwargs):
            result = original_request(*args, **kwargs)
            result["state"] = {"utc_ms": 1000}
            stale_pending[0] = True
            return result
        def status():
            if stale_pending[0]:
                stale_pending[0] = False
                return dict(boot_id="fake-boot", utc_ms=999, session_id="old-session")
            return original_status()
        mailbox.request, mailbox.status = request, status
        result = self.fake_run(mailbox)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["recorded_points"], 13)

    def test_timeout_cancels_owned_session_only(self):
        mailbox = FakeQt(self.root / "scans", stall=True)
        result = self.fake_run(mailbox, timeout=0.15)
        self.assertEqual(result["state"], "failed")
        self.assertEqual([call[0] for call in mailbox.calls], ["start_scan", "cancel_scan"])
        self.assertFalse(mailbox.active)

    def test_immediate_stop_waits_for_acknowledged_session_heartbeat(self):
        mailbox = FakeQt(self.root / "scans")
        original_request, original_status = mailbox.request, mailbox.status
        stale = [False]
        def request(*args, **kwargs):
            reply = original_request(*args, **kwargs)
            if args[0] == "start_scan":
                reply["state"] = {"utc_ms": 1000}
                stale[0] = True
            return reply
        def status():
            if stale[0]:
                stale[0] = False
                return dict(boot_id="fake-boot", utc_ms=999, session_id="old-session")
            return original_status()
        mailbox.request, mailbox.status = request, status
        cancel = threading.Event()
        def callback(name, value):
            if name == "log" and value.startswith("Session:"):
                cancel.set()
        result = self.fake_run(mailbox, cancel=cancel, on_event=callback)
        self.assertEqual(result["state"], "cancelled", result)
        self.assertEqual([call[0] for call in mailbox.calls], ["start_scan", "cancel_scan"])
        self.assertFalse(mailbox.active)

    def test_user_stop_records_partial_run_and_cancels(self):
        mailbox = FakeQt(self.root / "scans")
        cancel = threading.Event()
        def callback(name, value):
            if name == "point":
                cancel.set()
        result = self.fake_run(mailbox, cancel=cancel, on_event=callback)
        self.assertEqual(result["state"], "cancelled", result)
        self.assertEqual(result["recorded_points"], 1)
        self.assertEqual([call[0] for call in mailbox.calls], ["start_scan", "cancel_scan"])
        self.assertNotEqual(result["stop_document"]["exit_status"], "success")

    def test_stop_before_start_submits_nothing(self):
        mailbox = FakeQt(self.root / "scans")
        cancel = threading.Event()
        cancel.set()
        result = self.fake_run(mailbox, cancel=cancel)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(mailbox.calls, [])

    def test_uncertain_start_never_auto_retries(self):
        mailbox = FakeQt(self.root / "scans")
        def uncertain(*args, **kwargs):
            mailbox.calls.append(args)
            raise TimeoutError("Qt reply uncertain; inspect status")
        mailbox.request = uncertain
        result = self.fake_run(mailbox)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(mailbox.calls), 1)
        request = bridge.read_json(Path(result["output_directory"]) / "start_request.json")
        self.assertTrue(request["request_id"].startswith("bs-"))

    def test_session_change_does_not_cancel_other_scan(self):
        mailbox = FakeQt(self.root / "scans")
        original = mailbox.status
        def replaced():
            state = original()
            if mailbox.active:
                state["session_id"] = "someone-else"
            return state
        mailbox.status = replaced
        result = self.fake_run(mailbox)
        self.assertEqual(result["state"], "failed")
        self.assertEqual([call[0] for call in mailbox.calls], ["start_scan"])

    def test_replay_real_session_and_unchanged_input(self):
        folder = bridge.device_settings(self.cfg)["scan_root"] / "1d_y_scan_20260929_112812_885"
        if not folder.is_dir():
            self.skipTest("Local historical scan not available")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob("point_*.json")}
        result = bridge.run_experiment("replay", session_dir=folder, cfg=self.cfg)
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(result["recorded_points"], 13)
        self.assertEqual(len(result["model_sha256"]), 64)
        self.assertNotIn("SIMULATED", result["labels"])
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob("point_*.json")}
        self.assertEqual(before, after)

    def test_replay_mixed_session_fails(self):
        folder = self.root / "replay"
        source = bridge.SimulatedScan()
        for index in range(2):
            record = source.poll()[0][0]
            if index:
                record["session_id"] = "other-session"
            bridge.save_json(folder / f"point_{index+1:04d}.json", record)
        result = bridge.run_experiment("replay", session_dir=folder, cfg=self.cfg, analyze=False)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["recorded_points"], 1)

    def test_in_gui_style_worker_thread(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(bridge.run_experiment(cfg=self.cfg)))
        thread.start()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["state"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
