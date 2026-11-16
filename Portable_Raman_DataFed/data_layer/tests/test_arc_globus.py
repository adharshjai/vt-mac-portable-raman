"""Uploads to ARC through Globus (PROTOCOL.md 5.6; design sections 10, 12 and 13).

The flow (arc_globus.GlobusFlow) runs in the real worker, against:
- the live client LiveGlobus, built with a stand-in for the globus_sdk module (FakeSDK below): its
  UserApp, TransferClient and AuthClient record every call, and the Transfer calls are answered by
  a demo service on a temporary folder, so a test can let a call through, change its answer, lose
  it, or refuse it with Globus's codes;
- the demo client arc_demo.DemoGlobus, end to end through arc-upload --demo;
- real worker processes stopped with a test fault (design 13, rows 5 to 12), then continued.

Nothing here reaches Globus: every ARC test sets RAMAN_ARC_TEST_GUARD=1, which refuses to build
LiveGlobus with the real SDK, and no_network() makes every socket connection fail. The few tests
that read the real SDK's classes are skipped when globus-sdk is not installed.
"""

import contextlib
import copy
import fcntl
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import arc_support
import support
from arc_support import ARC_FOLDER, SESSION
from raman_datafed import arc_commands, arc_demo, arc_globus, arc_payload, arc_records, arc_worker
from raman_datafed.arc_globus import GlobusCallError

HAVE_SDK = importlib.util.find_spec("globus_sdk") is not None
REAL_MAKE_CLIENT = arc_globus.make_client
# A made-up collection ID for this Mac's Globus Connect Personal (no real one is used in the tests).
MAC_COLLECTION = "3b7f0c52-9a64-4f1e-8d2a-5c6e7f8091a2"
ARC_COLLECTION = "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"
# What the stand-in sign-in hands out: these strings must never reach a file.
TOKENS = ("AccessToken-for-the-tests-7f3a91", "IdToken-for-the-tests-c2d84e")
IDENTITY = "tester-identity"
GLOBUS_BASE = "/pcl_photonics/raman_globus_upload"


class Crash(BaseException):
    """Ends a worker in this process the way a killed process ends: nothing more is written."""


# ---------------------------------------------------------------------- a stand-in for globus_sdk


class FakeAPIError(Exception):
    def __init__(self, http_status, code, message="", gare=None):
        super().__init__("%s %s %s" % (http_status, code, message))
        self.http_status = http_status
        self.code = code
        self.message = message
        self.fake_gare = gare


class FakeTransferAPIError(FakeAPIError):
    pass


class FakeNetworkError(Exception):
    pass


class FakeConnectionError(FakeNetworkError):
    pass


class FakeTokenValidationError(Exception):
    pass


class FakeLoginError(Exception):
    pass


class FakeEnvironmentalLoginError(FakeLoginError):
    pass


class FakeParameters:
    def __init__(self, **values):
        self.values = values
        for key, value in values.items():
            setattr(self, key, value)


class FakeGARE:
    def __init__(self, scopes=("urn:globus:auth:scope:transfer.api.globus.org:all",)):
        self.authorization_parameters = FakeParameters(required_scopes=list(scopes))


# The scope a consent for ARC's collection asks for (a standard mapped collection's data_access).
DATA_ACCESS = ("urn:globus:auth:scope:transfer.api.globus.org:all[*https://auth.globus.org/scopes/%s/data_access]"
               % ARC_COLLECTION)


def consent_error(status=403, scopes=None):
    return FakeTransferAPIError(status, "ConsentRequired", "Missing required data_access consent",
                                gare=FakeGARE(scopes) if scopes else FakeGARE())


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def __getitem__(self, key):
        return self.data[key]


class FakeRetryConfig:
    """The SDK's RetryConfig as far as the data layer uses it: max_retries and tune()."""

    def __init__(self, max_retries=5):
        self.max_retries = max_retries

    @contextlib.contextmanager
    def tune(self, *, max_retries=None, **_ignored):
        saved = self.max_retries
        if max_retries is not None:
            self.max_retries = max_retries
        try:
            yield
        finally:
            self.max_retries = saved


class FakeTransport:
    """The SDK's RequestsTransport as far as the data layer uses it: http_timeout and tune()."""

    def __init__(self):
        self.http_timeout = 60.0

    @contextlib.contextmanager
    def tune(self, *, http_timeout=None, **_ignored):
        saved = self.http_timeout
        if http_timeout is not None:
            self.http_timeout = http_timeout
        try:
            yield
        finally:
            self.http_timeout = saved


# What the SDK's transport sends again by itself (its RetryConfig's transient codes and network errors).
TRANSIENT = (429, 500, 502, 503, 504)


class FakeSDK:
    """A module object standing in for globus_sdk, its Transfer calls answered by a demo service.

    `script[name]` lists what the next calls of a Transfer method do: an exception instance is raised,
    None lets the call through to the service, and a function f(client, *arguments, **options) is
    called instead (client.through(name, ...) reaches the service).

    `resends`: the SDK's transport sends a call again by itself after a network error or a transient
    answer, up to the client's retry_config.max_retries (off unless a test turns it on).
    `consent_required`: ARC's collection asks for its data_access consent until a sign-in gave it;
    Globus keeps a consent given once (for every later worker) until `consented` is set back.
    Every sign-in hands out new tokens; `revoked` lists the tokens revoked through NativeAppAuthClient."""

    def __init__(self, arc_root, mac_root, pace=0, clock=time.time):
        self.server = arc_demo.DemoGlobus(arc_root, "/projects", mac_root=mac_root, mac_collection=MAC_COLLECTION,
                                          arc_collection=ARC_COLLECTION, pace=pace, clock=clock)
        self.script = {}
        self.apps, self.transfers, self.auths, self.configs, self.revokers = [], [], [], [], []
        self.login_behaviour = None
        self.logout_error = None
        self.identity = IDENTITY
        self.identity_id = "0f5e8c1a-0000-4000-8000-00000000aaaa"
        self.resends = False
        self.consent_required = False
        self.consented = False
        self.issued, self.revoked, self.revoke_timeouts = [], [], []
        self.module = self._module()

    def calls(self, name):
        return [call for transfer in self.transfers for call in transfer.calls if call[0] == name]

    def issue(self):
        """A new token pair of one sign-in: {resource server: access token}."""
        number = len(self.issued) // 2 + 1
        pair = {"transfer.api.globus.org": "%s-%d" % (TOKENS[0], number),
                "auth.globus.org": "%s-%d" % (TOKENS[1], number)}
        self.issued.extend(pair.values())
        return pair

    def unrevoked(self):
        return [token for token in self.issued if token not in self.revoked]

    def _module(self):
        sdk = self
        module = types.ModuleType("fake_globus_sdk")

        class RedirectHandler:
            def log_message(self, format_text, *arguments):
                sys.stderr.write("%s\n" % (format_text % arguments))

        class LocalServerLoginFlowManager:
            def __init__(self, login_client, *, request_refresh_tokens=False, native_prefill_named_grant=None,
                         server_address=("localhost", 0), html_template=None):
                self.login_client = login_client
                self.request_refresh_tokens = request_refresh_tokens
                self.native_prefill_named_grant = native_prefill_named_grant
                self.server_address = server_address

        class Config:
            def __init__(self, **values):
                self.values = values
                for key, value in values.items():
                    setattr(self, key, value)
                sdk.configs.append(self)

        class MemoryStorage:
            """The SDK's memory token storage: {resource server: token data}."""

            def __init__(self):
                self.data = {}

            def get_token_data_by_resource_server(self):
                return {server: types.SimpleNamespace(resource_server=server, access_token=token, refresh_token=None)
                        for server, token in self.data.items()}

            def remove_token_data(self, resource_server):
                return self.data.pop(resource_server, None) is not None

        class ValidatingStorage:
            def __init__(self):
                self.token_storage = MemoryStorage()

            def get_token_data_by_resource_server(self):
                raise AssertionError("the validating storage may call Globus; the data layer reads the inner one")

        class UserApp:
            def __init__(self, app_name, *, client_id, config):
                self.app_name, self.client_id, self.config = app_name, client_id, config
                self.scopes, self.logins = [], []
                self.token_storage = ValidatingStorage()
                self.logout_calls = self.close_calls = 0
                sdk.apps.append(self)

            @property
            def tokens(self):
                return dict(self.token_storage.token_storage.data)

            def add_scope_requirements(self, requirements):
                self.scopes.append(requirements)

            def login(self, *, auth_params=None, force=False):
                self.logins.append(auth_params)
                if sdk.login_behaviour is not None:
                    sdk.login_behaviour(self, auth_params)
                # A consent sign-in replaces the stored tokens, as the SDK's login flow does.
                self.token_storage.token_storage.data.update(sdk.issue())
                scopes = getattr(auth_params, "required_scopes", None) or []
                if any("data_access" in scope for scope in scopes):
                    sdk.consented = True

            def logout(self):
                self.logout_calls += 1
                self.token_storage.token_storage.data.clear()

            def close(self):
                self.close_calls += 1

        class NativeAppAuthClient:
            def __init__(self, client_id, app_name=None, **_options):
                self.client_id, self.app_name, self.closed = client_id, app_name, 0
                self.retry_config, self.transport = FakeRetryConfig(), FakeTransport()
                sdk.revokers.append(self)

            def oauth2_revoke_token(self, token):
                sdk.revoke_timeouts.append((self.retry_config.max_retries, self.transport.http_timeout))
                if sdk.logout_error is not None:
                    raise sdk.logout_error
                sdk.revoked.append(token)
                return FakeResponse({"active": False})

            def close(self):
                self.closed += 1

        class TransferClient:
            def __init__(self, *, app):
                self.app, self.calls, self.closed = app, [], 0
                self.retry_config = FakeRetryConfig()
                sdk.transfers.append(self)

            def through(self, name, *arguments, **options):
                server = sdk.server
                try:
                    if name == "get_endpoint":
                        return FakeResponse(server.get_endpoint(*arguments))
                    if name == "operation_ls":
                        collection, path = arguments
                        return FakeResponse({"DATA_TYPE": "file_list", "path": path, "DATA": server.ls(
                            collection, path, options["offset"], options["limit"])})
                    if name == "operation_stat":
                        return FakeResponse(server.stat(*arguments))
                    if name == "operation_mkdir":
                        return FakeResponse(server.mkdir(*arguments))
                    if name == "operation_rename":
                        return FakeResponse(server.rename(*arguments))
                    if name == "get_submission_id":
                        return FakeResponse({"value": server.submission_id()})
                    if name == "submit_transfer":
                        return FakeResponse(server.submit(*arguments))
                    if name == "get_task":
                        return FakeResponse(server.get_task(*arguments))
                    if name == "task_list":
                        return FakeResponse({"DATA": server.task_list(options["filter"], options["limit"])})
                    if name == "task_successful_transfers":
                        return FakeResponse(server.successful_transfers(arguments[0], options.get("marker")))
                    if name == "cancel_task":
                        return FakeResponse(server.cancel(*arguments))
                    if name == "task_event_list":
                        return FakeResponse({"DATA": server.task_events(arguments[0], options["limit"])})
                except GlobusCallError as error:
                    raise FakeTransferAPIError(error.http_status, error.code, error.message) from None
                raise AssertionError("no such call: %s" % name)

            def _do(self, name, *arguments, **options):
                attempts = self.retry_config.max_retries + 1 if sdk.resends else 1
                for attempt in range(attempts):
                    try:
                        return self._once(name, *arguments, **options)
                    except (FakeNetworkError, FakeAPIError) as error:
                        transient = isinstance(error, FakeNetworkError) or error.http_status in TRANSIENT
                        if not transient or attempt + 1 >= attempts:
                            raise

            def _once(self, name, *arguments, **options):
                self.calls.append((name, copy.deepcopy(arguments), copy.deepcopy(options),
                                   self.retry_config.max_retries))
                if sdk.consent_required and not sdk.consented and (
                        (name.startswith("operation_") and arguments[0] == ARC_COLLECTION) or
                        (name == "submit_transfer" and arguments[0].get("destination_endpoint") == ARC_COLLECTION)):
                    raise consent_error(scopes=(DATA_ACCESS,))
                planned = sdk.script.get(name)
                if planned:
                    action = planned.pop(0)
                    if isinstance(action, BaseException):
                        raise action
                    if action is not None:
                        result = action(self, *arguments, **options)
                        return result if isinstance(result, FakeResponse) else FakeResponse(result)
                return self.through(name, *arguments, **options)

            def get_endpoint(self, endpoint_id):
                return self._do("get_endpoint", endpoint_id)

            def operation_ls(self, endpoint_id, path, *, show_hidden, limit, offset):
                assert show_hidden is True
                return self._do("operation_ls", endpoint_id, path, limit=limit, offset=offset)

            def operation_stat(self, endpoint_id, path):
                return self._do("operation_stat", endpoint_id, path)

            def operation_mkdir(self, endpoint_id, path):
                return self._do("operation_mkdir", endpoint_id, path)

            def operation_rename(self, endpoint_id, oldpath, newpath):
                return self._do("operation_rename", endpoint_id, oldpath, newpath)

            def get_submission_id(self):
                return self._do("get_submission_id")

            def submit_transfer(self, data):
                assert isinstance(data, dict)
                return self._do("submit_transfer", data)

            def get_task(self, task_id):
                return self._do("get_task", task_id)

            def task_list(self, *, filter, limit):  # noqa: A002 - the SDK's name
                return self._do("task_list", filter=filter, limit=limit)

            def task_successful_transfers(self, task_id, *, marker=None):
                return self._do("task_successful_transfers", task_id, marker=marker)

            def cancel_task(self, task_id):
                return self._do("cancel_task", task_id)

            def task_event_list(self, task_id, *, limit):
                return self._do("task_event_list", task_id, limit=limit)

            def close(self):
                self.closed += 1

        class AuthClient:
            def __init__(self, *, app, app_scopes=None):
                self.app, self.app_scopes, self.closed = app, app_scopes, 0
                sdk.auths.append(self)

            def userinfo(self):
                return FakeResponse({"preferred_username": sdk.identity, "name": "Test Identity",
                                     "sub": sdk.identity_id})

            def close(self):
                self.closed += 1

        module.GlobusAppConfig = Config
        module.UserApp = UserApp
        module.TransferClient = TransferClient
        module.AuthClient = AuthClient
        module.NativeAppAuthClient = NativeAppAuthClient
        module.Scope = str
        module.GlobusAPIError = FakeAPIError
        module.TransferAPIError = FakeTransferAPIError
        module.NetworkError = FakeNetworkError
        module.GlobusConnectionError = FakeConnectionError
        module.login_flows = types.SimpleNamespace(
            LocalServerLoginFlowManager=LocalServerLoginFlowManager, LocalServerLoginError=FakeLoginError,
            LocalServerEnvironmentalLoginError=FakeEnvironmentalLoginError,
            local_server_login_flow_manager=types.SimpleNamespace(
                local_server=types.SimpleNamespace(RedirectHandler=RedirectHandler)))
        module.gare = types.SimpleNamespace(to_gare=lambda error: getattr(error, "fake_gare", None),
                                            GlobusAuthorizationParameters=FakeParameters)
        module.token_storage = types.SimpleNamespace(TokenValidationError=FakeTokenValidationError)
        return module


def lost_reply(client, *arguments, **options):
    """The service receives the request; its answer never arrives."""
    client.through("submit_transfer", *arguments, **options)
    raise FakeConnectionError("ConnectionError on request")


def received_then(error):
    def answer(client, *arguments, **options):
        client.through("submit_transfer", *arguments, **options)
        raise error

    return answer


def refused_sign_in(app, parameters):
    raise FakeLoginError("Login timed out. Please try again.")


REFUSED_SIGN_IN = "Signing in to Globus did not work: Login timed out. Please try again."


def capture_flows(test):
    """Every GlobusFlow made while the test runs, so that a test can reach its worker context."""
    flows = []
    real = arc_globus.GlobusFlow.__init__

    def init(flow, *arguments, **options):
        real(flow, *arguments, **options)
        flows.append(flow)

    patcher = mock.patch.object(arc_globus.GlobusFlow, "__init__", init)
    patcher.start()
    test.addCleanup(patcher.stop)
    return flows


def active_task(task_id, **values):
    task = {"task_id": task_id, "status": "ACTIVE", "nice_status": "OK", "nice_status_short_description": None,
            "files": 18, "files_transferred": 3, "files_skipped": 0, "bytes_transferred": 30000, "faults": 0,
            "fatal_error": None, "deadline": "2026-11-16T16:00:09+00:00", "completion_time": None,
            "is_paused": False, "owner_id": "never-kept"}
    task.update(values)
    return task


class Clock:
    """A clock that moves `step` seconds every time it is read."""

    def __init__(self, step):
        self.now, self.step = 1000.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def files_below(folder):
    return sorted(path.relative_to(folder).as_posix() for path in Path(folder).rglob("*") if path.is_file())


def same_as_manifest(test, folder):
    """The final folder holds exactly the files of the manifest (SHA-256), the manifest and SHA256SUMS."""
    manifest = json.loads((Path(folder) / arc_payload.MANIFEST_NAME).read_text(encoding="utf-8"))
    expected = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    test.assertEqual(set(files_below(folder)), set(expected) | {arc_payload.MANIFEST_NAME, arc_payload.SUMS_NAME})
    for path, digest in expected.items():
        test.assertEqual(arc_payload.hash_file(Path(folder) / path)[1], digest, path)


# ---------------------------------------------------------------------- shared set-up


