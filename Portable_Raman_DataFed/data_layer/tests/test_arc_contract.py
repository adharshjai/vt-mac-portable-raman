"""Uploads to ARC: what the data layer, PROTOCOL.md, the Studio page and the README say must agree.

The state labels are made in one place, the data layer (arc_records.STATE_LABELS); the window shows
them as they come, and the Studio's demo engine keeps a copy for its re-enactment. PROTOCOL.md
section 5.6 documents them. This test ties the three together, checks that every reason a command
or the upload program can give is documented, that the arc-* commands are part of the command line,
and that no text gives an example VT username.

It reads files only. It passes once the data layer, the window, the Studio and the documentation of
the feature are all in place.
"""

import argparse
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
JOB_README = support.DATA_LAYER / "arc" / "README.md"
STUDIO = EDITION / "studio" / "index.html"

# The stored states of an upload (PROTOCOL.md 5.6). Their labels are compared below, not copied here.
STATES = ("queued", "waiting_for_terminal", "preparing", "signing_in", "checking", "submitting", "transferring",
          "verifying", "placing", "uncertain", "conflict", "verified", "failed", "stopped")
COMMANDS = ("arc-sessions", "arc-upload", "arc-list", "arc-status", "arc-check", "arc-retry", "arc-stop",
            "arc-keep-copy", "arc-doctor")
# Every `reason` of a refusal of a command and of an upload program's result.
REASONS = (
    "unknown_session", "unknown_upload", "no_session_json", "unreadable_session", "no_points", "running", "settling",
    "future_time", "odd_name", "link", "special_file", "name_clash", "simulated_not_allowed", "simulation_unknown",
    "mixed", "declare_real_not_unknown", "not_demo", "in_progress", "earlier_upload", "already_on_arc",
    "another_live_upload", "busy", "settings_invalid", "mac_collection_missing", "username_missing",
    "known_hosts_unreadable", "globus_sdk_missing", "ssh_missing", "terminal_missing", "not_macos", "not_retryable",
    "not_checkable", "nothing_to_stop", "no_conflict", "payload_missing", "terminal_not_opened",
    "no_space", "changed_during_copy", "payload_changed", "signin_timeout", "no_browser", "signin_failed", "consent",
    "mac_collection_unknown", "gcp_not_connected", "source_not_visible", "source_mismatch", "listing_truncated",
    "arc_folder_not_dir", "destination_permission", "refused_by_globus", "request_conflict", "task_failed",
    "task_not_visible", "verify_mismatch", "ssh_signin_failed", "ssh_host_refused_commands", "ssh_interrupted",
    "terminal_closed", "remote_tools", "remote_failed", "internal",
)


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


# ---------------------------------------------------------------------- reading the documents


def markdown_section(text, heading):
    """The text under a heading that starts with `heading` ("### 5.6"), up to the next heading of the
    same or a higher level. Lines in fenced code blocks (a "# comment" of a script) are no headings."""
    level = heading.split()[0]
    lines = text.splitlines()
    fenced = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and (line.startswith(heading + " ") or line == heading):
            body, inner = [], False
            for following in lines[index + 1:]:
                if following.startswith("```"):
                    inner = not inner
                marks = None if inner else re.match(r"^(#+) ", following)
                if marks and len(marks.group(1)) <= len(level):
                    break
                body.append(following)
            return "\n".join(body)
    raise AssertionError("no section %r" % heading)


