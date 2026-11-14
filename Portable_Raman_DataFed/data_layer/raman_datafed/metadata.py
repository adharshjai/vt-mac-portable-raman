"""Scan session folders: reading session.json and point_NNNN.json, validating the capture block,
building the catalog metadata of a raw spectrum (PROTOCOL.md 2 and 5.3), and completing folders
that were recorded without those fields (sessions of the original project).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from .catalog import DARK_TYPE, MAX_METADATA_BYTES, RAW_TYPE
from .config import DataLayerError, Paths, atomic_write_text, utc_now

SESSION_SCHEMA = "raman-session/1"
CAPTURE_SCHEMA = "raman-capture/1"
DARK_SCHEMA = "raman-dark/1"
SCAN_MODES = ("1d-x", "1d-y", "2d", "single")
LASER_POWER_LEVELS = ("LOW", "MEDIUM", "HIGH")
# Motor steps per millimetre of stage travel, the same on all three axes.
STEPS_PER_MM = 34700
REQUIRED_OPERATOR_FIELDS = ("sample_id", "operator", "location")
# What a dark spectrum is, by its description file (see dark_kind).
DARK_REAL, DARK_SIMULATED, DARK_UNKNOWN = "real", "simulated", "unknown"
# The record id of the dark is put into a raw record after its size was checked.
_ROOM_FOR_RECORD_IDS = 256
# Whole numbers in the files are 64-bit, as the controller writes them and SQLite stores them.
_WHOLE_RANGE = range(-2 ** 63, 2 ** 63)
# The deepest nesting of arrays and objects Qt's JSON parser takes (QJsonDocument::fromJson).
_QT_JSON_DEPTH = 1024

_POINT_FILE = re.compile(r"^point_(\d{4,})\.json$")
# The controller names a scan folder after its mode; used only for folders that say nothing else.
_FOLDER_SCAN_MODES = (("1d_x_scan_", "1d-x"), ("1d_y_scan_", "1d-y"), ("2d_scan_", "2d"), ("single_", "single"))


class MetadataError(DataLayerError):
    """A session or point file cannot be used as it is."""

    kind = "metadata"


def sha256_file(path):
    """(hex digest, size in bytes) of a file."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def read_json_object(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise MetadataError("%s does not exist" % path.name) from error
    except (ValueError, OSError, RecursionError) as error:
        # RecursionError: nested deeper than the json module goes. That is a fault of this one file,
        # which must not end the whole sync.
        raise MetadataError("%s cannot be read as JSON" % path.name, detail=str(error)) from error
    if not isinstance(value, dict):
        raise MetadataError("%s does not hold a JSON object" % path.name)
    return value


