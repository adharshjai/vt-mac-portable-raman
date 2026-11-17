"""Tests of the Studio server (PROTOCOL.md section 7) against temporary HOME folders.

Everything here is made up or simulated. The scan folders are fake files in the controller's
formats, the "runner" is a stand-in script that only prints and waits, and the catalog is the
local demo. No controller, capture backend or motor program is started, and nothing connects
anywhere but to the test's own server on 127.0.0.1.
"""

import datetime
import http.client
import importlib.util
import io
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import support

from raman_catalog import catalog as catalog_module
from raman_catalog import cli, studio_server, sync
from raman_catalog.local_catalog import LocalDemoCatalog

CORE_COMMANDS = ["raman_catalog.commands_core", "raman_catalog.studio_server"]
SECRET = "TOP-SECRET-MARKER"
PAGE = b"<!doctype html><title>Studio page of the test</title>"

# Stands in for HOME/raman_sim_runner. It prints what it was started with and then waits to be
# ended; the sample id chooses how it behaves. It starts nothing and simulates nothing.
RUNNER = """\
import json, os, signal, subprocess, sys, time
arguments = sys.argv[1:]
print("ARGUMENTS " + json.dumps(arguments), flush=True)
print("SIMULATE backend=%s motor=%s" % (os.environ.get("RAMAN_BACKEND_SIMULATE"),
                                         os.environ.get("RAMAN_MOTOR_SIMULATE")), flush=True)
print("CWD " + os.getcwd(), flush=True)
sample = arguments[arguments.index("--sample-id") + 1]
if sample == "ends by itself":
    sys.exit(3)
if sample.startswith("ignores SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sample == "ignores SIGTERM and has a helper":
    # Stands in for a helper program the real runner starts: a child in its process group.
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    print("HELPER %d" % helper.pid, flush=True)
print("READY", flush=True)
while True:
    time.sleep(0.02)
"""
RUN = {"mode": "1d-x", "power": "HIGH", "sample_id": "S-001", "operator": "Test Operator", "location": "Bench 1"}


def install_runner(home):
    script = Path(home) / "runner_stand_in.py"
    script.write_text(RUNNER, encoding="utf-8")
    runner = Path(home) / "raman_sim_runner"
    runner.write_text('#!/bin/sh\nexec %s %s "$@"\n' % (shlex.quote(sys.executable), shlex.quote(str(script))),
                      encoding="utf-8")
    runner.chmod(0o755)
    return runner


def wait_for(condition, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("timed out waiting for %s" % what)


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def block_maximum(values):
    return [max(values[start:start + 8]) for start in range(0, len(values), 8)]


def set_times(path, moment):
    os.utime(path, (moment, moment))


class ServerCase(unittest.TestCase):
    """A Studio server on a free port, serving a temporary HOME, for the length of one test."""

    commands = CORE_COMMANDS
    grace = 0.3

    def setUp(self):
        self.home = support.make_home(self)
        # The classifier part publishes its model during a sync; these tests are about the server.
        self.patch(mock.patch.object(sync, "_optional_module", return_value=None))
        if self.commands is not None:
            self.patch(mock.patch.object(cli, "COMMAND_MODULES", list(self.commands)))
        self.prepare()
        self.server = studio_server.StudioServer(self.home, 0, stop_grace_seconds=self.grace)
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, thread)

    def prepare(self):
        """Hook: what a test class sets up before its server exists."""

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def stop_server(self, thread):
        self.server.shutdown()
        thread.join(10)
        self.server.close()

    def exchange(self, method, path, body=None, headers=None):
        """One request on a connection of its own: (status, headers, body bytes)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def get(self, path, headers=None):
        status, _headers, body = self.exchange("GET", path, headers=headers)
        return status, json.loads(body)

    def post(self, path, fields=None, headers=None):
        sent = {"Content-Type": "application/json", "Origin": self.origin}
        sent.update(headers or {})
        status, _headers, body = self.exchange("POST", path, json.dumps({} if fields is None else fields), sent)
        return status, json.loads(body)

    def raw(self, text):
        """Send the request text as it is; returns everything the server sent before closing."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            connection.sendall(text.encode("ascii"))
            received = b""
            while True:
                block = connection.recv(65536)
                if not block:
                    return received
                received += block


# ---------------------------------------------------------------------- the page, and nothing else from disk


class PageTest(ServerCase):
    def prepare(self):
        (self.home / "studio").mkdir()
        (self.home / "studio" / "index.html").write_bytes(PAGE)
        (self.home / "studio" / "private.html").write_text(SECRET)
        (self.home / "secret.txt").write_text(SECRET)
        (self.home / "catalog_config.json").write_text(json.dumps({"note": SECRET}))
        outside = self.home / "elsewhere"
        outside.mkdir()
        (outside / "session.json").write_text(json.dumps({"schema": "raman-session/1", "session_id": SECRET}))
        support.make_session(self.home, folder="_hidden_session", points=1)

    def test_the_page_of_home_is_served_at_the_root(self):
        status, headers, body = self.exchange("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, PAGE)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Python", headers["Server"])

    def test_no_request_path_reaches_another_file(self):
        paths = [
            "/index.html", "/private.html", "/studio/index.html", "/studio/private.html", "/secret.txt",
            "/../secret.txt", "/../../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/.%2e/secret.txt",
            "/./secret.txt", "//secret.txt", "/private.html?x=1", "/?file=../secret.txt", "/catalog_config.json",
            "/api/../secret.txt", "/api/scan/../../secret.txt", "/api/scan/", "/api", "/api/", "/API/PING",
            "/api/ping/../../secret.txt", "/%2fsecret.txt", "/\\..\\secret.txt", "/secret.txt%00.html",
            "http://127.0.0.1:%d/secret.txt" % self.port, "/api/sample/../../secret.txt",
        ]
        for path in paths:
            status, _headers, body = self.exchange("GET", path)
            self.assertNotIn(SECRET.encode(), body, path)
            if path == "/?file=../secret.txt":
                self.assertEqual((status, body), (200, PAGE), path)
            else:
                self.assertEqual(status, 404, path)
                self.assertFalse(json.loads(body)["ok"], path)

    def test_a_session_name_cannot_leave_the_scan_folder(self):
        refused = ["..", "../elsewhere", "%2E%2E%2Felsewhere", "..%2Felsewhere", "/etc", "%2Fetc", ".", "",
                   "_hidden_session", "a%00b", "%20padded", "..%5Celsewhere", str(self.home / "elsewhere")]
        for name, expected in [(name, 400) for name in refused] + [("elsewhere", 404), ("else%5Cwhere", 404)]:
            status, answer = self.get("/api/scan?session=" + name)
            self.assertEqual(status, expected, name)
            self.assertFalse(answer["ok"], name)
            self.assertNotIn(SECRET, json.dumps(answer), name)
        self.assertEqual(self.get("/api/scan")[1]["sessions"], [], "a folder starting with _ is not a session")

    def test_only_get_and_post_and_only_on_their_own_addresses(self):
        for method, path, allowed in [("GET", "/api/sync", "POST"), ("GET", "/api/run", "POST"),
                                      ("POST", "/api/scan", "GET"), ("POST", "/", "GET"),
                                      ("PUT", "/api/sync", "POST"), ("DELETE", "/api/scan", "GET"),
                                      ("OPTIONS", "/api/run", "POST"), ("PATCH", "/", "GET")]:
            body = "{}" if method in ("POST", "PUT", "PATCH") else None
            status, headers, answer = self.exchange(method, path, body, {"Content-Type": "application/json"})
            self.assertEqual((status, headers["Allow"]), (405, allowed), (method, path))
            self.assertFalse(json.loads(answer)["ok"])
        status, _headers, body = self.exchange("HEAD", "/")
        self.assertEqual((status, body), (405, b""))
        self.assertEqual(self.exchange("DELETE", "/secret.txt")[0], 404)


class SourceTreePageTest(ServerCase):
    def test_the_fallback_is_the_studio_folder_two_levels_above_the_package(self):
        self.assertEqual(studio_server.SOURCE_STUDIO, support.DATA_LAYER.parent / "studio")

    def test_without_a_page_in_home_the_page_of_the_source_tree_is_served(self):
        source = self.home / "source_tree_studio"
        source.mkdir()
        (source / "index.html").write_bytes(PAGE)
        with mock.patch.object(studio_server, "SOURCE_STUDIO", source):
            self.assertEqual(self.exchange("GET", "/")[2], PAGE)
            (self.home / "studio").mkdir()
            (self.home / "studio" / "index.html").write_bytes(b"the page of HOME")
            self.assertEqual(self.exchange("GET", "/")[2], b"the page of HOME")

    def test_without_any_page_the_answer_says_so(self):
        with mock.patch.object(studio_server, "SOURCE_STUDIO", self.home / "no_such_folder"):
            status, answer = self.get("/")
        self.assertEqual(status, 404)
        self.assertEqual(answer["error"], "The Studio page is not installed")


# ---------------------------------------------------------------------- who may ask


