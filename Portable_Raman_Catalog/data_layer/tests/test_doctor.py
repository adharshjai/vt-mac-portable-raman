"""`doctor`: the checks of the data layer, and the Globus Connect Personal checks arc-doctor uses."""

import subprocess
import unittest
from pathlib import Path
from unittest import mock

import support
from raman_catalog import doctor
from raman_catalog.config import load_settings

GLOBUS_COMMAND = "/Applications/Globus Connect Personal.app/Contents/MacOS/Globus Connect Personal"


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.apps = self.home / "Applications"
        self.settings = load_settings(self.home)
        self.commands = ["/sbin/launchd", "/usr/sbin/cfprefsd agent"]

    def run_checks(self):
        report = doctor.run_checks(self.settings)
        return report, {check["name"]: check for check in report["checks"]}

    def globus(self, running_commands=None):
        checks = doctor._check_globus([self.apps], running_commands or (lambda: self.commands))
        return {check["name"]: check for check in checks}


class Checks(DoctorCase):
    def test_the_checks_of_a_new_home(self):
        report, checks = self.run_checks()
        self.assertEqual(list(report), ["checks"])
        self.assertEqual(list(checks), ["python", "settings"])
        self.assertTrue(checks["python"]["ok"])
        self.assertTrue(checks["settings"]["ok"])
        self.assertIn("root alias raman-portable", checks["settings"]["detail"])
        self.assertIn("the catalog on this Mac", checks["settings"]["detail"])
        for check in report["checks"]:
            self.assertEqual(check["fix"] == "", check["ok"], check)

    def test_python_floor_is_the_one_of_the_pinned_packages(self):
        for version, ok in (((3, 10, 14, "final", 0), False), ((3, 11, 0, "final", 0), True)):
            with mock.patch.object(doctor.sys, "version_info", version):
                _report, checks = self.run_checks()
            self.assertIs(checks["python"]["ok"], ok, version)
            self.assertIn("Python %d.%d.%d" % version[:3], checks["python"]["detail"])
            self.assertEqual(checks["python"]["fix"], "" if ok else
                             "Use Python 3.11 or newer for the data layer: put its path on the first line of catalog_python.txt.")

    def test_invalid_settings_fail_the_settings_check(self):
        self.settings.set("catalog.root_alias", "Not Valid")
        _report, checks = self.run_checks()
        self.assertFalse(checks["settings"]["ok"])
        self.assertIn("catalog.root_alias", checks["settings"]["detail"])
        self.assertIn("config --set", checks["settings"]["fix"])

    def test_a_path_is_shown_with_the_users_home_folder_shortened(self):
        self.assertEqual(doctor._shown(Path.home() / "somewhere" / "file"), "~/somewhere/file")
        self.assertEqual(doctor._shown("/elsewhere/file"), "/elsewhere/file")


class GlobusConnectPersonal(DoctorCase):
    def test_not_installed_and_not_running(self):
        checks = self.globus()
        self.assertEqual(list(checks), ["globus_connect_personal_installed", "globus_connect_personal_running"])
        self.assertFalse(checks["globus_connect_personal_installed"]["ok"])
        self.assertFalse(checks["globus_connect_personal_running"]["ok"])
        self.assertIn("Install Globus Connect Personal", checks["globus_connect_personal_installed"]["fix"])
        self.assertIn("Start Globus Connect Personal", checks["globus_connect_personal_running"]["fix"])

    def test_installed_and_running(self):
        (self.apps / "Globus Connect Personal.app").mkdir(parents=True)
        self.commands.append(GLOBUS_COMMAND)
        checks = self.globus()
        self.assertEqual([name for name, check in checks.items() if not check["ok"]], [])
        self.assertEqual([check["fix"] for check in checks.values()], ["", ""])

    def test_installed_but_not_running(self):
        (self.apps / "Globus Connect Personal.app").mkdir(parents=True)
        checks = self.globus()
        self.assertTrue(checks["globus_connect_personal_installed"]["ok"])
        self.assertFalse(checks["globus_connect_personal_running"]["ok"])

    def test_process_list_that_cannot_be_read_is_said_so(self):
        def failing():
            raise subprocess.CalledProcessError(1, "ps")

        checks = self.globus(running_commands=failing)
        self.assertFalse(checks["globus_connect_personal_running"]["ok"])
        self.assertIn("was not verified", checks["globus_connect_personal_running"]["detail"])


if __name__ == "__main__":
    unittest.main()
