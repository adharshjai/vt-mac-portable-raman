"""The `arc_upload` settings of HOME/datafed_config.json, for uploads to ARC (PROTOCOL.md 5.1, 5.6).

They are not in config.DEFAULTS and are never written on first use: the effective values are the
file's values over DEFAULTS below. An invalid value stops only the arc-* commands; status, sync,
list, review and the Studio keep working.

Standard library and the package only: arc_commands imports this at module level.
"""

from __future__ import annotations

import copy
import os
import re
import stat
from pathlib import Path

from . import arc_paths
from .config import ConfigError

GROUP = "arc_upload"
ARC_PROJECTS_COLLECTION = "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"
# The Globus SDK's public tutorial app: the default until the team registers its own (README 14).
TUTORIAL_CLIENT_ID = "61338d24-54d5-408f-a10d-66c06b59f6d2"
DEFAULT_FOLDER = "/projects/pcl_photonics/raman_globus_upload"

DEFAULTS = {
    "demo": True,
    "transport": "globus",
    "folder": DEFAULT_FOLDER,
    "include_simulated": False,
    "settle_seconds": 60,
    "demo_folder": "data_catalog/arc_demo",
    "globus": {
        "mac_collection": "",
        "arc_collection": ARC_PROJECTS_COLLECTION,
        "collection_root": "/projects",
        "client_id": TUTORIAL_CLIENT_ID,
        "deadline_hours": 24,
        "force_login": False,
    },
    "ssh": {"username": "", "host": "datatransfer.arc.vt.edu", "known_hosts": ""},
}

