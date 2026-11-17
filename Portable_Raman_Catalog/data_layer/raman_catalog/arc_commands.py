"""The arc-* commands: uploads of finished scan sessions to ARC (PROTOCOL.md 5.6 and section 6).

    arc-sessions, arc-upload, arc-list, arc-status, arc-check, arc-retry, arc-stop, arc-keep-copy,
    arc-doctor

Each prints one JSON object at once: none waits for a sign-in, a Terminal window or a transfer.
The work runs in a worker (arc_worker) that a command starts and never waits for. A failure
carries "reason" (a stable code) and "fix" (what to do next) beside the usual keys.

Loaded through cli.FEATURE_MODULES. This module and everything it imports at module level use
only the standard library and the package, so that it can never break the other commands; it
never imports globus_sdk.
"""

from __future__ import annotations

import copy
import functools
import importlib.util
import logging
import os
import sys
from pathlib import Path

from . import arc_paths, arc_payload, arc_records, arc_session, arc_settings, arc_worker, doctor
from .arc_records import ArcError, refusal
from .cli import UsageError
from .config import DEFAULTS, ConfigError, DataLayerError, Settings, load_settings, utc_now

log = logging.getLogger(__name__)

SSH_PROGRAM = "/usr/bin/ssh"
OPEN_PROGRAM = arc_worker.OPEN_PROGRAM
TERMINAL_APP = "/System/Applications/Utilities/Terminal.app"
REMOTE_SCRIPT = Path(__file__).with_name("arc_remote.sh")
DEMO_FAULTS = ("lose_submit_reply", "fail_task_once", "drop_connection")
DEMO_MAC_COLLECTION = arc_records.DEMO_MAC_COLLECTION
DEMO_ARC_COLLECTION = arc_records.DEMO_ARC_COLLECTION
SESSIONS_LIMIT = 30
LIST_LIMIT = 12

# A live SSH upload signs in with the VT username it keeps (settings_used): the reply names that account,
# since Retry and Check status never use a username set later (a Mac several people use).
UPLOAD_MESSAGES = {
    "globus": "A web page opens for you to sign in to Globus. Come back here when you have signed in.",
    "ssh": "A Terminal window opens. Sign in there once as {username}: the VT password of that account, then Duo.",
    "demo": "Demo upload started. Nothing leaves this Mac.",
}
CHECK_MESSAGES = {
    "globus": "A web page opens for you to sign in to Globus. Nothing is sent.",
    "ssh": "A Terminal window opens. Sign in there once as {username}: the VT password of that account, then Duo. "
           "Nothing is sent.",
    "demo": "Demo: checking the upload. Nothing leaves this Mac.",
}
STOP_MESSAGES = {
    "globus": "A web page opens for you to sign in to Globus; then the transfer is cancelled.",
    "demo": "Demo: stopping the upload. Nothing leaves this Mac.",
    "requested": "The upload program stops the upload within a second.",
    # A live Globus upload whose transfer may still run is cancelled first; a program still waiting for its
    # sign-in cancels it once you have signed in (review F6, decision I3 as built).
    "requested_globus": "The upload program stops the upload. If Globus may still be copying it, the transfer is "
                        "cancelled first, after the Globus sign-in if that page is still open.",
}
USAGE_FIX = "Run the command with -h to see its options."


class ArcUsageError(UsageError):
    """Wrong arguments of an arc-* command: kind usage (exit code 2), with reason and fix."""

    def __init__(self, message, detail=""):
        super().__init__(message, detail)
        self.reason = "usage"
        self.fix = USAGE_FIX
        self.extra = {"reason": "usage", "fix": USAGE_FIX}


class _ArcParser:
    """Mixed into the parsers of the arc-* commands: wrong arguments, unknown ones included, are
    reported by the command itself, with reason and fix."""

    def error(self, message):
        raise ArcUsageError("Invalid arguments: %s" % message, detail=self.format_usage().strip())

    def parse_known_args(self, args=None, namespace=None):
        namespace, extras = super().parse_known_args(args, namespace)
        if extras:
            self.error("unrecognized arguments: %s" % " ".join(extras))
        return namespace, extras


_PARSER_CLASSES = {}


def _arc_parser(parser):
    base = type(parser)
    if base not in _PARSER_CLASSES:
        _PARSER_CLASSES[base] = type("Arc" + base.__name__, (_ArcParser, base), {})
    parser.__class__ = _PARSER_CLASSES[base]
    return parser


def _command(function):
    """Every arc-* command: reap ended workers first (8.11), and fail with reason and fix."""

    @functools.wraps(function)
    def run(args, ctx):
        arc_worker.reap()
        try:
            return function(args, ctx)
        except (ArcError, ArcUsageError):
            raise
        except UsageError as error:
            raise ArcUsageError(str(error), detail=error.detail) from error
        except ConfigError as error:
            raise refusal("settings_invalid", problems=str(error).rstrip("."), detail=error.detail) from error
        except DataLayerError as error:
            raise ArcError(error.kind, "internal", str(error), "Try again; if it happens again, send this message "
                           "to the team.", detail=error.detail) from error
        except KeyboardInterrupt:
            raise refusal("interrupted") from None
        except Exception as error:
            log.exception("the command failed")
            lines = str(error).strip().splitlines()
            summary = "%s: %s" % (type(error).__name__, lines[0]) if lines else type(error).__name__
            raise refusal("internal", message=summary, detail="The traceback is in the log on stderr.") from error

    return run


# ---------------------------------------------------------------------- settings and setup


def _require_home(ctx):
    if not ctx.home.is_dir():
        raise refusal("home_missing", home=str(ctx.home))


def read_settings(ctx):
    """The settings file as it is (nothing is written: no file means the defaults)."""
    path = ctx.paths.config_file
    if path.exists():
        return load_settings(ctx.home)
    return Settings(copy.deepcopy(DEFAULTS), path, ctx.home)


def _settings(ctx):
    """(Settings, effective arc_upload values); refuses invalid ARC settings (config / settings_invalid)."""
    _require_home(ctx)
    settings = read_settings(ctx)
    found = arc_settings.problems(settings, files=False)
    if found:
        raise refusal("settings_invalid", problems="; ".join(problem.rstrip(".") for problem in found))
    return settings, arc_settings.effective(settings)


