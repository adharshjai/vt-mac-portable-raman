"""Uploads to ARC: what the documentation must say after the review of the feature (fix plan I1 to I12).

README.md (sections 12, 13 and 14), data_layer/PROTOCOL.md (sections 5.6, 6 and 7) and
macos-native/README.md are checked against the code where the code
holds the words or the numbers (the Terminal text of an SSH upload, the snapshot's modes, the
signals, the uploads that need a person, the ssh options, the Studio's settings), and for the steps
a person must find in them (the team lead's chmod 3770, the order of ssh's questions, the shared
Mac, the consent link, the set-aside folder).

It reads files only.
"""

import importlib
import os
import re
import socket
import unittest
from unittest import mock

import support

EDITION = support.DATA_LAYER.parent
PROTOCOL = support.DATA_LAYER / "PROTOCOL.md"
README = EDITION / "README.md"
BUILD_README = EDITION / "macos-native" / "README.md"


def _no_network(*_arguments, **_options):
    raise AssertionError("an ARC test tried to open a network connection")


class ArcCase(unittest.TestCase):
    """Every ARC test runs with RAMAN_ARC_TEST_GUARD=1 (PROTOCOL.md 5.6) and without network."""

    def setUp(self):
        for patcher in (mock.patch.dict(os.environ, {"RAMAN_ARC_TEST_GUARD": "1"}),
                        mock.patch.object(socket.socket, "connect", _no_network),
                        mock.patch.object(socket, "create_connection", _no_network)):
            patcher.start()
            self.addCleanup(patcher.stop)


def section(path, heading):
    """The text under a heading that starts with `heading` ("## 14." or "### 5.6"), up to the next
    heading of the same or a higher level; lines in fenced code blocks are no headings."""
    level = heading.split()[0]
    lines = path.read_text(encoding="utf-8").splitlines()
    fenced = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and (line.startswith(heading + " ") or line == heading):
            body, inner = [], False
            for following in lines[index + 1:]:
                if following.strip().startswith("```"):
                    inner = not inner
                marks = None if inner else re.match(r"^(#+) ", following)
                if marks and len(marks.group(1)) <= len(level):
                    break
                body.append(following)
            return "\n".join(body)
    raise AssertionError("no section %r in %s" % (heading, path.name))


def flat(text):
    """The words of a text on one line: line breaks and indents of the Markdown source are spaces, and
    `**bold**` marks are gone, so that a sentence is found wherever its lines were broken."""
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def part(text, start, end=None):
    """The text from the line starting with `start` up to the next line starting with `end`."""
    begin = text.index(start)
    stop = text.index(end, begin + len(start)) if end else len(text)
    return text[begin:stop]


