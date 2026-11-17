"""The command-line contract: exactly one JSON object on stdout, logs on stderr, exit codes.

The command is run the way the controller runs it: as a child process, with HOME as the
working directory and data_layer on PYTHONPATH.
"""

import io
import json
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from raman_catalog import cli
from raman_catalog.catalog import raw_alias
from raman_catalog.ledger import Ledger
from raman_catalog.local_catalog import LocalDemoCatalog

PLUGIN = '''
def hello(args, ctx):
    return {"greeting": "hello " + args.name, "home": ctx.home, "root": ctx.config.root_alias}


def serve(args, ctx):
    ctx.emit({"url": "http://127.0.0.1:1/"})
    print("a library printing on stdout")
    return {"never": "printed"}


def broken(args, ctx):
    raise RuntimeError("internal fault\\nwith a second line")


def register(subparsers):
    command = subparsers.add_parser("hello")
    command.add_argument("--name", default="there")
    command.set_defaults(func=hello)
    subparsers.add_parser("serve").set_defaults(func=serve)
    subparsers.add_parser("broken").set_defaults(func=broken)
'''


def one_json_object(test, stdout):
    """The object on stdout, after checking that stdout holds exactly that and nothing else."""
    test.assertEqual(stdout.count("\n"), 1, stdout)
    test.assertTrue(stdout.endswith("\n"))
    value = json.loads(stdout)
    test.assertIsInstance(value, dict)
    return value