def _settings_for_records(ctx):
    """(Settings or None, effective values, the settings_invalid refusal or None) for the commands that act
    on records only (arc-list, arc-status, arc-stop): they answer whatever the ARC settings are, since a
    record keeps the settings it was made with (settings_used)."""
    _require_home(ctx)
    try:
        settings = read_settings(ctx)
    except ConfigError as error:
        return None, copy.deepcopy(arc_settings.DEFAULTS), refusal(
            "settings_invalid", problems=str(error).rstrip("."), detail=error.detail)
    found = arc_settings.problems(settings, files=False)
    problem = refusal("settings_invalid", problems="; ".join(item.rstrip(".") for item in found)) if found else None
    return settings, arc_settings.effective(settings), problem


def _executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def setup_errors(transport, values, platform=None):
    """What a live upload with this transport lacks on this Mac, as refusals (cheap local checks,
    design 8.3 step 4); the first one is the answer of arc-upload."""
    if (platform or sys.platform) != "darwin":
        return [refusal("not_macos")]
    found = []
    if transport == "globus":
        if not values["globus"]["mac_collection"]:
            found.append(refusal("mac_collection_missing"))
        if importlib.util.find_spec("globus_sdk") is None:
            found.append(refusal("globus_sdk_missing"))
        return found
    if not values["ssh"]["username"]:
        found.append(refusal("username_missing"))
    known_hosts = values["ssh"]["known_hosts"]
    if known_hosts and arc_settings.known_hosts_problem(known_hosts):
        found.append(refusal("known_hosts_unreadable", path=known_hosts))
    if not _executable(SSH_PROGRAM):
        found.append(refusal("ssh_missing"))
    if not (_executable(OPEN_PROGRAM) and os.path.isdir(TERMINAL_APP)):
        found.append(refusal("terminal_missing"))
    return found


def _demo_problem(values, home):
    """(demo root or None, why it cannot be the demo's stand-in for ARC or None): the rule of
    arc_records.ensure_demo_root, which the demo transports apply, looked at without changing anything."""
    root = arc_settings.demo_root_or_none(values, home)
    if root is None:
        return None, "arc_upload.demo_folder is not valid"
    return root, arc_records.demo_root_problem(root)


def _demo_errors(values, home):
    root, problem = _demo_problem(values, home)
    if problem is None:
        return []
    return [refusal("demo_folder_unwritable", path=str(root) if root is not None else "setting",
                    problem=problem)]


def _problems(errors):
    return [{"reason": error.reason, "text": str(error), "fix": error.fix} for error in errors]


def _ready(values, home, invalid=None):
    """ready of arc-list: what each transport lacks; with invalid settings (`invalid`: the settings_invalid
    refusal), every transport lacks them."""
    if invalid is not None:
        return {name: {"ok": False, "problems": _problems([invalid])} for name in ("globus", "ssh", "demo")}
    ready = {}
    for transport in ("globus", "ssh"):
        found = setup_errors(transport, values)
        ready[transport] = {"ok": not found, "problems": _problems(found)}
    found = _demo_errors(values, home)
    ready["demo"] = {"ok": not found, "problems": _problems(found)}
    return ready


# ---------------------------------------------------------------------- destinations


def destination(values, home, demo, transport, area, folder_name, upload_id, new_copy=False):
    """The record's "destination" (design 7.2): every path of the upload, computed once."""
    arc_folder = values["folder"]
    base = arc_folder if area == arc_session.AREA_REAL else arc_folder + "/simulated"
    root = values["globus"]["collection_root"]
    final = base + "/" + folder_name
    incoming = base + "/_incoming/" + folder_name + "." + upload_id
    demo_root = str(arc_settings.demo_root(values, home)) if demo else None
    globus = transport == "globus"
    return {
        "key": ("demo:%s|%s" % (demo_root, area)) if demo else ("live:%s|%s" % (arc_folder, area)),
        "arc_folder": arc_folder,
        "area": area,
        "base": base,
        "name": folder_name,
        "final_path": final,
        "incoming_path": incoming,
        "globus_base": arc_paths.collection_path(base, root) if globus else None,
        "globus_final_path": arc_paths.collection_path(final, root) if globus else None,
        "globus_incoming_path": arc_paths.collection_path(incoming, root) if globus else None,
        "demo_root": demo_root,
        "new_copy": bool(new_copy),
        "placed_as": None,
        "local_final_path": str(arc_paths.demo_local_path(demo_root, final)) if demo else None,
    }


def _key(values, home, demo, area):
    if demo:
        return "demo:%s|%s" % (arc_settings.demo_root(values, home), area)
    return "live:%s|%s" % (values["folder"], area)


def _base(values, area):
    return values["folder"] if area == arc_session.AREA_REAL else values["folder"] + "/simulated"


def settings_used(values, transport, demo):
    """The settings an upload keeps for its whole life (Retry same request never uses newer ones)."""
    globus, ssh = values["globus"], values["ssh"]
    used = {"folder": values["folder"], "collection_root": globus["collection_root"], "mac_collection": None,
            "arc_collection": None, "client_id": None, "deadline_hours": None, "force_login": None,
            "ssh_username": None, "ssh_host": None, "ssh_known_hosts": None,
            "include_simulated": values["include_simulated"], "settle_seconds": values["settle_seconds"]}
    if transport == "globus":
        used.update(mac_collection=DEMO_MAC_COLLECTION if demo else globus["mac_collection"],
                    arc_collection=DEMO_ARC_COLLECTION if demo else globus["arc_collection"],
                    client_id=globus["client_id"], deadline_hours=globus["deadline_hours"],
                    force_login=globus["force_login"])
    elif not demo:
        used.update(ssh_username=ssh["username"], ssh_host=ssh["host"], ssh_known_hosts=ssh["known_hosts"])
    return used


# ---------------------------------------------------------------------- records


def _group(record, key):
    """A group of a record (session, destination, worker...), {} when it is missing or not an object."""
    value = record.get(key) if isinstance(record, dict) else None
    return value if isinstance(value, dict) else {}


def _item(folder, record):
    """The listing item of an upload; one that cannot be shown is listed as unreadable (never acted on)."""
    try:
        return arc_records.item(record, arc_records.worker_alive(folder),
                                payload_present=arc_records.payload_present_in(folder))
    except Exception as error:  # noqa: BLE001 - one odd record must not hide every other upload
        log.warning("the record of %s cannot be shown: %s", Path(folder).name, type(error).__name__)
        return arc_records.unreadable_item(folder, arc_records.UnreadableRecord(
            "record.json of %s does not have the shape of a record" % Path(folder).name))