class ReadmeSection14(ArcCase):
    def setUp(self):
        super().setUp()
        self.text = section(README, "## 14.")
        self.flat = flat(self.text)

    def test_ssh_asks_about_the_host_key_before_the_password_in_the_words_of_the_terminal(self):
        # Review DOC-01, SEC-V02, DOC-05, DOC-06; fix plan I9: the same words as the Terminal text.
        arc_ssh = importlib.import_module("raman_catalog.arc_ssh")
        steps = flat(part(self.text, "**First time, SSH.**", "**Host key.**"))
        host_key = steps.index("(yes/no/[fingerprint])?")
        password = steps.index(arc_ssh.PASSWORD_STEP[0])
        duo = steps.index(flat(" ".join(arc_ssh.DUO_STEP)))
        self.assertLess(host_key, password)
        self.assertLess(password, duo)
        self.assertIn("Type only yes or no here; what you type is shown on screen, so never type your password at "
                      "this question.", flat(" ".join(arc_ssh.HOST_KEY_STEP)))
        self.assertIn("Type only yes or no here; what you type is shown on screen, so never type your password at "
                      "this question.", steps)
        self.assertIn(flat(" ".join(arc_ssh.WARNING_TEXT)), steps)
        self.assertIn(arc_ssh.NETWORK_TEXT, steps)
        self.assertNotIn("type 1", flat(README.read_text(encoding="utf-8")).lower())

    def test_ssh_needs_an_arc_account_in_the_project(self):
        # Review DOC-V03.
        steps = flat(part(self.text, "**First time, SSH.**", "**Host key.**"))
        self.assertIn("An ARC account, and membership in the ARC project that owns the ARC folder", steps)

    def test_the_team_lead_makes_the_shared_folders_group_writable(self):
        # Review DOC-03, SEC-V01; fix plan I7. The fix text of shared_folder_not_writable sends the person here.
        arc_records = importlib.import_module("raman_catalog.arc_records")
        self.assertIn("README section 14", arc_records.WORKER_RESULTS["shared_folder_not_writable"][1])
        lead = part(self.text, "**For the team lead, once.**", "**Not known yet**")
        self.assertIn('chmod 3770 "$d" "$d/_incoming" "$d/simulated" "$d/simulated/_incoming"', lead)
        self.assertIn('ls -ld "$d" "$d/_incoming" "$d/simulated" "$d/simulated/_incoming"', lead)
        self.assertIn("drwxrws--T", lead)
        self.assertIn("3770", flat(part(self.text, "**What goes to ARC and who sees it.**", "**ARC settings**")))

    def test_globus_from_a_shared_mac_is_the_account_that_set_up_globus_connect_personal(self):
        # Review DOC-02, UI-V04, SEC-03.
        shared = flat(part(self.text, "**On a Mac that several people use.**", "**What goes to ARC"))
        self.assertIn("only the Globus account that set up Globus Connect Personal on this Mac can send", shared)
        self.assertIn("sends with SSH", shared)
        # The account is named in the State tool tip and the details; the line above says it only while checking.
        self.assertIn("Signed in as in its details", shared)
        self.assertIn("says it only while the upload checks ARC", shared)
        # Retry same request and Check status sign in with the account saved with the upload.
        self.assertIn("Retry same request and Check status sign in with the VT username saved with the upload", shared)

    def test_the_consent_is_removed_where_globus_documents_it(self):
        # Review DOC-12.
        self.assertIn("https://app.globus.org/settings/identities", self.text)
        self.assertIn("Manage Your Consents", self.text)
        self.assertNotIn("auth.globus.org/consents", README.read_text(encoding="utf-8"))

    def test_retry_same_request_says_what_it_sends_again(self):
        # Review DOC-V02.
        self.assertNotIn("nothing is copied twice", flat(README.read_text(encoding="utf-8")))
        every_day = flat(part(self.text, "**Every day.**", "**On a Mac that several people use.**"))
        self.assertIn("after a transfer that failed it makes a new request, which copies every file again", every_day)

    def test_first_time_globus_has_the_permissions_the_sdk_and_the_collection_path(self):
        # Review DOC-04, DOC-V01, DOC-10, DOC-14.
        steps = flat(part(self.text, "**First time, Globus.**", "**First time, SSH.**"))
        self.assertIn("leave Sharable off", steps)
        self.assertIn("Full Disk Access", steps)
        self.assertIn("run ./data_layer/setup_env.sh once in Portable_Raman_Catalog/", steps.replace("`", ""))
        self.assertIn("Virginia Tech ARC Globus Projects Directories", steps)
        self.assertIn("Folder the collection shows as its top", steps)
        self.assertNotIn("share the deploy folder", README.read_text(encoding="utf-8"))
        self.assertNotIn("share only these two", README.read_text(encoding="utf-8"))

    def test_what_goes_names_the_imported_folder_and_who_reads_it(self):
        # Review COR-02 (fix plan I11) and DOC-03: the Globus side depends on ARC.
        goes = flat(part(self.text, "**What goes to ARC and who sees it.**", "**ARC settings**"))
        self.assertIn("keeps only that folder's name, not this Mac's path to it", goes)
        self.assertIn("adjusted", goes)
        self.assertIn("Over Globus, folders and files get what ARC's collection sets", goes)

    def test_a_failed_check_and_the_uploads_that_need_a_person(self):
        # Fix plan I1 and I2.
        arc_records = importlib.import_module("raman_catalog.arc_records")
        every_day = flat(part(self.text, "**Every day.**", "**On a Mac that several people use.**"))
        self.assertIn(arc_records.DISPLAY_LABELS["check_failed"], every_day)
        self.assertIn("the last %d days needs you" % arc_records.ATTENTION_DAYS, every_day)
        self.assertIn("1 upload to ARC needs you. Press to see it.", every_day)