def _broken(value):
    """True when a text in this JSON value, a key included, holds a lone UTF-16 surrogate: half of a
    character such as an emoji, as text cut in the middle of one leaves it. JSON can carry it as an
    escape, but it has no UTF-8 form, so neither the ledger nor a catalog can store it."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _broken_problems(mapping, leave_out=()):
    """One problem per key of a JSON object whose value, or whose own name, holds a broken character
    (_broken). A broken name is called "a key", so that the broken text is not repeated."""
    return ["%s holds a broken character" % ("a key" if _broken(key) else key)
            for key, value in mapping.items() if key not in leave_out and (_broken(key) or _broken(value))]


def point_files(folder):
    """The point_NNNN.json files of a session folder, in point order."""
    found = []
    for entry in Path(folder).iterdir():
        match = _POINT_FILE.match(entry.name)
        if match and entry.is_file():
            found.append((int(match.group(1)), entry))
    return [entry for _number, entry in sorted(found)]


def is_session_folder(folder):
    """Folders starting with "_" are working folders of the helper programs, not sessions."""
    folder = Path(folder)
    return folder.is_dir() and not folder.name.startswith("_")


def read_session(folder):
    session = read_json_object(Path(folder) / "session.json")
    if session.get("schema") != SESSION_SCHEMA:
        raise MetadataError('session.json: schema is not "%s"' % SESSION_SCHEMA)
    if not _text(session.get("session_id")):
        raise MetadataError("session.json: session_id is missing")
    if _broken(session["session_id"]):
        raise MetadataError("session.json: session_id holds a broken character")
    return session


def read_point(path):
    """A point file with the keys every raw spectrum has; the capture block is checked separately."""
    point = read_json_object(path)
    name = Path(path).name
    for key in ("capture_id", "session_id"):
        if not _text(point.get(key)):
            raise MetadataError("%s: %s is missing" % (name, key))
        # The ledger row and the record alias are keyed by these: with a broken one the capture
        # cannot even wait in error/metadata.
        if _broken(point[key]):
            raise MetadataError("%s: %s holds a broken character" % (name, key))
    if not _whole(point.get("point_id")):
        raise MetadataError("%s: point_id is not a whole number" % name)
    intensity = point.get("intensity")
    if not isinstance(intensity, list) or not intensity:
        raise MetadataError("%s holds no spectrum (no intensity values)" % name)
    if point.get("status", "OK") != "OK":
        raise MetadataError("%s: the capture status is %r, not OK" % (name, point.get("status")))
    return point


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _whole(value):
    # A larger number is no point number or step count; the ledger could not store it, nor could a
    # step count be turned into millimetres.
    return isinstance(value, int) and not isinstance(value, bool) and value in _WHOLE_RANGE


def capture_problems(capture):
    """What is wrong with a point's capture block; an empty list when it can be registered.

    A block marked "imported" may leave unknown what nobody recorded at the time:
    laser_power_level, simulated and software may be null there.
    """
    if not isinstance(capture, dict):
        return ["the capture block is missing"]
    imported = capture.get("imported") is True
    problems = []
    if capture.get("schema") != CAPTURE_SCHEMA:
        problems.append('schema is not "%s"' % CAPTURE_SCHEMA)
    for key in REQUIRED_OPERATOR_FIELDS:
        if not _text(capture.get(key)):
            problems.append("%s is empty" % key)
    if not isinstance(capture.get("notes", ""), str):
        problems.append("notes is not text")
    level = capture.get("laser_power_level")
    if level not in LASER_POWER_LEVELS and not (imported and level is None):
        problems.append("laser_power_level is not LOW, MEDIUM or HIGH")
    exposure = capture.get("integration_time_us")
    if exposure is not None and (not _whole(exposure) or exposure <= 0):
        problems.append("integration_time_us is not a positive whole number or null")
    if capture.get("instrument") is not None and not isinstance(capture["instrument"], dict):
        problems.append("instrument is not an object or null")
    simulated = capture.get("simulated")
    if isinstance(simulated, dict):
        if not all(isinstance(simulated.get(key), bool) for key in ("backend", "motor")):
            problems.append("simulated.backend and simulated.motor must be true or false")
    elif not (imported and simulated is None):
        problems.append("simulated is missing")
    dark_file = capture.get("dark_file")
    if dark_file is not None and (not _text(dark_file) or Path(dark_file).name != dark_file):
        problems.append("dark_file is not a file name or null")
    software = capture.get("software")
    if not isinstance(software, dict) and not (imported and software is None):
        problems.append("software is missing")
    problems.extend(_broken_problems(capture))
    return problems


def _position(point):
    steps = {axis: point.get("controller_%s_steps" % axis) for axis in ("x", "y", "z")}
    if not all(_whole(value) for value in steps.values()):
        # A single capture has no motor program, so the controller does not know where the stage is.
        return None, None
    return steps, {axis: value / STEPS_PER_MM for axis, value in steps.items()}


def dark_description(dark_path):
    """dark_<stamp>.json, the controller's description of the dark spectrum dark_<stamp>.txt."""
    return Path(dark_path).with_suffix(".json")


def _refused_constant(name):
    raise ValueError("%s is not a JSON number" % name)


def _double(text):
    """A JSON number with a fraction or an exponent, refused where Qt's parser refuses it: beyond the
    range of a double (1e400), or so small that it reads as 0 although a digit of it is not (1e-400)."""
    value = float(text)
    if math.isinf(value) or (value == 0 and re.search("[1-9]", re.split("[eE]", text)[0])):
        raise ValueError("%s is beyond the range of a double" % text)
    return value


def _integer_of_double_range(text):
    """A JSON whole number, refused where Qt's parser refuses it: beyond the range of a double, into
    which Qt turns a whole number that does not fit in 64 bits."""
    value = int(text)
    try:
        float(value)
    except OverflowError as error:
        raise ValueError("%s is beyond the range of a double" % text) from error
    return value