class GlobusCase(arc_support.ArcCase):
    """A live Globus upload of a measured session whose worker runs in this process with a FakeSDK."""

    pace = 0

    def setUp(self):
        super().setUp()
        arc_support.write_settings(self.home, {"demo": False, "globus": {"mac_collection": MAC_COLLECTION}})
        arc_support.ready_session(self.home, simulated=False)
        self.arc = self.home / "fake_arc"
        self.server_clock = [time.time()]
        self.sdk = FakeSDK(self.arc, self.home, pace=self.pace, clock=lambda: self.server_clock[0])
        for target, name, value in (
                (arc_commands, "setup_errors", mock.Mock(return_value=[])),
                (arc_globus, "make_client", lambda ctx: arc_globus.LiveGlobus(ctx.settings_used, sdk=self.sdk.module)),
                (arc_globus, "RESEND_DELAYS", (0, 0)),
                (arc_globus, "POLL_SCHEDULE", ((None, 0),)),
                (arc_globus, "SIGN_IN_POLL_SECONDS", 0.005),
                (arc_globus, "STOP_POLL_SECONDS", 0),
                # A safety net: a watch that never ends stops after 60 s (the tests with a moving
                # clock set the design's 90 minutes again).
                (arc_globus, "WATCH_LIMIT_SECONDS", 60),
                (arc_worker, "STOP_POLL_SECONDS", 0.02)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.upload_id = self.upload("--live")["upload"]["upload_id"]
        self.folder = self.record_dir(self.upload_id)

    def work(self, action="send"):
        with contextlib.redirect_stderr(io.StringIO()):
            return arc_worker.run_worker(self.home, self.upload_id, action)

    def state(self):
        record = self.record(self.upload_id)
        return record["state"], (record.get("error") or {}).get("reason")

    def final(self, name=SESSION):
        return self.arc / "projects" / "pcl_photonics" / "raman_globus_upload" / name

    def incoming(self):
        return self.final("_incoming") / ("%s.%s" % (SESSION, self.upload_id))

    def app(self, index=-1):
        return self.sdk.apps[index]

    def shown(self):
        return self.ok("arc-status", "--upload=" + self.upload_id)["upload"]


# ---------------------------------------------------------------------- the live client and its sign-in


class LiveClient(GlobusCase):
    def test_a_live_upload_is_checked_with_globus_checksums_and_put_in_place(self):
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertEqual((record["verification"]["method"], record["verification"]["algorithm"]),
                         ("globus-checksum", "MD5"))
        files = record["payload"]["files"]
        self.assertEqual(record["message"], "On ARC: %d files in %s/%s. Globus checked every file, and its MD5 "
                                            "checksums match this Mac's copy." % (files, ARC_FOLDER, SESSION))
        self.assertEqual(record["destination"]["placed_as"], SESSION)
        same_as_manifest(self, self.final())
        self.assertEqual(os.listdir(self.final("_incoming")), [])
        self.assertEqual(record["globus"]["identity"], IDENTITY)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["accepted"])
        self.assertEqual(set(record["globus"]["task"]), set(arc_globus.TASK_FIELDS) | {"checked_utc"})
        self.assertNotIn("owner_id", json.dumps(record))
        self.assertFalse((self.folder / "payload").exists())
        self.assertEqual(len(self.sdk.server.task_list()), 1)

    def test_the_sign_in_is_set_up_as_design_10_2_says(self):
        self.work()
        config = self.sdk.configs[0]
        self.assertEqual(set(config.values), {"login_flow_manager", "token_storage", "request_refresh_tokens",
                                              "auto_redrive_gares", "token_validation_error_handler"})
        self.assertEqual((config.token_storage, config.request_refresh_tokens, config.auto_redrive_gares,
                          config.token_validation_error_handler), ("memory", False, False, None))
        manager = config.login_flow_manager
        self.assertEqual(manager.__name__, "QuietLocalServerLoginFlowManager")
        app = self.app()
        self.assertEqual((app.app_name, app.client_id, app.config), (arc_globus.APP_NAME,
                                                                     "61338d24-54d5-408f-a10d-66c06b59f6d2", config))
        self.assertIn({"auth.globus.org": ["openid", "profile"]}, app.scopes)
        self.assertEqual(self.sdk.auths[0].app_scopes, ["openid", "profile"])
        self.assertIs(self.sdk.transfers[0].app, app)
        # One browser sign-in, without prompt=login; nothing signs in by itself after it.
        self.assertEqual(app.logins, [None])
        flow = manager.for_globus_app(app_name="ignored", login_client="a login client", config=config)
        self.assertEqual((flow.native_prefill_named_grant, flow.server_address, flow.request_refresh_tokens),
                         ("Portable Raman ARC upload", ("127.0.0.1", 0), False))
        import socket

        self.assertNotIn(socket.gethostname(), repr(vars(flow)))
        handler = self.sdk.module.login_flows.local_server_login_flow_manager.local_server.RedirectHandler
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            handler().log_message('"%s" %s %s', "GET /?code=not-a-real-code&state=x HTTP/1.1", "200", "-")
        self.assertEqual(errors.getvalue(), "")

    def test_force_login_asks_for_the_sign_in_every_time(self):
        arc_records.update_record(self.folder, lambda record: record["settings_used"].update(force_login=True))
        self.work()
        (parameters,) = self.app().logins
        self.assertEqual(parameters.values, {"prompt": "login"})

    def test_no_file_holds_a_token_after_a_sign_in(self):
        user_folder = Path(support.make_home(self))
        with mock.patch.dict(os.environ, {"HOME": str(user_folder)}):
            self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.app().tokens, {})
        checked = 0
        for folder in (self.home, user_folder):
            for path in folder.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes()
                    checked += 1
                    for token in TOKENS:
                        self.assertNotIn(token.encode(), data, path)
        self.assertGreater(checked, 20)
        self.assertEqual(list(user_folder.iterdir()), [])

    def test_a_worker_whose_sign_in_never_ends_still_exits(self):
        """The sign-in thread is left waiting (as the SDK's loopback listener waits for its browser); the
        worker process records signin_timeout and exits all the same, which closes the listener."""
        program = (
            "import sys, threading\n"
            "sys.path.insert(0, sys.argv[3])\n"
            "import test_arc_globus as tests\n"
            "from pathlib import Path\n"
            "from raman_datafed import arc_globus, arc_worker\n"
            "sdk = tests.FakeSDK(Path(sys.argv[1]) / 'fake_arc', Path(sys.argv[1]))\n"
            "sdk.login_behaviour = lambda app, parameters: threading.Event().wait()\n"
            "arc_globus.make_client = lambda ctx: arc_globus.LiveGlobus(ctx.settings_used, sdk=sdk.module)\n"
            "arc_globus.SIGN_IN_LIMIT_SECONDS = 1\n"
            "sys.exit(arc_worker.main(['--home', sys.argv[1], '--upload', sys.argv[2], '--action', 'send']))\n")
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        started = time.monotonic()
        done = subprocess.run([sys.executable, "-c", program, str(self.home), self.upload_id,
                               str(Path(__file__).resolve().parent)], capture_output=True, text=True, timeout=60,
                              cwd=str(self.home), env=env)
        self.assertEqual(done.returncode, arc_worker.EXIT_ENDED, done.stderr)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(self.state(), ("failed", "signin_timeout"))
        self.assertTrue(arc_support.lock_is_free(self.folder / "worker.lock"))

    def test_every_token_is_revoked_and_everything_closed_once_on_every_path(self):
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        endings = [self.app()]

        def refused(app, parameters):
            raise FakeLoginError("Login timed out. Please try again.")

        for script, login, revocation_fails, expected in (
                ({"submit_transfer": [FakeTransferAPIError(400, "BadRequest", "no")]}, None, True, "refused_by_globus"),
                ({"operation_stat": [TypeError("a program error")]}, None, False, "internal"),
                ({}, refused, False, "signin_failed")):
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]
            self.sdk.script, self.sdk.login_behaviour = script, login
            # A revocation that fails changes nothing for the person: the end is written all the same.
            self.sdk.logout_error = RuntimeError("revocation failed") if revocation_fails else None
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
            self.assertEqual(self.state(), ("failed", expected))
            endings.append(self.app())
        for app in endings:
            self.assertEqual((app.close_calls, app.tokens), (1, {}))
        for client in self.sdk.transfers + self.sdk.auths + self.sdk.revokers:
            self.assertEqual(client.closed, 1)
        # Every token was revoked, but those of the upload whose revocation failed.
        self.assertEqual(len(self.sdk.unrevoked()), 2)
        self.assertEqual(len(self.sdk.issued), 6)

    def test_sign_in_errors_keep_their_own_words(self):
        def times_out(app, parameters):
            raise FakeLoginError("Login timed out. Please try again.")

        self.sdk.login_behaviour = times_out
        self.work()
        record = self.record(self.upload_id)
        self.assertEqual(record["error"]["reason"], "signin_failed")
        self.assertEqual(record["error"]["message"],
                         "Signing in to Globus did not work: Login timed out. Please try again.")
        self.assertTrue(record["error"]["retryable"])

        def no_browser(app, parameters):
            raise FakeEnvironmentalLoginError("Cannot use LocalServerLoginFlowManager in a remote session")

        self.sdk.login_behaviour = no_browser
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.work()
        self.assertEqual(self.state(), ("failed", "no_browser"))

    def test_a_sign_in_that_quotes_a_code_is_not_kept(self):
        def quotes(app, parameters):
            raise FakeLoginError("GET /?code=not-a-real-code&state=abc failed")

        self.sdk.login_behaviour = quotes
        self.work()
        record = self.record(self.upload_id)
        self.assertNotIn("code=", json.dumps(record))
        self.assertEqual(record["error"]["reason"], "signin_failed")

    def test_a_sign_in_longer_than_10_minutes_ends_the_worker(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.sdk.login_behaviour = lambda app, parameters: release.wait(30)
        started = time.monotonic()
        with mock.patch.object(arc_globus, "_clock", Clock(100.0)):
            code = self.work()
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                         ("failed", "signin_timeout", True))
        self.assertIsNone(record["worker"])
        self.assertEqual(self.sdk.calls("submit_transfer"), [])
        self.assertEqual((self.app().close_calls, self.sdk.issued, self.sdk.revokers[-1].closed), (1, [], 1))

    def test_every_token_of_a_consent_sign_in_is_revoked(self):
        """Review GLO-04: a consent sign-in replaces the stored tokens; the first pair is revoked too."""
        self.sdk.script = {"operation_stat": [consent_error()]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(self.app().logins), 2)
        self.assertEqual(len(self.sdk.issued), 4)
        self.assertEqual(self.sdk.unrevoked(), [])
        self.assertEqual(self.app().tokens, {})

    def test_tokens_are_revoked_with_one_short_try_each(self):
        """Review GLO-V02: no SDK resends and a short timeout, so the end is never held up for minutes."""
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.sdk.unrevoked(), [])
        self.assertEqual(self.sdk.revoke_timeouts, [(0, arc_globus.REVOKE_TIMEOUT_SECONDS)] * 2)
        self.assertLessEqual(arc_globus.REVOKE_TIMEOUT_SECONDS, 10)

    def test_globus_auth_out_of_reach_does_not_hold_up_the_end(self):
        self.sdk.logout_error = FakeConnectionError("no route to Globus Auth")
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        # The first revocation found no network: the second one is not tried.
        self.assertEqual(len(self.sdk.revoke_timeouts), 1)
        self.assertEqual((self.record(self.upload_id)["state"], self.app().tokens), ("verified", {}))

    def test_a_denied_sign_in_keeps_its_own_reason(self):
        """Review GLO-06: the SDK's words of a sign-in that came back with an error are kept."""
        def denied(app, parameters):
            raise FakeLoginError("Authorization failed with unexpected error:\nThe user denied consent.")

        self.sdk.login_behaviour = denied
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        self.assertEqual(self.record(self.upload_id)["error"]["message"],
                         "Signing in to Globus did not work: Authorization failed with unexpected error: The user "
                         "denied consent.")

    def test_messages_keep_globus_s_words_and_lose_only_secrets(self):
        kept = ("Authorization failed with unexpected error:\nThe user denied consent.",
                "Error: 530 Authorization denied for user",
                "Invalid request: state=ACTIVE",
                "quota exceeded: error code=122 (EDQUOT)")
        for text in kept:
            self.assertEqual(arc_globus._clean(text), " ".join(text.split()).rstrip("."), text)
        token = "Ag" + "x7Kp2" * 9
        for text in ("GET /?code=not-a-real-code-0123456789&state=abc HTTP/1.1 failed",
                     "Authorization: Bearer %s" % token,
                     '{"access_token": "%s"}' % token,
                     "the token %s was refused" % token,
                     "code=%s" % ("q" * 30)):
            cleaned = arc_globus._clean(text)
            self.assertIn(arc_globus.REMOVED, cleaned, text)
            for secret in (token, "code=", "q" * 16, "not-a-real-code"):
                self.assertNotIn(secret, cleaned, text)

    def test_a_record_changed_by_hand_is_refused_before_any_sign_in(self):
        """Design 20 (S-13, S-V01), review SEC-07: the record's values are checked again, as SSH does."""
        edits = (
            lambda record: record["destination"].update(globus_incoming_path="/somebody_else/inbox/x"),
            lambda record: record["destination"].update(globus_base="/somebody_else"),
            lambda record: record["destination"].update(base="/projects/elsewhere/raman"),
            lambda record: record["settings_used"].update(arc_collection="not-a-collection"),
            lambda record: record["settings_used"].update(mac_collection=ARC_COLLECTION),
            lambda record: record["settings_used"].update(collection_root="/projects/../x"),
            lambda record: record["destination"].update(placed_as="../elsewhere"),
        )
        for edit in edits:
            arc_records.update_record(self.folder, edit)
            apps = len(self.sdk.apps)
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
            record = self.record(self.upload_id)
            self.assertEqual(record["error"]["reason"], "internal")
            self.assertIn("RecordValueError", record["error"]["message"])
            self.assertEqual(len(self.sdk.apps), apps, "nothing signs in with a changed record")
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]
            self.folder = self.record_dir(self.upload_id)
        self.assertEqual(self.sdk.calls("submit_transfer"), [])

    def test_the_session_folder_and_the_upload_copy_are_checked_too(self):
        context = types.SimpleNamespace(session_folder=SESSION, upload_id=self.upload_id, record_dir=self.folder,
                                        payload_dir=self.folder / "payload" / SESSION,
                                        settings_used=self.record(self.upload_id)["settings_used"])
        arc_globus.check_record(context, self.record(self.upload_id))
        for name in ("../../../x", "simulated", ""):
            with self.assertRaises(arc_globus.RecordValueError):
                arc_globus.check_record(types.SimpleNamespace(**dict(vars(context), session_folder=name)),
                                        self.record(self.upload_id))
        with self.assertRaises(arc_globus.RecordValueError):
            arc_globus.check_record(types.SimpleNamespace(**dict(vars(context), upload_id="u000000000000")),
                                    self.record(self.upload_id))
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (self.folder / "payload").mkdir()
        os.symlink(elsewhere, self.folder / "payload" / SESSION)
        with self.assertRaises(arc_globus.RecordValueError):
            arc_globus.check_record(context, self.record(self.upload_id))

    def test_the_guard_refuses_the_live_client_without_a_stand_in(self):
        with self.assertRaises(arc_worker.GuardRefused):
            arc_globus.LiveGlobus({"client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2"})

    def test_a_live_worker_under_the_guard_never_reaches_globus(self):
        with mock.patch.object(arc_globus, "make_client", REAL_MAKE_CLIENT):
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["error"]["reason"], "internal")
        self.assertIn("GuardRefused", record["error"]["message"])

    def test_sdk_errors_become_globus_call_errors(self):
        client = arc_globus.LiveGlobus({"client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2"}, sdk=self.sdk.module)
        for raised, expected in ((FakeTransferAPIError(404, "ClientError.NotFound", "missing"),
                                  (404, "ClientError.NotFound", "missing", None)),
                                 (FakeConnectionError("ConnectionError on request"),
                                  (0, "network", "FakeConnectionError", None)),
                                 (TimeoutError("slow"), (0, "network", "TimeoutError", None)),
                                 (FakeTokenValidationError("expired"), (401, "TokenValidationError",
                                                                        "The sign-in is no longer valid", None))):
            self.sdk.script = {"get_task": [raised]}
            with self.assertRaises(GlobusCallError) as caught:
                client.get_task("t")
            error = caught.exception
            self.assertEqual((error.http_status, error.code, error.message, error.gare), expected)
        consent = consent_error()
        self.sdk.script = {"get_task": [consent]}
        with self.assertRaises(GlobusCallError) as caught:
            client.get_task("t")
        self.assertIs(caught.exception.gare, consent.fake_gare)
        self.sdk.script = {"get_task": [KeyError("a program error")]}
        with self.assertRaises(KeyError):
            client.get_task("t")


# ---------------------------------------------------------------------- checking (design 10.3)


class FakeFlowContext:
    """Just enough of a WorkerContext for GlobusFlow's listing helpers."""

    demo = False
    action = "send"
    settings_used = {"collection_root": "/projects", "mac_collection": MAC_COLLECTION}

    def stop_requested(self):
        return False


