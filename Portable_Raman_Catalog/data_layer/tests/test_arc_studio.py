"""Tests of the Studio server's endpoints for uploads to ARC (design 15.4), against temporary HOMEs.

The arc-* commands here are a stand-in module that records how it was called and answers what the
test gives it. It is loaded where the front end loads the ARC commands from (cli.FEATURE_MODULES).
No upload program is started, nothing is copied, and nothing connects anywhere but to the test's
own server on 127.0.0.1.
"""

import http.client
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import types
import unittest
from unittest import mock

import support

from raman_catalog import cli, doctor, studio_server
from raman_catalog.config import DataLayerError

CORE_COMMANDS = ["raman_catalog.commands_core", "raman_catalog.studio_server"]
STAND_IN = "studio_test_arc_commands"
NOT_THERE = "studio_test_no_arc_commands"
GLOBAL_ARGUMENTS = ("func", "command", "home", "verbose")
SESSION = "1d_x_scan_20261113_101500_123"
UPLOAD = "u3f9a0c1d2e4b"
ACTION_PATHS = (("/api/arc/retry", "arc-retry"), ("/api/arc/stop", "arc-stop"), ("/api/arc/check", "arc-check"),
                ("/api/arc/keep-copy", "arc-keep-copy"))
PING = {"ok": True, "mode": "local-demo", "simulated_stack_available": False}

ITEM = {"upload_id": UPLOAD, "session_folder": SESSION, "session_id": "5f0c1d2e-3a4b-4c5d-8e6f-7a8b9c0d1e2f",
        "kind": "simulated", "area": "simulated", "transport": "globus", "demo": True, "state": "transferring",
        "state_label": "Copying with Globus", "display_state": "transferring", "display_label": "Copying with Globus",
        "final": False, "message": "Demo: Copying with Globus: 12 of 22 files.",
        "progress": {"files": 22, "files_done": 12, "bytes": 433104, "bytes_done": 310000}, "actions": ["stop"]}
# A stand-in for this Mac's Globus collection ID, which GET /api/arc must not pass on.
MAC_COLLECTION = "00000000-0000-4000-8000-0000000000aa"
# arc-list's settings: every effective arc_upload value under its dotted name (design 8.4).
FLAT_SETTINGS = {"arc_upload.demo": True, "arc_upload.transport": "globus",
                 "arc_upload.folder": "/projects/pcl_photonics/raman_globus_upload",
                 "arc_upload.include_simulated": False, "arc_upload.settle_seconds": 60,
                 "arc_upload.demo_folder": "data_catalog/arc_demo", "arc_upload.globus.mac_collection": MAC_COLLECTION,
                 "arc_upload.globus.arc_collection": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
                 "arc_upload.globus.collection_root": "/projects",
                 "arc_upload.globus.client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2",
                 "arc_upload.globus.deadline_hours": 24, "arc_upload.globus.force_login": False,
                 "arc_upload.ssh.username": "", "arc_upload.ssh.host": "datatransfer.arc.vt.edu",
                 "arc_upload.ssh.known_hosts": ""}
# What GET /api/arc passes on of them.
SHOWN_SETTINGS = {"demo": True, "transport": "globus", "folder": "/projects/pcl_photonics/raman_globus_upload",
                  "include_simulated": False, "settle_seconds": 60}
LISTING = {"items": [ITEM], "total": 1, "active": 1, "unfinished": 1,
           "ready": {"demo": {"ok": True, "problems": []}}, "settings": FLAT_SETTINGS,
           "as_of_utc": "2026-11-15T16:01:12.020Z"}
SESSIONS = {"items": [{"folder": SESSION, "ready": True, "why_not": None, "kind": "simulated"}], "total": 1,
            "controller_idle_used": False,
            "settings": {"demo": True, "transport": "globus", "folder": "/projects/pcl_photonics/raman_globus_upload",
                         "globus_folder": "/pcl_photonics/raman_globus_upload", "include_simulated": False,
                         "settle_seconds": 60}}


