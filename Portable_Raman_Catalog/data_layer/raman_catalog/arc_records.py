"""Records of uploads to ARC (PROTOCOL.md 5.6): HOME/data_catalog/arc_uploads/<upload id>/.

One folder per upload, holding record.json (schema raman-arc-upload/1), events.jsonl, the locks
and, while it is kept, the snapshot of the session (payload/). This module also holds the states
and their labels, the allowed state changes (TRANSITIONS), what the listing shows while nobody
works on an upload, the actions a person may take in each state, and the texts of every refusal
and worker result (design Appendix A).

Locks (flock, so they go away with the process that held them):
- requests.lock: held by arc-upload while it looks for earlier uploads and creates one.
- <id>/record.lock: every read-modify-write of record.json, by any process (milliseconds).
- <id>/worker.lock: held by the one process that works on the upload, for as long as it does.

Standard library and the package only: arc_commands and the window's status use this module.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path

from . import arc_paths
from .config import DataLayerError, utc_now

SCHEMA = "raman-arc-upload/1"
SCHEMA_PREFIX = "raman-arc-upload/"
FOLDER_NAME = "arc_uploads"
RECORD_FILE = "record.json"
RECORD_LOCK = "record.lock"
WORKER_LOCK = "worker.lock"
EVENTS_FILE = "events.jsonl"
REQUESTS_LOCK = "requests.lock"
PAYLOAD_FOLDER = "payload"
MANIFEST_NAME = "raman_upload_manifest.json"
SUMS_NAME = "SHA256SUMS"
TERMINAL_SCRIPT = "ssh_upload.command"
WORKER_LOG = "worker.log"
CREATING_PREFIX = ".creating-"
# Always used with fullmatch(): in Python "$" also matches before a newline at the end.
UPLOAD_ID = re.compile(r"^u[0-9a-f]{12}$")

# The demo Globus service's two collections (design 12): this Mac (absolute paths) and ARC. A demo
# Globus record keeps them in settings_used as mac_collection and arc_collection.
DEMO_MAC_COLLECTION = "00000000-0000-4000-8000-0000000000a1"
DEMO_ARC_COLLECTION = "00000000-0000-4000-8000-0000000000a2"

RECORD_LOCK_SECONDS = 5
REQUESTS_LOCK_SECONDS = 10
# A queued upload whose worker has not started after this long is shown as not started.
NOT_STARTED_SECONDS = 120
EVENTS_SHOWN = 30
# Uploads that need a person (status.arc_attention, arc-list "attention"): these display states, when
# the upload changed less than ATTENTION_DAYS ago.
ATTENTION_DAYS = 7
ATTENTION_STATES = ("failed", "conflict", "uncertain", "interrupted", "not_started", "not_watched", "check_failed")
# The file that marks a folder the data layer made as the demo's stand-in for ARC (ensure_demo_root).
DEMO_MARKER = ".raman_arc_demo"
DEMO_MARKER_TEXT = ("This folder stands in for ARC in the demo of the Portable Raman controller. It holds copies of "
                    "scan sessions. It may be deleted when no demo upload needs it.\n")

# ---------------------------------------------------------------------- states

WORKING_STATES = ("queued", "waiting_for_terminal", "preparing", "signing_in", "checking", "submitting",
                  "transferring", "verifying", "placing")
WAITING_STATES = ("uncertain", "conflict")
FINAL_STATES = ("verified", "failed", "stopped")
STATES = WORKING_STATES + WAITING_STATES + FINAL_STATES
UNFINISHED_STATES = WORKING_STATES + WAITING_STATES

# The State column of the window and the Studio; neither makes labels of its own. "transferring"
# depends on the transport: TRANSFERRING_LABELS (state_label() picks).
STATE_LABELS = {
    "queued": "Waiting to start",
    "waiting_for_terminal": "Waiting for Terminal",
    "preparing": "Preparing the files",
    "signing_in": "Signing in",
    "checking": "Checking ARC",
    "submitting": "Handing over to Globus",
    "transferring": "Copying with Globus",
    "verifying": "Checking the copy on ARC",
    "placing": "Putting the copy in place",
    "uncertain": "Not sure Globus got it",
    "conflict": "Name already on ARC",
    "verified": "On ARC, checked",
    "failed": "Failed",
    "stopped": "Stopped",
}
TRANSFERRING_LABELS = {"globus": "Copying with Globus", "ssh": "Copying over SSH"}
# Shown, never stored (display()): while nobody works on an upload in a working state; a verified upload
# whose newest check failed; a record that cannot be read.
DISPLAY_LABELS = {"not_watched": "Not being watched", "not_started": "Not started", "interrupted": "Interrupted",
                  "check_failed": "On ARC, last check failed", "unreadable": "Cannot be read"}

GLOBUS_PATH = ("queued", "preparing", "signing_in", "checking", "submitting", "transferring", "verifying", "placing",
               "verified")
SSH_PATH = ("queued", "waiting_for_terminal", "preparing", "signing_in", "checking", "transferring", "verifying",
            "placing", "verified")
_AFTER_SIGN_IN = ("signing_in", "checking", "submitting", "transferring", "verifying", "placing", "verified")


def _transitions():
    pairs = {(None, "queued")}
    for path in (GLOBUS_PATH, SSH_PATH):
        pairs.update(zip(path, path[1:]))
    # After signing in, a worker may move forward over steps that are already done: a resumed upload
    # (resume.at), and an SSH run whose remote script reports what was left to do.
    for index, earlier in enumerate(_AFTER_SIGN_IN):
        pairs.update((earlier, later) for later in _AFTER_SIGN_IN[index + 1:])
    pairs.update({("checking", "conflict"), ("placing", "conflict"), ("submitting", "uncertain")})
    for state in WORKING_STATES:
        # An error, a stop, and Retry same request of an upload whose worker is gone.
        pairs.update({(state, "failed"), (state, "stopped"), (state, "queued"), (state, "waiting_for_terminal")})
    for state in ("uncertain", "conflict", "failed", "stopped"):
        pairs.update({(state, "queued"), (state, "waiting_for_terminal")})
    pairs.update({("conflict", "stopped"), ("uncertain", "stopped")})
    # A check worker that found the transfer running or finished, or the copy already in place.
    for state in ("uncertain", "failed", "stopped"):
        pairs.update((state, later) for later in ("transferring", "verifying", "placing", "verified"))
    # A check or stop worker for which Globus no longer shows the transfer (task_not_visible).
    pairs.update({("uncertain", "failed"), ("stopped", "failed")})
    # A check of an upload already on ARC: its result goes into "checks".
    pairs.add(("verified", "verified"))
    return frozenset(pairs)


# Every allowed (from, to); set_state() raises for any other. (None, "queued") is the creation.
TRANSITIONS = _transitions()

# ---------------------------------------------------------------------- texts (design Appendix A)

# Fixes that more than one text gives: where setup_env.sh is run (review DOC-10), and the SSH computer to use
# when ARC's transfer host does not run commands or lacks a tool (review DOC-13).
SETUP_ENV_FIX = ("In Terminal, in Portable_Raman_Catalog/ (from deploy-macos/, cd .. first), run "
                 "./data_layer/setup_env.sh once, or send with SSH.")
LOGIN_NODE_FIX = ("Use a login node instead: ARC settings > More > SSH computer: tinkercliffs1.arc.vt.edu (command "
                  "line: config --set arc_upload.ssh.host=tinkercliffs1.arc.vt.edu), then start a new upload.")

# A.1: reason -> (kind, error, fix). Placeholders in braces; <ID> and <name> are meant literally.
REFUSALS = {
    "unknown_session": ("not_found", "There is no session folder {folder} in scan_sessions.",
                        "Choose a session from the list (command line: arc-sessions)."),
    "unknown_upload": ("not_found", "There is no upload {id} on this Mac.",
                       "Choose an upload under Recent uploads (command line: arc-list)."),
    "no_session_json": ("metadata", "{folder} has no session.json, so this edition did not record it.",
                        "Add it with Import scan folder in the Data tab (command line: import-session --folder "
                        "{folder}), then upload."),
    "unreadable_session": ("metadata", "session.json of {folder} cannot be read: {why}.",
                           "Nothing was sent. Check that file."),
    "no_points": ("not_ready", "{folder} has no saved spectrum.", "There is nothing to upload."),
    "running": ("not_ready", "{folder} is still being recorded.",
                "Wait until the scan has ended; the chart title then says finished, stopped or failed."),
    "settling": ("not_ready", "This scan ended less than a minute ago, or is still being written.",
                 "Try again in {seconds} s."),
    "future_time": ("not_ready", "A file of this session has a date in the future.", "Check this Mac's clock."),
    "odd_name": ("refused", "This session holds a file (or has a name) that cannot be sent safely: {name}.",
                 "Rename it, then upload."),
    "link": ("refused", "This session folder holds a link ({name}); only plain files are sent.",
             "Replace it with a plain file or move it out of the folder."),
    "special_file": ("refused", "{name} in this session is not a plain file.", "Move it out of the folder."),
    "name_clash": ("refused", "{folder} has a folder of its own named dark, where the upload puts the dark spectra.",
                   "Rename that folder, then upload."),
    "simulated_not_allowed": (
        "refused", "This session was made in simulation; simulated sessions are not sent to ARC.",
        "Tick Demo to try the upload. To send simulated sessions to ARC's simulated/ folder, turn on \"Also send "
        "simulated sessions\" in ARC settings (command line: config --set arc_upload.include_simulated=true)."),
    "simulation_unknown": (
        "refused", "It is not recorded whether this session was made in simulation, so it is handled like a "
        "simulated one.",
        "If it was measured with the real instruments, tick \"Measured with the real instruments\" (command line: "
        "--declare-real). Otherwise turn on \"Also send simulated sessions\" in ARC settings."),
    "mixed": ("refused", "Some points of this session were made in simulation and some were not, so it is not "
              "uploaded.", "Check the session; nothing was sent."),
    "declare_real_not_unknown": (
        "refused", "This session records whether it was simulated; \"measured with the real instruments\" is only "
        "for sessions that do not.", "Upload it without that choice."),
    "not_demo": ("refused", "The Studio acts on demo uploads only.",
                 "Uploads to ARC are handled in the controller window."),
    "in_progress": ("conflict", "This session is being uploaded already.", "Stop that upload first, or wait for it."),
    "earlier_upload": ("conflict", "This session has an earlier upload that ended as {label}.",
                       "Press Retry same request on it, or start a new upload (command line: --new)."),
    "already_on_arc": ("conflict", "{folder} is already on ARC: {path} (checked {date}).",
                       "To see that it is still there, select it under Recent uploads and press Check status. To "
                       "upload it again beside it: --new."),
    "another_live_upload": ("conflict", "Another upload to ARC is running ({folder}).",
                            "Wait for it, or press Stop upload on it."),
    "busy": ("conflict", "The upload program is working on this upload right now.",
             "Wait a moment, or press Stop upload."),
    "settings_invalid": ("config", "The ARC settings are not valid: {problems}.",
                         "Correct them in ARC settings (command line: config --set key=value)."),
    "mac_collection_missing": (
        "config", "This Mac's Globus collection ID is not set.",
        "Open ARC settings and paste it: Globus Connect Personal (its icon in the menu bar) > Settings shows it "
        "(command line: config --set arc_upload.globus.mac_collection=<ID>)."),
    "username_missing": (
        "config", "Your VT username is not set.",
        "Open ARC settings and enter it: the part of your VT email before @vt.edu (command line: config --set "
        "arc_upload.ssh.username=<name>)."),
    "known_hosts_unreadable": ("config", "The team file of ARC's SSH keys cannot be used: {path}.",
                               "Correct the setting, or empty it to use your own known_hosts."),
    "globus_sdk_missing": ("not_installed",
                           "The Globus part of the data layer is not installed (the globus-sdk package).",
                           SETUP_ENV_FIX),
    "ssh_missing": ("not_installed", "/usr/bin/ssh is missing on this Mac.",
                    "Send with Globus; macOS normally has ssh, so ask for help."),
    "terminal_missing": ("not_installed", "Terminal or /usr/bin/open is missing on this Mac.", "Send with Globus."),
    "not_macos": ("not_installed", "Uploads to ARC need macOS in this edition.", "Use Demo to try the steps."),
    "not_retryable": ("command", "This upload cannot be retried while it is {label}.", "(The button is disabled.)"),
    "not_checkable": ("command", "This upload cannot be checked while it is {label}.", "(The button is disabled.)"),
    "nothing_to_stop": ("command", "This upload cannot be stopped while it is {label}.", "(The button is disabled.)"),
    "no_conflict": ("command", "This upload cannot be kept as a new copy while it is {label}.",
                    "(The button is disabled.)"),
    "payload_missing": ("command", "The files of this upload are no longer kept on this Mac.",
                        "Press Upload to ARC to start a new upload."),
    "terminal_not_opened": ("command", "Terminal could not be opened: {message}.",
                            "Double-click ssh_upload.command in the upload's folder, or press Retry same request."),
    # Not in Appendix A: failures every arc-* command can meet.
    "home_missing": ("command", "The HOME folder {home} does not exist.",
                     "Run the command with --home set to the controller's folder."),
    "usage": ("usage", "{message}", "Run the command with -h to see its options."),
    "internal": ("internal", "{message}", "Try again; if it happens again, send this message to the team."),
    "interrupted": ("interrupted", "Interrupted", "Run the command again."),
    "demo_folder_unwritable": ("config", "The demo folder {path} cannot be used: {problem}.",
                               "Choose an empty or new demo folder (command line: config --set "
                               "arc_upload.demo_folder=<folder>)."),
}

# Other wordings of some refusals, for the cases their usual text does not fit: (reason, variant) ->
# (error, fix). The kind and the reason stay the reason's own (refusal(..., variant=...)).
REFUSAL_VARIANTS = {
    # A demo upload: the copy is in the demo folder on this Mac, not on ARC.
    ("already_on_arc", "demo"): (
        "Demo: {folder} is already in the demo folder: {path} (checked {date}).",
        "To see that it is still there, press Check status on it. To upload it again beside it, press Upload again "
        "in the controller window (command line: --new)."),
    # A copy of a session folder (cp -R) holds the same session as the folder that was uploaded.
    ("already_on_arc", "same_session"): (
        "{folder} holds the same session as {other}, which is already on ARC: {path} (checked {date}).",
        "To see that it is still there, select it under Recent uploads and press Check status. To upload it again "
        "beside it: --new."),
    ("already_on_arc", "demo_same_session"): (
        "Demo: {folder} holds the same session as {other}, which is already in the demo folder: {path} (checked "
        "{date}).",
        "To see that it is still there, press Check status on it. To upload it again beside it, press Upload again "
        "in the controller window (command line: --new)."),
    # The upload program works on the upload in a Terminal window (a live SSH upload or its check).
    ("busy", "terminal"): ("The upload program is working on this upload right now, in a Terminal window.",
                           "Finish or close that Terminal window, or press Stop upload."),
    # Check status of a live SSH upload: the Terminal window of the check did not open.
    ("terminal_not_opened", "check"): ("Terminal could not be opened: {message}.",
                                       "Double-click ssh_upload.command in the upload's folder, or press Check "
                                       "status again."),
}

# A.2: reason -> (message, fix, retryable) of a worker's result, the "error" of a record.
WORKER_RESULTS = {
    "no_space": ("This Mac has not enough free disk space to prepare the upload (needs {mb} MB). Nothing was sent.",
                 "Free some space, then press Retry same request.", True),
    "changed_during_copy": ("The session's files kept changing while they were copied. Nothing was sent.",
                            "Wait until nothing writes into the session, then press Retry same request.", True),
    "payload_changed": ("The files kept for this upload were changed on this Mac, so the same request cannot be "
                        "sent.", "Start a new upload.", False),
    "signin_timeout": ("You did not finish signing in within 10 minutes. Nothing was sent.",
                       "Press Retry same request and sign in when the page opens.", True),
    "no_browser": ("No web browser could be opened for the Globus sign-in.",
                   "Use the controller on the Mac's own screen, not over a remote connection, or send with SSH.", True),
    "signin_failed": ("Signing in to Globus did not work: {message}.",
                      "Press Retry same request and sign in with your Virginia Tech identity.", True),
    "consent": ("Globus asked for a permission that was not given. Nothing was sent.",
                "Press Retry same request and allow access to the ARC collection when Globus asks.", True),
    "mac_collection_unknown": ("Globus does not know the collection ID in ARC settings.",
                               "Copy it again from Globus Connect Personal > Settings into ARC settings, then start a "
                               "new upload.", False),
    # Globus (review DOC-02): Globus Connect Personal serves only the Globus account that set it up.
    "mac_collection_not_yours": ("The Globus collection in ARC settings belongs to another Globus account, so "
                                 "{identity} cannot send from it.",
                                 "Only the Globus account that set up Globus Connect Personal on this Mac can send "
                                 "from it. Sign in with that account (turn on \"Ask for the Globus sign-in every "
                                 "time\" in ARC settings), then start a new upload; or send with SSH. Check also that "
                                 "the ID in ARC settings is this Mac's own (Globus Connect Personal > Settings).",
                                 False),
    "gcp_not_connected": ("Globus Connect Personal on this Mac is not running or not connected (or the Mac slept).",
                          "Open it from the menu bar and check that it says Connected, then press Retry same "
                          "request.", True),
    "source_not_visible": ("Globus cannot read the upload copy on this Mac ({path}).",
                           "In Globus Connect Personal > Settings > Access, add the deploy folder to the list (leave "
                           "Sharable off). In System Settings > Privacy & Security > Files and Folders, allow Globus "
                           "Connect Personal to read Downloads (Full Disk Access when it is not listed there). Then "
                           "press Retry same request.", True),
    "source_mismatch": ("Globus sees other files in this Mac's folder than the upload holds ({count} differ, for "
                        "example {file}).",
                        "Check that the collection ID in ARC settings is this Mac's own, then start a new upload.",
                        False),
    "listing_truncated": ("Globus listed only part of the upload copy; nothing was sent.",
                          "Press Retry same request; if it happens again, send with SSH.", True),
    "arc_folder_not_dir": ("The ARC folder {path} is a file on ARC.",
                           "Choose another ARC folder in ARC settings, then start a new upload.", False),
    "destination_permission": ("Your Globus account may not write into {path} on ARC.",
                               "Ask the owner of the project folder to add you on ARC, sign in with your VT identity, "
                               "then press Retry same request.", True),
    "refused_by_globus": ("Globus refused the request: {code}: {message}. Nothing was copied.",
                          "If ARC's project folder is full, ask its owner. Otherwise press Retry same request; if it "
                          "repeats, send this message to the team.", True),
    "request_conflict": ("Globus is still running another transfer of these files to the same folder.",
                         "Wait until that transfer has ended (app.globus.org > Activity shows it), then press Retry "
                         "same request.", True),
    "task_failed": ("Globus could not finish the copy: {description}. Files copied so far are in {incoming} on ARC.",
                    "Press Retry same request: the same files go into the same place.", True),
    "task_not_visible": ("Globus does not show this transfer to {identity} (another Globus identity started it, or "
                         "Globus no longer keeps it).",
                         "Press Check status and sign in with the Globus identity that started this upload (sign out "
                         "at app.globus.org first if the browser signs you in as someone else). If Globus no longer "
                         "keeps the transfer, start a new upload.", True),
    "verify_mismatch": ("The copy on ARC does not match this Mac's list of files (for example {file}). It was not put "
                        "in place.", "Press Retry same request: the files are copied again.", True),
    "ssh_signin_failed": ("Signing in to ARC did not work (ssh ended with code 255). The Terminal window shows ssh's "
                          "own reason. Nothing was changed on ARC.",
                          "Check your VT username in ARC settings, your password and Duo, and that you have an ARC "
                          "account in the project that owns the ARC folder. ARC refuses VT Open WiFi: use eduroam, a "
                          "wired VT connection or the VT VPN. Then press Retry same request. If ssh says the host key "
                          "changed, stop and ask the team: do not continue.", True),
    "ssh_host_refused_commands": ("ARC's transfer host did not run the upload commands.", LOGIN_NODE_FIX, False),
    "ssh_interrupted": ("The SSH connection ended during the upload. Files sent so far are in {incoming} on ARC.",
                        "Press Retry same request: all files are sent again, into the same folder on ARC.", True),
    "terminal_closed": ("The Terminal window was closed before the upload finished.", "Press Retry same request.",
                        True),
    "remote_tools": ("ARC lacks a tool the upload needs: {text}.", LOGIN_NODE_FIX, False),
    "remote_failed": ("ARC could not {what}: {text}.",
                      "Check the space and your permission in the project folder, then press Retry same request.",
                      True),
    # Not in Appendix A (SSH; decision I7 and SSH-06, SSH-10).
    "shared_folder_not_writable": ("Your VT account may not write into {path} on ARC: that shared folder is not "
                                   "group-writable.",
                                   "Ask the folder's owner to run chmod 3770 {path} on ARC (README section 14), or "
                                   "to add you to the ARC project if you are not in it. Then press Retry same "
                                   "request.", True),
    "placed_changed": ("This upload's copy on ARC ({path}) was changed after it was put in place: it no longer "
                       "matches this Mac's list of files (for example {file}).",
                       "This program changed nothing on ARC. Look at that folder there; to send the session again, "
                       "start a new upload (command line: --new).", False),
    "demo_ssh_name": ("This Mac's tar changes accented letters in file names, so demo SSH cannot send {name}: its "
                      "check would fail. Nothing was sent.",
                      "Try the demo with Globus. A live upload over SSH keeps such names as they are.", False),
    "terminal_not_opened": ("Terminal could not be opened ({why}).",
                            "Press Retry same request, or double-click ssh_upload.command in the upload's folder.",
                            True),
    # Retryable: Retry same request resumes from the record (Globus answers Duplicate to a request it has, and the
    # SSH remote script finds what is left), so it is safe after any unexpected end.
    "internal": ("Something went wrong in the upload program ({type}). The record and the log are in {folder}.",
                 "Press Retry same request. If it happens again, send the record folder's worker.log to the team.",
                 True),
    # Not in Appendix A: the session's files no longer say what they said when the upload was made.
    "session_changed": ("This session's files changed after the upload was made: {what}. Nothing was sent.",
                        "Press Upload to ARC to start a new upload of the session as it is now.", False),
    # Not in Appendix A: Globus could not be reached, or gave an answer no other reason covers.
    "globus_error": ("Globus could not be reached, or gave an answer this program does not expect ({what}).",
                     "Check that this Mac is online, then press Retry same request. If it happens again, send this "
                     "message to the team.", True),
}

# Messages of the working and waiting states (Appendix A.2, last paragraph). Demo uploads put
# "Demo: " in front (demo_text()).
MESSAGES = {
    "queued": "Waiting to start.",
    "waiting_for_terminal": "Waiting for the Terminal window to start the upload.",
    "preparing": "Preparing the files: copying the session and its dark spectrum and computing a checksum for every "
                 "file.",
    "signing_in_globus": "A web page has opened for the Globus sign-in. Sign in there; nothing is sent before that.",
    "signing_in_ssh": "Sign in to ARC in the Terminal window. The first time, ssh asks yes or no about ARC's key "
                      "before the password: never type your password there. Then type your VT password and approve "
                      "Duo.",
    "checking_globus": "Signed in to Globus as {identity}. Checking this Mac's files and the ARC folder. If the "
                       "browser asks to allow access to ARC, allow it.",
    "submitting": "Handing the copy to Globus.",
    "verifying": "Checking the copy on ARC.",
    "placing": "Putting the copy in its folder on ARC.",
    "uncertain": "Not sure Globus received the request. Press Retry same request: Globus will not start a second "
                 "transfer.",
    "stopped_nothing_sent": "Stopped before anything was sent.",
    "stopped": "Stopped. Files already copied stay in {incoming} on ARC; nothing was deleted.",
    # Globus (design 10.8): stopped after the transfer had finished, and stopped while the copy was
    # being renamed into place.
    "stopped_complete": "The copy had finished before the stop arrived. It is complete in {incoming} on ARC but was "
                        "not put in place. Retry same request or Check status checks it and puts it in place.",
    "stopped_placing": "Stopped while the copy was being put in place; it may already be in its folder on ARC. Press "
                       "Check status to see.",
    # Globus Connect Personal serves the files only while this Mac is awake (review GLO-V03).
    "not_watched": "Globus keeps copying, but this Mac stopped watching. Press Check status to look; it asks for a "
                   "Globus sign-in. Keep this Mac awake until the copy is done.",
    "not_watched_demo": "Globus keeps copying, but this Mac stopped watching. Press Check status to look.",
    "not_started_ssh": "The Terminal window did not start the upload. If it says \"Operation not permitted\", allow "
                       "Terminal to use the Downloads folder: System Settings > Privacy & Security > Files and "
                       "Folders. Then press Retry same request. If no Terminal window opens, double-click "
                       "ssh_upload.command in the upload's folder.",
    "not_started": "The upload program did not start. Press Retry same request.",
    "interrupted": "The upload program stopped (for example when this Mac restarted). Retry same request continues "
                   "where it stopped.",
    # The demo wording of the texts above that name ARC: the copy is in the demo folder on this Mac, not on ARC
    # (message(key, demo=True) picks "<key>_demo"; review COR-05).
    "checking_globus_demo": "Checking this Mac's files and the demo folder.",
    "verifying_demo": "Checking the copy in the demo folder.",
    "placing_demo": "Putting the copy in its folder in the demo folder.",
    "stopped_demo": "Stopped. Files already copied stay in {incoming}; nothing was deleted.",
    "stopped_complete_demo": "The copy had finished before the stop arrived. It is complete in {incoming} but was not "
                             "put in place. Retry same request or Check status checks it and puts it in place.",
    "stopped_placing_demo": "Stopped while the copy was being put in place; it may already be in its folder. Press "
                            "Check status to see.",
}
# The demo wording of some results (review COR-05): the copy is in the demo folder on this Mac, not on ARC.
# reason -> (message, fix or None for the reason's own fix); worker_error(reason, demo=True) uses them.
DEMO_RESULTS = {
    "task_failed": ("Globus could not finish the copy: {description}. Files copied so far are in {incoming}.", None),
    "verify_mismatch": ("The copy in the demo folder does not match this Mac's list of files (for example {file}). It "
                        "was not put in place.", None),
    "ssh_interrupted": ("The SSH connection ended during the upload. Files sent so far are in {incoming}.",
                        "Press Retry same request: all files are sent again, into the same folder."),
    "remote_failed": ("The demo could not {what} in the demo folder: {text}.",
                      "Check the free space and the permissions of the demo folder, then press Retry same request."),
    "shared_folder_not_writable": ("This Mac's account may not write into {path}, a folder of the demo folder.",
                                   "Make it writable again (in Terminal: chmod u+w {path}). Then press Retry same "
                                   "request: it uses the demo folder saved with the upload."),
    "placed_changed": ("This upload's copy in the demo folder ({path}) was changed after it was put in place: it no "
                       "longer matches this Mac's list of files (for example {file}).",
                       "This program changed nothing in the demo folder. Look at that folder there; to send the "
                       "session again, start a new upload (command line: --new)."),
    "destination_permission": ("This Mac's account may not write into {path} in the demo folder.",
                               "Check the permissions of the demo folder, then press Retry same request."),
    "arc_folder_not_dir": ("{path} in the demo folder is a file, where the ARC folder must be a folder.", None),
}

# What a Terminal window says after Check status of an upload on ARC found it missing or different.
CHECK_FAILED_TERMINAL = ("This upload stays listed as on ARC, with its last check failed. Press Check status to look "
                         "again, or Upload to ARC to send the session again.")
CHECK_STOPPED_TERMINAL = "The check was stopped. Nothing was changed."
# Check status of an upload on ARC that could not look at the copy (review REG-01): a sign-in that did not happen,
# ssh that ended before it signed in, Globus or ARC out of reach, an error of this program. Its "checks" entry has
# "looked": false and a fix (check_note()). It says nothing about the copy: it never makes the upload check_failed,
# never counts for attention and never lifts already_on_arc. Retry same request is not offered for an upload on
# ARC, so these fixes say Check status instead.
CHECK_NOT_LOOKED = "Check status could not look."
CHECK_AGAIN_FIX = "Nothing was changed. Press Check status to look again."
CHECK_FIXES = {
    "signin_timeout": "Press Check status again and sign in when the page opens.",
    "no_browser": "Use the controller on the Mac's own screen, not over a remote connection. Then press Check status "
                  "again.",
    "signin_failed": "Press Check status again and sign in with your Virginia Tech identity.",
    "consent": "Press Check status again and allow access to the ARC collection when Globus asks.",
    "globus_error": "Check that this Mac is online, then press Check status again. If it happens again, send this "
                    "message to the team.",
    "ssh_signin_failed": "Check your password and Duo, and that you have an ARC account in the project that owns the "
                         "ARC folder. ARC refuses VT Open WiFi: use eduroam, a wired VT connection or the VT VPN. Then "
                         "press Check status again. If ssh says the host key changed, stop and ask the team: do not "
                         "continue.",
    "internal": "Press Check status again. If it happens again, send the record folder's worker.log to the team.",
    # The check's own reasons (review R2-03, R2-06). Another check meets the same problem until someone clears it,
    # so these name who can: a folder of the copy this account may not read (Globus answered 403, or the remote
    # script printed FAIL access), a folder on the way to the copy that ARC refused (FAIL prepare: a link), and a
    # computer that cannot run the check. A demo upload takes the "<reason>_demo" fix where there is one. Every
    # check of an upload looks in the demo folder saved with it, so a demo fix never sends the person to another
    # demo folder (review R3-02), and it names this Mac, which runs every check of a demo upload (review R3-04).
    "folder_not_readable": "Ask the team to check that you are still in the ARC project and may read its folder on "
                           "ARC. Then press Check status again.",
    "folder_not_readable_demo": "Make that folder and the folders in it readable again for this Mac's account. Then "
                                "press Check status again.",
    "folder_refused": "Nothing was changed. Ask the team to look at that folder on ARC, then press Check status "
                      "again.",
    "folder_refused_demo": "Nothing was changed. Put the moved folder back in place of the link. Then press Check "
                           "status again.",
    "remote_tools": "Nothing was changed. Ask the team to fix that computer: every check of this upload runs on it. "
                    "Then press Check status again.",
    "remote_tools_demo": "Nothing was changed. Ask the team to fix this Mac: every check of this upload runs on it. "
                         "Then press Check status again.",
    "ssh_host_refused_commands": "Nothing was changed. Ask the team to fix that computer: every check of this upload "
                                 "runs on it. Then press Check status again.",
    "ssh_host_refused_commands_demo": "Nothing was changed. Ask the team to fix this Mac: every check of this upload "
                                      "runs on it. Then press Check status again.",
}
NEWER_NOTE = "Made by a newer version of the data layer; shown only."
# Words that keep their capital after "Demo: ".
_PROPER_WORDS = ("ARC", "Globus", "SSH", "Terminal", "VT", "Duo", "Retry", "Check", "Stop", "Upload", "SHA-256")


class ArcError(DataLayerError):
    """A failure of an arc-* command: `kind` as PROTOCOL.md section 6, plus "reason" (a stable code)
    and "fix" (what to do next), and any other keys, in the failure object (cli.error_payload adds
    `extra` after the four standard keys)."""

    def __init__(self, kind, reason, message, fix, detail="", **extra):
        super().__init__(message, detail)
        self.kind = kind
        self.reason = reason
        self.fix = fix
        self.extra = {"reason": reason, "fix": fix}
        self.extra.update(extra)


class TransitionError(DataLayerError):
    """A state change that TRANSITIONS does not allow: a fault of the program, never of the user."""

    kind = "internal"


class UnreadableRecord(DataLayerError):
    kind = "internal"


def refusal(reason, /, detail="", extra=None, variant=None, **values):
    """The ArcError of an Appendix A.1 reason, its placeholders filled from `values`; `variant` picks
    another wording of the same reason (REFUSAL_VARIANTS)."""
    kind, error, fix = REFUSALS[reason]
    if variant is not None:
        error, fix = REFUSAL_VARIANTS[(reason, variant)]
    return ArcError(kind, reason, error.format(**values), fix.format(**values), detail, **(extra or {}))


def worker_error(reason, /, demo=False, **values):
    """The "error" object of a record for an Appendix A.2 reason: {kind, reason, message, fix, retryable}. A demo
    upload gets the reason's demo wording when it has one (DEMO_RESULTS: the demo folder, not ARC)."""
    message, fix, retryable = WORKER_RESULTS[reason]
    if demo and reason in DEMO_RESULTS:
        message, fix = DEMO_RESULTS[reason][0], DEMO_RESULTS[reason][1] or fix
    return {"kind": "internal" if reason == "internal" else "upload", "reason": reason,
            "message": message.format(**values), "fix": fix.format(**values), "retryable": retryable}