class Checking(GlobusCase):
    def listing(self, change):
        """operation_ls answers of the upload copy, changed by `change(entries)` (the others let through)."""

        def answer(client, collection, path, **options):
            response = client.through("operation_ls", collection, path, **options)
            if collection == MAC_COLLECTION and path.rstrip("/").endswith("/payload/" + SESSION):
                change(response.data["DATA"])
            return response

        self.sdk.script = {"operation_ls": [answer] * 5}

    def test_listings_are_read_page_by_page(self):
        names = ["point_%04d.json" % index for index in range(2500)]

        class Pages:
            def __init__(self):
                self.asked = []

            def ls(self, collection, path, offset, limit):
                self.asked.append((offset, limit))
                return [{"name": name, "type": "file", "size": 1} for name in names[offset:offset + limit]]

        client = Pages()
        flow = arc_globus.GlobusFlow(FakeFlowContext(), client)
        entries = flow.list_folder(MAC_COLLECTION, "/x/")
        self.assertEqual([entry["name"] for entry in entries], names)
        self.assertEqual(client.asked, [(0, 1000), (1000, 1000), (2000, 1000)])

    def test_a_name_listed_twice_is_listing_truncated(self):
        self.listing(lambda entries: entries.append(dict(entries[0])))
        self.work()
        self.assertEqual(self.state(), ("failed", "listing_truncated"))
        self.assertEqual(self.sdk.calls("submit_transfer"), [])

    def test_directory_size_limit_is_listing_truncated(self):
        error = FakeTransferAPIError(502, "ExternalError.DirListingFailed.DirectorySizeLimit", "too many")
        self.sdk.script = {"operation_ls": [error]}
        self.work()
        self.assertEqual(self.state(), ("failed", "listing_truncated"))

    def test_more_entries_than_globus_lists_is_listing_truncated(self):
        with mock.patch.object(arc_globus, "MAX_ENTRIES", 5):
            self.work()
        self.assertEqual(self.state(), ("failed", "listing_truncated"))

    def test_what_globus_sees_must_equal_the_upload_copy(self):
        def link(entries):
            entries[0]["link_target"] = "/elsewhere"

        def size(entries):
            entries[-1]["size"] += 1

        def hidden(entries):
            entries.append({"name": ".DS_Store", "type": "file", "size": 6148, "link_target": None})

        for change, named in ((link, None), (size, None), (hidden, ".DS_Store")):
            self.listing(change)
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
            record = self.record(self.upload_id)
            self.assertEqual(record["error"]["reason"], "source_mismatch")
            self.assertFalse(record["error"]["retryable"])
            self.assertIn("1 differ, for example", record["error"]["message"])
            if named:
                self.assertIn(named, record["error"]["message"])
            self.assertEqual(self.sdk.calls("submit_transfer"), [])
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]

    def test_a_copy_globus_cannot_read_is_source_not_visible(self):
        for error, reason in ((FakeTransferAPIError(404, "ClientError.NotFound", "no such folder"),
                               "source_not_visible"),
                              (FakeTransferAPIError(403, "EndpointPermissionDenied", "no"), "source_not_visible"),
                              (FakeTransferAPIError(502, "ExternalError.DirListingFailed.GCDisconnected", "off"),
                               "gcp_not_connected")):
            self.sdk.script = {"operation_ls": [error]}
            self.work()
            record = self.record(self.upload_id)
            self.assertEqual(record["error"]["reason"], reason)
            if reason == "source_not_visible":
                self.assertIn(str((self.folder / "payload" / SESSION).resolve()), record["error"]["message"])
                self.ok("arc-retry", "--upload=" + self.upload_id)
            else:
                self.ok("arc-retry", "--upload=" + self.upload_id)
        self.sdk.script = {}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)

    def test_this_mac_s_collection_must_be_connected_and_known(self):
        for answer, reason in ((lambda client, collection: {"gcp_connected": False, "gcp_paused": False},
                                "gcp_not_connected"),
                               (lambda client, collection: {"gcp_connected": True, "gcp_paused": True},
                                "gcp_not_connected"),
                               (FakeTransferAPIError(404, "ClientError.NotFound", "unknown"),
                                "mac_collection_unknown")):
            self.sdk.script = {"get_endpoint": [answer]}
            self.work()
            self.assertEqual(self.state(), ("failed", reason))
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]

    def test_a_collection_this_identity_may_not_read_is_another_account_s(self):
        """Review DOC-02: Globus Connect Personal serves only the Globus account that set it up; another
        identity gets 403 for the collection, and its own reason, not 'copy the ID again'."""
        self.sdk.script = {"get_endpoint": [FakeTransferAPIError(403, "PermissionDenied", "not allowed")]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        error = self.record(self.upload_id)["error"]
        self.assertEqual((error["reason"], error["retryable"]), ("mac_collection_not_yours", False))
        self.assertEqual(error["message"], "The Globus collection in ARC settings belongs to another Globus account, "
                                           "so %s cannot send from it." % IDENTITY)
        self.assertIn("send with SSH", error["fix"])
        self.assertEqual(self.sdk.calls("submit_transfer"), [])

    def test_a_refused_listing_of_a_collection_another_identity_owns(self):
        other = "0f5e8c1a-0000-4000-8000-00000000bbbb"
        for owner, reason in ((other, "mac_collection_not_yours"), (self.sdk.identity_id, "source_not_visible")):
            def endpoint(client, collection, owner=owner):
                return {"DATA_TYPE": "endpoint", "id": collection, "gcp_connected": True, "gcp_paused": False,
                        "owner_id": owner}

            self.sdk.script = {"get_endpoint": [endpoint],
                               "operation_ls": [FakeTransferAPIError(403, "EndpointPermissionDenied", "no")]}
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
            self.assertEqual(self.state(), ("failed", reason))
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]

    def test_the_consent_window_counts_from_the_end_of_the_first_sign_in(self):
        """Review GLO-01: a first sign-in that took most of the window still leaves time for the consent."""
        now = [1000.0]

        def slow_first_sign_in(app, parameters):
            if parameters is None:
                now[0] += 150

        self.sdk.login_behaviour = slow_first_sign_in
        self.sdk.script = {"operation_stat": [consent_error()]}
        with mock.patch.object(arc_globus, "_clock", lambda: now[0]), \
                mock.patch.object(arc_globus, "CONSENT_WINDOW_SECONDS", 100):
            self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(self.app().logins), 2)

    def test_the_folders_on_arc_are_made_from_the_deepest_existing_one(self):
        def lost(client, collection, path):
            client.through("operation_mkdir", collection, path)
            raise FakeTransferAPIError(502, "ExternalError.MkdirFailed.Exists", "Path already exists")

        self.sdk.script = {"operation_mkdir": [lost, lost]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        made = [call[1][1] for call in self.sdk.calls("operation_mkdir")]
        self.assertEqual(made, ["/pcl_photonics", GLOBUS_BASE, GLOBUS_BASE + "/_incoming",
                                "%s/_incoming/%s.%s" % (GLOBUS_BASE, SESSION, self.upload_id)])
        stats = [call[1][1] for call in self.sdk.calls("operation_stat")][:3]
        self.assertEqual(stats, [GLOBUS_BASE, "/pcl_photonics", "/"])
        for call in self.sdk.calls("operation_mkdir") + self.sdk.calls("operation_stat"):
            self.assertFalse(call[1][1].startswith("/projects"), call)

    def test_a_file_where_the_arc_folder_should_be_is_arc_folder_not_dir(self):
        (self.arc / "projects" / "pcl_photonics").mkdir(parents=True)
        self.final("").with_name("raman_globus_upload").write_text("a file", encoding="utf-8")
        self.work()
        record = self.record(self.upload_id)
        self.assertEqual(record["error"]["reason"], "arc_folder_not_dir")
        self.assertIn(ARC_FOLDER, record["error"]["message"])

    def test_no_permission_on_arc_is_destination_permission(self):
        self.sdk.script = {"operation_stat": [FakeTransferAPIError(403, "PermissionDenied", "no")]}
        self.work()
        record = self.record(self.upload_id)
        self.assertEqual((record["error"]["reason"], record["error"]["retryable"]), ("destination_permission", True))
        self.assertIn(GLOBUS_BASE, record["error"]["message"])

    def test_a_folder_of_that_name_on_arc_is_a_conflict_before_anything_is_sent(self):
        self.final().mkdir(parents=True)
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["conflict_at"], record["error"]), ("conflict", "checking", None))
        self.assertEqual(record["message"], "ARC already has a folder named %s in %s. Nothing was sent."
                         % (SESSION, ARC_FOLDER))
        self.assertEqual((self.sdk.calls("get_submission_id"), self.sdk.calls("submit_transfer")), ([], []))
        self.assertFalse(self.incoming().exists())
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.record(self.upload_id)["destination"]["placed_as"], SESSION + "_copy2")
        same_as_manifest(self, self.final(SESSION + "_copy2"))
        self.assertEqual(os.listdir(self.final()), [])

    def test_a_consent_asked_for_while_checking_signs_in_once_more(self):
        self.sdk.script = {"operation_stat": [consent_error()]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        logins = self.app().logins
        self.assertEqual(len(logins), 2)
        self.assertEqual(logins[1].required_scopes, ["urn:globus:auth:scope:transfer.api.globus.org:all"])
        self.assertEqual(len(self.sdk.calls("operation_stat")) >= 2, True)

    def test_a_second_consent_is_a_failure_named_consent(self):
        self.sdk.script = {"operation_stat": [consent_error(), consent_error()]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["error"]["reason"], record["error"]["retryable"]), ("consent", True))
        self.assertEqual(len(self.app().logins), 2)


# ---------------------------------------------------------------------- submitting (design 10.4)


def _path_of(handle):
    try:
        return fcntl.fcntl(handle, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()
    except (OSError, AttributeError):
        return None


def fsynced_paths(test, durable=None):
    """Record the path of every file and folder synced while the test runs (macOS F_GETPATH): os.fsync,
    and arc_records.durable_sync (F_FULLFSYNC, decision I5). `durable`, a list, gets the paths that
    durable_sync reached."""
    seen = []
    real_fsync, real_durable = os.fsync, arc_records.durable_sync

    def fsync(handle):
        seen.append(_path_of(handle))
        return real_fsync(handle)

    def durable_sync(handle):
        path = _path_of(handle)
        seen.append(path)
        if durable is not None:
            durable.append(path)
        return real_durable(handle)

    for target, name, value in ((os, "fsync", fsync), (arc_records, "durable_sync", durable_sync)):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)
    return seen


class Submitting(GlobusCase):
    def body(self):
        return json.loads((self.folder / arc_globus.REQUEST_FILE).read_text(encoding="utf-8"))

    def test_the_request_body_field_by_field(self):
        self.work()
        record = self.record(self.upload_id)
        body = self.body()
        globus = record["globus"]
        self.assertEqual(list(body), ["DATA_TYPE", "submission_id", "source_endpoint", "destination_endpoint", "label",
                                      "verify_checksum", "encrypt_data", "preserve_timestamp",
                                      "delete_destination_extra", "fail_on_quota_errors", "skip_source_errors",
                                      "deadline", "notify_on_succeeded", "notify_on_failed", "DATA"])
        self.assertEqual((body["DATA_TYPE"], body["submission_id"], body["source_endpoint"],
                          body["destination_endpoint"]), ("transfer", globus["submission_id"], MAC_COLLECTION,
                                                          ARC_COLLECTION))
        self.assertEqual(body["label"], "Raman upload %s a1 %s" % (self.upload_id, SESSION))
        self.assertEqual(globus["label"], body["label"])
        self.assertEqual((body["verify_checksum"], body["encrypt_data"], body["preserve_timestamp"],
                          body["delete_destination_extra"], body["fail_on_quota_errors"], body["skip_source_errors"],
                          body["notify_on_succeeded"], body["notify_on_failed"]),
                         (True, True, True, False, True, False, False, True))
        self.assertNotIn("notify_on_inactive", body)
        self.assertNotIn("sync_level", body)
        saved = time.strptime(globus["request_saved_utc"][:19], "%Y-%m-%dT%H:%M:%S")
        deadline = time.strptime(body["deadline"], "%Y-%m-%dT%H:%M:%S+00:00")
        self.assertEqual(time.mktime(deadline) - time.mktime(saved), 24 * 3600)
        (item,) = body["DATA"]
        self.assertEqual(item, {"DATA_TYPE": "transfer_item",
                                "source_path": str((self.folder / "payload" / SESSION).resolve()) + "/",
                                "destination_path": "%s/_incoming/%s.%s/" % (GLOBUS_BASE, SESSION, self.upload_id),
                                "recursive": True})
        self.assertNotIn("/projects", item["destination_path"])
        self.assertEqual(oct(os.stat(self.folder / arc_globus.REQUEST_FILE).st_mode & 0o777), "0o600")
        (posted,) = self.sdk.calls("submit_transfer")
        self.assertEqual(posted[1][0], body)

    def test_the_label_is_cut_to_128_characters(self):
        label = arc_globus.transfer_label("u3f9a0c1d2e4b", 12, "x" * 127)
        self.assertEqual(len(label), 128)
        self.assertTrue(label.startswith("Raman upload u3f9a0c1d2e4b a12 xxx"))
        self.assertEqual(arc_globus.transfer_label("u3f9a0c1d2e4b", 1, "a b"), "Raman upload u3f9a0c1d2e4b a1 a b")

    def test_the_request_is_on_disk_before_it_is_posted(self):
        durable = []
        seen = fsynced_paths(self, durable)
        posted = []

        def check(client, data):
            record = arc_records.read_record(self.folder)
            saved = json.loads((self.folder / arc_globus.REQUEST_FILE).read_text(encoding="utf-8"))
            self.assertEqual(record["globus"]["submission_id"], data["submission_id"])
            self.assertEqual(record["globus"]["label"], data["label"])
            self.assertEqual(saved, data)
            folder = str(self.folder.resolve())
            request = [index for index, path in enumerate(seen) if path and arc_globus.REQUEST_FILE in path]
            record_writes = [index for index, path in enumerate(seen) if path and "record.json" in path]
            folder_syncs = [index for index, path in enumerate(seen) if path == folder]
            self.assertTrue(request, seen)
            self.assertTrue(any(index > request[-1] for index in folder_syncs), seen)
            last_record = max(index for index in record_writes if index > request[-1])
            self.assertTrue(any(index > last_record for index in folder_syncs), seen)
            # Decision I5: on macOS the request, the record and their folder reach the drive itself
            # (F_FULLFSYNC), not only its cache, before every POST.
            self.assertTrue(any(path and arc_globus.REQUEST_FILE in path for path in durable), durable)
            self.assertTrue(any(path and "record.json" in path for path in durable), durable)
            self.assertIn(folder, durable)
            posted.append(data["submission_id"])
            return client.through("submit_transfer", data)

        self.sdk.script = {"submit_transfer": [check]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(posted), 1)

    def test_a_lost_answer_is_asked_again_with_the_same_body(self):
        shown = []

        def look(client, task_id):
            shown.append(arc_records.read_record(self.folder)["message"])
            return client.through("get_task", task_id)

        self.sdk.script = {"submit_transfer": [lost_reply], "get_task": [look]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(shown, ["Globus answered: already received. One copy."])
        posts = self.sdk.calls("submit_transfer")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][1], posts[1][1])
        self.assertEqual(len(self.sdk.calls("get_submission_id")), 1)
        record = self.record(self.upload_id)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["unknown", "duplicate"])
        self.assertEqual(len(self.sdk.server.task_list()), 1)
        self.assertEqual(self.sdk.server.posts_of(record["globus"]["submission_id"]), 2)
        texts = [event["text"] for event in arc_records.read_events(self.folder, last=0)]
        self.assertIn(arc_globus.TEXTS["unknown_post"], texts)
        self.assertIn("Globus answered: already received. One copy.", texts)

    def test_three_lost_answers_are_uncertain_and_retry_sends_the_same_body(self):
        self.sdk.script = {"submit_transfer": [FakeConnectionError("x"), FakeTransferAPIError(503, "ServiceUnavailable",
                                                                                              "busy"),
                                               lambda client, data: {"code": "Accepted"}]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "uncertain")
        self.assertEqual(record["message"], arc_globus.TEXTS["uncertain"])
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["unknown"] * 3)
        self.assertEqual(self.shown()["actions"], ["retry", "check", "stop"])
        self.assertTrue(self.ok("arc-retry", "--upload=" + self.upload_id)["same_submission"])
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        bodies = [call[1][0] for call in self.sdk.calls("submit_transfer")]
        self.assertEqual(len(bodies), 4)
        self.assertTrue(all(body == bodies[0] for body in bodies))
        self.assertEqual(len(self.sdk.calls("get_submission_id")), 1)

    def test_a_definite_refusal_is_never_uncertain(self):
        for error in (FakeTransferAPIError(400, "BadRequest", "Invalid transfer document"),
                      FakeTransferAPIError(403, "PermissionDenied", "Quota exceeded")):
            self.sdk.script = {"submit_transfer": [error]}
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
            record = self.record(self.upload_id)
            self.assertEqual((record["state"], record["error"]["reason"], record["error"]["retryable"]),
                             ("failed", "refused_by_globus", True))
            self.assertEqual(record["error"]["message"], "Globus refused the request: %s: %s. Nothing was copied."
                             % (error.code, error.message))
            self.assertEqual(len(self.sdk.calls("task_list")), 0)
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]
            self.folder = self.record_dir(self.upload_id)

    def test_conflict_or_a_refused_resend_looks_for_the_task_by_its_label(self):
        cases = (
            ([received_then(FakeTransferAPIError(409, "Conflict", "running"))], "verified", None),
            ([FakeTransferAPIError(409, "Conflict", "An existing transfer has not yet completed")], "failed",
             "request_conflict"),
            ([lost_reply, FakeTransferAPIError(400, "BadRequest", "resend")], "verified", None),
            ([FakeConnectionError("x"), FakeTransferAPIError(403, "PermissionDenied", "resend")], "failed",
             "refused_by_globus"),
        )
        for script, state, reason in cases:
            self.sdk.script = {"submit_transfer": list(script)}
            self.work()
            self.assertEqual(self.state(), (state, reason), script)
            if reason == "request_conflict":
                self.assertEqual(self.record(self.upload_id)["error"]["message"],
                                 "Globus is still running another transfer of these files to the same folder.")
            self.assertTrue(self.sdk.calls("task_list"))
            for call in self.sdk.calls("task_list"):
                self.assertEqual(call[2]["filter"], {"label": self.record(self.upload_id)["globus"]["label"]})
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]
            for transfer in self.sdk.transfers:
                transfer.calls.clear()

    def test_several_tasks_with_the_label_is_internal(self):
        def two(client, **options):
            task = {"task_id": "6f1d2c3b-0000-4000-8000-0000000000bb", "label": options["filter"]["label"]}
            return {"DATA": [task, dict(task, task_id="6f1d2c3b-0000-4000-8000-0000000000cc")]}

        self.sdk.script = {"submit_transfer": [FakeTransferAPIError(409, "Conflict", "running")], "task_list": [two]}
        self.work()
        self.assertEqual(self.state(), ("failed", "internal"))

    def test_a_consent_asked_for_at_the_post_signs_in_and_sends_the_same_body(self):
        self.sdk.script = {"submit_transfer": [consent_error()]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        bodies = [call[1][0] for call in self.sdk.calls("submit_transfer")]
        self.assertEqual((len(bodies), bodies[0] == bodies[1]), (2, True))
        self.assertEqual(len(self.app().logins), 2)

    def test_the_sdk_does_not_send_the_transfer_request_again_by_itself(self):
        """Review GLO-03: the SDK's own resends were invisible to the exactly-once bookkeeping. With them off,
        every POST is one the flow counts: a refusal after a lost answer looks the task up by its label."""
        self.sdk.resends = True
        self.sdk.script = {"submit_transfer": [lost_reply, FakeTransferAPIError(400, "BadRequest", "resent")]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual([call[3] for call in self.sdk.calls("submit_transfer")], [0, 0])
        record = self.record(self.upload_id)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["unknown", "refused"])
        self.assertEqual(len(self.sdk.server.task_list()), 1)
        # The other calls keep the SDK's resends.
        self.assertTrue(all(call[3] == 5 for call in self.sdk.calls("get_task")))

    def test_busy_answers_are_asked_again_like_lost_ones(self):
        self.assertTrue(arc_globus._no_answer(GlobusCallError(429, "TooManyRequests", "slow down")))
        self.assertTrue(arc_globus._no_answer(GlobusCallError(408, "RequestTimeout", "slow")))
        self.assertFalse(arc_globus._no_answer(GlobusCallError(400, "BadRequest", "no")))
        self.sdk.script = {"submit_transfer": [FakeTransferAPIError(429, "TooManyRequests", "slow down")]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["unknown", "accepted"])

    def test_retry_of_a_refused_request_takes_a_new_request_number(self):
        """Review GLO-05: a request Globus refused is never sent again unchanged (frozen deadline included):
        Retry same request looks for its task by label, then makes a new attempt."""
        self.sdk.script = {"submit_transfer": [FakeTransferAPIError(400, "BadRequest", "Invalid deadline")]}
        self.work()
        self.assertEqual(self.state(), ("failed", "refused_by_globus"))
        first = self.record(self.upload_id)["globus"]
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        globus = self.record(self.upload_id)["globus"]
        (earlier,) = globus["earlier_attempts"]
        self.assertEqual((earlier["submission_id"], earlier["task_id"], earlier["status"]),
                         (first["submission_id"], None, None))
        self.assertEqual(globus["attempt"], 2)
        bodies = [call[1][0] for call in self.sdk.calls("submit_transfer")]
        self.assertEqual([body["submission_id"] for body in bodies], [first["submission_id"], globus["submission_id"]])
        self.assertNotEqual(bodies[0]["submission_id"], bodies[1]["submission_id"])
        self.assertEqual(len(self.sdk.calls("task_list")), 1)
        texts = [event["text"] for event in arc_records.read_events(self.folder, last=0)]
        self.assertIn(arc_globus.TEXTS["request_spent"], texts)

    def test_a_saved_request_past_its_deadline_is_not_sent_again(self):
        self.sdk.script = {"submit_transfer": [FakeConnectionError("x")] * 3}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        first = self.record(self.upload_id)["globus"]["submission_id"]
        # The request was saved long ago: its 24-hour deadline has passed.
        arc_records.update_record(self.folder, lambda record: record["globus"].update(
            request_saved_utc="2026-01-01T10:00:00.000000Z"))
        record = self.record(self.upload_id)
        arc_globus.save_request(self.folder, arc_globus.build_request(record, first, self.folder / "payload" / SESSION))
        self.assertTrue(arc_globus.deadline_passed(arc_globus.load_request(self.folder)))
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        globus = self.record(self.upload_id)["globus"]
        self.assertEqual((globus["attempt"], len(globus["earlier_attempts"])), (2, 1))
        last = self.sdk.calls("submit_transfer")[-1][1][0]
        self.assertNotEqual(last["submission_id"], first)
        self.assertFalse(arc_globus.deadline_passed(last))

    def test_a_saved_request_that_no_longer_matches_is_not_sent_again(self):
        self.sdk.script = {"submit_transfer": [FakeConnectionError("x")] * 3}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        body = json.loads((self.folder / arc_globus.REQUEST_FILE).read_text(encoding="utf-8"))
        body["deadline"] = "2030-01-01T00:00:00+00:00"
        arc_globus.save_request(self.folder, body)
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.work()
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "payload_changed"))
        self.assertEqual(record["message"], "The saved request no longer matches its files; it is not sent again. "
                                            "Start a new upload.")
        self.assertEqual(len(self.sdk.calls("submit_transfer")), 3)