class ArcFailure(DataLayerError):
    """A refusal of a stand-in arc command: its kind, and the reason and fix the real ones carry
    (design 8.1), which the front end adds to the failure object."""

    def __init__(self, kind, message, reason, fix, **extra):
        super().__init__(message)
        self.kind = kind
        self.extra = dict(reason=reason, fix=fix, **extra)


NOT_DEMO = ArcFailure("refused", "The Studio acts on demo uploads only.", "not_demo",
                      "Uploads to ARC are handled in the controller window.")


def arc_commands(calls, results, gate):
    """A command module with the arc-* commands the Studio uses (design 8.2 to 8.6), and a slow
    `classify` that holds the catalog's turn. They record how they were called and answer from `results`."""

    def command(name):
        def run(args, ctx):
            calls.append((name, {key: value for key, value in vars(args).items() if key not in GLOBAL_ARGUMENTS}))
            if name in gate:
                gate[name]()
            result = results[name]
            if isinstance(result, Exception):
                raise result
            return dict(result)
        return run

    def register(subparsers):
        listing = subparsers.add_parser("arc-list")
        listing.add_argument("--limit", type=int, default=12)
        listing.add_argument("--session")
        listing.set_defaults(func=command("arc-list"))
        sessions = subparsers.add_parser("arc-sessions")
        sessions.add_argument("--limit", type=int, default=30)
        sessions.add_argument("--controller-idle", action="store_true")
        sessions.set_defaults(func=command("arc-sessions"))
        upload = subparsers.add_parser("arc-upload")
        upload.add_argument("--session", required=True)
        upload.add_argument("--transport", choices=("globus", "ssh"))
        where = upload.add_mutually_exclusive_group()
        where.add_argument("--demo", action="store_true")
        where.add_argument("--live", action="store_true")
        upload.add_argument("--declare-real", action="store_true")
        upload.add_argument("--new", action="store_true")
        upload.add_argument("--dry-run", action="store_true")
        upload.add_argument("--controller-idle", action="store_true")
        upload.add_argument("--demo-fault", choices=("lose_submit_reply", "fail_task_once", "drop_connection"))
        upload.set_defaults(func=command("arc-upload"))
        for name in ("arc-check", "arc-retry", "arc-stop", "arc-keep-copy"):
            action = subparsers.add_parser(name)
            action.add_argument("--upload", required=True)
            action.add_argument("--demo-only", action="store_true")
            action.set_defaults(func=command(name))
        classify = subparsers.add_parser("classify")
        classify.add_argument("--limit", type=int)
        classify.set_defaults(func=command("classify"))

    module = types.ModuleType(STAND_IN)
    module.register = register
    return module


def only_this_server(port):
    """No connection but to the test's own server: the ARC tests run without a network (design 19.1)."""
    real_connect = socket.socket.connect

    def connect(sock, address):
        if not (isinstance(address, tuple) and tuple(address[:2]) == ("127.0.0.1", port)):
            raise AssertionError("a test of the ARC endpoints tried to connect to %r" % (address,))
        return real_connect(sock, address)

    return mock.patch.object(socket.socket, "connect", connect)