def table_rows(text, header_start):
    """The rows of the first Markdown table whose header row starts with `header_start`, as lists of
    cells."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(header_start):
            rows = []
            for row in lines[index + 2:]:
                if not row.startswith("|"):
                    break
                rows.append([cell.strip() for cell in row.strip().strip("|").split("|")])
            return rows
    raise AssertionError("no table starting with %r" % header_start)


def code(cell):
    """`name` -> name; anything else -> None."""
    match = re.fullmatch(r"`([a-z_]+)`", cell)
    return match.group(1) if match else None


def documented_labels():
    """(stored states, display states) of PROTOCOL.md 5.6, each {state: set of labels}."""
    section = markdown_section(PROTOCOL.read_text(encoding="utf-8"), "### 5.6")
    stored = {}
    for cells in table_rows(section, "| state | `state_label` |"):
        state = code(cells[0])
        if state:
            stored[state] = {label.strip() for label in cells[1].split(" / ")}
    display = {}
    for cells in table_rows(section, "| condition | `display_state` | `display_label` |"):
        state = code(cells[1])
        if state:
            display[state] = {cells[2]}
    return stored, display


def normalised(labels):
    """{state: set of labels} from a labels constant: a label, a {transport: label} dict, or a list of
    labels per state; tuple keys (state, transport) count for their state."""
    result = {}
    for key, value in labels.items():
        state = key[0] if isinstance(key, tuple) else key
        if isinstance(value, str):
            found = {value}
        elif isinstance(value, dict):
            found = set(value.values())
        else:
            found = set(value)
        result.setdefault(state, set()).update(found)
    return result


def data_layer_labels(arc_records):
    """{state: set of labels} that the data layer gives: STATE_LABELS, what state_label(state,
    transport) gives for each transport (the label of "transferring" names it), and the labels of
    the display states (DISPLAY_LABELS), where the module has them."""
    found = normalised(arc_records.STATE_LABELS)
    state_label = getattr(arc_records, "state_label", None)
    if callable(state_label):
        for state in STATES:
            for transport in ("globus", "ssh"):
                found.setdefault(state, set()).add(state_label(state, transport))
    for state, labels in normalised(getattr(arc_records, "DISPLAY_LABELS", {})).items():
        found.setdefault(state, set()).update(labels)
    return found


# ---------------------------------------------------------------------- reading the Studio page

_TOKEN = re.compile(r"""\s*(?:(?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")|(?P<word>[A-Za-z_$][\w$]*)"""
                    r"""|(?P<punct>[{}:,\[\]])|(?P<other>.))""", re.S)


def _tokens(text, start):
    position = start
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            return
        position = match.end()
        kind = match.lastgroup
        value = match.group(kind)
        if kind == "string":
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        yield kind, value


def _parse_object(tokens):
    """{key: str, or {key: str}} of a JavaScript object literal whose values are strings or objects of
    strings; None when it holds anything else. The first token, "{", is already taken."""
    result = {}
    for kind, value in tokens:
        if (kind, value) == ("punct", "}"):
            return result
        if (kind, value) == ("punct", ","):
            continue
        if kind not in ("word", "string"):
            return None
        key = value
        kind, value = next(tokens, (None, None))
        if (kind, value) != ("punct", ":"):
            return None
        kind, value = next(tokens, (None, None))
        if kind == "string":
            result[key] = value
        elif (kind, value) == ("punct", "{"):
            inner = _parse_object(tokens)
            if inner is None or not all(isinstance(item, str) for item in inner.values()):
                return None
            result[key] = inner
        else:
            return None
    return None


def studio_labels(page):
    """{state: set of labels} of the object literal on the page that maps the most stored states to
    labels (the Studio engine's copy of the labels), or {} when there is none."""
    best = {}
    for match in re.finditer(r"(?<![\w$])(['\"]?)waiting_for_terminal\1\s*:\s*['\"]", page):
        # The labels object is flat (apart from a {transport: label} value), so its opening brace is
        # the first unmatched "{" before the key.
        depth, start = 0, None
        for position in range(match.start() - 1, -1, -1):
            if page[position] == "}":
                depth += 1
            elif page[position] == "{":
                if depth == 0:
                    start = position
                    break
                depth -= 1
        if start is None:
            continue
        parsed = _parse_object(_tokens(page, start + 1))
        if parsed and len(set(parsed) & set(STATES)) > len(set(best) & set(STATES)):
            best = parsed
    if not best:
        # Or a list of [state, label] pairs.
        pairs = re.findall(r"\[\s*(['\"])(%s)\1\s*,\s*(['\"])(.*?)\3" % "|".join(STATES), page)
        return {state: {label for _q, name, _p, label in pairs if name == state}
                for state in {name for _q, name, _p, _l in pairs}}
    return normalised(best)


# ---------------------------------------------------------------------- the checks


class StateLabels(ArcCase):
    def setUp(self):
        super().setUp()
        self.stored, self.display = documented_labels()

    def compare(self, source, found):
        missing = {state: sorted(self.stored[state] - found.get(state, set()))
                   for state in self.stored if self.stored[state] - found.get(state, set())}
        documented = {state: self.stored.get(state, set()) | self.display.get(state, set())
                      for state in set(self.stored) | set(self.display)}
        undocumented = {state: sorted(labels - documented.get(state, set()))
                        for state, labels in found.items() if labels - documented.get(state, set())}
        self.assertEqual((missing, undocumented), ({}, {}),
                         "%s and PROTOCOL.md 5.6 disagree: (labels PROTOCOL.md has and %s lacks, labels %s has that "
                         "PROTOCOL.md does not document)" % (source, source, source))

    def test_protocol_documents_every_stored_state_once(self):
        self.assertEqual(sorted(self.stored), sorted(STATES))
        for state, labels in self.stored.items():
            self.assertTrue(all(labels), state)
        # One label per state, except the copy itself, whose label names the transport.
        self.assertEqual({state for state, labels in self.stored.items() if len(labels) > 1}, {"transferring"})
        self.assertEqual(self.stored["transferring"], {"Copying with Globus", "Copying over SSH"})
        # check_failed: a verified upload whose newest Check status did not find its copy (fix plan I1).
        self.assertEqual(sorted(self.display), ["check_failed", "interrupted", "not_started", "not_watched",
                                                "unreadable"])

    def test_the_data_layer_makes_the_documented_labels(self):
        arc_records = importlib.import_module("raman_datafed.arc_records")
        self.compare("raman_datafed.arc_records", data_layer_labels(arc_records))

    def test_the_studio_engine_shows_the_documented_labels(self):
        found = studio_labels(STUDIO.read_text(encoding="utf-8"))
        self.assertTrue(found, "studio/index.html has no object literal that maps the upload states (with "
                               "waiting_for_terminal) to their labels")
        self.compare("studio/index.html", found)


class Reasons(ArcCase):
    def test_every_reason_is_documented_in_the_protocol(self):
        text = PROTOCOL.read_text(encoding="utf-8")
        arc_parts = markdown_section(text, "### 5.6") + markdown_section(text, "## 6.")
        missing = [reason for reason in REASONS if "`%s`" % reason not in arc_parts]
        self.assertEqual(missing, [], "reasons missing from PROTOCOL.md 5.6 and 6")

    def test_the_new_kinds_are_documented(self):
        commands = markdown_section(PROTOCOL.read_text(encoding="utf-8"), "## 6.")
        missing = [kind for kind in ("not_ready", "refused", "not_installed") if "`%s`" % kind not in commands]
        missing += [command for command in COMMANDS if "| `%s" % command not in commands]
        self.assertEqual(missing, [], "kinds and command rows missing from PROTOCOL.md 6")


# ---------------------------------------------------------------------- PROTOCOL.md against the code


def documented_refusals():
    """{reason: (kind, error, fix)} of the refusals table of PROTOCOL.md 6 (a row may name several reasons)."""
    section = markdown_section(PROTOCOL.read_text(encoding="utf-8"), "## 6.")
    found = {}
    for cells in table_rows(section, "| kind / reason | error | fix |"):
        kind, _, reasons = cells[0].partition(" / ")
        for reason in re.findall(r"`([a-z_]+)`", reasons):
            found[reason] = (code(kind.strip()), cells[1], cells[2])
    return found


def documented_results():
    """{reason: (message, fix, retryable)} of the table of the upload program's results in PROTOCOL.md 5.6."""
    section = markdown_section(PROTOCOL.read_text(encoding="utf-8"), "### 5.6")
    return {code(cells[0]): (cells[1], cells[2], cells[3])
            for cells in table_rows(section, "| reason | message | fix | retryable |")}


def documented_pattern(cell):
    """A regular expression for a text of PROTOCOL.md: each `<placeholder>` stands for any words; a closing
    note in brackets that only names keys ("(`retry_after_seconds`)") is not part of the text."""
    cell = re.sub(r"\s*\((?:`[a-z_]+`(?:, )?)+\)$", "", cell)
    parts = re.split(r"`?<[^<>]*>`?", cell)
    return "^" + ".+".join(re.escape(part.replace("`", "")) for part in parts) + "$"


def filled(template):
    """A text of the code with each {placeholder} filled."""
    return re.sub(r"\{[^{}]*\}", "X", template)


class ProtocolNamesWhatTheCodeDoes(ArcCase):
    """PROTOCOL.md 5.6 and 6 name every state, label and reason the data layer uses, with its words, and
    nothing it does not use."""

    def setUp(self):
        super().setUp()
        self.records = importlib.import_module("raman_datafed.arc_records")

    def test_the_states_and_display_states_are_the_code_s(self):
        stored, display = documented_labels()
        self.assertEqual(tuple(self.records.STATES), STATES)
        self.assertEqual(sorted(stored), sorted(self.records.STATES))
        self.assertEqual(sorted(display), sorted(self.records.DISPLAY_LABELS))

    def test_every_refusal_of_the_code_and_no_other(self):
        documented = documented_refusals()
        self.assertEqual(sorted(documented), sorted(self.records.REFUSALS),
                         "PROTOCOL.md 6 and arc_records.REFUSALS name other reasons")
        for reason, (kind, error, fix) in self.records.REFUSALS.items():
            with self.subTest(reason=reason):
                doc_kind, doc_error, doc_fix = documented[reason]
                self.assertEqual(doc_kind, kind)
                if error != "{message}":
                    self.assertRegex(filled(error), documented_pattern(doc_error))
                self.assertRegex(filled(fix), documented_pattern(doc_fix))

    def test_every_result_of_the_upload_program_and_no_other(self):
        documented = documented_results()
        self.assertEqual(sorted(documented), sorted(self.records.WORKER_RESULTS),
                         "PROTOCOL.md 5.6 and arc_records.WORKER_RESULTS name other reasons")
        for reason, (message, fix, retryable) in self.records.WORKER_RESULTS.items():
            with self.subTest(reason=reason):
                doc_message, doc_fix, doc_retryable = documented[reason]
                self.assertRegex(filled(message), documented_pattern(doc_message))
                self.assertRegex(filled(fix), documented_pattern(doc_fix))
                self.assertEqual(doc_retryable, "yes" if retryable else "no")

    def test_every_kind_of_a_refusal_is_named(self):
        text = PROTOCOL.read_text(encoding="utf-8")
        opening = markdown_section(text, "## 6.").split("\n\n")[0]
        kinds = {kind for kind, _error, _fix in self.records.REFUSALS.values()}
        self.assertEqual(sorted(kind for kind in kinds if "`%s`" % kind not in opening), [])
        # The kinds of a record's "error": an upload program's result, or a refusal the program met.
        self.assertIn("`kind` is `upload`", markdown_section(text, "### 5.6"))

    def test_the_reader_of_the_tables(self):
        self.assertRegex("Try again in X s.", documented_pattern("Try again in `<N>` s. (`retry_after_seconds`)"))
        self.assertRegex("This upload cannot be X while it is X.", documented_pattern(
            "This upload cannot be `<retried / checked / stopped / kept as a new copy>` while it is `<label>`."))
        self.assertNotRegex("This upload cannot be X.", documented_pattern("This upload can be `<x>`."))
        self.assertEqual(filled("ARC could not {what}: {text}."), "ARC could not X: X.")


class CommandLine(ArcCase):
    def test_a_feature_module_registers_the_nine_commands(self):
        cli = importlib.import_module("raman_datafed.cli")
        self.assertIn("raman_datafed.arc_commands", cli.FEATURE_MODULES)
        registered = set()
        for name in cli.FEATURE_MODULES:
            subparsers = argparse.ArgumentParser(prog="test").add_subparsers()
            importlib.import_module(name).register(subparsers)
            registered.update(subparsers.choices)
        self.assertEqual(sorted(set(COMMANDS) - registered), [])
        # And the command line of the edition loads them.
        parser = cli.build_parser()
        (commands,) = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        self.assertEqual(sorted(set(COMMANDS) - set(commands.choices)), [])


def example_usernames(text):
    """Places where a text gives a VT username instead of a placeholder in <angle brackets>."""
    found = []
    found += re.findall(r"arc_upload\.ssh\.username=(?!<|\s|`|$)\S+", text, re.M)
    found += re.findall(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)*vt\.edu", text)
    found += re.findall(r'"(?:username|ssh_username)":\s*"(?!<)[^"]+"', text)
    found += re.findall(r"\s-l\s+(?!<)[A-Za-z0-9._-]+\s+(?:--\s+)?\S*arc\.vt\.edu", text)
    return found


class NoExampleUsername(ArcCase):
    def test_readme_section_14_names_no_vt_username(self):
        section = markdown_section(README.read_text(encoding="utf-8"), "## 14.")
        self.assertIn("datatransfer.arc.vt.edu", section)
        self.assertEqual(example_usernames(section), [])

    def test_the_protocol_and_the_job_readme_name_none_either(self):
        for path in (PROTOCOL, JOB_README, README):
            with self.subTest(path=path.name):
                self.assertEqual(example_usernames(path.read_text(encoding="utf-8")), [])

    def test_the_check_finds_one(self):
        # The tests' own, plainly made-up name (arc_support.TEST_USERNAME): never an example of a real one.
        made_up = "test_user_0"
        for text in ("config --set arc_upload.ssh.username=" + made_up, "ssh %s@datatransfer.arc.vt.edu" % made_up,
                     '{"username": "%s"}' % made_up, "/usr/bin/ssh -T -l %s -- datatransfer.arc.vt.edu" % made_up,
                     "mail %s@vt.edu" % made_up):
            with self.subTest(text=text):
                self.assertTrue(example_usernames(text))
        for text in ("config --set arc_upload.ssh.username=<name>", "<username>@datatransfer.arc.vt.edu",
                     '{"username": "<pid>"}', "the part of your VT email before @vt.edu", "-l <username> -- <host>"):
            with self.subTest(text=text):
                self.assertEqual(example_usernames(text), [])


class StudioReader(ArcCase):
    """The reader of the Studio's labels object, on small pages."""

    def test_an_object_with_a_transport_label(self):
        page = ("const X = /[{]/; const ARC = { queued: 'Waiting to start', \"waiting_for_terminal\": "
                "\"Waiting for Terminal\", transferring: { globus: 'Copying with Globus', ssh: 'Copying over SSH' }, "
                "'failed': 'Failed' };")
        self.assertEqual(studio_labels(page), {
            "queued": {"Waiting to start"}, "waiting_for_terminal": {"Waiting for Terminal"},
            "transferring": {"Copying with Globus", "Copying over SSH"}, "failed": {"Failed"}})

    def test_a_list_of_pairs(self):
        page = "const ARC = [['queued', 'Waiting to start'], ['transferring', 'Copying over SSH']];"
        self.assertEqual(studio_labels(page), {"queued": {"Waiting to start"}, "transferring": {"Copying over SSH"}})

    def test_the_object_with_the_most_states_wins(self):
        page = ("const TIMES = { waiting_for_terminal: 'x' }; "
                "const LABELS = { queued: 'Waiting to start', waiting_for_terminal: 'Waiting for Terminal' };")
        self.assertEqual(studio_labels(page)["queued"], {"Waiting to start"})


if __name__ == "__main__":
    unittest.main()
