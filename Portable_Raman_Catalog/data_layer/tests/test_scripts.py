"""The build files of the edition: the package lists, setup_env.sh and build_macos.sh.

Nothing here connects anywhere: the shell scripts run with stand-in programs only.
"""

import os
import shutil
import socket
import subprocess
import sys
import unittest
from unittest import mock

import support

DATA_LAYER = support.DATA_LAYER
EDITION = DATA_LAYER.parent
SETUP_ENV = DATA_LAYER / "setup_env.sh"
BUILD = EDITION / "macos-native" / "build_macos.sh"


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


class Scripts(ArcCase):
    def test_the_shell_scripts_parse(self):
        for script in (SETUP_ENV, BUILD):
            with self.subTest(script=script.name):
                done = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_mac_package_list(self):
        mac = (DATA_LAYER / "requirements-mac.txt").read_text(encoding="utf-8")
        lines = [line for line in mac.splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines, ["-r requirements.txt", "globus-sdk==4.10.0"])
        # The data layer's own list stays without the Globus SDK, which only the uploads to ARC need.
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
                        self.text.index('rsync -a --delete --exclude "__pycache__/" "$ROOT/data_layer/raman_catalog/"'))

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