class ArcServerCase(unittest.TestCase):
    """A Studio server on a free port of 127.0.0.1, with the stand-in arc-* commands."""

    feature_modules = (STAND_IN,)

    def setUp(self):
        self.patch(mock.patch.dict(os.environ, {"RAMAN_ARC_TEST_GUARD": "1", "RAMAN_BACKEND_SIMULATE": "1",
                                                "RAMAN_MOTOR_SIMULATE": "1"}))
        self.home = support.make_home(self)
        self.calls, self.gate = [], {}
        self.results = {"arc-list": dict(LISTING), "arc-sessions": dict(SESSIONS),
                        "arc-upload": {"upload": ITEM, "existing": False, "worker": "detached",
                                       "message": "Demo upload started. Nothing leaves this Mac."},
                        "classify": {"runner": "local", "processed": 0, "predictions": 0, "skipped": 0, "errors": []}}
        for _path, name in ACTION_PATHS:
            self.results[name] = {"upload": ITEM, "worker": "detached", "message": "%s answered" % name}
        self.patch(mock.patch.dict(sys.modules, {STAND_IN: arc_commands(self.calls, self.results, self.gate)}))
        self.load_commands(self.feature_modules)
        # Every command line the server runs, the help probes of missing() included.
        self.argv = []
        invoke = cli.invoke

        def recording_invoke(ctx, argv):
            self.argv.append([str(item) for item in argv])
            return invoke(ctx, argv)

        self.patch(mock.patch.object(cli, "invoke", recording_invoke))
        self.server = studio_server.StudioServer(self.home, 0)
        self.port = self.server.server_address[1]
        self.assertNotEqual(self.port, studio_server.DEFAULT_PORT)
        self.origin = "http://127.0.0.1:%d" % self.port
        self.patch(only_this_server(self.port))
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server, thread)

    def load_commands(self, modules):
        """The core commands, then `modules` where the front end loads the ARC commands from:
        cli.FEATURE_MODULES, loaded after COMMAND_MODULES and skipped when a module does not exist."""
        self.patch(mock.patch.object(cli, "COMMAND_MODULES", list(CORE_COMMANDS)))
        self.patch(mock.patch.object(cli, "FEATURE_MODULES", list(modules)))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def stop_server(self, thread):
        self.server.shutdown()
        thread.join(10)
        self.server.close()

    def exchange(self, method, path, body=None, headers=None):
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

    def commands_run(self):
        """The command lines the server ran, without the help probes that ask whether a command exists."""
        return [argv for argv in self.argv if "-h" not in argv]

    def given(self, name):
        """The parsed options of every call of the stand-in command `name`."""
        return [options for called, options in self.calls if called == name]


# ---------------------------------------------------------------------- GET /api/arc


class ArcListTest(ArcServerCase):
    def test_the_uploads_and_a_few_settings_from_arc_list_alone(self):
        status, answer = self.get("/api/arc")
        self.assertEqual(status, 200)
        self.assertEqual(answer, {"ok": True, "uploads": [ITEM], "active": 1, "unfinished": 1,
                                  "settings": SHOWN_SETTINGS})
        # The page asks every second while an upload works: arc-sessions (every session.json and a
        # list of the running programs) is not run for it.
        self.assertEqual(self.commands_run(), [["arc-list", "--limit=12"]])
        self.assertEqual(self.given("arc-sessions"), [])

    def test_the_settings_leave_out_what_the_page_has_no_use_for(self):
        status, _headers, body = self.exchange("GET", "/api/arc")
        self.assertEqual(status, 200)
        self.assertNotIn(MAC_COLLECTION.encode("ascii"), body)
        self.assertNotIn(b"username", body)
        # A value arc-list does not give is null; the names stay the same.
        self.results["arc-list"] = dict(LISTING, settings={"arc_upload.folder": "/projects/team/elsewhere"})
        expected = dict.fromkeys(SHOWN_SETTINGS)
        expected["folder"] = "/projects/team/elsewhere"
        self.assertEqual(self.get("/api/arc")[1]["settings"], expected)

    def test_a_failed_list_answers_with_its_failure_object(self):
        invalid = ArcFailure("config", "The ARC settings are not valid: arc_upload.demo must be true or false.",
                             "settings_invalid", "Correct them in ARC settings (command line: config --set key=value).")
        self.results["arc-list"] = invalid
        status, answer = self.get("/api/arc")
        self.assertEqual((status, answer), (400, cli.error_payload(invalid)))

    def test_a_failing_arc_sessions_does_not_hide_the_uploads(self):
        self.results["arc-sessions"] = ArcFailure("command", "ps cannot list the programs", "internal", "Try again.")
        status, answer = self.get("/api/arc")
        self.assertEqual((status, answer.get("uploads")), (200, [ITEM]))
        self.assertEqual(self.given("arc-sessions"), [])

    def test_the_answer_is_read_again_every_time(self):
        self.assertEqual(self.get("/api/arc")[1]["active"], 1)
        self.results["arc-list"] = dict(LISTING, items=[], total=0, active=0, unfinished=0)
        self.assertEqual(self.get("/api/arc")[1]["uploads"], [])
        self.assertEqual(len(self.given("arc-list")), 2)

    def test_query_fields_change_nothing(self):
        self.assertEqual(self.get("/api/arc?limit=500&live=1&sessions=1")[0], 200)
        self.assertEqual(self.commands_run(), [["arc-list", "--limit=12"]])


