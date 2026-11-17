"""Reading scan session folders and building the record metadata of PROTOCOL.md 5.3."""

import hashlib
import json
import unittest

import support
from raman_catalog import metadata
from raman_catalog.metadata import MetadataError

RAW_KEYS = [
    "type", "schema", "capture_id", "session_id", "session_folder", "point_id", "scan_mode", "sample_id",
    "operator", "location", "notes", "timestamp_utc", "started_utc", "capture_elapsed_ms", "integration_time_us",
    "laser_power_level", "position_steps", "position_mm", "pixel_count", "instrument", "simulated", "dark",
    "raw_file", "software",
]


class ReadingFolders(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)

    def test_session_json_is_read_and_checked(self):
        session = support.make_session(self.home, points=1)
        self.assertEqual(metadata.read_session(session["folder"])["session_id"], session["session_id"])

        path = session["folder"] / "session.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        for change, message in (({"schema": "something/9"}, "schema"), ({"session_id": " "}, "session_id"),
                                ({"session_id": "\ud83d"}, "session_id holds a broken character")):
            path.write_text(json.dumps({**content, **change}), encoding="utf-8")
            with self.assertRaisesRegex(MetadataError, message):
                metadata.read_session(session["folder"])
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(MetadataError):
            metadata.read_session(session["folder"])
        path.unlink()
        with self.assertRaisesRegex(MetadataError, "does not exist"):
            metadata.read_session(session["folder"])

    def test_point_files_are_found_in_point_order(self):
        session = support.make_session(self.home, points=0)
        folder = session["folder"]
        for name in ("point_0010.json", "point_0002.json", "point_10000.json", "point_0001.txt", "point_12.json",
                     "xpoint_0003.json", "point_0004.json.bak", "request.txt"):
            (folder / name).write_text("{}", encoding="utf-8")
        (folder / "point_0007.json").mkdir()
        self.assertEqual([path.name for path in metadata.point_files(folder)],
                         ["point_0002.json", "point_0010.json", "point_10000.json"])

    def test_session_folders_do_not_start_with_an_underscore(self):
        scan_sessions = self.home / "scan_sessions"
        (scan_sessions / "_backend_x").mkdir(parents=True)
        (scan_sessions / "2d_scan_1").mkdir()
        (scan_sessions / "_sim_stage.json").write_text("{}", encoding="utf-8")
        self.assertEqual([path.name for path in scan_sessions.iterdir() if metadata.is_session_folder(path)],
                         ["2d_scan_1"])

    def test_point_must_be_a_raw_spectrum(self):
        session = support.make_session(self.home, points=1)
        good = json.loads(session["files"][0].read_text(encoding="utf-8"))
        self.assertEqual(metadata.read_point(session["files"][0])["capture_id"], good["capture_id"])
        broken = [
            ("capture_id is missing", {"capture_id": ""}),
            ("session_id is missing", {"session_id": None}),
            ("point_id is not a whole number", {"point_id": "3"}),
            # Beyond 64 bits: no point number the controller writes, and the ledger could not store it.
            ("point_id is not a whole number", {"point_id": 2 ** 63}),
            ("no intensity values", {"intensity": []}),
            ("status is 'FAIL'", {"status": "FAIL"}),
            # Half of an emoji: the ledger and the record alias are keyed by these two.
            ("capture_id holds a broken character", {"capture_id": "S-\ud83d_1"}),
            ("session_id holds a broken character", {"session_id": "\udc80"}),
        ]
        for message, change in broken:
            session["files"][0].write_text(json.dumps({**good, **change}), encoding="utf-8")
            with self.assertRaisesRegex(MetadataError, message):
                metadata.read_point(session["files"][0])
        session["files"][0].write_text("[1, 2]", encoding="utf-8")
        with self.assertRaisesRegex(MetadataError, "JSON object"):
            metadata.read_point(session["files"][0])