class ReadmeOtherSections(ArcCase):
    def test_a_check_that_could_not_look_is_explained(self):
        # Review REG-01: what a check that could not look means and what to do.
        trouble = flat(section(README, "## 12."))
        self.assertIn('"Check status could not look." The check did not get to the copy', trouble)
        self.assertIn("the upload stays On ARC, checked", trouble)
        self.assertIn("Upload to ARC still says the session is on ARC.", trouble)
        every_day = flat(section(README, "## 14."))
        self.assertIn("A check that could not look (a sign-in that did not happen, ARC or Globus out of reach) "
                      "changes nothing", every_day)

    def test_troubleshooting_names_the_new_results(self):
        # Review DOC-05, DOC-06, DOC-13, DOC-V01, SSH-V02, DOC-02; fix plan I7.
        trouble = flat(section(README, "## 12."))
        for words in ("The Globus collection in ARC settings belongs to another Globus account",
                      "that shared folder is not group-writable", "post-quantum key exchange",
                      "ARC lacks a tool the upload needs", "ARC refuses VT Open WiFi", "On ARC, last check failed",
                      "Full Disk Access", "an ARC account in the project"):
            with self.subTest(words=words):
                self.assertIn(words, trouble)

    def test_a_set_aside_folder_with_a_kept_upload_copy_can_be_deleted(self):
        # Review DOC-08; fix plan I12: kept copies are read-only files in folders that can be deleted.
        arc_payload = importlib.import_module("raman_catalog.arc_payload")
        self.assertEqual((arc_payload.FILE_MODE, arc_payload.FOLDER_MODE), (0o400, 0o700))
        trouble = section(README, "## 12.")
        self.assertIn("chmod -R u+w set_aside_<stamp>\n  rm -r set_aside_<stamp>", trouble)

    def test_section_13_says_which_commands_for_uploads_to_arc_were_run(self):
        # Review DOC-09.
        verified = flat(section(README, "## 13."))
        self.assertIn("The command-line examples for uploads to ARC of sections 3, 12 and 14, run as written", verified)
        self.assertIn("arc-upload --live --dry-run", verified.replace("`", ""))
        self.assertIn("a live arc-upload among them", verified.replace("`", ""))