def _records(paths):
    return arc_records.list_records(paths)


def _newest_first(entries):
    return sorted(entries, key=lambda entry: str((entry[1] or {}).get("created_utc") or ""), reverse=True)


def _shown_name(value):
    text = str(value)
    if arc_session.plain_name(text) and len(text) <= 200:
        return text
    return repr(text)[:80]


def _find(ctx, upload_id):
    """(folder, record or None, error or None) of an upload; refuses an unknown id."""
    if not isinstance(upload_id, str) or not arc_records.UPLOAD_ID.fullmatch(upload_id):
        raise refusal("unknown_upload", id=_shown_name(upload_id))
    folder = arc_records.record_folder(ctx.paths, upload_id)
    if not folder.is_dir() or folder.is_symlink():
        raise refusal("unknown_upload", id=upload_id)
    try:
        return folder, arc_records.read_record(folder), None
    except arc_records.UnreadableRecord as error:
        return folder, None, error


def _incoming_shown(record):
    destination = record.get("destination") or {}
    incoming = destination.get("incoming_path")
    if record.get("demo") and destination.get("demo_root") and incoming:
        return arc_records.local_path(destination["demo_root"], incoming)
    return incoming


def _live_worker_elsewhere(paths, session_id, key):
    """The session folder of another live upload whose worker is alive, or None. An upload of this
    session to the same place is not another one: arc-upload shows it instead (existing). A Check
    status worker never holds another upload back: it sends nothing, and Stop upload ends it."""
    for folder, record, _error in _records(paths):
        if record is None or record.get("demo") is not False:
            continue
        if _group(record, "session").get("session_id") == session_id and \
                _group(record, "destination").get("key") == key:
            continue
        if arc_records.worker_alive(folder) and _group(record, "worker").get("action") != "check":
            return _group(record, "session").get("folder") or folder.name
    return None


# ---------------------------------------------------------------------- arc-sessions


def _uploads_of(records, session_id, live_prefix, demo_prefix):
    found = {"live": None, "demo": None}
    for folder, record, _error in _newest_first(records):
        if record is None or _group(record, "session").get("session_id") != session_id:
            continue
        key = _group(record, "destination").get("key") or ""
        slot = "live" if key.startswith(live_prefix) else "demo" if key.startswith(demo_prefix) else None
        if slot is None or found[slot] is not None:
            continue
        shown = _item(folder, record)
        found[slot] = {"upload_id": shown["upload_id"], "state": shown["state"],
                       "display_state": shown["display_state"], "display_label": shown["display_label"],
                       "transport": shown["transport"], "updated_utc": shown["updated_utc"],
                       "final_path": shown["destination"]["final_path"]}
    return found


def _session_item(paths, values, result, records):
    session = result.summary()
    include = values["include_simulated"]
    root = values["globus"]["collection_root"]
    live_area, live_reason = arc_session.policy(result.kind, False, include)
    demo_area, demo_reason = arc_session.policy(result.kind, True, include)
    live = {"allowed": live_reason is None, "why_not": _why(live_reason), "area": live_area,
            "cluster_path": None, "globus_path": None}
    if live_area:
        live["cluster_path"] = _base(values, live_area) + "/" + result.name
        live["globus_path"] = arc_paths.collection_path(live["cluster_path"], root)
    demo = {"allowed": demo_reason is None, "why_not": _why(demo_reason), "area": demo_area, "demo_path": None}
    if demo_area:
        demo["demo_path"] = str(arc_paths.demo_local_path(arc_settings.demo_root(values, paths.home),
                                                          _base(values, demo_area) + "/" + result.name))
    session_id = session["session_id"]
    error = result.refusal
    return {
        "folder": result.name, "session_id": session_id, "scan_mode": session["scan_mode"],
        "started_utc": session["started_utc"], "sample_id": session["sample_id"],
        "points": result.points, "points_planned": result.planned, "end": result.end,
        "end_text": result.end_text(), "kind": result.kind,
        "files": result.file_count(), "bytes": result.byte_count(),
        "darks": arc_session.public_darks(result.darks),
        "ready": error is None, "retry_after_seconds": result.retry_after_seconds,
        "why_not": None if error is None else {"reason": error.reason, "text": str(error), "fix": error.fix},
        "live": live, "demo": demo,
        "uploads": _uploads_of(records, session_id, "live:%s|" % values["folder"],
                               "demo:%s|" % arc_settings.demo_root(values, paths.home)) if session_id else
        {"live": None, "demo": None},
        "warnings": list(result.warnings),
    }


def _why(reason):
    if reason is None:
        return None
    error = refusal(reason)
    return {"reason": reason, "text": str(error), "fix": error.fix}


@_command
def cmd_sessions(args, ctx):
    _settings_obj, values = _settings(ctx)
    limit = getattr(args, "limit", None)
    limit = SESSIONS_LIMIT if limit is None else limit
    if limit < 1:
        raise ArcUsageError("--limit must be at least 1")
    folders = arc_session.session_folders(ctx.paths)
    idle = arc_session.controller_idle(ctx.paths, bool(getattr(args, "controller_idle", False)))
    commands = arc_session.running_commands()
    records = _records(ctx.paths)
    items = []
    for folder in folders[:limit]:
        result = arc_session.check(ctx.paths, folder, values["settle_seconds"], idle, full=False, commands=commands)
        items.append(_session_item(ctx.paths, values, result, records))
    return {"items": items, "total": len(folders), "controller_idle_used": idle,
            "settings": arc_settings.summary(values)}


# ---------------------------------------------------------------------- arc-upload


def _notes(result, area, declared, new_copy):
    """Sentences for the confirmation (14.5) that apply to this upload."""
    notes = []
    if result.end == "stopped":
        notes.append("This scan was stopped early: %d of %d spectra." % (result.points, result.planned))
    elif result.end == "failed":
        notes.append("This scan ended with an error after %d spectra." % result.points)
    elif result.end == "ended":
        notes.append("This scan has no end mark: %d spectra." % result.points)
    if area == arc_session.AREA_SIMULATED:
        if result.kind == arc_session.KIND_UNKNOWN:
            notes.append("It is not recorded whether this session was made in simulation, so it goes into the "
                         "separate folder simulated/.")
        else:
            notes.append("This session was made in simulation. It goes into the separate folder simulated/.")
    if declared:
        notes.append("You said this session was measured with the real instruments; the upload records that.")
    if new_copy:
        notes.append("It goes beside the earlier copy, under the first free name of %s, %s_copy2, %s_copy3 and so on."
                     % (result.name, result.name, result.name))
    return notes