# ---------------------------------------------------------------------- POST /api/arc/upload


class ArcUploadTest(ArcServerCase):
    def test_an_upload_from_the_studio_is_always_a_demo_upload(self):
        cases = [
            ({"session": SESSION, "transport": "globus"}, []),
            ({"session": SESSION, "transport": "ssh"}, []),
            ({"session": SESSION, "transport": "globus", "fault": "lose_submit_reply"},
             ["--demo-fault=lose_submit_reply"]),
            ({"session": SESSION, "transport": "globus", "fault": "fail_task_once"}, ["--demo-fault=fail_task_once"]),
            ({"session": SESSION, "transport": "ssh", "fault": "drop_connection"}, ["--demo-fault=drop_connection"]),
        ]
        for fields, extra in cases:
            status, answer = self.post("/api/arc/upload", fields)
            self.assertEqual((status, answer), (200, dict(self.results["arc-upload"], ok=True)), fields)
            self.assertEqual(self.commands_run()[-1], ["arc-upload", "--session=" + SESSION,
                                                       "--transport=" + fields["transport"], "--demo"] + extra)
        for argv in self.commands_run():
            forbidden = ("--live", "--controller-idle", "--new", "--declare-real", "--dry-run")
            self.assertFalse([item for item in argv if item.startswith(forbidden)], argv)
        for options in self.given("arc-upload"):
            self.assertEqual((options["demo"], options["live"], options["controller_idle"], options["new"],
                              options["declare_real"], options["dry_run"]), (True, False, False, False, False, False))
        self.assertEqual([options["demo_fault"] for options in self.given("arc-upload")],
                         [None, None, "lose_submit_reply", "fail_task_once", "drop_connection"])

    def test_a_session_name_that_looks_like_an_option_stays_the_session(self):
        for name in ("--live", "-x", "--controller-idle"):
            self.assertEqual(self.post("/api/arc/upload", {"session": name, "transport": "globus"})[0], 200, name)
        self.assertEqual([options["session"] for options in self.given("arc-upload")],
                         ["--live", "-x", "--controller-idle"])
        for options in self.given("arc-upload"):
            self.assertEqual((options["live"], options["controller_idle"]), (False, False))

    def test_fields_that_cannot_be_used_are_refused_before_anything_runs(self):
        good = {"session": SESSION, "transport": "globus"}
        refused = [
            {}, {"session": SESSION}, {"transport": "globus"}, dict(good, session=""), dict(good, session="   "),
            dict(good, session=" " + SESSION), dict(good, session=SESSION + "\n"), dict(good, session="a/b"),
            dict(good, session="../elsewhere"), dict(good, session="/etc"), dict(good, session="_hidden"),
            dict(good, session=".hidden"), dict(good, session="x" * 201), dict(good, session="é" * 201),
            dict(good, session=5), dict(good, session=None), dict(good, session=["x"]), dict(good, session=True),
            dict(good, session="a‮b"), dict(good, session="a\x00b"), dict(good, session="a b"),
            dict(good, transport="GLOBUS"), dict(good, transport="scp"), dict(good, transport=""),
            dict(good, transport=None), dict(good, transport=["globus"]), dict(good, transport=1),
            dict(good, fault="fail"), dict(good, fault=""), dict(good, fault=1), dict(good, fault=True),
            dict(good, fault=None),
            dict(good, fault=["drop_connection"]), dict(good, live=True), dict(good, demo=False),
            dict(good, controller_idle=True), dict(good, upload=UPLOAD), dict(good, new=True),
        ]
        for fields in refused:
            status, answer = self.post("/api/arc/upload", fields)
            self.assertEqual((status, answer["ok"]), (400, False), fields)
        self.assertEqual(self.calls, [])

    def test_a_name_of_200_code_points_in_any_script_is_passed_on(self):
        for name in ("é" * 200, "\U0001f600" * 200, "Probe-1.ü"):
            self.assertEqual(self.post("/api/arc/upload", {"session": name, "transport": "ssh"})[0], 200, name)
        self.assertEqual([options["session"] for options in self.given("arc-upload")],
                         ["é" * 200, "\U0001f600" * 200, "Probe-1.ü"])

    def test_the_answer_of_the_command_comes_back_as_it_is(self):
        existing = {"upload": ITEM, "existing": True}
        self.results["arc-upload"] = existing
        self.assertEqual(self.post("/api/arc/upload", {"session": SESSION, "transport": "globus"}),
                         (200, dict(existing, ok=True)))

    def test_a_fault_of_the_other_transport_is_refused_before_anything_runs(self):
        # The demo of one transport ignores the other's fault, so the upload would simply succeed.
        for transport, fault, allowed in (("ssh", "lose_submit_reply", "drop_connection"),
                                          ("ssh", "fail_task_once", "drop_connection"),
                                          ("globus", "drop_connection", "lose_submit_reply, fail_task_once")):
            status, answer = self.post("/api/arc/upload", {"session": SESSION, "transport": transport, "fault": fault})
            self.assertEqual((status, answer.get("ok"), answer.get("error")),
                             (400, False, "fault with transport %s must be one of: %s" % (transport, allowed)),
                             (transport, fault))
        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------------- the four actions