# ---------------------------------------------------------------------- watching (design 10.5)


class Watching(GlobusCase):
    def answers(self, *tasks, then_through=True):
        """get_task answers: these task documents (task_id filled in), then the service's own."""
        seen = []

        def answer(task):
            def respond(client, task_id):
                seen.append(arc_records.read_record(self.folder)["message"])
                return dict(task, task_id=task_id)

            return respond

        def last(client, task_id):
            seen.append(arc_records.read_record(self.folder)["message"])
            return client.through("get_task", task_id)

        self.sdk.script = {"get_task": [answer(task) for task in tasks] + ([last] if then_through else [])}
        return seen

    def test_the_messages_follow_the_task(self):
        seen = self.answers(
            active_task("x", files_transferred=12, bytes_transferred=310000),
            active_task("x", nice_status="PERMISSION_DENIED", nice_status_short_description="Permission denied."),
            active_task("x", status="INACTIVE", nice_status="CREDENTIAL_EXPIRED",
                        nice_status_short_description="Credentials expired"),
            active_task("x", is_paused=True))
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        files = self.record(self.upload_id)["payload"]["files"]
        total = arc_globus._size_text(self.record(self.upload_id)["progress"]["bytes"])
        deadline = arc_globus._local_time("2026-11-16T16:00:09+00:00")
        self.assertEqual(seen[1:], [
            "Copying with Globus: 12 of %d files (310 kB of %s). Keep Globus Connect Personal running."
            % (files, total),
            "Globus reports: Permission denied. It keeps trying until %s." % deadline,
            "Globus has paused this copy: Credentials expired. Open app.globus.org > Activity to let it continue.",
            "An administrator of a collection has paused this copy."])

    def test_inactive_is_not_final(self):
        self.answers(*[active_task("x", status="INACTIVE", nice_status_short_description="Waiting")] * 3)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(self.sdk.calls("get_task")), 4)

    def test_globus_connect_personal_not_connected_after_two_quiet_minutes(self):
        quiet = active_task("x", files_transferred=0, bytes_transferred=0)
        seen = self.answers(quiet, quiet, quiet)
        endpoints = [None, lambda client, collection: {"gcp_connected": False}]
        self.sdk.script["get_endpoint"] = endpoints
        with mock.patch.object(arc_globus, "_clock", Clock(70.0)), \
                mock.patch.object(arc_globus, "WATCH_LIMIT_SECONDS", 90 * 60):
            self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertIn(arc_globus.TEXTS["gcp_waiting"], seen)

    def test_a_failed_task_is_task_failed_with_its_reason(self):
        self.answers(active_task("x", status="FAILED", fatal_error={"code": "PERMISSION_DENIED",
                                                                    "description": "permission denied on ARC"}),
                     then_through=False)
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual(record["error"]["message"], "Globus could not finish the copy: permission denied on ARC. "
                                                     "Files copied so far are in %s on ARC." % incoming)
        self.assertEqual(record["globus"]["task"]["fatal_error"], {"code": "PERMISSION_DENIED",
                                                                   "description": "permission denied on ARC"})
        self.assertEqual(self.shown()["actions"], ["retry", "check"])
        self.assertEqual(self.shown()["leftover_on_arc"], incoming)

    def test_a_failed_task_without_a_reason_takes_the_newest_error_event(self):
        events = {"DATA": [{"code": "OLD", "description": "older", "is_error": True, "time": "2026-11-15T10:00:00"},
                           {"code": "NEW", "description": "the newest error", "is_error": True,
                            "time": "2026-11-15T11:00:00"},
                           {"code": "INFO", "description": "not an error", "is_error": False,
                            "time": "2026-11-15T12:00:00"}]}
        self.answers(active_task("x", status="FAILED", fatal_error=None), then_through=False)
        self.sdk.script["task_event_list"] = [lambda client, task_id, **options: events]
        self.work()
        self.assertIn("the newest error", self.record(self.upload_id)["error"]["message"])
        self.assertEqual(self.sdk.calls("task_event_list")[0][2], {"limit": 10})

    def test_the_watch_ends_90_minutes_after_the_sign_in(self):
        self.answers(*[active_task("x")] * 50, then_through=False)
        with mock.patch.object(arc_globus, "_clock", Clock(1000.0)), \
                mock.patch.object(arc_globus, "WATCH_LIMIT_SECONDS", 90 * 60):
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        shown = self.shown()
        self.assertEqual((shown["state"], shown["display_state"], shown["actions"]),
                         ("transferring", "not_watched", ["check", "stop"]))
        self.assertLess(len(self.sdk.calls("get_task")), 10)
        self.assertEqual(len(self.app().logins), 1)

    def stops_watching(self, error, text):
        self.sdk.script = {"get_task": [error]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("transferring", text))
        self.assertEqual(self.shown()["display_state"], "not_watched")
        self.assertEqual(len(self.app().logins), 1)

    def test_a_rejected_token_while_watching_stops_watching(self):
        self.stops_watching(FakeTransferAPIError(401, "AuthenticationFailed", "expired"),
                            arc_records.MESSAGES["not_watched"])

    def test_a_consent_asked_for_while_watching_stops_watching_without_a_browser(self):
        self.stops_watching(consent_error(), arc_globus.TEXTS["new_sign_in"])

    def test_a_task_this_identity_cannot_see_is_task_not_visible(self):
        self.sdk.script = {"get_task": [FakeTransferAPIError(404, "ClientError.NotFound", "no task")]}
        self.work()
        record = self.record(self.upload_id)
        self.assertEqual(record["error"]["reason"], "task_not_visible")
        self.assertIn(IDENTITY, record["error"]["message"])

    def test_short_network_trouble_does_not_end_the_watch(self):
        self.sdk.script = {"get_task": [FakeConnectionError("x"), FakeTransferAPIError(502, "BadGateway", "x"), None]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)

    def test_globus_unreachable_for_a_long_time_ends_the_watch(self):
        self.sdk.script = {"get_task": [FakeConnectionError("x")] * 30}
        with mock.patch.object(arc_globus, "_clock", Clock(200.0)), \
                mock.patch.object(arc_globus, "WATCH_LIMIT_SECONDS", 90 * 60):
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("transferring", arc_globus.TEXTS["unreachable"]))
        # Review GLO-V03: the Mac's protection against sleep ended with the worker; the texts say to keep it awake.
        awake = "Keep this Mac awake until the copy is done."
        self.assertTrue(record["message"].endswith(awake))
        shown = self.shown()
        self.assertEqual(shown["display_state"], "not_watched")
        self.assertTrue(shown["message"].endswith(awake), shown["message"])


# ---------------------------------------------------------------------- verifying (design 10.6)


class Verifying(GlobusCase):
    def transfers(self, change):
        def answer(client, task_id, **options):
            response = client.through("task_successful_transfers", task_id, **options)
            change(response.data["DATA"])
            return response

        self.sdk.script = {"task_successful_transfers": [answer]}

    def test_one_checksum_that_differs_is_verify_mismatch(self):
        def flip(items):
            items[2]["checksum"] = "0" * 32

        self.transfers(flip)
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["error"]["reason"], record["error"]["retryable"]), ("verify_mismatch", True))
        self.assertFalse(self.final().exists())
        self.assertTrue(self.incoming().is_dir())

    def test_retry_after_a_failed_check_is_a_new_attempt_into_the_same_folder(self):
        def flip(items):
            items[0]["checksum"] = "0" * 32

        self.transfers(flip)
        self.work()
        first = self.record(self.upload_id)["globus"]
        self.assertTrue(self.ok("arc-retry", "--upload=" + self.upload_id))
        self.assertEqual(self.record(self.upload_id)["resume"], {"at": "checking"})
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        globus = self.record(self.upload_id)["globus"]
        (earlier,) = globus["earlier_attempts"]
        self.assertEqual((earlier["attempt"], earlier["submission_id"], earlier["task_id"], earlier["status"]),
                         (1, first["submission_id"], first["task_id"], "SUCCEEDED"))
        self.assertEqual(globus["attempt"], 2)
        self.assertNotEqual(globus["submission_id"], first["submission_id"])
        self.assertEqual(globus["label"], "Raman upload %s a2 %s" % (self.upload_id, SESSION))
        bodies = [call[1][0] for call in self.sdk.calls("submit_transfer")]
        self.assertEqual([body["DATA"][0]["destination_path"] for body in bodies],
                         ["%s/_incoming/%s.%s/" % (GLOBUS_BASE, SESSION, self.upload_id)] * 2)
        same_as_manifest(self, self.final())

    def test_a_new_attempt_waits_for_an_earlier_transfer_that_still_runs(self):
        def flip(items):
            items[0]["checksum"] = "0" * 32

        self.transfers(flip)
        self.work()
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.sdk.script = {"get_task": [lambda client, task_id: active_task(task_id)]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        globus = self.record(self.upload_id)["globus"]
        self.assertEqual((globus["earlier_attempts"], globus["attempt"]), ([], 1))
        self.assertEqual(len(self.sdk.calls("submit_transfer")), 1)
        texts = [event["text"] for event in arc_records.read_events(self.folder, last=0)]
        self.assertIn(arc_globus.TEXTS["watch_earlier"], texts)

    def test_a_file_missing_or_extra_in_globus_s_list_is_verify_mismatch(self):
        for change in (lambda items: items.pop(), lambda items: items.append(dict(
                items[0], destination_path=items[0]["destination_path"] + ".extra"))):
            self.transfers(change)
            self.work()
            self.assertEqual(self.state(), ("failed", "verify_mismatch"))
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]

    def test_the_listing_of_incoming_must_match_too(self):
        def extra(client, task_id, **options):
            (self.incoming() / "stray.txt").write_text("x", encoding="utf-8")
            return client.through("task_successful_transfers", task_id, **options)

        self.sdk.script = {"task_successful_transfers": [extra]}
        self.work()
        record = self.record(self.upload_id)
        self.assertEqual(record["error"]["reason"], "verify_mismatch")
        self.assertIn("stray.txt", record["error"]["message"])

    def test_without_checksums_globus_s_own_verification_is_named_honestly(self):
        def strip(items):
            for item in items:
                item.pop("checksum")

        def unknown(items):
            for item in items:
                item["checksum_algorithm"] = "CRC32C"

        for change in (strip, unknown):
            self.transfers(change)
            self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
            record = self.record(self.upload_id)
            self.assertEqual(record["verification"]["method"], "globus-verify")
            self.assertTrue(record["message"].endswith("Globus compared every file with this Mac's copy after "
                                                       "copying it."), record["message"])
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]
            self.folder = self.record_dir(self.upload_id)

    def test_other_checksum_algorithms_are_computed_here(self):
        import hashlib
        import zlib

        for algorithm in ("SHA256", "sha512", "ADLER32", "SHA1"):
            def recompute(items, algorithm=algorithm):
                for item in items:
                    data = Path(item["source_path"]).read_bytes()
                    if algorithm == "ADLER32":
                        item["checksum"] = "%x" % zlib.adler32(data)
                    else:
                        item["checksum"] = hashlib.new(algorithm.lower(), data).hexdigest().upper()
                    item["checksum_algorithm"] = algorithm

            self.transfers(recompute)
            self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED, algorithm)
            verification = self.record(self.upload_id)["verification"]
            self.assertEqual((verification["method"], verification["algorithm"]),
                             ("globus-checksum", algorithm.upper()))
            self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]
            self.folder = self.record_dir(self.upload_id)

    def test_a_list_of_copied_files_globus_no_longer_keeps(self):
        """Review GLO-V01: Globus keeps the list of a task's copied files for a month. A finished task whose
        list is gone is checked by the listing on ARC (names and sizes), with Globus's own verification."""
        history_deleted = FakeTransferAPIError(404, "ClientError.NotFound", "history deleted")
        self.sdk.script = {"task_successful_transfers": [history_deleted]}
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual((record["verification"]["method"], record["verification"]["algorithm"]),
                         ("globus-verify", None))
        self.assertTrue(record["message"].endswith(arc_globus.TEXTS["sentence_verify"]), record["message"])
        same_as_manifest(self, self.final())
        texts = [event["text"] for event in arc_records.read_events(self.folder, last=0)]
        self.assertIn(arc_globus.TEXTS["history_gone"], texts)

    def test_a_missing_list_still_needs_the_listing_to_match(self):
        def gone(client, task_id, **options):
            (self.incoming() / "stray.txt").write_text("x", encoding="utf-8")
            raise FakeTransferAPIError(404, "ClientError.NotFound", "history deleted")

        self.sdk.script = {"task_successful_transfers": [gone]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        self.assertEqual(self.state(), ("failed", "verify_mismatch"))

    def test_a_missing_list_of_a_task_this_identity_cannot_see_is_task_not_visible(self):
        self.sdk.script = {"task_successful_transfers": [FakeTransferAPIError(404, "ClientError.NotFound", "no")],
                           "get_task": [None, FakeTransferAPIError(404, "ClientError.NotFound", "no task")]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        self.assertEqual(self.state(), ("failed", "task_not_visible"))
        fix = self.record(self.upload_id)["error"]["fix"]
        self.assertIn("Check status", fix)
        self.assertNotIn("Retry same request", fix)

    def test_transfers_are_read_page_by_page(self):
        with mock.patch.object(arc_demo, "TRANSFERS_PAGE", 5):
            self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        markers = [call[2]["marker"] for call in self.sdk.calls("task_successful_transfers")]
        self.assertEqual(markers[0], None)
        self.assertEqual(markers[1:], ["5", "10", "15"][:len(markers) - 1])
        self.assertGreater(len(markers), 2)

    def test_an_upload_copy_changed_on_this_mac_is_payload_changed(self):
        def change(client, task_id, **options):
            target = self.folder / "payload" / SESSION / "point_0001.json"
            os.chmod(target, 0o644)
            target.write_text("{}", encoding="utf-8")
            return client.through("get_task", task_id)

        self.sdk.script = {"get_task": [change]}
        self.work()
        self.assertEqual(self.state(), ("failed", "payload_changed"))
        self.assertFalse(self.final().exists())


# ---------------------------------------------------------------------- placing (design 10.7)


class Placing(GlobusCase):
    def taken_at_placing(self, *names):
        def take(client, collection, old, new):
            for name in names:
                self.final(name).mkdir(parents=True, exist_ok=True)
            return client.through("operation_rename", collection, old, new)

        self.sdk.script = {"operation_rename": [take]}

    def test_a_name_taken_at_placing_is_a_conflict_with_the_copy_checked(self):
        self.taken_at_placing(SESSION)
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual((record["state"], record["conflict_at"]), ("conflict", "placing"))
        self.assertEqual(record["message"], "Your copy is complete and checked in %s on ARC. Keep it as a new copy, or "
                                            "move the other folder on ARC and press Retry same request." % incoming)
        self.assertEqual(self.shown()["actions"], ["keep_copy", "retry", "stop"])
        same_as_manifest(self, self.incoming())
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual(record["destination"]["placed_as"], SESSION + "_copy2")
        self.assertEqual(record["destination"]["final_path"], "%s/%s_copy2" % (ARC_FOLDER, SESSION))
        self.assertEqual(len(self.sdk.server.task_list()), 1)

    def test_retry_after_the_other_folder_was_moved_puts_it_in_place(self):
        self.taken_at_placing(SESSION)
        self.work()
        os.rmdir(self.final())
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        same_as_manifest(self, self.final())
        self.assertEqual(len(self.sdk.calls("submit_transfer")), 1)

    def test_a_new_copy_takes_the_first_free_name(self):
        self.taken_at_placing(SESSION)
        self.work()
        self.final(SESSION + "_copy2").mkdir()
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.record(self.upload_id)["destination"]["placed_as"], SESSION + "_copy3")
        renames = [call[1][2].rsplit("/", 1)[1] for call in self.sdk.calls("operation_rename")]
        # The first worker's rename, then Keep as new copy: the plain name first (decision I4).
        self.assertEqual(renames, [SESSION, SESSION, SESSION + "_copy2", SESSION + "_copy3"])

    def test_a_new_copy_whose_plain_name_is_free_takes_it(self):
        """Decision I4 (review COR-V02): a new copy goes under the first free name of <folder>, <folder>_copy2
        ... _copy99, so a session uploaded again after its folder left ARC gets its own name back."""
        self.taken_at_placing(SESSION)
        self.work()
        os.rmdir(self.final())
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual((record["destination"]["placed_as"], record["destination"]["new_copy"]), (SESSION, True))
        same_as_manifest(self, self.final())
        self.assertFalse(self.final(SESSION + "_copy2").exists())

    def test_a_new_upload_with_new_after_the_folder_left_arc_takes_its_own_name(self):
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        import shutil

        shutil.rmtree(self.final())
        self.upload_id = self.upload("--live", "--new")["upload"]["upload_id"]
        self.assertTrue(self.record(self.upload_id)["destination"]["new_copy"])
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.record(self.upload_id)["destination"]["placed_as"], SESSION)
        same_as_manifest(self, self.final())

    def test_keep_as_new_copy_asks_for_the_consent_before_placing(self):
        """Review GLO-01: a send worker that resumes past checking asks for ARC's data_access consent right
        after its sign-in, while the person is at the screen, instead of stopping at the rename."""
        self.sdk.consent_required = self.sdk.consented = True
        self.taken_at_placing(SESSION)
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        self.assertEqual(self.state(), ("conflict", None))
        # Globus no longer has the consent (withdrawn, or another identity of the person signs in).
        self.sdk.consented = False
        self.ok("arc-keep-copy", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(self.record(self.upload_id)["destination"]["placed_as"], SESSION + "_copy2")
        logins = self.app().logins
        self.assertEqual(len(logins), 2)
        self.assertIn(DATA_ACCESS, logins[1].required_scopes)

    def test_a_rename_done_before_a_crash_is_found_in_place(self):
        def renamed_then_killed(client, *arguments):
            client.through("operation_rename", *arguments)
            raise Crash()

        self.sdk.script = {"operation_rename": [renamed_then_killed]}
        with self.assertRaises(Crash):
            self.work()
        self.assertEqual(self.record(self.upload_id)["state"], "placing")
        self.assertEqual(self.shown()["display_state"], "not_watched")
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["destination"]["placed_as"]), ("verified", SESSION))
        same_as_manifest(self, self.final())
        self.assertFalse(self.incoming().exists())

    def test_a_stop_after_a_worker_ended_while_placing(self):
        def renamed_then_killed(client, *arguments):
            client.through("operation_rename", *arguments)
            raise Crash()

        self.sdk.script = {"operation_rename": [renamed_then_killed]}
        with self.assertRaises(Crash):
            self.work()
        apps = len(self.sdk.apps)
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        # The task had finished: nothing runs on Globus, so arc-stop stops it at once, without a stop
        # worker, a sign-in or a cancel (design 7.4).
        self.assertEqual((answer["stopped_now"], answer["worker"], answer["message"]),
                         (True, None, arc_globus.TEXTS["stopped_placing"]))
        self.assertNotIn("stop", [action for _home, _upload, action in self.spawned])
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("stopped", arc_globus.TEXTS["stopped_placing"]))
        self.assertEqual((len(self.sdk.apps), self.sdk.calls("cancel_task")), (apps, []))
        self.assertIn("check", self.shown()["actions"])
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual((record["destination"]["placed_as"], record["verification"]["method"]),
                         (SESSION, "globus-checksum"))
        same_as_manifest(self, self.final())

    def test_an_incoming_folder_gone_from_arc_is_verify_mismatch(self):
        def gone(client, collection, old, new):
            import shutil

            shutil.rmtree(self.incoming())
            return client.through("operation_rename", collection, old, new)

        self.sdk.script = {"operation_rename": [gone]}
        self.work()
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "verify_mismatch"))
        self.assertEqual(record["message"], "The upload's folder in _incoming is no longer on ARC.")