def _plan(values, home, result, transport, demo, area, declared, new_copy):
    final = _base(values, area) + "/" + result.name
    ssh_target = None
    if transport == "ssh" and not demo:
        ssh_target = "%s@%s:%s" % (values["ssh"]["username"], values["ssh"]["host"], final)
    return {
        "session": result.summary(declared), "transport": transport, "demo": demo,
        "files": result.file_count(), "bytes": result.byte_count(),
        "darks": arc_session.public_darks(result.darks), "excluded": list(result.listing.excluded),
        "warnings": list(result.warnings), "notes": _notes(result, area, declared, new_copy), "area": area,
        "new_copy": new_copy,
        "destination": {
            "cluster_path": final,
            "globus_path": arc_paths.collection_path(final, values["globus"]["collection_root"]),
            "demo_path": str(arc_paths.demo_local_path(arc_settings.demo_root(values, home), final)) if demo else None,
            "ssh_target": ssh_target,
        },
    }


def _earlier(paths, session_id, key):
    """[(folder, record)] of earlier uploads of this session to this place, oldest first."""
    found = [(folder, record) for folder, record, _error in _records(paths)
             if record is not None and arc_records.is_current(record) and
             _group(record, "session").get("session_id") == session_id and
             _group(record, "destination").get("key") == key]
    return sorted(found, key=lambda entry: str(entry[1].get("created_utc") or ""))


def _on_arc(record):
    """A verified upload whose copy is on ARC as far as is known: no Check status since, or the newest one
    that looked found it (not "On ARC, last check failed"). A check that could not look (a sign-in that did
    not happen, for example) changes nothing (review REG-01)."""
    return record.get("state") == "verified" and not arc_records.check_failed(record)


def _checked_date(record):
    """When the copy was last seen as it was sent: the newest check when it found it, else the verification (a
    check that could not look saw nothing; the window's "checked on" follows the same rule)."""
    last = arc_records.last_check(record) or {}
    verification = _group(record, "verification")
    text = last.get("utc") if last.get("ok") is True else None
    text = text or verification.get("utc") or record.get("state_since_utc") or ""
    text = text if isinstance(text, str) else ""
    return text[:16].replace("T", " ") + " UTC" if len(text) >= 16 else text


def _decide(earlier, args, name):
    """Step 5 of arc-upload: (existing (folder, record) or None, new_copy); raises the conflicts.

    A verified upload counts as on ARC unless its newest Check status that looked found the copy missing
    or different: then a new upload goes ahead without --new, under the session's own name (new_copy
    false), and the person decides at a conflict if that name is still taken. A check that could not look
    never lets a new upload go ahead (review REG-01)."""
    active = [entry for entry in earlier if entry[1].get("state") in arc_records.UNFINISHED_STATES]
    if active:
        folder, record = active[-1]
        if getattr(args, "new", False):
            raise refusal("in_progress", extra={"upload": _item(folder, record)})
        return active[-1], False
    on_arc = [entry for entry in earlier if _on_arc(entry[1])]
    ended = [entry for entry in earlier if entry[1].get("state") in arc_records.FINAL_STATES]
    if ended and not getattr(args, "new", False):
        folder, record = ended[-1]
        if record.get("state") != "verified":
            shown = _item(folder, record)
            raise refusal("earlier_upload", label=shown["display_label"], extra={"upload": shown})
        if on_arc:
            folder, record = on_arc[-1]
            shown = _item(folder, record)
            demo = bool(record.get("demo"))
            other = _group(record, "session").get("folder")
            if other and other != name:
                # A copy of a session folder (cp -R) holds the same session as the folder uploaded before.
                variant = "demo_same_session" if demo else "same_session"
            else:
                variant = "demo" if demo else None
            path = shown["destination"]["local_final_path"] if demo else shown["destination"]["final_path"]
            raise refusal("already_on_arc", variant=variant, folder=name, other=other, path=path,
                          date=_checked_date(record), extra={"upload": shown})
    return None, bool(on_arc)


def new_record(values, home, result, transport, demo, area, declared, new_copy, fault_name, idle):
    upload_id = arc_records.new_upload_id()
    now = utc_now()
    record = {
        "schema": arc_records.SCHEMA,
        "upload_id": upload_id,
        "created_utc": now,
        "updated_utc": now,
        "transport": transport,
        "demo": demo,
        "state": "queued",
        "state_since_utc": now,
        "message": arc_records.message("queued", demo),
        "error": None,
        "warnings": list(result.warnings),
        "notes": _notes(result, area, declared, new_copy),
        "resume": None,
        "conflict_at": None,
        "session": result.summary(declared),
        "destination": destination(values, home, demo, transport, area, result.name, upload_id, new_copy),
        "settings_used": settings_used(values, transport, demo),
        "payload": None,
        "progress": {"files": result.file_count(), "files_done": 0, "bytes": result.byte_count(), "bytes_done": 0},
        "requests": {"stop_utc": None},
        "worker": expected_worker("send", transport == "ssh" and not demo),
        "globus": None,
        "ssh": None,
        "verification": None,
        "checks": [],
        "demo_fault": fault_name,
        "controller_idle_used": bool(idle),
    }
    if transport == "globus":
        record["globus"] = {"identity": None, "attempt": 1, "submission_id": None, "request_saved_utc": None,
                            "label": None, "posts": [], "task_id": None, "task": None, "earlier_attempts": []}
    else:
        used = record["settings_used"]
        record["ssh"] = {"attempt": 1, "host": used["ssh_host"], "username": used["ssh_username"],
                         "known_hosts": used["ssh_known_hosts"], "terminal_opened_utc": None, "runs": [],
                         "sent": False}
    return record