class ArcActionTest(ArcServerCase):
    def test_the_four_actions_act_on_demo_uploads_only(self):
        for path, name in ACTION_PATHS:
            status, answer = self.post(path, {"upload": UPLOAD})
            self.assertEqual((status, answer), (200, dict(self.results[name], ok=True)), path)
        self.assertEqual(self.commands_run(),
                         [[name, "--upload=" + UPLOAD, "--demo-only"] for _path, name in ACTION_PATHS])
        self.assertEqual([options for called, options in self.calls],
                         [{"upload": UPLOAD, "demo_only": True}] * len(ACTION_PATHS))

    def test_an_upload_to_the_real_arc_answers_403_with_the_failure_of_the_command(self):
        for path, name in ACTION_PATHS:
            self.results[name] = NOT_DEMO
            status, answer = self.post(path, {"upload": UPLOAD})
            self.assertEqual((status, answer), (403, cli.error_payload(NOT_DEMO)), path)
            self.assertEqual((answer["kind"], answer["reason"], answer["error"], answer["fix"]),
                             ("refused", "not_demo", "The Studio acts on demo uploads only.",
                              "Uploads to ARC are handled in the controller window."))

    def test_upload_ids_that_cannot_be_used_are_refused_before_anything_runs(self):
        for value in ("", "u3f9a0c1d2e4", "u3f9a0c1d2e4bb", "U3F9A0C1D2E4B", "u3F9A0C1D2E4B", "u3f9a0c1d2e4g",
                      "x3f9a0c1d2e4b", UPLOAD + "\n", " " + UPLOAD, "--upload=" + UPLOAD, "../" + UPLOAD,
                      "u３f9a0c1d2e4b", 3, None, True, [UPLOAD], {"id": UPLOAD}):
            for path, _name in ACTION_PATHS:
                status, answer = self.post(path, {"upload": value})
                self.assertEqual((status, answer["ok"]), (400, False), (path, value))
        for fields in ({}, {"upload": UPLOAD, "demo_only": False}, {"upload": UPLOAD, "live": True},
                       {"id": UPLOAD}, {"upload": UPLOAD, "session": SESSION}):
            for path, _name in ACTION_PATHS:
                self.assertEqual(self.post(path, fields)[0], 400, (path, fields))
        self.assertEqual(self.calls, [])


# ---------------------------------------------------------------------- statuses, methods, who may ask


