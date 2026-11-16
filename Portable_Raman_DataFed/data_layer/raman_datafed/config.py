"""Settings, the standard folders, and the small helpers every module of the data layer shares.

The settings file and the folder layout are PROTOCOL.md sections 1 and 5.1.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import json
import os
import re
import stat
import tempfile
from pathlib import Path

CONFIG_FILE_NAME = "datafed_config.json"
LEDGER_FILE_NAME = "ledger.sqlite"
SYNC_LOCK_FILE_NAME = "sync.lock"
REVIEW_LOCK_FILE_NAME = "review.lock"
MODE_LOCAL_DEMO = "local-demo"
MODE_DATAFED = "datafed"
MODES = (MODE_LOCAL_DEMO, MODE_DATAFED)

DEFAULTS = {
    "mode": MODE_LOCAL_DEMO,
    "datafed": {"context": "", "root_alias": "raman-portable", "repo_id": "", "include_simulated": False},
    "review": {"confidence_threshold": None},
    "classifier": {"model_dir": "models/pca_svm_pure_20260922"},
    "sync": {"max_attempts": 5},
}

ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_ALIAS_LENGTH = 40
# The longest alias built from the root is "<root>-needs-review"; it has to fit too.
MAX_ROOT_ALIAS_LENGTH = MAX_ALIAS_LENGTH - len("-needs-review")
_PROJECT_ID_PATTERN = re.compile(r"^p/[A-Za-z0-9._-]+$")

# Uploads to ARC (PROTOCOL.md 5.6). Not in DEFAULTS and never written on first use: arc_settings
# holds their defaults and their rules, and only the arc-* commands stop on an invalid one.
ARC_UPLOAD_KEYS = (
    "arc_upload.demo", "arc_upload.transport", "arc_upload.folder", "arc_upload.include_simulated",
    "arc_upload.settle_seconds", "arc_upload.demo_folder",
    "arc_upload.globus.mac_collection", "arc_upload.globus.arc_collection", "arc_upload.globus.collection_root",
    "arc_upload.globus.client_id", "arc_upload.globus.deadline_hours", "arc_upload.globus.force_login",
    "arc_upload.ssh.username", "arc_upload.ssh.host", "arc_upload.ssh.known_hosts",
)
_ARC_UPLOAD_TEXT_KEYS = (
    "arc_upload.transport", "arc_upload.folder", "arc_upload.demo_folder", "arc_upload.globus.mac_collection",
    "arc_upload.globus.arc_collection", "arc_upload.globus.collection_root", "arc_upload.globus.client_id",
    "arc_upload.ssh.username", "arc_upload.ssh.host", "arc_upload.ssh.known_hosts",
)

# `config --set` keeps these as typed, so that a value such as 2026 stays text.
_TEXT_KEYS = ("mode", "datafed.context", "datafed.root_alias", "datafed.repo_id",
              "classifier.model_dir") + _ARC_UPLOAD_TEXT_KEYS


class DataLayerError(Exception):
    """An error whose message is one short sentence for the user.

    `detail` holds the longer explanation and `kind` lets a caller branch without parsing text.
    """

    kind = "error"

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


class ConfigError(DataLayerError):
    kind = "config"


class CommandError(DataLayerError):
    """A command cannot do what was asked (wrong option value, wrong mode, missing folder)."""

    kind = "command"


def utc_text(moment):
    """A UTC datetime in the controller's format: ISO 8601, milliseconds, trailing Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def utc_now():
    return utc_text(datetime.datetime.now(datetime.timezone.utc))


def seconds_since(text):
    """Seconds between a time written by `utc_text` and now."""
    then = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def atomic_write_bytes(path, data):
    """Replace `path` in one step, so that a reader never sees a half-written file."""
    path = Path(path)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path, value, indent=2):
    atomic_write_text(path, json.dumps(value, indent=indent, ensure_ascii=False) + "\n")


class Paths:
    """The files and folders of PROTOCOL.md section 1, all derived from HOME."""

    def __init__(self, home):
        home = Path(home)
        self.home = home
        self.config_file = home / CONFIG_FILE_NAME
        self.scan_sessions = home / "scan_sessions"
        self.sim_stage_file = self.scan_sessions / "_sim_stage.json"
        self.dark_measurements = home / "dark_measurements"
        self.data_catalog = home / "data_catalog"
        self.ledger_file = self.data_catalog / LEDGER_FILE_NAME
        self.sync_lock = self.data_catalog / SYNC_LOCK_FILE_NAME
        self.local_demo = self.data_catalog / "local_demo"
        self.staging = self.data_catalog / "staging"
        self.data_layer = home / "data_layer"
        self.arc = self.data_layer / "arc"
        self.models = home / "models"
        self.virtual_sample = home / "virtual_sample"
        self.studio = home / "studio"
        self.sim_runner = home / "raman_sim_runner"