# ---------------------------------------------------------------------- stop and check status (design 10.8)


class StopAndCheck(GlobusCase):
    pace = 1.0

    def setUp(self):
        super().setUp()
        # The service's clock stands still: a task copies nothing until the test moves it.

    def finish_tasks(self):
        self.server_clock[0] += 3600

    def watched_then_killed(self):
        """A worker that dies while Globus copies: the upload is not watched any more."""

        def killed(client, task_id):
            client.through("get_task", task_id)
            raise Crash()

        self.sdk.script = {"get_task": [None, killed]}
        with self.assertRaises(Crash):
            self.work()
        self.assertEqual(self.shown()["display_state"], "not_watched")

    def test_a_stop_while_watching_cancels_the_task(self):
        def ask_for_a_stop(client, task_id):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            time.sleep(0.2)
            return client.through("get_task", task_id)

        self.sdk.script = {"get_task": [None, ask_for_a_stop]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual((record["state"], record["message"]), (
            "stopped", "Stopped. Files copied before the stop are in %s on ARC; nothing was deleted." % incoming))
        self.assertEqual([call[1][0] for call in self.sdk.calls("cancel_task")], [record["globus"]["task_id"]])
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["fatal_error"]["code"], "CANCELED")

    def ask_for_a_stop(self, then_wait):
        def behaviour(app, parameters):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            then_wait()

        self.sdk.login_behaviour = behaviour

    def test_a_stop_during_the_first_sign_in_sends_nothing(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.ask_for_a_stop(lambda: release.wait(30))
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("stopped", "Stopped before anything was sent."))
        self.assertEqual((self.sdk.calls("get_submission_id"), self.sdk.calls("submit_transfer")), ([], []))
        self.assertEqual((self.app().close_calls, self.sdk.issued, self.sdk.revokers[-1].closed), (1, [], 1))

    def test_a_stop_while_signing_in_for_a_request_globus_may_have_waits_and_cancels(self):
        self.sdk.script = {"submit_transfer": [lost_reply, FakeConnectionError("x"), FakeConnectionError("x")]}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.ask_for_a_stop(lambda: time.sleep(0.3))
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual((record["state"], record["message"]), (
            "stopped", "Stopped. Files copied before the stop are in %s on ARC; nothing was deleted." % incoming))
        (task,) = self.sdk.server.task_list()
        self.assertEqual((record["globus"]["task_id"], task["status"]), (task["task_id"], "FAILED"))
        self.assertEqual(len(self.sdk.calls("submit_transfer")), 3)

    def test_the_stop_worker_signs_in_cancels_and_waits_for_the_end(self):
        self.watched_then_killed()
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertEqual((answer["stopped_now"], answer["worker"]), (False, "detached"))
        self.assertEqual(self.spawned[-1][2], "stop")
        before = len(self.sdk.calls("get_task"))
        self.sdk.script = {"cancel_task": [lambda client, task_id: {"code": "CancelAccepted"}],
                           "get_task": [lambda client, task_id: active_task(task_id)] * 2 + [
                               lambda client, task_id: (self.sdk.server.cancel(task_id), client.through(
                                   "get_task", task_id))[1]]}
        self.assertEqual(self.work("stop"), arc_worker.EXIT_ENDED)
        self.assertEqual(self.record(self.upload_id)["state"], "stopped")
        self.assertEqual(len(self.sdk.calls("get_task")) - before, 3)
        self.assertEqual(len(self.sdk.apps[-1].logins), 1)

    def test_a_copy_that_finished_before_the_stop_is_kept_whole(self):
        self.watched_then_killed()
        self.ok("arc-stop", "--upload=" + self.upload_id)
        self.finish_tasks()
        self.work("stop")
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("stopped", arc_globus.TEXTS["stopped_complete"].format(
            incoming=incoming)))
        self.assertEqual(self.sdk.calls("cancel_task")[0][0], "cancel_task")
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["status"], "SUCCEEDED")
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(self.sdk.server.task_list()), 1)

    def test_a_stop_worker_for_a_task_this_identity_cannot_see(self):
        self.watched_then_killed()
        self.ok("arc-stop", "--upload=" + self.upload_id)
        self.sdk.script = {"cancel_task": [FakeTransferAPIError(403, "PermissionDenied", "not yours")]}
        self.work("stop")
        self.assertEqual(self.state(), ("failed", "task_not_visible"))

    def test_check_status_watches_the_same_task(self):
        self.watched_then_killed()
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.finish_tasks()
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(self.sdk.server.task_list()), 1)
        self.assertEqual(len(self.sdk.calls("submit_transfer")), 1)
        same_as_manifest(self, self.final())

    def test_check_status_of_an_uncertain_upload_finds_the_task_by_its_label(self):
        self.sdk.script = {"submit_transfer": [lost_reply, FakeConnectionError("x"), FakeConnectionError("x")]}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.finish_tasks()
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual(record["globus"]["task_id"], self.sdk.server.task_list()[0]["task_id"])
        self.assertEqual(len(self.sdk.calls("submit_transfer")), 3)

    def test_check_status_without_a_task_leaves_the_upload_as_it_was(self):
        self.sdk.script = {"submit_transfer": [FakeConnectionError("x")] * 3}
        self.work()
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("uncertain", "Globus has no transfer for this upload. "
                                                                             "Press Retry same request to send it."))
        self.assertEqual(self.shown()["actions"], ["retry", "check", "stop"])

    def test_check_status_of_a_task_nobody_can_see_is_task_not_visible(self):
        self.watched_then_killed()
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.sdk.script = {"get_task": [FakeTransferAPIError(403, "PermissionDenied", "not yours")]}
        self.work("check")
        self.assertEqual(self.state(), ("failed", "task_not_visible"))
        self.assertIn(IDENTITY, self.record(self.upload_id)["error"]["message"])

    def test_check_status_of_a_verified_upload_lists_the_folder_on_arc(self):
        self.sdk.server.pace = 0
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        files = self.record(self.upload_id)["payload"]["files"]
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        record = self.record(self.upload_id)
        self.assertEqual((record["checks"][-1]["ok"], record["checks"][-1]["message"]),
                         (True, "Still on ARC: %d files with the right sizes." % files))
        os.remove(self.final() / "point_0002.json")
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertEqual(record["checks"][-1]["ok"], False)
        self.assertIn("point_0002.json", record["checks"][-1]["message"])
        self.assertTrue(record["message"].startswith("On ARC: "))

    def test_a_check_of_a_verified_upload_that_cannot_look_keeps_it_on_arc(self):
        # REG-01: a sign-in that did not work, one that took too long, Globus out of reach, a listing of the folder
        # that Globus gives only in part. Each check says why and what to do; none makes the upload "On ARC, last
        # check failed", counts for attention or lets Upload to ARC go ahead without --new.
        self.sdk.server.pace = 0
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        release = threading.Event()
        self.addCleanup(release.set)
        too_big = FakeTransferAPIError(502, "ExternalError.DirListingFailed.DirectorySizeLimit", "too many entries")
        forbidden = FakeTransferAPIError(403, "PermissionDenied", "no access to this path")
        cases = (("signin_failed", refused_sign_in, None, {}, REFUSED_SIGN_IN),
                 ("signin_timeout", lambda app, parameters: release.wait(30), Clock(100.0), {},
                  "You did not finish signing in within 10 minutes."),
                 ("globus_error", None, None, {"operation_ls": [FakeConnectionError("x")]},
                  "Globus could not be reached, or gave an answer this program does not expect (no connection)."),
                 ("listing", None, None, {"operation_ls": [too_big]}, "Globus listed only part of the folder on ARC."),
                 # Review R2-03: Globus may not list the copy for this identity (403): a note about access to the
                 # project folder, not "Globus could not be reached".
                 ("folder_not_readable", None, None, {"operation_ls": [forbidden]},
                  "Your Globus identity may not list %s/%s on ARC." % (ARC_FOLDER, SESSION)))
        for reason, login, clock, script, why in cases:
            with self.subTest(reason=reason):
                self.sdk.login_behaviour, self.sdk.script = login, dict(script)
                self.ok("arc-check", "--upload=" + self.upload_id)
                with mock.patch.object(arc_globus, "_clock", clock or arc_globus._clock):
                    self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
                record = self.record(self.upload_id)
                note = record["checks"][-1]
                self.assertEqual({key: note[key] for key in ("ok", "looked", "message", "fix")},
                                 {"ok": False, "looked": False, "message": why + " Check status could not look.",
                                  "fix": arc_records.CHECK_FIXES.get(reason, arc_records.CHECK_AGAIN_FIX)})
                self.assertEqual(record["state"], "verified")
                self.assertTrue(record["message"].startswith("On ARC: "))
                shown = self.shown()
                self.assertEqual((shown["display_state"], shown["display_label"], shown["last_check"]),
                                 ("verified", "On ARC, checked", note))
                self.assertEqual((self.ok("status")["arc_attention"], self.ok("arc-list")["attention"]), (0, 0))
                self.refused("conflict", "already_on_arc", "arc-upload", "--session=" + SESSION, "--live", "--dry-run")
                # The history says why, and what to do.
                self.assertEqual(arc_records.read_events(self.folder)[-1]["text"], note["message"] + " " + note["fix"])
        # A check that looked and found the copy changed stays what the listing shows after a later check that could
        # not look; Upload to ARC then goes ahead under the session's own name (decision I4).
        os.remove(self.final() / "point_0002.json")
        self.sdk.login_behaviour, self.sdk.script = None, {}
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        self.sdk.login_behaviour = refused_sign_in
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual([(entry["ok"], entry["looked"]) for entry in record["checks"][-2:]],
                         [(False, True), (False, False)])
        self.assertEqual((self.shown()["display_state"], self.shown()["message"]),
                         ("check_failed", record["checks"][-2]["message"]))
        self.assertIs(self.ok("arc-upload", "--session=" + SESSION, "--live", "--dry-run")["plan"]["new_copy"], False)

    def test_a_check_whose_sign_in_fails_leaves_the_state(self):
        self.watched_then_killed()
        self.ok("arc-check", "--upload=" + self.upload_id)

        def refused(app, parameters):
            raise FakeLoginError("Login timed out. Please try again.")

        self.sdk.login_behaviour = refused
        self.work("check")
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "transferring")
        self.assertEqual(record["message"], "Signing in to Globus did not work: Login timed out. Please try again. "
                                            "Check status could not look.")
        self.assertEqual(self.shown()["actions"], ["check", "stop"])

    # -- review GLO-01: the consent is asked for while the person is at the screen

    def test_a_check_asks_for_the_consent_before_it_watches(self):
        self.sdk.consent_required = self.sdk.consented = True
        self.watched_then_killed()
        # Globus no longer has the consent (withdrawn, or another identity of the person signs in).
        self.sdk.consented = False
        self.ok("arc-check", "--upload=" + self.upload_id)

        def finished(client, task_id):
            self.finish_tasks()
            return client.through("get_task", task_id)

        # The transfer still runs when the check starts: the check watches it, then checks the copy on ARC.
        self.sdk.script = {"get_task": [None, finished]}
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        logins = self.app().logins
        self.assertEqual(len(logins), 2)
        self.assertIn(DATA_ACCESS, logins[1].required_scopes)
        same_as_manifest(self, self.final())

    # -- review GLO-02: a sign-in that did not happen keeps what Globus may hold

    def test_a_retry_whose_sign_in_fails_keeps_an_uncertain_upload_uncertain(self):
        self.sdk.script = {"submit_transfer": [lost_reply, FakeConnectionError("x"), FakeConnectionError("x")]}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        release = threading.Event()
        self.addCleanup(release.set)
        for login, clock, why in ((refused_sign_in, None, REFUSED_SIGN_IN),
                                  (lambda app, parameters: release.wait(30), Clock(100.0),
                                   "You did not finish signing in within 10 minutes.")):
            self.ok("arc-retry", "--upload=" + self.upload_id)
            self.sdk.login_behaviour = login
            with mock.patch.object(arc_globus, "_clock", clock or arc_globus._clock):
                self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
            record = self.record(self.upload_id)
            self.assertEqual((record["state"], record["error"]), ("uncertain", None))
            self.assertEqual(record["message"], why + " " + arc_globus.TEXTS["uncertain"])
            self.assertNotIn("Nothing was sent", record["message"])
            self.assertEqual(self.shown()["actions"], ["retry", "check", "stop"])
        (task,) = self.sdk.server.task_list()
        self.assertEqual(task["status"], "ACTIVE")

    def test_a_retry_whose_sign_in_fails_while_globus_copies_keeps_check_and_stop(self):
        self.sdk.script = {"get_task": [FakeTransferAPIError(400, "BadRequest", "a strange answer")]}
        self.work()
        self.assertEqual(self.state(), ("failed", "globus_error"))
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.sdk.login_behaviour = refused_sign_in
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        shown = self.shown()
        self.assertEqual((shown["state"], shown["display_state"], shown["actions"]),
                         ("transferring", "not_watched", ["check", "stop"]))
        self.assertEqual(self.record(self.upload_id)["message"],
                         REFUSED_SIGN_IN + " " + arc_globus.TEXTS["not_watching"])

    def test_a_retry_of_a_finished_copy_whose_sign_in_fails_says_where_the_copy_is(self):
        self.watched_then_killed()
        self.ok("arc-stop", "--upload=" + self.upload_id)
        self.finish_tasks()
        self.work("stop")
        self.assertEqual(self.state(), ("stopped", None))
        self.ok("arc-retry", "--upload=" + self.upload_id)
        self.sdk.login_behaviour = refused_sign_in
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "signin_failed"))
        self.assertEqual(record["message"], REFUSED_SIGN_IN + " " + arc_globus.TEXTS["earlier_complete"].format(
            incoming=incoming))
        self.assertEqual(record["error"]["message"], record["message"])
        self.assertEqual(self.shown()["actions"][:2], ["retry", "check"])

    def test_check_and_stop_whose_sign_in_fails_keep_what_the_upload_said(self):
        self.sdk.script = {"submit_transfer": [lost_reply, FakeConnectionError("x"), FakeConnectionError("x")]}
        self.work()
        self.sdk.login_behaviour = refused_sign_in
        for _ in range(2):
            # A second check that cannot sign in does not pile its note on the first one's.
            self.ok("arc-check", "--upload=" + self.upload_id)
            self.work("check")
            record = self.record(self.upload_id)
            self.assertEqual((record["state"], record["message"]), (
                "uncertain", REFUSED_SIGN_IN + " Check status could not look. " + arc_globus.TEXTS["uncertain"]))
        self.ok("arc-stop", "--upload=" + self.upload_id)
        self.work("stop")
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), (
            "uncertain", REFUSED_SIGN_IN + " The upload was not stopped. " + arc_globus.TEXTS["uncertain"]))
        self.assertEqual(self.shown()["actions"], ["retry", "check", "stop"])

    def test_a_stopped_check_whose_sign_in_fails_says_the_stop_did_not_happen(self):
        self.watched_then_killed()
        self.ok("arc-check", "--upload=" + self.upload_id)

        def stop_then_fail(app, parameters):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            time.sleep(0.2)
            raise FakeLoginError("Login timed out. Please try again.")

        self.sdk.login_behaviour = stop_then_fail
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]),
                         ("transferring", REFUSED_SIGN_IN + " The upload was not stopped."))
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["status"], "ACTIVE")
        self.assertEqual(self.shown()["actions"], ["check", "stop"])

    # -- review GLO-07: a stop that cannot reach Globus

    def ask_for_a_stop_while_watching(self):
        def ask(client, task_id):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            time.sleep(0.2)
            return client.through("get_task", task_id)

        return ask

    def test_a_stop_during_a_globus_outage_keeps_the_upload_and_says_so(self):
        down = FakeTransferAPIError(503, "ServiceUnavailable", "down")
        self.sdk.script = {"get_task": [None, self.ask_for_a_stop_while_watching()] + [down] * 100,
                           "cancel_task": [down] * 100}
        with mock.patch.object(arc_globus, "STOP_SETTLE_SECONDS", 0.3), \
                mock.patch.object(arc_globus, "STOP_POLL_SECONDS", 0.05):
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["message"]), ("transferring", arc_globus.TEXTS["stop_not_delivered"]))
        self.assertGreater(len(self.sdk.calls("cancel_task")), 1)
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["status"], "ACTIVE")
        self.assertEqual(self.shown()["actions"], ["check", "stop"])
        # This stop was tried: the person presses Stop upload again, as the text says; no stop worker is started.
        self.assertEqual((record["requests"]["stop_utc"], record["worker"]), (None, None))
        self.assertNotIn("stop", [spawned[2] for spawned in self.spawned])

    def test_a_stop_pressed_as_a_retry_starts_cancels_what_globus_runs(self):
        """Review COR-04: Stop pressed while the worker of Retry same request starts (the request is in the record
        before the transport begins) is not ended at once while Globus may run the transfer: the worker signs
        in, finds the transfer by its label and cancels it."""
        self.sdk.script = {"submit_transfer": [lost_reply, FakeConnectionError("x"), FakeConnectionError("x")]}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        self.ok("arc-retry", "--upload=" + self.upload_id)
        arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
        real_prepare = arc_worker._Run.prepare

        def prepare(run):
            early = real_prepare(run)
            # The worker's poller has seen the request before the transport starts.
            deadline = time.monotonic() + 10
            while not run.ctx.stop_requested() and time.monotonic() < deadline:
                time.sleep(0.01)
            return early

        with mock.patch.object(arc_worker._Run, "prepare", prepare):
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        (task,) = self.sdk.server.task_list()
        self.assertEqual((record["state"], record["globus"]["task_id"]), ("stopped", task["task_id"]))
        self.assertEqual((task["status"], task["fatal_error"]["code"]), ("FAILED", "CANCELED"))
        self.assertEqual(len(self.sdk.calls("cancel_task")), 1)

    def test_stop_with_a_worker_at_work_says_the_transfer_is_cancelled_first(self):
        """Stop pressed while a worker works on a live Globus upload whose transfer may run: the reply does not
        promise "within a second", since a worker still at its sign-in cancels only after it (decision I3 as
        built; the window's question says the same)."""
        self.sdk.script = {"submit_transfer": [lost_reply, FakeConnectionError("x"), FakeConnectionError("x")]}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        arc_records.update_record(self.folder, lambda current: current.update(worker={
            "pid": 1, "action": "check", "started_utc": "2026-11-15T16:00:00.000Z", "terminal": False}))
        arc_support.hold_lock(self, self.folder / "worker.lock")
        answer = self.ok("arc-stop", "--upload=" + self.upload_id)
        self.assertEqual((answer["stopped_now"], answer["worker"], answer["message"]),
                         (False, None, "The upload program stops the upload. If Globus may still be copying it, the "
                                       "transfer is cancelled first, after the Globus sign-in if that page is still "
                                       "open."))
        self.assertTrue(self.record(self.upload_id)["requests"]["stop_utc"])

    def test_a_stop_while_the_worker_signs_out_goes_to_a_stop_worker(self):
        """Review GLO-V02: a stop pressed while the worker signs out, after it decided to leave the transfer
        running (here: Globus refused its token while it watched), is handed to a stop worker, not dropped."""
        self.sdk.script = {"get_task": [None, FakeTransferAPIError(401, "AuthenticationFailed", "expired")]}
        real_end = arc_globus.GlobusFlow.end

        def end(flow):
            if not flow.ended:
                arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
                time.sleep(0.2)  # the worker's poller (every 20 ms here) sees it
            real_end(flow)

        with mock.patch.object(arc_globus.GlobusFlow, "end", end):
            self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["requests"]["stop_utc"]), ("transferring", "now"))
        self.assertEqual(self.sdk.calls("cancel_task"), [])
        self.assertEqual(record["worker"], {"pid": None, "action": "stop", "started_utc": None, "terminal": False})
        self.assertEqual(self.spawned[-1][1:], (self.upload_id, "stop"))
        self.assertEqual(arc_records.read_events(self.folder)[-1]["text"], arc_worker.STOP_HANDED_ON_TEXT)
        self.assertEqual(self.shown()["actions"], ["check", "stop"])
        # The stop worker signs in and cancels the transfer.
        self.assertEqual(self.work("stop"), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["requests"]["stop_utc"], record["worker"]), ("stopped", None, None))
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["fatal_error"]["code"], "CANCELED")

    def test_a_stop_that_comes_as_the_watch_ends_is_not_dropped(self):
        """Review GLO-V02: a stop pressed just as the worker stops watching is settled before the tokens go."""
        def refused_with_a_stop(client, task_id):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            time.sleep(0.2)
            raise FakeTransferAPIError(401, "AuthenticationFailed", "expired")

        self.sdk.script = {"get_task": [None, refused_with_a_stop]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "stopped")
        self.assertEqual(len(self.sdk.calls("cancel_task")), 1)
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["fatal_error"]["code"], "CANCELED")

    def test_a_cancel_without_an_answer_is_asked_for_again(self):
        self.sdk.script = {"get_task": [None, self.ask_for_a_stop_while_watching()],
                           "cancel_task": [FakeConnectionError("x")]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "stopped")
        self.assertEqual(len(self.sdk.calls("cancel_task")), 2)
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["fatal_error"]["code"], "CANCELED")

    # -- decision I8 (review COR-01): a process that must end is not a person who asked to stop

    def test_sigterm_while_watching_cancels_nothing(self):
        flows = capture_flows(self)

        def logout_or_shutdown(client, task_id):
            flows[-1].ctx.request_stop("SIGTERM")
            return client.through("get_task", task_id)

        self.sdk.script = {"get_task": [logout_or_shutdown]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "transferring")
        self.assertEqual(self.sdk.calls("cancel_task"), [])
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["status"], "ACTIVE")
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["actions"]), ("not_watched", ["check", "stop"]))

    def test_sighup_of_a_detached_worker_during_its_sign_in_ends_it_at_once(self):
        flows = capture_flows(self)
        release = threading.Event()
        self.addCleanup(release.set)

        def hang_up(app, parameters):
            flows[-1].ctx.request_stop("SIGHUP")
            release.wait(30)

        self.sdk.login_behaviour = hang_up
        started = time.monotonic()
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        self.assertLess(time.monotonic() - started, 10)
        shown = self.shown()
        # No final state: the listing shows the upload as interrupted, and Retry same request goes on.
        self.assertEqual((shown["state"], shown["display_state"], shown["actions"]),
                         ("signing_in", "interrupted", ["retry", "stop"]))

    def test_a_person_s_stop_beside_a_signal_still_cancels(self):
        flows = capture_flows(self)

        def both(client, task_id):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            flows[-1].ctx.request_stop("SIGTERM")
            return client.through("get_task", task_id)

        self.sdk.script = {"get_task": [both]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        self.assertEqual(self.record(self.upload_id)["state"], "stopped")
        self.assertEqual(len(self.sdk.calls("cancel_task")), 1)

    # -- decision I3 (review SEC-01): a check worker can be stopped

    def test_a_stop_ends_a_check_of_a_verified_upload_at_once(self):
        self.sdk.server.pace = 0
        self.assertEqual(self.work(), arc_worker.EXIT_VERIFIED)
        checks = self.record(self.upload_id)["checks"]
        self.ok("arc-check", "--upload=" + self.upload_id)
        release = threading.Event()
        self.addCleanup(release.set)
        self.ask_for_a_stop(lambda: release.wait(30))
        started = time.monotonic()
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        self.assertLess(time.monotonic() - started, 10)
        record = self.record(self.upload_id)
        self.assertEqual((record["state"], record["checks"], record["requests"]["stop_utc"]),
                         ("verified", checks, None))
        self.assertEqual(self.sdk.calls("cancel_task"), [])

    def test_a_stop_during_a_check_of_a_running_transfer_cancels_it(self):
        self.watched_then_killed()
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.ask_for_a_stop(lambda: time.sleep(0.3))
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "stopped")
        self.assertEqual(self.sdk.server.get_task(record["globus"]["task_id"])["fatal_error"]["code"], "CANCELED")

    # -- review R2-01: a check past the end of the transfer keeps the stored state when it is stopped

    SHOWN_FIELDS = ("state", "display_state", "display_label", "message", "actions", "may_run", "error")

    def stop_while_globus_lists_the_copied_files(self):
        """The person presses Stop (arc-stop writes requests.stop_utc) while the check reads Globus's list of the
        files it copied."""
        def stop_then_list(client, task_id, **options):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            time.sleep(0.3)
            return client.through("task_successful_transfers", task_id, **options)

        self.sdk.script = {"task_successful_transfers": [stop_then_list]}

    def assert_kept_by_the_stopped_check(self, before):
        """The check ended at once and left the upload as the listing showed it: no "stopped", nothing cancelled,
        nothing renamed, the copy complete in _incoming; the history says that the check was stopped."""
        shown = self.shown()
        self.assertEqual({key: shown[key] for key in self.SHOWN_FIELDS},
                         {key: before[key] for key in self.SHOWN_FIELDS})
        record = self.record(self.upload_id)
        self.assertEqual((record["requests"]["stop_utc"], record["worker"]), (None, None))
        self.assertEqual((self.sdk.calls("cancel_task"), self.sdk.calls("operation_rename")), ([], []))
        same_as_manifest(self, self.incoming())
        self.assertFalse(self.final().exists())
        texts = [event["text"] for event in arc_records.read_events(self.folder)]
        # Last, or followed by the message the upload had before the check.
        self.assertIn(arc_globus.TEXTS["check_stopped"], texts[-2:])
        self.assertNotIn(arc_globus.TEXTS["stopped_complete"].format(incoming=before["destination"]["incoming_path"]),
                         texts)
        # Check status again, without a stop, puts the copy in place.
        self.sdk.script = {}
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        same_as_manifest(self, self.final())

    def test_a_stop_of_a_check_of_an_upload_that_was_checking_the_copy(self):
        # The worker ended while it checked the copy (its task SUCCEEDED): not watched, with nothing that Globus may
        # still run, so the window asks "Stop the check of this upload? ... the upload stays as it is".
        self.sdk.server.pace = 0

        def killed(client, task_id, **options):
            raise Crash()

        self.sdk.script = {"task_successful_transfers": [killed]}
        with self.assertRaises(Crash):
            self.work()
        before = self.shown()
        self.assertEqual((before["state"], before["display_state"], before["may_run"], before["actions"]),
                         ("verifying", "not_watched", False, ["check", "stop"]))
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertIs(self.shown()["may_run"], False)
        self.stop_while_globus_lists_the_copied_files()
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        self.assert_kept_by_the_stopped_check(before)

    def test_a_stop_of_a_check_of_a_failed_upload_whose_copy_is_complete(self):
        # The upload failed after Globus had finished the copy; its check checks the copy and would put it in place.
        # Stopped while it checks, the check gives the upload back with its failure, which Retry same request and
        # Check status continue.
        self.sdk.server.pace = 0
        self.sdk.script = {"task_successful_transfers": [FakeTransferAPIError(400, "BadRequest", "an odd answer")]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        before = self.shown()
        self.assertEqual((before["state"], before["error"]["reason"], before["may_run"], before["actions"]),
                         ("failed", "globus_error", False, ["retry", "check"]))
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.stop_while_globus_lists_the_copied_files()
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        self.assert_kept_by_the_stopped_check(before)

    def test_a_stop_before_the_check_checks_the_copy_keeps_a_transfer_that_had_ended(self):
        # The worker ended right after Globus finished the copy (SUCCEEDED is recorded), before it checked the copy:
        # "transferring", not watched, with nothing Globus may still run. The stop comes as the check asks Globus for
        # the task, before the check checks the copy: the upload keeps "transferring", never "verifying", which no
        # allowed state change leads back from (review R3-01: a check that keeps the stored state sees the stop first).
        self.sdk.server.pace = 0
        real = arc_globus.GlobusFlow.verifying

        def ended_before_it_checked(flow, *arguments, **options):
            if flow.ctx.action == "send":
                raise Crash()
            return real(flow, *arguments, **options)

        with mock.patch.object(arc_globus.GlobusFlow, "verifying", ended_before_it_checked), \
                self.assertRaises(Crash):
            self.work()
        before = self.shown()
        self.assertEqual((before["state"], before["display_state"], before["may_run"], before["actions"]),
                         ("transferring", "not_watched", False, ["check", "stop"]))
        self.assertEqual(self.record(self.upload_id)["globus"]["task"]["status"], "SUCCEEDED")
        self.ok("arc-check", "--upload=" + self.upload_id)

        def stop_then_answer(client, task_id, **options):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            time.sleep(0.3)
            return client.through("get_task", task_id, **options)

        self.sdk.script = {"get_task": [stop_then_answer]}
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)
        self.assert_kept_by_the_stopped_check(before)

    # -- review R3-01: a check that started while Globus could run the transfer is stopped as Stop upload stops

    def check_that_may_cancel(self, display_state):
        """Check status of an upload whose transfer Globus may still run (may_run true), so the window asks "Stop
        this upload? The check ends, and the transfer Globus may still be running is cancelled ... Files already
        copied stay in <incoming> on ARC". The transfer ends meanwhile, and nobody saw it. The person presses Stop
        right after the sign-in, while the check asks Globus for the task."""
        before = self.shown()
        self.assertEqual((before["display_state"], before["may_run"]), (display_state, True))
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertIs(self.shown()["may_run"], True)
        self.finish_tasks()

        def stop_then_answer(client, task_id, **options):
            arc_records.update_record(self.folder, lambda record: record["requests"].update(stop_utc="now"))
            time.sleep(0.3)
            return client.through("get_task", task_id, **options)

        self.sdk.script = {"get_task": [stop_then_answer]}
        self.assertEqual(self.work("check"), arc_worker.EXIT_ENDED)

    def assert_stopped_with_the_copy_complete(self):
        """As round 2 ended it, and as the Stop question said: "stopped", the copy Globus finished complete in
        _incoming, nothing cancelled or renamed. Check status then puts the copy in place."""
        record = self.record(self.upload_id)
        incoming = "%s/_incoming/%s.%s" % (ARC_FOLDER, SESSION, self.upload_id)
        self.assertEqual((record["state"], record["message"], record["error"]),
                         ("stopped", arc_globus.TEXTS["stopped_complete"].format(incoming=incoming), None))
        self.assertEqual(record["globus"]["task"]["status"], "SUCCEEDED")
        self.assertEqual((record["requests"]["stop_utc"], record["worker"]), (None, None))
        shown = self.shown()
        self.assertEqual((shown["display_state"], shown["may_run"], shown["actions"]),
                         ("stopped", False, ["retry", "check"]))
        self.assertEqual((self.sdk.calls("cancel_task"), self.sdk.calls("operation_rename")), ([], []))
        same_as_manifest(self, self.incoming())
        self.assertFalse(self.final().exists())
        texts = [event["text"] for event in arc_records.read_events(self.folder)]
        self.assertEqual(texts[-1], record["message"])
        self.assertNotIn(arc_globus.TEXTS["check_stopped"], texts)
        self.sdk.script = {}
        self.ok("arc-check", "--upload=" + self.upload_id)
        self.assertEqual(self.work("check"), arc_worker.EXIT_VERIFIED)
        same_as_manifest(self, self.final())

    def test_a_stop_of_a_check_of_an_uncertain_upload_whose_transfer_ended_meanwhile(self):
        self.sdk.script = {"submit_transfer": [lost_reply, FakeConnectionError("x"), FakeConnectionError("x")]}
        self.work()
        self.assertEqual(self.state(), ("uncertain", None))
        self.check_that_may_cancel("uncertain")
        self.assert_stopped_with_the_copy_complete()

    def test_a_stop_of_a_check_of_a_transfer_nobody_watched_that_ended_meanwhile(self):
        self.watched_then_killed()
        self.check_that_may_cancel("not_watched")
        self.assert_stopped_with_the_copy_complete()

    def test_a_stop_of_a_check_of_a_failed_upload_whose_transfer_still_ran(self):
        # The upload failed while Globus still copied (an odd answer while watching): a failed upload goes to
        # "stopped" only through verifying, as in round 2.
        self.sdk.script = {"get_task": [None, FakeTransferAPIError(400, "BadRequest", "an odd answer")]}
        self.assertEqual(self.work(), arc_worker.EXIT_ENDED)
        self.assertEqual(self.state(), ("failed", "globus_error"))
        self.check_that_may_cancel("failed")
        self.assert_stopped_with_the_copy_complete()