def _start(ctx, folder, action):
    """Start the work on an upload whose record is written: a detached worker, or for live SSH the
    Terminal worker of the record's current attempt. Returns (worker kind, record)."""
    record = arc_records.read_record(folder)
    if record.get("transport") == "ssh" and not record.get("demo"):
        script = arc_worker.write_terminal_script(ctx.home, folder, record, action, record["ssh"]["attempt"])
        opened, why = arc_worker.open_terminal(script)
        if not opened:
            # The record stays as it is (a send waits for Terminal), so that ssh_upload.command still runs this
            # attempt when it is double-clicked, or when Terminal opens late; the listing shows the upload
            # as not started at once, with Retry same request.
            def not_opened(current):
                current["ssh"]["terminal_open_failed_utc"] = utc_now()
                current["ssh"]["terminal_opened_utc"] = None

            record = arc_records.update_record(folder, not_opened)
            arc_records.append_event(folder, record.get("state"), "Terminal could not be opened: %s."
                                     % why.rstrip("."))
            raise refusal("terminal_not_opened", variant="check" if action == "check" else None,
                          message=why.rstrip("."), extra={"upload": _item(folder, record)})

        def opened_now(current):
            current["ssh"]["terminal_opened_utc"] = utc_now()
            current["ssh"]["terminal_open_failed_utc"] = None

        return "terminal", arc_records.update_record(folder, opened_now)
    arc_worker.spawn(ctx.home, record["upload_id"], action)
    return "detached", record


def expected_worker(action, terminal):
    """record["worker"] as a command writes it before it starts a worker: what that worker will do. The
    worker writes its own pid when it takes the upload; until then the listing (which shows `worker` only
    while worker.lock is held) already knows the action of a worker that has just taken the lock."""
    return {"pid": None, "action": action, "started_utc": None, "terminal": bool(terminal)}


def _transport_message(messages, record):
    if record.get("demo"):
        return messages["demo"]
    username = _group(record, "settings_used").get("ssh_username") or ""
    return messages[record.get("transport")].format(username=username)


@_command
def cmd_upload(args, ctx):
    _settings_obj, values = _settings(ctx)
    name = args.session
    transport = args.transport or values["transport"]
    demo = values["demo"] if args.demo is None else args.demo
    fault_name = getattr(args, "demo_fault", None)
    if fault_name and not demo:
        raise ArcUsageError("--demo-fault works only with a demo upload (--demo)")
    declared = bool(getattr(args, "declare_real", False))
    if not arc_session.is_session_entry(ctx.paths.scan_sessions, name):
        raise refusal("unknown_session", folder=_shown_name(name))
    idle = arc_session.controller_idle(ctx.paths, bool(getattr(args, "controller_idle", False)))
    result = arc_session.check(ctx.paths, Path(ctx.paths.scan_sessions) / name, values["settle_seconds"], idle,
                               full=True, declare_real=declared, commands=arc_session.running_commands())
    if result.refusal is not None:
        raise result.refusal
    area, refused = arc_session.check_policy(result, demo, values["include_simulated"], declared)
    if refused is not None:
        raise refused
    session_id = result.session["session_id"]
    key = _key(values, ctx.home, demo, area)
    if not demo:
        found = setup_errors(transport, values)
        if found:
            raise found[0]
        other = _live_worker_elsewhere(ctx.paths, session_id, key)
        if other is not None:
            raise refusal("another_live_upload", folder=other)
    else:
        # The demo's own setup (arc-list ready.demo): a demo folder that can be made and written.
        found = _demo_errors(values, ctx.home)
        if found:
            raise found[0]

    if getattr(args, "dry_run", False):
        existing, new_copy = _decide(_earlier(ctx.paths, session_id, key), args, name)
        return {"dry_run": True,
                "plan": _plan(values, ctx.home, result, transport, demo, area, declared, new_copy),
                "existing": _item(*existing) if existing else None}

    with arc_records.requests_lock(ctx.paths):
        arc_records.remove_stale_creations(ctx.paths)
        existing, new_copy = _decide(_earlier(ctx.paths, session_id, key), args, name)
        if existing is not None:
            shown = _item(*existing)
            return {"upload": shown, "existing": True, "worker": None,
                    "message": "An upload of this session is already under way (%s)." % shown["display_label"]}
        record = new_record(values, ctx.home, result, transport, demo, area, declared, new_copy, fault_name, idle)
        while arc_records.record_folder(ctx.paths, record["upload_id"]).exists():
            record["upload_id"] = arc_records.new_upload_id()
            record["destination"] = destination(values, ctx.home, demo, transport, area, name, record["upload_id"],
                                                new_copy)
        folder = arc_records.create_record(ctx.paths, record, fault=arc_worker.fault)
    log.info("upload %s of %s created (%s, %s)", record["upload_id"], name, transport, "demo" if demo else "live")
    arc_worker.fault("upload_before_spawn")
    if transport == "ssh" and not demo:
        def waiting(current):
            arc_records.set_state(current, "waiting_for_terminal", arc_records.message("waiting_for_terminal"))

        arc_records.update_record(folder, waiting)
        arc_records.append_event(folder, "waiting_for_terminal", arc_records.message("waiting_for_terminal"))
    kind, record = _start(ctx, folder, "send")
    return {"upload": _item(folder, record), "existing": False, "worker": kind,
            "message": _transport_message(UPLOAD_MESSAGES, record)}


# ---------------------------------------------------------------------- arc-list, arc-status


@_command
def cmd_list(args, ctx):
    # The uploads are listed whatever the ARC settings are (each record keeps its own): with invalid
    # settings, "ready" says so for every transport, and Stop upload still works.
    settings, values, invalid = _settings_for_records(ctx)
    limit = getattr(args, "limit", None)
    limit = LIST_LIMIT if limit is None else limit
    if limit < 1:
        raise ArcUsageError("--limit must be at least 1")
    session = getattr(args, "session", None)
    items = []
    for folder, record, error in _records(ctx.paths):
        if record is None:
            if session is None:
                items.append(arc_records.unreadable_item(folder, error))
            continue
        if session is not None and _group(record, "session").get("folder") != session:
            continue
        items.append(_item(folder, record))
    items.sort(key=lambda shown: str(shown["created_utc"] or ""), reverse=True)
    unfinished = [shown for shown in items if shown["state"] in arc_records.UNFINISHED_STATES]
    shown = items[:limit] + [entry for entry in items[limit:] if entry["state"] in arc_records.UNFINISHED_STATES]
    return {"items": shown, "total": len(items),
            "active": sum(1 for entry in items if entry["worker"]["alive"]),
            "unfinished": len(unfinished),
            # Uploads that wait for a person (the window's ARC upload button), counted over all, not only
            # the items shown.
            "attention": sum(1 for entry in items if arc_records.needs_attention(entry["display_state"], entry)),
            "ready": _ready(values, ctx.home, invalid),
            "settings": arc_settings.flat_effective(settings) if settings is not None else
            arc_settings.flatten(arc_settings.DEFAULTS),
            "as_of_utc": utc_now()}