def _depth(value):
    """How deeply arrays and objects are nested in a JSON value, the outermost one counting 1."""
    deepest, pending = 0, [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            deepest = max(deepest, depth)
            pending.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return deepest


def _read_like_the_controller(path):
    """The JSON value of a dark's description, read as the controller reads it (darkKind() in
    laser_test/main.cpp): the file as Python's json module reads it, less what Qt's parser refuses
    on top of that, NaN and Infinity, a number beyond the range of a double and nesting deeper than
    1024. Both then give a dark the same kind. Raises ValueError for what either refuses."""
    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=_refused_constant, parse_float=_double,
                       parse_int=_integer_of_double_range)
    if _depth(value) > _QT_JSON_DEPTH:
        raise ValueError("%s is nested more than %d deep" % (path.name, _QT_JSON_DEPTH))
    return value


def dark_kind(dark_path):
    """(DARK_REAL, DARK_SIMULATED or DARK_UNKNOWN, why it is unknown or None) for a dark spectrum file.

    The controller describes each dark it saves in a file beside it, and says there whether the
    capture backend simulated. A dark without a description counts as real: the darks taken
    before the controller wrote descriptions, the hand-off darks among them, were measured. A
    description that cannot be read, or that does not say, leaves the kind unknown.
    """
    description = dark_description(dark_path)
    if description == Path(dark_path):
        return DARK_UNKNOWN, "a dark named %s has no room for a description beside it" % description.name
    try:
        content = _read_like_the_controller(description)
    except FileNotFoundError:
        if not description.is_symlink():
            return DARK_REAL, None
        # A link to nothing is a description that cannot be read, as the controller sees it too.
        return DARK_UNKNOWN, "%s cannot be read" % description.name
    except (OSError, ValueError, RecursionError):
        # RecursionError: nested deeper than the json module goes, which Qt's parser refuses too.
        return DARK_UNKNOWN, "%s cannot be read" % description.name
    simulated = content.get("simulated") if isinstance(content, dict) else None
    backend = simulated.get("backend") if isinstance(simulated, dict) else None
    if not isinstance(backend, bool):
        return DARK_UNKNOWN, "%s does not say whether the backend simulated" % description.name
    return (DARK_SIMULATED if backend else DARK_REAL), None


def unpaired_reason(kind, why, backend_simulated):
    """Why a capture must not use a dark of this kind, or None when it may.

    A simulated spectrum minus a real dark, or the other way round, is not a spectrum of the
    sample, so the two are never paired. `backend_simulated` None (an imported session: nobody
    recorded it) pairs the capture with its dark whatever the kind, as before the kinds existed.
    """
    if backend_simulated is None:
        return None
    spectrum = DARK_SIMULATED if backend_simulated else DARK_REAL
    if kind == spectrum:
        return None
    if kind == DARK_UNKNOWN:
        return "dark not used: it is not known whether the dark was taken in simulation (%s); this spectrum is %s" % (
            why, spectrum)
    taken = "in simulation" if kind == DARK_SIMULATED else "with the real spectrometer"
    return "dark not used: the dark was taken %s; this spectrum is %s" % (taken, spectrum)


def dark_reference(home, dark_file, backend_simulated=None):
    """The "dark" entry of a raw record: file name and, when the file is here, its SHA-256 and kind.

    `backend_simulated` is the capture's simulated.backend. A dark the capture must not use
    (unpaired_reason) keeps its name and says why in "reason"; it never gets a record, so that
    nothing is derived from it.
    """
    if not dark_file:
        return None
    path = Paths(home).dark_measurements / dark_file
    if not path.is_file():
        return {"file": dark_file, "sha256": None, "record": None, "kind": None}
    kind, why = dark_kind(path)
    entry = {"file": dark_file, "sha256": sha256_file(path)[0], "record": None, "kind": kind}
    reason = unpaired_reason(kind, why, backend_simulated)
    if reason is not None:
        entry["reason"] = reason
    return entry


def build_dark_metadata(path):
    """The record metadata of a dark spectrum file; `simulated` is null when its kind is unknown."""
    digest, size = sha256_file(path)
    kind = dark_kind(path)[0]
    return {"type": DARK_TYPE, "schema": DARK_SCHEMA, "file": Path(path).name, "sha256": digest, "bytes": size,
            "simulated": None if kind == DARK_UNKNOWN else kind == DARK_SIMULATED}