class HostAndOriginTest(ServerCase):
    def offline_flag(self):
        return (self.home / "data_catalog" / "local_demo" / "OFFLINE").exists()

    def test_requests_addressed_to_this_server_are_answered(self):
        for host in ["127.0.0.1:%d" % self.port, "localhost:%d" % self.port, "LOCALHOST:%d" % self.port]:
            status, answer = self.get("/api/ping", {"Host": host})
            self.assertEqual((status, answer["ok"]), (200, True), host)

    def test_any_other_host_is_refused(self):
        hosts = ["evil.example", "evil.example:%d" % self.port, "127.0.0.1", "localhost",
                 "127.0.0.1:%d" % (self.port + 1), "127.0.0.1.evil.example:%d" % self.port, "[::1]:%d" % self.port,
                 "0.0.0.0:%d" % self.port, "localhost.:%d" % self.port, "127.0.0.1:%d:1" % self.port, ""]
        for host in hosts:
            for path in ("/", "/api/ping", "/api/scan"):
                status, _headers, body = self.exchange("GET", path, headers={"Host": host})
                self.assertEqual(status, 403, (host, path))
                self.assertFalse(json.loads(body)["ok"])
            status, answer = self.post("/api/offline", {"offline": True}, {"Host": host, "Origin": "http://" + host})
            self.assertEqual(status, 403, host)
        self.assertFalse(self.offline_flag())

    def test_a_request_without_host_or_with_two_is_refused(self):
        self.assertIn(b" 403 ", self.raw("GET /api/ping HTTP/1.0\r\n\r\n").split(b"\r\n", 1)[0])
        twice = "GET /api/ping HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nHost: evil.example\r\nConnection: close\r\n\r\n"
        self.assertIn(b" 403 ", self.raw(twice % self.port).split(b"\r\n", 1)[0])

    def test_a_post_from_another_origin_is_refused_and_nothing_happens(self):
        origins = ["http://evil.example", "https://127.0.0.1:%d" % self.port, "http://127.0.0.1:%d" % (self.port + 1),
                   "http://localhost:%d" % self.port, "null", "http://127.0.0.1", ""]
        for origin in origins:
            status, answer = self.post("/api/offline", {"offline": True}, {"Origin": origin})
            self.assertEqual((status, answer["ok"]), (403, False), origin)
            self.assertFalse(self.offline_flag(), origin)
        status, _answer = self.post("/api/run", RUN, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_a_post_from_the_page_or_without_origin_is_accepted(self):
        status, answer = self.post("/api/offline", {"offline": True})
        self.assertEqual((status, answer), (200, {"ok": True, "offline": True}))
        self.assertTrue(self.offline_flag())
        body = json.dumps({"offline": False})
        status, _headers, answer = self.exchange("POST", "/api/offline", body, {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(answer)), (200, {"ok": True, "offline": False}))
        named = {"Host": "localhost:%d" % self.port, "Origin": "http://LOCALHOST:%d" % self.port}
        self.assertEqual(self.post("/api/offline", {"offline": True}, named)[0], 200)

    def test_reading_is_not_tied_to_an_origin(self):
        status, answer = self.get("/api/ping", {"Origin": "http://evil.example"})
        self.assertEqual((status, answer["ok"]), (200, True))

    def test_a_read_a_page_of_another_site_made_the_browser_send_is_refused(self):
        for site in ("cross-site", "same-site", "Cross-Site"):
            for path in ("/api/ping", "/api/pipeline", "/api/provenance?record=d/1", "/api/scan"):
                status, answer = self.get(path, {"Sec-Fetch-Site": site})
                self.assertEqual((status, answer["ok"]), (403, False), (site, path))
        # The Studio page itself, and an address the user typed, are answered; so is a program
        # that is not a browser. Following a link to the page from elsewhere only shows the page.
        for site in ("same-origin", "none", None):
            status, answer = self.get("/api/ping", {"Sec-Fetch-Site": site} if site else {})
            self.assertEqual((status, answer["ok"]), (200, True), site)
        status, _headers, _body = self.exchange("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertNotEqual(status, 403)

    def test_a_post_body_must_be_a_json_object_of_at_most_64_kb(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            headers = {"Origin": self.origin}
            if content_type:
                headers["Content-Type"] = content_type
            status, _headers, body = self.exchange("POST", "/api/offline", '{"offline": true}', headers)
            self.assertEqual(status, 415, content_type)
        for body in ("", "not json", "[1, 2]", '"text"', "null", '{"offline": NaN}', '{"offline": true'):
            headers = {"Content-Type": "application/json; charset=utf-8", "Origin": self.origin}
            status, _headers, answer = self.exchange("POST", "/api/offline", body, headers)
            self.assertEqual(status, 400, body)
            self.assertFalse(json.loads(answer)["ok"])
        self.assertFalse(self.offline_flag())
        padded = json.dumps({"offline": True, "padding": "x" * 65000})
        status, _headers, answer = self.exchange("POST", "/api/offline", padded,
                                                 {"Content-Type": "application/json", "Origin": self.origin})
        self.assertEqual(status, 200, "a body just under the limit is read")

    def test_a_body_that_is_too_large_or_has_no_length_is_refused_unread(self):
        head = "POST /api/offline HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n" % self.port
        for length_header, status in (("Content-Length: 65537\r\n", b" 413 "), ("", b" 411 "),
                                      ("Transfer-Encoding: chunked\r\n", b" 411 "),
                                      ("Content-Length: -1\r\n", b" 411 "), ("Content-Length: 1e3\r\n", b" 411 ")):
            answer = self.raw(head + length_header + "\r\n")
            self.assertIn(status, answer.split(b"\r\n", 1)[0], length_header)
            self.assertIn(b"Connection: close", answer)
        self.assertFalse(self.offline_flag())

    def test_a_fault_in_the_server_itself_is_logged_and_does_not_stop_it(self):
        with mock.patch.object(studio_server._Handler, "_send", side_effect=RuntimeError("a fault of the test")), \
                self.assertLogs("raman_catalog.studio_server", "ERROR") as logged:
            with self.assertRaises((http.client.HTTPException, OSError)):
                self.exchange("GET", "/api/ping")
            # The record is written by the thread that served the request, after the connection is gone.
            wait_for(lambda: logged.output, "the fault to be logged")
        self.assertIn("a fault of the test", logged.output[0])
        self.assertEqual(self.get("/api/ping")[0], 200)

    def test_a_refused_post_leaves_the_connection_usable(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/offline", '{"offline": true}',
                           {"Content-Type": "application/json", "Origin": "http://evil.example"})
        refused = connection.getresponse()
        refused.read()
        connection.request("GET", "/api/ping")
        answered = connection.getresponse()
        self.assertEqual((refused.status, answered.status), (403, 200))
        self.assertTrue(json.loads(answered.read())["ok"])


# ---------------------------------------------------------------------- GET /api/ping, /api/sample


class PingAndSampleTest(ServerCase):
    def test_ping_names_home_mode_and_whether_the_runner_is_there(self):
        status, answer = self.get("/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(answer, {"ok": True, "home": str(self.home), "mode": "local-demo",
                                  "simulated_stack_available": False})
        install_runner(self.home)
        self.assertTrue(self.get("/api/ping")[1]["simulated_stack_available"])
        (self.home / "raman_sim_runner").chmod(0o644)
        self.assertFalse(self.get("/api/ping")[1]["simulated_stack_available"])

    def test_ping_still_answers_with_unusable_settings(self):
        (self.home / "catalog_config.json").write_text("{ not json")
        status, answer = self.get("/api/ping")
        self.assertEqual((status, answer["ok"], answer["mode"]), (200, True, None))
        self.assertIn("catalog_config.json", answer["problem"])
        (self.home / "catalog_config.json").write_text(json.dumps({"sync": {"max_attempts": 0}}))
        answer = self.get("/api/ping")[1]
        self.assertEqual((answer["ok"], answer["mode"]), (True, "local-demo"))
        self.assertIn("sync.max_attempts", answer["problem"])

    def test_sample_is_sample_json_plus_the_reduced_reference_spectra(self):
        folder = self.home / "virtual_sample"
        folder.mkdir()
        sample = {"schema": "raman-virtual-sample/1", "materials": {"r6g": {"label": "Rhodamine 6G"}},
                  "regions": [], "focus": {"z_focus_mm": 4.0, "half_width_mm": 0.1}}
        dark = [800.0 + index % 5 for index in range(2048)]
        signal_curve = [float((index * 37) % 1000) for index in range(2048)]
        (folder / "sample.json").write_text(json.dumps(sample))
        (folder / "reference_spectra.json").write_text(json.dumps(
            {"schema": "raman-reference-spectra/1", "source": "made up for this test", "pixel_count": 2048,
             "dark": dark, "signals": {"r6g": signal_curve}}))
        status, answer = self.get("/api/sample")
        self.assertEqual(status, 200)
        reference = answer.pop("reference_spectra")
        self.assertEqual(answer, dict(sample, ok=True))
        self.assertEqual(reference["dark"], block_maximum(dark))
        self.assertEqual(reference["signals"], {"r6g": block_maximum(signal_curve)})
        self.assertEqual((len(reference["dark"]), reference["reduced_to"], reference["pixel_count"]), (256, 256, 2048))
        self.assertEqual(reference["source"], "made up for this test")
        self.assertIn("block maximum of 8", reference["reduction"])

    def test_sample_without_usable_reference_spectra_or_without_a_sample(self):
        status, answer = self.get("/api/sample")
        self.assertEqual((status, answer["ok"]), (404, False))
        folder = self.home / "virtual_sample"
        folder.mkdir()
        (folder / "sample.json").write_text(json.dumps({"materials": {}}))
        (folder / "reference_spectra.json").write_text(json.dumps({"dark": [1, 2, "x"], "signals": {}}))
        status, answer = self.get("/api/sample")
        self.assertEqual((status, answer), (200, {"ok": True, "materials": {}, "reference_spectra": None}))

    def test_the_real_virtual_sample_of_this_edition_can_be_served(self):
        source = support.DATA_LAYER.parent / "virtual_sample"
        if not (source / "reference_spectra.json").is_file():
            self.skipTest("this checkout has no virtual_sample/reference_spectra.json")
        (self.home / "virtual_sample").symlink_to(source)
        status, answer = self.get("/api/sample")
        self.assertEqual(status, 200)
        full = json.loads((source / "reference_spectra.json").read_text(encoding="utf-8"))
        self.assertEqual(answer["reference_spectra"]["dark"], block_maximum(full["dark"]))
        self.assertEqual(sorted(answer["reference_spectra"]["signals"]), sorted(answer["materials"]))


class ReduceSpectrumTest(unittest.TestCase):
    def test_block_maximum_of_eight_pixels(self):
        values = [(index * 7919) % 6000 for index in range(2048)]
        reduced = studio_server.reduce_spectrum(values)
        self.assertEqual(len(reduced), 256)
        self.assertEqual(reduced, block_maximum(values))
        self.assertEqual(studio_server.reduce_spectrum([1, 9, 2] + [0] * 8), [9, 0])
        self.assertEqual(studio_server.reduce_spectrum([1.5, 2]), [2])

    def test_anything_but_a_list_of_numbers_gives_none(self):
        for values in (None, [], "12345678", [1, None], [1, "2"], [1, True], [float("nan")], [float("inf")], {"a": 1}):
            self.assertIsNone(studio_server.reduce_spectrum(values), values)


# ---------------------------------------------------------------------- GET /api/scan


OLDER, NEWER, LEGACY = "1d_x_scan_20261113_101500_123", "2d_scan_20261113_110000_000", "1d_y_scan_20261111_132829_780"


def moment(hour, minute=0):
    """Seconds since 1970 for a time of day on 13 November 2026, UTC."""
    return datetime.datetime(2026, 11, 13, hour, minute, tzinfo=datetime.timezone.utc).timestamp()


class ScanTest(ServerCase):
    def session(self, folder, started_utc, points=3, **options):
        made = support.make_session(self.home, folder=folder, points=points, **options)
        path = made["folder"] / "session.json"
        if path.exists():
            described = json.loads(path.read_text())
            described["started_utc"] = started_utc
            path.write_text(json.dumps(described))
        return made

    def scan(self, query=""):
        status, answer = self.get("/api/scan" + query)
        self.assertEqual(status, 200, answer)
        return answer

    def test_an_empty_home(self):
        self.assertEqual(self.scan(), {
            "ok": True, "folder": None, "session": None, "active": False, "stage": None, "request": None,
            "ack": None, "points": [], "sessions": [], "run": None, "log_tail": []})
        (self.home / "scan_sessions").mkdir()
        self.assertEqual(self.scan()["sessions"], [])

    def test_the_newest_session_with_its_points(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z")
        answer = self.scan()
        self.assertEqual(answer["folder"], OLDER)
        self.assertEqual(answer["session"], json.loads((made["folder"] / "session.json").read_text()))
        self.assertEqual(answer["session"]["sample_id"], "S-001")
        self.assertEqual([point["point_id"] for point in answer["points"]], [1, 2, 3])
        for point, path in zip(answer["points"], made["files"]):
            saved = json.loads(path.read_text())
            self.assertEqual(sorted(point), ["capture_id", "completed_utc", "point_id", "spectrum", "x_mm", "y_mm",
                                             "z_mm"])
            self.assertEqual(point["capture_id"], saved["capture_id"])
            self.assertEqual(point["completed_utc"], saved["completed_utc"])
            self.assertEqual(len(point["spectrum"]), 256)
            self.assertEqual(point["spectrum"], block_maximum(saved["intensity"]))
            self.assertAlmostEqual(point["x_mm"], saved["controller_x_steps"] / 34700)
            self.assertAlmostEqual(point["y_mm"], 2.5)
            self.assertAlmostEqual(point["z_mm"], 4.0)
        self.assertEqual([point["x_mm"] for point in answer["points"]], [0.0, 2.0, 4.0])

    def test_newest_is_decided_by_started_utc_then_by_the_folders_time(self):
        older = self.session(OLDER, "2026-11-13T15:15:00.123Z")
        newer = self.session(NEWER, "2026-11-13T16:00:00.000Z", scan_mode="2d")
        legacy = self.session(LEGACY, None, points=1, with_session_json=False, with_capture=False)
        # The folder times say the opposite of the start times, and are ignored where a start time is known.
        set_times(older["folder"], moment(18))
        set_times(newer["folder"], moment(17))
        set_times(legacy["folder"], moment(15, 30))
        answer = self.scan()
        self.assertEqual(answer["folder"], NEWER)
        self.assertEqual([entry["folder"] for entry in answer["sessions"]], [NEWER, LEGACY, OLDER])
        self.assertEqual(answer["sessions"][0], {
            "folder": NEWER, "session_id": newer["session_id"], "scan_mode": "2d",
            "started_utc": "2026-11-13T16:00:00.000Z", "sample_id": "S-001",
            "simulated": {"backend": True, "motor": True}})
        self.assertEqual(answer["sessions"][1], {
            "folder": LEGACY, "session_id": None, "scan_mode": "1d-y", "started_utc": None, "sample_id": None,
            "simulated": None})

        set_times(legacy["folder"], moment(16, 30))
        answer = self.scan()
        self.assertEqual((answer["folder"], answer["session"]), (LEGACY, None))
        self.assertEqual(len(answer["points"]), 1)
        self.assertEqual([entry["folder"] for entry in answer["sessions"]], [LEGACY, NEWER, OLDER])

    def test_another_session_can_be_selected(self):
        older = self.session(OLDER, "2026-11-13T15:15:00.123Z", points=2)
        self.session(NEWER, "2026-11-13T16:00:00.000Z", points=1, scan_mode="2d")
        answer = self.scan("?session=" + OLDER)
        self.assertEqual((answer["folder"], answer["session"]["session_id"]), (OLDER, older["session_id"]))
        self.assertEqual(len(answer["points"]), 2)
        self.assertEqual(self.scan()["folder"], NEWER)
        status, answer = self.get("/api/scan?session=no_such_session")
        self.assertEqual((status, answer["ok"]), (404, False))
        self.assertEqual(self.get("/api/scan?session=%s&session=%s" % (OLDER, NEWER))[0], 400)

    def test_each_listed_session_says_which_helper_program_simulated(self):
        self.session(OLDER, "2026-11-13T15:15:00.123Z", points=1, simulated=False)
        path = self.session(NEWER, "2026-11-13T16:00:00.000Z", points=1)["folder"] / "session.json"
        described = json.loads(path.read_text())

        def listed_with(flags, minute):
            described["simulated"] = flags
            path.write_text(json.dumps(described))
            # Another time for every version: the server reads a file again only when it changed.
            set_times(path, moment(12, minute))
            return {entry["folder"]: entry["simulated"] for entry in self.scan()["sessions"]}

        self.assertEqual(listed_with({"backend": False, "motor": True}, 0),
                         {NEWER: {"backend": False, "motor": True}, OLDER: {"backend": False, "motor": False}})
        # Only both flags, each true or false, describe a session; anything else is not known.
        unknown = ({"backend": True}, {"backend": "true", "motor": True}, {"backend": 1, "motor": 1}, True, None)
        for minute, flags in enumerate(unknown, 1):
            self.assertIsNone(listed_with(flags, minute)[NEWER], flags)

    def test_only_a_short_list_of_recent_sessions(self):
        for number in range(15):
            self.session("1d_x_scan_20261113_1015%02d_000" % number, "2026-11-13T15:15:%02d.000Z" % number, points=0)
        self.session("_backend_working_folder", "2026-11-13T23:59:59.000Z", points=1)
        (self.home / "scan_sessions" / "a_file.txt").write_text("not a session")
        answer = self.scan()
        listed = [entry["folder"] for entry in answer["sessions"]]
        self.assertEqual(listed, ["1d_x_scan_20261113_1015%02d_000" % number for number in range(14, 2, -1)])
        self.assertEqual(answer["folder"], listed[0])

    def test_the_last_step_and_its_ack(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z")
        folder, session_id = made["folder"], made["session_id"]
        self.assertEqual((self.scan()["request"], self.scan()["ack"]), (None, None))
        (folder / "request.txt").write_text("STEP %s 4 208200 86750 138800\n" % session_id)
        (folder / "response.txt").write_text("ACK %s 3 OK 1794421734830\n" % session_id)
        answer = self.scan()
        self.assertEqual(answer["request"]["point_id"], 4)
        self.assertAlmostEqual(answer["request"]["x_mm"], 6.0)
        self.assertAlmostEqual(answer["request"]["y_mm"], 2.5)
        self.assertAlmostEqual(answer["request"]["z_mm"], 4.0)
        self.assertIsNone(answer["ack"], "the ACK on disk still answers point 3")
        for state in ("HOLD", "OK", "PAUSED", "FAIL"):
            (folder / "response.txt").write_text("ACK %s 4 %s 1794421734999\n" % (session_id, state))
            self.assertEqual(self.scan()["ack"], state)

    def test_handshake_lines_that_cannot_be_used_are_left_out(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z")
        folder, session_id = made["folder"], made["session_id"]
        (folder / "response.txt").write_text("ACK %s 4 OK 1\n" % session_id)
        for line in ("", "STEP", "STEP %s 4 1 2" % session_id, "STEP another-session 4 1 2 3",
                     "STOP %s 4 1 2 3" % session_id, "STEP %s four 1 2 3" % session_id,
                     "STEP %s 4 1.5 2 3" % session_id, "STEP %s 4 1 2 %s" % (session_id, "9" * 400), "\xff\xfe"):
            (folder / "request.txt").write_text(line, encoding="latin-1")
            answer = self.scan()
            self.assertEqual((answer["request"], answer["ack"]), (None, None), line)
        (folder / "request.txt").write_text("STEP %s 4 -347 0 34700 trailing words\n" % session_id)
        self.assertEqual(self.scan()["request"], {"point_id": 4, "x_mm": -0.01, "y_mm": 0.0, "z_mm": 1.0})
        for line in ("ACK %s 4 DONE 1" % session_id, "ACK another-session 4 OK 1", "ACK %s 4" % session_id, "NAK"):
            (folder / "response.txt").write_text(line)
            self.assertIsNone(self.scan()["ack"], line)

    def test_a_scan_is_active_while_its_handshake_files_change(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z")
        folder, session_id = made["folder"], made["session_id"]
        # A slow test machine must not turn "written just now" into "a while ago".
        patient = mock.patch.object(studio_server, "ACTIVE_SECONDS", 600.0)
        self.assertFalse(self.scan()["active"])
        (folder / "request.txt").write_text("STEP %s 4 0 0 0\n" % session_id)
        with patient:
            self.assertTrue(self.scan()["active"])
        set_times(folder / "request.txt", time.time() - 60)
        self.assertFalse(self.scan()["active"], "a minute without a STEP or an ACK is not a scan in progress")
        (folder / "response.txt").write_text("ACK %s 4 PAUSED 1\n" % session_id)
        with patient:
            self.assertTrue(self.scan()["active"], "a paused scan keeps sending heartbeats")
            (folder / "cancel.txt").write_text("STOP\n")
            self.assertFalse(self.scan()["active"], "a scan that was told to stop takes no further point")
        (folder / "cancel.txt").unlink()
        set_times(folder / "response.txt", time.time() + 3600)
        self.assertFalse(self.scan()["active"], "a time stamp far ahead of the clock is not activity")

    def test_only_the_newest_session_can_be_active(self):
        older = self.session(OLDER, "2026-11-13T15:15:00.123Z")
        self.session(NEWER, "2026-11-13T16:00:00.000Z", scan_mode="2d")
        (older["folder"] / "request.txt").write_text("STEP %s 4 0 0 0\n" % older["session_id"])
        self.assertFalse(self.scan()["active"])
        self.assertFalse(self.scan("?session=" + OLDER)["active"])

    def test_the_simulated_stage(self):
        self.session(OLDER, "2026-11-13T15:15:00.123Z", points=0)
        path = self.home / "scan_sessions" / "_sim_stage.json"
        stage = {"x_steps": 86750, "y_steps": 86750, "z_steps": 138800, "steps_per_mm": 34700, "moving": True,
                 "updated_utc": "2026-11-13T15:15:01.250Z"}
        path.write_text(json.dumps(stage))
        # A slow test machine must not turn "written just now" into "a while ago".
        with mock.patch.object(studio_server, "MOVING_SECONDS", 600.0):
            self.assertEqual(self.scan()["stage"], {
                "x_mm": 2.5, "y_mm": 2.5, "z_mm": 4.0, "moving": True, "updated_utc": "2026-11-13T15:15:01.250Z",
                "simulated": True})
        set_times(path, time.time() - 30)
        self.assertFalse(self.scan()["stage"]["moving"], "a move that nobody reports any more is over")
        path.write_text(json.dumps(dict(stage, moving=False, steps_per_mm=1000, x_steps=1500)))
        self.assertEqual(self.scan()["stage"]["x_mm"], 1.5)
        self.assertFalse(self.scan()["stage"]["moving"])
        for unusable in ("", "{", "[1, 2, 3]", json.dumps({"x_steps": 1, "y_steps": 2}),
                         json.dumps(dict(stage, z_steps="high")), '{"x_steps": NaN, "y_steps": 1, "z_steps": 1}'):
            path.write_text(unusable)
            self.assertIsNone(self.scan()["stage"], unusable)
        path.write_text(json.dumps(dict(stage, steps_per_mm=0)))
        self.assertEqual(self.scan()["stage"]["x_mm"], 2.5)

    def test_a_single_capture_has_no_position(self):
        made = self.session("single_20261113_101700_000", "2026-11-13T15:17:00.000Z", points=1, scan_mode="single",
                            steps=None)
        answer = self.scan()
        self.assertEqual(answer["session"]["scan_mode"], "single")
        point = answer["points"][0]
        self.assertEqual((point["x_mm"], point["y_mm"], point["z_mm"]), (None, None, None))
        self.assertEqual(point["capture_id"], made["capture_ids"][0])
        self.assertEqual(len(point["spectrum"]), 256)

    def test_files_that_did_not_change_are_not_read_again(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z", points=3)
        first = self.scan()
        with mock.patch.object(studio_server, "_read", wraps=studio_server._read) as read:
            self.assertEqual(self.scan(), first)
            self.assertEqual(sorted(call.args[0].name for call in read.call_args_list),
                             ["_sim_stage.json", "request.txt", "response.txt"])
            read.reset_mock()
            support.add_points(made, 1, capture=support.capture_block())
            self.assertEqual(len(self.scan()["points"]), 4)
            self.assertEqual([call.args[0].name for call in read.call_args_list].count("point_0004.json"), 1)
            self.assertNotIn("point_0001.json", [call.args[0].name for call in read.call_args_list])

    def test_a_point_file_is_read_again_when_it_changes(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z", points=2)
        before = self.scan()["points"]
        saved = json.loads(made["files"][1].read_text())
        saved["intensity"] = [7] * 2048
        saved["controller_x_steps"] = 34700
        made["files"][1].write_text(json.dumps(saved))
        after = self.scan()["points"]
        self.assertEqual(after[0], before[0])
        self.assertEqual((after[1]["spectrum"], after[1]["x_mm"]), ([7] * 256, 1.0))
        made["files"][0].unlink()
        self.assertEqual([point["point_id"] for point in self.scan()["points"]], [2])

    def test_files_that_cannot_be_read_yet_are_left_out_until_they_can(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z", points=2)
        folder = made["folder"]
        whole = json.loads(made["files"][0].read_text())
        whole.update(point_id=3, capture_id=made["session_id"] + "_3")
        text = json.dumps(whole)
        (folder / "point_0003.json").write_text(text[: len(text) // 2])
        (folder / "point_0004.json").write_text(json.dumps(dict(whole, point_id=4, intensity="none")))
        (folder / "point_0005.json").write_text(json.dumps(["not", "a", "point"]))
        (folder / "point_0006.json").write_text(json.dumps(dict(whole, point_id="six")))
        (folder / "point_0007.json").mkdir()
        (folder / "point_12.json").write_text(text)
        points = self.scan()["points"]
        self.assertEqual([point["point_id"] for point in points], [1, 2, 4])
        self.assertIsNone(points[2]["spectrum"])
        (folder / "point_0003.json").write_text(text)
        self.assertEqual([point["point_id"] for point in self.scan()["points"]], [1, 2, 3, 4])
        (folder / "session.json").write_text("{ half a file")
        answer = self.scan()
        self.assertEqual((answer["folder"], answer["session"]), (OLDER, None))
        self.assertEqual(len(answer["points"]), 4)

    def test_answers_stay_whole_while_the_files_are_being_replaced(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z", points=2)
        folder, session_id = made["folder"], made["session_id"]
        stage = self.home / "scan_sessions" / "_sim_stage.json"
        point = json.loads(made["files"][0].read_text())
        done = threading.Event()

        def rewrite():
            number = 0
            while not done.is_set():
                number += 1
                text = json.dumps(dict(point, point_id=3, capture_id=session_id + "_3", intensity=[number] * 2048))
                # Alternately in one step, as the controller does, and in two, as a careless writer would.
                if number % 2:
                    (folder / "point_0003.tmp").write_text(text)
                    os.replace(folder / "point_0003.tmp", folder / "point_0003.json")
                else:
                    with open(folder / "point_0003.json", "w") as stream:
                        stream.write(text[:5000])
                        stream.flush()
                        stream.write(text[5000:])
                (folder / "request.txt").write_text("STEP %s %d 0 0 0\n" % (session_id, number))
                (folder / "response.txt").write_text("ACK %s %d HOLD %d\n" % (session_id, number, number))
                stage.write_text(json.dumps({"x_steps": number, "y_steps": 0, "z_steps": 0, "moving": True}))

        writer = threading.Thread(target=rewrite)
        writer.start()
        try:
            for _ in range(60):
                answer = self.scan()
                self.assertEqual(answer["folder"], OLDER)
                self.assertIn(len(answer["points"]), (2, 3))
                for shown in answer["points"]:
                    self.assertEqual(len(shown["spectrum"]), 256)
        finally:
            done.set()
            writer.join()
        self.assertEqual(len(self.scan()["points"]), 3)

    def test_numbers_that_json_cannot_carry_make_a_file_unusable_not_the_answer(self):
        made = self.session(OLDER, "2026-11-13T15:15:00.123Z", points=2)
        described = (made["folder"] / "session.json").read_text()
        for number in ("1e999", "-1e999", "NaN", "Infinity"):
            (made["folder"] / "session.json").write_text(described.replace('"S-001"', number, 1))
            saved = made["files"][1].read_text()
            made["files"][1].write_text(saved.replace("1100", number, 1))
            answer = self.scan()
            self.assertEqual((answer["folder"], answer["session"]), (OLDER, None), number)
            self.assertEqual([point["point_id"] for point in answer["points"]], [1], number)
            made["files"][1].write_text(saved)


# ---------------------------------------------------------------------- the pipeline on the local demo catalog


class PipelineTest(ServerCase):
    def prepare(self):
        support.write_dark(self.home)
        self.made = support.make_session(self.home, points=3)

    def sync_until_uploaded(self):
        for _ in range(5):
            status, answer = self.post("/api/sync")
            self.assertEqual((status, answer["ok"]), (200, True), answer)
            if answer["status"]["counts"]["uploaded"] == 3:
                return answer
        self.fail("the captures were not uploaded after five syncs: %r" % (answer["status"]["counts"],))

    def catalog(self):
        return LocalDemoCatalog(self.home / "data_catalog" / "local_demo")

    def test_pipeline_before_and_after_sync(self):
        status, answer = self.get("/api/pipeline")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(answer), ["as_of_utc", "items", "ok", "review", "review_problem", "status", "total"])
        self.assertEqual((answer["items"], answer["total"], answer["review"]), ([], 0, []))
        self.assertEqual(answer["review_problem"], "not available")
        self.assertEqual((answer["status"]["mode"], answer["status"]["demo"], answer["status"]["online"]),
                         ("local-demo", True, True))
        self.assertIn("nothing is sent anywhere", answer["status"]["mode_label"])
        self.assertNotIn("ok", answer["status"])

        status, first = self.post("/api/sync")
        self.assertEqual((status, first["ingested"], first["registered"], first["uploads_started"]), (200, 3, 3, 3))
        self.sync_until_uploaded()
        answer = self.get("/api/pipeline")[1]
        self.assertEqual(answer["total"], 3)
        self.assertEqual(answer["status"]["counts"]["uploaded"], 3)
        self.assertEqual(sorted(item["capture_id"] for item in answer["items"]), sorted(self.made["capture_ids"]))
        self.assertEqual({item["state"] for item in answer["items"]}, {"uploaded"})
        self.assertTrue(all(item["simulated"] and item["record_alias"].startswith("rs-") for item in answer["items"]))

    def test_pipeline_limit_and_session(self):
        other = support.make_session(self.home, folder="2d_scan_20261113_110000_000", points=2, scan_mode="2d")
        self.post("/api/sync")
        answer = self.get("/api/pipeline?limit=2")[1]
        self.assertEqual((len(answer["items"]), answer["total"]), (2, 5))
        answer = self.get("/api/pipeline?session=" + other["session_id"])[1]
        self.assertEqual(sorted(item["capture_id"] for item in answer["items"]), sorted(other["capture_ids"]))
        self.assertEqual(self.get("/api/pipeline?session=2d_scan_20261113_110000_000")[1]["total"], 2)
        for limit in ("0", "-1", "abc", "5001", "1.5", ""):
            self.assertEqual(self.get("/api/pipeline?limit=" + limit)[0], 400, limit)

    def test_provenance_of_a_record(self):
        self.sync_until_uploaded()
        catalog = self.catalog()
        raw = catalog.get_record(catalog_module.raw_alias(self.made["capture_ids"][0]))
        prediction = support.save_prediction(catalog, "raman-portable", raw)
        status, answer = self.get("/api/provenance?record=" + prediction.id.replace("/", "%2F"))
        self.assertEqual((status, answer["ok"], answer["truncated"]), (200, True, False))
        roles = {node["id"]: node["role"] for node in answer["nodes"]}
        self.assertEqual((roles[prediction.id], roles[raw.id]), ("start", "source"))
        self.assertIn({"from": prediction.id, "to": raw.id, "relation": "der", "label": "is derived from"},
                      answer["edges"])
        by_alias = self.get("/api/provenance?record=" + raw.alias)[1]
        self.assertEqual(by_alias["nodes"][0]["id"], raw.id)

        status, answer = self.get("/api/provenance?record=no-such-record")
        self.assertEqual((status, answer["ok"], answer["kind"]), (404, False, "not_found"))
        for query in ("", "?record=", "?record=%0A", "?other=1", "?record=" + "r" * 201):
            self.assertEqual(self.get("/api/provenance" + query)[0], 400, query)

    def test_offline_switch_of_the_local_demo(self):
        flag = self.home / "data_catalog" / "local_demo" / "OFFLINE"
        self.assertEqual(self.post("/api/offline", {"offline": True}), (200, {"ok": True, "offline": True}))
        self.assertTrue(flag.exists())
        status, answer = self.post("/api/sync")
        self.assertEqual((status, answer["ok"], answer["online"], answer["ingested"], answer["registered"]),
                         (200, True, False, 3, 0))
        pipeline = self.get("/api/pipeline")[1]
        self.assertEqual((pipeline["status"]["online"], pipeline["status"]["pending"]), (False, 3))
        self.assertEqual({item["state"] for item in pipeline["items"]}, {"captured"})
        status, answer = self.get("/api/provenance?record=anything")
        self.assertEqual((status, answer["kind"]), (503, "offline"))

        self.assertEqual(self.post("/api/offline", {"offline": False}), (200, {"ok": True, "offline": False}))
        self.assertFalse(flag.exists())
        self.assertEqual(self.post("/api/sync")[1]["registered"], 3)
        for fields in ({}, {"offline": "yes"}, {"offline": 1}, {"offline": None}):
            self.assertEqual(self.post("/api/offline", fields)[0], 400, fields)

    def test_unusable_settings_fail_the_commands_but_not_the_scan_view(self):
        (self.home / "catalog_config.json").write_text(json.dumps({"sync": {"max_attempts": 0}}))
        status, answer = self.post("/api/offline", {"offline": True})
        self.assertEqual((status, answer["ok"], answer["kind"]), (400, False, "config"))
        status, answer = self.get("/api/pipeline")
        self.assertEqual((status, answer["ok"], answer["kind"]), (400, False, "config"))
        self.assertEqual(self.get("/api/scan")[0], 200, "the scan view needs no settings")

    def test_sync_options(self):
        with mock.patch.object(sync, "run_sync", wraps=sync.run_sync) as run_sync:
            status, answer = self.post("/api/sync", {"retry_failed": True, "unknown": "ignored"})
            self.assertEqual((status, answer["registered"]), (200, 3))
            self.assertEqual(self.post("/api/sync", {"retry_failed": False})[0], 200)
            self.assertEqual(self.post("/api/sync")[0], 200)
            self.assertEqual(self.post("/api/sync", {"retry_failed": "yes"})[0], 400)
        self.assertEqual([call.kwargs["retry_failed"] for call in run_sync.call_args_list], [True, False, False])

    def test_commands_of_parts_that_are_not_installed_answer_not_available(self):
        for path, fields in (("/api/classify", {}),
                             ("/api/review", {"prediction": "d/1", "decision": "confirm", "reviewer": "A Reviewer"})):
            status, answer = self.post(path, fields)
            self.assertEqual(status, 501, path)
            self.assertEqual((answer["ok"], answer["error"]), (False, "not available"), path)

    def test_a_command_module_that_is_broken_is_reported_as_broken(self):
        with mock.patch.object(cli, "COMMAND_MODULES", CORE_COMMANDS + ["studio_test_broken"]), \
                mock.patch.dict(sys.modules, {"studio_test_broken": types.ModuleType("studio_test_broken")}), \
                self.assertLogs("raman_catalog", "ERROR"):
            status, answer = self.post("/api/classify")
        self.assertEqual((status, answer["ok"], answer["kind"]), (500, False, "internal"))
        self.assertNotEqual(answer["error"], "not available")


GLOBAL_ARGUMENTS = ("func", "command", "home", "verbose")


def fake_commands(calls, results, gate=None):
    """A command module with the classifier and review commands of PROTOCOL.md section 6. The
    commands only record how they were called and return what the test put in `results`."""

    def command(name):
        def run(args, ctx):
            given = {key: value for key, value in vars(args).items() if key not in GLOBAL_ARGUMENTS}
            calls.append((name, given))
            if gate is not None and name in gate:
                gate[name]()
            return dict(results[name])
        return run

    def register(subparsers):
        classify = subparsers.add_parser("classify")
        classify.add_argument("--limit", type=int)
        classify.set_defaults(func=command("classify"))
        subparsers.add_parser("review-list").set_defaults(func=command("review-list"))
        review = subparsers.add_parser("review-set")
        review.add_argument("--prediction", required=True)
        review.add_argument("--decision", required=True, choices=("confirm", "relabel", "reject"))
        review.add_argument("--label")
        review.add_argument("--reviewer", required=True)
        review.add_argument("--comment")
        review.set_defaults(func=command("review-set"))

    module = types.ModuleType("studio_test_commands")
    module.register = register
    return module


class OptionalCommandsTest(ServerCase):
    commands = ["raman_catalog.commands_core", "studio_test_commands", "raman_catalog.studio_server"]

    def prepare(self):
        self.calls = []
        self.results = {
            "classify": {"runner": "local", "processed": 2, "predictions": 2, "skipped": 0, "errors": []},
            "review-list": {"items": [{"capture_id": "c-1", "prediction_id": "d/9", "threshold": 0.8}]},
            "review-set": {"item": {"capture_id": "c-1", "state": "reviewed"}, "queued": False},
        }
        self.gate = {}
        module = fake_commands(self.calls, self.results, self.gate)
        self.patch(mock.patch.dict(sys.modules, {"studio_test_commands": module}))

    def test_classify_maps_onto_the_command(self):
        status, answer = self.post("/api/classify")
        self.assertEqual((status, answer), (200, dict(self.results["classify"], ok=True)))
        self.assertEqual(self.post("/api/classify", {"limit": 7})[0], 200)
        self.assertEqual(self.calls, [("classify", {"limit": None}), ("classify", {"limit": 7})])
        for limit in (0, -3, "many", 2.5, True, 10 ** 9):
            self.assertEqual(self.post("/api/classify", {"limit": limit})[0], 400, limit)
        self.assertEqual(len(self.calls), 2)

    def test_review_maps_onto_review_set(self):
        fields = {"prediction": "d/9", "decision": "relabel", "label": "adenine", "reviewer": "  A Reviewer ",
                  "comment": "--label=r6g looks wrong\nsecond line"}
        status, answer = self.post("/api/review", fields)
        self.assertEqual((status, answer), (200, dict(self.results["review-set"], ok=True)))
        self.assertEqual(self.calls, [("review-set", {
            "prediction": "d/9", "decision": "relabel", "label": "adenine", "reviewer": "A Reviewer",
            "comment": "--label=r6g looks wrong\nsecond line"})])

    def test_review_fields_as_the_page_sends_them(self):
        fields = {"prediction": "-d/9", "decision": "confirm", "label": None, "reviewer": "--reviewer", "comment": ""}
        self.assertEqual(self.post("/api/review", fields)[0], 200)
        self.assertEqual(self.calls, [("review-set", {
            "prediction": "-d/9", "decision": "confirm", "label": None, "reviewer": "--reviewer", "comment": None})])

    def test_review_fields_that_cannot_be_used(self):
        good = {"prediction": "d/9", "decision": "reject", "reviewer": "A Reviewer"}
        for change in ({"decision": "approve"}, {"decision": None}, {"decision": ["confirm"]}, {"prediction": ""},
                       {"prediction": 9}, {"reviewer": "   "}, {"reviewer": None}, {"reviewer": "x" * 201},
                       {"label": 5}, {"label": "a\nb"}, {"comment": "c" * 2001}, {"comment": "bell \x07"},
                       {"reviewer": "two\nlines"}):
            status, answer = self.post("/api/review", dict(good, **change))
            self.assertEqual((status, answer["ok"]), (400, False), change)
        self.assertEqual(self.calls, [])

    def test_a_failing_command_answers_with_its_failure_object(self):
        def refuse():
            raise catalog_module.CatalogConflict("The prediction was reviewed already", detail="by someone else")
        self.gate["review-set"] = refuse
        status, answer = self.post("/api/review", {"prediction": "d/9", "decision": "confirm", "reviewer": "A"})
        self.assertEqual(status, 409)
        self.assertEqual(answer, {"ok": False, "error": "The prediction was reviewed already",
                                  "detail": "by someone else", "kind": "conflict"})

    def test_an_answer_that_json_cannot_carry_becomes_a_failure(self):
        self.results["classify"] = {"processed": float("nan")}
        status, answer = self.post("/api/classify")
        self.assertEqual((status, answer["ok"], answer["kind"]), (500, False, "internal"))

    def test_pipeline_includes_the_review_list(self):
        answer = self.get("/api/pipeline")[1]
        self.assertEqual(answer["review"], self.results["review-list"]["items"])
        self.assertNotIn("review_problem", answer)

    def test_pipeline_still_answers_when_the_review_list_fails(self):
        def unreachable():
            raise catalog_module.CatalogOffline("The catalog cannot be reached")
        self.gate["review-list"] = unreachable
        status, answer = self.get("/api/pipeline")
        self.assertEqual((status, answer["ok"], answer["review"]), (200, True, []))
        self.assertEqual(answer["review_problem"], "The catalog cannot be reached")
        self.assertIn("counts", answer["status"])

    def test_commands_that_change_something_run_one_at_a_time_and_do_not_hold_up_reading(self):
        inside, most, entered, release = [0], [0], threading.Event(), threading.Event()
        guard = threading.Lock()
        # A failing assertion below must not leave the commands waiting.
        self.addCleanup(release.set)

        def slow():
            with guard:
                inside[0] += 1
                most[0] = max(most[0], inside[0])
            entered.set()
            self.assertTrue(release.wait(30))
            with guard:
                inside[0] -= 1

        self.gate["classify"] = self.gate["review-set"] = slow
        answers = []
        requests = [("/api/classify", {}),
                    ("/api/review", {"prediction": "d/9", "decision": "confirm", "reviewer": "A"}),
                    ("/api/classify", {"limit": 1})]
        threads = [threading.Thread(target=lambda path=path, fields=fields: answers.append(self.post(path, fields)))
                   for path, fields in requests]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(30))
        # One command is in the middle of its work and two wait for it; reading goes on meanwhile.
        self.assertEqual(self.get("/api/pipeline")[0], 200)
        self.assertEqual(self.get("/api/scan")[0], 200)
        self.assertEqual(self.get("/api/ping")[0], 200)
        self.assertEqual(self.get("/api/provenance?record=none")[0], 404)
        time.sleep(0.05)
        self.assertEqual(len([call for call in self.calls if call[0] != "review-list"]), 1)
        release.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(status for status, _answer in answers), [200, 200, 200])
        self.assertEqual(most[0], 1)


@unittest.skipUnless(importlib.util.find_spec("raman_catalog.review"), "the review commands are not installed")
class InstalledReviewCommandsTest(ServerCase):
    """The review endpoints with the commands of this installation, on the local demo catalog."""

    commands = None

    def prepare(self):
        support.write_dark(self.home)
        self.made = support.make_session(self.home, points=2)

    def test_a_prediction_goes_from_the_review_list_to_reviewed(self):
        for _ in range(3):
            self.post("/api/sync")
        catalog = LocalDemoCatalog(self.home / "data_catalog" / "local_demo")
        raws = [catalog.get_record(catalog_module.raw_alias(capture_id)) for capture_id in self.made["capture_ids"]]
        predictions = [support.save_prediction(catalog, "raman-portable", raw, outcome="low_confidence",
                                               confidence=0.61) for raw in raws]
        self.assertEqual(self.post("/api/sync")[1]["predictions_found"], 2)

        pipeline = self.get("/api/pipeline")[1]
        self.assertNotIn("review_problem", pipeline)
        self.assertEqual({item["state"] for item in pipeline["items"]}, {"needs_review"})
        self.assertEqual(sorted(item["capture_id"] for item in pipeline["review"]), sorted(self.made["capture_ids"]))

        status, answer = self.post("/api/review", {"prediction": predictions[0].id, "decision": "confirm",
                                                   "label": None, "reviewer": "A Reviewer", "comment": ""})
        self.assertEqual((status, answer["ok"], answer["queued"]), (200, True, False), answer)
        self.assertEqual(self.post("/api/offline", {"offline": True})[0], 200)
        status, answer = self.post("/api/review", {"prediction": predictions[1].id, "decision": "reject",
                                                   "label": None, "reviewer": "A Reviewer", "comment": "noise"})
        self.assertEqual((status, answer["ok"], answer["queued"]), (200, True, True), answer)

        pipeline = self.get("/api/pipeline")[1]
        self.assertEqual({item["state"] for item in pipeline["items"]}, {"reviewed"})
        self.assertEqual(pipeline["status"]["reviews_queued"], 1)
        self.post("/api/offline", {"offline": False})
        self.assertEqual(self.post("/api/sync")[1]["reviews_sent"], 1)
        reviewed = catalog.get_record(predictions[1].id).metadata
        self.assertEqual((reviewed["review"]["status"], reviewed["needs_review"]), ("rejected", False))


# ---------------------------------------------------------------------- the catalog object of the server


class KeptCatalogTest(ServerCase):
    def prepare(self):
        self.catalog = LocalDemoCatalog(self.home / "data_catalog" / "local_demo")
        self.opened = self.patch(mock.patch.object(studio_server, "open_catalog", return_value=self.catalog))

    def test_the_catalog_object_is_kept_between_requests(self):
        first = self.get("/api/pipeline")[1]
        self.assertEqual((first["ok"], first["status"]["mode"], first["status"]["demo"]), (True, "local-demo", True))
        self.assertEqual(self.get("/api/pipeline?limit=3")[0], 200)
        self.assertEqual(self.post("/api/sync")[0], 200)
        again = self.get("/api/pipeline")[1]
        self.assertGreaterEqual(again["as_of_utc"], first["as_of_utc"])
        self.assertEqual(self.opened.call_count, 1)

    def test_changed_settings_select_another_catalog_object(self):
        self.get("/api/pipeline")
        (self.home / "catalog_config.json").write_text(json.dumps({"catalog": {"root_alias": "another-root"}}))
        self.get("/api/pipeline")
        self.assertEqual(self.opened.call_count, 2)


# ---------------------------------------------------------------------- POST /api/run, /api/stop


class RunTest(ServerCase):
    def state(self):
        return self.get("/api/scan")[1]

    def start(self, **changes):
        status, answer = self.post("/api/run", dict(RUN, **changes))
        self.assertEqual((status, answer["ok"]), (200, True), answer)
        self.addCleanup(self.kill, answer["pid"])
        return answer["pid"]

    @staticmethod
    def kill(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def ready(self):
        wait_for(lambda: "READY" in self.state()["log_tail"], "the stand-in runner to be ready")

    def ended(self):
        """The scan answer once the log says that the run ended."""
        def look():
            state = self.state()
            return state if state["log_tail"][-1].startswith("[studio] simulated run ended") else None
        return wait_for(look, "the end of the run in the log")

    def test_without_the_runner_the_answer_is_a_clear_error(self):
        status, answer = self.post("/api/run", RUN)
        self.assertEqual((status, answer["ok"], answer["error"]), (501, False, "The simulated runner is not installed"))
        self.assertIn(str(self.home / "raman_sim_runner"), answer["detail"])
        status, answer = self.post("/api/stop")
        self.assertEqual((status, answer["ok"]), (409, False))
        self.assertIn("controller window", answer["detail"])
        self.assertEqual((self.state()["run"], self.state()["active"]), (None, False))

    def test_a_run_gets_the_fixed_arguments_the_forced_simulation_and_home(self):
        install_runner(self.home)
        fields = {"mode": "2d", "power": "LOW", "sample_id": "S 1; rm -rf $(HOME) `id` 'q' \"d\" * \u200c",
                  "operator": "\u00dcn\u00efcode Operator", "location": "Bench 1 && echo hacked > /dev/null",
                  "program": "/bin/sh", "args": ["-c", "id"], "arguments": "--help",
                  "env": {"RAMAN_MOTOR_SIMULATE": "0"}}
        with mock.patch.dict(os.environ, {"RAMAN_BACKEND_SIMULATE": "0", "RAMAN_MOTOR_SIMULATE": ""}):
            status, answer = self.post("/api/run", fields)
        self.assertEqual((status, answer["ok"], answer["simulated"]), (200, True, True), answer)
        self.addCleanup(self.kill, answer["pid"])
        self.ready()
        state = self.state()
        log = state["log_tail"]
        self.assertEqual(log[0], "[studio] simulated run started (process %d): 2d, laser power LOW" % answer["pid"])
        self.assertEqual(json.loads(log[1][len("ARGUMENTS "):]), [
            "--mode", "2d", "--sample-id", fields["sample_id"], "--operator", fields["operator"],
            "--location", fields["location"], "--power", "LOW"])
        self.assertEqual(log[2], "SIMULATE backend=1 motor=1")
        self.assertEqual(log[3], "CWD " + str(self.home))
        self.assertTrue(state["active"], "a run of this server counts as an active scan")
        run = state["run"]
        self.assertEqual((run["pid"], run["running"], run["exit_code"], run["finished_utc"], run["stop_requested"]),
                         (answer["pid"], True, None, None, False))
        self.assertEqual((run["mode"], run["power"], run["sample_id"], run["simulated"]),
                         ("2d", "LOW", fields["sample_id"], True))

    def test_one_run_at_a_time_and_stop_ends_it(self):
        install_runner(self.home)
        pid = self.start()
        self.ready()
        status, answer = self.post("/api/run", RUN)
        self.assertEqual((status, answer["ok"]), (409, False))
        self.assertEqual(answer["error"], "A simulated run is already in progress")

        status, answer = self.post("/api/stop")
        self.assertEqual((status, answer), (200, {"ok": True, "stopped": True, "pid": pid, "forced": False,
                                                  "exit_code": -signal.SIGTERM}))
        self.assertFalse(process_exists(pid))
        state = self.ended()
        self.assertEqual((state["active"], state["run"]["running"], state["run"]["exit_code"]),
                         (False, False, -signal.SIGTERM))
        self.assertTrue(state["run"]["stop_requested"])
        self.assertIsNotNone(state["run"]["finished_utc"])
        self.assertEqual(state["log_tail"][-1], "[studio] simulated run ended: ended by SIGTERM")
        self.assertIn("[studio] stop requested: SIGTERM sent to the simulated run", state["log_tail"])
        self.assertEqual(self.post("/api/stop")[0], 409, "there is nothing left to stop")

        second = self.start(mode="single")
        self.assertNotEqual(second, pid)
        self.ready()
        self.assertEqual(self.state()["log_tail"][0],
                         "[studio] simulated run started (process %d): single, laser power HIGH" % second)
        self.assertEqual(self.post("/api/stop")[1]["pid"], second)

    def test_a_run_that_ignores_sigterm_is_killed_after_the_grace_period(self):
        install_runner(self.home)
        pid = self.start(sample_id="ignores SIGTERM")
        self.ready()
        started = time.monotonic()
        status, answer = self.post("/api/stop")
        waited = time.monotonic() - started
        self.assertEqual((status, answer), (200, {"ok": True, "stopped": True, "pid": pid, "forced": True,
                                                  "exit_code": -signal.SIGKILL}))
        self.assertGreaterEqual(waited, self.grace)
        self.assertFalse(process_exists(pid))
        log = self.ended()["log_tail"]
        self.assertEqual(log[-1], "[studio] simulated run ended: ended by SIGKILL")
        self.assertIn("[studio] the run did not end within 0.3 s: SIGKILL sent", log)

    def test_a_run_that_has_to_be_killed_takes_its_helper_programs_with_it(self):
        install_runner(self.home)
        pid = self.start(sample_id="ignores SIGTERM and has a helper")
        self.ready()
        helper = int(next(line for line in self.state()["log_tail"] if line.startswith("HELPER "))[len("HELPER "):])
        self.addCleanup(self.kill, helper)
        self.assertTrue(process_exists(helper))
        status, answer = self.post("/api/stop")
        self.assertEqual((status, answer["forced"], answer["pid"]), (200, True, pid))
        wait_for(lambda: not process_exists(helper), "the helper of the killed run to be gone")

    def test_a_run_that_ends_by_itself(self):
        install_runner(self.home)
        self.start(sample_id="ends by itself")
        state = self.ended()
        self.assertEqual(state["log_tail"][-1], "[studio] simulated run ended: exit code 3")
        self.assertEqual((state["active"], state["run"]["running"], state["run"]["exit_code"]), (False, False, 3))
        self.assertFalse(state["run"]["stop_requested"])
        self.assertIsNotNone(state["run"]["finished_utc"])
        self.assertNotIn("READY", state["log_tail"])
        self.assertEqual(self.post("/api/stop")[0], 409)

    def test_only_values_from_the_fixed_lists_and_plain_texts_start_a_run(self):
        install_runner(self.home)
        for change in ({"mode": "3d"}, {"mode": "1D-X"}, {"mode": None}, {"mode": ["2d"]}, {"mode": "2d --help"},
                       {"power": "MAX"}, {"power": "high"}, {"power": 3}, {"power": None},
                       {"sample_id": ""}, {"sample_id": "   "}, {"sample_id": None}, {"sample_id": 7},
                       {"sample_id": ["a", "b"]}, {"sample_id": "s" * 201}, {"sample_id": "two\nlines"},
                       {"sample_id": "nul\x00"}, {"sample_id": "--mode"}, {"sample_id": " -x"},
                       {"operator": ""}, {"operator": "-o"}, {"operator": "tab\there"},
                       {"location": None}, {"location": "--location=elsewhere"}, {"location": "\u2028"},
                       {"location": "Lab\u2028B"}, {"location": "Lab\u0085B"}, {"location": "half \ud800"},
                       # Shown as "S-1006" although it holds "S-6001": text that reorders itself for display.
                       {"sample_id": "S-\u202e6001"}, {"operator": "A\u2066B\u2069"}, {"location": "Lab\u202aB"}):
            status, answer = self.post("/api/run", dict(RUN, **change))
            self.assertEqual((status, answer["ok"]), (400, False), change)
        incomplete = dict(RUN)
        del incomplete["location"]
        self.assertEqual(self.post("/api/run", incomplete)[0], 400)
        self.assertIsNone(self.state()["run"], "none of these started anything")

    def test_the_log_keeps_only_the_end_of_long_output(self):
        script = self.home / "raman_sim_runner"
        script.write_text("#!/bin/sh\ni=0\nwhile [ $i -lt 450 ]; do echo \"line $i\"; i=$((i+1)); done\n"
                          "printf '%05000d\\n' 7 >&2\nprintf 'no newline at the end'\n")
        script.chmod(0o755)
        self.start()
        log = self.ended()["log_tail"]
        self.assertEqual(len(log), 200)
        self.assertEqual(log[-1], "[studio] simulated run ended: exit code 0")
        self.assertIn("line 449", log)
        self.assertIn("no newline at the end", log)
        self.assertNotIn("line 100", log)
        self.assertEqual(log[-5:-2], ["0" * 2000, "0" * 2000, "0" * 999 + "7"], "a long line arrives in pieces")

    def test_closing_the_server_ends_its_run(self):
        install_runner(self.home)
        pid = self.start()
        self.ready()
        self.server.studio.close()
        self.assertFalse(process_exists(pid))
        self.assertEqual(self.state()["run"]["exit_code"], -signal.SIGTERM)


# ---------------------------------------------------------------------- the command


class OpenServerTest(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)

    def listener(self, address="127.0.0.1", port=0):
        taken = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(taken.close)
        taken.bind((address, port))
        taken.listen(1)
        return taken

    def test_the_next_free_port_is_used_when_the_first_is_taken(self):
        for address in ("127.0.0.1", "0.0.0.0"):
            first = self.listener(address).getsockname()[1]
            server = studio_server.open_server(self.home, first)
            self.addCleanup(server.close)
            port = server.server_address[1]
            self.assertTrue(first < port < first + studio_server.PORT_ATTEMPTS, (address, first, port))
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertEqual(server.url, "http://127.0.0.1:%d/" % port)

    def test_it_gives_up_after_a_fixed_number_of_ports(self):
        first = self.listener().getsockname()[1]
        with mock.patch.object(studio_server, "PORT_ATTEMPTS", 1), \
                self.assertRaises(studio_server.CommandError) as raised:
            studio_server.open_server(self.home, first)
        self.assertIn(str(first), raised.exception.detail)

    def test_port_zero_takes_any_free_port(self):
        server = studio_server.open_server(self.home, 0)
        self.addCleanup(server.close)
        self.assertGreater(server.server_address[1], 0)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)

    def run_command(self, arguments, served=None):
        """`studio` in this process, with serving replaced by `served` and no real browser."""
        output = io.StringIO()
        seen = {}

        def serve_forever(server, *_arguments, **_options):
            seen["url"] = server.url
            if served is not None:
                served(server)

        with mock.patch.object(studio_server.webbrowser, "open", return_value=True) as browser, \
                mock.patch.object(studio_server.StudioServer, "serve_forever", serve_forever), \
                cli.Context(self.home, stdout=output) as ctx:
            answer = cli.invoke(ctx, ["studio", "--port", "0"] + arguments)
        return answer, output.getvalue(), browser, seen

    def test_it_prints_one_line_with_the_address_and_opens_the_browser(self):
        answer, printed, browser, seen = self.run_command([])
        self.assertEqual(answer, {"ok": True, "url": seen["url"]})
        self.assertEqual(json.loads(printed), {"ok": True, "url": seen["url"]})
        self.assertEqual(printed.count("\n"), 1)
        self.assertRegex(seen["url"], r"^http://127\.0\.0\.1:\d+/$")
        browser.assert_called_once_with(seen["url"])

    def test_no_open_leaves_the_browser_alone(self):
        _answer, _printed, browser, _seen = self.run_command(["--no-open"])
        browser.assert_not_called()

    def test_arguments_that_cannot_be_used(self):
        for arguments in (["--port", "70000"], ["--port", "-1"], ["--port", "http"], ["--idle-exit", "-5"],
                          ["--idle-exit", "nan"], ["--idle-exit", "inf"], ["--idle-exit", "soon"]):
            with cli.Context(self.home) as ctx:
                answer = cli.invoke(ctx, ["studio", "--no-open"] + arguments)
            self.assertFalse(answer["ok"], arguments)
        with cli.Context(self.home / "no_such_home") as ctx:
            self.assertEqual(cli.invoke(ctx, ["studio", "--no-open"])["error"], "The HOME folder does not exist")

    def test_the_command_is_part_of_the_registry_and_breaks_no_other(self):
        with cli.Context(self.home) as ctx:
            self.assertIn("--no-open", cli.invoke(ctx, ["studio", "-h"])["help"])
            self.assertTrue(cli.invoke(ctx, ["status"])["ok"])

    def test_a_server_is_in_use_while_it_answers_and_while_its_run_is_in_progress(self):
        install_runner(self.home)
        clock = [1000.0]
        patcher = mock.patch.object(studio_server.time, "monotonic", side_effect=lambda: clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        server = studio_server.StudioServer(self.home, 0, stop_grace_seconds=0.3)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(server.close)
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.shutdown)

        def call(method, path, fields=None, host=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                headers = {"Content-Type": "application/json"}
                if host is not None:
                    headers["Host"] = host
                connection.request(method, path, None if fields is None else json.dumps(fields), headers)
                return json.loads(connection.getresponse().read())
            finally:
                connection.close()

        clock[0] = 2000.0
        self.assertEqual(server.idle_seconds(), 1000.0)
        self.assertFalse(call("GET", "/api/ping", host="evil.example")["ok"])
        self.assertFalse(call("GET", "/no/such/address")["ok"])
        self.assertEqual(server.idle_seconds(), 1000.0, "refused requests are not use")
        self.assertTrue(call("GET", "/api/ping")["ok"])
        self.assertEqual(server.idle_seconds(), 0.0)
        clock[0] = 2500.0
        self.assertEqual(server.idle_seconds(), 500.0)
        with server.in_use():
            clock[0] = 2600.0
            self.assertEqual(server.idle_seconds(), 0.0, "a request that is being answered is use")
        clock[0] = 2700.0
        self.assertEqual(server.idle_seconds(), 100.0)

        pid = call("POST", "/api/run", RUN)["pid"]
        self.addCleanup(RunTest.kill, pid)
        clock[0] = 9000.0
        self.assertEqual(server.idle_seconds(), 0.0, "a run in progress is use")
        self.assertTrue(call("POST", "/api/stop", {})["stopped"])
        clock[0] = 9600.0
        self.assertEqual(server.idle_seconds(), 600.0)

    def test_idle_exit_ends_the_serving(self):
        server = studio_server.StudioServer(self.home, 0)
        port = server.server_address[1]
        ended = threading.Event()
        thread = threading.Thread(target=lambda: (studio_server._serve(server, 0.2), ended.set()), daemon=True)
        thread.start()
        self.assertTrue(ended.wait(30), "the server ends once nobody has used it for the given time")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()


# Starts a program with SIGINT, SIGTERM and SIGHUP at their default action, the ones named in the
# first argument ignored. Without it the server would inherit what the test run was started with:
# a shell starts a background job with SIGINT ignored, nohup starts one with SIGHUP ignored.
START_WITH_SIGNALS = """\
import os, signal, sys
ignored = [int(number) for number in sys.argv[1].split(",") if number]
for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(number, signal.SIG_IGN if number in ignored else signal.SIG_DFL)
os.execv(sys.argv[2], sys.argv[2:])
"""


class CommandLineTest(unittest.TestCase):
    """`python -m raman_catalog studio` as its own process, the way the controller starts it."""

    def setUp(self):
        self.home = support.make_home(self)

    def start(self, *options, ignored=()):
        process = subprocess.Popen(
            [sys.executable, "-c", START_WITH_SIGNALS, ",".join(str(int(number)) for number in ignored),
             sys.executable, "-m", "raman_catalog", "--home", str(self.home), "studio", "--port", "0", "--no-open",
             *options],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, cwd=str(self.home),
            env=support.cli_environment(self.home))
        self.addCleanup(process.communicate)
        self.addCleanup(process.kill)
        # The address has to come at once; a process that prints nothing must not hang the test.
        watchdog = threading.Timer(30, process.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        first = json.loads(process.stdout.readline())
        self.assertEqual(sorted(first), ["ok", "url"])
        self.assertTrue(first["ok"])
        return process, first["url"]

    def call(self, url, path, fields=None):
        port = int(url.rsplit(":", 1)[1].strip("/"))
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            body = None if fields is None else json.dumps(fields)
            connection.request("GET" if body is None else "POST", path, body, {"Content-Type": "application/json"})
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()

    def test_it_serves_until_interrupted_and_takes_its_run_with_it(self):
        install_runner(self.home)
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            process, url = self.start()
            self.assertEqual(self.call(url, "/api/ping")["home"], str(self.home))
            pid = self.call(url, "/api/run", RUN)["pid"]
            self.addCleanup(RunTest.kill, pid)
            wait_for(lambda: "READY" in self.call(url, "/api/scan")["log_tail"], "the stand-in runner to be ready")
            process.send_signal(number)
            rest, errors = process.communicate(timeout=30)
            self.assertEqual((process.returncode, rest), (0, ""), errors)
            self.assertFalse(process_exists(pid), "the run does not go on without its server")

    def test_a_signal_the_server_was_started_to_ignore_stays_ignored(self):
        process, url = self.start(ignored=[signal.SIGHUP])
        self.assertTrue(self.call(url, "/api/ping")["ok"])
        process.send_signal(signal.SIGHUP)
        time.sleep(0.2)
        self.assertTrue(self.call(url, "/api/ping")["ok"], "started the way nohup starts it, the server goes on")
        process.send_signal(signal.SIGTERM)
        rest, errors = process.communicate(timeout=30)
        self.assertEqual((process.returncode, rest), (0, ""), errors)

    def test_idle_exit_ends_a_server_nobody_uses(self):
        process, _url = self.start("--idle-exit", "0.2")
        rest, errors = process.communicate(timeout=30)
        self.assertEqual((process.returncode, rest), (0, ""), errors)


if __name__ == "__main__":
    unittest.main()