def _attempts(record):
    if isinstance(record.get("globus"), dict):
        globus = record["globus"]
        attempts = [dict(attempt) for attempt in globus.get("earlier_attempts") or [] if isinstance(attempt, dict)]
        task = globus.get("task") if isinstance(globus.get("task"), dict) else {}
        attempts.append({"attempt": globus.get("attempt"), "submission_id": globus.get("submission_id"),
                         "task_id": globus.get("task_id"), "status": task.get("status"),
                         "posts": globus.get("posts") or []})
        return attempts
    if isinstance(record.get("ssh"), dict):
        return list(record["ssh"].get("runs") or [])
    return []


@_command
def cmd_status(args, ctx):
    # A record is shown whatever the ARC settings are.
    _require_home(ctx)
    folder, record, error = _find(ctx, args.upload)
    if record is None:
        shown = arc_records.unreadable_item(folder, error)
        shown.update(events=arc_records.read_events(folder), manifest=None, attempts=[], record_folder=str(folder),
                     session=None)
        return {"upload": shown}
    shown = _item(folder, record)
    manifest = None
    content = arc_payload.read_manifest(folder)
    if content is not None:
        totals = content.get("totals") if isinstance(content.get("totals"), dict) else {}
        manifest = {"files": totals.get("files"), "bytes": totals.get("bytes"), "darks": content.get("darks")}
    # The session as the record keeps it (points, how the scan ended, kind): the window's details say
    # "13 spectra, finished" from it.
    session = record.get("session") if isinstance(record.get("session"), dict) else None
    shown.update(events=arc_records.read_events(folder), manifest=manifest, attempts=_attempts(record),
                 record_folder=str(folder), session=session)
    return {"upload": shown}


# ---------------------------------------------------------------------- arc-check, arc-retry, arc-stop, arc-keep-copy


def _busy(record, extra=None):
    """The busy refusal: a worker works on the upload (in a Terminal window: its own wording)."""
    terminal = bool(_group(record, "worker").get("terminal"))
    return refusal("busy", variant="terminal" if terminal else None, extra=extra)


def _not_allowed(folder, record, action, refusal_reason, alive, shown_label, extra=None):
    """The refusal of an action the upload does not offer now: payload_missing when only the snapshot is
    missing (it was kept, and its folder is gone), else `refusal_reason`."""
    present = arc_records.payload_present_in(folder)
    display_state = arc_records.display(record, alive)[0]
    if action in ("retry", "keep_copy") and not present and arc_records.payload_made(record) and \
            action in arc_records.actions(record, display_state, alive, payload_present=True):
        return refusal("payload_missing", extra=extra)
    return refusal(refusal_reason, label=shown_label, extra=extra)


def _still_allowed(folder, record, action, refusal_reason):
    """Inside record.lock: the action is still one of the upload's (another click, the window and the
    Studio, or a worker that just ended may have changed it since it was looked at)."""
    alive = arc_records.worker_alive(folder)
    if alive and action != "stop":
        raise _busy(record)
    display_state, label, _message = arc_records.display(record, alive)
    if action not in arc_records.actions(record, display_state, alive, arc_records.payload_present_in(folder)):
        raise _not_allowed(folder, record, action, refusal_reason, alive, label)
    return alive


def _acting_on(ctx, args, action, refusal_reason):
    """(folder, record, item) of an upload the person acts on, after the shared refusals (8.6). Stop works
    whatever the ARC settings are: it acts on the record, which keeps the settings it was made with."""
    if action == "stop":
        _require_home(ctx)
    else:
        _settings(ctx)
    folder, record, error = _find(ctx, args.upload)
    if getattr(args, "demo_only", False) and (record is None or record.get("demo") is not True):
        raise refusal("not_demo")
    if record is None:
        raise refusal(refusal_reason, label="unreadable",
                      extra={"upload": arc_records.unreadable_item(folder, error)})
    alive = arc_records.worker_alive(folder)
    shown = _item(folder, record)
    if alive and action != "stop":
        raise _busy(record, extra={"upload": shown})
    if action not in shown["actions"]:
        raise _not_allowed(folder, record, action, refusal_reason, alive, shown["display_label"],
                           extra={"upload": shown})
    return folder, record, shown


def _restart(folder, state, resume_at, new_copy=False):
    """Retry same request and Keep as new copy: the record goes back to queued (or, for live SSH, to
    waiting_for_terminal with a new attempt), under record.lock, while no worker holds the upload."""

    def mutate(record):
        if new_copy:
            _still_allowed(folder, record, "keep_copy", "no_conflict")
        else:
            _still_allowed(folder, record, "retry", "not_retryable")
        demo = bool(record.get("demo"))
        key = "waiting_for_terminal" if state == "waiting_for_terminal" else "queued"
        arc_records.set_state(record, state, arc_records.message(key, demo))
        record["state_since_utc"] = utc_now()
        record["resume"] = {"at": resume_at} if resume_at else None
        record["error"] = None
        record["conflict_at"] = None
        record["requests"] = {"stop_utc": None}
        record["worker"] = expected_worker("send", record.get("transport") == "ssh" and not demo)
        if new_copy:
            record["destination"]["new_copy"] = True
        if isinstance(record.get("ssh"), dict):
            record["ssh"]["attempt"] = int(record["ssh"].get("attempt") or 0) + 1

    record = arc_records.update_record(folder, mutate)
    arc_records.append_event(folder, record["state"], "Retry same request." if not new_copy else
                             "Keep as new copy.")
    return record


@_command
def cmd_retry(args, ctx):
    folder, record, shown = _acting_on(ctx, args, "retry", "not_retryable")
    gone = arc_records.payload_made(record) and not (record["payload"].get("kept") and
                                                     arc_records.payload_present_in(folder))
    if gone:
        raise refusal("payload_missing", extra={"upload": shown})
    state, resume_at = arc_records.retry_target(record, shown["display_state"])
    _restart(folder, state, resume_at)
    kind, record = _start(ctx, folder, "send")
    answer = {"upload": _item(folder, record), "worker": kind, "message": _transport_message(UPLOAD_MESSAGES, record)}
    if resume_at == "submitting":
        answer["same_submission"] = True
    return answer