def check_result(ok, message):
    """The "checks" entry of a Check status that looked at the copy on ARC: found as it was sent (`ok`), or
    missing or different. The worker adds "utc"."""
    return {"ok": bool(ok), "looked": True, "message": message, "fix": None}


def check_fix(reason, demo=False):
    """The fix of a Check status that could not look, for `reason`: the check's own one (CHECK_FIXES; a demo upload
    takes its "<reason>_demo" wording where there is one), else CHECK_AGAIN_FIX."""
    if demo and reason and reason + "_demo" in CHECK_FIXES:
        reason += "_demo"
    return CHECK_FIXES.get(reason, CHECK_AGAIN_FIX)


def check_note(message, reason=None, demo=False):
    """The "checks" entry of a Check status that could not look at the copy (review REG-01). `message` says why;
    CHECK_NOT_LOOKED is added when it does not say so. The fix is check_fix(reason, demo). The worker adds
    "utc"."""
    text = " ".join(str(message or "").split())
    if CHECK_NOT_LOOKED not in text:
        text = (text + " " + CHECK_NOT_LOOKED).strip()
    return {"ok": False, "looked": False, "message": text, "fix": check_fix(reason, demo)}


def looked(check):
    """False for a "checks" entry of a Check status that could not look ("looked": false). An entry without the
    key counts as a look."""
    return isinstance(check, dict) and check.get("looked") is not False