class Contract(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)

    def run_cli(self, *arguments):
        code, stdout, stderr = support.run_cli(self.home, *arguments)
        return code, one_json_object(self, stdout), stderr

    def test_every_core_command_prints_one_json_object_and_exits_0(self):
        support.write_dark(self.home)
        support.make_session(self.home, points=2)
        commands = [
            ("status",), ("sync",), ("sync", "--retry-failed"), ("list",),
            ("list", "--limit", "1", "--state", "uploaded"),
            ("config",), ("config", "--set", "sync.max_attempts=4", "review.confidence_threshold=0.75"),
            ("doctor",), ("offline",), ("offline", "--on"), ("status",), ("sync",),
            ("offline", "--off"),
        ]
        for arguments in commands:
            code, result, _stderr = self.run_cli(*arguments)
            self.assertEqual((code, result["ok"]), (0, True), arguments)
            self.assertEqual(list(result)[0], "ok")
        listing = self.run_cli("list")[1]
        self.assertEqual(listing["total"], 2)
        self.assertEqual({item["state"] for item in listing["items"]}, {"uploaded"})
        record_id = listing["items"][0]["record_id"]
        code, chain, _stderr = self.run_cli("provenance", "--record", record_id)
        self.assertEqual((code, chain["nodes"][0]["id"]), (0, record_id))

    def test_status_keys(self):
        code, status, _stderr = self.run_cli("status")
        self.assertEqual(code, 0)
        for key in ("mode", "mode_label", "demo", "online", "user", "context", "endpoint", "counts", "pending",
                    "last_sync_utc", "problems"):
            self.assertIn(key, status)
        self.assertEqual((status["mode"], status["demo"]), ("local-demo", True))

    def test_failure_is_a_json_object_with_a_non_zero_exit_code(self):
        code, result, _stderr = self.run_cli("provenance", "--record", "d/404")
        self.assertEqual(code, 1)
        self.assertEqual((result["ok"], result["kind"]), (False, "not_found"))
        self.assertEqual(set(result), {"ok", "error", "detail", "kind"})
        self.assertIn("d/404", result["error"])

        code, result, _stderr = self.run_cli("import-session", "--folder", str(self.home / "nowhere"))
        self.assertEqual((code, result["ok"]), (1, False))

    def test_wrong_arguments_are_a_json_failure_too(self):
        for arguments in (("no-such-command",), (), ("list", "--state", "finished"), ("provenance",),
                          ("list", "--limit", "many"), ("offline", "--on", "--off"), ("status", "--surprise")):
            code, result, stderr = self.run_cli(*arguments)
            self.assertEqual((code, result["ok"], result["kind"]), (2, False, "usage"), arguments)
            self.assertIn("usage:", result["detail"])
            self.assertEqual(stderr, "")

    def test_help_is_a_json_object(self):
        for arguments in (("-h",), ("sync", "--help")):
            code, result, _stderr = self.run_cli(*arguments)
            self.assertEqual((code, result["ok"]), (0, True))
            self.assertIn("usage:", result["help"])

    def test_logs_go_to_stderr(self):
        support.make_session(self.home, points=1, dark_file=None)
        self.assertEqual(self.run_cli("offline", "--on")[0], 0)
        code, result, stderr = self.run_cli("sync")
        self.assertEqual((code, result["online"], result["ingested"]), (0, False, 1))
        self.assertIn("not reachable", stderr)
        self.assertEqual(self.run_cli("offline", "--off")[0], 0)
        code, result, stderr = self.run_cli("--verbose", "sync")
        self.assertEqual((code, result["registered"]), (0, 1))
        self.assertIn("DEBUG raman_catalog.sync: registered", stderr)

    def test_internal_error_has_no_traceback_on_stdout(self):
        (self.home / "data_catalog").mkdir()
        (self.home / "data_catalog" / "ledger.sqlite").write_text("this is not a database", encoding="utf-8")
        code, result, stderr = self.run_cli("list")
        self.assertEqual((code, result["ok"], result["kind"]), (1, False, "internal"))
        self.assertIn("DatabaseError", result["error"])
        self.assertIn("Traceback", stderr)

    def test_missing_home_and_broken_settings(self):
        code, stdout, _stderr = support.run_cli(self.home / "missing", "status", user_home=self.home)
        self.assertEqual(code, 1)
        self.assertIn("HOME folder does not exist", one_json_object(self, stdout)["error"])

        (self.home / "catalog_config.json").write_text("{broken", encoding="utf-8")
        code, result, _stderr = self.run_cli("status")
        self.assertEqual((code, result["kind"]), (1, "config"))

    def test_home_defaults_to_the_working_directory(self):
        done = subprocess.run([sys.executable, "-m", "raman_catalog", "config"], capture_output=True, text=True,
                              cwd=str(self.home), env=support.cli_environment(self.home), timeout=60)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(one_json_object(self, done.stdout)["path"], str(self.home / "catalog_config.json"))

    def test_output_is_utf8_whatever_the_locale(self):
        support.write_dark(self.home)
        support.make_session(self.home, points=1, sample_id="Probe äö 水")
        env = support.cli_environment(self.home)
        env.update(LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONIOENCODING="ascii")
        for arguments in (("sync",), ("list",)):
            done = subprocess.run([sys.executable, "-m", "raman_catalog", "--home", str(self.home), *arguments],
                                  capture_output=True, cwd=str(self.home), env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
        listing = one_json_object(self, done.stdout.decode("utf-8"))
        self.assertEqual(listing["items"][0]["sample_id"], "Probe äö 水")


class TwoProcesses(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        support.write_dark(self.home)

    def test_sync_while_another_process_holds_the_lock_returns_busy_quickly(self):
        support.make_session(self.home, points=2)
        (self.home / "data_catalog").mkdir()
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys, time\n"
             "handle = open(sys.argv[1], 'a')\n"
             "fcntl.flock(handle, fcntl.LOCK_EX)\n"
             "print('locked', flush=True)\n"
             "time.sleep(60)\n",
             str(self.home / "data_catalog" / "sync.lock")],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "locked")

        started = time.monotonic()
        code, stdout, _stderr = support.run_cli(self.home, "sync")
        elapsed = time.monotonic() - started
        result = one_json_object(self, stdout)
        self.assertEqual((code, result["ok"], result["busy"], result["ingested"]), (0, True, True, 0))
        self.assertLess(elapsed, 10)

        holder.kill()
        holder.wait()
        code, stdout, _stderr = support.run_cli(self.home, "sync")
        result = one_json_object(self, stdout)
        self.assertEqual((result["busy"], result["ingested"]), (False, 2))

    def test_syncs_started_together_register_every_capture_exactly_once(self):
        first = support.make_session(self.home, folder="1d_x_scan_a", points=7)
        second = support.make_session(self.home, folder="2d_scan_b", points=6, scan_mode="2d")
        command = [sys.executable, "-m", "raman_catalog", "--home", str(self.home), "sync"]
        for _round in range(3):
            running = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        cwd=str(self.home), env=support.cli_environment(self.home))
                       for _process in range(4)]
            for process in running:
                stdout, stderr = process.communicate(timeout=120)
                result = one_json_object(self, stdout)
                self.assertEqual((process.returncode, result["ok"]), (0, True), stderr)
                self.assertIsInstance(result["busy"], bool)

        ledger = Ledger(self.home / "data_catalog")
        self.addCleanup(ledger.close)
        rows = ledger.rows()
        capture_ids = first["capture_ids"] + second["capture_ids"]
        self.assertEqual(sorted(row["capture_id"] for row in rows), sorted(capture_ids))
        self.assertEqual({row["state"] for row in rows}, {"uploaded"})
        catalog = LocalDemoCatalog(self.home / "data_catalog" / "local_demo")
        record_ids = {catalog.get_record(raw_alias(capture_id)).id for capture_id in capture_ids}
        self.assertEqual(len(record_ids), 13)
        self.assertEqual({row["record_id"] for row in rows}, record_ids)
        # No record was made twice.
        kinds = support.record_types(self.home / "data_catalog" / "local_demo")
        self.assertEqual((kinds["raman.raw_spectrum"], kinds["raman.dark_spectrum"]), (13, 1))