# ---------------------------------------------------------------------- the demo (design 12)


class Demo(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        self.demo_root = self.home / "data_catalog" / "arc_demo"

    def work(self, upload_id, action="send"):
        with contextlib.redirect_stderr(io.StringIO()):
            return arc_worker.run_worker(self.home, upload_id, action)

    def demo_final(self, area="simulated", name=SESSION):
        folder = self.demo_root / "projects" / "pcl_photonics" / "raman_globus_upload"
        return (folder / area if area == "simulated" else folder) / name

    def test_a_demo_upload_lands_in_the_demo_folder_as_on_arc(self):
        arc_support.ready_session(self.home)
        upload_id = self.upload("--transport=globus")["upload"]["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        record = self.record(upload_id)
        files = record["payload"]["files"]
        self.assertEqual(record["message"], "Demo: %d files in the demo folder on this Mac, every one checked. Nothing "
                                            "left this Mac." % files)
        same_as_manifest(self, self.demo_final())
        self.assertEqual(record["destination"]["local_final_path"], str(self.demo_final()))
        self.assertEqual(oct(os.stat(self.demo_root).st_mode & 0o777), "0o700")
        self.assertEqual((record["globus"]["identity"], record["verification"]["method"]),
                         ("demo (no account)", "globus-checksum"))
        events = arc_records.read_events(self.record_dir(upload_id), last=0)
        after_sign_in = [event for event in events if event["state"] in
                         ("signing_in", "checking", "submitting", "transferring", "verifying", "placing", "verified")]
        self.assertTrue(after_sign_in)
        for event in after_sign_in:
            self.assertTrue(event["text"].startswith("Demo: "), event)
        self.assertIn("Demo: no sign-in. A real upload opens your browser here.", [event["text"] for event in events])
        self.assertEqual(os.listdir(self.demo_final().parent / "_incoming"), [])

    def test_a_measured_session_goes_beside_simulated_not_into_it(self):
        arc_support.ready_session(self.home, folder="1d_x_scan_real", simulated=False)
        upload_id = self.upload("--transport=globus", session="1d_x_scan_real")["upload"]["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        same_as_manifest(self, self.demo_final("real", "1d_x_scan_real"))

    def test_check_status_of_a_verified_demo_upload(self):
        arc_support.ready_session(self.home)
        upload_id = self.upload("--transport=globus")["upload"]["upload_id"]
        self.work(upload_id)
        self.ok("arc-check", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_VERIFIED)
        check = self.record(upload_id)["checks"][-1]
        files = self.record(upload_id)["payload"]["files"]
        self.assertEqual((check["ok"], check["message"]),
                         (True, "Demo: still in the demo folder: %d files with the right sizes." % files))

    def test_a_check_whose_copy_globus_may_not_list_is_a_note(self):
        # Review R2-03 in the demo: the copy's folder is not readable, so the demo's Globus refuses to list it (403).
        # The check could not look: a note in the demo's words with the demo's fix, and the upload stays on record.
        arc_support.ready_session(self.home)
        upload_id = self.upload("--transport=globus")["upload"]["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        final = self.demo_final()
        kept = os.stat(final).st_mode & 0o7777
        os.chmod(final, 0)
        try:
            self.ok("arc-check", "--upload=" + upload_id)
            self.assertEqual(self.work(upload_id, "check"), arc_worker.EXIT_ENDED)
        finally:
            os.chmod(final, kept)
        note = self.record(upload_id)["checks"][-1]
        self.assertEqual({key: note[key] for key in ("ok", "looked", "message", "fix")}, {
            "ok": False, "looked": False,
            "message": "Demo: Globus may not list %s in the demo folder. Check status could not look." % final,
            "fix": arc_records.CHECK_FIXES["folder_not_readable_demo"]})
        shown = self.ok("arc-status", "--upload=" + upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["last_check"]), ("verified", note))
        self.assertEqual(self.ok("arc-list")["attention"], 0)

    def test_a_sign_in_that_did_not_happen_names_the_demo_folder(self):
        # Review R2-05: the demo's sign-in never fails, but where the words of one could show they name the demo
        # folder, never ARC: a retry whose sign-in did not happen says where the files copied earlier, or the copy
        # Globus finished, are (earlier_files_demo, earlier_complete_demo).
        arc_support.ready_session(self.home)
        arc_support.ready_session(self.home, folder="1d_y_scan_finished", scan_mode="1d-y")
        refused = mock.patch.object(arc_demo.DemoGlobus, "login",
                                    side_effect=GlobusCallError(0, arc_globus.SIGNIN_FAILED, "Login refused"))
        incoming = self.demo_final().parent / "_incoming"
        # Files copied earlier: the teaching fault failed the transfer halfway.
        partly = self.upload("--transport=globus", "--demo-fault=fail_task_once")["upload"]["upload_id"]
        self.assertEqual(self.work(partly), arc_worker.EXIT_ENDED)
        # The copy Globus finished: the upload program ended while it checked the copy, then the upload was stopped.
        complete = self.upload("--transport=globus", session="1d_y_scan_finished")["upload"]["upload_id"]

        def crash(name):
            if name == "verify_mid":
                raise Crash()

        with mock.patch.object(arc_worker, "fault", side_effect=crash), self.assertRaises(Crash):
            self.work(complete)
        self.assertTrue(self.ok("arc-stop", "--upload=" + complete)["stopped_now"])
        for upload_id, folder, where in ((partly, SESSION, "files copied earlier stay in"),
                                         (complete, "1d_y_scan_finished", "the copy Globus finished is complete in")):
            with self.subTest(where=where):
                self.ok("arc-retry", "--upload=" + upload_id)
                with refused:
                    self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
                record = self.record(upload_id)
                self.assertEqual((record["state"], record["error"]["reason"]), ("failed", "signin_failed"))
                shown = incoming / ("%s.%s" % (folder, upload_id))
                self.assertEqual(record["message"], "Demo: signing in to Globus did not work: Login refused. Nothing "
                                                    "more was sent; %s %s." % (where, shown))
                self.assertNotIn("on ARC", record["message"])

    def test_the_teaching_fault_is_shown_once(self):
        arc_support.ready_session(self.home)
        upload_id = self.upload("--transport=globus", "--demo-fault=fail_task_once")["upload"]["upload_id"]
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_ENDED)
        record = self.record(upload_id)
        self.assertEqual(record["error"]["reason"], "task_failed")
        self.assertTrue(record["message"].startswith(
            "Demo: Globus could not finish the copy: the connection to this Mac was lost."), record["message"])
        self.assertEqual(record["globus"]["demo_fault_used"], "fail_task_once")
        self.ok("arc-retry", "--upload=" + upload_id)
        self.assertEqual(self.work(upload_id), arc_worker.EXIT_VERIFIED)
        self.assertEqual(len(self.record(upload_id)["globus"]["earlier_attempts"]), 1)

    def test_the_demo_never_imports_the_globus_sdk(self):
        arc_support.ready_session(self.home)
        upload_id = self.upload("--transport=globus")["upload"]["upload_id"]
        program = ("import sys\n"
                   "from raman_datafed import arc_worker\n"
                   "code = arc_worker.main(['--home', sys.argv[1], '--upload', sys.argv[2], '--action', 'send'])\n"
                   "print(sorted(name for name in sys.modules if name.split('.')[0] == 'globus_sdk'))\n"
                   "sys.exit(code)\n")
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        done = subprocess.run([sys.executable, "-c", program, str(self.home), upload_id], capture_output=True,
                              text=True, timeout=60, cwd=str(self.home), env=env)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "[]"), done.stderr)
        self.assertEqual(self.record(upload_id)["state"], "verified")