class ProtocolSection56(ArcCase):
    def setUp(self):
        super().setUp()
        self.text = flat(section(PROTOCOL, "### 5.6"))

    def test_signals_end_a_worker_without_a_final_state(self):
        # Review COR-01; fix plan I8.
        arc_worker = importlib.import_module("raman_catalog.arc_worker")
        self.assertIn("128 + N when signal N ended it without a final state (143 SIGTERM", self.text)
        self.assertIn(arc_worker.ENDED_BY_SIGNAL_TEXT.format(signal="`<signal>`"), self.text)
        self.assertIn("only a person's stop may cancel a Globus task", self.text)

    def test_the_snapshot_modes_and_the_adjusted_session_json(self):
        # Fix plan I11 and I12.
        arc_payload = importlib.import_module("raman_catalog.arc_payload")
        self.assertIn("Files %04o and folders %04o" % (arc_payload.FILE_MODE, arc_payload.FOLDER_MODE), self.text)
        self.assertIn(arc_payload.SOURCE_FOLDER_SHORTENED, self.text)
        self.assertNotIn("0444", self.text)

    def test_the_shared_folders_on_arc_and_the_new_copy_names(self):
        # Fix plan I4 and I7.
        self.assertIn("gets mode 3770", self.text)
        self.assertIn("drwxrws--T", self.text)
        self.assertIn("first free name of `<session folder>`, `<session folder>_copy2` ... `_copy99`", self.text)
        self.assertIn("`tools`, `prepare`, `access`, `receive`, `verify`, `place`", self.text)

    def test_the_uploads_that_need_a_person_are_the_code_s(self):
        # Fix plan I2.
        arc_records = importlib.import_module("raman_catalog.arc_records")
        attention = part(self.text, "Uploads that need a person", "Locks and writes.")
        for state in arc_records.ATTENTION_STATES:
            with self.subTest(state=state):
                self.assertIn("`%s`" % state, attention)
        self.assertIn("less than %d days old" % arc_records.ATTENTION_DAYS, attention)
        commands = flat(section(PROTOCOL, "## 6."))
        self.assertIn("`arc_attention`", commands)
        self.assertIn("`attention`", commands)

    def test_the_listing_s_and_the_terminal_s_messages_are_the_code_s(self):
        # Review SSH-V02, SSH-01, SSH-03, GLO-V03, COR-05: what the listing and the Terminal window say.
        arc_records = importlib.import_module("raman_catalog.arc_records")
        for key in ("not_started_ssh", "not_started", "not_watched", "not_watched_demo", "interrupted",
                    "signing_in_ssh"):
            with self.subTest(message=key):
                self.assertIn(flat(arc_records.MESSAGES[key].replace("{", "`<").replace("}", ">`")), self.text)
        self.assertIn(flat(arc_records.CHECK_FAILED_TERMINAL), self.text)
        self.assertIn(flat(arc_records.CHECK_STOPPED_TERMINAL), self.text)
        self.assertIn(arc_records.message("stopped", True, incoming="`<incoming>`"), self.text)

    def test_a_check_that_could_not_look_and_its_fixes(self):
        # Review REG-01: the entry's keys, what it changes (nothing), and the fix of each reason, word for word.
        arc_records = importlib.import_module("raman_catalog.arc_records")
        self.assertIn('`checks`: `[{"utc", "ok", "looked", "message", "fix"}]`', self.text)
        self.assertIn("never makes an upload `check_failed`, never counts for attention and never lets "
                      "`arc-upload` go ahead without `--new`", self.text)
        fixes = {cells[0].strip("` "): cells[1].strip() for cells in (
            line.strip("|").split(" | ") for line in section(PROTOCOL, "### 5.6").splitlines()
            if line.startswith("| `") and line.count(" | ") == 1)}
        for reason, fix in arc_records.CHECK_FIXES.items():
            with self.subTest(reason=reason):
                self.assertEqual(fixes.get(reason), fix)
        self.assertIn("| any other | %s |" % arc_records.CHECK_AGAIN_FIX, section(PROTOCOL, "### 5.6"))
        self.assertIn(arc_records.CHECK_NOT_LOOKED, self.text)

    def test_a_check_has_its_own_words_and_a_stopped_check_keeps_the_upload(self):
        # Reviews R2-01, R2-03 and R2-06: the check's own texts word for word, the folders a check must be able to
        # read, and a stop of a check that checks or places the copy of a Globus transfer that has ended.
        arc_globus = importlib.import_module("raman_catalog.arc_globus")
        arc_ssh = importlib.import_module("raman_catalog.arc_ssh")
        for text in (arc_ssh.NOT_READABLE.format(path="`<folder>`"),
                     arc_ssh.CHECK_FOLDER_REFUSED.format(why="`<folder>` is a link"),
                     arc_ssh.CHECK_TOOLS.format(host="`<host>`", why="`<text>`"),
                     arc_ssh.CHECK_REFUSED.format(host="`<host>`"),
                     arc_ssh.DEMO_TEXTS[arc_ssh.NOT_READABLE].format(path="`<folder>`"),
                     arc_globus.TEXTS["listing_forbidden"].format(path="`<path>`"),
                     arc_globus.TEXTS["listing_forbidden_demo"].format(path="`<path>`"),
                     arc_globus.TEXTS["check_stopped"]):
            with self.subTest(text=text):
                self.assertIn(text, self.text)
        self.assertIn("Mode `check` then names a folder it must look into but may not read (`FAIL access <folder>`)",
                      self.text)
        # Review R3-03: also every folder in a copy the check compares.
        self.assertIn("in mode `check` the copy and every folder in it must be readable first", self.text)
        self.assertIn("and so must every folder in this upload's folder before it is compared", self.text)
        self.assertIn("also a folder in a copy it compares; `folder_not_readable`", self.text)
        # Reviews R2-01 and R3-01: the stop of a Globus check follows may_run as the check started.
        self.assertIn("also a Globus check that was checking or placing the copy of a transfer that had ended when the "
                      "check started", self.text)
        self.assertIn("A Globus check is stopped as the listing's `may_run` was when the check started", self.text)
        self.assertIn("is stopped as Stop upload stops, to the end of the check", self.text)
        self.assertIn("nothing is cancelled and the upload ends `stopped` with the copy complete in `_incoming`",
                      self.text)
        self.assertIn("but the stop still ends the upload `stopped`", self.text)
        commands = flat(section(PROTOCOL, "## 6."))
        self.assertIn("also while it checks or places the copy of a Globus transfer that had ended when the check "
                      "started, except a Globus check that started while the transfer could still run", commands)
        self.assertIn("A running Check status is stopped as `may_run` was when it started", commands)
        readme = flat(section(README, "## 14."))
        self.assertIn("also when Globus had finished the copy before the check started and the check was checking it "
                      "or putting it in place", readme)
        self.assertIn("is stopped like any other upload, and the Stop question says so", readme)
        # Review R3-02: a demo upload's check looks in the demo folder saved with it.
        self.assertIn("never in the one ARC settings name now, so no fix sends the person to another demo folder",
                      self.text)
        self.assertIn("so choosing another demo folder does not help them", flat(section(README, "## 12.")))

    def test_the_demo_wording_of_the_results(self):
        # Review COR-05: every demo wording of a result, with the demo's own fix where it has one.
        arc_records = importlib.import_module("raman_catalog.arc_records")
        rows = section(PROTOCOL, "### 5.6").split("| reason | demo message | demo fix |", 1)[1]
        for reason, (message, fix) in arc_records.DEMO_RESULTS.items():
            with self.subTest(reason=reason):
                row = re.search(r"^\| `%s` \| (.*) \| (.*) \|$" % reason, rows, re.M)
                self.assertIsNotNone(row, reason)
                self.assertRegex(re.sub(r"\{[^{}]*\}", "X", message),
                                 "^" + ".+".join(re.escape(part.replace("`", "")) for part in re.split(
                                     r"`<[^<>]*>`", row.group(1))) + "$")
                self.assertEqual(row.group(2).replace("`<path>`", "{path}"), fix or "(its own)")

    def test_stop_is_offered_while_any_worker_works(self):
        # Fix plan I3.
        self.assertIn("| any, while a worker is alive (a send, a check or a stop worker) | `stop`", self.text)
        self.assertIn("Check workers never hold back another upload", self.text)

    def test_a_worker_keeps_the_code_it_started_with(self):
        # Review DOC-07; fix plan I10.
        arc_worker = importlib.import_module("raman_catalog.arc_worker")
        for name in arc_worker.TRANSPORT_MODULES:
            with self.subTest(module=name):
                self.assertIn("`%s`" % name.rsplit(".", 1)[1], self.text)
        self.assertIn("a running upload keeps the code it started with", self.text)

    def test_the_drive_flush_and_the_log_filter(self):
        # Fix plan I5; review SEC-08.
        arc_worker = importlib.import_module("raman_catalog.arc_worker")
        self.assertIn("F_FULLFSYNC", self.text)
        for marker in arc_worker.SECRET_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn("`%s`" % marker, self.text)

    def test_the_ssh_command_line_and_the_terminal_text(self):
        # Review SSH-08, DOC-01; fix plan I9.
        arc_ssh = importlib.import_module("raman_catalog.arc_ssh")
        command = section(PROTOCOL, "### 5.6")
        command = command[command.index("/usr/bin/ssh -T -e none"):]
        for option in ("IgnoreUnknown=*",) + tuple(arc_ssh.PINNED_OPTIONS):
            with self.subTest(option=option):
                self.assertIn("-o " + option, command[:command.index("```")])
        host_key = self.text.index("Are you sure you want to continue connecting")
        self.assertLess(host_key, self.text.index(arc_ssh.PASSWORD_STEP[0]))
        self.assertLess(self.text.index(arc_ssh.PASSWORD_STEP[0]), self.text.index(flat(" ".join(arc_ssh.DUO_STEP))))
        self.assertIn(flat(" ".join(arc_ssh.WARNING_TEXT)), self.text)


