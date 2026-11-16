"""The ARC classifier job's downloads through ARC's Globus collection, and the build files of uploads to ARC.

ARC's Projects collection shows the cluster folder /projects/<dir> as /<dir>. The datafed client
puts its endpoint in front of a local path, so a job that names its work folder by the cluster path
asks Globus for /projects/<dir>/... inside that collection, a folder that is not there (review F-01).
With a collection root, DataFedCatalog.get_data names the folder by its path inside the collection
(PROTOCOL.md 5.5); arc_job takes --collection-root and classify_job.slurm passes /projects.

Nothing here connects anywhere: DataFed is an autospec of the client's API class or the in-memory
stand-in of test_datafed_flow, the cluster is a temporary folder, and the shell scripts run with
stand-in programs only.
"""

import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import fixtures
import support
from raman_datafed import arc_job, commands_core
from raman_datafed.catalog import Catalog, CatalogAuthRequired, CatalogError
from raman_datafed.config import CommandError

try:
    from datafed import envelope_pb2 as sdms
    from datafed.CommandLib import API
    from raman_datafed.datafed_catalog import ARC_ENDPOINT_HELP, DataFedCatalog
    from test_datafed_flow import PROJECT, FakeDataFed
except ImportError:
    # The data layer's tests also run in an interpreter without the datafed package.
    API = FakeDataFed = None

needs_datafed = unittest.skipIf(API is None, "the datafed package is not installed here")

DATA_LAYER = support.DATA_LAYER
EDITION = DATA_LAYER.parent
SLURM = DATA_LAYER / "arc" / "classify_job.slurm"
SETUP_ENV = DATA_LAYER / "setup_env.sh"
BUILD = EDITION / "macos-native" / "build_macos.sh"
# ARC's Projects collection (public) and the instrument computer's own collection in these tests.
ARC_COLLECTION = "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"
MAC_COLLECTION = "5a1b0e6e-1111-2222-3333-444455556666"
# The DataFed project of these tests (test_datafed_flow's PROJECT, also when datafed is not installed).
PROJECT_ID = "p/raman.test"
TIMEOUT_DETAIL = ("Transfer task/77 into %s is still running. Look at its error at app.globus.org > Activity, "
                  "and cancel it there if it is stuck.")


def _no_network(*_arguments, **_options):
    raise AssertionError("an ARC test tried to open a network connection")


class ArcCase(unittest.TestCase):
    """Every ARC test runs with RAMAN_ARC_TEST_GUARD=1 (PROTOCOL.md 5.6) and without network."""

    def setUp(self):
        for patcher in (mock.patch.dict(os.environ, {"RAMAN_ARC_TEST_GUARD": "1"}),
                        mock.patch.object(socket.socket, "connect", _no_network),
                        mock.patch.object(socket, "create_connection", _no_network)):
            patcher.start()
            self.addCleanup(patcher.stop)