class CaptureBlock(unittest.TestCase):
    def test_block_written_by_the_controller_is_valid(self):
        self.assertEqual(metadata.capture_problems(support.capture_block()), [])
        self.assertEqual(metadata.capture_problems(support.capture_block(dark_file=None, notes="second try")), [])
        block = support.capture_block()
        block["integration_time_us"] = None
        block["instrument"] = None
        self.assertEqual(metadata.capture_problems(block), [])

    def test_each_rule(self):
        self.assertEqual(metadata.capture_problems(None), ["the capture block is missing"])
        cases = {
            "schema": ("schema", "raman-capture/2"),
            "sample_id is empty": ("sample_id", "  "),
            "operator is empty": ("operator", None),
            "location is empty": ("location", ""),
            "notes": ("notes", 5),
            "laser_power_level": ("laser_power_level", "MAX"),
            "integration_time_us": ("integration_time_us", -1),
            "instrument": ("instrument", "C15471"),
            "simulated.backend": ("simulated", {"backend": "yes", "motor": True}),
            "simulated is missing": ("simulated", None),
            "dark_file": ("dark_file", "../dark.txt"),
            "software is missing": ("software", None),
        }
        for expected, (key, value) in cases.items():
            block = support.capture_block()
            block[key] = value
            problems = metadata.capture_problems(block)
            self.assertEqual(len(problems), 1, (key, problems))
            self.assertIn(expected, problems[0])

    def test_imported_block_may_leave_unknown_what_nobody_recorded(self):
        block = support.capture_block()
        block.update(laser_power_level=None, simulated=None, software=None)
        self.assertEqual(len(metadata.capture_problems(block)), 3)
        block["imported"] = True
        self.assertEqual(metadata.capture_problems(block), [])
        block["sample_id"] = ""
        self.assertEqual(metadata.capture_problems(block), ["sample_id is empty"])

    def test_text_with_half_a_character_is_named_and_not_repeated(self):
        # 199 letters and the first half of an emoji: a field of 200 UTF-16 units cut in the middle of one.
        block = support.capture_block(location="L" * 199 + "\ud83d")
        self.assertEqual(metadata.capture_problems(block), ["location holds a broken character"])
        block = support.capture_block(notes="\U0001F52C a whole emoji is fine")
        self.assertEqual(metadata.capture_problems(block), [])
        block["instrument"]["serial"] = "\udc80"
        block["\ud800"] = "x"
        self.assertEqual(metadata.capture_problems(block),
                         ["instrument holds a broken character", "a key holds a broken character"])