class DemoService(unittest.TestCase):
    """arc_demo.DemoGlobus on its own: Globus's codes, Duplicate, tasks on disk, paced copies."""

    def setUp(self):
        arc_support.guard(self)
        arc_support.no_network(self)
        self.home = arc_support.arc_home(self)
        self.root = self.home / "data_catalog" / "arc_demo"
        self.now = [5000.0]
        self.service = self.make()
        self.payload = self.home / "payload" / "folder"
        (self.payload / "dark").mkdir(parents=True)
        for index in range(5):
            (self.payload / ("point_%04d.json" % index)).write_text("x" * (index + 1), encoding="utf-8")
        (self.payload / "dark" / "dark.txt").write_text("dark", encoding="utf-8")

    def make(self, pace=0):
        return arc_demo.DemoGlobus(self.root, "/projects", mac_root=self.home, pace=pace, clock=lambda: self.now[0])

    def body(self, submission):
        return {"DATA_TYPE": "transfer", "submission_id": submission, "label": "Raman upload u000000000001 a1 folder",
                "source_endpoint": arc_demo.DEMO_MAC_COLLECTION, "destination_endpoint": arc_demo.DEMO_ARC_COLLECTION,
                "DATA": [{"DATA_TYPE": "transfer_item", "source_path": str(self.payload) + "/",
                          "destination_path": "/team/upload/_incoming/folder.u000000000001/", "recursive": True}]}

    def code(self, function, *arguments):
        with self.assertRaises(GlobusCallError) as caught:
            function(*arguments)
        return caught.exception.http_status, caught.exception.code

    def test_globus_codes(self):
        arc = arc_demo.DEMO_ARC_COLLECTION
        self.assertEqual(self.code(self.service.stat, arc, "/team"), (404, "ClientError.NotFound"))
        self.service.mkdir(arc, "/team")
        self.assertEqual(self.code(self.service.mkdir, arc, "/team"), (502, "ExternalError.MkdirFailed.Exists"))
        self.assertTrue((self.root / "projects" / "team").is_dir())
        self.service.mkdir(arc, "/team/a")
        self.service.mkdir(arc, "/team/b")
        (self.root / "projects" / "team" / "b" / "kept.txt").write_text("kept", encoding="utf-8")
        self.assertEqual(self.code(self.service.rename, arc, "/team/a", "/team/b"), (409, "Exists"))
        self.assertTrue((self.root / "projects" / "team" / "b" / "kept.txt").exists())
        self.assertEqual(self.code(self.service.rename, arc, "/team/none", "/team/c"), (404, "ClientError.NotFound"))
        self.assertEqual(self.code(self.service.ls, arc, "/team/../x"), (400, "ClientError.BadRequest"))
        self.assertEqual(self.code(self.service.ls, arc_demo.DEMO_MAC_COLLECTION, "/"),
                         (403, "ClientError.PermissionDenied"))
        self.assertEqual(self.code(self.service.get_endpoint, "00000000-0000-4000-8000-000000000000"),
                         (404, "ClientError.NotFound"))
        self.assertEqual([entry["name"] for entry in self.service.ls(arc, "/team", 0, 1)], ["a"])
        self.assertEqual([entry["name"] for entry in self.service.ls(arc, "/team", 1, 1)], ["b"])
        self.assertEqual(self.service.stat(arc, "/")["type"], "dir")

    def test_a_known_request_number_is_a_duplicate_of_the_same_task_for_every_worker(self):
        first = self.service.submit(self.body("sid-1"))
        self.assertEqual(first["code"], "Accepted")
        again = self.make().submit(self.body("sid-1"))
        self.assertEqual((again["code"], again["task_id"]), ("Duplicate", first["task_id"]))
        self.assertEqual(self.service.posts_of("sid-1"), 2)
        task = self.make().get_task(first["task_id"])
        self.assertEqual((task["status"], task["files_transferred"]), ("SUCCEEDED", 6))
        self.assertNotIn("_demo", task)
        self.assertEqual(len(self.make().task_list({"label": "Raman upload u000000000001 a1 folder"})), 1)
        self.assertEqual(self.make().task_list({"label": "another"}), [])

    def test_tasks_copy_by_elapsed_time_and_pace_with_md5_checksums(self):
        import hashlib

        service = self.make(pace=1.0)
        task_id = service.submit(self.body("sid-2"))["task_id"]
        self.assertEqual(service.get_task(task_id)["files_transferred"], 0)
        self.now[0] += 2.5
        self.assertEqual(service.get_task(task_id)["files_transferred"], 2)
        self.now[0] += 100
        self.assertEqual(service.get_task(task_id)["status"], "SUCCEEDED")
        with mock.patch.object(arc_demo, "TRANSFERS_PAGE", 4):
            first = service.successful_transfers(task_id)
            second = service.successful_transfers(task_id, first["next_marker"])
        self.assertEqual((len(first["DATA"]), first["next_marker"], len(second["DATA"]), second["next_marker"]),
                         (4, "4", 2, None))
        for item in first["DATA"] + second["DATA"]:
            source = Path(item["source_path"])
            self.assertEqual(item["checksum"], hashlib.md5(source.read_bytes()).hexdigest())
            self.assertEqual(item["checksum_algorithm"], "MD5")
            relative = item["destination_path"].split("/_incoming/folder.u000000000001/", 1)[1]
            copy_path = self.root / "projects" / "team" / "upload" / "_incoming" / "folder.u000000000001" / relative
            self.assertEqual(copy_path.read_bytes(), source.read_bytes())
            self.assertEqual(os.stat(copy_path).st_mtime_ns, os.stat(source).st_mtime_ns)

    def test_cancel(self):
        service = self.make(pace=1.0)
        task_id = service.submit(self.body("sid-3"))["task_id"]
        self.assertEqual(service.cancel(task_id)["code"], "Canceled")
        task = service.get_task(task_id)
        self.assertEqual((task["status"], task["fatal_error"]["code"]), ("FAILED", "CANCELED"))
        self.assertEqual(service.cancel(task_id)["code"], "TaskComplete")
        self.assertEqual(service.task_events(task_id)[0]["code"], "CANCELED")

    def test_the_teaching_faults(self):
        used = []
        service = arc_demo.DemoGlobus(self.root, "/projects", mac_root=self.home, pace=0, fault="lose_submit_reply",
                                      on_fault=used.append)
        with self.assertRaises(GlobusCallError) as caught:
            service.submit(self.body("sid-4"))
        self.assertEqual((caught.exception.http_status, caught.exception.code), (0, "network"))
        self.assertEqual(service.submit(self.body("sid-4"))["code"], "Duplicate")
        service = arc_demo.DemoGlobus(self.root, "/projects", mac_root=self.home, pace=0, fault="fail_task_once",
                                      on_fault=used.append)
        task_id = service.submit(self.body("sid-5"))["task_id"]
        task = service.get_task(task_id)
        self.assertEqual((task["status"], task["files_transferred"], task["fatal_error"]["description"]),
                         ("FAILED", 3, "the connection to this Mac was lost"))
        self.assertEqual(service.get_task(service.submit(self.body("sid-6"))["task_id"])["status"], "SUCCEEDED")
        self.assertEqual(used, ["lose_submit_reply", "fail_task_once"])

    def test_the_demo_folder_is_made_once_and_an_existing_folder_is_never_chmodded(self):
        """Decision I6 (reviews SEC-05, COR-09): the demo service takes its folder from
        arc_records.ensure_demo_root, which never changes the mode of a folder it did not make."""
        fresh = self.home / "fresh_demo"
        arc_demo.DemoGlobus(fresh, pace=0)
        self.assertEqual(oct(os.stat(fresh).st_mode & 0o777), "0o700")
        self.assertTrue((fresh / arc_records.DEMO_MARKER).is_file())
        arc_demo.DemoGlobus(fresh, pace=0)
        shared = self.home / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        (shared / "notes.txt").write_text("someone's notes", encoding="utf-8")
        with self.assertRaises(arc_records.ArcError) as caught:
            arc_demo.DemoGlobus(shared, pace=0)
        self.assertEqual((caught.exception.kind, caught.exception.reason), ("config", "demo_folder_unwritable"))
        self.assertEqual(oct(os.stat(shared).st_mode & 0o777), "0o755")
        self.assertEqual(os.listdir(shared), ["notes.txt"])
        empty = self.home / "empty"
        empty.mkdir()
        os.chmod(empty, 0o755)
        arc_demo.DemoGlobus(empty, pace=0)
        self.assertEqual(oct(os.stat(empty).st_mode & 0o777), "0o755")
        self.assertTrue((empty / arc_records.DEMO_MARKER).is_file())

    def test_the_sign_in_pause_scales_with_the_pace(self):
        pauses = []
        arc_demo.DemoGlobus(self.root, pace=0.1, sleep=pauses.append).login()
        arc_demo.DemoGlobus(self.root, pace=0, sleep=pauses.append).login()
        self.assertEqual(pauses, [1.5])
        self.assertEqual(arc_demo.DemoGlobus(self.root, pace=0).identity(), "demo (no account)")


