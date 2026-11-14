"""The core commands: status, sync, list, provenance, import-session, config, doctor, offline.

Each `cmd_*` function takes `(args, ctx)` and returns the command's output keys (PROTOCOL.md
section 6). Optional arguments are read with a default, so the functions can also be called in
this process with a bare `argparse.Namespace` holding only what matters.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import uuid
from pathlib import Path

from . import doctor, sync
from .config import MODE_DATAFED, MODE_LOCAL_DEMO, CommandError, load_settings, utc_text
from .ledger import PENDING_STATES, STATES, item_from_row
from .metadata import (
    LASER_POWER_LEVELS,
    MetadataError,
    apply_completion,
    dark_description,
    is_session_folder,
    plan_completion,
    read_session,
    sha256_file,
)

# Shown per node of a provenance chain, when the record's metadata has them.
_NODE_INFO_KEYS = (
    "capture_id", "sample_id", "point_id", "timestamp_utc", "simulated", "label", "top_class", "confidence",
    "outcome", "needs_review", "name", "file", "sha256",
)
_RELATION_LABELS = {"der": "is derived from", "comp": "is a component of", "ver": "is a new version of"}
PROVENANCE_NODE_LIMIT = 200
# A transfer of one small file takes Globus well under this; longer means it is not moving.
SLOW_UPLOAD = datetime.timedelta(minutes=10)


def _option(args, name, default=None):
    return getattr(args, name, default)


# ---------------------------------------------------------------------- status


def build_status(ctx, ask_catalog=True, online=None):
    """The object the `status` command returns.

    With `ask_catalog` false the catalog is not contacted and "online" is `online`: false when a
    sync just found it unreachable, None when it was not checked (another sync is running).
    """
    config, ledger, catalog = ctx.config, ctx.ledger, ctx.catalog
    if ask_catalog:
        who = catalog.whoami()
    else:
        who = {"online": online, "user": None, "context": config.context, "endpoint": None,
               "demo": config.mode == MODE_LOCAL_DEMO}
    counts = ledger.counts()
    total = sum(counts.values())
    errors = ledger.error_counts(config.max_attempts, config.holds_back_simulated)
    bound = ledger.catalog_key
    rebinding = bound is not None and bound != config.catalog_key
    if rebinding:
        # The rows still describe the catalog used before. The next sync makes every capture but
        # those lacking fields "captured" again for this one (Ledger.bind_catalog) and forgets the
        # dark spectra's uploads, so count as that sync will: otherwise the window would read
        # "synced", and its periodic sync would not start, while nothing is in this catalog yet.
        rebound, simulated = ledger.count_rebound()
        held = simulated if config.holds_back_simulated else 0
        pending = total - held
        errors.update(retrying=0, given_up=0)
        to_advance, darks = rebound - held, 0
    else:
        # Held back on purpose, so not waiting for anything: they are counted on their own.
        held = ledger.count_held_simulated() if config.holds_back_simulated else 0
        pending = sum(counts[state] for state in PENDING_STATES) - held
        to_advance = ledger.count_to_advance(config.max_attempts, config.holds_back_simulated)
        darks = len(ledger.darks_to_advance(config.max_attempts))
    waiting_reviews = ledger.queued_reviews(config.catalog_key)
    # What a plain sync can move on now; the window's periodic sync runs for this. Not what waits
    # for a person (missing fields, attempts used up) nor what the settings hold back.
    sync_work = to_advance + darks + sum(1 for entry in waiting_reviews if entry["attempts"] < config.max_attempts)

    problems = []
    trouble = who.get("problem")
    if who["online"] is False:
        trouble = trouble or "The catalog cannot be reached."
        if pending:
            trouble = "%s %d capture(s) wait for the next sync." % (trouble, pending)
    if trouble:
        problems.append(trouble)
    if total and rebinding:
        kept = ", except the simulated ones, which stay on this Mac" if config.holds_back_simulated else ""
        problems.append("The catalog settings changed: the next sync registers every capture in %s%s."
                        % (catalog.label, kept))
    if errors["metadata"]:
        problems.append(
            "%d capture(s) cannot be registered as their files are (their Error rows say why). Where Sample ID, "
            "Operator or Location is missing, import the session folder again with these fields (Data tab: Import "
            "scan folder; command line: import-session --folder FOLDER)." % errors["metadata"]
        )
    if errors["retrying"]:
        problems.append("%d capture(s) failed and will be tried again by the next sync." % errors["retrying"])
    if errors["given_up"]:
        problems.append(
            "%d capture(s) failed %d times and are no longer retried: fix the cause, then run sync --retry-failed."
            % (errors["given_up"], config.max_attempts)
        )
    # Uploads to the catalog used before are not this catalog's concern: the next sync forgets them.
    if config.mode == MODE_DATAFED and not rebinding:
        cutoff = utc_text(datetime.datetime.now(datetime.timezone.utc) - SLOW_UPLOAD)
        slow = ledger.count_uploads_without_news_since(cutoff)
        if slow:
            problems.append(
                "%d upload(s) have made no progress for more than 10 minutes: check that Globus Connect "
                "Personal is running on this Mac (see doctor)." % slow
            )
    failed_darks = [] if rebinding else ledger.failed_darks()
    if failed_darks:
        problems.append("%d dark spectrum file(s) could not be uploaded (the last: %s); Sync now tries again."
                        % (len(failed_darks), failed_darks[0]["last_error"]))
    stuck = [entry for entry in waiting_reviews if entry["attempts"] >= config.max_attempts]
    if stuck:
        problems.append("%d review(s) could not be sent: %s" % (len(stuck), stuck[-1]["last_error"]))
    if held:
        problems.append(
            "%d simulated capture(s) stay on this Mac and are not sent to DataFed: to send them as well, run "
            "config --set datafed.include_simulated=true." % held
        )
    problems.extend(json.loads(ledger.get_value(sync.LAST_PROBLEMS_KEY) or "[]"))

    return {
        "mode": config.mode,
        "mode_label": catalog.label,
        "demo": bool(who["demo"]),
        "online": who["online"],
        "user": who["user"],
        "context": who["context"],
        "endpoint": who["endpoint"],
        "counts": counts,
        "total": total,
        "pending": pending,
        "held_simulated": held,
        "sync_work": sync_work,
        # The rows, with their states, record ids and predictions, still belong to the catalog used
        # before the settings changed: the window must not show them as this catalog's.
        "catalog_changed": bool(total and rebinding),
        "reviews_queued": len(waiting_reviews),
        "last_sync_utc": ledger.get_value(sync.LAST_SYNC_KEY),
        "problems": problems,
    }


def cmd_status(args, ctx):
    return build_status(ctx)


def cmd_sync(args, ctx):
    result = sync.run_sync(ctx.home, ctx.config, ctx.ledger, ctx.catalog,
                           retry_failed=_option(args, "retry_failed", False))
    if result["busy"]:
        # The other sync holds the lock; answer at once instead of waiting for the catalog.
        result["status"] = build_status(ctx, ask_catalog=False)
    elif result["online"]:
        result["status"] = build_status(ctx)
        # A pass with nothing to send never contacted the catalog; the status check did.
        result["online"] = bool(result["status"]["online"])
    else:
        result["status"] = build_status(ctx, ask_catalog=False, online=False)
    return result


# ---------------------------------------------------------------------- list


def cmd_list(args, ctx):
    state = _option(args, "state")
    if state is not None and state not in STATES:
        raise CommandError("Unknown state: %s" % state, detail="States: " + ", ".join(STATES))
    limit = _option(args, "limit")
    if limit is not None and limit < 1:
        raise CommandError("--limit must be at least 1")
    states = [state] if state else None
    session = _option(args, "session")
    rows = ctx.ledger.rows(states=states, session=session, limit=limit)
    return {"items": [item_from_row(row) for row in rows], "total": ctx.ledger.count(states=states, session=session)}


# ---------------------------------------------------------------------- provenance


def _node(record, role):
    info = {key: record.metadata[key] for key in _NODE_INFO_KEYS if key in record.metadata}
    review = record.metadata.get("review")
    if isinstance(review, dict):
        info["review_status"] = review.get("status")
    return {"id": record.id, "alias": record.alias, "title": record.title, "type": record.metadata.get("type"),
            "role": role, "info": info}


def provenance(catalog, record, limit=PROVENANCE_NODE_LIMIT):
    """The provenance chain around a record: what it was derived from, what was derived from it,
    and what those derived records were themselves derived from.

    Records that merely share an ancestor (every prediction made with the same model) are left
    out; they are not this record's history. Returns {"nodes", "edges", "truncated"}.
    """
    start = catalog.get_record(record)
    nodes = {start.id: _node(start, "start")}
    edges = {}
    truncated = False

    def walk(origin, direction, role):
        nonlocal truncated
        reached, pending = [], [origin]
        while pending:
            current = pending.pop()
            for dep in current.deps:
                if dep.direction != direction:
                    continue
                source, target = (current.id, dep.id) if direction == "out" else (dep.id, current.id)
                if dep.id not in nodes:
                    if len(nodes) >= limit:
                        truncated = True
                        continue
                    other = catalog.get_record(dep.id)
                    nodes[other.id] = _node(other, role)
                    reached.append(other)
                    pending.append(other)
                edges[(source, target, dep.relation)] = {
                    "from": source, "to": target, "relation": dep.relation,
                    "label": _RELATION_LABELS.get(dep.relation, dep.relation),
                }
        return reached

    walk(start, "out", "source")
    for derived in walk(start, "in", "derived"):
        walk(derived, "out", "source")
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "truncated": truncated}


def cmd_provenance(args, ctx):
    return provenance(ctx.catalog, args.record)


# ---------------------------------------------------------------------- import-session


def _dark_file(paths, value):
    """(name inside HOME/dark_measurements, file to copy there or None) for a dark file given as
    a name or as a path. Nothing is copied here: the import may still be refused."""
    if not value:
        return None, None
    given = Path(value).expanduser()
    if given.name == value:
        if not (paths.dark_measurements / value).is_file():
            raise CommandError("There is no dark file %s in dark_measurements" % value)
        return value, None
    if not given.is_file():
        raise CommandError("There is no dark file %s" % given)
    target = paths.dark_measurements / given.name
    if not target.exists():
        description = dark_description(given)
        if description != given and (description.exists() or description.is_symlink()) and not (
                description.is_file() and os.access(description, os.R_OK)):
            # It makes the kind of the dark unknown, which pairs it with no spectrum; the dark copied
            # without it would count as real and be paired with a real spectrum.
            raise CommandError(
                "The description %s beside the dark cannot be read; nothing was imported" % description.name,
                detail="Copied without it, the dark would count as real here and be paired with real spectra. "
                "Put the description the controller wrote back beside the dark, then import again.",
            )
        return given.name, given
    if sha256_file(target)[0] != sha256_file(given)[0]:
        raise CommandError("dark_measurements already has a different file named %s" % given.name)
    return given.name, None


def _copy_dark(paths, source):
    """Copy a dark spectrum file into HOME/dark_measurements, with its description when it has one.

    The description goes first: for the moment between the two, the dark would otherwise count
    as real (metadata.dark_kind).
    """
    paths.dark_measurements.mkdir(parents=True, exist_ok=True)
    description = dark_description(source)
    for path in ([description] if description != source and description.is_file() else []) + [source]:
        target = paths.dark_measurements / path.name
        partial = target.with_name("." + target.name + ".importing")
        shutil.copyfile(path, partial)
        partial.replace(target)


def _folder_of_session(paths, session_id):
    """The folder under HOME/scan_sessions that already holds this session, if any."""
    if not paths.scan_sessions.is_dir():
        return None
    for folder in sorted(paths.scan_sessions.iterdir()):
        if is_session_folder(folder) and (folder / "session.json").is_file():
            try:
                if read_session(folder)["session_id"] == session_id:
                    return folder
            except MetadataError:
                # A folder with an unreadable session.json is simply not this session.
                continue
    return None


def _free_folder_name(paths, wanted):
    name = wanted.lstrip("_") or "imported_session"
    candidate, suffix = name, 1
    while (paths.scan_sessions / candidate).exists():
        candidate = "%s_%d" % (name, suffix)
        suffix += 1
    return candidate


def cmd_import_session(args, ctx):
    paths = ctx.paths
    source = Path(args.folder).expanduser().resolve()
    if not source.is_dir():
        raise CommandError("There is no folder %s" % source)
    simulated = _option(args, "simulated")
    dark_name, dark_to_copy = _dark_file(paths, _option(args, "dark_file"))
    supplied = {
        "sample_id": (_option(args, "sample_id") or "").strip() or None,
        "operator": (_option(args, "operator") or "").strip() or None,
        "location": (_option(args, "location") or "").strip() or None,
        "notes": _option(args, "notes"),
        "laser_power_level": _option(args, "laser_power"),
        "simulated": None if simulated is None else simulated == "yes",
        "dark_file": dark_name,
    }
    plan = plan_completion(source, supplied)
    if dark_to_copy is not None:
        _copy_dark(paths, dark_to_copy)

    copied = False
    if source.parent == paths.scan_sessions.resolve():
        if not is_session_folder(source):
            raise CommandError("Folders whose name starts with '_' are not sessions; rename %s first" % source.name)
        target = source
        apply_completion(target, plan)
    else:
        target = _folder_of_session(paths, plan["session_id"])
        if target is None:
            paths.scan_sessions.mkdir(parents=True, exist_ok=True)
            # Copied and completed under a name sync ignores, then renamed: a sync running at the
            # same time never sees a half-copied session.
            staging = paths.scan_sessions / ("_importing_" + uuid.uuid4().hex)
            target = paths.scan_sessions / _free_folder_name(paths, source.name)
            try:
                shutil.copytree(source, staging)
                apply_completion(staging, plan, folder_name=target.name, source_folder=source)
                staging.rename(target)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            copied = True
        else:
            apply_completion(target, plan_completion(target, supplied))

    report = sync.ingest_folder(paths, ctx.ledger, target)
    errors = [sync.problem(None, "metadata", "%s: %s" % (entry["file"], entry["message"])) for entry in plan["errors"]]
    return {
        "ingested": report["ingested"],
        "skipped": report["skipped"],
        "errors": errors + report["errors"],
        "folder": target.name,
        "session_id": plan["session_id"],
        "copied": copied,
    }


# ---------------------------------------------------------------------- config, doctor, offline


def _assignments(value):
    """--set may be given several times, each time with several key=value items."""
    items = []
    for group in value or []:
        items.extend([group] if isinstance(group, str) else group)
    return items


def cmd_config(args, ctx):
    # Read without the validity check of ctx.config: this command is how invalid settings get repaired.
    settings = load_settings(ctx.home)
    assignments = _assignments(_option(args, "set"))
    warnings = []
    for text in assignments:
        key, separator, value = text.partition("=")
        key = key.strip()
        if not separator or not key:
            raise CommandError("--set needs key=value, for example mode=datafed", detail="Got: %s" % text)
        if not settings.is_known(key):
            warnings.append("%s is not a setting this version knows; it was stored as given." % key)
        settings.set_from_text(key, value)
    if assignments:
        settings.require_valid()
        settings.save()
        ctx.reload()
    result = {"config": settings.as_dict(), "path": str(settings.path)}
    problems = settings.problems()
    if problems:
        result["problems"] = problems
    if warnings:
        result["warnings"] = warnings
    return result


def cmd_doctor(args, ctx):
    settings = load_settings(ctx.home)
    online = bool(_option(args, "online", False))
    # ctx.catalog is touched only for a real online check: building it needs valid settings.
    wanted = online and not settings.problems() and settings.mode != MODE_LOCAL_DEMO
    report = doctor.run_checks(settings, online=online, catalog=ctx.catalog if wanted else None)
    report["mode"] = settings.mode
    # About DataFed only: the local demo needs none of this.
    report["datafed_ready"] = all(check["ok"] for check in report["checks"])
    report["include_simulated"] = settings.include_simulated
    return report


def cmd_offline(args, ctx):
    if ctx.config.mode != MODE_LOCAL_DEMO:
        raise CommandError(
            "offline is a switch of the local demo catalog only",
            detail="In datafed mode the connection is the real one and cannot be simulated away.",
        )
    catalog = ctx.catalog
    if _option(args, "on", False):
        catalog.set_offline(True)
    elif _option(args, "off", False):
        catalog.set_offline(False)
    return {"offline": catalog.is_offline()}


def register(subparsers):
    command = subparsers.add_parser("status", help="report the connection and how many captures are in each state")
    command.set_defaults(func=cmd_status)

    command = subparsers.add_parser(
        "sync", help="find new points, register and upload them, pick up predictions and reviews")
    command.add_argument("--retry-failed", action="store_true",
                         help="also retry captures that failed too often to be retried automatically")
    command.set_defaults(func=cmd_sync)

    command = subparsers.add_parser("list", help="captures with their state, newest first")
    command.add_argument("--session", metavar="ID", help="only this session (session id or folder name)")
    command.add_argument("--state", choices=STATES, help="only captures in this state")
    command.add_argument("--limit", type=int, metavar="N", help="at most N captures")
    command.set_defaults(func=cmd_list)

    command = subparsers.add_parser("provenance", help="the provenance chain around a record")
    command.add_argument("--record", required=True, metavar="ID", help="record id or alias")
    command.set_defaults(func=cmd_provenance)

    command = subparsers.add_parser(
        "import-session", help="add an existing scan folder, supplying the fields it was recorded without")
    command.add_argument("--folder", required=True, metavar="DIR")
    command.add_argument("--sample-id", metavar="S")
    command.add_argument("--operator", metavar="O")
    command.add_argument("--location", metavar="L")
    command.add_argument("--notes", metavar="TEXT")
    command.add_argument("--laser-power", choices=LASER_POWER_LEVELS, help="left unknown when not given")
    command.add_argument("--simulated", choices=("yes", "no"), help="left unknown when not given")
    command.add_argument("--dark-file", metavar="FILE",
                         help="dark spectrum of the session: a file name in dark_measurements, or a path to copy there")
    command.set_defaults(func=cmd_import_session)

    command = subparsers.add_parser("config", help="show or change the settings")
    command.add_argument("--set", action="append", nargs="+", metavar="KEY=VALUE",
                         help="dotted key, for example mode=datafed or datafed.context=p/my.project")
    command.set_defaults(func=cmd_config)

    command = subparsers.add_parser("doctor", help="check what DataFed needs on this Mac, without logging in")
    command.add_argument("--online", action="store_true", help="in datafed mode, also ask DataFed who you are")
    command.set_defaults(func=cmd_doctor)

    command = subparsers.add_parser("offline", help="local demo only: simulate losing the connection")
    switch = command.add_mutually_exclusive_group()
    switch.add_argument("--on", action="store_true")
    switch.add_argument("--off", action="store_true")
    command.set_defaults(func=cmd_offline)