class RawMetadata(unittest.TestCase):
    def setUp(self):
        self.home = support.make_home(self)
        self.dark = support.write_dark(self.home)

    def build(self, session, index=0):
        path = session["files"][index]
        return metadata.build_raw_metadata(self.home, session["folder"].name, metadata.read_point(path), path)

    def test_metadata_of_a_scan_point(self):
        session = support.make_session(self.home, points=3, notes="grid A")
        built = self.build(session, 2)
        self.assertEqual(list(built), RAW_KEYS)
        content = session["files"][2].read_bytes()
        self.assertEqual(built, {
            "type": "raman.raw_spectrum", "schema": "raman-capture/1",
            "capture_id": session["capture_ids"][2], "session_id": session["session_id"],
            "session_folder": "1d_x_scan_20261113_101500_123", "point_id": 3, "scan_mode": "1d-x",
            "sample_id": "S-001", "operator": "Test Operator", "location": "Bench 1", "notes": "grid A",
            "timestamp_utc": "2026-11-13T15:15:04.250Z", "started_utc": "2026-11-13T15:15:04.150Z",
            "capture_elapsed_ms": 1100, "integration_time_us": 1000000, "laser_power_level": "HIGH",
            "position_steps": {"x": 138800, "y": 86750, "z": 138800},
            "position_mm": {"x": 4.0, "y": 2.5, "z": 4.0},
            "pixel_count": 2048, "instrument": support.INSTRUMENT, "simulated": True,
            "dark": {"file": support.DARK_NAME, "sha256": hashlib.sha256(self.dark.read_bytes()).hexdigest(),
                     "record": None, "kind": "simulated"},
            "raw_file": {"name": "point_0003.json", "sha256": hashlib.sha256(content).hexdigest(),
                         "bytes": len(content)},
            "software": support.SOFTWARE,
        })
        json.dumps(built)

    def test_position_is_steps_divided_by_34700(self):
        session = support.make_session(self.home, points=1, steps=(34700, 17350, 1))
        built = self.build(session)
        self.assertEqual(built["position_steps"], {"x": 34700, "y": 17350, "z": 1})
        self.assertEqual(built["position_mm"], {"x": 1.0, "y": 0.5, "z": 1 / 34700})

    def test_single_capture_has_null_positions(self):
        session = support.make_session(self.home, folder="single_20261113_101500_123", points=1, scan_mode="single",
                                       steps=None)
        built = self.build(session)
        self.assertEqual((built["position_steps"], built["position_mm"], built["scan_mode"]), (None, None, "single"))

    def test_steps_beyond_64_bits_are_no_position(self):
        # 10**400 steps are no millimetres a float can hold; such a point must not stop a sync.
        session = support.make_session(self.home, points=1, steps=(10 ** 400, 0, 0))
        built = self.build(session)
        self.assertEqual((built["position_steps"], built["position_mm"]), (None, None))

    def test_a_broken_character_outside_the_capture_block_is_named_too(self):
        session = support.make_session(self.home, points=1)
        point = json.loads(session["files"][0].read_text(encoding="utf-8"))
        point.update(scan_mode="1d-x\udc80", started_utc="\ud83d")
        session["files"][0].write_text(json.dumps(point), encoding="utf-8")
        with self.assertRaises(MetadataError) as raised:
            self.build(session)
        self.assertEqual(str(raised.exception), "point_0001.json cannot be registered: scan_mode holds a broken "
                                                "character; started_utc holds a broken character")

    def test_simulated_is_true_when_either_simulator_was_used(self):
        session = support.make_session(self.home, points=1, simulated=False)
        self.assertIs(self.build(session)["simulated"], False)
        point = json.loads(session["files"][0].read_text(encoding="utf-8"))
        for backend, motor in ((True, False), (False, True)):
            point["capture"]["simulated"] = {"backend": backend, "motor": motor}
            session["files"][0].write_text(json.dumps(point), encoding="utf-8")
            self.assertIs(self.build(session)["simulated"], True)

    def test_dark_is_null_without_a_dark_and_has_no_hash_when_the_file_is_not_here(self):
        without = support.make_session(self.home, folder="a", points=1, dark_file=None)
        self.assertIsNone(self.build(without)["dark"])
        elsewhere = support.make_session(self.home, folder="b", points=1, dark_file="dark_other.txt")
        self.assertEqual(self.build(elsewhere)["dark"],
                         {"file": "dark_other.txt", "sha256": None, "record": None, "kind": None})

    def test_invalid_capture_block_says_how_to_supply_the_fields(self):
        session = support.make_session(self.home, points=1, with_capture=False)
        with self.assertRaises(MetadataError) as raised:
            self.build(session)
        self.assertIn("capture block is missing", str(raised.exception))
        self.assertIn("import-session", raised.exception.detail)

    def test_dark_record_metadata(self):
        content = self.dark.read_bytes()
        self.assertEqual(metadata.build_dark_metadata(self.dark), {
            "type": "raman.dark_spectrum", "schema": "raman-dark/1", "file": support.DARK_NAME,
            "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content), "simulated": True})
        real = support.write_dark(self.home, name="dark_20260921_153016_700.txt", simulated=None)
        self.assertIs(metadata.build_dark_metadata(real)["simulated"], False)
        metadata.dark_description(real).write_text("{half", encoding="utf-8")
        self.assertIsNone(metadata.build_dark_metadata(real)["simulated"])

    def test_metadata_a_catalog_record_cannot_take_is_refused_naming_the_field(self):
        session = support.make_session(self.home, points=1, notes="\u4e2d" * 17000)
        with self.assertRaises(MetadataError) as raised:
            self.build(session)
        # Sent with ASCII escapes, each of these characters takes six bytes.
        self.assertIn("more than the 102400 a catalog record takes; notes alone is 17000 characters long",
                      str(raised.exception))
        self.assertIn("Shorten the notes", raised.exception.detail)
        fitting = support.make_session(self.home, folder="fits", points=1, notes="\u4e2d" * 16000)
        self.assertEqual(len(self.build(fitting)["notes"]), 16000)


class DarkPairing(unittest.TestCase):
    """Real and simulated darks are never paired with the other kind of spectrum."""

    def setUp(self):
        self.home = support.make_home(self)

    def dark(self, simulated, name=support.DARK_NAME):
        return support.write_dark(self.home, name=name, simulated=simulated)

    def test_the_kind_is_what_the_description_beside_the_dark_says(self):
        dark = self.dark(None)
        self.assertEqual(metadata.dark_kind(dark), ("real", None))
        support.write_dark_description(dark, True, motor=False)
        self.assertEqual(metadata.dark_kind(dark), ("simulated", None))
        # The capture backend decides; the motor program does not take darks.
        support.write_dark_description(dark, False, motor=True)
        self.assertEqual(metadata.dark_kind(dark), ("real", None))
        description = metadata.dark_description(dark)
        self.assertEqual(description.name, "dark_20261113_101400_001.json")
        for content in (b"{half", b"[1, 2]", b'{"simulated": true}', b'{"simulated": {"motor": true}}',
                        b'{"simulated": {"backend": "true"}}', b'{"simulated": {"backend": 1}}', b"\xff\xfe{}", b""):
            description.write_bytes(content)
            kind, why = metadata.dark_kind(dark)
            self.assertEqual(kind, "unknown", content)
            self.assertIn(description.name, why)
        description.unlink()
        description.symlink_to(self.home / "nowhere.json")
        self.assertEqual(metadata.dark_kind(dark), ("unknown", "%s cannot be read" % description.name))
        description.unlink()
        description.mkdir()
        self.assertEqual(metadata.dark_kind(dark)[0], "unknown")

    def test_what_the_controllers_parser_refuses_is_unknown_here_too(self):
        """darkKind() in laser_test/main.cpp reads the description with Qt's parser, which refuses
        some JSON that Python's json module reads. Both must give a dark the same kind."""
        dark = self.dark(True)
        description = metadata.dark_description(dark)

        def with_x(value):
            return '{"x": %s, "simulated": {"backend": true}}' % value

        refused = {
            "NaN": with_x("NaN"),
            "Infinity": with_x("Infinity"),
            "-Infinity, backend false": '{"x": -Infinity, "simulated": {"backend": false}}',
            "beyond a double": with_x("1e400"),
            "beyond a double, negative": with_x("-1.7976931348623159e308"),
            "a whole number beyond a double": with_x("1" + "0" * 400),
            "read as 0 though it is not": with_x("1e-400"),
            "1025 levels": with_x("[" * 1024 + "]" * 1024),
            # Deeper than the json module goes: it raises RecursionError, which must not escape.
            "100000 levels": with_x("[" * 100000 + "]" * 100000),
        }
        for case, content in refused.items():
            description.write_text(content, encoding="utf-8")
            self.assertEqual(metadata.dark_kind(dark), ("unknown", "%s cannot be read" % description.name), case)
        taken = {
            "the largest double": with_x("1.7976931348623157e308"),
            "a whole number beyond 64 bits": with_x("9223372036854775808"),
            "zero with a small exponent": with_x("0e-400"),
            "a subnormal number": with_x("1e-320"),
            "1024 levels": with_x("[" * 1023 + "]" * 1023),
            "half of an emoji in another value": '{"n": "\\ud800", "simulated": {"backend": true}}',
        }
        for case, content in taken.items():
            description.write_text(content, encoding="utf-8")
            self.assertEqual(metadata.dark_kind(dark), ("simulated", None), case)

    def test_a_dark_with_a_suffix_after_its_stamp_has_its_own_description(self):
        dark = self.dark(True, name="dark_20261113_101400_001_1.txt")
        self.assertEqual(metadata.dark_description(dark).name, "dark_20261113_101400_001_1.json")
        self.assertEqual(metadata.dark_kind(dark)[0], "simulated")
        self.assertEqual(metadata.dark_kind(self.dark(None))[0], "real")

    def test_a_capture_takes_only_a_dark_of_its_own_kind(self):
        cases = [
            (True, True, None),
            (False, False, None),
            (True, False, "dark not used: the dark was taken with the real spectrometer; this spectrum is simulated"),
            (False, True, "dark not used: the dark was taken in simulation; this spectrum is real"),
        ]
        for capture_simulated, dark_simulated, reason in cases:
            dark = self.dark(dark_simulated)
            session = support.make_session(self.home, folder="s_%s_%s" % (capture_simulated, dark_simulated),
                                           points=1, simulated=capture_simulated)
            path = session["files"][0]
            built = metadata.build_raw_metadata(self.home, path.parent.name, metadata.read_point(path), path)
            expected = {"file": support.DARK_NAME, "sha256": metadata.sha256_file(dark)[0], "record": None,
                        "kind": "simulated" if dark_simulated else "real"}
            if reason:
                expected["reason"] = reason
            self.assertEqual(built["dark"], expected, (capture_simulated, dark_simulated))

    def test_a_dark_of_unknown_kind_is_paired_with_no_capture(self):
        dark = self.dark(True)
        metadata.dark_description(dark).write_text('{"schema": "raman-dark-description/1"}', encoding="utf-8")
        for simulated, spectrum in ((True, "simulated"), (False, "real")):
            session = support.make_session(self.home, folder="s_%s" % simulated, points=1, simulated=simulated)
            path = session["files"][0]
            built = metadata.build_raw_metadata(self.home, path.parent.name, metadata.read_point(path), path)
            self.assertEqual(built["dark"]["kind"], "unknown")
            self.assertEqual(built["dark"]["reason"],
                             "dark not used: it is not known whether the dark was taken in simulation "
                             "(dark_20261113_101400_001.json does not say whether the backend simulated); "
                             "this spectrum is %s" % spectrum)

    def test_the_backend_decides_for_the_spectrum_too(self):
        self.dark(None)
        session = support.make_session(self.home, points=1)
        path = session["files"][0]
        point = json.loads(path.read_text(encoding="utf-8"))
        # Moved by the motor simulator, measured by the real spectrometer: a real spectrum.
        point["capture"]["simulated"] = {"backend": False, "motor": True}
        path.write_text(json.dumps(point), encoding="utf-8")
        built = metadata.build_raw_metadata(self.home, path.parent.name, metadata.read_point(path), path)
        self.assertNotIn("reason", built["dark"])
        self.assertIs(built["simulated"], True)

    def test_an_imported_session_nobody_recorded_simulated_for_uses_its_dark_as_before(self):
        for number, (kind, simulated) in enumerate((("simulated", True), ("real", None), ("unknown", True))):
            dark = self.dark(simulated, name="dark_20261113_10140%d_001.txt" % number)
            if kind == "unknown":
                metadata.dark_description(dark).write_text("{half", encoding="utf-8")
            session = support.make_session(self.home, folder="imported_%s" % kind, points=1, dark_file=dark.name)
            path = session["files"][0]
            point = json.loads(path.read_text(encoding="utf-8"))
            point["capture"].update(imported=True, simulated=None)
            path.write_text(json.dumps(point), encoding="utf-8")
            built = metadata.build_raw_metadata(self.home, path.parent.name, metadata.read_point(path), path)
            self.assertEqual((built["simulated"], built["dark"]["kind"]), (None, kind))
            self.assertNotIn("reason", built["dark"], kind)


class Completion(unittest.TestCase):
    """Folders recorded by the original project: no session.json, no capture block."""

    def setUp(self):
        self.home = support.make_home(self)
        self.outside = support.make_home(self)
        self.supplied = {"sample_id": "S-OLD", "operator": "Test Operator", "location": "Bench 2", "notes": None,
                         "laser_power_level": None, "simulated": None, "dark_file": None}

    def legacy(self, **options):
        return support.make_session(self.home, root=self.outside, with_capture=False, with_session_json=False,
                                    **options)

    def test_plan_changes_nothing_on_disk(self):
        session = self.legacy(points=2)
        before = {path.name: path.read_bytes() for path in session["folder"].iterdir()}
        plan = metadata.plan_completion(session["folder"], self.supplied)
        self.assertEqual({path.name: path.read_bytes() for path in session["folder"].iterdir()}, before)
        self.assertEqual(plan["session_id"], session["session_id"])
        self.assertEqual([name for name, _capture in plan["changes"]], ["point_0001.json", "point_0002.json"])
        self.assertEqual(plan["errors"], [])

    def test_apply_adds_the_capture_block_and_keeps_every_existing_key(self):
        session = self.legacy(points=2)
        before = [json.loads(path.read_text(encoding="utf-8")) for path in session["files"]]
        metadata.apply_completion(session["folder"], metadata.plan_completion(session["folder"], self.supplied))
        for path, old in zip(session["files"], before):
            new = json.loads(path.read_text(encoding="utf-8"))
            capture = new.pop("capture")
            self.assertEqual(new, old)
            self.assertEqual(capture, {
                "schema": "raman-capture/1", "imported": True, "sample_id": "S-OLD", "operator": "Test Operator",
                "location": "Bench 2", "notes": "", "laser_power_level": None, "integration_time_us": None,
                "instrument": None, "simulated": None, "dark_file": None, "software": None})
            self.assertEqual(metadata.capture_problems(capture), [])

        written = metadata.read_session(session["folder"])
        self.assertEqual(list(written)[:3], ["schema", "session_id", "folder"])
        self.assertEqual((written["session_id"], written["folder"], written["scan_mode"], written["started_utc"]),
                         (session["session_id"], session["folder"].name, "1d-x", "2026-11-13T15:15:02.150Z"))
        self.assertEqual((written["sample_id"], written["operator"], written["location"]),
                         ("S-OLD", "Test Operator", "Bench 2"))
        self.assertEqual(written["imported"]["source_folder"], str(session["folder"]))
        # Nothing is left to do the second time.
        again = metadata.plan_completion(session["folder"], self.supplied)
        self.assertEqual((again["changes"], again["session"]), ([], None))

    def test_imported_record_metadata_says_so_and_leaves_the_unknown_null(self):
        session = self.legacy(points=1)
        metadata.apply_completion(session["folder"], metadata.plan_completion(session["folder"], self.supplied))
        path = session["files"][0]
        built = metadata.build_raw_metadata(self.home, session["folder"].name, metadata.read_point(path), path)
        self.assertIs(built["imported"], True)
        self.assertEqual((built["simulated"], built["laser_power_level"], built["software"], built["dark"]),
                         (None, None, None, None))

    def test_optional_values_are_used_when_given(self):
        session = self.legacy(points=1)
        support.write_dark(self.home)
        supplied = dict(self.supplied, notes="from the 2026 campaign", laser_power_level="LOW", simulated=True,
                        dark_file=support.DARK_NAME)
        metadata.apply_completion(session["folder"], metadata.plan_completion(session["folder"], supplied))
        capture = json.loads(session["files"][0].read_text(encoding="utf-8"))["capture"]
        self.assertEqual((capture["notes"], capture["laser_power_level"], capture["dark_file"]),
                         ("from the 2026 campaign", "LOW", support.DARK_NAME))
        self.assertEqual(capture["simulated"], {"backend": True, "motor": True})
        self.assertEqual(metadata.read_session(session["folder"])["simulated"], {"backend": True, "motor": True})

    def test_values_already_in_the_files_win_over_supplied_ones(self):
        session = support.make_session(self.home, root=self.outside, points=2, sample_id="S-REAL")
        point = json.loads(session["files"][1].read_text(encoding="utf-8"))
        point["capture"]["operator"] = ""
        session["files"][1].write_text(json.dumps(point), encoding="utf-8")
        supplied = dict(self.supplied, operator="Somebody Else", laser_power_level="LOW")
        plan = metadata.plan_completion(session["folder"], supplied)
        self.assertEqual([name for name, _capture in plan["changes"]], ["point_0002.json"])
        self.assertIsNone(plan["session"])
        capture = plan["changes"][0][1]
        # The sample comes from the point itself, the operator from session.json; neither from the options.
        self.assertEqual((capture["sample_id"], capture["operator"], capture["laser_power_level"]),
                         ("S-REAL", "Test Operator", "HIGH"))

    def test_missing_required_fields_are_named_as_options(self):
        session = self.legacy(points=1)
        supplied = dict(self.supplied, operator=None, location=None)
        with self.assertRaises(MetadataError) as raised:
            metadata.plan_completion(session["folder"], supplied)
        self.assertIn("--operator, --location", str(raised.exception))
        self.assertNotIn("--sample-id", str(raised.exception))
        self.assertFalse((session["folder"] / "session.json").exists())

    def test_scan_mode_falls_back_to_the_folder_name(self):
        session = self.legacy(folder="2d_scan_20260918_174032_408", points=1)
        point = json.loads(session["files"][0].read_text(encoding="utf-8"))
        del point["scan_mode"]
        session["files"][0].write_text(json.dumps(point), encoding="utf-8")
        self.assertEqual(metadata.plan_completion(session["folder"], self.supplied)["session"]["scan_mode"], "2d")
        self.assertIsNone(metadata.scan_mode_from_folder("0de3e85c-b212-4bc1-b754-a875333dcc93"))

    def test_folders_that_are_not_one_scan_session_are_refused(self):
        empty = self.outside / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(MetadataError, "not a scan session"):
            metadata.plan_completion(empty, self.supplied)
        with self.assertRaisesRegex(MetadataError, "not a scan session"):
            metadata.plan_completion(self.outside / "does_not_exist", self.supplied)

        mixed = self.legacy(folder="mixed", points=1)
        other = support.point_dict("another-session", 2)
        support.write_point(mixed["folder"], other)
        with self.assertRaisesRegex(MetadataError, "more than one session"):
            metadata.plan_completion(mixed["folder"], self.supplied)

        summaries = self.outside / "summaries"
        summaries.mkdir()
        (summaries / "point_0001.json").write_text(json.dumps({"capture_id": "x_1", "label": "r6g"}), encoding="utf-8")
        with self.assertRaisesRegex(MetadataError, "No point file .* is a raw spectrum"):
            metadata.plan_completion(summaries, self.supplied)

    def test_unusable_point_files_are_reported_and_the_rest_completed(self):
        session = self.legacy(points=2)
        (session["folder"] / "point_0003.json").write_text("{broken", encoding="utf-8")
        plan = metadata.plan_completion(session["folder"], self.supplied)
        self.assertEqual(len(plan["changes"]), 2)
        self.assertEqual([entry["file"] for entry in plan["errors"]], ["point_0003.json"])

    def test_a_point_with_a_broken_character_is_reported_not_written_again(self):
        session = self.legacy(points=2)
        point = json.loads(session["files"][1].read_text(encoding="utf-8"))
        point["started_utc"] = "2026-11-13T15:15:03.150Z\ud83d"
        session["files"][1].write_text(json.dumps(point), encoding="utf-8")
        before = session["files"][1].read_bytes()
        plan = metadata.plan_completion(session["folder"], self.supplied)
        self.assertEqual([name for name, _capture in plan["changes"]], ["point_0001.json"])
        self.assertEqual(plan["errors"],
                         [{"file": "point_0002.json", "message": "started_utc holds a broken character"}])
        metadata.apply_completion(session["folder"], plan)
        self.assertEqual(session["files"][1].read_bytes(), before)
        self.assertEqual(metadata.read_session(session["folder"])["started_utc"], "2026-11-13T15:15:02.150Z")


if __name__ == "__main__":
    unittest.main()