class ArcHttpTest(ArcServerCase):
    def test_http_status_by_kind(self):
        for kind, expected in (("not_ready", 409), ("refused", 403), ("not_installed", 501), ("not_found", 404),
                               ("conflict", 409), ("config", 400), ("command", 400), ("metadata", 400),
                               ("usage", 400), ("internal", 500)):
            extra = {"retry_after_seconds": 42} if kind == "not_ready" else {}
            failure = ArcFailure(kind, "a %s failure of the stand-in" % kind, "a_reason", "A fix.", **extra)
            self.results["arc-upload"] = failure
            status, answer = self.post("/api/arc/upload", {"session": SESSION, "transport": "globus"})
            self.assertEqual((status, answer), (expected, cli.error_payload(failure)), kind)
            self.assertEqual((answer["kind"], answer["reason"], answer["fix"], answer.get("retry_after_seconds")),
                             (kind, "a_reason", "A fix.", extra.get("retry_after_seconds")), kind)
            self.results["arc-check"] = failure
            self.assertEqual(self.post("/api/arc/check", {"upload": UPLOAD})[0], expected, kind)
            self.results["arc-list"] = failure
            self.assertEqual(self.get("/api/arc")[0], expected, kind)

    def test_each_address_takes_its_own_method(self):
        for method, path, allowed in [("GET", "/api/arc/upload", "POST"), ("GET", "/api/arc/keep-copy", "POST"),
                                      ("POST", "/api/arc", "GET"), ("PUT", "/api/arc", "GET"),
                                      ("DELETE", "/api/arc/stop", "POST"), ("OPTIONS", "/api/arc/retry", "POST")]:
            body = "{}" if method in ("POST", "PUT") else None
            status, headers, answer = self.exchange(method, path, body, {"Content-Type": "application/json"})
            self.assertEqual((status, headers["Allow"]), (405, allowed), (method, path))
            self.assertFalse(json.loads(answer)["ok"])
        for path in ("/api/arc/", "/api/arc/upload/", "/api/arc/keep_copy", "/api/arc/delete", "/api/arc/status",
                     "/api/ARC", "/api/arc/../arc/upload"):
            self.assertEqual(self.exchange("POST", path, "{}", {"Content-Type": "application/json"})[0], 404, path)
        self.assertEqual(self.calls, [])

    def test_host_origin_and_sec_fetch_site_rules_apply_to_the_new_endpoints(self):
        posts = [("/api/arc/upload", {"session": SESSION, "transport": "globus"})] + \
            [(path, {"upload": UPLOAD}) for path, _name in ACTION_PATHS]
        for host in ("evil.example", "evil.example:%d" % self.port, "127.0.0.1", "localhost",
                     "127.0.0.1:%d" % (self.port + 1), ""):
            self.assertEqual(self.get("/api/arc", {"Host": host})[0], 403, host)
            for path, fields in posts:
                self.assertEqual(self.post(path, fields, {"Host": host, "Origin": "http://" + host})[0], 403,
                                 (host, path))
        for origin in ("http://evil.example", "null", "http://127.0.0.1:%d" % (self.port + 1),
                       "https://127.0.0.1:%d" % self.port, "http://localhost:%d" % self.port):
            for path, fields in posts:
                self.assertEqual(self.post(path, fields, {"Origin": origin})[0], 403, (origin, path))
        for site in ("cross-site", "same-site", "Cross-Site"):
            self.assertEqual(self.get("/api/arc", {"Sec-Fetch-Site": site})[0], 403, site)
        self.assertEqual(self.calls, [])
        # The Studio page itself, an address typed by the user and a program that is not a browser are answered.
        for site in ("same-origin", "none", None):
            self.assertEqual(self.get("/api/arc", {"Sec-Fetch-Site": site} if site else {})[0], 200, site)
        named = {"Host": "localhost:%d" % self.port, "Origin": "http://localhost:%d" % self.port}
        for path, fields in posts:
            self.assertEqual(self.post(path, fields, named)[0], 200, path)
        status, _headers, _body = self.exchange("POST", "/api/arc/check", json.dumps({"upload": UPLOAD}),
                                                {"Content-Type": "application/json"})
        self.assertEqual(status, 200, "a program that sends no Origin")

    def test_a_post_body_must_be_a_json_object_of_at_most_64_kb(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            headers = {"Origin": self.origin}
            if content_type:
                headers["Content-Type"] = content_type
            body = json.dumps({"session": SESSION, "transport": "globus"})
            self.assertEqual(self.exchange("POST", "/api/arc/upload", body, headers)[0], 415, content_type)
        for body in ("", "not json", "[1, 2]", '"%s"' % UPLOAD, "null", '{"upload": NaN}', '{"upload": "%s"' % UPLOAD):
            headers = {"Content-Type": "application/json", "Origin": self.origin}
            self.assertEqual(self.exchange("POST", "/api/arc/stop", body, headers)[0], 400, body)
        head = "POST /api/arc/upload HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n" % self.port
        for length_header, status in (("Content-Length: 65537\r\n", b" 413 "), ("", b" 411 "),
                                      ("Transfer-Encoding: chunked\r\n", b" 411 ")):
            answer = self.raw(head + length_header + "\r\n")
            self.assertIn(status, answer.split(b"\r\n", 1)[0], length_header)
        self.assertEqual(self.calls, [])

    def test_ping_is_unchanged(self):
        status, answer = self.get("/api/ping")
        self.assertEqual((status, answer), (200, dict(PING, home=str(self.home))))


class WithoutArcCommandsTest(ArcServerCase):
    feature_modules = (NOT_THERE,)

    def test_every_arc_endpoint_answers_501_not_available(self):
        status, answer = self.get("/api/arc")
        self.assertEqual((status, answer["ok"], answer["error"]), (501, False, "not available"))
        self.assertIn("arc-list", answer["detail"])
        status, answer = self.post("/api/arc/upload", {"session": SESSION, "transport": "globus"})
        self.assertEqual((status, answer["error"]), (501, "not available"))
        for path, name in ACTION_PATHS:
            status, answer = self.post(path, {"upload": UPLOAD})
            self.assertEqual((status, answer["error"]), (501, "not available"), path)
            self.assertIn(name, answer["detail"])
        self.assertEqual(self.calls, [])

    def test_the_fields_are_still_checked_first(self):
        self.assertEqual(self.post("/api/arc/upload", {"session": "a/b", "transport": "globus"})[0], 400)
        self.assertEqual(self.post("/api/arc/stop", {"upload": "nothing"})[0], 400)

    def test_ping_and_the_rest_of_the_studio_are_unchanged(self):
        self.assertEqual(self.get("/api/ping"), (200, dict(PING, home=str(self.home))))
        self.assertEqual(self.get("/api/scan")[0], 200)
        self.assertEqual(self.get("/api/pipeline")[0], 200)


class BrokenArcCommandsTest(ArcServerCase):
    def test_an_arc_module_that_is_broken_is_reported_as_broken(self):
        # A module without register(): loading the commands fails, which is not the same as a missing part.
        with mock.patch.dict(sys.modules, {STAND_IN: types.ModuleType(STAND_IN)}), \
                self.assertLogs("raman_catalog", "ERROR"):
            status, answer = self.get("/api/arc")
        self.assertEqual((status, answer["ok"], answer["kind"]), (500, False, "internal"))
        self.assertNotEqual(answer["error"], "not available")


# ---------------------------------------------------------------------- one at a time, and not behind the catalog


class ArcTurnTest(ArcServerCase):
    def hold(self, name):
        """Make the stand-in command `name` wait inside until the returned release() is called."""
        entered, released = threading.Event(), threading.Event()
        # A failing assertion must not leave the command waiting.
        self.addCleanup(released.set)

        def wait():
            entered.set()
            self.assertTrue(released.wait(30))

        self.gate[name] = wait
        return entered, released.set

    def test_arc_commands_do_not_wait_for_a_command_that_has_the_catalog(self):
        entered, release = self.hold("classify")
        answers = []
        worker = threading.Thread(target=lambda: answers.append(self.post("/api/classify")))
        worker.start()
        self.assertTrue(entered.wait(30))
        started = time.monotonic()
        self.assertEqual(self.get("/api/arc")[0], 200)
        self.assertEqual(self.post("/api/arc/upload", {"session": SESSION, "transport": "globus"})[0], 200)
        self.assertLess(time.monotonic() - started, 10)
        release()
        worker.join(30)
        self.assertEqual(answers[0][0], 200)

    def test_arc_commands_run_one_at_a_time(self):
        entered, release = self.hold("arc-upload")
        answers = []
        upload = threading.Thread(target=lambda: answers.append(
            self.post("/api/arc/upload", {"session": SESSION, "transport": "globus"})))
        upload.start()
        self.assertTrue(entered.wait(30))
        listing = threading.Thread(target=lambda: answers.append(self.get("/api/arc")))
        listing.start()
        time.sleep(0.2)
        self.assertEqual(self.given("arc-list"), [], "the list waits for the upload command")
        release()
        upload.join(30)
        listing.join(30)
        self.assertEqual(sorted(status for status, _answer in answers), [200, 200])
        self.assertEqual([name for name, _options in self.calls], ["arc-upload", "arc-list"])

    def test_unusable_catalog_settings_do_not_stop_the_arc_endpoints(self):
        (self.home / "catalog_config.json").write_text(json.dumps({"sync": {"max_attempts": 0}}))
        self.assertEqual(self.get("/api/pipeline")[0], 400, "the catalog's own endpoints report the settings")
        self.assertEqual(self.get("/api/arc")[0], 200)
        self.assertEqual(self.post("/api/arc/upload", {"session": SESSION, "transport": "ssh"})[0], 200)
        self.assertEqual(self.post("/api/arc/retry", {"upload": UPLOAD})[0], 200)


# ---------------------------------------------------------------------- with the data layer's own commands


@unittest.skipUnless(importlib.util.find_spec("raman_catalog.arc_commands"), "the arc-* commands are not installed")
class InstalledArcCommandsTest(ArcServerCase):
    """The endpoints with the arc-* commands of this installation, on an empty HOME. Nothing here makes
    an upload, so no upload program starts."""

    feature_modules = ("raman_catalog.arc_commands",)

    def test_the_list_of_an_empty_home(self):
        status, answer = self.get("/api/arc")
        self.assertEqual(status, 200, answer)
        self.assertEqual(sorted(answer), ["active", "ok", "settings", "unfinished", "uploads"])
        self.assertEqual((answer["uploads"], answer["active"], answer["unfinished"]), ([], 0, 0))
        self.assertEqual(answer["settings"], SHOWN_SETTINGS, "the defaults")

    def test_the_list_does_not_list_the_running_programs(self):
        # The page asks every second while an upload works; arc-sessions would run ps each time.
        listed = []

        def running_commands():
            listed.append("ps")
            return []

        self.patch(mock.patch.object(doctor, "_running_commands", running_commands))
        for _ in range(3):
            self.assertEqual(self.get("/api/arc")[0], 200)
        self.assertEqual(listed, [])
        self.assertEqual(self.commands_run(), [["arc-list", "--limit=12"]] * 3)

    def test_unknown_sessions_and_uploads(self):
        status, answer = self.post("/api/arc/upload", {"session": SESSION, "transport": "globus"})
        self.assertEqual((status, answer["kind"]), (404, "not_found"), answer)
        for path, _name in ACTION_PATHS:
            status, answer = self.post(path, {"upload": UPLOAD})
            self.assertEqual((status, answer["kind"]), (404, "not_found"), (path, answer))
        records = self.home / "data_catalog" / "arc_uploads"
        made = [entry.name for entry in records.iterdir() if not entry.name.startswith(".")] if records.is_dir() else []
        self.assertEqual([name for name in made if name.startswith("u")], [], "no upload was made")


if __name__ == "__main__":
    unittest.main()
