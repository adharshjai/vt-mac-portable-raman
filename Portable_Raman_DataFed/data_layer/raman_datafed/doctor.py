"""`doctor`: checks what DataFed needs on this Mac and says what the user has to do themselves.

Nothing here logs in. Key files are only tested for existence, never opened. The DataFed
server is contacted only when asked to (`--online`) and only in datafed mode.
"""

from __future__ import annotations

import configparser
import contextlib
import importlib
import os
import subprocess
import sys
from pathlib import Path

from .config import MODE_DATAFED

USER_KEY_FILES = ("datafed-user-key.pub", "datafed-user-key.priv")
CLIENT_CONFIG_FILE = "datafed-client.ini"
GLOBUS_APP_NAME = "Globus Connect Personal.app"
GLOBUS_APP_FOLDERS = ("/Applications", "~/Applications")

_SETUP_FIX = (
    "In a terminal, run 'datafed setup' yourself and enter your DataFed user ID and password when it asks. "
    "The data layer never logs in for you."
)
_ENDPOINT_FIX = (
    "In a terminal, run 'datafed ep default set <endpoint>' with the UUID of this Mac's Globus Connect "
    "Personal collection (shown in its Preferences and on app.globus.org)."
)


def _check(name, ok, detail, fix=""):
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix}


def _shown(path):
    """A path as the user knows it: with ~ for their home folder."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home) else text


def default_client_dir(environ=None):
    """Where the datafed package keeps the user's key files and settings."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("DATAFED_CLIENT_CFG_DIR") or "~/.datafed").expanduser()


def _running_commands():
    listing = subprocess.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True, timeout=10, check=True)
    return listing.stdout.splitlines()


def _check_python():
    # The pinned numpy 2.3.4 and scikit-learn 1.8.0 (requirements.txt) exist for Python 3.11 and newer only.
    version = "%d.%d.%d" % sys.version_info[:3]
    return _check(
        "python", sys.version_info >= (3, 11), "Python %s" % version,
        "Use Python 3.11 or newer for the data layer: put its path on the first line of datafed_python.txt.",
    )


def _check_package():
    try:
        # Importing connects to nothing; the package prints on stdout in places, hence the redirect.
        with contextlib.redirect_stdout(sys.stderr):
            importlib.import_module("datafed.CommandLib")
            version = importlib.import_module("datafed").version
    except Exception as error:
        # A missing package raises ImportError; a broken binary dependency can raise anything.
        return _check(
            "datafed_package", False, "The datafed Python package cannot be imported (%s)." % error,
            "Create the data layer's Python environment with data_layer/setup_env.sh and put its interpreter "
            "on the first line of datafed_python.txt.",
        )
    return _check("datafed_package", True, "datafed %s can be imported." % version)


def _check_credentials(client_dir):
    missing = [name for name in USER_KEY_FILES if not (client_dir / name).is_file()]
    if missing:
        return _check("datafed_credentials", False,
                      "Missing in %s: %s." % (_shown(client_dir), ", ".join(missing)), _SETUP_FIX)
    return _check("datafed_credentials", True,
                  "Both user key files exist in %s (existence only; they were not opened)." % _shown(client_dir))


def _default_endpoint(client_dir, environ):
    # Only the endpoint is looked up. No interpolation: a "%" elsewhere in the file must not matter.
    settings = configparser.ConfigParser(interpolation=None)
    with contextlib.suppress(configparser.Error, OSError):
        settings.read(client_dir / CLIENT_CONFIG_FILE, encoding="utf-8")
    # The package gives its settings file priority over the environment variable.
    return settings.get("general", "default_endpoint", fallback="") or environ.get("DATAFED_DEFAULT_ENDPOINT", "")


def _check_endpoint(client_dir, environ):
    endpoint = _default_endpoint(client_dir, environ)
    if not endpoint:
        return _check("globus_default_endpoint", False, "No default Globus endpoint is set for DataFed.", _ENDPOINT_FIX)
    return _check("globus_default_endpoint", True, "Default Globus endpoint: %s." % endpoint)


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
    detail = "mode %s, context %s, root alias %s, datafed.include_simulated %s." % (
        settings.mode, settings.context or "(your own space)", settings.root_alias,
        "true" if settings.include_simulated else "false")
    if settings.include_simulated:
        detail += " In datafed mode, simulated captures are sent to DataFed as well."
    else:
        detail += (" In datafed mode, simulated captures stay on this Mac (to send them as well:"
                   " config --set datafed.include_simulated=true).")
    if settings.mode != MODE_DATAFED:
        detail += (" In this mode captures go to the demo catalog on this Mac and nothing is sent to DataFed;"
                   " switch with: config --set mode=datafed")
    return _check("settings", True, detail)


def _check_connection(catalog):
    who = catalog.whoami()
    if who["online"] and who["user"] and not who.get("problem"):
        return _check("datafed_connection", True, "DataFed answered: user %s, context %s, endpoint %s." % (
            who["user"], who["context"], who["endpoint"]))
    detail = who.get("problem") or "DataFed did not answer."
    if not who["online"]:
        fix = ("Check the network connection of this Mac (DataFed listens on datafed.ornl.gov, port 7512), "
               "then try again.")
    elif not who["user"]:
        fix = _SETUP_FIX
    else:
        fix = _ENDPOINT_FIX
    return _check("datafed_connection", False, detail, fix)


def run_checks(settings, online=False, catalog=None, client_dir=None, app_folders=GLOBUS_APP_FOLDERS,
               running_commands=_running_commands, environ=None, platform=sys.platform):
    """Run the checks. Returns {"checks": [{name, ok, detail, fix}], "online_checked": bool}.

    `catalog` is only used, and DataFed only contacted, when `online` is true and the mode is
    datafed. The other parameters exist so that tests can point the checks at their own folders.
    """
    environ = os.environ if environ is None else environ
    client_dir = default_client_dir(environ) if client_dir is None else Path(client_dir)
    checks = [_check_python(), _check_package(), _check_credentials(client_dir), _check_endpoint(client_dir, environ)]
    if platform == "darwin":
        checks.extend(_check_globus(app_folders, running_commands))
    settings_check = _check_settings(settings)
    checks.append(settings_check)
    online_checked = bool(online and catalog is not None and settings_check["ok"] and settings.mode == MODE_DATAFED)
    if online_checked:
        checks.append(_check_connection(catalog))
    return {"checks": checks, "online_checked": online_checked}