# ---------------------------------------------------------------------- crash and restart (design 13, rows 5 to 12, 21)


class RecoveryRows(arc_support.ArcCase):
    """Each row: a real worker process ends at the step with os._exit(97) (RAMAN_ARC_FAULT), then the next
    command or worker continues. Demo transports, pace 0."""

    def setUp(self):
        super().setUp()
        arc_support.ready_session(self.home)
        self.demo_root = self.home / "data_catalog" / "arc_demo"

    def start(self, *options):
        self.upload_id = self.upload("--transport=globus", *options)["upload"]["upload_id"]
        self.folder = self.record_dir(self.upload_id)

    def worker(self, action="send", fault=None):
        extra = {"RAMAN_ARC_TEST_HOOKS": "1", "RAMAN_ARC_FAULT": fault} if fault else None
        code, _stdout, stderr = arc_support.run_worker_program(self.home, "--upload", self.upload_id, "--action",
                                                               action, extra_environment=extra)
        return code

    def service(self):
        return arc_demo.DemoGlobus(self.demo_root, pace=0)

    def base(self):
        return self.demo_root / "projects" / "pcl_photonics" / "raman_globus_upload" / "simulated"

    def faulted(self, fault):
        self.assertEqual(self.worker(fault=fault), arc_worker.EXIT_FAULT)
        self.assertTrue(arc_support.wait_for_workers(self.home))

    def retried(self, action="arc-retry"):
        self.ok(action, "--upload=" + self.upload_id)
        self.assertEqual(self.worker("check" if action == "arc-check" else "send"), arc_worker.EXIT_VERIFIED)

    def assert_one_folder(self, tasks=1):
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "verified")
        self.assertEqual(sorted(os.listdir(self.base())), sorted([SESSION, "_incoming"]))
        self.assertEqual(os.listdir(self.base() / "_incoming"), [])
        same_as_manifest(self, self.base() / SESSION)
        self.assertEqual(len(self.service().task_list(limit=100)), tasks)
        return record

    def test_row_5_checking(self):
        self.start()
        self.faulted("check_mid")
        self.assertEqual(self.record(self.upload_id)["state"], "checking")
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["display_state"], "interrupted")
        self.assertTrue((self.base() / "_incoming" / ("%s.%s" % (SESSION, self.upload_id))).is_dir())
        self.retried()
        self.assert_one_folder()

    def test_row_6_request_saved_before_the_post(self):
        self.start()
        self.faulted("submit_before_post")
        record = self.record(self.upload_id)
        submission = record["globus"]["submission_id"]
        self.assertEqual((record["state"], record["globus"]["posts"], record["globus"]["task_id"]),
                         ("submitting", [], None))
        self.assertEqual(json.loads((self.folder / arc_globus.REQUEST_FILE).read_text())["submission_id"], submission)
        self.assertEqual(self.service().posts_of(submission), 0)
        self.retried()
        record = self.assert_one_folder()
        self.assertEqual(record["globus"]["submission_id"], submission)
        self.assertEqual(self.service().posts_of(submission), 1)
        self.assertEqual(record["globus"]["earlier_attempts"], [])

    def test_row_7_post_accepted_task_not_saved(self):
        self.start()
        self.faulted("submit_after_post")
        record = self.record(self.upload_id)
        submission = record["globus"]["submission_id"]
        self.assertEqual((record["globus"]["task_id"], record["globus"]["posts"]), (None, []))
        self.assertEqual(self.service().posts_of(submission), 1)
        self.retried()
        record = self.assert_one_folder()
        self.assertEqual(self.service().posts_of(submission), 2)
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["duplicate"])
        self.assertEqual(record["globus"]["submission_id"], submission)

    def test_row_8_reply_lost(self):
        self.start("--demo-fault=lose_submit_reply")
        self.assertEqual(self.worker(), arc_worker.EXIT_VERIFIED)
        record = self.assert_one_folder()
        self.assertEqual([post["outcome"] for post in record["globus"]["posts"]], ["unknown", "duplicate"])
        texts = [event["text"] for event in arc_records.read_events(self.folder, last=0)]
        self.assertIn("Demo: it is not known whether Globus received the request; asking again with the same request "
                      "number.", texts)
        self.assertIn("Demo: Globus answered: already received. One copy.", texts)

    def test_row_9_worker_killed_while_the_task_runs(self):
        self.start()
        self.faulted("poll_mid_transfer")
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        self.assertEqual((shown["state"], shown["display_state"], shown["actions"]),
                         ("transferring", "not_watched", ["check", "stop"]))
        self.retried("arc-check")
        self.assert_one_folder()

    def test_row_10_task_failed(self):
        self.start("--demo-fault=fail_task_once")
        self.assertEqual(self.worker(), arc_worker.EXIT_ENDED)
        incoming = self.base() / "_incoming" / ("%s.%s" % (SESSION, self.upload_id))
        self.assertTrue(files_below(incoming))
        self.assertEqual(self.ok("arc-status", "--upload=" + self.upload_id)["upload"]["leftover_on_arc"],
                         str(incoming))
        self.retried()
        record = self.assert_one_folder(tasks=2)
        self.assertEqual(len(record["globus"]["earlier_attempts"]), 1)
        self.assertEqual(record["globus"]["earlier_attempts"][0]["status"], "FAILED")
        self.assertEqual(record["globus"]["attempt"], 2)

    def test_row_11_verifying(self):
        self.start()
        self.faulted("verify_mid")
        shown = self.ok("arc-status", "--upload=" + self.upload_id)["upload"]
        # Globus has a finished task: the listing offers Check status, which checks again and places.
        self.assertEqual((shown["state"], shown["display_state"], shown["actions"]),
                         ("verifying", "not_watched", ["check", "stop"]))
        self.retried("arc-check")
        self.assert_one_folder()

    def test_row_12_after_the_rename(self):
        self.start()
        self.faulted("place_after_rename")
        record = self.record(self.upload_id)
        self.assertEqual(record["state"], "placing")
        self.assertTrue((self.base() / SESSION).is_dir())
        self.assertFalse((self.base() / "_incoming" / ("%s.%s" % (SESSION, self.upload_id))).exists())
        self.retried("arc-check")
        record = self.assert_one_folder()
        self.assertEqual(record["destination"]["placed_as"], SESSION)
        texts = [event["text"] for event in arc_records.read_events(self.folder, last=0)]
        self.assertIn("Demo: the copy was already in place as %s." % SESSION, texts)

    def test_row_21_the_request_number_is_on_disk_before_the_post(self):
        self.start()
        seen = fsynced_paths(self)
        real_submit = arc_demo.DemoGlobus.submit
        checked = []

        def submit(service, body):
            folder = str(self.folder.resolve())
            request = [index for index, path in enumerate(seen) if path and arc_globus.REQUEST_FILE in path]
            records = [index for index, path in enumerate(seen) if path and "record.json" in path and
                       request and index > request[-1]]
            folders = [index for index, path in enumerate(seen) if path == folder]
            self.assertTrue(request and records, seen)
            self.assertTrue(any(index > max(records) for index in folders), seen)
            on_disk = arc_records.read_record(self.folder)["globus"]["submission_id"]
            self.assertEqual(on_disk, body["submission_id"])
            checked.append(True)
            return real_submit(service, body)

        with mock.patch.object(arc_demo.DemoGlobus, "submit", submit), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(arc_worker.run_worker(self.home, self.upload_id, "send"), arc_worker.EXIT_VERIFIED)
        self.assertEqual(checked, [True])


class RealSignal(arc_support.ArcCase):
    """Decision I8 (review COR-01) with a real detached worker process: SIGTERM, which macOS sends at a
    logout, a restart or a shutdown, ends the worker at once and cancels nothing."""

    def test_sigterm_to_a_detached_worker_leaves_the_transfer_running(self):
        import signal

        arc_support.ready_session(self.home)
        upload_id = self.upload("--transport=globus")["upload"]["upload_id"]
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        env["RAMAN_ARC_DEMO_PACE"] = "0.2"
        process = subprocess.Popen(arc_worker.worker_argv(self.home, upload_id, "send"), cwd=str(self.home), env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and self.record(upload_id)["state"] != "transferring":
            time.sleep(0.05)
        self.assertEqual(self.record(upload_id)["state"], "transferring")
        time.sleep(0.3)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
        record = self.record(upload_id)
        self.assertEqual(record["state"], "transferring")
        shown = self.ok("arc-status", "--upload=" + upload_id)["upload"]
        self.assertEqual((shown["display_state"], shown["actions"]), ("not_watched", ["check", "stop"]))
        task = json.loads((self.home / "data_catalog" / "arc_demo" / arc_demo.SERVICE_FOLDER / arc_demo.TASKS_FOLDER /
                           (record["globus"]["task_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual((task["status"], task["fatal_error"]), ("ACTIVE", None))


class Texts(unittest.TestCase):
    """The Globus texts name only what the person can do (reviews GLO-08, COR-07, GLO-V01, GLO-V03, DOC-02)."""

    def test_the_texts_name_the_buttons_that_do_it(self):
        self.assertIn("Retry same request or Check status checks it and puts it in place.",
                      arc_records.MESSAGES["stopped_complete"])
        _message, fix, _retryable = arc_records.WORKER_RESULTS["request_conflict"]
        self.assertNotIn("Check status", fix)
        self.assertIn("Retry same request", fix)
        _message, fix, retryable = arc_records.WORKER_RESULTS["task_not_visible"]
        self.assertNotIn("Retry same request", fix)
        self.assertIn("Check status", fix)
        awake = "Keep this Mac awake until the copy is done."
        self.assertTrue(arc_records.MESSAGES["not_watched"].endswith(awake))
        self.assertTrue(arc_globus.TEXTS["unreachable"].endswith(awake))
        self.assertNotIn(awake, arc_records.MESSAGES["not_watched_demo"])
        message, fix, retryable = arc_records.WORKER_RESULTS["mac_collection_not_yours"]
        self.assertFalse(retryable)
        self.assertIn("{identity}", message)
        for text in (message, fix):
            self.assertNotIn("Nothing was sent", text)

    def test_a_request_conflict_names_only_buttons_it_offers(self):
        record = {"schema": arc_records.SCHEMA, "state": "failed", "transport": "globus", "demo": False,
                  "payload": {"kept": True},
                  "error": arc_records.worker_error("request_conflict"),
                  "globus": {"submission_id": "a-request", "task_id": None, "posts": [{"outcome": "conflict"}]}}
        offered = arc_records.actions(record, "failed", False)
        named = [action for words, action in (("Retry same request", "retry"), ("Check status", "check"),
                                              ("Stop upload", "stop")) if words in record["error"]["fix"]]
        self.assertTrue(named)
        for action in named:
            self.assertIn(action, offered)


# ---------------------------------------------------------------------- the real SDK (read, never used)


@unittest.skipUnless(HAVE_SDK, "globus-sdk is not installed")
class RealSDK(arc_support.ArcCase):
    def setUp(self):
        super().setUp()
        import globus_sdk

        self.sdk = globus_sdk

    def test_the_guard_refuses_the_real_sdk(self):
        with self.assertRaises(arc_worker.GuardRefused):
            arc_globus.LiveGlobus({"client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2"}, sdk=self.sdk)

    def test_the_quiet_manager_passes_the_fixed_label_and_the_loopback_address(self):
        import socket

        manager = arc_globus.quiet_login_flow_manager(self.sdk)
        self.assertTrue(issubclass(manager, self.sdk.login_flows.LocalServerLoginFlowManager))
        config = self.sdk.GlobusAppConfig(login_flow_manager=manager, token_storage="memory",
                                          request_refresh_tokens=False, auto_redrive_gares=False,
                                          token_validation_error_handler=None)
        from globus_sdk.globus_app.protocols import LoginFlowManagerProvider

        self.assertIsInstance(manager, LoginFlowManagerProvider)
        client = self.sdk.NativeAppAuthClient(client_id="61338d24-54d5-408f-a10d-66c06b59f6d2")
        self.addCleanup(client.close)
        flow = manager.for_globus_app(app_name="Portable Raman ARC upload", login_client=client, config=config)
        self.assertEqual((flow.native_prefill_named_grant, flow.server_address, flow.request_refresh_tokens),
                         ("Portable Raman ARC upload", ("127.0.0.1", 0), False))
        self.assertNotIn(socket.gethostname(), flow.native_prefill_named_grant)

    def test_the_patched_redirect_handler_writes_nothing(self):
        from globus_sdk.login_flows.local_server_login_flow_manager import local_server

        handler_class = local_server.RedirectHandler
        had_own = "log_message" in vars(handler_class)
        before = vars(handler_class).get("log_message")

        def restore():
            if had_own:
                handler_class.log_message = before
            elif "log_message" in vars(handler_class):
                del handler_class.log_message

        self.addCleanup(restore)
        handler = handler_class.__new__(handler_class)
        handler.requestline = "GET /?code=not-a-real-code&state=not-a-real-state HTTP/1.1"
        handler.request_version, handler.command = "HTTP/1.1", "GET"
        handler.client_address = ("127.0.0.1", 50000)
        written = io.StringIO()
        with contextlib.redirect_stderr(written):
            handler.log_request(200)
        self.assertIn("code=", written.getvalue())
        arc_globus.quiet_redirect_handler(self.sdk)
        written = io.StringIO()
        with contextlib.redirect_stderr(written):
            handler.log_request(200)
            handler.log_error("%s", "code=not-a-real-code")
        self.assertEqual(written.getvalue(), "")

    def test_the_calls_exist_with_these_signatures(self):
        import inspect

        sdk = self.sdk
        self.assertEqual(sdk.__version__, "4.10.0")
        transfer = sdk.TransferClient
        calls = {
            "get_endpoint": ((MAC_COLLECTION,), {}),
            "operation_ls": ((MAC_COLLECTION, "/x/"), {"show_hidden": True, "limit": 1000, "offset": 0}),
            "operation_stat": ((ARC_COLLECTION, "/x"), {}),
            "operation_mkdir": ((ARC_COLLECTION, "/x"), {}),
            "operation_rename": ((ARC_COLLECTION, "/x", "/y"), {}),
            "get_submission_id": ((), {}),
            "submit_transfer": (({},), {}),
            "get_task": (("t",), {}),
            "task_list": ((), {"filter": {"label": "x"}, "limit": 10}),
            "task_successful_transfers": (("t",), {"marker": "m"}),
            "cancel_task": (("t",), {}),
            "task_event_list": (("t",), {"limit": 10}),
        }
        for name, (arguments, options) in calls.items():
            inspect.signature(getattr(transfer, name)).bind(None, *arguments, **options)
        inspect.signature(sdk.UserApp).bind("Portable Raman ARC upload", client_id="x", config=None)
        inspect.signature(sdk.AuthClient).bind(app=None, app_scopes=[sdk.Scope("openid")])
        inspect.signature(sdk.TransferClient).bind(app=None)
        inspect.signature(sdk.UserApp.login).bind(None, auth_params=None)
        inspect.signature(sdk.UserApp.add_scope_requirements).bind(None, {"auth.globus.org": ["openid"]})
        for name in ("logout", "close"):
            inspect.signature(getattr(sdk.UserApp, name)).bind(None)
        self.assertTrue(callable(sdk.AuthClient.userinfo))
        fields = {field.name for field in __import__("dataclasses").fields(sdk.GlobusAppConfig)}
        self.assertLessEqual({"login_flow_manager", "token_storage", "request_refresh_tokens", "auto_redrive_gares",
                              "token_validation_error_handler"}, fields)
        self.assertEqual(sdk.gare.GlobusAuthorizationParameters(prompt="login").prompt, "login")
        self.assertTrue(callable(sdk.gare.to_gare))
        self.assertTrue(issubclass(sdk.login_flows.LocalServerEnvironmentalLoginError,
                                   sdk.login_flows.LocalServerLoginError))
        self.assertTrue(issubclass(sdk.TransferAPIError, sdk.GlobusAPIError))
        self.assertTrue(issubclass(sdk.GlobusConnectionError, sdk.NetworkError))
        self.assertTrue(issubclass(sdk.GlobusTimeoutError, sdk.NetworkError))
        self.assertTrue(hasattr(sdk.token_storage, "TokenValidationError"))


class Logging(arc_support.ArcCase):
    def test_the_log_filter_drops_a_line_with_a_sign_in_code(self):
        import logging

        drop = arc_worker._SecretFilter()
        for text in ('"GET /?code=not-a-real-code&state=x HTTP/1.1" 200 -', "Authorization: Bearer x"):
            self.assertFalse(drop.filter(logging.LogRecord("x", logging.INFO, "f", 1, text, (), None)))
        self.assertTrue(drop.filter(logging.LogRecord("x", logging.INFO, "f", 1, "Signed in.", (), None)))

    def test_the_module_imports_no_sdk(self):
        program = ("import sys\nfrom raman_datafed import arc_globus, arc_demo\n"
                   "print(sorted(name for name in sys.modules if name.split('.')[0] == 'globus_sdk'))\n")
        env = support.cli_environment(self.home)
        env.update(arc_support.GUARD_ENVIRONMENT)
        done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=60, env=env,
                              cwd=str(self.home))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "[]"), done.stderr)