def error_of_refusal(error):
    """The "error" object of a record for a refusal the worker met (the session cannot go now)."""
    return {"kind": error.kind, "reason": error.reason, "message": str(error), "fix": error.fix,
            "retryable": error.kind == "not_ready"}


def demo_text(text, demo=True):
    """A message as a demo upload shows it: "Demo: " in front, the first word lowercase unless it is a name (or a
    short form such as SSH: two capitals or more; "A" is a word)."""
    if not demo or not text or text.startswith("Demo: "):
        return text
    first = text.split(" ", 1)[0].rstrip(".,:;")
    short_form = first.isupper() and len(first) > 1
    if first in _PROPER_WORDS or short_form or any(character.isdigit() for character in first):
        return "Demo: " + text
    return "Demo: " + text[0].lower() + text[1:]


def message(key, demo=False, **values):
    """A message of MESSAGES; a demo upload gets its "<key>_demo" wording when there is one, and "Demo: " in front."""
    if demo and key + "_demo" in MESSAGES:
        key += "_demo"
    return demo_text(MESSAGES[key].format(**values), demo)


def state_label(state, transport=None):
    if state == "transferring" and transport in TRANSFERRING_LABELS:
        return TRANSFERRING_LABELS[transport]
    return STATE_LABELS.get(state) or DISPLAY_LABELS.get(state) or str(state)