class PlugIns(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.modules = support.make_home(self)
        (self.modules / "demo_commands.py").write_text(PLUGIN, encoding="utf-8")
        (self.modules / "broken_part.py").write_text("import a_package_nobody_installed\n", encoding="utf-8")
        patcher = mock.patch.object(sys, "path", [str(self.modules)] + sys.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(sys.modules.pop, "demo_commands", None)

    def main(self, *arguments, modules=("raman_catalog.commands_core", "demo_commands")):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "COMMAND_MODULES", list(modules)):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--home", str(self.home), *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_the_command_modules_of_the_edition(self):
        self.assertEqual(cli.COMMAND_MODULES, ["raman_catalog.commands_core", "raman_catalog.job",
                                               "raman_catalog.review", "raman_catalog.studio_server"])

    def test_module_that_does_not_exist_is_skipped(self):
        code, stdout, _stderr = self.main("hello", "--name", "studio",
                                          modules=("raman_catalog.commands_core", "raman_catalog.not_written_yet",
                                                   "demo_commands"))
        result = one_json_object(self, stdout)
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True, "greeting": "hello studio", "home": str(self.home), "root": "raman-portable"})

    def test_module_that_exists_but_cannot_be_imported_is_an_error(self):
        code, stdout, stderr = self.main("status", modules=("raman_catalog.commands_core", "broken_part"))
        result = one_json_object(self, stdout)
        self.assertEqual((code, result["ok"], result["kind"]), (1, False, "internal"))
        self.assertIn("a_package_nobody_installed", result["error"])
        self.assertIn("Traceback", stderr)

    def test_long_running_command_emits_its_object_once(self):
        code, stdout, stderr = self.main("serve")
        self.assertEqual(code, 0)
        self.assertEqual(one_json_object(self, stdout), {"ok": True, "url": "http://127.0.0.1:1/"})
        self.assertIn("a library printing on stdout", stderr)

    def test_exception_in_a_command_becomes_the_failure_object(self):
        code, stdout, stderr = self.main("broken")
        result = one_json_object(self, stdout)
        self.assertEqual(code, 1)
        self.assertEqual(result, {"ok": False, "error": "RuntimeError: internal fault",
                                  "detail": "The traceback is in the log on stderr.", "kind": "internal"})
        self.assertIn("Traceback", stderr)

    def test_core_commands_are_registered(self):
        with mock.patch.object(cli, "COMMAND_MODULES", ["raman_catalog.commands_core"]):
            parser = cli.build_parser()
        for arguments in (["status"], ["sync"], ["list"], ["provenance", "--record", "d/1"],
                          ["import-session", "--folder", "x"], ["config"], ["doctor"], ["offline"]):
            self.assertTrue(callable(parser.parse_args(arguments).func), arguments)


if __name__ == "__main__":
    unittest.main()