class ProtocolOtherSections(ArcCase):
    def test_settings_saved_with_invalid_arc_values_elsewhere(self):
        # Review COR-06, SEC-04, SEC-05.
        settings = flat(section(PROTOCOL, "### 5.1"))
        self.assertIn("arc_settings.require_valid_change", settings)
        self.assertIn("`arc-list`, `arc-status`, `arc-stop` and `arc-doctor` still answer", settings)
        self.assertIn("compared without letter case", settings)

    def test_the_studio_s_arc_endpoint_runs_arc_list_alone(self):
        # Review UI-V06, UI-12.
        studio_server = importlib.import_module("raman_catalog.studio_server")
        studio = flat(section(PROTOCOL, "## 7."))
        row = part(studio, "| `GET /api/arc` |", "| `POST /api/arc/upload` |")
        self.assertNotIn('"sessions"', row)
        for name in studio_server.ARC_SETTINGS_SHOWN:
            with self.subTest(setting=name):
                self.assertIn('"%s"' % name, row)
        for transport, faults in studio_server.ARC_FAULTS_OF.items():
            for fault in faults:
                self.assertIn(fault, part(studio, "| `POST /api/arc/upload` |", "| `POST /api/arc/retry`"))
        self.assertIn("Its page draws live uploads, which are made in the window, read-only", studio)