def _check_size(metadata, point_name):
    """Refuse metadata the catalog would refuse, before a single attempt is spent on it.

    Measured as the DataFed adapter sends it (JSON with ASCII escapes), which is not less than
    the service is understood to count (not checked against a live server). Only the free text
    the operator typed can make it this large.
    """
    size = len(json.dumps(metadata))
    if size <= MAX_METADATA_BYTES - _ROOM_FOR_RECORD_IDS:
        return
    longest = max(("sample_id", "operator", "location", "notes"), key=lambda key: len(str(metadata[key])))
    raise MetadataError(
        "%s cannot be registered: its metadata takes %d bytes, more than the %d a catalog record takes; %s alone "
        "is %d characters long" % (point_name, size, MAX_METADATA_BYTES, longest, len(str(metadata[longest]))),
        detail="Shorten the %s of the capture block in %s." % (longest, point_name),
    )


def build_raw_metadata(home, session_folder, point, point_path):
    """The record metadata of one raw spectrum (PROTOCOL.md 5.3).

    `dark.record` is left null: the caller fills it in once the dark record exists in the catalog.
    """
    capture = point.get("capture")
    # The other keys of the point go into the record and the ledger too.
    problems = capture_problems(capture) + _broken_problems(point, leave_out=("capture",))
    if problems:
        raise MetadataError(
            "%s cannot be registered: %s" % (Path(point_path).name, "; ".join(problems)),
            detail="Supply the missing fields with: import-session --folder <the session folder> "
            "--sample-id ... --operator ... --location ...",
        )
    steps, millimetres = _position(point)
    simulated = capture.get("simulated")
    digest, size = sha256_file(point_path)
    intensity = point["intensity"]
    metadata = {
        "type": RAW_TYPE,
        "schema": CAPTURE_SCHEMA,
        "capture_id": point["capture_id"],
        "session_id": point["session_id"],
        "session_folder": session_folder,
        "point_id": point["point_id"],
        "scan_mode": point.get("scan_mode"),
        "sample_id": capture["sample_id"].strip(),
        "operator": capture["operator"].strip(),
        "location": capture["location"].strip(),
        "notes": capture.get("notes", ""),
        "timestamp_utc": point.get("completed_utc"),
        "started_utc": point.get("started_utc"),
        "capture_elapsed_ms": point.get("capture_elapsed_ms"),
        "integration_time_us": capture.get("integration_time_us"),
        "laser_power_level": capture.get("laser_power_level"),
        "position_steps": steps,
        "position_mm": millimetres,
        "pixel_count": point["pixel_count"] if _whole(point.get("pixel_count")) else len(intensity),
        "instrument": capture.get("instrument"),
        "simulated": None if simulated is None else bool(simulated["backend"] or simulated["motor"]),
        "dark": dark_reference(home, capture.get("dark_file"), None if simulated is None else simulated["backend"]),
        "raw_file": {"name": Path(point_path).name, "sha256": digest, "bytes": size},
        "software": capture.get("software"),
    }
    if capture.get("imported") is True:
        # Says that the operator fields were supplied afterwards, not recorded at capture time.
        metadata["imported"] = True
    _check_size(metadata, Path(point_path).name)
    return metadata


# ---------------------------------------------------------------------- completing a folder


def scan_mode_from_folder(name):
    for prefix, mode in _FOLDER_SCAN_MODES:
        if name.startswith(prefix):
            return mode
    return None


def _first_time(points, key):
    times = [point[key] for point in points if _text(point.get(key))]
    return min(times) if times else None