def write_program(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def run_arc_job(test, *arguments, catalog=None):
    """The cluster entry point in this process, as test_job runs it. Returns (exit code, summary, log)."""
    out, err = io.StringIO(), io.StringIO()
    environment = dict(os.environ)
    environment.pop("SLURM_JOB_ID", None)
    handlers, level = logging.root.handlers[:], logging.root.level
    try:
        with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = arc_job.main([str(argument) for argument in arguments], catalog=catalog)
    finally:
        logging.root.handlers[:] = handlers
        logging.root.setLevel(level)
    lines = out.getvalue().splitlines()
    test.assertEqual(len(lines), 1, out.getvalue())
    return code, json.loads(lines[0]), err.getvalue()


if FakeDataFed is not None:
    class CollectionDataFed(FakeDataFed):
        """The stand-in of test_datafed_flow with a Globus collection in front of its downloads, the way
        ARC's Projects collection works: it shows the folder `top` as its "/".

        dataGet takes its path as the datafed client sends it on (CommandLib.API._resolvePathForGlobus):
        a path that starts with a collection ID goes as it is, any other gets the client's endpoint in
        front. A folder the collection does not have is what Globus pauses and retries, so the task stays
        running until the client stops waiting (review F-V01).
        """

        def __init__(self, staging, top, endpoint):
            super().__init__(staging)
            self.top = Path(top)
            self.endpoint = endpoint
            self.requested = []
            self.delivered = []

        def dataGet(self, item_id, path, encrypt=sdms.ENCRYPT_AVAIL, orig_fname=False, wait=False, timeout_sec=0,
                    context=None):
            record = self.records[self._resolve(item_id, "d")]
            if not record["size"]:
                raise Exception("Specified record(s) contain no raw data.")
            full = path if API._endpoint_uuid.match(path) else self.endpoint + os.path.abspath(path)
            self.requested.append(full)
            endpoint, _slash, inside = full.partition("/")
            folder = self.top / inside
            reply = sdms.TaskDataReply()
            task = reply.task.add()
            task.id = self._new_id("task")
            if endpoint != self.endpoint or not inside or not folder.is_dir():
                task.status = sdms.TS_RUNNING
                return reply, "TaskDataReply"
            name = os.path.basename(record["source"]) if orig_fname else item_id.replace("/", "_")
            shutil.copyfile(record["stored"], folder / name)
            self.delivered.append(folder)
            task.status = sdms.TS_SUCCEEDED
            return reply, "TaskDataReply"


@needs_datafed
class GetDataThroughTheCollection(ArcCase):
    def setUp(self):
        super().setUp()
        self.cluster = support.make_home(self)
        self.top = self.cluster / "projects"
        self.top.mkdir()
        self.dest = self.top / "pcl_photonics" / "raman" / "work" / "download-x"
        self.api = mock.create_autospec(API, instance=True)
        self.api.getAuthUser.return_value = "u/tester"
        self.api.endpointGet.return_value = ARC_COLLECTION
        self.api.endpointDefaultGet.return_value = ARC_COLLECTION
        self.api.dataView.return_value = self.record_reply()

    @staticmethod
    def record_reply():
        reply = sdms.RecordDataReply()
        data = reply.data.add()
        data.id, data.alias, data.title = "d/101", "rs-abc", "Raw"
        data.metadata = json.dumps({"capture_id": "c1"})
        data.size = 20
        data.source = MAC_COLLECTION + "/deploy/scan_sessions/s1/point_0001.json"
        return reply, "RecordDataReply"

    @staticmethod
    def task_reply(status):
        reply = sdms.TaskDataReply()
        task = reply.task.add()
        task.id, task.type, task.status = "task/77", sdms.TT_DATA_GET, status
        return reply, "TaskDataReply"

    def catalog(self, root=None):
        return DataFedCatalog(context=PROJECT, api=self.api,
                              collection_root=str(self.top) if root is None else str(root))

    def deliver_into(self, folder):
        def download(_record_id, _path, **_options):
            (Path(folder) / "point_0001.json").write_text('{"intensity": []}', encoding="utf-8")
            return self.task_reply(sdms.TS_SUCCEEDED)

        self.api.dataGet.side_effect = download

    def test_the_folder_is_named_by_its_path_inside_the_collection(self):
        self.deliver_into(self.dest)
        path = self.catalog().get_data("rs-abc", self.dest)
        self.api.dataGet.assert_called_once_with(
            "d/101", ARC_COLLECTION + "/pcl_photonics/raman/work/download-x", orig_fname=True, wait=True,
            timeout_sec=600, context=PROJECT)
        # The file is still looked for in the local folder, where the collection put it.
        self.assertEqual(path, self.dest / "point_0001.json")
        self.assertTrue(path.is_file())

    def test_a_linked_root_or_work_folder_gives_the_same_path(self):
        real = self.cluster / "gpfs_projects"
        self.top.rmdir()
        real.mkdir()
        # /projects is a link to the real folder (arc_job resolves its work folder: review F-04).
        self.top.symlink_to(real, target_is_directory=True)
        expected = ARC_COLLECTION + "/pcl_photonics/raman/work/download-x"
        for root, dest in ((self.top, self.dest), (real, self.dest),
                           (self.top, real / "pcl_photonics" / "raman" / "work" / "download-x")):
            with self.subTest(root=root, dest=dest):
                self.api.dataGet.reset_mock()
                self.deliver_into(dest)
                self.catalog(root).get_data("rs-abc", dest)
                self.assertEqual(self.api.dataGet.call_args.args, ("d/101", expected))

    def test_a_folder_outside_the_root_is_refused_before_anything_is_asked(self):
        outside = self.cluster / "home" / "work" / "download-x"
        with self.assertRaises(CommandError) as raised:
            self.catalog().get_data("rs-abc", outside)
        self.assertEqual(str(raised.exception),
                         "The download folder %s is not inside %s, the part of the cluster that ARC's Globus "
                         "collection shows" % (outside, self.top))
        self.api.dataView.assert_not_called()
        self.api.dataGet.assert_not_called()
        self.assertFalse(outside.exists())

    def test_no_endpoint_or_one_that_is_not_a_collection_id(self):
        cases = ((None, None), ("", ""), (None, "someone#laptop"), ("not-a-collection-id", ARC_COLLECTION),
                 (None, ARC_COLLECTION + "\n"), (None, ARC_COLLECTION[:-1]), (None, ARC_COLLECTION + "/x"))
        for current, default in cases:
            with self.subTest(current=current, default=default):
                self.api.reset_mock()
                self.api.getAuthUser.return_value = "u/tester"
                self.api.endpointGet.return_value = current
                self.api.endpointDefaultGet.return_value = default
                with self.assertRaises(CatalogAuthRequired) as raised:
                    self.catalog().get_data("rs-abc", self.dest)
                self.assertEqual(str(raised.exception), ARC_ENDPOINT_HELP)
                self.api.dataGet.assert_not_called()
        self.assertIn("'datafed ep default set <ID of ARC's Projects collection>'", ARC_ENDPOINT_HELP)
        self.assertIn("data_layer/arc/README.md step 5", ARC_ENDPOINT_HELP)

    def test_the_current_endpoint_comes_before_the_default(self):
        self.api.endpointGet.return_value = ARC_COLLECTION
        self.api.endpointDefaultGet.return_value = MAC_COLLECTION
        self.deliver_into(self.dest)
        self.catalog().get_data("rs-abc", self.dest)
        self.assertEqual(self.api.dataGet.call_args.args[1], ARC_COLLECTION + "/pcl_photonics/raman/work/download-x")
        # No current endpoint: the default one, as the client itself would use it.
        self.api.endpointGet.return_value = None
        self.api.endpointDefaultGet.return_value = ARC_COLLECTION.upper()
        self.catalog().get_data("rs-abc", self.dest)
        self.assertEqual(self.api.dataGet.call_args.args[1],
                         ARC_COLLECTION.upper() + "/pcl_photonics/raman/work/download-x")

    def test_without_a_collection_root_the_local_path_goes_as_before(self):
        dest = self.cluster / "anywhere" / "download-x"
        self.deliver_into(dest)
        catalog = DataFedCatalog(context=PROJECT, api=self.api)
        self.assertEqual(catalog.get_data("rs-abc", dest), dest / "point_0001.json")
        self.api.dataGet.assert_called_once_with("d/101", str(dest), orig_fname=True, wait=True, timeout_sec=600,
                                                 context=PROJECT)
        self.api.endpointGet.assert_not_called()
        self.api.endpointDefaultGet.assert_not_called()

    def test_a_transfer_still_running_at_the_time_limit_names_its_target_and_globus_activity(self):
        self.api.dataGet.return_value = self.task_reply(sdms.TS_RUNNING)
        with self.assertRaises(CatalogError) as raised:
            self.catalog().get_data("rs-abc", self.dest)
        self.assertEqual(str(raised.exception), "The download of d/101 did not finish within 600 s")
        target = ARC_COLLECTION + "/pcl_photonics/raman/work/download-x"
        self.assertEqual(raised.exception.detail, TIMEOUT_DETAIL % target)
        self.assertNotIn("Globus Connect Personal", raised.exception.detail)
        # On the instrument computer the detail stays as it was.
        with self.assertRaises(CatalogError) as raised:
            DataFedCatalog(context=PROJECT, api=self.api).get_data("rs-abc", self.cluster / "staging")
        self.assertEqual(raised.exception.detail,
                         "Transfer task/77 is still running. Is Globus Connect Personal running?")


class JobArguments(ArcCase):
    def setUp(self):
        super().setUp()
        self.cluster = support.make_home(self)
        self.top = self.cluster / "projects"
        self.work = self.top / "pcl_photonics" / "raman" / "work"
        self.catalog = mock.create_autospec(Catalog, instance=True)

    def test_a_relative_or_empty_collection_root_is_refused(self):
        for root in ("projects", "./projects", ""):
            with self.subTest(root=root):
                code, summary, _log = run_arc_job(self, "--root-alias", "raman-portable", "--work-dir", self.work,
                                                  "--collection-root", root, catalog=self.catalog)
                self.assertEqual((code, summary["ok"], summary["error"], summary["kind"]),
                                 (1, False, "The arguments cannot be used", "command"))
                self.assertIn("--collection-root must be an absolute folder, such as /projects", summary["detail"])
        self.assertEqual(self.catalog.mock_calls, [])
        self.assertFalse(self.work.exists())

    def test_a_work_folder_outside_the_root_is_refused_before_anything_is_contacted(self):
        outside = self.cluster / "home" / "raman" / "work"
        with mock.patch("raman_datafed.datafed_catalog.DataFedCatalog") as built:
            code, summary, log = run_arc_job(self, "--root-alias", "raman-portable", "--work-dir", outside,
                                             "--collection-root", self.top)
        built.assert_not_called()
        self.assertEqual((code, summary["ok"], summary["kind"]), (1, False, "command"))
        self.assertEqual(summary["error"], "The work folder must be inside --collection-root (%s): ARC's Globus "
                                           "collection reaches only that folder" % self.top)
        self.assertIn(str(outside), summary["detail"])
        self.assertEqual(summary["collection_root"], str(self.top))
        self.assertIn("nothing was classified", log)
        self.assertFalse(outside.exists())

    def test_the_root_reaches_the_catalog_and_the_summary(self):
        real = self.cluster / "gpfs_projects"
        real.mkdir()
        self.top.symlink_to(real, target_is_directory=True)
        for root, work in ((self.top, self.work), (real, self.work), (self.top, real / "pcl_photonics" / "w")):
            with self.subTest(root=root, work=work):
                with mock.patch("raman_datafed.datafed_catalog.DataFedCatalog") as built:
                    built.return_value.list_collection.return_value = []
                    code, summary, log = run_arc_job(self, "--root-alias", "raman-portable", "--work-dir", work,
                                                     "--collection-root", root, "--context", PROJECT_ID)
                self.assertEqual(code, 0, log)
                built.assert_called_once_with(context=PROJECT_ID, repo_id="", collection_root=str(root))
                self.assertEqual((summary["ok"], summary["processed"], summary["collection_root"]),
                                 (True, 0, str(root)))

    def test_without_the_option_the_summary_says_none(self):
        code, summary, _log = run_arc_job(self, "--root-alias", "Not An Alias", "--work-dir", self.work,
                                          catalog=self.catalog)
        self.assertEqual((code, summary["collection_root"]), (1, None))
        keys = list(summary)
        self.assertEqual(keys[0], "ok")
        self.assertEqual(keys.index("collection_root"), keys.index("context") + 1)

    def test_help_names_the_option(self):
        helped = subprocess.run([sys.executable, "-m", "raman_datafed.arc_job", "-h"], capture_output=True, text=True,
                                timeout=60, cwd=str(self.cluster), env=support.cli_environment(self.cluster))
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("--collection-root", helped.stdout)


@fixtures.needs_model
@unittest.skipIf(FakeDataFed is None, "the datafed package, or the stand-in server of test_datafed_flow, is not there")
class WholeJobThroughTheCollection(ArcCase):
    """The test review F-01 found missing: a job whose downloads reach the work folder only when the
    work folder is named by its path inside ARC's collection."""

    def test_the_job_classifies_with_the_collection_root_and_waits_in_vain_without_it(self):
        home = support.make_home(self)
        settings = fixtures.use_shipped_model(home, 0.9)
        settings.set("mode", "datafed")
        settings.set("datafed.context", PROJECT)
        settings.save()
        cluster = support.make_home(self)
        top = cluster / "projects"
        work = top / "pcl_photonics" / "raman" / "work"
        work.mkdir(parents=True)
        server = CollectionDataFed(support.make_home(self), top, ARC_COLLECTION)

        # The instrument computer registers and uploads the spectra and the model.
        mac = DataFedCatalog(context=PROJECT, api=server.behind(mock.create_autospec(API, instance=True)))
        ctx = support.context(self, home, catalog=mac)
        session = fixtures.make_real_session(home)
        for _pass in range(4):
            self.assertEqual(commands_core.cmd_sync(Namespace(), ctx)["errors"], [])

        # On ARC the client's endpoint is ARC's collection.
        arc_api = server.behind(mock.create_autospec(API, instance=True))
        arc_api.endpointGet.return_value = ARC_COLLECTION
        arc_api.endpointDefaultGet.return_value = ARC_COLLECTION
        arguments = ("--root-alias", "raman-portable", "--context", PROJECT, "--work-dir", work,
                     "--confidence-threshold", "0.9")

        # As the job was before the fix: the cluster path goes to the collection, and nothing arrives.
        with mock.patch.object(DataFedCatalog, "_connect", return_value=arc_api):
            code, summary, log = run_arc_job(self, *arguments)
        self.assertEqual((code, summary["ok"], summary["kind"], summary["collection_root"]),
                         (1, False, "catalog", None), log)
        self.assertRegex(summary["error"], r"^The download of d/\d+ did not finish within 600 s$")
        self.assertEqual(server.delivered, [])
        self.assertTrue(server.requested)
        for target in server.requested:
            self.assertTrue(target.startswith(ARC_COLLECTION + str(work) + "/"), target)

        # With the collection root of the batch script: every download reaches the work folder.
        server.requested.clear()
        with mock.patch.object(DataFedCatalog, "_connect", return_value=arc_api):
            code, summary, log = run_arc_job(self, *arguments, "--collection-root", top)
        self.assertEqual(code, 0, log)
        self.assertEqual((summary["ok"], summary["processed"], summary["predictions"], summary["errors"],
                          summary["collection_root"]), (True, 3, 3, [], str(top)))
        self.assertEqual([item["outcome"] for item in summary["items"]], ["low_confidence", "confident", "background"])
        # The model archive, the dark spectrum (once) and the three spectra, each in a download folder of
        # its own inside the work folder.
        self.assertEqual(len(server.requested), 5)
        for target in server.requested:
            self.assertTrue(target.startswith(ARC_COLLECTION + "/pcl_photonics/raman/work/"), target)
            self.assertNotIn(str(cluster), target)
        self.assertEqual(len(server.delivered), 5)
        for folder in server.delivered:
            self.assertEqual(folder.parent, work)

        # The instrument computer hears of the predictions with its next sync.
        result = commands_core.cmd_sync(Namespace(), ctx)
        self.assertEqual((result["predictions_found"], result["errors"]), (3, []))
        self.assertEqual(len(session["capture_ids"]), 3)


class BatchScript(ArcCase):
    def test_the_settings_block_sets_the_collection_root(self):
        text = SLURM.read_text(encoding="utf-8")
        self.assertIn('\nCOLLECTION_ROOT="/projects"\n', text)
        self.assertIn('if [ -n "$COLLECTION_ROOT" ]; then arguments+=(--collection-root "$COLLECTION_ROOT"); fi\n',
                      text)
        # Before the line that names nothing below needs editing, so a person sees and can change it.
        self.assertLess(text.index('COLLECTION_ROOT="/projects"'), text.index("# ---- nothing below needs editing"))
        # The account, the project folder and the DataFed project stay placeholders.
        for placeholder in ("<your-slurm-account>", "<your-project-directory>", "<p/your-datafed-project>"):
            self.assertIn(placeholder, text)

    def run_script(self, collection_root=None):
        """classify_job.slurm with stand-ins for module, the environment, hostname and python. Returns the
        arguments python was started with and the RAMAN_DIR used."""
        tmp = support.make_home(self)
        stubs = tmp / "bin"
        stubs.mkdir()
        venv = tmp / "venv"
        (venv / "bin").mkdir(parents=True)
        raman = tmp / "projects" / "team" / "raman"
        record = tmp / "python_arguments.txt"
        write_program(stubs / "module", "#!/bin/bash\nexit 0\n")
        write_program(stubs / "hostname", "#!/bin/bash\necho compute-node\n")
        write_program(stubs / "python", '#!/bin/bash\nprintf "%s\\n" "$@" > "$RAMAN_TEST_RECORD"\n'
                                        "echo '{\"ok\": true}'\n")
        (venv / "bin" / "activate").write_text('PATH="%s:$PATH"\n' % stubs, encoding="utf-8")
        script = SLURM.read_text(encoding="utf-8")
        replacements = {'RAMAN_DIR="/projects/<your-project-directory>/raman"': 'RAMAN_DIR="%s"' % raman,
                        'VENV_DIR="$HOME/venvs/raman-datafed"': 'VENV_DIR="%s"' % venv,
                        'CONTEXT="<p/your-datafed-project>"': 'CONTEXT=""'}
        if collection_root is not None:
            replacements['COLLECTION_ROOT="/projects"'] = 'COLLECTION_ROOT="%s"' % collection_root
        for old, new in replacements.items():
            self.assertIn(old, script)
            script = script.replace(old, new)
        copy = tmp / "classify_job.slurm"
        copy.write_text(script, encoding="utf-8")
        environment = {"PATH": "%s:/usr/bin:/bin" % stubs, "HOME": str(tmp), "SLURM_JOB_ID": "4242",
                       "RAMAN_TEST_RECORD": str(record), "LC_ALL": "C"}
        done = subprocess.run(["/bin/bash", str(copy)], cwd=str(tmp), env=environment, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Job 4242 on compute-node: python -m raman_datafed.arc_job", done.stdout)
        return record.read_text(encoding="utf-8").splitlines(), raman

    def test_the_script_passes_the_root_to_the_job(self):
        arguments, raman = self.run_script()
        self.assertEqual(arguments[:2], ["-m", "raman_datafed.arc_job"])
        self.assertEqual(arguments[arguments.index("--collection-root") + 1], "/projects")
        self.assertEqual(arguments[arguments.index("--work-dir") + 1], str(raman / "work"))
        self.assertEqual(arguments[arguments.index("--root-alias") + 1], "raman-portable")
        self.assertNotIn("--context", arguments)
        self.assertTrue((raman / "work").is_dir())

    def test_an_empty_root_leaves_the_option_out(self):
        arguments, _raman = self.run_script(collection_root="")
        self.assertNotIn("--collection-root", arguments)


class Scripts(ArcCase):
    def test_the_shell_scripts_parse(self):
        for script in (SLURM, SETUP_ENV, BUILD):
            with self.subTest(script=script.name):
                done = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_mac_package_list(self):
        mac = (DATA_LAYER / "requirements-mac.txt").read_text(encoding="utf-8")
        lines = [line for line in mac.splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines, ["-r requirements.txt", "globus-sdk==4.10.0"])
        # The ARC job's list stays without the Globus SDK.
        self.assertNotIn("globus", (DATA_LAYER / "requirements.txt").read_text(encoding="utf-8").lower())

    def setup_env(self, with_venv=True, with_mac_list=True):
        """setup_env.sh in a folder of its own, with stand-ins for the interpreters. Returns (the
        commands they were given, the script's output)."""
        tmp = support.make_home(self)
        (tmp / "data_layer").mkdir()
        shutil.copy2(SETUP_ENV, tmp / "data_layer" / "setup_env.sh")
        shutil.copyfile(DATA_LAYER / "requirements.txt", tmp / "data_layer" / "requirements.txt")
        if with_mac_list:
            shutil.copyfile(DATA_LAYER / "requirements-mac.txt", tmp / "data_layer" / "requirements-mac.txt")
        record = tmp / "commands.txt"
        stand_in = '#!/bin/bash\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$RAMAN_TEST_RECORD"\n'
        if with_venv:
            (tmp / ".venv" / "bin").mkdir(parents=True)
            write_program(tmp / ".venv" / "bin" / "python", stand_in)
        base = write_program(tmp / "base_python", stand_in + (
            'if [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; fi\n'))
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "RAMAN_TEST_RECORD": str(record),
                       "RAMAN_BASE_PYTHON": str(base)}
        done = subprocess.run(["/bin/bash", str(tmp / "data_layer" / "setup_env.sh")], cwd=str(tmp), env=environment,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return record.read_text(encoding="utf-8").splitlines(), done.stdout, tmp

    def test_setup_env_adds_the_globus_sdk_to_an_existing_environment(self):
        commands, output, tmp = self.setup_env()
        self.assertIn("adding what is missing to %s/.venv" % tmp, output)
        self.assertEqual(commands, [
            "python -m pip install --quiet --upgrade pip",
            "python -m pip install --prefer-binary -r %s/data_layer/requirements-mac.txt" % tmp])

    def test_setup_env_makes_the_environment_when_there_is_none(self):
        commands, output, tmp = self.setup_env(with_venv=False)
        self.assertIn("creating %s/.venv" % tmp, output)
        self.assertEqual(commands, [
            "base_python -m venv %s/.venv" % tmp,
            "python -m pip install --quiet --upgrade pip",
            "python -m pip install --prefer-binary -r %s/data_layer/requirements-mac.txt" % tmp])

    def test_setup_env_without_the_mac_list_installs_the_data_layers_list(self):
        commands, _output, tmp = self.setup_env(with_mac_list=False)
        self.assertEqual(commands[-1], "python -m pip install --prefer-binary -r %s/data_layer/requirements.txt" % tmp)


class BuildScript(ArcCase):
    def setUp(self):
        super().setUp()
        self.text = BUILD.read_text(encoding="utf-8")

    def test_the_tests_run_with_the_guard_and_the_new_suites(self):
        self.assertIn("export RAMAN_BACKEND_SIMULATE=1 RAMAN_MOTOR_SIMULATE=1 PYTHONDONTWRITEBYTECODE=1 "
                      "RAMAN_ARC_TEST_GUARD=1\n", self.text)
        studio = 'suite studio_arc      "$NODE" "$ROOT/studio/test_arc.mjs"\n'
        window = "suite arc_ui_tests    controller_test ui_home arc_ui_tests\n"
        self.assertIn(studio, self.text)
        self.assertIn(window, self.text)
        self.assertLess(self.text.index('suite studio_page'), self.text.index(studio))
        self.assertLess(self.text.index("suite ui_tests "), self.text.index(window))
        # The data layer's new tests run in its existing suite, which discovers every tests/test_*.py.
        self.assertIn("-m unittest discover -s tests -v", self.text)

    def test_the_deploy_gets_the_mac_package_list_and_a_note_without_the_sdk(self):
        self.assertIn('cp "$ROOT/data_layer/requirements-mac.txt" "$DEPLOY/data_layer/requirements-mac.txt"', self.text)
        self.assertIn("(note: the Globus SDK is not in .venv: live Globus uploads to ARC need it, demo and SSH uploads "
                      "work without it; run data_layer/setup_env.sh)", self.text)
        # It looks for the SDK without importing it, and only runs setup_env.sh when there is no interpreter.
        self.assertIn('find_spec("globus_sdk")', self.text)
        self.assertEqual(self.text.count('bash "$ROOT/data_layer/setup_env.sh"'), 1)
        self.assertIn('if [ ! -x "$DATAPY" ] && [ -f "$ROOT/data_layer/setup_env.sh" ]', self.text)

    def test_a_running_upload_is_noted_before_the_package_is_replaced(self):
        note = "(note: an ARC upload is running; its program keeps the code it started with until it ends)"
        self.assertIn(note, self.text)
        self.assertLess(self.text.index("if arc_upload_running; then"),
                        self.text.index('rsync -a --delete --exclude "__pycache__/" "$ROOT/data_layer/raman_datafed/"'))

    def test_the_lock_check_sees_a_held_worker_lock_only(self):
        start = self.text.index("arc_upload_running() {")
        function = self.text[start:self.text.index("\n}\n", start) + 3]
        tmp = support.make_home(self)
        uploads = tmp / "deploy" / "data_catalog" / "arc_uploads"
        script = tmp / "check.sh"
        script.write_text('PYTHON="$1"; DEPLOY="$2"\n' + function +
                          'if arc_upload_running; then echo running; else echo idle; fi\n', encoding="utf-8")

        def check():
            done = subprocess.run(["/bin/bash", str(script), sys.executable, str(tmp / "deploy")], capture_output=True,
                                  text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()

        self.assertEqual(check(), "idle")
        (uploads / "u0123456789ab").mkdir(parents=True)
        (uploads / ".creating-abc").mkdir()
        lock = uploads / "u0123456789ab" / "worker.lock"
        lock.touch()
        self.assertEqual(check(), "idle")
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c", "import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.read()\n", str(lock)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.assertEqual(check(), "running")
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        self.assertEqual(check(), "idle")


if __name__ == "__main__":
    unittest.main()