class OtherReadmes(ArcCase):
    def test_the_build_readme_names_the_suites_of_the_uploads_to_arc(self):
        # Review DOC-11.
        build = flat(BUILD_README.read_text(encoding="utf-8"))
        # The full run takes about 15 minutes since the review of the uploads to ARC added its tests.
        for words in ("about 15 minutes", "`studio_arc`", "`arc_ui_tests`", "RAMAN_ARC_TEST_GUARD=1",
                      "The build never installs into an existing `.venv/`"):
            with self.subTest(words=words):
                self.assertIn(words, build)
        self.assertNotIn("about 11 minutes", build)
        self.assertNotIn("about 13 minutes", build)


class FixTexts(ArcCase):
    """The fixes a person reads in the window say what to do in plain words (reviews DOC-13, DOC-V01, DOC-10,
    DOC-V03); PROTOCOL.md gives the same words (test_arc_contract)."""

    def setUp(self):
        super().setUp()
        self.records = importlib.import_module("raman_catalog.arc_records")
        self.fixes = {reason: row[2] for reason, row in self.records.REFUSALS.items()}
        self.fixes.update((reason, row[1]) for reason, row in self.records.WORKER_RESULTS.items())

    def test_no_fix_names_a_reason_of_the_tables(self):
        # The reasons that are codes, not words (ssh_host_refused_commands, not "busy").
        reasons = {reason for reason in self.fixes if "_" in reason}
        for reason, fix in self.fixes.items():
            with self.subTest(reason=reason):
                self.assertFalse([name for name in reasons if re.search(r"\b%s\b" % name, fix)], fix)
        self.assertEqual(self.fixes["remote_tools"], self.fixes["ssh_host_refused_commands"])
        self.assertIn("ARC settings > More > SSH computer: tinkercliffs1.arc.vt.edu", self.fixes["remote_tools"])

    def test_the_deploy_folder_is_added_to_the_access_list_never_shared(self):
        fix = self.fixes["source_not_visible"]
        self.assertIn("add the deploy folder to the list (leave Sharable off)", fix)
        self.assertNotIn("share", fix.replace("Sharable", ""))
        self.assertIn("Full Disk Access", fix)

    def test_setup_env_is_run_in_its_folder(self):
        folder = "in Portable_Raman_Catalog/ (from deploy-macos/, cd .. first), run ./data_layer/setup_env.sh"
        for fix in (self.fixes["globus_sdk_missing"], self.records.SETUP_ENV_FIX):
            self.assertIn(folder, fix)

    def test_a_failed_ssh_sign_in_names_the_arc_account(self):
        fix = self.fixes["ssh_signin_failed"]
        self.assertIn("that you have an ARC account in the project that owns the ARC folder", fix)
        self.assertIn(importlib.import_module("raman_catalog.arc_ssh").NETWORK_TEXT, fix)


if __name__ == "__main__":
    unittest.main()