def _with_defaults(data, defaults):
    """Fill the keys the file does not have; everything the file has, known or not, is kept."""
    merged = dict(data)
    for key, default in defaults.items():
        if key not in merged:
            merged[key] = copy.deepcopy(default)
        elif isinstance(default, dict) and isinstance(merged[key], dict):
            merged[key] = _with_defaults(merged[key], default)
    return merged


def _default_at(dotted):
    node = DEFAULTS
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


class Settings:
    """The content of HOME/datafed_config.json with typed access to the known keys."""

    def __init__(self, data, path, home):
        self.data = data
        self.path = Path(path)
        self.home = Path(home)

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted, value):
        parts = dotted.split(".")
        if not all(parts):
            raise ConfigError("Not a setting name: %r" % dotted)
        node = self.data
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ConfigError("%r is a value, not a group of settings" % part)
            node = child
        node[parts[-1]] = value

    def set_from_text(self, dotted, text):
        """Store what `config --set key=value` gave: JSON when it parses, the text otherwise."""
        value = text
        if dotted not in _TEXT_KEYS:
            with contextlib.suppress(ValueError):
                value = json.loads(text)
        self.set(dotted, value)

    def is_known(self, dotted):
        return _default_at(dotted)[1] or dotted in ARC_UPLOAD_KEYS

    def save(self):
        atomic_write_json(self.path, self.data)

    def as_dict(self):
        return copy.deepcopy(self.data)

    def problems(self):
        """Sentences describing every invalid setting; empty when the settings can be used."""
        found = []
        if self.get("mode") not in MODES:
            found.append('mode must be "local-demo" or "datafed", not %r' % (self.get("mode"),))
        context = self.get("datafed.context")
        if not isinstance(context, str) or (context and not _PROJECT_ID_PATTERN.match(context)):
            found.append('datafed.context must be empty (your own space) or a project id such as "p/my.project"')
        root = self.get("datafed.root_alias")
        if not isinstance(root, str) or not ALIAS_PATTERN.match(root) or len(root) > MAX_ROOT_ALIAS_LENGTH:
            found.append(
                "datafed.root_alias must be 1 to %d characters of a-z, 0-9, '.', '_' or '-', starting with a "
                "letter or digit" % MAX_ROOT_ALIAS_LENGTH
            )
        if not isinstance(self.get("datafed.repo_id"), str):
            found.append("datafed.repo_id must be text (empty for your default allocation)")
        if not isinstance(self.get("datafed.include_simulated"), bool):
            # Text such as "False" would read as true; only the two JSON words are taken.
            found.append("datafed.include_simulated must be true or false")
        threshold = self.get("review.confidence_threshold")
        if threshold is not None and (
            isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1
        ):
            found.append("review.confidence_threshold must be null or a number above 0 and at most 1")
        model_dir = self.get("classifier.model_dir")
        if not isinstance(model_dir, str) or not model_dir.strip():
            found.append("classifier.model_dir must name the model folder")
        attempts = self.get("sync.max_attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            found.append("sync.max_attempts must be a whole number of at least 1")
        return found

    def require_valid(self):
        found = self.problems()
        if found:
            raise ConfigError(
                "The settings in %s are not valid" % self.path.name,
                detail="; ".join(found) + ". Change them with: config --set key=value",
            )

    @property
    def mode(self):
        return self.get("mode")

    @property
    def context(self):
        return self.get("datafed.context")

    @property
    def root_alias(self):
        return self.get("datafed.root_alias")

    @property
    def repo_id(self):
        return self.get("datafed.repo_id")

    @property
    def include_simulated(self):
        return self.get("datafed.include_simulated") is True

    @property
    def holds_back_simulated(self):
        """True when sync leaves simulated captures out of the catalog: in datafed mode, unless
        datafed.include_simulated asks for them. The local demo takes everything."""
        return self.mode == MODE_DATAFED and not self.include_simulated

    @property
    def confidence_threshold(self):
        """None means: use the threshold stored with the model."""
        return self.get("review.confidence_threshold")

    @property
    def model_dir(self):
        folder = Path(self.get("classifier.model_dir")).expanduser()
        return folder if folder.is_absolute() else self.home / folder

    @property
    def max_attempts(self):
        return self.get("sync.max_attempts")

    @property
    def catalog_key(self):
        """Names the catalog tree the ledger rows belong to; it changes when the user switches catalog."""
        if self.mode == MODE_DATAFED:
            return "datafed|%s|%s" % (self.context, self.root_alias)
        return "%s|%s" % (MODE_LOCAL_DEMO, self.root_alias)


def load_settings(home):
    """Read the settings, writing the defaults on first use.

    The result is not validated here so that `config --set` can still repair a broken file;
    callers that act on the settings use `require_valid()`.
    """
    home = Path(home)
    path = Paths(home).config_file
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ConfigError("%s is not valid JSON" % path.name, detail="%s: %s" % (path, error)) from error
        if not isinstance(data, dict):
            raise ConfigError("%s must hold a JSON object" % path.name, detail=str(path))
        return Settings(_with_defaults(data, DEFAULTS), path, home)
    settings = Settings(copy.deepcopy(DEFAULTS), path, home)
    settings.save()
    return settings