TRANSPORTS = ("globus", "ssh")
UUID_KEYS = ("arc_upload.globus.mac_collection", "arc_upload.globus.arc_collection", "arc_upload.globus.client_id")
# Always used with fullmatch(): in Python "$" also matches before a newline at the end, so match()
# would take "name\n".
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
COLLECTION_ROOT = re.compile(r"^(/[A-Za-z0-9][A-Za-z0-9._-]*)+$")
USERNAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
HOST = re.compile(r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+arc\.vt\.edu$")
# A team known-hosts file reaches ssh as -o UserKnownHostsFile=<path>; ssh splits that value at
# spaces and expands % and ${ in it, so only these characters are taken.
KNOWN_HOSTS_PATH = re.compile(r"^/[A-Za-z0-9._/+@,-]+$")
# Folders of HOME the demo folder must stay out of (relative to HOME).
_KEEP_OUT = ("scan_sessions", "dark_measurements", "data_catalog/arc_uploads")


def flatten(nested, prefix=GROUP, shape=None):
    """{"arc_upload.demo": ..., "arc_upload.globus.mac_collection": ..., ...} of a nested group. Only the
    groups of DEFAULTS (globus, ssh) are opened: an invalid value that is an object stays one value."""
    shape = DEFAULTS if shape is None else shape
    flat = {}
    for key, value in nested.items():
        name = prefix + "." + key
        if isinstance(value, dict) and isinstance(shape.get(key), dict):
            flat.update(flatten(value, name, shape[key]))
        else:
            flat[name] = value
    return flat


def _uuid_text(value):
    """The canonical (lowercase) form of a UUID; anything else unchanged."""
    if isinstance(value, str) and UUID.fullmatch(value):
        return value.lower()
    return value


def canonical(dotted, value):
    """The form a value is stored in: a collection or app ID lowercase, everything else as given."""
    return _uuid_text(value) if dotted in UUID_KEYS else value


def holds_group(settings):
    """True when the settings file has an arc_upload group (valid or not)."""
    return GROUP in settings.data


def effective(settings):
    """The arc_upload values in use: the file's over DEFAULTS. Invalid values are kept as they are
    (problems() names them); a group that is not an object counts as absent."""
    values = copy.deepcopy(DEFAULTS)
    group = settings.get(GROUP)
    if not isinstance(group, dict):
        return values
    for key, default in DEFAULTS.items():
        if key not in group:
            continue
        if isinstance(default, dict):
            if isinstance(group[key], dict):
                for sub in default:
                    if sub in group[key]:
                        values[key][sub] = copy.deepcopy(group[key][sub])
        else:
            values[key] = copy.deepcopy(group[key])
    for dotted in UUID_KEYS:
        _section, name = dotted.split(".")[1:]
        values["globus"][name] = _uuid_text(values["globus"][name])
    return values


def demo_root(values, home):
    """The demo folder that stands in for ARC: HOME/<demo_folder>, or the folder itself when absolute."""
    folder = Path(os.path.expanduser(values["demo_folder"]))
    return folder if folder.is_absolute() else Path(home) / folder


def demo_root_or_none(values, home):
    """demo_root(), or None when arc_upload.demo_folder is not a usable text (problems() names it)."""
    value = values.get("demo_folder")
    if not isinstance(value, str) or not value.strip() or not _plain_text(value):
        return None
    return demo_root(values, home)


def globus_folder(values):
    """The ARC folder as ARC's Globus collection shows it, or None when the settings do not allow it."""
    try:
        return arc_paths.collection_path(values["folder"], values["globus"]["collection_root"])
    except (TypeError, ValueError, AttributeError):
        return None


def summary(values):
    """The short settings object of arc-sessions."""
    return {"demo": values["demo"], "transport": values["transport"], "folder": values["folder"],
            "globus_folder": globus_folder(values), "include_simulated": values["include_simulated"],
            "settle_seconds": values["settle_seconds"]}


def _whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _plain_text(value):
    """Text without control characters, a backslash or half of a character."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ord(character) < 32 or ord(character) == 127 or character == "\\" for character in value)


def _inside(path, folder):
    """True when `path` is `folder` or a folder below it. Letter case is ignored, as the Mac's disk ignores it
    (Scan_Sessions is scan_sessions there)."""
    path_parts = [part.casefold() for part in Path(path).parts]
    folder_parts = [part.casefold() for part in Path(folder).parts]
    return path_parts[:len(folder_parts)] == folder_parts


def _demo_folder_problem(value, home):
    if not isinstance(value, str) or not value.strip() or not _plain_text(value):
        return ("arc_upload.demo_folder must name a folder: a path relative to the controller's folder, such as "
                "data_catalog/arc_demo, or an absolute path")
    if home is None:
        return None
    root = Path(os.path.realpath(demo_root({"demo_folder": value}, home)))
    home = Path(os.path.realpath(home))
    if _inside(home, root):
        return "arc_upload.demo_folder must not be the controller's folder or a folder that holds it"
    for name in _KEEP_OUT:
        if _inside(root, Path(os.path.realpath(home / name))):
            return "arc_upload.demo_folder must not be inside %s/ of the controller's folder" % name
    return None


def known_hosts_problem(value):
    """Why a team known-hosts file cannot be used, or None (an empty setting is always fine)."""
    if value == "":
        return None
    if not isinstance(value, str) or not _plain_text(value) or not os.path.isabs(value):
        return "it must be an absolute path"
    if not KNOWN_HOSTS_PATH.fullmatch(value):
        return "%s holds a character that ssh reads differently: use letters, digits and . _ / + @ , - only" % value
    try:
        info = os.lstat(value)
    except OSError:
        return "there is no file %s" % value
    if stat.S_ISLNK(info.st_mode):
        return "%s is a link; name the file itself" % value
    if not stat.S_ISREG(info.st_mode):
        return "%s is not a plain file" % value
    if info.st_uid != os.getuid():
        return "%s belongs to another user" % value
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "%s may be changed by other users (remove their write permission)" % value
    if not os.access(value, os.R_OK):
        return "%s cannot be read" % value
    return None


def keyed_problems(settings, files=True):
    """[(dotted key, sentence)] of every invalid arc_upload value; [] when they can be used.

    `files` false leaves out what depends on files on this Mac (the team known-hosts file):
    the arc-* commands check that file only when a live SSH upload needs it.
    """
    group = settings.get(GROUP)
    if group is None and not holds_group(settings):
        return []
    if not isinstance(group, dict):
        return [(GROUP, "arc_upload must be a group of settings (a JSON object)")]
    found = []
    for section in ("globus", "ssh"):
        if section in group and not isinstance(group[section], dict):
            found.append((GROUP + "." + section, "arc_upload.%s must be a group of settings (a JSON object)" % section))
    values = effective(settings)
    globus, ssh = values["globus"], values["ssh"]

    for key in ("demo", "include_simulated"):
        if not isinstance(values[key], bool):
            # Text such as "False" would read as true; only the two JSON words are taken.
            found.append(("arc_upload." + key, "arc_upload.%s must be true or false" % key))
    if values["transport"] not in TRANSPORTS:
        found.append(("arc_upload.transport", 'arc_upload.transport must be "globus" or "ssh"'))
    if not _whole(values["settle_seconds"], 10, 3600):
        found.append(("arc_upload.settle_seconds",
                      "arc_upload.settle_seconds must be a whole number of seconds from 10 to 3600"))
    problem = _demo_folder_problem(values["demo_folder"], settings.home)
    if problem:
        found.append(("arc_upload.demo_folder", problem))

    root = globus["collection_root"]
    root_valid = isinstance(root, str) and (root == "/" or bool(COLLECTION_ROOT.fullmatch(root)))
    if not root_valid:
        found.append(("arc_upload.globus.collection_root",
                      'arc_upload.globus.collection_root must be "/" or an absolute folder such as /projects'))
    folder = values["folder"]
    if not isinstance(folder, str):
        found.append(("arc_upload.folder", "arc_upload.folder must be text: a folder on ARC such as %s"
                      % DEFAULT_FOLDER))
    else:
        found.extend(("arc_upload.folder", "arc_upload.folder: %s" % sentence.rstrip("."))
                     for sentence in arc_paths.check_cluster_folder(folder, root if root_valid else "/"))

    mac, arc = globus["mac_collection"], globus["arc_collection"]
    if not (mac == "" or (isinstance(mac, str) and UUID.fullmatch(mac))):
        found.append(("arc_upload.globus.mac_collection",
                      "arc_upload.globus.mac_collection must be empty or this Mac's Globus collection ID "
                      "(36 characters: hex digits and dashes)"))
    elif mac and isinstance(arc, str) and mac == arc.lower():
        found.append(("arc_upload.globus.mac_collection",
                      "arc_upload.globus.mac_collection must be this Mac's collection, not ARC's "
                      "(arc_upload.globus.arc_collection)"))
    if not (isinstance(arc, str) and UUID.fullmatch(arc)):
        found.append(("arc_upload.globus.arc_collection", "arc_upload.globus.arc_collection must be a Globus "
                      "collection ID (36 characters: hex digits and dashes)"))
    if not (isinstance(globus["client_id"], str) and UUID.fullmatch(globus["client_id"])):
        found.append(("arc_upload.globus.client_id", "arc_upload.globus.client_id must be the ID of a Globus app "
                      "(36 characters: hex digits and dashes)"))
    if not _whole(globus["deadline_hours"], 1, 168):
        found.append(("arc_upload.globus.deadline_hours",
                      "arc_upload.globus.deadline_hours must be a whole number of hours from 1 to 168"))
    if not isinstance(globus["force_login"], bool):
        found.append(("arc_upload.globus.force_login", "arc_upload.globus.force_login must be true or false"))

    username = ssh["username"]
    if not (username == "" or
            (isinstance(username, str) and USERNAME.fullmatch(username) and username != "root")):
        found.append(("arc_upload.ssh.username", "arc_upload.ssh.username must be empty or a VT username: 2 to 32 "
                      "characters of a-z, 0-9, '_' or '-', starting with a letter, not root"))
    if not (isinstance(ssh["host"], str) and HOST.fullmatch(ssh["host"])):
        found.append(("arc_upload.ssh.host",
                      "arc_upload.ssh.host must be the name of an ARC computer in lowercase, ending in .arc.vt.edu"))
    known_hosts = ssh["known_hosts"]
    if not isinstance(known_hosts, str):
        found.append(("arc_upload.ssh.known_hosts",
                      "arc_upload.ssh.known_hosts must be empty or the path of a known-hosts file"))
    elif files:
        problem = known_hosts_problem(known_hosts)
        if problem:
            found.append(("arc_upload.ssh.known_hosts", "arc_upload.ssh.known_hosts must be empty or a known-hosts "
                          "file of yours that only you can change: %s" % problem))
    elif known_hosts and not (_plain_text(known_hosts) and os.path.isabs(known_hosts)):
        found.append(("arc_upload.ssh.known_hosts", "arc_upload.ssh.known_hosts must be empty or an absolute path"))
    return found


def problems(settings, files=True):
    """Sentences naming every invalid arc_upload value; [] when they can be used (keyed_problems())."""
    return [sentence for _key, sentence in keyed_problems(settings, files)]


def require_valid(settings):
    found = problems(settings)
    if found:
        raise ConfigError("The ARC settings in datafed_config.json are not valid", detail="; ".join(found))


def _assigned(key, names):
    """True when `key` (of a problem) is one of the assigned `names`, or lies inside an assigned group."""
    return any(key == name or key.startswith(name + ".") for name in names)


def require_valid_change(before, after, assigned):
    """What config --set of arc_upload keys may save (`before` and `after`: the settings without and with
    the assignments; `assigned`: the dotted keys it sets). Refused when a value it sets is invalid, or when
    it brings a problem that was not there before. Problems of values it does not touch do not stop it
    (config reports them in arc_problems), so one invalid value never blocks repairing another. The team
    known-hosts file is looked at only when this command sets arc_upload.ssh.known_hosts."""
    assigned = list(assigned)
    # The key itself, or a group around it (arc_upload.ssh, arc_upload) set as a whole.
    files = _assigned("arc_upload.ssh.known_hosts", assigned)
    old = set(keyed_problems(before, files=files))
    found = [sentence for key, sentence in keyed_problems(after, files=files)
             if _assigned(key, assigned) or (key, sentence) not in old]
    if found:
        raise ConfigError("The ARC settings in datafed_config.json are not valid", detail="; ".join(found))


def flat_effective(settings):
    """Every effective arc_upload value under its dotted name, as config --set takes it."""
    return flatten(effective(settings))