@_command
def cmd_keep_copy(args, ctx):
    folder, record, shown = _acting_on(ctx, args, "keep_copy", "no_conflict")
    if record.get("transport") == "ssh":
        state, resume_at = ("queued" if record.get("demo") else "waiting_for_terminal"), None
    else:
        state = "queued"
        resume_at = "placing" if record.get("conflict_at") == "placing" else "checking"
    _restart(folder, state, resume_at, new_copy=True)
    kind, record = _start(ctx, folder, "send")
    return {"upload": _item(folder, record), "worker": kind, "message": _transport_message(UPLOAD_MESSAGES, record)}


@_command
def cmd_check(args, ctx):
    folder, record, shown = _acting_on(ctx, args, "check", "not_checkable")
    live_ssh = record.get("transport") == "ssh" and not record.get("demo")

    def mutate(current):
        _still_allowed(folder, current, "check", "not_checkable")
        current["requests"] = {"stop_utc": None}
        current["worker"] = expected_worker("check", live_ssh)
        if live_ssh:
            current["ssh"]["attempt"] = int(current["ssh"].get("attempt") or 0) + 1

    arc_records.update_record(folder, mutate)
    arc_records.append_event(folder, record.get("state"), "Check status.")
    kind, record = _start(ctx, folder, "check")
    return {"upload": _item(folder, record), "worker": kind, "message": _transport_message(CHECK_MESSAGES, record)}


def _stop_needs_worker(record):
    """True when Globus may still run something of this upload: a stop worker signs in, settles the
    request and cancels the task (design 10.8). A conflict, or a task whose last status was final,
    runs nothing any more, so arc-stop stops it at once (7.4)."""
    if record.get("transport") != "globus" or record.get("state") == "conflict":
        return False
    globus = record.get("globus") if isinstance(record.get("globus"), dict) else {}
    task = globus.get("task") if isinstance(globus.get("task"), dict) else {}
    if globus.get("task_id"):
        return task.get("status") not in ("SUCCEEDED", "FAILED")
    return bool(globus.get("submission_id"))


def _stopped_text(record):
    """The message of an upload that arc-stop stops at once (nothing runs for it any more)."""
    demo = bool(record.get("demo"))
    state = record.get("state")
    incoming = _incoming_shown(record)
    globus = record.get("globus") if isinstance(record.get("globus"), dict) else {}
    task = globus.get("task") if isinstance(globus.get("task"), dict) else {}
    if state != "conflict" and globus.get("task_id") and task.get("status") == "SUCCEEDED":
        # The copy is complete on ARC: in _incoming, or already in its folder when a rename was asked for.
        return arc_records.message("stopped_placing" if globus.get("rename") else "stopped_complete", demo,
                                   incoming=incoming)
    ssh = record.get("ssh") if isinstance(record.get("ssh"), dict) else {}
    if record.get("transport") == "ssh" and (state == "placing" or ssh.get("may_be_placed")):
        # The remote script may have renamed the copy into its folder already: nothing is left in _incoming
        # to name (review SSH-V01).
        return arc_records.message("stopped_placing", demo)
    sent = arc_records.may_be_on_arc(record) or state == "placing" or (
        state == "conflict" and record.get("conflict_at") != "checking")
    return arc_records.message("stopped" if sent else "stopped_nothing_sent", demo, incoming=incoming)


@_command
def cmd_stop(args, ctx):
    folder, record, shown = _acting_on(ctx, args, "stop", "nothing_to_stop")
    outcome = {}

    def mutate(current):
        if _still_allowed(folder, current, "stop", "nothing_to_stop"):
            # A worker works on it (a send, a check or a stop worker): it ends what it does within a second.
            current["requests"] = {"stop_utc": utc_now()}
            outcome["how"] = "requested"
            return
        if _stop_needs_worker(current):
            # The request is written as well: if a send or check worker that is just starting takes the
            # upload before the stop worker does, it stops within a second (the stop worker then has nothing
            # to do, and a stop worker that cannot take the upload writes the request again).
            current["requests"] = {"stop_utc": utc_now()}
            current["worker"] = expected_worker("stop", False)
            outcome["how"] = "worker"
            return
        text = _stopped_text(current)
        arc_records.set_state(current, "stopped", text)
        current["error"] = None
        current["conflict_at"] = None
        current["resume"] = None
        current["requests"] = {"stop_utc": None}
        current["worker"] = None
        outcome["how"] = "now"
        outcome["text"] = text

    record = arc_records.update_record(folder, mutate)
    if outcome["how"] == "now":
        arc_records.append_event(folder, "stopped", outcome["text"])
        return {"upload": _item(folder, record), "stopped_now": True, "worker": None, "message": outcome["text"]}
    if outcome["how"] == "requested":
        arc_records.append_event(folder, record.get("state"), "Stop requested.")
        live_globus = record.get("transport") == "globus" and not record.get("demo")
        return {"upload": _item(folder, record), "stopped_now": False, "worker": None,
                "message": STOP_MESSAGES["requested_globus" if live_globus else "requested"]}
    arc_records.append_event(folder, record.get("state"), "Stop requested; a worker cancels the transfer.")
    arc_worker.spawn(ctx.home, record["upload_id"], "stop")
    return {"upload": _item(folder, arc_records.read_record(folder)), "stopped_now": False, "worker": "detached",
            "message": STOP_MESSAGES["demo" if record.get("demo") else "globus"]}


# ---------------------------------------------------------------------- arc-doctor


