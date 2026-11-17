"""`doctor`: checks what the data layer needs on this Mac and says what the user has to do themselves.

The helpers for Globus Connect Personal are here as well; the uploads to ARC use them (arc-doctor).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GLOBUS_APP_NAME = "Globus Connect Personal.app"
GLOBUS_APP_FOLDERS = ("/Applications", "~/Applications")


def _check(name, ok, detail, fix=""):
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix}


def _shown(path):
    """A path as the user knows it: with ~ for their home folder."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home) else text


def _running_commands():
    listing = subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True, timeout=10, check=True)
    return listing.stdout.splitlines()


def _check_python():
    # The pinned numpy 2.3.4 and scikit-learn 1.8.0 (requirements.txt) exist for Python 3.11 and newer only.
    version = "%d.%d.%d" % sys.version_info[:3]
    return _check(
        "python", sys.version_info >= (3, 11), "Python %s" % version,
        "Use Python 3.11 or newer for the data layer: put its path on the first line of catalog_python.txt.",
    )


def _check_globus(app_folders, running_commands):
    apps = [Path(folder).expanduser() / GLOBUS_APP_NAME for folder in app_folders]
    installed = [app for app in apps if app.is_dir()]
    checks = [
        _check(
            "globus_connect_personal_installed", installed,
            "Found %s." % _shown(installed[0]) if installed else "%s is not in %s." % (
                GLOBUS_APP_NAME, " or ".join(str(folder) for folder in app_folders)),
            "Install Globus Connect Personal from https://www.globus.org/globus-connect-personal and sign in "
            "to it with your Globus account.",
        )
    ]
    try:
        commands = running_commands()
    except (OSError, subprocess.SubprocessError) as error:
        checks.append(_check(
            "globus_connect_personal_running", False,
            "The running programs could not be listed (%s), so this was not verified." % error,
            "Make sure Globus Connect Personal is running; uploads and downloads go through it.",
        ))
        return checks
    running = any(GLOBUS_APP_NAME + "/Contents/MacOS" in command for command in commands)
    checks.append(_check(
        "globus_connect_personal_running", running,
        "Globus Connect Personal is running." if running else "Globus Connect Personal is not running.",
        "Start Globus Connect Personal and leave it running; uploads and downloads go through it.",
    ))
    return checks


def _check_settings(settings):
    problems = settings.problems()
    if problems:
        return _check("settings", False, "; ".join(problems) + ".", "Correct them with: config --set key=value")
    return _check("settings", True, "root alias %s; captures go to the catalog on this Mac (%s)." % (
        settings.root_alias, _shown(settings.home / "data_catalog")))


def run_checks(settings):
    """Run the checks. Returns {"checks": [{name, ok, detail, fix}]}."""
    return {"checks": [_check_python(), _check_settings(settings)]}