def check_transition(old, new):
    if (old, new) not in TRANSITIONS:
        raise TransitionError("An upload cannot go from %s to %s" % (old, new),
                              detail="arc_records.TRANSITIONS lists the allowed changes.")


def set_state(record, state, message_text=None):
    """Change a record (a dict) to `state`, checked against TRANSITIONS; the same state is a plain
    update. Clears "resume" once its state is reached. Returns the record."""
    old = record.get("state")
    if state != old:
        check_transition(old, state)
        record["state"] = state
        record["state_since_utc"] = utc_now()
    if message_text is not None:
        record["message"] = message_text
    resume = record.get("resume")
    if isinstance(resume, dict) and resume.get("at") == state:
        record["resume"] = None
    return record


# ---------------------------------------------------------------------- folders and files


def uploads_folder(paths):
    return Path(paths.data_catalog) / FOLDER_NAME


def record_folder(paths, upload_id):
    return uploads_folder(paths) / upload_id


def new_upload_id():
    return "u" + secrets.token_hex(6)


def fsync_folder(folder):
    handle = os.open(str(folder), os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def durable_sync(handle):
    """Make what was written to an open file (or folder) last through a power loss (design 13, row 21).

    On macOS fsync(2) only hands the data to the drive, whose cache may still lose it; the F_FULLFSYNC
    fcntl asks the drive to write it to permanent storage. os.fsync where F_FULLFSYNC is missing or
    refused (another system, a file system without it)."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(handle, full)
            return
        except OSError:
            pass
    os.fsync(handle)


def durable_sync_folder(folder):
    """durable_sync of a folder: a rename or a new file in it lasts through a power loss."""
    handle = os.open(str(folder), os.O_RDONLY)
    try:
        durable_sync(handle)
    finally:
        os.close(handle)


def replace_file(path, data, mode=0o600, durable=False):
    """Replace a file in one step (a temporary file beside it, written, synced, renamed), then sync its
    folder. `durable`: durable_sync for both (what must be on disk before an effect outside this Mac:
    record.json, and globus_request.json before every POST); else os.fsync."""
    path = Path(path)
    sync = durable_sync if durable else os.fsync
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        try:
            os.fchmod(handle, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(handle, view):]
            sync(handle)
        finally:
            os.close(handle)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    (durable_sync_folder if durable else fsync_folder)(path.parent)


def ensure_uploads_folder(paths):
    folder = uploads_folder(paths)
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    return folder


def demo_root_problem(path):
    """Why a folder cannot be the demo's stand-in for ARC, or None. A missing folder must be one this user
    can make; one that exists must be a real folder (not a link) of this user that this user may write,
    empty or holding DEMO_MARKER (a folder this program made). Nothing is changed."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        # Missing: the nearest part of its path that exists must be a folder this user may make folders in.
        parent = path.parent
        while True:
            try:
                above = os.stat(parent)
            except (FileNotFoundError, NotADirectoryError):
                if parent.parent == parent:
                    return "no folder above it exists"
                parent = parent.parent
                continue
            except OSError as error:
                return "%s cannot be read (%s)" % (parent, error.strerror or type(error).__name__)
            if not stat.S_ISDIR(above.st_mode):
                return "%s is not a folder" % parent
            if not os.access(parent, os.W_OK | os.X_OK):
                return "this user may not make a folder in %s" % parent
            return None
    except OSError as error:
        return "it cannot be read (%s)" % (error.strerror or type(error).__name__)
    if stat.S_ISLNK(info.st_mode):
        return "it is a link; name the folder itself"
    if not stat.S_ISDIR(info.st_mode):
        return "it is not a folder"
    if info.st_uid != os.getuid():
        return "it belongs to another user"
    if not os.access(path, os.W_OK | os.X_OK):
        return "this user may not write into it"
    try:
        names = os.listdir(path)
    except OSError as error:
        return "it cannot be read (%s)" % (error.strerror or type(error).__name__)
    if names and DEMO_MARKER not in names:
        return "it already holds other files; choose an empty folder or a new one"
    return None


def ensure_demo_root(path):
    """The demo folder that stands in for ARC (design 12), ready for a demo upload; returns its Path.

    A missing folder is made with mode 0700 and DEMO_MARKER in it. A folder that exists must pass
    demo_root_problem(); it is then used as it is (an empty one gets the marker): the mode of a folder
    this program did not make is never changed. Otherwise the refusal config / demo_folder_unwritable."""
    path = Path(path)
    problem = demo_root_problem(path)
    if problem is not None:
        raise refusal("demo_folder_unwritable", path=str(path), problem=problem)
    made = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(path, 0o700)
        made = True
    except FileExistsError:
        # Made a moment ago by another worker, or there before: look again.
        problem = demo_root_problem(path)
        if problem is not None:
            raise refusal("demo_folder_unwritable", path=str(path), problem=problem) from None
    except OSError as error:
        raise refusal("demo_folder_unwritable", path=str(path),
                      problem=error.strerror or type(error).__name__) from None
    if made:
        # mkdir's mode passes through the umask; this folder was made here, so its mode is set here.
        os.chmod(path, 0o700)
    if not (path / DEMO_MARKER).exists():
        with contextlib.suppress(FileExistsError):
            write_new_file(path / DEMO_MARKER, DEMO_MARKER_TEXT.encode("utf-8"), 0o600)
    return path


def _dumps(record):
    return (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_new_file(path, data, mode=0o600):
    """Write a file that must not exist yet, with `mode`, and fsync it."""
    handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(handle, view)
            view = view[written:]
        os.fsync(handle)
    finally:
        os.close(handle)


def write_private_file(path, data, durable=False):
    """Replace a file atomically with mode 0600 (a copy kept in the record folder). `durable`: synced with
    durable_sync, the file and its folder (globus_request.json before every POST)."""
    replace_file(path, data, 0o600, durable)


def create_record(paths, record, fault=None):
    """Make the record folder of a new upload in one step: first as .creating-<random> holding a
    complete record.json, then renamed to the upload id, so that no reader sees a folder without a
    record. Returns the folder."""
    folder = ensure_uploads_folder(paths)
    target = folder / record["upload_id"]
    if target.exists():
        raise UnreadableRecord("The upload id %s is taken" % record["upload_id"])
    temporary = folder / (CREATING_PREFIX + secrets.token_hex(6))
    os.mkdir(temporary, 0o700)
    try:
        write_new_file(temporary / RECORD_FILE, _dumps(record))
        append_event(temporary, record["state"], record.get("message") or "Upload created.")
        fsync_folder(temporary)
        if fault is not None:
            fault("create_before_rename")
        os.rename(temporary, target)
        fsync_folder(folder)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def remove_stale_creations(paths, older_than_seconds=600):
    """Remove .creating-* folders a crashed arc-upload left (every reader ignores them anyway)."""
    folder = uploads_folder(paths)
    if not folder.is_dir():
        return
    cutoff = time.time() - older_than_seconds
    for entry in os.scandir(folder):
        if entry.name.startswith(CREATING_PREFIX):
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)


def read_record(folder):
    path = Path(folder) / RECORD_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise UnreadableRecord("%s has no record.json" % Path(folder).name) from error
    except (OSError, ValueError, RecursionError) as error:
        raise UnreadableRecord("record.json of %s cannot be read" % Path(folder).name, detail=str(error)) from error
    if not isinstance(record, dict):
        raise UnreadableRecord("record.json of %s does not hold an object" % Path(folder).name)
    problem = shape_problem(record)
    if problem is not None:
        # A hand edit, a partial restore or another writer: listed as unreadable, never acted on (7.2).
        raise UnreadableRecord("record.json of %s does not have the shape of a record" % Path(folder).name,
                               detail=problem)
    return record


# The record keys that the listing, the commands and the worker read, and the types they must have
# (absent or null is always allowed; the readers fall back for those).
_RECORD_TYPES = {
    "schema": str, "upload_id": str, "state": str, "transport": str, "demo": bool, "created_utc": str,
    "updated_utc": str, "state_since_utc": str, "message": str, "error": dict, "warnings": list, "notes": list,
    "resume": dict, "conflict_at": str, "session": dict, "destination": dict, "settings_used": dict,
    "payload": dict, "progress": dict, "requests": dict, "worker": dict, "globus": dict, "ssh": dict,
    "verification": dict, "checks": list,
}
_TEXT_FIELDS = {
    "session": ("folder", "session_id", "sample_id", "kind"),
    "destination": ("key", "arc_folder", "area", "base", "name", "final_path", "incoming_path", "globus_base",
                    "globus_final_path", "globus_incoming_path", "demo_root", "placed_as", "local_final_path"),
    "worker": ("action",),
}


def shape_problem(record):
    """What makes a record unusable for the readers (a key of the wrong type), or None."""
    for key, kind in _RECORD_TYPES.items():
        value = record.get(key)
        if value is not None and (not isinstance(value, kind) or (kind is not bool and isinstance(value, bool))):
            return "%s is not a %s" % (key, {str: "text", bool: "true or false", dict: "group (an object)",
                                             list: "list"}[kind])
    for key, names in _TEXT_FIELDS.items():
        group = record.get(key) or {}
        for name in names:
            if group.get(name) is not None and not isinstance(group[name], str):
                return "%s.%s is not text" % (key, name)
    for key in ("warnings", "notes"):
        if not all(isinstance(entry, str) for entry in record.get(key) or []):
            return "%s holds something that is not text" % key
    if not all(isinstance(entry, dict) for entry in record.get("checks") or []):
        return "checks holds something that is not a group"
    globus = record.get("globus") or {}
    if globus.get("task") is not None and not isinstance(globus["task"], dict):
        return "globus.task is not a group (an object)"
    attempts = globus.get("earlier_attempts")
    if attempts is not None and not (isinstance(attempts, list) and all(isinstance(entry, dict)
                                                                        for entry in attempts)):
        return "globus.earlier_attempts is not a list of groups"
    ssh = record.get("ssh") or {}
    if ssh.get("runs") is not None and not isinstance(ssh["runs"], list):
        return "ssh.runs is not a list"
    return None


def is_current(record):
    return record.get("schema") == SCHEMA


@contextlib.contextmanager
def flock(path, seconds, what):
    """Hold an exclusive flock on `path` (created 0600 when missing), waiting at most `seconds`."""
    handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise ArcError("internal", "internal", "%s is locked by another program" % what,
                                   "Try again in a moment.",
                                   detail="%s was held for more than %d s." % (Path(path).name, seconds)) from None
                time.sleep(0.02)
        yield
    finally:
        # Closing the descriptor releases the lock.
        os.close(handle)


def requests_lock(paths):
    """requests.lock: held by arc-upload while it looks for earlier uploads and creates one."""
    folder = ensure_uploads_folder(paths)
    return flock(folder / REQUESTS_LOCK, REQUESTS_LOCK_SECONDS, "The list of uploads to ARC")


def write_record(folder, record):
    """Replace record.json (atomic; durable_sync of the file and the folder, so that a step written before
    an effect outside this Mac is still there after a power loss). Callers hold record.lock."""
    replace_file(Path(folder) / RECORD_FILE, _dumps(record), 0o600, durable=True)


def update_record(folder, mutate):
    """Read-modify-write of record.json under record.lock. `mutate(record)` changes the dict in place
    (raising leaves the file as it was). Returns the record as written."""
    folder = Path(folder)
    with flock(folder / RECORD_LOCK, RECORD_LOCK_SECONDS, "The record of upload %s" % folder.name):
        record = read_record(folder)
        mutate(record)
        record["updated_utc"] = utc_now()
        write_record(folder, record)
        return record


def append_event(folder, state, text):
    """One line of events.jsonl, appended with a single write (never rewritten)."""
    line = json.dumps({"utc": utc_now(), "state": state, "text": text}, ensure_ascii=False) + "\n"
    handle = os.open(str(Path(folder) / EVENTS_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(handle, line.encode("utf-8"))
    finally:
        os.close(handle)


def read_events(folder, last=EVENTS_SHOWN):
    """The last events; a last line without its newline (being written) is left out."""
    try:
        text = (Path(folder) / EVENTS_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.split("\n")[:-1]
    events = []
    for line in lines:
        with contextlib.suppress(ValueError):
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
    return events[-last:] if last else events


def worker_alive(folder):
    """True while a process holds the upload's worker.lock (tested with LOCK_NB and released at once)."""
    try:
        handle = os.open(str(Path(folder) / WORKER_LOCK), os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            return error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        os.close(handle)


def list_records(paths):
    """[(folder, record or None, error or None)] of every upload folder; names starting with "." are
    folders being made and are left out."""
    folder = uploads_folder(paths)
    if not folder.is_dir():
        return []
    found = []
    for entry in sorted(os.scandir(folder), key=lambda item: item.name):
        if entry.name.startswith(".") or not UPLOAD_ID.fullmatch(entry.name):
            continue
        if not entry.is_dir(follow_symlinks=False):
            continue
        try:
            found.append((Path(entry.path), read_record(entry.path), None))
        except UnreadableRecord as error:
            found.append((Path(entry.path), None, error))
    return found


def count_unfinished(paths):
    """Uploads in a working state, uncertain or in conflict (status.arc_unfinished); reads only the
    schema and state of each record."""
    count = 0
    for _folder, record, _error in list_records(paths):
        if record is not None and str(record.get("schema", "")).startswith(SCHEMA_PREFIX) and \
                record.get("state") in UNFINISHED_STATES:
            count += 1
    return count


def needs_attention(display_state, record, now=None):
    """True for an upload that waits for a person (ATTENTION_STATES) and changed less than ATTENTION_DAYS ago."""
    return display_state in ATTENTION_STATES and \
        _age_seconds(record.get("updated_utc"), now) < ATTENTION_DAYS * 86400


def count_attention(paths, now=None):
    """Uploads, live and demo, that need a person (status.arc_attention, arc-list "attention"): failed, in
    conflict, uncertain, interrupted, not started, not watched, or on ARC with the last check failed, and
    changed less than ATTENTION_DAYS ago. worker.lock is looked at only for working states."""
    count = 0
    for folder, record, _error in list_records(paths):
        if record is None or not str(record.get("schema", "")).startswith(SCHEMA_PREFIX):
            continue
        alive = worker_alive(folder) if record.get("state") in WORKING_STATES else False
        if needs_attention(display(record, alive, now)[0], record, now):
            count += 1
    return count


# ---------------------------------------------------------------------- what the listing shows


def _age_seconds(utc_text, now=None):
    try:
        then = datetime.datetime.strptime(utc_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return float("inf")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - then).total_seconds()


def _object(record, key):
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def last_check(record):
    """The newest entry of "checks" (Check status of an upload already on ARC), or None. It may be a check
    that could not look (looked())."""
    checks = record.get("checks") if isinstance(record.get("checks"), list) else []
    return checks[-1] if checks and isinstance(checks[-1], dict) else None


def last_look(record):
    """The newest entry of "checks" whose Check status looked at the copy, or None (review REG-01)."""
    checks = record.get("checks") if isinstance(record.get("checks"), list) else []
    for entry in reversed(checks):
        if isinstance(entry, dict) and looked(entry):
            return entry
    return None


def check_failed(record):
    """True for a verified upload whose newest Check status that looked found its copy missing or different
    (decision I1). A newer check that could not look changes nothing (review REG-01)."""
    last = last_look(record)
    return record.get("state") == "verified" and last is not None and last.get("ok") is False


# Globus task statuses after which Globus copies nothing more, and the outcomes of a POST after which Globus
# made no task of the request (arc_globus uses both).
GLOBUS_FINAL_STATUSES = ("SUCCEEDED", "FAILED")
GLOBUS_REFUSED_OUTCOMES = ("refused", "conflict")


def globus_refused_for_sure(globus):
    """True when every POST of the current request got a definite refusal: Globus made no task of it."""
    outcomes = [post.get("outcome") for post in globus.get("posts") or [] if isinstance(post, dict)]
    return bool(outcomes) and all(outcome in GLOBUS_REFUSED_OUTCOMES for outcome in outcomes)


def globus_may_run(record):
    """True when Globus may still run a transfer of this upload: its task is not known to have ended, or its
    request number may have reached Globus without a task id being known. Such a transfer is cancelled by a
    stop, also by the stop of a running Check status (the listing's "may_run", review REG-03)."""
    globus = _object(record, "globus")
    if globus.get("task_id"):
        task = globus.get("task") if isinstance(globus.get("task"), dict) else {}
        return task.get("status") not in GLOBUS_FINAL_STATUSES
    return bool(globus.get("submission_id")) and not globus_refused_for_sure(globus)


def display(record, alive, now=None):
    """(display_state, display_label, message) of an upload (7.5). `now`: a UTC datetime, for tests."""
    state = record.get("state")
    transport = record.get("transport")
    demo = bool(record.get("demo"))
    stored = (state, state_label(state, transport), record.get("message"))
    if check_failed(record):
        # Also while a new check runs: until a newer check looks, the newest look is the failed one.
        return "check_failed", DISPLAY_LABELS["check_failed"], last_look(record).get("message") or stored[2]
    if alive or state not in WORKING_STATES:
        return stored
    globus = _object(record, "globus")
    if transport == "globus" and globus.get("task_id") and state in ("transferring", "verifying", "placing"):
        key = "not_watched_demo" if demo else "not_watched"
        return "not_watched", DISPLAY_LABELS["not_watched"], message(key, demo)
    if state in ("queued", "waiting_for_terminal"):
        # /usr/bin/open failed for this attempt: nothing will start it unless the person acts.
        open_failed = state == "waiting_for_terminal" and bool(_object(record, "ssh").get("terminal_open_failed_utc"))
        if not open_failed and _age_seconds(record.get("state_since_utc"), now) <= NOT_STARTED_SECONDS:
            # The worker is starting; it takes worker.lock within moments.
            return stored
        key = "not_started_ssh" if transport == "ssh" and not demo else "not_started"
        return "not_started", DISPLAY_LABELS["not_started"], message(key, demo)
    return "interrupted", DISPLAY_LABELS["interrupted"], message("interrupted", demo)


def payload_made(record):
    return isinstance(record.get("payload"), dict)


def payload_available(record, payload_present=None):
    """False once the snapshot was made and is not kept any more: deleted after "verified", or its folder
    is gone (`payload_present` False: the caller looked at the record folder). A snapshot not made yet is
    made by the next worker."""
    if not payload_made(record):
        return True
    return record["payload"].get("kept") is True and payload_present is not False


def payload_present_in(folder):
    """Whether the record folder holds its snapshot (payload/), for item() and actions()."""
    return (Path(folder) / PAYLOAD_FOLDER).is_dir()


def may_be_on_arc(record):
    """True when something of this upload may be on ARC: Globus has a task for it, or an SSH run got
    past `RAMAN STEP receive` (the transport sets ssh.sent)."""
    globus = record.get("globus") if isinstance(record.get("globus"), dict) else None
    if globus is not None:
        if globus.get("task_id"):
            return True
        return any(isinstance(attempt, dict) and attempt.get("task_id")
                   for attempt in globus.get("earlier_attempts") or [])
    ssh = record.get("ssh") if isinstance(record.get("ssh"), dict) else None
    return bool(ssh and ssh.get("sent"))


def checkable(record):
    """True when Check status can find something of this upload: what may_be_on_arc() says, or a Globus
    request number (the check looks the transfer up by its label)."""
    return may_be_on_arc(record) or bool(_object(record, "globus").get("submission_id"))


def actions(record, display_state, alive, payload_present=None):
    """The buttons a person may use on an upload (7.7): the window and the Studio enable these only.
    `payload_present`: whether the record folder holds the snapshot (None: as the record says)."""
    state = record.get("state")
    if not is_current(record):
        return []
    local = ["show_in_finder" if record.get("demo") else "copy_path"] if state == "verified" else []
    if alive:
        # A worker works on it (a send, a check or a stop worker): Stop asks it to end; nothing else may start.
        return ["stop"] + local
    retry = payload_available(record, payload_present)
    if state in WORKING_STATES:
        if display_state == state:
            # The worker is starting; it takes worker.lock within moments.
            return ["stop"]
        if display_state == "not_watched":
            return ["check", "stop"]
        return (["retry"] if retry else []) + (["check"] if checkable(record) else []) + ["stop"]
    if state == "uncertain":
        return (["retry"] if retry else []) + ["check", "stop"]
    if state == "conflict":
        return (["keep_copy", "retry"] if retry else []) + ["stop"]
    if state in ("failed", "stopped"):
        found = []
        error = _object(record, "error")
        if retry and (state == "stopped" or error.get("retryable") is True):
            found.append("retry")
        if checkable(record):
            found.append("check")
        return found
    if state == "verified":
        return ["check"] + local
    return []


def retry_target(record, display_state):
    """(state, resume.at) for Retry same request (7.7): a pure function of the record.

    Live SSH: a new attempt in a Terminal window (the remote script finds what is left). Demo SSH:
    a detached worker. Globus (live and demo): a detached worker that resumes at `resume.at`;
    "checking" with a task_id known means a new attempt (10.4).
    """
    if not payload_available(record):
        raise refusal("payload_missing")
    if record.get("transport") == "ssh":
        return ("queued" if record.get("demo") else "waiting_for_terminal"), None
    state = record.get("state")
    globus = record.get("globus") if isinstance(record.get("globus"), dict) else {}
    task = globus.get("task") if isinstance(globus.get("task"), dict) else {}
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    if state == "conflict":
        return "queued", ("placing" if record.get("conflict_at") == "placing" else "checking")
    if state == "uncertain":
        return "queued", "submitting"
    if state == "placing":
        return "queued", "placing"
    if error.get("reason") == "verify_mismatch":
        return "queued", "checking"
    if globus.get("task_id"):
        status = task.get("status")
        if status == "FAILED":
            return "queued", "checking"
        if status == "SUCCEEDED":
            return "queued", "verifying"
        return "queued", "transferring"
    if globus.get("submission_id"):
        # Possibly received by Globus: the same body and request number (Globus answers Duplicate).
        return "queued", "submitting"
    return "queued", "checking"


def leftover_on_arc(record):
    """Where the files of a failed or stopped upload that sent something are (the incoming folder)."""
    if record.get("state") not in ("failed", "stopped") or not may_be_on_arc(record):
        return None
    if _object(record, "ssh").get("may_be_placed"):
        # SSH-V01: ARC may have put the copy in its final folder already; it is not left in _incoming.
        return None
    destination = _object(record, "destination")
    if record.get("demo") and destination.get("demo_root") and destination.get("incoming_path"):
        return local_path(destination["demo_root"], destination["incoming_path"])
    return destination.get("incoming_path")


def local_path(demo_root, cluster_path):
    """The folder of the demo root that stands in for a cluster path (arc_paths.demo_local_path)."""
    return str(arc_paths.demo_local_path(demo_root, cluster_path))


def needs(record, display_state, alive):
    state = record.get("state")
    if state in ("conflict", "uncertain"):
        return "decision"
    if record.get("demo"):
        return None
    if record.get("transport") == "globus" and alive and state == "signing_in":
        return "browser"
    if record.get("transport") == "ssh":
        worker = _object(record, "worker")
        if (state == "waiting_for_terminal" and display_state == state) or (alive and state == "signing_in"):
            return "terminal"
        if alive and worker.get("terminal") and worker.get("action") == "check":
            # Check status runs in a Terminal window that waits for the password and Duo.
            return "terminal"
    return None


def item(record, alive, now=None, payload_present=None):
    """The listing item of an upload (arc-list, and "upload" in the other replies). `payload_present`:
    whether the record folder holds the snapshot (payload_present_in(); None: as the record says)."""
    display_state, display_label, shown = display(record, alive, now)
    destination = record.get("destination") if isinstance(record.get("destination"), dict) else {}
    session = record.get("session") if isinstance(record.get("session"), dict) else {}
    settings_used = record.get("settings_used") if isinstance(record.get("settings_used"), dict) else {}
    globus = record.get("globus") if isinstance(record.get("globus"), dict) else {}
    worker = record.get("worker") if isinstance(record.get("worker"), dict) else {}
    ssh_target = None
    if record.get("transport") == "ssh" and not record.get("demo") and settings_used.get("ssh_username"):
        ssh_target = "%s@%s:%s" % (settings_used["ssh_username"], settings_used.get("ssh_host"),
                                   destination.get("final_path"))
    checks = record.get("checks") if isinstance(record.get("checks"), list) else []
    return {
        "upload_id": record.get("upload_id"),
        "created_utc": record.get("created_utc"),
        "updated_utc": record.get("updated_utc"),
        "session_folder": session.get("folder"),
        "session_id": session.get("session_id"),
        "sample_id": session.get("sample_id"),
        "kind": session.get("kind"),
        "area": destination.get("area"),
        "transport": record.get("transport"),
        "demo": bool(record.get("demo")),
        "state": record.get("state"),
        "state_label": state_label(record.get("state"), record.get("transport")),
        "display_state": display_state,
        "display_label": display_label,
        "final": record.get("state") in FINAL_STATES,
        "message": shown,
        "progress": record.get("progress"),
        "destination": {
            "final_path": destination.get("final_path"),
            "globus_final_path": destination.get("globus_final_path"),
            "incoming_path": destination.get("incoming_path"),
            "placed_as": destination.get("placed_as"),
            "ssh_target": ssh_target,
            "local_final_path": destination.get("local_final_path"),
        },
        "worker": {"alive": bool(alive), "action": worker.get("action") if alive else None,
                   "terminal": bool(worker.get("terminal")) if alive else False},
        # Globus may still run a transfer of this upload: a stop cancels it, also the stop of a running check.
        "may_run": record.get("transport") == "globus" and globus_may_run(record),
        "identity": globus.get("identity"),
        "needs": needs(record, display_state, alive),
        "actions": actions(record, display_state, alive, payload_present),
        "error": record.get("error"),
        "warnings": record.get("warnings") or [],
        "notes": (record.get("notes") or []) + ([] if is_current(record) else [NEWER_NOTE]),
        "verification": record.get("verification"),
        "last_check": checks[-1] if checks else None,
        "leftover_on_arc": leftover_on_arc(record),
    }


def unreadable_item(folder, error):
    """The listing item of a record that cannot be read: shown with its error, never acted on."""
    return {
        "upload_id": Path(folder).name, "created_utc": None, "updated_utc": None, "session_folder": None,
        "session_id": None, "sample_id": None, "kind": None, "area": None, "transport": None, "demo": False,
        "state": "unreadable", "state_label": DISPLAY_LABELS["unreadable"], "display_state": "unreadable",
        "display_label": DISPLAY_LABELS["unreadable"], "final": True, "message": str(error), "progress": None,
        "destination": {"final_path": None, "globus_final_path": None, "incoming_path": None, "placed_as": None,
                        "ssh_target": None, "local_final_path": None},
        "worker": {"alive": worker_alive(folder), "action": None, "terminal": False}, "may_run": False,
        "identity": None, "needs": None, "actions": [],
        "error": {"kind": "internal", "reason": "internal", "message": str(error), "fix": "Look at record.json in "
                  "%s." % folder, "retryable": False},
        "warnings": [], "notes": [], "verification": None, "last_check": None, "leftover_on_arc": None,
    }
