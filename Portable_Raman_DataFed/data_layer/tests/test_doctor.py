"""`doctor`: prerequisite checks that never log in and never open a key file."""

import builtins
import io
import subprocess
import unittest
from unittest import mock

import support
from raman_datafed import doctor
from raman_datafed.config import load_settings

GLOBUS_COMMAND = "/Applications/Globus Connect Personal.app/Contents/MacOS/Globus Connect Personal"


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.client_dir = self.home / ".datafed"
        self.apps = self.home / "Applications"
        self.settings = load_settings(self.home)
        self.commands = ["/sbin/launchd", "/usr/sbin/cfprefsd agent"]

    def install_everything(self):
        self.client_dir.mkdir()
        (self.client_dir / "datafed-user-key.pub").write_text("not a real key", encoding="utf-8")
        (self.client_dir / "datafed-user-key.priv").write_text("not a real key", encoding="utf-8")
        (self.client_dir / "datafed-client.ini").write_text(
            "[client]\ntoken = 100%secret\n[general]\ndefault_endpoint = 5a1b0e6e-1111-2222-3333-444455556666\n",
            encoding="utf-8")
        (self.apps / "Globus Connect Personal.app").mkdir(parents=True)
        self.commands.append(GLOBUS_COMMAND)
        self.settings.set("mode", "datafed")

    def run_checks(self, **options):
        options.setdefault("client_dir", self.client_dir)
        options.setdefault("app_folders", [self.apps])
        options.setdefault("running_commands", lambda: self.commands)
        options.setdefault("environ", {})
        options.setdefault("platform", "darwin")
        report = doctor.run_checks(self.settings, **options)
        return report, {check["name"]: check for check in report["checks"]}


