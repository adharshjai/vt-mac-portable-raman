"""Uploads to ARC through Globus (PROTOCOL.md 5.6, design sections 10 and 12).

The worker (arc_worker) calls send(ctx), check(ctx) or stop(ctx). Each makes a client, the live
one (LiveGlobus, through the Globus SDK) or the demo's stand-in (arc_demo.DemoGlobus), and runs
GlobusFlow with it. GlobusFlow depends only on GlobusCallError and on this client interface
(design 10.9), so the demo and the tests drive the same steps as a live upload:

    login(gare=None, force_login=False)   one sign-in (a browser page for a live upload)
    identity() -> str or None             who signed in
    identity_id() -> str or None          that identity's Globus ID (compared with a collection's owner)
    logout()                              revoke the tokens and close everything (errors ignored)
    get_endpoint(collection) -> dict
    ls(collection, path, offset, limit) -> [entry]     one page of a folder, hidden entries included
    stat(collection, path) -> entry       mkdir(collection, path)       rename(collection, old, new)
    submission_id() -> str                submit(body) -> {"code", "task_id", ...}
    get_task(task_id) -> dict             successful_transfers(task_id, marker=None) -> {"DATA", "next_marker"}
    task_list(filter, limit) -> [task]    cancel(task_id) -> {"code", ...}    task_events(task_id, limit) -> [event]

Every failure of a client is a GlobusCallError(http_status, code, message, gare): http_status 0
and code "network" when Globus could not be reached; gare set when Globus asks for another
sign-in (a consent).

What an upload does (design 10.2 to 10.8): sign in (a thread; the worker gives up after 10
minutes); check that Globus sees exactly this Mac's upload copy and make the folders on ARC;
save the request number and the exact request body (fsync, and on macOS a full flush of the drive)
before the request is posted, and post that same body again when an answer is lost (Globus answers
Duplicate); watch the task; compare Globus's per-file checksums with this Mac's; put the copy in
place with one rename that never replaces anything, under the first free name of <folder>,
<folder>_copy2 ... _copy99 for a new copy. Tokens stay in this process's memory and every token
the process was given is revoked at every end (short timeouts, no retries).

Stops (decision I8): only a person's stop (requests.stop_utc, set by arc-stop and the Stop button)
cancels a Globus task. A signal that ends the process (SIGTERM at a logout or shutdown, SIGHUP or
SIGINT sent to a detached worker) ends the flow at once: nothing is cancelled and the state stays,
so the listing shows "not being watched" or "interrupted". A check worker that started with nothing to
cancel stops waiting for its sign-in when a person stops it, and keeps the stored state (decision I3);
one that started while Globus could still run the transfer is stopped as Stop upload stops (R3-01).

The Globus SDK (globus-sdk 4.10.0) is imported only inside LiveGlobus, never at module level, so
the arc-* commands, the demo and every listing work without it.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import importlib
import json
import logging
import re
import threading
import time
import types
import zlib
from pathlib import Path

from . import arc_paths, arc_payload, arc_records, arc_session, arc_settings, arc_worker
from .arc_records import MESSAGES
from .arc_worker import Outcome, StopRequested
from .config import utc_now

log = logging.getLogger(__name__)

APP_NAME = "Portable Raman ARC upload"
# The loopback address of the sign-in's local listener (never the host name: design 10.2).
LOOPBACK = ("127.0.0.1", 0)
REQUEST_FILE = "globus_request.json"
LABEL_PREFIX = "Raman upload"
LABEL_LIMIT = 128
STEPS = ("checking", "submitting", "transferring", "verifying", "placing")
DEMO_FAULTS = ("lose_submit_reply", "fail_task_once")
ACTIVE_STATUSES = ("ACTIVE", "INACTIVE")
FINAL_STATUSES = arc_records.GLOBUS_FINAL_STATUSES
# The task fields a record keeps (design 7.2); never the whole task document.
TASK_FIELDS = ("status", "nice_status", "nice_status_short_description", "files", "files_transferred",
               "files_skipped", "bytes_transferred", "faults", "fatal_error", "deadline", "completion_time",
               "is_paused")
QUIET_NICE_STATUSES = (None, "", "OK", "Queued")
CHECKSUM_ALGORITHMS = ("md5", "sha1", "sha256", "sha512", "adler32")
# Client error codes for a sign-in that did not work (no browser could be opened; anything else).
SIGNIN_NO_BROWSER = "SignIn.NoBrowser"
SIGNIN_FAILED = "SignIn.Failed"
NETWORK = "network"
# Results that mean nothing was asked of Globus: no sign-in, or no consent (review GLO-02).
SIGN_IN_REASONS = ("signin_timeout", "no_browser", "signin_failed", "consent")
# Post outcomes that say Globus made no task of the request.
REFUSED_OUTCOMES = arc_records.GLOBUS_REFUSED_OUTCOMES
# HTTP answers that come before Globus looks at a request: asked again like a lost answer.
BUSY_STATUSES = (408, 429)
# The most names a new copy tries: <folder>, then <folder>_copy2 ... <folder>_copy99 (decision I4).
LAST_COPY = 99

# Times (seconds). Tests patch them; the demo scales its pauses with RAMAN_ARC_DEMO_PACE.
SIGN_IN_LIMIT_SECONDS = 600
SIGN_IN_POLL_SECONDS = 0.2
# A consent sign-in is asked for only this long after the first sign-in ended (review GLO-01).
CONSENT_WINDOW_SECONDS = 600
WATCH_LIMIT_SECONDS = 90 * 60
RESEND_DELAYS = (10, 30)
POLL_SCHEDULE = ((60, 5), (600, 15), (None, 60))
DEMO_POLL_SECONDS = 0.5
GCP_QUIET_SECONDS = 120
UNREACHABLE_SECONDS = 600
STOP_SETTLE_SECONDS = 120
STOP_POLL_SECONDS = 5
# Revoking a token at the end: one try, at most this long (review GLO-V02).
REVOKE_TIMEOUT_SECONDS = 5
PAGE = 1000
MAX_ENTRIES = 100000
_clock = time.monotonic
_sleep = time.sleep

TEXTS = {
    "conflict_checking": "ARC already has a folder named {folder} in {base}. Nothing was sent.",
    "conflict_placing": "Your copy is complete and checked in {incoming} on ARC. Keep it as a new copy, or move the "
                        "other folder on ARC and press Retry same request.",
    # A text with a "<key>_demo" wording names the demo folder for a demo upload, not ARC (message(); COR-05).
    "conflict_checking_demo": "The demo folder already has a folder named {folder} in {base}. Nothing was sent.",
    "conflict_placing_demo": "Your copy is complete and checked in {incoming}. Keep it as a new copy, or move the "
                             "other folder and press Retry same request.",
    "copying": "Copying with Globus: {done} of {files} files ({bytes_done} of {bytes}).",
    "copying_live": " Keep Globus Connect Personal running.",
    "nice": "Globus reports: {description}. It keeps trying until {deadline}.",
    "gcp_waiting": "Globus is waiting for Globus Connect Personal on this Mac. Open it from the menu bar and check "
                   "that it says Connected; keep this Mac awake.",
    "inactive": "Globus has paused this copy: {description}. Open app.globus.org > Activity to let it continue.",
    "admin_paused": "An administrator of a collection has paused this copy.",
    "new_sign_in": "Globus asks for a new sign-in. Press Check status.",
    "unreachable": "This Mac could not reach Globus for a while and stopped watching. Globus keeps copying. Press "
                   "Check status to look. Keep this Mac awake until the copy is done.",
    "consent_asked": "Globus asks you to allow access to ARC's collection. A web page has opened for it; allow "
                     "access there.",
    "saved": "Saved request number {short} before sending it to Globus.",
    "accepted": "Globus accepted the request: task {short}.",
    "unknown_post": "It is not known whether Globus received the request; asking again with the same request number.",
    "duplicate": "Globus answered: already received. One copy.",
    "uncertain": "It is not known whether Globus received the request. Retry same request asks again with the same "
                 "request number; Globus cannot make a second copy from it.",
    "request_changed": "The saved request no longer matches its files; it is not sent again. Start a new upload.",
    "new_attempt": "The earlier transfer ended ({status}); attempt {attempt} takes a new request number.",
    "new_attempt_unseen": "Globus has no transfer of the earlier request; attempt {attempt} takes a new request "
                          "number.",
    "watch_earlier": "The earlier transfer is still running on Globus; it is watched instead of starting another.",
    "stopped_after_cancel": "Stopped. Files copied before the stop are in {incoming} on ARC; nothing was deleted.",
    "stopped_after_cancel_demo": "Stopped. Files copied before the stop are in {incoming}; nothing was deleted.",
    # Shared with arc-stop, which stops an upload whose transfer has ended without a worker.
    "stopped_complete": MESSAGES["stopped_complete"],
    "stopped_placing": MESSAGES["stopped_placing"],
    "stop_pending": "Globus has not ended the copy yet. Press Stop upload again in a minute.",
    # A cancel that never reached Globus (review GLO-07), and a stop during a request whose answers were lost.
    "stop_not_delivered": "The stop did not reach Globus, so the copy may still be running. Press Stop upload again.",
    "uncertain_stop": "It is not known whether Globus received the request, so the stop could not cancel anything. "
                      "Press Stop upload again: it looks for the transfer and cancels it.",
    # After a sign-in or a consent that did not happen (review GLO-02): what that means for this upload.
    "check_not_signed_in": arc_records.CHECK_NOT_LOOKED,
    "stop_not_signed_in": "The upload was not stopped.",
    "not_watching": "This Mac is not watching the transfer; Globus may still be copying. Press Check status to look.",
    "earlier_files": "Nothing more was sent; files copied earlier stay in {incoming} on ARC.",
    "earlier_complete": "Nothing more was sent; the copy Globus finished is complete in {incoming} on ARC.",
    "earlier_files_demo": "Nothing more was sent; files copied earlier stay in {incoming}.",
    "earlier_complete_demo": "Nothing more was sent; the copy Globus finished is complete in {incoming}.",
    # Events.
    "request_spent": "The saved request is not sent again: Globus refused it, or its deadline has passed.",
    "found_finished": "Globus had finished the transfer of the earlier request; it is checked instead of copied "
                      "again.",
    "history_gone": "Globus no longer keeps the list of the files it copied, so the copy is checked by listing it on "
                    "ARC.",
    "history_gone_demo": "Globus no longer keeps the list of the files it copied, so the copy is checked by listing it "
                         "in the demo folder.",
    "folders_ready": "The folders on ARC are ready: {path}.",
    "folders_ready_demo": "The folders in the demo folder are ready: {path}.",
    "no_transfer": "Globus has no transfer for this upload. Press Retry same request to send it.",
    "still_on_arc": "Still on ARC: {files} files with the right sizes.",
    "still_in_demo": "Demo: still in the demo folder: {files} files with the right sizes.",
    "differs_on_arc": "The folder on ARC differs from this upload: {count} differ, for example {files}.",
    "differs_on_arc_demo": "The folder in the demo folder differs from this upload: {count} differ, for example "
                           "{files}.",
    "gone_from_arc": "The folder {path} is no longer on ARC.",
    "gone_from_arc_demo": "The folder {path} is no longer in the demo folder.",
    # Check status of an upload on ARC whose folder Globus listed only in part: it could not compare it.
    "listed_in_part": "Globus listed only part of the folder on ARC.",
    "listed_in_part_demo": "Globus listed only part of the folder in the demo folder.",
    # ... and of one whose folder Globus may not list for this identity (403; review R2-03): a permission note.
    "listing_forbidden": "Your Globus identity may not list {path} on ARC.",
    "listing_forbidden_demo": "Globus may not list {path} in the demo folder.",
    # A check that a person stopped (decision I3), as an SSH check says it in the history.
    "check_stopped": "The check was not finished: it was stopped.",
    "verified": "On ARC: {files} files in {path}. {sentence}",
    "verified_demo": "Demo: {files} files in the demo folder on this Mac, every one checked. Nothing left this Mac.",
    "sentence_checksum": "Globus checked every file, and its {algorithm} checksums match this Mac's copy.",
    "sentence_verify": "Globus compared every file with this Mac's copy after copying it.",
    "incoming_gone": "The upload's folder in _incoming is no longer on ARC.",
    "incoming_gone_demo": "The upload's folder in _incoming is no longer in the demo folder.",
    "checked": "Checked the copy on ARC: {files} files, as this Mac's list says.",
    "checked_demo": "Checked the copy in the demo folder: {files} files, as this Mac's list says.",
}


class GlobusCallError(Exception):
    """The one error type of a Globus client, live or demo (design 10.9)."""

    def __init__(self, http_status, code, message, gare=None):
        super().__init__("%s %s: %s" % (http_status, code, message))
        self.http_status = int(http_status or 0)
        self.code = str(code or "")
        self.message = str(message or "")
        self.gare = gare


class _Finished(Exception):
    """Ends the flow with this outcome (raised from deep inside a step)."""

    def __init__(self, outcome):
        super().__init__(outcome.state)
        self.outcome = outcome


class _Truncated(Exception):
    """A listing that cannot be complete: DirectorySizeLimit, too many entries, or a name seen twice."""


# ---------------------------------------------------------------------- error kinds


def _code_part(error, name):
    return name in error.code.split(".") or error.code.endswith(name)


def _not_found(error):
    return error.http_status == 404 or _code_part(error, "NotFound")


def _exists(error):
    return _code_part(error, "Exists")


def _forbidden(error):
    return error.http_status == 403 or "PermissionDenied" in error.code


def _gcp_off(error):
    return "GCDisconnected" in error.code or "GCPaused" in error.code


def _token_rejected(error):
    return error.http_status == 401


def _no_answer(error):
    """No usable answer: the network, a timeout, a server error (5xx), or Globus too busy to look at the
    request (408, 429: the SDK's own retries are off for the transfer request, review GLO-03)."""
    return error.http_status == 0 or error.http_status >= 500 or error.http_status in BUSY_STATUSES


def _size_limit(error):
    return "DirectorySizeLimit" in error.code or "SizeLimit" in error.code


# ---------------------------------------------------------------------- what Globus may hold


def _sub(record):
    """The record's "globus" sub-record, read only ({} when missing)."""
    return record.get("globus") if isinstance(record.get("globus"), dict) else {}


def _task_status(record):
    task = _sub(record).get("task")
    return task.get("status") if isinstance(task, dict) else None


def _refused_for_sure(globus):
    """True when every POST of the current request got a definite refusal: Globus made no task of it."""
    return arc_records.globus_refused_for_sure(globus)


def may_run(record):
    """True when Globus may still run a transfer of this upload: its task is not known to have ended, or
    its request number may have reached Globus without a task id being known (arc_records.globus_may_run,
    which the listing gives as "may_run")."""
    return arc_records.globus_may_run(record)


# ---------------------------------------------------------------------- the request


def transfer_label(upload_id, attempt, folder):
    """The task's label: the upload id and the attempt, then the session folder cut to fit 128
    characters of [A-Za-z0-9._ -] (Transfer docs), so that a task is found by its exact label."""
    prefix = "%s %s a%d " % (LABEL_PREFIX, upload_id, int(attempt))
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", str(folder))[:max(0, LABEL_LIMIT - len(prefix))]
    return (prefix + name).strip()


def deadline_text(saved_utc, hours):
    """The task's deadline: `hours` after the request was saved, as Globus writes times."""
    then = datetime.datetime.strptime(saved_utc, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
    return (then + datetime.timedelta(hours=int(hours))).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def source_path(payload_dir):
    """The upload copy as this Mac's Globus Connect Personal shows it: the absolute, resolved path."""
    return str(Path(payload_dir).resolve()) + "/"


def build_request(record, submission_id, payload_dir):
    """The exact body of the transfer request (design 10.4): a plain dict, built only from the record,
    so that the saved body can be built again and compared before it is sent again."""
    globus = record["globus"]
    used = record["settings_used"]
    return {
        "DATA_TYPE": "transfer",
        "submission_id": submission_id,
        "source_endpoint": used["mac_collection"],
        "destination_endpoint": used["arc_collection"],
        "label": transfer_label(record["upload_id"], globus.get("attempt") or 1, record["session"]["folder"]),
        "verify_checksum": True,
        "encrypt_data": True,
        "preserve_timestamp": True,
        "delete_destination_extra": False,
        "fail_on_quota_errors": True,
        "skip_source_errors": False,
        "deadline": deadline_text(globus["request_saved_utc"], used.get("deadline_hours") or 24),
        "notify_on_succeeded": False,
        "notify_on_failed": True,
        "DATA": [{"DATA_TYPE": "transfer_item",
                  "source_path": source_path(payload_dir),
                  "destination_path": record["destination"]["globus_incoming_path"].rstrip("/") + "/",
                  "recursive": True}],
    }


def save_request(record_dir, body):
    """globus_request.json: replaced atomically, the file and its folder made durable (on macOS a full
    flush of the drive, decision I5) before the POST."""
    arc_records.write_private_file(Path(record_dir) / REQUEST_FILE,
                                   (json.dumps(body, indent=2) + "\n").encode("utf-8"), durable=True)


def load_request(record_dir):
    try:
        value = json.loads((Path(record_dir) / REQUEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def deadline_passed(body, now=None):
    """True when the saved request's deadline is over: Globus would not copy it any more (review GLO-05)."""
    try:
        deadline = datetime.datetime.strptime(str((body or {}).get("deadline")), "%Y-%m-%dT%H:%M:%S+00:00")
    except ValueError:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return deadline.replace(tzinfo=datetime.timezone.utc) <= now


# ---------------------------------------------------------------------- the record's values (design 20, S-13)


class RecordValueError(ValueError):
    """A value of the record that arc-upload would not have written: nothing is signed in or sent."""


def _copy_name(folder, name):
    """The session folder's own name, or <folder>_copy2 ... <folder>_copy99."""
    return name == folder or bool(re.fullmatch(re.escape(folder) + r"_copy([2-9]|[1-9][0-9])", str(name)))


def check_record(ctx, record):
    """The record's values that reach Globus or this Mac's files, checked again as arc-upload made them
    (design section 20: S-13, S-V01; review SEC-07): the session folder name, the collection IDs and the
    collection root, the ARC folder and its area, every Globus path recomputed from them, and the upload
    copy inside the record folder. Raises RecordValueError."""
    folder = ctx.session_folder
    if not (isinstance(folder, str) and arc_session.SESSION_NAME.fullmatch(folder)) or folder.lower() == "simulated":
        raise RecordValueError("not a session folder name")
    if not (isinstance(ctx.upload_id, str) and arc_records.UPLOAD_ID.fullmatch(ctx.upload_id)) or \
            ctx.upload_id != Path(ctx.record_dir).name:
        raise RecordValueError("not this record folder's upload id")
    used = ctx.settings_used
    for key in ("mac_collection", "arc_collection", "client_id"):
        if not (isinstance(used.get(key), str) and arc_settings.UUID.fullmatch(used[key])):
            raise RecordValueError("settings_used.%s is not a Globus ID" % key)
    if used["mac_collection"].lower() == used["arc_collection"].lower():
        raise RecordValueError("this Mac's collection is ARC's")
    root = used.get("collection_root")
    if not (root == "/" or (isinstance(root, str) and arc_settings.COLLECTION_ROOT.fullmatch(root))):
        raise RecordValueError("not a collection root")
    hours = used.get("deadline_hours")
    if hours is not None and not (isinstance(hours, int) and not isinstance(hours, bool) and 1 <= hours <= 168):
        raise RecordValueError("not a deadline")
    destination = record.get("destination") if isinstance(record.get("destination"), dict) else {}
    arc_folder = destination.get("arc_folder")
    if not isinstance(arc_folder, str) or arc_paths.check_cluster_folder(arc_folder, root) or \
            arc_folder != used.get("folder"):
        raise RecordValueError("not the ARC folder of this upload")
    area = destination.get("area")
    base = arc_folder if area == arc_session.AREA_REAL else arc_folder + "/simulated"
    if area not in (arc_session.AREA_REAL, arc_session.AREA_SIMULATED) or destination.get("base") != base:
        raise RecordValueError("the ARC folder and the area do not agree")
    name = destination.get("placed_as") or folder
    if destination.get("name") != folder or not _copy_name(folder, name):
        raise RecordValueError("not a name this upload may have on ARC")
    incoming = base + "/_incoming/" + folder + "." + ctx.upload_id
    expected = {"incoming_path": incoming, "final_path": base + "/" + name,
                "globus_base": arc_paths.collection_path(base, root),
                "globus_incoming_path": arc_paths.collection_path(incoming, root),
                "globus_final_path": arc_paths.collection_path(base + "/" + name, root)}
    for key, value in expected.items():
        if destination.get(key) != value:
            raise RecordValueError("destination.%s is not this upload's" % key)
    payload = Path(ctx.payload_dir)
    if payload.exists() or payload.is_symlink():
        inside = Path(ctx.record_dir).resolve() / arc_records.PAYLOAD_FOLDER / folder
        if payload.is_symlink() or payload.resolve() != inside:
            raise RecordValueError("the upload copy is not in the record folder")


# ---------------------------------------------------------------------- small helpers


def _empty_globus():
    return {"identity": None, "attempt": 1, "submission_id": None, "request_saved_utc": None, "label": None,
            "posts": [], "task_id": None, "task": None, "earlier_attempts": []}


def _globus(record):
    """The record's "globus" sub-record (made when missing)."""
    if not isinstance(record.get("globus"), dict):
        record["globus"] = _empty_globus()
    return record["globus"]


def _size_text(count):
    count = int(count or 0)
    if count < 1000000:
        return "%d kB" % round(count / 1000)
    return "%.1f MB" % (count / 1e6)


def _local_time(text):
    try:
        moment = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return str(text)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment.tzinfo else str(text)


# The parts of a message that may hold a sign-in secret (review GLO-06): a web address's query, a code,
# state or token value as long as a real one (16 characters or more), a bearer token, and a string
# shaped like an access token (32 or more letters and digits, both cases and digits mixed). Only these
# parts are removed; the rest keeps Globus's or the SDK's own words.
_SECRET_PARTS = (
    re.compile(r"\?[^\s\"'<>]*=[^\s\"'<>]*"),
    re.compile(r"(?i)\b(?:code|state|access_token|refresh_token|id_token|token|client_secret|password)[\"']?"
               r"\s*[=:]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}[\"']?"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[a-z])(?=[A-Za-z0-9]*[A-Z])[A-Za-z0-9]{32,}\b"),
)
REMOVED = "[removed]"


def _clean(text):
    """A message as a record may keep it: one line, short, its secret-shaped parts removed."""
    text = " ".join(str(text or "").split())
    for pattern in _SECRET_PARTS:
        text = pattern.sub(REMOVED, text)
    return text[:300].rstrip(".") or "no reason was given"


def _without_note(text):
    """A record's message without "Demo: " and without the note that an earlier sign-in that did not
    happen put in front of it (not_signed_in), so that notes never pile up."""
    text = str(text or "")
    for note in (TEXTS["check_not_signed_in"], TEXTS["stop_not_signed_in"]):
        if note in text:
            text = text.rsplit(note, 1)[1]
    text = text.strip()
    if text.startswith("Demo: "):
        text = text[len("Demo: "):]
        text = text[:1].upper() + text[1:]
    return text


def _short(identifier):
    return str(identifier or "")[:8]


def _identity_id(client):
    """The Globus ID of the identity that signed in, when the client knows it (else None)."""
    found = getattr(client, "identity_id", None)
    try:
        value = found() if callable(found) else None
    except GlobusCallError:
        return None
    return value if isinstance(value, str) and value else None


def _is_hex(text):
    return isinstance(text, str) and bool(re.fullmatch(r"[0-9a-fA-F]+", text))


def _sort_key(path):
    return arc_payload.sort_key(path)


def _differences(expected, found):
    """Paths whose presence, type or size differ, a link included, in bytewise order.

    `expected` {path: ("file", bytes) or ("dir", None)}; `found` {path: Globus entry}."""
    names = []
    for path in sorted(set(expected) | set(found), key=_sort_key):
        want, have = expected.get(path), found.get(path)
        if want is None or have is None or have.get("link_target") or have.get("type") != want[0] or \
                (want[0] == "file" and have.get("size") != want[1]):
            names.append(path)
    return names


def _expected_tree(files):
    """{path: ("file", bytes) | ("dir", None)} of the upload copy, from {path: bytes} of its files."""
    tree = {}
    for path, size in files.items():
        tree[path] = ("file", size)
        parts = path.split("/")
        for depth in range(1, len(parts)):
            tree["/".join(parts[:depth])] = ("dir", None)
    return tree


def _checksum(algorithm, path, entry):
    """This Mac's checksum of one file of the upload copy, lowercase hex (adler32 as 8 hex digits)."""
    if algorithm == "md5":
        return entry["md5"]
    if algorithm == "sha256":
        return entry["sha256"]
    if algorithm == "adler32":
        value = 1
        with open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                value = zlib.adler32(block, value)
        return "%08x" % value
    digest = hashlib.new(algorithm)
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_checksum(algorithm, mine, theirs):
    if algorithm == "adler32":
        return int(mine, 16) == int(theirs, 16)
    return mine == theirs.lower()


# ---------------------------------------------------------------------- the flow


class GlobusFlow:
    """One worker's Globus steps for one upload, against one client (live or demo)."""

    def __init__(self, ctx, client, clock=None):
        self.ctx = ctx
        self.client = client
        self.clock = clock or _clock
        self.sleep = _sleep
        self.started = self.clock()
        self.signed_in_at = None
        self.identity = None
        self.identity_id = None
        self.fresh_request = True
        self.stop_tried = False
        self.consents = 0
        self.phase = None
        self.watching = False
        self.moved = False
        self.ended = False
        self._last_write = None
        self._last_status = None
        self._gcp_checked = None
        self._gcp_off = False
        self.scale = (arc_worker.demo_pace() / arc_worker.DEFAULT_DEMO_PACE) if ctx.demo else 1.0
        # A check worker: the upload as the check found it (state, message, error), which a person's stop keeps.
        self.stored = None
        # A check worker: whether Globus could still run the transfer when the check started (may_run). The window
        # then asked "Stop this upload? ... the transfer ... is cancelled", so the stop is Stop upload's (review
        # R3-01); otherwise it keeps the stored state (keeps_the_state()).
        self.check_may_run = False

    # ------------------------------------------------------------------ the record

    def read(self):
        return self.ctx.read()

    def update_globus(self, **values):
        return self.ctx.update(lambda record: _globus(record).update(values))

    def set_state(self, state, message=None, event=None):
        before = self.read().get("state")
        record = self.ctx.set_state(state, message, event)
        if state != before:
            self.moved = True
        return record

    def text(self, text):
        return self.ctx.text(text)

    def message(self, key, **values):
        """A text of MESSAGES or TEXTS as this upload shows it: a demo upload gets its "<key>_demo" wording
        when there is one (the copy is then in the demo folder, not on ARC), and "Demo: " in front."""
        if key in MESSAGES:
            return arc_records.message(key, self.ctx.demo, **values)
        if self.ctx.demo and key + "_demo" in TEXTS:
            key += "_demo"
        return self.text(TEXTS[key].format(**values))

    def shown(self, globus_path):
        """A path of ARC's collection as the person knows it: the cluster path, or the demo folder."""
        root = self.ctx.settings_used.get("collection_root") or "/"
        cluster = globus_path if root == "/" else (root.rstrip("/") + globus_path if globus_path != "/" else root)
        if self.ctx.demo and self.ctx.demo_root is not None:
            return arc_records.local_path(self.ctx.demo_root, cluster)
        return cluster

    def incoming_shown(self, record=None):
        destination = (record or self.read())["destination"]
        incoming = destination.get("incoming_path")
        if self.ctx.demo and destination.get("demo_root") and incoming:
            return arc_records.local_path(destination["demo_root"], incoming)
        return incoming

    def keep_state(self, text, reason=None):
        """A check or stop worker that cannot go on: the state stays, the record says why. A verified upload
        gets a "checks" entry instead, so its message keeps saying where it is: a check that could not look,
        with the fix of `reason` (review REG-01)."""
        state = self.read().get("state")
        if state == "verified":
            return Outcome("verified", None, check=arc_records.check_note(self.text(text), reason))
        return Outcome(state, self.text(text))

    def failure(self, reason, **values):
        """failed / reason, or, for a check or stop worker that has not moved the upload, the state as it
        was with the reason's message. A sign-in or a consent that did not happen: not_signed_in()."""
        if reason in SIGN_IN_REASONS:
            return self.not_signed_in(reason, **values)
        if self.ctx.action == "send" or self.moved:
            return self.ctx.failed(reason, **values)
        return self.keep_state(self.ctx.error(reason, **values)["message"], reason)

    def not_signed_in(self, reason, **values):
        """A sign-in or a consent that did not happen, so nothing was asked of Globus (review GLO-02). The
        upload keeps what Globus may hold: Check status and Stop upload stay offered while Globus may run
        its transfer, and the record never says "Nothing was sent" when something may have been.

        - A check or stop worker that has not moved the upload: the state stays; the message says why
          nothing happened, then what it said before (a verified upload gets a "checks" entry).
        - A transfer that may still run: the state "transferring" (shown as not being watched).
        - A request Globus may have received, without a task id: "uncertain".
        - Otherwise failed / reason; when files of this upload may be on ARC, the message says where
          instead of "Nothing was sent"."""
        error = self.ctx.error(reason, **values)
        why = error["message"].replace(" Nothing was sent.", "")
        record = self.read()
        state = record.get("state")
        if self.ctx.action != "send" and not self.moved:
            # A check worker that a person asked to stop says that the stop did not happen.
            stopping = self.ctx.action == "stop" or (self.ctx.stop_requested() and self.person_stop())
            after = TEXTS["stop_not_signed_in" if stopping else "check_not_signed_in"]
            if state == "verified":
                # The check could not look: the copy on ARC is not in question (review REG-01).
                return Outcome("verified", None, check=arc_records.check_note(self.text(why + " " + after), reason))
            # A working state is shown with the listing's own message; the others keep theirs after the note.
            before = "" if state in arc_records.WORKING_STATES else _without_note(record.get("message"))
            return Outcome(state, self.text(" ".join(part for part in (why, after, before) if part)))
        globus = _sub(record)
        if may_run(record) and globus.get("task_id"):
            target = state if state in ("transferring", "verifying", "placing") else "transferring"
            return Outcome(target, self.text(why + " " + TEXTS["not_watching"]))
        if may_run(record):
            if state != "submitting":
                # Through "submitting" (TRANSITIONS), without a message or an event: nothing was posted.
                self.ctx.set_state("submitting")
            return Outcome("uncertain", self.text(why + " " + TEXTS["uncertain"]))
        if arc_records.may_be_on_arc(record):
            key = "earlier_complete" if _task_status(record) == "SUCCEEDED" else "earlier_files"
            if self.ctx.demo:
                key += "_demo"
            error["message"] = why + " " + TEXTS[key].format(incoming=self.incoming_shown(record))
        return Outcome("failed", self.text(error["message"]), error)

    def unexpected(self, error):
        """An answer this program does not expect at this step (the network, a server error)."""
        if error.http_status == 0:
            what = "no connection"
        else:
            what = "%s: %s" % (error.code or error.http_status, _clean(error.message))
        log.info("an unexpected Globus answer: %s %s", error.http_status, error.code)
        return _Finished(self.failure("globus_error", what=what))

    # ------------------------------------------------------------------ stops (decisions I3 and I8)

    def person_stop(self):
        """True when a person asked to stop this upload: requests.stop_utc (arc-stop, the Stop button),
        which the worker's poller turns into a stop request without a signal name, or Ctrl-C in a
        Terminal worker. SIGTERM (a logout, a restart, a shutdown) and a SIGHUP or SIGINT sent to a
        detached worker only end the process (decision I8, review COR-01)."""
        ctx = self.ctx
        name = getattr(ctx, "signal_name", None)
        if name is None or (name == "SIGINT" and ctx.terminal):
            return True
        requests = self.read().get("requests")
        return isinstance(requests, dict) and bool(requests.get("stop_utc"))

    def stop_wanted(self):
        """True when a person asked to stop. A signal that only ends the process ends the flow here, at
        once, keeping the stored state and cancelling nothing (decision I8)."""
        if not self.ctx.stop_requested():
            return False
        if self.person_stop():
            return True
        raise _Finished(self.ended_by_signal())

    def ended_by_signal(self):
        """The worker must end (SIGTERM, a detached worker's SIGHUP): no final state, nothing cancelled. The
        listing then shows "not being watched" or "interrupted"; Check status or Retry same request
        continues (design 7.5, section 13 row 9)."""
        log.info("the worker ends at once (%s); nothing is cancelled", self.ctx.signal_name)
        return Outcome(self.read().get("state"))

    def check_stop(self):
        """A person's stop between steps: what may run on Globus is looked up and cancelled; else the
        worker writes "stopped" with the standard message."""
        if self.stop_wanted():
            if may_run(self.read()):
                raise _Finished(self.stop_now())
            raise StopRequested()

    # ------------------------------------------------------------------ sign-in and consent

    def sign_in(self, gare=None, stoppable=False):
        """One sign-in, in a thread: the main thread watches the clock and the stop requests. After 10
        minutes the worker records the result and ends (its process exit closes the listener).

        `stoppable`: a person's stop ends the sign-in at once (nothing of this upload runs on Globus): a
        send worker ends "stopped", a check worker keeps the stored state (decision I3). Otherwise the
        stop waits for the sign-in, which it needs to cancel the transfer. A signal that only ends the
        process ends the sign-in at once in every case (decision I8)."""
        ctx = self.ctx
        force = bool(ctx.settings_used.get("force_login"))
        done = threading.Event()
        result = {}

        def run():
            try:
                self.client.login(gare=gare, force_login=force)
                if gare is None:
                    result["identity"] = self.client.identity()
                    result["identity_id"] = _identity_id(self.client)
            except BaseException as error:  # noqa: BLE001 - handed to the main thread
                result["error"] = error
            finally:
                done.set()

        threading.Thread(target=run, name="globus-sign-in", daemon=True).start()
        started = self.clock()
        while not done.wait(SIGN_IN_POLL_SECONDS):
            if ctx.stop_requested() and (self.stop_wanted() and stoppable):
                if ctx.action == "send":
                    raise StopRequested()
                log.info("a stop ended the check's sign-in; nothing runs on Globus to cancel")
                raise _Finished(self.check_stopped())
            if self.clock() - started > SIGN_IN_LIMIT_SECONDS:
                log.info("the sign-in took longer than %d s", SIGN_IN_LIMIT_SECONDS)
                raise _Finished(self.failure("signin_timeout"))
        error = result.get("error")
        if isinstance(error, GlobusCallError):
            if error.code == SIGNIN_NO_BROWSER:
                raise _Finished(self.failure("no_browser"))
            raise _Finished(self.failure("signin_failed", message=_clean(error.message)))
        if error is not None:
            raise error
        if gare is None:
            self.identity = result.get("identity") or None
            self.identity_id = result.get("identity_id") or None
            self.signed_in_at = self.clock()
            shown = self.identity or "an unknown identity"
            if ctx.action == "send":
                self.update_globus(identity=self.identity)
            ctx.event(self.text("Signed in to Globus as %s." % shown))

    def consent_allowed(self):
        """A consent sign-in only while the person who started the worker can be expected at the screen:
        within 10 minutes of the end of the first sign-in (review GLO-01), never while a transfer is
        watched, and for a send worker only while it checks, submits, or settles the consent first."""
        since = self.signed_in_at if self.signed_in_at is not None else self.started
        if self.clock() - since > CONSENT_WINDOW_SECONDS or self.watching:
            return False
        return self.ctx.action != "send" or self.phase in ("checking", "submitting", "settling")

    def settle_consent(self):
        """Ask for ARC's collection consent now, while the person who started this worker is at the screen
        (review GLO-01): a worker that resumes after the checking step, or a check worker, would otherwise
        meet Globus's consent request only while watching, when no browser may open. One look at the ARC
        folder through call(); its answer is not used, and a failure other than a consent is left to the
        steps that follow."""
        base = (self.read().get("destination") or {}).get("globus_base")
        if not base:
            return
        phase, self.phase = self.phase, "settling"
        try:
            self.call(self.client.stat, self.ctx.settings_used["arc_collection"], base.rstrip("/") or "/")
        except GlobusCallError as error:
            log.info("the look at the ARC folder before going on: %s %s", error.http_status, error.code)
        finally:
            self.phase = phase

    def call(self, function, *arguments):
        """A client call; an answer that asks for a consent leads to one more sign-in and the same call."""
        try:
            return function(*arguments)
        except GlobusCallError as error:
            if error.gare is None:
                raise
            gare = error.gare
        self.consent(gare)
        try:
            return function(*arguments)
        except GlobusCallError as error:
            if error.gare is not None:
                raise _Finished(self.failure("consent")) from None
            raise

    def consent(self, gare):
        if not self.consent_allowed():
            raise _Finished(self.sign_in_needed())
        if self.consents >= 1:
            raise _Finished(self.failure("consent"))
        self.consents += 1
        state = self.read().get("state")
        if state in arc_records.WORKING_STATES:
            self.ctx.set_state(state, self.text(TEXTS["consent_asked"]))
        else:
            self.ctx.event(self.text(TEXTS["consent_asked"]))
        self.sign_in(gare=gare)

    def sign_in_needed(self):
        """Globus asks for a sign-in when nobody may be at the screen: stop watching, never open a browser."""
        record = self.read()
        if record.get("state") in ("transferring", "verifying", "placing") and _globus(record).get("task_id"):
            return Outcome(record["state"], self.text(TEXTS["new_sign_in"]))
        return self.failure("consent")

    def late_stop(self, outcome):
        """A person's stop that came as the flow decided to stop watching a transfer that may still run (the
        90-minute watch, a refused token, Globus out of reach): acted on now, while this worker is still
        signed in, instead of being dropped when the worker ends (review GLO-V02)."""
        record = self.read()
        if self.stop_tried or self.signed_in_at is None or outcome.state != record.get("state") or \
                outcome.state not in ("transferring", "verifying", "placing", "uncertain") or not may_run(record):
            return outcome
        try:
            if not self.stop_wanted():
                return outcome
            log.info("a stop came as the watch ended; it is settled now")
            return self.stop_now()
        except _Finished as finished:
            return finished.outcome

    def end(self):
        """Revoke the tokens and close the client, once, whatever happened."""
        if self.ended:
            return
        self.ended = True
        try:
            self.client.logout()
        except Exception as error:  # noqa: BLE001 - an end is never stopped by its own cleanup
            log.info("signing out of Globus failed: %s", type(error).__name__)

    # ------------------------------------------------------------------ listings

    def list_folder(self, collection, path):
        """Every entry of one folder, page by page (a page as long as the limit means: ask again)."""
        entries, seen, offset = [], set(), 0
        while True:
            try:
                page = self.call(self.client.ls, collection, path, offset, PAGE)
            except GlobusCallError as error:
                if _size_limit(error):
                    raise _Truncated(path) from None
                raise
            for entry in page:
                name = entry.get("name")
                if name in seen:
                    raise _Truncated(path)
                seen.add(name)
                entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                raise _Truncated(path)
            if len(page) < PAGE:
                return entries
            offset += PAGE

    def tree(self, collection, top):
        """{relative path: entry} of everything below a folder (its subfolders listed too)."""
        found, pending = {}, [""]
        while pending:
            relative = pending.pop(0)
            path = top.rstrip("/") + ("/" + relative if relative else "") + "/"
            for entry in self.list_folder(collection, path):
                name = str(entry.get("name"))
                below = relative + "/" + name if relative else name
                if below in found:
                    raise _Truncated(path)
                found[below] = entry
                if entry.get("type") == "dir" and not entry.get("link_target"):
                    pending.append(below)
                if len(found) > MAX_ENTRIES:
                    raise _Truncated(path)
        return found

    def payload_files(self):
        """{path: {"bytes", "sha256", "md5"}} of every file of the upload copy (from the snapshot pass)."""
        return self.ctx.payload_digests() or self.ctx.check_payload()

    def kept_files(self):
        """{path: bytes} of what was sent, also after the upload copy was deleted (a verified upload)."""
        digests = self.ctx.payload_digests()
        if digests:
            return {path: entry["bytes"] for path, entry in digests.items()}
        manifest = arc_payload.read_manifest(self.ctx.record_dir) or {}
        files = {entry["path"]: entry["bytes"] for entry in manifest.get("files") or []}
        for name in (arc_payload.MANIFEST_NAME, arc_payload.SUMS_NAME):
            path = self.ctx.record_dir / name
            if path.exists():
                files[name] = path.stat().st_size
        return files

    # ------------------------------------------------------------------ send

    def send(self):
        """Action send: the snapshot is ready (state "preparing"); resume where Retry says."""
        ctx = self.ctx
        self.phase = "signing_in"
        self.set_state("signing_in", self.signing_in_text())
        self.sign_in(stoppable=not may_run(self.read()))
        if self.stop_wanted():
            return self.stop_now()
        record = self.read()
        globus = _globus(record)
        step = ctx.resume_at if ctx.resume_at in STEPS else "checking"
        if step == "submitting" and not globus.get("submission_id"):
            step = "checking"
        if step == "submitting" and not globus.get("task_id") and self.request_spent(globus):
            # A request Globus refused, or one past its deadline, is never sent again: a new attempt, after
            # a look for its task by label (review GLO-05).
            ctx.event(self.text(TEXTS["request_spent"]))
            step = "checking"
        if step in ("transferring", "verifying") and not globus.get("task_id"):
            step = "submitting" if globus.get("submission_id") else "checking"
        if step == "placing" and not globus.get("verification"):
            step = "verifying" if globus.get("task_id") else "checking"
        log.info("Globus steps from %s (resume %s)", step, ctx.resume_at)
        if step in ("transferring", "verifying", "placing"):
            self.settle_consent()
        while True:
            if step == "checking":
                step = self.checking()
            elif step == "submitting":
                step = self.submitting()
            elif step == "transferring":
                step = self.transferring()
            elif step == "verifying":
                step = self.verifying()
            else:
                return self.placing()

    def request_spent(self, globus):
        """True when the saved request must not be sent again: every POST of it was refused, or its
        deadline has passed (Globus would refuse it, or start a task that fails at once)."""
        return _refused_for_sure(globus) or deadline_passed(load_request(self.ctx.record_dir))

    def signing_in_text(self):
        if self.ctx.demo:
            from . import arc_demo

            return arc_demo.SIGN_IN_MESSAGE
        return MESSAGES["signing_in_globus"]

    def checking_text(self):
        if self.ctx.demo:
            return arc_records.message("checking_globus", True)
        template = MESSAGES["checking_globus"]
        if self.identity:
            return self.text(template.format(identity=self.identity))
        return self.text(template.replace(" as {identity}", ""))

    # ------------------------------------------------------------------ checking (10.3)

    def checking(self):
        ctx = self.ctx
        self.phase = "checking"
        self.set_state("checking", self.checking_text())
        self.check_stop()
        self.check_source()
        self.check_stop()
        record = self.read()
        self.check_destination(record)
        ctx.fault("check_mid")
        globus = _globus(self.read())
        if globus.get("task_id") or globus.get("submission_id"):
            if self.new_attempt():
                return "transferring"
        return "submitting"

    def not_yours(self):
        """This Mac's collection is another Globus account's (review DOC-02): a Globus Connect Personal
        collection serves only the account that set it up, so no other sign-in can send from it."""
        return self.ctx.failed("mac_collection_not_yours", identity=self.identity or "this Globus identity")

    def check_source(self):
        """Globus must see exactly the upload copy in this Mac's collection (design 10.3, 1 to 4). A
        collection the signed-in identity may not read (403), or one owned by another identity whose
        listing is refused, is mac_collection_not_yours (review DOC-02)."""
        ctx = self.ctx
        mac = ctx.settings_used["mac_collection"]
        try:
            endpoint = self.call(self.client.get_endpoint, mac) or {}
        except GlobusCallError as error:
            if _forbidden(error):
                raise _Finished(self.not_yours()) from None
            if _not_found(error):
                raise _Finished(ctx.failed("mac_collection_unknown")) from None
            raise self.unexpected(error) from None
        if endpoint.get("gcp_connected") is False or endpoint.get("gcp_paused") is True:
            raise _Finished(ctx.failed("gcp_not_connected"))
        owner = endpoint.get("owner_id")
        files = self.payload_files()
        top = source_path(ctx.payload_dir)
        try:
            found = self.tree(mac, top)
        except _Truncated:
            raise _Finished(ctx.failed("listing_truncated")) from None
        except GlobusCallError as error:
            if _gcp_off(error):
                raise _Finished(ctx.failed("gcp_not_connected")) from None
            if _not_found(error) or _forbidden(error):
                if isinstance(owner, str) and owner and self.identity_id and owner != self.identity_id:
                    raise _Finished(self.not_yours()) from None
                raise _Finished(ctx.failed("source_not_visible", path=top.rstrip("/"))) from None
            raise self.unexpected(error) from None
        differ = _differences(_expected_tree({path: entry["bytes"] for path, entry in files.items()}), found)
        if differ:
            raise _Finished(ctx.failed("source_mismatch", count=len(differ), file=", ".join(differ[:3])))
        ctx.event(self.text("Globus sees the upload copy on this Mac: %d files, as listed." % len(files)))

    def check_destination(self, record):
        """The folders on ARC (made when missing), and the final name not taken (design 10.3, 5 to 8)."""
        ctx = self.ctx
        destination = record["destination"]
        arc = ctx.settings_used["arc_collection"]
        base = destination["globus_base"].rstrip("/") or "/"
        try:
            self.make_folder(arc, base)
            if not destination.get("new_copy"):
                final = base.rstrip("/") + "/" + destination["name"]
                if self.present(arc, final):
                    text = self.message("conflict_checking", folder=destination["name"], base=self.shown(base))
                    raise _Finished(Outcome("conflict", self.text(text), fields={"conflict_at": "checking"}))
            self.make_folder(arc, base.rstrip("/") + "/_incoming")
            self.mkdir(arc, destination["globus_incoming_path"])
        except GlobusCallError as error:
            if _forbidden(error):
                raise _Finished(ctx.failed("destination_permission", path=base)) from None
            raise self.unexpected(error) from None
        ctx.event(self.message("folders_ready", path=self.shown(destination["globus_incoming_path"])))

    def present(self, collection, path):
        try:
            self.call(self.client.stat, collection, path)
        except GlobusCallError as error:
            if _not_found(error):
                return False
            raise
        return True

    def mkdir(self, collection, path):
        try:
            self.call(self.client.mkdir, collection, path)
        except GlobusCallError as error:
            # Exists: made by an earlier run of this upload, whose answer was lost, or by another upload.
            if not _exists(error):
                raise

    def make_folder(self, collection, path):
        """Make a folder and the folders above it that are missing, from the deepest existing one down."""
        missing, current = [], path
        while True:
            try:
                entry = self.call(self.client.stat, collection, current) or {}
            except GlobusCallError as error:
                if not _not_found(error) or current == "/":
                    raise
                missing.append(current)
                current = current.rsplit("/", 1)[0] or "/"
                continue
            if entry.get("type") != "dir":
                raise _Finished(self.ctx.failed("arc_folder_not_dir", path=self.shown(current)))
            break
        for folder in reversed(missing):
            self.mkdir(collection, folder)

    def new_attempt(self):
        """Retry after a failed task or a failed check, or of a request that is not sent again: a new
        attempt (design 10.4). True when the earlier transfer still runs: it is watched instead, so one
        upload never has two active tasks. A finished task of the request that the record never knew
        (found by its label) is checked instead of copied again."""
        ctx = self.ctx
        record = self.read()
        globus = _globus(record)
        task_id = globus.get("task_id")
        task = None
        if task_id:
            task = self.fetch_task(task_id)
        elif globus.get("submission_id"):
            found = self.find_by_label(globus.get("label"))
            if len(found) > 1:
                raise _Finished(ctx.failed("internal", type="SeveralTasks", folder=str(ctx.record_dir)))
            if found:
                task = found[0]
                task_id = task.get("task_id")
        if task is not None:
            self.keep_task(task, force=True)
            status = task.get("status")
            if status in ACTIVE_STATUSES or (status == "SUCCEEDED" and not globus.get("task_id")):
                if not globus.get("task_id"):
                    self.update_globus(task_id=task_id)
                ctx.event(self.text(TEXTS["watch_earlier" if status in ACTIVE_STATUSES else "found_finished"]))
                return True
        fatal = (task or {}).get("fatal_error")
        earlier = {"attempt": globus.get("attempt") or 1, "submission_id": globus.get("submission_id"),
                   "task_id": task_id, "status": (task or {}).get("status"),
                   "fatal_error": {key: fatal.get(key) for key in ("code", "description")}
                   if isinstance(fatal, dict) else None,
                   "label": globus.get("label"), "posts": globus.get("posts") or []}

        def mutate(current):
            sub = _globus(current)
            sub["earlier_attempts"] = list(sub.get("earlier_attempts") or []) + [earlier]
            sub.update(attempt=int(sub.get("attempt") or 1) + 1, submission_id=None, request_saved_utc=None,
                       label=None, posts=[], task_id=None, task=None, verification=None, rename=None)
            progress = current.get("progress") if isinstance(current.get("progress"), dict) else {}
            progress.update(files_done=0, bytes_done=0)
            current["progress"] = progress

        record = ctx.update(mutate)
        attempt = _globus(record)["attempt"]
        if task is None:
            ctx.event(self.text(TEXTS["new_attempt_unseen"].format(attempt=attempt)))
        else:
            ctx.event(self.text(TEXTS["new_attempt"].format(status=task.get("status"), attempt=attempt)))
        return False

    # ------------------------------------------------------------------ submitting (10.4)

    def submitting(self):
        ctx = self.ctx
        self.phase = "submitting"
        self.set_state("submitting", self.text(MESSAGES["submitting"]))
        record = self.read()
        globus = _globus(record)
        if globus.get("task_id"):
            return "transferring"
        submission = globus.get("submission_id")
        if not submission:
            try:
                submission = str(self.call(self.client.submission_id))
            except GlobusCallError as error:
                raise self.unexpected(error) from None
            saved = utc_now()
            attempt = int(globus.get("attempt") or 1)
            planned = copy.deepcopy(record)
            _globus(planned).update(submission_id=submission, request_saved_utc=saved, attempt=attempt)
            body = build_request(planned, submission, ctx.payload_dir)
            # The body and the request number are on disk (durable: decision I5) before the request is posted.
            save_request(ctx.record_dir, body)
            self.update_globus(submission_id=submission, request_saved_utc=saved, attempt=attempt,
                               label=body["label"], posts=[])
            self.fresh_request = True
            ctx.event(self.text(TEXTS["saved"].format(short=_short(submission))))
        else:
            body = load_request(ctx.record_dir)
            if body is None or body != build_request(record, submission, ctx.payload_dir):
                error = ctx.error("payload_changed")
                raise _Finished(Outcome("failed", self.text(TEXTS["request_changed"]), error))
            ctx.check_payload()
            # Saved by an earlier worker, which may have posted it before it ended.
            self.fresh_request = False
        ctx.fault("submit_before_post")
        return self.post(body)

    def add_post(self, outcome):
        def mutate(record):
            sub = _globus(record)
            sub["posts"] = list(sub.get("posts") or []) + [{"utc": utc_now(), "outcome": outcome}]

        self.ctx.update(mutate)

    def post(self, body):
        """Post the saved body; an answer that is lost is asked for again with the same body (Globus
        answers Duplicate for a request number it already has). Every POST is one request that this
        flow counts and records: the client sends it without the SDK's own resends (review GLO-03). A
        person's stop waits for a definite answer, so that nothing keeps running unseen."""
        ctx = self.ctx
        unknown = 0
        delays = [delay * self.scale for delay in RESEND_DELAYS]
        while True:
            try:
                reply = self.call(self.client.submit, body) or {}
            except GlobusCallError as error:
                if not _no_answer(error):
                    return self.refused(error)
                reply = {}
            task_id = reply.get("task_id")
            if task_id:
                ctx.fault("submit_after_post")
                code = str(reply.get("code") or "accepted")
                text = TEXTS["duplicate"] if code.lower() == "duplicate" else TEXTS["accepted"].format(
                    short=_short(task_id))
                self.adopt(task_id, code.lower(), text)
                return "transferring"
            self.add_post("unknown")
            unknown += 1
            if unknown >= len(RESEND_DELAYS) + 1:
                text = TEXTS["uncertain_stop"] if self.stop_wanted() else TEXTS["uncertain"]
                raise _Finished(Outcome("uncertain", self.text(text)))
            self.ctx.set_state("submitting", self.text(TEXTS["unknown_post"]))
            if delays[unknown - 1] > 0 and ctx.wait(delays[unknown - 1]):
                # A person's stop does not end the wait early: the same body is posted at once, so that the
                # stop finds the task. A signal that only ends the process ends the flow here.
                if self.stop_wanted():
                    log.info("a stop was asked for; the request is settled first")

    def adopt(self, task_id, outcome, text):
        """Record the task of this upload's request and move on to watching it (one write). After a lost
        answer, the record says that Globus had the request already until the first look at the task."""

        def mutate(record):
            sub = _globus(record)
            answered_late = any(post.get("outcome") == "unknown" for post in sub.get("posts") or [])
            sub["task_id"] = task_id
            sub["posts"] = list(sub.get("posts") or []) + [{"utc": utc_now(), "outcome": outcome}]
            shown = self.text(text) if answered_late else self.copying_text(record, None)
            arc_records.set_state(record, "transferring", shown)

        self.ctx.update(mutate)
        self.moved = True
        self.ctx.event(self.text(text), "transferring")

    def refused(self, error):
        """A definite answer to the POST: never "uncertain" (review G-01). After a 409, and after any other
        refusal when an earlier POST of this request number may have reached Globus (an answer was lost,
        or an earlier worker saved the request and may have posted it), the task is looked for by its
        label first (review GLO-03)."""
        ctx = self.ctx
        globus = _globus(self.read())
        outcome = "conflict" if error.http_status == 409 else "refused"
        reached = not self.fresh_request or any(isinstance(post, dict) and post.get("outcome") == "unknown"
                                                for post in globus.get("posts") or [])
        if error.http_status == 409 or reached:
            # Maybe an earlier post of this request made the task: look for it by its exact label.
            found = self.find_by_label(globus.get("label"))
            if len(found) == 1 and found[0].get("task_id"):
                self.adopt(found[0]["task_id"], outcome,
                           "Globus already has this upload's transfer: task %s." % _short(found[0]["task_id"]))
                return "transferring"
            self.add_post(outcome)
            if len(found) > 1:
                raise _Finished(ctx.failed("internal", type="SeveralTasks", folder=str(ctx.record_dir)))
            if error.http_status == 409:
                raise _Finished(ctx.failed("request_conflict"))
        else:
            self.add_post(outcome)
        raise _Finished(ctx.failed("refused_by_globus", code=error.code or error.http_status,
                                   message=_clean(error.message)))

    def find_by_label(self, label):
        """The tasks whose label is exactly this upload's attempt's label."""
        try:
            return self.tasks_labelled(label)
        except GlobusCallError as error:
            raise self.unexpected(error) from None

    def tasks_labelled(self, label):
        """find_by_label, its GlobusCallError raised as it is."""
        if not label:
            return []
        tasks = self.call(self.client.task_list, {"label": label}, 10) or []
        return [task for task in tasks if isinstance(task, dict) and task.get("label") == label]

    # ------------------------------------------------------------------ transferring (10.5)

    def copying_text(self, record, task):
        progress = record.get("progress") if isinstance(record.get("progress"), dict) else {}
        task = task or {}
        text = TEXTS["copying"].format(done=task.get("files_transferred") or 0, files=progress.get("files") or 0,
                                       bytes_done=_size_text(task.get("bytes_transferred")),
                                       bytes=_size_text(progress.get("bytes")))
        if not self.ctx.demo:
            text += TEXTS["copying_live"]
        return self.text(text)

    def keep_task(self, task, message=None, force=False):
        """The task's fields in the record (design 7.2), with the progress. A change of what Globus
        reports is written at once; progress alone at most once a second."""
        now = self.clock()
        # The message without its numbers: a new kind of message is written at once, new numbers are not.
        mark = (task.get("status"), task.get("nice_status"), task.get("nice_status_short_description"),
                task.get("is_paused"), None if message is None else re.sub(r"[0-9.,]+", "#", message))
        if not force and mark == self._last_status and self._last_write is not None and now - self._last_write < 1.0:
            return
        self._last_write, self._last_status = now, mark
        kept = {name: task.get(name) for name in TASK_FIELDS}
        if isinstance(kept["fatal_error"], dict):
            kept["fatal_error"] = {key: kept["fatal_error"].get(key) for key in ("code", "description")}
        kept["checked_utc"] = utc_now()

        def mutate(record):
            _globus(record)["task"] = kept
            progress = record.get("progress") if isinstance(record.get("progress"), dict) else {}
            if task.get("files_transferred") is not None:
                progress["files_done"] = task["files_transferred"]
            if task.get("bytes_transferred") is not None:
                progress["bytes_done"] = task["bytes_transferred"]
            record["progress"] = progress
            if message is not None:
                record["message"] = message

        self.ctx.update(mutate)

    def fetch_task(self, task_id):
        try:
            return self.call(self.client.get_task, task_id) or {}
        except GlobusCallError as error:
            if _forbidden(error) or _not_found(error):
                raise _Finished(self.not_visible()) from None
            raise self.unexpected(error) from None

    def not_visible(self):
        """A task this identity cannot see: failed / task_not_visible, so no record stays open for ever."""
        return self.ctx.failed("task_not_visible", identity=self.identity or "this Globus identity")

    def poll_seconds(self, watched):
        if self.ctx.demo:
            return DEMO_POLL_SECONDS * self.scale
        for until, seconds in POLL_SCHEDULE:
            if until is None or watched < until:
                return seconds
        return POLL_SCHEDULE[-1][1]

    def watch_text(self, record, task, quiet_for):
        status = task.get("status")
        nice = task.get("nice_status")
        description = task.get("nice_status_short_description") or nice or "no reason was given"
        if status == "INACTIVE":
            return self.text(TEXTS["inactive"].format(description=str(description).rstrip(".")))
        if task.get("is_paused"):
            return self.text(TEXTS["admin_paused"])
        if quiet_for >= GCP_QUIET_SECONDS and not self.ctx.demo and self.gcp_off():
            return self.text(TEXTS["gcp_waiting"])
        if nice in QUIET_NICE_STATUSES:
            return self.copying_text(record, task)
        return self.text(TEXTS["nice"].format(description=str(description).rstrip("."),
                                              deadline=_local_time(task.get("deadline"))))

    def gcp_off(self):
        """Globus Connect Personal on this Mac is not connected (looked at after 2 minutes without progress)."""
        now = self.clock()
        if self._gcp_checked is not None and now - self._gcp_checked < GCP_QUIET_SECONDS:
            return self._gcp_off
        self._gcp_checked = now
        try:
            endpoint = self.client.get_endpoint(self.ctx.settings_used["mac_collection"]) or {}
        except GlobusCallError:
            return False
        self._gcp_off = endpoint.get("gcp_connected") is False or endpoint.get("gcp_paused") is True
        return self._gcp_off

    def transferring(self):
        ctx = self.ctx
        self.phase = "transferring"
        self.watching = True
        record = self.read()
        task_id = _globus(record)["task_id"]
        if record.get("state") != "transferring":
            self.set_state("transferring", self.copying_text(record, _globus(record).get("task")))
        watch_started = self.clock()
        progress_mark, progress_at = None, watch_started
        failing_since = None
        first = True
        while True:
            if self.stop_wanted():
                raise _Finished(self.cancel_and_stop(task_id))
            if self.signed_in_at is not None and self.clock() - self.signed_in_at > WATCH_LIMIT_SECONDS:
                raise _Finished(Outcome("transferring", self.watch_ended_text()))
            try:
                task = self.call(self.client.get_task, task_id) or {}
            except GlobusCallError as error:
                if _token_rejected(error):
                    raise _Finished(Outcome("transferring", self.watch_ended_text())) from None
                if _forbidden(error) or _not_found(error):
                    raise _Finished(self.not_visible()) from None
                if not _no_answer(error):
                    raise self.unexpected(error) from None
                failing_since = failing_since if failing_since is not None else self.clock()
                if self.clock() - failing_since > UNREACHABLE_SECONDS:
                    raise _Finished(Outcome("transferring", self.text(TEXTS["unreachable"]))) from None
                ctx.wait(self.poll_seconds(self.clock() - watch_started))
                continue
            failing_since = None
            record = self.read()
            mark = (task.get("files_transferred"), task.get("bytes_transferred"))
            if mark != progress_mark:
                progress_mark, progress_at = mark, self.clock()
            status = task.get("status")
            if status == "SUCCEEDED":
                self.keep_task(task, force=True)
            elif status == "FAILED":
                self.keep_task(task, force=True)
            else:
                self.keep_task(task, self.watch_text(record, task, self.clock() - progress_at))
            if first:
                first = False
                ctx.fault("poll_mid_transfer")
            if status == "SUCCEEDED":
                ctx.event(self.text("Globus finished the copy: %s files." % (task.get("files_transferred") or 0)))
                return "verifying"
            if status == "FAILED":
                raise _Finished(self.task_failed(task_id, task))
            ctx.wait(self.poll_seconds(self.clock() - watch_started))

    def watch_ended_text(self):
        return arc_records.message("not_watched_demo" if self.ctx.demo else "not_watched", self.ctx.demo)

    def task_failed(self, task_id, task):
        fatal = task.get("fatal_error") if isinstance(task.get("fatal_error"), dict) else {}
        description = fatal.get("description")
        if not description:
            try:
                events = self.call(self.client.task_events, task_id, 10) or []
            except GlobusCallError:
                events = []
            errors = [event for event in events if isinstance(event, dict) and event.get("is_error")]
            errors.sort(key=lambda event: str(event.get("time") or ""), reverse=True)
            description = errors[0].get("description") if errors else None
        return self.ctx.failed("task_failed", description=_clean(description or "no reason was given"),
                               incoming=self.incoming_shown())

    # ------------------------------------------------------------------ stop (10.8)

    def cancel_and_stop(self, task_id):
        """Cancel the task, then look until it has ended (at most 2 minutes). A cancel that gets no answer
        is asked for again on the next look; a stop that never reached Globus, or whose task Globus could
        not be asked about, leaves the upload as it was and says so: the copy may still be running, and
        Stop upload stays offered (review GLO-07)."""
        self.stop_tried = True
        deadline = self.clock() + STOP_SETTLE_SECONDS
        delivered = False
        while True:
            if not delivered:
                try:
                    self.call(self.client.cancel, task_id)
                    delivered = True
                except GlobusCallError as error:
                    if _forbidden(error) or _not_found(error):
                        return self.not_visible()
                    if _token_rejected(error):
                        return self.keep_state(TEXTS["stop_not_delivered"])
                    if not _no_answer(error):
                        raise self.unexpected(error) from None
                    log.info("the cancel got no answer; it is asked for again")
            task = self.look_at_task(task_id)
            if task is not None:
                self.keep_task(task, force=True)
                status = task.get("status")
                if status == "FAILED":
                    return Outcome("stopped", self.message("stopped_after_cancel", incoming=self.incoming_shown()))
                if status == "SUCCEEDED":
                    return Outcome("stopped", self.message("stopped_complete", incoming=self.incoming_shown()))
            if self.clock() > deadline:
                return self.keep_state(TEXTS["stop_pending" if delivered else "stop_not_delivered"])
            self.sleep(STOP_POLL_SECONDS * (self.scale if self.ctx.demo else 1.0))

    def look_at_task(self, task_id):
        """The task document, or None when Globus gave no answer (a stop goes on asking)."""
        try:
            return self.call(self.client.get_task, task_id) or {}
        except GlobusCallError as error:
            if _forbidden(error) or _not_found(error):
                raise _Finished(self.not_visible()) from None
            if _no_answer(error) or _token_rejected(error):
                return None
            raise self.unexpected(error) from None

    def keeps_the_state(self):
        """True for a check worker that started while nothing of this upload could run on Globus (may_run false):
        a person's stop ends it and the upload keeps the stored state (check_stopped(); decision I3, review R2-01).
        A check that started while Globus could still run the transfer is stopped as Stop upload stops, for the
        whole check (review R3-01)."""
        return self.ctx.action == "check" and not self.check_may_run

    def check_stopped(self):
        """A person's stop of a check that started while nothing of this upload could run on Globus (decision I3):
        the check ends at once, and the upload gets back the state, message and error it had when the check
        started; the history says that the check was stopped. This holds also when the check had gone on to
        check or place the copy of the transfer that had ended (review R2-01): from verifying or placing the
        allowed state changes lead back to failed and stopped. They do not lead back to transferring or
        uncertain: an upload that the check found there (its task had ended), and that it had moved to verifying
        when the stop came, stays at verifying; the listing then shows it as not being watched."""
        log.info("a stop ended the check; the upload keeps its state")
        self.ctx.event(TEXTS["check_stopped"])
        record = self.read()
        current = record.get("state")
        stored = self.stored or Outcome(current, record.get("message"), record.get("error"))
        if current == stored.state:
            return Outcome(current, stored.message if stored.message != record.get("message") else None)
        if (current, stored.state) in arc_records.TRANSITIONS:
            return Outcome(stored.state, stored.message, stored.error)
        return Outcome(current)

    def stop_after_the_copy(self):
        """A person's stop once the transfer has ended (verifying, placing).

        - A check that started while nothing of this upload could run on Globus keeps the stored state
          (check_stopped()). Its Stop question said that the upload stays as it is (review R2-01).
        - A send worker, and a check that started while Globus could still run the transfer, end "stopped" with
          the copy complete in _incoming (stopped_complete(); "Stopped while the copy was being put in place"
          once a rename was asked for). The Stop question of such a check said that the transfer is cancelled
          and that files already copied stay in _incoming (review R3-01)."""
        return self.check_stopped() if self.keeps_the_state() else self.stopped_complete()

    def stopped_complete(self):
        """Stopped with the copy complete on ARC. When a rename was already asked for (a worker that ended
        while placing), the copy may be in place: the text says so instead."""
        record = self.read()
        if record.get("state") == "conflict":
            # The rename answered Exists: the copy is complete in _incoming and not in place.
            return Outcome("stopped", self.message("stopped", incoming=self.incoming_shown(record)))
        if _globus(record).get("rename"):
            return Outcome("stopped", self.message("stopped_placing"))
        return Outcome("stopped", self.message("stopped_complete", incoming=self.incoming_shown(record)))

    def stop(self):
        """Action stop: settle the request and cancel any task (design 10.8)."""
        ctx = self.ctx
        self.phase = "stop"
        globus = _globus(self.read())
        task = globus.get("task") if isinstance(globus.get("task"), dict) else {}
        if globus.get("task_id") and task.get("status") in FINAL_STATUSES:
            # Nothing runs on Globus any more: no sign-in is needed to stop.
            if task["status"] == "SUCCEEDED":
                return self.stopped_complete()
            return Outcome("stopped", self.message("stopped_after_cancel", incoming=self.incoming_shown()))
        ctx.event(self.text("Stop: signing in to Globus to cancel the transfer."))
        self.sign_in()
        # A signal that only ends the process ends the stop worker here too, cancelling nothing (I8).
        self.stop_wanted()
        return self.stop_now()

    def stop_now(self):
        """After a sign-in: find this upload's task (by its label when only the request number is known)
        and cancel it; without a task, the upload is stopped at once. When Globus cannot be asked, the
        upload stays as it was and says so (review GLO-07)."""
        ctx = self.ctx
        self.stop_tried = True
        globus = _globus(self.read())
        task_id = globus.get("task_id")
        if not task_id and globus.get("submission_id"):
            try:
                found = self.tasks_labelled(globus.get("label"))
            except GlobusCallError as error:
                if _no_answer(error) or _token_rejected(error):
                    return self.keep_state(TEXTS["stop_not_delivered"])
                raise self.unexpected(error) from None
            if len(found) > 1:
                raise _Finished(ctx.failed("internal", type="SeveralTasks", folder=str(ctx.record_dir)))
            if found:
                task_id = found[0].get("task_id")
                self.update_globus(task_id=task_id)
        if not task_id:
            current = self.read()
            if current.get("state") not in arc_records.WORKING_STATES + ("uncertain", "conflict"):
                return Outcome(current.get("state"))
            if arc_records.may_be_on_arc(current):
                return Outcome("stopped", self.message("stopped", incoming=self.incoming_shown(current)))
            return Outcome("stopped", self.message("stopped_nothing_sent"))
        return self.cancel_and_stop(task_id)

    # ------------------------------------------------------------------ check status (10.8)

    def check(self):
        """Action check: how the upload stands; nothing is sent. The state stays while signing in.

        A person's stop (decision I3) is decided by may_run as the check starts, which the window's Stop
        question followed then:
        - may_run false (nothing of this upload can run on Globus: a verified upload, a failed or stopped one
          whose transfer has ended, a conflict): the check ends at once, its sign-in included, and the upload
          keeps the stored state, also while the check checks or places the copy (check_stopped()).
        - may_run true: the stop is Stop upload's for the whole check. It waits for the sign-in and cancels
          the transfer. When the transfer has ended meanwhile (the check checks or places the copy), the upload
          ends "stopped" with the copy complete in _incoming (stop_after_the_copy(), review R3-01). This holds
          also after the check has written the task's end, when the listing's may_run is false."""
        ctx = self.ctx
        self.phase = "check"
        record = self.read()
        state = record.get("state")
        self.stored = Outcome(state, record.get("message"), record.get("error"))
        runs = self.check_may_run = may_run(record)
        ctx.event(self.text("Check status: signing in to Globus."))
        self.sign_in(stoppable=not runs)
        if self.stop_wanted():
            return self.stop_now() if runs else self.check_stopped()
        if state == "verified":
            return self.check_verified()
        # The consent for ARC's collection, while the person is at the screen (review GLO-01).
        self.settle_consent()
        globus = _globus(self.read())
        task_id = globus.get("task_id")
        if not task_id and globus.get("submission_id"):
            found = self.find_by_label(globus.get("label"))
            if len(found) > 1:
                raise _Finished(ctx.failed("internal", type="SeveralTasks", folder=str(ctx.record_dir)))
            if found:
                task_id = found[0].get("task_id")
                self.update_globus(task_id=task_id)
                ctx.event(self.text("Globus has this upload's transfer: task %s." % _short(task_id)))
        if not task_id:
            return self.keep_state(TEXTS["no_transfer"])
        task = self.fetch_task(task_id)
        self.keep_task(task, force=True)
        status = task.get("status")
        if status == "FAILED":
            if state == "stopped":
                return Outcome("stopped", self.message("stopped_after_cancel", incoming=self.incoming_shown()))
            return self.task_failed(task_id, task)
        if status in ACTIVE_STATUSES:
            if state in ("verifying", "placing"):
                return self.keep_state("Globus still runs a transfer of this upload. Press Check status again later.")
            step = self.transferring()
        elif status == "SUCCEEDED":
            step = "placing" if state == "placing" and _globus(self.read()).get("verification") else "verifying"
        else:
            return self.keep_state("Globus reports the transfer as %s." % (status or "unknown"))
        if step == "verifying":
            self.verifying(set_state=state != "placing")
        return self.placing()

    def check_verified(self):
        """A verified upload: list the folder on ARC against what was sent; the state stays verified. Only a
        listing that found the folder gone or different makes a failed check; a listing that could not be
        made, or was made only in part, is a check that could not look (review REG-01). A listing that Globus
        refuses for this identity (403) is one too, with a fix about access to the folder (review R2-03)."""
        ctx = self.ctx
        record = self.read()
        arc = ctx.settings_used["arc_collection"]
        final = record["destination"].get("globus_final_path")
        files = self.kept_files()
        try:
            found = self.tree(arc, final)
        except _Truncated:
            return Outcome("verified", None, check=arc_records.check_note(self.message("listed_in_part")))
        except GlobusCallError as error:
            if _not_found(error):
                return Outcome("verified", None, check=arc_records.check_result(False, self.message(
                    "gone_from_arc", path=self.shown(final))))
            if _forbidden(error):
                log.info("Globus refused the listing of the copy: %s %s", error.http_status, error.code)
                return Outcome("verified", None, check=arc_records.check_note(self.message(
                    "listing_forbidden", path=self.shown(final)), "folder_not_readable", ctx.demo))
            raise self.unexpected(error) from None
        differ = _differences(_expected_tree(files), found)
        if differ:
            return Outcome("verified", None, check=arc_records.check_result(False, self.message(
                "differs_on_arc", count=len(differ), files=", ".join(differ[:3]))))
        message = TEXTS["still_in_demo" if ctx.demo else "still_on_arc"].format(files=len(files))
        return Outcome("verified", None, check=arc_records.check_result(True, message))

    # ------------------------------------------------------------------ verifying (10.6)

    def verifying(self, set_state=True):
        ctx = self.ctx
        self.phase = "verifying"
        # A stop that is already there: a check that keeps the stored state ends before the upload moves on (review
        # R2-01). Any other stop ends the upload "stopped" from verifying, as in round 2 (review R3-01): a failed
        # upload may go to verifying, but not to "stopped" at once.
        if self.keeps_the_state() and self.stop_wanted():
            raise _Finished(self.check_stopped())
        if set_state:
            self.set_state("verifying", self.message("verifying"))
        if self.stop_wanted():
            raise _Finished(self.stop_after_the_copy())
        found = ctx.check_payload()
        record = self.read()
        globus = _globus(record)
        task_id = globus["task_id"]
        incoming = record["destination"]["globus_incoming_path"].rstrip("/")
        try:
            transfers = self.successful_transfers(task_id)
        except GlobusCallError as error:
            if _not_found(error) and self.history_gone(task_id):
                # Globus keeps the list of copied files for a month (review GLO-V01). It compared every
                # file when it copied it (verify_checksum); the listing below checks names and sizes.
                ctx.event(self.message("history_gone"))
                transfers = None
            elif _forbidden(error) or _not_found(error):
                raise _Finished(self.not_visible()) from None
            else:
                raise self.unexpected(error) from None
        problems, seen = [], {}
        if transfers is None:
            # Without Globus's list, every file counts as seen, without a checksum of Globus's.
            seen = {path: {} for path in found}
        for item in transfers or []:
            destination = str(item.get("destination_path") or "")
            if not destination.startswith(incoming + "/"):
                problems.append(destination or "?")
                continue
            relative = destination[len(incoming) + 1:]
            if relative in seen:
                problems.append(relative)
            seen[relative] = item
        for path in sorted(set(found) | set(seen), key=_sort_key):
            item = seen.get(path)
            if item is None or path not in found:
                problems.append(path)
            elif isinstance(item.get("size"), int) and item["size"] != found[path]["bytes"]:
                problems.append(path)
        algorithms, every_one = set(), True
        # MD5 and SHA-256 as the snapshot pass computed them (the re-hash above found the same files).
        snapshot = ctx.payload_digests() or {}
        for path in sorted(set(found) & set(seen), key=_sort_key):
            item = seen[path]
            algorithm = str(item.get("checksum_algorithm") or "").lower()
            value = item.get("checksum")
            if algorithm not in CHECKSUM_ALGORITHMS or not _is_hex(value):
                every_one = False
                continue
            mine = _checksum(algorithm, ctx.payload_dir / path, snapshot.get(path) or found[path])
            if _same_checksum(algorithm, mine, value):
                algorithms.add(algorithm.upper())
            else:
                problems.append(path)
        ctx.fault("verify_mid")
        if every_one and algorithms:
            verification = {"method": "globus-checksum", "algorithm": ", ".join(sorted(algorithms)),
                            "files": len(found), "utc": utc_now()}
        else:
            verification = {"method": "globus-verify", "algorithm": None, "files": len(found), "utc": utc_now()}
        try:
            listed = self.tree(ctx.settings_used["arc_collection"], incoming)
        except _Truncated:
            problems.append("(the listing of _incoming was not complete)")
            listed = {}
        except GlobusCallError as error:
            if not _not_found(error):
                raise self.unexpected(error) from None
            if problems:
                raise _Finished(self.mismatch(self.message("incoming_gone"), self.incoming_shown(record))) from None
            # Put in place by an earlier worker that ended before it could write so.
            self.update_globus(verification=verification)
            destination = record["destination"]
            target = destination["globus_base"].rstrip("/") + "/" + destination["name"]
            raise _Finished(self.placed_before((globus.get("rename") or {}).get("to"), target)) from None
        problems.extend(_differences(_expected_tree({path: entry["bytes"] for path, entry in found.items()}), listed))
        if problems:
            unique = list(dict.fromkeys(problems))
            raise _Finished(ctx.failed("verify_mismatch", file=", ".join(unique[:3])))
        self.update_globus(verification=verification)
        ctx.event(self.message("checked", files=len(found)))
        return "placing"

    def history_gone(self, task_id):
        """True when Globus shows the task as finished but no longer keeps its list of copied files (it
        keeps it for a month). A task this identity cannot see is task_not_visible (fetch_task)."""
        task = self.fetch_task(task_id)
        self.keep_task(task, force=True)
        return task.get("status") == "SUCCEEDED" or task.get("history_deleted") is True

    def successful_transfers(self, task_id):
        """Every successful transfer of the task, page by page (marker, next_marker)."""
        items, marker, pages = [], None, 0
        while True:
            page = self.call(self.client.successful_transfers, task_id, marker) or {}
            items.extend(item for item in page.get("DATA") or [] if isinstance(item, dict))
            marker = page.get("next_marker")
            pages += 1
            if not marker or pages > MAX_ENTRIES:
                return items

    def mismatch(self, text, file):
        error = self.ctx.error("verify_mismatch", file=file)
        return Outcome("failed", self.text(text), error)

    # ------------------------------------------------------------------ placing (10.7)

    def placing(self):
        ctx = self.ctx
        self.phase = "placing"
        record = self.read()
        previous = (_globus(record).get("rename") or {}).get("to")
        # Before the state moves on, so that a check that keeps the stored state keeps it here too. Any other stop
        # ends the upload "stopped" from verifying (stop_after_the_copy()).
        if self.stop_wanted():
            raise _Finished(self.stop_after_the_copy())
        self.set_state("placing", self.message("placing"))
        destination = record["destination"]
        arc = ctx.settings_used["arc_collection"]
        incoming = destination["globus_incoming_path"].rstrip("/")
        base = destination["globus_base"].rstrip("/")
        folder = destination["name"]
        # A new copy goes under the first free name: <folder> itself, then _copy2 ... _copy99 (decision I4).
        names = [folder] + (["%s_copy%d" % (folder, number) for number in range(2, LAST_COPY + 1)]
                            if destination.get("new_copy") else [])
        for name in names:
            target = base + "/" + name
            # Written first: a crash right after the rename leaves the record saying where it went.
            self.update_globus(rename={"from": incoming, "to": target, "name": name, "utc": utc_now()})
            try:
                self.call(self.client.rename, arc, incoming, target)
            except GlobusCallError as error:
                failed = error
            else:
                failed = None
            if failed is not None:
                if _exists(failed) and self.incoming_there(arc, incoming):
                    if destination.get("new_copy"):
                        continue
                    return self.conflict_at_placing(record)
                if _exists(failed) or _not_found(failed):
                    return self.placed_before(previous, target)
                if _forbidden(failed):
                    raise _Finished(ctx.failed("destination_permission", path=base))
                raise self.unexpected(failed)
            ctx.fault("place_after_rename")
            try:
                entry = self.call(self.client.stat, arc, target) or {}
            except GlobusCallError as error:
                raise self.unexpected(error) from None
            if entry.get("type") != "dir":
                raise _Finished(ctx.failed("verify_mismatch", file=self.shown(target)))
            return self.verified(name)
        return self.conflict_at_placing(record)

    def incoming_there(self, collection, incoming):
        try:
            return self.present(collection, incoming)
        except GlobusCallError as error:
            raise self.unexpected(error) from None

    def conflict_at_placing(self, record):
        return Outcome("conflict", self.message("conflict_placing", incoming=self.incoming_shown(record)),
                       fields={"conflict_at": "placing"})

    def placed_before(self, previous, target):
        """The incoming folder is gone: the rename may have happened before a crash. Verified when a
        folder it was put under holds exactly the upload copy."""
        arc = self.ctx.settings_used["arc_collection"]
        files = {path: entry["bytes"] for path, entry in self.payload_files().items()}
        for candidate in dict.fromkeys(name for name in (previous, target) if name):
            try:
                found = self.tree(arc, candidate)
            except _Truncated:
                continue
            except GlobusCallError as error:
                if _not_found(error):
                    continue
                raise self.unexpected(error) from None
            if not _differences(_expected_tree(files), found):
                self.ctx.event(self.text("The copy was already in place as %s." % candidate.rsplit("/", 1)[-1]))
                return self.verified(candidate.rsplit("/", 1)[-1])
        return self.mismatch(self.message("incoming_gone"), self.incoming_shown())

    def verified(self, name):
        ctx = self.ctx
        record = ctx.placed_as(name)
        verification = _globus(record).get("verification") or {
            "method": "globus-verify", "algorithm": None, "files": None, "utc": utc_now()}
        files = (record.get("payload") or {}).get("files") or verification.get("files") or 0
        progress = record.get("progress") if isinstance(record.get("progress"), dict) else {}
        progress.update(files_done=progress.get("files") or files, bytes_done=progress.get("bytes") or 0)
        if ctx.demo:
            message = TEXTS["verified_demo"].format(files=files)
        else:
            if verification.get("method") == "globus-checksum":
                sentence = TEXTS["sentence_checksum"].format(algorithm=verification.get("algorithm"))
            else:
                sentence = TEXTS["sentence_verify"]
            message = TEXTS["verified"].format(files=files, path=record["destination"]["final_path"],
                                               sentence=sentence)
        return Outcome("verified", message, fields={"verification": verification, "progress": progress})


# ---------------------------------------------------------------------- the live client


def _is_real_sdk(sdk):
    return isinstance(sdk, types.ModuleType) and sdk.__name__.split(".")[0] == "globus_sdk"


def _sdk_part(sdk, dotted):
    """An attribute or submodule of the SDK (imported when needed; a stand-in module has it as attributes)."""
    value, names = sdk, dotted.split(".")
    for index, name in enumerate(names):
        try:
            value = getattr(value, name)
        except AttributeError:
            if not isinstance(sdk, types.ModuleType):
                raise
            value = importlib.import_module(sdk.__name__ + "." + ".".join(names[:index + 1]))
    return value


def _log_nothing(handler, format_text, *arguments):
    """RedirectHandler.log_message: the sign-in's callback line holds a one-time code; never write it."""
    return None


def quiet_redirect_handler(sdk):
    """Silence the SDK's loopback listener, which would write `GET /?code=...&state=...` to stderr."""
    local_server = _sdk_part(sdk, "login_flows.local_server_login_flow_manager.local_server")
    local_server.RedirectHandler.log_message = _log_nothing


_QUIET_MANAGERS = {}


def quiet_login_flow_manager(sdk):
    """QuietLocalServerLoginFlowManager of an SDK module: its local-server sign-in, listening on
    127.0.0.1 only, with the fixed consent label APP_NAME (the SDK's own label would send this Mac's
    host name to Globus: review S-V02)."""
    base = _sdk_part(sdk, "login_flows").LocalServerLoginFlowManager
    if base in _QUIET_MANAGERS:
        return _QUIET_MANAGERS[base]

    class QuietLocalServerLoginFlowManager(base):
        """The SDK's local-server sign-in with a fixed label and the loopback address."""

        @classmethod
        def for_globus_app(cls, app_name, login_client, config):
            if getattr(config, "login_redirect_uri", None):
                raise ValueError("A local-server sign-in cannot use another redirect address")
            return cls(login_client, request_refresh_tokens=bool(getattr(config, "request_refresh_tokens", False)),
                       native_prefill_named_grant=APP_NAME, server_address=LOOPBACK)

    _QUIET_MANAGERS[base] = QuietLocalServerLoginFlowManager
    return QuietLocalServerLoginFlowManager


def _data(response):
    """A response's JSON document (the SDK's responses keep it in .data)."""
    value = getattr(response, "data", response)
    return value if value is not None else {}


class LiveGlobus:
    """The client of a live upload, through the Globus SDK (design 10.2, 10.9).

    Tokens live in this process's memory only, without refresh tokens; nothing signs in by
    itself; the consent label is fixed; the loopback listener writes nothing; every SDK error
    becomes a GlobusCallError. Every token the process is given is kept in a list, because a consent
    sign-in replaces the stored ones, and all of them are revoked at the end, one try each with a
    short timeout (reviews GLO-04, GLO-V02). The transfer request goes without the SDK's own resends,
    so that every POST is one the flow counts (review GLO-03). `sdk` stands in for the globus_sdk
    module in the tests; the real module is refused under the test guard (RAMAN_ARC_TEST_GUARD=1)."""

    def __init__(self, settings_used, sdk=None):
        if sdk is None or _is_real_sdk(sdk):
            arc_worker.check_live_globus()
        if sdk is None:
            import globus_sdk as sdk  # the only import of the SDK (design 10.1)
        self.sdk = sdk
        self.closed = False
        self._tokens = []
        self._userinfo = {}
        quiet_redirect_handler(sdk)
        config = sdk.GlobusAppConfig(
            login_flow_manager=quiet_login_flow_manager(sdk),
            token_storage="memory",
            request_refresh_tokens=False,
            auto_redrive_gares=False,
            token_validation_error_handler=None)
        self.app = sdk.UserApp(APP_NAME, client_id=settings_used["client_id"], config=config)
        self.transfer = sdk.TransferClient(app=self.app)
        self.app.add_scope_requirements({"auth.globus.org": ["openid", "profile"]})
        # Who signed in: openid and profile only (the AuthClient's default would also ask for e-mail).
        self.auth = sdk.AuthClient(app=self.app, app_scopes=[sdk.Scope("openid"), sdk.Scope("profile")])
        # Revokes the tokens at the end: the app's own kind of client (a native app sends its client ID).
        self.revoker = sdk.NativeAppAuthClient(client_id=settings_used["client_id"], app_name=APP_NAME)

    # -- errors

    def _translate(self, error):
        sdk = self.sdk
        if isinstance(error, GlobusCallError):
            return error
        api_error = getattr(sdk, "GlobusAPIError", None)
        if api_error is not None and isinstance(error, api_error):
            gare = None
            try:
                gare = _sdk_part(sdk, "gare").to_gare(error)
            except Exception as problem:  # noqa: BLE001 - an unreadable error document is not a consent
                log.info("the error document could not be read: %s", type(problem).__name__)
            return GlobusCallError(getattr(error, "http_status", 0) or 0, getattr(error, "code", None) or "",
                                   getattr(error, "message", None) or "", gare=gare)
        try:
            token_error = _sdk_part(sdk, "token_storage").TokenValidationError
        except (AttributeError, ImportError):
            token_error = None
        if token_error is not None and isinstance(error, token_error):
            return GlobusCallError(401, "TokenValidationError", "The sign-in is no longer valid")
        kinds = (getattr(sdk, "NetworkError", None), getattr(sdk, "GlobusConnectionError", None),
                 getattr(sdk, "GlobusTimeoutError", None))
        network = tuple(kind for kind in kinds if isinstance(kind, type))
        try:
            import requests

            network += (requests.RequestException,)
        except ImportError:
            pass
        if isinstance(error, network + (OSError,)):
            return GlobusCallError(0, NETWORK, type(error).__name__)
        return None

    def _call(self, method, *arguments, **options):
        try:
            return _data(method(*arguments, **options))
        except Exception as error:  # noqa: BLE001 - translated below; anything else is a program error
            translated = self._translate(error)
            if translated is None:
                raise
            raise translated from None

    # -- sign-in

    def login(self, gare=None, force_login=False):
        # The tokens of an earlier sign-in are kept before a consent sign-in replaces them (review GLO-04).
        self._remember_tokens()
        try:
            if gare is not None:
                self.app.login(auth_params=gare.authorization_parameters)
            elif force_login:
                parameters = _sdk_part(self.sdk, "gare").GlobusAuthorizationParameters(prompt="login")
                self.app.login(auth_params=parameters)
            else:
                self.app.login()
        except Exception as error:  # noqa: BLE001 - every sign-in failure is reported with its own words
            raise self._sign_in_error(error) from None
        finally:
            self._remember_tokens()

    def _token_storage(self):
        """The app's own token storage (in memory), without the validators that may call Globus."""
        storage = self.app.token_storage
        return getattr(storage, "token_storage", storage)

    def _remember_tokens(self):
        """Keep every token string this process was given, so that logout() revokes each of them."""
        try:
            stored = self._token_storage().get_token_data_by_resource_server()
        except Exception as error:  # noqa: BLE001 - no tokens to keep is not an error of the upload
            log.info("the tokens could not be read: %s", type(error).__name__)
            return
        for data in stored.values():
            for token in (getattr(data, "access_token", None), getattr(data, "refresh_token", None)):
                if isinstance(token, str) and token and token not in self._tokens:
                    self._tokens.append(token)

    def _sign_in_error(self, error):
        flows = _sdk_part(self.sdk, "login_flows")
        environmental = getattr(flows, "LocalServerEnvironmentalLoginError", None)
        if environmental is not None and isinstance(error, environmental):
            return GlobusCallError(0, SIGNIN_NO_BROWSER, _clean(str(error)))
        api_error = getattr(self.sdk, "GlobusAPIError", None)
        if api_error is not None and isinstance(error, api_error):
            text = getattr(error, "message", None) or getattr(error, "code", None) or type(error).__name__
        else:
            text = str(error) or type(error).__name__
        return GlobusCallError(0, SIGNIN_FAILED, _clean(text))

    def identity(self):
        try:
            info = self._call(self.auth.userinfo)
        except GlobusCallError:
            return None
        self._userinfo = info if isinstance(info, dict) else {}
        return self._userinfo.get("preferred_username") or self._userinfo.get("name") or None

    def identity_id(self):
        """The Globus ID ("sub") of the identity that signed in, from the same userinfo answer."""
        value = self._userinfo.get("sub")
        return value if isinstance(value, str) and value else None

    def logout(self):
        """Revoke every token this process was given, forget them, then close the app and the clients;
        errors are ignored. Each revocation is one try of at most REVOKE_TIMEOUT_SECONDS, and none is
        tried once Globus Auth could not be reached, so that the end of a worker is never held up for
        minutes (review GLO-V02): the tokens live only in this process's memory."""
        if self.closed:
            return
        self.closed = True
        self._remember_tokens()
        self._revoke()
        for step in (self._forget_tokens, self.app.close, self.transfer.close, self.auth.close, self.revoker.close):
            try:
                step()
            except Exception as error:  # noqa: BLE001 - the worker ends anyway
                log.info("Globus clean-up step failed: %s", type(error).__name__)

    def _revoke(self):
        tokens, self._tokens = self._tokens, []
        revoker = self.revoker
        try:
            with revoker.retry_config.tune(max_retries=0), revoker.transport.tune(http_timeout=REVOKE_TIMEOUT_SECONDS):
                for token in tokens:
                    try:
                        revoker.oauth2_revoke_token(token)
                    except Exception as error:  # noqa: BLE001 - a token that cannot be revoked dies with us
                        translated = self._translate(error)
                        log.info("a token could not be revoked: %s", type(error).__name__)
                        if translated is not None and translated.http_status == 0:
                            break
        except Exception as error:  # noqa: BLE001 - the worker ends anyway
            log.info("revoking the tokens failed: %s", type(error).__name__)

    def _forget_tokens(self):
        storage = self._token_storage()
        for resource_server in list(storage.get_token_data_by_resource_server()):
            storage.remove_token_data(resource_server)

    # -- Transfer

    def get_endpoint(self, collection):
        return self._call(self.transfer.get_endpoint, collection)

    def ls(self, collection, path, offset=0, limit=PAGE):
        page = self._call(self.transfer.operation_ls, collection, path, show_hidden=True, limit=limit, offset=offset)
        return list(page.get("DATA") or [])

    def stat(self, collection, path):
        return self._call(self.transfer.operation_stat, collection, path)

    def mkdir(self, collection, path):
        return self._call(self.transfer.operation_mkdir, collection, path)

    def rename(self, collection, old_path, new_path):
        return self._call(self.transfer.operation_rename, collection, old_path, new_path)

    def submission_id(self):
        return self._call(self.transfer.get_submission_id)["value"]

    def submit(self, body):
        """POST /transfer once, without the SDK's own resends (review GLO-03): the flow counts and records
        every POST, and sends the same body again itself when an answer is lost."""
        with self.transfer.retry_config.tune(max_retries=0):
            return self._call(self.transfer.submit_transfer, copy.deepcopy(body))

    def get_task(self, task_id):
        return self._call(self.transfer.get_task, task_id)

    def successful_transfers(self, task_id, marker=None):
        options = {"marker": marker} if marker else {}
        return self._call(self.transfer.task_successful_transfers, task_id, **options)

    def task_list(self, filter, limit=10):  # noqa: A002 - the SDK's name
        return list(self._call(self.transfer.task_list, filter=filter, limit=limit).get("DATA") or [])

    def cancel(self, task_id):
        return self._call(self.transfer.cancel_task, task_id)

    def task_events(self, task_id, limit=10):
        return list(self._call(self.transfer.task_event_list, task_id, limit=limit).get("DATA") or [])


# ---------------------------------------------------------------------- the worker's entry points


def make_client(ctx):
    """The client of this upload: the demo's stand-in, or the live Globus client."""
    used = ctx.settings_used
    if not ctx.demo:
        return LiveGlobus(used)
    from . import arc_demo

    record = ctx.read()
    fault = record.get("demo_fault")
    if fault not in DEMO_FAULTS or _globus(record).get("demo_fault_used"):
        fault = None

    def fault_used(name):
        ctx.update(lambda current: _globus(current).update(demo_fault_used=name))
        log.info("the demo fault %s was shown", name)

    return arc_demo.DemoGlobus(ctx.demo_root, used.get("collection_root") or "/", mac_root=ctx.home,
                               mac_collection=used["mac_collection"], arc_collection=used["arc_collection"],
                               fault=fault, on_fault=fault_used, pace=arc_worker.demo_pace())


def _run(ctx, action):
    # The record's values are checked before anything signs in or is sent (design 20, review SEC-07).
    check_record(ctx, ctx.read())
    flow = GlobusFlow(ctx, make_client(ctx))
    try:
        try:
            outcome = getattr(flow, action)()
        except _Finished as finished:
            outcome = finished.outcome
        return flow.late_stop(outcome)
    finally:
        # From here on (signing out), a person's stop is left to a stop worker by the worker's end, unless
        # this flow already tried to stop (review GLO-V02).
        ctx.stop_tried = flow.stop_tried
        flow.end()


def send(ctx):
    """Action send (design 10.2 to 10.7): sign in, check, submit, watch, verify and place."""
    return _run(ctx, "send")


def check(ctx):
    """Action check (design 10.8): how the upload stands; nothing is sent."""
    return _run(ctx, "check")


def stop(ctx):
    """Action stop (design 10.8): settle the request and cancel any task."""
    return _run(ctx, "stop")