def _doctor_checks(settings, values, home):
    check = doctor._check
    found = arc_settings.problems(settings)
    checks = [check("settings_valid", not found,
                    "The ARC settings are valid." if not found else "; ".join(found) + ".",
                    "Correct them in ARC settings (command line: config --set key=value).")]
    mac = sys.platform == "darwin"
    checks.append(check("macos", mac, "This is macOS." if mac else "This is not macOS (%s)." % sys.platform,
                        "Uploads to ARC need macOS in this edition; use Demo to try the steps."))
    sdk = importlib.util.find_spec("globus_sdk") is not None
    checks.append(check("globus_sdk_installed", sdk,
                        "The Globus SDK (globus-sdk) is installed." if sdk else
                        "The Globus SDK (globus-sdk) is not installed.", arc_records.SETUP_ENV_FIX))
    collection = values["globus"]["mac_collection"]
    checks.append(check("mac_collection_set", bool(collection),
                        "This Mac's Globus collection ID is set: %s." % collection if collection else
                        "This Mac's Globus collection ID is not set.", refusal("mac_collection_missing").fix))
    if mac:
        checks.extend(doctor._check_globus(doctor.GLOBUS_APP_FOLDERS, doctor._running_commands))
    client = values["globus"]["client_id"]
    tutorial = client == arc_settings.TUTORIAL_CLIENT_ID
    checks.append(check("client_id_is_registered", not tutorial,
                        "Sign-in uses the Globus SDK's public example app." if tutorial else
                        "Sign-in uses the Globus app %s." % client,
                        "For regular use, register an app for the team and set arc_upload.globus.client_id."))
    username = values["ssh"]["username"]
    checks.append(check("ssh_username_set", bool(username),
                        "The VT username is set: %s." % username if username else "Your VT username is not set.",
                        refusal("username_missing").fix))
    checks.append(check("ssh_program", _executable(SSH_PROGRAM),
                        "%s is there." % SSH_PROGRAM if _executable(SSH_PROGRAM) else "%s is missing." % SSH_PROGRAM,
                        "Send with Globus; macOS normally has ssh, so ask for help."))
    terminal = os.path.isdir(TERMINAL_APP)
    checks.append(check("terminal_app", terminal, "Terminal is there." if terminal else
                        "%s is missing." % TERMINAL_APP, "Send with Globus."))
    opener = _executable(OPEN_PROGRAM)
    checks.append(check("open_program", opener, "%s is there." % OPEN_PROGRAM if opener else
                        "%s is missing." % OPEN_PROGRAM, "Send with Globus."))
    known_hosts = values["ssh"]["known_hosts"]
    problem = arc_settings.known_hosts_problem(known_hosts) if isinstance(known_hosts, str) else "not text"
    checks.append(check("known_hosts_file", problem is None,
                        "ssh uses your own ~/.ssh/known_hosts and asks you to confirm ARC's key the first time." if
                        not known_hosts else "ssh checks ARC's key against the team's file %s." % known_hosts
                        if problem is None else "The team file of ARC's SSH keys cannot be used: %s." % problem,
                        "Correct the setting, or empty it to use your own known_hosts."))
    # The demo folder: only when the setting is a usable text (an invalid one is named by settings_valid).
    root, problem = _demo_problem(values, home)
    checks.append(check("demo_folder_writable", problem is None,
                        "The demo folder %s can be used." % root if problem is None else
                        "The demo folder %s cannot be used: %s." % (root, problem) if root is not None else
                        "The demo folder cannot be used: %s (see settings_valid)." % problem,
                        refusal("demo_folder_unwritable", path="", problem="").fix))
    script = REMOTE_SCRIPT.is_file()
    checks.append(check("remote_script_present", script,
                        "The ARC side of SSH uploads (%s) is there." % REMOTE_SCRIPT.name if script else
                        "%s is missing from the data layer." % REMOTE_SCRIPT.name,
                        "Install the data layer again (macos-native/build_macos.sh)."))
    return checks


@_command
def cmd_doctor(args, ctx):
    _require_home(ctx)
    settings = read_settings(ctx)
    values = arc_settings.effective(settings)
    checks = _doctor_checks(settings, values, ctx.home)
    ok = {check["name"]: check["ok"] for check in checks}
    common = ok["settings_valid"] and ok["macos"]
    ready = {
        "globus": common and ok["globus_sdk_installed"] and ok["mac_collection_set"],
        "ssh": common and ok["ssh_username_set"] and ok["ssh_program"] and ok["terminal_app"] and
        ok["open_program"] and ok["known_hosts_file"],
        "demo": ok["settings_valid"] and ok["demo_folder_writable"],
    }
    return {"checks": checks, "ready": ready, "settings": arc_settings.flatten(values)}


# ---------------------------------------------------------------------- registration


def register(subparsers):
    command = _arc_parser(subparsers.add_parser(
        "arc-sessions", help="scan sessions and whether each can go to ARC now, newest first"))
    command.add_argument("--limit", type=int, default=SESSIONS_LIMIT, metavar="N", help="at most N sessions")
    command.add_argument("--controller-idle", action="store_true",
                         help="the controller window holds controller.lock and acquires nothing")
    command.set_defaults(func=cmd_sessions)

    command = _arc_parser(subparsers.add_parser(
        "arc-upload", help="upload a finished scan session to ARC (the work runs in a worker)"))
    command.add_argument("--session", required=True, metavar="FOLDER", help="a folder in scan_sessions")
    command.add_argument("--transport", choices=arc_settings.TRANSPORTS, help="default: arc_upload.transport")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--demo", dest="demo", action="store_true", default=None,
                      help="a folder on this Mac stands in for ARC")
    mode.add_argument("--live", dest="demo", action="store_false", default=None, help="send to ARC")
    command.add_argument("--declare-real", action="store_true",
                         help="the session was measured with the real instruments (only for a session whose files "
                              "do not say)")
    command.add_argument("--new", action="store_true", help="upload again although an earlier upload exists")
    command.add_argument("--dry-run", action="store_true", help="answer what would go where; change nothing")
    command.add_argument("--controller-idle", action="store_true",
                         help="the controller window holds controller.lock and acquires nothing")
    command.add_argument("--demo-fault", choices=DEMO_FAULTS, help="demo only: show one failure and its recovery")
    command.set_defaults(func=cmd_upload)

    command = _arc_parser(subparsers.add_parser("arc-list", help="uploads to ARC, newest first"))
    command.add_argument("--limit", type=int, default=LIST_LIMIT, metavar="N")
    command.add_argument("--session", metavar="FOLDER", help="only uploads of this session folder")
    command.set_defaults(func=cmd_list)

    command = _arc_parser(subparsers.add_parser("arc-status", help="one upload in full, with its history"))
    command.add_argument("--upload", required=True, metavar="ID")
    command.set_defaults(func=cmd_status)

    for name, function, text in (
            ("arc-check", cmd_check, "ask Globus or ARC how an upload stands; nothing is sent"),
            ("arc-retry", cmd_retry, "Retry same request: the same files, place and settings"),
            ("arc-stop", cmd_stop, "stop an upload; files already copied stay on ARC"),
            ("arc-keep-copy", cmd_keep_copy, "put an upload beside the folder that has its name on ARC")):
        command = _arc_parser(subparsers.add_parser(name, help=text))
        command.add_argument("--upload", required=True, metavar="ID")
        command.add_argument("--demo-only", action="store_true", help="refuse an upload that is not a demo (Studio)")
        command.set_defaults(func=function)

    command = _arc_parser(subparsers.add_parser(
        "arc-doctor", help="check what uploads to ARC need on this Mac, without contacting anything"))
    command.set_defaults(func=cmd_doctor)