class Checks(DoctorCase):
    def test_nothing_installed(self):
        report, checks = self.run_checks()
        self.assertEqual(list(checks), ["python", "datafed_package", "datafed_credentials", "globus_default_endpoint",
                                        "globus_connect_personal_installed", "globus_connect_personal_running",
                                        "settings"])
        self.assertIs(report["online_checked"], False)
        self.assertTrue(checks["python"]["ok"])
        self.assertTrue(checks["datafed_package"]["ok"])
        self.assertIn("datafed 5.", checks["datafed_package"]["detail"])
        self.assertTrue(checks["settings"]["ok"])
        self.assertIn("nothing is sent to DataFed", checks["settings"]["detail"])
        failed = {name: check for name, check in checks.items() if not check["ok"]}
        self.assertEqual(set(failed), {"datafed_credentials", "globus_default_endpoint",
                                       "globus_connect_personal_installed", "globus_connect_personal_running"})
        self.assertIn("datafed-user-key.pub, datafed-user-key.priv", failed["datafed_credentials"]["detail"])
        self.assertIn("run 'datafed setup' yourself", failed["datafed_credentials"]["fix"])
        self.assertIn("'datafed ep default set <endpoint>'", failed["globus_default_endpoint"]["fix"])
        self.assertIn("Install Globus Connect Personal", failed["globus_connect_personal_installed"]["fix"])
        self.assertIn("Start Globus Connect Personal", failed["globus_connect_personal_running"]["fix"])
        for check in report["checks"]:
            self.assertEqual(check["fix"] == "", check["ok"], check)

    def test_python_floor_is_the_one_of_the_pinned_packages(self):
        for version, ok in (((3, 10, 14, "final", 0), False), ((3, 11, 0, "final", 0), True)):
            with mock.patch.object(doctor.sys, "version_info", version):
                report, checks = self.run_checks()
            self.assertIs(checks["python"]["ok"], ok, version)
            self.assertIn("Python %d.%d.%d" % version[:3], checks["python"]["detail"])
            self.assertEqual(checks["python"]["fix"], "" if ok else
                             "Use Python 3.11 or newer for the data layer: put its path on the first line of datafed_python.txt.")

    def test_everything_in_place(self):
        self.install_everything()
        report, checks = self.run_checks()
        self.assertEqual([name for name, check in checks.items() if not check["ok"]], [])
        self.assertIn("5a1b0e6e-1111-2222-3333-444455556666", checks["globus_default_endpoint"]["detail"])
        self.assertIn("mode datafed", checks["settings"]["detail"])
        # Only the endpoint is taken from the settings file; nothing else in it is shown.
        self.assertNotIn("secret", str(report))

    def test_key_files_are_tested_for_existence_only(self):
        self.install_everything()
        opened = []
        real_open = builtins.open

        def watching_open(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        # pathlib opens files through io.open, plain code through the builtin; both are watched.
        with mock.patch.object(builtins, "open", watching_open), mock.patch.object(io, "open", watching_open):
            report, _checks = self.run_checks()
        self.assertTrue(any(name.endswith("datafed-client.ini") for name in opened))
        self.assertEqual([name for name in opened if "datafed-user-key" in name], [])
        self.assertNotIn("not a real key", str(report))

    def test_one_missing_key_file_is_named(self):
        self.install_everything()
        (self.client_dir / "datafed-user-key.priv").unlink()
        _report, checks = self.run_checks()
        self.assertFalse(checks["datafed_credentials"]["ok"])
        self.assertIn("datafed-user-key.priv", checks["datafed_credentials"]["detail"])
        self.assertNotIn("datafed-user-key.pub", checks["datafed_credentials"]["detail"])

    def test_endpoint_from_the_environment(self):
        _report, checks = self.run_checks(environ={"DATAFED_DEFAULT_ENDPOINT": "from-environment"})
        self.assertTrue(checks["globus_default_endpoint"]["ok"])
        self.assertIn("from-environment", checks["globus_default_endpoint"]["detail"])

    def test_client_folder_follows_the_packages_environment_variable(self):
        self.assertEqual(doctor.default_client_dir({"DATAFED_CLIENT_CFG_DIR": "/somewhere/keys"}).as_posix(),
                         "/somewhere/keys")
        self.assertEqual(doctor.default_client_dir({}).name, ".datafed")

    def test_globus_installed_but_not_running(self):
        self.install_everything()
        self.commands.remove(GLOBUS_COMMAND)
        _report, checks = self.run_checks()
        self.assertTrue(checks["globus_connect_personal_installed"]["ok"])
        self.assertFalse(checks["globus_connect_personal_running"]["ok"])

    def test_process_list_that_cannot_be_read_is_said_so(self):
        def failing():
            raise subprocess.CalledProcessError(1, "ps")

        _report, checks = self.run_checks(running_commands=failing)
        self.assertFalse(checks["globus_connect_personal_running"]["ok"])
        self.assertIn("was not verified", checks["globus_connect_personal_running"]["detail"])

    def test_globus_app_checks_are_for_macos(self):
        _report, checks = self.run_checks(platform="linux")
        self.assertNotIn("globus_connect_personal_installed", checks)
        self.assertNotIn("globus_connect_personal_running", checks)

    def test_invalid_settings_fail_the_settings_check(self):
        self.settings.set("datafed.root_alias", "Not Valid")
        _report, checks = self.run_checks()
        self.assertFalse(checks["settings"]["ok"])
        self.assertIn("datafed.root_alias", checks["settings"]["detail"])
        self.assertIn("config --set", checks["settings"]["fix"])

    def test_missing_package_is_a_failed_check_with_the_way_to_install_it(self):
        with mock.patch.dict("sys.modules", {"datafed.CommandLib": None}):
            _report, checks = self.run_checks()
        self.assertFalse(checks["datafed_package"]["ok"])
        self.assertIn("setup_env.sh", checks["datafed_package"]["fix"])


class Online(DoctorCase):
    def catalog(self, **who):
        answer = {"online": True, "user": "u/tester", "context": "u/tester", "endpoint": "5a1b0e6e", "demo": False}
        answer.update(who)
        catalog = mock.Mock()
        catalog.whoami.return_value = answer
        return catalog

    def test_catalog_is_not_asked_without_online_or_outside_datafed_mode(self):
        catalog = self.catalog()
        report, _checks = self.run_checks(catalog=catalog)
        self.assertIs(report["online_checked"], False)
        report, _checks = self.run_checks(catalog=catalog, online=True)
        self.assertIs(report["online_checked"], False)
        self.settings.set("mode", "datafed")
        report, _checks = self.run_checks(catalog=catalog)
        self.assertIs(report["online_checked"], False)
        catalog.whoami.assert_not_called()

    def test_signed_in(self):
        self.install_everything()
        report, checks = self.run_checks(catalog=self.catalog(), online=True)
        self.assertIs(report["online_checked"], True)
        self.assertTrue(checks["datafed_connection"]["ok"])
        self.assertIn("user u/tester", checks["datafed_connection"]["detail"])

    def test_each_failure_names_what_the_user_has_to_do(self):
        self.install_everything()
        cases = [
            ({"online": False, "user": None, "problem": "DataFed cannot be reached"}, "network connection"),
            ({"user": None, "problem": "DataFed credentials are missing"}, "'datafed setup'"),
            ({"endpoint": None, "problem": "No default Globus endpoint is set"}, "'datafed ep default set <endpoint>'"),
        ]
        for who, fix in cases:
            _report, checks = self.run_checks(catalog=self.catalog(**who), online=True)
            check = checks["datafed_connection"]
            self.assertFalse(check["ok"])
            self.assertEqual(check["detail"], who["problem"])
            self.assertIn(fix, check["fix"])


if __name__ == "__main__":
    unittest.main()