def plan_completion(folder, supplied):
    """Work out what a scan folder lacks, without changing anything.

    `supplied` holds the values given on the command line: sample_id, operator, location, notes,
    laser_power_level, simulated (True, False or None for unknown) and dark_file. Values already
    in the files always win; `supplied` only fills gaps.

    Returns a plan for `apply_completion`. Raises MetadataError when the folder is not a scan
    session or when a required field is neither in the files nor supplied.
    """
    folder = Path(folder)
    files = point_files(folder) if folder.is_dir() else []
    if not files:
        raise MetadataError("%s holds no point_NNNN.json files, so it is not a scan session" % folder.name)
    points, errors = [], []
    for path in files:
        try:
            point = read_point(path)
        except MetadataError as error:
            errors.append({"file": path.name, "message": str(error)})
            continue
        # Completing a point writes it again as UTF-8, which a broken character has no form in.
        broken = _broken_problems(point, leave_out=("capture",))
        if broken:
            errors.append({"file": path.name, "message": "; ".join(broken)})
            continue
        points.append((path, point))
    if not points:
        raise MetadataError(
            "No point file in %s is a raw spectrum" % folder.name,
            detail="; ".join(entry["message"] for entry in errors),
        )

    session_path = folder / "session.json"
    session = read_session(folder) if session_path.exists() else None
    session_ids = {point["session_id"] for _path, point in points}
    if session is not None:
        session_ids.add(session["session_id"])
    if len(session_ids) != 1:
        raise MetadataError("The point files in %s belong to more than one session" % folder.name)

    defaults = dict(session or {})
    for key, value in supplied.items():
        if value is not None and defaults.get(key) in (None, ""):
            defaults[key] = value
    if isinstance(defaults.get("simulated"), bool):
        # Which of the two simulators was used is not known afterwards, only that one was (or none).
        defaults["simulated"] = {"backend": defaults["simulated"], "motor": defaults["simulated"]}

    changes, missing = [], set()
    for path, point in points:
        capture = point.get("capture") if isinstance(point.get("capture"), dict) else {}
        if not capture_problems(capture):
            continue
        completed = dict(capture)
        completed["schema"] = CAPTURE_SCHEMA
        completed["imported"] = True
        for key in REQUIRED_OPERATOR_FIELDS:
            if not _text(completed.get(key)):
                completed[key] = defaults.get(key)
        completed.setdefault("notes", defaults.get("notes") or "")
        for key in ("laser_power_level", "integration_time_us", "instrument", "simulated", "dark_file", "software"):
            if completed.get(key) is None:
                completed[key] = defaults.get(key)
        missing.update(key for key in REQUIRED_OPERATOR_FIELDS if not _text(completed.get(key)))
        problems = capture_problems(completed)
        if problems and not missing:
            errors.append({"file": path.name, "message": "; ".join(problems)})
            continue
        changes.append((path.name, completed))
    if missing:
        options = ", ".join("--" + key.replace("_", "-") for key in REQUIRED_OPERATOR_FIELDS if key in missing)
        raise MetadataError(
            "%s lacks fields that have to be supplied: %s" % (folder.name, options),
            detail="Run import-session again with those options.",
        )

    session_id = session_ids.pop()
    new_session = None
    if session is None:
        every_point = [point for _path, point in points]
        modes = {point.get("scan_mode") for point in every_point}
        mode = modes.pop() if len(modes) == 1 else None
        new_session = {
            "schema": SESSION_SCHEMA,
            "session_id": session_id,
            "scan_mode": mode if mode in SCAN_MODES else scan_mode_from_folder(folder.name),
            "started_utc": _first_time(every_point, "started_utc") or _first_time(every_point, "completed_utc"),
            "sample_id": defaults.get("sample_id"),
            "operator": defaults.get("operator"),
            "location": defaults.get("location"),
            "notes": defaults.get("notes") or "",
            "laser_power_level": defaults.get("laser_power_level"),
            "integration_time_us": None,
            "instrument": None,
            "simulated": defaults.get("simulated"),
            "dark_file": defaults.get("dark_file"),
            "software": None,
        }
    return {"session_id": session_id, "changes": changes, "session": new_session, "errors": errors}


def apply_completion(folder, plan, folder_name=None, source_folder=None):
    """Write what `plan_completion` found missing into `folder`.

    A point file gains (or has completed) its "capture" key; every other key keeps its value.
    `folder_name` is the name the folder will have under scan_sessions when it is still being
    put in place under another one.
    """
    folder = Path(folder)
    if plan["session"] is not None:
        session = dict(plan["session"])
        session["folder"] = folder_name or folder.name
        session["imported"] = {"utc": utc_now(), "source_folder": str(source_folder or folder)}
        # Key order follows PROTOCOL.md 2.2 so that the file reads like one the controller wrote.
        ordered = {key: session[key] for key in ("schema", "session_id", "folder")}
        ordered.update(session)
        atomic_write_text(folder / "session.json", json.dumps(ordered, indent=2, ensure_ascii=False) + "\n")
    for name, capture in plan["changes"]:
        path = folder / name
        point = read_json_object(path)
        point["capture"] = capture
        atomic_write_text(path, json.dumps(point, indent=4, ensure_ascii=False) + "\n")
